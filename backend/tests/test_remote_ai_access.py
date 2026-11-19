"""VS-195 X-A1: real Studio/Remote ACP apps, one event loop, no external I/O.

Both browser APIs and the Remote factory/lifespan are real. The HTTP and WS
carriers dispatch into ASGI without changing any header, frame or response.
Install the Remote distribution alongside Studio; a missing package is an error.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from sqlmodel import select
from starlette.applications import Starlette
from starlette.routing import Mount
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from voxarium_studio.ai_access import identity, protocol, service
from voxarium_studio.ai_access.executor import now_ms
from voxarium_studio.ai_access.transport import AiTransport, DeviceCredentials
from voxarium_studio.core import application_config, paths, workspace
from voxarium_studio.core.project import init_project
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.main import create_app
from voxarium_studio.models.ai_access import AiCommandLedger
from voxarium_studio.models.teams import AgentChat, AgentChatMessage, AgentMessage, MemberKind, Team, TeamMember, TeamTask
from voxarium_studio.sync import bindings, catalog_runtime, credentials, service as sync_service, state as sync_state
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.teams.backends.base import AgentEvent


URL = "https://relay.invalid/prefix"
PROJECT = "shared-acp-fixture"
API = "/api/ai-access"
LOCAL_TOKEN = "fixture-browser-token"
BACKEND = "acp-interoperability-fixture"
BRIEF = "Fixture brief: never persisted by Remote"
CHAT_TEXT = "Fixture prompt: never persisted by Remote"
CATALOG_DETAIL = f"/prefix/api/v2/projects/{PROJECT}"
CATALOG_VERIFY = [("GET", "/prefix/healthz"), ("GET", "/prefix/api/v2/me")]
CONSENT_READS = [("GET", "/prefix/api/ai/v1/capabilities"), *CATALOG_VERIFY, ("GET", CATALOG_DETAIL)]


class SocketClosed(Exception):
    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        super().__init__(f"{code} {reason}")


class Wire:
    """ASGI transport only. Trace kinds/codes and index metadata, never headers."""

    def __init__(self, remote) -> None:
        self.app = Starlette(routes=[Mount("/prefix", app=remote)])
        self.loop = asyncio.get_running_loop()
        self.socket_paths: list[str] = []
        self.index_commits: list[tuple[int, dict, dict, dict]] = []
        self.frames: list[tuple[str, str | int, str | None]] = []
        self.document_urls: list[str] = []
        self.document_frames: list[tuple[int, str, str | int, str | None]] = []
        self.http: list[tuple[str, str]] = []
        self.http_statuses: list[tuple[str, str, int]] = []
        self.drop_receipt: str | None = None
        self.dropped_receipt = False

    async def edge(self, scope, receive, send):
        assert scope["server"][0] == "relay.invalid"
        assert scope["scheme"] in {"https", "wss"}
        assert asyncio.get_running_loop() is self.loop
        if scope["type"] == "http":
            self.http.append((scope["method"], scope["path"]))

            async def observe(message):
                if message["type"] == "http.response.start":
                    self.http_statuses.append((scope["method"], scope["path"], message["status"]))
                await send(message)

            await self.app(scope, receive, observe)
        else:
            await self.app(scope, receive, send)

    def catalog_transport(self) -> CatalogHttpTransport:
        return CatalogHttpTransport(transport=httpx.ASGITransport(app=self.edge))

    def transport(self) -> AiTransport:
        return AiTransport(http_transport=httpx.ASGITransport(app=self.edge), ws_connect=self.connect)

    def connect(self, url, *, additional_headers, **options):
        assert options["ssl"].check_hostname and options["proxy"] is None
        return self._connect(url, additional_headers, documents=False)

    def connect_documents(self, url, *, additional_headers, **options):
        return self._connect(url, additional_headers, documents=True)

    @asynccontextmanager
    async def _connect(self, url, additional_headers, *, documents):
        parts = urlsplit(url)
        socket_id = len(self.document_urls) if documents else len(self.socket_paths)
        if documents:
            self.document_urls.append(url)
        else:
            self.socket_paths.append(parts.path)
        indexes = {}
        commits = {}
        headers = [(b"host", parts.netloc.encode("ascii"))]
        headers.extend((k.lower().encode("ascii"), v.encode("ascii")) for k, v in additional_headers.items())
        incoming: asyncio.Queue = asyncio.Queue()
        outgoing: asyncio.Queue = asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})
        scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.5"},
            "scheme": parts.scheme, "path": parts.path, "root_path": "", "headers": headers,
            "raw_path": parts.path.encode("ascii"), "query_string": parts.query.encode("ascii"),
            "client": ("127.0.0.1", 12345), "server": (parts.hostname, parts.port or 443),
            "subprotocols": [],
        }
        worker = asyncio.create_task(self.edge(scope, incoming.get, outgoing.put))

        def record(direction, raw):
            try:
                value = json.loads(raw)
                if documents:
                    self.document_frames.append((socket_id, direction, value["t"], value.get("path")))
                    return
                kind, body = value["type"], value["body"]
                self.frames.append((direction, kind, body.get("code")))
                if direction == "studio→remote" and kind == "resource.index.begin":
                    indexes[body["index_revision"]] = body
                elif direction == "studio→remote" and kind == "resource.index.commit":
                    commits[value["request_id"]] = indexes.pop(body["index_revision"]), body
                elif direction == "remote→studio" and kind == "response" and value["request_id"] in commits:
                    self.index_commits.append((socket_id, *commits.pop(value["request_id"]), body))
            except (ValueError, KeyError, TypeError, AttributeError):
                if documents:
                    self.document_frames.append((socket_id, direction, "invalid-frame", None))
                else:
                    self.frames.append((direction, "invalid-envelope", None))

        disconnected = False

        class Endpoint:
            close_code = None
            close_reason = None

            async def close(endpoint):
                nonlocal disconnected
                if not disconnected:
                    disconnected = True
                    incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

            async def __aiter__(endpoint):
                while True:
                    try:
                        yield await endpoint.recv()
                    except ConnectionClosedOK:
                        return

            async def send(endpoint, raw):
                record("studio→remote", raw)
                await incoming.put({"type": "websocket.receive", "text": raw})

            async def recv(endpoint):
                msg = await outgoing.get()
                if msg["type"] == "websocket.close":
                    code, reason = msg["code"], msg.get("reason", "")
                    endpoint.close_code, endpoint.close_reason = code, reason
                    if documents:
                        self.document_frames.append((socket_id, "close", code, reason))
                        error = ConnectionClosedOK if code in {1000, 1001} else ConnectionClosedError
                        raise error(Close(code, reason), None)
                    self.frames.append(("close", code, msg.get("reason")))
                    raise SocketClosed(code, reason)
                assert msg["type"] == "websocket.send"
                if not documents and self.drop_receipt is not None:
                    frame = json.loads(msg["text"])
                    if frame["type"] == "response" and frame["body"].get("command_id") == self.drop_receipt:
                        # Real Remote generated this receipt after commit. Fault only
                        # the downstream carrier, never the request, relay or ledger.
                        self.drop_receipt = None
                        self.dropped_receipt = True
                        raise SocketClosed(1006, "fixture link loss after receipt")
                record("remote→studio", msg["text"])
                return msg["text"]

        endpoint = Endpoint()
        try:
            async with asyncio.timeout(3):
                msg = await outgoing.get()
            assert msg["type"] == "websocket.accept", msg
            yield endpoint
        finally:
            await endpoint.close()
            async with asyncio.timeout(3):
                await worker


@dataclass
class Studio:
    app: object
    client: httpx.AsyncClient
    home: Path
    selected_home: ContextVar
    project: Path
    profile_id: str
    user_id: str
    key: str = field(repr=False)
    registration: dict = field(default_factory=dict)

    @property
    def context(self):
        return self.app.state.ctx

    async def browser(self, method, path, *, authenticated=True, **kwargs):
        return await self.in_home(self.client.request, method, path,
                                  headers={"Authorization": f"Bearer {LOCAL_TOKEN}"} if authenticated else {}, **kwargs)

    async def call(self, method, path, **kwargs):
        return await self.browser(method, API + path, **kwargs)

    async def remote_project(self, method, path="", **kwargs):
        return await self.project_api(method, "/remote" + path, **kwargs)

    async def project_api(self, method, path, **kwargs):
        descriptor, = self.app.state.project_registry.values()
        return await self.browser(method, f"/api/projects/{descriptor.project_id}{path}", **kwargs)

    async def in_home(self, operation: Callable[..., Awaitable], *args, **kwargs):
        token = self.selected_home.set(self.home)
        try:
            return await operation(*args, **kwargs)
        finally:
            self.selected_home.reset(token)

    @asynccontextmanager
    async def events(self):
        """Drive the real authenticated SSE route without buffering its whole body."""
        request = self.client.build_request("GET", API + "/events", headers={
            "Authorization": f"Bearer {LOCAL_TOKEN}", "Accept": "text/event-stream",
        })
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": request.url.scheme,
            "path": request.url.path, "raw_path": request.url.raw_path, "root_path": "",
            "query_string": b"", "server": (request.url.host, 80), "client": ("127.0.0.1", 12345),
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
        }
        incoming: asyncio.Queue = asyncio.Queue()
        outgoing: asyncio.Queue = asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        stream = asyncio.create_task(self.in_home(self.app, scope, incoming.get, outgoing.put))

        async def next_event():
            async with asyncio.timeout(5):
                message = await outgoing.get()
            assert message["type"] == "http.response.body" and message["more_body"]
            event, data = message["body"].decode().splitlines()[:2]
            return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))

        try:
            async with asyncio.timeout(5):
                start = await outgoing.get()
            assert start["type"] == "http.response.start" and start["status"] == 200
            headers = dict(start["headers"])
            assert headers[b"content-type"].startswith(b"text/event-stream") and headers[b"cache-control"] == b"no-store"
            yield next_event
        finally:
            stream.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stream

    @property
    def id(self):
        return self.registration["registration_id"]

    async def register(self, wire: Wire, server_id: str):
        assert _ok(await self.call("GET", "/registrations")) == {"registrations": []}
        self.registration = _ok(await self.call("POST", "/registrations", json={
            "profile_id": self.profile_id, "server_instance_id": server_id,
            "display_name": self.project.name,
        }), 201, wire=wire)
        assert self.registration["user_id"] == str(UUID(hex=self.user_id))
        assert self.registration["has_device_token"] is True
        assert self.registration["publication_enabled"] is False
        assert "device_token" not in self.registration
        assert _ok(await self.call("GET", "/exports")) == {"exports": []}


def _ok(response: httpx.Response, status=200, *, wire: Wire | None = None) -> dict:
    if response.status_code != status:
        trace = f"; Remote HTTP {wire.http_statuses[-4:]!r}; WS {wire.frames[-6:]!r}" if wire else ""
        pytest.fail(f"{response.request.url.path}: expected {status}, got {response.status_code} {response.json()!r}{trace}")
    return response.json()


@dataclass
class Rig:
    remote: object
    wire: Wire
    executor: Studio
    requester: Studio
    dispatches: list[Path]
    running: asyncio.Event
    release: asyncio.Event
    remote_dir: Path
    storage_epoch: str

    @property
    def server_id(self):
        return self.remote.state.ai_store.server_instance_id

    async def register(self):
        await self.executor.register(self.wire, self.server_id)
        await self.requester.register(self.wire, self.server_id)
        assert self.executor.id != self.requester.id
        assert self.executor.registration["client_id"] != self.requester.registration["client_id"]

    def documents(self):
        return _db_dump(self.remote_dir / "remote.db")


@pytest.fixture
async def rig(tmp_path, studio_home, monkeypatch, request):
    from voxarium_studio.teams.backends import base
    from voxarium_studio_remote.app import create_app as create_remote
    from voxarium_studio_remote.auth import hash_token

    # Simulate two installation processes in one loop. The only override is home
    # *location*, inherited by spawned tasks: all persistence/parsing/CAS stays real.
    selected_home = ContextVar("acp_fixture_home", default=studio_home)
    home_locator = paths.user_config_dir
    for module in (paths, application_config, workspace, bindings, credentials, identity, sync_state):
        assert module.user_config_dir is home_locator
        monkeypatch.setattr(module, "user_config_dir", selected_home.get)
    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)

    dispatches: list[Path] = []
    running = asyncio.Event()
    release = asyncio.Event()

    class Provider:
        supports_effort = False

        def is_available(self):
            return True, "fixture"

        async def run(self, request):
            dispatches.append(selected_home.get())
            running.set()
            await release.wait()
            yield AgentEvent(kind="result", text="Fixture model result", is_final=True, data={"is_error": False})

    monkeypatch.setattr(base, "_BACKENDS", {BACKEND: Provider})
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    remote = create_remote(remote_dir, ai_control=True)
    wire = Wire(remote)
    monkeypatch.setattr(sync_service, "connect", wire.connect_documents)
    # Both Select and the separate AI consent use their real CatalogClient and
    # HTTP validation. Only the low-level carrier is redirected into real Remote.
    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport", wire.catalog_transport)
    monkeypatch.setattr(service, "CatalogHttpTransport", wire.catalog_transport)
    store = remote.state.store
    space = store.create_project(PROJECT, "Shared ACP fixture")
    store.put(PROJECT, "fixture.md", "# Unchanged Documents fixture\n", None)

    @asynccontextmanager
    async def studio(name, *, account=None):
        if account is None:
            key = f"fixture-key-{name}"
            user_id = store.create_user(name, hash_token(key)).id
        else:
            key, user_id = account.key, account.user_id
        root, home = tmp_path / name, tmp_path / f"{name}-home"
        root.mkdir()
        home.mkdir()
        profile = application_config.RemoteServerProfile(profile_id=str(uuid4()), name="Fixture Remote", url=URL)
        token = selected_home.set(home)
        try:
            with application_config.application_config_transaction():
                application_config._write_application_profiles(
                    application_config.ApplicationConfig(remote_servers=[profile]),
                )
            credentials.save_profile_token(profile.profile_id, URL, key, expected_revision=0)
            init_project(root)
            app = create_app(str(root), auth=AuthSettings(token=LOCAL_TOKEN))
        finally:
            selected_home.reset(token)

        lifespan = app.router.lifespan_context(app)
        token = selected_home.set(home)
        try:
            await lifespan.__aenter__()
        finally:
            selected_home.reset(token)
        try:
            assert isinstance(app.state.ai_access, service.AiAccessService)
            app.state.ai_access.transport = wire.transport()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                yield Studio(app, client, home, selected_home, root, profile.profile_id, user_id, key)
        finally:
            token = selected_home.set(home)
            try:
                await lifespan.__aexit__(None, None, None)
            finally:
                selected_home.reset(token)

    async with remote.router.lifespan_context(remote):
        async with studio("executor") as executor:
            account = executor if getattr(request, "param", False) else None
            async with studio("requester", account=account) as requester:
                yield Rig(remote, wire, executor, requester, dispatches, running, release, remote_dir,
                          space.storage_epoch)


def _db_dump(path):
    assert path.exists(), path
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        return tuple(conn.iterdump())


async def _rows(studio, *models):
    async with studio.context.db.session() as session:
        return [
            sorted((row.model_dump_json() for row in (await session.exec(select(model))).all()))
            for model in models
        ]


async def test_browser_registers_two_studios_against_real_remote(rig):
    before = rig.documents()
    assert (await rig.requester.call("GET", "/registrations", authenticated=False)).status_code == 401
    assert _ok(await rig.executor.call("GET", "/registrations")) == {"registrations": []}
    assert not rig.wire.frames and not rig.wire.http
    await rig.register()
    assert not rig.wire.frames and not rig.dispatches
    assert rig.documents() == before


async def test_default_deny_for_already_registered_peers(rig):
    before = rig.documents()
    await rig.register()
    for studio in (rig.executor, rig.requester):
        assert _ok(await studio.call("GET", f"/registrations/{studio.id}/targets"), wire=rig.wire) == {
            "targets": [], "next_after": None,
        }
        directory = _ok(await studio.call("GET", f"/registrations/{studio.id}/directory"), wire=rig.wire)
        peers = directory["registrations"]
        public_fields = {"registration_id", "user_id", "client_id", "display_name", "kind"}
        assert {entry["registration_id"] for entry in peers} == {rig.executor.id, rig.requester.id}
        by_id = {entry["registration_id"]: entry for entry in peers}
        for device in (rig.executor, rig.requester):
            assert by_id[device.id] == {key: device.registration[key] for key in public_fields}
    assert all(kind not in {"executor.attach", "forward"} for _, kind, _ in rig.wire.frames)
    assert rig.documents() == before
    assert not rig.dispatches


@pytest.mark.parametrize("failure", ["wrong-key", "wrong-device", "other-user", "malformed-device"])
async def test_dual_auth_rejects_before_hello(rig, failure):
    await rig.register()
    studio = rig.requester

    async def native_credentials():
        return studio.app.state.ai_access.credentials(studio.id)

    _, key, device = await studio.in_home(native_credentials)
    if failure == "wrong-key":
        key = "synthetic-wrong-key"
    elif failure == "wrong-device":
        assert device.token.startswith("vxd_") and len(device.token) == 47
        prefix, first_digit, rest = device.token[:4], device.token[4], device.token[5:]
        device = DeviceCredentials(device.client_id, prefix + ("A" if first_digit != "A" else "B") + rest)
    elif failure == "malformed-device":
        device = DeviceCredentials(device.client_id, "a" * 43)
    else:
        key = rig.executor.key
    before = rig.documents()
    control = studio.app.state.ai_access.transport
    async with control.connect(URL, key, device) as socket:
        with pytest.raises(SocketClosed) as closed:
            async with asyncio.timeout(3):
                await socket.recv()
    assert closed.value.code == 4401
    assert rig.wire.frames == [("close", 4401, "unauthorised")]
    assert rig.documents() == before
    assert not rig.remote.state.ai_relay.connections
    assert not rig.dispatches


async def _seed_local_rows(studio, *, seed_rows=True, fixture_task_id=None):
    ctx = studio.context
    async with ctx.db.session() as session:
        member = (await session.exec(select(TeamMember).where(TeamMember.kind == MemberKind.AGENT))).first()
        assert member is not None
        team = await session.get(Team, member.team_id)
        team.autostart_tasks = not seed_rows
        session.add(team)
        member.agent_backend = BACKEND
        member.model = "fixture-model"
        member.isolate_worktree = False
        session.add(member)
    task = chat = None
    if seed_rows:
        if fixture_task_id is None:
            task = await ctx.teams.create_task(team_id=member.team_id, assignee_id=member.id,
                                               title="Local fixture task", brief=BRIEF, auto_start=False)
        else:
            task = TeamTask(id=fixture_task_id, team_id=member.team_id, assignee_id=member.id,
                            number=1, title=f"Task on {studio.project.name}", brief=BRIEF)
            async with ctx.db.session() as session:
                session.add(task)
        chat = await ctx.chats.create_chat(member.id, title="Local fixture chat")
    return member, task, chat


async def _select_project(rig, studio):
    shown = _ok(await studio.remote_project("GET"))
    assert not shown["enabled"] and shown["profile_id"] is None
    catalog = _ok(await studio.browser("GET", f"/api/remote/servers/{studio.profile_id}/projects"), wire=rig.wire)
    project, = catalog["items"]
    assert (project["id"], project["storage_epoch"]) == (PROJECT, rig.storage_epoch)
    observed = {"expected_revision": shown["identity_revision"], "config_fingerprint": shown["config_fingerprint"]}
    prepared = _ok(await studio.remote_project("POST", "/prepare", json={
        **observed, "operation": "select", "profile_id": studio.profile_id, "url": URL,
        "persistent_id": project["id"], "catalog_id": project["id"], "catalog_instance_id": project["storage_epoch"],
    }), wire=rig.wire)
    change = f"/changes/{prepared['change_id']}"
    generation = shown["binding_generation"]
    ready = _ok(await studio.remote_project("POST", change + "/quiesce", json={"expected_generation": generation}))
    assert ready["state"] == "ready"
    committed = _ok(await studio.remote_project("POST", change + "/commit", json={
        **observed, "expected_generation": generation + 1,
        "confirmation": {key: prepared["to"][key] for key in ("url", "persistent_id")},
    }), wire=rig.wire)
    assert committed["enabled"] is False
    shown = _ok(await studio.remote_project("GET"))
    assert (shown["profile_id"], shown["url"], shown["persistent_id"], shown["space_instance_id"]) == (
        studio.profile_id, URL, PROJECT, rig.storage_epoch,
    )
    assert shown["enabled"] is False
    return shown


def _export_consent(shown, registration):
    assert (registration["profile_id"], registration["url"]) == (shown["profile_id"], shown["url"])
    return {
        "registration_id": registration["registration_id"], "profile_id": shown["profile_id"],
        "server_instance_id": registration["server_instance_id"], "project_id": shown["project_id"],
        "project_epoch": shown["epoch"], "persistent_id": shown["persistent_id"],
        "identity_revision": shown["identity_revision"], "storage_epoch": shown["space_instance_id"],
        "display_name": "Executor export",
    }


async def _draft_export(rig, studio, consent):
    response = await studio.call("POST", "/exports", json=consent)
    draft = _ok(response, 201, wire=rig.wire)
    assert response.headers["Cache-Control"] == "no-store"
    assert draft["enabled"] is False and draft["mirror_state"] == "unpublished"
    return draft


async def _publish(rig, *, reversed_roles=False):
    studio, peer = (rig.requester, rig.executor) if reversed_roles else (rig.executor, rig.requester)
    selected = await _select_project(rig, studio)
    draft = await _draft_export(rig, studio, _export_consent(selected, studio.registration))
    enabled = _ok(await studio.call("PUT", f"/registrations/{studio.id}/publication", json={
        "enabled": True, "expected_revision": studio.registration["revision"],
    }), wire=rig.wire)
    assert enabled["publication_enabled"] is True
    target = draft["target"]
    published = _ok(await studio.call("PUT", f"/exports/{target['export_id']}/publication", json={
        "enabled": True, "expected_revision": target["export_revision"],
    }), wire=rig.wire)
    assert published["enabled"] and published["mirror_state"] == "effective"
    assert _ok(await peer.call("GET", f"/registrations/{peer.id}/targets"), wire=rig.wire)["targets"] == []
    return published["target"]


def _policy_path(target, member):
    return f"/exports/{target['export_id']}/teams/{member.team_id}/members/{member.id}/execution-policy"


async def _preview_policy(studio, target, member):
    shown = _ok(await studio.call("GET", _policy_path(target, member)))
    assert (shown["target"], shown["team_id"], shown["member_id"]) == (target, member.team_id, member.id)
    return shown


def _consent(preview, operations, **options):
    return {
        **options,
        "expected_export_revision": preview["target"]["export_revision"],
        "execution_policy_digest": preview["execution_policy_digest"],
        "team_ids": [preview["team_id"]], "member_ids": [preview["member_id"]],
        "operations": operations,
    }


def _grantee(studio):
    return {"grantee_user_id": studio.registration["user_id"], "grantee_registration_id": studio.id}


async def _create_grant(rig, studio, peer, preview, operations):
    target = preview["target"]
    receipt = _ok(await studio.call("POST", f"/exports/{target['export_id']}/grants", json={
        **_grantee(peer), **_consent(preview, operations, resource_scope="all_resources_in_selected_teams"),
    }), 201, wire=rig.wire)
    grant = receipt["grant"]
    assert receipt["acked_revision"] == grant["revision"] and grant["target"] == target
    return grant


async def _grant(rig, target, member, operations, *, reversed_roles=False):
    studio, peer = (rig.requester, rig.executor) if reversed_roles else (rig.executor, rig.requester)
    preview = await _preview_policy(studio, target, member)
    grant = await _create_grant(rig, studio, peer, preview, operations)
    entry, = _ok(await peer.call("GET", f"/registrations/{peer.id}/targets"), wire=rig.wire)["targets"]
    assert entry["target"] == target
    assert entry["state"] == "online"
    assert entry["grant"]["grant_id"] == grant["grant_id"]
    return entry


def _request(entry, op, payload, *, mutation=False, **preconditions):
    return {"target": entry["target"], "grant_id": entry["grant"]["grant_id"],
            "grant_revision": entry["grant"]["revision"], "expected_executor_epoch": entry["executor_epoch"],
            "op": op, "payload": payload, **preconditions,
            **({"command_id": str(uuid4()), "issued_at": now_ms()} if mutation else {})}


async def _send(rig, request, status=200, *, reversed_roles=False):
    peer = rig.executor if reversed_roles else rig.requester
    return _ok(await peer.call("POST", "/requests", json={
        "registration_id": peer.id, "request": request,
    }), status, wire=rig.wire)


def _result(reply):
    assert reply["state"] == "completed" and "code" not in reply, reply
    return reply["result"]


def _assert_committed_index(trace, target):
    socket_id, begin, commit, ack = trace
    assert begin["export_id"] == target["export_id"] and begin["export_revision"] == target["export_revision"]
    assert commit == {key: begin[key] for key in ("hash", "index_revision")}
    assert begin["row_count"] > 0
    assert ack == {"state": "completed", "result": {**begin, "state": "committed"}}
    return socket_id, begin


async def _enable_documents(rig, studio):
    selected = await _select_project(rig, studio)
    observed = {"expected_revision": selected["identity_revision"],
                "config_fingerprint": selected["config_fingerprint"]}
    preview = _ok(await studio.remote_project("POST", "/reconcile-preview", json=observed), wire=rig.wire)
    enabled = _ok(await studio.remote_project("POST", "/enable", json={
        "expected_revision": observed["expected_revision"], "expected_generation": selected["binding_generation"],
        "preview_id": preview["preview_id"], "fingerprint": preview["fingerprint"],
        "confirmation": {"url": URL, "persistent_id": PROJECT, "action": "reconcile"},
    }), wire=rig.wire)
    assert enabled["enabled"] is True
    async with asyncio.timeout(5):
        while True:
            shown = _ok(await studio.remote_project("GET"))
            assert shown["state"] not in {"blocked", "unauthorised"}, shown
            if shown["state"] == "connected":
                break
            await asyncio.sleep(0.02)
    assert (shown["enabled"], shown["persistent_id"], shown["space_instance_id"]) == (True, PROJECT, rig.storage_epoch)


async def _save_shared_document(rig, writer, reader, content):
    binding = _ok(await writer.remote_project("GET"))
    fence = {"backend_epoch": binding["backend_epoch"], "expected_binding_generation": binding["binding_generation"]}
    client = f"fixture-editor-{uuid4()}"
    lease = _ok(await writer.remote_project("POST", "/editors", json={
        **fence, "document_path": "fixture.md", "client_instance_id": client,
    }), 201)
    saved = _ok(await writer.project_api("PUT", "/docs/fixture.md", json={
        **fence, "editor_id": lease["editor_id"], "version": 1, "content": content,
    }))
    _ok(await writer.remote_project("DELETE", f"/editors/{lease['editor_id']}", json={
        **fence, "client_instance_id": client, "version": 1,
        "last_saved_version": saved["last_saved_version"], "dirty": False,
    }))
    async with asyncio.timeout(5):
        while True:
            received = _ok(await reader.project_api("GET", "/docs/fixture.md"))
            settled = _ok(await writer.remote_project("GET"))["pending_writes"] == 0
            if received["content"] == content and settled:
                break
            await asyncio.sleep(0.02)
    stored = rig.remote.state.store.doc(PROJECT, "fixture.md")
    assert (stored.content, stored.updated_by) == (content, writer.user_id)
    return stored.rev


@pytest.mark.parametrize("rig", [True], indirect=True, ids=["same-user"])
async def test_shared_documents_give_no_ai_authority(rig):
    owner, peer = rig.executor, rig.requester
    assert owner.user_id == peer.user_id and owner.project != peer.project
    member, task, _ = await owner.in_home(_seed_local_rows, owner)
    transcript = "Existing local task transcript: never shared by Documents"
    async with owner.context.db.session() as session:
        session.add(AgentMessage(task_id=task.id, member_id=member.id, role="assistant", text=transcript))
    studios = (owner, peer)
    control_db = rig.remote_dir / "ai-control.db"

    def project_dbs():
        return tuple(_db_dump(studio.context.paths.db_file) for studio in studios)

    def document_snapshot():
        markdown = tuple({path.relative_to(studio.context.docs.directory).as_posix(): path.read_bytes()
                          for path in Path(studio.context.docs.directory).rglob("*.md")} for studio in studios)
        return rig.documents(), markdown

    projects_before, documents_before, control_before = project_dbs(), rig.documents(), _db_dump(control_db)
    assert transcript in "\n".join(projects_before[0]) and transcript not in "\n".join(projects_before[1])

    for studio in studios:
        await _enable_documents(rig, studio)
        assert _ok(await studio.project_api("GET", "/docs/fixture.md"))["content"] == "# Unchanged Documents fixture\n"
        assert _ok(await studio.call("GET", "/registrations")) == {"registrations": []}
        assert _ok(await studio.call("GET", "/exports")) == {"exports": []}
    first = "# Shared Documents\n\nSaved on the owner Studio.\n"
    assert await _save_shared_document(rig, owner, peer, first) == 2
    assert rig.documents() != documents_before
    unavailable = await peer.call("GET", f"/registrations/{uuid4()}/targets")
    assert _ok(unavailable, 404) == {"detail": "not_available"}
    assert project_dbs() == projects_before and _db_dump(control_db) == control_before
    assert not rig.wire.socket_paths and not rig.wire.frames and not rig.dispatches
    assert all(method == "GET" and (path == "/prefix/healthz" or path.startswith("/prefix/api/v2/"))
               for method, path in rig.wire.http)

    documents_synced = document_snapshot()
    assert documents_synced[1] == ({"fixture.md": first.encode()},) * 2
    # Registration is explicit so well-formed probes reach Remote, but never publish or grant.
    await rig.register()
    assert owner.registration["user_id"] == peer.registration["user_id"]
    control_registered = _db_dump(control_db)
    http, doc_trace = len(rig.wire.http), tuple(rig.wire.document_frames)
    for studio in studios:
        assert _ok(await studio.call("GET", f"/registrations/{studio.id}/targets"), wire=rig.wire) == {
            "targets": [], "next_after": None,
        }
    unissued = {"target": {"server_instance_id": rig.server_id, "executor_registration_id": owner.id,
                           "export_id": str(uuid4()), "persistent_id": PROJECT,
                           "storage_epoch": rig.storage_epoch, "export_revision": 1},
                "grant": {"grant_id": str(uuid4()), "revision": 1}, "executor_epoch": str(uuid4())}
    read = _request(unissued, "transcript.page", {"resource_id": task.id})
    start = _request(unissued, "task.start", {"resource_id": task.id}, mutation=True, expected_resource_revision=1)
    refused = {"target": unissued["target"], "state": "rejected", "code": "not_available"}
    assert await _send(rig, read) == refused
    assert await _send(rig, start) == {**refused, "command_id": start["command_id"]}
    receipt, = _ok(await peer.call("GET", "/receipts"))["receipts"]
    assert (receipt["op"], receipt["command_id"], receipt["state"]) == ("task.start", start["command_id"], "rejected")
    assert not rig.wire.http[http:]  # Neither legacy REST proxy nor extra catalog/sync probes.
    assert tuple(rig.wire.document_frames) == doc_trace
    assert rig.wire.socket_paths == ["/prefix/api/ai/v1/control"] * 2
    kinds = [kind for _, kind, _ in rig.wire.frames]
    assert kinds.count("request") == 2 and kinds.count("directory.list") == 2
    assert rig.wire.frames.count(("remote→studio", "response", "not_available")) == 2
    assert not {"forward", "executor.attach", "export.publish", "grant.put"} & set(kinds)
    assert not rig.remote.state.ai_relay.executors
    assert (project_dbs(), _db_dump(control_db), document_snapshot()) == (
        projects_before, control_registered, documents_synced,
    )
    assert not rig.dispatches

    second = "# Shared Documents\n\nSaved back from the other Studio.\n"
    assert await _save_shared_document(rig, peer, owner, second) == 3
    assert document_snapshot()[1] == ({"fixture.md": second.encode()},) * 2
    assert (project_dbs(), _db_dump(control_db)) == (projects_before, control_registered)
    assert rig.wire.document_urls == [f"wss://relay.invalid/prefix/api/v2/sync?space={PROJECT}"] * 2
    for socket in range(2):
        for side, kind, path in (("remote→studio", "hello", None), ("studio→remote", "sync", None),
                                 ("remote→studio", "synced", None), ("studio→remote", "put", "fixture.md"),
                                 ("remote→studio", "ack", "fixture.md"), ("remote→studio", "change", "fixture.md")):
            assert (socket, side, kind, path) in rig.wire.document_frames
    assert all("/api/v1/" not in path for _, path in rig.wire.http if "/api/ai/" not in path)
    documents_sql = "\n".join(rig.documents())
    assert transcript not in documents_sql and BRIEF not in documents_sql
    assert not rig.dispatches


async def test_selected_project_requires_explicit_ai_draft_without_documents_side_effects(rig):
    await rig.register()
    studio, peer = rig.executor, rig.requester
    remote_before = rig.documents(), _db_dump(rig.remote_dir / "ai-control.db")
    start = len(rig.wire.http)
    shown = await _select_project(rig, studio)
    select_reads = rig.wire.http[start:]
    assert ("GET", "/prefix/api/v2/projects") in select_reads
    assert ("GET", f"{CATALOG_DETAIL}/snapshot") in select_reads
    assert all(method == "GET" for method, _ in select_reads)
    assert not rig.wire.socket_paths and not rig.wire.frames
    assert (rig.documents(), _db_dump(rig.remote_dir / "ai-control.db")) == remote_before
    local_registration, = _ok(await studio.call("GET", "/registrations"))["registrations"]
    assert local_registration == studio.registration and local_registration["publication_enabled"] is False
    assert _ok(await studio.call("GET", "/exports")) == {"exports": []}

    async def selected_binding():
        return bindings.RemoteBindings().read().get(studio.project)

    binding = await studio.in_home(selected_binding)
    assert binding.selection_state == "selected" and binding.space_instance_id == shown["space_instance_id"]
    consent = _export_consent(shown, local_registration)

    async def snapshot(studio):
        files = (studio.context.paths.config_file, application_config.application_config_file(),
                 bindings.bindings_file(), credentials.keys_file(), identity.secrets_file())
        return ([(path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None for path in files],
                _db_dump(studio.context.paths.db_file))

    local_before = await studio.in_home(snapshot, studio), await peer.in_home(snapshot, peer)
    registry_path = studio.home / identity.REGISTRY_FILE
    registry_before = registry_path.read_bytes(), registry_path.stat().st_mtime_ns
    start = len(rig.wire.http)
    assert (await studio.call("POST", "/exports", authenticated=False, json=consent)).status_code == 401
    for required in ("profile_id", "server_instance_id"):
        response = await studio.call("POST", "/exports", json={key: value for key, value in consent.items()
                                                              if key != required})
        assert _ok(response, 422) == {"detail": "invalid_request"}
    response = await studio.call("POST", "/exports", json={**consent, "policy_digest": "a" * 64})
    assert _ok(response, 422) == {"detail": "invalid_request"}
    response = await studio.call("POST", "/exports", json={**consent, "server_instance_id": str(uuid4())})
    assert _ok(response, 409) == {"detail": "stale_target"}
    assert len(rig.wire.http) == start and not rig.wire.socket_paths
    assert (registry_path.read_bytes(), registry_path.stat().st_mtime_ns) == registry_before
    assert _ok(await studio.call("GET", "/exports")) == {"exports": []}

    async def read_registry():
        return identity.load_registry()

    original_registry = await studio.in_home(read_registry)
    draft = await _draft_export(rig, studio, consent)
    target = draft["target"]
    saved_registry = await studio.in_home(read_registry)
    stored = saved_registry.exports[target["export_id"]]
    assert stored.policy_digest == service.export_policy_digest() and stored.consent is not None
    assert stored.acked_revision == stored.mirror_revision == 0
    assert saved_registry == original_registry.model_copy(update={
        "revision": original_registry.revision + 1, "exports": {target["export_id"]: stored},
    })
    after_draft = registry_path.read_bytes(), registry_path.stat().st_mtime_ns
    assert target == {"server_instance_id": consent["server_instance_id"],
                      "executor_registration_id": studio.id, "export_id": target["export_id"],
                      "persistent_id": shown["persistent_id"], "storage_epoch": shown["space_instance_id"],
                      "export_revision": 1}
    assert rig.wire.http[start:] == CONSENT_READS
    assert rig.wire.http_statuses[start:] == [(method, path, 200) for method, path in CONSENT_READS]

    publication = await studio.call("PUT", f"/exports/{target['export_id']}/publication", json={
        "enabled": True, "expected_revision": target["export_revision"],
    })
    assert _ok(publication, 409) == {"detail": "publication_disabled"}
    assert (registry_path.read_bytes(), registry_path.stat().st_mtime_ns) == after_draft
    assert rig.wire.http[start:] == CONSENT_READS
    assert _ok(await studio.call("GET", "/exports")) == {"exports": [draft]}
    assert _ok(await studio.call("GET", "/registrations")) == {"registrations": [local_registration]}
    assert _ok(await studio.call("GET", f"/exports/{target['export_id']}/grants")) == {"grants": []}
    latest = _ok(await studio.remote_project("GET"))
    assert latest["enabled"] is False and latest["active_rooms"] == latest["pending_writes"] == 0
    assert await studio.in_home(selected_binding) == binding
    assert (await studio.in_home(snapshot, studio), await peer.in_home(snapshot, peer)) == local_before
    assert (rig.documents(), _db_dump(rig.remote_dir / "ai-control.db")) == remote_before
    assert not rig.wire.socket_paths and not rig.wire.frames and not rig.dispatches


async def test_real_publish_mirror_and_index(rig):
    before = rig.documents()
    await rig.register()
    member, _, _ = await rig.executor.in_home(_seed_local_rows, rig.executor)
    target = await _publish(rig)
    await _grant(rig, target, member, ["view_status"])
    assert [(direction, kind) for direction, kind, _ in rig.wire.frames if kind in {
        "executor.attach", "export.publish", "grant.put", "resource.index.begin", "resource.index.commit",
    }] == [("studio→remote", kind) for kind in (
        "executor.attach", "export.publish", "grant.put", "resource.index.begin", "resource.index.commit",
    )]
    assert not rig.dispatches
    assert rig.documents() == before


async def test_requester_can_page_all_targets_and_receive_terminal_cursor(rig):
    await rig.register()
    executor, requester = rig.executor, rig.requester
    member, *_ = await executor.in_home(_seed_local_rows, executor)
    target = await _publish(rig)
    preview = await _preview_policy(executor, target, member)
    expected = sorted([(await _create_grant(rig, executor, requester, preview, ["view_status"]))["grant_id"]
                       for _ in range(9)])
    path = f"/registrations/{requester.id}/targets"
    before = rig.documents(), _db_dump(rig.remote_dir / "ai-control.db")
    start = len(rig.wire.frames)
    first = _ok(await requester.call("GET", path), wire=rig.wire)
    assert len(first["targets"]) == 6 and first["next_after"] == expected[5]
    second = _ok(await requester.call("GET", path, params={"after": first["next_after"]}), wire=rig.wire)
    assert len(second["targets"]) == 3 and second["next_after"] is None
    entries = first["targets"] + second["targets"]
    assert [item["grant"]["grant_id"] for item in entries] == expected
    assert all(item["target"] == target and item["state"] == "online" for item in entries)
    assert [kind for _, kind, _ in rig.wire.frames[start:] if kind in {"directory.list", "forward"}] == [
        "directory.list", "directory.list",
    ]
    assert not rig.dispatches and (rig.documents(), _db_dump(rig.remote_dir / "ai-control.db")) == before


async def test_public_policy_consent_rejects_digest_after_local_policy_changes(rig):
    await rig.register()
    executor, requester = rig.executor, rig.requester
    member, *_ = await executor.in_home(_seed_local_rows, executor)
    target = await _publish(rig)
    preview = _policy_path(target, member)
    assert (await executor.call("GET", preview, authenticated=False)).status_code == 401
    assert (await requester.call("GET", preview)).status_code == 404
    start = len(rig.wire.frames)
    response = await executor.call("GET", preview)
    old = _ok(response)
    assert response.headers["cache-control"] == "no-store"
    assert set(old) == {"target", "team_id", "member_id", "execution_policy_digest", "backend",
                        "permission_mode", "tools", "mcp", "limits"}
    assert (old["target"], old["team_id"], old["member_id"], old["backend"]) == (
        target, member.team_id, member.id, BACKEND,
    )
    assert old["tools"] == {"backend_native_tools_may_include_shell": True}
    assert set(old["mcp"]) == {"strict", "project_server_count"}
    assert old["limits"]["remote_runs_per_export"] == old["limits"]["remote_runs_per_grant"] == 1
    assert len(rig.wire.frames) == start

    max_turns = 8 if old["limits"]["max_turns"] != 8 else 9
    _ok(await executor.in_home(executor.client.patch, f"/api/teams/{member.team_id}/members/{member.id}",
                                headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
                                json={"run_settings_override": True, "max_turns": max_turns}))
    path = f"/exports/{target['export_id']}/grants"
    operations = ["view_status", "run_task"]
    consent = {**_grantee(requester), **_consent(old, operations)}
    remote_before = rig.documents(), _db_dump(rig.remote_dir / "ai-control.db")
    start = len(rig.wire.frames)
    assert _ok(await executor.call("POST", path, json=consent), 409)["detail"] == "stale_target"
    assert _ok(await executor.call("GET", path)) == {"grants": []}
    assert len(rig.wire.frames) == start
    assert (rig.documents(), _db_dump(rig.remote_dir / "ai-control.db")) == remote_before

    current = await _preview_policy(executor, target, member)
    assert current["limits"]["max_turns"] == max_turns
    assert current["execution_policy_digest"] != old["execution_policy_digest"]
    created = _ok(await executor.call("POST", path, json={
        **_grantee(requester), **_consent(current, operations),
    }), 201, wire=rig.wire)
    grant = created["grant"]
    assert created["acked_revision"] == grant["revision"]
    assert grant["execution_policy_digest"] == current["execution_policy_digest"]
    assert grant["resource_scope"] == "created_by_this_grantee"
    entry, = _ok(await requester.call("GET", f"/registrations/{requester.id}/targets"))["targets"]
    assert entry["target"] == target and entry["grant"]["grant_id"] == grant["grant_id"]
    assert not rig.dispatches and rig.documents() == remote_before[0]


async def test_republished_export_requires_fresh_public_consent_for_create_and_update(rig):
    await rig.register()
    executor, requester = rig.executor, rig.requester
    member, task, _ = await executor.in_home(_seed_local_rows, executor)
    target = await _publish(rig)
    old = await _preview_policy(executor, target, member)
    grants_path = f"/exports/{target['export_id']}/grants"
    targets_path = f"/registrations/{requester.id}/targets"
    old_grant = await _create_grant(rig, executor, requester, old, ["view_status"])
    old_entry, = _ok(await requester.call("GET", targets_path))["targets"]
    assert old_entry["target"] == target and old_entry["grant"]["grant_id"] == old_grant["grant_id"]
    payload = {"resource_id": task.id}
    read = _request(old_entry, "resource.get", payload)
    before_republish = _result(await _send(rig, read))
    assert (before_republish["resource_id"], before_republish["title"]) == (task.id, task.title)
    initial_ack, = rig.wire.index_commits
    executor_socket, initial_index = _assert_committed_index(initial_ack, target)
    sockets = tuple(rig.wire.socket_paths)

    publication = f"/exports/{target['export_id']}/publication"
    withdrawn = _ok(await executor.call("PUT", publication, json={
        "enabled": False, "expected_revision": target["export_revision"],
    }), wire=rig.wire)
    assert not withdrawn["enabled"] and withdrawn["mirror_state"] == "unpublished"
    republished = _ok(await executor.call("PUT", publication, json={
        "enabled": True, "expected_revision": withdrawn["target"]["export_revision"],
    }), wire=rig.wire)
    current_target = republished["target"]
    assert republished["enabled"] and republished["mirror_state"] == "effective"
    assert current_target == {**target, "export_revision": target["export_revision"] + 2}
    no_targets = {"targets": [], "next_after": None}
    assert _ok(await requester.call("GET", targets_path), wire=rig.wire) == no_targets
    forwarded_before = rig.wire.frames.count(("remote→studio", "forward", None))
    denied = await _send(rig, read)
    assert (denied["state"], denied["code"]) == ("rejected", "denied")
    assert rig.wire.frames.count(("remote→studio", "forward", None)) == forwarded_before
    assert rig.wire.index_commits == [initial_ack]

    def durable_state():
        registries = [studio.home / identity.REGISTRY_FILE for studio in (executor, requester)]
        return ([(path.read_bytes(), path.stat().st_mtime_ns) for path in registries],
                _db_dump(rig.remote_dir / "ai-control.db"), rig.documents())

    models = (TeamTask, AgentChat, AgentChatMessage, AiCommandLedger)
    projects_before = await _rows(executor, *models), await _rows(requester, *models)

    async def reject(method, path, payload, expected_targets, *, code="stale_target", status=409):
        before = durable_state()
        local_grants = _ok(await executor.call("GET", grants_path))
        start = len(rig.wire.frames), len(rig.wire.http)
        response = await executor.call(method, path, json=payload)
        assert _ok(response, status, wire=rig.wire) == {"detail": code}
        assert (len(rig.wire.frames), len(rig.wire.http)) == start  # No mirror, index, or other network call.
        assert _ok(await executor.call("GET", grants_path)) == local_grants
        assert _ok(await requester.call("GET", targets_path), wire=rig.wire) == expected_targets
        assert durable_state() == before
        assert (await _rows(executor, *models), await _rows(requester, *models)) == projects_before
        assert not rig.remote.state.ai_relay.pending and not rig.dispatches

    create_operations = ["view_status", "run_task"]
    stale_create = _consent(old, create_operations, resource_scope="all_resources_in_selected_teams")
    await reject("POST", grants_path, {**_grantee(requester), **stale_create}, no_targets)
    update_operations = ["view_status", "run_task", "operate_existing"]
    stale_edit = _consent(old, update_operations, resource_scope="all_resources_in_selected_teams")
    old_grant_path = f"{grants_path}/{old_grant['grant_id']}"
    await reject("PUT", old_grant_path, {**stale_edit, "expected_revision": old_grant["revision"]}, no_targets)
    for method, path, body in (
        ("POST", grants_path, {**_grantee(requester), **stale_create}),
        ("PUT", old_grant_path, {**stale_edit, "expected_revision": old_grant["revision"]}),
    ):
        body.pop("expected_export_revision")
        await reject(method, path, body, no_targets, code="invalid_request", status=422)

    fresh = await _preview_policy(executor, current_target, member)
    assert fresh == {**old, "target": current_target}  # In particular, unchanged execution policy digest.
    await reject("PUT", f"{grants_path}/{old_grant['grant_id']}", {
        **_consent(fresh, update_operations, resource_scope="all_resources_in_selected_teams"),
        "expected_revision": old_grant["revision"],
    }, no_targets)  # Even a fresh preview does not migrate the old grant.
    grant = await _create_grant(rig, executor, requester, fresh, create_operations)
    first_ack, new_ack = rig.wire.index_commits
    assert first_ack == initial_ack
    index_socket, new_index = _assert_committed_index(new_ack, current_target)
    assert index_socket == executor_socket
    assert new_index["index_revision"] > initial_index["index_revision"]
    assert (new_index["hash"], new_index["row_count"]) == (initial_index["hash"], initial_index["row_count"])
    assert tuple(rig.wire.socket_paths) == sockets
    assert grant["grant_id"] != old_grant["grant_id"]
    assert all(grant[key] == value for key, value in _grantee(requester).items())
    visible = _ok(await requester.call("GET", targets_path), wire=rig.wire)
    entry, = visible["targets"]
    assert entry["target"] == current_target and entry["state"] == "online"
    assert entry["grant"]["grant_id"] == grant["grant_id"]
    assert entry["grant"]["revision"] == grant["revision"] and entry["grant"]["operations"] == create_operations
    assert entry["executor_epoch"] == old_entry["executor_epoch"]
    denied = await _send(rig, read)
    assert (denied["state"], denied["code"]) == ("rejected", "denied")
    assert rig.wire.frames.count(("remote→studio", "forward", None)) == forwarded_before
    # Open the original task directly on the new revision, before listing could
    # hide a missing index or the client could use a list row as the task view.
    reopened = _result(await _send(rig, _request(entry, "resource.get", payload)))
    assert reopened["stream_epoch"] != before_republish["stream_epoch"]  # The status feed is target-scoped.
    assert reopened == {**before_republish, "stream_epoch": reopened["stream_epoch"]}
    assert rig.wire.frames.count(("remote→studio", "forward", None)) == forwarded_before + 1
    assert tuple(rig.wire.socket_paths) == sockets
    listed = _result(await _send(rig, _request(entry, "resource.list", {})))
    assert any(item["resource_id"] == task.id for item in listed["items"])

    # A grant already on the current publication must still reject the old preview;
    # this cannot pass merely because the previous grant's own target was fenced.
    update_path = f"{grants_path}/{grant['grant_id']}"
    await reject("PUT", update_path, {**stale_edit, "expected_revision": grant["revision"]}, visible)
    fresh_edit = await _preview_policy(executor, current_target, member)
    edit = _consent(fresh_edit, update_operations, resource_scope="all_resources_in_selected_teams",
                    expected_revision=grant["revision"])
    mirror_start = len(rig.wire.frames)
    updated = _ok(await executor.call("PUT", update_path, json=edit), wire=rig.wire)
    result = updated["grant"]
    assert updated["acked_revision"] == result["revision"] == grant["revision"] + 1
    assert (result["target"], result["grant_id"], result["operations"]) == (
        current_target, grant["grant_id"], update_operations,
    )
    assert result["execution_policy_digest"] == fresh_edit["execution_policy_digest"]
    assert [kind for _, kind, _ in rig.wire.frames[mirror_start:] if kind == "grant.put"] == ["grant.put"]
    visible = _ok(await requester.call("GET", targets_path), wire=rig.wire)
    entry, = visible["targets"]
    assert entry["target"] == current_target and entry["state"] == "online"
    assert entry["grant"] == {key: result[key] for key in entry["grant"]}
    await reject("PUT", update_path, edit, visible, code="ai_access_conflict")  # Grant revision CAS remains separate.


async def test_two_executors_with_same_portable_id_and_same_task_id_are_distinct(rig):
    await rig.register()
    task_id = "task_same_id_on_both_studios"
    a, b = rig.executor, rig.requester
    ma, ta, _ = await a.in_home(_seed_local_rows, a, fixture_task_id=task_id)
    mb, tb, _ = await b.in_home(_seed_local_rows, b, fixture_task_id=task_id)
    target_a = await _publish(rig)
    target_b = await _publish(rig, reversed_roles=True)
    assert target_a["persistent_id"] == target_b["persistent_id"] == PROJECT
    assert target_a["storage_epoch"] == target_b["storage_epoch"] == rig.storage_epoch
    assert target_a["executor_registration_id"] == a.id and target_b["executor_registration_id"] == b.id
    operations = ["view_status", "run_task", "operate_existing"]
    ea = await _grant(rig, target_a, ma, operations)
    eb = await _grant(rig, target_b, mb, operations, reversed_roles=True)
    assert target_a["export_id"] != target_b["export_id"]
    assert ea["executor_epoch"] != eb["executor_epoch"]
    db_a, db_b = a.context.db.path, b.context.db.path
    before_a, before_b, documents = _db_dump(db_a), _db_dump(db_b), rig.documents()
    payload = {"resource_id": task_id}
    shown_a = _result(await _send(rig, _request(ea, "resource.get", payload)))
    shown_b = _result(await _send(rig, _request(eb, "resource.get", payload), reversed_roles=True))
    assert (shown_a["title"], shown_b["title"]) == (ta.title, tb.title)
    swapped = await _send(rig, _request({**ea, "target": target_b}, "resource.get", payload))
    assert (swapped["state"], swapped["code"]) == ("rejected", "not_available")
    assert (_db_dump(db_a), _db_dump(db_b), rig.documents()) == (before_a, before_b, documents)
    assert not rig.dispatches

    start = _request(ea, "task.start", payload, mutation=True, expected_resource_revision=shown_a["revision"])
    accepted = await _send(rig, start)
    assert (accepted["state"], accepted["command_id"]) == ("accepted", start["command_id"])
    run_id = accepted["result"]["run_id"]
    job = None
    try:
        async with asyncio.timeout(5):
            await rig.running.wait()
        job = a.context.jobs.get(task_id)
        assert job is not None and not job.task.done()
        assert b.context.jobs.get(task_id) is None
        async with a.context.db.session() as session:
            running = await session.get(TeamTask, task_id)
            assert running is not None and running.status == "in_progress"
        assert _db_dump(db_b) == before_b
        assert rig.dispatches == [a.home]
    finally:
        rig.release.set()
        if job is not None:
            async with asyncio.timeout(5):
                await job.task

    receipt = _result(await _send(rig, _request(ea, "command.get", {"command_id": start["command_id"]})))
    assert (receipt["state"], receipt["run_id"]) == ("completed", run_id)
    async with a.context.db.session() as session:
        updated, = (await session.exec(select(TeamTask))).all()
        ledger, = (await session.exec(select(AiCommandLedger))).all()
        assert (updated.id, updated.title) == (task_id, ta.title)
        assert updated.status in {"needs_review", "done"}
        assert (ledger.op, ledger.resource_id, ledger.run_id, ledger.state) == (
            "task.start", task_id, run_id, "completed",
        )
        assert (ledger.executor_registration_id, ledger.export_id, ledger.source_registration_id) == (
            a.id, target_a["export_id"], b.id,
        )
    assert _result(await _send(rig, _request(eb, "resource.get", payload), reversed_roles=True)) == shown_b
    assert (_db_dump(db_b), rig.documents()) == (before_b, documents)
    assert rig.dispatches == [a.home]


@pytest.mark.parametrize("rig", [True], indirect=True, ids=["same-user"])
async def test_grant_does_not_extend_to_another_device_of_the_same_user(rig):
    await rig.register()
    executor, requester = rig.executor, rig.requester
    assert executor.registration["user_id"] == requester.registration["user_id"]
    member, task, _ = await executor.in_home(_seed_local_rows, executor)
    target = await _publish(rig)
    entry = await _grant(rig, target, member, ["view_status"])
    models = (TeamTask, AgentChat, AiCommandLedger)
    before = await _rows(executor, *models), await _rows(requester, *models), rig.documents()
    read = _request(entry, "resource.get", {"resource_id": task.id})
    assert _result(await _send(rig, read))["title"] == task.title
    start = len(rig.wire.frames)
    denied = await _send(rig, read, reversed_roles=True)
    assert (denied["state"], denied["code"]) == ("rejected", "not_available")
    assert not any(kind == "forward" for _, kind, _ in rig.wire.frames[start:])
    assert not rig.dispatches and (await _rows(executor, *models), await _rows(requester, *models), rig.documents()) == before


async def test_read_only_grant_and_stale_target_do_not_change_either_studio(rig):
    await rig.register()
    member, task, chat = await rig.executor.in_home(_seed_local_rows, rig.executor)
    target = await _publish(rig)
    entry = await _grant(rig, target, member, ["view_status", "run_task"])
    models = (TeamTask, AgentChat, AgentChatMessage, AiCommandLedger)
    before = await _rows(rig.executor, *models), await _rows(rig.requester, *models), rig.documents()

    listing = _result(await _send(rig, _request(entry, "resource.list", {})))
    assert {item["resource_id"] for item in listing["items"]} == {task.id, chat.id}
    local = _result(await _send(rig, _request(entry, "resource.get", {"resource_id": task.id})))
    assert local["title"] == task.title and local["status"] == "todo"
    assert BRIEF not in json.dumps([listing, local])
    forwarded = rig.wire.frames.count(("remote→studio", "forward", None))
    transcript = await _send(rig, _request(entry, "transcript.page", {"resource_id": task.id}))
    assert transcript["state"] == "rejected" and transcript["code"] == "denied"
    start = await _send(rig, _request(entry, "task.start", {"resource_id": task.id},
                                     mutation=True, expected_resource_revision=local["revision"]))
    assert start["state"] == "rejected" and start["code"] == "denied"  # no operate_existing
    stale_entry = {**entry, "target": {**target, "storage_epoch": "b" * 32}}
    stale = await _send(rig, _request(stale_entry, "resource.get", {"resource_id": task.id}))
    assert stale["state"] == "rejected" and stale["code"] == "stale_target"
    forged = _request(entry, "resource.get", {"resource_id": task.id, "actor_id": None})
    assert (await _send(rig, forged, status=422))["detail"] == "invalid_request"
    assert rig.wire.frames.count(("remote→studio", "forward", None)) == forwarded
    assert not rig.dispatches
    assert (await _rows(rig.executor, *models), await _rows(rig.requester, *models), rig.documents()) == before


@pytest.mark.parametrize("kind", ["task", "chat"])
async def test_explicit_draft_then_dispatch_once_on_pinned_executor(rig, kind):
    await rig.register()
    member, *_ = await rig.executor.in_home(_seed_local_rows, rig.executor, seed_rows=False)
    target = await _publish(rig)
    entry = await _grant(rig, target, member, [
        "view_status", "view_transcript", f"create_{kind}", f"run_{kind}",
    ])
    models = (TeamTask, AgentChat, AgentChatMessage, AiCommandLedger)
    untouched = await _rows(rig.requester, *models), rig.documents()
    draft = {"team_id": member.team_id, "title": f"Remote fixture {kind}", "locale": "en"}
    if kind == "task":
        draft.update(assignee_id=member.id, brief=BRIEF, write_paths=[])
    else:
        draft.update(member_id=member.id)
    create = _request(entry, f"{kind}.create", draft, mutation=True)
    rid = _result(await _send(rig, create))["resource_id"]
    model = TeamTask if kind == "task" else AgentChat
    ctx = rig.executor.context
    async with ctx.db.session() as session:
        row = await session.get(model, rid)
        assert row is not None
        if kind == "task":
            assert row.status == "todo" and row.auto_start is False
    assert ctx.jobs.get(rid) is None and not rig.dispatches
    if kind == "task":
        assert not await rig.executor.in_home(ctx.teams._release_tasks, member.team_id, automatic=True)
    precondition = _result(await _send(rig, _request(entry, "resource.get", {"resource_id": rid})))
    payload = {"resource_id": rid}
    if kind == "chat":
        payload["text"] = CHAT_TEXT
    launch = _request(entry, "task.start" if kind == "task" else "chat.send", payload,
                      mutation=True, expected_resource_revision=precondition["revision"])
    accepted = await _send(rig, launch)
    assert accepted["state"] == "accepted" and accepted["command_id"] == launch["command_id"], accepted
    async with asyncio.timeout(5):
        await rig.running.wait()
    job = ctx.jobs.get(rid)
    assert job is not None
    try:
        # The browser API refuses another POST of a staged intent; simulate a
        # repeated wire delivery on its authenticated control connection instead.
        assert (await _send(rig, launch, status=409))["detail"] == "recover_explicitly"

        async def wire_delivery(request):
            control = await rig.requester.app.state.ai_access._connection(rig.requester.id)
            return await control.exchange(protocol.FrameType.REQUEST,
                                          service._wire_request(protocol.RequestBody.from_wire(request)))

        frame_type, duplicate = await rig.requester.in_home(wire_delivery, launch)
        assert frame_type == "response" and duplicate["state"] in {"accepted", "dispatching", "running"}, duplicate
        assert duplicate["result"]["run_id"] == accepted["result"]["run_id"]
        if kind == "task":
            _, conflict = await rig.requester.in_home(wire_delivery, {
                **launch, "expected_resource_revision": precondition["revision"] + 1,
            })
            assert conflict["state"] == "rejected" and conflict["code"] == "idempotency_conflict"
        else:
            before_busy = await _rows(rig.executor, AgentChatMessage)
            busy = await _send(rig, _request(entry, "chat.send", {"resource_id": rid, "text": "Not queued"},
                                            mutation=True, expected_resource_revision=precondition["revision"] + 1))
            assert busy["state"] == "rejected" and busy["code"] == "busy"
            assert await _rows(rig.executor, AgentChatMessage) == before_busy
            inbox = ctx.chats._inboxes[rid]
            assert len(inbox) == inbox.unacknowledged == 0
        assert rig.dispatches == [rig.executor.home]
    finally:
        rig.release.set()
        async with asyncio.timeout(5):
            await job.task
    receipt = _result(await _send(rig, _request(entry, "command.get", {"command_id": launch["command_id"]})))
    assert receipt["state"] == "completed" and receipt["run_id"] == accepted["result"]["run_id"]
    transcript = _result(await _send(rig, _request(entry, "transcript.page", {"resource_id": rid})))
    assert transcript["items"] and all(item["resource_id"] == rid for item in transcript["items"])
    if kind == "chat":
        assert sum(item["text"] == CHAT_TEXT for item in transcript["items"]) == 1
    assert rig.dispatches == [rig.executor.home] and (await _rows(rig.requester, *models), rig.documents()) == untouched
    remote_control = "\n".join(_db_dump(rig.remote_dir / "ai-control.db"))
    assert BRIEF not in remote_control and CHAT_TEXT not in remote_control


async def test_requester_reads_prepublication_history_then_cancels_fake_task(rig):
    owner, peer = rig.executor, rig.requester
    member, task, chat = await owner.in_home(_seed_local_rows, owner)
    documents = rig.documents()
    task_text = "Local task note written before AI publication"
    chat_text = "Local chat message written before AI publication"
    note = _ok(await owner.browser("POST", f"/api/teams/{member.team_id}/tasks/{task.id}/notes",
                                   json={"text": task_text}), 201)
    message = _ok(await owner.browser("POST", f"/api/chats/{chat.id}/messages", json={"text": chat_text}), 202)["message"]
    try:
        async with asyncio.timeout(5):
            await rig.running.wait()
        historical_turn = owner.context.jobs.get(chat.id)
        assert historical_turn is not None
    finally:
        rig.release.set()
    async with asyncio.timeout(5):
        await historical_turn.task
    assert rig.dispatches == [owner.home]
    assert not rig.wire.http and not rig.wire.socket_paths  # Both texts predate even Register.
    rig.running.clear()
    rig.release.clear()

    await rig.register()
    target = await _publish(rig)
    operations = ["view_status", "view_transcript", "run_task", "cancel_task", "operate_existing"]
    entry = await _grant(rig, target, member, operations)
    assert entry["grant"]["resource_scope"] == "all_resources_in_selected_teams"
    assert set(entry["grant"]["operations"]) == set(operations)
    owner_db, peer_db = owner.context.db.path, peer.context.db.path
    before_owner, before_peer = _db_dump(owner_db), _db_dump(peer_db)
    listing = _result(await _send(rig, _request(entry, "resource.list", {})))
    assert {item["resource_id"] for item in listing["items"]} == {task.id, chat.id}
    assert all(text not in json.dumps(listing) for text in (task_text, chat_text))
    for resource, stored, kind, text in ((task, note, "note", task_text), (chat, message, "text", chat_text)):
        page = _result(await _send(rig, _request(entry, "transcript.page", {"resource_id": resource.id})))
        assert page["next_cursor"] is None
        assert all(item["resource_id"] == resource.id for item in page["items"])
        projected, = [item for item in page["items"] if item["message_id"] == stored["id"]]
        assert (projected["origin"], projected["role"], projected["kind"], projected["text"]) == (
            "local", "user", kind, text,
        )
        assert not projected["content_omitted"] and "data" not in projected
    assert (_db_dump(owner_db), _db_dump(peer_db)) == (before_owner, before_peer)
    assert rig.dispatches == [owner.home]  # Reading history did not dispatch.

    payload = {"resource_id": task.id}
    read = _request(entry, "resource.get", payload)
    draft = _result(await _send(rig, read))
    assert draft["status"] == "todo"
    start = _request(entry, "task.start", payload, mutation=True, expected_resource_revision=draft["revision"])
    accepted = await _send(rig, start)
    assert (accepted["state"], accepted["command_id"]) == ("accepted", start["command_id"])
    run_id = accepted["result"]["run_id"]
    job = None
    try:
        async with asyncio.timeout(5):
            await rig.running.wait()
        job = owner.context.jobs.get(task.id)
        assert job is not None and not job.task.done() and not rig.release.is_set()
        running = _result(await _send(rig, read))
        assert (running["status"], running["is_running"], running["run_id"]) == ("in_progress", True, run_id)
        cancel = _request(entry, "task.cancel", payload, mutation=True, expected_run_id=run_id)
        cancelled = _result(await _send(rig, cancel))
        assert (cancelled["command_id"], cancelled["resource_id"], cancelled["run_id"], cancelled["state"]) == (
            cancel["command_id"], task.id, run_id, "completed",
        )
        # With the fake still blocked, only cancellation can stop this job.
        async with asyncio.timeout(5):
            await asyncio.wait({job.task})
        assert job.task.cancelled()
    finally:
        rig.release.set()
        if job is not None and not job.task.done():
            async with asyncio.timeout(5):
                await asyncio.wait({job.task})

    status = _result(await _send(rig, read))
    assert (status["status"], status["is_running"], status["run_id"]) == ("pending", False, None)
    receipt = _result(await _send(rig, _request(entry, "command.get", {"command_id": start["command_id"]})))
    assert (receipt["state"], receipt["resource_id"], receipt["run_id"]) == ("cancelled", task.id, run_id)
    async with owner.context.db.session() as session:
        saved = await session.get(TeamTask, task.id)
        assert saved is not None and saved.status == "pending"
        ledger = (await session.exec(select(AiCommandLedger))).all()
        assert {(row.op, row.state, row.resource_id, row.run_id) for row in ledger} == {
            ("task.start", "cancelled", task.id, run_id), ("task.cancel", "completed", task.id, run_id),
        }
    assert rig.dispatches == [owner.home, owner.home]  # One historical chat turn, one ACP task run.
    assert (_db_dump(peer_db), rig.documents()) == (before_peer, documents)
    control = "\n".join(_db_dump(rig.remote_dir / "ai-control.db"))
    assert all(text not in control for text in (task_text, chat_text))


async def test_lost_ack_recovers_receipt_without_resuming_or_resending(rig):
    await rig.register()
    executor, requester = rig.executor, rig.requester
    member, *_ = await executor.in_home(_seed_local_rows, executor, seed_rows=False)
    target = await _publish(rig)
    entry = await _grant(rig, target, member, ["view_status", "create_chat", "run_chat"])
    before = rig.documents()
    created = _result(await _send(rig, _request(entry, "chat.create", {
        "team_id": member.team_id, "member_id": member.id, "title": "Fixture lost ACK", "locale": "en",
    }, mutation=True)))
    rid = created["resource_id"]
    launch = _request(entry, "chat.send", {"resource_id": rid, "text": CHAT_TEXT},
                      mutation=True, expected_resource_revision=1)
    command_id = launch["command_id"]
    rig.wire.drop_receipt = command_id
    assert (await _send(rig, launch, status=503))["detail"] == "outcome_unknown"
    assert rig.wire.dropped_receipt
    async with asyncio.timeout(5):
        await rig.running.wait()
    job = executor.context.jobs.get(rid)
    assert job is not None
    rig.release.set()
    async with asyncio.timeout(5):
        await job.task
    recovery = _result(_ok(await requester.call("POST", f"/receipts/{command_id}/recover", json={
        "executor_epoch": entry["executor_epoch"], "resend": False,
    })))
    assert recovery["command_id"] == command_id and recovery["resource_id"] == rid
    assert recovery["state"] == "completed" and recovery["run_id"]
    async with executor.context.db.session() as session:
        messages = (await session.exec(select(AgentChatMessage).where(AgentChatMessage.chat_id == rid))).all()
        assert sum(message.text == CHAT_TEXT for message in messages) == 1
    assert rig.dispatches == [executor.home] and rig.documents() == before


async def test_valid_task_note_schema_is_accepted_on_both_sides(rig):
    await rig.register()
    member, task, _ = await rig.executor.in_home(_seed_local_rows, rig.executor)
    target = await _publish(rig)
    entry = await _grant(rig, target, member, ["view_status", "write_task", "operate_existing"])
    note = _request(entry, "task.note", {"resource_id": task.id, "text": "Fixture task note"}, mutation=True)
    assert protocol.RequestBody.from_wire(note).parsed_payload().resource_id == task.id
    reply = _result(await _send(rig, note))
    assert reply["resource_id"] == task.id
    assert not rig.dispatches


async def test_revoke_and_offline_keep_authority_bound_to_executor(rig):
    await rig.register()
    member, task, _ = await rig.executor.in_home(_seed_local_rows, rig.executor)
    target = await _publish(rig)
    entry = await _grant(rig, target, member, ["view_status", "run_task", "operate_existing"])
    payload = {"resource_id": task.id}
    snapshot = _result(await _send(rig, _request(entry, "resource.get", payload)))
    models = (TeamTask, AiCommandLedger)
    before = await _rows(rig.executor, *models), await _rows(rig.requester, *models), rig.documents()

    # Turn off the executor, not the grant. The target remains pinned and offline.
    executor = rig.executor
    pub = _ok(await executor.call("GET", "/registrations"))["registrations"][0]
    disabled = _ok(await executor.call("PUT", f"/registrations/{executor.id}/publication", json={
        "enabled": False, "expected_revision": pub["revision"],
    }))
    assert not disabled["publication_enabled"]
    entries = _ok(await rig.requester.call("GET", f"/registrations/{rig.requester.id}/targets"))["targets"]
    assert len(entries) == 1 and entries[0]["target"] == target and entries[0]["state"] == "offline"
    stale_start = _request(entry, "task.start", payload, mutation=True, expected_resource_revision=snapshot["revision"])
    wire_start = len(rig.wire.frames)
    unavailable = await _send(rig, stale_start)
    assert (unavailable["state"], unavailable["code"], unavailable["target"], unavailable["command_id"]) == (
        "rejected", "offline", target, stale_start["command_id"],
    )
    hidden = await _send(rig, {**stale_start, "grant_id": str(uuid4()), "command_id": str(uuid4())})
    assert (hidden["state"], hidden["code"]) == ("rejected", "not_available")
    assert not rig.remote.state.ai_relay.pending

    # Reconnect explicitly. No offline backlog, and the previous epoch is stale.
    _ok(await executor.call("PUT", f"/registrations/{executor.id}/publication", json={
        "enabled": True, "expected_revision": disabled["revision"],
    }))
    current, = _ok(await rig.requester.call("GET", f"/registrations/{rig.requester.id}/targets"))["targets"]
    assert current["executor_epoch"] != entry["executor_epoch"]
    assert [kind for _, kind, _ in rig.wire.frames[wire_start:] if kind in {"request", "forward"}] == [
        "request", "request",
    ]  # No Forward or replay on either rejection or reconnect.
    stale_start = {**stale_start, "command_id": str(uuid4())}
    rejected = await _send(rig, stale_start)
    assert rejected["state"] == "rejected" and rejected["code"] == "stale_target"
    grant = current["grant"]
    revoked = _ok(await executor.call("DELETE", f"/grants/{grant['grant_id']}", json={
        "expected_revision": grant["revision"],
    }))
    assert revoked["acked_revision"] == grant["revision"] + 1
    assert _ok(await rig.requester.call("GET", f"/registrations/{rig.requester.id}/targets"))["targets"] == []
    denied = await _send(rig, _request(current, "resource.get", payload))
    assert denied["state"] == "rejected" and denied["code"] == "denied"
    assert [kind for _, kind, _ in rig.wire.frames[wire_start:] if kind in {"request", "forward"}] == [
        "request", "request", "request", "forward", "request",
    ]  # Only the explicit stale-epoch request reached the executor; it had no effect.
    assert not rig.remote.state.ai_relay.pending and not rig.dispatches
    assert (await _rows(executor, *models), await _rows(rig.requester, *models), rig.documents()) == before


async def test_revoke_does_not_cancel_an_already_running_job(rig):
    await rig.register()
    executor, requester = rig.executor, rig.requester
    member, task, _ = await executor.in_home(_seed_local_rows, executor)
    target = await _publish(rig)
    entry = await _grant(rig, target, member, ["view_status", "run_task", "operate_existing"])
    untouched = await _rows(requester, TeamTask, AiCommandLedger), rig.documents()
    payload = {"resource_id": task.id}
    status = _result(await _send(rig, _request(entry, "resource.get", payload)))
    accepted = await _send(rig, _request(entry, "task.start", payload, mutation=True,
                                         expected_resource_revision=status["revision"]))
    assert accepted["state"] == "accepted"
    async with asyncio.timeout(5):
        await rig.running.wait()
    job = executor.context.jobs.get(task.id)
    assert job is not None
    try:
        grant = entry["grant"]
        revoked = _ok(await executor.call("DELETE", f"/grants/{grant['grant_id']}", json={
            "expected_revision": grant["revision"],
        }))
        assert revoked["acked_revision"] == grant["revision"] + 1
        denied = await _send(rig, _request(entry, "resource.get", payload))
        assert (denied["state"], denied["code"]) == ("rejected", "denied")
        assert executor.context.jobs.get(task.id) is job and not job.task.done()
        assert rig.dispatches == [executor.home]
    finally:
        rig.release.set()
        async with asyncio.timeout(5):
            await job.task
    assert (await _rows(requester, TeamTask, AiCommandLedger), rig.documents()) == untouched


async def test_status_stream_replays_snapshot_delta_then_closes_on_revoke(rig):
    await rig.register()
    executor, requester = rig.executor, rig.requester
    member, task, _ = await executor.in_home(_seed_local_rows, executor)
    target = await _publish(rig)
    entry = await _grant(rig, target, member, ["view_status"])
    before = rig.documents()
    assert (await requester.call("GET", "/events", authenticated=False)).status_code == 401
    payload = {"resource_id": task.id}
    snapshot = _result(await _send(rig, _request(entry, "resource.get", payload)))
    cursor = {"stream_epoch": snapshot["stream_epoch"], "after_seq": snapshot["seq"]}
    ahead = await _send(rig, _request(entry, "stream.subscribe", {
        **payload, **cursor, "after_seq": snapshot["seq"] + 10, "topic": "status",
    }))
    assert ahead["state"] == "rejected" and ahead["code"] == "resync_required"

    async with requester.events() as next_event:
        await executor.in_home(executor.context.teams.update_task, task.id, {"title": "Edited after snapshot"})
        sub = _result(await _send(rig, _request(entry, "stream.subscribe", {**payload, **cursor, "topic": "status"})))
        assert (sub["stream_epoch"], sub["seq"]) == (snapshot["stream_epoch"], snapshot["seq"])
        event, delta = await next_event()
        assert event == "remote" and delta["target"] == target and delta["resource_id"] == task.id
        assert delta["subscription_id"] == sub["subscription_id"]
        assert (delta["stream_epoch"], delta["seq"]) == (snapshot["stream_epoch"], snapshot["seq"] + 1)
        assert delta["payload"]["title"] == "Edited after snapshot" and BRIEF not in json.dumps(delta)
        assert delta["resource_revision"] == delta["payload"]["revision"] == snapshot["revision"] + 1
        grant = entry["grant"]
        _ok(await executor.call("DELETE", f"/grants/{grant['grant_id']}", json={"expected_revision": grant["revision"]}))
        assert await next_event() == ("resync_required", {"subscription_id": sub["subscription_id"]})
    assert not rig.remote.state.ai_relay.subscriptions and not rig.dispatches and rig.documents() == before
