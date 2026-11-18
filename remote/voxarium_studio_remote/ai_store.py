"""ACP/1 control registry: registrations, export/grant mirrors, resource index, audit.

ai-access design §3–§6, §8–§9 (R-A1). A separate, versioned SQLite file
(``ai-control.db`` beside ``remote.db``) so the Documents schema, its strict
signature check and its offline migration never change when ACP is off. It holds:

- control registrations with the SHA-256 of a random 256-bit device secret only;
- export and grant *mirrors* published by executors (CAS by revision + semantic hash),
  with revoke/unpublish tombstones that are never deleted on reconnect;
- the minimal authorisation-only resource index (no titles, briefs or text);
- an audit of command IDs/op/outcome/time — never prompts, transcripts or bodies.

It stores no offline command queue and no results. Every method is synchronous; callers
pass ``now_ms`` (integer Unix UTC milliseconds) so expiry is testable and explicit.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import ai_protocol as p

SCHEMA_VERSION = 1
APPLICATION_ID = 0x41435031  # "ACP1": refuses to open a Documents database by mistake.
AUDIT_LIMIT = 50_000
SCHEMA = """
CREATE TABLE ai_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE ai_registrations (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, client_id TEXT NOT NULL,
 display_name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('studio','android')),
 token_hash TEXT, user_credential_version INTEGER NOT NULL CHECK (user_credential_version >= 1),
 created_at INTEGER NOT NULL, revoked_at INTEGER, revoke_reason TEXT
);
CREATE UNIQUE INDEX ai_registrations_active ON ai_registrations (user_id, client_id) WHERE revoked_at IS NULL;
CREATE INDEX ai_registrations_by_user ON ai_registrations (user_id);
CREATE TABLE ai_exports (
 export_id TEXT PRIMARY KEY, executor_registration_id TEXT NOT NULL REFERENCES ai_registrations(id),
 persistent_id TEXT NOT NULL, storage_epoch TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK (revision >= 1), policy_digest TEXT NOT NULL,
 display_name TEXT NOT NULL, hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK (state IN ('published','unpublished','deleted')), updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX ai_exports_one_published ON ai_exports (executor_registration_id, persistent_id) WHERE state = 'published';
CREATE TABLE ai_grants (
 grant_id TEXT PRIMARY KEY, export_id TEXT NOT NULL REFERENCES ai_exports(export_id),
 export_revision INTEGER NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 1),
 hash TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('active','revoked')),
 grantee_user_id TEXT NOT NULL, grantee_registration_id TEXT NOT NULL,
 record TEXT, expires_at INTEGER NOT NULL, revoked_at INTEGER, revoke_reason TEXT
);
CREATE INDEX ai_grants_by_export ON ai_grants (export_id);
CREATE INDEX ai_grants_by_grantee ON ai_grants (grantee_registration_id);
CREATE TABLE ai_index_meta (
 export_id TEXT PRIMARY KEY REFERENCES ai_exports(export_id), export_revision INTEGER NOT NULL,
 index_revision INTEGER NOT NULL, hash TEXT NOT NULL, row_count INTEGER NOT NULL
);
CREATE TABLE ai_resource_index (
 export_id TEXT NOT NULL REFERENCES ai_exports(export_id), resource_id TEXT NOT NULL,
 row TEXT NOT NULL, PRIMARY KEY (export_id, resource_id)
);
CREATE TABLE ai_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, user_id TEXT, registration_id TEXT,
 export_id TEXT, op TEXT NOT NULL, command_id TEXT, command_hash TEXT, outcome TEXT NOT NULL
);
"""


class AiSchemaError(ValueError):
    code = "unsupported_schema"


class RegistrationExists(ValueError):
    code = "registration_exists"

    def __init__(self, registration_id: str) -> None:
        super().__init__("registration_exists")
        self.registration_id = registration_id


class CapacityExceeded(ValueError):
    code = "capacity_exceeded"


class NotFound(LookupError):
    code = "not_available"


@dataclass(frozen=True, slots=True)
class Registration:
    id: str
    user_id: str
    client_id: str
    display_name: str
    kind: str
    token_hash: str | None
    user_credential_version: int
    created_at: int
    revoked_at: int | None
    revoke_reason: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None and self.token_hash is not None

    def to_json(self) -> dict:
        """Directory DTO: no roots, exports or credentials."""
        return {"user_id": self.user_id, "registration_id": self.id, "client_id": self.client_id,
                "display_name": self.display_name, "kind": self.kind}

    def __repr__(self) -> str:  # never print the digest
        return f"Registration(id={self.id!r}, user_id={self.user_id!r}, kind={self.kind!r})"


@dataclass(frozen=True, slots=True)
class Export:
    export_id: str
    executor_registration_id: str
    persistent_id: str
    storage_epoch: str
    revision: int
    policy_digest: str
    display_name: str
    hash: str
    state: str
    updated_at: int

    def ack(self) -> dict:
        return {"export_id": self.export_id, "revision": self.revision, "hash": self.hash,
                "state": self.state}


@dataclass(frozen=True, slots=True)
class Grant:
    grant_id: str
    export_id: str
    export_revision: int
    revision: int
    hash: str
    state: str
    grantee_user_id: str
    grantee_registration_id: str
    record: dict | None
    expires_at: int
    revoked_at: int | None
    revoke_reason: str | None

    def ack(self) -> dict:
        return {"export_id": self.export_id, "export_revision": self.export_revision,
                "grant_id": self.grant_id, "revision": self.revision, "hash": self.hash,
                "state": self.state}


def _execute(db: sqlite3.Connection, schema: str) -> None:
    for statement in schema.split(";"):
        if statement.strip():
            db.execute(statement)


def _signature(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return sorted((kind, name, re.sub(r"\s+", "", sql).lower()) for kind, name, sql in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"))


def _check_schema(db: sqlite3.Connection) -> bool:
    """True when empty (to bootstrap); raises on anything but the exact v1 schema."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    application = db.execute("PRAGMA application_id").fetchone()[0]
    signature = _signature(db)
    if not signature and version == 0 and application == 0:
        return True
    if version != SCHEMA_VERSION or application != APPLICATION_ID:
        raise AiSchemaError("Unknown AI control schema; refusing to open")
    expected = sqlite3.connect(":memory:")
    try:
        _execute(expected, SCHEMA)
        if signature != _signature(expected):
            raise AiSchemaError("Unrecognised AI control schema")
    finally:
        expected.close()
    return False


