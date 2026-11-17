"""VS-139 C6: mounted production HTTP -> real synthetic S2 create/name-only CAS.

No listener, Remote server, real key or network. Only unrelated project startup
and the owned HTTP pool are stubbed; create/rename and presence readers stay real.
Run -n0 -p no:cacheprovider --basetemp in .voxarium_studio/temp/TASK-139.
"""
from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import yaml

from voxarium_studio import main
from voxarium_studio.api import remote_metadata
from voxarium_studio.build_info import BuildInfo
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.sync import catalog_runtime, credentials, profiles as profile_store
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.profiles import RemoteProfiles
from voxarium_studio.ws.bus import EventBus


BASE = "/api/remote/servers"
APP_KEY = "SYNTHETIC-STUDIO-AUTH-C6-139"
KEY = "SYNTHETIC-REMOTE-KEY-C6-139"
CANARY = "SYNTHETIC-UNTRUSTED-PAYLOAD-C6-139"
P1 = dict(profile_id="11111111-1111-4111-8111-111111111139", name="same",
          url="https://one.invalid/deployment/prefix", revision=3)
P2 = dict(profile_id="22222222-2222-4222-8222-222222222139", name="same",
          url="https://two.invalid/other/prefix", revision=8)
CREATE = dict(name="same", url="https://new.invalid/prefix")
PATCH = dict(expected_revision=3, name="renamed")
OPAQUE = {"future_global": {"nested": [1, None, {"retain": True}]},
          "theme": {"mode": "dark", "future_palette": ["opaque"]},
          "language": "ru", "respond_language": "Russian", "show_data_dir": True,
          "node_cache_enabled": False, "updates": {"auto_check": False}}


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def config(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def secret_free(value):
    for secret in (KEY, CANARY, APP_KEY):
        assert secret not in value


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": code, "details": {}}
    secret_free(response.text)


def success(response, status, row, *, source="none"):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {**row, "has_token": source != "none",
                               "credential_source": source,
                               "verification": {"state": "unavailable"}}
    secret_free(response.text)


async def mutate(client, operation, *, body=None, **kwargs):
    if operation == "create":
        return await client.post(BASE, json=CREATE if body is None else body, **kwargs)
    return await client.patch(BASE + "/" + P1["profile_id"],
                              json=PATCH if body is None else body, **kwargs)


@asynccontextmanager
async def client_for(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1", trust_env=False,
                                     headers={"authorization": "Bearer " + APP_KEY}) as client:
            yield client


@pytest.fixture
async def harness(monkeypatch, studio_home, tmp_path):
    counts = Counter()

    def denied(*_args, **_kwargs):
        counts["forbidden"] += 1
        raise AssertionError("C6 attempted network/process/credential/consumer mutation")

    # The async fixture begins after Windows' event-loop wakeup socket exists.
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, denied)
    for name in ("getaddrinfo", "gethostbyname", "create_connection"):
        monkeypatch.setattr(socket, name, denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    for name in ("change_url", "delete", "recover", "load_token", "_consumers",
                 "set_credential", "clear_credential", "import_legacy_credential"):
        if hasattr(RemoteProfiles, name):
            monkeypatch.setattr(RemoteProfiles, name, denied)
    for name in ("load_token", "load_profile_token", "save_token", "save_profile_token",
                 "import_legacy_token"):
        monkeypatch.setattr(credentials, name, denied)
    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)

    for name in ("create", "rename"):
        original = getattr(RemoteProfiles, name)

        def counted(self, *args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(RemoteProfiles, name, counted)
    original_write = profile_store._write_application_config

    def publish(*args, **kwargs):
        counts["publication"] += 1
        return original_write(*args, **kwargs)

    monkeypatch.setattr(profile_store, "_write_application_config", publish)
    monkeypatch.setattr(main, "build_info", lambda: BuildInfo(version="test", commit="test"))
    monkeypatch.setattr(main, "static_dir", lambda: None)
    monkeypatch.setattr(main, "record_recent", lambda *_: None)

    class Project:
        def __init__(self, root, auth):
            self.paths = ProjectPaths.for_project(Path(root))
            self.auth, self.bus = auth, EventBus()
            self.config = SimpleNamespace(project_name=None)
            self.sync = SimpleNamespace(generation=41, reconnect=denied, connect=denied,
                                        stop=denied, start=denied)

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

    class Pool(httpx.AsyncBaseTransport):
        closed = 0

        async def handle_async_request(self, request):
            return denied()

        async def aclose(self):
            self.closed += 1

    pool = Pool()
    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport",
                        lambda: CatalogHttpTransport(transport=pool))
    (studio_home / "config.yaml").write_text(
        json.dumps({**OPAQUE, "remote_servers": [P1, P2]}), encoding="utf-8")
    (studio_home / "remote-keys.json").write_text(json.dumps({
        "version": 2, "profiles": {p["profile_id"]: {
            "url": p["url"], "token": KEY, "revision": 2} for p in (P1, P2)},
        "legacy": {"https://legacy.invalid": KEY}}), encoding="utf-8")
    root = tmp_path / "project"
    data = root / ".voxarium_studio"
    data.mkdir(parents=True)
    (data / "config.yaml").write_text("theme:\n  accent: '#aabbcc'\nremote:\n  space: legacy\n",
                                     encoding="utf-8")
    (data / "app.db").write_bytes(b"SYNTHETIC-UNCHANGED-PROJECT-DATA")
    app = main.create_app(str(root), auth=AuthSettings(token=APP_KEY))
    yield SimpleNamespace(app=app, home=studio_home, root=root, pool=pool, counts=counts)
    assert counts["forbidden"] == 0
    assert pool.closed == 1


