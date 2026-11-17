"""VS-152 C9: mounted ASGI -> C8 -> durable Create, decoded fake wire only.

No application lifespan, listener, live keys or Remote is started. The actual
create_app holder/router is exercised without unrelated project startup. Run
with PYTHONDONTWRITEBYTECODE=1, -n0, -p no:cacheprovider and --basetemp beneath
D:\\VoxariumStudio\\.voxarium_studio\\temp\\TASK-152.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from voxarium_studio import main
from voxarium_studio.api.remote import create_remote_router
from voxarium_studio.build_info import BuildInfo
from voxarium_studio.core.application_config import RemoteServerProfile
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.sync import catalog_runtime, credentials
from voxarium_studio.sync.catalog import CatalogError, CatalogProject, CatalogResponse, CreateResult
from voxarium_studio.sync.catalog_create_runtime import CatalogCreateRuntime
from voxarium_studio.sync.catalog_create_service import CatalogCreateService
from voxarium_studio.sync.catalog_intents import CatalogIntentJournal
from voxarium_studio.sync.catalog_providers import PendingCatalogProviders, S2CatalogReaders
from voxarium_studio.sync.profile_lifecycle import BindingInventory, ProfileLifecycle
from voxarium_studio.sync.profiles import ProfileError


BASE = "/api/remote/servers"
PROFILE = "00000000-0000-4000-8000-000000000152"
OTHER = "00000000-0000-4000-8000-000000000153"
MISSING = "00000000-0000-4000-8000-000000000154"
URL = "https://create.invalid/deployment/prefix"
APP_KEY = "VS152-SYNTHETIC-STUDIO-KEY"
TOKEN = "VS152-SYNTHETIC-REMOTE-SECRET-NEVER-PERSIST"
CANARY = "VS152-UNTRUSTED-RESPONSE-CANARY"
OP = "VS152.explicit_command-1"
LABEL = "  Cafe\u0301 / Project  "
WIRE_LABEL = "Caf\u00e9 / Project"
JOURNAL = "remote-catalog-intents.sqlite3"
# Windows ACL/journal calls can queue behind other scoped verification runs.
DEADLINE = 15
PROJECT = dict(id="alpha", display_name=WIRE_LABEL,
               storage_epoch="0123456789abcdef0123456789abcdef",
               metadata_revision=1, created_at="2026-11-16T12:00:00Z")


def reply(body, status=200):
    return CatalogResponse(status, deepcopy(body))


def project_reply(status=201, **changes):
    return reply({"protocol": 2, "project": {**PROJECT, **changes}}, status)


def error_reply(status, code):
    # Both message/request_id are attacker-controlled, including secret echoes.
    return reply({"error": {"code": code, "message": TOKEN + CANARY,
                            "request_id": TOKEN + CANARY}}, status)


def command(**changes):
    return dict(expected_revision=1, id="alpha", display_name=LABEL,
                operation_id=OP) | changes


def create_path(profile_id=PROFILE):
    return f"{BASE}/{profile_id}/projects"


def observe_path(profile_id=PROFILE, operation_id=OP):
    return f"{create_path(profile_id)}/operations/{operation_id}/observe"


def secret_free(value):
    for secret in (TOKEN, CANARY, APP_KEY):
        assert secret not in value


def clean_exception(error):
    """Check actual retained chains, not only the rendered 'from None'."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        secret_free(str(current))
        secret_free(repr(current))
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)


