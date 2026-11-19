"""ACP/1 relay, frame by frame, with a fake clock (ai-access design §6–§10; R-A2).

The relay is driven directly: no sockets, no sleeps. A connection's outbound frames are
read from its queue the way the route's writer does it — ``dequeued`` and then
``deliverable`` immediately before the "network send".
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

import pytest

from voxarium_studio_remote import ai_protocol as p
from voxarium_studio_remote.ai_auth import Actor, AiAuth, hash_device_token, new_device_token
from voxarium_studio_remote.ai_relay import CloseConnection, ControlConnection, Relay
from voxarium_studio_remote.ai_store import AiStore
from voxarium_studio_remote.auth import build_authenticator, hash_token, new_token
from voxarium_studio_remote.store import Store, UserRow

BASE_MS = 1_800_000_000_000
ALL_OPS = sorted(p.PERMISSIONS)
CLOSED = None  # the close sentinel a connection's queue ends with
PAGE_ROWS = 20


def U() -> str:
    return str(uuid.uuid4())


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def wall_ms(self) -> int:
        return BASE_MS + int(round(self.t * 1000))

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class Person:
    row: UserRow
    key: str


@dataclass
class Device:
    person: Person
    registration_id: str
    client_id: str
    token: str
    actor: Actor


@dataclass
class Scene:
    """One executor with one published, indexed export granted to one requester."""

    executor: Device
    ex: ControlConnection
    epoch: str
    requester: Device
    rq: ControlConnection
    export: dict
    target: dict
    grant_id: str


class World:
    def __init__(self) -> None:
        self.clock = Clock()
        self.store = Store(":memory:")
        self.ai = AiStore(":memory:")
        # As create_app wires it: mirrors die inside the credential transaction.
        self.store.add_credential_hook(lambda user_id, version: self.ai.invalidate_user(
            user_id, version, self.clock.wall_ms()))
        self.auth = AiAuth(self.store, self.ai, build_authenticator(self.store))
        self.relay = Relay(self.auth, monotonic=self.clock.monotonic, wall_ms=self.clock.wall_ms)
        self.sid = self.ai.server_instance_id
        self.stream_epoch = U()

    def close(self) -> None:
        self.ai.close()
        self.store.close()

    # ------------------------------------------------------------- identities
    def person(self, name: str) -> Person:
        key = new_token()
        return Person(self.store.create_user(name, hash_token(key)), key)

    def device(self, person: Person, kind: str = "studio") -> Device:
        client_id, token = U(), new_device_token()
        version = self.store.user(person.row.id).credential_version
        registration = self.ai.register(person.row.id, version, client_id, "Device", kind,
                                        hash_device_token(token), self.clock.wall_ms())
        actor = self.auth.device(self.headers(person, client_id, token))
        assert actor is not None
        return Device(person, registration.id, client_id, token, actor)

    @staticmethod
    def headers(person: Person, client_id: str, token: str) -> dict:
        return {"authorization": f"Bearer {person.key}", "x-vxs-client-id": client_id,
                "x-vxs-device-token": token}

    def rotate(self, person: Person) -> None:
        row = self.store.user(person.row.id)
        person.key = new_token()
        person.row = self.store.rotate_user(row.id, hash_token(person.key), row.metadata_revision,
                                            row.credential_version)

    # ------------------------------------------------------------- frames
    def connect(self, device: Device) -> ControlConnection:
        connection = self.relay.connect(device.actor)
        hello = self.one(connection)
        assert hello["type"] == "hello"
        return connection

    def send(self, connection: ControlConnection, kind: str, body: dict, rid: str | None = None) -> str:
        rid = rid or U()
        self.relay.handle(connection, p.frame(kind, rid, body))
        return rid

    def drain(self, connection: ControlConnection) -> list:
        frames = []
        while not connection.queue.empty():
            item = connection.queue.get_nowait()
            if item is None:
                frames.append(CLOSED)
                continue
            connection.dequeued(item)
            if self.relay.deliverable(connection, item):
                frames.append(json.loads(item.text))
        return frames

    def one(self, connection: ControlConnection) -> dict:
        frames = self.drain(connection)
        assert len(frames) == 1, frames
        return frames[0]

    def ask(self, connection: ControlConnection, kind: str, body: dict) -> dict:
        """Send a frame that is answered at once; return the answer's body."""
        rid = self.send(connection, kind, body)
        frame = self.one(connection)
        assert frame["type"] == "response" and frame["request_id"] == rid, frame
        return frame["body"]

    # ------------------------------------------------------------- executor lifecycle
    def attach(self, connection: ControlConnection, epoch: str | None = None, manifest=()) -> dict:
        epoch = epoch or U()
        return self.ask(connection, "executor.attach", {"executor_epoch": epoch, "manifest": list(manifest)})

    @staticmethod
    def export_body(export_id: str | None = None, persistent_id: str = "game", revision: int = 1,
                    expected: int = 0, display_name: str = "Game", storage_epoch: str = "e1") -> dict:
        body = {"export_id": export_id or U(), "persistent_id": persistent_id,
                "storage_epoch": storage_epoch, "revision": revision, "expected_revision": expected,
                "policy_digest": "0" * 64, "display_name": display_name}
        body["hash"] = p.semantic_hash(p.export_record(body))
        return body

    def target(self, executor: Device, export: dict) -> dict:
        return {"server_instance_id": self.sid, "executor_registration_id": executor.registration_id,
                "export_id": export["export_id"], "persistent_id": export["persistent_id"],
                "storage_epoch": export["storage_epoch"], "export_revision": export["revision"]}

    def grant_body(self, executor: Device, export: dict, grantee: Device, *, grant_id: str | None = None,
                   ops=ALL_OPS, revision: int = 1, expected: int = 0, expires_in_ms: int = 3_600_000,
                   team_ids=("t1",), member_ids=()) -> dict:
        now = self.clock.wall_ms()
        grant_id = grant_id or U()
        grant = {"grant_id": grant_id, "revision": revision, "target": self.target(executor, export),
                 "grantee_user_id": grantee.person.row.id,
                 "grantee_registration_id": grantee.registration_id, "operations": list(ops),
                 "team_ids": list(team_ids), "member_ids": list(member_ids),
                 "resource_scope": "all_resources_in_selected_teams", "resource_ids": [],
                 "execution_policy_digest": "1" * 64, "issued_at": now,
                 "expires_at": now + expires_in_ms, "revoked_at": None}
        return {"export_id": export["export_id"], "export_revision": export["revision"],
                "grant_id": grant_id, "expected_revision": expected, "revision": revision,
                "grant": grant, "hash": p.semantic_hash(p.grant_record(export["export_id"], export["revision"], grant))}

    @staticmethod
    def revoke_body(export: dict, grant_id: str, revision: int = 2) -> dict:
        return {"export_id": export["export_id"], "export_revision": export["revision"],
                "grant_id": grant_id, "expected_revision": revision - 1, "revision": revision,
                "hash": p.semantic_hash(p.revoke_record(export["export_id"], export["revision"], grant_id, revision))}

    @staticmethod
    def rows(*resource_ids: str) -> list[dict]:
        return [{"resource_id": rid, "kind": "task", "team_id": "t1", "member_id": None,
                 "remote_origin": None, "revision": 1} for rid in resource_ids]

    def index(self, connection: ControlConnection, export: dict, rows: list[dict], index_revision: int = 1) -> dict:
        digest = p.index_rows_hash(rows)
        begin = self.ask(connection, "resource.index.begin", {
            "export_id": export["export_id"], "export_revision": export["revision"],
            "index_revision": index_revision, "row_count": len(rows), "hash": digest})
        assert begin == {"state": "completed", "result": {"index_revision": index_revision, "state": "staging"}}
        # 20 rows per page: the frame-wide 128-field cap admits at most 20 six-field rows
        # (see test_a_full_fifty_row_index_page_fits_a_frame for the design clash).
        for page_no, start in enumerate(range(0, len(rows), PAGE_ROWS)):
            self.ask(connection, "resource.index.page", {"index_revision": index_revision, "page_no": page_no,
                                                         "rows": rows[start:start + PAGE_ROWS]})
        return self.ask(connection, "resource.index.commit", {"index_revision": index_revision, "hash": digest})

    def publish(self, executor_connection: ControlConnection, **kwargs) -> dict:
        body = self.export_body(**kwargs)
        answer = self.ask(executor_connection, "export.publish", body)
        assert answer["state"] == "completed", answer
        return body

    def grant(self, executor: Device, connection: ControlConnection, export: dict, grantee: Device, **kwargs) -> str:
        body = self.grant_body(executor, export, grantee, **kwargs)
        answer = self.ask(connection, "grant.put", body)
        assert answer["state"] == "completed", answer
        return body["grant_id"]

    def scene(self, *, requester_kind: str = "android", ops=ALL_OPS, expires_in_ms: int = 3_600_000,
              persistent_id: str = "game", executor_person: Person | None = None,
              requester_person: Person | None = None) -> Scene:
        executor = self.device(executor_person or self.person("Alice"))
        requester = self.device(requester_person or self.person("Bob"), requester_kind)
        ex, rq = self.connect(executor), self.connect(requester)
        epoch = U()
        self.attach(ex, epoch)
        export = self.publish(ex, persistent_id=persistent_id)
        grant_id = self.grant(executor, ex, export, requester, ops=ops, expires_in_ms=expires_in_ms)
        self.index(ex, export, self.rows("task_1", "task_2"))
        return Scene(executor, ex, epoch, requester, rq, export, self.target(executor, export), grant_id)

    # ------------------------------------------------------------- requests
    @staticmethod
    def read(sc: Scene, op: str = "resource.get", *, resource_id: str | None = "task_1",
             payload: dict | None = None, target: dict | None = None, grant_id: str | None = None) -> dict:
        body = {"target": target or sc.target, "grant_id": grant_id or sc.grant_id, "grant_revision": 1,
                "op": op, "payload": {} if payload is None else payload}
        if resource_id is not None:
            body["resource_id"] = resource_id
        return body

    def mutation(self, sc: Scene, op: str = "task.start", *, payload: dict | None = None,
                 command_id: str | None = None) -> dict:
        body = {"target": sc.target, "grant_id": sc.grant_id, "grant_revision": 1, "op": op,
                "resource_id": "task_1", "command_id": command_id or U(), "issued_at": self.clock.wall_ms(),
                "expected_executor_epoch": sc.epoch, "payload": {} if payload is None else payload}
        if op in ("task.edit_draft", "task.start", "chat.send"):
            body["expected_resource_revision"] = 1
        return body

    def forwarded(self, sc: Scene, body: dict) -> tuple[str, dict]:
        """Requester sends *body*; return (requester request_id, forward frame at the executor)."""
        rid = self.send(sc.rq, "request", body)
        assert self.drain(sc.rq) == []
        forward = self.one(sc.ex)
        assert forward["type"] == "forward"
        return rid, forward

    def subscribe(self, sc: Scene, rq: ControlConnection | None = None, grant_id: str | None = None) -> str:
        rq = rq or sc.rq
        body = self.read(sc, "stream.subscribe", payload={"topic": "status"}, grant_id=grant_id)
        rid = self.send(rq, "request", body)
        forward = self.one(sc.ex)
        subscription_id = U()
        self.send(sc.ex, "response", {"target": sc.target, "state": "completed",
                                      "result": {"subscription_id": subscription_id}}, rid=forward["request_id"])
        answer = self.one(rq)
        assert answer["request_id"] == rid and answer["body"]["result"] == {"subscription_id": subscription_id}
        return subscription_id

    def event(self, sc: Scene, subscription_id: str, seq: int, resource_id: str = "task_1",
              payload: dict | None = None) -> dict:
        return {"subscription_id": subscription_id, "target": sc.target, "resource_id": resource_id,
                "stream_epoch": self.stream_epoch, "seq": seq, "resource_revision": 1,
                "payload": payload or {"status": "running"}}


