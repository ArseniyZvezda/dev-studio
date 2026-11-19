"""AI Control Protocol 1 (ACP/1) wire rules on the Remote side (ai-access design §5–§8).

A separate namespace (``/api/ai/v1``) and version (``acp=1``): nothing here touches the
Documents protocol in :mod:`.protocol`, whose ``PROTOCOL_VERSION`` stays as it is. The
Studio restates these rules in its own package; the two share only the wire.

Everything is strict: unknown fields, duplicate keys, booleans posing as integers, binary
frames and oversized/deep payloads are protocol errors, never silently coerced.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

ACP_VERSION = 1
SUPPORTED_VERSIONS = (1,)

# --- §8 bounds (the Remote enforces them before allocation; the PC may go lower) ----------
MAX_FRAME_BYTES = 64 * 1024
MAX_DEPTH = 12
MAX_FIELDS = 128
MAX_TEXT_BYTES = 16 * 1024
MAX_TITLE_BYTES = 480
MAX_WRITE_PATHS = 64
MAX_WRITE_PATH_BYTES = 256
MAX_PAGE_ITEMS = 50
MAX_INFLIGHT = 16
HEARTBEAT_SECONDS = 5
LEASE_SECONDS = 15
RECEIPT_TIMEOUT_SECONDS = 10.0
SWEEP_SECONDS = 5.0
MAX_REGISTRATIONS_PER_USER = 32
MAX_EXPORTS_PER_EXECUTOR = 16
MAX_GRANTS_PER_EXPORT = 128
MAX_INDEX_ROWS = 1024
MAX_INDEX_ROW_BYTES = 1024
MAX_STAGING_BYTES = 2 * 1024 * 1024
MAX_MANIFEST = 16
MAX_SUBSCRIPTIONS_PER_SOURCE = 8
MAX_SUBSCRIPTIONS_PER_EXPORT = 32
STREAM_FRAMES_PER_SECOND = 20
STREAM_BYTES_PER_SECOND = 256 * 1024
STREAM_BUFFER_BYTES = 1024 * 1024
STREAM_BUFFER_FRAMES = 128
MAX_GRANT_MS = 30 * 24 * 3600 * 1000
MAX_ISSUED_SKEW_MS = 30_000
MAX_COMMAND_AGE_MS = 60_000
INT64_MAX = 2**63 - 1

# --- §6 close codes and per-request codes ------------------------------------------------
CLOSE_PROTOCOL_ERROR = 4400
CLOSE_UNAUTHORISED = 4401
CLOSE_PERMISSION_REVOKED = 4403
CLOSE_EXECUTOR_CONFLICT = 4409
CLOSE_OVERLOADED = 4429

ERROR_CODES = frozenset({
    "not_available", "offline", "denied", "stale_target", "revision_conflict", "idempotency_conflict",
    "busy", "expired", "rate_limited", "outcome_unknown", "resync_required",
    "snapshot_changed", "unsupported", "capacity_exceeded", "duplicate_export",
    "invalid_request",
})
COMMAND_STATES = frozenset({
    "rejected", "accepted", "dispatching", "running", "completed", "failed", "cancelled",
    "denied_before_dispatch", "outcome_unknown",
})

# --- §4/§5 operations and permissions ---------------------------------------------------
PERMISSIONS = frozenset({
    "view_status", "view_transcript", "create_task", "edit_task", "run_task", "write_task",
    "cancel_task", "create_chat", "run_chat", "cancel_chat", "operate_existing",
})
RESOURCE_SCOPES = frozenset({"created_by_this_grantee", "selected_resources",
                             "all_resources_in_selected_teams"})
# op -> (permissions required, is_mutation, needs resource_id)
OPERATIONS: dict[str, tuple[frozenset[str], bool, bool]] = {
    "catalog.list": (frozenset({"view_status"}), False, False),
    "resource.list": (frozenset({"view_status"}), False, False),
    "resource.get": (frozenset({"view_status"}), False, True),
    "transcript.page": (frozenset({"view_status", "view_transcript"}), False, True),
    "stream.subscribe": (frozenset({"view_status"}), False, True),
    "stream.unsubscribe": (frozenset(), False, False),
    "task.create": (frozenset({"view_status", "create_task"}), True, False),
    "task.edit_draft": (frozenset({"view_status", "edit_task"}), True, True),
    "task.start": (frozenset({"view_status", "run_task"}), True, True),
    "task.note": (frozenset({"view_status", "write_task"}), True, True),
    "task.cancel": (frozenset({"view_status", "cancel_task"}), True, True),
    "chat.create": (frozenset({"view_status", "create_chat"}), True, False),
    "chat.send": (frozenset({"view_status", "run_chat"}), True, True),
    "chat.cancel": (frozenset({"view_status", "cancel_chat"}), True, True),
    "command.get": (frozenset(), False, False),
}
RESOURCE_KINDS = frozenset({"task", "chat"})
REGISTRATION_KINDS = frozenset({"studio", "android"})

CLIENT_TYPES = frozenset({
    "executor.attach", "directory.list", "request", "response", "event",
    "export.publish", "export.unpublish", "grant.put", "grant.revoke", "mirror.get",
    "resource.index.begin", "resource.index.page", "resource.index.commit", "heartbeat.ping",
})
EXECUTOR_TYPES = frozenset({
    "response", "event", "export.publish", "export.unpublish", "grant.put", "grant.revoke",
    "mirror.get", "resource.index.begin", "resource.index.page", "resource.index.commit",
    "heartbeat.ping",
})

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_PERSISTENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_LOCALE = re.compile(r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8}){0,3}")


class AcpError(ValueError):
    """A frame or body that breaks the wire contract: closes the socket (4400)."""

    code = "protocol_error"


class RequestRejected(ValueError):
    """A valid frame whose request is refused: answered, the socket stays open."""

    def __init__(self, code: str, *, retry_after: int | None = None, result: Any = None) -> None:
        assert code in ERROR_CODES, code
        super().__init__(code)
        self.code, self.retry_after, self.result = code, retry_after, result


# --- primitives ------------------------------------------------------------------------

def is_int(value: object) -> bool:
    return type(value) is int


def check_int(value: object, low: int = 0, high: int = INT64_MAX) -> int:
    if type(value) is not int or not low <= value <= high:
        raise AcpError("integer out of range")
    return value


def check_uuid(value: object) -> str:
    """Canonical lowercase hyphenated UUID; no braces, no upper case, no rewriting."""
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise AcpError("canonical UUID required")
    return value


def check_uuid4(value: object) -> str:
    check_uuid(value)
    if uuid.UUID(value).version != 4:
        raise AcpError("UUIDv4 required")
    return value


def check_opaque(value: object) -> str:
    if not isinstance(value, str) or _OPAQUE.fullmatch(value) is None:
        raise AcpError("opaque identifier required")
    return value


def check_hash(value: object) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise AcpError("SHA-256 hex digest required")
    return value


def check_persistent_id(value: object) -> str:
    if not isinstance(value, str) or _PERSISTENT.fullmatch(value) is None:
        raise AcpError("persistent project id required")
    return value


def check_text(value: object, max_bytes: int, *, allow_empty: bool = False,
               single_line: bool = False) -> str:
    if not isinstance(value, str):
        raise AcpError("text required")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise AcpError("UTF-8 text required") from None
    if size > max_bytes or (not allow_empty and not value.strip()):
        raise AcpError("text length out of range")
    if "\x00" in value or (single_line and any(ch in value for ch in "\r\n")):
        raise AcpError("control characters not allowed")
    return value


def check_label(value: object) -> str:
    text = check_text(value, 120, single_line=True)
    if text != text.strip() or any(ord(ch) < 32 or 0x202A <= ord(ch) <= 0x202E or
                                   0x2066 <= ord(ch) <= 0x2069 for ch in text):
        raise AcpError("invalid display label")
    return text


def check_locale(value: object) -> str:
    if not isinstance(value, str) or _LOCALE.fullmatch(value) is None:
        raise AcpError("locale required")
    return value


def check_write_path(value: object) -> str:
    """Safe relative coordination path; the PC re-checks it against its managed root."""
    text = check_text(value, MAX_WRITE_PATH_BYTES, single_line=True)
    if (text.startswith(("/", "\\")) or "\\" in text or re.match(r"[A-Za-z]:", text)
            or any(part in ("", ".", "..") for part in text.removesuffix("/*").split("/"))
            or "*" in text.removesuffix("/*") or any(ord(ch) < 32 for ch in text)):
        raise AcpError("unsafe write path")
    return text


def check_list(value: object, item, *, max_items: int, unique: bool = True,
               allow_empty: bool = True) -> list:
    if type(value) is not list or len(value) > max_items or (not allow_empty and not value):
        raise AcpError("list out of range")
    items = [item(entry) for entry in value]
    if unique and len(set(items)) != len(items):
        raise AcpError("duplicate list entries")
    return items


def fields(obj: object, required: set[str] | frozenset[str], optional: set[str] | frozenset[str] = frozenset()) -> dict:
    if type(obj) is not dict:
        raise AcpError("object required")
    keys = set(obj)
    if not required <= keys or not keys <= set(required) | set(optional):
        raise AcpError("unexpected or missing fields")
    return obj


# --- JSON -----------------------------------------------------------------------------

def _unique(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise AcpError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    raise AcpError("non-finite number")


def _shape(value: Any, depth: int, counter: list[int]) -> None:
    if depth > MAX_DEPTH:
        raise AcpError("JSON too deep")
    if type(value) is dict:
        counter[0] += len(value)
        if counter[0] > MAX_FIELDS:
            raise AcpError("too many JSON fields")
        for item in value.values():
            _shape(item, depth + 1, counter)
    elif type(value) is list:
        for item in value:
            _shape(item, depth + 1, counter)
    elif type(value) is float:
        raise AcpError("floating point numbers are not part of ACP/1")


def field_count(value: Any) -> int:
    """Total object fields in *value*, as :func:`decode` counts them against MAX_FIELDS."""
    if type(value) is dict:
        return len(value) + sum(field_count(item) for item in value.values())
    if type(value) is list:
        return sum(field_count(item) for item in value)
    return 0


def decode(raw: str | bytes, *, max_bytes: int = MAX_FRAME_BYTES) -> Any:
    """Strict UTF-8 JSON: size, duplicate keys, depth, field count, no floats/NaN."""
    if isinstance(raw, str):
        try:
            data = raw.encode("utf-8")
        except UnicodeEncodeError:
            raise AcpError("UTF-8 required") from None
    elif isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    else:
        raise AcpError("text frame required")
    if len(data) > max_bytes:
        raise AcpError("frame too large")
    try:
        text = data.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except AcpError:
        raise
    except (ValueError, RecursionError):
        raise AcpError("malformed JSON") from None
    _shape(value, 1, [0])
    return value


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def canonical(value: Any) -> bytes:
    """§7 canonical JSON: sorted keys, integers only, UTF-8, no whitespace, no text normalisation."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def frame(kind: str, request_id: str, body: dict) -> str:
    return encode({"acp": ACP_VERSION, "type": kind, "request_id": request_id, "body": body})


