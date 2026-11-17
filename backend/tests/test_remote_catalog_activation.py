"""VS-138 C4: persisted synthetic S2 -> real Verify -> owned HTTP -> ASGI.

No Remote package, listener, real credential or network is involved. Only the
unrelated project bootstrap and the low-level HTTP pool are replaced. Run with
--basetemp inside .voxarium_studio/temp/TASK-138 and -p no:cacheprovider -n0.
"""
from __future__ import annotations

import asyncio
import builtins
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
import inspect
import io
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from voxarium_studio import main
from voxarium_studio.build_info import BuildInfo
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.sync import catalog_runtime, credentials
from voxarium_studio.sync.catalog import CatalogError
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.catalog_providers import PendingCatalogProviders
from voxarium_studio.sync.catalog_verification import CatalogVerificationService
from voxarium_studio.sync.profiles import RemoteProfiles
from voxarium_studio.ws.bus import EventBus


BASE = "/api/remote/servers"
APP_KEY = "synthetic-studio-auth-138"
KEY_A = "SYNTHETIC-REMOTE-SECRET-A-138"
KEY_B = "SYNTHETIC-REMOTE-SECRET-B-138"
RAW = "SYNTHETIC-RAW-ERROR-SECRET-138"
P1 = dict(profile_id="11111111-1111-4111-8111-111111111138", name="same",
          url="https://one.invalid/deployment/prefix", revision=3)
P2 = dict(profile_id="22222222-2222-4222-8222-222222222138", name="same",
          url="https://two.invalid/other/prefix", revision=8)
CAPS = ["project_catalog", "explicit_project_create", "strict_connect"]


async def test_installed_remote_v1_verifies_but_cannot_release_r3(
    monkeypatch, studio_home,
):
    """Real installed Remote, not a synthetic v2 capability/hello (I9/I16).

    ASGI replaces only the network hop. Both applications' stores, profile
    providers, auth, HTTP parser and lifecycle are real. An authenticated v1
    result is diagnostic only: production catalog/Create remain closed before
    any further key lookup, journal publication or remote request.
    Missing Remote is a failed prerequisite, deliberately NOT importorskip.
    """
    from voxarium_studio_remote.app import create_app as create_remote_app
    from voxarium_studio_remote.auth import hash_token
    from voxarium_studio_remote.store import Store

    row = {**P1, "url": "http://127.0.0.1"}
    seed(studio_home, rows=(row,))
    store = Store(":memory:")
    store.add_user("Synthetic VS-177", hash_token(KEY_B))
    app = create_remote_app(store=store)
    calls = []

    @app.middleware("http")
    async def observe(request, call_next):
        calls.append((request.method, request.url.path))
        if request.url.path == "/healthz":
            assert "authorization" not in request.headers
        return await call_next(request)

    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)
    transport = CatalogHttpTransport(transport=httpx.ASGITransport(app=app))
    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport", lambda: transport)
    runtime = catalog_runtime.RemoteCatalogRuntime()
    try:
        ready = await runtime.start()
        before = snapshot(studio_home)
        status = await ready.verification.verify(row["profile_id"], expected_revision=row["revision"])
        assert status.state == "valid"
        assert status.wire_protocol == 1
        assert status.catalog_version is None
        assert status.error_code == "catalog_unavailable"
        assert calls == [("GET", "/healthz"), ("GET", "/api/v1/me")]
        assert snapshot(studio_home) == before

        def forbidden_key(*_args, **_kwargs):
            pytest.fail("R3 gate performed credential lookup")

        monkeypatch.setattr(credentials, "load_profile_token", forbidden_key)
        with pytest.raises(CatalogError, match="service_unavailable"):
            await ready.catalog.list_projects(row["profile_id"], expected_revision=row["revision"])
        with pytest.raises(CatalogError, match="service_unavailable"):
            await ready.create.create_project(
                row["profile_id"], expected_revision=row["revision"], id="game",
                display_name="Synthetic VS-177", operation_id="17700000-0000-4000-8000-000000000001",
            )
        assert snapshot(studio_home) == before
        assert store.list_spaces() == []
        assert calls == [("GET", "/healthz"), ("GET", "/api/v1/me")]
    finally:
        await runtime.close()
        store.close()