@pytest.fixture
def w():
    world = World()
    yield world
    world.close()


def closes(code: int):
    class _Closes:
        def __enter__(self):
            self.ctx = pytest.raises(CloseConnection)
            self.info = self.ctx.__enter__()
            return self.info

        def __exit__(self, *exc):
            result = self.ctx.__exit__(*exc)
            assert self.info.value.code == code, (self.info.value.code, self.info.value.reason)
            return result
    return _Closes()


def responses(frames: list) -> list[dict]:
    return [frame for frame in frames if frame is not CLOSED and frame["type"] == "response"]


# ============================================================== 1. hello and protocol errors


def test_hello_carries_exactly_the_session_parameters(w: World) -> None:
    device = w.device(w.person("Alice"))
    connection = w.relay.connect(device.actor)
    hello = w.one(connection)
    assert set(hello) == {"acp", "type", "request_id", "body"}
    assert hello["acp"] == 1 and hello["type"] == "hello"
    p.check_uuid(hello["request_id"])
    assert hello["body"] == {"acp": 1, "server_instance_id": w.sid, "connection_id": connection.id,
                             "lease_seconds": 15, "heartbeat_seconds": 5}
    assert type(hello["body"]["lease_seconds"]) is int and type(hello["body"]["acp"]) is int


def _deep(depth: int) -> dict:
    value: dict = {}
    for _ in range(depth):
        value = {"x": value}
    return value


def _raw(body: dict | str, **override) -> str:
    message = {"acp": 1, "type": "directory.list", "request_id": U(), "body": body}
    message.update(override)
    return json.dumps(message)


PROTOCOL_ERRORS = {
    "binary": b'{"acp":1,"type":"directory.list","request_id":"00000000-0000-4000-8000-000000000000","body":{}}',
    "non_json": "{not json",
    "duplicate_key": '{"acp":1,"acp":1,"type":"directory.list","request_id":"%s","body":{}}' % U(),
    "unknown_type": _raw({}, type="hello"),
    "unknown_top_field": _raw({}, extra=1),
    "unknown_body_field": _raw({"cursor": "x"}),
    "acp_2": _raw({}, acp=2),
    "acp_true": _raw({}, acp=True),
    "acp_string": _raw({}, acp="1"),
    "float": _raw({"after": 1.5}),
    "nan": _raw({}).replace('"body": {}', '"body": {"x": NaN}'),
    "non_uuid_request_id": _raw({}, request_id="abc"),
    "upper_uuid_request_id": _raw({}, request_id=U().upper()),
    "body_not_object": _raw([]),  # type: ignore[arg-type]
    "too_deep": _raw(_deep(12)),
    "too_many_fields": _raw({f"k{i}": 0 for i in range(130)}),
    "too_large": _raw({"pad": "x" * (64 * 1024)}),
}


@pytest.mark.parametrize("raw", PROTOCOL_ERRORS.values(), ids=PROTOCOL_ERRORS.keys())
def test_malformed_frames_close_with_protocol_error(w: World, raw) -> None:
    connection = w.connect(w.device(w.person("Alice"), "android"))
    with closes(4400):
        w.relay.handle(connection, raw)


def test_frame_bounds_are_exact(w: World) -> None:
    connection = w.connect(w.device(w.person("Alice"), "android"))
    # Depth 12 (frame=1, body=2, ...) is accepted by the decoder and refused only by the
    # directory.list schema, i.e. still 4400 — so check the decoder bound directly.
    p.decode(json.dumps(_deep(11)))
    with pytest.raises(p.AcpError):
        p.decode(json.dumps(_deep(12)))
    p.decode(json.dumps({f"k{i}": 0 for i in range(128)}))
    with pytest.raises(p.AcpError):
        p.decode(json.dumps({f"k{i}": 0 for i in range(129)}))
    p.decode("x" * 0 + json.dumps("y" * (64 * 1024 - 2)))
    with pytest.raises(p.AcpError):
        p.decode(json.dumps("y" * (64 * 1024 - 1)))
    # A well-formed directory.list is answered and the socket stays.
    assert w.ask(connection, "directory.list", {})["state"] == "completed"


@pytest.mark.parametrize("kind", ["export.publish", "export.unpublish", "grant.put", "grant.revoke",
                                  "mirror.get", "resource.index.begin", "response", "event",
                                  "heartbeat.ping"])
def test_executor_frames_before_attach_are_protocol_errors(w: World, kind: str) -> None:
    connection = w.connect(w.device(w.person("Alice")))
    with closes(4400):
        w.send(connection, kind, {})


def test_android_cannot_attach_and_studio_attaches_once(w: World) -> None:
    person = w.person("Alice")
    android = w.connect(w.device(person, "android"))
    with closes(4400):
        w.attach(android)
    studio = w.connect(w.device(person))
    w.attach(studio)
    with closes(4400):
        w.attach(studio)


@pytest.mark.parametrize("field", ["user_id", "source", "actor", "registration_id", "connection_id"])
def test_actor_fields_in_a_request_body_are_refused(w: World, field: str) -> None:
    sc = w.scene()
    body = w.read(sc)
    body[field] = sc.executor.person.row.id if field == "user_id" else {"user_id": sc.executor.person.row.id}
    with closes(4400):
        w.send(sc.rq, "request", body)
    assert w.drain(sc.ex) == []  # nothing reached the executor


