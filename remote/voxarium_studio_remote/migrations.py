"""Explicit, offline SQLite v1 -> v2 migration (dev spec §16.8).

The runtime only bootstraps an empty database. Inspection and dry-run use read-only
source connections; SQLite's backup API includes committed WAL data. No operation
here deploys a binary, stops a service, or claims to persist ephemeral room updates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .protocol import content_hash, check_path

SCHEMA_VERSION = 2
V1_SCHEMA = """
CREATE TABLE users (
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL,
 token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE spaces (
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
 seq INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE docs (
 space_id TEXT NOT NULL REFERENCES spaces(id), path TEXT NOT NULL,
 content TEXT, hash TEXT, rev INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
 updated_by TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (space_id, path)
);
CREATE INDEX docs_by_rev ON docs (space_id, rev);
"""
SCHEMA = """
CREATE TABLE users (
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
 color TEXT NOT NULL, token_hash TEXT UNIQUE, created_at TEXT NOT NULL,
 revoked_at TEXT, deleted_at TEXT,
 credential_version INTEGER NOT NULL DEFAULT 1 CHECK (credential_version >= 1),
 metadata_revision INTEGER NOT NULL DEFAULT 1 CHECK (metadata_revision >= 1)
);
CREATE TABLE spaces (
 id TEXT PRIMARY KEY, display_name TEXT NOT NULL, storage_epoch TEXT NOT NULL,
 metadata_revision INTEGER NOT NULL DEFAULT 1 CHECK (metadata_revision >= 1),
 seq INTEGER NOT NULL DEFAULT 0 CHECK (seq >= 0), created_at TEXT NOT NULL
);
CREATE TABLE docs (
 space_id TEXT NOT NULL REFERENCES spaces(id), path TEXT NOT NULL,
 content TEXT, hash TEXT, rev INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
 updated_by TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (space_id, path)
);
CREATE INDEX docs_by_rev ON docs (space_id, rev);
CREATE TABLE legacy_spaces (
 name TEXT PRIMARY KEY, project_id TEXT NOT NULL UNIQUE REFERENCES spaces(id)
);
"""


class SchemaError(ValueError):
    code = "unsupported_schema"


def _execute_schema(db: sqlite3.Connection, schema: str) -> None:
    # executescript commits an open transaction: deliberately do not use it here.
    for statement in schema.split(";"):
        if statement.strip():
            db.execute(statement)


def _signature(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return sorted((kind, name, re.sub(r"\s+", "", sql).lower())
                  for kind, name, sql in db.execute(
                      "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL"
                      " AND name NOT LIKE 'sqlite_%'")
                  if sql)


def schema_version(db: sqlite3.Connection) -> int:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    signature = _signature(db)
    if not signature and version == 0:
        return 0
    expected = sqlite3.connect(":memory:")
    try:
        if version in (0, 1):
            _execute_schema(expected, V1_SCHEMA)
            result = 1
        elif version == SCHEMA_VERSION:
            _execute_schema(expected, SCHEMA)
            result = SCHEMA_VERSION
        else:
            raise SchemaError("Unknown database schema; explicit offline migration required")
        if signature != _signature(expected):
            raise SchemaError("Unrecognised database schema; manual inspection required")
        return result
    finally:
        expected.close()


def bootstrap_or_check(db: sqlite3.Connection) -> None:
    """No implicit upgrade, even for a structurally valid unversioned v1 DB."""
    db.execute("BEGIN IMMEDIATE")
    try:
        version = schema_version(db)
        if version == 0:
            _execute_schema(db, SCHEMA)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            raise SchemaError("Legacy database requires explicit offline migration")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def _valid_uuid(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _validate(db: sqlite3.Connection, version: int) -> None:
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SchemaError("Database integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise SchemaError("Database foreign key check failed")
    users = {r[0] for r in db.execute("SELECT id FROM users")}
    if not all(_valid_uuid(user) for user in users):
        raise SchemaError("Invalid stored user identity")
    for project_id, seq in db.execute("SELECT id,seq FROM spaces"):
        valid_id = (_valid_uuid(project_id) if version == 1 else
                    isinstance(project_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", project_id))
        if not valid_id or type(seq) is not int or seq < 0:
            raise SchemaError("Invalid stored project identity or sequence")
    for row in db.execute("SELECT d.*,s.seq FROM docs d JOIN spaces s ON s.id=d.space_id"):
        _, path, content, digest, rev, deleted, author, stamp, seq = row
        try:
            valid_path = check_path(path) == path
        except ValueError:
            valid_path = False
        if (not valid_path or type(rev) is not int or not 1 <= rev <= seq
                or deleted not in (0, 1) or (author is not None and author not in users)
                or not isinstance(stamp, str)
                or (deleted == 1 and (content is not None or digest is not None))
                or (deleted == 0 and (not isinstance(content, str) or digest != content_hash(content)))):
            raise SchemaError("Invalid document identity, revision, content or author")
    if db.execute("SELECT 1 FROM docs GROUP BY space_id,rev HAVING COUNT(*)>1").fetchone():
        raise SchemaError("Duplicate document revisions")
    for (digest,) in db.execute("SELECT token_hash FROM users"):
        if digest is None and version == 2:
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SchemaError("Invalid stored credential hash")
    if version == 2:
        if any(not _valid_uuid(row[0]) for row in db.execute("SELECT storage_epoch FROM spaces")):
            raise SchemaError("Invalid storage epoch")
        for credential, revision in db.execute("SELECT credential_version,metadata_revision FROM users"):
            if type(credential) is not int or credential < 1 or type(revision) is not int or revision < 1:
                raise SchemaError("Invalid user revision")
        for (revision,) in db.execute("SELECT metadata_revision FROM spaces"):
            if type(revision) is not int or revision < 1:
                raise SchemaError("Invalid project metadata revision")


def _preserved(db: sqlite3.Connection, version: int) -> tuple:
    """Private comparison includes hashes; never exposed in an inspection report."""
    project_label = "name" if version == 1 else "display_name"
    return (
        tuple(db.execute(f"SELECT id,{project_label},seq,created_at FROM spaces ORDER BY id")),
        tuple(db.execute("SELECT * FROM docs ORDER BY space_id,path")),
        tuple(db.execute("SELECT id,name,color,token_hash,created_at,revoked_at FROM users ORDER BY id")),
    )


def _upgrade(db: sqlite3.Connection) -> None:
    before = _preserved(db, 1)
    for table in ("docs", "spaces", "users"):
        db.execute(f"ALTER TABLE {table} RENAME TO old_{table}")
    db.execute("DROP INDEX docs_by_rev")
    _execute_schema(db, SCHEMA)
    db.execute("INSERT INTO users (id,name,display_name,color,token_hash,created_at,revoked_at)"
               " SELECT id,name,name,color,token_hash,created_at,revoked_at FROM old_users")
    db.execute("INSERT INTO spaces (id,display_name,storage_epoch,seq,created_at)"
               " SELECT id,name,id,seq,created_at FROM old_spaces")
    db.execute("INSERT INTO legacy_spaces SELECT name,id FROM old_spaces")
    db.execute("INSERT INTO docs SELECT * FROM old_docs")
    for table in ("docs", "spaces", "users"):
        db.execute(f"DROP TABLE old_{table}")
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    _validate(db, 2)
    if _preserved(db, 2) != before:
        raise SchemaError("Migration preservation verification failed")


def _report(db: sqlite3.Connection, version: int) -> dict:
    label = "name" if version == 1 else "display_name"
    spaces = []
    for project_id, name, seq in db.execute(f"SELECT id,{label},seq FROM spaces ORDER BY id"):
        docs = list(db.execute("SELECT path,content,hash,rev,deleted,updated_by,updated_at"
                               " FROM docs WHERE space_id=? ORDER BY path", (project_id,)))
        checksum = hashlib.sha256(json.dumps(docs, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        epoch = project_id if version == 1 else db.execute(
            "SELECT storage_epoch FROM spaces WHERE id=?", (project_id,)).fetchone()[0]
        legacy = name if version == 1 else db.execute(
            "SELECT name FROM legacy_spaces WHERE project_id=?", (project_id,)).fetchone()
        spaces.append({"id": project_id, "display_name": name, "storage_epoch": epoch,
                       "legacy_space_name": legacy if version == 1 else (legacy[0] if legacy else None),
                       "seq": seq, "active_documents": sum(not row[4] for row in docs),
                       "tombstones": sum(bool(row[4]) for row in docs), "documents_checksum": checksum})
    return {"schema_version": version, "target_schema_version": SCHEMA_VERSION,
            "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "projects": spaces}


def _readonly(path: str | Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)


def inspect_database(path: str | Path) -> dict:
    """Read-only report with no personal keys or credential hashes."""
    db = _readonly(path)
    try:
        db.execute("BEGIN")
        version = schema_version(db)
        if version == 0:
            return {"schema_version": 0, "target_schema_version": SCHEMA_VERSION, "users": 0, "projects": []}
        _validate(db, version)
        return _report(db, version)
    finally:
        db.close()


def migrate_database(path: str | Path, *, backup_path: str | Path | None = None,
                     dry_run: bool = False, confirm_offline: bool = False) -> dict:
    """Dry-run a consistent in-memory copy; apply only with an offline assertion + backup.

    Offline is an operator assertion (all server binaries stopped), not something
    SQLite can prove. The immediate transaction also prevents concurrent DB writers.
    Backups contain credential hashes and must be protected like the data directory.
    """
    if not dry_run and confirm_offline is not True:
        raise SchemaError("Explicit offline confirmation is required")
    source = _readonly(path)
    copy = sqlite3.connect(":memory:", isolation_level=None)
    try:
        source.backup(copy)
    finally:
        source.close()
    try:
        version = schema_version(copy)
        if version == 0:
            raise SchemaError("Migration requires an existing recognised database")
        _validate(copy, version)
        if version == SCHEMA_VERSION:
            return {**_report(copy, version), "changed": False, "dry_run": dry_run}
        copy.execute("BEGIN IMMEDIATE")
        try:
            _upgrade(copy)
            report = _report(copy, 2)
            copy.execute("COMMIT")
        except BaseException:
            copy.execute("ROLLBACK")
            raise
        if dry_run:
            return {**report, "changed": True, "dry_run": True}
    finally:
        copy.close()
    if backup_path is None:
        raise SchemaError("A new backup path is required")
    destination = Path(backup_path).resolve()
    if destination == Path(path).resolve():
        raise SchemaError("Backup must not replace the source database")
    # Exclusive create: never overwrite an earlier recovery point.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    db = sqlite3.connect(str(path), isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        version = schema_version(db)
        if version != 1:
            raise SchemaError("Database changed after migration inspection")
        _validate(db, 1)
        # A second connection can back up the committed pre-migration state while
        # this connection's writer lock prevents changes (including WAL writers).
        reader = _readonly(path)
        backup = sqlite3.connect(str(destination))
        try:
            reader.backup(backup)
            if schema_version(backup) != 1:
                raise SchemaError("Backup schema verification failed")
            _validate(backup, 1)
            if _preserved(backup, 1) != _preserved(db, 1):
                raise SchemaError("Backup preservation verification failed")
        finally:
            reader.close()
            backup.close()
        _upgrade(db)
        report = _report(db, 2)
        db.execute("COMMIT")
        _validate(db, 2)
        return {**report, "changed": True, "dry_run": False}
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


# Short aliases for embedding applications; neither implicitly upgrades a Store.
inspect = inspect_database
migrate = migrate_database