def seed(home, rows=(P1, P2), *, key=KEY_A):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps({"remote_servers": list(rows)}), encoding="utf-8")
    (home / "remote-keys.json").write_text(json.dumps({"version": 2, "profiles": {
        row["profile_id"]: {"url": row["url"], "token": key if row == P1 else KEY_B,
                             "revision": 2} for row in rows}, "legacy": {}}), encoding="utf-8")


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def health(version=1):
    body = dict(status="ok", service="voxarium_studio_remote", version="synthetic", protocol=version)
    if version == 2:
        body.update(supported_protocols=[2], catalog_api=2)
    return body


def identity(version=1, *, user="synthetic-user"):
    body = {"protocol": version, "user": {"id": user, "color": "#aAbBcC",
            "name" if version == 1 else "display_name": user}}
    if version == 2:
        body.update(catalog_api=2, capabilities=CAPS.copy())
    return body


def reply(request, *, version=1, user="synthetic-user"):
    return httpx.Response(200, json=health(version) if request.url.path.endswith("/healthz")
                          else identity(version, user=user))


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": code, "details": {}}
    secret_free(response.text)


def secret_free(value):
    for secret in (KEY_A, KEY_B, RAW):
        assert secret not in value


async def verify(client, row=P1, **kwargs):
    return await client.post(BASE + "/" + row["profile_id"] + "/verify",
                             json={"expected_revision": row["revision"]}, **kwargs)


@asynccontextmanager
async def client_for(app, *, lifespan=True):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1", trust_env=False,
                                 headers={"authorization": "Bearer " + APP_KEY}) as client:
        if lifespan:
            async with app.router.lifespan_context(app):
                yield client
        else:
            yield client


class Pool(httpx.AsyncBaseTransport):
    """Only HTTPX's trusted low-level seam; production parser/cleanup stay real."""
    def __init__(self):
        self.calls = []
        self.closed = 0
        self.handler = reply
        self.close_started = asyncio.Event()
        self.close_release = None

    async def handle_async_request(self, request):
        assert not self.closed, "Send after owned pool close"
        self.calls.append(request)
        response = self.handler(request)
        return await response if inspect.isawaitable(response) else response

    async def aclose(self):
        self.close_started.set()
        if self.close_release is not None:
            await self.close_release.wait()
        self.closed += 1


@pytest.fixture
async def harness(monkeypatch, studio_home, tmp_path):
    """Own fixtures, not V0 mount fixtures which deliberately deny Verify."""
    counts = Counter()

    def denied(*_args, **_kwargs):
        counts["forbidden"] += 1
        raise AssertionError("Activation attempted real network/process/S2 mutation")

    # Install after the async loop's own Windows wakeup socket is initialized.
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, denied)
    for name in ("getaddrinfo", "gethostbyname", "create_connection"):
        monkeypatch.setattr(socket, name, denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    for name in ("create", "rename", "change_url", "delete", "recover", "load_token"):
        monkeypatch.setattr(RemoteProfiles, name, denied)
    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)
    monkeypatch.setattr(main, "build_info", lambda: BuildInfo(version="test", commit="test"))
    monkeypatch.setattr(main, "static_dir", lambda: None)
    monkeypatch.setattr(main, "record_recent", lambda *_: None)

    class Project:
        def __init__(self, root, auth):
            self.paths = ProjectPaths.for_project(Path(root))
            self.auth, self.bus = auth, EventBus()
            self.config = SimpleNamespace(project_name=None)

        async def startup(self):
            pass

        async def shutdown(self):
            pass

    def initialise(state, *, browser_bus):
        state.browser_bus = browser_bus
        state.project_contexts = {state.ctx.paths.root: state.ctx}

    async def shutdown(state):
        for ctx in state.project_contexts.values():
            await ctx.shutdown()
        state.project_contexts.clear()

    monkeypatch.setattr(main, "AppContext", SimpleNamespace(create=Project))
    monkeypatch.setattr(main, "initialise_workspace", initialise)
    monkeypatch.setattr(main, "shutdown_workspace", shutdown)
    pool = Pool()
    transports, services = [], []

    def transport_factory():
        counts["transport_factory"] += 1
        transport = CatalogHttpTransport(transport=pool)
        transports.append(transport)
        return transport

    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport", transport_factory)
    original_init = CatalogVerificationService.__init__

    def service_init(service, *, profiles, credentials, transport, clock):
        assert isinstance(profiles, PendingCatalogProviders)
        assert credentials.__self__ is profiles
        assert transport in transports

        def counted_clock():
            counts["clock"] += 1
            return clock()

        original_init(service, profiles=profiles, credentials=credentials,
                      transport=transport, clock=counted_clock)
        services.append(service)

    monkeypatch.setattr(CatalogVerificationService, "__init__", service_init)
    original_token = credentials.load_profile_token

    def token(*args):
        counts["token"] += 1
        return original_token(*args)

    monkeypatch.setattr(credentials, "load_profile_token", token)
    seed(studio_home)
    root = tmp_path / "project"
    root.mkdir()
    app = main.create_app(str(root), auth=AuthSettings(token=APP_KEY))
    yield SimpleNamespace(app=app, pool=pool, home=studio_home, counts=counts,
                          services=services, transports=transports)
    assert counts["forbidden"] == 0