def test_task_note_schema_accepts_no_resource_revision(w: World) -> None:
    body = w.mutation(w.scene(), "task.note", payload={"text": "Leave a note"})
    assert "expected_resource_revision" not in body
    assert p.check_request(body) == body


@pytest.mark.parametrize("revision", [1, None], ids=["integer", "null"])
def test_task_note_schema_forbids_resource_revision(w: World, revision: int | None) -> None:
    body = w.mutation(w.scene(), "task.note", payload={"text": "Leave a note"})
    body["expected_resource_revision"] = revision
    with pytest.raises(p.AcpError, match="expected_resource_revision forbidden for task.note"):
        p.check_request(body)


def test_task_note_schema_still_requires_command_id(w: World) -> None:
    body = w.mutation(w.scene(), "task.note", payload={"text": "Leave a note"})
    del body["command_id"]
    with pytest.raises(p.AcpError, match="mutation requires command_id"):
        p.check_request(body)


@pytest.mark.parametrize("op,payload", [
    ("task.edit_draft", {"title": "Updated title"}),
    ("task.start", {}),
    ("chat.send", {"text": "Send a message"}),
], ids=["task.edit_draft", "task.start", "chat.send"])
def test_edit_start_send_schema_still_requires_resource_revision(w: World, op: str, payload: dict) -> None:
    body = w.mutation(w.scene(), op, payload=payload)
    assert p.check_request(body) == body
    del body["expected_resource_revision"]
    with pytest.raises(p.AcpError, match="expected_resource_revision required"):
        p.check_request(body)


# ============================================================== 2. per-frame auth


def test_key_rotation_closes_on_the_next_frame_with_4401(w: World) -> None:
    sc = w.scene()
    w.rotate(sc.requester.person)
    with closes(4401):
        w.send(sc.rq, "directory.list", {})
    assert w.drain(sc.ex) == []


def test_device_revoke_closes_on_the_next_frame_with_4403(w: World) -> None:
    sc = w.scene()
    w.ai.revoke_registration(sc.requester.person.row.id, sc.requester.registration_id, w.clock.wall_ms())
    with closes(4403):
        w.send(sc.rq, "request", w.read(sc))
    assert w.drain(sc.ex) == []


def test_queued_frames_are_discarded_when_a_registration_is_closed(w: World) -> None:
    sc = w.scene()
    rid, forward = w.forwarded(sc, w.read(sc))
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "result": {"title": "secret"}},
           rid=forward["request_id"])
    assert sc.rq.queue.qsize() == 1
    w.relay.close_registration(sc.requester.registration_id)
    assert sc.rq.closed and sc.rq.close_code == 4403
    assert w.drain(sc.rq) == [CLOSED]
    assert sc.rq.queued_bytes == 0


def test_a_queued_response_is_not_released_after_the_device_is_revoked(w: World) -> None:
    sc = w.scene()
    _, forward = w.forwarded(sc, w.read(sc))
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "result": {"title": "secret"}},
           rid=forward["request_id"])
    # Revoked in the store (e.g. by another worker) after enqueue, before the writer sends.
    w.ai.revoke_registration(sc.requester.person.row.id, sc.requester.registration_id, w.clock.wall_ms())
    item = sc.rq.queue.get_nowait()
    sc.rq.dequeued(item)
    assert w.relay.deliverable(sc.rq, item) is False
    assert sc.rq.closed and sc.rq.close_code == 4403


def test_a_queued_response_is_not_released_after_the_grant_is_revoked(w: World) -> None:
    sc = w.scene()
    _, forward = w.forwarded(sc, w.read(sc))
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "result": {"title": "secret"}},
           rid=forward["request_id"])
    w.ask(sc.ex, "grant.revoke", w.revoke_body(sc.export, sc.grant_id))
    frames = w.drain(sc.rq)
    assert frames == []  # dropped at dequeue, socket stays (only the grant is gone)
    assert not sc.rq.closed


def test_a_queued_response_is_not_released_after_key_rotation(w: World) -> None:
    sc = w.scene()
    _, forward = w.forwarded(sc, w.read(sc))
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "result": {"title": "secret"}},
           rid=forward["request_id"])
    w.rotate(sc.requester.person)
    item = sc.rq.queue.get_nowait()
    sc.rq.dequeued(item)
    assert w.relay.deliverable(sc.rq, item) is False
    assert sc.rq.closed and sc.rq.close_code == 4401


# ============================================================== 3. executor lease


def test_one_executor_per_registration_and_takeover_after_lease(w: World) -> None:
    executor = w.device(w.person("Alice"))
    first, second = w.connect(executor), w.connect(executor)
    w.attach(first)
    w.clock.advance(14.9)
    with closes(4409):
        w.attach(second)
    assert w.relay.executors[executor.registration_id] is first and not first.closed
    w.clock.advance(0.1)  # 15 s without a ping: the first lease is over
    third = w.connect(executor)
    epoch = U()
    assert w.attach(third, epoch)["result"]["executor_epoch"] == epoch
    assert w.relay.executors[executor.registration_id] is third
    assert first.closed and first.close_code == 4409
    assert w.drain(first)[-1] is CLOSED


def test_heartbeat_extends_the_lease_from_the_ping(w: World) -> None:
    executor = w.device(w.person("Alice"))
    first = w.connect(executor)
    epoch = U()
    attached = w.attach(first, epoch)
    assert attached["result"]["lease_seconds"] == 15
    w.clock.advance(10)
    nonce = U()
    rid = w.send(first, "heartbeat.ping", {"nonce": nonce, "executor_epoch": epoch})
    pong = w.one(first)
    assert pong == {"acp": 1, "type": "heartbeat.pong", "request_id": rid,
                    "body": {"nonce": nonce, "executor_epoch": epoch, "lease_seconds": 15}}
    assert first.lease_deadline == w.clock.t + 15
    w.clock.advance(14)  # 24 s after attach, 14 s after ping
    with closes(4409):
        w.attach(w.connect(executor))
    w.relay.tick()
    assert not first.closed
    w.clock.advance(1)  # 15 s after the ping
    w.relay.tick()
    assert first.closed and first.close_code == 4400 and first.close_reason == "lease_expired"
    assert executor.registration_id not in w.relay.executors


def test_ping_for_another_epoch_is_a_protocol_error(w: World) -> None:
    connection = w.connect(w.device(w.person("Alice")))
    w.attach(connection)
    with closes(4400):
        w.send(connection, "heartbeat.ping", {"nonce": U(), "executor_epoch": U()})


def test_frames_after_lease_expiry_are_refused(w: World) -> None:
    sc = w.scene()
    w.clock.advance(15)
    with closes(4400):
        w.send(sc.ex, "heartbeat.ping", {"nonce": U(), "executor_epoch": sc.epoch})
    with closes(4400):
        w.send(sc.ex, "mirror.get", {"export_id": sc.export["export_id"]})


# ============================================================== 4. routing


def test_a_request_reaches_only_the_exact_target_executor(w: World) -> None:
    alice, bob = w.person("Alice"), w.person("Bob")
    sc = w.scene(executor_person=alice, requester_person=bob)
    # A second PC of the same owner exporting a project with the same persistent id.
    other = w.device(alice)
    ex2 = w.connect(other)
    w.attach(ex2)
    export2 = w.publish(ex2, persistent_id="game")
    grant2 = w.grant(other, ex2, export2, sc.requester)
    w.index(ex2, export2, w.rows("task_1"))

    body = w.read(sc)
    rid, forward = w.forwarded(sc, body)
    assert w.drain(ex2) == []
    assert set(forward["body"]) == {"source", "grant", "deadline", "request"}
    assert forward["body"]["source"] == {"user_id": bob.row.id, "registration_id": sc.requester.registration_id,
                                         "connection_id": sc.rq.id}
    assert forward["body"]["grant"] == {"grant_id": sc.grant_id, "revision": 1,
                                        "expires_at": w.ai.grant(sc.grant_id).expires_at}
    assert forward["body"]["deadline"] == w.clock.wall_ms() + 10_000
    assert forward["body"]["request"] == body
    assert forward["request_id"] != rid  # the requester's id never leaves its socket

    result = {"title": "from pc 1"}
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "result": result},
           rid=forward["request_id"])
    answer = w.one(sc.rq)
    assert answer == {"acp": 1, "type": "response", "request_id": rid,
                      "body": {"target": sc.target, "state": "completed", "result": result}}

    # And the other target goes to the other PC only.
    target2 = w.target(other, export2)
    w.send(sc.rq, "request", w.read(sc, target=target2, grant_id=grant2))
    assert w.drain(sc.ex) == []
    assert w.one(ex2)["body"]["request"]["target"] == target2