def parse_frame(raw: str | bytes) -> tuple[str, str, dict]:
    message = fields(decode(raw), {"acp", "type", "request_id", "body"})
    if message["acp"] != ACP_VERSION or type(message["acp"]) is not int:
        raise AcpError("unsupported acp version")
    kind = message["type"]
    if kind not in CLIENT_TYPES:
        raise AcpError("unknown frame type")
    request_id = check_uuid(message["request_id"])
    if type(message["body"]) is not dict:
        raise AcpError("body must be an object")
    return kind, request_id, message["body"]


# --- REST bodies -----------------------------------------------------------------------

def capabilities(server_instance_id: str) -> dict:
    """§6 exact discovery DTO, nothing more."""
    return {"acp": ACP_VERSION, "server_instance_id": server_instance_id,
            "ai_control": {"versions": list(SUPPORTED_VERSIONS)}}


def registration_request(body: object) -> dict:
    fields(body, {"client_id", "display_name", "kind"})
    check_uuid4(body["client_id"])
    check_label(body["display_name"])
    if body["kind"] not in REGISTRATION_KINDS:
        raise AcpError("unknown registration kind")
    return body


# --- target and records ----------------------------------------------------------------

TARGET_FIELDS = frozenset({"server_instance_id", "executor_registration_id", "export_id",
                           "persistent_id", "storage_epoch", "export_revision"})