@contextmanager
def no_writes(monkeypatch):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Read-only catalog request attempted a write")

    with monkeypatch.context() as mp:
        for owner in (builtins, io):
            original = owner.open

            def opened(file, mode="r", *args, _original=original, **kwargs):
                if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
                    return denied()
                return _original(file, mode, *args, **kwargs)

            mp.setattr(owner, "open", opened)
        original_os_open = os.open

        def os_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                return denied()
            return original_os_open(path, flags, *args, **kwargs)

        mp.setattr(os, "open", os_open)
        for name in ("mkdir", "replace", "rename", "remove", "unlink", "rmdir"):
            mp.setattr(os, name, denied)
        yield
    assert not attempts


@pytest.mark.parametrize("version", [1, 2])
async def test_real_mounted_verify_is_explicit_prefix_bound_and_never_enables_catalog(harness, monkeypatch, version):
    h = harness
    h.pool.handler = lambda request: reply(request, version=version)
    before = snapshot(h.home)
    async with client_for(h.app) as client:
        with no_writes(monkeypatch):
            response = await verify(client)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body == {"state": "valid", "checked_at": body["checked_at"],
                            "user": {"id": "synthetic-user", "display_name": "synthetic-user", "color": "#aAbBcC"},
                            "wire_protocol": version, "catalog_version": 2 if version == 2 else None,
                            "error_code": None if version == 2 else "catalog_unavailable"}
            assert body["checked_at"].endswith("Z")
            assert response.headers["cache-control"] == "no-store"
            assert [str(r.url) for r in h.pool.calls] == [P1["url"] + "/healthz", P1["url"] + f"/api/v{version}/me"]
            health_request, me_request = h.pool.calls
            assert "authorization" not in health_request.headers
            assert me_request.headers["authorization"] == "Bearer " + KEY_A
            for request in h.pool.calls:
                assert request.method == "GET" and not request.content and not request.url.query
                assert request.headers["cache-control"] == "no-store"
                assert "cookie" not in request.headers
                assert APP_KEY not in str(request.headers)
            counts, sends = h.counts.copy(), len(h.pool.calls)
            metadata = await client.get(BASE)
            assert metadata.status_code == 200, metadata.text
            assert all(row["verification"] == {"state": "unavailable"} for row in metadata.json()["items"])
            error(await client.get(BASE + "/" + P1["profile_id"] + "/projects"), 502, "service_unavailable")
            assert h.counts == counts and len(h.pool.calls) == sends
            # Success is not a cache: a second explicit Verify does fresh I/O.
            assert (await verify(client)).status_code == 200
            assert len(h.pool.calls) == 4
            assert len(h.services) == 2 and h.services[0] is not h.services[1]
            assert h.services[0]._guards._profiles is not h.services[1]._guards._profiles
    assert h.pool.closed == 1 and h.counts["transport_factory"] == 1
    assert snapshot(h.home) == before


