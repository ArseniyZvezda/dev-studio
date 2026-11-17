"""Online sync settings and status (§16.5).

Kept apart from ``PUT /api/settings`` because one of its fields is not a setting of the
project at all: the key is the user's and is stored in their own config directory. This
endpoint takes it, stores it there, and never gives it back — a status says only whether one
is stored.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from ..core.config import save_config
from ..core.project_identity import ProjectIdentity, _PlanConfigLoader
from ..explorer.service import PathOutsideProject
from ..sync import credentials
from ..sync.protocol import check_space_name, normalise_url
from ..sync.transitions import MutationGateError
from ..ws.events import EventType
from .deps import Ctx

class FencedRoute(APIRoute):
    """I4: fencing errors are typed, and sensitive responses are never cached.

    Wrap dependency resolution too: auth, invalid handles and validation must
    receive the same no-store policy as a successful editor lease response.
    Documents retain their existing read/asset caching policy.
    """

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            sensitive = "/docs" not in self.path or request.method not in {"GET", "HEAD"}
            try:
                response = await original(request)
            except MutationGateError as exc:
                code = "unknown_sid" if exc.code == "unknown_collab_session" else exc.code
                response = JSONResponse({"code": code, "details": {}}, status_code=409)
                sensitive = True
            except HTTPException as exc:
                if isinstance(exc.detail, dict) and "code" in exc.detail:
                    return JSONResponse(exc.detail, status_code=exc.status_code,
                                        headers={"Cache-Control": "no-store"})
                if sensitive:
                    exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            except RequestValidationError:
                if not sensitive:
                    raise
                # Do not echo arbitrary submitted tokens or invalid lease bodies.
                return JSONResponse({"code": "invalid_request", "details": {}},
                                    status_code=422, headers={"Cache-Control": "no-store"})
            if sensitive:
                response.headers["Cache-Control"] = "no-store"
            return response

        return handler

    async def handle(self, scope, receive, send) -> None:
        if self.methods and scope["method"] not in self.methods:
            await JSONResponse({"code": "method_not_allowed", "details": {}}, status_code=405,
                               headers={"Cache-Control": "no-store",
                                        "Allow": ", ".join(sorted(self.methods))})(scope, receive, send)
            return
        await super().handle(scope, receive, send)


async def bind_http_editor_inventory(request: Request, ctx: Ctx) -> None:
    # Resolve only an already-resident Ctx. The callback reads the shared legacy
    # browser inventory at cutover time, not just its initial hello/selection.
    from ..ws.routes import bind_editor_inventory

    bind_editor_inventory(request.app.state, ctx)


router = APIRouter(prefix="/sync", tags=["sync"], route_class=FencedRoute,
                   dependencies=[Depends(bind_http_editor_inventory)])
# Only mounted below /api/projects/{project_id}; never a global profiles bypass.
remote_router = APIRouter(prefix="/remote", tags=["remote-project"], route_class=FencedRoute,
                          dependencies=[Depends(bind_http_editor_inventory)])


def activated_model(ctx: Any) -> bool:
    """Observe only trusted activation evidence, not identity metadata or clients.

    I6 owns durable evidence. I4 consumes the existing I2 seam and latches a
    positive/unreadable observation; later disappearance cannot revive legacy.
    """
    transitions = ctx.docs.transitions
    active = transitions.activated or getattr(ctx.sync, "_activated_model_seen", False)
    try:
        active = active or ctx.remote_sync_activation() is not None
    except Exception:
        active = True
    if active:
        ctx.sync._activated_model_seen = True
        transitions.observe_activated_model()
    return active


class EditorRegister(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    document_path: str
    client_instance_id: str = Field(min_length=1, max_length=200)
    backend_epoch: str
    expected_binding_generation: StrictInt
    sid: str | None = None


class EditorHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    backend_epoch: str
    expected_binding_generation: StrictInt
    client_instance_id: str | None = None
    document_path: str | None = None
    sid: str | None = None
    dirty: bool | None = None
    version: StrictInt | None = None
    last_saved_version: StrictInt | None = None


class EditorMaterialization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    backend_epoch: str
    expected_binding_generation: StrictInt
    document_path: str
    client_instance_id: str
    sid: str
    version: StrictInt
    revision: StrictInt


class EditorMaterializationConfirm(EditorMaterialization):
    proof_id: str = Field(min_length=1, max_length=128)
    content: str


def document_path(ctx: Any, path: str) -> str:
    try:
        return ctx.docs.relative(ctx.docs.resolve(path))
    except PathOutsideProject as exc:
        raise HTTPException(403, "Invalid document path.") from exc
    except ValueError as exc:
        raise HTTPException(400, "Invalid document path.") from exc


@remote_router.get("")
async def get_remote(request: Request, ctx: Ctx) -> dict[str, Any]:
    """Local observation only; no selection, activation, Verify or connection."""
    active = activated_model(ctx)
    identity = ctx.config.project_identity
    scope = request.state.project_scope
    try:
        fingerprint = hashlib.sha256(ctx.paths.config_file.read_bytes()).hexdigest()
    except OSError:
        raise HTTPException(503, "Remote metadata unavailable.") from None
    status = ctx.sync.status()
    state = status["state"]
    try:
        url = normalise_url(ctx.config.sync.url) if ctx.config.sync.url else ""
    except ValueError:
        raise HTTPException(503, "Remote metadata unavailable.") from None
    if not active and (ctx.config.sync.url or ctx.config.sync.space):
        state = "migration_required"
    change = getattr(ctx.docs.transitions, "_change", None)
    change_state = ctx.docs.transitions.change_status(change.change_id) if change is not None else None
    if ctx.docs.transitions.binding_quiescing:
        state = "blocked" if change_state["state"] in {"blocked", "paused"} else "quiescing"
        reason = change_state["reason"]
    else:
        # I12: a runner's fixed typed stop code (recovery_required, *_mismatch…).
        reason = status["message"] if active and state in {"blocked", "unauthorised"} else None
    return {
        "project_id": scope.project_id, "epoch": scope.epoch,
        "backend_epoch": ctx.docs.transitions.backend_epoch,
        "persistent_id": identity.id if identity else None,
        "identity_revision": identity.revision if identity else None,
        "config_fingerprint": fingerprint,
        "url": url, "enabled": ctx.config.sync.enabled,
        "has_token": status["has_token"],
        "binding_generation": ctx.docs.transitions.generation,
        "state": state,
        "reason": reason,
        "active_rooms": len(ctx.sync._sessions),
        "pending_writes": len(ctx.sync._pending) + len(ctx.sync._live_writes),
        "binding_change": change_state,
    }


@remote_router.post("/editors", status_code=201)
async def register_editor(payload: EditorRegister, ctx: Ctx) -> dict[str, Any]:
    activated_model(ctx)
    row = ctx.docs.transitions.register_editor(
        document_path(ctx, payload.document_path), payload.client_instance_id,
        payload.backend_epoch, payload.expected_binding_generation, sid=payload.sid,
    )
    if payload.sid is not None:
        ctx.sync.bind_collab_editor(payload.sid, row["editor_id"])
    if payload.sid is None:
        try:
            # Clean open writes nothing. Only an untouched ordinary lease may
            # receive initial evidence, from the bound server read we return.
            return ctx.docs.transitions.confirm_loaded_server_version(
                row["editor_id"], payload.backend_epoch, payload.expected_binding_generation,
                docs=ctx.docs, document_path=row["document_path"],
                client_instance_id=payload.client_instance_id,
            )
        except FileNotFoundError:
            pass  # New document: no existing server version to acknowledge.
        except (OSError, ValueError):
            raise HTTPException(503, "Remote metadata unavailable.") from None
    return row


def _editor_arguments(ctx: Any, payload: EditorHeartbeat) -> dict[str, Any]:
    args = payload.model_dump()
    if payload.document_path is not None:
        args["document_path"] = document_path(ctx, payload.document_path)
    return args


@remote_router.post("/editors/{editor_id}/heartbeat")
async def heartbeat_editor(editor_id: str, payload: EditorHeartbeat, ctx: Ctx) -> dict[str, Any]:
    activated_model(ctx)
    return ctx.docs.transitions.heartbeat_editor(editor_id, **_editor_arguments(ctx, payload))


@remote_router.delete("/editors/{editor_id}")
async def close_editor(editor_id: str, payload: EditorHeartbeat, ctx: Ctx) -> dict[str, Any]:
    activated_model(ctx)
    return ctx.docs.transitions.close_editor(editor_id, **_editor_arguments(ctx, payload))


@remote_router.post("/editors/{editor_id}/materialization")
async def editor_materialization(editor_id: str, payload: EditorMaterialization, ctx: Ctx) -> dict[str, Any]:
    activated_model(ctx)
    args = payload.model_dump()
    args["document_path"] = document_path(ctx, payload.document_path)
    return ctx.sync.materialization(editor_id, **args)


@remote_router.post("/editors/{editor_id}/materialization/confirm")
async def confirm_editor_materialization(
    editor_id: str, payload: EditorMaterializationConfirm, ctx: Ctx,
) -> dict[str, Any]:
    activated_model(ctx)
    args = payload.model_dump()
    args["document_path"] = document_path(ctx, payload.document_path)
    return ctx.sync.materialization(editor_id, **args)


class RemotePrepare(BaseModel):
    """Read-only intention; no catalog identity supplied by a browser is trusted."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_revision: StrictInt = Field(ge=1)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    persistent_id: str
    operation: Literal["edit_id", "select", "disable", "unbind", "migrate"]
    profile_id: str | None = None
    url: str | None = None
    catalog_id: str | None = None
    catalog_instance_id: str | None = None
    migration_plan_id: str | None = None
    backend_epoch: str | None = None

    @field_validator("persistent_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return ProjectIdentity.canonical_id(value)


class RemoteQuiesce(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_generation: StrictInt


class RemoteEditorAck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    editor_id: str = Field(min_length=1, max_length=256)
    old_generation: StrictInt
    last_saved_version: StrictInt


class RemoteConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    url: str
    persistent_id: str


class RemoteCommit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_revision: StrictInt = Field(ge=1)
    expected_generation: StrictInt
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: RemoteConfirmation


def _remote_refusal(code: str, status: int = 409) -> None:
    raise HTTPException(status, {"code": code, "details": {}},
                        headers={"Cache-Control": "no-store"})


def _prepare_observation(ctx: Any) -> dict[str, Any]:
    """Read owning evidence, never create storage, look up a key or connect (§3.4).

    A copy of this observation is captured by prepare and rechecked at every
    later phase. In particular in-memory config alone is not a CAS fingerprint.
    """
    import yaml
    from ..core.paths import user_config_dir
    from ..core.project_scope import canonical_root
    from ..sync.bindings import RemoteBindings
    from ..sync.profiles import RemoteProfiles

    try:
        home = user_config_dir()
        if home != getattr(ctx.sync, "_remote_home", home):
            raise ValueError
        content = ctx.paths.config_file.read_bytes()
        raw = yaml.load(content.decode("utf-8"), Loader=_PlanConfigLoader)
        if not isinstance(raw, dict) or not isinstance(raw.get("sync", {}), dict):
            raise ValueError
        identity = ProjectIdentity.model_validate(raw.get("project_identity"))
        sync = raw.get("sync", {})
        url = normalise_url(sync.get("url")) if sync.get("url") else ""
        if (identity != ctx.config.project_identity or url != ctx.config.sync.url
                or sync.get("enabled", False) != ctx.config.sync.enabled
                or sync.get("space", "") != ctx.config.sync.space):
            raise ValueError
        private = RemoteBindings().read()
        row = private.get(ctx.paths.root)
        profile = None
        credential = None
        if row is not None:
            selected = RemoteProfiles().get(row.profile_id)
            if (row.url != url or row.persistent_id != identity.id
                    or row.identity_revision != identity.revision or selected.url != row.url):
                raise ValueError
            profile = selected.model_dump()
            credential = {
                "revision": credentials.profile_token_revision(row.profile_id, row.url),
                "source": credentials.profile_token_source(row.profile_id, row.url),
            }
        return {
            "canonical_root": str(canonical_root(ctx.paths.root)),
            "personal_home": str(home),
            "backend_epoch": ctx.docs.transitions.backend_epoch,
            "identity_revision": identity.revision,
            "persistent_id": identity.id,
            "url": url,
            "config_fingerprint": hashlib.sha256(content).hexdigest(),
            "enabled": ctx.config.sync.enabled,
            "private_fingerprint": private.fingerprint,
            "binding": row.model_dump() if row else None,
            "profile": profile, "credential": credential,
        }
    except Exception:
        _remote_refusal("stale_binding", 409)


def _prepare_destination(ctx: Any, payload: RemotePrepare, observed: dict[str, Any],
                         selection: dict | None = None) -> dict[str, Any]:
    # I9 owns authoritative catalog observations. A browser's ID/incarnation
    # is not one; do not probe the v1 server to manufacture a successful plan.
    if payload.operation == "select":
        selection = selection or _select_observation(ctx, payload)
        destination = {"persistent_id": selection["catalog"]["persistent_id"],
                       "url": selection["profile"]["url"], "enabled": False}
        _check_destination_reservation(ctx, observed, destination)
        return destination
    if payload.operation == "migrate":
        _remote_refusal("migration_required", 409)
    if (payload.catalog_id is not None or payload.catalog_instance_id is not None
            or payload.migration_plan_id is not None):
        _remote_refusal("invalid_request", 422)
    binding = observed["binding"]
    if payload.profile_id is not None and (binding is None or payload.profile_id != binding["profile_id"]):
        _remote_refusal("stale_binding")
    if payload.url is not None:
        try:
            matches = (normalise_url(payload.url) if payload.url else "") == observed["url"]
        except ValueError:
            matches = False
        if not matches:
            _remote_refusal("stale_binding")
    if payload.operation != "edit_id" and payload.persistent_id != observed["persistent_id"]:
        _remote_refusal("stale_binding")
    destination = {"persistent_id": payload.persistent_id,
                   "url": "" if payload.operation == "unbind" else observed["url"],
                   "enabled": False}
    # Advisory read of the SAME app-owned registry; prepare never reserves and
    # does not release an incumbent. I6/Enable must perform actual admission.
    _check_destination_reservation(ctx, observed, destination)
    return destination


def _check_destination_reservation(ctx, observed, destination):
    owner = getattr(ctx, "remote_reservations", None)
    if owner is not None and destination["url"]:
        with owner._lock:
            incumbent = owner._reservations.get((destination["url"], destination["persistent_id"]))
            if incumbent is not None and str(incumbent.root) != observed["canonical_root"]:
                _remote_refusal("duplicate_local_binding")


def _select_observation(ctx: Any, payload: RemotePrepare,
                        observed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate one exact Select observation against live local state (I6/I9).

    Production passes ``observed`` from ``_catalog_select_source`` (the app
    owner's strict-v2 read). The injected ``ctx.remote_select_observer`` stays a
    test/in-process override and takes precedence. No request flag, browser
    catalog identity, v1 fallback or fabrication is ever a source; the profile
    and credential are re-read here, so every call is also a CAS recheck.
    """
    from ..core.application_config import RemoteServerProfile
    from ..sync.bindings import BindingRecord
    from ..sync.profiles import RemoteProfiles
    observer = getattr(ctx, "remote_select_observer", None)
    if observer is None and observed is None:
        _remote_refusal("catalog_unavailable", 503)
    try:
        source = (observer(payload.profile_id, payload.persistent_id)
                  if observer is not None else observed)
        if not isinstance(source, dict) or set(source) != {"profile", "credential", "catalog"}:
            raise ValueError
        profile = RemoteServerProfile.model_validate(source["profile"],
                                                     context={"stored_profile_metadata": True})
        catalog = source["catalog"]
        credential = source["credential"]
        if (profile != RemoteProfiles().get(profile.profile_id)
                or profile.profile_id != payload.profile_id
                or payload.url is not None and payload.url != profile.url
                or not isinstance(catalog, dict)
                or set(catalog) != {"persistent_id", "space_instance_id", "snapshot_seq"}
                or catalog["persistent_id"] != payload.persistent_id
                or payload.catalog_id != payload.persistent_id
                or payload.catalog_instance_id != catalog["space_instance_id"]
                or type(catalog["snapshot_seq"]) is not int or catalog["snapshot_seq"] < 0
                or credential != {
                    "revision": credentials.profile_token_revision(profile.profile_id, profile.url),
                    "source": credentials.profile_token_source(profile.profile_id, profile.url)}
                or credential["source"] not in {"stored", "environment"}):
            raise ValueError
        BindingRecord.for_root(ctx.paths.root, profile=profile,
            identity=ProjectIdentity(id=payload.persistent_id, revision=payload.expected_revision),
            selection_state="selected", space_instance_id=catalog["space_instance_id"])
        if payload.migration_plan_id is not None:
            raise ValueError
        return {"profile": profile.model_dump(), "credential": dict(credential),
                "catalog": dict(catalog)}
    except Exception:
        _remote_refusal("stale_binding")


# Local/browser staleness (the chosen profile, key or exact project no longer
# matches) is a 409; everything else about the Remote read is unavailability.
_SELECT_STALE_CODES = frozenset({
    "profile_not_found", "profile_revision_conflict", "profile_recovery_required",
    "credential_binding_conflict", "credential_required", "invalid_credential",
    "project_not_found", "identity_mismatch", "snapshot_changed", "invalid_id",
    "invalid_request",
})


async def _catalog_select_source(request: Request, ctx: Any, profile_id: str | None,
                                 persistent_id: str) -> dict[str, Any] | None:
    """I9: observe the exact catalog project through the app owner for Select.

    Reached via ``request.app.state.remote_catalog`` (the lifespan holder), never
    app.state.ctx or a new singleton. The profile/credential are captured
    BEFORE the read, so ``_select_observation``'s live re-read detects drift
    during it. The owner's read is strict v2 with a fresh revision/URL-bound
    provider and a lifecycle lease; there is no v1 probe, Create or Enable.
    Returns None when the injected I6 test observer is installed.
    """
    from ..sync.catalog import CatalogError
    from ..sync.profiles import ProfileError
    if getattr(ctx, "remote_select_observer", None) is not None:
        return None
    holder = getattr(request.app.state, "remote_catalog", None)
    if holder is None:
        _remote_refusal("catalog_unavailable", 503)
    if profile_id is None:
        _remote_refusal("stale_binding")
    code = None
    profile = credential = catalog = None
    try:
        profile = holder.get(profile_id)
        credential = {"revision": credentials.profile_token_revision(profile.profile_id, profile.url),
                      "source": credentials.profile_token_source(profile.profile_id, profile.url)}
    except CatalogError as error:
        code = "catalog_unavailable" if error.code == "service_unavailable" else "stale_binding"
    except Exception:
        code = "stale_binding"
    if code is None:
        try:
            catalog = await holder.select_observation(
                profile.profile_id, persistent_id, expected_revision=profile.revision,
            )
        except CatalogError as error:
            code = "stale_binding" if error.code in _SELECT_STALE_CODES else "catalog_unavailable"
        except ProfileError as error:
            code = ("stale_binding" if len(error.args) == 1 and error.args[0] in _SELECT_STALE_CODES
                    else "catalog_unavailable")
        except Exception:
            code = "catalog_unavailable"
    # Refuse outside the handlers: no provider/transport exception is retained.
    if code is not None:
        _remote_refusal(code, 503 if code == "catalog_unavailable" else 409)
    return {"profile": profile.model_dump(), "credential": credential, "catalog": catalog}


def _check_change_observation(ctx: Any, change_id: str) -> None:
    expected = ctx.docs.transitions.expected_change_evidence(change_id)
    if _prepare_observation(ctx) != expected["observation"]:
        _remote_refusal("stale_binding")


def _bind_change_owners(ctx: Any) -> None:
    """One bound service and one Ctx event stream; no selected-project lookup."""
    transitions = ctx.docs.transitions
    if transitions.binding_quiescing:
        return

    async def drain(change_id: str, timeout: float) -> dict[str, Any]:
        _check_change_observation(ctx, change_id)
        if any(not transitions.room_drain_ready(
                room.sid, room.path, room.editor_id, room.binding_generation,
        ) for room in ctx.sync._sessions.values()):
            return {"drained": False, "fenced": False, "reason": "pending_edits"}
        proof = await ctx.sync.drain_binding_change(change_id, timeout)
        _check_change_observation(ctx, change_id)
        return {"drained": ctx.sync.validate_binding_drain(proof, change_id),
                "fenced": proof.fenced, "reason": proof.reason}

    async def event(body: dict[str, Any]) -> None:
        await ctx.bus.publish(EventType.REMOTE_BINDING_CHANGE, body)

    transitions.set_change_callbacks(
        begin_quiesce=ctx.sync.begin_binding_quiesce, drain=drain,
        cancel_quiesce=ctx.sync.cancel_binding_quiesce, event=event,
        fence_status=lambda: ctx.sync.binding_fenced,
    )


@remote_router.post("/prepare")
async def prepare_remote(payload: RemotePrepare, request: Request, ctx: Ctx) -> dict[str, Any]:
    """Read-only preparation for the separate disabled identity/config commit."""
    activated_model(ctx)
    from ..sync.transitions import journal_unresolved, read_binding_change_journals
    if any(journal_unresolved(row) for row in read_binding_change_journals(ctx.paths.root)):
        _remote_refusal("recovery_required")
    if payload.backend_epoch is not None:
        ctx.docs.transitions._require_epoch(payload.backend_epoch)
    observed = _prepare_observation(ctx)
    if (observed["identity_revision"] != payload.expected_revision
            or observed["config_fingerprint"] != payload.config_fingerprint):
        _remote_refusal("stale_binding")
    selection = None
    if payload.operation == "select":
        # Local refusals above are cheap and come first; only then the network.
        source = await _catalog_select_source(request, ctx, payload.profile_id, payload.persistent_id)
        if source is not None and _prepare_observation(ctx) != observed:
            _remote_refusal("stale_binding")  # Local state moved during the read.
        selection = _select_observation(ctx, payload, source)
    destination = _prepare_destination(ctx, payload, observed, selection)
    _bind_change_owners(ctx)
    expected = {"observation": observed, "from": {
                    "persistent_id": observed["persistent_id"], "url": observed["url"]},
                "to": destination, "operation": payload.operation,
                "binding_generation": ctx.docs.transitions.generation,
                "backend_epoch": ctx.docs.transitions.backend_epoch}
    if payload.operation == "select":
        expected["selection"] = selection
    result = ctx.docs.transitions.prepare_change(expected)
    return {**result, "from": expected["from"], "to": destination,
            "plan": {"operation": payload.operation, "enabled": False}, "conflicts": []}


@remote_router.post("/changes/{change_id}/quiesce")
async def quiesce_remote(change_id: str, payload: RemoteQuiesce, ctx: Ctx) -> dict[str, Any]:
    _check_change_observation(ctx, change_id)
    # I5's durable evidence sink is installed without writing during prepare.
    from ..sync.transitions import binding_change_evidence_writer
    expected = ctx.docs.transitions.expected_change_evidence(change_id)
    if not ctx.sync._binding_quiescing:
        ctx.sync.set_binding_evidence_sink(binding_change_evidence_writer(
            ctx.paths.root, change_id, expected,
        ))
    return await ctx.docs.transitions.quiesce_change(change_id, payload.expected_generation)


@remote_router.post("/changes/{change_id}/editor-ack")
async def acknowledge_remote_editor(change_id: str, payload: RemoteEditorAck, ctx: Ctx) -> dict[str, Any]:
    _check_change_observation(ctx, change_id)
    return ctx.docs.transitions.editor_ack(
        change_id, payload.editor_id, payload.old_generation, payload.last_saved_version,
    )


@remote_router.delete("/changes/{change_id}")
async def cancel_remote_change(change_id: str, ctx: Ctx) -> dict[str, Any]:
    # Cancellation never needs a successful CAS read; stale/unavailable config
    # cannot trap the editor in a wait or convert cancellation into a commit.
    from ..sync.transitions import read_binding_change_journals
    if any(row["change_id"] == change_id and row["version"] == 2 and row["phase"] == "completed"
           for row in read_binding_change_journals(ctx.paths.root)):
        _remote_refusal("already_committed")
    result = ctx.docs.transitions.cancel_change(change_id)
    await ctx.bus.publish(EventType.REMOTE_BINDING_CHANGE, result)
    return result


def _commit_selection(ctx, expected, fresh=None):
    """Repeat profile/credential/catalog CAS, including label revision drift.

    ``fresh`` is the I9 catalog observation re-read just before the commit's
    synchronous locks (no network under them). Profile and credential are
    re-read live at every call; the catalog part must equal prepare's exactly.
    """
    from ..sync.profiles import RemoteProfiles
    source = expected.get("selection")
    if source is not None:
        catalog, profile = source["catalog"], source["profile"]
        payload = RemotePrepare(
            expected_revision=expected["observation"]["identity_revision"],
            config_fingerprint=expected["observation"]["config_fingerprint"],
            persistent_id=catalog["persistent_id"], profile_id=profile["profile_id"],
            url=profile["url"], operation="select", catalog_id=catalog["persistent_id"],
            catalog_instance_id=catalog["space_instance_id"],
        )
        if _select_observation(ctx, payload, fresh) != source:
            _remote_refusal("stale_binding")
    else:
        source = expected["observation"]
        profile = source.get("profile")
        if profile is not None:
            if (RemoteProfiles().get(profile["profile_id"]).model_dump() != profile
                    or source["credential"] != {
                        "revision": credentials.profile_token_revision(profile["profile_id"], profile["url"]),
                        "source": credentials.profile_token_source(profile["profile_id"], profile["url"])}):
                _remote_refusal("stale_binding")


def _commit_target_binding(ctx, expected):
    from ..sync.bindings import BindingRecord
    source = expected.get("selection")
    if source is not None:
        return BindingRecord(
            canonical_root=expected["observation"]["canonical_root"],
            profile_id=source["profile"]["profile_id"], url=expected["to"]["url"],
            persistent_id=expected["to"]["persistent_id"],
            identity_revision=expected["observation"]["identity_revision"] + 1,
            selection_state="selected", space_instance_id=source["catalog"]["space_instance_id"],
        )
    old = expected["observation"]["binding"]
    if expected["operation"] == "disable" and old is not None:
        return BindingRecord.model_validate({**dict(old), "selection_state": "selected",
            "identity_revision": expected["observation"]["identity_revision"] + 1})
    return None


def _commit_corpus_matches(ctx, change):
    from ..sync.service import BindingDrainProof
    proof = ctx.sync._binding_proof
    if (type(proof) is not BindingDrainProof or proof.change_id != change.change_id or proof._owner is not ctx.sync
            or proof.generation != change.generation or not proof.fenced
            or proof.target != ctx.sync._binding_target
            or not proof.runner_stopped or not proof.reconciled or proof.reason is not None
            or not ctx.sync._binding_fenced or not ctx.sync._binding_quiescing
            or ctx.sync._binding_draining or ctx.sync._binding_failure or ctx.sync._cleanup_failed
            or ctx.sync._synced or ctx.sync._skip
            or ctx.sync._profile_lease is not None or ctx.sync._reservation_lease is not None
            or ctx.sync._runner is not None or ctx.sync._ws is not None
            or ctx.sync._pending or ctx.sync._pending_paths or ctx.sync._live_writes
            or ctx.sync._sessions or ctx.sync._tasks or ctx.sync._retained_evidence
            or ctx.sync._binding_corpus()[1] != proof.corpus_fingerprint
            or ctx.sync._binding_checkpoint_fingerprint() != proof.checkpoint_fingerprint):
        _remote_refusal("stale_binding")


def _committed_response(request, ctx, change_id, result):
    """Historical command result, not a mixture with a later target's status."""
    scope = request.state.project_scope
    binding = result["binding"]
    return {"project_id": scope.project_id, "epoch": scope.epoch,
            "backend_epoch": ctx.docs.transitions.backend_epoch,
            **{key: result[key] for key in ("persistent_id", "identity_revision", "url", "enabled",
                                           "config_fingerprint", "binding_generation")},
            "profile_id": binding["profile_id"] if binding else None,
            "space_instance_id": binding["space_instance_id"] if binding else None,
            "state": "disabled", "reason": None, "has_token": False,
            "active_rooms": 0, "pending_writes": 0,
            "binding_change": {"change_id": change_id, "state": "committed"},
            "change_id": change_id, "committed": True}


def _confirmed_completed_result(ctx, payload, completed):
    expected, result = completed["expected"], completed["commit"]
    # A retained receipt is historical evidence, not restart authorization.
    # Never relabel an old-epoch G with the new backend epoch, or rewind RAM G
    # when an earlier same-epoch command is explicitly retried.
    transitions = ctx.docs.transitions
    transitions._require_epoch(expected["backend_epoch"])
    from ..sync.transitions import MAX_BINDING_GENERATION
    if (not 0 <= result["binding_generation"] <= MAX_BINDING_GENERATION
            or transitions.generation < result["binding_generation"]):
        _remote_refusal("recovery_required")
    observation = expected["observation"]
    if (payload.expected_revision != observation["identity_revision"]
            or payload.config_fingerprint != observation["config_fingerprint"]
            or payload.expected_generation != expected["binding_generation"] + 1
            or payload.confirmation.model_dump() != {
                "url": result["url"], "persistent_id": result["persistent_id"]}):
        _remote_refusal("confirmation_required")
    return result


@remote_router.post("/changes/{change_id}/commit")
async def commit_remote_change(change_id: str, payload: RemoteCommit, request: Request,
                               ctx: Ctx) -> dict[str, Any]:
    """I6 disabled-only commit, no network/Enable/rollback/automatic recovery (§7).

    The only awaits are short Ctx/DocStore admission and response publication.
    Lifecycle → config → credential locks cover the synchronous final CAS.
    Explicit same-process retries can finish a matching partial publication;
    restart merely classifies it blocked and never repairs either store.
    """
    import json
    import yaml
    from ..core import config as config_owner
    from ..core.application_config import application_config_transaction
    from ..core.paths import user_config_dir
    from ..sync.bindings import BindingError, RemoteBindings, root_key
    from ..sync.transitions import (BindingChangeError, read_binding_change_journals,
                                    publish_binding_change_commit, next_binding_generation)

    transitions = ctx.docs.transitions
    confirmation = payload.confirmation.model_dump()
    # A completed journal is retained forever, even if another change replaced
    # the RAM slot. This read cannot repair config/private state or adopt G.
    records = read_binding_change_journals(ctx.paths.root)
    completed = next((row for row in records if row["change_id"] == change_id
                      and row["version"] == 2 and row["phase"] == "completed"), None)
    if completed is not None:
        result = _confirmed_completed_result(ctx, payload, completed)
        return _committed_response(request, ctx, change_id, result)

    change = transitions.require_ready_commit(change_id, **payload.model_dump(exclude={"confirmation"}),
                                               confirmation=confirmation)
    expected = change.expected
    fresh = None
    if expected.get("selection") is not None:
        # I9: re-observe the exact project BEFORE any lock (the read is async;
        # lifecycle/config/credential locks below are synchronous). A failed
        # read refuses with nothing written; the change stays retryable.
        selected = expected["selection"]
        fresh = await _catalog_select_source(request, ctx, selected["profile"]["profile_id"],
                                             selected["catalog"]["persistent_id"])
    # All fallible I/O below leaves admission fenced; never resurrect old leases.
    async with request.app.state.project_lock:
        # A same-change request may have committed while this one waited for
        # an ordinary settings writer or another command. Return its durable
        # result instead of trying to drain/advance the identity a second time.
        completed = next((row for row in read_binding_change_journals(ctx.paths.root)
                          if row["change_id"] == change_id and row["version"] == 2
                          and row["phase"] == "completed"), None)
        if completed is not None:
            return _committed_response(request, ctx, change_id,
                                       _confirmed_completed_result(ctx, payload, completed))
        async with transitions.final_snapshot():
            transitions.require_ready_commit(change_id, **payload.model_dump(exclude={"confirmation"}),
                                               confirmation=confirmation)
            lifecycle = ctx.remote_profile_lifecycle
            if lifecycle is None:
                _remote_refusal("consumer_lifecycle_unavailable", 503)
            with lifecycle._critical():
                with application_config_transaction():
                    with credentials._file_lock:
                        if str(user_config_dir()) != expected["observation"]["personal_home"]:
                            _remote_refusal("stale_binding")
                        _commit_selection(ctx, expected, fresh)
                        _check_destination_reservation(ctx, expected["observation"], expected["to"])
                        _commit_corpus_matches(ctx, change)
                        store = RemoteBindings()
                        snapshot = store.read()
                        target = _commit_target_binding(ctx, expected)
                        previous = next((row for row in read_binding_change_journals(ctx.paths.root)
                                         if row["change_id"] == change_id and row["version"] == 2), None)
                        raw, disk_config, fingerprint = config_owner._project_config_document(ctx.paths)
                        if previous is None:
                            _check_change_observation(ctx, change_id)
                            identity = ProjectIdentity(id=expected["to"]["persistent_id"],
                                revision=payload.expected_revision + 1)
                            document = config_owner._project_fields(raw)
                            document["project_identity"] = identity.model_dump(mode="json")
                            document["sync"] = {"enabled": False, "url": expected["to"]["url"]}
                            content = yaml.safe_dump(document, sort_keys=False, allow_unicode=True,
                                                     indent=2).encode("utf-8")
                            roots = {root_key(row.canonical_root): row.model_dump()
                                     for row in snapshot.records}
                            if target is None:
                                roots.pop(root_key(ctx.paths.root), None)
                            else:
                                roots[root_key(ctx.paths.root)] = target.model_dump()
                            private_bytes = (json.dumps({"version": 1, "roots": roots},
                                ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
                            private_fingerprint = (snapshot.fingerprint if target is None
                                and snapshot.get(ctx.paths.root) is None else hashlib.sha256(private_bytes).hexdigest())
                            result = {"persistent_id": identity.id, "identity_revision": identity.revision,
                                "url": expected["to"]["url"], "enabled": False,
                                "config_fingerprint": hashlib.sha256(content).hexdigest(),
                                "private_fingerprint": private_fingerprint,
                                "binding": target.model_dump() if target else None,
                                "binding_generation": next_binding_generation(transitions.generation)}
                        else:
                            transitions._require_epoch(previous["expected"]["backend_epoch"])
                            result = previous["commit"]
                            identity = ProjectIdentity(id=result["persistent_id"], revision=result["identity_revision"])
                        # A partial-publication retry must finish this exact
                        # fence, never restore an arbitrary durable counter.
                        if result["binding_generation"] != next_binding_generation(transitions.generation):
                            _remote_refusal("stale_binding")
                        if (fingerprint not in {payload.config_fingerprint, result["config_fingerprint"]}
                                or snapshot.fingerprint not in {
                                    expected["observation"]["private_fingerprint"], result["private_fingerprint"]}):
                            _remote_refusal("stale_binding")
                        try:
                            publish_binding_change_commit(ctx.paths.root, change_id, expected, result, "committing")
                            # Journal fsync/staging can be slow, and external
                            # writers do not respect our locks. Repeat EVERY
                            # observation at the actual config commit boundary.
                            def before_replace():
                                transitions.require_ready_commit(change_id,
                                    **payload.model_dump(exclude={"confirmation"}), confirmation=confirmation)
                                _commit_corpus_matches(ctx, change)
                                _commit_selection(ctx, expected, fresh)
                                _check_destination_reservation(ctx, expected["observation"], expected["to"])
                                if store.read().fingerprint != snapshot.fingerprint:
                                    raise BindingChangeError("stale_binding")
                            before_replace()
                            if fingerprint == payload.config_fingerprint:
                                committed = config_owner._commit_disabled_identity_in_transaction(
                                    ctx.paths, identity=identity, url=result["url"],
                                    expected_fingerprint=payload.config_fingerprint,
                                    before_replace=before_replace)
                                if committed.fingerprint != result["config_fingerprint"]:
                                    raise BindingChangeError("stale_binding")
                                disk_config = committed.config
                            # Metadata only: never replace Ctx, paths, jobs, MCP or descriptor.
                            ctx.config.project_identity = disk_config.project_identity
                            ctx.config.sync = disk_config.sync
                            transitions.observe_activated_model()
                            ctx.sync._activated_model_seen = True
                            publish_binding_change_commit(ctx.paths.root, change_id, expected, result, "config_committed")
                            if config_owner.read_project_config_snapshot(ctx.paths).fingerprint != result["config_fingerprint"]:
                                raise BindingChangeError("stale_binding")
                            _commit_selection(ctx, expected, fresh)
                            if snapshot.fingerprint != result["private_fingerprint"]:
                                if target is None:
                                    snapshot = store._remove_in_transaction(ctx.paths.root,
                                        expected_fingerprint=expected["observation"]["private_fingerprint"])
                                else:
                                    snapshot = store._save_in_transaction(target,
                                        expected_fingerprint=expected["observation"]["private_fingerprint"])
                            if snapshot.fingerprint != result["private_fingerprint"]:
                                raise BindingError("binding_revision_conflict")
                            if config_owner.read_project_config_snapshot(ctx.paths).fingerprint != result["config_fingerprint"]:
                                raise BindingChangeError("stale_binding")
                            publish_binding_change_commit(ctx.paths.root, change_id, expected, result, "completed")
                            transitions.complete_commit(change_id, result["binding_generation"])
                            # Old-target evidence is now durably retained in the
                            # completed record. Shutdown must not rewrite it as
                            # a fresh I5 draining record or erase activation.
                            ctx.sync.complete_disabled_binding_change(change_id)
                        except Exception:
                            change.phase, change.reason = "blocked", "recovery_required"
                            _remote_refusal("recovery_required")
    await ctx.sync._set_status("disabled")
    await ctx.bus.publish(EventType.REMOTE_BINDING_CHANGE, transitions.change_status(change_id))
    return _committed_response(request, ctx, change_id, result)


# --- I11: read-only reconcile preview (studio-design §5.2/§6, server-design §3.3) ---

class RemoteReconcilePreview(BaseModel):
    """CAS of the observation the reader is looking at; no target is accepted."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_revision: StrictInt = Field(ge=1)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend_epoch: str | None = None


# Remote/owner codes meaning "the selected project or snapshot moved": 409 and a
# new preview (or a new Select). Everything else is unavailability (503).
_PREVIEW_REFUSALS = {
    "snapshot_changed": (409, "snapshot_changed"),
    "instance_mismatch": (409, "instance_mismatch"),
    "project_not_found": (409, "project_not_found"),
    "identity_mismatch": (409, "instance_mismatch"),
    "pagination_limit": (413, "snapshot_too_large"),
    "snapshot_size_limit": (413, "snapshot_too_large"),
    "payload_too_large": (413, "snapshot_too_large"),
}


def _preview_selected_binding(ctx: Any) -> Any:
    """The exact selected tuple of THIS canonical root, with typed refusals.

    Reads only: binding-change journals, the private bindings store, profiles
    and the in-memory config. Copied roots and same-ID/other-URL tuples are
    refused before any network access; ``_prepare_observation`` then captures
    every remaining nonsecret component for CAS and the fingerprint.
    """
    from ..core.project_scope import canonical_root
    from ..sync.bindings import RemoteBindings
    from ..sync.profiles import RemoteProfiles
    from ..sync.transitions import journal_unresolved, read_binding_change_journals

    if any(journal_unresolved(row) for row in read_binding_change_journals(ctx.paths.root)):
        _remote_refusal("recovery_required")
    if ctx.docs.transitions.binding_quiescing:
        _remote_refusal("change_in_progress")
    code = None
    row = None
    try:
        root = str(canonical_root(ctx.paths.root))
        private = RemoteBindings().read()
        row = private.get(root)
        identity = ctx.config.project_identity
        url = normalise_url(ctx.config.sync.url) if ctx.config.sync.url else ""
        if identity is None:
            code = "binding_required"
        elif any(other.canonical_root != root and other.persistent_id == identity.id
                 and other.url in {url, row.url if row is not None else url}
                 for other in private.records):
            # A copied checkout carries the same portable identity/URL; the
            # binding belongs to the original root and is never shared (§4.3).
            code = "duplicate_local_binding"
        elif row is None or row.space_instance_id is None:
            code = "binding_required"
        elif row.persistent_id == identity.id and (
                row.url != url or RemoteProfiles().get(row.profile_id).url != row.url):
            code = "url_mismatch"
    except Exception:
        code = "stale_binding"
    if code is not None:
        _remote_refusal(code)
    return row


async def _preview_snapshot(request: Request, row: Any, profile: dict[str, Any]) -> Any:
    """One complete consistent snapshot through the app owner: GET only (§6)."""
    from ..sync.catalog import CatalogError
    from ..sync.profiles import ProfileError

    holder = getattr(request.app.state, "remote_catalog", None)
    if holder is None:
        _remote_refusal("catalog_unavailable", 503)
    code = shot = None
    try:
        shot = await holder.snapshot(row.profile_id, row.persistent_id,
                                     expected_revision=profile["revision"],
                                     expected_epoch=row.space_instance_id)
    except CatalogError as error:
        code = error.code
    except ProfileError as error:
        code = error.args[0] if len(error.args) == 1 and isinstance(error.args[0], str) else "unavailable"
    except Exception:
        code = "unavailable"
    if code == "snapshot_changed":
        # The first page is bound to the selected incarnation: tell a replaced
        # project (new Select) from drift between pages (new preview).
        try:
            project = await holder.get_project(row.profile_id, row.persistent_id,
                                               expected_revision=profile["revision"])
            if project.storage_epoch != row.space_instance_id:
                code = "instance_mismatch"
        except Exception:
            pass
    if code is not None:
        status, refusal = _PREVIEW_REFUSALS.get(code, (None, None))
        if status is None:
            status, refusal = ((409, "stale_binding") if code in _SELECT_STALE_CODES
                               else (503, "catalog_unavailable"))
        _remote_refusal(refusal, status)
    return shot


def _checkpoint_fingerprint(checkpoint: Any) -> str | None:
    """Nonsecret digest of one v2 checkpoint's incarnation, cursor and bases."""
    import json
    if checkpoint is None:
        return None
    return hashlib.sha256(json.dumps(
        [checkpoint.space_instance_id, checkpoint.cursor,
         [[p, b.rev, b.hash] for p, b in sorted(checkpoint.docs.items())]],
        ensure_ascii=False).encode("utf-8")).hexdigest()


async def reconcile_preview_evidence(request: Request, ctx: Any, *, expected_revision: int,
                                     config_fingerprint: str,
                                     backend_epoch: str | None = None) -> dict[str, Any]:
    """Compute a FRESH read-only preview observation (I11; reused by I12 recheck).

    Only GETs reach the Remote. No DocStore/config/private/base/checkpoint/queue
    write, room or WS. The returned ``evidence`` is the complete fingerprint
    input; ``counts``/``conflicts`` follow the §16.3 engine policy.
    """
    import json
    from ..sync.state import SyncStateError, SyncStateV2
    from ..sync.transitions import classify_reconcile

    transitions = ctx.docs.transitions
    if backend_epoch is not None:
        transitions._require_epoch(backend_epoch)
    row = _preview_selected_binding(ctx)
    observed = _prepare_observation(ctx)
    generation = transitions.generation
    if (observed["identity_revision"] != expected_revision
            or observed["config_fingerprint"] != config_fingerprint
            or observed["binding"] != row.model_dump() or observed["profile"] is None):
        _remote_refusal("stale_binding")
    _check_destination_reservation(ctx, observed, {"url": row.url, "persistent_id": row.persistent_id})
    shot = await _preview_snapshot(request, row, observed["profile"])
    # Local state moved during the network read: discard, never mix observations.
    if (shot.id != row.persistent_id or shot.storage_epoch != row.space_instance_id
            or _prepare_observation(ctx) != observed or transitions.generation != generation
            or transitions.binding_quiescing):
        _remote_refusal("stale_binding")
    try:
        local, local_fingerprint = ctx.sync._binding_corpus()
    except Exception:
        _remote_refusal("documents_unavailable")
    try:
        checkpoint = SyncStateV2.load(ctx.paths.root, url=row.url, persistent_id=row.persistent_id)
    except SyncStateError:
        _remote_refusal("checkpoint_unavailable")
    bases: dict[str, tuple[int, str | None]] = {}
    checkpoint_fingerprint = _checkpoint_fingerprint(checkpoint)
    if checkpoint is None:
        base_state = "none"
    else:
        # Old bases are preserved, but they only apply to the SAME incarnation
        # whose seq has not gone backwards (§6); otherwise full reconciliation.
        if checkpoint.space_instance_id != shot.storage_epoch:
            base_state = "epoch_mismatch"
        elif checkpoint.cursor > shot.seq:
            base_state = "cursor_rollback"
        else:
            base_state = "matched"
            bases = {p: (b.rev, b.hash) for p, b in checkpoint.docs.items()}
    remote = {d.path: (d.rev, None if d.deleted else d.hash) for d in shot.documents}
    counts, conflicts = classify_reconcile(local, remote, bases)
    remote_digest = hashlib.sha256(json.dumps(
        [[d.path, d.rev, d.hash, d.deleted] for d in shot.documents],
        ensure_ascii=False).encode("utf-8")).hexdigest()
    evidence = {
        "observation": observed, "binding_generation": generation,
        "backend_epoch": transitions.backend_epoch,
        "snapshot": {"id": shot.id, "storage_epoch": shot.storage_epoch, "seq": shot.seq,
                     "documents": remote_digest},
        "local_fingerprint": local_fingerprint,
        "checkpoint": {"state": base_state, "fingerprint": checkpoint_fingerprint},
        "ttl": transitions.reconcile_previews.ttl,
    }
    return {"evidence": evidence, "counts": counts, "conflicts": conflicts,
            "base": base_state, "incarnation": {"space_instance_id": shot.storage_epoch,
                                                "seq": shot.seq}}


async def recheck_reconcile_preview(request: Request, ctx: Any, preview_id: str, fingerprint: str, *,
                                    expected_revision: int, config_fingerprint: str,
                                    backend_epoch: str | None = None) -> Any:
    """I12 Enable gate: re-read everything and reproduce the stored fingerprint.

    Raises 409 ``preview_not_found``/``preview_expired``/``preview_stale`` or the
    same typed refusals as the preview itself; any refusal discards ONLY that
    temporary preview. Success returns the ReconcilePreview without consuming
    it. Still not a lock: Enable keeps its own CAS/conflict handling.
    """
    previews = ctx.docs.transitions.reconcile_previews
    previews.get(preview_id)
    try:
        fresh = await reconcile_preview_evidence(
            request, ctx, expected_revision=expected_revision,
            config_fingerprint=config_fingerprint, backend_epoch=backend_epoch)
    except BaseException:
        previews.discard(preview_id)
        raise
    return previews.require(preview_id, fingerprint, fresh["evidence"])


@remote_router.post("/reconcile-preview")
async def reconcile_preview(payload: RemoteReconcilePreview, request: Request,
                            ctx: Ctx) -> dict[str, Any]:
    """Read-only Preview of the exact selected tuple; never Enable or a lock."""
    fresh = await reconcile_preview_evidence(
        request, ctx, expected_revision=payload.expected_revision,
        config_fingerprint=payload.config_fingerprint, backend_epoch=payload.backend_epoch)
    preview = ctx.docs.transitions.reconcile_previews.publish(
        fresh["evidence"], fresh["counts"], fresh["conflicts"])
    return {**preview.dto(), "base": fresh["base"], "incarnation": fresh["incarnation"]}


# --- I12: separate Enable/catch-up (studio-design §5.2/§7.7, runtime-integration-plan I12) ---

class RemoteEnableConfirmation(BaseModel):
    """The exact target the reader saw in the preview, and the reconcile action."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    url: str
    persistent_id: str
    action: Literal["reconcile"]


class RemoteEnable(BaseModel):
    """A current preview plus exact confirmation; no target is accepted from the browser."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_revision: StrictInt = Field(ge=1)
    expected_generation: StrictInt = Field(ge=0)
    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: RemoteEnableConfirmation
    config_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    backend_epoch: str | None = None


class _EnableRefused(Exception):
    """A typed refusal from inside the synchronous publication section."""

    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code, self.status = code, status


_VERIFY_REFUSALS = {"unauthorised": (409, "unauthorised"),
                    "incompatible": (409, "protocol_unavailable"),
                    "insecure_blocked": (409, "insecure_blocked")}


async def _enable_verified_account(request: Request, profile: dict[str, Any]) -> str:
    """(c) Verify the exact profile revision via the app owner; return me.user.id.

    GET /healthz + /api/v2/me only. The account is recorded in the Enable
    journal and every hello (also after restart) must present it (§6).
    """
    from ..sync.catalog import CatalogError
    from ..sync.profiles import ProfileError

    holder = getattr(request.app.state, "remote_catalog", None)
    if holder is None:
        _remote_refusal("catalog_unavailable", 503)
    code, status, verified = None, 409, None
    try:
        verified = await holder.verify(profile["profile_id"], expected_revision=profile["revision"])
    except CatalogError as error:
        code = "stale_binding" if error.code in _SELECT_STALE_CODES else "catalog_unavailable"
    except ProfileError as error:
        code = ("stale_binding" if len(error.args) == 1 and error.args[0] in _SELECT_STALE_CODES
                else "catalog_unavailable")
    except Exception:
        code = "catalog_unavailable"
    if code is None:
        account = getattr(getattr(verified, "user", None), "id", None)
        if (getattr(verified, "state", None) == "valid" and verified.wire_protocol == 2
                and verified.catalog_version == 2 and verified.error_code is None
                and isinstance(account, str) and 0 < len(account) <= 256):
            return account
        status, code = _VERIFY_REFUSALS.get(getattr(verified, "state", None),
                                            (503, "catalog_unavailable"))
    # Refuse outside the handlers: no provider/transport exception is retained.
    _remote_refusal(code, 503 if code == "catalog_unavailable" else status)


def _publish_enable(ctx: Any, preview: Any, account: str, fence: Any, owner: Any) -> dict[str, Any]:
    """Steps (d)–(i), synchronously, under project → snapshot → lifecycle → config → credential.

    No await/network. Before the project config replace nothing is published:
    a refusal settles the intent as ``aborted`` and releases only this
    reservation. After it, every failure is blocked ``recovery_required``:
    no rollback, retry or reset (studio-design §7.5/§7.7).
    """
    import json
    from uuid import uuid4

    import yaml
    from ..core import config as config_owner
    from ..core.paths import user_config_dir
    from ..sync.bindings import BindingRecord, RemoteBindings, root_key
    from ..sync.reservations import ReservationError
    from ..sync.state import SyncStateV2
    from ..sync.transitions import (_plain_preview_evidence, journal_unresolved, next_binding_generation,
                                    publish_enable_journal, read_binding_change_journals)

    transitions = ctx.docs.transitions
    evidence = preview.evidence
    observation = _plain_preview_evidence(evidence["observation"])
    seen = evidence["snapshot"]
    row = BindingRecord.model_validate(dict(observation["binding"]))
    root = observation["canonical_root"]
    destination = (row.url, row.persistent_id)

    def local_cas(own_change: str | None) -> None:
        """Every LOCAL component the preview observed, re-read now (no network)."""
        try:
            stale = (any(journal_unresolved(r) and r["change_id"] != own_change
                         for r in read_binding_change_journals(ctx.paths.root))
                     or transitions.binding_quiescing
                     or transitions.generation != evidence["binding_generation"]
                     or transitions.backend_epoch != evidence["backend_epoch"]
                     or str(user_config_dir()) != observation["personal_home"]
                     or (ctx.config.sync.enabled and own_change is None))
            if not stale:
                # Config bytes, R, private store, profile revision and credential.
                stale = _plain_preview_evidence(_prepare_observation(ctx)) != observation
            if not stale:
                checkpoint = SyncStateV2.load(ctx.paths.root, url=row.url, persistent_id=row.persistent_id)
                stale = (ctx.sync._binding_corpus()[1] != evidence["local_fingerprint"]
                         or _checkpoint_fingerprint(checkpoint) != evidence["checkpoint"]["fingerprint"])
            others = RemoteBindings().read().records if not stale else ()
        except Exception:
            stale = True
        if stale:
            raise _EnableRefused("preview_stale")
        # One working copy per (URL, ID): no other root may hold this destination.
        if any(other.canonical_root != root and (other.url, other.persistent_id) == destination
               for other in others):
            raise _EnableRefused("duplicate_local_binding")

    def reserved(lease: object) -> None:
        with owner._lock:
            incumbent = owner._reservations.get(destination)
        if incumbent is None or incumbent.lease is not lease or incumbent.consumer is not ctx.sync:
            raise _EnableRefused("duplicate_local_binding")

    local_cas(None)
    # (d) Reserve: the same consumer the runner uses, so it adopts THIS lease.
    try:
        lease = owner.reserve(root=root, url=row.url, persistent_id=row.persistent_id, consumer=ctx.sync)
    except ReservationError as error:
        if error.code == "duplicate_local_binding":
            raise _EnableRefused("duplicate_local_binding") from None
        raise _EnableRefused("consumer_lifecycle_unavailable", 503) from None
    adopted = lease is getattr(ctx.sync, "_reservation_lease", None)

    def release() -> None:
        if not adopted:
            owner.release(lease)

    change_id = expected = commit = None
    try:
        raw, _, fingerprint = config_owner._project_config_document(ctx.paths)
        if fingerprint != observation["config_fingerprint"]:
            raise _EnableRefused("preview_stale")
        identity = ProjectIdentity(id=row.persistent_id, revision=observation["identity_revision"] + 1)
        document = config_owner._project_fields(raw)
        document["project_identity"] = identity.model_dump(mode="json")
        document["sync"] = {"enabled": True, "url": row.url}
        content = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, indent=2).encode("utf-8")
        confirmed = BindingRecord.model_validate({**row.model_dump(), "selection_state": "confirmed",
                                                  "identity_revision": identity.revision})
        store = RemoteBindings()
        private = store.read()
        if private.fingerprint != observation["private_fingerprint"]:
            raise _EnableRefused("preview_stale")
        roots = {root_key(r.canonical_root): r.model_dump() for r in private.records}
        roots[root_key(ctx.paths.root)] = confirmed.model_dump()
        private_bytes = (json.dumps({"version": 1, "roots": roots}, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n").encode("utf-8")
        checkpoint = SyncStateV2.load(ctx.paths.root, url=row.url, persistent_id=row.persistent_id)
        if checkpoint is not None and (checkpoint.space_instance_id != row.space_instance_id
                                       or checkpoint.cursor > seen["seq"]):
            raise _EnableRefused("preview_stale")
        change_id = uuid4().hex
        expected = {"operation": "enable", "observation": observation,
                    "to": {"persistent_id": identity.id, "url": row.url, "enabled": True},
                    "snapshot": {"id": seen["id"], "storage_epoch": seen["storage_epoch"],
                                 "seq": seen["seq"]},
                    "binding_generation": transitions.generation,
                    "backend_epoch": transitions.backend_epoch, "account_id": account}
        commit = {"identity_revision": identity.revision, "persistent_id": identity.id,
                  "url": row.url, "enabled": True,
                  "config_fingerprint": hashlib.sha256(content).hexdigest(),
                  "private_fingerprint": hashlib.sha256(private_bytes).hexdigest(),
                  "binding": confirmed.model_dump(),
                  "binding_generation": next_binding_generation(transitions.generation)}
        # (e) Durable intent BEFORE any publication.
        publish_enable_journal(ctx.paths.root, change_id, expected, commit, "enabling")
    except Exception as error:
        release()
        if (change_id is not None and any(r["change_id"] == change_id for r in _journals_or_empty(ctx))
                and not _abort_enable(ctx, change_id, expected, commit)):
            # The intent became durable although its write reported failure, and
            # it cannot be settled: unresolved, i.e. blocked recovery_required.
            raise _EnableRefused("recovery_required") from None
        if isinstance(error, _EnableRefused):
            raise
        raise _EnableRefused("binding_journal_unavailable", 503) from None

    def abort(code: str, status: int = 409) -> None:
        """Nothing of ours was published: settle the intent, release, refuse."""
        release()
        if not _abort_enable(ctx, change_id, expected, commit):
            raise _EnableRefused("recovery_required")
        raise _EnableRefused(code, status)

    # (f) Project config: R+1, enabled, same URL — the commit point.
    def before_replace() -> None:
        local_cas(change_id)
        reserved(lease)

    try:
        written = config_owner._write_project_document_in_transaction(
            ctx.paths, document, expected_fingerprint=observation["config_fingerprint"],
            before_replace=before_replace)
    except Exception as error:
        try:
            current = config_owner._project_config_bytes(ctx.paths)
            ours = current is not None and hashlib.sha256(current).hexdigest() == commit["config_fingerprint"]
        except Exception:
            ours = None  # Unknown: never guess that nothing was published.
        if ours is False:
            if isinstance(error, _EnableRefused):
                abort(error.code, error.status)
            if getattr(error, "code", None) == "config_revision_conflict":
                abort("preview_stale")
            abort("config_store_unavailable", 503)
        raise _EnableRefused("recovery_required") from None
    try:
        if written != commit["config_fingerprint"]:
            raise ValueError
        published = config_owner.read_project_config_snapshot(ctx.paths)
        if published.fingerprint != commit["config_fingerprint"]:
            raise ValueError
        # Metadata only: never replace Ctx, paths, jobs, MCP or descriptor (as I6).
        ctx.config.project_identity = published.config.project_identity
        ctx.config.sync = published.config.sync
        transitions.observe_activated_model()
        ctx.sync._activated_model_seen = True
        # Enable's own G: old leases/callbacks can no longer save or join.
        if fence.advance_generation() != commit["binding_generation"]:
            raise ValueError
        publish_enable_journal(ctx.paths.root, change_id, expected, commit, "config_committed")
        # (g) Private confirmed binding of the SAME R+1.
        reserved(lease)
        snapshot = store._save_in_transaction(confirmed,
                                              expected_fingerprint=observation["private_fingerprint"])
        if snapshot.fingerprint != commit["private_fingerprint"]:
            raise ValueError
        publish_enable_journal(ctx.paths.root, change_id, expected, commit, "binding_committed")
        # (h) The namespace's checkpoint: a fresh one for this incarnation, or the
        # same-incarnation one the preview's bases came from. save() itself
        # refuses to overwrite a foreign/newer state: never a fresh reset.
        if checkpoint is None:
            SyncStateV2.fresh(ctx.paths.root, url=row.url, persistent_id=row.persistent_id,
                              space_instance_id=row.space_instance_id).save()
        current = SyncStateV2.load(ctx.paths.root, url=row.url, persistent_id=row.persistent_id)
        if (current is None or current.space_instance_id != row.space_instance_id
                or checkpoint is not None and current != checkpoint):
            raise ValueError
        # (i) Only a completed record is activation evidence for the runner.
        publish_enable_journal(ctx.paths.root, change_id, expected, commit, "completed")
    except Exception:
        raise _EnableRefused("recovery_required") from None
    return {"change_id": change_id, "persistent_id": identity.id,
            "identity_revision": identity.revision, "url": row.url, "enabled": True,
            "config_fingerprint": commit["config_fingerprint"],
            "binding_generation": commit["binding_generation"],
            "profile_id": row.profile_id, "space_instance_id": row.space_instance_id}


def _journals_or_empty(ctx: Any) -> tuple:
    from ..sync.transitions import read_binding_change_journals
    try:
        return read_binding_change_journals(ctx.paths.root)
    except Exception:
        return ()


def _abort_enable(ctx: Any, change_id: str, expected: Any, commit: Any) -> bool:
    """Settle a proven-unpublished intent; False leaves it unresolved (fail closed)."""
    from ..sync.transitions import publish_enable_journal
    try:
        publish_enable_journal(ctx.paths.root, change_id, expected, commit, "aborted")
    except Exception:
        return False
    return True


async def enable_remote_binding(request: Request, ctx: Any, payload: RemoteEnable) -> dict[str, Any]:
    """I12 Enable/catch-up, without the HTTP envelope (studio-design §5.2/§7.7).

    Local admission → (b) preview recheck incl. snapshot (GET only) and CAS of
    R/G/config/private/profile/credential → (c) Verify (GET only) → one
    synchronous section: (d) reserve, (e) journal intent, (f) project R+1
    enabled, (g) private confirmed R+1, (h) v2 checkpoint, (i) journal
    completed. Only then, outside every lock, the runner starts: strict hello
    (ID/epoch/account/cursor) and catch-up. Rooms are offered once connected.
    """
    from ..core.application_config import application_config_transaction
    from ..sync.profiles import ProfileError
    from ..sync.transitions import journal_unresolved, read_binding_change_journals

    activated_model(ctx)
    transitions = ctx.docs.transitions
    previews = transitions.reconcile_previews
    if payload.backend_epoch is not None:
        transitions._require_epoch(payload.backend_epoch)
    if any(journal_unresolved(row) for row in read_binding_change_journals(ctx.paths.root)):
        _remote_refusal("recovery_required")
    if transitions.binding_quiescing:
        _remote_refusal("change_in_progress")
    if ctx.config.sync.enabled:
        _remote_refusal("already_enabled")
    preview = previews.get(payload.preview_id)
    evidence = preview.evidence
    observation = evidence["observation"]
    binding = observation.get("binding") or {}
    if (payload.expected_revision != observation["identity_revision"]
            or payload.config_fingerprint not in {None, observation["config_fingerprint"]}
            or payload.expected_generation != evidence["binding_generation"]
            or payload.confirmation.model_dump() != {
                "url": binding.get("url"), "persistent_id": binding.get("persistent_id"),
                "action": "reconcile"}):
        _remote_refusal("confirmation_required")
    transitions._require_generation(payload.expected_generation)
    base = evidence["checkpoint"]["state"]
    if base not in {"none", "matched"}:
        # Another incarnation's/rolled-back checkpoint owns this namespace. It is
        # retained as recovery evidence, never reset into a fresh one (§4.3/§6).
        _remote_refusal("instance_mismatch" if base == "epoch_mismatch" else "cursor_rollback")
    # (b) Re-read everything, the snapshot included; a refusal discards only the preview.
    await recheck_reconcile_preview(
        request, ctx, payload.preview_id, payload.fingerprint,
        expected_revision=payload.expected_revision,
        config_fingerprint=observation["config_fingerprint"], backend_epoch=payload.backend_epoch)
    # (c) The account the hello must present.
    account = await _enable_verified_account(request, dict(observation["profile"]))
    lifecycle, owner = ctx.remote_profile_lifecycle, ctx.remote_reservations
    if lifecycle is None or owner is None:
        _remote_refusal("consumer_lifecycle_unavailable", 503)
    refused = result = None
    try:
        async with request.app.state.project_lock:
            async with transitions.final_snapshot() as fence:
                # A concurrent Enable may have consumed it while this one waited.
                previews.get(payload.preview_id)
                with lifecycle._critical():
                    with application_config_transaction():
                        with credentials._file_lock:
                            result = _publish_enable(ctx, preview, account, fence, owner)
    except _EnableRefused as error:
        refused = error
    except ProfileError:
        refused = _EnableRefused("consumer_lifecycle_unavailable", 503)
    if refused is not None:
        if refused.code == "preview_stale":
            previews.discard(payload.preview_id)
        _remote_refusal(refused.code, refused.status)
    previews.discard(payload.preview_id)
    await ctx.bus.publish(EventType.REMOTE_BINDING_CHANGE, {
        "change_id": result["change_id"], "state": "enabled", "operation": "enable",
        "binding_generation": result["binding_generation"],
        "expected_generation": payload.expected_generation, "fenced": False,
        "required_editor_acks": [], "missing_acks": [], "reason": None})
    try:
        # Runner → strict hello → `sync since cursor`. Never before completion.
        await ctx.sync.reconfigure()
    except Exception:
        await ctx.sync._set_status("blocked", "runner_unavailable")
    status = ctx.sync.status()
    return {**result, "backend_epoch": transitions.backend_epoch,
            "state": status["state"],
            "reason": status["message"] if status["state"] in {"blocked", "unauthorised"} else None,
            "has_token": status["has_token"], "active_rooms": len(ctx.sync._sessions),
            "pending_writes": len(ctx.sync._pending) + len(ctx.sync._live_writes)}


@remote_router.post("/enable")
async def enable_remote(payload: RemoteEnable, request: Request, ctx: Ctx) -> dict[str, Any]:
    """Separate Enable after a current preview and exact confirmation (I12)."""
    result = await enable_remote_binding(request, ctx, payload)
    scope = request.state.project_scope
    return {"project_id": scope.project_id, "epoch": scope.epoch, **result}


class SyncUpdate(BaseModel):
    """A patch: a field left out is left alone."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    url: str | None = None
    space: str | None = None
    token: str | None = None
    """The user's key for the server at ``url``. An empty string forgets the stored one."""


@router.get("")
async def get_sync(ctx: Ctx) -> dict[str, Any]:
    active = activated_model(ctx)
    status = ctx.sync.status()
    if active:
        status = {**status, "space": ctx.config.project_identity.id if ctx.config.project_identity else ""}
    return status


@router.put("")
async def update_sync(payload: SyncUpdate, request: Request, ctx: Ctx) -> dict[str, Any]:
    """Save the settings and reconnect with them.

    Validated whole before anything is stored: an address that does not parse must not leave
    a key filed under it. The address and the space go to the project's ``config.yaml``; the
    key goes to the user's config directory, under the address it is for.
    """
    sent = payload.model_fields_set
    # The lock every other settings write holds (§5.2), so two saves cannot interleave.
    async with request.app.state.project_lock:
        if activated_model(ctx) or ctx.docs.transitions.binding_quiescing:
            # Disable is a guarded transition too, never the old cancel path.
            raise HTTPException(409, {"code": "use_remote_settings", "details": {}},
                                headers={"Cache-Control": "no-store"})
        draft = ctx.config.sync.model_copy()
        if "url" in sent and payload.url is not None:
            draft.url = payload.url.strip()
        if "space" in sent and payload.space is not None:
            draft.space = payload.space.strip()
        if "enabled" in sent and payload.enabled is not None:
            draft.enabled = payload.enabled

        url: str | None = None
        try:
            if draft.url:
                url = normalise_url(draft.url)
            if draft.space:
                draft.space = check_space_name(draft.space)
            if draft.enabled and (url is None or not draft.space):
                raise ValueError("Online sync needs a server address and a space")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if "token" in sent and payload.token is not None:
            if url is None:
                raise HTTPException(status_code=400, detail="A key is stored for a server address")
            credentials.save_token(url, payload.token.strip() or None)

        if draft != ctx.config.sync:
            ctx.config.sync = draft
            save_config(ctx.paths, ctx.config)
        return await ctx.sync.reconfigure()
