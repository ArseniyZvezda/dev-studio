"""Online sync, end to end: two Studios and a real remote in one event loop (§16).

Nothing here is mocked. The remote is the actual server from ``remote/``, listening on an
ephemeral loopback port; each "person" is a managed project with its own
:class:`DocStore`, bus and :class:`RemoteSync`. What is asserted is what is on each
person's disk — which is the only thing sync promises anybody.

The remote is a separate package, so this module is skipped unless it is installed into the
backend's environment (``uv pip install -e ../remote``).
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

remote = pytest.importorskip("voxarium_studio_remote.app")
pycrdt = pytest.importorskip("pycrdt")

import uvicorn  # noqa: E402
from voxarium_studio_remote.auth import hash_token, new_token  # noqa: E402
from voxarium_studio_remote.store import Store  # noqa: E402

from voxarium_studio.core.config import AppConfig, SyncConfig, load_config  # noqa: E402
from voxarium_studio.core.context import AppContext  # noqa: E402
from voxarium_studio.core.project import init_project  # noqa: E402
from voxarium_studio.core.project_scope import ProjectDescriptor  # noqa: E402
from voxarium_studio.core.security import AuthSettings  # noqa: E402
from voxarium_studio.sync import RemoteSync, credentials  # noqa: E402
from voxarium_studio.sync import service as sync_service  # noqa: E402
from voxarium_studio.sync.protocol import (  # noqa: E402
    CLOSE_COLLAB_RESET,
    CLOSE_COLLAB_UNAVAILABLE,
    is_insecure,
    normalise_url,
    socket_url,
)
from voxarium_studio.teams.docs import DocStore  # noqa: E402
from voxarium_studio.ws.bus import EventBus  # noqa: E402
from voxarium_studio.ws.events import EventType  # noqa: E402
from voxarium_studio.ws.routes import router as ws_router  # noqa: E402

# The suite must not wait out a human-scale debounce on every assertion.
sync_service.PUSH_DEBOUNCE_SECONDS = 0.01


async def eventually(check: Callable[[], Any], *, timeout: float = 5.0, what: str = "") -> Any:
    """Wait until *check* returns something truthy, and return it."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError(f"Timed out waiting for {what or check}")
        await asyncio.sleep(0.01)


@dataclass
class Server:
    store: Store
    url: str
    hub: Any
    stop: Callable[[], Awaitable[None]]

    def issue(self, name: str) -> str:
        token = new_token()
        self.store.add_user(name, hash_token(token))
        return token

    def doc(self, path: str, space: str = "game") -> Any:
        return self.store.doc(self.store.space(space).id, path)


async def start_server(store: Store, port: int = 0) -> Server:
    app = remote.create_app(store=store)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    await eventually(lambda: server.started, what="the remote to start")
    bound = server.servers[0].sockets[0].getsockname()[1]

    async def stop() -> None:
        server.should_exit = True
        await task

    return Server(store=store, url=f"http://127.0.0.1:{bound}", hub=app.state.hub, stop=stop)


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    store = Store(":memory:")
    running = await start_server(store)
    try:
        yield running
    finally:
        await running.stop()
        store.close()


@dataclass
class Studio:
    """One person's machine: a project on disk, and the sync client beside it."""

    root: Path
    docs: DocStore
    bus: EventBus
    sync: RemoteSync
    config: AppConfig
    events: list[Any] = field(default_factory=list)

    def text(self, path: str) -> str | None:
        file = self.docs.directory / path
        return file.read_text(encoding="utf-8") if file.is_file() else None

    def paths(self) -> list[str]:
        return sorted(entry.path for entry in self.docs.list_docs())

    def conflicts(self) -> list[dict[str, Any]]:
        return [e.data for e in self.bus.replay(0) if e.type == EventType.SYNC_CONFLICT]

    async def connected(self) -> None:
        await eventually(lambda: self.sync.live, what=f"{self.root.name} to connect")

    async def settled(self) -> None:
        """Nothing left to push and nothing in flight."""
        await asyncio.sleep(0.05)
        await eventually(
            lambda: not self.sync._dirty.is_set() and not self.sync._pending,
            what=f"{self.root.name} to settle",
        )