def boundary(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": code, "details": {}}
    secret_free(response.text)


def outcome(response, state, code=None):
    assert response.status_code == (201 if state == "created" else 200), response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == dict(state=state, project=(PROJECT if state in {
        "created", "found"} else None), error_code=code)
    secret_free(response.text)


class Wire:
    def __init__(self):
        self.calls = []
        self.responses = {
            "/healthz": reply(dict(service="voxarium_studio_remote", version="synthetic-v2",
                                  protocol=2, supported_protocols=[2], catalog_api=2)),
            "/api/v2/me": reply(dict(protocol=2, catalog_api=2, capabilities=[
                "project_catalog", "explicit_project_create", "strict_connect"],
                user=dict(id="synthetic-user", display_name="Same", color="#3e63dd"))),
            "POST": project_reply(),
            "GET": project_reply(200),
        }
        self.hook = None

    async def request(self, method, url, *, headers, params, json, follow_redirects):
        path = url.removeprefix(URL)
        assert path in {"/healthz", "/api/v2/me", "/api/v2/projects",
                        "/api/v2/projects/alpha"}, (method, url)
        assert not params and follow_redirects is False
        expected_headers = {"Accept": "application/json", "Cache-Control": "no-store"}
        if path != "/healthz":
            expected_headers["Authorization"] = "Bearer " + TOKEN
        assert headers == expected_headers
        if method == "POST":
            assert path == "/api/v2/projects"
            assert json == {"id": "alpha", "display_name": WIRE_LABEL}
        else:
            assert method == "GET" and json is None
        self.calls.append((method, path, deepcopy(json)))
        if self.hook:
            await self.hook(method, path)
        result = self.responses.get(path, self.responses[method])
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def posts(self):
        return sum(method == "POST" for method, _, _ in self.calls)


class Harness:
    def __init__(self, home, monkeypatch):
        self.home, self.mp = home, monkeypatch
        self.counts, self.forbidden = Counter(), Counter()
        self.rows = {
            PROFILE: RemoteServerProfile(profile_id=PROFILE, name="Same", url=URL, revision=1),
            OTHER: RemoteServerProfile(profile_id=OTHER, name="Same",
                                      url="https://other.invalid/prefix", revision=3),
        }
        self.token, self.check_error = TOKEN, None
        self.wire, self.runtimes = Wire(), []
        self.runtime = self.make()
        self.app = self.mount(self.runtime)

    def deny(self, category):
        def denied(*_args, **_kwargs):
            self.forbidden[category] += 1
            raise AssertionError("C9 forbidden " + category)
        return denied

    def check(self):
        self.counts["check"] += 1
        if self.check_error:
            raise ProfileError(self.check_error)

    def read(self, profile_id):
        self.counts["profile"] += 1
        if profile_id not in self.rows:
            raise ProfileError("profile_not_found")
        return self.rows[profile_id]

    def key(self, profile_id, url):
        self.counts["key"] += 1
        assert self.rows[profile_id].url == url
        return self.token

    def providers(self, profile_id, *, expected_revision):
        self.counts["providers"] += 1
        row = self.read(profile_id)
        readers = S2CatalogReaders(self.home, self.read, self.key,
            lambda: self.home / "config.yaml", lambda: self.home / "remote-keys.json",
            lambda: self.home / "remote-profile-change.json")
        return PendingCatalogProviders(readers, profile_id=profile_id,
            expected_url=row.url, expected_revision=expected_revision)

    def make(self, *, transport=True):
        lifecycle = ProfileLifecycle(profile_reader=self.read,
            inventory=lambda _: BindingInventory(state="known", drained=True))
        runtime = CatalogCreateRuntime(home=self.home, check=self.check,
            providers=self.providers, lifecycle=lifecycle,
            transport=self.wire if transport else None)
        self.runtimes.append(runtime)
        return runtime

    def mount(self, creation):
        # Production factory and real holder, but NO lifespan/project startup.
        app = main.create_app(str(self.home / "never-opened-project"),
                              auth=AuthSettings(token=APP_KEY))

        class UntouchableProject:
            def __getattribute__(_self, _name):
                return self.deny("selected_project")()

        app.state.ctx = UntouchableProject()
        ready = SimpleNamespace(create=creation, metadata=SimpleNamespace(list=lambda: ()),
            profiles=SimpleNamespace(get=self.read),
            verification=SimpleNamespace(verify=self.deny("verify")),
            catalog=SimpleNamespace(list_projects=self.deny("list")))
        app.state.remote_catalog.runtime = SimpleNamespace(ready=ready)
        return app

    def client(self, *, app=None, authenticated=True):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.app),
            base_url="http://127.0.0.1", trust_env=False,
            headers={"Authorization": "Bearer " + APP_KEY} if authenticated else {})

    async def create(self, *, client=None, profile_id=PROFILE, body=None, **kwargs):
        if client is not None:
            return await client.post(create_path(profile_id),
                                     json=command() if body is None else body, **kwargs)
        async with self.client() as client:
            return await self.create(client=client, profile_id=profile_id, body=body, **kwargs)

    async def observe(self, *, profile_id=PROFILE, operation_id=OP, body=None):
        async with self.client() as client:
            return await client.post(observe_path(profile_id, operation_id),
                json={"expected_revision": 1} if body is None else body)

    def record(self):
        return CatalogIntentJournal(self.home / JOURNAL).lookup(OP)


