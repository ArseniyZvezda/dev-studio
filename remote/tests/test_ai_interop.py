"""X-A1: native Studio discovery and strict Documents APIs on the same HTTP wire.

Install both editable packages (including backend[dev]); a missing peer is a
collection error, not a skip. HTTP is carried in-process on a single event loop:
real clients, routing, serialization, authentication, and stores, without a live
listener, AI provider, or production network. Legacy/fault peers are isolated
at the wire boundary; they never replace a Studio parser or decision.
"""
from __future__ import annotations

import json
import sqlite3
import ssl
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Router

from voxarium_studio.ai_access.service import AiAccessService
from voxarium_studio.ai_access.transport import AiTransport
from voxarium_studio.api.ai_access import router as ai_router
from voxarium_studio.core.db import Database
from voxarium_studio.core.paths import HOME_ENV_VAR
from voxarium_studio.core.project import init_project
from voxarium_studio.core.security import AuthSettings, require_auth
from voxarium_studio.sync import catalog_runtime, credentials
from voxarium_studio.sync.catalog import CatalogError, CatalogUser
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.profiles import RemoteProfiles
from voxarium_studio.sync.protocol import PROTOCOL_VERSION as STUDIO_DOCUMENTS
from voxarium_studio_remote import __version__ as remote_release
from voxarium_studio_remote.app import create_app
from voxarium_studio_remote.auth import build_authenticator, credentials_from_headers, hash_token, new_token
from voxarium_studio_remote.protocol import PROTOCOL_VERSION as REMOTE_DOCUMENTS

pytestmark = pytest.mark.asyncio

PREFIX = "/tenant/remote"
BASE = "https://remote.example.test" + PREFIX
CAPS = "/api/ai/v1/capabilities"
BROWSER_KEY = "local-test-browser-token"
DOC_CAPABILITIES = [
    "project_catalog", "explicit_project_create", "strict_connect", "project_snapshot", "legacy_resolution",
]
PROJECT_FIELDS = {"id", "display_name", "storage_epoch", "metadata_revision", "created_at"}
LEGACY_HEALTH = {"status": "ok", "service": "voxarium_studio_remote", "version": "0.1.0", "protocol": 1}


def _unique_object(pairs):
    assert len(pairs) == len({name for name, _ in pairs}), "duplicate key on the wire"
    return dict(pairs)


@dataclass
class Exchange:
    request: httpx.Request = field(repr=False)
    status: int | None = None
    headers: httpx.Headers = field(default_factory=httpx.Headers)
    raw: bytes = field(default=b"", repr=False)

    def json(self):
        return json.loads(self.raw, object_pairs_hook=_unique_object)


class Wire(httpx.AsyncBaseTransport):
    """Capture HTTP bytes without consuming/re-encoding the client response stream."""

    def __init__(self, app, *, fault=None):
        self.calls: list[Exchange] = []
        self.fault = fault
        self.transport = httpx.ASGITransport(app=Router(routes=[Mount(PREFIX, app=app)]))

    async def handle_async_request(self, request):
        call = Exchange(request)
        self.calls.append(call)
        # Each native operation below uses one request at a time. The ASGI
        # stream is memory-backed, so wrapping it for observation is lossless.
        if self.fault and request.url.path == PREFIX + CAPS:
            answer = self.fault(request)
        else:
            answer = await self.transport.handle_async_request(request)
        call.status, call.headers = answer.status_code, answer.headers
        original = answer.stream

        class Observe(httpx.AsyncByteStream):
            async def __aiter__(self):
                async for chunk in original:
                    call.raw += chunk
                    yield chunk

            async def aclose(self):
                await original.aclose()

        answer.stream = Observe()
        return answer