def check_target(value: object) -> dict:
    fields(value, TARGET_FIELDS)
    check_uuid(value["server_instance_id"])
    check_uuid(value["executor_registration_id"])
    check_uuid(value["export_id"])
    check_persistent_id(value["persistent_id"])
    check_opaque(value["storage_epoch"])
    check_int(value["export_revision"], 1)
    return value


GRANT_FIELDS = frozenset({"grant_id", "revision", "target", "grantee_user_id",
                          "grantee_registration_id", "operations", "team_ids", "member_ids",
                          "resource_scope", "resource_ids", "execution_policy_digest",
                          "issued_at", "expires_at", "revoked_at"})


def check_grant(value: object) -> dict:
    """§4 grant record. ``resource_ids`` holds the exact IDs of ``selected_resources``."""
    fields(value, GRANT_FIELDS)
    check_uuid(value["grant_id"])
    check_int(value["revision"], 1)
    check_target(value["target"])
    if not isinstance(value["grantee_user_id"], str) or re.fullmatch(r"[0-9a-f]{32}", value["grantee_user_id"]) is None:
        raise AcpError("grantee user id required")
    check_uuid(value["grantee_registration_id"])
    ops = check_list(value["operations"], _permission, max_items=len(PERMISSIONS), allow_empty=False)
    if "view_status" not in ops:
        raise AcpError("every grant needs view_status")
    check_list(value["team_ids"], check_opaque, max_items=64, allow_empty=False)
    check_list(value["member_ids"], check_opaque, max_items=64)
    if value["resource_scope"] not in RESOURCE_SCOPES:
        raise AcpError("unknown resource scope")
    ids = check_list(value["resource_ids"], check_opaque, max_items=MAX_INDEX_ROWS)
    if (value["resource_scope"] == "selected_resources") != bool(ids):
        raise AcpError("resource_ids only and always for selected_resources")
    check_hash(value["execution_policy_digest"])
    issued, expires = check_int(value["issued_at"], 0), check_int(value["expires_at"], 1)
    if not issued < expires <= issued + MAX_GRANT_MS:
        raise AcpError("grant lifetime out of range")
    if value["revoked_at"] is not None:
        raise AcpError("a put grant is never revoked; use grant.revoke")
    return value