def test_pinned_executor_detaching_before_request_is_offline_without_fallback_or_replay(w: World) -> None:
    sc = w.scene()
    (entry,) = w.ask(sc.rq, "directory.list", {})["result"]["targets"]
    assert entry["state"] == "online"
    assert entry["target"] == sc.target and entry["executor_epoch"] == sc.epoch
    assert entry["grant"]["grant_id"] == sc.grant_id and entry["grant"]["revision"] == 1
    command = dict(w.mutation(sc), target=entry["target"], grant_id=entry["grant"]["grant_id"],
                   grant_revision=entry["grant"]["revision"], expected_executor_epoch=entry["executor_epoch"])

    # Another online PC is granted for the same project and resource, but is not the pinned target.
    other = w.device(sc.executor.person)
    ex2 = w.connect(other)
    w.attach(ex2)
    export2 = w.publish(ex2, persistent_id=sc.export["persistent_id"])
    w.grant(other, ex2, export2, sc.requester)
    w.index(ex2, export2, w.rows(command["resource_id"]))

    # §16.12: This is a known failure before dispatch, not an ambiguous delivery.
    w.relay.disconnect(sc.ex)
    assert w.ask(sc.rq, "request", command) == {
        "state": "rejected", "target": sc.target, "command_id": command["command_id"], "code": "offline"}
    assert w.ask(sc.rq, "request", w.read(sc)) == {
        "state": "rejected", "target": sc.target, "code": "offline"}
    assert w.drain(sc.ex) == [CLOSED]
    assert w.drain(ex2) == []
    assert w.relay.pending == {}
    assert not any(row["command_id"] == command["command_id"] and row["outcome"] == "forwarded"
                   for row in w.ai.audit_rows())

    # Reattaching even with the same epoch must not deliver the rejected work.
    reattached = w.connect(sc.executor)
    assert w.attach(reattached, sc.epoch)["state"] == "completed"
    w.clock.advance(p.RECEIPT_TIMEOUT_SECONDS)
    w.relay.tick()
    assert w.drain(reattached) == w.drain(ex2) == w.drain(sc.rq) == []
    assert w.relay.pending == {}


@pytest.mark.parametrize("case,code", [
    ("other_device", "not_available"),
    ("unknown_export", "not_available"),
    ("operation_scope", "denied"),
    ("resource_scope", "not_available"),
])
def test_refused_request_does_not_reveal_executor_availability(w: World, case: str, code: str) -> None:
    sc = w.scene(ops=("view_status",) if case == "operation_scope" else ALL_OPS)
    caller = sc.rq
    command = w.mutation(sc)
    if case == "other_device":
        caller = w.connect(w.device(sc.requester.person, "android"))
    elif case == "unknown_export":
        command["target"] = dict(sc.target, export_id=U())
    elif case == "resource_scope":
        # The resource exists, but in a team outside this grant's scope.
        (resource,) = w.rows(command["resource_id"])
        w.index(sc.ex, sc.export, [dict(resource, team_id="t2")], index_revision=2)

    online = w.ask(caller, "request", command)
    assert online == {"state": "rejected", "target": command["target"],
                      "command_id": command["command_id"], "code": code}
    assert w.drain(sc.ex) == []
    assert w.relay.pending == {}
    w.relay.disconnect(sc.ex)
    assert w.ask(caller, "request", command) == online
    assert w.relay.pending == {}


@pytest.mark.parametrize("expire_during_auth", [False, True], ids=["before-request", "during-authorization"])
def test_expired_lease_counts_as_offline_for_routing_before_cleanup(
        w: World, monkeypatch: pytest.MonkeyPatch, expire_during_auth: bool) -> None:
    sc = w.scene()
    if expire_during_auth:
        w.clock.advance(p.LEASE_SECONDS - 1)
        authorize = w.auth.authorize

        def authorize_then_expire_lease(actor: Actor, body: dict, now_ms: int):
            authorization = authorize(actor, body, now_ms)
            w.clock.advance(1)
            return authorization

        monkeypatch.setattr(w.auth, "authorize", authorize_then_expire_lease)
    else:
        w.clock.advance(p.LEASE_SECONDS)
    # No sweep yet: the lease deadline, not merely presence in the map, decides availability.
    assert w.relay.executors[sc.executor.registration_id] is sc.ex and not sc.ex.closed
    command = w.mutation(sc)
    assert w.ask(sc.rq, "request", command) == {
        "state": "rejected", "target": sc.target, "command_id": command["command_id"], "code": "offline"}
    assert w.drain(sc.ex) == []
    assert w.relay.pending == {}


def test_a_response_from_a_connection_that_was_not_asked_is_refused(w: World) -> None:
    sc = w.scene()
    other = w.connect(w.device(w.person("Mallory")))
    w.attach(other)
    rid, forward = w.forwarded(sc, w.read(sc))
    with closes(4400):
        w.send(other, "response", {"target": sc.target, "state": "completed", "result": {"x": 1}},
               rid=forward["request_id"])
    assert w.drain(sc.rq) == []
    # The real executor can still answer.
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed"}, rid=forward["request_id"])
    assert w.one(sc.rq)["request_id"] == rid


def test_requester_cannot_answer_its_own_forward(w: World) -> None:
    sc = w.scene(requester_kind="studio")
    w.attach(sc.rq)  # a studio requester that is also an executor of its own
    _, forward = w.forwarded(sc, w.read(sc))
    with closes(4400):
        w.send(sc.rq, "response", {"target": sc.target, "state": "completed"}, rid=forward["request_id"])


def test_a_response_for_an_unknown_request_is_dropped_silently(w: World) -> None:
    sc = w.scene()
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "result": {"x": 1}})
    assert w.drain(sc.ex) == [] and w.drain(sc.rq) == []
    assert not sc.ex.closed


def test_a_response_with_another_target_or_command_is_refused(w: World) -> None:
    sc = w.scene()
    _, forward = w.forwarded(sc, w.read(sc))
    wrong = dict(sc.target, export_revision=2)
    with closes(4400):
        w.send(sc.ex, "response", {"target": wrong, "state": "completed"}, rid=forward["request_id"])
    command = w.mutation(sc)
    _, forward = w.forwarded(sc, command)
    with closes(4400):
        w.send(sc.ex, "response", {"target": sc.target, "state": "accepted", "command_id": U()},
               rid=forward["request_id"])
    with closes(4400):  # a mutation's receipt must name its command
        w.send(sc.ex, "response", {"target": sc.target, "state": "accepted"}, rid=forward["request_id"])
    w.send(sc.ex, "response", {"target": sc.target, "state": "accepted", "command_id": command["command_id"]},
           rid=forward["request_id"])
    assert w.one(sc.rq)["body"]["state"] == "accepted"


# ============================================================== 5. receipt timeout


def test_receipt_timeout_answers_reads_and_mutations_and_drops_late_responses(w: World) -> None:
    sc = w.scene()
    read_rid, read_forward = w.forwarded(sc, w.read(sc))
    command = w.mutation(sc)
    write_rid, write_forward = w.forwarded(sc, command)
    w.clock.advance(9.9)
    w.relay.tick()
    assert w.drain(sc.rq) == []
    w.clock.advance(0.1)
    w.relay.tick()
    answers = {frame["request_id"]: frame["body"] for frame in w.drain(sc.rq)}
    assert answers == {
        read_rid: {"state": "rejected", "target": sc.target, "code": "not_available"},
        write_rid: {"state": "outcome_unknown", "target": sc.target, "command_id": command["command_id"],
                    "code": "outcome_unknown"},
    }
    assert w.relay.pending == {}
    # Late answers from the PC are never released.
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "result": {"x": 1}},
           rid=read_forward["request_id"])
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "command_id": command["command_id"]},
           rid=write_forward["request_id"])
    assert w.drain(sc.rq) == []
    outcomes = [row["outcome"] for row in w.ai.audit_rows() if row["command_id"] == command["command_id"]]
    assert outcomes == ["forwarded", "outcome_unknown"]


@pytest.mark.parametrize("mutation", [False, True], ids=["read", "mutation"])
def test_executor_disconnect_after_forward_fails_pending_requests_at_once(w: World, mutation: bool) -> None:
    sc = w.scene()
    body = w.mutation(sc) if mutation else w.read(sc)
    rid, forward = w.forwarded(sc, body)
    assert forward["body"]["request"] == body
    assert forward["request_id"] in w.relay.pending

    w.relay.disconnect(sc.ex)
    if mutation:
        expected = {"state": "outcome_unknown", "target": sc.target, "command_id": body["command_id"],
                    "code": "outcome_unknown"}
        outcomes = [row["outcome"] for row in w.ai.audit_rows() if row["command_id"] == body["command_id"]]
        assert outcomes == ["forwarded", "outcome_unknown"]
    else:
        expected = {"state": "rejected", "target": sc.target, "code": "not_available"}
    assert w.one(sc.rq) == {"acp": 1, "type": "response", "request_id": rid, "body": expected}
    assert w.relay.pending == {}