@pytest.mark.parametrize("headers,status,code", [
    ({"authorization": ""}, 401, "studio_unauthorised"),
    ({"authorization": "Bearer wrong"}, 401, "studio_unauthorised"),
    ({"origin": "https://foreign.invalid"}, 403, "studio_forbidden"),
    ({"host": "bad host"}, 403, "studio_forbidden"),
])
async def test_verify_auth_precedes_s2_and_transport(harness, monkeypatch, headers, status, code):
    h = harness
    async with client_for(h.app) as client:
        reads = []
        def denied(*_args):
            reads.append(True)
            raise AssertionError("Authentication must precede S2")
        monkeypatch.setattr(RemoteProfiles, "get", denied)
        error(await verify(client, headers=headers), status, code)
        assert not reads and not h.pool.calls and not h.services
        assert h.counts["token"] == h.counts["clock"] == 0


@pytest.mark.parametrize("body", [
    {"expected_revision": 3, "url": "https://attacker.invalid"},
    {"expected_revision": 3, "token": RAW},
    {"expected_revision": True}, {"expected_revision": 0},
])
async def test_browser_cannot_inject_binding_or_transport(harness, body):
    async with client_for(harness.app) as client:
        error(await client.post(BASE + "/" + P1["profile_id"] + "/verify", json=body), 422, "invalid_request")
    assert not harness.pool.calls and not harness.services
    assert harness.counts["token"] == 0


@pytest.mark.parametrize("case,state,code,sends", [
    ("unknown-protocol", "incompatible", "version_mismatch", 1),
    ("boolean-protocol", "incompatible", "invalid_response", 1),
    ("v2-no-catalog", "incompatible", "invalid_response", 1),
    ("v2-no-supported", "incompatible", "invalid_response", 1),
    ("v2-no-strict-connect", "incompatible", "capabilities_missing", 2),
    ("v2-no-catalog-cap", "incompatible", "capabilities_missing", 2),
    ("v2-malformed-user", "incompatible", "invalid_response", 2),
    ("v1-extra-field", "incompatible", "invalid_response", 1),
    ("v1-wrong-user", "incompatible", "invalid_response", 2),
    ("me-unauthorised", "unauthorised", "remote_unauthorised", 2),
    ("me-error", "error", "remote_error", 2),
    ("health-error", "error", "remote_error", 1),
    # Existing _ReadTransport deliberately flattens exceptions raised by its
    # transport to transport_unavailable. Do not silently change that S3 mapping
    # in C4; body-level protocol failures above remain incompatible instead.
    ("redirect", "offline", "transport_unavailable", 1),
    ("invalid-json", "offline", "transport_unavailable", 1),
    ("timeout", "offline", "transport_unavailable", 1),
    ("raw-exception", "offline", "transport_unavailable", 1),
])
async def test_strict_discovery_and_remote_errors_are_sanitized_without_fallback(harness, caplog, case, state, code, sends):
    h = harness
    for logger in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
        caplog.set_level(logging.DEBUG, logger=logger)
    def handler(request):
        is_health = request.url.path.endswith("/healthz")
        version = 2 if case.startswith("v2-") else 1
        body = health(version) if is_health else identity(version)
        status = 200
        if case == "unknown-protocol": body["protocol"] = 99
        elif case == "boolean-protocol": body["protocol"] = True
        elif case == "v2-no-catalog": body.pop("catalog_api", None)
        elif case == "v2-no-supported": body.pop("supported_protocols", None)
        elif case in {"v2-no-strict-connect", "v2-no-catalog-cap"} and not is_health:
            body["capabilities"].remove("strict_connect" if case.endswith("strict-connect") else "project_catalog")
        elif case == "v2-malformed-user" and not is_health: body["user"].pop("color")
        elif case == "v1-extra-field": body["message"] = RAW
        elif case == "v1-wrong-user" and not is_health: body["user"]["display_name"] = body["user"].pop("name")
        elif case == "me-unauthorised" and not is_health: status, body = 401, {"message": RAW}
        elif case == "me-error" and not is_health: status, body = 503, {"message": RAW}
        elif case == "health-error": status, body = 500, {"message": RAW}
        elif case == "redirect": return httpx.Response(307, headers={"location": "https://attacker.invalid/" + RAW})
        elif case == "invalid-json": return httpx.Response(200, content=b'{"protocol":1,"protocol":2}', headers={"content-type": "application/json"})
        elif case == "timeout": raise httpx.ReadTimeout(RAW, request=request)
        elif case == "raw-exception": raise RuntimeError(RAW)
        return httpx.Response(status, json=body, headers={"x-private": RAW})

    h.pool.handler = handler
    async with client_for(h.app) as client:
        response = await verify(client)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == state
        assert response.json()["error_code"] == code
        assert response.json()["user"] is None
        assert response.headers["cache-control"] == "no-store"
        secret_free(response.text)
        assert len(h.pool.calls) == sends
        assert all(r.url.host == "one.invalid" for r in h.pool.calls)
        if sends == 1: assert "authorization" not in h.pool.calls[0].headers
        error(await client.get(BASE + "/" + P1["profile_id"] + "/projects"), 502, "service_unavailable")
        assert len(h.pool.calls) == sends
    secret_free(caplog.text)


