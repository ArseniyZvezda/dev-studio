"""Bounded, allowlisted stdin RPC for the SSH boundary (dev spec §16.9)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import UUID

from .auth import hash_token, new_token
from .protocol import PROTOCOL_VERSION

ADMIN_RPC_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
OPERATIONS = (
    "capabilities", "users.list", "users.get", "users.create", "users.update",
    "users.rotate", "users.revoke", "users.delete", "projects.list", "projects.update",
    "migration.inspect",
)
MUTATIONS = frozenset({"users.create", "users.update", "users.rotate", "users.revoke", "users.delete", "projects.update"})
ERROR_MESSAGES = {
    "invalid_request": "Invalid RPC request.", "unsupported_version": "Unsupported RPC version.",
    "unsupported_schema": "Offline schema migration or a server update is required.",
    "user_not_found": "User not found.", "user_exists": "User ID already exists.",
    "user_deleted": "Deleted users cannot be changed.", "metadata_conflict": "Metadata changed; read and confirm again.",
    "credential_conflict": "Credential changed; read and confirm again.",
    "project_not_found": "Project not found.", "temporarily_unavailable": "Operation unavailable; read status before another action.",
    "restore_required": "Restoring a revoked user requires explicit confirmation.",
}


class RPCValidationError(ValueError):
    pass


class RPCVersionError(RPCValidationError):
    pass


def uuid_value(value: object, *, proposed: bool = False) -> str:
    if not isinstance(value, str):
        raise RPCValidationError()
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise RPCValidationError() from None
    if value not in (parsed.hex, str(parsed)) or (proposed and (value != parsed.hex or parsed.version != 4)):
        raise RPCValidationError()
    return value


def _fields(obj, required=(), optional=()):
    if type(obj) is not dict or not set(required) <= obj.keys() or obj.keys() - set(required) - set(optional):
        raise RPCValidationError()


def _integer(value, low=1, high=2**63 - 1):
    if type(value) is not int or not low <= value <= high:
        raise RPCValidationError()


def _project_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value) is None:
        raise RPCValidationError()


def validate_payload(operation, p):
    if operation not in OPERATIONS:
        raise RPCValidationError()
    if operation in ("capabilities", "migration.inspect"):
        _fields(p)
    elif operation in ("users.list", "projects.list"):
        _fields(p, optional=("limit", "after", "include_deleted") if operation == "users.list" else ("limit", "after"))
        if "limit" in p: _integer(p["limit"], high=100)
        if "after" in p:
            (uuid_value if operation == "users.list" else _project_id)(p["after"])
        if "include_deleted" in p and type(p["include_deleted"]) is not bool: raise RPCValidationError()
    elif operation == "users.get":
        _fields(p, ("user_id",)); uuid_value(p["user_id"])
    elif operation == "users.create":
        _fields(p, ("user_id", "display_name"), ("color",))
        uuid_value(p["user_id"], proposed=True)
    elif operation == "users.update":
        _fields(p, ("user_id", "expected_metadata_revision"), ("display_name", "color"))
        uuid_value(p["user_id"]); _integer(p["expected_metadata_revision"])
        if not {"display_name", "color"} & p.keys(): raise RPCValidationError()
    elif operation in ("users.rotate", "users.revoke", "users.delete"):
        _fields(p, ("user_id", "expected_metadata_revision", "expected_credential_version"), ("restore_revoked",) if operation == "users.rotate" else ())
        uuid_value(p["user_id"])
        _integer(p["expected_metadata_revision"]); _integer(p["expected_credential_version"])
        if "restore_revoked" in p and type(p["restore_revoked"]) is not bool: raise RPCValidationError()
    elif operation == "projects.update":
        _fields(p, ("project_id", "display_name", "expected_metadata_revision"))
        _project_id(p["project_id"]); _integer(p["expected_metadata_revision"])
    if "display_name" in p:
        # The store performs canonical NFC/trim and the full shared display rules.
        from .protocol import check_display_name
        try: check_display_name(p["display_name"])
        except ValueError: raise RPCValidationError() from None
    if "color" in p and (not isinstance(p["color"], str) or re.fullmatch(r"#[0-9a-fA-F]{6}", p["color"]) is None):
        raise RPCValidationError()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise RPCValidationError()
        result[key] = value
    return result


def decode_json(raw: bytes, limit: int):
    if len(raw) > limit: raise RPCValidationError()
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(RPCValidationError()))
    except (ValueError, UnicodeError, RecursionError):
        raise RPCValidationError() from None


def parse_request(raw):
    obj = decode_json(raw, MAX_REQUEST_BYTES)
    _fields(obj, ("admin_rpc_version", "request_id", "operation", "payload"))
    uuid_value(obj["request_id"])
    if type(obj["admin_rpc_version"]) is not int or obj["admin_rpc_version"] != ADMIN_RPC_VERSION:
        raise RPCVersionError()
    if not isinstance(obj["operation"], str): raise RPCValidationError()
    validate_payload(obj["operation"], obj["payload"])
    return obj


def _admin_user(row):
    from .protocol import check_display_name
    value = row.to_admin_json()
    try: check_display_name(row.display_name)
    except ValueError: value["legacy_display_name_invalid"] = True
    return value


def _page(rows, p, key):
    rows = sorted(rows, key=lambda row: row.id)
    if p.get("after") is not None: rows = [r for r in rows if r.id > p["after"]]
    limit = p.get("limit", 50)
    page = rows[:limit]
    return {key: [_admin_user(r) if key == "users" else r.to_json() for r in page],
            "next_after": page[-1].id if len(rows) > limit else None}


def dispatch(store, operation, p):
    if operation == "capabilities":
        from .migrations import SCHEMA_VERSION
        return {"protocol": PROTOCOL_VERSION, "schema_version": SCHEMA_VERSION,
                "admin_rpc_version": ADMIN_RPC_VERSION, "operations": list(OPERATIONS)}
    if operation == "users.list":
        # The historical CLI list is intentionally unpaged. RPC must bound its
        # database result as well as its stdout, and never selects token_hash.
        limit = p.get("limit", 50)
        with store._lock:
            rows = store._db.execute(
                "SELECT id,name,display_name,color,created_at,revoked_at,deleted_at,credential_version,metadata_revision"
                " FROM users WHERE id>?" + ("" if p.get("include_deleted", False) else " AND deleted_at IS NULL") +
                " ORDER BY id LIMIT ?", (p.get("after", ""), limit + 1)).fetchall()
        return _page([store._user(row) for row in rows], p, "users")
    if operation == "users.get":
        user = store.user(p["user_id"])
        if user is None: return None
        return {"user": _admin_user(user)}
    if operation == "users.create":
        key = new_token()
        user = store.create_user(p["display_name"], hash_token(key), user_id=p["user_id"], color=p.get("color"))
        return {"user": _admin_user(user), "issued_key": key}
    if operation == "users.update":
        fields = {k: p[k] for k in ("display_name", "color") if k in p}
        user = store.update_user(p["user_id"], expected_revision=p["expected_metadata_revision"], **fields)
        return {"user": _admin_user(user)}
    if operation in ("users.rotate", "users.revoke", "users.delete"):
        args = {"expected_revision": p["expected_metadata_revision"], "expected_credential_version": p["expected_credential_version"]}
        if operation == "users.rotate":
            current = store.user(p["user_id"])
            if current is not None and current.deleted_at:
                return {"_error": "user_deleted"}
            if current is not None and current.revoked_at and not p.get("restore_revoked", False):
                return {"_error": "restore_required"}
            key = new_token()
            user = store.rotate_user(p["user_id"], hash_token(key), **args)
            return {"user": _admin_user(user), "issued_key": key}
        fn = store.revoke_user_by_id if operation == "users.revoke" else store.delete_user
        return {"user": _admin_user(fn(p["user_id"], **args))}
    if operation == "projects.list":
        page = store.list_projects(limit=p.get("limit", 50), after=p.get("after"))
        projects = page["projects"]
        from .protocol import check_display_name
        with store._lock:
            for project in projects:
                try: check_display_name(project["display_name"])
                except ValueError: project["legacy_display_name_invalid"] = True
                project["documents"] = store._db.execute(
                    "SELECT COUNT(*) FROM docs WHERE space_id=? AND deleted=0", (project["id"],)).fetchone()[0]
        return {"projects": projects, "next_after": page["next_after"]}
    if operation == "projects.update":
        return {"project": store.update_project(p["project_id"], p["display_name"], expected_revision=p["expected_metadata_revision"]).to_json()}
    raise RPCValidationError()


def _error_code(exc):
    # Never include exception text, input, locals, hash or stdout in diagnostics.
    return {
        "UnknownUser": "user_not_found", "UserExists": "user_exists", "UserDeleted": "user_deleted",
        "MetadataConflict": "metadata_conflict", "CredentialConflict": "credential_conflict",
        "UnknownProject": "project_not_found", "ProjectNotFound": "project_not_found",
        "SchemaError": "unsupported_schema", "MigrationRequired": "unsupported_schema",
        "UnsupportedSchema": "unsupported_schema", "RPCValidationError": "invalid_request", "RPCVersionError": "unsupported_version",
        "ValueError": "invalid_request", "InvalidRequest": "invalid_request", "ProtocolError": "invalid_request",
    }.get(type(exc).__name__, "temporarily_unavailable")


def handle_request(raw: bytes, data_dir: Path) -> bytes:
    request_id = None
    store = None
    code = None
    result = None
    try:
        # Correlate invalid payload/schema errors without reflecting untrusted IDs.
        candidate = decode_json(raw, MAX_REQUEST_BYTES)
        if type(candidate) is dict:
            try: request_id = uuid_value(candidate.get("request_id"))
            except RPCValidationError: pass
        request = parse_request(raw)
        request_id = request["request_id"]
        if request["operation"] == "migration.inspect":
            from .migrations import inspect_database
            report = inspect_database(Path(data_dir) / "remote.db")
            result = {k: report[k] for k in ("schema_version", "target_schema_version", "users")}
            result["project_count"] = len(report["projects"])
        else:
            from .store import Store
            path = Path(data_dir) / "remote.db"
            if request["operation"] not in MUTATIONS:
                # Opening a read-only menu/capability probe must not bootstrap DB.
                from .migrations import _readonly, schema_version, SchemaError, SCHEMA_VERSION
                reader = _readonly(path)
                try:
                    if schema_version(reader) != SCHEMA_VERSION:
                        raise SchemaError("RPC requires the current schema")
                finally: reader.close()
            store = Store(path)
            result = dispatch(store, request["operation"], request["payload"])
            if result is None: code = "user_not_found"
            elif "_error" in result: code = result["_error"]
    except Exception as exc:
        code = _error_code(exc)
    finally:
        if store is not None:
            try: store.close()
            except Exception:
                code = "temporarily_unavailable"
                result = None
    response = {"admin_rpc_version": ADMIN_RPC_VERSION, "request_id": request_id}
    if code:
        response["error"] = {"code": code, "message": ERROR_MESSAGES[code]}
    else:
        response["result"] = result
    encoded = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        response.pop("result", None)
        response["error"] = {"code": "temporarily_unavailable", "message": ERROR_MESSAGES["temporarily_unavailable"]}
        encoded = json.dumps(response).encode("utf-8")
    return encoded + b"\n"


def validate_response(raw: bytes, request_id: str, operation: str) -> dict:
    obj = decode_json(raw, MAX_RESPONSE_BYTES)
    _fields(obj, ("admin_rpc_version", "request_id"), ("result", "error"))
    if type(obj["admin_rpc_version"]) is not int or obj["admin_rpc_version"] != 1 or obj["request_id"] != request_id or (("result" in obj) == ("error" in obj)):
        raise RPCValidationError()
    if "error" in obj:
        _fields(obj["error"], ("code", "message"))
        if obj["error"]["code"] not in ERROR_MESSAGES or not isinstance(obj["error"]["message"], str): raise RPCValidationError()
        return {"error": {"code": obj["error"]["code"], "message": ERROR_MESSAGES[obj["error"]["code"]]}}
    result = obj["result"]
    if operation == "capabilities":
        _fields(result, ("protocol", "schema_version", "admin_rpc_version", "operations"))
        for k in ("protocol", "schema_version", "admin_rpc_version"): _integer(result[k])
        if result["admin_rpc_version"] != 1 or result["operations"] != list(OPERATIONS): raise RPCValidationError()
    elif operation.startswith("users."):
        issue = operation in ("users.create", "users.rotate")
        if operation == "users.list":
            _fields(result, ("users", "next_after"))
            if type(result["users"]) is not list or len(result["users"]) > 100: raise RPCValidationError()
            records = result["users"]
            if result["next_after"] is not None: uuid_value(result["next_after"])
        else:
            _fields(result, ("user", "issued_key") if issue else ("user",))
            records = [result["user"]]
            if issue and (not isinstance(result["issued_key"], str) or re.fullmatch(r"vxs_[A-Za-z0-9_-]{43}", result["issued_key"]) is None): raise RPCValidationError()
        for record in records:
            _fields(record, ("id", "display_name", "color", "created_at", "status", "revoked_at", "deleted_at", "credential_version", "metadata_revision"), ("legacy_display_name_invalid",))
            uuid_value(record["id"])
            if not all(isinstance(record[k], str) for k in ("display_name", "color", "created_at")): raise RPCValidationError()
            if record["status"] not in ("active", "revoked", "deleted"): raise RPCValidationError()
            for k in ("revoked_at", "deleted_at"):
                if record[k] is not None and not isinstance(record[k], str): raise RPCValidationError()
            for k in ("credential_version", "metadata_revision"): _integer(record[k])
            if "legacy_display_name_invalid" in record and type(record["legacy_display_name_invalid"]) is not bool: raise RPCValidationError()
        if operation == "users.list":
            ids = [r["id"] for r in records]
            if ids != sorted(set(ids)) or (result["next_after"] is not None and (not ids or result["next_after"] != ids[-1])): raise RPCValidationError()
    elif operation.startswith("projects."):
        if operation == "projects.list":
            _fields(result, ("projects", "next_after"))
            if type(result["projects"]) is not list or len(result["projects"]) > 100: raise RPCValidationError()
            records = result["projects"]
            if result["next_after"] is not None: _project_id(result["next_after"])
        else:
            _fields(result, ("project",)); records = [result["project"]]
        for record in records:
            _fields(record, ("id", "display_name", "storage_epoch", "metadata_revision", "created_at"), ("documents", "legacy_display_name_invalid"))
            _project_id(record["id"]); uuid_value(record["storage_epoch"]); _integer(record["metadata_revision"])
            if not isinstance(record["display_name"], str) or not isinstance(record["created_at"], str): raise RPCValidationError()
            if "documents" in record: _integer(record["documents"], low=0)
            if "legacy_display_name_invalid" in record and type(record["legacy_display_name_invalid"]) is not bool: raise RPCValidationError()
        if operation == "projects.list":
            ids = [r["id"] for r in records]
            if ids != sorted(set(ids)) or (result["next_after"] is not None and (not ids or result["next_after"] != ids[-1])): raise RPCValidationError()
    elif operation == "migration.inspect":
        _fields(result, ("schema_version", "target_schema_version", "users", "project_count"))
        for k in result: _integer(result[k], low=0)
    return {"result": result}