# ============================================================== 6. revoke race


@pytest.mark.parametrize("mutation", [False, True], ids=["read", "mutation"])
def test_grant_revoked_between_forward_and_response_reveals_nothing(w: World, mutation: bool) -> None:
    sc = w.scene()
    body = w.mutation(sc) if mutation else w.read(sc)
    rid, forward = w.forwarded(sc, body)
    w.ask(sc.ex, "grant.revoke", w.revoke_body(sc.export, sc.grant_id))
    response = {"target": sc.target, "state": "completed", "result": {"title": "secret"}}
    if mutation:
        response["command_id"] = body["command_id"]
    w.send(sc.ex, "response", response, rid=forward["request_id"])
    answer = w.one(sc.rq)
    assert answer["request_id"] == rid
    expected = {"state": "outcome_unknown" if mutation else "rejected", "target": sc.target, "code": "denied"}
    if mutation:
        expected["command_id"] = body["command_id"]
    assert answer["body"] == expected and "result" not in answer["body"]
    if mutation:
        assert [r["outcome"] for r in w.ai.audit_rows()] == ["forwarded", "denied_after_forward"]


def test_grant_expiry_between_forward_and_response_reveals_nothing(w: World) -> None:
    sc = w.scene(expires_in_ms=5_000)
    rid, forward = w.forwarded(sc, w.read(sc))
    w.clock.advance(5)  # now >= expires_at
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "result": {"title": "secret"}},
           rid=forward["request_id"])
    assert w.one(sc.rq)["body"] == {"state": "rejected", "target": sc.target, "code": "denied"}


def test_revoke_ends_subscriptions_on_both_sides(w: World) -> None:
    sc = w.scene()
    subscription_id = w.subscribe(sc)
    w.send(sc.ex, "grant.revoke", w.revoke_body(sc.export, sc.grant_id))
    end = {"subscription_id": subscription_id, "target": sc.target, "resource_id": "task_1", "code": "denied"}
    executor_frames = w.drain(sc.ex)
    assert [f["body"] for f in executor_frames if f["type"] == "event"] == [end]
    assert [f["body"]["state"] for f in executor_frames if f["type"] == "response"] == ["completed"]
    assert [f["body"] for f in w.drain(sc.rq)] == [end]
    assert subscription_id not in w.relay.subscriptions
    # Later events for it are dropped, not buffered.
    w.send(sc.ex, "event", w.event(sc, subscription_id, 1))
    assert w.drain(sc.rq) == []


def test_grant_expiry_ends_subscriptions_on_tick(w: World) -> None:
    sc = w.scene(expires_in_ms=5_000)
    subscription_id = w.subscribe(sc)
    w.clock.advance(5)
    w.relay.tick()
    end = {"subscription_id": subscription_id, "target": sc.target, "resource_id": "task_1", "code": "denied"}
    assert [f["body"] for f in w.drain(sc.rq)] == [end]
    assert [f["body"] for f in w.drain(sc.ex)] == [end]


def test_event_with_an_expired_grant_ends_the_subscription(w: World) -> None:
    sc = w.scene(expires_in_ms=5_000)
    subscription_id = w.subscribe(sc)
    w.clock.advance(5)
    w.send(sc.ex, "event", w.event(sc, subscription_id, 0, payload={"status": "secret"}))
    frames = w.drain(sc.rq)
    assert [f["body"].get("code") for f in frames] == ["denied"]
    assert "secret" not in json.dumps(frames)


# ============================================================== 7. limits


def test_seventeenth_in_flight_request_is_rate_limited(w: World) -> None:
    sc = w.scene()
    for _ in range(16):
        w.send(sc.rq, "request", w.read(sc))
        w.clock.advance(0.5)
    assert len(w.drain(sc.ex)) == 16
    assert w.drain(sc.rq) == []
    w.clock.advance(1)  # buckets have tokens again: the cap is the in-flight one
    rid = w.send(sc.rq, "request", w.read(sc))
    assert w.one(sc.rq) == {"acp": 1, "type": "response", "request_id": rid, "body": {
        "state": "rejected", "target": sc.target, "code": "rate_limited", "retry_after": 1}}
    assert w.drain(sc.ex) == []


def test_read_rate_is_four_per_second_burst_eight(w: World) -> None:
    sc = w.scene()
    for _ in range(8):
        w.send(sc.rq, "request", w.read(sc))
    assert len(w.drain(sc.ex)) == 8
    w.send(sc.rq, "request", w.read(sc))
    assert w.one(sc.rq)["body"]["code"] == "rate_limited"
    assert w.drain(sc.ex) == []
    w.clock.advance(0.25)
    w.send(sc.rq, "request", w.read(sc))
    assert len(w.drain(sc.ex)) == 1 and w.drain(sc.rq) == []


def test_mutation_rate_is_one_per_second_burst_four(w: World) -> None:
    sc = w.scene()
    for _ in range(4):
        w.send(sc.rq, "request", w.mutation(sc))
    assert len(w.drain(sc.ex)) == 4
    command = w.mutation(sc)
    w.send(sc.rq, "request", command)
    answer = w.one(sc.rq)["body"]
    assert answer["code"] == "rate_limited" and answer["retry_after"] == 1
    assert answer["command_id"] == command["command_id"]  # same command id, retry later
    # Reads on the same export have their own bucket.
    w.send(sc.rq, "request", w.read(sc))
    assert len(w.drain(sc.ex)) == 1
    w.clock.advance(1)
    w.send(sc.rq, "request", command)
    assert w.one(sc.ex)["body"]["request"]["command_id"] == command["command_id"]


def test_principal_rate_is_twenty_per_second_burst_forty(w: World) -> None:
    alice, bob = w.person("Alice"), w.person("Bob")
    executor, requester = w.device(alice), w.device(bob, "android")
    ex = w.connect(executor)
    w.attach(ex)
    targets = []
    for n in range(6):
        export = w.publish(ex, persistent_id=f"game{n}")
        targets.append((w.target(executor, export), w.grant(executor, ex, export, requester)))
    w.relay.disconnect(ex)  # offline: every request is answered at once, none is in flight
    rq = w.connect(requester)

    def request(target, grant_id):
        w.send(rq, "request", {"target": target, "grant_id": grant_id, "grant_revision": 1,
                               "op": "resource.list", "payload": {}})
        return w.one(rq)["body"]["code"]

    for target, grant_id in targets[:5]:
        assert [request(target, grant_id) for _ in range(8)] == ["offline"] * 8
    # 40 requests spent the principal's burst; a fresh export bucket does not help.
    assert request(*targets[5]) == "rate_limited"
    # Another principal is unaffected.
    carol_device = w.device(w.person("Carol"), "android")
    carol = w.connect(carol_device)
    w.send(carol, "request", {"target": targets[5][0], "grant_id": targets[5][1], "grant_revision": 1,
                              "op": "resource.list", "payload": {}})
    assert w.one(carol)["body"]["code"] == "not_available"
    w.clock.advance(0.06)  # 20/s → one token (0.05 s is a hair short in float)
    assert request(*targets[5]) == "offline"


def test_subscriptions_are_capped_per_source(w: World) -> None:
    sc = w.scene()
    for n in range(8):
        w.subscribe(sc)
        if n == 7:
            w.clock.advance(1)
    rid = w.send(sc.rq, "request", w.read(sc, "stream.subscribe", payload={"topic": "status"}))
    assert w.one(sc.rq) == {"acp": 1, "type": "response", "request_id": rid, "body": {
        "state": "rejected", "target": sc.target, "code": "capacity_exceeded"}}
    assert w.drain(sc.ex) == []
    # A second socket of the same registration is the same source.
    rq2 = w.connect(sc.requester)
    w.send(rq2, "request", w.read(sc, "stream.subscribe", payload={"topic": "status"}))
    assert w.one(rq2)["body"]["code"] == "capacity_exceeded"


def test_subscriptions_are_capped_per_export(w: World) -> None:
    sc = w.scene()
    bob = sc.requester.person
    for n in range(4):
        device = sc.requester if n == 0 else w.device(bob, "android")
        rq = sc.rq if n == 0 else w.connect(device)
        grant_id = sc.grant_id if n == 0 else w.grant(sc.executor, sc.ex, sc.export, device)
        for _ in range(8):
            w.subscribe(sc, rq, grant_id)
    assert len(w.relay.subscriptions) == 32
    fifth = w.device(bob, "android")
    rq5 = w.connect(fifth)
    grant5 = w.grant(sc.executor, sc.ex, sc.export, fifth)
    w.clock.advance(2)
    w.send(rq5, "request", w.read(sc, "stream.subscribe", payload={"topic": "status"}, grant_id=grant5))
    assert w.one(rq5)["body"]["code"] == "capacity_exceeded"