@pytest.fixture
async def studio(tmp_path: Path, server: Server) -> AsyncIterator[Callable[..., Awaitable[Studio]]]:
    made: list[Studio] = []

    async def make(
        name: str, *, token: str | None = None, start: bool = True, space: str = "game",
    ) -> Studio:
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        paths = init_project(root)
        config = AppConfig(sync=SyncConfig(enabled=True, url=server.url, space=space))
        bus = EventBus()
        docs = DocStore(paths, bus)
        key = token or server.issue(name)
        sync = RemoteSync(paths, lambda: config, docs, bus, token=lambda _url: key)
        studio = Studio(root=root, docs=docs, bus=bus, sync=sync, config=config)
        made.append(studio)
        if start:
            await sync.start()
            await studio.connected()
        return studio

    yield make
    for studio in made:
        await studio.sync.stop()


# ------------------------------------------------------------------ addresses


def test_addresses_are_folded_to_one_spelling() -> None:
    assert normalise_url("studio.example.com") == "https://studio.example.com"
    assert normalise_url(" WSS://Studio.Example.com/ ") == "https://studio.example.com"
    assert normalise_url("http://10.0.0.5:8787/remote/") == "http://10.0.0.5:8787/remote"
    assert socket_url("https://h.example/r", "game") == "wss://h.example/r/api/v1/sync?space=game"
    assert socket_url("http://127.0.0.1:8787", "game") == "ws://127.0.0.1:8787/api/v1/sync?space=game"
    assert is_insecure("http://10.0.0.5:8787") and not is_insecure("http://127.0.0.1:8787")
    assert not is_insecure("https://studio.example.com")
    for bad in ("", "ftp://host", "https://user:pw@host", "https://host/?x=1"):
        with pytest.raises(ValueError):
            normalise_url(bad)


# ------------------------------------------------------------------ documents (§16.3)


async def test_a_document_written_on_one_machine_appears_on_the_other(
    studio: Callable[..., Awaitable[Studio]], server: Server
) -> None:
    alice, bob = await studio("alice"), await studio("bob")

    await alice.docs.write("design/dash.md", "# Dash\n\nFast.\n")
    await eventually(lambda: bob.text("design/dash.md") == "# Dash\n\nFast.\n", what="bob to receive it")
    # Bob's browser hears of it the way it hears of an agent's write, with whose it was.
    event = next(e for e in bob.bus.replay(0) if e.type == EventType.TEAM_DOC_CHANGED)
    assert event.data["path"] == "design/dash.md" and event.data["author"] == "remote:alice"

    await bob.docs.write("design/dash.md", "# Dash\n\nFaster.\n")
    await eventually(lambda: alice.text("design/dash.md") == "# Dash\n\nFaster.\n", what="the edit to return")

    await alice.docs.delete("design/dash.md")
    await eventually(lambda: bob.text("design/dash.md") is None, what="the delete to arrive")
    assert server.doc("design/dash.md").deleted

    # A rename is a delete and a write, and arrives as both.
    await bob.docs.write("notes.md", "# Notes\n")
    await eventually(lambda: alice.text("notes.md") is not None)
    await alice.docs.move("notes.md", "archive/notes.md")
    await eventually(lambda: bob.paths() == ["archive/notes.md"], what="the move to arrive")
    await alice.settled()
    await bob.settled()
    assert not alice.conflicts() and not bob.conflicts()


async def test_a_file_changed_behind_the_studios_back_is_synced_too(
    studio: Callable[..., Awaitable[Studio]]
) -> None:
    alice, bob = await studio("alice"), await studio("bob")
    # A checkout, or the user's own editor: nothing goes through the DocStore. The file
    # watcher is what reports it in a running Studio; here its event is published by hand.
    file = alice.docs.directory / "outside.md"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("# Outside\n", encoding="utf-8", newline="\n")
    prefix = alice.sync._docs_prefix()
    await alice.bus.publish(
        EventType.EXPLORER_FILE_CHANGED,
        {"changes": [{"change": "added", "path": f"{prefix}outside.md"}], "root": str(alice.root)},
    )
    await eventually(lambda: bob.text("outside.md") == "# Outside\n", what="the outside edit to arrive")


