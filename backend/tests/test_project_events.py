"""Project event transport isolation/recovery (dev spec §5.3.2–5.3.3).

Real ASGI routing, auth and lifespan; no TCP server. The socket harness bounds every
wait and can stop an ASGI send deterministically to exercise a slow consumer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import pytest

from voxarium_studio.core.project import init_project
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.project_scope import resolve_project
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.explorer.watcher import FileWatcher
from voxarium_studio.main import create_app
from voxarium_studio.ws.bus import EventBus


@pytest.mark.parametrize("history_size", [0, 1, 3, 500])
async def test_replay_gap_retention_boundaries_and_scoped_serialization(history_size):
    bus = EventBus(history_size=history_size)
    assert bus.replay_gap(0) is None
    assert bus.replay_gap(1) == "cursor_ahead"
    for n in range(5):
        event = await bus.publish("test.item", {"n": n}, channel="test.topic")
    earliest_complete_cursor = max(0, 5 - history_size)
    for cursor in range(6):
        expected = "history_gap" if cursor < earliest_complete_cursor else None
        assert bus.replay_gap(cursor) == expected
    assert bus.replay_gap(6) == "cursor_ahead"
    plain = event.to_json()
    assert event.to_json(project_id="a", epoch="generation") == {
        **plain, "project_id": "a", "epoch": "generation",
    }
    assert event.to_json() == plain, "scoped serialization must not alter legacy events"
    bus.clear_history()
    assert bus.replay_gap(4) == "history_gap"
    assert bus.replay_gap(5) is None
    assert bus.current_seq == 5


@pytest.fixture(autouse=True)
def no_filesystem_noise(monkeypatch):
    """Only deliberate bus publications belong in these transport assertions."""
    monkeypatch.setattr(FileWatcher, "start", lambda self: None)
    monkeypatch.setattr(AppContext, "_start_plugin_hooks", lambda self: None)


class Socket:
    def __init__(self, app, url, headers=None):
        self.app, self.url = app, url
        self.headers = {"host": "127.0.0.1", **(headers or {})}
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.received = asyncio.Queue()
        self.block_type = None
        self.blocked, self.release = asyncio.Event(), asyncio.Event()

    async def __aenter__(self):
        parsed = urlsplit(self.url)
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"},
            "scheme": "ws", "path": parsed.path, "raw_path": parsed.path.encode(),
            "query_string": parsed.query.encode(), "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in self.headers.items()],
            "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 80),
            "subprotocols": [], "state": {},
        }

        async def send(message):
            if message["type"] == "websocket.send":
                frame = json.loads(message.get("text", "{}"))
                if frame.get("type") == self.block_type:
                    self.blocked.set()
                    await self.release.wait()
            await self.outgoing.put(message)

        async def receive():
            message = await self.incoming.get()
            if message["type"] == "websocket.receive":
                self.received.put_nowait(None)
            return message

        self.task = asyncio.create_task(self.app(scope, receive, send))
        await self.incoming.put({"type": "websocket.connect"})
        self.handshake = await self.raw()
        return self

    async def __aexit__(self, *exc):
        self.release.set()
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self.task, 3)
        finally:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def raw(self):
        return await asyncio.wait_for(self.outgoing.get(), 3)

    async def frame(self):
        message = await self.raw()
        assert message["type"] == "websocket.send", message
        return json.loads(message["text"])

    async def action(self, **data):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(data)})
        await asyncio.wait_for(self.received.get(), 3)


@asynccontextmanager
async def application(project, auth=None):
    app = create_app(str(project), auth=auth)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer secret"} if auth and auth.enabled else {},
        ) as http:
            yield app, http


async def opened(http, root):
    response = await http.post("/api/projects/open", json={"path": str(root)})
    assert response.status_code == 200, response.text
    return response.json()


def url(binding, **query):
    path = f"/ws/projects/{binding['project_id']}"
    return path + ("?" + urlencode(query) if query else "")


def scoped(frame, binding, kind):
    assert frame["type"] == kind, frame
    assert frame["project_id"] == binding["project_id"]
    assert frame["epoch"] == binding["epoch"]
    return frame


async def hello(socket, binding):
    assert socket.handshake["type"] == "websocket.accept", socket.handshake
    frame = scoped(await socket.frame(), binding, "system.hello")
    assert Path(frame["data"]["project"]) == Path(binding["root"])
    assert frame["data"]["project_id"] == binding["project_id"]
    assert frame["data"]["epoch"] == binding["epoch"]
    assert "resumed_from" in frame["data"]
    return frame


async def pong(socket, binding=None):
    await socket.action(action="ping")
    frame = await socket.frame()
    if binding:
        scoped(frame, binding, "system.pong")
    else:
        assert frame == {"type": "system.pong", "data": {}}


async def test_simultaneous_projects_local_subscribers_and_legacy_selection(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    init_project(other)
    async with application(project) as (app, http):
        a, b = await opened(http, project), await opened(http, other)
        ca, cb = (resolve_project(app.state, s["project_id"]).ctx for s in (a, b))
        await app.state.browser_bus.publish("test.browser-only")
        local_a, local_b = ca.bus.subscribe(), cb.bus.subscribe()
        try:
            async with Socket(app, url(a)) as wa, Socket(app, url(b)) as wb, Socket(app, "/ws") as legacy:
                assert (await hello(wa, a))["seq"] == ca.bus.current_seq
                assert (await hello(wb, b))["seq"] == cb.bus.current_seq
                assert app.state.browser_bus.current_seq > ca.bus.current_seq
                lh = await legacy.frame()
                assert lh == {"type": "system.hello", "seq": app.state.browser_bus.current_seq,
                              "data": {"project": str(project.resolve()), "resumed_from": None}}
                ea = await ca.bus.publish("test.a", {"owner": "a"}, channel="test.channel")
                eb = await cb.bus.publish("test.b", {"owner": "b"})
                assert await wa.frame() == ea.to_json(project_id=a["project_id"], epoch=a["epoch"])
                assert await wb.frame() == eb.to_json(project_id=b["project_id"], epoch=b["epoch"])
                legacy_a = await legacy.frame()
                assert set(legacy_a) == {"type", "seq", "ts", "data", "channel"}
                assert legacy_a["data"] == {"owner": "a"}
                assert await asyncio.wait_for(local_a.get(), 3) is ea
                assert await asyncio.wait_for(local_b.get(), 3) is eb

                response = await http.post("/api/project/open", json={"path": str(other)})
                assert response.status_code == 200
                assert (await legacy.frame())["type"] == "project.opened"
                ea = await ca.bus.publish("test.a.after")
                eb = await cb.bus.publish("test.b.after")
                scoped(await wa.frame(), a, ea.type)
                scoped(await wb.frame(), b, eb.type)
                assert (await legacy.frame())["type"] == eb.type
                await pong(wa, a)
                await pong(wb, b)
                await pong(legacy)
                assert ca.bus.forward_to is None
                assert cb.bus.forward_to is app.state.browser_bus
        finally:
            local_a.close()
            local_b.close()


@pytest.mark.parametrize("query,headers", [
    ({"project_id": "wrong"}, {}), ({"epoch": "stale"}, {}),
    ({}, {"x-voxarium-project-id": "wrong"}), ({}, {"x-voxarium-epoch": "stale"}),
    ({"project_id": ""}, {}), ({"epoch": ""}, {}),
])
async def test_conflicting_handshake_selectors_close_1008(project, query, headers):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        incumbent = app.state.ctx
        count = incumbent.bus.subscriber_count
        async with Socket(app, url(binding, **query), headers) as ws:
            assert ws.handshake["type"] == "websocket.close"
            assert ws.handshake["code"] == 1008
        assert app.state.ctx is incumbent
        assert incumbent.bus.subscriber_count == count


async def test_unknown_id_never_falls_back_or_creates_context(project):
    async with application(project) as (app, http):
        registry = dict(app.state.project_registry)
        async with Socket(app, "/ws/projects/unknown-runtime-id") as ws:
            assert ws.handshake["type"] == "websocket.close"
            assert ws.handshake["code"] == 1008
        assert app.state.project_registry == registry


@pytest.mark.parametrize("headers,query,allowed", [
    ({}, {}, False), ({}, {"token": "wrong"}, False),
    ({}, {"token": "secret"}, True),
    ({"authorization": "Bearer secret"}, {}, True),
    ({"origin": "http://evil.example"}, {"token": "secret"}, False),
    ({"origin": "null"}, {"token": "secret"}, False),
    ({"origin": "http://127.0.0.1"}, {"token": "secret"}, True),
])
async def test_scoped_auth_and_origin_policy(project, headers, query, allowed):
    async with application(project, AuthSettings(token="secret")) as (app, http):
        binding = await opened(http, project)
        async with Socket(app, url(binding, **query), headers) as ws:
            if allowed:
                await hello(ws, binding)
            else:
                assert ws.handshake["type"] == "websocket.close"
                assert ws.handshake["code"] == 1008


async def test_unauthenticated_scoped_host_rebinding_is_rejected(project):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        async with Socket(app, url(binding), {"host": "evil.example"}) as ws:
            assert ws.handshake["type"] == "websocket.close"
            assert ws.handshake["code"] == 1008


async def test_reconnect_replays_only_local_cursor_then_continues_live(project):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        bus = resolve_project(app.state, binding["project_id"]).ctx.bus
        async with Socket(app, url(binding)) as ws:
            await hello(ws, binding)
            first = await bus.publish("test.first")
            scoped(await ws.frame(), binding, first.type)
        second = await bus.publish("test.second")
        async with Socket(app, url(binding, project_id=binding["project_id"], epoch=binding["epoch"], since_seq=first.seq)) as ws:
            h = await hello(ws, binding)
            assert h["data"]["resumed_from"] == first.seq
            assert await ws.frame() == second.to_json(project_id=binding["project_id"], epoch=binding["epoch"])
            await bus.publish("test.live")
            scoped(await ws.frame(), binding, "test.live")
            await pong(ws, binding)


@pytest.mark.parametrize("case,reason", [
    ("missing_project", "scope_mismatch"), ("missing_epoch", "scope_mismatch"),
    ("foreign_project", "scope_mismatch"), ("old_epoch", "scope_mismatch"),
    ("invalid", "invalid_cursor"), ("negative", "invalid_cursor"),
    ("ahead", "cursor_ahead"), ("gap", "history_gap"),
])
async def test_bad_reconnect_cursor_resyncs_without_partial_replay(project, case, reason):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        bus = resolve_project(app.state, binding["project_id"]).ctx.bus
        for _ in range(3):
            await bus.publish("test.history")
        query = dict(project_id=binding["project_id"], epoch=binding["epoch"], since_seq=0)
        if case == "missing_project":
            query.pop("project_id")
        elif case == "missing_epoch":
            query.pop("epoch")
        elif case == "foreign_project":
            query["project_id"] = "another-runtime-id"
        elif case == "old_epoch":
            query["epoch"] = "previous-backend"
        elif case == "invalid":
            query["since_seq"] = "1.5"
        elif case == "negative":
            query["since_seq"] = -1
        elif case == "ahead":
            query["since_seq"] = bus.current_seq + 1
        elif case == "gap":
            bus.clear_history()
        async with Socket(app, url(binding, **query)) as ws:
            await hello(ws, binding)
            dropped = scoped(await ws.frame(), binding, "system.dropped")
            assert dropped["data"]["resync"] is True
            assert dropped["data"]["reason"] == reason
            await bus.publish("test.after-gap")
            scoped(await ws.frame(), binding, "test.after-gap")
            await pong(ws, binding)


@pytest.mark.parametrize("changes,reason", [
    ({"project_id": "other"}, "scope_mismatch"), ({"epoch": "old"}, "scope_mismatch"),
    ({"project_id": None}, "scope_mismatch"), ({"epoch": None}, "scope_mismatch"),
    ({"since_seq": "0"}, "invalid_cursor"), ({"since_seq": True}, "invalid_cursor"),
    ({"since_seq": None}, "invalid_cursor"), ({"since_seq": 0.5}, "invalid_cursor"),
    ({"since_seq": -1}, "invalid_cursor"), ({"since_seq": 999999}, "cursor_ahead"),
])
async def test_replay_action_rejects_foreign_scope_and_noninteger_cursor(project, changes, reason):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        bus = resolve_project(app.state, binding["project_id"]).ctx.bus
        async with Socket(app, url(binding)) as ws:
            await hello(ws, binding)
            event = await bus.publish("test.history")
            await ws.frame()
            action = dict(action="replay", project_id=binding["project_id"], epoch=binding["epoch"], since_seq=event.seq - 1)
            action.update(changes)
            await ws.action(**action)
            frame = scoped(await ws.frame(), binding, "system.dropped")
            assert frame["data"]["reason"] == reason
            assert frame["data"]["resync"] is True
            await bus.publish("test.continues")
            scoped(await ws.frame(), binding, "test.continues")
            await pong(ws, binding)


async def test_explicit_replay_is_scoped_ordered_and_live_continues(project):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        bus = resolve_project(app.state, binding["project_id"]).ctx.bus
        async with Socket(app, url(binding)) as ws:
            await hello(ws, binding)
            events = [await bus.publish("test.item", {"index": n}) for n in range(4)]
            for event in events:
                assert (await ws.frame())["seq"] == event.seq
            await ws.action(action="replay", project_id=binding["project_id"], epoch=binding["epoch"], since_seq=events[0].seq)
            for event in events[1:]:
                assert await ws.frame() == event.to_json(project_id=binding["project_id"], epoch=binding["epoch"])
            await bus.publish("test.after-replay")
            scoped(await ws.frame(), binding, "test.after-replay")
            await pong(ws, binding)


async def test_fresh_scoped_connection_does_not_replay_history(project):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        bus = resolve_project(app.state, binding["project_id"]).ctx.bus
        await bus.publish("test.must-not-replay")
        async with Socket(app, url(binding, epoch=binding["epoch"], project_id=binding["project_id"]),
                          {"x-voxarium-project-id": binding["project_id"], "x-voxarium-epoch": binding["epoch"]}) as ws:
            h = await hello(ws, binding)
            assert h["data"]["resumed_from"] is None
            await pong(ws, binding)


async def test_replay_replaces_queued_backlog_without_duplicates(project):
    """A replay arriving during a blocked live send replaces, not appends to, backlog."""
    async with application(project) as (app, http):
        binding = await opened(http, project)
        bus = resolve_project(app.state, binding["project_id"]).ctx.bus
        async with Socket(app, url(binding)) as ws:
            await hello(ws, binding)
            ws.block_type = "test.block"
            first = await bus.publish("test.block")
            await asyncio.wait_for(ws.blocked.wait(), 3)
            events = [await bus.publish("test.queued", {"n": n}) for n in range(4)]
            await ws.action(action="replay", project_id=binding["project_id"], epoch=binding["epoch"], since_seq=first.seq)
            ws.release.set()
            scoped(await ws.frame(), binding, "test.block")
            for event in events:
                assert await ws.frame() == event.to_json(project_id=binding["project_id"], epoch=binding["epoch"])
            await bus.publish("test.after-replacement")
            scoped(await ws.frame(), binding, "test.after-replacement")
            await pong(ws, binding)


async def test_replay_action_history_gap_resyncs_and_continues(project):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        bus = resolve_project(app.state, binding["project_id"]).ctx.bus
        async with Socket(app, url(binding)) as ws:
            await hello(ws, binding)
            event = await bus.publish("test.gone")
            await ws.frame()
            bus.clear_history()
            await ws.action(action="replay", project_id=binding["project_id"], epoch=binding["epoch"], since_seq=event.seq - 1)
            frame = scoped(await ws.frame(), binding, "system.dropped")
            assert frame["data"]["reason"] == "history_gap"
            assert frame["data"]["resync"] is True
            await bus.publish("test.after-gap")
            scoped(await ws.frame(), binding, "test.after-gap")


async def test_bounded_overflow_notifies_only_slow_scoped_consumer(project):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        bus = resolve_project(app.state, binding["project_id"]).ctx.bus
        bus._queue_size = 2
        async with Socket(app, url(binding)) as slow, Socket(app, url(binding)) as fast:
            await hello(slow, binding)
            await hello(fast, binding)
            slow.block_type = "test.block"
            await bus.publish("test.block")
            await asyncio.wait_for(slow.blocked.wait(), 3)
            await fast.frame()
            for n in range(5):
                await bus.publish("test.burst", {"index": n})
                scoped(await fast.frame(), binding, "test.burst")
            slow.release.set()
            scoped(await slow.frame(), binding, "test.block")
            dropped = scoped(await slow.frame(), binding, "system.dropped")
            assert dropped["data"]["resync"] is True
            assert dropped["data"]["count"] == 3
            assert [(await slow.frame())["data"]["index"] for _ in range(2)] == [3, 4]
            await pong(slow, binding)
            await pong(fast, binding)


async def test_backend_restart_rejects_old_runtime_id_and_old_epoch(project):
    async with application(project) as (_, http):
        old = await opened(http, project)
    async with application(project) as (app, http):
        new = await opened(http, project)
        assert (old["project_id"], old["epoch"]) != (new["project_id"], new["epoch"])
        assert old["epoch"] != new["epoch"]
        async with Socket(app, url(old)) as ws:
            assert ws.handshake["type"] == "websocket.close"
            assert ws.handshake["code"] == 1008
        async with Socket(app, url(new, epoch=old["epoch"])) as ws:
            assert ws.handshake["type"] == "websocket.close"
            assert ws.handshake["code"] == 1008


async def test_legacy_replay_and_ping_keep_exact_unscoped_envelope(project):
    async with application(project) as (app, _):
        bus = app.state.browser_bus
        first = await bus.publish("test.legacy", {"n": 1})
        async with Socket(app, "/ws?since_seq=0") as ws:
            h = await ws.frame()
            assert h == {"type": "system.hello", "seq": bus.current_seq,
                         "data": {"project": str(project.resolve()), "resumed_from": 0}}
            assert await ws.frame() == first.to_json()
            await ws.action(action="replay", since_seq=0)
            assert await ws.frame() == first.to_json()
            await pong(ws)


async def test_legacy_overflow_keeps_exact_dropped_envelope(project):
    async with application(project) as (app, _):
        bus = app.state.browser_bus
        bus._queue_size = 2
        async with Socket(app, "/ws") as ws:
            await ws.frame()
            ws.block_type = "test.block"
            await bus.publish("test.block")
            await asyncio.wait_for(ws.blocked.wait(), 3)
            for n in range(5):
                await bus.publish("test.burst", {"n": n})
            ws.release.set()
            assert (await ws.frame())["type"] == "test.block"
            assert await ws.frame() == {"type": "system.dropped", "data": {"count": 3, "resync": True}}
            for n in (3, 4):
                frame = await ws.frame()
                assert frame["data"] == {"n": n}
                assert set(frame) == {"type", "seq", "ts", "data"}
            await pong(ws)
