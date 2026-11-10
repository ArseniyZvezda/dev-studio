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
import json
import logging
import random
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
from .state import Base, SyncState

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


@dataclass(slots=True)
class _LiveWrite:
    hash: str
    sid: str


@dataclass(slots=True)
class _Session:
    """One browser editing one document through this backend."""

    sid: str
    path: str
    websocket: WebSocket
    client_id: int
    content: str = ""
    """The file's text at the revision the room was joined at — what the browser seeds an
    empty room from, so every member seeds from the same bytes."""
    attempts: int = 0


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
        self._hashes: dict[str, tuple[int, int, str]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._watcher is None:
            self._subscription = self.bus.subscribe()
            self._watcher = asyncio.create_task(self._watch_bus(), name="sync-watch")
        await self._start_runner()

    async def stop(self) -> None:
        await self._stop_runner()
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
            self._watcher = None
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

    async def reconfigure(self) -> dict[str, Any]:
        """Adopt the settings as they are now — a changed address, space, key or switch."""
        await self._stop_runner()
        await self._start_runner()
        return self.status()

    async def _start_runner(self) -> None:
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
        runner, self._runner = self._runner, None
        for task in (runner, *self._tasks):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._disconnected()
        if runner is not None:
            # A cancelled connection has no chance to say it ended; whatever starts next
            # (or nothing, at shutdown) must not inherit "connected".
            await self._set_status("offline")

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
            await self._set_status("connecting", self._message if self._state_name == "offline" else None)
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
        self._ws = None
        self._synced = False
        self._pending.clear()
        self._pending_paths.clear()
        self._live_writes.clear()
        self._settled.set()
        self._peers = []
        if self._state is not None:
            self._state.save()
        for session in list(self._sessions.values()):
            self._sessions.pop(session.sid, None)
            await self._close_browser(session, CLOSE_COLLAB_UNAVAILABLE, "offline")

    async def _send(self, message: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("Not connected to the remote")
        async with self._send_lock:
            await ws.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind in _SESSION_FRAMES:
            await self._to_browser(message)
        elif kind == "change":
            async with self._lock:
                await self._on_change(message)
        elif kind == "ack":
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
        self, path: str, remote: _Remote, *, by: dict[str, Any] | None = None, force: bool = False
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
            await self._apply(path, remote, by)
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
            await self._apply(path, remote, by)
            return
        copy = await self._keep_copy(path)
        await self._apply(path, remote, by)
        log.warning("Sync conflict on %s; the local version was kept as %s", path, copy)
        await self.bus.publish(
            EventType.SYNC_CONFLICT,
            {"path": path, "copy": copy, "by": (by or {}).get("name")},
        )

    async def _apply(self, path: str, remote: _Remote, by: dict[str, Any] | None) -> None:
        """Write a server version to disk, through the store every other writer uses."""
        state = self._state
        assert state is not None
        author = f"remote:{by['name']}" if by and by.get("name") else "remote"
        try:
            if remote.hash is None:
                if self.docs.resolve(path).is_file():
                    await self.docs.delete(path, author=author)
            else:
                await self.docs.write(path, remote.content or "", author=author)
        except (ValueError, OSError, PathOutsideProject) as exc:
            # A name this filesystem cannot hold, a locked file. The revision is recorded all
            # the same — otherwise the stale file would look like an edit and be pushed back
            # over the change it failed to receive — and the path is left alone until the
            # file itself changes.
            log.warning("Could not apply remote change to %s: %s", path, exc)
            self._skip[path] = self._read(path)[0]
        state.docs[path] = Base(remote.rev, remote.hash)

    async def _keep_copy(self, path: str) -> str:
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
                await self.docs.write(copy, text or "", exclusive=True)
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
        if state is None or not self._synced or self._ws is None:
            return
        local = self._scan()
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
            self._pending[self._next_id] = _Pending(path, digest)
            self._pending_paths.add(path)
            await self._send(frame)

    def _on_ack(self, message: dict[str, Any]) -> None:
        state = self._state
        pending = self._pending.pop(_integer(message.get("id")), None)
        if state is None or pending is None:
            return
        self._pending_paths.discard(pending.path)
        rev = _integer(message.get("rev"))
        state.docs[pending.path] = Base(rev, pending.hash)
        state.cursor = max(state.cursor, rev)
        live = self._live_writes.get(pending.path)
        if live is not None and live.hash == pending.hash:
            del self._live_writes[pending.path]
        state.save()
        self._settled.set()
        # The file may have changed again while this was in flight; the next comparison finds
        # that out against the revision just recorded.
        self._dirty.set()

    async def _on_nack(self, message: dict[str, Any]) -> None:
        """The server moved first. Its version arrives with the refusal; settle the two."""
        state = self._state
        pending = self._pending.pop(_integer(message.get("id")), None)
        if state is None or pending is None:
            return
        self._pending_paths.discard(pending.path)
        current = message.get("current")
        if isinstance(current, dict):
            deleted = bool(current.get("deleted"))
            remote = _Remote(
                rev=_integer(current.get("rev")),
                hash=None if deleted else current.get("hash"),
                content=None if deleted else current.get("content"),
            )
        else:
            remote = _Remote(rev=0, hash=None, content=None)
        state.cursor = max(state.cursor, remote.rev)
        await self._reconcile(pending.path, remote, force=True)
        state.save()
        self._settled.set()
        self._dirty.set()

    def _on_error(self, message: dict[str, Any]) -> None:
        log.warning("Remote: %s", message.get("message"))
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
            # Compared again after every answer: a refusal is settled by writing the server's
            # version, and what that leaves to push — a conflict copy — is pushed here.
            async with self._lock:
                await self._push_local()
            if path not in self._pending_paths:
                return
            remaining = deadline - loop.time()
            if remaining <= 0 or self._ws is None:
                raise TimeoutError(f"The remote did not answer for {path}")
            self._settled.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._settled.wait(), remaining)

    # ------------------------------------------------------------------ rooms (§16.4)

    def note_live_write(self, path: str, sid: str, content: str) -> None:
        """A room's scribe is about to save *content* to *path* through the docs API.

        Recorded by hash, so the push that follows is recognised as the room's own Markdown
        and nothing else is: an agent's write to the same path a moment later has another
        hash, is pushed as an ordinary write, and ends the room as it should.
        """
        session = self._sessions.get(sid)
        if session is None:
            return
        try:
            relative = self.docs.relative(self.docs.resolve(path))
        except (ValueError, PathOutsideProject):
            return
        if relative == session.path:
            self._live_writes[relative] = _LiveWrite(content_hash(content), sid)

    async def serve_collab(self, websocket: WebSocket) -> None:
        """One browser's editing session, from its first frame until either end hangs up.

        *websocket* is already accepted and authorised (``ws/routes.py``).
        """
        session: _Session | None = None
        try:
            first = await asyncio.wait_for(websocket.receive_json(), HELLO_TIMEOUT_SECONDS)
            path = self.docs.relative(self.docs.resolve(str(first["path"])))
            session = _Session(
                sid=uuid.uuid4().hex, path=path, websocket=websocket,
                client_id=int(first["client_id"]),
            )
        except (WebSocketDisconnect, RuntimeError):
            return
        except (TimeoutError, ValueError, KeyError, TypeError, PathOutsideProject):
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
            if session.sid in self._sessions:
                # The editor saves its last Markdown and then hangs up. That save has to
                # reach the server while this session is still a member of the room —
                # pushed afterwards it would be a write from outside, and end the room for
                # everyone still in it.
                with contextlib.suppress(TimeoutError, ConnectionError, ConnectionClosed, OSError):
                    await self._flush(session.path)
            if self._sessions.pop(session.sid, None) is not None:
                with contextlib.suppress(ConnectionError, ConnectionClosed, OSError):
                    await self._send({"t": "leave", "sid": session.sid})
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()

    async def _join(self, session: _Session) -> bool:
        """Ask the remote for the room, from a revision both sides agree on."""
        if not self.live:
            return False
        try:
            # Anything saved here and not yet pushed goes first: the room is seeded from the
            # server's revision, and the file has to *be* that revision.
            await self._flush(session.path)
        except (TimeoutError, ConnectionError, ConnectionClosed):
            return False
        async with self._lock:
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
                await self._close_browser(session, CLOSE_COLLAB_UNAVAILABLE, "unavailable")
            return
        if kind == "reset":
            self._sessions.pop(session.sid, None)
            self._live_writes.pop(session.path, None)
            await self._close_browser(session, CLOSE_COLLAB_RESET, str(message.get("reason") or "reset"))
            return
        if kind == "joined":
            frame["sid"] = session.sid
            frame["user"] = self._user
            # Only an empty room needs the text, and then it must be this revision's.
            frame["content"] = None if message.get("seeded") else session.content
        try:
            await session.websocket.send_json(frame)
        except (RuntimeError, WebSocketDisconnect, OSError):
            # The browser went away; its own handler reports the leave.
            pass

    async def _rejoin(self, session: _Session) -> None:
        if not await self._join(session):
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
