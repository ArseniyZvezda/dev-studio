"""Project metadata, settings and theme (§7, §11.1)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from ..build_info import build_info, changelog_path
from ..core.config import (
    ApplicationThemeConfig,
    ConcurrencyConfig,
    DocsConfig,
    TeamsConfig,
    ThemeConfig,
    UpdatesConfig,
    normalise_project_code,
    resolve_launch_root,
    save_config,
)
from ..core.application_config import (
    ApplicationConfig,
    apply_application_config,
    load_application_config,
    save_application_config,
)
from ..core.context import AppContext
from ..core.prompts import SPECS, AgentPromptsConfig
from ..core.project_identity import identity_migration_plan
from ..core.task_counts import read_task_counts
from ..core.workspace import (
    ProjectNotFound,
    ProjectNotInitialised,
    forget_recent,
    load_recent,
    ensure_project,
    open_project,
    workspace_activity,
)
from ..teams.backends.base import available_backends, get_backend
from ..teams.backend_usage import get_usage_limits
from ..vcs import candidate_backends
from .deps import Ctx

router = APIRouter(tags=["project"])
# Only project operations are mounted again under an immutable selector (§5.3.3).
scoped_router = APIRouter(tags=["project"])
projects_router = APIRouter(prefix="/projects", tags=["projects"])


class ThemeUpdate(ThemeConfig):
    # §5.2.1: reject unknown request fields, without tightening historical disk
    # schemas. Inherit their fields/validators; free-form maps stay free-form.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ApplicationThemeUpdate(ApplicationThemeConfig):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ConcurrencyUpdate(ConcurrencyConfig):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class AgentPromptsUpdate(AgentPromptsConfig):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class TeamsUpdate(TeamsConfig):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    prompts: AgentPromptsUpdate = Field(default_factory=AgentPromptsUpdate)


class DocsUpdate(DocsConfig):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class UpdatesUpdate(UpdatesConfig):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def _validate_orchestrator_models(value: Any) -> dict[str, dict[str, str]]:
    # §10.11: share disk/runtime validation; never accept a recursive Auto cell.
    return ApplicationConfig(orchestrator_models=value).orchestrator_models


OrchestratorModelsUpdate = Annotated[
    dict[str, dict[str, str]], BeforeValidator(_validate_orchestrator_models),
]


class SettingsUpdate(BaseModel):
    """A patch, not a replacement — see :func:`update_settings` for why that matters."""

    # Identity/target edits must not sneak through the compatibility PUT (§5.2.1).
    model_config = ConfigDict(extra="forbid")

    project_name: str | None = None
    project_code: str | None = None
    """Short code in front of every task id (§10.2). ``""`` clears it back to the derived
    one — the only field here where an empty string is a value rather than a mistake."""

    theme: ThemeUpdate | None = None
    concurrency: ConcurrencyUpdate | None = None
    teams: TeamsUpdate | None = None
    docs: DocsUpdate | None = None
    updates: UpdatesUpdate | None = None
    node_cache_enabled: bool | None = None
    show_data_dir: bool | None = None
    language: Literal["en", "ru"] | None = None
    respond_language: str | None = None
    orchestrator_models: OrchestratorModelsUpdate | None = None


class ApplicationSettingsUpdate(BaseModel):
    """Settings can only change application preferences, never project fields (§5.2)."""

    model_config = ConfigDict(extra="forbid")

    theme: ApplicationThemeUpdate | None = None
    updates: UpdatesUpdate | None = None
    node_cache_enabled: bool | None = None
    show_data_dir: bool | None = None
    language: Literal["en", "ru"] | None = None
    respond_language: str | None = None
    orchestrator_models: OrchestratorModelsUpdate | None = None


class OpenProjectRequest(BaseModel):
    path: str
    create: bool = True
    """Initialise ``.voxarium_studio/`` when the folder is not a project yet. The dialog sends
    ``false`` first so it can show what is about to be created, then ``true``."""


class EnsureProjectRequest(OpenProjectRequest):
    # Restore/open must not initialise a directory without explicit confirmation (§5.3.2).
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    create: bool = False


class ProjectThemeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accent: str = "#a394e8"


class ProjectSettingsUpdate(BaseModel):
    """Project saves cannot silently modify user-wide preferences (§5.2, §5.3.3)."""

    model_config = ConfigDict(extra="forbid")
    project_name: str | None = None
    project_code: str | None = None
    theme: ProjectThemeUpdate | None = None
    concurrency: ConcurrencyUpdate | None = None
    teams: TeamsUpdate | None = None
    docs: DocsUpdate | None = None


@router.get("/project")
async def get_project(ctx: Ctx) -> dict[str, Any]:
    return {
        "root": str(ctx.paths.root),
        "name": ctx.config.project_name or ctx.paths.root.name,
        "persistent_id": ctx.config.project_identity.id if ctx.config.project_identity else None,
        "data_dir": str(ctx.paths.data),
        "is_git_repo": ctx.is_git_repo,
        "vcs": ctx.vcs.to_json() if ctx.vcs is not None else None,
        "auth_required": ctx.auth.enabled,
        # The Studio's own build, not the project's. This is the call every client makes
        # before it renders anything, which is why the version rides along with it.
        "version": build_info().to_json(),
        "config": ctx.config.model_dump(mode="json"),
        "concurrency_limits": ctx.jobs.limits,
        "counts": {
            "plugins": len(ctx.registry.plugins()),
            "node_types": len(ctx.registry.node_types()),
            "pipelines": len(ctx.pipelines.list()),
        },
    }


@router.get("/remote/migration")
@scoped_router.get("/remote/migration")
async def get_identity_migration(ctx: Ctx, response: Response) -> dict[str, Any]:
    """S1 diagnostics only: no profiles, resolution, sync or apply (§16.8)."""
    response.headers["Cache-Control"] = "no-store"
    return identity_migration_plan(ctx.paths).model_dump(mode="json")


@scoped_router.get("/project")
async def get_scoped_project(ctx: Ctx, request: Request) -> dict[str, Any]:
    scope = request.state.project_scope
    return {
        **await get_project(ctx), "root": str(scope.root),
        "project_id": scope.project_id, "epoch": scope.epoch,
    }


@router.get("/project/recent")
def get_recent_projects() -> list[dict[str, Any]]:
    """Projects this machine has opened before, newest first (§5)."""
    return [entry.to_json() for entry in load_recent()]


@router.delete("/project/recent")
def forget_recent_project(path: str) -> list[dict[str, Any]]:
    return [entry.to_json() for entry in forget_recent(path)]


@router.get("/project/task-counts")
@scoped_router.get("/project/task-counts")
def get_project_task_counts(ctx: Ctx) -> dict[str, int] | None:
    """The current project's footer summary, across all teams (§5.1)."""
    return read_task_counts(ctx.paths.root)


