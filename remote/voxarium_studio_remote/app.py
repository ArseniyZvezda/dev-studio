"""Authenticated catalog and lookup-only sync (dev spec §16.7–16.9).

No browser/static/admin HTTP API. The SSH operator remains the only user administrator.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .auth import Authenticator, User, build_authenticator, credentials_from_headers
from .hub import Connection, Hub
from .protocol import (
    CAPABILITIES, CATALOG_API_VERSION, CLOSE_BAD_REQUEST, CLOSE_NOT_FOUND,
    CLOSE_OVERLOADED, CLOSE_UNAUTHORISED, PROTOCOL_VERSION, ProtocolError,
    check_display_name, check_path, check_project_id,
)
from .store import ProjectExists, SnapshotChanged, SnapshotTooLarge, Store, UnknownLegacySpace, UnknownProject

REVOCATION_CHECK_SECONDS = 30.0
MAX_CREATE_BYTES = 2048


class ApiError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code


def _error(status: int, code: str) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(status_code=status, headers=headers, content={"error": {
        "code": code, "message": code.replace("_", " ").capitalize(),
        "request_id": uuid.uuid4().hex,
    }})


def _query(request: Any, allowed: set[str]) -> dict[str, str]:
    pairs = list(request.query_params.multi_items())
    if (any(key not in allowed or len(value) > 2048 for key, value in pairs)
            or len({key for key, _ in pairs}) != len(pairs)):
        raise ApiError(400, "invalid_request")
    return dict(pairs)


def _number(raw: str | None, default: int, maximum: int) -> int:
    if raw is None:
        return default
    if re.fullmatch(r"[0-9]{1,20}", raw) is None or not 1 <= int(raw) <= maximum:
        raise ApiError(400, "invalid_request")
    return int(raw)


def _id(raw: object) -> str:
    try:
        return check_project_id(raw)
    except ProtocolError:
        raise ApiError(400, "invalid_id") from None


def create_app(data_dir: str | Path | None = None, *, store: Store | None = None,
               authenticator: Authenticator | None = None) -> FastAPI:
    owns_store = store is None
    if store is None:
        if data_dir is None:
            raise ValueError("create_app needs a data directory or a store")
        store = Store(Path(data_dir) / "remote.db")
    auth, hub = authenticator or build_authenticator(store), Hub(store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owns_store:
                store.close()

    app = FastAPI(title="Voxarium Studio Remote", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.store, app.state.hub = store, hub

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return _error(exc.status, exc.code)

    @app.exception_handler(PermissionError)
    async def credential_error(_request: Request, _exc: PermissionError):
        return _error(401, "unauthorised")

    @app.exception_handler(sqlite3.Error)
    async def database_error(_request: Request, _exc: sqlite3.Error):
        return _error(503, "temporarily_unavailable")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        return _error(exc.status_code, "invalid_request")

    @app.exception_handler(UnknownProject)
    async def unknown_project(_request: Request, _exc: UnknownProject):
        return _error(404, "project_not_found")

    @app.exception_handler(UnknownLegacySpace)
    async def unknown_legacy(_request: Request, _exc: UnknownLegacySpace):
        return _error(404, "legacy_space_not_found")

    def require_user(request: Any) -> User:
        user = _authenticate(auth, request.headers)
        if user is None:
            raise ApiError(401, "unauthorised")
        return user

    @contextmanager
    def guarded(user: User):
        with store.credential_guard(user.id, user.credential_version) as row:
            yield User.from_row(row)

    def success(content: dict[str, Any], *, status: int = 200, headers: dict | None = None):
        # Render inside the guard/read transaction, never across a network await.
        return JSONResponse({"protocol": PROTOCOL_VERSION, **content}, status_code=status,
                            headers={"Cache-Control": "no-store", **(headers or {})})

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "voxarium_studio_remote", "version": __version__,
                "protocol": PROTOCOL_VERSION, "supported_protocols": [PROTOCOL_VERSION],
                "catalog_api": CATALOG_API_VERSION}

    @app.get("/api/v1/me")
    async def legacy_me(request: Request):
        user = require_user(request)
        with guarded(user) as user:
            return success({"user": {"id": user.id, "name": user.display_name, "color": user.color}})

    @app.get("/api/v2/me")
    async def me(request: Request):
        user = require_user(request)
        _query(request, set())
        with guarded(user) as user:
            return success({"catalog_api": CATALOG_API_VERSION,
                            "capabilities": list(CAPABILITIES), "user": user.to_json()})

    @app.get("/api/v2/projects")
    async def projects(request: Request):
        user = require_user(request)
        query = _query(request, {"limit", "after"})
        limit = _number(query.get("limit"), 100, 200)
        after = _id(query["after"]) if "after" in query else None
        with guarded(user):
            return success(store.list_projects(limit=limit, after=after))

    @app.post("/api/v2/projects")
    async def create_project(request: Request):
        user = require_user(request)
        _query(request, set())
        length = request.headers.get("content-length")
        if length is not None and (not length.isascii() or not length.isdecimal()):
            raise ApiError(400, "invalid_request")
        if length is not None and (len(length) > 10 or int(length) > MAX_CREATE_BYTES):
            raise ApiError(413, "payload_too_large")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_CREATE_BYTES:
                raise ApiError(413, "payload_too_large")
            body.extend(chunk)
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            payload = json.loads(body, object_pairs_hook=unique)
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError(400, "invalid_request") from None
        if not isinstance(payload, dict) or set(payload) != {"id", "display_name"}:
            raise ApiError(400, "invalid_request")
        project_id = _id(payload["id"])
        try:
            label = check_display_name(payload["display_name"])
        except ProtocolError:
            raise ApiError(400, "invalid_display_name") from None
        with guarded(user):
            try:
                project = store.create_project(project_id, label)
            except ProjectExists:
                raise ApiError(409, "project_exists") from None
            return success({"project": project.to_json()}, status=201,
                           headers={"Location": f"/api/v2/projects/{project_id}"})

    @app.get("/api/v2/projects/{project_id}")
    async def project(request: Request, project_id: str):
        user = require_user(request)
        _query(request, set())
        project_id = _id(project_id)
        with guarded(user):
            row = store.project(project_id)
            if row is None:
                raise ApiError(404, "project_not_found")
            return success({"project": row.to_json()})

    @app.get("/api/v2/projects/{project_id}/snapshot")
    async def snapshot(request: Request, project_id: str):
        user = require_user(request)
        project_id = _id(project_id)
        query = _query(request, {"limit", "after", "expected_seq", "expected_epoch"})
        limit = _number(query.get("limit"), 20, 100)
        after, seq, epoch = query.get("after"), None, query.get("expected_epoch")
        if after is not None:
            try:
                if check_path(after) != after:
                    raise ProtocolError()
            except ProtocolError:
                raise ApiError(400, "invalid_request") from None
            if "expected_seq" not in query or epoch is None:
                raise ApiError(400, "invalid_request")
        elif "expected_seq" in query:
            raise ApiError(400, "invalid_request")
        if "expected_seq" in query:
            raw = query["expected_seq"]
            if re.fullmatch(r"[0-9]{1,19}", raw) is None or int(raw) > 2**63 - 1:
                raise ApiError(400, "invalid_request")
            seq = int(raw)
        if epoch is not None and re.fullmatch(r"[0-9a-f]{32}", epoch) is None:
            raise ApiError(400, "invalid_request")
        with guarded(user):
            try:
                page = store.snapshot(project_id, limit=limit, after=after,
                                      expected_seq=seq, expected_epoch=epoch)
            except SnapshotChanged:
                raise ApiError(409, "snapshot_changed") from None
            except SnapshotTooLarge:
                raise ApiError(413, "payload_too_large") from None
            except ValueError:
                raise ApiError(400, "invalid_request") from None
            return success(page)

    @app.get("/api/v2/legacy-spaces/resolve")
    async def resolve_legacy(request: Request):
        user = require_user(request)
        query = _query(request, {"name"})
        name = query.get("name")
        if name is None or not name or len(name.encode("utf-8")) > 480:
            raise ApiError(400, "invalid_request")
        with guarded(user):
            row = store.resolve_legacy(name)
            if row is None:
                raise ApiError(404, "legacy_space_not_found")
            return success({"legacy_space_name": name, "project": row.to_json()})

    @app.get("/api/v2/projects/{invalid_id:path}")
    async def invalid_project_path(request: Request, invalid_id: str):
        require_user(request)
        # Slashes decoded once by ASGI must not escape identity validation by routing.
        _id(invalid_id)
        raise ApiError(400, "invalid_request")

    @app.websocket("/api/v1/sync")
    async def legacy_sync(websocket: WebSocket):
        await websocket.accept()
        user = _authenticate(auth, websocket.headers)
        if user is None or not store.credential_valid(user.id, user.credential_version):
            await websocket.close(code=CLOSE_UNAUTHORISED, reason="unauthorised")
            return
        # Never validate a selector or enter Hub for a v1 socket (dev spec §16.8).
        await websocket.close(code=CLOSE_BAD_REQUEST, reason="protocol_upgrade_required")

    @app.websocket("/api/v2/sync")
    async def sync(websocket: WebSocket):
        await websocket.accept()
        user = _authenticate(auth, websocket.headers)
        if user is None:
            await websocket.close(code=CLOSE_UNAUTHORISED, reason="unauthorised")
            return
        try:
            query = _query(websocket, {"space"})
            project_id = _id(query.get("space"))
        except ApiError as exc:
            await websocket.close(code=CLOSE_BAD_REQUEST, reason=exc.code)
            return
        try:
            connection = hub.connect(user, project_id)
        except PermissionError:
            await websocket.close(code=CLOSE_UNAUTHORISED, reason="unauthorised")
            return
        except LookupError:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="project_not_found")
            return
        except sqlite3.Error:
            await websocket.close(code=1013, reason="temporarily_unavailable")
            return
        tasks = {
            asyncio.create_task(_write(websocket, connection, hub), name="remote-write"),
            asyncio.create_task(_read(websocket, connection, hub), name="remote-read"),
            asyncio.create_task(_watch(websocket, connection, store, hub), name="remote-watch"),
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
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()

    return app


def _authenticate(auth: Authenticator, headers: Any) -> User | None:
    if hasattr(headers, "getlist") and len(headers.getlist("authorization")) != 1:
        return None
    credentials = credentials_from_headers(headers)
    return auth.authenticate(credentials) if credentials and credentials.scheme == "bearer" else None


async def _deny(websocket: WebSocket, connection: Connection, hub: Hub) -> None:
    hub.disconnect(connection)
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=CLOSE_UNAUTHORISED, reason="unauthorised")


async def _read(websocket: WebSocket, connection: Connection, hub: Hub) -> None:
    while True:
        raw = await websocket.receive_text()
        try:
            hub.handle(connection, raw)
        except PermissionError:
            await _deny(websocket, connection, hub)
            return


async def _write(websocket: WebSocket, connection: Connection, hub: Hub) -> None:
    while True:
        frame = await connection.queue.get()
        # Authorise at dequeue, not enqueue. No DB lock spans the send await;
        # already authorised in-flight bytes cannot be recalled (dev spec §16.9).
        if connection.closed or not hub.store.credential_valid(
            connection.user.id, connection.user.credential_version
        ):
            await _deny(websocket, connection, hub)
            return
        if frame is None:
            await websocket.close(code=CLOSE_OVERLOADED, reason="Too far behind")
            return
        await websocket.send_text(frame)


async def _watch(websocket: WebSocket, connection: Connection, store: Store, hub: Hub) -> None:
    while True:
        await asyncio.sleep(min(REVOCATION_CHECK_SECONDS, 30.0))
        try:
            # A metadata refresh must never adopt a rotated generation between a
            # validity check and a second SELECT; that would revive the old socket.
            with store.credential_guard(connection.user.id, connection.user.credential_version) as row:
                user = User.from_row(row)
                if user != connection.user:
                    connection.user = user
                    hub._broadcast_presence(connection.space)
        except PermissionError:
            await _deny(websocket, connection, hub)
            return
