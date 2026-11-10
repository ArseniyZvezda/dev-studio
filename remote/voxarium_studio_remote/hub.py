"""Spaces, connections and live rooms — everything that happens between two frames (dev spec §16).

Two layers share one connection per local Studio, and keeping them apart is the design:

- **Documents** (§16.3). The latest Markdown of every document in the space, with a revision.
  A Studio pushes what changed on its disk and is told what changed on everybody else's. This
  is how a document reaches a machine that does not have it open, and it is the only thing
  that is ever stored.
- **Rooms** (§16.4). While at least one browser has a document open, the people editing it
  share a Yjs document here, which is what makes edits and cursors arrive as they are typed.
  A room is a session, not a record: it is seeded from the Markdown when the first editor
  arrives, one member — the *scribe* — writes the Markdown back as the room changes, and the
  Yjs document is dropped when the last editor leaves.

**No handler awaits.** Outgoing frames go into each connection's queue, the store is
synchronous, and so a frame is handled start to finish before the next one is looked at.
That is what makes "check the revision, then write" a compare-and-set without a lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from pycrdt import Doc

from .auth import User
from .protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    check_content,
    check_path,
    content_hash,
    decode_update,
    encode_update,
)
from .store import DocRow, SpaceRow, Store

log = logging.getLogger(__name__)

# A connection this far behind is not coming back to life by waiting; it is closed and the
# Studio behind it reconnects and resynchronises from its cursor.
MAX_QUEUED_FRAMES = 5_000


class Connection:
    """One local Studio, attached to one space."""

    _ids = 0

    def __init__(self, user: User, space: "SpaceHub") -> None:
        Connection._ids += 1
        self.id = Connection._ids
        self.user = user
        self.space = space
        self.sessions: dict[str, RoomSession] = {}
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.overloaded = False

    def send(self, message: dict[str, Any]) -> None:
        if self.overloaded:
            return
        if self.queue.qsize() >= MAX_QUEUED_FRAMES:
            log.warning("Connection %s (%s) fell too far behind; closing it", self.id, self.user.name)
            self.overloaded = True
            self.queue.put_nowait(None)
            return
        self.queue.put_nowait(json.dumps(message, ensure_ascii=False, separators=(",", ":")))


@dataclass(slots=True)
class RoomSession:
    """One browser editing one document, reached through its Studio's connection."""

    sid: str
    connection: Connection
    room: "Room"
    client_id: int
    """The Yjs client id the browser's cursor is published under, so peers can drop it."""
    synced: bool = False
    """Whether this browser holds the room's document yet. Until it does it is sent no
    updates: it could not apply them, and it must not seed over one that already exists."""

    def send(self, message: dict[str, Any]) -> None:
        self.connection.send({**message, "sid": self.sid})


@dataclass(slots=True)
class Room:
    path: str
    epoch: str
    """Names this incarnation of the room. A browser holding a Yjs document from another one
    must not merge the two — they were seeded separately and would duplicate every block."""
    doc: Doc = field(default_factory=Doc)
    seeded: bool = False
    sessions: list[RoomSession] = field(default_factory=list)
    """In joining order, which is also the order the scribe's job is inherited in."""
    scribe: RoomSession | None = None
    dirty: bool = False
    """Edits have been made that no member has written to the file yet. Told to whoever
    joins, because the member who would have written them may leave first — and a
    newcomer handed the scribe's job has no other way to know the file is behind."""


class SpaceHub:
    def __init__(self, row: SpaceRow) -> None:
        self.id = row.id
        self.name = row.name
        self.connections: list[Connection] = []
        self.rooms: dict[str, Room] = {}


