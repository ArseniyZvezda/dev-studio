"""VS-135/VS-140/VS-139 C3/C4/C6: real router, security and ASGI lifespan.

All homes/projects are pytest-owned. HTTPX's in-process ASGI transport never
opens a listener; normal conftest isolation remains enabled. Most cases replace
only the unrelated project bootstrap, not Remote's persisted S2 composition.
The project-switch case also starts real AppContexts and workspace registries.
"""
from __future__ import annotations

import asyncio
import builtins
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from voxarium_studio import main
from voxarium_studio.api import project as project_api
from voxarium_studio.api.remote_metadata import CatalogMetadataAdapter
from voxarium_studio.build_info import BuildInfo
from voxarium_studio.core import workspace
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project import init_project
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.plugins.registry import Registry
from voxarium_studio.sync import catalog_runtime, catalog_verification, credentials
from voxarium_studio.sync.catalog import CatalogClient, CatalogError
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.catalog_metadata import CatalogMetadataObserver
from voxarium_studio.sync.catalog_service import CatalogService
from voxarium_studio.sync.profiles import ProfileError, RemoteProfiles
from voxarium_studio.updates import service as update_service
from voxarium_studio.ws.bus import EventBus


BASE = "/api/remote/servers"
CANARY = "VS135-SYNTHETIC-SECRET-NEVER-PUBLISH"
P1 = dict(profile_id="11111111-1111-4111-8111-111111111111", name="same",
          url="https://one.invalid/deployment/prefix", revision=3)
P2 = dict(profile_id="22222222-2222-4222-8222-222222222222", name="same",
          url="http://two.invalid/other", revision=8)
READ_ROUTES = (("GET", BASE, None),
          ("POST", BASE + "/" + P1["profile_id"] + "/verify", {"expected_revision": 3}),
          ("GET", BASE + "/" + P1["profile_id"] + "/projects", None))
MUTATION_ROUTES = (
    ("POST", BASE, {"name": "same", "url": "https://new.invalid/prefix"}),
    ("PATCH", BASE + "/" + P1["profile_id"], {"expected_revision": 3, "name": "renamed"}),
)
SENSITIVE_ROUTES = (
    ("PATCH", BASE + "/" + P1["profile_id"],
     {"expected_revision": 3, "url": "https://new.invalid", "clear_credential": True}),
    ("PUT", BASE + "/" + P1["profile_id"] + "/credential", {"expected_revision": 3, "token": CANARY}),
    ("DELETE", BASE + "/" + P1["profile_id"] + "/credential", {"expected_revision": 3}),
    ("DELETE", BASE + "/" + P1["profile_id"], {"expected_revision": 3}),
    ("POST", BASE + "/" + P1["profile_id"] + "/recover", {"expected_revision": 3,
     "confirmation": {"profile_id": P1["profile_id"], "action": "discard_uncertain_credential"}}),
)
ROUTES = READ_ROUTES + MUTATION_ROUTES + SENSITIVE_ROUTES


