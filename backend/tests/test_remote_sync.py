"""Online sync, end to end: two Studios and a real remote in one event loop (§16).

Nothing on the wire is mocked. The remote is the actual v2 server from ``remote/``,
listening on an ephemeral loopback port; each "person" is a managed project with its own
:class:`DocStore`, bus and activated sync client (I10): a confirmed private binding, a
profile-bound key in the S2 credential store and a SyncStateV2 checkpoint, all in this
test's own ``VOXARIUM_STUDIO_HOME``. What is asserted is what is on each person's disk —
which is the only thing sync promises anybody — and what the remote holds.

The legacy v1 runner is still exercised once: an unactivated project against this v2
remote stops on 4400 ``protocol_upgrade_required`` instead of retrying or falling back.

The remote is a separate package, so this module is skipped unless it is installed into the
backend's environment (``uv pip install -e ../remote``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

remote = pytest.importorskip("voxarium_studio_remote.app")
pycrdt = pytest.importorskip("pycrdt")

import uvicorn  # noqa: E402
from voxarium_studio_remote import hub as remote_hub  # noqa: E402
from voxarium_studio_remote.auth import hash_token, new_token  # noqa: E402
from voxarium_studio_remote.store import Store  # noqa: E402

from voxarium_studio.core.application_config import RemoteServerProfile  # noqa: E402
from voxarium_studio.core.config import AppConfig, SyncConfig, load_config  # noqa: E402
from voxarium_studio.core.context import AppContext, _ProfileRemoteSync  # noqa: E402
from voxarium_studio.core.paths import ProjectPaths  # noqa: E402
from voxarium_studio.core.project import init_project  # noqa: E402
from voxarium_studio.core.project_scope import ProjectDescriptor, canonical_root  # noqa: E402
from voxarium_studio.core.security import AuthSettings  # noqa: E402
from voxarium_studio.sync import RemoteSync, credentials  # noqa: E402
from voxarium_studio.sync import service as sync_service  # noqa: E402
from voxarium_studio.sync.bindings import bindings_file, root_key  # noqa: E402
from voxarium_studio.sync.profile_lifecycle import ProfileLifecycle  # noqa: E402
from voxarium_studio.sync.protocol import (  # noqa: E402
    CLOSE_COLLAB_RESET,
    CLOSE_COLLAB_UNAVAILABLE,
    content_hash,
    is_insecure,
    normalise_url,
    socket_url,
    socket_url_v2,
)
from voxarium_studio.sync.reservations import ReservationOwner  # noqa: E402
from voxarium_studio.sync.service import ActivatedRemoteTarget  # noqa: E402
from voxarium_studio.sync.state import SyncState, SyncStateV2  # noqa: E402
from voxarium_studio.teams.docs import DocStore  # noqa: E402
from voxarium_studio.ws.bus import EventBus  # noqa: E402
from voxarium_studio.ws.events import EventType  # noqa: E402
from voxarium_studio.ws.routes import router as ws_router  # noqa: E402

# The suite must not wait out a human-scale debounce on every assertion.
sync_service.PUSH_DEBOUNCE_SECONDS = 0.01

PROFILES = uuid.UUID("6f1d3c2a-8b4e-4f5a-9c7d-0e1f2a3b4c5d")


@pytest.fixture(autouse=True)
def no_ambient_remote_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the profile-bound stored key may authenticate a Studio here."""
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_URL", raising=False)


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
    users: dict[str, str] = field(default_factory=dict)

    def issue(self, name: str) -> str:
        token = new_token()
        self.users[name] = self.store.add_user(name, hash_token(token)).id
        return token

    def project(self, project_id: str = "game") -> Any:
        """The project as an operator created it — the remote never creates one itself."""
        try:
            return self.store.project(project_id)
        except LookupError:
            return self.store.create_project(project_id, project_id.title())

    def doc(self, path: str, space: str = "game") -> Any:
        return self.store.doc(self.store.space(space).id, path)

    def seq(self, space: str = "game") -> int:
        return self.store.seq(space)


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
    paths: ProjectPaths | None = None
    target: ActivatedRemoteTarget | None = None
    events: list[Any] = field(default_factory=list)

    def text(self, path: str) -> str | None:
        file = self.docs.directory / path
        return file.read_text(encoding="utf-8") if file.is_file() else None

    def paths_list(self) -> list[str]:
        return sorted(entry.path for entry in self.docs.list_docs())

    def conflicts(self) -> list[dict[str, Any]]:
        return [e.data for e in self.bus.replay(0) if e.type == EventType.SYNC_CONFLICT]

    def checkpoint(self) -> SyncStateV2 | None:
        assert self.target is not None
        return SyncStateV2.load(self.root, url=self.target.url, persistent_id=self.target.persistent_id)

    def checkpoint_bytes(self) -> bytes:
        assert self.target is not None
        namespace = SyncStateV2.fresh(self.root, url=self.target.url, persistent_id=self.target.persistent_id,
                                      space_instance_id=self.target.space_instance_id).namespace
        return namespace.path.read_bytes()

    def status(self) -> dict[str, Any]:
        return self.sync.status()

    async def connected(self) -> None:
        await eventually(lambda: self.sync.live, what=f"{self.root.name} to connect")

    async def stopped(self) -> dict[str, Any]:
        """The runner ended on its own — a terminal state, not a retry."""
        await eventually(lambda: self.sync._runner is not None and self.sync._runner.done(),
                         what=f"{self.root.name}'s runner to stop")
        return self.status()

    async def settled(self) -> None:
        """Nothing left to push and nothing in flight."""
        await asyncio.sleep(0.05)
        await eventually(
            lambda: not self.sync._dirty.is_set() and not self.sync._pending,
            what=f"{self.root.name} to settle",
        )