def drift(h, monkeypatch, kind):
    if kind == "key":
        seed(h.home, key=KEY_B)
    elif kind == "url":
        rows = [{**P1, "url": "https://changed.invalid/new"}, P2]
        seed(h.home, rows)
    elif kind == "revision":
        seed(h.home, [{**P1, "revision": 4}, P2])
    elif kind == "environment":
        monkeypatch.setenv(credentials.TOKEN_ENV_VAR, KEY_B)
        monkeypatch.setenv(credentials.URL_ENV_VAR, P1["url"])
    elif kind == "environment-unbound":
        monkeypatch.setenv(credentials.TOKEN_ENV_VAR, KEY_B)
        monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)
    elif kind == "pending":
        (h.home / "remote-profile-change.json").write_text(RAW, encoding="utf-8")
    elif kind == "home":
        monkeypatch.setenv("VOXARIUM_STUDIO_HOME", str(h.home / "other-home"))
    elif kind == "corrupt":
        (h.home / "remote-keys.json").write_text(RAW, encoding="utf-8")


@pytest.mark.parametrize("kind,status,code", [
    ("pending", 409, "profile_recovery_required"),
    ("home", 409, "profile_recovery_required"),
    ("revision", 409, "profile_revision_conflict"),
    ("environment-unbound", 409, "credential_required"),
    ("corrupt", 503, "credential_store_unavailable"),
])
async def test_drift_before_verify_fails_before_network(harness, monkeypatch, kind, status, code):
    h = harness
    async with client_for(h.app) as client:
        drift(h, monkeypatch, kind)
        before = snapshot(h.home)
        error(await verify(client), status, code)
        assert not h.pool.calls and h.counts["clock"] == 0
        assert snapshot(h.home) == before


@pytest.mark.parametrize("kind,status,code", [
    ("key", 409, "credential_binding_conflict"),
    ("url", 503, "profile_store_unavailable"),
    ("revision", 503, "profile_store_unavailable"),
    ("environment", 409, "credential_binding_conflict"),
    ("environment-unbound", 409, "credential_required"),
    ("pending", 409, "profile_recovery_required"),
    ("home", 409, "profile_recovery_required"),
])
@pytest.mark.parametrize("boundary", ["health", "me"])
@pytest.mark.parametrize("failed", [False, True])
async def test_drift_across_successful_or_failed_await_never_continues(harness, monkeypatch, kind, status, code, boundary, failed):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.url.path.endswith("/healthz") == (boundary == "health"):
            entered.set()
            await release.wait()
            if failed:
                raise httpx.ReadError(RAW, request=request)
        return reply(request)
    h.pool.handler = handler
    async with client_for(h.app) as client:
        task = asyncio.create_task(verify(client))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            drift(h, monkeypatch, kind)
            before = snapshot(h.home)
            release.set()
            error(await asyncio.wait_for(task, 3), status, code)
            assert snapshot(h.home) == before
        finally:
            release.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(h.pool.calls) == (1 if boundary == "health" else 2)
        assert h.counts["clock"] == 0
        assert all(r.url.host == "one.invalid" for r in h.pool.calls)