@pytest.mark.parametrize("raw,normalized", [
    ("  WSS://NEW.INVALID/deployment/prefix///  ", "https://new.invalid/deployment/prefix"),
    ("New.Invalid/prefix/", "https://new.invalid/prefix"),
    ("ws://LOCALHOST:8787/prefix/", "http://localhost:8787/prefix"),
    ("https://[::1]:8787/prefix/", "https://[::1]:8787/prefix"),
])
async def test_create_normalizes_s2_url_and_allows_duplicate_labels(harness, raw, normalized):
    h = harness
    before_project = snapshot(h.root)
    key_before = snapshot(h.home)["remote-keys.json"]
    opaque = {k: v for k, v in config(h.home).items() if k != "remote_servers"}
    async with client_for(h.app) as client:
        response = await mutate(client, "create", body={"name": "  same  ", "url": raw})
        row = {k: response.json()[k] for k in ("profile_id", "name", "url", "revision")}
        assert str(UUID(row["profile_id"])) == row["profile_id"]
        assert row["profile_id"] not in {P1["profile_id"], P2["profile_id"]}
        assert row == {**row, "name": "same", "url": normalized, "revision": 1}
        success(response, 201, row)
    saved = config(h.home)
    assert saved["remote_servers"] == [P1, P2, row]
    assert {k: v for k, v in saved.items() if k != "remote_servers"} == opaque
    assert snapshot(h.root) == before_project
    assert snapshot(h.home)["remote-keys.json"] == key_before
    assert h.counts == Counter(create=1, publication=1)
    assert not (h.home / "remote-profile-change.json").exists()


async def test_normalized_url_collision_is_409_and_never_publishes(harness):
    h = harness
    before = (h.home / "config.yaml").read_bytes()
    async with client_for(h.app) as client:
        error(await mutate(client, "create", body={"name": "different label",
                    "url": "WSS://ONE.INVALID/deployment/prefix///"}),
              409, "profile_url_collision")
    assert (h.home / "config.yaml").read_bytes() == before
    assert h.counts == Counter(create=1)


async def test_rename_cas_preserves_url_key_generation_opaque_and_project_split(harness):
    h = harness
    key_before = snapshot(h.home)["remote-keys.json"]
    project_before = snapshot(h.root)
    opaque = {k: v for k, v in config(h.home).items() if k != "remote_servers"}
    async with client_for(h.app) as client:
        ctx, owner = h.app.state.ctx, h.app.state.remote_catalog.runtime
        ready, transport, sync = owner.ready, owner._transport, ctx.sync
        success(await mutate(client, "rename", body={**PATCH, "name": "  renamed  "}),
                200, {**P1, "name": "renamed", "revision": 4}, source="stored")
        error(await mutate(client, "rename"), 409, "profile_revision_conflict")
        assert h.app.state.ctx is ctx and ctx.sync is sync and sync.generation == 41
        assert h.app.state.remote_catalog.runtime is owner
        assert owner.ready is ready and owner._transport is transport
    saved = config(h.home)
    assert saved["remote_servers"] == [{**P1, "name": "renamed", "revision": 4}, P2]
    assert {k: v for k, v in saved.items() if k != "remote_servers"} == opaque
    assert snapshot(h.home)["remote-keys.json"] == key_before
    assert snapshot(h.root) == project_before
    assert h.counts == Counter(rename=2, publication=1)
    assert not (h.home / "remote-profile-change.json").exists()