@pytest.fixture
async def h(tmp_path, monkeypatch, caplog):
    home = tmp_path / "synthetic-home"
    home.mkdir()
    monkeypatch.setenv("VOXARIUM_STUDIO_HOME", str(home))
    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)
    monkeypatch.setattr(main, "build_info", lambda: BuildInfo(version="test", commit="test"))
    monkeypatch.setattr(main, "static_dir", lambda: None)
    fixture = Harness(home, monkeypatch)
    # The asyncio loop already exists; only in-process ASGI I/O is permitted.
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, fixture.deny("network"))
    for name in ("getaddrinfo", "gethostbyname", "gethostbyaddr", "create_connection"):
        monkeypatch.setattr(socket, name, fixture.deny("network"))
    monkeypatch.setattr(subprocess, "Popen", fixture.deny("process"))
    for name in ("load_token", "load_profile_token", "save_token", "save_profile_token"):
        monkeypatch.setattr(credentials, name, fixture.deny("real_credentials"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fixture.deny("http"))
    yield fixture
    for runtime in reversed(fixture.runtimes):
        await asyncio.wait_for(runtime.close(), timeout=DEADLINE)
    assert not fixture.forbidden, dict(fixture.forbidden)
    assert not (home / "never-opened-project").exists()
    assert not (home / "never-opened-project").exists()
    secret_free(caplog.text)
    for path in home.glob(JOURNAL + "*"):
        if path.is_file():
            raw = path.read_bytes()
            assert TOKEN.encode() not in raw and TOKEN.encode("utf-16-le") not in raw
            for digest in (hashlib.sha256, hashlib.sha1, hashlib.md5):
                assert digest(TOKEN.encode()).digest() not in raw
                assert digest(TOKEN.encode()).hexdigest().encode() not in raw


POST_CASES = [
    (project_reply(), "created", None),
    *[(error_reply(400, code), "rejected", code) for code in (
        "invalid_id", "invalid_display_name", "invalid_request")],
    (error_reply(401, "unauthorised"), "rejected", "unauthorised"),
    (error_reply(404, "project_not_found"), "rejected", "project_not_found"),
    (error_reply(409, "project_exists"), "conflict", "project_exists"),
    *[(error_reply(409, code), "rejected", code) for code in (
        "metadata_conflict", "snapshot_changed")],
    (error_reply(413, "payload_too_large"), "rejected", "payload_too_large"),
    (error_reply(503, "temporarily_unavailable"), "unknown", "temporarily_unavailable"),
    *[(project_reply(status), "unknown", "remote_error") for status in (200, 204, 500)],
    (reply(None, 201), "unknown", "invalid_response"),
    (project_reply(id="different"), "unknown", "invalid_response"),
    (reply({"protocol": 1, "project": PROJECT}, 201), "unknown", "version_mismatch"),
    (project_reply(metadata_revision=True), "unknown", "invalid_response"),
    (reply({"protocol": 2, "project": PROJECT, "extra": CANARY}, 201),
     "unknown", "invalid_response"),
    (OSError(TOKEN + CANARY), "unknown", "transport_unavailable"),
    (reply(None, 302), "unknown", "redirect_refused"),
    (reply(None, 307), "unknown", "redirect_refused"),
]


@pytest.mark.parametrize("remote,state,code", POST_CASES)
async def test_post_status_matrix_and_exact_live_replay(h, remote, state, code):
    h.wire.responses["POST"] = remote
    response = await h.create()
    outcome(response, state, code)
    assert h.wire.calls == [("GET", "/healthz", None), ("GET", "/api/v2/me", None),
                            ("POST", "/api/v2/projects", {"id": "alpha", "display_name": WIRE_LABEL})]
    record = h.record()
    assert record.state == state
    assert record.intent.display_name == LABEL  # Raw input persisted, not wire normalization.
    calls = list(h.wire.calls)
    replay = await h.create()
    assert replay.status_code == response.status_code and replay.json() == response.json()
    assert h.wire.calls == calls and h.wire.posts == 1
    assert isinstance(next(iter(h.runtime._services.values()))[1], CatalogCreateService)


@pytest.mark.parametrize("status", [400, 401, 404, 409, 413])
@pytest.mark.parametrize("body", [None, {}, {"error": {}},
    {"error": {"code": CANARY, "message": TOKEN, "request_id": CANARY}},
    {"error": {"code": "temporarily_unavailable", "message": TOKEN, "request_id": CANARY}},
    {"error": {"code": "project_exists", "message": TOKEN, "request_id": CANARY}, "extra": CANARY}])
async def test_malformed_or_mismatched_post_errors_are_fixed_rejection(h, status, body):
    h.wire.responses["POST"] = reply(body, status)
    outcome(await h.create(), "rejected", "remote_error")
    assert h.wire.posts == 1 and h.record().state == "rejected"


@pytest.mark.parametrize("initial", [error_reply(409, "project_exists"),
    error_reply(400, "invalid_request"), error_reply(503, "temporarily_unavailable"), project_reply()])
@pytest.mark.parametrize("found", [True, False])
async def test_explicit_observe_reads_only_original_id_and_keeps_reservation(h, initial, found):
    h.wire.responses["POST"] = initial
    await h.create()
    before = len(h.wire.calls)
    h.wire.responses["GET"] = project_reply(200) if found else error_reply(404, "project_not_found")
    state, code = ("found", None) if found else ("absent", "project_not_found")
    outcome(await h.observe(), state, code)
    assert h.wire.calls[before:] == [("GET", "/healthz", None), ("GET", "/api/v2/me", None),
                                     ("GET", "/api/v2/projects/alpha", None)]
    assert h.record().state == state
    calls = list(h.wire.calls)
    outcome(await h.create(), state, code)
    assert h.wire.calls == calls and h.wire.posts == 1
    boundary(await h.create(body=command(id="corrected")), 409, "operation_conflict")
    assert h.wire.calls == calls


@pytest.mark.parametrize("phase", ["/healthz", "/api/v2/me"])
@pytest.mark.parametrize("status,remote_code,code", [
    (401, "unauthorised", "remote_unauthorised"),
    (404, "project_not_found", "project_not_found"),
    (503, "temporarily_unavailable", "temporarily_unavailable")])
@pytest.mark.parametrize("observe", [False, True])
async def test_health_me_failures_are_boundary_not_post_or_observation_outcomes(
        h, phase, status, remote_code, code, observe):
    if observe:
        await h.create()
    before = h.wire.posts
    h.wire.responses[phase] = error_reply(status, remote_code)
    boundary(await (h.observe() if observe else h.create()), 502, code)
    assert h.wire.posts == before
    assert h.wire.calls[-1][:2] == ("GET", phase)
    assert h.record().state == "unknown"


@pytest.mark.parametrize("phase,changes,code", [
    ("/healthz", {"protocol": 1}, "version_mismatch"),
    ("/healthz", {"catalog_api": 1}, "version_mismatch"),
    ("/healthz", {"supported_protocols": [1]}, "version_mismatch"),
    ("/api/v2/me", {"protocol": 1}, "version_mismatch"),
    ("/api/v2/me", {"catalog_api": 1}, "version_mismatch"),
    *[("/api/v2/me", {"capabilities": [capability for capability in (
        "project_catalog", "explicit_project_create", "strict_connect") if capability != missing]},
       "capabilities_missing") for missing in (
        "project_catalog", "explicit_project_create", "strict_connect")],
])
@pytest.mark.parametrize("observe", [False, True])
async def test_v1_or_missing_preflight_capability_never_authorizes_post_or_lookup(
        h, phase, changes, code, observe):
    if observe:
        await h.create()
    before, posts = len(h.wire.calls), h.wire.posts
    original = h.wire.responses[phase]
    h.wire.responses[phase] = reply({**original.body, **changes})
    boundary(await (h.observe() if observe else h.create()), 502, code)
    expected = [("GET", "/healthz", None)]
    if phase == "/api/v2/me":
        expected.append(("GET", "/api/v2/me", None))
    assert h.wire.calls[before:] == expected
    assert h.wire.posts == posts == (1 if observe else 0)
    assert h.record().state == "unknown"


@pytest.mark.parametrize("remote,code", [
    (error_reply(401, "unauthorised"), "remote_unauthorised"),
    (error_reply(409, "metadata_conflict"), "metadata_conflict"),
    (error_reply(409, "snapshot_changed"), "snapshot_changed"),
    (error_reply(413, "payload_too_large"), "payload_too_large"),
    (error_reply(503, "temporarily_unavailable"), "temporarily_unavailable"),
    (reply(None, 404), "remote_error"),
    (project_reply(200, id="another"), "identity_mismatch"),
    (reply(None), "invalid_response"),
    (OSError(TOKEN + CANARY), "transport_unavailable"),
])
async def test_observe_other_error_or_invalid_response_is_boundary(h, remote, code):
    await h.create()
    h.wire.responses["GET"] = remote
    boundary(await h.observe(), 502, code)
    assert h.wire.posts == 1 and h.record().state == "unknown"


@pytest.mark.parametrize("changes", [dict(id="other"), dict(display_name=WIRE_LABEL),
                                     dict(expected_revision=2)])
async def test_replay_changes_conflict_before_credentials(h, changes):
    h.wire.responses["POST"] = error_reply(400, "invalid_request")
    outcome(await h.create(), "rejected", "invalid_request")
    counts, calls = h.counts.copy(), list(h.wire.calls)
    boundary(await h.create(body=command(**changes)), 409, "operation_conflict")
    for category in ("key", "profile", "providers"):
        assert h.counts[category] == counts[category]
    assert h.wire.calls == calls
    assert h.record().state == "rejected"


async def test_other_profile_conflicts_before_credentials_and_cannot_observe(h):
    await h.create()
    counts, calls = h.counts.copy(), list(h.wire.calls)
    boundary(await h.create(profile_id=OTHER), 409, "operation_conflict")
    boundary(await h.observe(profile_id=OTHER, body={"expected_revision": 3}),
             409, "operation_conflict")
    for category in ("key", "profile", "providers"):
        assert h.counts[category] == counts[category]
    assert h.wire.calls == calls


@pytest.mark.parametrize("state", ["created", "rejected", "unknown", "conflict", "found", "absent"])
@pytest.mark.parametrize("observe", [False, True])
async def test_cold_repeat_and_observe_refuse_before_key_transport_or_profile(h, state, observe):
    if state == "rejected":
        h.wire.responses["POST"] = error_reply(400, "invalid_request")
    elif state == "unknown":
        h.wire.responses["POST"] = error_reply(503, "temporarily_unavailable")
    elif state == "conflict":
        h.wire.responses["POST"] = error_reply(409, "project_exists")
    await h.create()
    if state in {"found", "absent"}:
        h.wire.responses["GET"] = project_reply(200) if state == "found" else error_reply(404, "project_not_found")
        await h.observe()
    assert h.record().state == state
    await h.runtime.close()
    h.runtime = h.make()
    h.app = h.mount(h.runtime)
    counts, calls = h.counts.copy(), list(h.wire.calls)
    boundary(await (h.observe() if observe else h.create()), 409, "credential_binding_unproven")
    for category in ("key", "profile", "providers"):
        assert h.counts[category] == counts[category]
    assert h.wire.calls == calls and h.record().state == state


INVALID_CREATE = [
    *[{field: value} for field in ("id", "display_name", "operation_id")
      for value in (None, False, 1, [], {})],
    *[{"expected_revision": value} for value in (None, False, True, 0, -1, 1.0, "1", [])],
    *[{"id": value} for value in ("", "Alpha", "../alpha", "a/b", "a%2fb", "a" * 65, "\u0430")],
    *[{"display_name": value} for value in ("", "  ", "x" * 121, "a\n", "a\x7f", "a\x85",
        "a\u2028", "a\u2029", "a\u202e", "a\u2066", "\ud800")],
    *[{"operation_id": value} for value in ("", "a" * 129, "op/a", "op a", "op%2f", "\u0430")],
    {"token": TOKEN}, {"url": URL}, {"extra": CANARY},
]


@pytest.mark.parametrize("changes", INVALID_CREATE)
async def test_create_strict_input_rejects_before_dispatch_without_reserving(h, changes):
    body = command(**changes)
    # JSON's ASCII escape form also permits exercising lone-surrogate validation.
    import json
    async with h.client() as client:
        response = await client.post(create_path(), content=json.dumps(body),
                                     headers={"content-type": "application/json"})
    boundary(response, 422, "invalid_request")
    assert not h.counts and not h.wire.calls and not (h.home / JOURNAL).exists()


@pytest.mark.parametrize("field", ["id", "display_name", "operation_id", "expected_revision"])
async def test_create_requires_every_field(h, field):
    body = command()
    del body[field]
    boundary(await h.create(body=body), 422, "invalid_request")
    assert not h.counts and not h.wire.calls and not (h.home / JOURNAL).exists()


@pytest.mark.parametrize("body", [{}, {"expected_revision": True}, {"expected_revision": "1"},
    {"expected_revision": 1.0}, {"expected_revision": 0}, {"expected_revision": 1, "id": "alpha"},
    {"expected_revision": 1, "url": URL}, {"expected_revision": 1, "token": TOKEN},
    {"expected_revision": 1, "operation_id": OP}, {"expected_revision": 1, "extra": CANARY}])
async def test_observe_strict_body_is_not_arbitrary_lookup(h, body):
    boundary(await h.observe(body=body), 422, "invalid_request")
    assert not h.counts and not h.wire.calls and not (h.home / JOURNAL).exists()


@pytest.mark.parametrize("observe", [False, True])
@pytest.mark.parametrize("query", ["?extra=x", "?expected_revision=1", "?cursor=alpha",
    "?id=other", "?operation_id=other", "?token=" + TOKEN, "?extra=a&extra=b"])
async def test_no_query_parameters_for_commands(h, observe, query):
    async with h.client() as client:
        response = await client.post((observe_path() if observe else create_path()) + query,
            json={"expected_revision": 1} if observe else command())
    boundary(response, 422, "invalid_request")
    assert not h.counts and not h.wire.calls and not (h.home / JOURNAL).exists()


@pytest.mark.parametrize("profile_id", ["not-a-uuid", PROFILE.replace("-", ""), PROFILE.upper(), "{" + PROFILE + "}"])
@pytest.mark.parametrize("observe", [False, True])
async def test_noncanonical_profile_paths_rejected_before_dispatch(h, profile_id, observe):
    # This UUID has no alphabetic digits; use a truly uppercase alternate UUID.
    if profile_id == PROFILE.upper():
        profile_id = "ABCDEFAB-0000-4000-8000-000000000152"
    response = await (h.observe(profile_id=profile_id) if observe else h.create(profile_id=profile_id))
    boundary(response, 422, "invalid_request")
    assert not h.counts and not h.wire.calls


@pytest.mark.parametrize("operation_id", ["space op", "colon:op", "\u0430", "x" * 129])
async def test_observe_operation_path_alphabet_rejected_before_dispatch(h, operation_id):
    boundary(await h.observe(operation_id=operation_id), 422, "invalid_request")
    assert not h.counts and not h.wire.calls and not (h.home / JOURNAL).exists()


@pytest.mark.parametrize("observe", [False, True])
@pytest.mark.parametrize("body", ["null", "[]", "{}", "{", '"string"'])
async def test_malformed_or_nonobject_json_body(h, observe, body):
    async with h.client() as client:
        response = await client.post(observe_path() if observe else create_path(), content=body,
                                     headers={"content-type": "application/json"})
    boundary(response, 422, "invalid_request")
    assert not h.counts and not h.wire.calls


@pytest.mark.parametrize("observe", [False, True])
@pytest.mark.parametrize("kind,status,code", [("missing", 401, "studio_unauthorised"),
    ("wrong", 401, "studio_unauthorised"), ("origin", 403, "studio_forbidden")])
async def test_studio_auth_and_origin_are_no_store_and_precede_dispatch(h, observe, kind, status, code):
    async with h.client(authenticated=kind == "origin") as client:
        headers = ({"authorization": "Bearer " + CANARY} if kind == "wrong" else
                   {"origin": "https://attacker.invalid"} if kind == "origin" else {})
        response = await client.post(observe_path() if observe else create_path(),
            json={"expected_revision": 1} if observe else command(), headers=headers)
    boundary(response, status, code)
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"
    assert not h.counts and not h.wire.calls and not (h.home / JOURNAL).exists()


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
async def test_observe_wrong_method_never_dispatches(h, method):
    async with h.client() as client:
        response = await client.request(method, observe_path(), json={"expected_revision": 1})
    boundary(response, 405, "method_not_allowed")
    assert not h.counts and not h.wire.calls


async def test_concurrent_same_operation_sends_at_most_one_post(h):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(method, _path):
        if method == "POST":
            entered.set()
            await release.wait()

    h.wire.hook = hold
    first = asyncio.create_task(h.create())
    try:
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE)
        repeated = await asyncio.wait_for(h.create(), timeout=DEADLINE)
        assert repeated.status_code in {200, 409}
        if repeated.status_code == 200:
            outcome(repeated, "unknown", "create_unknown")
        else:
            boundary(repeated, 409, "operation_in_progress")
        boundary(await h.observe(), 409, "operation_in_progress")
        assert h.wire.posts == 1
    finally:
        release.set()
        outcome(await asyncio.wait_for(first, timeout=DEADLINE), "created")
    outcome(await h.create(), "created")
    assert h.wire.posts == 1