def _connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=5.0)
    db.row_factory = sqlite3.Row
    return db


def _invalidate(db: sqlite3.Connection, user_id: str, credential_version: int, now_ms: int,
                reason: str = "credential_changed") -> list[str]:
    """Tombstone every registration of *user_id* older than *credential_version*.

    Cascades to grants held by those registrations, grants on exports they publish,
    and the exports themselves (``deleted``: export IDs are never reused). Irreversible:
    a later reactivation or rotation never resurrects any of it (design §9).
    """
    rows = [row[0] for row in db.execute(
        "SELECT id FROM ai_registrations WHERE user_id=? AND revoked_at IS NULL"
        " AND user_credential_version < ?", (user_id, credential_version))]
    for registration_id in rows:
        _retire_registration(db, registration_id, now_ms, reason)
    return rows


def _retire_registration(db: sqlite3.Connection, registration_id: str, now_ms: int, reason: str) -> None:
    db.execute("UPDATE ai_registrations SET token_hash=NULL, revoked_at=COALESCE(revoked_at,?),"
               " revoke_reason=COALESCE(revoke_reason,?) WHERE id=?", (now_ms, reason, registration_id))
    db.execute("UPDATE ai_grants SET state='revoked', revoked_at=COALESCE(revoked_at,?),"
               " revoke_reason=COALESCE(revoke_reason,?) WHERE grantee_registration_id=? OR export_id IN"
               " (SELECT export_id FROM ai_exports WHERE executor_registration_id=?)",
               (now_ms, reason, registration_id, registration_id))
    exports = [row[0] for row in db.execute(
        "SELECT export_id FROM ai_exports WHERE executor_registration_id=?", (registration_id,))]
    db.execute("UPDATE ai_exports SET state='deleted', updated_at=? WHERE executor_registration_id=?",
               (now_ms, registration_id))
    for export_id in exports:
        _drop_index(db, export_id)


