"""``/api/ai/v1`` routes (ai-access design §6; R-A2). Mounted only when ACP is enabled.

When ACP is off none of these routes exist, so ``GET /api/ai/v1/capabilities`` is the
ordinary 404 — discovery never gets a positive DTO from an unmounted server. ``/healthz``,
``/api/v1/me``, ``/api/v2/me`` and the Documents ``PROTOCOL_VERSION`` are untouched.

Every response is ``Cache-Control: no-store``. Secrets travel only in headers; the device
token is returned exactly once, in the 201 of an explicit registration. Bodies are never
logged.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from . import ai_protocol as p
from .ai_auth import AiAuth, has_device_headers, hash_device_token, new_device_token
from .ai_relay import CloseConnection, ControlConnection, Relay
from .ai_store import AiStore, CapacityExceeded, NotFound, RegistrationExists
from .auth import Authenticator
from .store import Store

log = logging.getLogger(__name__)

MAX_REGISTRATION_BYTES = 2048
TICK_SECONDS = 1.0
NO_STORE = {"Cache-Control": "no-store"}


def _error(status: int, code: str, **extra: Any) -> JSONResponse:
    headers = dict(NO_STORE)
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(status_code=status, headers=headers, content={"error": {
        "code": code, "message": code.replace("_", " ").capitalize(),
        "request_id": uuid.uuid4().hex}, **extra})


def _json(content: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status, headers=NO_STORE)


def _empty_query(scope_query: bytes) -> bool:
    return scope_query in (b"", None)


async def _body(request: Request) -> bytes | None:
    length = request.headers.get("content-length")
    if length is not None and (not length.isascii() or not length.isdecimal() or len(length) > 10
                               or int(length) > MAX_REGISTRATION_BYTES):
        return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REGISTRATION_BYTES:
            return None
        body.extend(chunk)
    return bytes(body)


def mount_ai_control(app: FastAPI, store: Store, authenticator: Authenticator,
                     ai_store: AiStore, relay: Relay | None = None) -> Relay:
    ai_auth = AiAuth(store, ai_store, authenticator)
    relay = relay or Relay(ai_auth)
    app.state.ai_store, app.state.ai_relay = ai_store, relay
    last_tick = [0.0]

    def tick() -> None:
        now = relay.monotonic()
        if now - last_tick[0] >= TICK_SECONDS / 2:
            last_tick[0] = now
            relay.tick()

    @app.get("/api/ai/v1/capabilities")
    async def capabilities(request: Request):
        # Discovery is read-only and happens before Register: personal key only.
        if not _empty_query(request.scope.get("query_string", b"")) or has_device_headers(request.headers):
            return _error(400, "invalid_request")
        if ai_auth.personal(request.headers) is None:
            return _error(401, "unauthorised")
        return _json(p.capabilities(ai_store.server_instance_id))

    @app.post("/api/ai/v1/registrations")
    async def register(request: Request):
        if not _empty_query(request.scope.get("query_string", b"")) or has_device_headers(request.headers):
            return _error(400, "invalid_request")
        user = ai_auth.personal(request.headers)
        if user is None:
            return _error(401, "unauthorised")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return _error(415, "unsupported_media_type")
        raw = await _body(request)
        if raw is None:
            return _error(413, "payload_too_large")
        try:
            body = p.registration_request(p.decode(raw, max_bytes=MAX_REGISTRATION_BYTES))
        except p.AcpError:
            return _error(400, "invalid_request")
        token = new_device_token()
        try:
            # Under the key generation's guard: a concurrent rotate either precedes this
            # (we fail) or follows it (its hook tombstones the new registration).
            with store.credential_guard(user.id, user.credential_version):
                registration = ai_store.register(user.id, user.credential_version, body["client_id"],
                                                 body["display_name"], body["kind"],
                                                 hash_device_token(token), relay.wall_ms())
        except PermissionError:
            return _error(401, "unauthorised")
        except RegistrationExists as exc:
            # Same user + client_id only; the old token is neither returned nor replaced.
            return _error(409, "registration_exists", registration_id=exc.registration_id)
        except CapacityExceeded:
            return _error(409, "capacity_exceeded")
        return _json({"server_instance_id": ai_store.server_instance_id,
                      "registration_id": registration.id, "client_id": registration.client_id,
                      "user_id": registration.user_id, "device_token": token}, status=201)

    @app.get("/api/ai/v1/registrations")
    async def directory(request: Request):
        if not _empty_query(request.scope.get("query_string", b"")):
            return _error(400, "invalid_request")
        if ai_auth.device(request.headers) is None:
            return _error(401, "unauthorised")
        rows = [row.to_json() for row in ai_store.list_registrations() if ai_auth.registration_live(row)]
        return _json({"acp": p.ACP_VERSION, "registrations": rows})

    @app.delete("/api/ai/v1/registrations/{registration_id}")
    async def delete_registration(request: Request, registration_id: str):
        if not _empty_query(request.scope.get("query_string", b"")):
            return _error(400, "invalid_request")
        user = ai_auth.personal(request.headers)
        if user is None:
            return _error(401, "unauthorised")
        try:
            p.check_uuid(registration_id)
            with store.credential_guard(user.id, user.credential_version):
                ai_store.revoke_registration(user.id, registration_id, relay.wall_ms())
        except (p.AcpError, NotFound):
            return _error(404, "not_available")
        except PermissionError:
            return _error(401, "unauthorised")
        relay.close_registration(registration_id)
        return Response(status_code=204, headers=NO_STORE)

    @app.websocket("/api/ai/v1/control")
    async def control(websocket: WebSocket):
        await websocket.accept()
        if not _empty_query(websocket.scope.get("query_string", b"")):
            await websocket.close(code=p.CLOSE_PROTOCOL_ERROR, reason="protocol_error")
            return
        actor = ai_auth.device(websocket.headers)
        if actor is None:
            await websocket.close(code=p.CLOSE_UNAUTHORISED, reason="unauthorised")
            return
        connection = relay.connect(actor)
        tasks = {
            asyncio.create_task(_reader(websocket, connection, relay), name="acp-read"),
            asyncio.create_task(_writer(websocket, connection, relay), name="acp-write"),
            asyncio.create_task(_ticker(connection, tick), name="acp-tick"),
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
            code, reason = connection.close_code, connection.close_reason
            relay.disconnect(connection)
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=code, reason=reason)

    return relay


async def _reader(websocket: WebSocket, connection: ControlConnection, relay: Relay) -> None:
    while not connection.closed:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            relay.disconnect(connection)
            return
        raw = message.get("text")
        if raw is None:
            raw = message.get("bytes") or b""  # binary frames are a protocol error
        try:
            relay.handle(connection, raw)
        except CloseConnection as exc:
            relay.disconnect(connection, exc.code, exc.reason)
            return
        except Exception:  # noqa: BLE001 - never echo internals; log no body
            log.exception("ACP frame handling failed (connection %s)", connection.id)
            relay.disconnect(connection, 1011, "internal_error")
            return


async def _writer(websocket: WebSocket, connection: ControlConnection, relay: Relay) -> None:
    while True:
        item = await connection.queue.get()
        if item is None:
            return
        connection.dequeued(item)
        # Re-check key, device and grant at dequeue; no lock spans the send await.
        if not relay.deliverable(connection, item):
            if connection.closed:
                return
            continue
        await websocket.send_text(item.text)


async def _ticker(connection: ControlConnection, tick) -> None:
    while not connection.closed:
        await asyncio.sleep(TICK_SECONDS)
        tick()
