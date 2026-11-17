"""The Studio's connection to a Voxarium Studio Remote (§16).

One socket, two jobs — the same two the remote's hub keeps apart:

**Documents (§16.3).** ``.voxarium_studio/docs`` is mirrored through the server. Whatever
changes on disk here — the editor's save, an agent's ``write_doc``, a checkout — is noticed
by comparing the tree with what was last agreed (:mod:`.state`) and pushed; whatever changed
on somebody else's disk arrives as a ``change`` and is written here through the ordinary
:class:`~voxarium_studio.teams.docs.DocStore`, so the browser hears about it the way it hears
about an agent's write. Detection is by comparison rather than by intercepting writes, which
is what makes every way a file can change — including the ones nobody thought of — one case.

**Rooms (§16.4).** A browser that opens a document is given a socket to this backend
(``/ws/collab``), and this backend relays it into the server's room for that document. The
frames are Yjs updates and cursor positions; nothing here reads them. The browser never
talks to the remote itself — the key stays in this process.

Three rules carry the correctness, and each is commented where it is implemented:

- nothing is pushed for a path that already has a push in flight;
- a remote change is only written over a file that has not been touched since the last
  agreed revision — anything else is a conflict, resolved without losing either side;
- the Markdown a live room's scribe saves is marked as such, so it is taken by the server
  whatever revision this Studio last saw, and is never mistaken for a conflicting edit.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import random
import re
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from ..core.config import AppConfig
from ..core.paths import ProjectPaths
from ..explorer.service import PathOutsideProject
from ..teams.docs import MAX_DOC_BYTES, DocStore
from ..ws.bus import EventBus, Subscription
from ..ws.events import EventType
from . import credentials
from .protocol import (
    CLOSE_BAD_REQUEST,
    CLOSE_COLLAB_RESET,
    CLOSE_COLLAB_UNAVAILABLE,
    CLOSE_UNAUTHORISED,
    PROTOCOL_VERSION,
    check_space_name,
    content_hash,
    is_insecure,
    normalise_url,
    socket_url,
)
from .state import Base, SyncNamespace, SyncState, SyncStateV2
from .transitions import EditorLeaseError, MutationBusy, MutationGateError, StaleBindingGeneration


@dataclass(frozen=True)
class ActivatedRemoteTarget:
    """Input from the future I6 completed-activation owner, NEVER inferred.

    I2 supplies no durable marker/reader or production activation path. The
    default context reader returns None, preserving genuinely legacy projects.
    Tests inject this evidence; I6 must obtain it from its strict disk journal
    and I10 must supply a compatible, separately enabled runner (§2.3/§3.1).
    """

    canonical_root: str
    config_fingerprint: str
    profile_id: str
    url: str
    persistent_id: str
    identity_revision: int
    space_instance_id: str


@dataclass(frozen=True)
class RemoteStartupBinding:
    """Read-only namespace observation, not permission to Enable or connect."""

    target: ActivatedRemoteTarget
    private_fingerprint: str | None
    namespace: SyncNamespace
    checkpoint: SyncStateV2 | None  # Missing is only a Preview candidate.
    enabled: bool


def preflight_remote_binding(
    paths: ProjectPaths, config: AppConfig, target: ActivatedRemoteTarget | None, *,
    home: Path, profile_reader: Callable[[str], Any],
) -> RemoteStartupBinding:
    """Exact disk/project/private/profile/v2 checks, without writes or keys (§4.3).

    No legacy state import, fresh checkpoint save, credential provider or socket
    is reachable here. The caller repeats this synchronous observation after
    admission and before publication; there is no thread/file lock across await.
    """
    import yaml

    from ..core.paths import user_config_dir
    from ..core.project_identity import ProjectIdentity, _PlanConfigLoader
    from ..core.project_scope import canonical_root
    from .bindings import BindingError, RemoteBindings
    from .state import SyncNamespace, SyncStateError, SyncStateV2

    try:
        if (type(target) is not ActivatedRemoteTarget or user_config_dir() != home
                or str(canonical_root(paths.root)) != target.canonical_root
                or type(target.identity_revision) is not int):
            raise BindingError("activation_unavailable")
        content = paths.config_file.read_bytes()
        if hashlib.sha256(content).hexdigest() != target.config_fingerprint:
            raise BindingError("activation_unavailable")
        raw = yaml.load(content.decode("utf-8"), Loader=_PlanConfigLoader)
        if not isinstance(raw, dict) or not isinstance(raw.get("sync"), dict):
            raise BindingError("activation_unavailable")
        settings = raw["sync"]
        identity = ProjectIdentity.model_validate(raw.get("project_identity"))
        if ("space" in settings or type(settings.get("enabled")) is not bool
                or settings.get("url") != target.url or normalise_url(target.url) != target.url
                or identity.id != target.persistent_id or identity.revision != target.identity_revision
                or config.project_identity != identity or config.sync.url != target.url
                or config.sync.enabled != settings["enabled"]):
            raise BindingError("activation_unavailable")
        private = RemoteBindings().read()
        # Missing/selected/clone refuse BEFORE even metadata lookup, let alone a key.
        row = private.get(paths.root)
        if (row is None or row.selection_state != "confirmed"
                or row.profile_id != target.profile_id or row.url != target.url
                or row.persistent_id != identity.id or row.identity_revision != identity.revision
                or row.space_instance_id != target.space_instance_id):
            raise BindingError("binding_required")
        profile = profile_reader(target.profile_id)
        private.require_confirmed(paths.root, profile=profile, identity=identity,
                                  url=target.url, space_instance_id=target.space_instance_id)
        namespace = SyncNamespace.for_project(paths.root, url=target.url, persistent_id=identity.id)
        checkpoint = SyncStateV2.load(paths.root, url=target.url, persistent_id=identity.id)
        if checkpoint is not None and checkpoint.space_instance_id != target.space_instance_id:
            raise SyncStateError("instance_mismatch")
        if user_config_dir() != home:
            raise BindingError("activation_unavailable")
        return RemoteStartupBinding(target, private.fingerprint, namespace, checkpoint, settings["enabled"])
    except (BindingError, SyncStateError):
        raise
    except Exception:
        # Do not reflect disk bytes, URL credentials, validation inputs or OS errors.
        raise BindingError("activation_unavailable") from None

log = logging.getLogger(__name__)

PUSH_DEBOUNCE_SECONDS = 0.15
"""How long a burst of local changes is allowed to settle before the tree is compared."""
RESCAN_SECONDS = 30.0
"""The tree is compared this often even if nothing announced a change — the file watcher is
best-effort on network drives, and a comparison that finds nothing costs a ``stat`` per file."""
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
HELLO_TIMEOUT_SECONDS = 15.0
FLUSH_TIMEOUT_SECONDS = 8.0
MAX_FRAME_BYTES = 64 * 1024 * 1024
JOIN_ATTEMPTS = 3

# Frames the remote addresses to one browser session; relayed with the session id removed.
_SESSION_FRAMES = frozenset(
    {"joined", "join_failed", "seeded", "update", "awareness", "peer_joined", "peer_left",
     "scribe", "reset"}
)
# Frames a browser may send into its room; relayed with the session id added.
_BROWSER_FRAMES = frozenset({"seed", "update", "awareness"})

_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f<>:"|?*/\\\[\]]+')


class _Stop(Exception):
    """The connection ended for a reason retrying cannot fix."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state
        self.message = message


@dataclass(slots=True)
class _Remote:
    """A document as the server has it."""

    rev: int
    hash: str | None
    content: str | None


@dataclass(slots=True)
class _Pending:
    path: str
    hash: str | None
    """What was sent: the text's hash, or ``None`` for a delete."""
    base_rev: int = 0
    generation: int | None = None
    target: tuple[str, str, str] | None = None
    sid: str | None = None
    base_hash: str | None = None
    room_save: _RoomAnchor | None = None


@dataclass(slots=True)
class _LiveWrite:
    hash: str
    sid: str
    room_save: _RoomAnchor | None = None


@dataclass(frozen=True)
class _RoomAnchor:
    epoch: str
    serial: int
    rev: int
    hash: str
    writer: tuple[str, int, int, int] | None = None


@dataclass(frozen=True)
class _Materialization:
    token: str
    target: tuple[str, str, str]
    generation: int
    version: int
    revision: int
    anchor: _RoomAnchor
    content: bytes


