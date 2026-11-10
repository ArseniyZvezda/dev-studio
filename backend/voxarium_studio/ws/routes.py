"""The single multiplexed WebSocket channel (§2.4, §11.2).

One connection carries every event type. It is bidirectional by design: today the client
only sends control frames (ping, replay), but the same channel is what a user reply to a
running agent would travel on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from ..core.security import authorise_websocket
from .events import EventType

log = logging.getLogger(__name__)
router = APIRouter()


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
    await websocket.accept()
    await ctx.sync.serve_collab(websocket)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not await authorise_websocket(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorised")
        return

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