def _drop_index(db: sqlite3.Connection, export_id: str) -> None:
    db.execute("DELETE FROM ai_resource_index WHERE export_id=?", (export_id,))
    db.execute("DELETE FROM ai_index_meta WHERE export_id=?", (export_id,))


def _now_ms() -> int:
    import time
    return time.time_ns() // 1_000_000


def invalidate_user_file(path: str | Path, user_id: str, credential_version: int) -> None:
    """Credential hook for processes without a server (CLI/admin RPC; R-A0).

    Runs inside the caller's users transaction: if this raises, no new key is issued.
    """
    db = _connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            if _check_schema(db):
                db.execute("ROLLBACK")
                return
            _invalidate(db, user_id, credential_version, _now_ms())
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    finally:
        db.close()


class AiStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = _connect(path)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if _check_schema(self._db):
                    self._db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    _execute(self._db, SCHEMA)
                    self._db.execute("INSERT INTO ai_meta VALUES ('server_instance_id', ?)", (str(uuid.uuid4()),))
                    self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            sid = self._db.execute("SELECT value FROM ai_meta WHERE key='server_instance_id'").fetchone()
            if sid is None:
                raise AiSchemaError("Missing server instance id")
            self.server_instance_id: str = p.check_uuid(sid[0])
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            nested = self._db.in_transaction
            savepoint = "sp_" + uuid.uuid4().hex
            self._db.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN" if read_only else "BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                if nested:
                    self._db.execute(f"ROLLBACK TO {savepoint}")
                    self._db.execute(f"RELEASE {savepoint}")
                else:
                    self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute(f"RELEASE {savepoint}" if nested else "COMMIT")

    # ------------------------------------------------------------- registrations
    @staticmethod
    def _registration(row: sqlite3.Row) -> Registration:
        return Registration(**{key: row[key] for key in Registration.__dataclass_fields__})

    def registration(self, registration_id: str) -> Registration | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM ai_registrations WHERE id=?", (registration_id,)).fetchone()
        return self._registration(row) if row else None

    def registration_by_client(self, user_id: str, client_id: str) -> Registration | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM ai_registrations WHERE user_id=? AND client_id=?"
                                   " AND revoked_at IS NULL", (user_id, client_id)).fetchone()
        return self._registration(row) if row else None

    def register(self, user_id: str, credential_version: int, client_id: str, display_name: str,
                 kind: str, token_hash: str, now_ms: int) -> Registration:
        p.check_uuid4(client_id)
        p.check_label(display_name)
        p.check_hash(token_hash)
        if kind not in p.REGISTRATION_KINDS:
            raise p.AcpError("unknown kind")
        with self._tx() as db:
            # Registrations from an older key generation are dead: tombstone them first.
            _invalidate(db, user_id, credential_version, now_ms)
            existing = db.execute("SELECT id FROM ai_registrations WHERE user_id=? AND client_id=?"
                                  " AND revoked_at IS NULL", (user_id, client_id)).fetchone()
            if existing:
                raise RegistrationExists(existing[0])
            count = db.execute("SELECT COUNT(*) FROM ai_registrations WHERE user_id=? AND revoked_at IS NULL",
                               (user_id,)).fetchone()[0]
            if count >= p.MAX_REGISTRATIONS_PER_USER:
                raise CapacityExceeded("too many registrations")
            registration_id = str(uuid.uuid4())
            db.execute("INSERT INTO ai_registrations (id,user_id,client_id,display_name,kind,token_hash,"
                       "user_credential_version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (registration_id, user_id, client_id, display_name, kind, token_hash,
                        credential_version, now_ms))
            return self.registration(registration_id)  # type: ignore[return-value]

    def list_registrations(self) -> list[Registration]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM ai_registrations WHERE revoked_at IS NULL"
                                    " ORDER BY user_id, created_at, id").fetchall()
        return [self._registration(row) for row in rows]

    def revoke_registration(self, user_id: str, registration_id: str, now_ms: int,
                            reason: str = "deleted_by_owner") -> Registration:
        with self._tx() as db:
            row = self.registration(registration_id)
            if row is None or row.user_id != user_id:
                raise NotFound("registration not found")
            if row.revoked_at is None:
                _retire_registration(db, registration_id, now_ms, reason)
            return self.registration(registration_id)  # type: ignore[return-value]

    def invalidate_user(self, user_id: str, credential_version: int, now_ms: int | None = None) -> list[str]:
        """In-process credential hook (R-A0): see :func:`_invalidate`."""
        with self._tx() as db:
            return _invalidate(db, user_id, credential_version, _now_ms() if now_ms is None else now_ms)

    # ------------------------------------------------------------- exports
    @staticmethod
    def _export(row: sqlite3.Row) -> Export:
        return Export(**{key: row[key] for key in Export.__dataclass_fields__})

    def export(self, export_id: str) -> Export | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM ai_exports WHERE export_id=?", (export_id,)).fetchone()
        return self._export(row) if row else None

    def exports_of(self, registration_id: str) -> list[Export]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM ai_exports WHERE executor_registration_id=?"
                                    " ORDER BY export_id", (registration_id,)).fetchall()
        return [self._export(row) for row in rows]

    def publish(self, executor_registration_id: str, body: dict, now_ms: int) -> Export:
        """``export.publish``: create (0→1) or exact CAS update (n→n+1)."""
        p.fields(body, {"export_id", "persistent_id", "storage_epoch", "revision", "expected_revision",
                        "policy_digest", "display_name", "hash"})
        p.check_uuid4(body["export_id"])
        p.check_persistent_id(body["persistent_id"])
        p.check_opaque(body["storage_epoch"])
        revision = p.check_int(body["revision"], 1)
        expected = p.check_int(body["expected_revision"], 0)
        p.check_hash(body["policy_digest"])
        p.check_label(body["display_name"])
        digest = p.check_hash(body["hash"])
        if digest != p.semantic_hash(p.export_record(body)):
            raise p.AcpError("export hash mismatch")
        with self._tx() as db:
            current = self.export(body["export_id"])
            if current is not None and current.executor_registration_id != executor_registration_id:
                raise NotFound("export not found")  # foreign namespace: no existence oracle
            if current is not None and current.state == "deleted":
                raise p.RequestRejected("revision_conflict", result=current.ack())
            if current is not None:
                if revision == current.revision:
                    if digest == current.hash and current.state == "published":
                        return current  # identical record: same ACK
                    raise p.RequestRejected("revision_conflict", result=current.ack())
                if revision < current.revision or expected != current.revision or revision != current.revision + 1:
                    raise p.RequestRejected("revision_conflict", result=current.ack())
                if (body["persistent_id"], body["storage_epoch"]) != (current.persistent_id, current.storage_epoch):
                    # A different identity is a different mapping: new export_id, new consent.
                    raise p.RequestRejected("stale_target", result=current.ack())
            elif expected != 0 or revision != 1:
                raise p.RequestRejected("revision_conflict")
            else:
                count = db.execute("SELECT COUNT(*) FROM ai_exports WHERE executor_registration_id=?"
                                   " AND state!='deleted'", (executor_registration_id,)).fetchone()[0]
                if count >= p.MAX_EXPORTS_PER_EXECUTOR:
                    raise p.RequestRejected("capacity_exceeded")
            duplicate = db.execute("SELECT 1 FROM ai_exports WHERE executor_registration_id=? AND"
                                   " persistent_id=? AND state='published' AND export_id!=?",
                                   (executor_registration_id, body["persistent_id"], body["export_id"])).fetchone()
            if duplicate:
                raise p.RequestRejected("duplicate_export")
            values = (executor_registration_id, body["persistent_id"], body["storage_epoch"], revision,
                      body["policy_digest"], body["display_name"], digest, now_ms, body["export_id"])
            if current is None:
                db.execute("INSERT INTO ai_exports (executor_registration_id,persistent_id,storage_epoch,"
                           "revision,policy_digest,display_name,hash,state,updated_at,export_id)"
                           " VALUES (?,?,?,?,?,?,?,'published',?,?)", values)
            else:
                db.execute("UPDATE ai_exports SET executor_registration_id=?,persistent_id=?,storage_epoch=?,"
                           "revision=?,policy_digest=?,display_name=?,hash=?,state='published',updated_at=?"
                           " WHERE export_id=?", values)
                # A new export revision voids every older target binding and the index.
                db.execute("UPDATE ai_grants SET state='revoked', revoked_at=COALESCE(revoked_at,?),"
                           " revoke_reason=COALESCE(revoke_reason,'export_revision') WHERE export_id=?"
                           " AND export_revision<?", (now_ms, body["export_id"], revision))
                _drop_index(db, body["export_id"])
            return self.export(body["export_id"])  # type: ignore[return-value]

    def unpublish(self, executor_registration_id: str, body: dict, now_ms: int) -> Export:
        p.fields(body, {"export_id", "expected_revision", "revision", "hash"})
        p.check_uuid(body["export_id"])
        revision = p.check_int(body["revision"], 1)
        expected = p.check_int(body["expected_revision"], 1)
        digest = p.check_hash(body["hash"])
        if digest != p.semantic_hash(p.unpublish_record(body["export_id"], revision)):
            raise p.AcpError("unpublish hash mismatch")
        with self._tx() as db:
            current = self.export(body["export_id"])
            if current is None or current.executor_registration_id != executor_registration_id:
                raise NotFound("export not found")
            if revision == current.revision and current.state == "unpublished" and digest == current.hash:
                return current
            if current.state == "deleted" or expected != current.revision or revision != current.revision + 1:
                raise p.RequestRejected("revision_conflict", result=current.ack())
            db.execute("UPDATE ai_exports SET revision=?, hash=?, state='unpublished', updated_at=?"
                       " WHERE export_id=?", (revision, digest, now_ms, body["export_id"]))
            db.execute("UPDATE ai_grants SET state='revoked', revoked_at=COALESCE(revoked_at,?),"
                       " revoke_reason=COALESCE(revoke_reason,'unpublished') WHERE export_id=?",
                       (now_ms, body["export_id"]))
            _drop_index(db, body["export_id"])
            return self.export(body["export_id"])  # type: ignore[return-value]

    # ------------------------------------------------------------- grants
    @staticmethod
    def _grant(row: sqlite3.Row) -> Grant:
        values = {key: row[key] for key in Grant.__dataclass_fields__}
        values["record"] = json.loads(row["record"]) if row["record"] else None
        return Grant(**values)

    def grant(self, grant_id: str) -> Grant | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM ai_grants WHERE grant_id=?", (grant_id,)).fetchone()
        return self._grant(row) if row else None

    def grants_for(self, registration_id: str) -> list[Grant]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM ai_grants WHERE grantee_registration_id=? AND state='active'"
                                    " ORDER BY grant_id", (registration_id,)).fetchall()
        return [self._grant(row) for row in rows]

    def _owned_export(self, executor_registration_id: str, export_id: str, export_revision: int) -> Export:
        export = self.export(export_id)
        if export is None or export.executor_registration_id != executor_registration_id:
            raise NotFound("export not found")
        if export.state != "published" or export.revision != export_revision:
            raise p.RequestRejected("stale_target", result=export.ack())
        return export

    def put_grant(self, executor_registration_id: str, body: dict, now_ms: int,
                  grantee: Registration | None) -> Grant:
        """``grant.put``. *grantee* is the live registration the caller resolved (or None)."""
        p.fields(body, {"export_id", "export_revision", "grant_id", "expected_revision", "revision",
                        "grant", "hash"})
        p.check_uuid(body["export_id"])
        export_revision = p.check_int(body["export_revision"], 1)
        p.check_uuid4(body["grant_id"])
        expected = p.check_int(body["expected_revision"], 0)
        revision = p.check_int(body["revision"], 1)
        grant = p.check_grant(body["grant"])
        digest = p.check_hash(body["hash"])
        if (grant["grant_id"], grant["revision"]) != (body["grant_id"], revision):
            raise p.AcpError("grant id/revision mismatch")
        if digest != p.semantic_hash(p.grant_record(body["export_id"], export_revision, grant)):
            raise p.AcpError("grant hash mismatch")
        with self._tx() as db:
            export = self._owned_export(executor_registration_id, body["export_id"], export_revision)
            target = grant["target"]
            if target != {"server_instance_id": self.server_instance_id,
                          "executor_registration_id": executor_registration_id,
                          "export_id": export.export_id, "persistent_id": export.persistent_id,
                          "storage_epoch": export.storage_epoch, "export_revision": export.revision}:
                raise p.RequestRejected("stale_target", result=export.ack())
            current = self.grant(body["grant_id"])
            if current is not None and current.export_id != export.export_id:
                raise NotFound("grant not found")
            if current is not None:
                if revision == current.revision and digest == current.hash and current.state == "active":
                    return current
                if (current.state == "revoked" or expected != current.revision
                        or revision != current.revision + 1
                        or grant["grantee_registration_id"] != current.grantee_registration_id
                        or grant["grantee_user_id"] != current.grantee_user_id):
                    raise p.RequestRejected("revision_conflict", result=current.ack())
            elif expected != 0 or revision != 1:
                raise p.RequestRejected("revision_conflict")
            if (grantee is None or not grantee.active or grantee.id != grant["grantee_registration_id"]
                    or grantee.user_id != grant["grantee_user_id"]):
                raise p.RequestRejected("not_available")
            if grant["expires_at"] <= now_ms:
                raise p.RequestRejected("expired")
            if current is None:
                count = db.execute("SELECT COUNT(*) FROM ai_grants WHERE export_id=? AND state='active'",
                                   (export.export_id,)).fetchone()[0]
                if count >= p.MAX_GRANTS_PER_EXPORT:
                    raise p.RequestRejected("capacity_exceeded")
            db.execute("INSERT INTO ai_grants (grant_id,export_id,export_revision,revision,hash,state,"
                       "grantee_user_id,grantee_registration_id,record,expires_at) VALUES"
                       " (?,?,?,?,?,'active',?,?,?,?) ON CONFLICT (grant_id) DO UPDATE SET"
                       " export_revision=excluded.export_revision, revision=excluded.revision,"
                       " hash=excluded.hash, record=excluded.record, expires_at=excluded.expires_at",
                       (body["grant_id"], export.export_id, export_revision, revision, digest,
                        grant["grantee_user_id"], grant["grantee_registration_id"],
                        p.encode(grant), grant["expires_at"]))
            return self.grant(body["grant_id"])  # type: ignore[return-value]

    def revoke_grant(self, executor_registration_id: str, body: dict, now_ms: int) -> Grant:
        """``grant.revoke``: a content-free tombstone; terminal for this grant_id."""
        p.fields(body, {"export_id", "export_revision", "grant_id", "expected_revision", "revision", "hash"})
        p.check_uuid(body["export_id"])
        export_revision = p.check_int(body["export_revision"], 1)
        p.check_uuid(body["grant_id"])
        expected = p.check_int(body["expected_revision"], 1)
        revision = p.check_int(body["revision"], 2)
        digest = p.check_hash(body["hash"])
        if digest != p.semantic_hash(p.revoke_record(body["export_id"], export_revision,
                                                     body["grant_id"], revision)):
            raise p.AcpError("revoke hash mismatch")
        with self._tx() as db:
            export = self.export(body["export_id"])
            if export is None or export.executor_registration_id != executor_registration_id:
                raise NotFound("export not found")
            current = self.grant(body["grant_id"])
            if current is None or current.export_id != export.export_id:
                raise NotFound("grant not found")
            if current.state == "revoked" and current.revision == revision and current.hash == digest:
                return current
            if expected != current.revision or revision != current.revision + 1:
                if current.state == "revoked":
                    return current  # already denied: narrowing never fails open
                raise p.RequestRejected("revision_conflict", result=current.ack())
            db.execute("UPDATE ai_grants SET state='revoked', revision=?, hash=?, record=NULL,"
                       " revoked_at=COALESCE(revoked_at,?), revoke_reason=COALESCE(revoke_reason,'revoked')"
                       " WHERE grant_id=?", (revision, digest, now_ms, body["grant_id"]))
            return self.grant(body["grant_id"])  # type: ignore[return-value]

    # ------------------------------------------------------------- resource index
    def replace_index(self, executor_registration_id: str, export_id: str, export_revision: int,
                      index_revision: int, rows: list[dict], digest: str) -> dict:
        with self._tx() as db:
            export = self._owned_export(executor_registration_id, export_id, export_revision)
            if len(rows) > p.MAX_INDEX_ROWS:
                raise p.RequestRejected("capacity_exceeded")
            if len({row["resource_id"] for row in rows}) != len(rows) or p.index_rows_hash(rows) != digest:
                raise p.RequestRejected("snapshot_changed")
            current = db.execute("SELECT index_revision FROM ai_index_meta WHERE export_id=?",
                                 (export_id,)).fetchone()
            if current is not None and index_revision <= current[0]:
                raise p.RequestRejected("revision_conflict")
            _drop_index(db, export_id)
            db.execute("INSERT INTO ai_index_meta VALUES (?,?,?,?,?)",
                       (export_id, export.revision, index_revision, digest, len(rows)))
            db.executemany("INSERT INTO ai_resource_index VALUES (?,?,?)",
                           [(export_id, row["resource_id"], p.encode(row)) for row in rows])
            return {"export_id": export_id, "export_revision": export.revision,
                    "index_revision": index_revision, "hash": digest, "row_count": len(rows)}

    def index_row(self, export_id: str, export_revision: int, resource_id: str) -> dict | None:
        with self._lock:
            meta = self._db.execute("SELECT export_revision FROM ai_index_meta WHERE export_id=?",
                                    (export_id,)).fetchone()
            if meta is None or meta[0] != export_revision:
                return None  # missing/stale index fails closed
            row = self._db.execute("SELECT row FROM ai_resource_index WHERE export_id=? AND resource_id=?",
                                   (export_id, resource_id)).fetchone()
        return json.loads(row[0]) if row else None

    # ------------------------------------------------------------- audit
    def audit(self, *, at: int, op: str, outcome: str, user_id: str | None = None,
              registration_id: str | None = None, export_id: str | None = None,
              command_id: str | None = None, command_hash: str | None = None) -> None:
        """IDs/op/outcome/time only — never prompts, transcripts or request bodies (§9)."""
        with self._tx() as db:
            db.execute("INSERT INTO ai_audit (at,user_id,registration_id,export_id,op,command_id,"
                       "command_hash,outcome) VALUES (?,?,?,?,?,?,?,?)",
                       (at, user_id, registration_id, export_id, op, command_id, command_hash, outcome))
            db.execute("DELETE FROM ai_audit WHERE id <= (SELECT MAX(id) FROM ai_audit) - ?", (AUDIT_LIMIT,))

    def audit_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM ai_audit ORDER BY id")]