def _legacy_documents(remote, *, acp):
    """The original Remote 0.1.0 HTTP envelopes (e86b726), not v2 with a URL alias.

    ACP-on is deliberately a mixed-version fixture: the old Documents HTTP
    surface plus the real, independently mounted current-Remote AI namespace.
    No current-Remote Documents response is rewritten to obtain a pass.
    """
    old = FastAPI()
    auth = build_authenticator(remote.state.store)

    @old.get("/healthz")
    async def health():
        return LEGACY_HEALTH

    @old.get("/api/v1/me")
    async def me(request: Request):
        user = auth.authenticate(credentials_from_headers(request.headers))
        if user is None:
            return JSONResponse({"detail": "Missing or invalid key."}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        return {"protocol": 1, "user": {"id": user.id, "name": user.display_name, "color": user.color}}

    async def app(scope, receive, send):
        path = scope["path"].removeprefix(scope["root_path"])
        await (remote if acp and path.startswith("/api/ai/") else old)(scope, receive, send)

    return app


def _tree(root):
    # Include file creation/deletion and writes with identical contents.
    return {file.relative_to(root): (file.stat().st_mtime_ns, sha256(file.read_bytes()).hexdigest())
            for file in root.rglob("*") if file.is_file()}


def _db(path):
    # A logical snapshot includes SQLite WAL state, not just the main file bytes.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return tuple(db.iterdump())


@pytest.fixture
def pair(tmp_path, monkeypatch):
    @asynccontextmanager
    async def make(*, acp=True, legacy=False, fault=None, ai_key=None, documents_extra=None):
        home, project, data = (tmp_path / name for name in ("home", "project", "remote"))
        for path in (home, project, data):
            path.mkdir()
        monkeypatch.setenv(HOME_ENV_VAR, str(home))
        monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_URL", raising=False)
        monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_TOKEN", raising=False)
        paths = init_project(project, with_samples=False)
        markdown = paths.docs / "keep.md"
        markdown.write_text("# Local Markdown\n", encoding="utf-8")
        db = Database(paths.db_file)
        await db.create_all()  # Real task/chat/provenance/command-ledger schema.
        await db.close()

        remote = create_app(data, ai_control=acp)
        store = remote.state.store
        key = new_token()
        user = store.create_user("Compatibility user", hash_token(key))
        seed = store.create_project("existing", "Existing project")
        doc = store.put(seed.id, "keep.md", "# Remote Markdown\n", user.id)
        peer = _legacy_documents(remote, acp=acp) if legacy else remote
        if documents_extra:
            # Deliberately incompatible peer, used only to prove strictness was
            # not relaxed in the Studio validators when AI discovery was added.
            real_peer = peer

            async def peer(scope, receive, send):
                path = scope["path"].removeprefix(scope["root_path"])
                if path != documents_extra:
                    await real_peer(scope, receive, send)
                    return
                body = LEGACY_HEALTH if legacy else _v2_health()
                if path.endswith("/me"):
                    body = _me(user, 1 if legacy else 2)
                await JSONResponse({**body, "ai_control": {"versions": [1]}})(scope, receive, send)

        ai_wire = Wire(peer, fault=fault)
        doc_wire = Wire(peer)
        profile = RemoteProfiles().create("Documents and AI", BASE + "/")
        assert profile.url == BASE
        credentials.save_profile_token(profile.profile_id, profile.url, key, expected_revision=0)
        ai_profile = profile
        if ai_key is not None:
            # A real invalid personal key on the same peer, without replacing
            # the credential used by the verified Documents profile.
            ai_profile = RemoteProfiles().create("Invalid AI key", BASE + "/invalid-key")
            credentials.save_profile_token(ai_profile.profile_id, ai_profile.url, ai_key, expected_revision=0)
            ai_wire.transport = httpx.ASGITransport(app=Router(routes=[Mount(PREFIX + "/invalid-key", app=peer)]))

        def unexpected_socket(*_args, **_kwargs):
            pytest.fail("read-only capability discovery attempted a control socket")

        service = AiAccessService(SimpleNamespace(project_roots={}),
                                  transport=AiTransport(http_transport=ai_wire, ws_connect=unexpected_socket))
        dispatches = []

        async def forbidden_dispatch(*args, **_kwargs):
            dispatches.append(args)
            pytest.fail("read-only discovery attempted to execute AI")

        monkeypatch.setattr(service.executor, "handle", forbidden_dispatch)
        studio = FastAPI()
        studio.state.ai_access = service
        studio.state.auth = AuthSettings(token=BROWSER_KEY)
        studio.include_router(ai_router, prefix="/api", dependencies=[Depends(require_auth)])
        # Inject only HTTP delivery, not the real Documents Verify/catalog/Create
        # logic or its real persisted profile/credential/operation providers.
        monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport", lambda: CatalogHttpTransport(transport=doc_wire))
        runtime = catalog_runtime.create_remote_catalog_runtime()
        async with remote.router.lifespan_context(remote), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=studio), base_url="http://127.0.0.1", trust_env=False,
        ) as browser:
            try:
                ready = await runtime.start()
                yield SimpleNamespace(
                    browser=browser, ai_wire=ai_wire, doc_wire=doc_wire, service=service,
                    ready=ready, remote=remote, user=user, key=key, ai_key=ai_key or key, profile=profile,
                    ai_profile=ai_profile, seed=seed, doc=doc, dispatches=dispatches, paths=paths,
                    snapshot=lambda: (_tree(home), _tree(project), _db(data / "remote.db"),
                                      _db(data / "ai-control.db") if (data / "ai-control.db").exists() else None,
                                      tuple(sorted(file.name for file in data.iterdir()))),
                )
            finally:
                await service.close()
                await runtime.close()

    return make