async def test_cancelled_http_request_keeps_reservation_and_does_not_retry(h):
    entered = asyncio.Event()

    async def hold(method, _path):
        if method == "POST":
            entered.set()
            await asyncio.Event().wait()

    h.wire.hook = hold
    task = asyncio.create_task(h.create())
    try:
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=DEADLINE)
    assert h.record().state == "unknown" and h.wire.posts == 1
    calls = list(h.wire.calls)
    outcome(await h.create(), "unknown", "create_unknown")
    assert h.wire.calls == calls


async def test_missing_profile_operation_and_stale_cas_are_typed(h):
    boundary(await h.create(profile_id=MISSING), 404, "profile_not_found")
    boundary(await h.observe(operation_id="never-reserved"), 404, "operation_not_found")
    boundary(await h.create(body=command(expected_revision=2, operation_id="stale")),
             409, "profile_revision_conflict")
    assert h.counts["key"] == 0 and not h.wire.calls


@pytest.mark.parametrize("observe", [False, True])
async def test_live_credential_binding_change_is_typed_409(h, observe):
    await h.create()
    h.token = "VS152-DIFFERENT-SYNTHETIC-KEY"
    before = list(h.wire.calls)
    boundary(await (h.observe() if observe else h.create()), 409, "credential_binding_conflict")
    assert h.wire.calls == before and h.record().state == "created"