async def test_key_rotation_before_first_send_is_not_hidden_by_same_source_presence(harness, monkeypatch):
    h = harness
    original = credentials.load_profile_token
    reads = []
    def changing_key(*args):
        result = original(*args)
        reads.append(result)
        if len(reads) == 1:
            seed(h.home, key=KEY_B)
        return result
    monkeypatch.setattr(credentials, "load_profile_token", changing_key)
    async with client_for(h.app) as client:
        error(await verify(client), 409, "credential_binding_conflict")
        assert reads[0] == KEY_A and KEY_B in reads
        assert not h.pool.calls and not h.counts["clock"]


@pytest.mark.parametrize("kind,status,code", [
    ("url", 503, "profile_store_unavailable"),
    ("revision", 503, "profile_store_unavailable"),
    ("pending", 409, "profile_recovery_required"),
])
async def test_captured_provider_never_retargets_before_first_await(harness, monkeypatch, kind, status, code):
    h = harness
    original = CatalogVerificationService.__init__

    def changed_after_capture(service, **kwargs):
        provider = kwargs["profiles"]
        assert provider.expected_url == P1["url"]
        assert provider.expected_revision == P1["revision"]
        original(service, **kwargs)
        drift(h, monkeypatch, kind)

    monkeypatch.setattr(CatalogVerificationService, "__init__", changed_after_capture)
    async with client_for(h.app) as client:
        error(await verify(client), status, code)
        assert not h.pool.calls and not h.counts["token"] and not h.counts["clock"]


async def test_fresh_verification_uses_current_url_bound_environment_not_stored_key(harness, monkeypatch):
    h = harness
    async with client_for(h.app) as client:
        assert (await verify(client)).status_code == 200
        monkeypatch.setenv(credentials.TOKEN_ENV_VAR, KEY_B)
        monkeypatch.setenv(credentials.URL_ENV_VAR, P1["url"])
        assert (await verify(client)).status_code == 200
        assert [r.headers["authorization"] for r in h.pool.calls if "authorization" in r.headers] == [
            "Bearer " + KEY_A, "Bearer " + KEY_B,
        ]
        assert len(h.services) == 2


async def test_concurrent_profiles_keep_fresh_providers_and_exact_bearers(harness):
    h = harness
    both = asyncio.Event()
    entered = set()
    async def handler(request):
        host = request.url.host
        if request.url.path.endswith("/healthz"):
            entered.add(host)
            if len(entered) == 2: both.set()
            await both.wait()
        return reply(request, user=host)
    h.pool.handler = handler
    async with client_for(h.app) as client:
        results = await asyncio.wait_for(asyncio.gather(verify(client, P1), verify(client, P2)), 3)
        assert [r.json()["user"]["id"] for r in results] == ["one.invalid", "two.invalid"]
        assert all(r.status_code == 200 for r in results)
        for row, key in ((P1, KEY_A), (P2, KEY_B)):
            requests = [r for r in h.pool.calls if str(r.url).startswith(row["url"] + "/")]
            assert len(requests) == 2
            assert "authorization" not in requests[0].headers
            assert requests[1].headers["authorization"] == "Bearer " + key
        assert len(h.services) == 2
        providers = [s._guards._profiles for s in h.services]
        assert providers[0] is not providers[1]
        assert {p.profile_id for p in providers} == {P1["profile_id"], P2["profile_id"]}


@pytest.mark.parametrize("boundary", ["health", "me"])
async def test_caller_cancellation_preserved_no_continuation_or_cache(harness, boundary):
    h = harness
    entered = asyncio.Event()
    async def handler(request):
        if request.url.path.endswith("/healthz") == (boundary == "health"):
            entered.set()
            await asyncio.Event().wait()
        return reply(request)
    h.pool.handler = handler
    async with client_for(h.app) as client:
        task = asyncio.create_task(verify(client))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel(RAW)
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 3)
        assert not caught.value.args
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert len(h.pool.calls) == (1 if boundary == "health" else 2)
        assert not h.counts["clock"]
        assert (await client.get(BASE)).json()["items"][0]["verification"] == {"state": "unavailable"}
    assert h.pool.closed == 1