def _v2_health():
    return {"status": "ok", "service": "voxarium_studio_remote", "version": remote_release,
            "protocol": 2, "supported_protocols": [2], "catalog_api": 2}


def _me(user, version):
    if version == 1:
        return {"protocol": 1, "user": {"id": user.id, "name": user.display_name, "color": user.color}}
    return {"protocol": 2, "catalog_api": 2, "capabilities": DOC_CAPABILITIES,
            "user": {"id": user.id, "display_name": user.display_name, "color": user.color}}


async def _verify(pair, version):
    start = len(pair.doc_wire.calls)
    result = await pair.ready.verification.verify(pair.profile.profile_id, expected_revision=1)
    assert (result.state, result.wire_protocol, result.catalog_version, result.error_code, result.user) == (
        "valid", version, 2 if version == 2 else None, None if version == 2 else "catalog_unavailable",
        CatalogUser(pair.user.id, pair.user.display_name, pair.user.color),
    )
    health, me = pair.doc_wire.calls[start:]
    assert [(call.request.method, str(call.request.url), call.status) for call in (health, me)] == [
        ("GET", BASE + "/healthz", 200), ("GET", BASE + f"/api/v{version}/me", 200),
    ]
    assert health.request.headers.get_list("authorization") == []
    assert me.request.headers.get_list("authorization") == [f"Bearer {pair.key}"]
    assert health.json() == (LEGACY_HEALTH if version == 1 else _v2_health())
    assert me.json() == _me(pair.user, version)
    assert type(health.json()["protocol"]) is type(me.json()["protocol"]) is int


async def _discover(pair, kind, code, *, server=None, status=None, no_store=False):
    before = pair.snapshot()
    start = len(pair.ai_wire.calls)
    docs_start = len(pair.doc_wire.calls)
    # Repeat to catch persistence/caching and an accidental auto-register after a positive probe.
    answers = [await pair.browser.get(f"/api/ai-access/profiles/{pair.ai_profile.profile_id}/capabilities",
                                      headers={"Authorization": f"Bearer {BROWSER_KEY}"}) for _ in range(2)]
    calls = pair.ai_wire.calls[start:]
    assert [(call.request.method, str(call.request.url), call.status) for call in calls] == [
        ("GET", pair.ai_profile.url + CAPS, status), ("GET", pair.ai_profile.url + CAPS, status),
    ]
    for call in calls:
        headers = call.request.headers
        assert headers.get_list("authorization") == [f"Bearer {pair.ai_key}"]
        assert headers.get_list("cache-control") == ["no-store"]
        assert headers.get_list("accept") == ["application/json"]
        assert not ({"x-vxs-client-id", "x-vxs-device-token", "cookie"} & headers.keys())
        assert not await call.request.aread()
        if no_store:
            assert call.headers.get_list("cache-control") == ["no-store"]
        if kind == "supported":
            body = call.json()
            assert body == {"acp": 1, "server_instance_id": server, "ai_control": {"versions": [1]}}
            assert type(body["acp"]) is type(body["ai_control"]["versions"][0]) is int
            assert str(UUID(server)) == server
    assert len(pair.doc_wire.calls) == docs_start
    assert not pair.service.connections and not pair.dispatches
    assert pair.snapshot() == before
    for answer in answers:
        assert answer.status_code == 200
        assert answer.headers["cache-control"] == "no-store"
        assert pair.ai_key not in answer.text
        assert answer.json() == {"kind": kind, "code": code, "server_instance_id": server}