def write_binding_row(target: ActivatedRemoteTarget, **changes: Any) -> None:
    """This root's confirmed private consent (S2), beside whatever other roots have."""
    file = bindings_file()
    data = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {"version": 1, "roots": {}}
    data["roots"][root_key(target.canonical_root)] = {
        "canonical_root": target.canonical_root, "profile_id": target.profile_id, "url": target.url,
        "persistent_id": target.persistent_id, "identity_revision": target.identity_revision,
        "selection_state": "confirmed", "space_instance_id": target.space_instance_id, **changes,
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
async def studio(tmp_path: Path, server: Server) -> AsyncIterator[Callable[..., Awaitable[Studio]]]:
    made: list[Studio] = []
    profiles: dict[str, RemoteServerProfile] = {}

    async def make(
        name: str, *, token: str | None = None, start: bool = True, space: str = "game",
        remote_server: Server | None = None, create: bool = True, cursor: int = 0,
        instance: str | None = None, account: str | None = None, verify: bool = True,
    ) -> Studio:
        """An activated project: confirmed binding + profile key + v2 checkpoint (I10)."""
        host = remote_server or server
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        paths = init_project(root)
        url = normalise_url(host.url)
        epoch = host.project(space).storage_epoch if create else uuid.uuid4().hex
        instance = instance or epoch
        raw = {"project_identity": {"id": space, "revision": 1}, "sync": {"enabled": True, "url": url}}
        paths.config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
        config = AppConfig.model_validate(raw)
        profile_id = str(uuid.uuid5(PROFILES, name))
        profiles[profile_id] = RemoteServerProfile(profile_id=profile_id, name=name.title(), url=url)
        key = token or host.issue(name)
        credentials.save_profile_token(profile_id, url, key,
                                       expected_revision=credentials.profile_token_revision(profile_id, url))
        target = ActivatedRemoteTarget(
            str(canonical_root(paths.root)), hashlib.sha256(paths.config_file.read_bytes()).hexdigest(),
            profile_id, url, space, 1, instance,
        )
        write_binding_row(target)
        replace(SyncStateV2.fresh(paths.root, url=url, persistent_id=space, space_instance_id=instance),
                cursor=cursor).save()
        verified = account if account is not None else (host.users.get(name) if verify else None)
        bus = EventBus()
        docs = DocStore(paths, bus)
        lifecycle = ProfileLifecycle(profile_reader=profiles.__getitem__)
        owner = ReservationOwner()  # one process per person: each owns its own destination
        holder: list[Studio] = []
        sync = _ProfileRemoteSync(
            paths, lambda: config, docs, bus, lifecycle=lambda: lifecycle, reservations=lambda: owner,
            activation=lambda: holder[0].target, profile_reader=profiles.__getitem__,
            verified_account=lambda _profile, _url: verified,
        )
        person = Studio(root=root, docs=docs, bus=bus, sync=sync, config=config, paths=paths, target=target)
        holder.append(person)
        made.append(person)
        if start:
            await sync.start()
            await person.connected()
        return person

    yield make
    for person in made:
        await person.sync.stop()


# ------------------------------------------------------------------ addresses


def test_addresses_are_folded_to_one_spelling() -> None:
    assert normalise_url("studio.example.com") == "https://studio.example.com"
    assert normalise_url(" WSS://Studio.Example.com/ ") == "https://studio.example.com"
    assert normalise_url("http://10.0.0.5:8787/remote/") == "http://10.0.0.5:8787/remote"
    assert socket_url("https://h.example/r", "game") == "wss://h.example/r/api/v1/sync?space=game"
    assert socket_url("http://127.0.0.1:8787", "game") == "ws://127.0.0.1:8787/api/v1/sync?space=game"
    # The activated runner's socket carries the exact ID — never a folded legacy selector.
    assert socket_url_v2("https://h.example/r", "game.v2") == "wss://h.example/r/api/v2/sync?space=game.v2"
    for bad in ("Game", " game", "game/../x", "", "a" * 65):
        with pytest.raises(ValueError):
            socket_url_v2("https://h.example", bad)
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
    await eventually(lambda: bob.paths_list() == ["archive/notes.md"], what="the move to arrive")
    await alice.settled()
    await bob.settled()
    assert not alice.conflicts() and not bob.conflicts()
    # The agreed bases live in each person's own v2 checkpoint, keyed by the exact target.
    await alice.sync.stop()
    saved = alice.checkpoint()
    assert saved is not None and saved.cursor == server.seq()
    assert saved.space_instance_id == server.project().storage_epoch
    assert saved.docs["archive/notes.md"].hash == content_hash("# Notes\n")


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
    await eventually(lambda: len(alice.paths_list()) == 5, what="alice to receive bob's files")

    # Identical files are simply agreed on; a file only one side had is on both.
    assert alice.paths_list() == bob.paths_list()
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
    await eventually(lambda: len(bob.paths_list()) == 4)
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
    assert alice.paths_list() == bob.paths_list()


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
    hellos: list[str] = []
    accept = sync_service._ActivatedLink.accept_hello

    def counted(link: Any, raw: object) -> Any:
        hellos.append("checked")
        return accept(link, raw)

    monkeypatch.setattr(sync_service._ActivatedLink, "accept_hello", counted)
    store = Store(":memory:")
    first = await start_server(store)
    port = int(first.url.rsplit(":", 1)[1])
    alice = await studio("alice", remote_server=first)
    await alice.docs.write("kept.md", "one\n")
    await alice.settled()
    assert hellos == ["checked"]

    await first.stop()
    await eventually(lambda: alice.sync.status()["state"] in {"offline", "connecting"}, what="the drop")
    await alice.docs.write("kept.md", "two\n")

    second = await start_server(store, port)
    try:
        await alice.connected()
        await alice.settled()
        assert second.doc("kept.md").content == "two\n"
        # The reconnect was admitted by a fresh strict hello, not by the earlier one.
        assert hellos == ["checked", "checked"]
    finally:
        await alice.sync.stop()
        await second.stop()
        store.close()


# ------------------------------------------------------------------ strict v2 connect (I10)


def count_connects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    addresses: list[str] = []
    real = sync_service.connect

    def counted(address: str, **kwargs: Any) -> Any:
        addresses.append(address)
        return real(address, **kwargs)

    monkeypatch.setattr(sync_service, "connect", counted)
    return addresses


def server_untouched(server: Server, space: str = "game") -> bool:
    try:
        project = server.store.project(space)
    except LookupError:
        return True
    return project.seq == 0 and server.doc("local.md", space) is None


@pytest.mark.parametrize("case,state,code", [
    ("unauthorised", "unauthorised", "unauthorised"),
    ("project_not_found", "blocked", "project_not_found"),
    ("invalid_request", "blocked", "invalid_request"),
])
async def test_terminal_close_codes_stop_without_retry_or_writes(
    studio: Callable[..., Awaitable[Studio]], server: Server, monkeypatch: pytest.MonkeyPatch,
    case: str, state: str, code: str,
) -> None:
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.01)
    addresses = count_connects(monkeypatch)
    if case == "invalid_request":
        # A query the remote refuses (4400) — as if the address had been mangled.
        monkeypatch.setattr(sync_service, "socket_url_v2",
                            lambda base, project_id: socket_url_v2(base, project_id) + "&extra=1")
    person = await studio("alice", start=False, create=case != "project_not_found",
                          token="vxs_not-a-key" if case == "unauthorised" else None)
    await person.docs.write("local.md", "# Local\n")
    checkpoint = person.checkpoint_bytes()
    await person.sync.start()
    status = await person.stopped()
    assert (status["state"], status["message"]) == (state, code)
    await asyncio.sleep(0.1)
    assert len(addresses) == 1, "4400/4401/4404 are not retried"
    assert all("/api/v2/sync?space=game" in address and "/api/v1/" not in address for address in addresses)
    assert server_untouched(server)
    if case == "project_not_found":
        assert not server.store.list_spaces(), "a v2 connect is lookup-only, it never creates"
    assert person.checkpoint_bytes() == checkpoint
    assert person.text("local.md") == "# Local\n"


def rewrite_hello(monkeypatch: pytest.MonkeyPatch, change: Callable[[dict[str, Any]], Any]) -> None:
    """A remote whose hello is not what the accepted protocol promises."""
    real = remote_hub.Connection.send

    def send(connection: Any, message: dict[str, Any]) -> None:
        if message.get("t") != "hello":
            return real(connection, message)
        changed = change(json.loads(json.dumps(message)))
        if isinstance(changed, str):
            connection.queue.put_nowait(changed)  # not even JSON
        else:
            real(connection, changed)

    monkeypatch.setattr(remote_hub.Connection, "send", send)


def _set(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def change(hello: dict[str, Any]) -> dict[str, Any]:
        node = hello
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        return hello
    return change


@pytest.mark.parametrize("case,code", [
    ("not_json", "invalid_hello"),
    ("protocol_1", "protocol_mismatch"),
    ("protocol_bool", "invalid_hello"),
    ("missing_seq", "invalid_hello"),
    ("extra_field", "invalid_hello"),
    ("bad_user", "invalid_hello"),
    ("foreign_id", "identity_mismatch"),
    ("other_epoch", "instance_mismatch"),
    ("foreign_account", "account_mismatch"),
    ("seq_below_cursor", "cursor_rollback"),
])
async def test_an_untrusted_hello_blocks_before_any_sync_or_write(
    studio: Callable[..., Awaitable[Studio]], server: Server, monkeypatch: pytest.MonkeyPatch,
    case: str, code: str,
) -> None:
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.01)
    addresses = count_connects(monkeypatch)
    rewrites: dict[str, Callable[[dict[str, Any]], Any]] = {
        "not_json": lambda hello: "hello?",
        "protocol_1": _set(("protocol",), 1),
        "protocol_bool": _set(("protocol",), True),
        "missing_seq": lambda hello: {k: v for k, v in hello.items() if k != "seq"},
        "extra_field": _set(("capabilities",), []),
        "bad_user": _set(("user", "color"), "blue"),
    }
    if case in rewrites:
        rewrite_hello(monkeypatch, rewrites[case])
    if case == "foreign_id":
        # The socket asked for "game" but is answered for another existing project:
        # never permission to bind to the answer.
        server.project("other-game")
        monkeypatch.setattr(sync_service, "socket_url_v2", lambda base, _id: socket_url_v2(base, "other-game"))
    person = await studio(
        "alice", start=False,
        instance=uuid.uuid4().hex if case == "other_epoch" else None,
        account="somebody-else" if case == "foreign_account" else None,
        cursor=7 if case == "seq_below_cursor" else 0,
    )
    await person.docs.write("local.md", "# Local\n")
    legacy = SyncState.file_for(person.root)
    checkpoint = person.checkpoint_bytes()
    await person.sync.start()
    status = await person.stopped()
    assert (status["state"], status["message"]) == ("blocked", code)
    await asyncio.sleep(0.1)
    assert len(addresses) == 1, "a refused hello is terminal, not retried"
    assert person.sync._ws is None and person.sync._state is None, "no socket/state was published"
    assert server_untouched(server) and server_untouched(server, "other-game")
    # Rollback/mismatch recovery keeps every base and the cursor exactly as saved.
    assert person.checkpoint_bytes() == checkpoint
    assert person.text("local.md") == "# Local\n"
    assert not legacy.exists(), "no legacy checkpoint fallback"