async def test_lifespan_shutdown_stops_admission_drains_requests_and_closes_owned_pool(harness):
    h = harness
    entered = asyncio.Event()
    async def handler(request):
        entered.set()
        await asyncio.Event().wait()
    h.pool.handler = handler
    lifetime = h.app.router.lifespan_context(h.app)
    await lifetime.__aenter__()
    owner, ready = h.app.state.remote_catalog.runtime, h.app.state.remote_catalog.ready
    async with client_for(h.app, lifespan=False) as client:
        request = asyncio.create_task(verify(client))
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(lifetime.__aexit__(None, None, None), 3)
        result = (await asyncio.gather(request, return_exceptions=True))[0]
        if isinstance(result, httpx.Response):
            error(result, 502, "service_unavailable")
        else:
            assert isinstance(result, asyncio.CancelledError)
        assert request.done() and h.pool.closed == 1
        assert h.app.state.remote_catalog.runtime is None
        for method, path in (("POST", BASE + "/" + P1["profile_id"] + "/verify"),
                             ("GET", BASE), ("GET", BASE + "/" + P1["profile_id"] + "/projects")):
            response = await client.request(method, path, json={"expected_revision": 3} if method == "POST" else None)
            error(response, 502, "service_unavailable")
        with pytest.raises(CatalogError, match="service_unavailable"):
            await ready.verification.verify(P1["profile_id"], expected_revision=3)
        with pytest.raises(CatalogError, match="service_unavailable"):
            _ = owner.ready
        assert len(h.pool.calls) == 1
        await owner.close()
        assert h.pool.closed == 1


async def test_cancelled_close_finishes_cleanup_before_propagating_cancellation(harness):
    h = harness
    lifetime = h.app.router.lifespan_context(h.app)
    await lifetime.__aenter__()
    owner, ready = h.app.state.remote_catalog.runtime, h.app.state.remote_catalog.ready
    h.pool.close_release = asyncio.Event()
    closing = asyncio.create_task(lifetime.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(h.pool.close_started.wait(), 3)
        closing.cancel(RAW)
        assert h.app.state.remote_catalog.runtime is None
        with pytest.raises(CatalogError, match="service_unavailable"):
            await ready.verification.verify(P1["profile_id"], expected_revision=3)
        h.pool.close_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(closing, 3)
        assert not caught.value.args
        assert h.pool.closed == 1 and not h.pool.calls
        await owner.close()
        assert h.pool.closed == 1
    finally:
        h.pool.close_release.set()
        if not closing.done(): closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)


async def test_repeated_close_cancellation_waits_for_inflight_response_cleanup(harness):
    h = harness
    body_started, closing_stream, release_stream = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stream_closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            body_started.set()
            yield b'{"protocol":'
            await asyncio.Event().wait()

        async def aclose(self):
            closing_stream.set()
            await release_stream.wait()
            stream_closed.append(True)

    h.pool.handler = lambda _request: httpx.Response(
        200, stream=Stream(), headers={"content-type": "application/json"},
    )
    lifetime = h.app.router.lifespan_context(h.app)
    await lifetime.__aenter__()
    owner = h.app.state.remote_catalog.runtime
    async with client_for(h.app, lifespan=False) as client:
        request = asyncio.create_task(verify(client))
        await asyncio.wait_for(body_started.wait(), 3)
        closing = asyncio.create_task(lifetime.__aexit__(None, None, None))
        try:
            await asyncio.wait_for(closing_stream.wait(), 3)
            assert not h.pool.close_started.is_set()
            closing.cancel(RAW)
            # Await an event-loop turn without sleeping or scheduling external I/O.
            turn = asyncio.Event()
            asyncio.get_running_loop().call_soon(turn.set)
            await turn.wait()
            closing.cancel(RAW)
            assert not closing.done() and not h.pool.closed
            assert h.app.state.remote_catalog.runtime is None
            release_stream.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(closing, 3)
            assert not caught.value.args
            await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 3)
            assert stream_closed == [True] and h.pool.closed == 1
            assert request.done() and len(h.pool.calls) == 1
            await owner.close()
            assert h.pool.closed == 1
        finally:
            release_stream.set()
            for task in (closing, request):
                if not task.done(): task.cancel()
            await asyncio.gather(closing, request, return_exceptions=True)


