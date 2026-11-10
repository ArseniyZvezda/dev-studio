"""What the remote keeps: users, spaces, and the latest text of every document (dev spec §16.2).

SQLite, one file, opened once. Every call is a handful of indexed statements against a local
file, so they run on the event loop directly — the hub relies on that: a handler that never
awaits is atomic, and the order frames arrive in is the order they are applied in.

**Markdown is the only stored form of a document.** The Yjs document of a live room is never
written here (dev spec §16.4): it exists while somebody has the document open, and what
outlives it is the Markdown the room's scribe materialised.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .protocol import content_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    color       TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);
CREATE TABLE IF NOT EXISTS spaces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    seq         INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS docs (
    space_id    TEXT NOT NULL REFERENCES spaces(id),
    path        TEXT NOT NULL,
    content     TEXT,
    hash        TEXT,
    rev         INTEGER NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
    updated_by  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (space_id, path)
);
CREATE INDEX IF NOT EXISTS docs_by_rev ON docs (space_id, rev);
"""

# Cursor colours handed out in turn. Fixed here rather than chosen by the client so two
# people cannot end up the same colour by accident; `user add --color` overrides one.
USER_COLORS = (
    "#e5484d", "#3e63dd", "#30a46c", "#f76b15", "#8e4ec6",
    "#0090ff", "#d6409f", "#12a594", "#ffb224", "#6e56cf",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class UserRow:
    id: str
    name: str
    color: str
    created_at: str
    revoked_at: str | None

    def to_json(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "color": self.color}


@dataclass(frozen=True, slots=True)
class SpaceRow:
    id: str
    name: str
    seq: int


@dataclass(frozen=True, slots=True)
class DocRow:
    path: str
    content: str | None
    hash: str | None
    rev: int
    deleted: bool
    updated_by: str | None
    updated_at: str


class UserExists(ValueError):
    pass


class UnknownUser(LookupError):
    pass


class Store:
    def __init__(self, path: str | Path) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False`: uvicorn's loop and the test client's portal are not the
        # thread that built the app. Access is still one statement at a time.
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    # ------------------------------------------------------------------ users

    @staticmethod
    def _user(row: sqlite3.Row) -> UserRow:
        return UserRow(row["id"], row["name"], row["color"], row["created_at"], row["revoked_at"])

    def add_user(self, name: str, token_hash: str, *, color: str | None = None) -> UserRow:
        name = name.strip()
        if not name:
            raise ValueError("A user name is required")
        with self._tx() as db:
            if db.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
                raise UserExists(f"User {name!r} already exists")
            count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            user = UserRow(
                id=uuid.uuid4().hex,
                name=name,
                color=color or USER_COLORS[count % len(USER_COLORS)],
                created_at=_now(),
                revoked_at=None,
            )
            db.execute(
                "INSERT INTO users (id, name, color, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user.id, user.name, user.color, token_hash, user.created_at),
            )
        return user

    def list_users(self) -> list[UserRow]:
        rows = self._db.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [self._user(row) for row in rows]

    def user_by_token_hash(self, token_hash: str) -> UserRow | None:
        row = self._db.execute(
            "SELECT * FROM users WHERE token_hash = ? AND revoked_at IS NULL", (token_hash,)
        ).fetchone()
        return self._user(row) if row else None

    def set_token_hash(self, name: str, token_hash: str) -> UserRow:
        """Replace a user's key — which also brings a revoked user back."""
        with self._tx() as db:
            changed = db.execute(
                "UPDATE users SET token_hash = ?, revoked_at = NULL WHERE name = ?",
                (token_hash, name),
            ).rowcount
            if not changed:
                raise UnknownUser(f"No user {name!r}")
            row = db.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
        return self._user(row)

    def revoke_user(self, name: str) -> UserRow:
        with self._tx() as db:
            changed = db.execute(
                "UPDATE users SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                (_now(), name),
            ).rowcount
            row = db.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise UnknownUser(f"No user {name!r}")
        if not changed:
            raise UnknownUser(f"User {name!r} is already revoked")
        return self._user(row)

    def is_active(self, user_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM users WHERE id = ? AND revoked_at IS NULL", (user_id,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ spaces

    def space(self, name: str) -> SpaceRow:
        """The space called *name*, created on first use (dev spec §16.2)."""
        with self._tx() as db:
            row = db.execute("SELECT * FROM spaces WHERE name = ?", (name,)).fetchone()
            if row is None:
                space_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO spaces (id, name, seq, created_at) VALUES (?, ?, 0, ?)",
                    (space_id, name, _now()),
                )
                return SpaceRow(space_id, name, 0)
        return SpaceRow(row["id"], row["name"], row["seq"])

    def list_spaces(self) -> list[tuple[SpaceRow, int]]:
        rows = self._db.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM docs d WHERE d.space_id = s.id AND d.deleted = 0)"
            " AS documents FROM spaces s ORDER BY s.name"
        ).fetchall()
        return [(SpaceRow(row["id"], row["name"], row["seq"]), row["documents"]) for row in rows]

    def seq(self, space_id: str) -> int:
        row = self._db.execute("SELECT seq FROM spaces WHERE id = ?", (space_id,)).fetchone()
        return int(row["seq"]) if row else 0

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
        row = self._db.execute(
            "SELECT * FROM docs WHERE space_id = ? AND path = ?", (space_id, path)
        ).fetchone()
        return self._doc(row) if row else None

    def changes_since(self, space_id: str, since: int) -> list[DocRow]:
        """Every document that moved after *since*, oldest first — tombstones included.

        A revision is the space's own counter at the moment of the write, so "everything
        after N" is one indexed range and a client's whole resume state is one integer.
        """
        rows = self._db.execute(
            "SELECT * FROM docs WHERE space_id = ? AND rev > ? ORDER BY rev", (space_id, since)
        ).fetchall()
        return [self._doc(row) for row in rows]

    def _write(
        self, space_id: str, path: str, content: str | None, user_id: str | None
    ) -> DocRow:
        deleted = content is None
        digest = None if content is None else content_hash(content)
        stamp = _now()
        with self._tx() as db:
            db.execute("UPDATE spaces SET seq = seq + 1 WHERE id = ?", (space_id,))
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