@pytest.mark.parametrize("phase", ["/healthz", "/api/v2/me", "/api/v2/projects"])
async def test_post_await_binding_failure_is_not_mapped_to_a_result(h, phase):
    async def mutate(_method, path):
        if path == phase:
            h.token = "VS152-DIFFERENT-SYNTHETIC-KEY"

    h.wire.hook = mutate
    boundary(await h.create(), 409, "credential_binding_conflict")
    assert h.wire.posts == (1 if phase == "/api/v2/projects" else 0)
    assert h.record().state == "unknown"


@pytest.mark.parametrize("observe", [False, True])
async def test_corrupt_journal_is_503_preserved_without_keys_or_network(h, observe):
    raw = b"VS152 deliberately invalid journal; never repair"
    (h.home / JOURNAL).write_bytes(raw)
    boundary(await (h.observe() if observe else h.create()), 503, "intent_store_invalid")
    assert (h.home / JOURNAL).read_bytes() == raw
    assert h.counts["key"] == 0 and not h.wire.calls


async def test_final_journal_failure_is_503_not_a_created_envelope(h, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError(TOKEN + CANARY)

    monkeypatch.setattr(CatalogIntentJournal, "record_state", fail)
    boundary(await h.create(), 503, "intent_store_unavailable")
    assert h.wire.posts == 1 and h.record().state == "reserved"
    before = list(h.wire.calls)
    boundary(await h.create(), 503, "intent_store_unavailable")
    assert h.wire.calls == before


async def test_production_r3_gate_uses_ready_create_before_key_network_or_journal(h, monkeypatch):
    class UnusedPool:
        async def aclose(self):
            pass

    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport", UnusedPool)
    monkeypatch.setattr(CatalogIntentJournal, "__init__", h.deny("journal"))
    production = catalog_runtime.RemoteCatalogRuntime()
    monkeypatch.setattr(production, "_token", h.deny("production_key"))
    try:
        ready = await asyncio.wait_for(production.start(), timeout=DEADLINE)
        assert type(ready.create) is CatalogCreateRuntime
        h.app.state.remote_catalog.runtime = production
        boundary(await h.create(), 503, "service_unavailable")
        boundary(await h.observe(), 503, "service_unavailable")
        assert not h.wire.calls and not (h.home / JOURNAL).exists()
        assert not h.counts
    finally:
        await asyncio.wait_for(production.close(), timeout=DEADLINE)


async def test_holder_not_ready_and_shutdown_remain_503(h):
    h.app.state.remote_catalog.runtime = None
    boundary(await h.create(), 503, "service_unavailable")
    boundary(await h.observe(), 503, "service_unavailable")
    assert not h.counts and not (h.home / JOURNAL).exists()
    h.app = h.mount(h.runtime)
    await h.runtime.close()
    boundary(await h.create(), 503, "service_unavailable")
    boundary(await h.observe(), 503, "service_unavailable")


async def test_router_creation_is_explicitly_opt_in(h):
    app = FastAPI()
    app.state.auth = AuthSettings(token=APP_KEY)
    unavailable = SimpleNamespace(verify=h.deny("verify"), list_projects=h.deny("list"))
    app.include_router(create_remote_router(metadata=SimpleNamespace(list=lambda: ()),
        profiles=SimpleNamespace(get=h.read), verification=unavailable, catalog=unavailable))
    async with h.client(app=app) as client:
        assert (await client.post(create_path(), json=command())).status_code == 405
        assert (await client.post(observe_path(), json={"expected_revision": 1})).status_code == 404
    assert not h.counts and not (h.home / JOURNAL).exists()


@pytest.mark.parametrize("state", ["unknown", "rejected"])
async def test_forged_returned_code_is_sanitized_not_echoed(h, state):
    class Forged:
        async def create_project(self, *_args, **_kwargs):
            return CreateResult(state, error_code=TOKEN + CANARY)

    h.app = h.mount(Forged())
    outcome(await h.create(), state, "remote_error")


@pytest.mark.parametrize("value", [None, {"state": "created"},
    CreateResult("bogus"), CreateResult("created"),
    CreateResult("conflict", CatalogProject(**PROJECT), "project_exists"),
    CreateResult("rejected", CatalogProject(**PROJECT), "invalid_request"),
    CreateResult("found", CatalogProject(**PROJECT), "invalid_request"),
    CreateResult("absent", error_code="wrong"),
    CreateResult("created", replace(CatalogProject(**PROJECT), id="other")),
    CreateResult("created", replace(CatalogProject(**PROJECT), metadata_revision=True)),
])
async def test_malformed_provider_result_is_boundary_not_false_success(h, value):
    class Forged:
        async def create_project(self, *_args, **_kwargs):
            return value

    h.app = h.mount(Forged())
    expected = "identity_mismatch" if (isinstance(value, CreateResult)
        and value.project is not None and value.project.id == "other") else "invalid_response"
    boundary(await h.create(), 502, expected)


@pytest.mark.parametrize("raised,status,code", [
    (CatalogError("intent_store_unavailable"), 503, "intent_store_unavailable"),
    (CatalogError("credential_binding_unproven"), 409, "credential_binding_unproven"),
    (CatalogError("operation_not_found"), 404, "operation_not_found"),
    (CatalogError("unauthorised"), 502, "remote_unauthorised"),
    (CatalogError(TOKEN + CANARY), 503, "service_unavailable"),
    (ValueError(TOKEN + CANARY), 503, "service_unavailable"),
])
@pytest.mark.parametrize("observe", [False, True])
async def test_boundary_exceptions_never_become_returned_result(h, raised, status, code, observe):
    class Failing:
        async def create_project(self, *_args, **_kwargs):
            raise raised

        async def recover_create(self, *_args, **_kwargs):
            raise raised

    h.app = h.mount(Failing())
    boundary(await (h.observe() if observe else h.create()), status, code)


@pytest.mark.parametrize("phase", ["/healthz", "/api/v2/me"])
async def test_real_preflight_exception_retains_no_secret_in_repr_or_chain(h, phase):
    seen = []

    class Capturing:
        async def create_project(self, *args, **kwargs):
            try:
                return await h.runtime.create_project(*args, **kwargs)
            except CatalogError as error:
                seen.append(error)
                raise

    h.wire.responses[phase] = OSError(TOKEN + CANARY)
    h.app = h.mount(Capturing())
    boundary(await h.create(), 502, "transport_unavailable")
    assert len(seen) == 1
    clean_exception(seen[0])
    assert h.wire.posts == 0 and h.record().state == "unknown"


@pytest.mark.parametrize("field", ["display_name", "created_at"])
async def test_successful_remote_secret_echo_is_boundary_not_success(h, field):
    h.wire.responses["POST"] = project_reply(**{field: TOKEN})
    boundary(await h.create(), 502, "invalid_response")
    assert h.wire.posts == 1 and h.record().state == "unknown"