def _permission(value: object) -> str:
    if value not in PERMISSIONS:
        raise AcpError("unknown permission")
    return value  # type: ignore[return-value]


def export_record(body: dict) -> dict:
    return {"kind": "export", "export_id": body["export_id"], "persistent_id": body["persistent_id"],
            "storage_epoch": body["storage_epoch"], "revision": body["revision"],
            "policy_digest": body["policy_digest"], "display_name": body["display_name"],
            "state": "published"}


def unpublish_record(export_id: str, revision: int) -> dict:
    return {"kind": "export", "export_id": export_id, "revision": revision, "state": "unpublished"}


def grant_record(export_id: str, export_revision: int, grant: dict) -> dict:
    return {"kind": "grant", "export_id": export_id, "export_revision": export_revision,
            "grant": grant, "state": "active"}


def revoke_record(export_id: str, export_revision: int, grant_id: str, revision: int) -> dict:
    return {"kind": "grant", "export_id": export_id, "export_revision": export_revision,
            "grant_id": grant_id, "revision": revision, "state": "revoked"}


def index_rows_hash(rows: list[dict]) -> str:
    return semantic_hash({"kind": "resource.index", "rows": sorted(rows, key=lambda r: r["resource_id"])})


def check_index_row(value: object) -> dict:
    fields(value, {"resource_id", "kind", "team_id", "member_id", "remote_origin", "revision"})
    check_opaque(value["resource_id"])
    if value["kind"] not in RESOURCE_KINDS:
        raise AcpError("unknown resource kind")
    check_opaque(value["team_id"])
    if value["member_id"] is not None:
        check_opaque(value["member_id"])
    origin = value["remote_origin"]
    if origin is not None:
        fields(origin, {"user_id", "registration_id"})
        if not isinstance(origin["user_id"], str) or re.fullmatch(r"[0-9a-f]{32}", origin["user_id"]) is None:
            raise AcpError("origin user id required")
        check_uuid(origin["registration_id"])
    check_int(value["revision"], 1)
    if len(canonical(value)) > MAX_INDEX_ROW_BYTES:
        raise AcpError("index row too large")
    return value


# --- request payloads (§5) --------------------------------------------------------------

def _cursor(value: object) -> None:
    if value is not None:
        check_text(value, 512, single_line=True)


def _limit(body: dict) -> None:
    if "limit" in body:
        check_int(body["limit"], 1, MAX_PAGE_ITEMS)


