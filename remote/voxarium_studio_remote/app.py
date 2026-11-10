"""The remote's HTTP surface: one health check, one identity check, one WebSocket (dev spec §16).

There is no browser-facing API and no static content. A person never talks to this server —
their local Studio does, on their behalf and with their key (§16.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from . import __version__
from .auth import Authenticator, User, build_authenticator, credentials_from_headers
from .hub import Connection, Hub
from .protocol import (
    CLOSE_BAD_REQUEST,
    CLOSE_OVERLOADED,
    CLOSE_UNAUTHORISED,
    PROTOCOL_VERSION,
    ProtocolError,
    check_space_name,
)
from .store import Store

log = logging.getLogger(__name__)

# How often a connected user is checked against the store, so revoking a key ends the
# connections it opened instead of only refusing the next one.
REVOCATION_CHECK_SECONDS = 30.0


def create_app(
    data_dir: str | Path | None = None,
    *,
    store: Store | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    """Build the server over *data_dir* (or an already open *store*, which tests pass)."""
    owns_store = store is None
    if store is None:
        if data_dir is None:
            raise ValueError("create_app needs a data directory or a store")
        store = Store(Path(data_dir) / "remote.db")
    auth = authenticator or build_authenticator(store)
    hub = Hub(store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owns_store:
                store.close()

    app = FastAPI(
        title="Voxarium Studio Remote",
        version=__version__,
        description="The shared server local Voxarium Studios synchronise through.",
        lifespan=lifespan,
        # No interactive docs: the surface is three routes and the server faces the internet.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store = store
    app.state.hub = hub

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "voxarium_studio_remote",
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
        }

    @app.get("/api/v1/me")
    async def me(request: Request) -> dict[str, Any]:
        """Whose key this is — what `curl` and a settings form check a key with."""
        user = _authenticate(auth, request.headers)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return {"user": user.to_json(), "protocol": PROTOCOL_VERSION}

    @app.websocket("/api/v1/sync")
    async def sync(websocket: WebSocket) -> None:
        # Accepted before it is judged, so the refusal can carry a close code the client
        # can tell apart from a dropped network: a rejected handshake is only ever "403".
        await websocket.accept()
        user = _authenticate(auth, websocket.headers)
        if user is None:
            await websocket.close(code=CLOSE_UNAUTHORISED, reason="Missing or invalid key")
            return
        try:
            space_name = check_space_name(websocket.query_params.get("space"))
        except ProtocolError as exc:
            await websocket.close(code=CLOSE_BAD_REQUEST, reason=str(exc)[:120])
            return

        connection = hub.connect(user, space_name)
        log.info("%s connected to space %s", user.name, space_name)
        tasks = {
            asyncio.create_task(_write(websocket, connection), name="remote-write"),
            asyncio.create_task(_read(websocket, connection, hub), name="remote-read"),
            asyncio.create_task(_watch(websocket, connection, store), name="remote-watch"),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            hub.disconnect(connection)
            log.info("%s disconnected from space %s", user.name, space_name)
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()

    return app


def _authenticate(auth: Authenticator, headers: Any) -> User | None:
    credentials = credentials_from_headers(headers)
    return auth.authenticate(credentials) if credentials else None


async def _read(websocket: WebSocket, connection: Connection, hub: Hub) -> None:
    while True:
        hub.handle(connection, await websocket.receive_text())


async def _write(websocket: WebSocket, connection: Connection) -> None:
    while True:
        frame = await connection.queue.get()
        if frame is None:
            await websocket.close(code=CLOSE_OVERLOADED, reason="Too far behind")
            return
        await websocket.send_text(frame)


async def _watch(websocket: WebSocket, connection: Connection, store: Store) -> None:
    while True:
        await asyncio.sleep(REVOCATION_CHECK_SECONDS)
        if not store.is_active(connection.user.id):
            await websocket.close(code=CLOSE_UNAUTHORISED, reason="Key revoked")
            return