@pytest.mark.parametrize("operation,body", [
    ("create", {}), ("create", {"name": "same"}),
    ("create", {**CREATE, "token": CANARY}),
    ("create", {**CREATE, "clear_credential": True}),
    ("create", {**CREATE, "expected_revision": 1}),
    ("create", {**CREATE, "name": None}), ("create", {**CREATE, "name": 12}),
    ("create", {**CREATE, "name": " "}),
    ("create", {**CREATE, "name": CANARY + "\n"}),
    ("create", {**CREATE, "name": CANARY * 5}),
    ("create", {**CREATE, "url": None}), ("create", {**CREATE, "url": 12}),
    ("create", {**CREATE, "url": "ftp://" + CANARY + ".invalid"}),
    ("create", {**CREATE, "url": "https://user:" + CANARY + "@host.invalid"}),
    ("create", {**CREATE, "url": "https://host.invalid/?token=" + CANARY}),
    ("create", {**CREATE, "url": "https://host.invalid/#" + CANARY}),
    ("create", {**CREATE, "url": "https://host.invalid:" + CANARY}),
    ("rename", {}), ("rename", {"expected_revision": 3}),
    ("rename", {"name": "renamed"}),
    ("rename", {**PATCH, "url": "https://host.invalid/" + CANARY}),
    ("rename", {**PATCH, "clear_credential": True}),
    ("rename", {**PATCH, "token": CANARY}),
    ("rename", {**PATCH, "name": ""}), ("rename", {**PATCH, "name": CANARY + "\x00"}),
    ("rename", {**PATCH, "expected_revision": True}),
    ("rename", {**PATCH, "expected_revision": 0}),
    ("rename", {**PATCH, "expected_revision": -1}),
    ("rename", {**PATCH, "expected_revision": 3.0}),
    ("rename", {**PATCH, "expected_revision": "3"}),
])
async def test_strict_input_prevalidation_is_secret_free_and_precedes_s2(harness, caplog, operation, body):
    h = harness
    caplog.set_level(logging.DEBUG)
    before = snapshot(h.home)
    async with client_for(h.app) as client:
        error(await mutate(client, operation, body=body), 422, "invalid_request")
    assert snapshot(h.home) == before
    assert not h.counts
    secret_free(caplog.text)