async def test_first_sync_merges_two_checkouts_without_losing_either(
    studio: Callable[..., Awaitable[Studio]]
) -> None:
    alice = await studio("alice", start=False)
    bob = await studio("bob", start=False)
    for person in (alice, bob):
        (person.docs.directory).mkdir(parents=True, exist_ok=True)
        (person.docs.directory / "same.md").write_text("# Same\n", encoding="utf-8", newline="\n")
    (alice.docs.directory / "only-alice.md").write_text("a\n", encoding="utf-8", newline="\n")
    (bob.docs.directory / "only-bob.md").write_text("b\n", encoding="utf-8", newline="\n")
    (alice.docs.directory / "differs.md").write_text("alice's\n", encoding="utf-8", newline="\n")
    (bob.docs.directory / "differs.md").write_text("bob's\n", encoding="utf-8", newline="\n")

    await alice.sync.start()
    await alice.connected()
    await alice.settled()
    await bob.sync.start()
    await bob.connected()
    await bob.settled()
    await eventually(lambda: len(alice.paths()) == 5, what="alice to receive bob's files")

    # Identical files are simply agreed on; a file only one side had is on both.
    assert alice.paths() == bob.paths()
    assert alice.text("only-bob.md") == "b\n" and bob.text("only-alice.md") == "a\n"
    # The one that differed: the server's version took the path on both machines, and Bob's
    # was kept beside it — as a document, so it reached Alice as well.
    assert alice.text("differs.md") == bob.text("differs.md") == "alice's\n"
    (conflict,) = bob.conflicts()
    assert conflict["path"] == "differs.md" and "conflict bob" in conflict["copy"]
    assert bob.text(conflict["copy"]) == alice.text(conflict["copy"]) == "bob's\n"


async def test_edits_made_offline_are_reconciled_on_reconnect(
    studio: Callable[..., Awaitable[Studio]], server: Server
) -> None:
    alice, bob = await studio("alice"), await studio("bob")
    await alice.docs.write("a.md", "one\n")
    await alice.docs.write("b.md", "one\n")
    await alice.docs.write("c.md", "one\n")
    await alice.docs.write("d.md", "one\n")
    await eventually(lambda: len(bob.paths()) == 4)
    await bob.settled()

    await bob.sync.stop()
    assert bob.sync.status()["state"] != "connected"
    # While Bob is away: Alice edits a, deletes b, edits c and d. Bob edits c and deletes d.
    await alice.docs.write("a.md", "alice\n")
    await alice.docs.delete("b.md")
    await alice.docs.write("c.md", "alice\n")
    await alice.docs.write("d.md", "alice\n")
    await bob.docs.write("c.md", "bob\n")
    await bob.docs.delete("d.md")
    await bob.docs.write("e.md", "bob's new one\n")
    await alice.settled()

    await bob.sync.start()
    await bob.connected()
    await bob.settled()
    await eventually(lambda: alice.text("e.md") == "bob's new one\n", what="bob's offline document")

    assert bob.text("a.md") == "alice\n", "untouched here, newer there: taken"
    assert bob.text("b.md") is None, "untouched here, deleted there: deleted"
    assert bob.text("d.md") == "alice\n", "deleted here, edited there: the edit wins"
    # Edited on both sides: the server's version has the path, Bob's is kept beside it.
    assert bob.text("c.md") == "alice\n"
    (conflict,) = bob.conflicts()
    assert bob.text(conflict["copy"]) == "bob\n"
    await eventually(lambda: alice.text(conflict["copy"]) == "bob\n", what="the copy to reach alice")
    assert alice.paths() == bob.paths()


async def test_a_refused_key_stops_instead_of_retrying(
    studio: Callable[..., Awaitable[Studio]]
) -> None:
    mallory = await studio("mallory", token="vxs_not-a-key", start=False)
    await mallory.sync.start()
    await eventually(lambda: mallory.sync.status()["state"] == "unauthorised", what="the refusal")
    assert mallory.sync._runner is not None and mallory.sync._runner.done()
    statuses = [e.data["state"] for e in mallory.bus.replay(0) if e.type == EventType.SYNC_STATUS]
    assert statuses[-1] == "unauthorised"