@router.get("/changelog")
async def get_changelog() -> dict[str, Any]:
    """What each release of the Studio changed (§12.1).

    The Studio's own file, never the managed project's — it sits beside ``version`` in the
    rail and answers the other half of the same question: which build is this, and what
    came with it. Served as its Markdown source, because the client renders documents
    itself and there is no second renderer here to keep in step.
    """
    path = changelog_path()
    if path is None:
        raise HTTPException(status_code=404, detail="This build shipped without a changelog.")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - a file that vanished between the two calls
        raise HTTPException(status_code=404, detail=f"Cannot read the changelog: {exc}") from exc
    return {"path": str(path), "text": text}


@router.post("/project/open")
async def open_another_project(payload: OpenProjectRequest, request: Request) -> dict[str, Any]:
    """Point the Studio at another managed project, without restarting it.

    The previous project's services and tasks keep running in its own context (§5.1). Clients
    learn about it from the `project.opened` event rather than from this response, so the
    tab that did not click the button reloads too.
    """
    try:
        ctx = await open_project(request.app.state, payload.path, create=payload.create)
    except ProjectNotInitialised as exc:
        raise HTTPException(
            status_code=409,
            detail=f"{exc} is not a Voxarium Studio project yet.",
        ) from exc
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError:
        # §5.2.1: stored config may contain rejected credentials, even in identity.
        raise HTTPException(status_code=400, detail="Invalid project configuration.") from None
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not open the project: {exc}") from exc
    return await get_project(ctx)