async def test_repeated_request_cancellation_joins_response_cleanup(harness):
    h = harness
    body_started, close_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            body_started.set()
            yield b'{"protocol":'
            await asyncio.Event().wait()

        async def aclose(self):
            close_started.set()
            await release.wait()
            closed.append(True)

    h.pool.handler = lambda _request: httpx.Response(
        200, stream=Stream(), headers={"content-type": "application/json"},
    )
    async with client_for(h.app) as client:
        owner = h.app.state.remote_catalog.runtime
        request = asyncio.create_task(verify(client))
        try:
            await asyncio.wait_for(body_started.wait(), 3)
            request.cancel(RAW)
            await asyncio.wait_for(close_started.wait(), 3)
            request.cancel(RAW)
            turn = asyncio.Event()
            asyncio.get_running_loop().call_soon(turn.set)
            await turn.wait()
            assert not request.done() and not closed
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(request, 3)
            assert not caught.value.args
            assert caught.value.__context__ is None and caught.value.__cause__ is None
            assert closed == [True] and not owner._requests
            assert len(h.pool.calls) == 1 and not h.counts["clock"]
            # Cancellation of one request does not revoke the application owner.
            h.pool.handler = reply
            assert (await verify(client)).status_code == 200
        finally:
            release.set()
            if not request.done(): request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert h.pool.closed == 1


async def test_concurrent_close_callers_share_cleanup_despite_one_cancellation(harness):
    h = harness
    async with client_for(h.app):
        owner = h.app.state.remote_catalog.runtime
        h.pool.close_release = asyncio.Event()
        first = asyncio.create_task(owner.close())
        await asyncio.wait_for(h.pool.close_started.wait(), 3)
        second = asyncio.create_task(owner.close())
        first.cancel(RAW)
        h.pool.close_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(first, 3)
        assert not caught.value.args
        await asyncio.wait_for(second, 3)
        assert h.pool.closed == 1 and not owner._requests
        with pytest.raises(CatalogError, match="service_unavailable"):
            _ = owner.ready
    assert h.pool.closed == 1


async def test_pool_close_failure_is_sanitized_and_terminal(harness, monkeypatch):
    h = harness
    attempts = []

    async def fail():
        attempts.append(True)
        raise RuntimeError(RAW)

    monkeypatch.setattr(h.pool, "aclose", fail)
    lifetime = h.app.router.lifespan_context(h.app)
    await lifetime.__aenter__()
    owner = h.app.state.remote_catalog.runtime
    with pytest.raises(CatalogError, match="service_unavailable") as caught:
        await lifetime.__aexit__(None, None, None)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    secret_free(repr(caught.value))
    assert h.app.state.remote_catalog.runtime is None
    assert owner._transport is None and not owner._requests
    with pytest.raises(CatalogError, match="service_unavailable"):
        await owner.close()
    assert attempts == [True] and not h.pool.calls


async def test_transport_constructor_failure_never_publishes_ready_owner(harness, monkeypatch):
    h = harness

    def fail():
        raise RuntimeError(RAW)

    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport", fail)
    owner = catalog_runtime.create_remote_catalog_runtime()
    before = snapshot(h.home)
    with pytest.raises(CatalogError, match="service_unavailable") as caught:
        await owner.start()
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    secret_free(repr(caught.value))
    with pytest.raises(CatalogError, match="service_unavailable"):
        _ = owner.ready
    with pytest.raises(CatalogError, match="service_unavailable"):
        await owner.start()
    await owner.close()
    assert owner._transport is None and not owner._requests
    assert not h.transports and not h.pool.calls and snapshot(h.home) == before
