"""AI Control Protocol 1 (AICP/1) — the Studio's restatement of the future wire (§16.12).

Source of the contract: ``.voxarium_studio/docs/remote-identity/ai-access-design.md``
§3–§8. Remote will restate the same names in its own package; nothing is shared by
import, exactly like Documents sync (``sync/protocol.py``). The Documents
``PROTOCOL_VERSION`` is unrelated and is never changed by this module.

Everything here is pure: strict schemas, canonical hashing, limits and a classifier for
the capabilities discovery response. No socket, HTTP client or route lives here.

Conventions shared by every schema:

* unknown fields are rejected (``extra="forbid"``), types are strict — a JSON ``true``
  is never an integer and a float is never accepted anywhere on the wire;
* identifiers are canonical lowercase hyphenated UUID strings, checked *before* any
  lookup; local Studio ids (tasks, chats, teams, members) are opaque ASCII tokens;
* timestamps are signed int64 Unix UTC milliseconds;
* revisions are integers ``>= 1``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from ..sync.protocol import check_project_id

# --------------------------------------------------------------------------- versions

AICP_VERSION = 1
"""Wire field ``acp`` of every frame. Never the Agent Client Protocol (§10.6)."""
SUPPORTED_VERSIONS = (AICP_VERSION,)
NAMESPACE = "/api/ai/v1"
CAPABILITIES_PATH = f"{NAMESPACE}/capabilities"
REGISTRATIONS_PATH = f"{NAMESPACE}/registrations"
CONTROL_PATH = f"{NAMESPACE}/control"

# --------------------------------------------------------------------------- limits (§8)

MAX_FRAME_BYTES = 64 * 1024
MAX_DEPTH = 12
MAX_OBJECT_FIELDS = 128
MAX_TEXT_BYTES = 16 * 1024
MAX_TITLE_BYTES = 480
MAX_WRITE_PATHS = 64
MAX_WRITE_PATH_BYTES = 256
MAX_PAGE_ITEMS = 50
MAX_PAGE_PAYLOAD_BYTES = 32 * 1024
MAX_CHUNK_BYTES = 16 * 1024
MAX_VISIBLE_RECORD_BYTES = 1024 * 1024
MAX_IN_FLIGHT_PER_SOCKET = 16
MAX_SUBSCRIPTIONS_PER_SOURCE = 8
MAX_SUBSCRIPTIONS_PER_EXPORT = 32
REPLAY_FRAMES = 500
REPLAY_SECONDS = 60
HEARTBEAT_SECONDS = 5
LEASE_SECONDS = 15
RECEIPT_TIMEOUT_SECONDS = 10
DISPATCH_PREPARE_SECONDS = 10
MAX_REGISTRATIONS_PER_USER = 32
MAX_EXPORTS_PER_EXECUTOR = 16
MAX_GRANTS_PER_EXPORT = 128
MAX_INDEX_ROWS_PER_EXPORT = 1024
MAX_INDEX_ROW_BYTES = 1024
MAX_ATTACH_MANIFEST = 16
MUTATION_MAX_AGE_MS = 60_000
MAX_CLOCK_SKEW_MS = 30_000
GRANT_DEFAULT_MS = 24 * 3600 * 1000
GRANT_MAX_MS = 30 * 24 * 3600 * 1000
SAFE_RESULT_RETENTION_MS = 30 * 24 * 3600 * 1000
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class ProtocolError(ValueError):
    """A fixed, input-free code. Never carries the offending value (no secret echo)."""

    def __init__(self, code: str = "protocol_error") -> None:
        self.code = code
        super().__init__(code)


# --------------------------------------------------------------------------- enums


class FrameType(StrEnum):
    HELLO = "hello"
    EXECUTOR_ATTACH = "executor.attach"
    DIRECTORY_LIST = "directory.list"
    REQUEST = "request"
    FORWARD = "forward"
    RESPONSE = "response"
    EVENT = "event"
    ACK = "ack"
    EXPORT_PUBLISH = "export.publish"
    EXPORT_UNPUBLISH = "export.unpublish"
    GRANT_PUT = "grant.put"
    GRANT_REVOKE = "grant.revoke"
    MIRROR_GET = "mirror.get"
    HEARTBEAT_PING = "heartbeat.ping"
    HEARTBEAT_PONG = "heartbeat.pong"
    RESOURCE_INDEX_BEGIN = "resource.index.begin"
    RESOURCE_INDEX_PAGE = "resource.index.page"
    RESOURCE_INDEX_COMMIT = "resource.index.commit"


class Operation(StrEnum):
    CATALOG_LIST = "catalog.list"
    RESOURCE_LIST = "resource.list"
    RESOURCE_GET = "resource.get"
    TRANSCRIPT_PAGE = "transcript.page"
    STREAM_SUBSCRIBE = "stream.subscribe"
    STREAM_UNSUBSCRIBE = "stream.unsubscribe"
    TASK_CREATE = "task.create"
    TASK_EDIT_DRAFT = "task.edit_draft"
    TASK_START = "task.start"
    TASK_NOTE = "task.note"
    TASK_CANCEL = "task.cancel"
    CHAT_CREATE = "chat.create"
    CHAT_SEND = "chat.send"
    CHAT_CANCEL = "chat.cancel"
    COMMAND_GET = "command.get"


class Permission(StrEnum):
    VIEW_STATUS = "view_status"
    VIEW_TRANSCRIPT = "view_transcript"
    CREATE_TASK = "create_task"
    EDIT_TASK = "edit_task"
    RUN_TASK = "run_task"
    WRITE_TASK = "write_task"
    CANCEL_TASK = "cancel_task"
    CREATE_CHAT = "create_chat"
    RUN_CHAT = "run_chat"
    CANCEL_CHAT = "cancel_chat"
    OPERATE_EXISTING = "operate_existing"


MUTATIONS: frozenset[Operation] = frozenset({
    Operation.TASK_CREATE, Operation.TASK_EDIT_DRAFT, Operation.TASK_START,
    Operation.TASK_NOTE, Operation.TASK_CANCEL, Operation.CHAT_CREATE,
    Operation.CHAT_SEND, Operation.CHAT_CANCEL,
})
"""Operations that need command_id + issued_at and go through the ledger (§7)."""

DISPATCHING: frozenset[Operation] = frozenset({Operation.TASK_START, Operation.CHAT_SEND})
"""The two mutations that reserve a run token and may reach an AI dispatch (§7.3)."""

CREATING: frozenset[Operation] = frozenset({Operation.TASK_CREATE, Operation.CHAT_CREATE})

OPERATION_PERMISSION: dict[Operation, Permission | None] = {
    Operation.CATALOG_LIST: Permission.VIEW_STATUS,
    Operation.RESOURCE_LIST: Permission.VIEW_STATUS,
    Operation.RESOURCE_GET: Permission.VIEW_STATUS,
    Operation.TRANSCRIPT_PAGE: Permission.VIEW_TRANSCRIPT,
    Operation.STREAM_SUBSCRIBE: Permission.VIEW_STATUS,  # topic=transcript adds view_transcript
    Operation.STREAM_UNSUBSCRIBE: None,  # own subscription only
    Operation.TASK_CREATE: Permission.CREATE_TASK,
    Operation.TASK_EDIT_DRAFT: Permission.EDIT_TASK,
    Operation.TASK_START: Permission.RUN_TASK,
    Operation.TASK_NOTE: Permission.WRITE_TASK,
    Operation.TASK_CANCEL: Permission.CANCEL_TASK,
    Operation.CHAT_CREATE: Permission.CREATE_CHAT,
    Operation.CHAT_SEND: Permission.RUN_CHAT,
    Operation.CHAT_CANCEL: Permission.CANCEL_CHAT,
    Operation.COMMAND_GET: None,  # own command_id under a still-valid scope
}

MUTATION_PERMISSIONS: frozenset[Permission] = frozenset({
    Permission.CREATE_TASK, Permission.EDIT_TASK, Permission.RUN_TASK, Permission.WRITE_TASK,
    Permission.CANCEL_TASK, Permission.CREATE_CHAT, Permission.RUN_CHAT, Permission.CANCEL_CHAT,
})


def required_permissions(
    op: Operation, *, existing_foreign: bool = False, topic: str | None = None,
) -> frozenset[Permission]:
    """Every permission an operation needs; derived rights are never implied (§5).

    ``existing_foreign`` — the object was not created by this grantee, so a mutation also
    needs ``operate_existing``. Every mutation also needs status visibility.
    """
    needed: set[Permission] = set()
    base = OPERATION_PERMISSION[op]
    if base is not None:
        needed.add(base)
    if op is Operation.TRANSCRIPT_PAGE or (
        op is Operation.STREAM_SUBSCRIBE and topic == "transcript"
    ):
        needed |= {Permission.VIEW_STATUS, Permission.VIEW_TRANSCRIPT}
    if op in MUTATIONS:
        needed.add(Permission.VIEW_STATUS)
        if existing_foreign and op not in CREATING:
            needed.add(Permission.OPERATE_EXISTING)
    return frozenset(needed)


class ResourceScope(StrEnum):
    CREATED_BY_THIS_GRANTEE = "created_by_this_grantee"
    SELECTED_RESOURCES = "selected_resources"
    ALL_RESOURCES_IN_SELECTED_TEAMS = "all_resources_in_selected_teams"


class ResourceKind(StrEnum):
    TASK = "task"
    CHAT = "chat"


class CommandState(StrEnum):
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DENIED_BEFORE_DISPATCH = "denied_before_dispatch"
    OUTCOME_UNKNOWN = "outcome_unknown"


TERMINAL_COMMAND_STATES: frozenset[CommandState] = frozenset({
    CommandState.REJECTED, CommandState.COMPLETED, CommandState.FAILED,
    CommandState.CANCELLED, CommandState.DENIED_BEFORE_DISPATCH,
    CommandState.OUTCOME_UNKNOWN,
})


class ErrorCode(StrEnum):
    NOT_AVAILABLE = "not_available"
    DENIED = "denied"
    STALE_TARGET = "stale_target"
    REVISION_CONFLICT = "revision_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    BUSY = "busy"
    EXPIRED = "expired"
    RATE_LIMITED = "rate_limited"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RESYNC_REQUIRED = "resync_required"
    SNAPSHOT_CHANGED = "snapshot_changed"
    UNSUPPORTED = "unsupported"
    CAPACITY_EXCEEDED = "capacity_exceeded"


class CloseCode:
    PROTOCOL_ERROR = 4400
    UNAUTHORISED = 4401
    PERMISSION_REVOKED = 4403
    EXECUTOR_CONFLICT = 4409
    OVERLOADED = 4429


class Availability(StrEnum):
    """Export availability — separate from task/chat execution status (§8)."""

    DISABLED = "disabled"
    UNPUBLISHED = "unpublished"
    ONLINE = "online"
    OFFLINE = "offline"
    REVOKED = "revoked"
    UNSUPPORTED = "unsupported"


class MirrorState(StrEnum):
    PENDING = "pending"
    EFFECTIVE = "effective"
    REVOKED = "revoked"
    UNPUBLISHED = "unpublished"
    CONFLICT = "conflict"


class ClientKind(StrEnum):
    STUDIO = "studio"
    ANDROID = "android"


# --------------------------------------------------------------------------- scalars


def check_uuid(value: object) -> str:
    """Canonical lowercase hyphenated UUID, validated before any lookup."""
    if not isinstance(value, str) or len(value) != 36:
        raise ProtocolError("invalid_id")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ProtocolError("invalid_id") from None
    return value


_LOCAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)


def check_local_id(value: object) -> str:
    """Opaque Studio id (task/chat/team/member). Never a display id like ``VS-130``.

    Display ids are syntactically local ids too; the executor resolves only real row ids.
    """
    if not isinstance(value, str) or _LOCAL_ID.fullmatch(value) is None:
        raise ProtocolError("invalid_id")
    return value


def check_storage_epoch(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ProtocolError("invalid_storage_epoch")
    return value


def check_sha256(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ProtocolError("invalid_hash")
    return value


def check_timestamp(value: object) -> int:
    if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
        raise ProtocolError("invalid_timestamp")
    return value


def check_revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= INT64_MAX:
        raise ProtocolError("invalid_revision")
    return value


def _forbidden_char(c: str) -> bool:
    code = ord(c)
    return (code < 32 and c not in "\n\t") or 127 <= code <= 159 or code in {
        0x2028, 0x2029, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2066, 0x2067, 0x2068, 0x2069,
    }


def check_title(value: object) -> str:
    """One line, 1..480 UTF-8 bytes after NFC + strip; no bidi/control characters."""
    if not isinstance(value, str) or any(_forbidden_char(c) or c in "\n\t" for c in value):
        raise ProtocolError("invalid_title")
    title = unicodedata.normalize("NFC", value).strip()
    if not title or len(title.encode("utf-8")) > MAX_TITLE_BYTES:
        raise ProtocolError("invalid_title")
    return title


def check_text(value: object, *, allow_empty: bool = False) -> str:
    """Prompt/note/brief body: ≤16 KiB UTF-8, verbatim (no normalisation, §7 hash)."""
    if not isinstance(value, str):
        raise ProtocolError("invalid_text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:  # lone surrogates
        raise ProtocolError("invalid_text") from None
    if size > MAX_TEXT_BYTES or (not allow_empty and not value.strip()):
        raise ProtocolError("invalid_text")
    if any(ord(c) < 32 and c not in "\n\t\r" for c in value):
        raise ProtocolError("invalid_text")
    return value


def check_locale(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", value) is None:
        raise ProtocolError("invalid_locale")
    return value


_DRIVE = re.compile(r"^[A-Za-z]:")


def check_write_path(value: object) -> str:
    """A safe relative declaration under the managed root (§5) — syntax only.

    ``dir/*`` is the Studio's directory form (§10.5). Rejected: absolute, drive, UNC,
    backslashes, ``..``/``.``/empty segments, ``:``, wildcards other than a final ``/*``,
    control characters and anything over 256 UTF-8 bytes. Symlink escape is checked
    against the real root by :func:`resolve_write_path` on the executor.
    """
    if not isinstance(value, str) or not value:
        raise ProtocolError("invalid_write_path")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ProtocolError("invalid_write_path") from None
    if (size > MAX_WRITE_PATH_BYTES or "\\" in value or ":" in value
            or value.startswith("/") or _DRIVE.match(value)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ProtocolError("invalid_write_path")
    body = value[:-2] if value.endswith("/*") else value
    if not body or "*" in body or "?" in body:
        raise ProtocolError("invalid_write_path")
    if any(part in {"", ".", ".."} for part in body.split("/")):
        raise ProtocolError("invalid_write_path")
    return value


def check_write_paths(values: object) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_WRITE_PATHS:
        raise ProtocolError("invalid_write_paths")
    checked = [check_write_path(v) for v in values]
    if len(set(checked)) != len(checked):
        raise ProtocolError("invalid_write_paths")
    return checked


def resolve_write_path(root: Path, value: str) -> Path:
    """Resolve a checked declaration under *root*, refusing symlink/junction escape."""
    check_write_path(value)
    base = root.resolve(strict=True)
    relative = value[:-2] if value.endswith("/*") else value
    resolved = (base / PurePosixPath(relative)).resolve(strict=False)
    if resolved != base and base not in resolved.parents:
        raise ProtocolError("invalid_write_path")
    return resolved


def _validated(fn):  # noqa: ANN001, ANN202 — adapter: ProtocolError → pydantic error
    def run(value: Any) -> Any:
        try:
            return fn(value)
        except ProtocolError as exc:
            raise ValueError(exc.code) from None

    return AfterValidator(run)


Uuid = Annotated[str, _validated(check_uuid)]
LocalId = Annotated[str, _validated(check_local_id)]
PersistentId = Annotated[str, _validated(lambda v: _persistent_id(v))]
StorageEpoch = Annotated[str, _validated(check_storage_epoch)]
Sha256 = Annotated[str, _validated(check_sha256)]
Timestamp = Annotated[int, _validated(check_timestamp)]
Revision = Annotated[int, _validated(check_revision)]
Title = Annotated[str, _validated(check_title)]
Text = Annotated[str, _validated(check_text)]
Locale = Annotated[str, _validated(check_locale)]
WritePaths = Annotated[list[str], _validated(check_write_paths)]


def _exact_int(value: int) -> Any:
    """A wire integer that must equal *value*. ``Literal[1]`` would also accept ``true``
    (``True == 1``) even in strict mode, so strict ``int`` plus bounds is used instead."""
    return Annotated[int, Field(ge=value, le=value)]


AcpVersion = _exact_int(AICP_VERSION)


def _persistent_id(value: object) -> str:
    try:
        return check_project_id(value)
    except ValueError:
        raise ProtocolError("invalid_persistent_id") from None


class Strict(BaseModel):
    """Every AICP schema: strict types, unknown fields rejected, immutable, no echo."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True,
        populate_by_name=False,
    )

    @classmethod
    def from_wire(cls, value: Any) -> Any:
        """Validate already-parsed JSON data in pydantic's strict *JSON* mode.

        Strict python-mode validation demands enum instances, so a wire string such as
        ``"pending"`` would be refused; JSON mode accepts enum values while still refusing
        ``true`` for an integer and any float. The value is re-encoded canonically first,
        so floats and non-JSON objects are rejected before pydantic sees them.
        """
        return cls.model_validate_json(canonical_json(value))


def _unique(values: list[Any], code: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(code)


# --------------------------------------------------------------------------- canonical JSON


def _check_canonical(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH + 4:  # records may nest slightly deeper than one frame body
        raise ProtocolError("too_deep")
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ProtocolError("invalid_text") from None
        return
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise ProtocolError("integer_out_of_range")
        return
    if isinstance(value, float):
        raise ProtocolError("float_not_allowed")
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_canonical(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError("invalid_key")
            _check_canonical(item, depth + 1)
        return
    raise ProtocolError("not_json")


def canonical_json(value: Any) -> bytes:
    """§7 canonicalisation: sorted keys, integer-only numbers, UTF-8, no whitespace.

    Texts are not normalised: two Unicode spellings of one prompt hash differently.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    _check_canonical(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def record_hash(record: BaseModel | dict, *, exclude: frozenset[str] = frozenset({"hash"})) -> str:
    """Hash of a semantic record without its own ``hash`` and transport fields (§6)."""
    data = record.model_dump(mode="json") if isinstance(record, BaseModel) else dict(record)
    return canonical_hash({k: v for k, v in data.items() if k not in exclude})


# --------------------------------------------------------------------------- strict JSON parse


def _no_float(_: str) -> Any:
    raise ProtocolError("float_not_allowed")


def _no_constant(_: str) -> Any:
    raise ProtocolError("float_not_allowed")


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate_key")
        result[key] = value
    return result


def _shape(value: Any, depth: int, counter: list[int]) -> None:
    # ``depth`` counts enclosing containers: the top-level object/array is level 1, so at
    # most MAX_DEPTH nested containers are accepted (§8 "decoded depth ≤12").
    if isinstance(value, (dict, list)) and depth >= MAX_DEPTH:
        raise ProtocolError("too_deep")
    if isinstance(value, dict):
        counter[0] += len(value)
        if counter[0] > MAX_OBJECT_FIELDS:
            raise ProtocolError("too_many_fields")
        for item in value.values():
            _shape(item, depth + 1, counter)
    elif isinstance(value, list):
        for item in value:
            _shape(item, depth + 1, counter)
    elif isinstance(value, int) and not isinstance(value, bool):
        if not INT64_MIN <= value <= INT64_MAX:
            raise ProtocolError("integer_out_of_range")


def parse_json(data: bytes | str, *, max_bytes: int = MAX_FRAME_BYTES) -> Any:
    """Parse one UTF-8 JSON text strictly: size, duplicates, floats, depth, field count.

    ``str`` input (a WebSocket text frame) is measured as UTF-8. Binary frames are the
    transport's to reject before calling this; bytes here must be valid UTF-8.
    """
    if isinstance(data, str):
        try:
            raw = data.encode("utf-8")
        except UnicodeEncodeError:
            raise ProtocolError("invalid_utf8") from None
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise ProtocolError("protocol_error")
    if len(raw) > max_bytes:
        raise ProtocolError("frame_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("invalid_utf8") from None
    if text.startswith("﻿"):
        raise ProtocolError("invalid_json")
    try:
        value = json.loads(
            text, object_pairs_hook=_pairs, parse_float=_no_float, parse_constant=_no_constant,
        )
    except ProtocolError:
        raise
    except (ValueError, RecursionError):
        raise ProtocolError("invalid_json") from None
    _shape(value, 0, [0])
    return value


# --------------------------------------------------------------------------- identity tuples


class Target(Strict):
    """The full network target (§3). Mutations additionally carry ``executor_epoch``."""

    server_instance_id: Uuid
    executor_registration_id: Uuid
    export_id: Uuid
    persistent_id: PersistentId
    storage_epoch: StorageEpoch
    export_revision: Revision


@dataclass(frozen=True, slots=True)
class RemoteActor:
    """Typed service principal (§4). Never serialised into ``actor_id``/``created_by``.

    Built only from the authenticated forward frame, never from requester JSON.
    """

    user_id: str
    registration_id: str
    grant_id: str
    grant_revision: int
    command_id: str | None = None

    def __post_init__(self) -> None:
        check_uuid(self.user_id)
        check_uuid(self.registration_id)
        check_uuid(self.grant_id)
        check_revision(self.grant_revision)
        if self.command_id is not None:
            check_uuid(self.command_id)


# --------------------------------------------------------------------------- grants (§4)


class Grant(Strict):
    """One consent record; the wire body of ``grant.put`` and the local stored grant."""

    grant_id: Uuid
    revision: Revision
    target: Target
    grantee_user_id: Uuid
    grantee_registration_id: Uuid
    operations: list[Permission] = Field(min_length=1, max_length=len(Permission))
    team_ids: list[LocalId] = Field(min_length=1, max_length=64)
    member_ids: list[LocalId] = Field(min_length=1, max_length=256)
    resource_scope: ResourceScope = ResourceScope.CREATED_BY_THIS_GRANTEE
    resource_ids: list[LocalId] = Field(default_factory=list, max_length=MAX_INDEX_ROWS_PER_EXPORT)
    execution_policy_digest: Sha256
    issued_at: Timestamp
    expires_at: Timestamp
    revoked_at: Timestamp | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Grant:
        _unique(self.operations, "duplicate_operation")
        _unique(self.team_ids, "duplicate_team")
        _unique(self.member_ids, "duplicate_member")
        _unique(self.resource_ids, "duplicate_resource")
        if self.expires_at <= self.issued_at or self.expires_at - self.issued_at > GRANT_MAX_MS:
            raise ValueError("invalid_grant_lifetime")
        ops = set(self.operations)
        if ops & MUTATION_PERMISSIONS and Permission.VIEW_STATUS not in ops:
            raise ValueError("mutation_requires_view_status")
        if Permission.VIEW_TRANSCRIPT in ops and Permission.VIEW_STATUS not in ops:
            raise ValueError("transcript_requires_view_status")
        if Permission.OPERATE_EXISTING in ops and not ops & MUTATION_PERMISSIONS:
            raise ValueError("operate_existing_requires_mutation")
        if (self.resource_scope is ResourceScope.SELECTED_RESOURCES) != bool(self.resource_ids):
            raise ValueError("resource_ids_scope_mismatch")
        if self.revoked_at is not None and self.revoked_at < self.issued_at:
            raise ValueError("invalid_revoked_at")
        return self

    def expired(self, now_ms: int) -> bool:
        """Expired at ``now >= expires_at``; no skew extends it by a millisecond (§7)."""
        return now_ms >= self.expires_at

    def effective(self, now_ms: int) -> bool:
        return self.revoked_at is None and not self.expired(now_ms) and now_ms >= self.issued_at


# --------------------------------------------------------------------------- payloads (§5)


class CatalogListPayload(Strict):
    pass


class ResourceListPayload(Strict):
    team_id: LocalId | None = None
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=MAX_PAGE_ITEMS, ge=1, le=MAX_PAGE_ITEMS)


class ResourceGetPayload(Strict):
    resource_id: LocalId


class TranscriptPagePayload(Strict):
    resource_id: LocalId
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=MAX_PAGE_ITEMS, ge=1, le=MAX_PAGE_ITEMS)
    message_id: LocalId | None = None
    offset: int | None = Field(default=None, ge=0, le=MAX_VISIBLE_RECORD_BYTES)

    @model_validator(mode="after")
    def _chunk(self) -> TranscriptPagePayload:
        if (self.message_id is None) != (self.offset is None):
            raise ValueError("chunk_requires_message_and_offset")
        return self


class StreamSubscribePayload(Strict):
    resource_id: LocalId
    topic: Literal["status", "transcript"]
    stream_epoch: Uuid | None = None
    after_seq: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _cursor(self) -> StreamSubscribePayload:
        if (self.stream_epoch is None) != (self.after_seq is None):
            raise ValueError("cursor_requires_epoch_and_seq")
        return self


class StreamUnsubscribePayload(Strict):
    subscription_id: Uuid


class TaskCreatePayload(Strict):
    team_id: LocalId
    assignee_id: LocalId
    title: Title
    brief: Text
    write_paths: WritePaths = Field(default_factory=list)
    locale: Locale


class TaskEditDraftPayload(Strict):
    resource_id: LocalId
    title: Title | None = None
    brief: Text | None = None
    write_paths: WritePaths | None = None

    @model_validator(mode="after")
    def _something(self) -> TaskEditDraftPayload:
        if self.title is None and self.brief is None and self.write_paths is None:
            raise ValueError("empty_edit")
        return self


class TaskStartPayload(Strict):
    resource_id: LocalId


class TaskNotePayload(Strict):
    resource_id: LocalId
    text: Text


class TaskCancelPayload(Strict):
    resource_id: LocalId


class ChatCreatePayload(Strict):
    team_id: LocalId
    member_id: LocalId
    title: Title
    locale: Locale


class ChatSendPayload(Strict):
    resource_id: LocalId
    text: Text


class ChatCancelPayload(Strict):
    resource_id: LocalId


class CommandGetPayload(Strict):
    command_id: Uuid


PAYLOADS: dict[Operation, type[Strict]] = {
    Operation.CATALOG_LIST: CatalogListPayload,
    Operation.RESOURCE_LIST: ResourceListPayload,
    Operation.RESOURCE_GET: ResourceGetPayload,
    Operation.TRANSCRIPT_PAGE: TranscriptPagePayload,
    Operation.STREAM_SUBSCRIBE: StreamSubscribePayload,
    Operation.STREAM_UNSUBSCRIBE: StreamUnsubscribePayload,
    Operation.TASK_CREATE: TaskCreatePayload,
    Operation.TASK_EDIT_DRAFT: TaskEditDraftPayload,
    Operation.TASK_START: TaskStartPayload,
    Operation.TASK_NOTE: TaskNotePayload,
    Operation.TASK_CANCEL: TaskCancelPayload,
    Operation.CHAT_CREATE: ChatCreatePayload,
    Operation.CHAT_SEND: ChatSendPayload,
    Operation.CHAT_CANCEL: ChatCancelPayload,
    Operation.COMMAND_GET: CommandGetPayload,
}

EXPECTS_RESOURCE_REVISION: frozenset[Operation] = frozenset({
    Operation.TASK_EDIT_DRAFT, Operation.TASK_START, Operation.CHAT_SEND,
})
EXPECTS_RUN_ID: frozenset[Operation] = frozenset({Operation.TASK_CANCEL, Operation.CHAT_CANCEL})


class RequestBody(Strict):
    """Requester → Remote ``request`` body (§6). Actor fields are deliberately absent."""

    target: Target
    expected_executor_epoch: Uuid
    grant_id: Uuid
    grant_revision: Revision
    op: Operation
    command_id: Uuid | None = None
    issued_at: Timestamp | None = None
    expected_resource_revision: Revision | None = None
    expected_run_id: Uuid | None = None
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _shape(self) -> RequestBody:
        mutation = self.op in MUTATIONS
        if mutation != (self.command_id is not None) or mutation != (self.issued_at is not None):
            raise ValueError("mutation_requires_command_id_and_issued_at")
        if (self.op in EXPECTS_RESOURCE_REVISION) != (self.expected_resource_revision is not None):
            raise ValueError("expected_resource_revision_mismatch")
        if (self.op in EXPECTS_RUN_ID) != (self.expected_run_id is not None):
            raise ValueError("expected_run_id_mismatch")
        return self

    def parsed_payload(self) -> Strict:
        try:
            return PAYLOADS[self.op].from_wire(self.payload)
        except ProtocolError:
            raise ValueError("invalid_payload") from None


class ForwardBody(Strict):
    """Remote → executor: the request plus the server-authenticated actor (§6)."""

    request: RequestBody
    source_user_id: Uuid
    source_registration_id: Uuid
    connection_id: Uuid
    current_grant_revision: Revision
    deadline_ms: Timestamp

    def actor(self) -> RemoteActor:
        return RemoteActor(
            user_id=self.source_user_id,
            registration_id=self.source_registration_id,
            grant_id=self.request.grant_id,
            grant_revision=self.current_grant_revision,
            command_id=self.request.command_id,
        )


class ResponseBody(Strict):
    target: Target
    command_id: Uuid | None = None
    state: CommandState | None = None
    code: ErrorCode | None = None
    retry_after: int | None = Field(default=None, ge=0, le=3600)
    result: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _outcome(self) -> ResponseBody:
        if self.state is None and self.code is None:
            raise ValueError("response_requires_state_or_code")
        return self


class EventBody(Strict):
    subscription_id: Uuid
    target: Target
    resource_id: LocalId
    stream_epoch: Uuid
    seq: int = Field(ge=1)
    resource_revision: Revision
    payload: dict[str, Any]


# --------------------------------------------------------------------------- lifecycle (§6)


class Hello(Strict):
    acp: AcpVersion
    server_instance_id: Uuid
    connection_id: Uuid
    lease_seconds: _exact_int(LEASE_SECONDS)  # type: ignore[valid-type]
    heartbeat_seconds: _exact_int(HEARTBEAT_SECONDS)  # type: ignore[valid-type]


class ManifestEntry(Strict):
    export_id: Uuid
    revision: Revision
    hash: Sha256


class ExecutorAttach(Strict):
    executor_epoch: Uuid
    manifest: list[ManifestEntry] = Field(max_length=MAX_ATTACH_MANIFEST)

    @model_validator(mode="after")
    def _unique_exports(self) -> ExecutorAttach:
        _unique([entry.export_id for entry in self.manifest], "duplicate_export")
        return self


class ExportPublish(Strict):
    export_id: Uuid
    persistent_id: PersistentId
    storage_epoch: StorageEpoch
    revision: Revision
    expected_revision: int = Field(ge=0)
    policy_digest: Sha256
    display_name: Title
    hash: Sha256

    @model_validator(mode="after")
    def _cas(self) -> ExportPublish:
        if self.revision != self.expected_revision + 1:
            raise ValueError("revision_must_be_next")
        return self


class ExportUnpublish(Strict):
    export_id: Uuid
    expected_revision: Revision
    revision: Revision
    hash: Sha256

    @model_validator(mode="after")
    def _cas(self) -> ExportUnpublish:
        if self.revision != self.expected_revision + 1:
            raise ValueError("revision_must_be_next")
        return self


class GrantPut(Strict):
    export_id: Uuid
    export_revision: Revision
    grant_id: Uuid
    expected_revision: int = Field(ge=0)
    revision: Revision
    grant: Grant
    hash: Sha256

    @model_validator(mode="after")
    def _cas(self) -> GrantPut:
        if self.revision != self.expected_revision + 1:
            raise ValueError("revision_must_be_next")
        if (self.grant.grant_id != self.grant_id or self.grant.revision != self.revision
                or self.grant.target.export_id != self.export_id
                or self.grant.target.export_revision != self.export_revision):
            raise ValueError("grant_envelope_mismatch")
        return self


class GrantRevoke(Strict):
    export_id: Uuid
    export_revision: Revision
    grant_id: Uuid
    expected_revision: Revision
    revision: Revision
    hash: Sha256

    @model_validator(mode="after")
    def _cas(self) -> GrantRevoke:
        if self.revision != self.expected_revision + 1:
            raise ValueError("revision_must_be_next")
        return self


class MirrorGet(Strict):
    export_id: Uuid
    grant_id: Uuid | None = None


class MirrorAck(Strict):
    export_id: Uuid
    grant_id: Uuid | None = None
    revision: Revision
    hash: Sha256
    state: MirrorState


class HeartbeatPing(Strict):
    nonce: Uuid
    executor_epoch: Uuid


class HeartbeatPong(Strict):
    nonce: Uuid
    executor_epoch: Uuid
    lease_seconds: _exact_int(LEASE_SECONDS)  # type: ignore[valid-type]


class IndexRow(Strict):
    """Authorisation metadata only — no title/brief/text (§6 ``resource.index``)."""

    resource_id: LocalId
    kind: ResourceKind
    team_id: LocalId
    member_id: LocalId
    remote_origin: Uuid | None = None  # source registration that created it, if remote
    revision: Revision


class IndexBegin(Strict):
    export_id: Uuid
    export_revision: Revision
    index_revision: Revision
    row_count: int = Field(ge=0, le=MAX_INDEX_ROWS_PER_EXPORT)
    hash: Sha256


class IndexPage(Strict):
    index_revision: Revision
    page_no: int = Field(ge=0, le=MAX_INDEX_ROWS_PER_EXPORT // MAX_PAGE_ITEMS + 1)
    rows: list[IndexRow] = Field(max_length=MAX_PAGE_ITEMS)


class IndexCommit(Strict):
    index_revision: Revision
    hash: Sha256


FRAME_BODIES: dict[FrameType, type[Strict]] = {
    FrameType.HELLO: Hello,
    FrameType.EXECUTOR_ATTACH: ExecutorAttach,
    FrameType.REQUEST: RequestBody,
    FrameType.FORWARD: ForwardBody,
    FrameType.RESPONSE: ResponseBody,
    FrameType.EVENT: EventBody,
    FrameType.ACK: MirrorAck,
    FrameType.EXPORT_PUBLISH: ExportPublish,
    FrameType.EXPORT_UNPUBLISH: ExportUnpublish,
    FrameType.GRANT_PUT: GrantPut,
    FrameType.GRANT_REVOKE: GrantRevoke,
    FrameType.MIRROR_GET: MirrorGet,
    FrameType.HEARTBEAT_PING: HeartbeatPing,
    FrameType.HEARTBEAT_PONG: HeartbeatPong,
    FrameType.RESOURCE_INDEX_BEGIN: IndexBegin,
    FrameType.RESOURCE_INDEX_PAGE: IndexPage,
    FrameType.RESOURCE_INDEX_COMMIT: IndexCommit,
}
# directory.list has no fixed body schema in the design yet: its request is ``{}``.
FRAME_BODIES[FrameType.DIRECTORY_LIST] = CatalogListPayload


class Frame(Strict):
    """Outer frame ``{acp:1,type,request_id,body}`` (§6)."""

    acp: AcpVersion
    type: FrameType
    request_id: Uuid
    body: dict[str, Any]

    def parsed_body(self) -> Strict:
        try:
            return FRAME_BODIES[self.type].from_wire(self.body)
        except ProtocolError:
            raise ValueError("invalid_body") from None


def parse_frame(data: bytes | str) -> tuple[Frame, Strict]:
    """Strict JSON → envelope → typed body. Any failure is a fixed ``protocol_error``.

    The caller closes the socket with :attr:`CloseCode.PROTOCOL_ERROR`; the message never
    includes the frame, so a prompt or token cannot leak through an error path.
    """
    value = parse_json(data)
    if not isinstance(value, dict):
        raise ProtocolError("protocol_error")
    try:
        frame = Frame.from_wire(value)
        return frame, frame.parsed_body()
    except (ValidationError, ValueError):
        raise ProtocolError("protocol_error") from None


def encode_frame(frame_type: FrameType, request_id: str, body: Strict) -> bytes:
    """Serialise a frame canonically and enforce the 64 KiB limit on what we send too."""
    frame = Frame(acp=1, type=frame_type, request_id=request_id,
                  body=body.model_dump(mode="json"))
    data = canonical_json(frame)
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large")
    return data


# --------------------------------------------------------------------------- discovery (§6)


class Capabilities(Strict):
    """Exact mounted-only DTO ``{acp:1,server_instance_id,ai_control:{versions:[1]}}``."""

    class AiControl(Strict):
        versions: list[int] = Field(min_length=1, max_length=16)

    acp: int
    server_instance_id: Uuid
    ai_control: AiControl


class DiscoveryKind(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    AUTH_FAILURE = "auth_failure"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    kind: DiscoveryKind
    code: str | None = None
    server_instance_id: str | None = None


DISCOVERY_REQUEST_HEADERS = {"Cache-Control": "no-store", "Accept": "application/json"}
"""Plus only ``Authorization: Bearer <personal key>`` — never device headers (§6)."""
FORBIDDEN_DISCOVERY_HEADERS = frozenset({"x-vxs-client-id", "x-vxs-device-token"})


def classify_transport_failure(code: str = "network") -> DiscoveryOutcome:
    """Network/timeout/TLS → ``unavailable`` with a safe fixed code; no fallback."""
    safe = code if code in {"network", "timeout", "tls"} else "network"
    return DiscoveryOutcome(DiscoveryKind.UNAVAILABLE, safe)


def classify_capabilities(
    status: int, body: bytes | None, *, redirected: bool = False,
) -> DiscoveryOutcome:
    """Typed outcome of ``GET /api/ai/v1/capabilities`` (§6). Pure: no I/O, no side effect.

    A negative outcome never implies Register, sync/connect, a control socket or Documents
    verification changes — the caller only displays it.
    """
    if redirected or 300 <= status < 400:
        return DiscoveryOutcome(DiscoveryKind.ERROR, "redirect_refused")
    if status == 404:
        return DiscoveryOutcome(DiscoveryKind.UNSUPPORTED, "not_mounted")
    if status == 401:
        return DiscoveryOutcome(DiscoveryKind.AUTH_FAILURE, "auth_failure")
    if status != 200:
        return DiscoveryOutcome(DiscoveryKind.ERROR, "remote_error")
    try:
        value = parse_json(body or b"")
    except ProtocolError:
        return DiscoveryOutcome(DiscoveryKind.ERROR, "invalid_response")
    if not isinstance(value, dict):
        return DiscoveryOutcome(DiscoveryKind.ERROR, "invalid_response")
    # Envelope first: acp integer and a non-empty list of unique integer versions.
    control = value.get("ai_control")
    versions = control.get("versions") if isinstance(control, dict) else None
    if (type(value.get("acp")) is not int or not isinstance(versions, list) or not versions
            or any(type(v) is not int for v in versions) or len(set(versions)) != len(versions)):
        return DiscoveryOutcome(DiscoveryKind.ERROR, "invalid_response")
    if value["acp"] != AICP_VERSION or AICP_VERSION not in versions:
        return DiscoveryOutcome(DiscoveryKind.UNSUPPORTED, "incompatible_version")
    if versions != [AICP_VERSION]:
        return DiscoveryOutcome(DiscoveryKind.ERROR, "invalid_response")
    try:
        parsed = Capabilities.model_validate(value)
    except ValidationError:
        return DiscoveryOutcome(DiscoveryKind.ERROR, "invalid_response")
    return DiscoveryOutcome(DiscoveryKind.SUPPORTED, None, parsed.server_instance_id)


# --------------------------------------------------------------------------- admission clock


def check_issued_at(issued_at: int, now_ms: int) -> None:
    """New mutation deadline (§7): ≤60 s old, ≤30 s in the future; skew never extends age."""
    check_timestamp(issued_at)
    if issued_at > now_ms + MAX_CLOCK_SKEW_MS:
        raise ProtocolError(ErrorCode.EXPIRED)
    if now_ms - issued_at > MUTATION_MAX_AGE_MS:
        raise ProtocolError(ErrorCode.EXPIRED)