def test_transcript_subscription_needs_view_transcript(w: World) -> None:
    sc = w.scene(ops=["view_status"])
    w.send(sc.rq, "request", w.read(sc, "stream.subscribe", payload={"topic": "transcript"}))
    assert w.one(sc.rq)["body"]["code"] == "denied"
    w.send(sc.rq, "request", w.read(sc, "transcript.page"))
    assert w.one(sc.rq)["body"]["code"] == "denied"
    assert w.drain(sc.ex) == []


def test_stream_faster_than_twenty_frames_per_second_ends_with_resync(w: World) -> None:
    sc = w.scene()
    subscription_id = w.subscribe(sc)
    for seq in range(21):
        w.send(sc.ex, "event", w.event(sc, subscription_id, seq))
    end = {"subscription_id": subscription_id, "target": sc.target, "resource_id": "task_1",
           "code": "resync_required"}
    # Deltas already buffered are not released after the gap (§8): only the end marker is.
    assert sc.rq.queue.qsize() == 21
    assert [f["body"] for f in w.drain(sc.rq)] == [end]
    assert [f["body"] for f in w.drain(sc.ex)] == [end]
    # Nothing after the gap is delivered as if continuous.
    w.clock.advance(1)
    w.send(sc.ex, "event", w.event(sc, subscription_id, 21))
    assert w.drain(sc.rq) == []


def test_slow_requester_buffer_over_128_frames_ends_with_resync(w: World) -> None:
    sc = w.scene()
    subscription_id = w.subscribe(sc)
    seq = 0
    while subscription_id in w.relay.subscriptions:
        for _ in range(20):
            w.send(sc.ex, "event", w.event(sc, subscription_id, seq))
            seq += 1
            if subscription_id not in w.relay.subscriptions:
                break
        w.clock.advance(1)
        w.send(sc.ex, "heartbeat.ping", {"nonce": U(), "executor_epoch": sc.epoch})
        w.drain(sc.ex)
    assert seq == 129  # the 129th buffered frame ended it
    assert sc.rq.queue.qsize() == 129  # 128 deltas + the end marker: bounded
    frames = w.drain(sc.rq)
    assert [f["body"].get("code") for f in frames] == ["resync_required"]
    assert sc.rq.queued_bytes == 0 and not sc.rq.closed


def test_draining_requester_keeps_its_stream(w: World) -> None:
    sc = w.scene()
    subscription_id = w.subscribe(sc)
    for second in range(10):
        for n in range(20):
            w.send(sc.ex, "event", w.event(sc, subscription_id, second * 20 + n))
        assert len(w.drain(sc.rq)) == 20
        w.clock.advance(1)
        w.send(sc.ex, "heartbeat.ping", {"nonce": U(), "executor_epoch": sc.epoch})
        w.drain(sc.ex)
    assert subscription_id in w.relay.subscriptions


def test_events_for_unknown_or_unsubscribed_subscriptions_are_dropped(w: World) -> None:
    sc = w.scene()
    w.send(sc.ex, "event", w.event(sc, U(), 0))
    assert w.drain(sc.rq) == [] and not sc.ex.closed
    subscription_id = w.subscribe(sc)
    rid, forward = w.forwarded(sc, w.read(sc, "stream.unsubscribe", resource_id=None,
                                         payload={"subscription_id": subscription_id}))
    assert subscription_id not in w.relay.subscriptions
    w.send(sc.ex, "event", w.event(sc, subscription_id, 0))
    assert w.drain(sc.rq) == []
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed"}, rid=forward["request_id"])
    assert w.one(sc.rq)["request_id"] == rid
    # Unsubscribing a subscription that is not yours is not available.
    w.send(sc.rq, "request", w.read(sc, "stream.unsubscribe", resource_id=None,
                                    payload={"subscription_id": subscription_id}))
    assert w.one(sc.rq)["body"]["code"] == "not_available"


def test_event_outside_its_subscription_is_a_protocol_error(w: World) -> None:
    sc = w.scene()
    subscription_id = w.subscribe(sc)
    with closes(4400):
        w.send(sc.ex, "event", w.event(sc, subscription_id, 0, resource_id="task_2"))
    other = w.connect(w.device(w.person("Mallory")))
    w.attach(other)
    with closes(4400):
        w.send(other, "event", w.event(sc, subscription_id, 0))


# ============================================================== 8. mirror lifecycle


def test_export_publish_ack_replay_and_conflicts(w: World) -> None:
    executor = w.device(w.person("Alice"))
    ex = w.connect(executor)
    w.attach(ex)
    body = w.export_body()
    ack = {"export_id": body["export_id"], "revision": 1, "hash": body["hash"], "state": "published"}
    assert w.ask(ex, "export.publish", body) == {"state": "completed", "result": ack}
    assert w.ask(ex, "export.publish", body) == {"state": "completed", "result": ack}  # lost ACK replay
    changed = w.export_body(body["export_id"], display_name="Renamed")
    assert w.ask(ex, "export.publish", changed) == {"state": "rejected", "code": "revision_conflict", "result": ack}
    skip = w.export_body(body["export_id"], revision=3, expected=2)
    assert w.ask(ex, "export.publish", skip)["code"] == "revision_conflict"
    second = w.export_body(body["export_id"], revision=2, expected=1, display_name="Renamed")
    ack2 = {"export_id": body["export_id"], "revision": 2, "hash": second["hash"], "state": "published"}
    assert w.ask(ex, "export.publish", second)["result"] == ack2
    assert w.ask(ex, "export.publish", body) == {"state": "rejected", "code": "revision_conflict", "result": ack2}
    other_identity = w.export_body(body["export_id"], revision=3, expected=2, storage_epoch="e2")
    assert w.ask(ex, "export.publish", other_identity)["code"] == "stale_target"
    duplicate = w.export_body(persistent_id="game")
    assert w.ask(ex, "export.publish", duplicate)["code"] == "duplicate_export"
    bad_hash = dict(w.export_body(), hash="f" * 64)
    with closes(4400):
        w.send(ex, "export.publish", bad_hash)


def test_grant_put_revoke_and_mirror_get(w: World) -> None:
    sc = w.scene()
    grant = w.ai.grant(sc.grant_id)
    active = {"export_id": sc.export["export_id"], "export_revision": 1, "grant_id": sc.grant_id,
              "revision": 1, "hash": grant.hash, "state": "active"}
    export_ack = {"export_id": sc.export["export_id"], "revision": 1, "hash": sc.export["hash"],
                  "state": "published"}
    assert w.ask(sc.ex, "mirror.get", {"export_id": sc.export["export_id"]}) == {
        "state": "completed", "result": {"export": export_ack}}
    assert w.ask(sc.ex, "mirror.get", {"export_id": sc.export["export_id"], "grant_id": sc.grant_id}) == {
        "state": "completed", "result": {"export": export_ack, "grant": active}}
    # Identical put → same ACK; a different record at the same revision → conflict.
    replay = w.grant_body(sc.executor, sc.export, sc.requester, grant_id=sc.grant_id)
    replay["grant"] = w.ai.grant(sc.grant_id).record
    replay["hash"] = grant.hash
    assert w.ask(sc.ex, "grant.put", replay) == {"state": "completed", "result": active}
    narrowed = w.grant_body(sc.executor, sc.export, sc.requester, grant_id=sc.grant_id, ops=["view_status"])
    assert w.ask(sc.ex, "grant.put", narrowed) == {"state": "rejected", "code": "revision_conflict",
                                                   "result": active}
    revoke = w.revoke_body(sc.export, sc.grant_id)
    revoked = {**active, "revision": 2, "hash": revoke["hash"], "state": "revoked"}
    assert w.ask(sc.ex, "grant.revoke", revoke) == {"state": "completed", "result": revoked}
    assert w.ask(sc.ex, "grant.revoke", revoke) == {"state": "completed", "result": revoked}
    # A revoked grant never comes back through put.
    again = w.grant_body(sc.executor, sc.export, sc.requester, grant_id=sc.grant_id, revision=3, expected=2)
    assert w.ask(sc.ex, "grant.put", again)["code"] == "revision_conflict"
    assert w.ask(sc.ex, "mirror.get", {"export_id": sc.export["export_id"], "grant_id": sc.grant_id})[
        "result"]["grant"] == revoked
    assert w.ask(sc.ex, "mirror.get", {"export_id": U()}) == {"state": "rejected", "code": "not_available"}