async def test_a_studio_reconnects_when_the_server_comes_back(
    tmp_path: Path, studio: Callable[..., Awaitable[Studio]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.05)
    store = Store(":memory:")
    first = await start_server(store)
    port = int(first.url.rsplit(":", 1)[1])
    token = first.issue("alice")
    alice = await studio("alice", token=token, start=False)
    alice.config.sync.url = first.url
    await alice.sync.start()
    await alice.connected()
    await alice.docs.write("kept.md", "one\n")
    await alice.settled()

    await first.stop()
    await eventually(lambda: alice.sync.status()["state"] in {"offline", "connecting"}, what="the drop")
    await alice.docs.write("kept.md", "two\n")

    second = await start_server(store, port)
    try:
        await alice.connected()
        await alice.settled()
        assert second.doc("kept.md").content == "two\n"
    finally:
        await alice.sync.stop()
        await second.stop()
        store.close()


# ------------------------------------------------------------------ rooms (§16.4)


class Browser:
    """The browser's end of ``/ws/collab``, as the backend's handler sees it."""

    def __init__(self, path: str, client_id: int) -> None:
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed: tuple[int, str] | None = None
        self.incoming.put_nowait({"t": "join", "path": path, "client_id": client_id})

    # --- what the handler calls
    async def receive_json(self) -> dict[str, Any]:
        message = await self.incoming.get()
        if message is None:
            raise WebSocketDisconnect(1000)
        return message

    async def send_json(self, message: dict[str, Any]) -> None:
        if self.closed is not None:
            raise RuntimeError("closed")
        self.frames.put_nowait(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed is None:
            self.closed = (code, reason)
            self.incoming.put_nowait(None)

    # --- what the test calls
    def send(self, **message: Any) -> None:
        self.incoming.put_nowait(message)

    async def expect(self, kind: str) -> dict[str, Any]:
        while True:
            message = await asyncio.wait_for(self.frames.get(), 5)
            if message["t"] == kind:
                return message

    def hang_up(self) -> None:
        self.incoming.put_nowait(None)


def open_in_browser(person: Studio, path: str, client_id: int) -> tuple[Browser, asyncio.Task[None]]:
    browser = Browser(path, client_id)
    task = asyncio.create_task(person.sync.serve_collab(browser))  # type: ignore[arg-type]
    return browser, task


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_two_browsers_edit_one_document_live(
    studio: Callable[..., Awaitable[Studio]], server: Server
) -> None:
    alice, bob = await studio("alice"), await studio("bob")
    await alice.docs.write("doc.md", "# Doc\n")
    await eventually(lambda: bob.text("doc.md") == "# Doc\n")
    await bob.settled()

    # Alice opens it first: an empty room, the file's text to seed it from, and the scribe's job.
    a, a_task = open_in_browser(alice, "doc.md", 11)
    joined = await a.expect("joined")
    assert joined["seeded"] is False and joined["scribe"] is True
    assert joined["content"] == "# Doc\n" and joined["user"]["name"] == "alice"
    a_doc = pycrdt.Doc()
    a_doc["body"] = pycrdt.Text("Doc")
    a.send(t="seed", update=b64(a_doc.get_update()))
    assert (await a.expect("seeded"))["ok"] is True

    # Bob opens it second and is handed Alice's document — no text, he must not seed.
    b, b_task = open_in_browser(bob, "doc.md", 22)
    second = await b.expect("joined")
    assert second["seeded"] is True and second["scribe"] is False and second["content"] is None
    assert second["user"]["name"] == "bob"
    b_doc = pycrdt.Doc()
    b_doc["body"] = pycrdt.Text()
    b_doc.apply_update(base64.b64decode(second["state"]))
    assert str(b_doc["body"]) == "Doc"
    await a.expect("peer_joined")

    # Bob types; the update crosses his backend, the remote and Alice's backend.
    before = b_doc.get_state()
    b_doc["body"] += " together"
    b.send(t="update", update=b64(b_doc.get_update(before)))
    a_doc.apply_update(base64.b64decode((await a.expect("update"))["update"]))
    assert str(a_doc["body"]) == "Doc together"
    b.send(t="awareness", update=b64(b"cursor"))
    assert base64.b64decode((await a.expect("awareness"))["update"]) == b"cursor"

    # Alice, the scribe, saves the room's Markdown through the ordinary docs API path.
    alice.sync.note_live_write("doc.md", joined["sid"], "# Doc together\n")
    await alice.docs.write("doc.md", "# Doc together\n")
    await eventually(lambda: bob.text("doc.md") == "# Doc together\n", what="the room's Markdown")
    # It was the room's own write, so the room is still there and nobody was reset.
    assert server.hub._spaces["game"].rooms["doc.md"].seeded
    assert a.closed is None and b.closed is None
    peers = {peer["user"]["name"]: peer for peer in bob.sync.status()["peers"]}
    assert peers["alice"]["docs"] == ["doc.md"] and peers["bob"]["self"] is True

    # Alice closes the document. Bob is told her cursor is gone and inherits the scribe's job.
    a.hang_up()
    await a_task
    assert (await b.expect("peer_left"))["client_id"] == 11
    await b.expect("scribe")
    b.hang_up()
    await b_task
    await eventually(lambda: not server.hub._spaces["game"].rooms, what="the room to close")
    assert not alice.conflicts() and not bob.conflicts()


async def test_the_scribes_last_save_is_pushed_before_it_leaves_the_room(
    studio: Callable[..., Awaitable[Studio]], server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editor saves and hangs up in one breath. Pushed after the leave, that save would be
    a write from outside the room and throw everyone else out of it."""
    # A debounce long enough that the push cannot beat the hang-up by accident.
    monkeypatch.setattr(sync_service, "PUSH_DEBOUNCE_SECONDS", 0.5)
    alice, bob = await studio("alice"), await studio("bob")
    await alice.docs.write("doc.md", "# Doc\n")
    await eventually(lambda: bob.text("doc.md") == "# Doc\n", timeout=10)
    await bob.settled()

    a, a_task = open_in_browser(alice, "doc.md", 11)
    joined = await a.expect("joined")
    seed = pycrdt.Doc()
    seed["body"] = pycrdt.Text("Doc")
    a.send(t="seed", update=b64(seed.get_update()))
    await a.expect("seeded")
    b, b_task = open_in_browser(bob, "doc.md", 22)
    await b.expect("joined")

    alice.sync.note_live_write("doc.md", joined["sid"], "# Doc, saved on the way out\n")
    await alice.docs.write("doc.md", "# Doc, saved on the way out\n")
    a.hang_up()
    await a_task

    await eventually(lambda: bob.text("doc.md") == "# Doc, saved on the way out\n", timeout=10)
    assert b.closed is None, "bob's room survived alice's parting save"
    b.hang_up()
    await b_task


async def test_a_write_from_outside_the_room_sends_its_editors_back_to_the_file(
    studio: Callable[..., Awaitable[Studio]], server: Server
) -> None:
    alice, bob = await studio("alice"), await studio("bob")
    await alice.docs.write("doc.md", "# Doc\n")
    await eventually(lambda: bob.text("doc.md") == "# Doc\n")
    await bob.settled()

    a, a_task = open_in_browser(alice, "doc.md", 11)
    await a.expect("joined")
    seed = pycrdt.Doc()
    seed["body"] = pycrdt.Text("Doc")
    a.send(t="seed", update=b64(seed.get_update()))
    await a.expect("seeded")

    # Bob's agent rewrites the document; Bob does not have it open.
    await bob.docs.write("doc.md", "# Rewritten by an agent\n", author="mbr_writer")
    await a_task
    assert a.closed == (CLOSE_COLLAB_RESET, "replaced")
    # By the time her editor is told, the new text is already on her disk to reopen.
    assert alice.text("doc.md") == "# Rewritten by an agent\n"

    # She reopens it: a fresh room, seeded from the rewritten text.
    again, again_task = open_in_browser(alice, "doc.md", 11)
    rejoined = await again.expect("joined")
    assert rejoined["seeded"] is False and rejoined["content"] == "# Rewritten by an agent\n"
    again.hang_up()
    await again_task


async def test_a_room_is_refused_while_offline_and_ended_when_the_connection_drops(
    studio: Callable[..., Awaitable[Studio]]
) -> None:
    alice = await studio("alice")
    await alice.docs.write("doc.md", "# Doc\n")
    a, a_task = open_in_browser(alice, "doc.md", 11)
    await a.expect("joined")

    await alice.sync.stop()
    await a_task
    assert a.closed == (CLOSE_COLLAB_UNAVAILABLE, "offline")

    late, late_task = open_in_browser(alice, "doc.md", 11)
    await late_task
    assert late.closed == (CLOSE_COLLAB_UNAVAILABLE, "unavailable")


# ------------------------------------------------------------------ project-bound rooms (§5.3.2, §16.4)


def collab_app(*people: Studio) -> tuple[FastAPI, list[ProjectDescriptor]]:
    """Real route/relay/remote, with only the resident-context registry supplied here."""
    app = FastAPI()
    app.include_router(ws_router)
    app.state.auth = AuthSettings()
    descriptors = [
        ProjectDescriptor(
            project_id=f"project-{person.root.name}", epoch="test-backend-epoch",
            root=person.root, ctx=SimpleNamespace(sync=person.sync, paths=SimpleNamespace(root=person.root)),
        )
        for person in people
    ]
    app.state.project_registry = {item.project_id: item for item in descriptors}
    app.state.ctx = descriptors[0].ctx
    return app, descriptors


class CollabSocket:
    """ASGI WebSocket client on the remote's event loop; no TestClient portal/thread.

    The queue contains actual ASGI frames, so rejected handshakes and wire envelopes
    are tested too, rather than calling the collaboration handler directly.
    """

    def __init__(self, app: FastAPI, url: str, binding: ProjectDescriptor | None = None) -> None:
        self.app, self.url, self.binding = app, url, binding
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.messages: list[dict[str, Any]] = []
        self.closed: dict[str, Any] | None = None

    async def __aenter__(self) -> CollabSocket:
        path, _, query = self.url.partition("?")
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
            "path": path, "raw_path": path.encode(), "query_string": query.encode(),
            "root_path": "", "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8765),
            "subprotocols": [], "state": {},
        }

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "websocket.send":
                frame = json.loads(message["text"])
                if self.binding is not None:
                    assert frame["project_id"] == self.binding.project_id
                    assert frame["epoch"] == self.binding.epoch
                else:
                    # Legacy joined already has a *room* epoch (§16.4); it is not
                    # the backend-lifetime scope epoch introduced by §5.3.2.
                    assert "project_id" not in frame
                self.messages.append(frame)
            elif message["type"] == "websocket.close":
                self.closed = message
            self.outgoing.put_nowait(message)

        self.incoming.put_nowait({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.app(scope, self.incoming.get, send))
        try:
            self.handshake = await self.next_asgi()
        except BaseException:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self.task, 5)
        finally:
            if not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    async def next_asgi(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.outgoing.get(), 5)

    def send(self, **message: Any) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})

    async def receive(self) -> dict[str, Any]:
        message = await self.next_asgi()
        assert message["type"] == "websocket.send", message
        return json.loads(message["text"])

    async def expect(self, kind: str) -> dict[str, Any]:
        while True:
            message = await self.receive()
            if message.get("t") == kind:
                return message

    async def join(self, client_id: int, **scope: Any) -> dict[str, Any]:
        assert self.handshake["type"] == "websocket.accept", self.handshake
        if self.binding is not None:
            hello = await self.receive()
            assert hello["type"] == "system.hello"
            assert hello["data"] == {
                "project_id": self.binding.project_id, "epoch": self.binding.epoch,
                "project": str(self.binding.root),
            }
        self.send(t="join", path="doc.md", client_id=client_id, **scope)
        return await self.expect("joined")


def scoped_collab_url(binding: ProjectDescriptor) -> str:
    return f"/ws/projects/{binding.project_id}/collab?epoch={binding.epoch}"


async def test_scoped_rooms_and_legacy_coexist_without_rebinding_on_selection(
    studio: Callable[..., Awaitable[Studio]], server: Server,
) -> None:
    a = await studio("project-a", space="space-a")
    b = await studio("project-b", space="space-b")
    for person, text in ((a, "# A\n"), (b, "# B\n")):
        await person.docs.write("doc.md", text)
        await person.settled()
    app, (a_binding, b_binding) = collab_app(a, b)

    async with AsyncExitStack() as stack:
        a_ws = await stack.enter_async_context(CollabSocket(app, scoped_collab_url(a_binding), a_binding))
        a_joined = await a_ws.join(11, project_id=a_binding.project_id, epoch=a_binding.epoch)
        assert a_joined["content"] == "# A\n" and not a_joined["seeded"]
        a_doc = pycrdt.Doc()
        a_doc["body"] = pycrdt.Text("A")
        a_ws.send(t="seed", update=b64(a_doc.get_update()))
        await a_ws.expect("seeded")

        b_ws = await stack.enter_async_context(CollabSocket(app, scoped_collab_url(b_binding), b_binding))
        b_joined = await b_ws.join(22)
        assert b_joined["content"] == "# B\n" and not b_joined["seeded"]
        b_doc = pycrdt.Doc()
        b_doc["body"] = pycrdt.Text("B")
        b_ws.send(t="seed", update=b64(b_doc.get_update()))
        await b_ws.expect("seeded")

        legacy_a = await stack.enter_async_context(CollabSocket(app, "/ws/collab"))
        legacy_joined = await legacy_a.join(33)
        assert legacy_joined["seeded"] and legacy_joined["content"] is None
        assert legacy_joined["epoch"] != a_binding.epoch
        assert a_joined["room_epoch"] == legacy_joined["epoch"]
        assert b_joined["room_epoch"] != a_joined["room_epoch"]
        assert legacy_joined["state"] == b64(a_doc.get_update())
        await a_ws.expect("peer_joined")

        # Only the single-selection adapter changes. Both existing scoped rooms and
        # the already accepted legacy socket must keep their original context.
        app.state.ctx = b_binding.ctx
        replica = pycrdt.Doc()
        replica["body"] = pycrdt.Text()
        replica.apply_update(a_doc.get_update())
        before = a_doc.get_state()
        a_doc["body"] += " after switch"
        legacy_a.send(t="update", update=b64(a_doc.get_update(before)))
        a_update = await a_ws.expect("update")
        replica.apply_update(base64.b64decode(a_update["update"]))
        assert str(replica["body"]) == "A after switch"
        assert a_update["update"] == b64(a_doc.get_update(before))

        legacy_b = await stack.enter_async_context(CollabSocket(app, "/ws/collab"))
        joined_b = await legacy_b.join(44)
        loaded_b = pycrdt.Doc()
        loaded_b["body"] = pycrdt.Text()
        loaded_b.apply_update(base64.b64decode(joined_b["state"]))
        assert str(loaded_b["body"]) == "B"
        await b_ws.expect("peer_joined")
        b_ws.send(t="awareness", update=b64(b"only-b"), project_id=b_binding.project_id, epoch=b_binding.epoch)
        assert (await legacy_b.expect("awareness"))["update"] == b64(b"only-b")
        assert not any(frame.get("t") == "update" for frame in b_ws.messages)
        assert not any(frame.get("t") == "awareness" for frame in a_ws.messages)
        a_sids = {session.sid for session in server.hub._spaces["space-a"].rooms["doc.md"].sessions}
        b_sids = {session.sid for session in server.hub._spaces["space-b"].rooms["doc.md"].sessions}
        assert a_sids.isdisjoint(b_sids) and len(a_sids) == len(b_sids) == 2
        assert a.text("doc.md") == "# A\n" and b.text("doc.md") == "# B\n"


@pytest.mark.parametrize("selector", ["unknown-id", "retired-id", "stale-epoch"])
async def test_scoped_collab_rejects_unknown_or_stale_binding_without_legacy_fallback(
    studio: Callable[..., Awaitable[Studio]], selector: str,
) -> None:
    person = await studio("alice")
    await person.docs.write("doc.md", "# Doc\n")
    await person.settled()
    app, (binding,) = collab_app(person)
    if selector == "stale-epoch":
        url = f"/ws/projects/{binding.project_id}/collab?epoch=previous-backend"
    else:
        project_id = binding.project_id if selector == "retired-id" else "never-published"
        if selector == "retired-id":
            app.state.project_registry.pop(project_id)
        url = f"/ws/projects/{project_id}/collab"
    async with CollabSocket(app, url) as socket:
        assert socket.handshake["type"] == "websocket.close", socket.handshake
        assert socket.handshake["code"] != 1000
        assert not person.sync._sessions, "an invalid selector must never join the legacy room"


@pytest.mark.parametrize("phase", ["join", "update"])
@pytest.mark.parametrize("field,value", [("project_id", "other-project"), ("epoch", "old-epoch")])
async def test_scoped_collab_rejects_conflicting_inbound_scope(
    studio: Callable[..., Awaitable[Studio]], phase: str, field: str, value: str,
) -> None:
    person = await studio("alice")
    await person.docs.write("doc.md", "# Doc\n")
    await person.settled()
    app, (binding,) = collab_app(person)
    async with CollabSocket(app, scoped_collab_url(binding), binding) as socket:
        if phase == "join":
            assert (await socket.receive())["type"] == "system.hello"
            socket.send(t="join", path="doc.md", client_id=11, **{field: value})
        else:
            await socket.join(11)
            seed = pycrdt.Doc()
            seed["body"] = pycrdt.Text("Doc")
            socket.send(t="seed", update=b64(seed.get_update()))
            await socket.expect("seeded")
            before = seed.get_state()
            seed["body"] += " must not cross the scope guard"
            socket.send(t="update", update=b64(seed.get_update(before)), **{field: value})
        closed = await socket.next_asgi()
        assert closed["type"] == "websocket.close", closed
        assert closed["code"] == 1008, "scope conflict, not a remote protocol/availability failure"
    assert not person.sync._sessions
    assert person.text("doc.md") == "# Doc\n"


async def test_scoped_room_open_writes_nothing_and_yjs_is_not_persisted(
    studio: Callable[..., Awaitable[Studio]], server: Server,
) -> None:
    person = await studio("alice")
    text = "# Doc\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    await person.docs.write("doc.md", text)
    await person.settled()
    file = person.docs.directory / "doc.md"
    stat = file.stat()
    stored = server.doc("doc.md")
    app, (binding,) = collab_app(person)
    # Omitting optional epoch is supported; the hello still identifies the epoch.
    url = f"/ws/projects/{binding.project_id}/collab"
    async with CollabSocket(app, url, binding) as socket:
        joined = await socket.join(11)
        assert joined["content"] == text and not joined["seeded"] and not joined["dirty"]
        seed = pycrdt.Doc()
        seed["body"] = pycrdt.Text("session-only Yjs, never saved as Markdown")
        socket.send(t="seed", update=b64(seed.get_update()))
        await socket.expect("seeded")
    await eventually(lambda: not server.hub._spaces["game"].rooms)
    await person.settled()
    assert file.read_bytes() == text.encode()
    assert file.stat().st_mtime_ns == stat.st_mtime_ns
    assert server.doc("doc.md") == stored, "opening/seeding does not increment the Markdown revision"
    assert person.paths() == ["doc.md"]

    # Reconnect the actual sync client too: neither local sync state nor the remote
    # may resurrect the old Yjs session. A fresh room must be rebuilt from Markdown.
    await person.sync.stop()
    await person.sync.start()
    await person.connected()
    async with CollabSocket(app, url, binding) as reopened:
        joined = await reopened.join(22)
        assert not joined["seeded"] and not joined["dirty"]
        assert joined["content"] == text
        assert joined["state"] is None, "a new room contains no persisted Yjs state"
    assert server.doc("doc.md") == stored and file.stat().st_mtime_ns == stat.st_mtime_ns


# ------------------------------------------------------------------ settings (§16.5)


def test_settings_keep_the_key_out_of_the_project(
    client: TestClient, ctx: AppContext, studio_home: Path
) -> None:
    assert client.get("/api/sync").json()["state"] == "disabled"

    refused = client.put("/api/sync", json={"enabled": True})
    assert refused.status_code == 400, "enabled with nowhere to connect to"
    assert client.put("/api/sync", json={"url": "ftp://nope"}).status_code == 400
    assert client.put("/api/sync", json={"space": "Not A Space!"}).status_code == 400
    assert client.put("/api/sync", json={"token": "vxs_orphan"}).status_code == 400

    saved = client.put(
        "/api/sync",
        json={"url": "studio.example.com", "space": "Game", "token": "vxs_secret-key"},
    ).json()
    assert saved["url"] == "studio.example.com" and saved["space"] == "game"
    assert saved["has_token"] is True and saved["state"] == "disabled" and not saved["insecure"]
    assert "vxs_secret-key" not in json.dumps(saved)

    # The project file says where; the user's directory holds the key.
    on_disk = ctx.paths.config_file.read_text(encoding="utf-8")
    assert "studio.example.com" in on_disk and "vxs_secret-key" not in on_disk
    assert load_config(ctx.paths).sync.space == "game"
    assert credentials.load_token("https://studio.example.com") == "vxs_secret-key"
    assert credentials.keys_file().is_relative_to(studio_home)
    assert "vxs_secret-key" not in json.dumps(client.get("/api/project").json())

    forgotten = client.put("/api/sync", json={"token": ""}).json()
    assert forgotten["has_token"] is False
    assert credentials.load_token("https://studio.example.com") is None