def seed(home: Path, rows=(P1, P2), *, stored=True) -> None:
    """Persist the actual S2 formats, never replace readers with canned DTOs."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps({"remote_servers": list(rows)}), encoding="utf-8")
    if stored:
        (home / "remote-keys.json").write_text(json.dumps({"version": 2, "profiles": {
            p["profile_id"]: {"url": p["url"], "token": CANARY, "revision": 2}
            for p in rows}, "legacy": {}}), encoding="utf-8")


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": code, "details": {}}
    assert CANARY not in response.text


def diagnostic(response, isolation):
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == dict(state="valid", checked_at=body["checked_at"],
                        user=dict(id="synthetic", display_name="Synthetic", color="#abcdef"),
                        wire_protocol=1, catalog_version=None, error_code="catalog_unavailable")
    assert body["checked_at"].endswith("Z")
    assert CANARY not in response.text
    assert isolation.calls["providers"] == isolation.calls["verify"] == isolation.calls["clock"] == 1
    assert isolation.calls["token"] > 0 and isolation.calls["v2_verify"] == 0
    assert [str(r.url) for r in isolation.requests] == [
        P1["url"] + "/healthz", P1["url"] + "/api/v1/me"]


@asynccontextmanager
async def client_for(app, *, lifespan=True):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1", trust_env=False) as client:
        if lifespan:
            async with app.router.lifespan_context(app):
                yield client
        else:
            yield client


class ProjectStub:
    """Only the project lifecycle is lightweight; no Remote methods live here."""
    def __init__(self, root, auth):
        self.paths = ProjectPaths.for_project(Path(root))
        self.auth = auth
        self.bus = EventBus()
        self.config = SimpleNamespace(project_name=None)
        self.started = self.stopped = 0

    async def startup(self):
        self.started += 1

    async def shutdown(self):
        self.stopped += 1


class MountIsolation(Counter):
    """Forbidden attempts stay separate from admitted C4/C6 calls."""
    def __init__(self):
        super().__init__()
        self.allow_verify = False
        self.allow_profile_mutations = False
        self.calls = Counter()
        self.requests = []


@pytest.fixture(autouse=True)
async def isolation(studio_home):
    """No external I/O; scoped Verify and local C6 writes are admitted."""
    counts = MountIsolation()

    def deny(name):
        def forbidden(*_args, **_kwargs):
            counts[name] += 1
            raise AssertionError("Unadmitted Remote operation: " + name)
        return forbidden

    def verification_only(name, original):
        def guarded(*args, **kwargs):
            if not counts.allow_verify:
                return deny(name)()
            counts.calls[name] += 1
            return original(*args, **kwargs)
        return guarded

    def reply(request):
        if not counts.allow_verify:
            return deny("http")()
        counts.requests.append(request)
        assert request.method == "GET"
        assert not request.url.query and not request.content
        assert request.headers["cache-control"] == "no-store"
        assert request.headers.get("cookie") is None
        if str(request.url) == P1["url"] + "/healthz":
            assert request.headers.get("authorization") is None
            body = dict(status="ok", service="voxarium_studio_remote",
                        version="synthetic", protocol=1)
        else:
            assert str(request.url) == P1["url"] + "/api/v1/me"
            assert request.headers["authorization"] == "Bearer " + CANARY
            body = dict(protocol=1, user=dict(id="synthetic", name="Synthetic", color="#abcdef"))
        return httpx.Response(200, json=body)

    # Async fixture: pytest's Windows event-loop wakeup socket is initialized
    # before guards and torn down afterwards. It is not an HTTP listener; every
    # connection attempted by the application itself remains forbidden.
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
        monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)
        build = BuildInfo(version="test", commit="test")
        monkeypatch.setattr(main, "build_info", lambda: build)
        monkeypatch.setattr(update_service, "build_info", lambda: build)
        monkeypatch.setattr(project_api, "build_info", lambda: build)
        monkeypatch.setattr(main, "static_dir", lambda: None)
        monkeypatch.setattr(socket.socket, "connect", deny("network"))
        monkeypatch.setattr(socket.socket, "connect_ex", deny("network"))
        monkeypatch.setattr(socket.socket, "bind", deny("listener"))
        monkeypatch.setattr(socket.socket, "listen", deny("listener"))
        monkeypatch.setattr(socket, "create_connection", deny("network"))
        monkeypatch.setattr(socket, "getaddrinfo", deny("network"))
        monkeypatch.setattr(socket, "gethostbyname", deny("network"))
        monkeypatch.setattr(subprocess, "Popen", deny("process"))
        monkeypatch.setattr(credentials, "load_profile_token", verification_only("token", credentials.load_profile_token))
        monkeypatch.setattr(RemoteProfiles, "load_token", deny("token"))
        monkeypatch.setattr(catalog_runtime.RemoteCatalogRuntime, "providers_for", verification_only(
            "providers", catalog_runtime.RemoteCatalogRuntime.providers_for))
        monkeypatch.setattr(catalog_verification.CatalogVerificationService, "verify", verification_only(
            "verify", catalog_verification.CatalogVerificationService.verify))
        # I9: catalog reads are admitted like explicit Verify (same isolation).
        monkeypatch.setattr(CatalogService, "list_projects", verification_only(
            "list_projects", CatalogService.list_projects))
        monkeypatch.setattr(CatalogClient, "verify", verification_only("v2_verify", CatalogClient.verify))
        monkeypatch.setattr(catalog_runtime, "datetime", SimpleNamespace(
            now=verification_only("clock", catalog_runtime.datetime.now)))
        # Replace only the low-level HTTP boundary, not services or ready DTOs.
        monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport", lambda: CatalogHttpTransport(
            transport=httpx.MockTransport(reply)))
        for name in ("create", "rename"):
            original = getattr(RemoteProfiles, name)

            def admitted(*args, _name=name, _original=original, **kwargs):
                if not counts.allow_profile_mutations:
                    return deny("mutation")()
                counts.calls[_name] += 1
                return _original(*args, **kwargs)

            monkeypatch.setattr(RemoteProfiles, name, admitted)
        for name in ("change_url", "delete", "recover"):
            monkeypatch.setattr(RemoteProfiles, name, deny("mutation"))
        yield counts
    assert not counts, dict(counts)


@contextmanager
def explicit_verify(isolation):
    assert not isolation.allow_verify
    isolation.allow_verify = True
    try:
        yield
    finally:
        isolation.allow_verify = False


@pytest.fixture
def lightweight(monkeypatch, tmp_path):
    contexts = []

    def create(root, auth):
        ctx = ProjectStub(root, auth)
        contexts.append(ctx)
        return ctx

    def initialise(state, *, browser_bus):
        state.browser_bus = browser_bus
        state.project_contexts = {state.ctx.paths.root: state.ctx}

    async def shutdown(state):
        # Startup may fail before initialise_workspace publishes its registry.
        # Match the real workspace's failed-context inventory as well.
        resident = getattr(state, "project_contexts", {})
        failed = getattr(state, "_failed_project_contexts", {})
        for ctx in {id(ctx): ctx for ctx in (*resident.values(), *failed.values())}.values():
            await ctx.shutdown()
        resident.clear()
        failed.clear()

    monkeypatch.setattr(main, "AppContext", SimpleNamespace(create=create))
    monkeypatch.setattr(main, "initialise_workspace", initialise)
    monkeypatch.setattr(main, "shutdown_workspace", shutdown)
    monkeypatch.setattr(main, "record_recent", lambda *_: None)
    root = tmp_path / "project"
    root.mkdir()
    return SimpleNamespace(root=root, contexts=contexts)


def count_services(monkeypatch, app):
    counts = Counter()
    holder = app.state.remote_catalog
    for name in ("list", "get", "create", "rename", "verify", "list_projects",
                 "change_url", "set_credential", "clear_credential", "delete", "recover"):
        original = getattr(holder, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(holder, name, counted)
    # Include the real S2 boundary, not just router service dispatch.
    for owner, name in ((RemoteProfiles, "list"), (RemoteProfiles, "get"),
                        (credentials, "profile_token_source")):
        original = getattr(owner, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            counts["S2:" + _name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)
    return counts


@contextmanager
def read_only(monkeypatch, counts):
    """Measure requests only: bootstrap/project init may legitimately write."""
    with monkeypatch.context() as mp:
        def denied(*_args, **_kwargs):
            counts["writes"] += 1
            raise AssertionError("Read-only Remote request wrote a file")

        for owner, name in ((builtins, "open"), (io, "open")):
            original = getattr(owner, name)

            def opened(file, mode="r", *args, _original=original, **kwargs):
                if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
                    return denied()
                return _original(file, mode, *args, **kwargs)

            mp.setattr(owner, name, opened)
        original_os_open = os.open

        def os_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                return denied()
            return original_os_open(path, flags, *args, **kwargs)

        mp.setattr(os, "open", os_open)
        for name in ("mkdir", "replace", "rename", "remove", "unlink", "rmdir"):
            mp.setattr(os, name, denied)
        yield
    assert counts["writes"] == 0


@pytest.mark.parametrize("method,path,body", ROUTES)
@pytest.mark.parametrize("authorization", [None, "Bearer wrong"])
async def test_all_routes_reject_missing_or_wrong_auth_before_services(
    lightweight, studio_home, monkeypatch, method, path, body, authorization,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root), auth=AuthSettings(token="app-key"))
    assert app.state.auth.token == "app-key"  # available before lifespan/requests
    counts = count_services(monkeypatch, app)
    async with client_for(app) as client:
        headers = {} if authorization is None else {"authorization": authorization}
        response = await client.request(method, path, json=body, headers=headers)
        error(response, 401, "studio_unauthorised")
        assert response.headers["www-authenticate"] == "Bearer"
        assert not counts


HOSTILE = [
    [("origin", "https://foreign.invalid")],
    [("origin", "null")],
    [("origin", "http://127.0.0.1/path")],
    [("origin", "http://127.0.0.1:bad")],
    [("origin", "http://user@127.0.0.1")],
    [("origin", "http://127.0.0.1"), ("origin", "http://127.0.0.1")],
    [("host", "127.0.0.1/path")],
    [("host", "127.0.0.1:bad")],
    [("host", "127.0.0.1"), ("host", "127.0.0.1")],
    [("host", "rebinding.invalid"), ("origin", "http://rebinding.invalid")],
    [("host", "rebinding.invalid"), ("forwarded", "host=127.0.0.1;proto=http"),
     ("x-forwarded-host", "127.0.0.1"), ("x-forwarded-proto", "http")],
    [("origin", "https://foreign.invalid"), ("forwarded", "host=foreign.invalid;proto=https"),
     ("x-forwarded-host", "foreign.invalid"), ("x-forwarded-proto", "https")],
]


@pytest.mark.parametrize("method,path,body", ROUTES)
@pytest.mark.parametrize("headers", HOSTILE)
async def test_all_routes_deny_hostile_origins_hosts_rebinding_and_forwarded(
    lightweight, studio_home, monkeypatch, method, path, body, headers,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root))  # unauthenticated loopback posture
    counts = count_services(monkeypatch, app)
    async with client_for(app) as client:
        response = await client.request(method, path, json=body, headers=headers)
        error(response, 403, "studio_forbidden")
        assert not counts


@pytest.mark.parametrize("method,path,body", ROUTES)
@pytest.mark.parametrize("headers", HOSTILE[:9] + HOSTILE[-1:] + [
    [("origin", "")], [("host", "")],
])
async def test_valid_token_does_not_bypass_browser_host_origin_policy(
    lightweight, studio_home, monkeypatch, method, path, body, headers,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root), auth=AuthSettings(token="app-key"))
    counts = count_services(monkeypatch, app)
    async with client_for(app) as client:
        response = await client.request(
            method, path, json=body, headers=headers + [("authorization", "Bearer app-key")],
        )
        error(response, 403, "studio_forbidden")
        assert not counts


@pytest.mark.parametrize("method,path,body", READ_ROUTES)
@pytest.mark.parametrize("origin", [None, "http://127.0.0.1", "http://localhost:5173"])
async def test_nonbrowser_same_origin_and_explicit_dev_origin_are_admitted(
    lightweight, studio_home, monkeypatch, isolation, method, path, body, origin,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root), auth=AuthSettings(token="app-key"),
                          dev_origins=["http://localhost:5173"])
    assert app.state.dev_origins == ["http://localhost:5173"]
    headers = {"authorization": "Bearer app-key"}
    if origin is not None:
        headers["origin"] = origin
    async with client_for(app) as client:
        with read_only(monkeypatch, isolation):
            if path != BASE:
                with explicit_verify(isolation):
                    response = await client.request(method, path, json=body, headers=headers)
            else:
                response = await client.request(method, path, json=body, headers=headers)
        if path == BASE:
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert not isolation.calls and not isolation.requests
        elif method == "POST":
            diagnostic(response, isolation)
        else:
            strict_v2_refusal(response, isolation)


def strict_v2_refusal(response, isolation, *, before=()):
    """I9: GET projects runs its own strict-v2 discovery, read-only.

    The synthetic Remote is v1, so the read stops at the unauthenticated
    /healthz reply: no /api/v1 request, no key-bearing send, no catalog GET.
    """
    error(response, 502, "version_mismatch")
    assert isolation.requests[:len(before)] == list(before)
    added = isolation.requests[len(before):]
    assert [str(r.url) for r in added] == [P1["url"] + "/healthz"]
    assert "authorization" not in added[0].headers
    assert isolation.calls["list_projects"] >= 1 and isolation.calls["v2_verify"] >= 1


async def test_exact_global_get_uses_persisted_s2_and_never_activates_or_writes(
    lightweight, studio_home, monkeypatch, isolation,
):
    seed(studio_home)
    before = snapshot(studio_home)
    app = main.create_app(str(lightweight.root))
    observed = Counter()
    for owner, name in ((CatalogMetadataObserver, "observe"), (CatalogMetadataAdapter, "list")):
        original = getattr(owner, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            observed[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)
    async with client_for(app) as client:
        with read_only(monkeypatch, isolation):
            response = await client.get(BASE)
            repeat = await client.get(BASE)
        assert response.status_code == repeat.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == repeat.json() == {"items": [
            dict(row, has_token=True, credential_source="stored", verification={"state": "unavailable"})
            for row in (P1, P2)]}
        assert CANARY not in response.text
        assert observed == {"observe": 2, "list": 2}
        for wrong in ("/api/api/remote/servers", "/api/projects/not-a-project/remote/servers",
                      "/api/projects/not-a-project/api/remote/servers"):
            assert (await client.get(wrong)).status_code == 404
    assert snapshot(studio_home) == before


@pytest.mark.parametrize("method,path,body", MUTATION_ROUTES)
@pytest.mark.parametrize("origin", [None, "http://127.0.0.1", "http://localhost:5173"])
async def test_only_explicit_create_and_name_patch_reach_real_s2_without_outbound_io(
    lightweight, studio_home, isolation, method, path, body, origin,
):
    seed(studio_home)
    keys = studio_home / "remote-keys.json"
    before_keys = (keys.read_bytes(), keys.stat().st_mtime_ns)
    app = main.create_app(str(lightweight.root), auth=AuthSettings(token="app-key"),
                          dev_origins=["http://localhost:5173"])
    headers = {"authorization": "Bearer app-key"}
    if origin is not None:
        headers["origin"] = origin
    async with client_for(app) as client:
        isolation.allow_profile_mutations = True
        try:
            response = await client.request(method, path, json=body, headers=headers)
        finally:
            isolation.allow_profile_mutations = False
        assert response.status_code == (201 if method == "POST" else 200), response.text
        assert response.headers["cache-control"] == "no-store"
        row = response.json()
        assert row == dict(
            profile_id=row["profile_id"] if method == "POST" else P1["profile_id"],
            name=body["name"], url=body["url"] if method == "POST" else P1["url"],
            revision=1 if method == "POST" else 4,
            has_token=method == "PATCH", credential_source="none" if method == "POST" else "stored",
            verification={"state": "unavailable"},
        )
        assert CANARY not in response.text
        listed = await client.get(BASE, headers=headers)
        assert listed.status_code == 200, listed.text
        assert row in listed.json()["items"]
    assert (keys.read_bytes(), keys.stat().st_mtime_ns) == before_keys
    assert isolation.calls == {"create" if method == "POST" else "rename": 1}
    assert not isolation.requests


async def test_missing_home_is_empty_without_creating_anything(
    lightweight, tmp_path, monkeypatch, isolation,
):
    home = tmp_path / "missing-home"
    monkeypatch.setenv("VOXARIUM_STUDIO_HOME", str(home))
    app = main.create_app(str(lightweight.root))
    async with client_for(app) as client:
        with read_only(monkeypatch, isolation):
            response = await client.get(BASE)
        assert response.status_code == 200, response.text
        assert response.json() == {"items": []}
        assert response.headers["cache-control"] == "no-store"
    assert not home.exists()


@pytest.mark.parametrize("fault,status,code", [
    ("pending", 409, "profile_recovery_required"),
    ("environment-unbound", 503, "metadata_unavailable"),
    ("config-corrupt", 503, "application_store_unavailable"),
    ("keys-corrupt", 503, "credential_store_unavailable"),
])
async def test_unsafe_metadata_fails_whole_get_not_empty_or_partial(
    lightweight, studio_home, monkeypatch, isolation, fault, status, code,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root))
    async with client_for(app) as client:
        # Introduce pending only after start, so the mounted request observes it.
        if fault == "pending":
            (studio_home / "remote-profile-change.json").write_text(CANARY, encoding="utf-8")
        elif fault == "environment-unbound":
            monkeypatch.setenv(credentials.TOKEN_ENV_VAR, CANARY)
            monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)
        else:
            filename = "config.yaml" if fault == "config-corrupt" else "remote-keys.json"
            (studio_home / filename).write_text(CANARY, encoding="utf-8")
        before = snapshot(studio_home)
        with read_only(monkeypatch, isolation):
            response = await client.get(BASE)
        error(response, status, code)
        assert snapshot(studio_home) == before


@pytest.mark.parametrize("method,path,options", [
    ("POST", BASE, {"json": {"name": "new", "url": P1["url"], "token": CANARY}}),
    ("POST", BASE + "?unknown=1", {"json": {"name": "new", "url": P1["url"]}}),
    ("PATCH", BASE + "/not-a-uuid", {"json": {"expected_revision": 3, "name": "new"}}),
    ("PATCH", BASE + "/" + P1["profile_id"].replace("-", ""),
     {"json": {"expected_revision": 3, "name": "new"}}),
    ("PATCH", BASE + "/" + P1["profile_id"], {"json": {}}),
    ("PATCH", BASE + "/" + P1["profile_id"],
     {"json": {"expected_revision": True, "name": "new"}}),
    ("PATCH", BASE + "/" + P1["profile_id"],
     {"json": {"expected_revision": 3, "name": "new", "url": P2["url"]}}),
    ("PATCH", BASE + "/" + P1["profile_id"],
     {"json": {"expected_revision": 3, "name": "new", "clear_credential": True}}),
    ("GET", BASE + "?unknown=1", {}),
    ("GET", BASE, {"content": b"{}"}),
    ("POST", BASE + "/not-a-uuid/verify", {"json": {"expected_revision": 3}}),
    ("POST", BASE + "/" + P1["profile_id"] + "/verify", {"json": {"expected_revision": True}}),
    ("POST", BASE + "/" + P1["profile_id"] + "/verify", {"json": {"expected_revision": 0}}),
    ("POST", BASE + "/" + P1["profile_id"] + "/verify", {"json": {"expected_revision": 3, "token": CANARY}}),
    ("GET", BASE + "/" + P1["profile_id"] + "/projects?cursor=a&cursor=b", {}),
    ("GET", BASE + "/" + P1["profile_id"] + "/projects?expected_revision=3", {}),
])
async def test_validation_is_typed_no_store_before_service_dispatch(
    lightweight, studio_home, monkeypatch, method, path, options,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root))
    counts = count_services(monkeypatch, app)
    async with client_for(app) as client:
        response = await client.request(method, path, **options)
        error(response, 422, "invalid_request")
        assert not counts


@pytest.mark.parametrize("method,path", [
    ("DELETE", BASE), ("PATCH", BASE), ("PUT", BASE),
    ("POST", BASE + "/" + P1["profile_id"]),
    ("PUT", BASE + "/" + P1["profile_id"]),
    ("GET", BASE + "/" + P1["profile_id"] + "/verify"),
    ("PUT", BASE + "/" + P1["profile_id"] + "/projects"),
])
async def test_other_unsupported_methods_are_no_store_405(
    lightweight, studio_home, monkeypatch, method, path,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root))
    counts = count_services(monkeypatch, app)
    async with client_for(app) as client:
        response = await client.request(method, path)
        error(response, 405, "method_not_allowed")
        assert response.headers["allow"]
        assert not counts


async def test_built_spa_cannot_shadow_remote_method_refusals_or_add_mirrors(
    lightweight, studio_home, tmp_path, monkeypatch,
):
    seed(studio_home)
    static = tmp_path / "synthetic-static"
    static.mkdir()
    (static / "index.html").write_text("synthetic SPA", encoding="utf-8")
    monkeypatch.setattr(main, "static_dir", lambda: static)
    shared_routes = tuple(main.api_router.routes)
    app = main.create_app(str(lightweight.root))
    remote_paths = [path for path in app.openapi()["paths"] if "/remote/servers" in path]
    assert set(remote_paths) == {
        BASE, BASE + "/{profile_id}", BASE + "/{profile_id}/verify", BASE + "/{profile_id}/projects",
        BASE + "/{profile_id}/credential", BASE + "/{profile_id}/recover",
        BASE + "/{profile_id}/projects/operations/{operation_id}/observe",
        # VS-187 Gate L (dev spec §16.11): census + explicit URL confirmation.
        BASE + "/{profile_id}/consumers", BASE + "/{profile_id}/consumers/confirmation",
    }
    paths = app.openapi()["paths"]
    assert set(paths[BASE + "/{profile_id}/consumers"]) == {"get"}
    assert set(paths[BASE + "/{profile_id}/consumers/confirmation"]) == {"post", "delete"}
    assert set(paths[BASE]) == {"get", "post"}
    assert set(paths[BASE + "/{profile_id}"]) == {"patch", "delete"}
    assert set(paths[BASE + "/{profile_id}/credential"]) == {"put", "delete"}
    assert set(paths[BASE + "/{profile_id}/recover"]) == {"post"}
    assert set(paths[BASE + "/{profile_id}/verify"]) == {"post"}
    assert set(paths[BASE + "/{profile_id}/projects"]) == {"get", "post"}
    assert set(paths[BASE + "/{profile_id}/projects/operations/{operation_id}/observe"]) == {"post"}
    # include_router must not mutate the shared, import-level project/API router.
    assert tuple(main.api_router.routes) == shared_routes
    async with client_for(app) as client:
        for method, path, allow in (
            ("PUT", BASE + "/" + P1["profile_id"], "PATCH"),
            ("GET", ROUTES[1][1], "POST"),
            ("PUT", ROUTES[2][1], "GET"),
        ):
            response = await client.request(method, path)
            error(response, 405, "method_not_allowed")
            assert response.headers["allow"] == allow
        for path in ("/api/api/remote/servers", "/api/projects/missing/remote/servers",
                     "/api/unknown", "/api/projects/missing/unknown"):
            response = await client.get(path)
            assert response.status_code == 404
            assert response.json() == {"detail": "Not found: " + path}
        assert (await client.get("/some/spa/page")).text == "synthetic SPA"


async def test_unexpected_service_failure_is_secret_free_typed_no_store(
    lightweight, studio_home, monkeypatch,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root))

    def unavailable():
        raise RuntimeError(CANARY)

    monkeypatch.setattr(app.state.remote_catalog, "list", unavailable)
    async with client_for(app) as client:
        error(await client.get(BASE), 502, "service_unavailable")


async def test_projects_read_is_strict_v2_read_only_and_never_v1_catalog(
    lightweight, studio_home, monkeypatch, isolation,
):
    seed(studio_home)
    before = snapshot(studio_home)
    app = main.create_app(str(lightweight.root))
    async with client_for(app) as client:
        with read_only(monkeypatch, isolation):
            with explicit_verify(isolation):
                response = await client.get(BASE + "/" + P1["profile_id"] + "/projects")
        strict_v2_refusal(response, isolation)
        assert isolation.calls["providers"] == isolation.calls["list_projects"] == 1
        assert isolation.calls["verify"] == isolation.calls["clock"] == 0
        assert not app.state.remote_catalog.runtime._requests
    assert snapshot(studio_home) == before


async def test_explicit_verify_is_http_only_and_does_not_activate_or_cache_gets(
    lightweight, studio_home, monkeypatch, isolation,
):
    seed(studio_home)
    before = snapshot(studio_home)
    app = main.create_app(str(lightweight.root))
    async with client_for(app) as client:
        with read_only(monkeypatch, isolation):
            metadata = await client.get(BASE)
            with explicit_verify(isolation):
                response = await client.post(BASE + "/" + P1["profile_id"] + "/verify",
                                             json={"expected_revision": 3})
            diagnostic(response, isolation)
            calls = isolation.calls.copy()
            requests = list(isolation.requests)
            repeated = await client.get(BASE)
            assert metadata.status_code == repeated.status_code == 200
            assert metadata.headers["cache-control"] == repeated.headers["cache-control"] == "no-store"
            assert repeated.json() == metadata.json() == {"items": [
                dict(row, has_token=True, credential_source="stored", verification={"state": "unavailable"})
                for row in (P1, P2)]}
            assert CANARY not in repeated.text
            # A v1 Verify success is diagnostic, not a cached catalog permit.
            with explicit_verify(isolation):
                response = await client.get(BASE + "/" + P1["profile_id"] + "/projects")
            strict_v2_refusal(response, isolation, before=requests)
            assert isolation.calls["verify"] == calls["verify"]
            assert isolation.calls["clock"] == calls["clock"]
    assert snapshot(studio_home) == before


async def test_closed_holder_and_retained_dependencies_reject_admission(
    lightweight, studio_home, monkeypatch,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root))
    holder = app.state.remote_catalog
    async with client_for(app) as client:
        runtime, ready = holder.runtime, holder.ready
        assert (await client.get(BASE)).status_code == 200
    assert holder.runtime is None
    with pytest.raises(CatalogError, match="service_unavailable"):
        _ = holder.ready
    with pytest.raises(CatalogError, match="service_unavailable"):
        _ = runtime.ready
    with pytest.raises(CatalogError, match="service_unavailable"):
        ready.metadata.list()
    with pytest.raises(CatalogError, match="service_unavailable"):
        ready.profiles.get(P1["profile_id"])
    with pytest.raises(CatalogError, match="service_unavailable"):
        ready.mutations.create("new", "https://new.invalid")
    with pytest.raises(CatalogError, match="service_unavailable"):
        ready.mutations.rename(P1["profile_id"], "new", expected_revision=3)
    async with client_for(app, lifespan=False) as client:
        for method, path, body in ROUTES:
            error(await client.request(method, path, json=body), 502, "service_unavailable")


async def test_auth_is_available_and_denies_before_startup(lightweight, monkeypatch):
    app = main.create_app(str(lightweight.root), auth=AuthSettings(token="private"))
    counts = count_services(monkeypatch, app)
    assert app.state.remote_catalog.runtime is None
    async with client_for(app, lifespan=False) as client:
        for method, path, body in ROUTES:
            response = await client.request(method, path, json=body)
            error(response, 401, "studio_unauthorised")
            assert response.headers["www-authenticate"] == "Bearer"
    assert not counts


async def test_two_apps_two_homes_and_repeat_lifespan_never_share_owner_or_auth(
    lightweight, studio_home, tmp_path, monkeypatch,
):
    seed(studio_home, (P1,))
    app_a = main.create_app(str(lightweight.root), auth=AuthSettings(token="key-a"))
    home_b = tmp_path / "home-b"
    seed(home_b, (P2,))
    app_b = main.create_app(str(lightweight.root), auth=AuthSettings(token="key-b"))
    assert app_a.state.remote_catalog is not app_b.state.remote_catalog
    async with client_for(app_a) as a:
        owner_a = app_a.state.remote_catalog.runtime
        assert (await a.get(BASE, headers={"authorization": "Bearer key-a"})).json()["items"][0]["profile_id"] == P1["profile_id"]
        monkeypatch.setenv("VOXARIUM_STUDIO_HOME", str(home_b))
        async with client_for(app_b) as b:
            owner_b = app_b.state.remote_catalog.runtime
            assert owner_b is not owner_a
            error(await b.get(BASE, headers={"authorization": "Bearer key-a"}), 401, "studio_unauthorised")
            error(await a.get(BASE, headers={"authorization": "Bearer key-b"}), 401, "studio_unauthorised")
            assert (await b.get(BASE, headers={"authorization": "Bearer key-b"})).json()["items"][0]["profile_id"] == P2["profile_id"]
            # Changing the process home cannot make A silently read B.
            error(await a.get(BASE, headers={"authorization": "Bearer key-a"}), 409, "profile_recovery_required")
            monkeypatch.setenv("VOXARIUM_STUDIO_HOME", str(studio_home))
            assert (await a.get(BASE, headers={"authorization": "Bearer key-a"})).status_code == 200
        assert app_b.state.remote_catalog.runtime is None
        assert app_a.state.remote_catalog.runtime is owner_a
        assert (await a.get(BASE, headers={"authorization": "Bearer key-a"})).status_code == 200
    # An embedded host may reuse its app, but never its closed runtime.
    async with client_for(app_a) as a:
        assert app_a.state.remote_catalog.runtime is not owner_a
        assert (await a.get(BASE, headers={"authorization": "Bearer key-a"})).status_code == 200
    with pytest.raises(CatalogError, match="service_unavailable"):
        _ = owner_a.ready


@pytest.mark.parametrize("failure", ["runtime", "context", "workspace", "recent"])
async def test_startup_failures_release_holder_close_owner_and_clean_context(
    lightweight, studio_home, monkeypatch, failure,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root))
    owners, closed = [], []
    original_factory = main.create_remote_catalog_runtime

    def factory():
        owner = original_factory()
        owners.append(owner)
        original_start, original_close = owner.start, owner.close

        async def start():
            assert app.state.remote_catalog.runtime is owner  # bound before await
            await original_start()
            if failure == "runtime":
                raise RuntimeError("synthetic runtime startup failure")
            return owner.ready

        async def close():
            assert app.state.remote_catalog.runtime is None  # release before cleanup
            closed.append(owner)
            await original_close()

        owner.start, owner.close = start, close
        return owner

    monkeypatch.setattr(main, "create_remote_catalog_runtime", factory)
    if failure == "context":
        async def failed_start(ctx):
            ctx.started += 1
            raise RuntimeError("synthetic context startup failure")
        monkeypatch.setattr(ProjectStub, "startup", failed_start)
    elif failure == "workspace":
        def failed_workspace(*_args, **_kwargs):
            raise RuntimeError("synthetic workspace startup failure")
        monkeypatch.setattr(main, "initialise_workspace", failed_workspace)
    elif failure == "recent":
        def failed_recent(*_args, **_kwargs):
            raise RuntimeError("synthetic recent startup failure")
        monkeypatch.setattr(main, "record_recent", failed_recent)
    with pytest.raises(RuntimeError, match="synthetic .* startup failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("Failed startup must never admit the application")
    assert owners == closed and len(owners) == 1
    assert app.state.remote_catalog.runtime is None
    assert all(ctx.stopped == 1 for ctx in lightweight.contexts)
    with pytest.raises(CatalogError, match="service_unavailable"):
        _ = owners[0].ready


@pytest.mark.parametrize("stage", ["runtime", "context"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("close_failure", [False, True])
async def test_startup_exception_and_cancellation_keep_primary_error_and_close_owner(
    lightweight, studio_home, monkeypatch, stage, cancelled, close_failure,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root))
    primary = asyncio.CancelledError("synthetic cancel") if cancelled else RuntimeError("synthetic startup")
    owners, closed = [], []
    original_factory = main.create_remote_catalog_runtime

    def factory():
        owner = original_factory()
        owners.append(owner)
        original_start, original_close = owner.start, owner.close

        async def start():
            await original_start()
            if stage == "runtime":
                raise primary
            return owner.ready

        async def close():
            assert app.state.remote_catalog.runtime is None
            await original_close()
            closed.append(owner)
            if close_failure:
                raise ValueError("secondary close failure")

        owner.start, owner.close = start, close
        return owner

    async def failed_context_start(ctx):
        ctx.started += 1
        raise primary

    monkeypatch.setattr(main, "create_remote_catalog_runtime", factory)
    if stage == "context":
        monkeypatch.setattr(ProjectStub, "startup", failed_context_start)
    with pytest.raises(type(primary)) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("Startup must not yield")
    assert caught.value is primary
    assert len(owners) == 1 and owners == closed
    assert app.state.remote_catalog.runtime is None
    assert all(ctx.stopped == 1 for ctx in lightweight.contexts)
    with pytest.raises(CatalogError, match="service_unavailable"):
        _ = owners[0].ready


@pytest.mark.parametrize("stage", ["partial-workspace", "shutdown"])
async def test_partial_workspace_or_shutdown_failure_still_releases_all_owners(
    lightweight, studio_home, monkeypatch, stage,
):
    seed(studio_home)
    app = main.create_app(str(lightweight.root))
    owners = []
    original_factory = main.create_remote_catalog_runtime
    initialise, shutdown = main.initialise_workspace, main.shutdown_workspace
    primary = RuntimeError("synthetic workspace failure")

    def factory():
        owner = original_factory()
        owners.append(owner)
        return owner

    def partial(state, **kwargs):
        initialise(state, **kwargs)
        raise primary

    async def failed_shutdown(state):
        assert app.state.remote_catalog.runtime is None
        await shutdown(state)
        raise primary

    monkeypatch.setattr(main, "create_remote_catalog_runtime", factory)
    if stage == "partial-workspace":
        monkeypatch.setattr(main, "initialise_workspace", partial)
    else:
        monkeypatch.setattr(main, "shutdown_workspace", failed_shutdown)
    with pytest.raises(RuntimeError) as caught:
        async with app.router.lifespan_context(app):
            assert stage == "shutdown"
    assert caught.value is primary
    assert not app.state.project_contexts
    assert app.state.remote_catalog.runtime is None
    assert lightweight.contexts[0].stopped == 1
    with pytest.raises(CatalogError, match="service_unavailable"):
        _ = owners[0].ready


async def test_pending_at_startup_never_creates_a_workspace_or_usable_owner(
    lightweight, studio_home, monkeypatch,
):
    seed(studio_home)
    (studio_home / "remote-profile-change.json").write_text(CANARY, encoding="utf-8")
    app = main.create_app(str(lightweight.root))
    owners = []
    original_factory = main.create_remote_catalog_runtime

    def factory():
        owner = original_factory()
        owners.append(owner)
        return owner

    monkeypatch.setattr(main, "create_remote_catalog_runtime", factory)
    before = snapshot(studio_home)
    with pytest.raises(ProfileError, match="profile_recovery_required"):
        async with app.router.lifespan_context(app):
            pytest.fail("Pending startup must not yield")
    assert not lightweight.contexts and app.state.remote_catalog.runtime is None
    with pytest.raises(CatalogError, match="service_unavailable"):
        _ = owners[0].ready
    assert snapshot(studio_home) == before


async def test_real_project_selection_keeps_global_owner_and_both_contexts_alive(
    tmp_path, studio_home, monkeypatch,
):
    """Real create/start/shutdown + open_project, not assignment to state.ctx."""
    from voxarium_studio.core import config as config_module
    from voxarium_studio.core import context as context_module

    # This is a Remote/workspace test, not installed AI SDK discovery. Config
    # validation otherwise imports the SDK while this module's blanket process/
    # socket guards are active; a failed optional import can leave cached MCP
    # children without their parent attributes and poison later status tests.
    # Keep config validation, but supply its unrelated vocabulary explicitly.
    monkeypatch.setattr(config_module, "permission_modes",
                        lambda: ["default", "acceptEdits", "plan", "bypassPermissions"])
    def sdk_modules():
        return {name: module for name, module in sys.modules.items()
                if name in {"mcp", "claude_agent_sdk"}
                or name.startswith(("mcp.", "claude_agent_sdk."))}
    sdk_before = sdk_modules()

    # No plugin processes, watcher threads or external systems are needed to
    # prove resident AppContext/registry selection semantics (§5.1/§5.3).
    monkeypatch.setattr(context_module.plugin_loader, "load_all", lambda paths, _config: Registry(paths.root))
    monkeypatch.setattr(context_module.FileWatcher, "start", lambda *_: None)
    a, b = tmp_path / "project-a", tmp_path / "project-b"
    for root in (a, b):
        root.mkdir()
        init_project(root, with_samples=False)
    seed(studio_home)
    app = main.create_app(str(a), auth=AuthSettings(token="global-key"))
    headers = {"authorization": "Bearer global-key"}
    async with client_for(app) as client:
        owner = app.state.remote_catalog.runtime
        ctx_a = app.state.ctx
        assert isinstance(ctx_a, AppContext)
        before = (await client.get(BASE, headers=headers)).json()
        # Exercise the actual application project-open route and real workspace.
        opened = await client.post("/api/project/open", json={"path": str(b), "create": False}, headers=headers)
        assert opened.status_code == 200, opened.text
        ctx_b = app.state.ctx
        assert isinstance(ctx_b, AppContext) and ctx_b is not ctx_a
        assert app.state.remote_catalog.runtime is owner
        assert len(app.state.project_contexts) == 2
        assert (await client.get(BASE, headers=headers)).json() == before
        assert app.state.auth.token == ctx_a.auth.token == ctx_b.auth.token == "global-key"
        assert await workspace.open_project(app.state, a, create=False) is ctx_a
        assert app.state.remote_catalog.runtime is owner
        assert (await client.get(BASE, headers=headers)).json() == before
        # Both contexts' databases still answer after A->B->A; selecting never
        # retires either one or closes the app-owned catalog.
        for ctx in (ctx_a, ctx_b):
            async with ctx.db.session() as session:
                from sqlalchemy import text
                assert await session.scalar(text("SELECT 1")) == 1
    assert not app.state.project_contexts
    assert app.state.remote_catalog.runtime is None
    assert sdk_modules() == sdk_before
