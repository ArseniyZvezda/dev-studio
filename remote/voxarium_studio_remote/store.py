"""Remote v2 catalog and hash-only user storage (dev spec §16.7–16.9).

All handlers are synchronous. Markdown is the only persisted document form;
Yjs rooms remain ephemeral (§16.4). Startup never migrates an existing database.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import protocol
from .protocol import content_hash
from .migrations import SCHEMA, SCHEMA_VERSION, SchemaError, bootstrap_or_check

USER_COLORS = (
    "#e5484d", "#3e63dd", "#30a46c", "#f76b15", "#8e4ec6",
    "#0090ff", "#d6409f", "#12a594", "#ffb224", "#6e56cf",
)
SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024
# AI control registry beside remote.db (ai-access design §3, R-A0). Kept as a separate
# file so the strict Documents schema signature never changes when ACP is off.
AI_CONTROL_DB = "ai-control.db"

CredentialHook = Callable[[str, int], None]
"""``(user_id, new_credential_version)`` — called inside the credential transaction."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise InvalidRequest("A positive revision is required")
    return value


def _user_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise InvalidRequest("A canonical user UUID is required")
    return value


def _token_hash(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise InvalidRequest("A SHA-256 credential digest is required")
    return value


class InvalidRequest(ValueError):
    code = "invalid_request"


class UserExists(ValueError):
    code = "user_exists"


class UnknownUser(LookupError):
    code = "user_not_found"


class UserDeleted(ValueError):
    code = "user_deleted"


class ProjectExists(ValueError):
    code = "project_exists"


class UnknownProject(LookupError):
    code = "project_not_found"


class UnknownLegacySpace(LookupError):
    code = "legacy_space_not_found"


class MetadataConflict(ValueError):
    code = "metadata_conflict"


class CredentialConflict(MetadataConflict):
    code = "credential_conflict"


class SnapshotChanged(ValueError):
    code = "snapshot_changed"


class SnapshotTooLarge(InvalidRequest):
    code = "payload_too_large"


class CredentialInvalid(PermissionError):
    code = "unauthorised"


@dataclass(frozen=True, slots=True)
class UserRow:
    id: str
    name: str  # Immutable legacy CLI handle, never an identity selector for v2.
    display_name: str
    color: str
    created_at: str
    revoked_at: str | None
    deleted_at: str | None
    credential_version: int
    metadata_revision: int

    @property
    def status(self) -> str:
        return "deleted" if self.deleted_at else "revoked" if self.revoked_at else "active"

    def to_json(self) -> dict:
        return {"id": self.id, "display_name": self.display_name, "color": self.color}

    def to_admin_json(self) -> dict:
        return {**self.to_json(), "created_at": self.created_at, "status": self.status,
                "revoked_at": self.revoked_at, "deleted_at": self.deleted_at,
                "credential_version": self.credential_version,
                "metadata_revision": self.metadata_revision}


@dataclass(frozen=True, slots=True)
class ProjectRow:
    id: str
    display_name: str
    storage_epoch: str
    metadata_revision: int
    created_at: str
    seq: int

    @property
    def name(self) -> str:
        """Legacy read-only presentation compatibility, not a selector."""
        return self.display_name

    def to_json(self) -> dict:
        return {"id": self.id, "display_name": self.display_name,
                "storage_epoch": self.storage_epoch, "metadata_revision": self.metadata_revision,
                "created_at": self.created_at}


SpaceRow = ProjectRow


@dataclass(frozen=True, slots=True)
class DocRow:
    path: str
    content: str | None
    hash: str | None
    rev: int
    deleted: bool
    updated_by: str | None
    updated_at: str

    def to_json(self) -> dict:
        return asdict(self)


class Store:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._path = None if str(path) == ":memory:" else Path(path)
        # R-A0: credential generation changes must invalidate AI control mirrors
        # *before* the new key is issued (pre-commit hooks; a failure rolls the
        # credential change back) and close live sockets afterwards (listeners).
        self._credential_hooks: list[CredentialHook] = []
        self._credential_listeners: list[CredentialHook] = []
        self._pending_notices: list[tuple[str, int]] = []
        self._file_hook = self._path is not None
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            # Check before changing journal mode or performing any application write.
            bootstrap_or_check(self._db)
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @property
    def path(self) -> Path | None:
        return self._path

    def add_credential_hook(self, hook: CredentialHook, *, replaces_file_hook: bool = False) -> None:
        """Run *hook* inside every rotate/revoke/reactivate/delete transaction.

        Raising aborts the credential change, so no new key is shown. With
        ``replaces_file_hook`` the in-process AI store owns ``ai-control.db`` and
        the separate-process fallback below is not used twice.
        """
        with self._lock:
            self._credential_hooks.append(hook)
            if replaces_file_hook:
                self._file_hook = False

    def add_credential_listener(self, listener: CredentialHook) -> None:
        """Called after the credential change committed (e.g. to close sockets)."""
        with self._lock:
            self._credential_listeners.append(listener)

    def _credential_changed(self, user_id: str, version: int) -> None:
        if self._file_hook and self._path is not None:
            control = self._path.with_name(AI_CONTROL_DB)
            if control.exists():
                # A CLI/admin process rotating a key reaches the registry through the
                # file, so revocation never depends on the server being the writer.
                from .ai_store import invalidate_user_file
                invalidate_user_file(control, user_id, version)
        for hook in list(self._credential_hooks):
            hook(user_id, version)
        self._pending_notices.append((user_id, version))

    def _flush_notices(self) -> None:
        notices, self._pending_notices = self._pending_notices, []
        for user_id, version in notices:
            for listener in list(self._credential_listeners):
                try:
                    listener(user_id, version)
                except Exception:  # noqa: BLE001 - a listener must not undo a commit
                    pass

    @contextmanager
    def _tx(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        # A frame's credential guard and nested document write share the transaction.
        # RLock prevents a different thread from borrowing this connection's guard.
        with self._lock:
            nested = self._db.in_transaction
            savepoint = "sp_" + uuid.uuid4().hex
            self._db.execute(f"SAVEPOINT {savepoint}" if nested else
                             "BEGIN" if read_only else "BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                if nested:
                    self._db.execute(f"ROLLBACK TO {savepoint}")
                    self._db.execute(f"RELEASE {savepoint}")
                else:
                    self._db.execute("ROLLBACK")
                    self._pending_notices.clear()
                raise
            else:
                self._db.execute(f"RELEASE {savepoint}" if nested else "COMMIT")
                if not nested:
                    self._flush_notices()

    @staticmethod
    def _user(row: sqlite3.Row) -> UserRow:
        return UserRow(**{key: row[key] for key in UserRow.__dataclass_fields__})

    def user(self, user_id: str) -> UserRow:
        _user_id(user_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise UnknownUser("User not found")
        return self._user(row)

    def create_user(self, display_name: str, token_hash: str, *, user_id: str | None = None,
                    color: str | None = None) -> UserRow:
        return self._create_user(display_name, token_hash, user_id=user_id, color=color)

    def _create_user(self, display_name: str, token_hash: str, *, user_id: str | None = None,
                     color: str | None = None, legacy_handle: str | None = None) -> UserRow:
        display_name = protocol.check_display_name(display_name)
        user_id = uuid.uuid4().hex if user_id is None else _user_id(user_id)
        if uuid.UUID(hex=user_id).version != 4:
            raise InvalidRequest("A UUID v4 is required for a new user")
        _token_hash(token_hash)
        if color is not None:
            color = protocol.check_color(color)
        with self._tx() as db:
            count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            try:
                db.execute("INSERT INTO users (id,name,display_name,color,token_hash,created_at)"
                           " VALUES (?,?,?,?,?,?)", (user_id, legacy_handle or user_id, display_name,
                           color or USER_COLORS[count % len(USER_COLORS)], token_hash, _now()))
            except sqlite3.IntegrityError:
                raise UserExists("User identity, handle or credential already exists") from None
            return self.user(user_id)

    def add_user(self, name: str, token_hash: str, *, color: str | None = None) -> UserRow:
        """Historical CLI: keep its unique immutable handle; UUID CRUD is separate."""
        display_name = protocol.check_display_name(name)
        return self._create_user(display_name, token_hash, color=color, legacy_handle=name.strip())

    def list_users(self, *, include_deleted: bool = False) -> list[UserRow]:
        if type(include_deleted) is not bool:
            raise InvalidRequest("Invalid deleted-user filter")
        with self._lock:
            rows = self._db.execute("SELECT * FROM users" +
                                    ("" if include_deleted else " WHERE deleted_at IS NULL") +
                                    " ORDER BY created_at,id").fetchall()
        return [self._user(row) for row in rows]

    def user_by_token_hash(self, token_hash: str) -> UserRow | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE token_hash=? AND revoked_at IS NULL"
                                   " AND deleted_at IS NULL", (token_hash,)).fetchone()
        return self._user(row) if row else None

    @staticmethod
    def _expected(user: UserRow, expected_revision: int, expected_credential_version: int | None = None) -> None:
        _revision(expected_revision)
        if expected_credential_version is not None:
            _revision(expected_credential_version)
        if expected_credential_version is not None and user.credential_version != expected_credential_version:
            raise CredentialConflict("Credential changed; refresh before confirming")
        if user.metadata_revision != expected_revision:
            raise MetadataConflict("User changed; refresh before confirming")

    def update_user(self, user_id: str, *, expected_revision: int,
                    display_name: str | None = None, color: str | None = None) -> UserRow:
        if display_name is not None:
            display_name = protocol.check_display_name(display_name)
        if color is not None:
            color = protocol.check_color(color)
        if display_name is None and color is None:
            raise InvalidRequest("No user metadata changes supplied")
        with self._tx() as db:
            user = self.user(user_id)
            if user.deleted_at:
                raise UserDeleted("Deleted users cannot be edited")
            self._expected(user, expected_revision)
            label, color = display_name if display_name is not None else user.display_name, color or user.color
            if (label, color) != (user.display_name, user.color):
                db.execute("UPDATE users SET display_name=?,color=?,metadata_revision=metadata_revision+1"
                           " WHERE id=?", (label, color, user_id))
            return self.user(user_id)

    def rotate_user(self, user_id: str, token_hash: str, expected_revision: int,
                    expected_credential_version: int) -> UserRow:
        _token_hash(token_hash)
        with self._tx() as db:
            user = self.user(user_id)
            if user.deleted_at:
                raise UserDeleted("Deleted users cannot be restored")
            self._expected(user, expected_revision, expected_credential_version)
            _revision(expected_credential_version)
            try:
                db.execute("UPDATE users SET token_hash=?,revoked_at=NULL,"
                           " credential_version=credential_version+1,metadata_revision=metadata_revision+1"
                           " WHERE id=?", (token_hash, user_id))
            except sqlite3.IntegrityError:
                raise UserExists("Credential already exists") from None
            rotated = self.user(user_id)
            self._credential_changed(user_id, rotated.credential_version)
            return rotated

    def revoke_user_by_id(self, user_id: str, expected_revision: int,
                          expected_credential_version: int) -> UserRow:
        return self._retire_user(user_id, expected_revision, expected_credential_version, delete=False)

    def delete_user(self, user_id: str, expected_revision: int,
                    expected_credential_version: int) -> UserRow:
        return self._retire_user(user_id, expected_revision, expected_credential_version, delete=True)

    def _retire_user(self, user_id: str, expected_revision: int,
                     expected_credential_version: int, *, delete: bool) -> UserRow:
        _revision(expected_revision)
        _revision(expected_credential_version)
        with self._tx() as db:
            user = self.user(user_id)
            # No mutation/replay on already retired state. Stale input must still
            # not revoke a newer active generation (e.g. revoke -> rotate).
            if user.deleted_at or (not delete and user.revoked_at):
                return user
            self._expected(user, expected_revision, expected_credential_version)
            stamp = _now()
            db.execute("UPDATE users SET token_hash=NULL,revoked_at=COALESCE(revoked_at,?),"
                       " deleted_at=?,credential_version=credential_version+1,"
                       " metadata_revision=metadata_revision+1 WHERE id=?",
                       (stamp, stamp if delete else None, user_id))
            retired = self.user(user_id)
            self._credential_changed(user_id, retired.credential_version)
            return retired

    def _legacy_user(self, name: str) -> UserRow:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        if row is None:
            raise UnknownUser("Legacy user handle not found")
        return self._user(row)

    def set_token_hash(self, name: str, token_hash: str) -> UserRow:
        with self._tx():
            user = self._legacy_user(name)
            return self.rotate_user(user.id, token_hash, user.metadata_revision, user.credential_version)

    def revoke_user(self, name: str) -> UserRow:
        with self._tx():
            user = self._legacy_user(name)
            return self.revoke_user_by_id(user.id, user.metadata_revision, user.credential_version)

    def credential_valid(self, user_id: str, version: int) -> bool:
        if type(version) is not int or version < 1 or not isinstance(user_id, str):
            return False
        with self._lock:
            return self._db.execute("SELECT 1 FROM users WHERE id=? AND credential_version=?"
                                    " AND revoked_at IS NULL AND deleted_at IS NULL AND token_hash IS NOT NULL",
                                    (user_id, version)).fetchone() is not None

    @contextmanager
    def credential_guard(self, user_id: str, version: int) -> Iterator[UserRow]:
        """Guard the *whole synchronous* handler against separate CLI connections.

        Never hold this context across await, including websocket sends. Outbound
        authorization should be checked immediately before the network await.
        """
        with self._tx():
            if not self.credential_valid(user_id, version):
                raise CredentialInvalid("Credential is no longer active")
            yield self.user(user_id)

    def is_active(self, user_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM users WHERE id=? AND revoked_at IS NULL"
                                    " AND deleted_at IS NULL AND token_hash IS NOT NULL",
                                    (user_id,)).fetchone() is not None

    # ------------------------------------------------------------------ catalog
    @staticmethod
    def _project(row: sqlite3.Row) -> ProjectRow:
        return ProjectRow(**{key: row[key] for key in ProjectRow.__dataclass_fields__})

    def project(self, project_id: str) -> ProjectRow:
        project_id = protocol.check_project_id(project_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM spaces WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise UnknownProject("Project not found")
        return self._project(row)

    def space(self, project_id: str) -> ProjectRow:
        """Lookup-only exact ID. No legacy-name fallback or implicit creation."""
        return self.project(project_id)

    def create_project(self, project_id: str, display_name: str) -> ProjectRow:
        project_id = protocol.check_project_id(project_id)
        display_name = protocol.check_display_name(display_name)
        with self._tx() as db:
            try:
                db.execute("INSERT INTO spaces (id,display_name,storage_epoch,created_at) VALUES (?,?,?,?)",
                           (project_id, display_name, uuid.uuid4().hex, _now()))
            except sqlite3.IntegrityError:
                raise ProjectExists("Project identity already exists") from None
            return self.project(project_id)

    def list_projects(self, limit: int = 100, after: str | None = None) -> dict:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidRequest("Invalid catalog page limit")
        if after is not None:
            after = protocol.check_project_id(after)
        with self._lock:
            rows = self._db.execute("SELECT * FROM spaces WHERE id>? COLLATE BINARY"
                                   " ORDER BY id COLLATE BINARY LIMIT ?", (after or "", limit+1)).fetchall()
        projects = [self._project(row).to_json() for row in rows[:limit]]
        return {"protocol": 2, "projects": projects,
                "next_after": projects[-1]["id"] if len(rows)>limit else None}

    def update_project(self, project_id: str, display_name: str, expected_revision: int) -> ProjectRow:
        project_id = protocol.check_project_id(project_id)
        display_name = protocol.check_display_name(display_name)
        _revision(expected_revision)
        with self._tx() as db:
            project = self.project(project_id)
            if project.metadata_revision != expected_revision:
                raise MetadataConflict("Project changed; refresh before confirming")
            if project.display_name != display_name:
                db.execute("UPDATE spaces SET display_name=?,metadata_revision=metadata_revision+1 WHERE id=?",
                           (display_name, project_id))
            return self.project(project_id)

    def resolve_legacy(self, name: str) -> ProjectRow:
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 480:
            raise InvalidRequest("Invalid legacy selector")
        with self._lock:
            row = self._db.execute("SELECT project_id FROM legacy_spaces WHERE name=?", (name,)).fetchone()
        if row is None:
            raise UnknownLegacySpace("Legacy space not found")
        return self.project(row[0])

    def list_spaces(self) -> list[tuple[ProjectRow, int]]:
        with self._lock:
            rows = self._db.execute("SELECT s.*, (SELECT COUNT(*) FROM docs d WHERE d.space_id=s.id"
                                    " AND d.deleted=0) AS documents FROM spaces s ORDER BY s.id").fetchall()
        return [(self._project(row), row["documents"]) for row in rows]

    def seq(self, space_id: str) -> int:
        return self.project(space_id).seq

    def snapshot(self, project_id: str, limit: int = 20, after: str | None = None,
                 expected_seq: int | None = None, expected_epoch: str | None = None) -> dict:
        project_id = protocol.check_project_id(project_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidRequest("Invalid snapshot page limit")
        if expected_seq is not None and (type(expected_seq) is not int or expected_seq < 0):
            raise InvalidRequest("Invalid snapshot sequence")
        if expected_epoch is not None and (not isinstance(expected_epoch, str) or
                re.fullmatch(r"[0-9a-f]{32}", expected_epoch) is None):
            raise InvalidRequest("Invalid snapshot epoch")
        if after is not None:
            try:
                valid = protocol.check_path(after) == after
            except ValueError:
                valid = False
            if not valid or expected_seq is None or expected_epoch is None:
                raise InvalidRequest("Snapshot continuation requires cursor, sequence and epoch")
        with self._tx(read_only=True) as db:
            project = self.project(project_id)
            if ((expected_seq is not None and project.seq != expected_seq) or
                    (expected_epoch is not None and project.storage_epoch != expected_epoch)):
                raise SnapshotChanged("Project changed during snapshot")
            result = {"protocol": 2, "snapshot": {"id": project.id,
                      "storage_epoch": project.storage_epoch, "seq": project.seq},
                      "documents": [], "next_after": None}
            rows = db.execute("SELECT * FROM docs WHERE space_id=? AND path>? COLLATE BINARY"
                              " ORDER BY path COLLATE BINARY LIMIT ?", (project_id, after or "", limit+1)).fetchall()
            for row in rows:
                if len(result["documents"]) == limit:
                    result["next_after"] = result["documents"][-1]["path"]
                    break
                document = self._doc(row).to_json()
                candidate = {**result, "documents": result["documents"] + [document], "next_after": document["path"]}
                # Match Starlette JSONResponse's actual compact UTF-8 encoding
                # (§16.7), not artificial ASCII escapes for otherwise valid text.
                if len(json.dumps(candidate, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8")) > SNAPSHOT_MAX_BYTES:
                    if not result["documents"]:
                        # Mandatory JSON escapes can exceed the cap even for 1 MB
                        # content. Never truncate/skip it or mutate the source.
                        raise SnapshotTooLarge("Stored document exceeds snapshot response budget")
                    result["next_after"] = result["documents"][-1]["path"]
                    break
                result["documents"].append(document)
            return result

    # ------------------------------------------------------------------ documents

    @staticmethod
    def _doc(row: sqlite3.Row) -> DocRow:
        return DocRow(
            path=row["path"],
            content=row["content"],
            hash=row["hash"],
            rev=row["rev"],
            deleted=bool(row["deleted"]),
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )

    def doc(self, space_id: str, path: str) -> DocRow | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM docs WHERE space_id = ? AND path = ?", (space_id, path)
            ).fetchone()
        return self._doc(row) if row else None

    def changes_since(self, space_id: str, since: int) -> list[DocRow]:
        """Every document that moved after *since*, oldest first — tombstones included.

        A revision is the space's own counter at the moment of the write, so "everything
        after N" is one indexed range and a client's whole resume state is one integer.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM docs WHERE space_id = ? AND rev > ? ORDER BY rev", (space_id, since)
            ).fetchall()
        return [self._doc(row) for row in rows]

    def _write(
        self, space_id: str, path: str, content: str | None, user_id: str | None
    ) -> DocRow:
        space_id = protocol.check_project_id(space_id)
        if protocol.check_path(path) != path:
            raise InvalidRequest("A canonical document path is required")
        if content is not None:
            protocol.check_content(content)
        deleted = content is None
        digest = None if content is None else content_hash(content)
        stamp = _now()
        with self._tx() as db:
            if not db.execute("UPDATE spaces SET seq = seq + 1 WHERE id = ?", (space_id,)).rowcount:
                raise UnknownProject("Project not found")
            rev = db.execute("SELECT seq FROM spaces WHERE id = ?", (space_id,)).fetchone()[0]
            db.execute(
                "INSERT INTO docs (space_id, path, content, hash, rev, deleted, updated_by, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (space_id, path) DO UPDATE SET content = excluded.content,"
                " hash = excluded.hash, rev = excluded.rev, deleted = excluded.deleted,"
                " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (space_id, path, content, digest, rev, int(deleted), user_id, stamp),
            )
        return DocRow(path, content, digest, rev, deleted, user_id, stamp)

    def put(self, space_id: str, path: str, content: str, user_id: str | None) -> DocRow:
        return self._write(space_id, path, content, user_id)

    def delete(self, space_id: str, path: str, user_id: str | None) -> DocRow:
        """Leave a tombstone: a Studio that was offline has to learn the document went."""
        return self._write(space_id, path, None, user_id)