@pytest.mark.parametrize("profile_id", ["not-uuid", P1["profile_id"].replace("-", ""),
    "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "{" + P1["profile_id"] + "}"])
async def test_rename_requires_canonical_uuid_before_s2(harness, profile_id):
    before = snapshot(harness.home)
    async with client_for(harness.app) as client:
        error(await client.patch(BASE + "/" + profile_id, json=PATCH), 422, "invalid_request")
    assert not harness.counts and snapshot(harness.home) == before


@pytest.mark.parametrize("operation", ["create", "rename"])
async def test_extra_queries_fail_before_s2(harness, operation):
    before = snapshot(harness.home)
    async with client_for(harness.app) as client:
        error(await mutate(client, operation, params={"token": CANARY}), 422, "invalid_request")
    assert not harness.counts and snapshot(harness.home) == before


@pytest.mark.parametrize("operation", ["create", "rename"])
@pytest.mark.parametrize("headers,status,code", [
    ({"authorization": ""}, 401, "studio_unauthorised"),
    ({"authorization": "Bearer wrong"}, 401, "studio_unauthorised"),
    ({"host": "bad host"}, 403, "studio_forbidden"),
    ({"origin": "https://foreign.invalid"}, 403, "studio_forbidden"),
])
async def test_auth_host_origin_precede_any_s2(harness, monkeypatch, operation, headers, status, code):
    h = harness
    before = snapshot(h.home)
    async with client_for(h.app) as client:
        reads = []

        def denied(*_args, **_kwargs):
            reads.append(True)
            raise AssertionError("Auth must precede S2")

        monkeypatch.setattr(RemoteProfiles, "_read", denied)
        monkeypatch.setattr(credentials, "profile_token_source", denied)
        error(await mutate(client, operation, headers=headers), status, code)
        assert not reads
    assert not h.counts and snapshot(h.home) == before


@pytest.mark.parametrize("operation", ["create", "rename"])
@pytest.mark.parametrize("failure", ["env-unbound", "env-wrong-url", "env-invalid-url",
                                    "corrupt-store", "source-exception", "dto-exception", "pending"])
async def test_committed_mutation_survives_response_failure_once(harness, monkeypatch, caplog, operation, failure):
    h = harness
    caplog.set_level(logging.DEBUG)
    before_project = snapshot(h.root)
    async with client_for(h.app) as client:
        if failure.startswith("env-"):
            monkeypatch.setenv(credentials.TOKEN_ENV_VAR, KEY)
            if failure != "env-unbound":
                monkeypatch.setenv(credentials.URL_ENV_VAR,
                    "https://unrelated.invalid" if failure == "env-wrong-url"
                    else "ftp://" + CANARY + ".invalid")
        elif failure == "corrupt-store":
            (h.home / "remote-keys.json").write_text('{"token":"' + CANARY, encoding="utf-8")
        elif failure in {"source-exception", "dto-exception"}:
            def failed(*_args, **_kwargs):
                raise RuntimeError(CANARY + KEY)
            if failure == "source-exception":
                monkeypatch.setattr(credentials, "profile_token_source", failed)
            else:
                monkeypatch.setattr(remote_metadata, "_profiles", failed)
        else:
            # Journal appears only AFTER successful S2 publication. No recovery,
            # retry or removal is allowed when the response's fence sees it.
            original = getattr(RemoteProfiles, operation)

            def committed_then_pending(self, *args, **kwargs):
                row = original(self, *args, **kwargs)
                (h.home / "remote-profile-change.json").write_text(CANARY, encoding="utf-8")
                return row

            monkeypatch.setattr(RemoteProfiles, operation, committed_then_pending)
        key_before = snapshot(h.home)["remote-keys.json"]
        error(await mutate(client, operation), 503, "profile_committed_metadata_unavailable")
        saved = config(h.home)["remote_servers"]
        if operation == "create":
            assert len(saved) == 3 and saved[:2] == [P1, P2]
            assert saved[2] == {**CREATE, "profile_id": saved[2]["profile_id"], "revision": 1}
        else:
            assert saved == [{**P1, "name": "renamed", "revision": 4}, P2]
        assert h.counts == Counter({operation: 1, "publication": 1})
        assert snapshot(h.home)["remote-keys.json"] == key_before
        assert snapshot(h.root) == before_project
        if failure == "pending":
            assert (h.home / "remote-profile-change.json").read_text(encoding="utf-8") == CANARY
    secret_free(caplog.text)


@pytest.mark.parametrize("operation", ["create", "rename"])
async def test_response_observes_only_committed_profile_not_full_list(harness, monkeypatch, operation):
    h = harness
    async with client_for(h.app) as client:
        # Real S2 ENV presence is usable only for the changed profile's URL.
        # P2 has environment_unbound, which fails the full list rather than
        # reporting its stored key or silently skipping the unrelated profile.
        monkeypatch.setenv(credentials.TOKEN_ENV_VAR, KEY)
        monkeypatch.setenv(credentials.URL_ENV_VAR,
                           CREATE["url"] if operation == "create" else P1["url"])
        error(await client.get(BASE), 503, "metadata_unavailable")
        lists, presence_ids = [], []
        original_source = credentials.profile_token_source

        def no_list(*_args):
            lists.append(True)
            raise AssertionError("C6 must not list after publication")

        def source(profile_id, url):
            presence_ids.append(profile_id)
            return original_source(profile_id, url)

        monkeypatch.setattr(RemoteProfiles, "list", no_list)
        monkeypatch.setattr(credentials, "profile_token_source", source)
        response = await mutate(client, operation)
        saved = config(h.home)["remote_servers"]
        row = saved[-1] if operation == "create" else saved[0]
        success(response, 201 if operation == "create" else 200, row, source="environment")
        assert not lists and presence_ids == [row["profile_id"]] * 2
        assert h.counts == Counter({operation: 1, "publication": 1})


@pytest.mark.parametrize("operation", ["create", "rename"])
async def test_exact_bound_environment_presence_is_allowed_without_loading_token(harness, monkeypatch, operation):
    target = CREATE["url"] if operation == "create" else P1["url"]
    monkeypatch.setenv(credentials.TOKEN_ENV_VAR, KEY)
    monkeypatch.setenv(credentials.URL_ENV_VAR, target)
    async with client_for(harness.app) as client:
        response = await mutate(client, operation)
        saved = config(harness.home)["remote_servers"]
        success(response, 201 if operation == "create" else 200,
                saved[-1] if operation == "create" else saved[0], source="environment")


@pytest.mark.parametrize("method,suffix,body,statuses", [
    ("post", "/" + P1["profile_id"] + "/projects", {"id": "unused"}, {422}),
    ("post", "/" + P1["profile_id"] + "/select", {}, {404, 405}),
])
async def test_invalid_create_and_unmounted_select_never_touch_s2(harness, method, suffix, body, statuses):
    before = snapshot(harness.home)
    async with client_for(harness.app) as client:
        response = await client.request(method, BASE + suffix, json=body)
        assert response.status_code in statuses, response.text
        # Unmounted paths retain the ordinary application 404 contract; only
        # the known Remote route's method refusal belongs to its no-store DTO.
        if response.status_code in {405, 422}:
            assert response.headers["cache-control"] == "no-store"
        secret_free(response.text)
    assert not harness.counts and snapshot(harness.home) == before
