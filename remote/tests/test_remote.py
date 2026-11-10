"""The remote's protocol, frame by frame (dev spec §16).

Each test is a conversation: the frames a local Studio would send, and the ones it must get
back. Two clients on one in-memory store stand in for two people's machines.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pycrdt import Doc, Text
from starlette.websockets import WebSocketDisconnect

from voxarium_studio_remote.app import create_app
from voxarium_studio_remote.auth import hash_token, new_token
from voxarium_studio_remote.protocol import (
    CLOSE_BAD_REQUEST,
    CLOSE_UNAUTHORISED,
    PROTOCOL_VERSION,
    content_hash,
)
from voxarium_studio_remote.store import Store, UnknownUser, UserExists


@pytest.fixture
def store() -> Iterator[Store]:
    store = Store(":memory:")
    yield store
    store.close()


@pytest.fixture
def client(store: Store) -> Iterator[TestClient]:
    with TestClient(create_app(store=store)) as client:
        yield client


def issue(store: Store, name: str) -> str:
    token = new_token()
    store.add_user(name, hash_token(token))
    return token


class Peer:
    """One Studio's end of the socket, with the frames nobody asked about skipped."""

    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self.hello = self.expect("hello")

    def send(self, **message: Any) -> None:
        self.socket.send_json(message)

    def expect(self, kind: str) -> dict[str, Any]:
        """The next frame of *kind*. Presence is skipped unless it is what was asked for."""
        while True:
            message = self.socket.receive_json()
            if message["t"] == kind:
                return message
            assert message["t"] == "presence", f"expected {kind}, got {message}"

    def sync(self, since: int = 0) -> list[dict[str, Any]]:
        self.send(t="sync", since=since)
        changes = []
        while True:
            message = self.socket.receive_json()
            if message["t"] == "synced":
                return changes
            if message["t"] == "change":
                changes.append(message)


@contextmanager
def connect(client: TestClient, token: str, space: str = "game") -> Iterator[Peer]:
    with client.websocket_connect(
        f"/api/v1/sync?space={space}", headers={"Authorization": f"Bearer {token}"}
    ) as socket:
        yield Peer(socket)


def update_of(text: str) -> str:
    doc = Doc()
    doc["body"] = Text(text)
    return base64.b64encode(doc.get_update()).decode()


def text_of(*updates: str) -> str:
    doc = Doc()
    doc["body"] = Text()
    for update in updates:
        doc.apply_update(base64.b64decode(update))
    return str(doc["body"])


# ------------------------------------------------------------------ users and keys


def test_a_key_is_stored_only_as_its_hash(store: Store) -> None:
    token = issue(store, "alice")
    raw = store._db.execute("SELECT token_hash FROM users").fetchone()[0]
    assert token not in raw and raw == hash_token(token)
    with pytest.raises(UserExists):
        store.add_user("alice", hash_token(new_token()))


def test_identity_endpoint_judges_a_key(client: TestClient, store: Store) -> None:
    token = issue(store, "alice")
    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    # A key in the query string is not a key: it would sit in every proxy log on the way.
    assert client.get(f"/api/v1/me?token={token}").status_code == 401
    answer = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert answer.status_code == 200 and answer.json()["user"]["name"] == "alice"
    assert client.get("/healthz").json()["protocol"] == PROTOCOL_VERSION