async def _read_catalog(pair, ids):
    catalog = pair.ready.catalog
    profile_id = pair.profile.profile_id
    page = await catalog.list_projects(profile_id, expected_revision=1)
    assert [entry.id for entry in page.projects] == ids
    assert page.next_after is None
    assert await catalog.get_project(profile_id, "existing", expected_revision=1) == page.projects[-1]
    assert pair.remote.state.store.doc("existing", "keep.md") == pair.doc
    assert pair.remote.state.store.seq("existing") == pair.doc.rev
    calls = [call for call in pair.doc_wire.calls if call.request.url.path.startswith(PREFIX + "/api/v2/projects")]
    assert calls
    for call in calls:
        assert call.request.headers.get_list("authorization") == [f"Bearer {pair.key}"]
        assert call.headers.get_list("cache-control") == ["no-store"]
        body = call.json()
        assert type(body["protocol"]) is int and body["protocol"] == 2
        if "projects" in body:
            assert body.keys() == {"protocol", "projects", "next_after"}
            assert all(row.keys() == PROJECT_FIELDS for row in body["projects"])
        else:
            assert body.keys() == {"protocol", "project"}
            assert body["project"].keys() == PROJECT_FIELDS


async def _explicit_create(peers):
    assert all(call.request.method == "GET" for call in peers.doc_wire.calls)
    # Only the explicit fixture action may create a space. No selection,
    # sync or implicit Create as a side effect of capability discovery.
    created = await peers.ready.create.create_project(peers.profile.profile_id, expected_revision=1,
                                                      id="created", display_name="Created project",
                                                      operation_id="interop-create")
    assert created.state == "created" and created.project is not None, created
    assert created.project.id == "created" and created.project.display_name == "Created project"
    posts = [call for call in peers.doc_wire.calls if call.request.method != "GET"]
    post, = posts
    assert (post.request.method, str(post.request.url), post.status) == ("POST", BASE + "/api/v2/projects", 201)
    assert json.loads(await post.request.aread()) == {"id": "created", "display_name": "Created project"}
    assert post.headers["location"] == "/api/v2/projects/created"
    assert peers.remote.state.store.project("created").storage_epoch == created.project.storage_epoch
    assert peers.remote.state.store.seq("created") == 0
    await _read_catalog(peers, ["created", "existing"])
    assert await peers.ready.catalog.get_project(peers.profile.profile_id, "created", expected_revision=1) == created.project


@pytest.mark.parametrize("acp", [True, False], ids=["acp-on", "acp-off"])
async def test_v2_documents_verify_catalog_create_with_acp_on_and_off(pair, acp):
    assert REMOTE_DOCUMENTS == 2 and STUDIO_DOCUMENTS == 1  # Existing protocols, not ACP/1.
    async with pair(acp=acp) as peers:
        await _verify(peers, 2)
        await _read_catalog(peers, ["existing"])
        # This is the current v2 server's v1 URL, not the historical v1 peer.
        async with httpx.AsyncClient(transport=peers.doc_wire, trust_env=False) as client:
            alias = await client.get(BASE + "/api/v1/me", headers={"Authorization": f"Bearer {peers.key}"})
        assert alias.status_code == 200
        assert peers.doc_wire.calls[-1].json() == {**_me(peers.user, 1), "protocol": 2}
        if acp:
            await _discover(peers, "supported", None, status=200, no_store=True,
                            server=peers.remote.state.ai_store.server_instance_id)
        else:
            assert not hasattr(peers.remote.state, "ai_store")
            await _discover(peers, "unsupported", "not_mounted", status=404, no_store=True)
        await _verify(peers, 2)
        await _read_catalog(peers, ["existing"])
        await _explicit_create(peers)


@pytest.mark.parametrize("acp", [True, False], ids=["with-mounted-acp", "old-peer"])
async def test_strict_v1_verify_has_no_catalog_or_discovery_fallback(pair, acp):
    async with pair(acp=acp, legacy=True) as peers:
        await _verify(peers, 1)
        if acp:
            await _discover(peers, "supported", None, status=200, no_store=True,
                            server=peers.remote.state.ai_store.server_instance_id)
        else:
            await _discover(peers, "unsupported", "not_mounted", status=404)
        await _verify(peers, 1)


async def test_live_remote_401_is_not_an_old_peer_or_a_documents_failure(pair):
    async with pair(ai_key=new_token()) as peers:
        await _verify(peers, 2)
        await _read_catalog(peers, ["existing"])
        await _discover(peers, "auth_failure", "auth_failure", status=401, no_store=True)
        await _verify(peers, 2)
        await _read_catalog(peers, ["existing"])
        await _explicit_create(peers)


CAPABILITY_ID = "84f19a2b-2a21-44d8-b0ce-940e632bd2e0"


def _envelope(acp=1, versions=None, **fields):
    return {"acp": acp, "server_instance_id": CAPABILITY_ID, "ai_control": {
        "versions": [1] if versions is None else versions,
    }, **fields}


