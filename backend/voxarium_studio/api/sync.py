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
        "reason": change_state["reason"] if ctx.docs.transitions.binding_quiescing else None,
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
    # R3/I9 own authoritative catalog observations. A browser's ID/incarnation
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


def _select_observation(ctx: Any, payload: RemotePrepare) -> dict[str, Any]:
    """I6 test/in-process observation seam; production is closed until I9/R3.

    No request flag, transport, token lookup, v1 fallback or catalog fabrication.
    The injected owner must return an exact validated snapshot, rechecked at CAS.
    """
    from ..core.application_config import RemoteServerProfile
    from ..sync.bindings import BindingRecord
    from ..sync.profiles import RemoteProfiles
    observer = getattr(ctx, "remote_select_observer", None)
    if observer is None:
        _remote_refusal("catalog_unavailable", 503)
    try:
        source = observer(payload.profile_id, payload.persistent_id)
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
async def prepare_remote(payload: RemotePrepare, ctx: Ctx) -> dict[str, Any]:
    """Read-only preparation for the separate disabled identity/config commit."""
    activated_model(ctx)
    from ..sync.transitions import read_binding_change_journals
    if any(row["phase"] != "completed" for row in read_binding_change_journals(ctx.paths.root)):
        _remote_refusal("recovery_required")
    if payload.backend_epoch is not None:
        ctx.docs.transitions._require_epoch(payload.backend_epoch)
    observed = _prepare_observation(ctx)
    if (observed["identity_revision"] != payload.expected_revision
            or observed["config_fingerprint"] != payload.config_fingerprint):
        _remote_refusal("stale_binding")
    selection = _select_observation(ctx, payload) if payload.operation == "select" else None
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
    if any(row["change_id"] == change_id and row["phase"] == "completed"
           for row in read_binding_change_journals(ctx.paths.root)):
        _remote_refusal("already_committed")
    result = ctx.docs.transitions.cancel_change(change_id)
    await ctx.bus.publish(EventType.REMOTE_BINDING_CHANGE, result)
    return result


def _commit_selection(ctx, expected):
    """Repeat profile/credential/catalog CAS, including label revision drift."""
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
        if _select_observation(ctx, payload) != source:
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
    completed = next((row for row in records
                      if row["change_id"] == change_id and row["phase"] == "completed"), None)
    if completed is not None:
        result = _confirmed_completed_result(ctx, payload, completed)
        return _committed_response(request, ctx, change_id, result)

    change = transitions.require_ready_commit(change_id, **payload.model_dump(exclude={"confirmation"}),
                                               confirmation=confirmation)
    expected = change.expected
    # All fallible I/O below leaves admission fenced; never resurrect old leases.
    async with request.app.state.project_lock:
        # A same-change request may have committed while this one waited for
        # an ordinary settings writer or another command. Return its durable
        # result instead of trying to drain/advance the identity a second time.
        completed = next((row for row in read_binding_change_journals(ctx.paths.root)
                          if row["change_id"] == change_id and row["phase"] == "completed"), None)
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
                        _commit_selection(ctx, expected)
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
                                _commit_selection(ctx, expected)
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
                            _commit_selection(ctx, expected)
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