@projects_router.get("/activity")
async def get_workspace_activity(request: Request, response: Response) -> dict[str, Any]:
    """App-level read-only quit/updater guard, including projects without tabs (§5.3).

    Deliberately not mounted under a project selector. Keep this async so the
    synchronous snapshot runs on the owners' event loop, not in a worker thread.
    The parent API router supplies the same auth/Host/Origin guards as open.
    """
    response.headers["Cache-Control"] = "no-store"
    return workspace_activity(request.app.state)


@projects_router.post("/open")
async def ensure_open_project(payload: EnsureProjectRequest, request: Request) -> dict[str, Any]:
    """Ensure a resident context without touching the legacy browser adapter (§5.3.2)."""
    try:
        scope = await ensure_project(request.app.state, payload.path, create=payload.create)
    except ProjectNotInitialised as exc:
        raise HTTPException(409, f"{exc} is not a Voxarium Studio project yet.") from exc
    except ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValidationError:
        raise HTTPException(400, "Invalid project configuration.") from None
    except (OSError, ValueError) as exc:
        raise HTTPException(400, f"Could not open the project: {exc}") from exc
    metadata = await get_project(scope.ctx)
    return {
        **metadata, "root": str(scope.root),
        "project_id": scope.project_id, "epoch": scope.epoch,
    }


def _checked_launch_root(ctx: AppContext, value: str) -> str:
    """Refuse a launch root that names nothing, before it reaches an agent session.

    Only existence is checked. Pointing *outside* the project is the whole reason the
    setting exists — a game whose Studio project is one folder of a larger checkout — so
    it is allowed, and the cost (no isolated checkouts, §10.5) is the orchestrator's to
    report on the task it changes rather than this endpoint's to refuse.
    """
    text = (value or ".").strip() or "."
    resolved = resolve_launch_root(ctx.paths.root, text)
    if not resolved.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"{resolved} is not a directory. Paths are relative to the project root.",
        )
    return text


@router.get("/settings")
@scoped_router.get("/settings")
async def get_settings(ctx: Ctx) -> dict[str, Any]:
    return ctx.config.model_dump(mode="json")


@router.get("/settings/prompts")
@scoped_router.get("/settings/prompts")
async def get_prompt_specs() -> dict[str, Any]:
    """The built-in text behind every line the Studio says to an agent (§10.3, §10.4).

    Static by design — no project state in it: this is what the Settings page shows under
    each override stored in ``teams.prompts``, and what Reset returns a field to. Keyed and
    ordered as the fragments are assembled into a session's context.
    """
    return {
        key: {
            "default": spec.default,
            "placeholders": list(spec.placeholders),
            "required": spec.required,
        }
        for key, spec in SPECS.items()
    }


@router.put("/settings")
async def update_settings(payload: SettingsUpdate, request: Request, ctx: Ctx) -> dict[str, Any]:
    """Compatibility endpoint: route each field to its owning config (§5.2)."""
    async with _settings_lock(request):
        return await _save_settings(payload, ctx)


@scoped_router.put("/settings")
async def update_project_settings(
    payload: ProjectSettingsUpdate, request: Request, ctx: Ctx,
) -> dict[str, Any]:
    async with _settings_lock(request):
        return await _save_settings(
            SettingsUpdate.model_validate(payload.model_dump(exclude_unset=True)), ctx,
            application=ApplicationConfig.from_effective(ctx.config),
        )


@router.get("/settings/application")
async def get_application_settings() -> dict[str, Any]:
    return load_application_config().model_dump(mode="json", exclude={"api_backends"})


@router.get("/settings/api-backends")
async def list_api_backends() -> list[dict[str, Any]]:
    return [item.public() for item in load_application_config().api_backends]