@dataclass(slots=True)
class _Session:
    """One browser editing one document through this backend."""

    sid: str
    path: str
    websocket: WebSocket
    client_id: int
    binding_generation: int = 0
    editor_id: str | None = None
    backend_epoch: str | None = None
    client_instance_id: str | None = None
    registered: bool = False
    closed: bool = False
    content: str = ""
    """The file's text at the revision the room was joined at — what the browser seeds an
    empty room from, so every member seeds from the same bytes."""
    attempts: int = 0
    room_epoch: str | None = None
    room_ready: bool = False
    clean_join: _RoomAnchor | None = None
    materialization: _Materialization | None = None
    clean_lease_revision: int | None = None
    materialized: bool = False
    seed_pending: bool = False


class InvalidCollabSession(MutationGateError):
    """A submitted sid is not permission to fall back to an ordinary save (I4)."""

    code = "unknown_collab_session"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class BindingDrainProof:
    """In-process receipt, NOT a JSON/client assertion or Enable permission (I5).

    Only ``RemoteSync.validate_binding_drain`` authenticates this receipt. A
    blocked receipt never authorizes commit, even when its pending count is zero.
    """

    change_id: str
    generation: int
    target: tuple[str, str, str]
    reconciled: bool
    left_sids: tuple[str, ...]
    runner_stopped: bool
    fenced: bool
    reason: str | None
    _owner: object = field(repr=False, compare=False)
    corpus_fingerprint: str | None = None
    checkpoint_fingerprint: str | None = None