def test_grant_for_a_dead_or_foreign_grantee_is_not_available(w: World) -> None:
    sc = w.scene()
    stranger = w.device(w.person("Carol"), "android")
    body = w.grant_body(sc.executor, sc.export, stranger)
    body["grant"]["grantee_user_id"] = sc.requester.person.row.id  # mismatched user/registration
    body["hash"] = p.semantic_hash(p.grant_record(sc.export["export_id"], 1, body["grant"]))
    assert w.ask(sc.ex, "grant.put", body)["code"] == "not_available"
    w.ai.revoke_registration(stranger.person.row.id, stranger.registration_id, w.clock.wall_ms())
    assert w.ask(sc.ex, "grant.put", w.grant_body(sc.executor, sc.export, stranger))["code"] == "not_available"


def test_attach_manifest_reports_mirror_status(w: World) -> None:
    sc = w.scene()
    second = w.publish(sc.ex, persistent_id="other")
    w.relay.disconnect(sc.ex)
    missing = U()
    ex = w.connect(sc.executor)
    attached = w.attach(ex, manifest=[
        {"export_id": sc.export["export_id"], "revision": 1, "hash": sc.export["hash"]},
        {"export_id": second["export_id"], "revision": 1, "hash": "a" * 64},
        {"export_id": missing, "revision": 1, "hash": "b" * 64},
    ])["result"]
    assert attached["lease_seconds"] == 15
    assert attached["mirrors"] == [
        {"export_id": sc.export["export_id"], "revision": 1, "hash": sc.export["hash"],
         "state": "published", "mirror_status": "matched"},
        {"export_id": second["export_id"], "revision": 1, "hash": second["hash"],
         "state": "published", "mirror_status": "mismatched"},
        {"export_id": missing, "mirror_status": "missing"},
    ]
    # A foreign executor's manifest never learns about someone else's export.
    foreign = w.connect(w.device(w.person("Mallory")))
    assert w.attach(foreign, manifest=[{"export_id": sc.export["export_id"], "revision": 1,
                                        "hash": sc.export["hash"]}])["result"]["mirrors"] == [
        {"export_id": sc.export["export_id"], "mirror_status": "missing"}]


def test_attach_manifest_is_bounded_and_unique(w: World) -> None:
    connection = w.connect(w.device(w.person("Alice")))
    entry = {"export_id": U(), "revision": 1, "hash": "a" * 64}
    with closes(4400):
        w.attach(connection, manifest=[entry, dict(entry)])
    connection = w.connect(w.device(w.person("Bob")))
    with closes(4400):
        w.attach(connection, manifest=[{"export_id": U(), "revision": 1, "hash": "a" * 64} for _ in range(17)])


def test_resource_index_lifecycle(w: World) -> None:
    executor = w.device(w.person("Alice"))
    ex = w.connect(executor)
    w.attach(ex)
    export = w.publish(ex)
    rows = w.rows(*[f"task_{n}" for n in range(60)])
    committed = w.index(ex, export, rows)
    assert committed == {"state": "completed", "result": {
        "export_id": export["export_id"], "export_revision": 1, "index_revision": 1,
        "hash": p.index_rows_hash(rows), "row_count": 60, "state": "committed"}}
    # Wrong page order → resync_required, staging dropped.
    digest = p.index_rows_hash(rows)
    begin = {"export_id": export["export_id"], "export_revision": 1, "index_revision": 2,
             "row_count": 60, "hash": digest}
    w.ask(ex, "resource.index.begin", begin)
    assert w.ask(ex, "resource.index.page", {"index_revision": 2, "page_no": 1, "rows": rows[:10]}) == {
        "state": "rejected", "code": "resync_required"}
    assert w.ask(ex, "resource.index.commit", {"index_revision": 2, "hash": digest})["code"] == "resync_required"
    # Count mismatch → snapshot_changed.
    w.ask(ex, "resource.index.begin", begin)
    w.ask(ex, "resource.index.page", {"index_revision": 2, "page_no": 0, "rows": rows[:20]})
    w.ask(ex, "resource.index.page", {"index_revision": 2, "page_no": 1, "rows": rows[20:40]})
    assert w.ask(ex, "resource.index.commit", {"index_revision": 2, "hash": digest})["code"] == "snapshot_changed"
    # Hash mismatch → snapshot_changed.
    w.ask(ex, "resource.index.begin", begin)
    for page_no in range(3):
        w.ask(ex, "resource.index.page", {"index_revision": 2, "page_no": page_no,
                                          "rows": rows[page_no * 20:page_no * 20 + 20]})
    assert w.ask(ex, "resource.index.commit", {"index_revision": 2, "hash": "c" * 64})["code"] == "snapshot_changed"
    # Rows that do not hash to the announced digest → snapshot_changed at commit.
    lie = dict(begin, index_revision=3, row_count=1, hash=p.index_rows_hash(w.rows("x")))
    w.ask(ex, "resource.index.begin", lie)
    w.ask(ex, "resource.index.page", {"index_revision": 3, "page_no": 0, "rows": w.rows("y")})
    assert w.ask(ex, "resource.index.commit", {"index_revision": 3, "hash": lie["hash"]})["code"] == "snapshot_changed"
    # More rows than announced → capacity_exceeded.
    w.ask(ex, "resource.index.begin", dict(begin, index_revision=4, row_count=1))
    assert w.ask(ex, "resource.index.page", {"index_revision": 4, "page_no": 0, "rows": rows[:2]})[
        "code"] == "capacity_exceeded"
    # A stale export revision cannot begin; the committed index is still revision 1.
    assert w.ask(ex, "resource.index.begin", dict(begin, export_revision=2))["code"] == "stale_target"
    assert w.ai.index_row(export["export_id"], 1, "task_59") is not None
    assert w.ai.index_row(export["export_id"], 1, "y") is None


def test_resource_index_page_and_row_bounds(w: World) -> None:
    executor = w.device(w.person("Alice"))
    ex = w.connect(executor)
    w.attach(ex)
    export = w.publish(ex)
    rows = w.rows(*[f"task_{n}" for n in range(41)])
    w.ask(ex, "resource.index.begin", {"export_id": export["export_id"], "export_revision": 1,
                                       "index_revision": 1, "row_count": 41, "hash": p.index_rows_hash(rows)})
    # 4 frame + 3 body + 20 * 6 row fields = 127 ≤ 128: the largest page that fits.
    assert w.ask(ex, "resource.index.page", {"index_revision": 1, "page_no": 0, "rows": rows[:20]})[
        "state"] == "completed"
    with closes(4400):  # 21 rows = 133 fields
        w.send(ex, "resource.index.page", {"index_revision": 1, "page_no": 1, "rows": rows[20:41]})
    ex = w.connect(w.device(w.person("Bob")))
    with closes(4400):  # row_count over 1024
        w.attach(ex)
        w.send(ex, "resource.index.begin", {"export_id": export["export_id"], "export_revision": 1,
                                            "index_revision": 1, "row_count": 1025, "hash": "a" * 64})


def test_index_pages_are_bound_by_the_frame_field_budget_not_fifty_rows(w: World) -> None:
    """Design §8 caps a frame at 128 fields in total, which binds before "pages ≤50":
    the stricter frame rule wins, so an executor sends index pages of ≤20 null-origin rows."""
    executor = w.device(w.person("Alice"))
    ex = w.connect(executor)
    w.attach(ex)
    export = w.publish(ex)
    rows = w.rows(*[f"task_{n}" for n in range(50)])
    w.ask(ex, "resource.index.begin", {"export_id": export["export_id"], "export_revision": 1,
                                       "index_revision": 1, "row_count": 50, "hash": p.index_rows_hash(rows)})
    with closes(4400):
        w.send(ex, "resource.index.page", {"index_revision": 1, "page_no": 0, "rows": rows})


def test_a_directory_page_decodes_under_acp_frame_limits(w: World) -> None:
    executor, requester = w.device(w.person("Alice")), w.device(w.person("Bob"), "android")
    ex, rq = w.connect(executor), w.connect(requester)
    w.attach(ex)
    export = w.publish(ex)
    for _ in range(10):
        w.grant(executor, ex, export, requester)
    seen, after = [], None
    while True:
        w.send(rq, "directory.list", {} if after is None else {"after": after})
        item = rq.queue.get_nowait()
        rq.dequeued(item)
        page = p.decode(item.text)["body"]["result"]  # what a strict peer decoder does
        seen += [target["grant"]["grant_id"] for target in page["targets"]]
        after = page["next_after"]
        if after is None:
            break
    assert len(seen) == len(set(seen)) == 10