def check_payload(op: str, payload: object) -> dict:
    if op == "catalog.list":
        fields(payload, set())
    elif op == "resource.list":
        fields(payload, set(), {"kind", "team_id", "cursor", "limit"})
        if "kind" in payload and payload["kind"] not in RESOURCE_KINDS:
            raise AcpError("unknown resource kind")
        if "team_id" in payload:
            check_opaque(payload["team_id"])
        _cursor(payload.get("cursor"))
        _limit(payload)
    elif op == "resource.get":
        fields(payload, set())
    elif op == "transcript.page":
        fields(payload, set(), {"cursor", "limit"})
        _cursor(payload.get("cursor"))
        _limit(payload)
    elif op == "stream.subscribe":
        fields(payload, {"topic"}, {"cursor"})
        if payload["topic"] not in ("status", "transcript"):
            raise AcpError("unknown topic")
        _cursor(payload.get("cursor"))
    elif op == "stream.unsubscribe":
        fields(payload, {"subscription_id"})
        check_uuid(payload["subscription_id"])
    elif op == "task.create":
        fields(payload, {"team_id", "member_id", "title", "brief", "write_paths", "locale"})
        check_opaque(payload["team_id"])
        check_opaque(payload["member_id"])
        check_text(payload["title"], MAX_TITLE_BYTES, single_line=True)
        check_text(payload["brief"], MAX_TEXT_BYTES, allow_empty=True)
        check_list(payload["write_paths"], check_write_path, max_items=MAX_WRITE_PATHS)
        check_locale(payload["locale"])
    elif op == "task.edit_draft":
        fields(payload, set(), {"title", "brief", "write_paths"})
        if not payload:
            raise AcpError("nothing to edit")
        if "title" in payload:
            check_text(payload["title"], MAX_TITLE_BYTES, single_line=True)
        if "brief" in payload:
            check_text(payload["brief"], MAX_TEXT_BYTES, allow_empty=True)
        if "write_paths" in payload:
            check_list(payload["write_paths"], check_write_path, max_items=MAX_WRITE_PATHS)
    elif op in ("task.start", "task.cancel", "chat.cancel"):
        fields(payload, set())
    elif op in ("task.note", "chat.send"):
        fields(payload, {"text"})
        check_text(payload["text"], MAX_TEXT_BYTES)
    elif op == "chat.create":
        fields(payload, {"team_id", "member_id", "title", "locale"})
        check_opaque(payload["team_id"])
        check_opaque(payload["member_id"])
        check_text(payload["title"], MAX_TITLE_BYTES, single_line=True)
        check_locale(payload["locale"])
    elif op == "command.get":
        fields(payload, {"command_id"})
        check_uuid(payload["command_id"])
    else:  # pragma: no cover - op validated by caller
        raise AcpError("unknown operation")
    return payload


REQUEST_REQUIRED = frozenset({"target", "grant_id", "grant_revision", "op", "payload"})
REQUEST_OPTIONAL = frozenset({"expected_executor_epoch", "command_id", "issued_at", "resource_id",
                              "expected_resource_revision", "expected_run_id"})


def check_request(body: dict) -> dict:
    """§6 ``request`` body. Actor/source fields are not accepted: they come from auth."""
    fields(body, REQUEST_REQUIRED, REQUEST_OPTIONAL)
    check_target(body["target"])
    check_uuid(body["grant_id"])
    check_int(body["grant_revision"], 1)
    op = body["op"]
    if op not in OPERATIONS:
        raise AcpError("unknown operation")
    _, mutation, needs_resource = OPERATIONS[op]
    if mutation:
        for key in ("command_id", "issued_at", "expected_executor_epoch"):
            if key not in body:
                raise AcpError("mutation requires command_id, issued_at and executor epoch")
        check_uuid4(body["command_id"])
        check_int(body["issued_at"], 0)
    elif "command_id" in body or "issued_at" in body:
        raise AcpError("reads carry no command_id/issued_at")
    if "expected_executor_epoch" in body:
        check_uuid(body["expected_executor_epoch"])
    if needs_resource != ("resource_id" in body):
        raise AcpError("resource_id required exactly for resource operations")
    if "resource_id" in body:
        check_opaque(body["resource_id"])
    if "expected_resource_revision" in body:
        if op == "task.note":
            raise AcpError("expected_resource_revision forbidden for task.note")
        check_int(body["expected_resource_revision"], 1)
    if "expected_run_id" in body:
        check_uuid(body["expected_run_id"])
    if op in ("task.edit_draft", "task.start", "chat.send") and "expected_resource_revision" not in body:
        raise AcpError("expected_resource_revision required")
    if op in ("task.cancel", "chat.cancel") and "expected_run_id" not in body:
        raise AcpError("expected_run_id required")
    check_payload(op, body["payload"])
    return body


def check_response(body: dict) -> dict:
    fields(body, {"target", "state"}, {"command_id", "code", "retry_after", "result"})
    check_target(body["target"])
    if body["state"] not in COMMAND_STATES:
        raise AcpError("unknown command state")
    if "command_id" in body:
        check_uuid(body["command_id"])
    if "code" in body and body["code"] not in ERROR_CODES:
        raise AcpError("unknown code")
    if "retry_after" in body:
        check_int(body["retry_after"], 0, 3600)
    return body


def check_event(body: dict) -> dict:
    fields(body, {"subscription_id", "target", "resource_id", "stream_epoch", "seq",
                  "resource_revision", "payload"})
    check_uuid(body["subscription_id"])
    check_target(body["target"])
    check_opaque(body["resource_id"])
    check_uuid(body["stream_epoch"])
    check_int(body["seq"], 0)
    check_int(body["resource_revision"], 1)
    if type(body["payload"]) is not dict:
        raise AcpError("event payload must be an object")
    return body