class Hub:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._spaces: dict[str, SpaceHub] = {}
        self._handlers: dict[str, Callable[[Connection, dict[str, Any]], None]] = {
            "sync": self._on_sync,
            "put": self._on_put,
            "delete": self._on_delete,
            "join": self._on_join,
            "seed": self._on_seed,
            "update": self._on_update,
            "awareness": self._on_awareness,
            "leave": self._on_leave,
        }

    # ------------------------------------------------------------------ connections

    def connect(self, user: User, space_name: str) -> Connection:
        space = self._spaces.get(space_name)
        if space is None:
            space = self._spaces[space_name] = SpaceHub(self.store.space(space_name))
        connection = Connection(user, space)
        space.connections.append(connection)
        connection.send(
            {
                "t": "hello",
                "protocol": PROTOCOL_VERSION,
                "user": user.to_json(),
                "space": {"id": space.id, "name": space.name},
                "seq": self.store.seq(space.id),
            }
        )
        self._broadcast_presence(space)
        return connection

    def disconnect(self, connection: Connection) -> None:
        space = connection.space
        for session in list(connection.sessions.values()):
            self._leave(session)
        if connection in space.connections:
            space.connections.remove(connection)
        self._broadcast_presence(space)
        if not space.connections:
            # Nothing in a space outlives its connections except what the store holds.
            self._spaces.pop(space.name, None)

    def handle(self, connection: Connection, raw: str) -> None:
        """Act on one frame. A frame that cannot be acted on is answered, never fatal."""
        try:
            message = json.loads(raw)
        except ValueError:
            connection.send({"t": "error", "message": "Frames must be JSON"})
            return
        kind = message.get("t") if isinstance(message, dict) else None
        handler = self._handlers.get(kind) if isinstance(kind, str) else None
        if handler is None:
            connection.send({"t": "error", "message": f"Unknown message {kind!r}"})
            return
        try:
            handler(connection, message)
        except ProtocolError as exc:
            reply: dict[str, Any] = {"t": "error", "message": str(exc), "of": kind}
            for key in ("id", "sid", "path"):
                if isinstance(message.get(key), (str, int)):
                    reply[key] = message[key]
            connection.send(reply)

    # ------------------------------------------------------------------ documents (§16.3)

    @staticmethod
    def _change(row: DocRow, by: User | None = None) -> dict[str, Any]:
        return {
            "t": "change",
            "path": row.path,
            "rev": row.rev,
            "hash": row.hash,
            "deleted": row.deleted,
            "content": row.content,
            "by": by.to_json() if by else None,
        }

    @staticmethod
    def _current(row: DocRow | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"rev": row.rev, "hash": row.hash, "deleted": row.deleted, "content": row.content}

    def _on_sync(self, connection: Connection, message: dict[str, Any]) -> None:
        """Everything that moved since the client's cursor, then a marker that it is caught up."""
        since = _integer(message.get("since"))
        space = connection.space
        for row in self.store.changes_since(space.id, since):
            connection.send(self._change(row))
        connection.send({"t": "synced", "seq": self.store.seq(space.id)})

    def _on_put(self, connection: Connection, message: dict[str, Any]) -> None:
        path = check_path(message.get("path"))
        content = check_content(message.get("content"))
        space = connection.space
        current = self.store.doc(space.id, path)
        room = space.rooms.get(path)
        live = room is not None and _owns(connection, room, message.get("live"))

        if live and room is not None:
            # The room's Markdown has been written. Edits that arrived after it was taken
            # are still unwritten, but the member who wrote this one has seen them and owes
            # another — and if it leaves instead, whoever inherits the job saw them too.
            room.dirty = False
        if current is not None and not current.deleted and current.hash == content_hash(content):
            # Already what the server holds — say which revision that is and move nothing.
            connection.send(_ack(message, current))
            return
        # A live write is the room's own Markdown, written by a member of it. It is taken
        # whatever revision its Studio last saw: the room has already merged everybody's
        # edits, so there is nothing for a revision check to protect (§16.4).
        if not live and _integer(message.get("base_rev")) != (current.rev if current else 0):
            connection.send(
                {"t": "nack", "id": message.get("id"), "path": path, "current": self._current(current)}
            )
            return

        row = self.store.put(space.id, path, content, connection.user.id)
        connection.send(_ack(message, row))
        self._broadcast(space, self._change(row, connection.user), skip=connection)
        if room is not None and not live:
            # Written from outside the room — an agent, a checkout, another editor. The room's
            # Yjs document no longer describes the file, so the room ends and its members
            # reopen the document from the Markdown that replaced it.
            self._reset(space, room, "replaced")

    def _on_delete(self, connection: Connection, message: dict[str, Any]) -> None:
        path = check_path(message.get("path"))
        space = connection.space
        current = self.store.doc(space.id, path)
        if current is None or current.deleted:
            connection.send(
                {
                    "t": "ack",
                    "id": message.get("id"),
                    "path": path,
                    "rev": current.rev if current else 0,
                    "hash": None,
                    "deleted": True,
                }
            )
            return
        if _integer(message.get("base_rev")) != current.rev:
            connection.send(
                {"t": "nack", "id": message.get("id"), "path": path, "current": self._current(current)}
            )
            return
        row = self.store.delete(space.id, path, connection.user.id)
        connection.send(_ack(message, row))
        self._broadcast(space, self._change(row, connection.user), skip=connection)
        room = space.rooms.get(path)
        if room is not None:
            self._reset(space, room, "deleted")

    def _broadcast(
        self, space: SpaceHub, message: dict[str, Any], *, skip: Connection | None = None
    ) -> None:
        for connection in space.connections:
            if connection is not skip:
                connection.send(message)

    # ------------------------------------------------------------------ rooms (§16.4)

    def _on_join(self, connection: Connection, message: dict[str, Any]) -> None:
        sid = _sid(message)
        path = check_path(message.get("path"))
        if sid in connection.sessions:
            raise ProtocolError(f"Session {sid!r} is already open")
        space = connection.space
        current = self.store.doc(space.id, path)
        if current is None or current.deleted:
            connection.send({"t": "join_failed", "sid": sid, "path": path, "reason": "missing"})
            return
        if _integer(message.get("base_rev")) != current.rev:
            # The Studio's copy is not the server's. Whatever it seeded the room with would
            # not be the document, so it synchronises first and asks again.
            connection.send({"t": "join_failed", "sid": sid, "path": path, "reason": "stale"})
            return

        room = space.rooms.get(path)
        if room is None:
            room = space.rooms[path] = Room(path=path, epoch=uuid.uuid4().hex)
        session = RoomSession(
            sid=sid, connection=connection, room=room, client_id=_integer(message.get("client_id"))
        )
        room.sessions.append(session)
        connection.sessions[sid] = session
        if room.scribe is None:
            room.scribe = session

        state: str | None = None
        if room.seeded:
            session.synced = True
            state = encode_update(room.doc.get_update())
            self._announce(room, session)
        session.send(
            {
                "t": "joined",
                "path": path,
                "epoch": room.epoch,
                "seeded": room.seeded,
                "state": state,
                "scribe": room.scribe is session,
                "dirty": room.dirty,
                "rev": current.rev,
            }
        )
        self._broadcast_presence(space)

    def _on_seed(self, connection: Connection, message: dict[str, Any]) -> None:
        """The first editor's offer of the document's content — taken only if nobody was first.

        Two browsers that open an unseeded room at once both build the document from the same
        Markdown, and merging both would give every block twice. So the offer is a
        compare-and-set: the loser is handed the winner's state and drops its own.
        """
        session = self._session(connection, message)
        room = session.room
        if room.seeded:
            session.synced = True
            session.send({"t": "seeded", "ok": False, "state": encode_update(room.doc.get_update())})
            self._announce(room, session)
            return
        update = decode_update(message.get("update"))
        _apply(room, update)
        room.seeded = True
        session.synced = True
        session.send({"t": "seeded", "ok": True, "state": None})

    def _on_update(self, connection: Connection, message: dict[str, Any]) -> None:
        session = self._session(connection, message)
        if not session.synced:
            return
        update = decode_update(message.get("update"))
        room = session.room
        _apply(room, update)
        room.dirty = True
        encoded = encode_update(update)
        for other in room.sessions:
            if other is not session and other.synced:
                other.send({"t": "update", "update": encoded})

    def _on_awareness(self, connection: Connection, message: dict[str, Any]) -> None:
        """Cursors and names. Relayed as it came — the server has no use for what is inside."""
        session = self._session(connection, message)
        if not session.synced:
            return
        encoded = encode_update(decode_update(message.get("update")))
        for other in session.room.sessions:
            if other is not session and other.synced:
                other.send({"t": "awareness", "update": encoded})

    def _on_leave(self, connection: Connection, message: dict[str, Any]) -> None:
        session = connection.sessions.get(_sid(message))
        if session is not None:
            self._leave(session)
            self._broadcast_presence(connection.space)

    def _session(self, connection: Connection, message: dict[str, Any]) -> RoomSession:
        session = connection.sessions.get(_sid(message))
        if session is None:
            raise ProtocolError("No such session")
        return session

    def _announce(self, room: Room, newcomer: RoomSession) -> None:
        """Ask everyone already in the room to publish their cursor again, for the newcomer.

        The server keeps no copy of anybody's awareness state, so the people who have one are
        the ones who send it.
        """
        for other in room.sessions:
            if other is not newcomer and other.synced:
                other.send({"t": "peer_joined"})

    def _leave(self, session: RoomSession) -> None:
        room = session.room
        space = session.connection.space
        if session in room.sessions:
            room.sessions.remove(session)
        session.connection.sessions.pop(session.sid, None)
        for other in room.sessions:
            if other.synced:
                other.send({"t": "peer_left", "client_id": session.client_id})
        if not room.sessions:
            # The last editor left: the Markdown they materialised is the document now.
            if space.rooms.get(room.path) is room:
                del space.rooms[room.path]
            return
        if room.scribe is session:
            # Somebody has to keep writing the file. Prefer a member who already holds the
            # document — one still waiting to be seeded has nothing to write yet.
            room.scribe = next((s for s in room.sessions if s.synced), room.sessions[0])
            room.scribe.send({"t": "scribe"})

    def _reset(self, space: SpaceHub, room: Room, reason: str) -> None:
        for session in room.sessions:
            session.connection.sessions.pop(session.sid, None)
            session.send({"t": "reset", "path": room.path, "reason": reason})
        room.sessions.clear()
        if space.rooms.get(room.path) is room:
            del space.rooms[room.path]
        self._broadcast_presence(space)

    # ------------------------------------------------------------------ presence

    def presence(self, space: SpaceHub) -> list[dict[str, Any]]:
        """Who is connected to the space, and which documents each of them has open."""
        people: dict[str, dict[str, Any]] = {}
        for connection in space.connections:
            entry = people.setdefault(
                connection.user.id,
                {"user": connection.user.to_json(), "connections": 0, "docs": set()},
            )
            entry["connections"] += 1
            entry["docs"].update(session.room.path for session in connection.sessions.values())
        return [
            {**entry, "docs": sorted(entry["docs"])}
            for entry in sorted(people.values(), key=lambda item: item["user"]["name"].lower())
        ]

    def _broadcast_presence(self, space: SpaceHub) -> None:
        if space.connections:
            self._broadcast(space, {"t": "presence", "peers": self.presence(space)})


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _sid(message: dict[str, Any]) -> str:
    sid = message.get("sid")
    if not isinstance(sid, str) or not sid or len(sid) > 64:
        raise ProtocolError("A session id is required")
    return sid


def _owns(connection: Connection, room: Room, sid: object) -> bool:
    """Whether *sid* is this connection's own session in *room*."""
    if not isinstance(sid, str):
        return False
    session = connection.sessions.get(sid)
    return session is not None and session.room is room


def _ack(message: dict[str, Any], row: DocRow) -> dict[str, Any]:
    return {
        "t": "ack",
        "id": message.get("id"),
        "path": row.path,
        "rev": row.rev,
        "hash": row.hash,
        "deleted": row.deleted,
    }


def _apply(room: Room, update: bytes) -> None:
    try:
        room.doc.apply_update(update)
    except Exception as exc:  # pycrdt raises ValueError; anything else is as unusable
        raise ProtocolError(f"Update could not be applied: {exc}") from exc
