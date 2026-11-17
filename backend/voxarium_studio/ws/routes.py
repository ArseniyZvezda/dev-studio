"""Project-scoped and legacy WebSocket channels (§2.4, §5.3.2, §11.2).

One connection carries every event type. It is bidirectional by design: today the client
only sends control frames (ping, replay), but the same channel is what a user reply to a
running agent would travel on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from ..core.project_scope import ProjectDescriptor, resolve_project
from ..core.security import authorise_websocket
from .bus import EventBus
from .events import EventType

log = logging.getLogger(__name__)
router = APIRouter()


def legacy_editor_blockers(state: Any, ctx: Any) -> int:
    """Authoritative *live socket* evidence, not proof that an editor saved (I4).

    A legacy /ws follows browser_bus selection, not its hello's captured root.
    Without editor-lease/flush capability its visibility is unknown, so it is
    conservatively a blocker for every resident Ctx, including roots opened
    after connection. A scoped socket/collab socket blocks only its captured
    Ctx. Future cutover must combine this with dirty/expired editor leases.
    No client-provided query flag or frame is trusted as capability evidence.
    """
    sockets = getattr(state, "legacy_editor_sockets", {})
    return sum(bound is None or bound is ctx for bound in sockets.values())


def bind_editor_inventory(state: Any, ctx: Any) -> None:
    """Install the app-owned live visibility callback on one resident Ctx.

    The context publication/HTTP owner must call this for newly opened roots;
    a project.opened event pump is too late to close that admission race. The
    callback reads the shared registry at check time, not a copied hello/root.
    Previously observed unconfirmed clients remain in the transition owner
    after socket close; neither a capability flag nor disconnect is save proof.
    """
    transitions = ctx.docs.transitions

    def inventory() -> bool:
        sockets = getattr(state, "legacy_editor_sockets", {})
        for token, bound in sockets.items():
            if bound is None or bound is ctx:
                transitions.register_client(token)
        return bool(legacy_editor_blockers(state, ctx))

    transitions.set_client_inventory(inventory)
    inventory()


def _inventory_contexts(state: Any, ctx: Any = None) -> list[Any]:
    contexts = list(getattr(state, "project_contexts", {}).values())
    for candidate in (getattr(state, "ctx", None), ctx):
        if candidate is not None and all(candidate is not item for item in contexts):
            contexts.append(candidate)
    return [item for item in contexts if hasattr(getattr(item, "docs", None), "transitions")]


@contextmanager
def _editor_socket_inventory(state: Any, ctx: Any = None) -> Iterator[None]:
    sockets = getattr(state, "legacy_editor_sockets", None)
    if sockets is None:
        sockets = state.legacy_editor_sockets = {}
    token = uuid4().hex
    sockets[token] = ctx
    try:
        for resident in _inventory_contexts(state, ctx):
            bind_editor_inventory(state, resident)
        yield
    finally:
        for resident in _inventory_contexts(state, ctx):
            if ctx is None or ctx is resident:
                # Also cover a root opened after connection but before teardown.
                resident.docs.transitions.register_client(token)
                resident.docs.transitions.close_client(token)
        sockets.pop(token, None)


@router.websocket("/ws/desktop")
async def desktop_websocket_endpoint(websocket: WebSocket) -> None:
    """App-lifetime notifications/attention, even with zero project views (§5.3.6).

    This is a narrow, authenticated adapter, not a second project event/collab
    protocol. Source identity comes only from workspace's resident registry.
    The cursor belongs to the app stream (epoch, seq), not any local bus.
    """
    if not await authorise_websocket(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorised")
        return
    state = websocket.app.state
    epoch, source = state.project_epoch, state.desktop_events
    query = websocket.query_params
    asserted_epoch = websocket.headers.getlist("x-voxarium-epoch")
    if (any(key in query for key in ("project_id", "root", "project"))
            or websocket.headers.getlist("x-voxarium-project-id")
            or asserted_epoch and asserted_epoch != [epoch]):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Conflicting desktop scope")
        return
    bus = source.bus
    await source.ready()
    since_seq, gap = None, None
    if "since_seq" in query:
        raw = query.get("since_seq", "")
        if query.get("epoch") != epoch:
            gap = "scope_mismatch"
        elif (len(query.getlist("epoch")) != 1 or len(query.getlist("since_seq")) != 1
                or not raw.isascii() or not raw.isdigit() or len(raw) > 16):
            gap = "invalid_cursor"
        else:
            since_seq = int(raw)
            gap = bus.replay_gap(since_seq)
            if gap:
                since_seq = None
    elif "epoch" in query and query.getlist("epoch") != [epoch]:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Conflicting desktop scope")
        return
    # Capture subscription, snapshot and watermark without an intervening await.
    subscription = bus.subscribe(since_seq=since_seq)
    hello = {
        "type": str(EventType.HELLO), "seq": bus.current_seq, "epoch": epoch,
        "data": {"epoch": epoch, "resumed_from": since_seq,
                 **({"attention": source.snapshot()} if since_seq is None else {})},
    }
    unavailable = source.unavailable()
    send_lock = asyncio.Lock()

    async def dropped(reason: str, count: int = 0, *, seq: int | None = None) -> None:
        missing = source.unavailable()
        frame = {
            "type": str(EventType.DROPPED), "epoch": epoch,
            "data": {"count": count, "resync": True, "reason": reason,
                     "attention": source.snapshot(), "snapshot_seq": bus.current_seq,
                     **({"unavailable": missing} if missing else {})},
        }
        if seq is not None:
            frame["seq"] = seq
        await websocket.send_json(frame)

    async def pump() -> None:
        while True:
            event = await subscription.get()
            async with send_lock:
                count = subscription.take_dropped()
                if count:
                    await dropped("overflow", count)
                if event.type == EventType.DROPPED:
                    await dropped(event.data["reason"], event.data.get("count", 0), seq=event.seq)
                else:
                    await websocket.send_json({**event.to_json(), "epoch": epoch})

    async def receive() -> None:
        while True:
            try:
                message = await websocket.receive_json()
            except (ValueError, TypeError):
                continue
            # No project selectors, publish or RPC surface. Replay is the checked
            # reconnect cursor only; unknown client actions have no side effects.
            if isinstance(message, dict) and message.get("action") == "ping":
                async with send_lock:
                    await websocket.send_json({"type": str(EventType.PONG), "epoch": epoch, "data": {}})

    try:
        await websocket.accept()
        await websocket.send_json(hello)
        if gap:
            await dropped(gap)
        elif unavailable:
            await dropped("attention_unavailable")
        async with asyncio.TaskGroup() as group:
            group.create_task(pump(), name="desktop-ws-pump")
            group.create_task(receive(), name="desktop-ws-receive")
    except* WebSocketDisconnect:
        pass
    except* Exception:
        log.debug("Desktop WebSocket closed with an error", exc_info=True)
    finally:
        subscription.close()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()


async def _project_binding(websocket: WebSocket, project_id: str) -> ProjectDescriptor | None:
    """Resolve once, before accepting; a bad handle never selects legacy ctx (§5.3.2).

    The URL is authoritative. Optional selectors are assertions, not a way to rebind.
    On the event channel the query scope accompanying since_seq belongs to the cursor
    instead, and is checked by _resume_cursor so a stale cursor can request a resync.
    """
    if not await authorise_websocket(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorised")
        return None
    try:
        binding = resolve_project(websocket.app.state, project_id)
    except KeyError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unknown or expired project id")
        return None
    assertions = [
        (websocket.headers.getlist("x-voxarium-project-id"), binding.project_id),
        (websocket.headers.getlist("x-voxarium-epoch"), binding.epoch),
    ]
    query = websocket.query_params
    cursor_scope = "since_seq" in query and not websocket.url.path.endswith("/collab")
    if not cursor_scope:
        assertions.extend([
            (query.getlist("project_id"), binding.project_id),
            (query.getlist("epoch"), binding.epoch),
        ])
    if any(values and (len(values) != 1 or values[0] != expected) for values, expected in assertions):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Conflicting project scope")
        return None
    return binding


def _scoped_frame(binding: ProjectDescriptor, payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "project_id": binding.project_id, "epoch": binding.epoch}


def _hello(binding: ProjectDescriptor, **data: Any) -> dict[str, Any]:
    return _scoped_frame(binding, {
        "type": str(EventType.HELLO),
        "data": {
            "project": str(binding.root), "project_id": binding.project_id,
            "epoch": binding.epoch, **data,
        },
    })


class _ProjectCollabSocket:
    """Local browser envelope only; the independent remote protocol stays intact (§16.4).

    Rooms and session IDs still belong to the bound context's RemoteSync. Two local
    contexts may intentionally join the same remote space, but local selection cannot
    redirect either socket. No Yjs state or Markdown is written by this adapter.
    """

    def __init__(self, websocket: WebSocket, binding: ProjectDescriptor) -> None:
        self.websocket = websocket
        self.binding = binding

    async def receive_json(self) -> Any:
        message = await self.websocket.receive_json()
        if isinstance(message, dict):
            for key, expected in (("project_id", self.binding.project_id), ("epoch", self.binding.epoch)):
                if key in message and message[key] != expected:
                    await self.close(code=status.WS_1008_POLICY_VIOLATION, reason="Conflicting project scope")
                    raise WebSocketDisconnect(status.WS_1008_POLICY_VIOLATION)
            return {key: value for key, value in message.items() if key not in {"project_id", "epoch"}}
        return message

    async def send_json(self, message: dict[str, Any]) -> None:
        # The remote's joined.epoch identifies a room incarnation (§16.4), not
        # a backend lifetime. Preserve it under an unambiguous local-only name.
        if "epoch" in message:
            message = {**message, "room_epoch": message["epoch"]}
        await self.websocket.send_json(_scoped_frame(self.binding, message))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self.websocket.close(code=code, reason=reason)


@router.websocket("/ws/projects/{project_id}/collab")
async def project_collab_endpoint(websocket: WebSocket, project_id: str) -> None:
    """Immutable (project, document path) editor binding, separate from legacy (§5.3.2)."""
    binding = await _project_binding(websocket, project_id)
    if binding is None:
        return
    with _editor_socket_inventory(websocket.app.state, binding.ctx):
        await websocket.accept()
        await websocket.send_json(_hello(binding))
        await binding.ctx.sync.serve_collab(_ProjectCollabSocket(websocket, binding))  # type: ignore[arg-type]


def _resume_cursor(
    binding: ProjectDescriptor, bus: EventBus, source: Any,
) -> tuple[int | None, str | None]:
    """A cursor is (project_id, epoch, since_seq), never a bare local sequence (§5.3.2).

    Query reconnect and replay actions use these same three fields. A failed cursor
    starts live-only and emits system.dropped/resync; it never replays a different
    stream or pretends that the retained history is complete.
    """
    if source.get("project_id") != binding.project_id or source.get("epoch") != binding.epoch:
        return None, "scope_mismatch"
    if hasattr(source, "getlist") and any(len(source.getlist(key)) != 1 for key in ("project_id", "epoch", "since_seq")):
        return None, "invalid_cursor"
    value = source.get("since_seq")
    if hasattr(source, "getlist") and isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            value = int(value)
        except ValueError:
            return None, "invalid_cursor"
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, "invalid_cursor"
    gap = bus.replay_gap(value)
    return (None, gap) if gap else (value, None)


@router.websocket("/ws/projects/{project_id}")
async def project_websocket_endpoint(websocket: WebSocket, project_id: str) -> None:
    """Subscribe to the resident local bus, not the browser forwarding bus (§5.3.2)."""
    binding = await _project_binding(websocket, project_id)
    if binding is None:
        return
    with _editor_socket_inventory(websocket.app.state, binding.ctx):
        await _serve_project_websocket(websocket, binding)


async def _serve_project_websocket(websocket: WebSocket, binding: ProjectDescriptor) -> None:
    bus = binding.ctx.bus
    await websocket.accept()
    since_seq, gap = (None, None)
    if "since_seq" in websocket.query_params:
        since_seq, gap = _resume_cursor(binding, bus, websocket.query_params)
    # No await between validating history, subscribing and taking the hello watermark.
    subscription = bus.subscribe(since_seq=since_seq)
    hello = {**_hello(binding, resumed_from=since_seq), "seq": bus.current_seq}
    send_lock = asyncio.Lock()

    async def dropped(reason: str, count: int = 0) -> None:
        await websocket.send_json(_scoped_frame(binding, {
            "type": str(EventType.DROPPED),
            "data": {"count": count, "resync": True, "reason": reason},
        }))

    async def pump() -> None:
        while True:
            event = await subscription.get()
            async with send_lock:
                count = subscription.take_dropped()
                if count:
                    await dropped("overflow", count)
                await websocket.send_json(event.to_json(project_id=binding.project_id, epoch=binding.epoch))

    async def receive(group: asyncio.TaskGroup) -> None:
        nonlocal subscription, pump_task
        while True:
            try:
                message = await websocket.receive_json()
            except (ValueError, TypeError):
                continue
            if not isinstance(message, dict):
                continue
            action = message.get("action")
            if action == "ping":
                async with send_lock:
                    await websocket.send_json(_scoped_frame(binding, {"type": str(EventType.PONG), "data": {}}))
            elif action == "replay":
                async with send_lock:
                    # A replay replaces pending live delivery. Cancel even an event
                    # already fetched by pump before reseeding, avoiding interleaved
                    # old queues, duplicate replay/live frames and sequence regression.
                    pump_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pump_task
                    subscription.close()
                    requested, reason = _resume_cursor(binding, bus, message)
                    subscription = bus.subscribe(since_seq=requested)
                    if reason:
                        await dropped(reason)
                    pump_task = group.create_task(pump(), name="project-ws-pump")

    try:
        await websocket.send_json(hello)
        if gap:
            await dropped(gap)
        async with asyncio.TaskGroup() as group:
            pump_task = group.create_task(pump(), name="project-ws-pump")
            group.create_task(receive(group), name="project-ws-receive")
    except* WebSocketDisconnect:
        pass
    except* Exception:
        log.debug("Project WebSocket closed with an error", exc_info=True)
    finally:
        subscription.close()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()


@router.websocket("/ws/collab")
async def collab_endpoint(websocket: WebSocket) -> None:
    """One browser's live editing session on one document (§16.4).

    A socket of its own rather than frames on ``/ws``: that channel is a broadcast every tab
    receives, and this one is a conversation between one editor and one room — it starts
    when a document is opened and its closing *is* the editor leaving. The remote is never
    reached from the browser; this backend relays, and keeps the key.
    """
    if not await authorise_websocket(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorised")
        return
    ctx = websocket.app.state.ctx
    with _editor_socket_inventory(websocket.app.state, ctx):
        await websocket.accept()
        await ctx.sync.serve_collab(websocket)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not await authorise_websocket(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorised")
        return

    # Not bound to initial ctx: browser_bus can switch while this socket lives
    # (runtime integration plan §2.1). Register before the first accepted await.
    with _editor_socket_inventory(websocket.app.state):
        await _serve_legacy_websocket(websocket)


async def _serve_legacy_websocket(websocket: WebSocket) -> None:

    ctx = websocket.app.state.ctx
    bus = websocket.app.state.browser_bus
    await websocket.accept()

    since_raw = websocket.query_params.get("since_seq")
    since_seq = int(since_raw) if since_raw and since_raw.isdigit() else None
    subscription = bus.subscribe(since_seq=since_seq)

    await websocket.send_json(
        {
            "type": str(EventType.HELLO),
            "seq": bus.current_seq,
            "data": {
                "project": str(ctx.paths.root),
                "resumed_from": since_seq,
            },
        }
    )

    async def pump() -> None:
        """Bus -> client."""
        while True:
            event = await subscription.get()
            # A client that fell behind has a gap in its stream. Say so before the next
            # event, so it can refetch over REST instead of rendering stale state — the
            # bus drops the oldest events rather than stalling the producer.
            dropped = subscription.take_dropped()
            if dropped:
                await websocket.send_json(
                    {
                        "type": str(EventType.DROPPED),
                        "data": {"count": dropped, "resync": True},
                    }
                )
            await websocket.send_json(event.to_json())

    async def receive() -> None:
        """Client -> bus. Keeps the socket readable so disconnects are detected."""
        while True:
            try:
                message = await websocket.receive_json()
                action = message.get("action") if isinstance(message, dict) else None
                if action == "ping":
                    await websocket.send_json({"type": str(EventType.PONG), "data": {}})
                elif action == "replay":
                    requested = int(message.get("since_seq") or 0)
                    for event in bus.replay(requested):
                        await websocket.send_json(event.to_json())
                else:
                    log.debug("Ignoring unknown websocket action: %r", action)
            except (ValueError, TypeError, AttributeError):
                # A malformed frame is the sender's problem, not a reason to drop a
                # connection that is otherwise streaming a live run.
                log.debug("Ignoring malformed websocket frame", exc_info=True)

    pump_task = asyncio.create_task(pump(), name="ws-pump")
    receive_task = asyncio.create_task(receive(), name="ws-receive")
    try:
        done, pending = await asyncio.wait(
            {pump_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError):
                task.result()
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - connection teardown races
        log.debug("WebSocket closed with an error", exc_info=True)
    finally:
        pump_task.cancel()
        receive_task.cancel()
        subscription.close()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()