def test_a_foreign_executor_cannot_write_into_another_export(w: World) -> None:
    sc = w.scene()
    mallory = w.device(w.person("Mallory"))
    mx = w.connect(mallory)
    w.attach(mx)
    hijack = w.export_body(sc.export["export_id"], revision=2, expected=1, display_name="Mine")
    assert w.ask(mx, "export.publish", hijack) == {"state": "rejected", "code": "not_available"}
    replay = w.export_body(sc.export["export_id"])
    assert w.ask(mx, "export.publish", replay) == {"state": "rejected", "code": "not_available"}
    grant = w.grant_body(sc.executor, sc.export, sc.requester)
    assert w.ask(mx, "grant.put", grant) == {"state": "rejected", "code": "not_available"}
    assert w.ask(mx, "grant.revoke", w.revoke_body(sc.export, sc.grant_id)) == {
        "state": "rejected", "code": "not_available"}
    assert w.ask(mx, "mirror.get", {"export_id": sc.export["export_id"]}) == {
        "state": "rejected", "code": "not_available"}
    assert w.ask(mx, "resource.index.begin", {
        "export_id": sc.export["export_id"], "export_revision": 1, "index_revision": 9,
        "row_count": 0, "hash": p.index_rows_hash([])}) == {"state": "rejected", "code": "not_available"}
    unpublish = {"export_id": sc.export["export_id"], "expected_revision": 1, "revision": 2,
                 "hash": p.semantic_hash(p.unpublish_record(sc.export["export_id"], 2))}
    assert w.ask(mx, "export.unpublish", unpublish) == {"state": "rejected", "code": "not_available"}
    assert w.ai.export(sc.export["export_id"]).revision == 1
    assert w.ai.grant(sc.grant_id).state == "active"


def test_unpublish_revokes_grants_and_ends_streams(w: World) -> None:
    sc = w.scene()
    subscription_id = w.subscribe(sc)
    unpublish = {"export_id": sc.export["export_id"], "expected_revision": 1, "revision": 2,
                 "hash": p.semantic_hash(p.unpublish_record(sc.export["export_id"], 2))}
    rid = w.send(sc.ex, "export.unpublish", unpublish)
    frames = w.drain(sc.ex)
    assert [f["body"] for f in frames if f["request_id"] == rid] == [{"state": "completed", "result": {
        "export_id": sc.export["export_id"], "revision": 2, "hash": unpublish["hash"], "state": "unpublished"}}]
    assert [f["body"]["code"] for f in w.drain(sc.rq)] == ["denied"]
    assert subscription_id not in w.relay.subscriptions
    assert w.ai.grant(sc.grant_id).state == "revoked"
    w.send(sc.rq, "request", w.read(sc))
    assert w.one(sc.rq)["body"]["code"] in ("denied", "not_available")


# ============================================================== 9. directory


def test_directory_lists_only_targets_granted_to_this_device(w: World) -> None:
    sc = w.scene()
    other_device = w.device(sc.requester.person, "android")  # same user, other device: no grant
    other = w.connect(other_device)
    entry_grant = {"grant_id": sc.grant_id, "revision": 1, "operations": ALL_OPS, "team_ids": ["t1"],
                   "member_ids": [], "resource_scope": "all_resources_in_selected_teams",
                   "expires_at": w.ai.grant(sc.grant_id).expires_at}
    online = w.ask(sc.rq, "directory.list", {})
    assert online == {"state": "completed", "result": {"next_after": None, "targets": [{
        "target": sc.target, "display_name": "Game", "state": "online", "executor_epoch": sc.epoch,
        "grant": entry_grant}]}}
    assert w.ask(other, "directory.list", {})["result"] == {"targets": [], "next_after": None}
    # The executor itself sees nothing either: it publishes, it is not a grantee.
    assert w.ask(sc.ex, "directory.list", {})["result"]["targets"] == []
    w.relay.disconnect(sc.ex)
    offline = w.ask(sc.rq, "directory.list", {})["result"]["targets"]
    assert [(t["state"], t["executor_epoch"]) for t in offline] == [("offline", None)]
    w.ask(w.connect(sc.executor), "directory.list", {})  # unattached socket: still offline
    assert w.ask(sc.rq, "directory.list", {})["result"]["targets"][0]["state"] == "offline"


def test_directory_hides_revoked_and_expired_grants(w: World) -> None:
    sc = w.scene(expires_in_ms=5_000)
    assert len(w.ask(sc.rq, "directory.list", {})["result"]["targets"]) == 1
    w.clock.advance(5)
    w.send(sc.ex, "heartbeat.ping", {"nonce": U(), "executor_epoch": sc.epoch})
    assert w.ask(sc.rq, "directory.list", {})["result"]["targets"] == []


def test_directory_paginates(w: World) -> None:
    executor, requester = w.device(w.person("Alice")), w.device(w.person("Bob"), "android")
    ex, rq = w.connect(executor), w.connect(requester)
    w.attach(ex)
    export = w.publish(ex)
    grants = sorted(w.grant(executor, ex, export, requester) for _ in range(55))
    seen, after, pages = [], None, 0
    while True:
        page = w.ask(rq, "directory.list", {} if after is None else {"after": after})["result"]
        ids = [t["grant"]["grant_id"] for t in page["targets"]]
        assert ids and len(ids) <= 50
        seen += ids
        pages += 1
        after = page["next_after"]
        if after is None:
            break
        assert after == ids[-1]
    # Ordered, complete, no duplicates; pages bounded by the 128-field frame budget.
    assert seen == grants and pages > 2


# ============================================================== 10. close_user


def test_close_user_closes_only_that_users_stale_sockets(w: World) -> None:
    alice, bob = w.person("Alice"), w.person("Bob")
    a1, a2 = w.connect(w.device(alice)), w.connect(w.device(alice, "android"))
    b1 = w.connect(w.device(bob))
    w.rotate(alice)
    fresh = w.connect(w.device(alice))  # registered under the new key generation
    assert w.relay.close_user(alice.row.id, alice.row.credential_version) == 2
    assert a1.closed and a2.closed and a1.close_code == a2.close_code == 4401
    assert not b1.closed and not fresh.closed
    assert w.drain(a1) == [CLOSED]
    assert w.ask(fresh, "directory.list", {})["state"] == "completed"
    assert w.relay.close_user(alice.row.id, alice.row.credential_version) == 0


def test_rotation_retires_registrations_exports_and_grants(w: World) -> None:
    sc = w.scene()
    w.rotate(sc.executor.person)
    assert w.relay.close_user(sc.executor.person.row.id) == 1
    assert sc.ex.closed and sc.ex.close_code == 4401
    assert w.ai.export(sc.export["export_id"]).state == "deleted"
    assert w.ai.grant(sc.grant_id).state == "revoked"
    w.send(sc.rq, "request", w.read(sc))
    assert w.one(sc.rq)["body"]["code"] == "denied"
    assert w.ask(sc.rq, "directory.list", {})["result"]["targets"] == []


# ============================================================== 11. no body logging


def test_bodies_are_never_logged(w: World, caplog: pytest.LogCaptureFixture) -> None:
    canary = "CANARY-" + uuid.uuid4().hex
    caplog.set_level(logging.DEBUG)
    sc = w.scene()
    command = w.mutation(sc, "task.note", payload={"text": f"note {canary}"})
    rid, forward = w.forwarded(sc, command)
    assert canary in json.dumps(forward)
    w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "command_id": command["command_id"],
                               "result": {"echo": canary}}, rid=forward["request_id"])
    assert w.one(sc.rq)["body"]["result"] == {"echo": canary}
    subscription_id = w.subscribe(sc)
    w.send(sc.ex, "event", w.event(sc, subscription_id, 0, payload={"text": canary}))
    w.drain(sc.rq)
    with closes(4400):
        w.send(sc.rq, "request", dict(w.read(sc), payload={"text": canary}))
    logged = caplog.text + "".join(str(record.args) + str(record.msg) for record in caplog.records)
    assert canary not in logged
    assert canary not in json.dumps(w.ai.audit_rows())


def test_a_bad_subscribe_receipt_still_answers_the_requester(w: World) -> None:
    sc = w.scene()
    rid = w.send(sc.rq, "request", w.read(sc, "stream.subscribe", payload={"topic": "status"}))
    forward = w.one(sc.ex)
    with closes(4400):
        w.send(sc.ex, "response", {"target": sc.target, "state": "completed", "result": {}},
               rid=forward["request_id"])
    w.relay.disconnect(sc.ex, 4400, "protocol_error")  # what the route does with CloseConnection
    w.clock.advance(10)
    w.relay.tick()
    answers = [f for f in w.drain(sc.rq) if f["request_id"] == rid]
    assert len(answers) == 1 and answers[0]["body"]["state"] == "rejected"
