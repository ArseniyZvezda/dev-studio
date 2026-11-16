"""App-lifetime desktop events, without project renderers (dev spec §5.3.6).

Reuse the project transport's real ASGI/lifespan harness; no server or agent SDK.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from test_project_events import Socket, application, opened
from test_project_events import no_filesystem_noise  # noqa: F401 -- activate the shared fixture

from voxarium_studio.core.project import init_project
from voxarium_studio.core.project_scope import resolve_project
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.core.workspace import DesktopEventSource
from voxarium_studio.models.teams import AgentChat
from voxarium_studio.ws.bus import DEFAULT_HISTORY_SIZE, DEFAULT_QUEUE_SIZE
from voxarium_studio.ws.events import EventType


async def desktop_hello(ws, epoch, *, resumed=None):
    assert ws.handshake["type"] == "websocket.accept", ws.handshake
    frame = await ws.frame()
    assert frame["type"] == "system.hello"
    assert frame["epoch"] == frame["data"]["epoch"] == epoch
    assert frame["data"]["resumed_from"] == resumed
    assert ("attention" in frame["data"]) == (resumed is None)
    assert "project_id" not in frame
    return frame


def identity(binding):
    return {key: binding[key] for key in ("project_id", "epoch", "root", "name")}


def notification(frame, binding, event):
    assert frame["type"] == "desktop.notification"
    assert frame["epoch"] == binding["epoch"]
    assert frame["data"] == {
        "project": identity(binding), "source_seq": event.seq,
        "target": event.data.get("route"),
        "notification": {key: value for key, value in event.data.items() if key in {
            "automation_id", "automation_name", "title", "message", "trigger", "values",
        }},
    }


async def test_resident_sources_without_renderers_survive_legacy_switch_and_receiver_close(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    init_project(other)
    async with application(project) as (app, http):
        a = await opened(http, project)
        ca = resolve_project(app.state, a["project_id"]).ctx
        source = app.state.desktop_events
        await source.ready()
        a_count = ca.bus.subscriber_count
        assert source.bus.subscriber_count == 0
        # No renderer or app receiver exists when the second resident is registered.
        b = await opened(http, other)
        cb = resolve_project(app.state, b["project_id"]).ctx
        await source.ready()
        b_count = cb.bus.subscriber_count
        assert app.state.ctx is ca
        assert ca.bus.current_seq == cb.bus.current_seq
        observed = source.bus.subscribe()
        gate, started = asyncio.Event(), asyncio.Event()

        async def work(_handle):
            started.set()
            await gate.wait()

        handles = [ctx.jobs.submit("same-job", "pipeline_run", work) for ctx in (ca, cb)]
        await asyncio.wait_for(started.wait(), 3)
        try:
            payload = {"automation_id": "same-id", "title": "Finished", "message": "Ready",
                       "route": "/tasks?task=same-id", "project_id": "forged", "root": str(other)}
            ea = await ca.bus.publish(EventType.AUTOMATION_NOTIFICATION, payload)
            eb = await cb.bus.publish(EventType.AUTOMATION_NOTIFICATION, payload)
            assert ea.seq == eb.seq
            before_receivers = [await asyncio.wait_for(observed.get(), 3) for _ in range(2)]
            assert {e.data["project"]["project_id"] for e in before_receivers} == {a["project_id"], b["project_id"]}
            cursor = before_receivers[0].seq - 1
            query = urlencode({"epoch": a["epoch"], "since_seq": cursor})
            async with Socket(app, "/ws/desktop?" + query) as ws, Socket(app, "/ws") as legacy:
                await desktop_hello(ws, a["epoch"], resumed=cursor)
                frames = [await ws.frame(), await ws.frame()]
                for frame in frames:
                    notification(frame, a if frame["data"]["project"]["project_id"] == a["project_id"] else b, ea)
                assert frames[0]["seq"] < frames[1]["seq"]
                assert (await legacy.frame())["type"] == "system.hello"
                assert await opened(http, other) == b
                assert await opened(http, project) == a
                assert (ca.bus.subscriber_count, cb.bus.subscriber_count) == (a_count, b_count)
                response = await http.post("/api/project/open", json={"path": str(other)})
                assert response.status_code == 200
                assert (await legacy.frame())["type"] == "project.opened"
                ea = await ca.bus.publish(EventType.AUTOMATION_NOTIFICATION, payload)
                eb = await cb.bus.publish(EventType.AUTOMATION_NOTIFICATION, payload)
                live = [await ws.frame(), await ws.frame()]
                for frame in live:
                    binding, event = (a, ea) if frame["data"]["project"]["project_id"] == a["project_id"] else (b, eb)
                    notification(frame, binding, event)
                assert live[0]["seq"] > frames[-1]["seq"]
                legacy_frame = await legacy.frame()
                assert legacy_frame["type"] == "automation.notification"
                assert "epoch" not in legacy_frame and "project_id" not in legacy_frame
                assert ca.bus.forward_to is None and cb.bus.forward_to is app.state.browser_bus
                await legacy.action(action="ping")
                assert (await legacy.frame())["type"] == "system.pong"
            # Receiver disconnects and source cleanup own no resident job lifecycle.
            assert source.bus.subscriber_count == 1  # only our observation remains
            assert all(not h.task.done() and not h.task.cancelling() for h in handles)
            for _ in range(2):
                async with Socket(app, "/ws/desktop") as ws:
                    await desktop_hello(ws, a["epoch"])
                    assert source.bus.subscriber_count == 2
                    assert (ca.bus.subscriber_count, cb.bus.subscriber_count) == (a_count, b_count)
                assert source.bus.subscriber_count == 1
            contexts = dict(app.state.project_contexts)
            await source.close()
            await source.close()
            assert (ca.bus.subscriber_count, cb.bus.subscriber_count) == (a_count - 1, b_count - 1)
            assert app.state.project_contexts == contexts
            assert all(not h.task.done() and not h.task.cancelling() for h in handles)
            assert await ca.db.healthcheck() and await cb.db.healthcheck()
            gate.set()
            await asyncio.wait_for(asyncio.gather(*(h.task for h in handles)), 3)
        finally:
            observed.close()


async def test_persisted_attention_seed_then_pause_running_delete_are_full_project_snapshots(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    init_project(other)
    # Persist actual rows before attaching the source, not synthetic seed events.
    # A full backend restart deliberately pauses every started Producer (§10.11),
    # so rebuild only the real event source to test its non-paused DB seed.
    async with application(project) as (app, http):
        await app.state.desktop_events.close()
        bindings = []
        for root in (project, other):
            binding = await opened(http, root)
            bindings.append(binding)
            ctx = resolve_project(app.state, binding["project_id"]).ctx
            async with ctx.db.session() as session:
                session.add_all([
                    AgentChat(id="same-chat", member_id="team_default__studio-producer",
                              producer_mode=True, producer_started=True, producer_state="awaiting_approval"),
                    AgentChat(id="second chat", member_id="team_default__studio-producer",
                              producer_mode=True, producer_started=True, producer_state="awaiting_user"),
                    AgentChat(id="not-started", member_id="team_default__studio-producer",
                              producer_mode=True, producer_state="discussing"),
                ])
        a, b = bindings
        source = app.state.desktop_events = DesktopEventSource(app.state)
        for binding in bindings:
            source.attach(resolve_project(app.state, binding["project_id"]))
        ca = resolve_project(app.state, a["project_id"]).ctx
        waiting = [{"chat_id": "same-chat", "target": "/producer?chat=same-chat"},
                   {"chat_id": "second chat", "target": "/producer?chat=second%20chat"}]
        async with Socket(app, "/ws/desktop") as ws:
            hello = await desktop_hello(ws, a["epoch"])
            assert hello["data"]["attention"] == source.snapshot()
            assert len(hello["data"]["attention"]) == 2
            for seeded, binding in zip(hello["data"]["attention"], bindings):
                assert seeded["project"] == identity(binding)
                assert seeded["target"] == "/producer"
                assert seeded["waiting"] == waiting
            await ca.chats.producer.pause("same-chat")
            paused = await ws.frame()
            assert paused["data"]["waiting"] == waiting[1:]
            assert paused["data"]["project"] == identity(a)
            assert paused["data"]["target"] == "/producer"
            second = await ca.chats.get_chat("second chat")
            event = await ca.bus.publish(EventType.TEAM_CHAT_UPDATED, ca.chats.to_json(second, running=True))
            running = await ws.frame()
            assert running["type"] == "desktop.producer_attention"
            assert running["data"]["waiting"] == []
            assert running["data"]["source_seq"] == event.seq
            await ca.bus.publish(EventType.TEAM_CHAT_UPDATED, ca.chats.to_json(second, running=False))
            assert (await ws.frame())["data"]["waiting"] == waiting[1:]
            assert await ca.chats.delete_chat("second chat")
            deleted = await ws.frame()
            assert deleted["type"] == "desktop.producer_attention"
            assert deleted["data"]["waiting"] == []
            assert deleted["data"]["project"] == identity(a)
            assert {item["project"]["project_id"]: item["waiting"] for item in source.snapshot()} == {
                a["project_id"]: [], b["project_id"]: waiting,
            }


async def test_app_cursor_replay_gap_stale_epoch_and_overflow_include_snapshot_watermark(project):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        source, local = app.state.desktop_events, app.state.ctx.bus
        await source.ready()
        observed = source.bus.subscribe()
        try:
            async with Socket(app, "/ws/desktop") as ws:
                hello = await desktop_hello(ws, binding["epoch"])
            event = await local.publish(EventType.AUTOMATION_NOTIFICATION, {"title": "Offline", "route": "/tasks"})
            first = await asyncio.wait_for(observed.get(), 3)
            query = urlencode({"epoch": binding["epoch"], "since_seq": hello["seq"]})
            async with Socket(app, "/ws/desktop?" + query) as ws:
                await desktop_hello(ws, binding["epoch"], resumed=hello["seq"])
                replay = await ws.frame()
                notification(replay, binding, event)
                assert replay["seq"] == first.seq
                # Block only this socket's send; production queue limits remain intact.
                ws.block_type = "desktop.notification"
                await local.publish(EventType.AUTOMATION_NOTIFICATION, {"title": "Blocked"})
                await asyncio.wait_for(observed.get(), 3)
                await asyncio.wait_for(ws.blocked.wait(), 3)
                for n in range(DEFAULT_QUEUE_SIZE + 1):
                    await local.publish(EventType.AUTOMATION_NOTIFICATION, {"title": str(n)})
                    await asyncio.wait_for(observed.get(), 3)
                watermark = source.bus.current_seq
                assert source.bus.replay_gap(first.seq) == "history_gap"
                assert watermark - first.seq > DEFAULT_HISTORY_SIZE
                ws.release.set()
                assert (await ws.frame())["type"] == "desktop.notification"
                dropped = await ws.frame()
                assert dropped["type"] == "system.dropped"
                assert dropped["epoch"] == binding["epoch"]
                assert dropped["data"] == {"reason": "overflow", "count": 1, "resync": True,
                                           "attention": source.snapshot(), "snapshot_seq": watermark}
            for epoch, cursor, reason in (
                (binding["epoch"], first.seq, "history_gap"),
                ("previous-app-lifetime", watermark, "scope_mismatch"),
                (binding["epoch"], "invalid", "invalid_cursor"),
            ):
                query = urlencode({"epoch": epoch, "since_seq": cursor})
                async with Socket(app, "/ws/desktop?" + query) as ws:
                    fresh = await desktop_hello(ws, binding["epoch"])
                    assert fresh["data"]["attention"] == source.snapshot()
                    dropped = await ws.frame()
                    assert dropped["type"] == "system.dropped"
                    assert dropped["epoch"] == binding["epoch"]
                    assert dropped["data"] == {"reason": reason, "count": 0, "resync": True,
                                               "attention": source.snapshot(), "snapshot_seq": fresh["seq"]}
                    await ws.action(action="ping")
                    assert (await ws.frame())["type"] == "system.pong"  # no stale replay
        finally:
            observed.close()
        assert source.bus.subscriber_count == 0


async def test_desktop_auth_host_origin_scope_and_notification_allowlist(project):
    async with application(project, AuthSettings(token="secret")) as (app, http):
        binding = await opened(http, project)
        source, local = app.state.desktop_events, app.state.ctx.bus
        await source.ready()
        count = local.subscriber_count
        for query, headers in (
            ({}, {}), ({"token": "wrong"}, {}),
            ({"token": "secret"}, {"origin": "http://evil.example"}),
            ({"token": "secret"}, {"origin": "null"}),
            ({"token": "secret", "project_id": binding["project_id"]}, {}),
            ({"token": "secret", "root": binding["root"]}, {}),
            ({"token": "secret"}, {"x-voxarium-project-id": binding["project_id"]}),
        ):
            async with Socket(app, "/ws/desktop?" + urlencode(query), headers) as ws:
                assert ws.handshake["type"] == "websocket.close"
                assert ws.handshake["code"] == 1008
        assert local.subscriber_count == count and source.bus.subscriber_count == 0
        async with Socket(app, "/ws/desktop?token=secret", {"origin": "http://127.0.0.1"}) as ws:
            await desktop_hello(ws, binding["epoch"])
            for kind, data in (
                (EventType.TEAM_MEMBER_MESSAGE, {"text": "private transcript"}),
                (EventType.PLUGIN_EVENT, {"name": "private plugin event"}),
                (EventType.AUTOMATION_NOTIFICATION, {"route": "https://evil.example/tasks"}),
                (EventType.AUTOMATION_NOTIFICATION, {"route": "/tasks?project_id=foreign"}),
                (EventType.AUTOMATION_NOTIFICATION, {"route": "/ws/projects/foreign"}),
            ):
                await local.publish(kind, data)
            # A valid notification is a FIFO barrier for all rejected local events.
            event = await local.publish(EventType.AUTOMATION_NOTIFICATION, {
                "title": "Allowed", "route": "/explorer?path=Assets%2Fshared.vox",
                "project_id": "foreign", "epoch": "foreign", "root": "foreign", "secret": "omit",
            })
            notification(await ws.frame(), binding, event)
            await ws.action(action="publish", type="automation.notification", data={"title": "Forged"})
            await ws.action(action="replay", since_seq=0, project_id="foreign")
            await ws.action(action="ping")
            assert (await ws.frame())["type"] == "system.pong"
    # Host rebinding is specifically forbidden in unauthenticated local mode.
    async with application(project) as (app, _http):
        async with Socket(app, "/ws/desktop", {"host": "evil.example"}) as ws:
            assert ws.handshake["type"] == "websocket.close"
            assert ws.handshake["code"] == 1008


async def test_transient_attention_read_failure_is_reported_and_recovers_on_idle_bus(project, monkeypatch):
    async with application(project) as (app, http):
        binding = await opened(http, project)
        ctx = app.state.ctx
        await app.state.desktop_events.close()
        async with ctx.db.session() as session:
            session.add(AgentChat(id="waiting", member_id="team_default__studio-producer",
                                  producer_mode=True, producer_started=True, producer_state="awaiting_user"))
        original = ctx.chats.list_chats
        fail = True

        async def flaky_read():
            if fail:
                raise OSError("temporary read failure")
            return await original()

        monkeypatch.setattr(ctx.chats, "list_chats", flaky_read)
        source = app.state.desktop_events = DesktopEventSource(app.state)
        source.attach(resolve_project(app.state, binding["project_id"]))
        async with Socket(app, "/ws/desktop") as ws:
            hello = await desktop_hello(ws, binding["epoch"])
            assert hello["data"]["attention"] == []
            dropped = await ws.frame()
            assert dropped["type"] == "system.dropped"
            assert dropped["data"]["reason"] == "attention_unavailable"
            assert dropped["data"]["unavailable"] == [binding["project_id"]]
            fail = False
            recovered = await ws.frame()  # no chat event/reconnect needed to repair the seed
            assert recovered["type"] == "desktop.producer_attention"
            assert recovered["data"]["waiting"] == [{"chat_id": "waiting", "target": "/producer?chat=waiting"}]
            assert not source.unavailable()
