"""``/api/ai/v1`` over HTTP and WebSocket, ACP on and off (ai-access design §6, §9, §10; R-A0/R-A2).

Discovery, registration, the device directory and the control socket through FastAPI's
TestClient, plus the Documents side that must not notice ACP at all.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voxarium_studio_remote import ai_protocol as p
from voxarium_studio_remote import protocol as documents_protocol
from voxarium_studio_remote.app import AI_CONTROL_ENV_VAR, create_app
from voxarium_studio_remote.auth import hash_token, new_token
from voxarium_studio_remote.store import Store, UserRow

CAPS = "/api/ai/v1/capabilities"
REGS = "/api/ai/v1/registrations"
CONTROL = "/api/ai/v1/control"


def U() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def store() -> Iterator[Store]:
    store = Store(":memory:")
    yield store
    store.close()


@contextmanager
def serve(store: Store, ai_control: bool | None = True) -> Iterator[TestClient]:
    with TestClient(create_app(store=store, ai_control=ai_control)) as client:
        yield client


@pytest.fixture
def client(store: Store) -> Iterator[TestClient]:
    with serve(store) as client:
        yield client


def person(store: Store, name: str = "Alice") -> tuple[UserRow, str]:
    key = new_token()
    return store.create_user(name, hash_token(key)), key


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def rotate(store: Store, user: UserRow) -> str:
    row = store.user(user.id)
    key = new_token()
    store.rotate_user(row.id, hash_token(key), row.metadata_revision, row.credential_version)
    return key


def register(client: TestClient, key: str, kind: str = "studio", display_name: str = "Device") -> tuple[dict, dict]:
    """Register a device; return (device headers, 201 body)."""
    client_id = U()
    response = client.post(REGS, headers=bearer(key),
                           json={"client_id": client_id, "display_name": display_name, "kind": kind})
    assert response.status_code == 201, response.text
    body = response.json()
    return {**bearer(key), "X-VXS-Client-ID": client_id, "X-VXS-Device-Token": body["device_token"]}, body


def ai_counts(client: TestClient) -> dict:
    db = client.app.state.ai_store._db
    return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ai_registrations", "ai_exports", "ai_grants", "ai_resource_index", "ai_audit")}


def closed_code(socket: Any) -> int:
    """Read until the server closes the socket; return its close code."""
    with pytest.raises(WebSocketDisconnect) as closed:
        for _ in range(100):
            socket.receive_json()
    return closed.value.code


def error_code(response) -> str:
    return response.json()["error"]["code"]


# ============================================================== 1. capabilities


def test_capabilities_exact_dto_when_mounted(client: TestClient, store: Store) -> None:
    _, key = person(store)
    response = client.get(CAPS, headers=bearer(key))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    raw = response.content.decode()
    body = json.loads(raw, object_pairs_hook=lambda pairs: (len({k for k, _ in pairs}) == len(pairs)
                                                            or pytest.fail("duplicate key")) and dict(pairs))
    assert set(body) == {"acp", "server_instance_id", "ai_control"}
    assert set(body["ai_control"]) == {"versions"}
    assert body["acp"] == 1 and type(body["acp"]) is int
    assert body["ai_control"]["versions"] == [1] and all(type(v) is int for v in body["ai_control"]["versions"])
    assert str(uuid.UUID(body["server_instance_id"])) == body["server_instance_id"]
    assert body["server_instance_id"] == client.app.state.ai_store.server_instance_id
    # Stable for the server instance.
    assert client.get(CAPS, headers=bearer(key)).json() == body


def test_capabilities_absent_when_acp_is_off(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _, key = person(store)
    with serve(store, ai_control=False) as client:
        assert client.get(CAPS, headers=bearer(key)).status_code == 404
        assert client.post(REGS, headers=bearer(key), json={}).status_code in (404, 405)
        assert not hasattr(client.app.state, "ai_store")
    monkeypatch.delenv(AI_CONTROL_ENV_VAR, raising=False)
    with serve(store, ai_control=None) as client:
        assert client.get(CAPS, headers=bearer(key)).status_code == 404
    for off in ("0", "false", "", "no"):
        monkeypatch.setenv(AI_CONTROL_ENV_VAR, off)
        with serve(store, ai_control=None) as client:
            assert client.get(CAPS, headers=bearer(key)).status_code == 404
    monkeypatch.setenv(AI_CONTROL_ENV_VAR, "1")
    with serve(store, ai_control=None) as client:
        assert client.get(CAPS, headers=bearer(key)).status_code == 200
    # An explicit argument wins over the environment.
    with serve(store, ai_control=False) as client:
        assert client.get(CAPS, headers=bearer(key)).status_code == 404


def test_capabilities_requires_a_live_personal_key(client: TestClient, store: Store) -> None:
    user, key = person(store)
    assert client.get(CAPS).status_code == 401
    assert client.get(CAPS, headers=bearer(new_token())).status_code == 401
    assert client.get(CAPS, headers={"Authorization": f"Basic {key}"}).status_code == 401
    unauthorised = client.get(CAPS, headers={"Authorization": "Bearer nope"})
    assert unauthorised.status_code == 401 and unauthorised.headers["cache-control"] == "no-store"
    assert error_code(unauthorised) == "unauthorised"
    row = store.user(user.id)
    store.revoke_user_by_id(user.id, row.metadata_revision, row.credential_version)
    assert client.get(CAPS, headers=bearer(key)).status_code == 401


def test_capabilities_refuses_device_headers_and_query(client: TestClient, store: Store) -> None:
    _, key = person(store)
    for extra in ({"X-VXS-Client-ID": U()}, {"X-VXS-Device-Token": "vxd_x"},
                  {"X-VXS-Client-ID": U(), "X-VXS-Device-Token": "vxd_x"}):
        response = client.get(CAPS, headers={**bearer(key), **extra})
        assert response.status_code == 400 and error_code(response) == "invalid_request"
    for query in ("?x=1", "?token=" + key, "?"):
        response = client.get(CAPS + query, headers=bearer(key))
        if query == "?":  # an empty query string is no query at all
            assert response.status_code == 200
        else:
            assert response.status_code == 400 and error_code(response) == "invalid_request"


def test_discovery_creates_nothing(client: TestClient, store: Store) -> None:
    user, key = person(store)
    store.create_project("game", "Game")
    before = (ai_counts(client), store.list_projects(), store.list_spaces(), store.user(user.id))
    for headers in (bearer(key), {}, bearer(new_token()), {**bearer(key), "X-VXS-Client-ID": U()}):
        client.get(CAPS, headers=headers)
    client.get(CAPS + "?space=new", headers=bearer(key))
    assert (ai_counts(client), store.list_projects(), store.list_spaces(), store.user(user.id)) == before


# ============================================================== 2. Documents compatibility


def _documents_view(store: Store, ai_control: bool, key: str) -> dict:
    with serve(store, ai_control=ai_control) as client:
        view = {
            "healthz": client.get("/healthz").json(),
            "v1_me": client.get("/api/v1/me", headers=bearer(key)).json(),
            "v2_me": client.get("/api/v2/me", headers=bearer(key)).json(),
            "v2_me_noauth": client.get("/api/v2/me").status_code,
        }
        name = "on" if ai_control else "off"
        created = client.post("/api/v2/projects", headers=bearer(key), json={"id": name, "display_name": name})
        assert created.status_code == 201
        assert created.headers["location"] == f"/api/v2/projects/{name}"
        project = created.json()
        view["created_keys"] = sorted(project) + sorted(project["project"])
        view["created_protocol"] = project["protocol"]
        listed = client.get("/api/v2/projects", headers=bearer(key))
        view["listed_status"] = listed.status_code
        view["listed_keys"] = sorted(listed.json())
        view["listed_ids"] = [row["id"] for row in listed.json()["projects"]]
        conflict = client.post("/api/v2/projects", headers=bearer(key), json={"id": name, "display_name": name})
        view["conflict"] = (conflict.status_code, error_code(conflict))
        detail = client.get(f"/api/v2/projects/{name}", headers=bearer(key))
        view["detail"] = (detail.status_code, sorted(detail.json()["project"]))
    return view


def test_documents_endpoints_are_identical_with_acp_on_and_off(store: Store) -> None:
    _, key = person(store)
    off = _documents_view(store, False, key)
    on = _documents_view(store, True, key)
    assert off["listed_ids"] == ["off"] and on["listed_ids"] == ["off", "on"]
    for view in (off, on):
        view.pop("listed_ids")
    assert on == off
    for view in (on, off):
        assert "ai_control" not in json.dumps(view) and "acp" not in json.dumps(view)
    # Read, not guessed: whatever the Documents protocol is, ACP does not change it.
    assert on["healthz"]["protocol"] == off["healthz"]["protocol"] == documents_protocol.PROTOCOL_VERSION
    assert on["v2_me"]["protocol"] == documents_protocol.PROTOCOL_VERSION
    assert on["healthz"]["supported_protocols"] == [documents_protocol.PROTOCOL_VERSION]


def test_acp_frames_are_not_accepted_on_the_documents_socket(client: TestClient, store: Store) -> None:
    _, key = person(store)
    client.post("/api/v2/projects", headers=bearer(key), json={"id": "game", "display_name": "Game"})
    with client.websocket_connect("/api/v2/sync?space=game", headers=bearer(key)) as socket:
        assert socket.receive_json()["t"] == "hello"
        socket.send_text(p.frame("directory.list", U(), {}))
        message = socket.receive_json()
        while message["t"] == "presence":
            message = socket.receive_json()
        assert message["t"] == "error"


# ============================================================== 3. POST registrations


def test_register_returns_the_secret_once(client: TestClient, store: Store) -> None:
    user, key = person(store)
    client_id = U()
    response = client.post(REGS, headers=bearer(key),
                           json={"client_id": client_id, "display_name": "Desk PC", "kind": "studio"})
    assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"server_instance_id", "registration_id", "client_id", "user_id", "device_token"}
    assert body["client_id"] == client_id and body["user_id"] == user.id
    assert body["server_instance_id"] == client.get(CAPS, headers=bearer(key)).json()["server_instance_id"]
    p.check_uuid4(body["registration_id"])
    assert body["device_token"].startswith("vxd_") and len(body["device_token"]) >= 40
    again = client.post(REGS, headers=bearer(key),
                        json={"client_id": client_id, "display_name": "Desk PC", "kind": "studio"})
    assert again.status_code == 409 and again.headers["cache-control"] == "no-store"
    assert again.json()["registration_id"] == body["registration_id"]
    assert error_code(again) == "registration_exists"
    assert "device_token" not in again.text and body["device_token"] not in again.text
    # The original token still works: nothing was replaced.
    headers = {**bearer(key), "X-VXS-Client-ID": client_id, "X-VXS-Device-Token": body["device_token"]}
    assert client.get(REGS, headers=headers).status_code == 200
    # The same client_id under another user is that user's own registration.
    _, other_key = person(store, "Bob")
    other = client.post(REGS, headers=bearer(other_key),
                        json={"client_id": client_id, "display_name": "Desk PC", "kind": "android"})
    assert other.status_code == 201 and other.json()["registration_id"] != body["registration_id"]


BAD_BODIES = {
    "extra_field": {"client_id": None, "display_name": "D", "kind": "studio", "user_id": "x"},
    "missing_field": {"client_id": None, "display_name": "D"},
    "kind_other": {"client_id": None, "display_name": "D", "kind": "web"},
    "kind_case": {"client_id": None, "display_name": "D", "kind": "Studio"},
    "client_id_v1": {"client_id": str(uuid.uuid1()), "display_name": "D", "kind": "studio"},
    "client_id_upper": {"client_id": str(uuid.uuid4()).upper(), "display_name": "D", "kind": "studio"},
    "client_id_braces": {"client_id": "{%s}" % uuid.uuid4(), "display_name": "D", "kind": "studio"},
    "display_name_empty": {"client_id": None, "display_name": " ", "kind": "studio"},
    "display_name_newline": {"client_id": None, "display_name": "a\nb", "kind": "studio"},
    "display_name_number": {"client_id": None, "display_name": 7, "kind": "studio"},
    "not_object": ["x"],
}


@pytest.mark.parametrize("body", BAD_BODIES.values(), ids=BAD_BODIES.keys())
def test_register_body_is_strict(client: TestClient, store: Store, body) -> None:
    _, key = person(store)
    if isinstance(body, dict) and body.get("client_id", 0) is None:
        body = {**body, "client_id": U()}
    response = client.post(REGS, headers=bearer(key), json=body)
    assert response.status_code == 400 and error_code(response) == "invalid_request"
    assert ai_counts(client)["ai_registrations"] == 0


def test_register_transport_rules(client: TestClient, store: Store) -> None:
    _, key = person(store)
    good = {"client_id": U(), "display_name": "D", "kind": "studio"}
    raw = '{"client_id":"%s","display_name":"D","kind":"studio","kind":"android"}' % U()
    response = client.post(REGS, headers={**bearer(key), "Content-Type": "application/json"}, content=raw)
    assert response.status_code == 400
    response = client.post(REGS, headers={**bearer(key), "Content-Type": "application/json"},
                           content=json.dumps({**good, "display_name": "x" * 3000}))
    assert response.status_code == 413
    response = client.post(REGS, headers={**bearer(key), "Content-Type": "text/plain"}, content=json.dumps(good))
    assert response.status_code == 415
    response = client.post(REGS, headers=bearer(key), content=json.dumps(good))
    assert response.status_code == 415
    response = client.post(REGS, headers={**bearer(key), "Content-Type": "application/json"}, content=b"\xff\xfe")
    assert response.status_code == 400
    response = client.post(REGS, headers={**bearer(key), "X-VXS-Client-ID": good["client_id"]}, json=good)
    assert response.status_code == 400
    response = client.post(REGS + "?kind=studio", headers=bearer(key), json=good)
    assert response.status_code == 400
    assert client.post(REGS, json=good).status_code == 401
    assert client.post(REGS, headers=bearer(new_token()), json=good).status_code == 401
    assert ai_counts(client)["ai_registrations"] == 0
    assert client.post(REGS, headers=bearer(key), json=good).status_code == 201


def test_device_token_is_stored_only_as_a_hash(tmp_path) -> None:
    store = Store(tmp_path / "remote.db")
    _, key = person(store)
    store.close()
    with TestClient(create_app(tmp_path, ai_control=True)) as client:
        headers, body = register(client, key)
        token = body["device_token"]
        assert client.get(REGS, headers=headers).status_code == 200
    files = sorted(path.name for path in tmp_path.iterdir())
    assert "ai-control.db" in files
    dump = b"".join(path.read_bytes() for path in tmp_path.iterdir() if path.is_file())
    assert token.encode() not in dump and token[4:].encode() not in dump
    db = sqlite3.connect(tmp_path / "ai-control.db")
    try:
        lines = "\n".join(db.iterdump())
    finally:
        db.close()
    assert token not in lines and token[4:] not in lines and key not in lines


# ============================================================== 4. GET registrations


def test_directory_requires_device_auth_and_returns_no_credentials(client: TestClient, store: Store) -> None:
    alice, alice_key = person(store, "Alice")
    bob, bob_key = person(store, "Bob")
    headers, mine = register(client, alice_key, "studio", "Desk PC")
    _, theirs = register(client, bob_key, "android", "Phone")
    assert client.get(REGS, headers=bearer(alice_key)).status_code == 401
    assert client.get(REGS).status_code == 401
    wrong = {**headers, "X-VXS-Device-Token": "vxd_" + "A" * 43}
    assert client.get(REGS, headers=wrong).status_code == 401
    other_user = {**headers, "Authorization": f"Bearer {bob_key}"}  # Bob's key, Alice's device
    assert client.get(REGS, headers=other_user).status_code == 401
    assert client.get(REGS + "?all=1", headers=headers).status_code == 400
    response = client.get(REGS, headers=headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"acp", "registrations"} and body["acp"] == 1
    rows = {row["registration_id"]: row for row in body["registrations"]}
    assert rows[mine["registration_id"]] == {"user_id": alice.id, "registration_id": mine["registration_id"],
                                             "client_id": mine["client_id"], "display_name": "Desk PC",
                                             "kind": "studio"}
    assert rows[theirs["registration_id"]]["user_id"] == bob.id
    text = response.text
    assert mine["device_token"] not in text and theirs["device_token"] not in text
    assert "token" not in text and "root" not in text and "export" not in text


# ============================================================== 5. DELETE registrations


class Scene:
    """Alice's PC publishes an export and grants it to Bob's phone, both sockets open."""

    def __init__(self, client: TestClient, store: Store) -> None:
        self.client, self.store = client, store
        self.alice, self.alice_key = person(store, "Alice")
        self.bob, self.bob_key = person(store, "Bob")
        self.sid = client.get(CAPS, headers=bearer(self.alice_key)).json()["server_instance_id"]
        self.ex_headers, self.ex_reg = register(client, self.alice_key, "studio")
        self.rq_headers, self.rq_reg = register(client, self.bob_key, "android")

    @staticmethod
    def send(socket, kind: str, body: dict, rid: str | None = None) -> str:
        rid = rid or U()
        socket.send_text(p.frame(kind, rid, body))
        return rid

    def answer(self, socket, kind: str, body: dict) -> dict:
        rid = self.send(socket, kind, body)
        frame = socket.receive_json()
        assert frame["type"] == "response" and frame["request_id"] == rid, frame
        return frame["body"]

    def setup(self, ex, rq) -> None:
        assert ex.receive_json()["type"] == "hello" and rq.receive_json()["type"] == "hello"
        self.epoch = U()
        attached = self.answer(ex, "executor.attach", {"executor_epoch": self.epoch, "manifest": []})
        assert attached["result"]["executor_epoch"] == self.epoch
        self.export_id = U()
        export = {"export_id": self.export_id, "persistent_id": "game", "storage_epoch": "e1", "revision": 1,
                  "expected_revision": 0, "policy_digest": "0" * 64, "display_name": "Game"}
        export["hash"] = p.semantic_hash(p.export_record(export))
        assert self.answer(ex, "export.publish", export)["state"] == "completed"
        self.target = {"server_instance_id": self.sid, "executor_registration_id": self.ex_reg["registration_id"],
                       "export_id": self.export_id, "persistent_id": "game", "storage_epoch": "e1",
                       "export_revision": 1}
        self.grant_id = U()
        now = time.time_ns() // 1_000_000
        grant = {"grant_id": self.grant_id, "revision": 1, "target": self.target, "grantee_user_id": self.bob.id,
                 "grantee_registration_id": self.rq_reg["registration_id"],
                 "operations": ["view_status", "view_transcript"], "team_ids": ["t1"], "member_ids": [],
                 "resource_scope": "all_resources_in_selected_teams", "resource_ids": [],
                 "execution_policy_digest": "1" * 64, "issued_at": now, "expires_at": now + 3_600_000,
                 "revoked_at": None}
        put = {"export_id": self.export_id, "export_revision": 1, "grant_id": self.grant_id,
               "expected_revision": 0, "revision": 1, "grant": grant,
               "hash": p.semantic_hash(p.grant_record(self.export_id, 1, grant))}
        assert self.answer(ex, "grant.put", put)["state"] == "completed"
        rows = [{"resource_id": "task_1", "kind": "task", "team_id": "t1", "member_id": None,
                 "remote_origin": None, "revision": 1}]
        digest = p.index_rows_hash(rows)
        self.answer(ex, "resource.index.begin", {"export_id": self.export_id, "export_revision": 1,
                                                 "index_revision": 1, "row_count": 1, "hash": digest})
        self.answer(ex, "resource.index.page", {"index_revision": 1, "page_no": 0, "rows": rows})
        assert self.answer(ex, "resource.index.commit", {"index_revision": 1, "hash": digest})["state"] == "completed"

    def request(self, rq) -> str:
        return self.send(rq, "request", {"target": self.target, "grant_id": self.grant_id, "grant_revision": 1,
                                         "op": "resource.get", "resource_id": "task_1", "payload": {}})


def test_delete_own_registration_closes_its_socket_and_revokes_grants(client: TestClient, store: Store) -> None:
    sc = Scene(client, store)
    with client.websocket_connect(CONTROL, headers=sc.ex_headers) as ex, \
            client.websocket_connect(CONTROL, headers=sc.rq_headers) as rq:
        sc.setup(ex, rq)
        registration_id = sc.rq_reg["registration_id"]
        # Somebody else's registration, an unknown id and a non-UUID are all "not available".
        assert client.delete(f"{REGS}/{registration_id}", headers=bearer(sc.alice_key)).status_code == 404
        assert client.delete(f"{REGS}/{U()}", headers=bearer(sc.bob_key)).status_code == 404
        assert client.delete(f"{REGS}/not-a-uuid", headers=bearer(sc.bob_key)).status_code == 404
        assert client.delete(f"{REGS}/{registration_id}").status_code == 401
        assert client.delete(f"{REGS}/{registration_id}?x=1", headers=bearer(sc.bob_key)).status_code == 400
        assert client.app.state.ai_store.grant(sc.grant_id).state == "active"
        started = time.monotonic()
        deleted = client.delete(f"{REGS}/{registration_id}", headers=bearer(sc.bob_key))
        assert deleted.status_code == 204 and deleted.content == b""
        assert deleted.headers["cache-control"] == "no-store"
        assert closed_code(rq) == 4403
        assert time.monotonic() - started < 2.0
        # The executor's socket is someone else's and stays.
        assert sc.answer(ex, "mirror.get", {"export_id": sc.export_id, "grant_id": sc.grant_id})[
            "result"]["grant"]["state"] == "revoked"
    assert client.app.state.ai_store.grant(sc.grant_id).state == "revoked"
    assert client.get(REGS, headers=sc.rq_headers).status_code == 401
    with client.websocket_connect(CONTROL, headers=sc.rq_headers) as rq:
        assert closed_code(rq) == 4401
    # Idempotent for the owner.
    assert client.delete(f"{REGS}/{sc.rq_reg['registration_id']}", headers=bearer(sc.bob_key)).status_code == 204
    # A new registration with the same client id gets a new id and inherits nothing.
    again = client.post(REGS, headers=bearer(sc.bob_key),
                        json={"client_id": sc.rq_reg["client_id"], "display_name": "Phone", "kind": "android"})
    assert again.status_code == 201 and again.json()["registration_id"] != sc.rq_reg["registration_id"]
    assert client.app.state.ai_store.grants_for(again.json()["registration_id"]) == []


# ============================================================== 6. control socket


def test_control_socket_refuses_query_and_bad_device_auth(client: TestClient, store: Store) -> None:
    _, key = person(store)
    headers, _ = register(client, key)
    with client.websocket_connect(CONTROL + "?x=1", headers=headers) as socket:
        assert closed_code(socket) == 4400
    for bad in (bearer(key), {}, {**headers, "X-VXS-Device-Token": "vxd_" + "B" * 43},
                {**headers, "X-VXS-Client-ID": U()}, {**headers, "Authorization": f"Bearer {new_token()}"}):
        with client.websocket_connect(CONTROL, headers=bad) as socket:
            assert closed_code(socket) == 4401
    with client.websocket_connect(CONTROL, headers=headers) as socket:
        hello = socket.receive_json()
        assert hello["type"] == "hello" and hello["body"]["lease_seconds"] == 15
        socket.send_bytes(b"{}")
        assert closed_code(socket) == 4400


def test_control_socket_full_round_trip(client: TestClient, store: Store) -> None:
    sc = Scene(client, store)
    with client.websocket_connect(CONTROL, headers=sc.ex_headers) as ex, \
            client.websocket_connect(CONTROL, headers=sc.rq_headers) as rq:
        sc.setup(ex, rq)
        listed = sc.answer(rq, "directory.list", {})["result"]["targets"]
        assert [(t["target"], t["state"], t["executor_epoch"]) for t in listed] == [(sc.target, "online", sc.epoch)]
        rid = sc.request(rq)
        forward = ex.receive_json()
        assert forward["type"] == "forward"
        assert forward["body"]["source"]["user_id"] == sc.bob.id
        assert forward["body"]["source"]["registration_id"] == sc.rq_reg["registration_id"]
        sc.send(ex, "response", {"target": sc.target, "state": "completed", "result": {"title": "Task"}},
                rid=forward["request_id"])
        answer = rq.receive_json()
        assert answer == {"acp": 1, "type": "response", "request_id": rid,
                          "body": {"target": sc.target, "state": "completed", "result": {"title": "Task"}}}
        nonce = U()
        rid = sc.send(ex, "heartbeat.ping", {"nonce": nonce, "executor_epoch": sc.epoch})
        assert ex.receive_json() == {"acp": 1, "type": "heartbeat.pong", "request_id": rid,
                                     "body": {"nonce": nonce, "executor_epoch": sc.epoch, "lease_seconds": 15}}
        # A second executor socket of the same registration is refused while the lease lives.
        with client.websocket_connect(CONTROL, headers=sc.ex_headers) as second:
            assert second.receive_json()["type"] == "hello"
            sc.send(second, "executor.attach", {"executor_epoch": U(), "manifest": []})
            assert closed_code(second) == 4409
        # The first one is unaffected.
        assert sc.answer(ex, "mirror.get", {"export_id": sc.export_id})["state"] == "completed"
    # Executor gone: the requester's next request is not available, never queued.
    with client.websocket_connect(CONTROL, headers=sc.rq_headers) as rq:
        rq.receive_json()
        sc.request(rq)
        assert rq.receive_json()["body"]["code"] == "not_available"


def test_key_rotation_closes_a_live_control_socket_promptly(client: TestClient, store: Store) -> None:
    sc = Scene(client, store)
    with client.websocket_connect(CONTROL, headers=sc.ex_headers) as ex, \
            client.websocket_connect(CONTROL, headers=sc.rq_headers) as rq:
        sc.setup(ex, rq)
        started = time.monotonic()
        new_key = rotate(store, sc.bob)
        assert closed_code(rq) == 4401
        assert time.monotonic() - started < 2.0
        # Alice's executor is another user's socket and stays open.
        assert sc.answer(ex, "mirror.get", {"export_id": sc.export_id, "grant_id": sc.grant_id})[
            "result"]["grant"]["state"] == "revoked"
    # Old device headers with the new key do not resurrect the registration.
    assert client.get(REGS, headers={**sc.rq_headers, "Authorization": f"Bearer {new_key}"}).status_code == 401
    assert client.get(REGS, headers=sc.rq_headers).status_code == 401
    headers, _ = register(client, new_key, "android")
    assert client.get(REGS, headers=headers).status_code == 200


def test_executor_key_rotation_closes_and_deletes_its_exports(client: TestClient, store: Store) -> None:
    sc = Scene(client, store)
    with client.websocket_connect(CONTROL, headers=sc.ex_headers) as ex, \
            client.websocket_connect(CONTROL, headers=sc.rq_headers) as rq:
        sc.setup(ex, rq)
        rotate(store, sc.alice)
        assert closed_code(ex) == 4401
        assert sc.answer(rq, "directory.list", {})["result"]["targets"] == []
        sc.request(rq)
        assert rq.receive_json()["body"]["code"] in ("denied", "not_available")
    assert client.app.state.ai_store.export(sc.export_id).state == "deleted"


def test_control_socket_is_absent_when_acp_is_off(store: Store) -> None:
    _, key = person(store)
    with serve(store, ai_control=False) as client:
        headers = {**bearer(key), "X-VXS-Client-ID": U(), "X-VXS-Device-Token": "vxd_x"}
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(CONTROL, headers=headers) as socket:
                socket.receive_json()
        assert client.get(REGS, headers=headers).status_code == 404


# ============================================================== 7. R-A0 Documents side


def _documents_hello(socket) -> dict:
    message = socket.receive_json()
    while message["t"] == "presence":
        message = socket.receive_json()
    assert message["t"] == "hello", message
    return message


@pytest.mark.parametrize("ai_control", [True, False], ids=["acp_on", "acp_off"])
def test_rotation_closes_a_documents_socket_without_waiting_for_the_watcher(store: Store, ai_control: bool) -> None:
    user, key = person(store)
    with serve(store, ai_control=ai_control) as client:
        assert client.post("/api/v2/projects", headers=bearer(key),
                           json={"id": "game", "display_name": "Game"}).status_code == 201
        with client.websocket_connect("/api/v2/sync?space=game", headers=bearer(key)) as socket:
            _documents_hello(socket)
            started = time.monotonic()
            new_key = rotate(store, user)
            assert closed_code(socket) == 4401
            # Well inside the 5 s REVOCATION_CHECK_SECONDS watcher period.
            assert time.monotonic() - started < 2.0
        with client.websocket_connect("/api/v2/sync?space=game", headers=bearer(new_key)) as socket:
            _documents_hello(socket)
            socket.send_json({"t": "sync", "since": 0})
            message = socket.receive_json()
            while message["t"] != "synced":
                message = socket.receive_json()
        with client.websocket_connect("/api/v2/sync?space=game", headers=bearer(key)) as socket:
            assert closed_code(socket) == 4401
        assert client.get("/api/v2/me", headers=bearer(key)).status_code == 401
        assert client.get("/api/v2/me", headers=bearer(new_key)).status_code == 200


def test_rotation_closes_only_the_rotated_users_documents_socket(store: Store) -> None:
    alice, alice_key = person(store, "Alice")
    _, bob_key = person(store, "Bob")
    with serve(store) as client:
        client.post("/api/v2/projects", headers=bearer(alice_key), json={"id": "game", "display_name": "Game"})
        with client.websocket_connect("/api/v2/sync?space=game", headers=bearer(alice_key)) as a, \
                client.websocket_connect("/api/v2/sync?space=game", headers=bearer(bob_key)) as b:
            _documents_hello(a)
            _documents_hello(b)
            rotate(store, alice)
            assert closed_code(a) == 4401
            b.send_json({"t": "sync", "since": 0})
            message = b.receive_json()
            while message["t"] != "synced":
                message = b.receive_json()