class RemoteSync:
    def __init__(
        self,
        paths: ProjectPaths,
        config: Callable[[], AppConfig],
        docs: DocStore,
        bus: EventBus,
        *,
        token: Callable[[str], str | None] = credentials.load_token,
    ) -> None:
        self.paths = paths
        self._config = config
        self.docs = docs
        self.bus = bus
        self._token = token

        self._runner: asyncio.Task[None] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._subscription: Subscription | None = None
        self._ws: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        # Held while the tree is compared and pushed, and while a remote change is applied:
        # the two must not interleave, or a file written from the server could be read back
        # as a local edit before its revision was recorded. Never held while waiting for the
        # server to answer — the answer is read by the task that would need the lock.
        self._lock = asyncio.Lock()

        self._state_name = "disabled"
        self._message: str | None = None
        self._url = ""
        self._space = ""
        self._user: dict[str, Any] | None = None
        self._peers: list[dict[str, Any]] = []

        self._state: SyncState | None = None
        self._synced = False
        self._dirty = asyncio.Event()
        self._settled = asyncio.Event()
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._pending_paths: set[str] = set()
        self._skip: dict[str, str | None] = {}
        """Paths left alone while their text is still the one that could not be synced —
        refused by the server, or unwritable here — so the same failure is not retried on
        every comparison. An edit to the file changes its hash and lifts the skip."""
        self._live_writes: dict[str, _LiveWrite] = {}
        self._sessions: dict[str, _Session] = {}
        self._room_serial: dict[str, int] = {}
        self._room_anchors: dict[str, _RoomAnchor] = {}
        self._hashes: dict[str, tuple[int, int, str]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._binding_quiescing = False
        self._binding_resumed = asyncio.Event()
        self._binding_resumed.set()
        self._binding_fenced = False
        self._binding_draining = False
        self._binding_target: tuple[str, str, str] | None = None
        self._binding_generation: int | None = None
        self._binding_proof: BindingDrainProof | None = None
        self._binding_evidence_sink: Callable[[dict[str, Any]], None] | None = None
        self._retained_evidence: list[dict[str, Any]] = []
        self._binding_failure: str | None = None

    # ----------------------------------------------------- binding cutover (I5)

    def _room_activity(self, path: str, *, invalidate_leases: bool = True) -> None:
        """Conservative local observation, NOT a Remote Yjs/version watermark."""
        self._room_serial[path] = self._room_serial.get(path, 0) + 1
        self._room_anchors.pop(path, None)
        for session in self._sessions.values():
            if session.path == path:
                session.materialization = None
                session.clean_join = None
                session.seed_pending = False
        if invalidate_leases:
            self.docs.transitions.invalidate_room_materialization(path)

    def _room_anchor_valid(self, session: _Session, anchor: _RoomAnchor) -> bool:
        base = self._state.docs.get(session.path) if self._state is not None else None
        if (not session.room_ready or session.closed or not session.registered
                or not self.live or not self._synced or self._ws is None
                or self._sessions.get(session.sid) is not session
                or self._binding_fenced or self._binding_failure or self._retained_evidence
                or session.room_epoch != anchor.epoch
                or self._room_serial.get(session.path, 0) != anchor.serial
                or self._room_anchors.get(session.path) is not anchor
                or base is None or (base.rev, base.hash) != (anchor.rev, anchor.hash)
                or session.path in self._pending_paths or session.path in self._live_writes
                or session.path in self._skip
                or any(p.path == session.path for p in self._pending.values())):
            return False
        if anchor.writer is not None:
            eid, version, revision, receipt = anchor.writer
            writer = self.docs.transitions._editors.get(eid)
            if (writer is None or writer.saves or not writer.confirmed or writer.dirty
                    or writer.generation != session.binding_generation
                    or self.docs.transitions._monotonic() >= writer.deadline
                    or (writer.saved_client_version, writer.saved_revision) != (version, revision)
                    or type(writer.last_saved_version) is not int or writer.last_saved_version < receipt):
                return False
        else:
            lease = self.docs.transitions._editors.get(session.editor_id)
            if (lease is None or not lease.initial_materialization or lease.version != 0 or lease.minimum_save_version
                    or lease.revision != session.clean_lease_revision):
                return False
        return not any(lease.saves for lease in self.docs.transitions._editors.values()
                       if lease.document_path == session.path)

    def materialization(
        self, editor_id: str, backend_epoch: str, expected_binding_generation: int, *,
        document_path: str, client_instance_id: str, sid: str, version: int, revision: int,
        proof_id: str | None = None, content: str | None = None,
    ) -> dict[str, object]:
        """Two-step local editor snapshot proof; no writes/send/await (I8).

        Materialized Markdown is editor input just as in a save, never a clean
        boolean. An agreed room/save anchor AND a fresh exact disk read are
        required. A generic remote change has no room identity and cannot mint
        this evidence. See remote-identity/i8-editor-proof for frontend ordering.
        """
        gate = self.docs.transitions
        captured = dict(document_path=document_path, client_instance_id=client_instance_id,
                        sid=sid, version=version, revision=revision)
        lease = gate.materialization_lease(editor_id, backend_epoch, expected_binding_generation, **captured)
        session = self._sessions.get(sid)
        if (session is None or session.editor_id != editor_id or session.path != document_path
                or session.backend_epoch != backend_epoch
                or session.client_instance_id != client_instance_id
                or session.binding_generation != expected_binding_generation):
            raise InvalidCollabSession()
        if proof_id is None:
            anchor = self._room_anchors.get(document_path)
            if anchor is None or not self._room_anchor_valid(session, anchor):
                raise EditorLeaseError("pending_edits")
            try:
                data = self.docs.resolve(document_path).read_bytes()
                text = data.decode("utf-8")
            except (OSError, ValueError):
                raise EditorLeaseError("pending_edits") from None
            if len(data) > MAX_DOC_BYTES or content_hash(text) != anchor.hash:
                raise EditorLeaseError("pending_edits")
            gate.materialization_lease(editor_id, backend_epoch, expected_binding_generation, **captured)
            ticket = _Materialization(uuid.uuid4().hex, self._target_tuple(), expected_binding_generation,
                                      version, revision, anchor, data)
            session.materialization = ticket
            return {**gate._editor_dto(lease), "proof_id": ticket.token}
        ticket, session.materialization = session.materialization, None
        try:
            submitted = content.encode("utf-8") if content is not None else None
        except UnicodeError:
            raise EditorLeaseError("pending_edits") from None
        if (ticket is None or ticket.token != proof_id or content is None
                or (ticket.version, ticket.revision) != (version, revision)
                or len(submitted) > MAX_DOC_BYTES or submitted != ticket.content):
            raise EditorLeaseError("pending_edits")

        def check() -> bool:
            # After an intentional I5 fence, corpus/checkpoint fingerprints
            # guard stop/leave. Preserve evidence across that destructive close,
            # never across a disconnect that merely emptied pending/settled.
            if ticket.target != self._target_tuple():
                return False
            if self._binding_fenced:
                if (self._binding_target != ticket.target
                        or self._binding_generation != ticket.generation):
                    return False
            elif (gate.generation != ticket.generation or gate._monotonic() >= lease.deadline
                  or not self._room_anchor_valid(session, ticket.anchor)):
                return False
            try:
                return self.docs.resolve(document_path).read_bytes() == ticket.content
            except (OSError, ValueError):
                return False

        result = gate.confirm_materialized(editor_id, backend_epoch, expected_binding_generation,
                                           check=check, **captured)
        session.materialized = True
        return result

    def _materialization_acks_ready(self) -> bool:
        gate = self.docs.transitions
        return all(gate.room_drain_ready(s.sid, s.path, s.editor_id, s.binding_generation)
                   for s in self._sessions.values() if s.materialized or gate._change is not None)

    @property
    def binding_fenced(self) -> bool:
        """Irreversible boundary, including when drain was cancelled mid-stop."""
        return self._binding_fenced

    def _target_tuple(self) -> tuple[str, str, str]:
        binding = getattr(self, "_startup_binding", None)
        if binding is not None:
            target = binding.target
            return target.canonical_root, target.url, target.persistent_id
        state = self._state
        try:
            url = normalise_url(state.url if state is not None else self._url)
        except ValueError:
            # Invalid legacy configuration may contain userinfo/query secrets;
            # never copy raw settings into a recovery journal.
            url = ""
        return (str(self.paths.root.resolve()), url,
                str(getattr(state, "space_id", self._space)))

    def set_binding_evidence_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Trusted I6 journal hook: synchronous durable write, no locks/await.

        The coordinator installs this BEFORE quiesce. The sink must atomically
        persist the nonsecret snapshot or raise; exceptions are never swallowed
        to authorize destructive cleanup. No new journal/store is owned here.
        """
        if self._binding_quiescing or not callable(sink):
            raise RuntimeError("binding_evidence_sink_unavailable")
        self._binding_evidence_sink = sink

    def binding_evidence(self) -> dict[str, Any]:
        """Nonsecret old-target recovery evidence; never Markdown, Yjs or keys."""
        state = self._state
        return {
            "target": list(self._binding_target or self._target_tuple()),
            "quiescing": self._binding_quiescing,
            "fenced": self._binding_fenced,
            "generation": self._binding_generation if self._binding_generation is not None
                else self.docs.transitions.generation,
            "cursor": state.cursor if state is not None else None,
            "space_instance_id": getattr(
                getattr(getattr(self, "_startup_binding", None), "target", None),
                "space_instance_id", getattr(state, "space_instance_id", getattr(state, "space_id", None)),
            ),
            "pending": [
                {"id": mid, "path": pending.path, "hash": pending.hash,
                 "base_rev": pending.base_rev, "base_hash": pending.base_hash,
                 "generation": pending.generation,
                 "target": list(pending.target or self._target_tuple()), "sid": pending.sid}
                for mid, pending in sorted(self._pending.items())
            ],
            "live": [{"path": path, "hash": live.hash, "sid": live.sid}
                     for path, live in sorted(self._live_writes.items())],
            "rooms": [{"sid": session.sid, "path": session.path,
                       "generation": session.binding_generation}
                      for session in self._sessions.values()],
            "retained": list(self._retained_evidence),
            "reason": self._binding_failure,
        }

    def _preserve_binding_evidence(self, reason: str) -> None:
        snapshot = self.binding_evidence()
        snapshot.pop("retained")  # No recursive history.
        snapshot["reason"] = reason
        if snapshot["pending"] or snapshot["live"] or snapshot["rooms"] and not self._binding_fenced:
            previous = next((row for row in self._retained_evidence
                             if row["target"] == snapshot["target"]
                             and row["generation"] == snapshot["generation"]), None)
            if previous is None:
                self._retained_evidence.append(snapshot)
            else:
                for key in ("pending", "live", "rooms"):
                    for item in snapshot[key]:
                        if item not in previous[key]:
                            previous[key].append(item)
        if self._binding_evidence_sink is not None:
            # Include every previous ambiguous send, not just the most recent
            # disconnect's queue. A sink failure leaves RAM evidence intact.
            durable = self.binding_evidence()
            durable["reason"] = reason
            self._binding_evidence_sink(durable)

    def begin_binding_quiesce(self) -> None:
        """Synchronous join/rejoin/reconnect gate, before the first await.

        The coordinator separately freezes/flushes and verifies editor leases.
        Existing old-G scribe saves remain possible until the fence. Neither
        this method nor an empty queue proves editor cleanliness (spec §7).
        """
        if self._binding_quiescing:
            return
        self._binding_target = self._target_tuple()
        self._binding_generation = self.docs.transitions.generation
        self._binding_quiescing = True
        self._binding_resumed.clear()
        self._binding_proof = None

    def cancel_binding_quiesce(self, *, fenced: bool = False) -> None:
        """Resume only before fence. After any leave/stop, explicit resume required."""
        self._binding_fenced |= fenced
        if self._binding_draining:
            raise RuntimeError("binding_drain_in_progress")
        self._binding_proof = None
        if not self._binding_fenced:
            self._binding_quiescing = False
            self._binding_target = None
            self._binding_generation = None
            self._binding_resumed.set()
            self._dirty.set()

    def validate_binding_drain(self, proof: BindingDrainProof, change_id: str) -> bool:
        """Authenticate the exact current receipt; never trust deserialized flags."""
        valid = (type(proof) is BindingDrainProof and proof is self._binding_proof
                and proof._owner is self and proof.change_id == change_id
                and proof.generation == self.docs.transitions.generation
                and proof.target == self._binding_target and self._binding_fenced
                and proof.reconciled and proof.runner_stopped and proof.reason is None
                and self._runner is None and self._ws is None
                and not self._pending and not self._pending_paths
                and not self._live_writes and not self._sessions
                and not self._tasks and not self._retained_evidence)
        if not valid or proof.corpus_fingerprint is None:
            return False
        try:
            return (self._binding_corpus()[1] == proof.corpus_fingerprint
                    and self._binding_checkpoint_fingerprint() == proof.checkpoint_fingerprint)
        except (OSError, ValueError, PathOutsideProject):
            return False

    def _binding_corpus(self) -> tuple[dict[str, str], str]:
        """Uncached raw corpus + wire hashes, using the existing DocStore owner.

        The ordinary watcher may cache mtime/size; final drain evidence may not.
        Strict snapshot reads also fail closed on unreadable/vanishing files,
        unlike the legacy best-effort scan. No lock/await is introduced here.
        """
        documents = self.docs._snapshot_docs()
        return ({path: content_hash(data.decode("utf-8", errors="replace"))
                 for path, data in documents}, self.docs._snapshot_fingerprint(documents))

    def _binding_checkpoint_fingerprint(self) -> str | None:
        state = self._state
        if state is None:
            return None
        checkpoint = (state.cursor, [(path, base.rev, base.hash)
                                     for path, base in sorted(state.docs.items())])
        return hashlib.sha256(json.dumps(checkpoint, ensure_ascii=False).encode("utf-8")).hexdigest()

    async def drain_binding_change(self, change_id: str, timeout: float) -> BindingDrainProof:
        """Exact ACK/nack -> leave -> fence -> joined runner stop (spec §7/I5).

        Call only AFTER backend editor ACK validation. No identity/send/state/
        DocStore lock may be held by the caller. Before R3 this supports only
        the existing legacy runner or an injected in-process fake new-model
        runner; it does not create/enable a transport or alter config/identity.
        A timeout leaves rooms and their browser text intact. A failure after
        leave stays fenced/paused. ``settled`` is only a wakeup, never proof.
        """
        if (not isinstance(change_id, str) or not change_id or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("invalid_binding_drain")
        if not self._binding_quiescing or self._binding_draining:
            raise RuntimeError("binding_quiesce_required")
        if self._binding_proof is not None and self.validate_binding_drain(self._binding_proof, change_id):
            return self._binding_proof
        self._binding_draining = True
        reason: str | None = None
        reconciled = False
        corpus: str | None = None
        checkpoint: str | None = None
        left: list[str] = []
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self._settled.clear()
                    if (self._binding_generation != self.docs.transitions.generation
                            or self._binding_target != self._target_tuple()):
                        reason = "stale_binding"
                        break
                    if self._binding_failure or self._retained_evidence:
                        reason = self._binding_failure or "unresolved_old_target"
                        break
                    if self._binding_fenced:
                        reason = "resume_required"
                        break
                    async with self._lock:
                        await self._push_local()
                        state = self._state
                        local, observed_corpus = self._binding_corpus()
                        known = state.docs if state is not None else {}
                        dirty = state is not None and any(
                            local.get(path) != (known[path].hash if path in known else None)
                            for path in local.keys() | known.keys()
                        )
                        ready = not (self._pending or self._pending_paths
                                     or self._live_writes or dirty or self._skip)
                        # I8 proof receipts stay fresh until the irreversible
                        # fence. Do not drain after a post-ACK room/disk race.
                        if ready and not self._materialization_acks_ready():
                            reason = "pending_edits"
                            break
                    if ready:
                        if self._sessions and (self._ws is None or not self._synced):
                            reason = "offline_unresolved"
                            break
                        reconciled = True
                        corpus = observed_corpus
                        checkpoint = self._binding_checkpoint_fingerprint()
                        break
                    if self._ws is None or not self._synced:
                        reason = "offline_unresolved"
                        break
                    # All locks above have been released before ACK reception.
                    await self._settled.wait()
                if reconciled:
                    # Fail closed before destructive leave if the activated
                    # owner has not supplied its durable journal seam.
                    if self._requires_editor_lease() and self._binding_evidence_sink is None:
                        reason = "binding_journal_unavailable"
                    else:
                        if self._binding_evidence_sink is not None:
                            self._binding_evidence_sink(self.binding_evidence())
                        if (self._binding_corpus()[1] != corpus
                                or self._binding_checkpoint_fingerprint() != checkpoint):
                            reconciled, reason = False, "stale_binding"
                        elif not self._materialization_acks_ready():
                            reconciled, reason = False, "pending_edits"
                        else:
                            self._binding_fenced = True
                            for session in list(self._sessions.values()):
                                await self._send({"t": "leave", "sid": session.sid})
                                left.append(session.sid)
                            ws = self._ws
                            if ws is not None:
                                await ws.close()
                            # Stop really joins; cancellation/error cannot mint
                            # proof or release failed-cleanup ownership leases.
                            await self._stop_runner()
                            if self._binding_evidence_sink is not None:
                                self._binding_evidence_sink(self.binding_evidence())
                            # Leave/close/join are await boundaries. A same-stat
                            # external edit after the final ACK is NOT drained.
                            if (self._binding_corpus()[1] != corpus
                                    or self._binding_checkpoint_fingerprint() != checkpoint):
                                reconciled, reason = False, "stale_binding"
        except TimeoutError:
            reason = "drain_timeout"
        except asyncio.CancelledError:
            self._preserve_binding_evidence("drain_cancelled")
            raise
        except Exception:
            reason = "drain_failed"
            self._preserve_binding_evidence(reason)
        finally:
            self._binding_draining = False
        proof = BindingDrainProof(
            change_id, self._binding_generation, self._binding_target,
            reconciled, tuple(left), self._runner is None and self._ws is None,
            self._binding_fenced, reason, self, corpus, checkpoint,
        )
        self._binding_proof = proof
        return proof

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._binding_quiescing or self._binding_fenced:
            return
        if self._watcher is None:
            self._subscription = self.bus.subscribe()
            self._watcher = asyncio.create_task(self._watch_bus(), name="sync-watch")
        await self._start_runner()

    async def stop(self) -> None:
        await self._stop_runner()
        if self._watcher is not None:
            await self._cancel_join(self._watcher)
            self._watcher = None
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

    async def reconfigure(self) -> dict[str, Any]:
        """Adopt the settings as they are now — a changed address, space, key or switch."""
        if self._binding_quiescing or self._binding_fenced:
            raise RuntimeError("binding_quiescing")
        await self._stop_runner()
        await self._start_runner()
        return self.status()

    async def _start_runner(self) -> None:
        if self._binding_quiescing or self._binding_fenced:
            return
        settings = self._config().sync
        self._url, self._space = settings.url, settings.space
        self._user, self._peers = None, []
        if not settings.enabled:
            await self._set_status("disabled")
            return
        try:
            url = normalise_url(settings.url)
            space = check_space_name(settings.space)
        except ValueError as exc:
            await self._set_status("unconfigured", str(exc))
            return
        token = self._token(url)
        if not token:
            await self._set_status("unconfigured", "No key is stored for this server")
            return
        self._runner = asyncio.create_task(self._run(url, space, token), name="sync-remote")

    async def _stop_runner(self) -> None:
        # Keep references until join AND cleanup complete. A cancelled stop is
        # not proof of termination and must not release its reservation (§3.1/I2).
        self._preserve_binding_evidence("runner_stop")
        runner = self._runner
        for task in (runner, *self._tasks):
            if task is not None:
                await self._cancel_join(task)
        await self._disconnected()
        if runner is not None:
            # A cancelled connection has no chance to say it ended; whatever starts next
            # (or nothing, at shutdown) must not inherit "connected".
            await self._set_status("offline")
        self._runner = None

    @staticmethod
    async def _cancel_join(task: asyncio.Task[Any]) -> None:
        # Repeated stop/cancellation must not inject another cancellation into a
        # runner which is awaiting its transport's finally/close work.
        if not task.done() and not task.cancelling():
            task.cancel()
        current = asyncio.current_task()
        cancellations = current.cancelling() if current is not None else 0
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A startup cancellation may already have been caught by the caller,
            # which is now cleaning up in that SAME task. Its old cancellation
            # count is not a new interruption of this join.
            if not task.cancelled() or current is not None and current.cancelling() > cancellations:
                raise

    def _may_inspect_token(self) -> bool:
        return True

    # ------------------------------------------------------------------ status

    @property
    def live(self) -> bool:
        """Connected and caught up — the only state rooms are offered in."""
        return self._state_name == "connected"

    def status(self) -> dict[str, Any]:
        settings = self._config().sync
        has_token = False
        insecure = False
        with contextlib.suppress(ValueError):
            url = normalise_url(settings.url)
            if self._may_inspect_token():
                has_token = bool(self._token(url))
            insecure = is_insecure(url)
        me = (self._user or {}).get("id")
        return {
            "enabled": settings.enabled,
            "url": settings.url,
            "space": settings.space,
            "has_token": has_token,
            "insecure": insecure,
            "state": self._state_name,
            "message": self._message,
            "user": self._user,
            "peers": [{**peer, "self": peer.get("user", {}).get("id") == me} for peer in self._peers],
        }

    async def _set_status(self, state: str, message: str | None = None) -> None:
        if (state, message) == (self._state_name, self._message):
            return
        self._state_name, self._message = state, message
        await self._publish_status()

    async def _publish_status(self) -> None:
        await self.bus.publish(EventType.SYNC_STATUS, self.status())

    # ------------------------------------------------------------------ the connection

    async def _run(self, url: str, space: str, token: str) -> None:
        delay = RECONNECT_MIN_SECONDS
        while True:
            if self._binding_fenced:
                return
            await self._binding_resumed.wait()
            if self._binding_fenced or self._retained_evidence and self._binding_target is not None:
                return
            await self._set_status("connecting", self._message if self._state_name == "offline" else None)
            if self._binding_quiescing or self._binding_fenced:
                continue
            try:
                async with connect(
                    socket_url(url, space),
                    additional_headers={"Authorization": f"Bearer {token}"},
                    max_size=MAX_FRAME_BYTES,
                    open_timeout=10,
                ) as ws:
                    try:
                        await self._converse(ws, url)
                    finally:
                        await self._disconnected()
                    # A handshake that got as far as a conversation earns a fresh backoff.
                    delay = RECONNECT_MIN_SECONDS
                    self._raise_for_close(ws)
                    message = "The server closed the connection"
            except asyncio.CancelledError:
                raise
            except _Stop as stop:
                await self._set_status(stop.state, stop.message)
                return
            except (InvalidURI, ValueError) as exc:
                await self._set_status("error", f"Not a usable server address: {exc}")
                return
            except (OSError, InvalidHandshake, ConnectionClosed, TimeoutError) as exc:
                message = str(exc) or type(exc).__name__
            except Exception as exc:  # a bug here must not end sync for the session
                log.exception("Remote sync failed; reconnecting")
                message = str(exc) or type(exc).__name__
            await self._set_status("offline", message)
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    @staticmethod
    def _raise_for_close(ws: ClientConnection) -> None:
        if ws.close_code == CLOSE_UNAUTHORISED:
            raise _Stop("unauthorised", ws.close_reason or "The server refused the key")
        if ws.close_code == CLOSE_BAD_REQUEST:
            raise _Stop("error", ws.close_reason or "The server refused the request")

    async def _converse(self, ws: ClientConnection, url: str) -> None:
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_SECONDS))
        except ConnectionClosed:
            self._raise_for_close(ws)
            raise
        if self._binding_quiescing or self._binding_fenced:
            return
        if not isinstance(hello, dict) or hello.get("t") != "hello":
            raise _Stop("error", "The server did not introduce itself — is this a Voxarium Studio Remote?")
        if hello.get("protocol") != PROTOCOL_VERSION:
            raise _Stop(
                "error",
                f"The server speaks protocol {hello.get('protocol')}, this Studio speaks "
                f"{PROTOCOL_VERSION}. Update whichever is older.",
            )
        self._user = hello.get("user")
        self._state = SyncState.load(
            self.paths.root, url=url, space_id=str((hello.get("space") or {}).get("id") or "")
        )
        self._ws = ws
        await self._set_status("syncing")
        await self._send({"t": "sync", "since": self._state.cursor})

        pusher = asyncio.create_task(self._push_loop(), name="sync-push")
        try:
            async for raw in ws:
                if isinstance(raw, str):
                    await self._handle(json.loads(raw))
        except ConnectionClosed:
            pass
        finally:
            pusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pusher

    async def _disconnected(self) -> None:
        protected = self._binding_quiescing or self._binding_fenced or self._requires_editor_lease()
        # Capture BEFORE any cleanup, including legacy cleanup. A later cutover
        # cannot reinterpret a discarded message ID as positively reconciled.
        unresolved = bool(self._pending or self._live_writes)
        if unresolved or self._sessions and not self._binding_fenced:
            self._preserve_binding_evidence("disconnected")
        self._ws = None
        self._synced = False
        if not protected:
            self._pending.clear()
            self._pending_paths.clear()
            self._live_writes.clear()
        self._settled.set()
        self._peers = []
        if self._state is not None:
            self._state.save()
        for session in list(self._sessions.values()):
            if not self._binding_fenced:
                self._room_activity(session.path)
            if protected and not self._binding_fenced:
                # No server-triggered browser close/Y.Doc destruction on a
                # blocked drain. The editor owner still owes a final save.
                continue
            self._sessions.pop(session.sid, None)
            self._close_collab_registration(session)
            await self._close_browser(session, CLOSE_COLLAB_UNAVAILABLE, "offline")

    async def _send(self, message: dict[str, Any]) -> None:
        if message.get("t") in {"seed", "update"}:
            session = self._sessions.get(message.get("sid"))
            if session is not None:
                self.docs.transitions._require_generation(session.binding_generation)
                clean_join = session.clean_join
                lease = self.docs.transitions._editors.get(session.editor_id)
                if (lease is None or not lease.initial_materialization
                        or lease.revision != session.clean_lease_revision):
                    clean_join = None
                # Observe before waiting on the send lock: an unsent update is
                # already unknown room work, not evidence of a clean snapshot.
                self._room_activity(session.path)
                if message["t"] == "seed":
                    session.seed_pending = True
                if message["t"] == "seed" and clean_join is not None:
                    lease.initial_materialization = True
                    session.clean_lease_revision = lease.revision
                    session.clean_join = _RoomAnchor(
                        clean_join.epoch, self._room_serial[session.path], clean_join.rev, clean_join.hash)
        ws = self._ws
        if ws is None:
            raise ConnectionError("Not connected to the remote")
        async with self._send_lock:
            if ws is not self._ws:
                raise ConnectionError("Remote connection changed")
            if self._binding_fenced and message.get("t") != "leave":
                raise ConnectionError("Remote binding is fenced")
            if self._binding_quiescing and message.get("t") == "join":
                raise ConnectionError("Remote binding is quiescing")
            if message.get("t") in {"put", "delete"}:
                pending = self._pending.get(_integer(message.get("id")))
                if pending is None or not self._matches_pending(message, pending):
                    raise ConnectionError("Remote write binding changed")
            # The send lock is an await boundary. An old room callback must not
            # adopt the current G after waiting (runtime integration plan §3.3).
            if message.get("t") in _BROWSER_FRAMES | {"join"}:
                session = self._sessions.get(message.get("sid"))
                if session is None:
                    raise InvalidCollabSession()
                self._require_collab_session(session)
            await ws.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if self._binding_fenced and kind not in {"ack", "nack", "error"}:
            return
        if kind in _SESSION_FRAMES:
            await self._to_browser(message)
        elif kind == "change":
            async with self._lock:
                await self._on_change(message)
        elif kind == "ack":
            async with self._lock:
                self._on_ack(message)
        elif kind == "nack":
            async with self._lock:
                await self._on_nack(message)
        elif kind == "synced":
            if self._state is not None:
                self._state.cursor = max(self._state.cursor, _integer(message.get("seq")))
            self._synced = True
            # What is on disk and the server has never seen is pushed before anyone is told
            # the two are in step — a room may only be joined from an agreed revision.
            async with self._lock:
                await self._push_local()
            await self._set_status("connected")
        elif kind == "presence":
            peers = message.get("peers")
            self._peers = peers if isinstance(peers, list) else []
            await self._publish_status()
        elif kind == "error":
            self._on_error(message)

    # ------------------------------------------------------------------ documents: remote -> disk

    async def _on_change(self, message: dict[str, Any]) -> None:
        state = self._state
        path = message.get("path")
        if state is None or not isinstance(path, str):
            return
        # change has no sid/room epoch or causal save watermark. It cannot
        # attest a peer room snapshot, even when its bytes match the disk.
        self._room_activity(path)
        rev = _integer(message.get("rev"))
        state.cursor = max(state.cursor, rev)
        if path in self._pending_paths:
            # Our own write for this path is on its way. The server answers it after this
            # change — with an ack if it was taken anyway, or a nack carrying the newest
            # version — and that answer is what settles the path. Acting here as well would
            # settle it twice.
            return
        deleted = bool(message.get("deleted"))
        remote = _Remote(
            rev=rev,
            hash=None if deleted else message.get("hash"),
            content=None if deleted else message.get("content"),
        )
        await self._reconcile(path, remote, by=message.get("by"))
        state.save()

    async def _reconcile(
        self, path: str, remote: _Remote, *, by: dict[str, Any] | None = None, force: bool = False,
        pending_receipt: tuple[int, _Pending] | None = None,
    ) -> None:
        """Bring one path in step with a server version it has just been told about."""
        state = self._state
        assert state is not None
        base = state.docs.get(path)
        if not force and base is not None and base.rev >= remote.rev:
            return
        local, _text = self._read(path)
        if local == remote.hash:
            state.docs[path] = Base(remote.rev, remote.hash)
            return
        if local == (base.hash if base else None):
            # Untouched here since the last agreed revision: theirs is simply newer.
            await self._apply(path, remote, by, pending_receipt=pending_receipt)
            return

        # Both sides moved. Nothing below may lose either one.
        live = self._live_writes.get(path)
        if live is not None and live.hash == local:
            # Ours is the room's own Markdown, saved a moment ago and not pushed yet; theirs
            # is the room's Markdown from the scribe before us. Ours is the later one, and the
            # next comparison pushes it on top of the revision recorded here.
            state.docs[path] = Base(remote.rev, remote.hash)
            self._dirty.set()
            return
        if remote.hash is None:
            # Deleted there, edited here — the edit is the newer intention; it goes back up.
            state.docs[path] = Base(remote.rev, None)
            self._dirty.set()
            return
        if local is None:
            # Deleted here, edited there — same rule, the other way round.
            await self._apply(path, remote, by, pending_receipt=pending_receipt)
            return
        copy = await self._keep_copy(path, pending_receipt=pending_receipt)
        await self._apply(path, remote, by, pending_receipt=pending_receipt)
        log.warning("Sync conflict on %s; the local version was kept as %s", path, copy)
        await self.bus.publish(
            EventType.SYNC_CONFLICT,
            {"path": path, "copy": copy, "by": (by or {}).get("name")},
        )

    @contextlib.contextmanager
    def _pending_reconciliation_mutation(
        self, source_path: str, captured: tuple[int, _Pending] | None,
    ) -> Iterator[int | None]:
        """One FS mutation belonging to an exact old-target nack (§3.4/I5).

        Not used by generic change/apply callbacks. The common gate consumes
        the capability before publishing events, so callbacks remain fenced.
        """
        gate = self.docs.transitions
        if captured is None or not gate.binding_quiescing:
            yield None
            return
        mid, pending = captured

        def check() -> None:
            if (source_path != pending.path or self._pending.get(mid) is not pending or self._binding_fenced
                    or pending.generation is None or pending.target is None
                    or pending.generation != self._binding_generation
                    or pending.target != self._binding_target
                    or not self._matches_pending({}, pending)):
                raise MutationBusy()
            gate._require_generation(pending.generation)

        with gate.reconciliation_mutation_guard(check):
            yield pending.generation

    async def _apply(
        self, path: str, remote: _Remote, by: dict[str, Any] | None, *,
        pending_receipt: tuple[int, _Pending] | None = None,
    ) -> None:
        """Write a server version to disk, through the store every other writer uses."""
        state = self._state
        assert state is not None
        author = f"remote:{by['name']}" if by and by.get("name") else "remote"
        try:
            with self._pending_reconciliation_mutation(path, pending_receipt) as generation:
                if remote.hash is None:
                    if self.docs.resolve(path).is_file():
                        await self.docs.delete(path, author=author, expected_binding_generation=generation)
                else:
                    await self.docs.write(path, remote.content or "", author=author,
                                          expected_binding_generation=generation)
        except (ValueError, OSError, PathOutsideProject) as exc:
            # A name this filesystem cannot hold, a locked file. The revision is recorded all
            # the same — otherwise the stale file would look like an edit and be pushed back
            # over the change it failed to receive — and the path is left alone until the
            # file itself changes.
            log.warning("Could not apply remote change to %s: %s", path, exc)
            self._skip[path] = self._read(path)[0]
        state.docs[path] = Base(remote.rev, remote.hash)

    async def _keep_copy(
        self, path: str, *, pending_receipt: tuple[int, _Pending] | None = None,
    ) -> str:
        """Set the local version aside as a document of its own, and say where."""
        _digest, text = self._read(path)
        folder, _, name = path.rpartition("/")
        stem, dot, suffix = name.rpartition(".")
        who = _UNSAFE_NAME_CHARS.sub("", str((self._user or {}).get("name") or "me")).strip() or "me"
        stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        for attempt in range(1, 50):
            tail = "" if attempt == 1 else f" {attempt}"
            copy_name = f"{stem} (conflict {who} {stamp}{tail}){dot}{suffix}"
            copy = f"{folder}/{copy_name}" if folder else copy_name
            try:
                with self._pending_reconciliation_mutation(path, pending_receipt) as generation:
                    await self.docs.write(copy, text or "", exclusive=True,
                                          expected_binding_generation=generation)
                return copy
            except FileExistsError:
                continue
        raise OSError(f"Could not find a free name for a conflict copy of {path}")

    # ------------------------------------------------------------------ documents: disk -> remote

    async def _watch_bus(self) -> None:
        subscription = self._subscription
        assert subscription is not None
        docs_prefix = self._docs_prefix()
        async for event in subscription.stream():
            if event.type in (EventType.TEAM_DOC_CHANGED, EventType.TEAM_DOC_FOLDER_CHANGED):
                self._dirty.set()
            elif event.type == EventType.EXPLORER_FILE_CHANGED and docs_prefix is not None:
                # Changes made behind the Studio's back — the user's own editor, a checkout.
                changes = event.data.get("changes") or []
                if event.data.get("truncated") or any(
                    str(change.get("path", "")).startswith(docs_prefix) for change in changes
                ):
                    self._dirty.set()

    def _docs_prefix(self) -> str | None:
        try:
            return self.paths.docs.resolve().relative_to(self.paths.root).as_posix() + "/"
        except (ValueError, OSError):
            return None

    async def _push_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._dirty.wait(), RESCAN_SECONDS)
            await asyncio.sleep(PUSH_DEBOUNCE_SECONDS)
            self._dirty.clear()
            async with self._lock:
                await self._push_local()

    async def _push_local(self) -> None:
        """Compare the tree with what was last agreed and push every difference."""
        state = self._state
        if state is None or not self._synced or self._ws is None or self._binding_fenced:
            return
        local = self._binding_corpus()[0] if self._binding_quiescing else self._scan()
        known = {path for path, base in state.docs.items() if base.hash is not None}
        for path in sorted(local.keys() | known):
            if path in self._pending_paths:
                continue
            digest = local.get(path)
            base = state.docs.get(path)
            if digest == (base.hash if base else None):
                self._skip.pop(path, None)
                continue
            if path in self._skip and self._skip[path] == digest:
                continue
            self._skip.pop(path, None)
            self._next_id += 1
            frame: dict[str, Any] = {
                "id": self._next_id, "path": path, "base_rev": base.rev if base else 0,
            }
            if digest is None:
                frame["t"] = "delete"
            else:
                # Read again rather than trusted from the scan: what is recorded as sent has
                # to be the hash of the very text that went.
                digest, text = self._read(path)
                if digest is None or text is None:
                    continue
                if len(text.encode("utf-8")) > MAX_DOC_BYTES:
                    log.warning("%s is too large to sync; leaving it local", path)
                    self._skip[path] = digest
                    continue
                live = self._live_writes.get(path)
                frame.update(t="put", content=text)
                if live is not None and live.hash == digest and live.sid in self._sessions:
                    frame["live"] = live.sid
            self._pending[self._next_id] = _Pending(
                path, digest, frame["base_rev"], self.docs.transitions.generation,
                self._target_tuple(), frame.get("live"), base.hash if base else None,
                live.room_save if digest is not None and frame.get("live") is not None else None,
            )
            self._pending_paths.add(path)
            await self._send(frame)

    def _on_ack(self, message: dict[str, Any]) -> None:
        state = self._state
        mid = _integer(message.get("id"))
        pending = self._pending.get(mid)
        if state is None or pending is None:
            return
        if (not self._matches_pending(message, pending)
                or type(message.get("rev")) is not int
                or message["rev"] < pending.base_rev
                or message["rev"] == 0 and pending.hash is not None):
            self._binding_failure = "invalid_pending_ack"
            self._settled.set()
            return
        rev = _integer(message.get("rev"))
        state.docs[pending.path] = Base(rev, pending.hash)
        state.cursor = max(state.cursor, rev)
        state.save()
        self._pending.pop(mid)
        self._pending_paths.discard(pending.path)
        anchor = pending.room_save
        session = self._sessions.get(pending.sid)
        if (anchor is not None and session is not None and session.room_ready
                and session.room_epoch == anchor.epoch
                and self._room_serial.get(pending.path, 0) == anchor.serial
                and pending.hash == anchor.hash):
            self._room_anchors[pending.path] = _RoomAnchor(
                anchor.epoch, anchor.serial, rev, anchor.hash, anchor.writer)
        live = self._live_writes.get(pending.path)
        if (live is not None and live.hash == pending.hash
                and (pending.sid is None or live.sid == pending.sid)):
            del self._live_writes[pending.path]
        self._settled.set()
        # The file may have changed again while this was in flight; the next comparison finds
        # that out against the revision just recorded.
        self._dirty.set()

    def _matches_pending(self, message: dict[str, Any], pending: _Pending) -> bool:
        """Message IDs belong to one socket/target/G, not just a document path."""
        return ((pending.target is None or pending.target == self._target_tuple())
                and (pending.generation is None or pending.generation == self.docs.transitions.generation)
                and ("path" not in message or message["path"] == pending.path)
                and ("hash" not in message or message["hash"] == pending.hash))

    async def _on_nack(self, message: dict[str, Any]) -> None:
        """The server moved first. Its version arrives with the refusal; settle the two."""
        state = self._state
        mid = _integer(message.get("id"))
        pending = self._pending.get(mid)
        if state is None or pending is None:
            return
        if not self._matches_pending(message, pending):
            self._binding_failure = "invalid_pending_nack"
            self._settled.set()
            return
        current = message.get("current")
        if (current is not None and (not isinstance(current, dict)
                or type(current.get("rev")) is not int or current["rev"] <= 0
                or type(current.get("deleted")) is not bool
                or not current["deleted"] and (
                    not isinstance(current.get("content"), str)
                    or content_hash(current["content"]) != current.get("hash")))):
            self._binding_failure = "invalid_pending_nack"
            self._settled.set()
            return
        if isinstance(current, dict):
            deleted = bool(current.get("deleted"))
            remote = _Remote(
                rev=_integer(current.get("rev")),
                hash=None if deleted else current.get("hash"),
                content=None if deleted else current.get("content"),
            )
        else:
            remote = _Remote(rev=0, hash=None, content=None)
        try:
            await self._reconcile(pending.path, remote, force=True, pending_receipt=(mid, pending))
        except MutationGateError:
            if not self._binding_quiescing:
                raise
            # A stale/ambiguous pending ID cannot obtain the single-mutation
            # reconciliation capability. Preserve its ID/live/text and block;
            # never downgrade this into generic remote-apply admission.
            self._binding_failure = "reconciliation_failed"
            self._preserve_binding_evidence("reconciliation_failed")
            self._settled.set()
            return
        state.cursor = max(state.cursor, remote.rev)
        state.save()
        if pending.path in self._skip:
            self._binding_failure = "reconciliation_failed"
            self._preserve_binding_evidence("reconciliation_failed")
            self._settled.set()
            return
        # Do not erase an ID while reconciliation/DocStore/save can still fail.
        # Conflict copies and a changed scribe hash are pushed and ACKed by the
        # next comparison before the drain can become ready.
        self._pending.pop(mid)
        self._pending_paths.discard(pending.path)
        live = self._live_writes.get(pending.path)
        if (live is not None and live.hash == pending.hash == remote.hash
                and self._read(pending.path)[0] == remote.hash
                and (pending.sid is None or pending.sid == live.sid)):
            del self._live_writes[pending.path]
        self._settled.set()
        self._dirty.set()

    def _on_error(self, message: dict[str, Any]) -> None:
        log.warning("Remote: %s", message.get("message"))
        pending = self._pending.get(_integer(message.get("id")))
        if pending is not None:
            self._preserve_binding_evidence("remote_refused")
            if self._binding_quiescing or self._requires_editor_lease():
                self._binding_failure = "remote_refused"
                self._settled.set()
                return
        pending = self._pending.pop(_integer(message.get("id")), None)
        if pending is None:
            return
        # Refused outright — a path or a size the server will not hold. Sending the same text
        # again would be refused again.
        self._pending_paths.discard(pending.path)
        self._skip[pending.path] = pending.hash
        self._settled.set()

    def _read(self, path: str) -> tuple[str | None, str | None]:
        """A document's hash and text as they are on disk now — ``(None, None)`` if absent.

        Decoded with replacement, like every other reader of a document: the hash is of the
        text that would be sent, so a file with a stray byte still compares equal to itself.
        """
        try:
            data = self.docs.resolve(path).read_bytes()
        except (OSError, ValueError, PathOutsideProject):
            return None, None
        text = data.decode("utf-8", errors="replace")
        return content_hash(text), text

    def _scan(self) -> dict[str, str]:
        """Every document's hash, re-reading only the files whose size or mtime moved."""
        seen: dict[str, tuple[int, int, str]] = {}
        for relative, _path, mtime_ns, size in self.docs.files():
            cached = self._hashes.get(relative)
            if cached is not None and cached[0] == mtime_ns and cached[1] == size:
                seen[relative] = cached
                continue
            digest, _text = self._read(relative)
            if digest is not None:
                seen[relative] = (mtime_ns, size, digest)
        self._hashes = seen
        return {relative: entry[2] for relative, entry in seen.items()}

    async def _flush(self, path: str) -> None:
        """Push what is pending and wait until *path* has nothing left in flight.

        Never called from the task that reads the socket: the answers it waits for are read
        there, and it would be waiting for itself.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FLUSH_TIMEOUT_SECONDS
        while True:
            if self._binding_fenced:
                raise ConnectionError("Remote binding is fenced")
            # Compared again after every answer: a refusal is settled by writing the server's
            # version, and what that leaves to push — a conflict copy — is pushed here.
            async with self._lock:
                await self._push_local()
            if (path not in self._pending_paths
                    and not (self._binding_quiescing or self._requires_editor_lease())):
                return
            if self._binding_quiescing or self._requires_editor_lease():
                if self._binding_failure or self._retained_evidence:
                    raise ConnectionError("Unresolved remote write")
                state = self._state
                base = state.docs.get(path) if state is not None else None
                if (path not in self._pending_paths and path not in self._live_writes
                        and self._read(path)[0] == (base.hash if base else None)):
                    return
            remaining = deadline - loop.time()
            if remaining <= 0 or self._ws is None:
                raise TimeoutError(f"The remote did not answer for {path}")
            self._settled.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._settled.wait(), remaining)

    # ------------------------------------------------------------------ rooms (§16.4)

    def _requires_editor_lease(self) -> bool:
        return self.docs.transitions.activated or getattr(self, "_activated_model_seen", False)

    def _require_collab_session(self, session: _Session) -> None:
        self.docs.transitions.require_http(
            session.binding_generation, backend_epoch=session.backend_epoch,
            editor_id=session.editor_id, document_path=session.path,
            client_instance_id=session.client_instance_id, sid=session.sid,
            require_editor=self._requires_editor_lease(),
        )

    def _close_collab_registration(self, session: _Session) -> None:
        if not self._binding_fenced:
            self._room_activity(session.path)
        self.docs.transitions.close_collab_session(session.sid)
        session.registered = False
        session.closed = True

    def bind_collab_editor(self, sid: str, editor_id: str) -> None:
        """Attach an explicitly registered backend lease to an existing legacy sid.

        The editor-registration HTTP owner calls this only AFTER register_editor
        succeeded. No client frame/capability creates this association or a save
        receipt. The normal activated flow registers its editor before joining.
        """
        session = self._sessions.get(sid)
        if session is None or (session.editor_id is not None and session.editor_id != editor_id):
            raise InvalidCollabSession()
        self.docs.transitions.require_http(
            session.binding_generation, backend_epoch=session.backend_epoch,
            editor_id=editor_id, document_path=session.path, sid=sid,
        )
        session.editor_id = editor_id

    def validate_live_write(
        self, path: str, sid: str, *, expected_binding_generation: int | None = None,
        editor_id: str | None = None,
    ) -> None:
        """Validate without recording or writing; the HTTP owner repeats before save.

        Session ownership is this RemoteSync/Ctx, not a process-global sid lookup.
        No unknown sid, wrong path/editor or captured old G becomes a plain write
        (studio-design §5.3). A legacy caller may omit editor_id, never sid validity.
        """
        session = self._sessions.get(sid)
        if session is None or self._binding_fenced:
            raise InvalidCollabSession()
        self.docs.transitions._require_generation(session.binding_generation)
        if (expected_binding_generation is not None
                and (type(expected_binding_generation) is not int
                     or expected_binding_generation != session.binding_generation)):
            raise StaleBindingGeneration(expected_binding_generation, session.binding_generation)
        try:
            relative = self.docs.relative(self.docs.resolve(path))
        except (ValueError, PathOutsideProject):
            raise InvalidCollabSession() from None
        if relative != session.path or (session.editor_id is not None and editor_id != session.editor_id):
            raise InvalidCollabSession()
        self.docs.transitions.require_http(
            expected_binding_generation, backend_epoch=session.backend_epoch,
            editor_id=editor_id, document_path=relative, sid=sid,
            require_editor=self._requires_editor_lease(),
        )
        if editor_id is not None:
            # A lease may be explicitly registered against an existing legacy
            # sid by the backend. Adopt that proven association, never a new G.
            session.editor_id = editor_id

    def note_live_write(
        self, path: str, sid: str, content: str, *,
        expected_binding_generation: int | None = None, editor_id: str | None = None,
    ) -> None:
        """A room's scribe is about to save *content* to *path* through the docs API.

        Recorded by hash, so the push that follows is recognised as the room's own Markdown
        and nothing else is: an agent's write to the same path a moment later has another
        hash, is pushed as an ordinary write, and ends the room as it should.
        """
        self.validate_live_write(path, sid, expected_binding_generation=expected_binding_generation,
                                 editor_id=editor_id)
        session = self._sessions[sid]
        anchor = None
        lease = self.docs.transitions._editors.get(editor_id)
        if (session.room_epoch is not None and session.room_ready and lease is not None
                and len(lease.saves) == 1 and not lease.overlapping):
            save = next(iter(lease.saves.values()))
            anchor = _RoomAnchor(session.room_epoch, self._room_serial.get(session.path, 0),
                                 0, content_hash(content),
                                 (editor_id, save.version, save.revision, save.sequence))
        self._live_writes[session.path] = _LiveWrite(content_hash(content), sid, anchor)

    async def serve_collab(self, websocket: WebSocket) -> None:
        """One browser's editing session, from its first frame until either end hangs up.

        *websocket* is already accepted and authorised (``ws/routes.py``).
        """
        session: _Session | None = None
        # Capture before the first receive; rejoin keeps this same G as well.
        generation = self.docs.transitions.generation
        backend_epoch = self.docs.transitions.backend_epoch
        try:
            first = await asyncio.wait_for(websocket.receive_json(), HELLO_TIMEOUT_SECONDS)
            expected = first.get("expected_binding_generation")
            self.docs.transitions._require_generation(generation)
            path = self.docs.relative(self.docs.resolve(str(first["path"])))
            # Before even flush/join traffic, validate the browser's captured
            # epoch/G/editor against this Ctx. A JSON capability/saved boolean
            # is neither a lease nor backend save evidence (studio-design §5.3).
            self.docs.transitions.require_http(
                expected, backend_epoch=first.get("backend_epoch"),
                editor_id=first.get("editor_id"), document_path=path,
                client_instance_id=first.get("client_instance_id"),
                require_editor=self._requires_editor_lease(),
            )
            session = _Session(
                sid=uuid.uuid4().hex, path=path, websocket=websocket,
                client_id=int(first["client_id"]),
                binding_generation=generation, editor_id=first.get("editor_id"),
                backend_epoch=backend_epoch, client_instance_id=first.get("client_instance_id"),
            )
        except MutationGateError as exc:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=CLOSE_BAD_REQUEST, reason=exc.code)
            return
        except (WebSocketDisconnect, RuntimeError):
            return
        except (TimeoutError, ValueError, KeyError, TypeError, AttributeError, PathOutsideProject):
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=CLOSE_BAD_REQUEST, reason="A join frame is required")
            return

        try:
            if not await self._join(session):
                await self._close_browser(session, CLOSE_COLLAB_UNAVAILABLE, "unavailable")
                return
            while True:
                message = await websocket.receive_json()
                kind = message.get("t") if isinstance(message, dict) else None
                if kind in _BROWSER_FRAMES and session.sid in self._sessions:
                    await self._send({"t": kind, "sid": session.sid, "update": message.get("update")})
        except (WebSocketDisconnect, RuntimeError, ConnectionError, ConnectionClosed):
            pass
        except ValueError:
            log.debug("Malformed collaboration frame; closing the session", exc_info=True)
        finally:
            if (self._binding_quiescing and not self._binding_fenced
                    and session.sid in self._sessions):
                self._preserve_binding_evidence("browser_unresolved")
                self._settled.set()
            else:
                try:
                    if session.sid in self._sessions:
                        # Closing a socket never marks the editor lease clean.
                        with contextlib.suppress(TimeoutError, ConnectionError, ConnectionClosed,
                                                OSError, MutationGateError):
                            self._require_collab_session(session)
                            await self._flush(session.path)
                    if self._sessions.pop(session.sid, None) is not None:
                        with contextlib.suppress(ConnectionError, ConnectionClosed, OSError):
                            await self._send({"t": "leave", "sid": session.sid})
                finally:
                    self._close_collab_registration(session)
                    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                        await websocket.close()

    async def _join(self, session: _Session) -> bool:
        """Ask the remote for the room, from a revision both sides agree on."""
        if session.closed or self._binding_quiescing or self._binding_fenced:
            # A stale retry task may start only after the browser handler's
            # finally ran. Disconnect cannot resurrect even a legacy room.
            return False
        try:
            if not session.registered:
                self.docs.transitions.require_http(
                    session.binding_generation, backend_epoch=session.backend_epoch,
                    editor_id=session.editor_id, document_path=session.path,
                    client_instance_id=session.client_instance_id,
                    require_editor=self._requires_editor_lease(),
                )
                self.docs.transitions.register_collab_session(
                    session.sid, session.path, session.binding_generation, editor_id=session.editor_id,
                )
                session.registered = True
            self._require_collab_session(session)
        except MutationGateError:
            return False
        if not self.live:
            return False
        try:
            # Anything saved here and not yet pushed goes first: the room is seeded from the
            # server's revision, and the file has to *be* that revision.
            await self._flush(session.path)
        except (TimeoutError, ConnectionError, ConnectionClosed):
            return False
        async with self._lock:
            if self._binding_quiescing or self._binding_fenced:
                return False
            try:
                self._require_collab_session(session)
            except MutationGateError:
                return False
            if self.docs.transitions.snapshot_pending:
                return False
            state = self._state
            base = state.docs.get(session.path) if state else None
            digest, text = self._read(session.path)
            if base is None or base.hash is None or digest != base.hash or text is None:
                return False
            session.content = text
            session.attempts += 1
            self._sessions[session.sid] = session
            try:
                await self._send(
                    {
                        "t": "join", "sid": session.sid, "path": session.path,
                        "client_id": session.client_id, "base_rev": base.rev,
                    }
                )
            except (ConnectionError, ConnectionClosed):
                self._sessions.pop(session.sid, None)
                return False
        return True

    async def _to_browser(self, message: dict[str, Any]) -> None:
        session = self._sessions.get(str(message.get("sid")))
        if session is None:
            return
        kind = message["t"]
        if kind in {"joined", "update", "reset", "join_failed"}:
            self._room_activity(session.path, invalidate_leases=kind != "joined")
        if kind in {"reset", "join_failed"}:
            session.room_ready = False
            session.room_epoch = None
        if self._binding_quiescing and not self._binding_fenced and kind in {"join_failed", "reset"}:
            # Never destroy a room/browser's screen text to escape a blocked
            # barrier. Its final save/room provenance must be reconciled first.
            self._binding_failure = "room_unresolved"
            self._preserve_binding_evidence("room_unresolved")
            self._settled.set()
            return
        frame = {key: value for key, value in message.items() if key != "sid"}
        if kind == "join_failed":
            self._sessions.pop(session.sid, None)
            # "stale" means a change for this path was still on its way here when we asked.
            # It has been applied by now — it came down the same socket, ahead of this — so
            # asking again from the new revision is all it takes. In a task of its own:
            # joining waits for the server's answers, and this is the task that reads them.
            if message.get("reason") == "stale" and session.attempts < JOIN_ATTEMPTS:
                self._spawn(self._rejoin(session))
            else:
                self._close_collab_registration(session)
                await self._close_browser(session, CLOSE_COLLAB_UNAVAILABLE, "unavailable")
            return
        if kind == "reset":
            self._sessions.pop(session.sid, None)
            self._close_collab_registration(session)
            self._live_writes.pop(session.path, None)
            await self._close_browser(session, CLOSE_COLLAB_RESET, str(message.get("reason") or "reset"))
            return
        try:
            self._require_collab_session(session)
        except MutationGateError:
            if self._binding_quiescing and not self._binding_fenced:
                # A post-ACK update, expiry or stale room callback must block
                # before leave, not destroy the browser's unconfirmed text.
                self._binding_failure = "room_unresolved"
                self._preserve_binding_evidence("room_unresolved")
                self._settled.set()
                return
            self._sessions.pop(session.sid, None)
            self._close_collab_registration(session)
            await self._close_browser(session, CLOSE_COLLAB_UNAVAILABLE, "stale_binding_generation")
            return
        if kind == "joined":
            # Observe existing Remote fields, never infer an epoch or clean
            # room from missing/false-ish values. No wire change is involved.
            base = self._state.docs.get(session.path) if self._state is not None else None
            epoch = message.get("epoch")
            session.room_epoch = epoch if isinstance(epoch, str) and epoch else None
            session.room_ready = message.get("seeded") is True
            lease = self.docs.transitions._editors.get(session.editor_id)
            session.clean_lease_revision = lease.revision if lease is not None else None
            if (session.room_epoch is not None and base is not None and base.hash is not None
                    and type(message.get("rev")) is int and message["rev"] == base.rev
                    and message.get("dirty") is False):
                session.clean_join = _RoomAnchor(
                    session.room_epoch, self._room_serial.get(session.path, 0), base.rev, base.hash)
                if session.room_ready:
                    self._room_anchors[session.path] = session.clean_join
            frame["sid"] = session.sid
            frame["user"] = self._user
            # Only an empty room needs the text, and then it must be this revision's.
            frame["content"] = None if message.get("seeded") else session.content
        elif kind == "seeded":
            pending, session.seed_pending = session.seed_pending, False
            session.room_ready = pending and message.get("ok") is True
            anchor = session.clean_join
            if (session.room_ready and anchor is not None
                    and self._room_serial.get(session.path, 0) == anchor.serial):
                self._room_anchors[session.path] = anchor
                session.clean_join = None
            elif not session.room_ready:
                self._room_activity(session.path)
        try:
            await session.websocket.send_json(frame)
        except (RuntimeError, WebSocketDisconnect, OSError):
            # The browser went away; its own handler reports the leave.
            pass

    async def _rejoin(self, session: _Session) -> None:
        if self._binding_quiescing or self._binding_fenced:
            return
        if not await self._join(session):
            self._close_collab_registration(session)
            await self._close_browser(session, CLOSE_COLLAB_UNAVAILABLE, "unavailable")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro, name="sync-rejoin")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _close_browser(session: _Session, code: int, reason: str) -> None:
        with contextlib.suppress(RuntimeError, WebSocketDisconnect, OSError):
            await session.websocket.close(code=code, reason=reason)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value
