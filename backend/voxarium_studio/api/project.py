"""Project metadata, settings and theme (§7, §11.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..build_info import build_info, changelog_path
from ..core.config import (
    ConcurrencyConfig,
    DocsConfig,
    TeamsConfig,
    ThemeConfig,
    UpdatesConfig,
    normalise_project_code,
    resolve_launch_root,
)
from ..core.context import AppContext
from ..core.prompts import SPECS
from ..core.task_counts import read_task_counts
from ..core.workspace import (
    ProjectNotFound,
    ProjectNotInitialised,
    forget_recent,
    load_recent,
    open_project,
)
from ..teams.backends.base import available_backends, get_backend
from ..vcs import candidate_backends
from .deps import Ctx

router = APIRouter(tags=["project"])


class SettingsUpdate(BaseModel):
    """A patch, not a replacement — see :func:`update_settings` for why that matters."""

    project_name: str | None = None
    project_code: str | None = None
    """Short code in front of every task id (§10.2). ``""`` clears it back to the derived
    one — the only field here where an empty string is a value rather than a mistake."""

    theme: ThemeConfig | None = None
    concurrency: ConcurrencyConfig | None = None
    teams: TeamsConfig | None = None
    docs: DocsConfig | None = None
    updates: UpdatesConfig | None = None
    node_cache_enabled: bool | None = None
    show_data_dir: bool | None = None


class OpenProjectRequest(BaseModel):
    path: str
    create: bool = True
    """Initialise ``voxarium_studio/`` when the folder is not a project yet. The dialog sends
    ``false`` first so it can show what is about to be created, then ``true``."""


@router.get("/project")
async def get_project(ctx: Ctx) -> dict[str, Any]:
    return {
        "root": str(ctx.paths.root),
        "name": ctx.config.project_name or ctx.paths.root.name,
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


@router.get("/project/recent")
def get_recent_projects() -> list[dict[str, Any]]:
    """Projects this machine has opened before, newest first (§5)."""
    return [entry.to_json() for entry in load_recent()]


@router.delete("/project/recent")
def forget_recent_project(path: str) -> list[dict[str, Any]]:
    return [entry.to_json() for entry in forget_recent(path)]


@router.get("/project/task-counts")
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
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not open the project: {exc}") from exc
    return await get_project(ctx)


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
async def get_settings(ctx: Ctx) -> dict[str, Any]:
    return ctx.config.model_dump(mode="json")


@router.get("/settings/prompts")
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
async def update_settings(payload: SettingsUpdate, ctx: Ctx) -> dict[str, Any]:
    """Persist settings to config.yaml so they travel with the project.

    The nested sections **merge**, the same way `PATCH …/members/{id}` does. They are full
    models with defaults for every field, so assigning one wholesale turns a partial body
    into a reset: `{"theme": {"radius": "round"}}` would validate into a ThemeConfig whose
    `mode` is the default `dark`, and setting the corners would quietly undo light mode.
    `exclude_unset` is what distinguishes "the client sent this" from "this is the
    default", so an omitted field keeps whatever is already in config.yaml.
    """
    if payload.project_name is not None:
        ctx.config.project_name = payload.project_name
    if payload.project_code is not None:
        # Blank means "go back to the derived code", so it is stored as null rather than as
        # an empty string a later reader would have to special-case (§10.2).
        try:
            ctx.config.project_code = normalise_project_code(payload.project_code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.theme is not None:
        ctx.config.theme = ctx.config.theme.model_copy(
            update=payload.theme.model_dump(exclude_unset=True)
        )
    if payload.concurrency is not None:
        ctx.config.concurrency = ctx.config.concurrency.model_copy(
            update=payload.concurrency.model_dump(exclude_unset=True)
        )
        ctx.jobs.update_limits(ctx.config.concurrency.as_dict())
    if payload.teams is not None:
        patch = payload.teams.model_dump(exclude_unset=True)
        if "commit_backend" in patch:
            from voxarium_studio.teams.backends.base import available_backends

            if patch["commit_backend"] not in available_backends():
                raise HTTPException(status_code=400, detail="Unknown commit description backend")
        if "launch_root" in patch:
            patch["launch_root"] = _checked_launch_root(ctx, patch["launch_root"])
        prompts_changed = "prompts" in patch
        if prompts_changed:
            # The same partial-patch rule as the sections around it, one level down:
            # `model_copy` does not validate, so without this the nested section would be
            # replaced by a raw dict holding only the fields the client sent.
            patch["prompts"] = ctx.config.teams.prompts.model_copy(update=patch["prompts"])
        ctx.config.teams = ctx.config.teams.model_copy(update=patch)
        if prompts_changed:
            # The identity templates are baked into `.claude/agents/*.md` (§10.3), so an
            # edit here has to reach the files a terminal reads too. Unchanged definitions
            # are left alone, mtime and all.
            await ctx.teams.sync_agent_definitions()
    if payload.docs is not None:
        ctx.config.docs = ctx.config.docs.model_copy(
            update=payload.docs.model_dump(exclude_unset=True)
        )
    if payload.updates is not None:
        ctx.config.updates = ctx.config.updates.model_copy(
            update=payload.updates.model_dump(exclude_unset=True)
        )
        # The poller is already running against the old setting, so the service is told
        # rather than left to find out at the next project switch (§12.1).
        await ctx.updates.configure(ctx.config.updates)
    if payload.node_cache_enabled is not None:
        ctx.config.node_cache_enabled = payload.node_cache_enabled
    if payload.show_data_dir is not None:
        ctx.config.explorer.show_data_dir = payload.show_data_dir
    ctx.save_config()
    return ctx.config.model_dump(mode="json")


@router.get("/status")
async def get_status(ctx: Ctx, request: Request) -> dict[str, Any]:
    """Health plus agent-backend readiness, shown in Settings before a first agent run."""
    backends = {}
    for name in available_backends():
        available, reason = get_backend(name).is_available()
        backends[name] = {"available": available, "reason": reason}

    # Whether each system's command-line tool is installed — what a commit node needs
    # before it will do anything, and the same shape as the agent backends above.
    vcs_backends = {}
    for backend_cls in candidate_backends(ctx.registry.vcs_backends()):
        backend = backend_cls(ctx.paths.root, ctx.vcs_settings(backend_cls.id))
        available, reason = backend.is_available()
        vcs_backends[backend_cls.id] = {
            "name": backend_cls.name,
            "available": available,
            "reason": reason,
            "active": ctx.vcs is not None and ctx.vcs.id == backend_cls.id,
        }
    return {
        "database": await ctx.db.healthcheck(),
        "event_seq": request.app.state.browser_bus.current_seq,
        "ws_clients": request.app.state.browser_bus.subscriber_count,
        "jobs": [
            {"id": job.id, "kind": job.kind, "meta": job.meta} for job in ctx.jobs.jobs()
        ],
        "agent_backends": backends,
        "vcs_backends": vcs_backends,
    }


@router.post("/settings/reload")
async def reload_everything(ctx: Ctx) -> dict[str, Any]:
    """Re-read config.yaml and re-scan plugins, for when files were edited by hand."""
    try:
        ctx.reload_config()
        await ctx.updates.configure(ctx.config.updates)
        await ctx.reload_plugins()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "config": ctx.config.model_dump(mode="json"),
        "plugins": [p.to_json() for p in ctx.registry.plugins()],
    }
