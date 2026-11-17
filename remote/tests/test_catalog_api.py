"""Remote catalog v2 and credential-generation boundaries (dev spec §16.7–16.9)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from pycrdt import Doc, Text

from voxarium_studio_remote.app import _write, create_app
from voxarium_studio_remote.auth import TokenAuthenticator, User, hash_token, new_token
from voxarium_studio_remote.protocol import (
    CAPABILITIES, ProtocolError, check_color, check_display_name, check_project_id, encode_update,
)
from voxarium_studio_remote.store import CredentialInvalid, Store


def test_snapshot_measures_actual_http_bytes_and_refuses_only_real_oversize(env):
    store, client, token, _ = env
    store.create_project("budget", "Budget")
    content = "\x7f" * 1_000_000
    store.put("budget", "valid.md", content, None)
    response = client.get("/api/v2/projects/budget/snapshot", headers=headers(token))
    assert response.status_code == 200
    assert len(response.content) < 4 * 1024 * 1024
    assert response.json()["documents"][0]["content"] == content
    # A JSON control character requires six bytes on the wire. Fail closed at
    # the cap, without pretending the page is empty or skipping its document.
    store.put("budget", "valid.md", "\x00" * 1_000_000, None)
    before = store.project("budget").seq
    response = client.get("/api/v2/projects/budget/snapshot", headers=headers(token))
    error(response, 413, "payload_too_large")
    assert store.project("budget").seq == before
    assert store.doc("budget", "valid.md").content == "\x00" * 1_000_000


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "remote.db")
    token = new_token()
    user = store.create_user("Анна", hash_token(token))
    with TestClient(create_app(store=store)) as client:
        yield store, client, token, user
    store.close()


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def create(client, token, project_id="game", label="Игра"):
    response = client.post("/api/v2/projects", headers=headers(token),
                           json={"id": project_id, "display_name": label})
    assert response.status_code == 201, response.text
    return response.json()["project"]


def error(response, status, code):
    assert response.status_code == status, response.text
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message", "request_id"}
    assert response.json()["error"]["code"] == code
    assert len(response.json()["error"]["request_id"]) == 32


@pytest.mark.parametrize("value", ["", "A", " game", "game ", "а", "a/b", "a\\b", "%61", "a\n", "_a", "a"*65, None, True, 7])
def test_id_never_normalises(value):
    with pytest.raises(ProtocolError):
        check_project_id(value)


@pytest.mark.parametrize("value", ["a", "1", "a._-0", "a"*64, "0123456789abcdef0123456789abcdef", "d95cd965-fb35-4b35-8c14-e082850984e5"])
def test_exact_ids(value):
    assert check_project_id(value) == value


@pytest.mark.parametrize("value", ["", " ", None, 5, True, "x"*121, "a\n", "\ta", "a\x7f", "a\x85", "a\u202e", "a\u2066", "a\u2028", "\ud800"])
def test_label_rejects_unsafe_values(value):
    with pytest.raises(ProtocolError):
        check_display_name(value)


def test_labels_and_colors_are_not_identities():
    assert check_display_name("  Игра e\u0301  ") == "Игра é"
    assert check_display_name("😀"*120) == "😀"*120
    assert check_color("#AB01EF") == "#ab01ef"
    for value in (None, "red", "#abc", "#1234567", "#abcdef\n"):
        with pytest.raises(ProtocolError):
            check_color(value)


def test_capabilities_and_public_identity_only(env):
    store, client, token, user = env
    health = client.get("/healthz").json()
    assert health["protocol"] == 2 and health["supported_protocols"] == [2]
    assert health["catalog_api"] == 2
    me = client.get("/api/v2/me", headers=headers(token)).json()
    assert me == {"protocol": 2, "catalog_api": 2, "capabilities": list(CAPABILITIES), "user": user.to_json()}
    assert set(me["user"]) == {"id", "display_name", "color"}
    assert "credential_version" not in json.dumps(me)
    legacy = client.get("/api/v1/me", headers=headers(token)).json()
    assert legacy == {"protocol": 2, "user": {"id": user.id, "name": "Анна", "color": user.color}}
    assert store.list_projects()["projects"] == []


@pytest.mark.parametrize("path", ["/api/v2/me", "/api/v2/projects", "/api/v2/projects/MALFORMED", "/api/v2/projects/missing/snapshot", "/api/v2/legacy-spaces/resolve?name=game"])
def test_auth_precedes_existence_and_validation(env, path):
    _, client, token, _ = env
    for request_headers in ({}, {"Authorization": "Bearer wrong"}, {"Cookie": f"token={token}"}, {"Authorization": f"Basic {token}"}):
        response = client.get(path, headers=request_headers)
        error(response, 401, "unauthorised")
        assert response.headers["www-authenticate"] == "Bearer"
        assert token not in response.text
    error(client.get("/api/v2/projects", params={"token": token}), 401, "unauthorised")
    error(client.post("/api/v2/projects", json={"token": token}), 401, "unauthorised")


def test_create_catalog_pagination_and_immutable_metadata(env):
    store, client, token, _ = env
    for project_id in ("c", "a", "b"):
        project = create(client, token, project_id)
        assert set(project) == {"id", "display_name", "storage_epoch", "metadata_revision", "created_at"}
        assert project["id"] == project_id and project["metadata_revision"] == 1
        assert len(project["storage_epoch"]) == 32
        assert store.seq(project_id) == 0
    first = client.get("/api/v2/projects?limit=2", headers=headers(token)).json()
    assert [p["id"] for p in first["projects"]] == ["a", "b"]
    assert first["next_after"] == "b"
    last = client.get("/api/v2/projects?limit=2&after=b", headers=headers(token)).json()
    assert [p["id"] for p in last["projects"]] == ["c"] and last["next_after"] is None
    assert client.get("/api/v2/projects?after=bb", headers=headers(token)).json() == last
    got = client.get("/api/v2/projects/a", headers=headers(token))
    assert got.json()["project"] == store.project("a").to_json()
    assert got.headers["cache-control"] == "no-store"
    error(client.post("/api/v2/projects", headers=headers(token), json={"id": "a", "display_name": "Other"}), 409, "project_exists")
    assert store.project("a").display_name == "Игра"
    assert client.patch("/api/v2/projects/a", headers=headers(token), json={"display_name": "Other"}).status_code == 405
    assert client.delete("/api/v2/projects/a", headers=headers(token)).status_code == 405
    assert client.get("/api/v2/users", headers=headers(token)).status_code == 404
    assert client.get("/api/v2/projects", headers={**headers(token), "Origin": "https://other.invalid"}).headers.get("access-control-allow-origin") is None


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=true", "limit=-1", "limit=1.0", "limit=+1", "limit=1&limit=2", "after=UPPER", "after=", "unknown=1", "after=a&after=b"])
def test_catalog_bad_query(env, query):
    _, client, token, _ = env
    response = client.get("/api/v2/projects?"+query, headers=headers(token))
    error(response, 400, "invalid_id" if query.startswith("after=") and "&" not in query else "invalid_request")


@pytest.mark.parametrize("payload,code", [
    ({"id": "UPPER", "display_name": "Good"}, "invalid_id"),
    ({"id": True, "display_name": "Good"}, "invalid_id"),
    ({"id": "good", "display_name": "bad\n"}, "invalid_display_name"),
    ({"id": "good", "display_name": 1}, "invalid_display_name"),
    ({"id": "good", "display_name": "Good", "operation_id": "x"}, "invalid_request"),
    ({"id": "good"}, "invalid_request"),
    ([], "invalid_request"), (None, "invalid_request"),
])
def test_create_strict_body(env, payload, code):
    store, client, token, _ = env
    before = store._db.total_changes
    error(client.post("/api/v2/projects", headers=headers(token), json=payload), 400, code)
    assert store._db.total_changes == before


def test_create_body_size_json_duplicates_and_normalization(env):
    store, client, token, _ = env
    error(client.post("/api/v2/projects", headers=headers(token), content="x"*2049), 413, "payload_too_large")
    for body in (b"{", b'{"id":"a","id":"b","display_name":"x"}', b"\xff"):
        error(client.post("/api/v2/projects", headers=headers(token), content=body), 400, "invalid_request")
    response = client.post("/api/v2/projects", headers=headers(token), json={"id": "label", "display_name": "  e\u0301  "})
    assert response.status_code == 201
    assert response.headers["location"] == "/api/v2/projects/label"
    assert store.project("label").display_name == "é"


def test_read_only_endpoints_unknown_v1_and_ws_never_create(env):
    store, client, token, _ = env
    create(client, token)
    before = store._db.total_changes
    for n in range(100):
        for version, selector, code, reason in ((1, f"typo-{n}", 4400, "protocol_upgrade_required"), (2, f"missing-{n}", 4404, "project_not_found")):
            with client.websocket_connect(f"/api/v{version}/sync?space={selector}", headers=headers(token)) as ws:
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_json()
                assert (closed.value.code, closed.value.reason) == (code, reason)
    error(client.get("/api/v2/projects/missing", headers=headers(token)), 404, "project_not_found")
    error(client.get("/api/v2/projects/missing/snapshot", headers=headers(token)), 404, "project_not_found")
    error(client.get("/api/v2/legacy-spaces/resolve?name=game", headers=headers(token)), 404, "legacy_space_not_found")
    assert client.get("/api/v2/projects/game/snapshot", headers=headers(token)).json()["documents"] == []
    assert store._db.total_changes == before
    assert len(store.list_projects()["projects"]) == 1
    assert client.app.state.hub._spaces == {}


@pytest.mark.parametrize("query,reason", [("space=UPPER", "invalid_id"), ("space=%20game", "invalid_id"), ("space=%2567ame", "invalid_id"), ("space=a%2Fb", "invalid_id"), ("space=game&space=other", "invalid_request"), ("space=game&foo=x", "invalid_request"), ("", "invalid_id")])
def test_ws_exact_single_decode_and_fixed_errors(env, query, reason):
    store, client, token, _ = env
    before = store._db.total_changes
    with client.websocket_connect("/api/v2/sync?"+query, headers=headers(token)) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert (closed.value.code, closed.value.reason) == (4400, reason)
    assert store._db.total_changes == before


def test_hello_and_one_transport_decode(env):
    store, client, token, user = env
    project = create(client, token)
    with client.websocket_connect("/api/v2/sync?space=%67ame", headers=headers(token)) as ws:
        hello = ws.receive_json()
        assert hello == {"t": "hello", "protocol": 2, "user": user.to_json(), "space": {
            "id": "game", "display_name": "Игра", "storage_epoch": project["storage_epoch"]}, "seq": 0}
        connection = client.app.state.hub._spaces["game"].connections[0]
        assert not any("token" in key or "credential" in key for key in vars(connection))
        assert token not in repr(vars(connection))
    assert store.seq("game") == 0


def test_legacy_mapping_is_exact_and_not_a_connect_alias(env):
    store, client, token, _ = env
    create(client, token, "canonical", "Legacy")
    create(client, token, "legacy", "Different")
    store._db.execute("INSERT INTO legacy_spaces(name,project_id) VALUES (?,?)", ("legacy", "canonical"))
    response = client.get("/api/v2/legacy-spaces/resolve?name=legacy", headers=headers(token)).json()
    assert response["project"]["id"] == "canonical" and response["legacy_space_name"] == "legacy"
    for name in ("Legacy", " legacy", "legacy "):
        error(client.get("/api/v2/legacy-spaces/resolve", params={"name": name}, headers=headers(token)), 404, "legacy_space_not_found")
    with client.websocket_connect("/api/v2/sync?space=legacy", headers=headers(token)) as ws:
        assert ws.receive_json()["space"]["id"] == "legacy"


def test_snapshot_consistent_tombstones_and_restart_on_change(env):
    store, client, token, user = env
    project = create(client, token)
    for path in ("c.md", "a.md", "b.md"):
        store.put("game", path, path, user.id)
    store.delete("game", "b.md", user.id)
    before = store._db.total_changes
    first = client.get("/api/v2/projects/game/snapshot?limit=2", headers=headers(token)).json()
    assert first["snapshot"] == {"id": "game", "storage_epoch": project["storage_epoch"], "seq": 4}
    assert [d["path"] for d in first["documents"]] == ["a.md", "b.md"]
    assert first["documents"][1]["deleted"] is True and first["documents"][1]["content"] is None
    continuation = {"after": first["next_after"], "expected_seq": 4, "expected_epoch": project["storage_epoch"]}
    final = client.get("/api/v2/projects/game/snapshot", params=continuation, headers=headers(token)).json()
    assert [d["path"] for d in final["documents"]] == ["c.md"] and final["next_after"] is None
    assert store._db.total_changes == before
    store.put("game", "d.md", "new", user.id)
    response = client.get("/api/v2/projects/game/snapshot", params=continuation, headers=headers(token))
    error(response, 409, "snapshot_changed")
    error(client.get("/api/v2/projects/game/snapshot", params={"expected_epoch": "0"*32}, headers=headers(token)), 409, "snapshot_changed")


@pytest.mark.parametrize("query", ["after=a.md", "after=a.md&expected_seq=0", "expected_seq=0", "expected_epoch=wrong", "limit=101", "limit=true", "after=../escape.md&expected_seq=0&expected_epoch="+"0"*32, "after=a.md&expected_seq=true&expected_epoch="+"0"*32, "extra=1", "limit=1&limit=2"])
def test_snapshot_rejects_invalid_continuation(env, query):
    _, client, token, _ = env
    create(client, token)
    error(client.get("/api/v2/projects/game/snapshot?"+query, headers=headers(token)), 400, "invalid_request")


def test_snapshot_byte_budget_never_skips_a_document(env):
    store, client, token, user = env
    project = create(client, token)
    for n in range(7):
        store.put("game", f"{n}.md", "x"*900_000, user.id)
    query = {"limit": 100}
    paths = []
    while True:
        response = client.get("/api/v2/projects/game/snapshot", params=query, headers=headers(token))
        assert response.status_code == 200 and len(response.content) <= 4*1024*1024
        page = response.json()
        paths.extend(d["path"] for d in page["documents"])
        if page["next_after"] is None:
            break
        assert page["next_after"] == page["documents"][-1]["path"]
        query.update(after=page["next_after"], expected_seq=7, expected_epoch=project["storage_epoch"])
    assert paths == [f"{n}.md" for n in range(7)]


def retire(store, user, action):
    current = store.user(user.id)
    args = (current.id, current.metadata_revision, current.credential_version)
    if action == "rotate":
        return store.rotate_user(current.id, hash_token(new_token()), *args[1:])
    if action == "revoke_rotate":
        store.revoke_user_by_id(*args)
        current = store.user(user.id)
        return store.rotate_user(current.id, hash_token(new_token()), current.metadata_revision, current.credential_version)
    return (store.delete_user if action == "delete" else store.revoke_user_by_id)(*args)


@pytest.mark.parametrize("action", ["rotate", "revoke", "delete", "revoke_rotate"])
def test_inbound_and_queued_outgoing_fenced_across_connections(env, tmp_path, action):
    store, client, token, row = env
    create(client, token)
    other = Store(tmp_path / "remote.db")
    hub = client.app.state.hub
    connection = hub.connect(User.from_row(row), "game")
    before = store.seq("game")
    retire(other, row, action)
    # Every inbound kind is behind the same atomic guard, including read/rooms/leave.
    for kind in ("sync", "put", "delete", "join", "seed", "update", "awareness", "leave"):
        with pytest.raises(CredentialInvalid):
            hub.handle(connection, json.dumps({"t": kind, "path": "x.md", "content": "bad", "base_rev": 0}))
    socket = Socket()
    asyncio.run(_write(socket, connection, hub))
    assert socket.sent == [] and socket.closed == [(4401, "unauthorised")]
    assert connection.queue.empty() and connection.closed and connection.sessions == {}
    assert hub._spaces == {} and store.seq("game") == before
    error(client.get("/api/v2/me", headers=headers(token)), 401, "unauthorised")
    other.close()


class Socket:
    def __init__(self):
        self.sent, self.closed = [], []

    async def send_text(self, frame):
        self.sent.append(frame)

    async def close(self, code, reason):
        self.closed.append((code, reason))


@pytest.mark.parametrize("route", ["/api/v2/sync?space=game", "/api/v2/projects"])
def test_auth_to_registration_or_create_race_is_fenced(env, tmp_path, route):
    store, _, token, row = env
    store.create_project("game", "Game")
    other = Store(tmp_path / "remote.db")

    class RacingAuth:
        def authenticate(self, credentials):
            user = TokenAuthenticator(store).authenticate(credentials)
            retire(other, row, "rotate")
            return user

    app = create_app(store=store, authenticator=RacingAuth())
    with TestClient(app) as client:
        if "sync" in route:
            with client.websocket_connect(route, headers=headers(token)) as ws:
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_json()
                assert closed.value.code == 4401
            assert app.state.hub._spaces == {}
        else:
            error(client.post(route, headers=headers(token), json={"id": "new", "display_name": "New"}), 401, "unauthorised")
            assert [p["id"] for p in store.list_projects()["projects"]] == ["game"]
    other.close()


def test_atomic_guard_serialises_handler_with_independent_credential_commit(env, tmp_path):
    store, client, token, row = env
    create(client, token)
    other = Store(tmp_path / "remote.db")
    hub = client.app.state.hub
    connection = hub.connect(User.from_row(row), "game")
    checked, blocked, released, committed = (threading.Event() for _ in range(4))

    def handler(_connection, _message):
        checked.set()
        assert blocked.wait(2)
        # Independent SQLite writer is blocked for the entire handler, not just SELECT.
        assert not committed.is_set()
        store.put("game", "accepted-before-commit.md", "before", row.id)

    def rotation():
        assert checked.wait(2)
        other._db.execute("PRAGMA busy_timeout=50")
        try:
            retire(other, row, "rotate")
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc)
            blocked.set()
        else:
            pytest.fail("Credential mutation committed inside the guarded handler")
        assert released.wait(2)
        retire(other, row, "rotate")
        committed.set()

    hub._handlers["probe"] = handler
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(rotation)
        try:
            hub.handle(connection, '{"t":"probe"}')
        finally:
            released.set()
        future.result(timeout=3)
    assert committed.is_set() and store.doc("game", "accepted-before-commit.md").content == "before"
    with pytest.raises(CredentialInvalid):
        hub.handle(connection, '{"t":"probe"}')
    hub.disconnect(connection)
    other.close()


def test_live_socket_closes_on_next_frame_after_external_rotate(env, tmp_path):
    store, client, token, row = env
    create(client, token)
    other = Store(tmp_path / "remote.db")
    with client.websocket_connect("/api/v2/sync?space=game", headers=headers(token)) as ws:
        assert ws.receive_json()["t"] == "hello"
        assert ws.receive_json()["t"] == "presence"
        retire(other, row, "rotate")
        ws.send_json({"t": "put", "path": "bad.md", "content": "bad", "base_rev": 0})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401
    assert store.seq("game") == 0 and client.app.state.hub._spaces == {}
    other.close()


def test_watcher_closes_idle_and_refreshes_public_metadata(env, tmp_path, monkeypatch):
    import voxarium_studio_remote.app as app_module
    store, client, token, row = env
    create(client, token)
    other = Store(tmp_path / "remote.db")
    monkeypatch.setattr(app_module, "REVOCATION_CHECK_SECONDS", .001)
    with client.websocket_connect("/api/v2/sync?space=game", headers=headers(token)) as ws:
        ws.receive_json()
        ws.receive_json()
        other.update_user(row.id, expected_revision=row.metadata_revision, display_name="New", color="#123456")
        while True:
            message = ws.receive_json()
            if message["t"] == "presence" and message["peers"][0]["user"]["display_name"] == "New":
                assert message["peers"][0]["user"]["color"] == "#123456"
                break
        retire(other, row, "revoke_rotate")
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
        assert closed.value.code == 4401
    assert client.app.state.hub._spaces == {}
    other.close()


def test_database_errors_are_safe(env, monkeypatch):
    store, client, token, _ = env
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("secret " + token)
    monkeypatch.setattr(store, "list_projects", fail)
    response = client.get("/api/v2/projects", headers=headers(token))
    error(response, 503, "temporarily_unavailable")
    assert token not in response.text


def test_concurrent_http_create_has_one_winner_across_connections(env, tmp_path):
    _, client, token, _ = env
    other = Store(tmp_path / "remote.db")
    barrier = threading.Barrier(2)
    with TestClient(create_app(store=other)) as second:
        def post(target):
            barrier.wait(timeout=3)
            return target.post("/api/v2/projects", headers=headers(token),
                               json={"id": "race", "display_name": "Same"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(post, [client, second]))
    assert sorted(r.status_code for r in responses) == [201, 409]
    error(next(r for r in responses if r.status_code == 409), 409, "project_exists")
    assert other.seq("race") == 0 and len(other.list_projects()["projects"]) == 1
    other.close()


def test_revoked_scribe_cleanup_handoff_and_last_editor_never_write(env, tmp_path):
    store, client, token, row = env
    create(client, token)
    other = Store(tmp_path / "remote.db")
    bob = store.create_user("Bob", hash_token(new_token()))
    store.put("game", "doc.md", "original", row.id)
    hub = client.app.state.hub
    a = hub.connect(User.from_row(row), "game")
    b = hub.connect(User.from_row(bob), "game")
    before = store._db.total_changes
    hub.handle(a, json.dumps({"t": "join", "sid": "a1", "path": "doc.md", "client_id": 11, "base_rev": 1}))
    doc = Doc()
    doc["body"] = Text("seed")
    hub.handle(a, json.dumps({"t": "seed", "sid": "a1", "update": encode_update(doc.get_update())}))
    hub.handle(b, json.dumps({"t": "join", "sid": "b1", "path": "doc.md", "client_id": 22, "base_rev": 1}))
    doc["body"] += " dirty"
    hub.handle(b, json.dumps({"t": "update", "sid": "b1", "update": encode_update(doc.get_update())}))
    room = a.space.rooms["doc.md"]
    assert room.dirty and room.scribe.connection is a
    assert store._db.total_changes == before, "opening/seeding/updating rooms persists nothing"
    while not b.queue.empty():
        b.queue.get_nowait()
    retire(other, row, "revoke")
    socket = Socket()
    asyncio.run(_write(socket, a, hub))
    assert room.dirty and room.scribe.connection is b and not a.sessions
    frames = []
    while not b.queue.empty():
        frames.append(json.loads(b.queue.get_nowait()))
    assert next(f for f in frames if f["t"] == "peer_left")["client_id"] == 11
    assert next(f for f in frames if f["t"] == "scribe")["sid"] == "b1"
    assert [p["user"]["id"] for p in frames[-1]["peers"]] == [bob.id]
    assert store.seq("game") == 1 and store.doc("game", "doc.md").content == "original"
    # The last editor's dirty CRDT is dropped, not silently materialised as a revoked put.
    b.send({"t": "queued_private_data"})
    retire(other, bob, "delete")
    asyncio.run(_write(Socket(), b, hub))
    assert hub._spaces == {} and store.seq("game") == 1
    assert store.doc("game", "doc.md").content == "original"
    assert b.queue.empty() and not b.sessions
    other.close()


def test_authorised_inflight_send_is_not_recalled_and_holds_no_db_lock(env, tmp_path):
    store, client, token, row = env
    create(client, token)
    other = Store(tmp_path / "remote.db")
    hub = client.app.state.hub
    connection = hub.connect(User.from_row(row), "game")

    class InFlight(Socket):
        async def send_text(self, frame):
            assert not store._db.in_transaction
            retire(other, row, "rotate")
            self.sent.append(frame)

    socket = InFlight()
    asyncio.run(_write(socket, connection, hub))
    assert [json.loads(frame)["t"] for frame in socket.sent] == ["hello"]
    assert socket.closed == [(4401, "unauthorised")]
    assert connection.queue.empty()
    other.close()


def test_rest_path_decode_and_auth_before_invalid_paths(env):
    _, client, token, _ = env
    for path in ("a%2Fb", "a%252Fb", "UPPER", "%20game", "game%20"):
        error(client.get("/api/v2/projects/"+path, headers=headers(token)), 400, "invalid_id")
        error(client.get("/api/v2/projects/"+path), 401, "unauthorised")


def test_duplicate_authorization_and_secret_fallbacks_are_refused(env):
    store, client, token, _ = env
    response = client.get("/api/v2/projects", headers=[("Authorization", "Bearer wrong"), ("Authorization", f"Bearer {token}")])
    error(response, 401, "unauthorised")
    for path in ("/api/v1/sync?space=game", "/api/v2/sync?space=game"):
        with client.websocket_connect(path+"&token="+token) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401
    assert store.list_projects()["projects"] == []