def test_a_bad_key_is_closed_with_its_own_code(client: TestClient, store: Store) -> None:
    with client.websocket_connect("/api/v1/sync?space=game") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == CLOSE_UNAUTHORISED

    token = issue(store, "alice")
    with client.websocket_connect(
        "/api/v1/sync?space=../etc", headers={"Authorization": f"Bearer {token}"}
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == CLOSE_BAD_REQUEST


def test_revoking_and_rotating_a_key(client: TestClient, store: Store) -> None:
    token = issue(store, "alice")
    store.revoke_user("alice")
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    with pytest.raises(UnknownUser):
        store.revoke_user("alice")

    fresh = new_token()
    store.set_token_hash("alice", hash_token(fresh))
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {fresh}"}).status_code == 200


# ------------------------------------------------------------------ documents (§16.3)


def test_a_write_reaches_the_other_studio_and_a_late_one(client: TestClient, store: Store) -> None:
    alice, bob = issue(store, "alice"), issue(store, "bob")
    with connect(client, alice) as a, connect(client, bob) as b:
        assert a.sync() == [] and b.sync() == []
        a.send(t="put", id=1, path="design/dash.md", content="# Dash\n", base_rev=0)
        ack = a.expect("ack")
        assert ack["id"] == 1 and ack["rev"] == 1 and ack["hash"] == content_hash("# Dash\n")

        change = b.expect("change")
        assert change["path"] == "design/dash.md" and change["content"] == "# Dash\n"
        assert change["rev"] == 1 and change["by"]["name"] == "alice"

    # Somebody who was not connected learns it from their cursor, with nothing to replay.
    with connect(client, bob) as late:
        assert [c["path"] for c in late.sync(since=0)] == ["design/dash.md"]
        assert late.sync(since=1) == []


def test_a_write_from_a_stale_revision_is_refused_with_what_is_there(
    client: TestClient, store: Store
) -> None:
    alice, bob = issue(store, "alice"), issue(store, "bob")
    with connect(client, alice) as a, connect(client, bob) as b:
        a.send(t="put", id=1, path="notes.md", content="one\n", base_rev=0)
        assert a.expect("ack")["rev"] == 1
        b.expect("change")

        # Both edit revision 1; Alice lands first.
        a.send(t="put", id=2, path="notes.md", content="alice\n", base_rev=1)
        assert a.expect("ack")["rev"] == 2
        b.expect("change")
        b.send(t="put", id=7, path="notes.md", content="bob\n", base_rev=1)
        nack = b.expect("nack")
        assert nack["id"] == 7 and nack["current"]["rev"] == 2
        assert nack["current"]["content"] == "alice\n"
        assert store.doc(a.hello["space"]["id"], "notes.md").content == "alice\n"

        # The same text is not a conflict, whatever revision it was written from.
        b.send(t="put", id=8, path="notes.md", content="alice\n", base_rev=1)
        assert b.expect("ack")["rev"] == 2


def test_a_delete_leaves_a_tombstone_and_a_write_brings_the_document_back(
    client: TestClient, store: Store
) -> None:
    alice, bob = issue(store, "alice"), issue(store, "bob")
    with connect(client, alice) as a, connect(client, bob) as b:
        a.send(t="put", id=1, path="old.md", content="x\n", base_rev=0)
        a.expect("ack")
        b.expect("change")
        b.send(t="delete", id=2, path="old.md", base_rev=0)
        assert b.expect("nack")["current"]["rev"] == 1
        b.send(t="delete", id=3, path="old.md", base_rev=1)
        gone = b.expect("ack")
        assert gone["deleted"] is True and gone["rev"] == 2
        assert a.expect("change")["deleted"] is True

    with connect(client, alice) as late:
        (tombstone,) = late.sync(since=1)
        assert tombstone["deleted"] is True and tombstone["content"] is None
        late.send(t="put", id=4, path="old.md", content="back\n", base_rev=2)
        assert late.expect("ack")["rev"] == 3


def test_paths_and_sizes_are_the_studios_own_bounds(client: TestClient, store: Store) -> None:
    with connect(client, issue(store, "alice")) as a:
        for path in ("../escape.md", "/abs.md", "a\\b.md", "script.py", "a//b.md", ""):
            a.send(t="put", id=1, path=path, content="x", base_rev=0)
            assert a.expect("error")["of"] == "put", path
        a.send(t="put", id=2, path="big.md", content="x" * 1_000_001, base_rev=0)
        assert "exceeds" in a.expect("error")["message"]
        a.send(t="nonsense")
        assert "Unknown" in a.expect("error")["message"]
        assert a.sync() == []


def test_spaces_do_not_see_each_other(client: TestClient, store: Store) -> None:
    token = issue(store, "alice")
    with connect(client, token, "game") as game, connect(client, token, "tools") as tools:
        assert game.hello["space"]["id"] != tools.hello["space"]["id"]
        game.send(t="put", id=1, path="a.md", content="a\n", base_rev=0)
        game.expect("ack")
        assert tools.sync() == []


# ------------------------------------------------------------------ rooms (§16.4)


def open_document(peer: Peer, path: str = "doc.md", content: str = "# Doc\n") -> int:
    peer.send(t="put", id=99, path=path, content=content, base_rev=0)
    return peer.expect("ack")["rev"]


def test_the_first_editor_seeds_the_room_and_the_second_is_handed_it(
    client: TestClient, store: Store
) -> None:
    alice, bob = issue(store, "alice"), issue(store, "bob")
    with connect(client, alice) as a, connect(client, bob) as b:
        rev = open_document(a)
        b.expect("change")

        a.send(t="join", sid="a1", path="doc.md", client_id=11, base_rev=rev)
        joined = a.expect("joined")
        assert joined["seeded"] is False and joined["scribe"] is True and joined["state"] is None

        a.send(t="seed", sid="a1", update=update_of("hello"))
        assert a.expect("seeded")["ok"] is True

        b.send(t="join", sid="b1", path="doc.md", client_id=22, base_rev=rev)
        second = b.expect("joined")
        assert second["seeded"] is True and second["scribe"] is False
        assert second["epoch"] == joined["epoch"]
        assert text_of(second["state"]) == "hello"
        # Alice is asked to publish her cursor again, for the newcomer.
        assert a.expect("peer_joined")["sid"] == "a1"


def test_two_simultaneous_seeds_do_not_duplicate_the_document(
    client: TestClient, store: Store
) -> None:
    alice, bob = issue(store, "alice"), issue(store, "bob")
    with connect(client, alice) as a, connect(client, bob) as b:
        rev = open_document(a)
        b.expect("change")
        # Both arrive before either has seeded, so both are told to.
        a.send(t="join", sid="a1", path="doc.md", client_id=11, base_rev=rev)
        b.send(t="join", sid="b1", path="doc.md", client_id=22, base_rev=rev)
        assert a.expect("joined")["seeded"] is False
        assert b.expect("joined")["seeded"] is False

        a.send(t="seed", sid="a1", update=update_of("hello"))
        assert a.expect("seeded")["ok"] is True
        b.send(t="seed", sid="b1", update=update_of("hello"))
        lost = b.expect("seeded")
        assert lost["ok"] is False
        assert text_of(lost["state"]) == "hello", "the winner's text once, not twice"


def test_edits_and_cursors_are_relayed_to_the_rest_of_the_room(
    client: TestClient, store: Store
) -> None:
    alice, bob = issue(store, "alice"), issue(store, "bob")
    with connect(client, alice) as a, connect(client, bob) as b:
        rev = open_document(a)
        b.expect("change")
        a.send(t="join", sid="a1", path="doc.md", client_id=11, base_rev=rev)
        a.expect("joined")

        seed = Doc()
        seed["body"] = Text("hello")
        seed_update = base64.b64encode(seed.get_update()).decode()
        a.send(t="seed", sid="a1", update=seed_update)
        a.expect("seeded")
        b.send(t="join", sid="b1", path="doc.md", client_id=22, base_rev=rev)
        state = b.expect("joined")["state"]
        a.expect("peer_joined")

        # Bob types; Alice receives exactly his update, addressed to her session.
        bobs = Doc()
        bobs["body"] = Text()
        bobs.apply_update(base64.b64decode(state))
        before = bobs.get_state()
        bobs["body"] += " world"
        edit = base64.b64encode(bobs.get_update(before)).decode()
        b.send(t="update", sid="b1", update=edit)
        relayed = a.expect("update")
        assert relayed["sid"] == "a1" and text_of(seed_update, relayed["update"]) == "hello world"

        b.send(t="awareness", sid="b1", update=base64.b64encode(b"cursor").decode())
        assert base64.b64decode(a.expect("awareness")["update"]) == b"cursor"

        # A third editor arriving now is handed both edits in one state — and told that the
        # room is ahead of the file: nobody has written Bob's edit to disk yet.
        with connect(client, alice) as c:
            c.send(t="join", sid="c1", path="doc.md", client_id=33, base_rev=rev)
            third = c.expect("joined")
            assert text_of(third["state"]) == "hello world" and third["dirty"] is True
            a.expect("peer_joined")
        assert a.expect("peer_left")["client_id"] == 33

        a.send(t="update", sid="a1", update="not base64!")
        assert a.expect("error")["of"] == "update"
        a.send(t="update", sid="a1", update=base64.b64encode(b"\x99junk").decode())
        assert "could not be applied" in a.expect("error")["message"]


def test_the_scribe_is_inherited_and_its_markdown_never_conflicts(
    client: TestClient, store: Store
) -> None:
    alice, bob = issue(store, "alice"), issue(store, "bob")
    with connect(client, alice) as a, connect(client, bob) as b:
        rev = open_document(a)
        b.expect("change")
        a.send(t="join", sid="a1", path="doc.md", client_id=11, base_rev=rev)
        a.expect("joined")
        a.send(t="seed", sid="a1", update=update_of("hello"))
        a.expect("seeded")
        b.send(t="join", sid="b1", path="doc.md", client_id=22, base_rev=rev)
        assert b.expect("joined")["dirty"] is False, "seeded, and nothing typed since"
        a.expect("peer_joined")

        # The room's Markdown is taken from whatever revision its Studio last saw…
        a.send(t="put", id=1, path="doc.md", content="# Doc\n\nhello\n", base_rev=rev, live="a1")
        first = a.expect("ack")
        b.expect("change")
        b.send(t="put", id=2, path="doc.md", content="# Doc\n\nhello world\n", base_rev=rev, live="b1")
        assert b.expect("ack")["rev"] == first["rev"] + 1
        assert a.expect("change")["content"] == "# Doc\n\nhello world\n"
        # …but only from a member: the same stale write claiming someone else's session is refused.
        a.send(t="put", id=3, path="doc.md", content="mine\n", base_rev=rev, live="b1")
        assert a.expect("nack")["current"]["rev"] == first["rev"] + 1

        a.send(t="leave", sid="a1")
        left = b.expect("peer_left")
        assert left["client_id"] == 11
        assert b.expect("scribe")["sid"] == "b1"


def test_a_write_from_outside_the_room_ends_it(client: TestClient, store: Store) -> None:
    alice, bob = issue(store, "alice"), issue(store, "bob")
    with connect(client, alice) as a, connect(client, bob) as b:
        rev = open_document(a)
        b.expect("change")
        a.send(t="join", sid="a1", path="doc.md", client_id=11, base_rev=rev)
        epoch = a.expect("joined")["epoch"]
        a.send(t="seed", sid="a1", update=update_of("hello"))
        a.expect("seeded")

        # Bob's agent rewrites the file; Bob does not have the document open.
        b.send(t="put", id=5, path="doc.md", content="# Rewritten\n", base_rev=rev)
        new_rev = b.expect("ack")["rev"]
        # Alice's Studio gets the new Markdown before her editor is told to reopen it.
        assert a.expect("change")["content"] == "# Rewritten\n"
        assert a.expect("reset")["reason"] == "replaced"

        # The room she comes back to is a new one, to be seeded from the new text.
        a.send(t="join", sid="a2", path="doc.md", client_id=11, base_rev=rev)
        assert a.expect("join_failed")["reason"] == "stale"
        a.send(t="join", sid="a2", path="doc.md", client_id=11, base_rev=new_rev)
        again = a.expect("joined")
        assert again["seeded"] is False and again["epoch"] != epoch

        b.send(t="delete", id=6, path="doc.md", base_rev=new_rev)
        b.expect("ack")
        a.expect("change")
        assert a.expect("reset")["reason"] == "deleted"
        a.send(t="join", sid="a3", path="doc.md", client_id=11, base_rev=new_rev + 1)
        assert a.expect("join_failed")["reason"] == "missing"


def test_presence_names_who_is_here_and_what_they_have_open(
    client: TestClient, store: Store
) -> None:
    alice, bob = issue(store, "alice"), issue(store, "bob")
    with connect(client, alice) as a:
        rev = open_document(a)
        with connect(client, bob) as b:
            peers = a.expect("presence")["peers"]
            assert [peer["user"]["name"] for peer in peers] == ["alice", "bob"]
            b.send(t="join", sid="b1", path="doc.md", client_id=22, base_rev=rev)
            b.expect("joined")
            peers = a.expect("presence")["peers"]
            assert peers[1]["docs"] == ["doc.md"] and peers[0]["docs"] == []
        # Bob's socket closing is a leave, without his having said so.
        peers = a.expect("presence")["peers"]
        assert [peer["user"]["name"] for peer in peers] == ["alice"]
        hub = client.app.state.hub
        assert hub._spaces["game"].rooms == {}