@router.put("/settings/api-backends/{connection_id}")
async def put_api_backend(connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from ..core.api_backends import ApiBackendConfig
    from ..core.application_config import (
        application_config_transaction, _load_application_document, _write_application_document,
    )
    with application_config_transaction():
        try:
            raw, application = _load_application_document()
            existing = next((c for c in application.api_backends if c.id == connection_id), None)
            values = {**payload, "id": connection_id}
            if "api_key" not in values and existing:
                values["api_key"] = existing.api_key
            if "context_window" not in values and existing:
                values["context_window"] = existing.context_window
            for field in ("models", "api_mode"):
                if field not in values and existing:
                    values[field] = getattr(existing, field)
            connection = ApiBackendConfig.model_validate(values)
            items = [connection if c.id == connection_id else c for c in application.api_backends]
            if existing is None:
                items.append(connection)
            # Secrets are serialized only here, never into generic settings or DTOs.
            raw["api_backends"] = [
                {**c.model_dump(mode="json", exclude={"api_key"}),
                 "api_key": c.api_key.get_secret_value()} for c in items
            ]
            _write_application_document(raw)
        except (ValueError, OSError):
            raise HTTPException(400, "Could not save API backend. Check its name, URL and model.") from None
    return connection.public()


@router.delete("/settings/api-backends/{connection_id}")
async def delete_api_backend(connection_id: str) -> dict[str, bool]:
    from ..core.application_config import (
        application_config_transaction, _load_application_document, _write_application_document,
    )
    with application_config_transaction():
        try:
            raw, application = _load_application_document()
            if not any(c.id == connection_id for c in application.api_backends):
                raise HTTPException(404, "API backend not found")
            raw["api_backends"] = [c for c in raw.get("api_backends", []) if c["id"] != connection_id]
            _write_application_document(raw)
        except (ValueError, OSError):
            raise HTTPException(400, "Could not remove API backend.") from None
    return {"deleted": True}


@router.put("/settings/application")
async def update_application_settings(
    payload: ApplicationSettingsUpdate, request: Request, ctx: Ctx,
) -> dict[str, Any]:
    """Global-only endpoint; accepts no project code, accent or agent settings."""
    async with _settings_lock(request):
        return await _save_settings(
            SettingsUpdate.model_validate(payload.model_dump(exclude_unset=True)), ctx,
        )


def _settings_lock(request: Request) -> asyncio.Lock:
    # A replacement reads the application config before its awaited startup, and joins
    # work_contexts only afterwards (§5.1). Share the switch lock so a global save cannot
    # miss that in-flight context and leave it with stale preferences or update policy.
    return request.app.state.project_lock


async def _save_settings(
    payload: SettingsUpdate, ctx: AppContext, *, application: ApplicationConfig | None = None,
) -> dict[str, Any]:
    """Validate the entire patch before touching live state or either file (§5.2).

    Nested models merge only explicitly supplied fields. Defaults in a one-field request
    must not reset the rest of the user's preferences or the project's configuration.
    """
    draft = ctx.config.model_copy(deep=True)
    application = application if application is not None else load_application_config()
    project_changed = False
    application_changed = False
    prompts_changed = False
    if payload.project_name is not None:
        draft.project_name = payload.project_name
        project_changed = True
    if payload.project_code is not None:
        try:
            draft.project_code = normalise_project_code(payload.project_code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        project_changed = True
    if payload.theme is not None:
        patch = payload.theme.model_dump(exclude_unset=True)
        if "accent" in patch:
            draft.theme.accent = patch.pop("accent")
            project_changed = True
        if patch:
            application.theme = application.theme.model_copy(update=patch)
            application_changed = True
    if payload.concurrency is not None:
        draft.concurrency = draft.concurrency.model_copy(
            update=payload.concurrency.model_dump(exclude_unset=True)
        )
        project_changed = True
    if payload.teams is not None:
        patch = payload.teams.model_dump(exclude_unset=True)
        from ..teams.required_context import checked_documents, checked_skills
        try:
            if "required_skills" in patch:
                patch["required_skills"] = checked_skills(
                    ctx.skills, patch["required_skills"], draft.teams.required_skills)
            if "required_documents" in patch:
                patch["required_documents"] = checked_documents(
                    ctx.teams.docs, patch["required_documents"], draft.teams.required_documents)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if "commit_backend" in patch:
            if patch["commit_backend"] not in available_backends():
                raise HTTPException(status_code=400, detail="Unknown commit description backend")
        if "launch_root" in patch:
            patch["launch_root"] = _checked_launch_root(ctx, patch["launch_root"])
        prompts_changed = "prompts" in patch
        if prompts_changed:
            patch["prompts"] = draft.teams.prompts.model_copy(update=patch["prompts"])
        draft.teams = draft.teams.model_copy(update=patch)
        project_changed = True
    if payload.docs is not None:
        draft.docs = draft.docs.model_copy(
            update=payload.docs.model_dump(exclude_unset=True)
        )
        project_changed = True
    if payload.updates is not None:
        application.updates = application.updates.model_copy(
            update=payload.updates.model_dump(exclude_unset=True)
        )
        application_changed = True
    if payload.node_cache_enabled is not None:
        application.node_cache_enabled = payload.node_cache_enabled
        application_changed = True
    if payload.show_data_dir is not None:
        application.show_data_dir = payload.show_data_dir
        application_changed = True
    if "language" in payload.model_fields_set:
        application.language = payload.language
        application_changed = True
    if payload.respond_language is not None:
        application.respond_language = payload.respond_language
        application_changed = True
    if payload.orchestrator_models is not None:
        application.orchestrator_models = payload.orchestrator_models
        application_changed = True

    apply_application_config(draft, application)
    if project_changed:
        save_config(ctx.paths, draft)
    if application_changed:
        save_application_config(application)
    # Services share this AppConfig object. Keep its identity when committing the draft.
    for name in type(draft).model_fields:
        setattr(ctx.config, name, getattr(draft, name))
    if payload.concurrency is not None:
        ctx.jobs.update_limits(ctx.config.concurrency.as_dict())
    if application_changed:
        await _configure_application(ctx, application)
    if prompts_changed:
        await ctx.teams.sync_agent_definitions()
    return ctx.config.model_dump(mode="json")


async def _configure_application(ctx: AppContext, application: ApplicationConfig) -> None:
    # Projects continue running in the background (§5.1). All resident contexts must
    # adopt global preferences now, not only after reopening or restarting the Studio.
    contexts = ctx.work_contexts()
    for resident in contexts:
        apply_application_config(resident.config, application)
    for resident in contexts:
        await resident.updates.configure(resident.config.updates)


@router.get("/status")
@scoped_router.get("/status")
async def get_status(ctx: Ctx, request: Request) -> dict[str, Any]:
    """Health plus agent-backend readiness, shown in Settings before a first agent run."""
    backends = {}
    for name in available_backends():
        available, reason = get_backend(name).is_available()
        backends[name] = {
            "available": available, "reason": reason,
            "usage_limits": get_usage_limits(name),
        }

    # Whether each system's command-line tool is installed — what a commit node needs
    # before it will do anything, and the same shape as the agent backends above.
    vcs_backends = {}
    for backend_cls in candidate_backends():
        backend = backend_cls(ctx.paths.root, ctx.vcs_settings(backend_cls.id))
        available, reason = backend.is_available()
        vcs_backends[backend_cls.id] = {
            "name": backend_cls.name,
            "available": available,
            "reason": reason,
            "active": ctx.vcs is not None and ctx.vcs.id == backend_cls.id,
        }
    bus = ctx.bus if hasattr(request.state, "project_scope") else request.app.state.browser_bus
    return {
        "database": await ctx.db.healthcheck(),
        "event_seq": bus.current_seq,
        "ws_clients": bus.subscriber_count,
        "jobs": [
            {"id": job.id, "kind": job.kind, "meta": job.meta} for job in ctx.jobs.jobs()
        ],
        "agent_backends": backends,
        "vcs_backends": vcs_backends,
    }


@router.post("/settings/reload")
@scoped_router.post("/settings/reload")
async def reload_everything(request: Request, ctx: Ctx) -> dict[str, Any]:
    """Re-read config.yaml and re-scan plugins, for when files were edited by hand."""
    try:
        async with _settings_lock(request):
            scoped = hasattr(request.state, "project_scope")
            application = ApplicationConfig.from_effective(ctx.config) if scoped else None
            try:
                ctx.reload_config()
            except ValidationError:
                # §5.2.1: only config validation is redacted, not plugin/sync errors.
                # The outer 400 handler can safely stringify this fixed message.
                raise ValueError("Invalid project configuration.") from None
            if application is not None:
                # A project reload is not a global-preferences reload (§5.3.3).
                apply_application_config(ctx.config, application)
                await ctx.updates.configure(ctx.config.updates)
            # §10.11: hand-edited prompts also refresh the Producer's derived responsibility.
            await ctx.teams.sync_agent_definitions()
            if not scoped:
                await _configure_application(ctx, load_application_config())
            await ctx.reload_plugins()
            # §16.5: a hand-edited address or space takes effect like any other setting.
            await ctx.sync.reconfigure()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "config": ctx.config.model_dump(mode="json"),
        "plugins": [p.to_json() for p in ctx.registry.plugins()],
    }