@pytest.mark.parametrize(("payload", "status", "kind", "code"), [
    pytest.param(_envelope(2, [2]), 200, "unsupported", "incompatible_version", id="future-acp"),
    pytest.param(_envelope(1, [2, 3]), 200, "unsupported", "incompatible_version", id="no-compatible-version"),
    pytest.param(b"{broken", 200, "error", "invalid_response", id="bad-json"),
    pytest.param(_envelope(server_instance_id="not-a-uuid"), 200, "error", "invalid_response", id="bad-uuid"),
    pytest.param(_envelope(extra=True), 200, "error", "invalid_response", id="extra-top-level"),
    pytest.param(_envelope(ai_control={"versions": [1], "extra": 1}), 200, "error", "invalid_response", id="extra-nested"),
    pytest.param(b'{"acp":1,"acp":1,"server_instance_id":"84f19a2b-2a21-44d8-b0ce-940e632bd2e0","ai_control":{"versions":[1]}}',
                 200, "error", "invalid_response", id="duplicate-key"),
    pytest.param(_envelope(True), 200, "error", "invalid_response", id="boolean-acp"),
    pytest.param(_envelope(1, [True]), 200, "error", "invalid_response", id="boolean-version"),
    pytest.param(_envelope(1, [1, 1]), 200, "error", "invalid_response", id="duplicate-version"),
    pytest.param(_envelope(1, [1, 2]), 200, "error", "invalid_response", id="non-exact-supported-version"),
    pytest.param(_envelope(2, [2], server_instance_id="not-a-uuid"), 200, "error", "invalid_response", id="bad-future-uuid"),
    pytest.param(_envelope(2, [2], extra=True), 200, "error", "invalid_response", id="extra-future-field"),
    pytest.param("https://other.example.test/elsewhere", 307, "error", "redirect_refused", id="cross-origin-redirect"),
    pytest.param("https://remote.example.test/another-tenant/api/ai/v1/capabilities", 308,
                 "error", "redirect_refused", id="cross-prefix-redirect"),
    pytest.param({"error": "temporarily down"}, 503, "error", "remote_error", id="other-http"),
])
async def test_negative_discovery_responses_leave_verified_documents_alone(pair, payload, status, kind, code):
    def reply(_request):
        if 300 <= status < 400:
            return httpx.Response(status, headers={"Location": payload})
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload, headers={"Content-Type": "application/json"})
        return httpx.Response(status, json=payload)

    async with pair(fault=reply) as peers:
        await _verify(peers, 2)
        await _read_catalog(peers, ["existing"])
        await _discover(peers, kind, code, status=status)
        await _verify(peers, 2)
        await _read_catalog(peers, ["existing"])
        await _explicit_create(peers)


@pytest.mark.parametrize("code", ["timeout", "tls", "network"])
async def test_discovery_transport_failures_are_typed_and_read_only(pair, code):
    def fail(request):
        if code == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if code == "tls":
            raise httpx.ConnectError("test TLS validation failure", request=request) from ssl.SSLCertVerificationError()
        raise httpx.ConnectError("test disconnected transport", request=request)

    async with pair(fault=fail) as peers:
        await _verify(peers, 2)
        await _read_catalog(peers, ["existing"])
        await _discover(peers, "unavailable", code)
        await _verify(peers, 2)
        await _read_catalog(peers, ["existing"])
        await _explicit_create(peers)


@pytest.mark.parametrize(("version", "path"), [(1, "/healthz"), (1, "/api/v1/me"),
                                               (2, "/healthz"), (2, "/api/v2/me")])
async def test_documents_verify_still_rejects_ai_fields_in_health_and_me(pair, version, path):
    async with pair(legacy=(version == 1), documents_extra=path) as peers:
        before = peers.snapshot()
        status = await peers.ready.verification.verify(peers.profile.profile_id, expected_revision=1)
        assert (status.state, status.error_code, status.user) == ("incompatible", "invalid_response", None)
        expected = ["/healthz"] if path == "/healthz" else ["/healthz", path]
        assert [(c.request.method, str(c.request.url)) for c in peers.doc_wire.calls] == [
            ("GET", BASE + endpoint) for endpoint in expected
        ]
        if version == 2:
            with pytest.raises(CatalogError) as err:
                await peers.ready.catalog.list_projects(peers.profile.profile_id, expected_revision=1)
            assert err.value.code == "invalid_response"
            assert all("/projects" not in c.request.url.path for c in peers.doc_wire.calls)
        assert not peers.ai_wire.calls and not peers.dispatches
        assert peers.snapshot() == before