async def test_reconnect_repeats_the_strict_hello_and_stops_on_a_replaced_project(
    studio: Callable[..., Awaitable[Studio]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.05)
    store = Store(":memory:")
    first = await start_server(store)
    port = int(first.url.rsplit(":", 1)[1])
    token = first.issue("alice")
    alice = await studio("alice", remote_server=first, token=token, account=first.users["alice"])
    await alice.docs.write("kept.md", "one\n")
    await alice.settled()
    await first.stop()
    await eventually(lambda: alice.sync.status()["state"] in {"offline", "connecting"}, what="the drop")
    saved = alice.checkpoint()
    assert saved is not None and saved.docs["kept.md"].rev == 1

    # The operator restored a different database: same ID, same user and key, new epoch.
    replaced = Store(":memory:")
    user = replaced.add_user("alice", hash_token(token))
    replaced.create_project("game", "Game")
    replaced_seq = replaced.seq("game")
    await alice.docs.write("kept.md", "written while away\n")
    second = await start_server(replaced, port)
    try:
        assert user.id != first.users["alice"]
        status = await alice.stopped()
        # Whichever check comes first, it is terminal and before any sync/write.
        assert status["state"] == "blocked"
        assert status["message"] in {"instance_mismatch", "account_mismatch"}
        assert replaced.doc("game", "kept.md") is None and replaced.seq("game") == replaced_seq
        assert alice.checkpoint() == saved, "no cursor reset, no re-based checkpoint"
        assert alice.text("kept.md") == "written while away\n"
    finally:
        await alice.sync.stop()
        await second.stop()
        store.close()
        replaced.close()


async def test_epoch_change_on_reconnect_blocks_even_for_the_same_account(
    studio: Callable[..., Awaitable[Studio]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.05)
    store = Store(":memory:")
    first = await start_server(store)
    port = int(first.url.rsplit(":", 1)[1])
    alice = await studio("alice", remote_server=first)
    await alice.docs.write("kept.md", "one\n")
    await alice.settled()
    await first.stop()
    await eventually(lambda: alice.sync.status()["state"] in {"offline", "connecting"}, what="the drop")
    saved = alice.checkpoint()
    # Same database and user, project storage re-created under the same ID (an operator
    # restore; the remote's own transaction helper, not a Remote code change).
    with store._tx() as db:
        db.execute("UPDATE spaces SET storage_epoch=? WHERE id=?", (uuid.uuid4().hex, "game"))
    second = await start_server(store, port)
    try:
        status = await alice.stopped()
        assert (status["state"], status["message"]) == ("blocked", "instance_mismatch")
        assert alice.checkpoint() == saved
    finally:
        await alice.sync.stop()
        await second.stop()
        store.close()


@pytest.mark.parametrize("drift", ["config", "private", "selected"])
async def test_manual_binding_drift_stops_the_old_target(
    studio: Callable[..., Awaitable[Studio]], server: Server, drift: str,
) -> None:
    alice = await studio("alice")
    await alice.docs.write("before.md", "# Before\n")
    await alice.settled()
    assert server.doc("before.md") is not None
    assert alice.paths is not None and alice.target is not None
    if drift == "config":
        # A hand edit (or checkout) of config.yaml while connected; the watcher reports it.
        with alice.paths.config_file.open("a", encoding="utf-8") as stream:
            stream.write("# edited by hand\n")
        await alice.bus.publish(EventType.EXPLORER_FILE_CHANGED, {
            "changes": [{"change": "modified", "path": ".voxarium_studio/config.yaml"}],
            "root": str(alice.root)})
        expected = "activation_unavailable"
    elif drift == "private":
        write_binding_row(alice.target, persistent_id="another-id")
        expected = "binding_required"
    else:
        write_binding_row(alice.target, selection_state="selected")
        expected = "binding_required"
    # Whatever is written next must not be pushed to the captured old target.
    await alice.docs.write("after.md", "# After\n")
    status = await alice.stopped()
    assert (status["state"], status["message"]) == ("blocked", expected)
    assert server.doc("after.md") is None
    assert alice.sync._ws is None and not alice.sync.live
    seq = server.seq()
    # A late change for the stopped target is not applied either.
    await alice.sync._handle({"t": "change", "path": "late.md", "rev": seq + 1, "hash": content_hash("x"),
                              "deleted": False, "content": "x", "by": None})
    assert alice.text("late.md") is None


async def test_late_ack_and_send_after_stop_or_reconnect_are_not_applied(
    studio: Callable[..., Awaitable[Studio]], server: Server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.05)
    alice = await studio("alice")
    await alice.docs.write("seen.md", "# Seen\n")
    await alice.settled()

    # The remote never answers the put; the connection then drops with the ID in flight.
    swallowed: list[dict[str, Any]] = []
    handlers = server.hub._handlers
    real_put = handlers["put"]

    def silent_put(connection: Any, message: dict[str, Any]) -> None:
        if message.get("path") == "slow.md":
            swallowed.append(message)
            return
        real_put(connection, message)

    monkeypatch.setitem(handlers, "put", silent_put)
    await alice.docs.write("slow.md", "# Slow\n")
    await eventually(lambda: swallowed, what="the unanswered put")
    (old_id,) = [mid for mid, pending in alice.sync._pending.items() if pending.path == "slow.md"]
    old_frame = swallowed[0]
    old_socket = alice.sync._ws
    handlers["put"] = real_put
    assert old_socket is not None
    await old_socket.close()  # the connection drops; the runner reconnects by itself

    await eventually(lambda: alice.sync._ws not in (None, old_socket) and alice.sync.live, what="reconnect")
    await alice.settled()
    # Catch-up + comparison settled the ambiguous put on the SAME target; its ID is gone.
    assert server.doc("slow.md").content == "# Slow\n"
    assert old_id not in alice.sync._pending and alice.sync._retained_evidence == []
    base = alice.sync._state.docs["slow.md"]
    cursor = alice.sync._state.cursor
    # A late ACK for the previous socket's ID is not applied to the new state.
    await alice.sync._handle({"t": "ack", "id": old_id, "path": "slow.md", "rev": 999,
                              "hash": content_hash("# Slow\n"), "deleted": False})
    assert alice.sync._state.docs["slow.md"] == base and alice.sync._state.cursor == cursor
    # Nor can the previous socket's write frame be sent again through the new one.
    with pytest.raises(ConnectionError, match="binding changed"):
        await alice.sync._send(old_frame)
    assert server.doc("slow.md").rev == base.rev

    await alice.sync.stop()
    with pytest.raises(ConnectionError):
        await alice.sync._send({"t": "put", "id": 10_000, "path": "late.md", "content": "x", "base_rev": 0})
    await alice.sync._handle({"t": "ack", "id": 10_000, "path": "late.md", "rev": 1000, "hash": None,
                              "deleted": True})
    assert server.doc("late.md") is None
    assert alice.checkpoint().cursor == cursor and alice.checkpoint().docs["slow.md"] == base


async def test_old_target_checkpoint_markdown_and_queues_stay_old_target(
    studio: Callable[..., Awaitable[Studio]], server: Server,
) -> None:
    alice = await studio("alice", start=False)
    # What the legacy runner left for this root: its own checkpoint and the Markdown.
    legacy = SyncState(SyncState.file_for(alice.root), url="https://legacy.invalid", space_id="old-space",
                       cursor=41)
    legacy.docs["old.md"] = sync_service.Base(41, content_hash("# Old\n"))
    legacy.save()
    legacy_bytes = SyncState.file_for(alice.root).read_bytes()
    await alice.docs.write("old.md", "# Old\n")
    evidence = [{"target": ["root", "https://legacy.invalid", "old-space"], "generation": 0,
                 "pending": [{"id": 3}], "live": [], "rooms": [], "reason": "disconnected"}]
    alice.sync._retained_evidence = list(evidence)

    await alice.sync.start()
    await alice.connected()
    await alice.settled()
    # The v2 target starts from ITS checkpoint (cursor 0) and reconciles by comparison:
    # nothing is imported from or written to the legacy checkpoint, nothing is deleted.
    assert server.doc("old.md").content == "# Old\n"
    assert SyncState.file_for(alice.root).read_bytes() == legacy_bytes
    assert alice.text("old.md") == "# Old\n"
    assert alice.sync._retained_evidence == evidence, "another target's evidence is not resolved"
    await alice.sync.stop()
    assert alice.checkpoint().docs["old.md"].rev == server.doc("old.md").rev


async def test_a_legacy_project_against_a_v2_remote_stops_without_fallback(
    tmp_path: Path, server: Server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuinely unactivated: the v1 compatibility runner, never a v2 connect."""
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.01)
    addresses = count_connects(monkeypatch)
    server.project("game")
    (tmp_path / "legacy").mkdir()
    paths = init_project(tmp_path / "legacy")
    config = AppConfig(sync=SyncConfig(enabled=True, url=server.url, space="game"))
    bus = EventBus()
    docs = DocStore(paths, bus)
    await docs.write("local.md", "# Local\n")
    key = server.issue("legacy")
    sync = RemoteSync(paths, lambda: config, docs, bus, token=lambda _url: key)
    await sync.start()
    try:
        await eventually(lambda: sync._runner is not None and sync._runner.done(), what="the stop")
        status = sync.status()
        assert status["state"] == "error" and "protocol_upgrade_required" in status["message"]
        await asyncio.sleep(0.1)
        assert len(addresses) == 1 and "/api/v1/sync?space=game" in addresses[0]
        assert not SyncState.file_for(paths.root).exists()
        assert server_untouched(server) and len(server.store.list_spaces()) == 1
        assert (docs.directory / "local.md").read_text(encoding="utf-8") == "# Local\n"
    finally:
        await sync.stop()


# ------------------------------------------------------------------ rooms (§16.4)


def editor_scope(person: Studio, path: str, client: str) -> dict[str, Any]:
    """The browser's registered editor lease (I8): an activated room requires one."""
    gate = person.docs.transitions
    lease = gate.register_editor(path, client, gate.backend_epoch, gate.generation)
    return {"editor_id": lease["editor_id"], "backend_epoch": gate.backend_epoch,
            "expected_binding_generation": gate.generation, "client_instance_id": client}


class Browser:
    """The browser's end of ``/ws/collab``, as the backend's handler sees it."""

    def __init__(self, path: str, client_id: int, scope: dict[str, Any]) -> None:
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed: tuple[int, str] | None = None
        self.scope = scope
        self.incoming.put_nowait({"t": "join", "path": path, "client_id": client_id, **scope})

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
    browser = Browser(path, client_id, editor_scope(person, path, f"browser-{client_id}-{uuid.uuid4().hex[:8]}"))
    task = asyncio.create_task(person.sync.serve_collab(browser))  # type: ignore[arg-type]
    return browser, task


def note_scribe_save(person: Studio, browser: Browser, sid: str, path: str, content: str) -> None:
    person.sync.note_live_write(
        path, sid, content, editor_id=browser.scope["editor_id"],
        expected_binding_generation=browser.scope["expected_binding_generation"],
    )


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
    assert joined["user"]["id"] == server.users["alice"]
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
    note_scribe_save(alice, a, joined["sid"], "doc.md", "# Doc together\n")
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

    note_scribe_save(alice, a, joined["sid"], "doc.md", "# Doc, saved on the way out\n")
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
    # A room callback of the stopped connection cannot send through it any more.
    with pytest.raises(ConnectionError):
        await alice.sync._send({"t": "update", "sid": "gone", "update": b64(b"late")})

    late, late_task = open_in_browser(alice, "doc.md", 11)
    await late_task
    assert late.closed == (CLOSE_COLLAB_UNAVAILABLE, "unavailable")


async def test_a_room_survives_nothing_across_a_reconnect_and_reopens_after_it(
    studio: Callable[..., Awaitable[Studio]], server: Server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remote's room sessions end with the socket; the reconnect starts clean."""
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.05)
    alice = await studio("alice")
    await alice.docs.write("doc.md", "# Doc\n")
    await alice.settled()
    a, a_task = open_in_browser(alice, "doc.md", 11)
    joined = await a.expect("joined")
    old_socket = alice.sync._ws
    assert old_socket is not None
    await old_socket.close()
    await a_task
    assert a.closed == (CLOSE_COLLAB_UNAVAILABLE, "offline")
    await eventually(lambda: alice.sync._ws not in (None, old_socket) and alice.sync.live, what="reconnect")
    # The old sid is not a session on the new socket: a late browser frame is refused locally.
    with pytest.raises(sync_service.InvalidCollabSession):
        await alice.sync._send({"t": "update", "sid": joined["sid"], "update": b64(b"late")})
    again, again_task = open_in_browser(alice, "doc.md", 12)
    rejoined = await again.expect("joined")
    assert rejoined["content"] == "# Doc\n" and rejoined["sid"] != joined["sid"]
    again.hang_up()
    await again_task
    await eventually(lambda: not server.hub._spaces["game"].rooms, what="the room to close")


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
        a_joined = await a_ws.join(11, project_id=a_binding.project_id, epoch=a_binding.epoch,
                                   **editor_scope(a, "doc.md", "a-11"))
        assert a_joined["content"] == "# A\n" and not a_joined["seeded"]
        a_doc = pycrdt.Doc()
        a_doc["body"] = pycrdt.Text("A")
        a_ws.send(t="seed", update=b64(a_doc.get_update()))
        await a_ws.expect("seeded")

        b_ws = await stack.enter_async_context(CollabSocket(app, scoped_collab_url(b_binding), b_binding))
        b_joined = await b_ws.join(22, **editor_scope(b, "doc.md", "b-22"))
        assert b_joined["content"] == "# B\n" and not b_joined["seeded"]
        b_doc = pycrdt.Doc()
        b_doc["body"] = pycrdt.Text("B")
        b_ws.send(t="seed", update=b64(b_doc.get_update()))
        await b_ws.expect("seeded")

        legacy_a = await stack.enter_async_context(CollabSocket(app, "/ws/collab"))
        legacy_joined = await legacy_a.join(33, **editor_scope(a, "doc.md", "a-33"))
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
        joined_b = await legacy_b.join(44, **editor_scope(b, "doc.md", "b-44"))
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
            socket.send(t="join", path="doc.md", client_id=11, **{field: value},
                        **editor_scope(person, "doc.md", "conflict"))
        else:
            await socket.join(11, **editor_scope(person, "doc.md", "conflict"))
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
        joined = await socket.join(11, **editor_scope(person, "doc.md", "first"))
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
    assert person.paths_list() == ["doc.md"]

    # Reconnect the actual sync client too: neither local sync state nor the remote
    # may resurrect the old Yjs session. A fresh room must be rebuilt from Markdown.
    await person.sync.stop()
    await person.sync.start()
    await person.connected()
    async with CollabSocket(app, url, binding) as reopened:
        joined = await reopened.join(22, **editor_scope(person, "doc.md", "second"))
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


# ------------------------------------------------------------------ I16: fresh-project v2 slice
#
# remote-identity/runtime-integration-plan §4 I16. Unlike the fixtures above (a synthetic
# confirmed binding written straight to disk), every step here goes through the real
# Studio app and its production routes: Create on the catalog, Select by
# prepare/quiesce/commit, read-only reconcile-preview, Enable, ordinary editor saves,
# rooms, ID change, disable and restart. What is already proven elsewhere is only
# referenced, not repeated: Enable's crash matrix/races (test_remote_enable.py), the
# preview's drift/TTL/copy refusals (test_remote_reconcile_preview.py), every untrusted
# hello variant (test_an_untrusted_hello_blocks_before_any_sync_or_write), and the
# room's own seeding rules (test_scoped_room_open_writes_nothing_and_yjs_is_not_persisted).
#
# Alice and Bob are two people: two roots AND two personal homes. One home may hold only
# one working copy per (URL, ID) - a second root is ``duplicate_local_binding`` by design -
# so each person's requests and every task they spawn see their own
# ``VOXARIUM_STUDIO_HOME`` through a context variable. Each home holds a synthetic,
# already usable profile and stored key; this is NOT the first key-entry onboarding (L/C7).

import contextlib  # noqa: E402
import contextvars  # noqa: E402
import os  # noqa: E402
from collections.abc import MutableMapping  # noqa: E402

import httpx  # noqa: E402

from voxarium_studio import main as studio_main  # noqa: E402
from voxarium_studio.core.paths import HOME_ENV_VAR, user_config_dir  # noqa: E402
from voxarium_studio.explorer.watcher import FileWatcher  # noqa: E402
from voxarium_studio.sync import catalog_runtime  # noqa: E402
from voxarium_studio.sync.catalog_http import CatalogHttpTransport  # noqa: E402
from voxarium_studio.sync.state import SyncNamespace  # noqa: E402

_I16_HOME: contextvars.ContextVar[str | None] = contextvars.ContextVar("i16_home", default=None)
I16_ID = "fresh-game"


class _PersonEnviron(MutableMapping):  # type: ignore[type-arg]
    """``os.environ`` whose Studio home is the person's in whose context code runs."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getitem__(self, key: str) -> str:
        home = _I16_HOME.get()
        if key == HOME_ENV_VAR and home is not None:
            return home
        return self._real[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._real[key] = value

    def __delitem__(self, key: str) -> None:
        del self._real[key]

    def __iter__(self) -> Any:
        return iter(self._real)

    def __len__(self) -> int:
        return len(self._real)

    def copy(self) -> dict[str, str]:
        return dict(self.items())


@contextlib.contextmanager
def as_person(person: Any) -> Any:
    token = _I16_HOME.set(str(person.home))
    try:
        yield person
    finally:
        _I16_HOME.reset(token)


class _PersonTransport(httpx.AsyncBaseTransport):
    """One person's browser: every request (and task it starts) runs in their home."""

    def __init__(self, person: Any, inner: httpx.AsyncBaseTransport) -> None:
        self.person, self.inner = person, inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        with as_person(self.person):
            return await self.inner.handle_async_request(request)


def _i16_hashes(person: Any) -> dict[str, str]:
    """What a read-only command must leave alone: config, private store, journals,
    checkpoints and Markdown."""
    with as_person(person):
        from voxarium_studio.sync.bindings import bindings_file
        roots = [person.root / ".voxarium_studio" / "config.yaml", bindings_file(),
                 user_config_dir() / "remote-changes", user_config_dir() / "sync", person.docs_dir]
    result = {}
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*")) if root.exists() else []
        for path in paths:
            if path.is_file():
                result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _i16_markdown(person: Any) -> dict[str, bytes]:
    return {str(p.relative_to(person.docs_dir)): p.read_bytes()
            for p in sorted(person.docs_dir.rglob("*.md")) if p.is_file()}


def _i16_binding(person: Any) -> Any:
    with as_person(person):
        from voxarium_studio.sync.bindings import RemoteBindings
        return RemoteBindings().read().get(person.root)


@pytest.fixture
async def fresh_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    monkeypatch.setattr(os, "environ", _PersonEnviron(os.environ))
    monkeypatch.setattr(FileWatcher, "start", lambda self: None)
    monkeypatch.setattr(AppContext, "_start_plugin_hooks", lambda self: None)
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.02)
    store = Store(":memory:")
    remote_app = remote.create_app(store=store)
    calls: list[tuple[str, str]] = []

    @remote_app.middleware("http")
    async def observe(request: Any, call_next: Any) -> Any:
        calls.append((request.method, request.url.path))
        return await call_next(request)

    # Catalog reads reach the Remote app over ASGI; the runner's WebSocket reaches the
    # same app over loopback TCP. A fresh owned transport per application lifespan.
    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport",
                        lambda: CatalogHttpTransport(transport=httpx.ASGITransport(app=remote_app)))
    server = uvicorn.Server(uvicorn.Config(remote_app, host="127.0.0.1", port=0,
                                           log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve())
    await eventually(lambda: server.started, what="the remote to listen")
    url = normalise_url(f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}")
    connects: list[tuple[str | None, str]] = []
    real_connect = sync_service.connect

    def counting(address: str, *args: Any, **kwargs: Any) -> Any:
        connects.append((_I16_HOME.get(), address))
        return real_connect(address, *args, **kwargs)

    monkeypatch.setattr(sync_service, "connect", counting)
    people: list[SimpleNamespace] = []

    def person(name: str) -> SimpleNamespace:
        key = new_token()
        user = store.add_user(name.title(), hash_token(key))
        home, root = tmp_path / f"home-{name}", tmp_path / name
        home.mkdir()
        root.mkdir()
        init_project(root)
        row = {"profile_id": str(uuid.uuid5(PROFILES, f"i16-{name}")), "name": name.title(),
               "url": url, "revision": 3}
        (home / "config.yaml").write_text(json.dumps({"remote_servers": [row]}), encoding="utf-8")
        (home / "remote-keys.json").write_text(json.dumps({"version": 2, "profiles": {
            row["profile_id"]: {"url": url, "token": key, "revision": 2}}, "legacy": {}}),
            encoding="utf-8")
        p = SimpleNamespace(name=name, key=key, user=user, home=home, root=root, row=row, stack=None)

        async def open_app() -> None:
            stack = AsyncExitStack()
            with as_person(p):
                app = studio_main.create_app(str(root))
                await stack.enter_async_context(app.router.lifespan_context(app))
            http = await stack.enter_async_context(httpx.AsyncClient(
                transport=_PersonTransport(p, httpx.ASGITransport(app=app)),
                base_url="http://127.0.0.1", trust_env=False))
            scope = (await http.post("/api/projects/open", json={"path": str(root)})).json()
            p.app, p.http, p.scope, p.stack = app, http, scope, stack
            p.ctx = app.state.project_registry[scope["project_id"]].ctx
            p.base = f"/api/projects/{scope['project_id']}/remote"
            p.docs_dir = Path(p.ctx.docs.directory)

        async def close() -> None:
            if p.stack is not None:
                stack, p.stack = p.stack, None
                with as_person(p):
                    await stack.aclose()

        async def restart() -> None:
            await close()
            await open_app()

        p.open_app, p.close, p.restart = open_app, close, restart
        p.connects = lambda: [address for home, address in connects if home == str(p.home)]
        people.append(p)
        return p

    alice, bob = person("alice"), person("bob")
    try:
        for p in (alice, bob):
            await p.open_app()
        yield SimpleNamespace(alice=alice, bob=bob, store=store, url=url, calls=calls,
                              hub=remote_app.state.hub, connects=connects)
    finally:
        for p in people:
            await p.close()
        server.should_exit = True
        await serving
        store.close()


async def _i16_observe(p: Any) -> dict[str, Any]:
    response = await p.http.get(p.base)
    assert response.status_code == 200, response.text
    return response.json()


async def _i16_prepare(p: Any, operation: str, persistent_id: str, **extra: Any) -> dict[str, Any]:
    observation = await _i16_observe(p)
    response = await p.http.post(p.base + "/prepare", json={
        "expected_revision": observation["identity_revision"],
        "config_fingerprint": observation["config_fingerprint"],
        "persistent_id": persistent_id, "operation": operation, **extra})
    assert response.status_code == 200, response.text
    return response.json()


async def _i16_change(p: Any, operation: str, persistent_id: str, **extra: Any) -> dict[str, Any]:
    """prepare -> quiesce -> commit, exactly as Project Settings drives it (§5.2/§7)."""
    prepared = await _i16_prepare(p, operation, persistent_id, **extra)
    change = f"{p.base}/changes/{prepared['change_id']}"
    gate = p.ctx.docs.transitions
    response = await p.http.post(change + "/quiesce", json={"expected_generation": gate.generation})
    assert response.status_code == 200 and response.json()["state"] == "ready", response.text
    observed = gate.expected_change_evidence(prepared["change_id"])["observation"]
    response = await p.http.post(change + "/commit", json={
        "expected_revision": observed["identity_revision"], "expected_generation": gate.generation,
        "config_fingerprint": observed["config_fingerprint"],
        "confirmation": {"url": prepared["to"]["url"], "persistent_id": prepared["to"]["persistent_id"]}})
    assert response.status_code == 200, response.text
    return response.json()


async def _i16_select(p: Any, store: Store) -> dict[str, Any]:
    return await _i16_change(p, "select", I16_ID, profile_id=p.row["profile_id"], url=p.row["url"],
                             catalog_id=I16_ID, catalog_instance_id=store.project(I16_ID).storage_epoch)


async def _i16_preview(p: Any) -> dict[str, Any]:
    observation = await _i16_observe(p)
    response = await p.http.post(p.base + "/reconcile-preview", json={
        "expected_revision": observation["identity_revision"],
        "config_fingerprint": observation["config_fingerprint"]})
    assert response.status_code == 200, response.text
    return response.json()


async def _i16_enable(p: Any, shown: dict[str, Any]) -> dict[str, Any]:
    observation = await _i16_observe(p)
    response = await p.http.post(p.base + "/enable", json={
        "expected_revision": observation["identity_revision"],
        "expected_generation": observation["binding_generation"],
        "preview_id": shown["preview_id"], "fingerprint": shown["fingerprint"],
        "confirmation": {"url": p.row["url"], "persistent_id": I16_ID, "action": "reconcile"}})
    assert response.status_code == 200, response.text
    return response.json()


def _i16_local(p: Any, path: str, text: str) -> None:
    target = p.docs_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def _i16_text(p: Any, path: str) -> str | None:
    target = p.docs_dir / path
    return target.read_text(encoding="utf-8") if target.is_file() else None


def _i16_fence(p: Any) -> dict[str, Any]:
    gate = p.ctx.docs.transitions
    return {"backend_epoch": gate.backend_epoch, "expected_binding_generation": gate.generation}


def _i16_settled(p: Any) -> bool:
    return not p.ctx.sync._dirty.is_set() and not p.ctx.sync._pending


async def _i16_editor_save(p: Any, path: str, content: str) -> dict[str, Any]:
    """An ordinary (non-room) editor: lease, HTTP save under it, close after the save."""
    client = f"editor-{p.name}-{uuid.uuid4().hex[:6]}"
    response = await p.http.post(p.base + "/editors", json={
        **_i16_fence(p), "document_path": path, "client_instance_id": client})
    assert response.status_code == 201, response.text
    editor = response.json()
    response = await p.http.put(f"/api/projects/{p.scope['project_id']}/docs/{path}", json={
        **_i16_fence(p), "editor_id": editor["editor_id"], "version": 1, "content": content})
    assert response.status_code == 200, response.text
    saved = response.json()["last_saved_version"]
    response = await p.http.request("DELETE", f"{p.base}/editors/{editor['editor_id']}", json={
        **_i16_fence(p), "client_instance_id": client, "last_saved_version": saved,
        "version": 1, "dirty": False})
    assert response.status_code == 200, response.text
    return editor


def _i16_room(p: Any, path: str, client_id: int, client: str) -> tuple[Browser, asyncio.Task[None]]:
    """The scoped collab socket's handler with a registered room editor lease (I8)."""
    with as_person(p):
        gate = p.ctx.docs.transitions
        lease = gate.register_editor(path, client, gate.backend_epoch, gate.generation)
        browser = Browser(path, client_id, {
            "editor_id": lease["editor_id"], "backend_epoch": gate.backend_epoch,
            "expected_binding_generation": gate.generation, "client_instance_id": client})
        browser.lease = lease  # type: ignore[attr-defined]
        return browser, asyncio.create_task(p.ctx.sync.serve_collab(browser))  # type: ignore[arg-type]


async def _i16_close_room_lease(p: Any, browser: Browser, sid: str, path: str) -> None:
    editor_id = browser.scope["editor_id"]
    lease = p.ctx.docs.transitions._editors[editor_id]
    captured = {"backend_epoch": browser.scope["backend_epoch"],
                "expected_binding_generation": browser.scope["expected_binding_generation"],
                "document_path": path, "client_instance_id": browser.scope["client_instance_id"],
                "sid": sid, "version": lease.version, "revision": lease.revision}
    url = f"{p.base}/editors/{editor_id}/materialization"
    response = await p.http.post(url, json=captured)
    assert response.status_code == 200, response.text
    response = await p.http.post(url + "/confirm", json={
        **captured, "proof_id": response.json()["proof_id"], "content": _i16_text(p, path)})
    assert response.status_code == 200, response.text
    response = await p.http.request("DELETE", f"{p.base}/editors/{editor_id}", json={
        **{k: v for k, v in captured.items() if k != "revision"},
        "last_saved_version": response.json()["last_saved_version"], "dirty": False})
    assert response.status_code == 200, response.text


async def _i16_enabled_pair(pair: SimpleNamespace) -> tuple[Any, Any]:
    """Create -> Select x2 -> Preview -> Enable, one person after the other."""
    alice, bob, store = pair.alice, pair.bob, pair.store
    _i16_local(alice, "alice.md", "# Alice\n")
    _i16_local(bob, "bob.md", "# Bob\n")
    fresh_ids = {p.name: (await _i16_observe(p))["persistent_id"] for p in (alice, bob)}

    # Create is its own explicit catalog command: it selects, enables and connects nothing.
    before = {p.name: _i16_hashes(p) for p in (alice, bob)}
    response = await alice.http.post(f"/api/remote/servers/{alice.row['profile_id']}/projects", json={
        "expected_revision": alice.row["revision"], "id": I16_ID, "display_name": "Fresh game",
        "operation_id": str(uuid.uuid4())})
    assert response.status_code == 201 and response.json()["state"] == "created", response.text
    assert store.project(I16_ID).seq == 0
    for p in (alice, bob):
        assert _i16_hashes(p) == before[p.name], "Create is not a Select"
        observed = await _i16_observe(p)
        assert observed["persistent_id"] == fresh_ids[p.name] and not observed["enabled"]
    assert pair.connects == []

    # Select: disabled + selected on both roots; still nothing connects.
    for p in (alice, bob):
        result = await _i16_select(p, store)
        assert result["persistent_id"] == I16_ID and not result["enabled"]
        assert result["identity_revision"] == 2 and result["state"] == "disabled"
        row = _i16_binding(p)
        assert (row.selection_state, row.persistent_id, row.space_instance_id) == (
            "selected", I16_ID, store.project(I16_ID).storage_epoch)
    assert pair.connects == [] and alice.ctx.sync._runner is None and bob.ctx.sync._runner is None

    # Preview is read-only (0 writes, GETs only), then Enable connects and catches up.
    for p, counts in ((alice, {"upload": 1, "download": 0}), (bob, {"upload": 1, "download": 1})):
        before_preview = _i16_hashes(p)
        pair.calls.clear()
        shown = await _i16_preview(p)
        assert {k: shown["counts"][k] for k in counts} == counts, shown["counts"]
        assert shown["counts"]["conflict"] == 0
        assert _i16_hashes(p) == before_preview, "preview writes nothing"
        assert pair.calls and all(method == "GET" for method, _ in pair.calls), pair.calls
        assert p.connects() == [] and p.ctx.sync._runner is None
        result = await _i16_enable(p, shown)
        assert result["enabled"] and result["identity_revision"] == 3
        assert _i16_binding(p).selection_state == "confirmed"
        await eventually(lambda p=p: p.ctx.sync.live, what=f"{p.name} to connect")
        await eventually(lambda p=p: _i16_settled(p), what=f"{p.name} to settle")
    await eventually(lambda: _i16_text(alice, "bob.md") == "# Bob\n", what="bob's file at alice")
    assert _i16_text(bob, "alice.md") == "# Alice\n"
    assert pair.connects and all(f"/api/v2/sync?space={I16_ID}" in a for _, a in pair.connects)
    assert not any("/api/v1/" in a for _, a in pair.connects)
    return alice, bob


async def test_i16_fresh_project_create_select_enable_edit_rooms_id_disable_restart(
    fresh_pair: SimpleNamespace,
) -> None:
    pair = fresh_pair
    alice, bob = await _i16_enabled_pair(pair)
    store, hub = pair.store, pair.hub

    # --- an ordinary editor save from one root reaches the other
    await _i16_editor_save(alice, "alice.md", "# Alice\n\nSaved in the editor.\n")
    await eventually(lambda: _i16_text(bob, "alice.md") == "# Alice\n\nSaved in the editor.\n",
                     what="alice's editor save at bob")

    # --- a clean room open writes nothing, and its Yjs state is persisted nowhere
    await eventually(lambda: _i16_settled(bob), what="bob to settle")
    file = bob.docs_dir / "alice.md"
    stat, markdown, stored = file.stat(), _i16_markdown(bob), store.doc(I16_ID, "alice.md")
    writes: list[Any] = []
    real_write = bob.ctx.docs.write

    async def counted_write(*args: Any, **kwargs: Any) -> Any:
        writes.append(args)
        return await real_write(*args, **kwargs)

    bob.ctx.docs.write = counted_write
    marker = f"i16-yjs-only-{uuid.uuid4().hex}"
    browser, task = _i16_room(bob, "alice.md", 31, "room-bob")
    joined = await browser.expect("joined")
    assert joined["content"] == "# Alice\n\nSaved in the editor.\n" and not joined["dirty"]
    session_doc = pycrdt.Doc()
    session_doc["body"] = pycrdt.Text(marker)
    browser.send(t="seed", update=b64(session_doc.get_update()))
    await browser.expect("seeded")
    # Leaving cleanly, as the browser does (I8): prove the unedited snapshot, then close
    # the lease. A lease left open — or a bare disconnect — would rightly block every
    # later drain as pending_edits.
    await _i16_close_room_lease(bob, browser, joined["sid"], "alice.md")
    browser.hang_up()
    await task
    await eventually(lambda: not hub._spaces[I16_ID].rooms, what="the room to empty")
    await asyncio.sleep(0.1)
    del bob.ctx.docs.write
    assert writes == [], "opening a room is not a DocStore write"
    assert _i16_markdown(bob) == markdown and file.stat().st_mtime_ns == stat.st_mtime_ns
    assert store.doc(I16_ID, "alice.md") == stored, "the room did not bump the Markdown revision"
    for scanned in (bob.root, bob.home, alice.root, alice.home):
        for path in scanned.rglob("*"):
            if path.is_file():
                assert marker.encode() not in path.read_bytes(), f"Yjs state persisted in {path}"

    # --- an agent edit concurrent with an ordinary save: a conflict copy, never a loss
    await _i16_editor_save(alice, "race.md", "# Race base\n")
    await eventually(lambda: _i16_text(bob, "race.md") == "# Race base\n", what="race base at bob")
    await eventually(lambda: _i16_settled(bob), what="bob to settle")
    release, held = asyncio.Event(), asyncio.Event()
    real_on_change = bob.ctx.sync._on_change

    async def delayed(message: dict[str, Any]) -> None:
        if message.get("path") == "race.md":
            held.set()
            await release.wait()
        await real_on_change(message)

    bob.ctx.sync._on_change = delayed  # Bob has not applied Alice's save yet...
    await _i16_editor_save(alice, "race.md", "# Race: alice's editor\n")
    await asyncio.wait_for(held.wait(), 5)
    with as_person(bob):  # ...when Bob's agent writes the same document from the old base.
        await bob.ctx.docs.write("race.md", "# Race: bob's agent\n", author="agent:i16")
    release.set()

    def both_kept(p: Any) -> bool:
        copies = sorted(p.docs_dir.glob("race (conflict*).md"))
        return (_i16_text(p, "race.md") == "# Race: alice's editor\n" and len(copies) == 1
                and copies[0].read_text(encoding="utf-8") == "# Race: bob's agent\n")

    await eventually(lambda: both_kept(bob), what="bob to keep both versions")
    await eventually(lambda: both_kept(alice), what="the conflict copy to reach alice")
    del bob.ctx.sync._on_change
    conflicts = [e.data["path"] for e in bob.ctx.bus.replay(0) if e.type == EventType.SYNC_CONFLICT]
    assert conflicts == ["race.md"]

    # --- an ID change of an ENABLED project is never silent: prepare writes nothing; the
    # commit needs the explicit drain/fence and lands disabled with no selection. The
    # Remote project is neither renamed, created nor emptied.
    await eventually(lambda: _i16_settled(bob), what="bob to settle")
    before_prepare = _i16_hashes(bob)
    prepared = await _i16_prepare(bob, "edit_id", "bob-renamed")
    assert prepared["plan"] == {"operation": "edit_id", "enabled": False}
    assert _i16_hashes(bob) == before_prepare and bob.ctx.sync.live, "prepare alone changes nothing"
    response = await bob.http.delete(f"{bob.base}/changes/{prepared['change_id']}")
    assert response.status_code == 200, response.text
    assert bob.ctx.sync.live and _i16_hashes(bob) == before_prepare, "cancel before fence resumes"
    bob_markdown = _i16_markdown(bob)
    server_bob = store.doc(I16_ID, "bob.md")
    result = await _i16_change(bob, "edit_id", "bob-renamed")
    assert (result["persistent_id"], result["enabled"], result["identity_revision"]) == (
        "bob-renamed", False, 4)
    assert _i16_binding(bob) is None, "edit_id clears the local selection"
    assert not bob.ctx.sync.live and bob.ctx.sync.status()["state"] == "disabled"
    with pytest.raises(LookupError):
        store.project("bob-renamed")
    assert store.doc(I16_ID, "bob.md") == server_bob, "old server documents stay"
    assert _i16_markdown(bob) == bob_markdown

    # --- disable: disabled + still selected, rooms closed, Markdown intact
    await eventually(lambda: _i16_settled(alice), what="alice to settle")
    old_fence = _i16_fence(alice)
    response = await alice.http.post(alice.base + "/editors", json={
        **old_fence, "document_path": "alice.md", "client_instance_id": "late-editor"})
    assert response.status_code == 201, response.text
    late = response.json()
    response = await alice.http.request("DELETE", f"{alice.base}/editors/{late['editor_id']}",
                                        json={**old_fence, "client_instance_id": "late-editor",
                                              "last_saved_version": late["last_saved_version"]})
    assert response.status_code == 200, response.text
    alice_markdown = _i16_markdown(alice)
    result = await _i16_change(alice, "disable", I16_ID)
    assert (result["persistent_id"], result["enabled"], result["identity_revision"]) == (I16_ID, False, 4)
    assert _i16_binding(alice).selection_state == "selected"
    assert not alice.ctx.sync.live and alice.ctx.sync.status()["state"] == "disabled"
    space = hub._spaces.get(I16_ID)
    assert alice.ctx.sync._sessions == {} and (space is None or not space.rooms)
    refused_room, task = _i16_room(alice, "alice.md", 41, "after-disable")
    await task
    assert refused_room.closed == (CLOSE_COLLAB_UNAVAILABLE, "unavailable"), "no rooms while disabled"
    # A late callback of the old generation (a save from before the fence) writes nothing.
    response = await alice.http.put(f"/api/projects/{alice.scope['project_id']}/docs/alice.md", json={
        **old_fence, "editor_id": late["editor_id"], "version": 2, "content": "# Late\n"})
    assert response.status_code == 409, response.text
    assert _i16_markdown(alice) == alice_markdown

    # --- restart: each root stays exactly as it was; nothing auto-enables or reconnects
    connected_before = len(pair.connects)
    from voxarium_studio.sync import credentials as credential_owner
    key_reads: list[str] = []
    real_load = credential_owner.load_profile_token

    def counted_load(profile_id: str, url: str) -> str | None:
        key_reads.append(profile_id)
        return real_load(profile_id, url)

    credential_owner.load_profile_token = counted_load
    try:
        for p, expected_id, selection in ((alice, I16_ID, "selected"), (bob, "bob-renamed", None)):
            markdown = _i16_markdown(p)
            await p.restart()
            await asyncio.sleep(0.2)
            observed = await _i16_observe(p)
            assert (observed["persistent_id"], observed["enabled"]) == (expected_id, False), observed
            # The start classifier still refuses a runner (core/context.py, fail-closed),
            # but GET reports the settled disabled commit as "disabled", not damage.
            assert (observed["state"], observed["reason"]) == ("disabled", None), observed
            assert p.ctx.sync.status()["state"] == "blocked", "the runner itself stays refused"
            row = _i16_binding(p)
            assert (row.selection_state if row else None) == selection
            assert _i16_markdown(p) == markdown and p.ctx.sync._runner is None
        assert len(pair.connects) == connected_before
        assert key_reads == [], "a restarted disabled root reads no key"
    finally:
        credential_owner.load_profile_token = real_load

    # --- after the restart the explicit path back still works: Preview -> Enable
    shown = await _i16_preview(alice)
    assert len(pair.connects) == connected_before, "preview opens no socket"
    enabled = await _i16_enable(alice, shown)
    assert (enabled["persistent_id"], enabled["enabled"]) == (I16_ID, True), enabled
    await eventually(lambda: alice.ctx.sync.live, what="alice to reconnect after re-enable")
    observed = await _i16_observe(alice)
    assert (observed["state"], observed["enabled"]) == ("connected", True), observed


async def test_i16_enabled_restart_stays_enabled_and_rollback_hello_blocks_without_reset(
    fresh_pair: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = fresh_pair
    alice, bob = await _i16_enabled_pair(pair)
    await _i16_editor_save(alice, "after.md", "# After\n")
    await eventually(lambda: _i16_text(bob, "after.md") == "# After\n", what="after.md at bob")
    await eventually(lambda: _i16_settled(bob), what="bob to settle")

    # A completed Enable journal is the restart authority: the rebuilt app reconnects.
    await bob.restart()
    await eventually(lambda: bob.ctx.sync.live, what="bob to reconnect after restart")
    observed = await _i16_observe(bob)
    assert observed["enabled"] and observed["state"] == "connected" and observed["identity_revision"] == 3
    await eventually(lambda: _i16_settled(bob), what="bob to settle")

    # The Remote now answers with a hello BELOW the saved cursor (a restored database):
    # blocked, no retry, and neither cursor/bases nor Markdown are reset.
    await bob.close()
    with as_person(bob):
        state_file = SyncNamespace.for_project(bob.root, url=pair.url, persistent_id=I16_ID).path
    checkpoint = state_file.read_bytes()
    assert json.loads(checkpoint)["cursor"] > 0
    markdown = _i16_markdown(bob)
    rewrite_hello(monkeypatch, _set(("seq",), 0))
    before = len(bob.connects())
    await bob.open_app()
    await eventually(lambda: bob.ctx.sync._runner is not None and bob.ctx.sync._runner.done(),
                     what="the terminal stop")
    status = bob.ctx.sync.status()
    assert (status["state"], status["message"]) == ("blocked", "cursor_rollback")
    await asyncio.sleep(0.2)
    assert len(bob.connects()) == before + 1, "a rollback hello is not retried"
    assert state_file.read_bytes() == checkpoint
    assert _i16_markdown(bob) == markdown and not bob.ctx.sync._sessions
    observed = await _i16_observe(bob)
    assert observed["state"] == "blocked" and observed["reason"] == "cursor_rollback"
    assert observed["enabled"], "blocked, not silently disabled or reset"
    assert alice.ctx.sync.live, "alice is untouched by bob's block"


# ------------------------------------------------------------------ VS-188: I5/I6 records → recovery
#
# A disabled I6 change (Disable, ID edit, Select) or a drain whose change was
# cancelled after its fence leaves an unresolved record; before VS-188 the root
# was blocked forever. Startup and GET only classify it; the explicit
# POST …/recover returns the root to its previous destination with sync off,
# or finishes a disabled commit already past its commit point. The I12 Enable
# matrix is in test_remote_enable.py; this file reuses its real-Remote studio.

from test_remote_enable import (  # noqa: E402
    Crash as _Crash, binding_row as _row, disk_config as _disk_config, enable as _enable,
    enable_body as _enable_body, enable_studio as _enable_studio, eventually as _eventually,
    journals as _journals,
    journal_files as _journal_files, local as _local, recover as _recover,
    recovery_body as _recovery_body, refused as _refused, selected as _selected, state as _state,
)


async def _vs188_enabled(s: Any) -> None:
    await _selected(s)
    response = await _enable(s, await _enable_body(s))
    assert response.status_code == 200, response.text
    await _eventually(lambda: s.ctx.sync.live, what="connected")


async def _vs188_quiesced(s: Any, operation: str, persistent_id: str,
                          **extra: Any) -> tuple[str, dict[str, Any]]:
    """prepare → quiesce (ready, fenced); returns the commit URL and its exact body."""
    observed = (await s.http.get(s.base)).json()
    response = await s.http.post(s.base + "/prepare", json={
        "expected_revision": observed["identity_revision"],
        "config_fingerprint": observed["config_fingerprint"],
        "persistent_id": persistent_id, "operation": operation, **extra})
    assert response.status_code == 200, response.text
    prepared = response.json()
    change = f"{s.base}/changes/{prepared['change_id']}"
    gate = s.ctx.docs.transitions
    response = await s.http.post(change + "/quiesce", json={"expected_generation": gate.generation})
    assert response.status_code == 200 and response.json()["state"] == "ready", response.text
    return change, {
        "expected_revision": observed["identity_revision"], "expected_generation": gate.generation,
        "config_fingerprint": observed["config_fingerprint"],
        "confirmation": {"url": prepared["to"]["url"], "persistent_id": prepared["to"]["persistent_id"]}}


# (owner, phase, when) of the I6 commit's publications.
_VS188_I6_POINTS = [
    ("journal", "committing", "before"), ("journal", "committing", "after"),
    ("config", None, "after"), ("journal", "config_committed", "after"),
    ("private", None, "after"), ("journal", "completed", "before"),
]


def _vs188_inject(mp: pytest.MonkeyPatch, point: tuple) -> None:
    from voxarium_studio.core import config as config_owner
    from voxarium_studio.sync import transitions
    from voxarium_studio.sync.bindings import RemoteBindings

    owner, phase, when = point
    if owner == "journal":
        original = transitions.publish_binding_change_commit

        def journal(root, change_id, expected, commit, current):
            if current == phase and when == "before":
                raise _Crash()
            row = original(root, change_id, expected, commit, current)
            if current == phase:
                raise _Crash()
            return row

        mp.setattr(transitions, "publish_binding_change_commit", journal)
    elif owner == "config":
        original_write = config_owner._write_project_document_in_transaction

        def write(paths, payload, **kwargs):
            original_write(paths, payload, **kwargs)
            raise _Crash()

        mp.setattr(config_owner, "_write_project_document_in_transaction", write)
    else:
        original_save = RemoteBindings._save_in_transaction
        original_remove = RemoteBindings._remove_in_transaction

        def save(self, record, **kwargs):
            original_save(self, record, **kwargs)
            raise _Crash()

        def remove(self, root, **kwargs):
            original_remove(self, root, **kwargs)
            raise _Crash()

        mp.setattr(RemoteBindings, "_save_in_transaction", save)
        mp.setattr(RemoteBindings, "_remove_in_transaction", remove)


async def _vs188_select(s: Any, persistent_id: str, storage_epoch: str) -> None:
    """An explicit Select of another catalog project (I6/I9), committed disabled."""
    change, body = await _vs188_quiesced(
        s, "select", persistent_id, profile_id=s.row["profile_id"], url=s.url,
        catalog_id=persistent_id, catalog_instance_id=storage_epoch)
    response = await s.http.post(change + "/commit", json=body)
    assert response.status_code == 200, response.text


async def _vs188_works(s: Any, persistent_id: str, path: str) -> None:
    """Working state on the real Remote: Preview → Enable → catch-up of a local file."""
    _local(s, path, "# " + path + "\n")
    response = await _enable(s, await _vs188_enable_body(s, persistent_id))
    assert response.status_code == 200, response.text
    await _eventually(lambda: s.ctx.sync.live, what="connected again")
    await _eventually(lambda: s.store.doc(persistent_id, path) is not None, what="upload")


async def _vs188_enable_body(s: Any, persistent_id: str) -> dict[str, Any]:
    observation = (await s.http.get(s.base)).json()
    response = await s.http.post(s.base + "/reconcile-preview", json={
        "expected_revision": observation["identity_revision"],
        "config_fingerprint": observation["config_fingerprint"]})
    assert response.status_code == 200, response.text
    shown = response.json()
    observation = (await s.http.get(s.base)).json()
    return {"expected_revision": observation["identity_revision"],
            "expected_generation": observation["binding_generation"],
            "preview_id": shown["preview_id"], "fingerprint": shown["fingerprint"],
            "confirmation": {"url": s.url, "persistent_id": persistent_id, "action": "reconcile"}}


@pytest.mark.parametrize("operation", ["disable", "edit_id"])
@pytest.mark.parametrize("point", _VS188_I6_POINTS, ids=lambda p: "-".join(x for x in p if x))
async def test_vs188_i6_crash_matrix_recovers_explicitly(project, monkeypatch, studio_home,
                                                         operation, point):
    async with _enable_studio(project, monkeypatch, studio_home) as s:
        await _vs188_enabled(s)
        target = "selected-id" if operation == "disable" else "renamed-id"
        change, body = await _vs188_quiesced(s, operation, target)
        with pytest.MonkeyPatch.context() as mp:
            _vs188_inject(mp, point)
            with pytest.raises(_Crash):
                await s.http.post(change + "/commit", json=body)
        s.connects.clear()
        await s.restart()
        await asyncio.sleep(0.1)
        # Startup classified only: blocked, no runner, no key lookup, no socket.
        assert s.connects == [] and s.ctx.sync._runner is None
        settled_disk = _state(s)
        observed = (await s.http.get(s.base)).json()
        for _ in range(3):
            assert (await s.http.get(s.base)).json()["recovery"] == observed["recovery"]
        assert _state(s) == settled_disk, "GET never repairs"
        assert (observed["state"], observed["reason"]) == ("blocked", "recovery_required")
        shown = observed["recovery"]
        published = _VS188_I6_POINTS.index(point) >= 2  # the project config is the commit point
        assert shown["available"] and shown["operation"] == operation
        assert shown["outcome"] == ("commit" if published else "previous")
        expected_id = target if published else "selected-id"
        assert shown["target"] == {"persistent_id": expected_id, "url": s.url,
                                   "identity_revision": 10, "enabled": False}
        # Exact confirmation only; refusals write nothing.
        recovery = _recovery_body(observed)
        wrong = json.loads(json.dumps(recovery))
        wrong["confirmation"]["persistent_id"] = "renamed-id" if expected_id == "selected-id" else "selected-id"
        _refused(await _recover(s, wrong), 409, "confirmation_required")
        _refused(await _recover(s, {**recovery, "config_fingerprint": "f" * 64}), 409, "stale_binding")
        assert _state(s) == settled_disk
        files = _journal_files(s)
        response = await _recover(s, recovery)
        assert response.status_code == 200, response.text
        result = response.json()
        assert (result["outcome"], result["persistent_id"], result["identity_revision"],
                result["enabled"], result["state"]) == (shown["outcome"], expected_id, 10, False, "disabled")
        assert _disk_config(s)["project_identity"] == {"id": expected_id, "revision": 10}
        assert _disk_config(s)["sync"] == {"enabled": False, "url": s.url}
        row = _row(s)
        if published and operation == "edit_id":
            assert row is None, "the committed ID edit removed the private selection"
        else:
            assert (row.persistent_id, row.selection_state, row.identity_revision) == (
                "selected-id", "selected", 10)
        assert _journal_files(s) == files, "records are advanced in place, never deleted"
        # Idempotent repeat; a restart stays settled and sync-off.
        after = _state(s)
        again = await _recover(s, recovery)
        assert again.status_code == 200, again.text
        assert _state(s) == after
        await s.restart()
        await asyncio.sleep(0.1)
        observed = (await s.http.get(s.base)).json()
        assert observed["recovery"] is None and not observed["enabled"]
        assert s.connects == [] and _state(s) == after
        # Working again on the real Remote.
        if expected_id == "renamed-id":
            created = s.project("renamed-id", "Renamed")
            await _vs188_select(s, "renamed-id", created.storage_epoch)
        await _vs188_works(s, expected_id, "after-recovery.md")


@pytest.mark.parametrize("restart", [False, True], ids=["in-process", "after-restart"])
async def test_vs188_cancel_after_fence_is_recoverable(project, monkeypatch, studio_home, restart):
    async with _enable_studio(project, monkeypatch, studio_home) as s:
        await _vs188_enabled(s)
        _local(s, "kept.md", "# kept\n")
        await _eventually(lambda: s.store.doc("selected-id", "kept.md") is not None, what="upload")
        change, _ = await _vs188_quiesced(s, "disable", "selected-id")
        response = await s.http.delete(change)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "paused"
        # The drain left a version-1 record: before VS-188 this was permanent.
        records = [row for row in _journals(s) or () if row["version"] == 1]
        assert len(records) == 1 and records[0]["phase"] == "draining"
        if restart:
            s.connects.clear()
            await s.restart()
            await asyncio.sleep(0.1)
            assert s.connects == []
        observed = (await s.http.get(s.base)).json()
        assert observed["state"] == "blocked"
        shown = observed["recovery"]
        assert shown["available"] and shown["outcome"] == "previous" and shown["operation"] == "disable"
        assert shown["target"] == {"persistent_id": "selected-id", "url": s.url,
                                   "identity_revision": 10, "enabled": False}
        _local(s, "kept.md", "# edited while blocked\n")
        response = await _recover(s, _recovery_body(observed))
        assert response.status_code == 200, response.text
        assert _disk_config(s)["sync"] == {"enabled": False, "url": s.url}
        assert _row(s).selection_state == "selected" and _row(s).identity_revision == 10
        assert s.store.doc("selected-id", "kept.md").content == "# kept\n", "recovery never pushes"
        assert not s.ctx.docs.transitions.binding_quiescing
        observed = (await s.http.get(s.base)).json()
        assert (observed["state"], observed["recovery"]) == ("disabled", None)
        # The kept edit and the old checkpoint reconcile through Preview → Enable.
        await _vs188_works(s, "selected-id", "next.md")
        await _eventually(lambda: s.store.doc("selected-id", "kept.md").content
                          == "# edited while blocked\n", what="the edit made while blocked")


async def test_vs188_select_crash_after_config_finishes_the_select(project, monkeypatch, studio_home):
    async with _enable_studio(project, monkeypatch, studio_home) as s:
        await _selected(s)
        other = s.project("other-id", "Other")
        change, body = await _vs188_quiesced(
            s, "select", "other-id", profile_id=s.row["profile_id"], url=s.url,
            catalog_id="other-id", catalog_instance_id=other.storage_epoch)
        with pytest.MonkeyPatch.context() as mp:
            _vs188_inject(mp, ("config", None, "after"))
            with pytest.raises(_Crash):
                await s.http.post(change + "/commit", json=body)
        await s.restart()
        observed = (await s.http.get(s.base)).json()
        shown = observed["recovery"]
        assert (shown["operation"], shown["outcome"]) == ("select", "commit")
        assert shown["target"]["persistent_id"] == "other-id"
        response = await _recover(s, _recovery_body(observed))
        assert response.status_code == 200, response.text
        row = _row(s)
        assert (row.persistent_id, row.selection_state, row.space_instance_id, row.identity_revision) == (
            "other-id", "selected", other.storage_epoch, 9)
        await _vs188_works(s, "other-id", "selected-other.md")
