"""VS-142 C7: mounted HTTP -> application lifecycle -> real synthetic S2.

No listener, Remote, real credential or outbound request. Only unrelated project
startup and the HTTP pool are stubbed. Run -n0 -p no:cacheprovider --basetemp
under .voxarium_studio/temp/TASK-142; all fixture homes/roots are disposable.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr, ValidationError
import yaml

from voxarium_studio import main
from voxarium_studio.api.remote import CredentialInput
from voxarium_studio.build_info import BuildInfo
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.core.project_scope import canonical_root
from voxarium_studio.sync import catalog_runtime, credentials, profiles as profile_store
from voxarium_studio.sync.catalog import CatalogError
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.bindings import BindingRecord, RemoteBindings
from voxarium_studio.sync.profile_lifecycle import BindingInventory
from voxarium_studio.sync.profiles import ProfileError, RemoteProfiles
from voxarium_studio.ws.bus import EventBus


BASE = "/api/remote/servers"
APP_KEY = "SYNTHETIC-STUDIO-AUTH-C7-142"
OLD_KEY = "SYNTHETIC-OLD-REMOTE-KEY-C7-142"
NEW_KEY = "SYNTHETIC-NEW-REMOTE-KEY-C7-142"
CANARY = "SYNTHETIC-UNTRUSTED-CANARY-C7-142"
P1 = dict(profile_id="11111111-1111-4111-8111-111111111142", name="same",
          url="https://one.invalid/deployment/prefix", revision=3)
P2 = dict(profile_id="22222222-2222-4222-8222-222222222142", name="same",
          url="https://two.invalid/other/prefix", revision=8)
NEW_URL = "https://new.invalid/deployment/prefix"
OPAQUE = {"future_global": {"nested": [1, None, {"retain": True}]},
          "theme": {"mode": "dark", "future_palette": ["opaque"]},
          "language": "ru", "respond_language": "Russian", "show_data_dir": True,
          "node_cache_enabled": False, "updates": {"auto_check": False}}
OPERATIONS = ("set_credential", "clear_credential", "change_url", "delete", "recover")


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def config(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def keys(home):
    return json.loads((home / "remote-keys.json").read_text(encoding="utf-8"))


def secret_free(value):
    for secret in (APP_KEY, OLD_KEY, NEW_KEY, CANARY):
        assert secret not in value


def clean_exception(error):
    """Inspect actual linked exceptions, not only the displayed 'from None'."""
    seen, pending = set(), [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        secret_free(str(current))
        secret_free(repr(current))
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": code, "details": {}}
    secret_free(response.text)


def dto(row, source="none"):
    return {**row, "has_token": source != "none", "credential_source": source,
            "verification": {"state": "unavailable"}}


def success(response, row, source="none"):
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == dto(row, source)
    secret_free(response.text)


def confirmation(profile_id=P1["profile_id"]):
    return {"profile_id": profile_id, "action": "discard_uncertain_credential"}


def command(operation, revision=3):
    body = {"expected_revision": revision}
    if operation == "set_credential":
        return "PUT", "/credential", {**body, "token": NEW_KEY}
    if operation == "clear_credential":
        return "DELETE", "/credential", body
    if operation == "change_url":
        return "PATCH", "", {**body, "url": NEW_URL, "clear_credential": True}
    if operation == "delete":
        return "DELETE", "", body
    assert operation == "recover"
    return "POST", "/recover", {**body, "confirmation": confirmation()}


async def mutate(client, operation, *, body=None, revision=3, profile_id=P1["profile_id"], **kwargs):
    method, suffix, default = command(operation, revision)
    return await client.request(method, BASE + "/" + profile_id + suffix,
                                json=default if body is None else body, **kwargs)


def journal(home, *, target_url=None):
    path = home / "remote-profile-change.json"
    path.write_text(json.dumps({"version": 1, "profile_id": P1["profile_id"],
        "expected_revision": 3, "url": P1["url"], "target_url": target_url or P1["url"]}),
        encoding="utf-8")
    return path


@asynccontextmanager
async def client_for(h, *, known=True):
    async with h.app.router.lifespan_context(h.app):
        owner = h.app.state.remote_catalog.runtime
        if known:
            # Explicit completeness proof seam; production default MUST stay unknown.
            owner.lifecycle._inventory = lambda _id: BindingInventory(state="known")
        second = h.project_type(h.other_root, h.app.state.auth)
        second.remote_profile_lifecycle = owner.lifecycle
        owner.lifecycle.register_context(second)
        h.app.state.project_contexts[second.paths.root] = second
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app),
            base_url="http://127.0.0.1", trust_env=False,
            headers={"authorization": "Bearer " + APP_KEY}) as client:
            yield client


@pytest.fixture
async def harness(monkeypatch, studio_home, tmp_path):
    counts = Counter()

    def denied(*_args, **_kwargs):
        counts["forbidden"] += 1
        raise AssertionError("C7 attempted network/process/token lookup/sync side effect")

    # Windows' asyncio wakeup socket already exists when this async fixture runs.
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, denied)
    for name in ("getaddrinfo", "gethostbyname", "create_connection"):
        monkeypatch.setattr(socket, name, denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    for name in ("load_token", "import_legacy_credential"):
        monkeypatch.setattr(RemoteProfiles, name, denied)
    for name in ("load_token", "load_profile_token", "save_token", "import_legacy_token"):
        monkeypatch.setattr(credentials, name, denied)
    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)

    for name in (*OPERATIONS, "create", "rename", "_begin", "_end"):
        original = getattr(RemoteProfiles, name)

        def counted(self, *args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(RemoteProfiles, name, counted)

    publish, save = profile_store._write_application_config, credentials.save_profile_token

    def publication(*args, **kwargs):
        counts["publication"] += 1
        return publish(*args, **kwargs)

    def credential_publication(*args, **kwargs):
        counts["credential_publication"] += 1
        return save(*args, **kwargs)

    monkeypatch.setattr(profile_store, "_write_application_config", publication)
    monkeypatch.setattr(credentials, "save_profile_token", credential_publication)
    monkeypatch.setattr(main, "build_info", lambda: BuildInfo(version="test", commit="test"))
    monkeypatch.setattr(main, "static_dir", lambda: None)
    monkeypatch.setattr(main, "record_recent", lambda *_: None)

    class Project:
        def __init__(self, root, auth):
            self.paths = ProjectPaths.for_project(Path(root))
            self.auth, self.bus = auth, EventBus()
            self.config = SimpleNamespace(project_name=None,
                sync=SimpleNamespace(url="", enabled=False))
            self.sync = SimpleNamespace(generation=41, reconnect=denied, connect=denied,
                stop=denied, start=denied, reconfigure=denied)

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
            "url": p["url"], "token": OLD_KEY, "revision": 2} for p in (P1, P2)},
        "legacy": {"https://legacy.invalid": OLD_KEY}}), encoding="utf-8")
    roots = (tmp_path / "project-a", tmp_path / "project-b")
    for root in roots:
        data = root / ".voxarium_studio"
        data.mkdir(parents=True)
        (data / "config.yaml").write_text("remote:\n  space: legacy\ntheme:\n  accent: '#aabbcc'\n",
                                         encoding="utf-8")
        (data / "app.db").write_bytes(b"SYNTHETIC-UNCHANGED-PROJECT-DATA")
    before_roots = [snapshot(root) for root in roots]
    app = main.create_app(str(roots[0]), auth=AuthSettings(token=APP_KEY))
    yield SimpleNamespace(app=app, home=studio_home, root=roots[0], other_root=roots[1],
                          project_type=Project, counts=counts, pool=pool)
    assert [snapshot(root) for root in roots] == before_roots
    assert counts["forbidden"] == 0
    assert pool.closed == 1


@pytest.mark.parametrize("operation", OPERATIONS[:-1])
async def test_real_s2_success_exact_committed_row_no_store_and_local_only(harness, operation):
    h = harness
    before_keys = keys(h.home)
    async with client_for(h) as client:
        owner, ctx = h.app.state.remote_catalog.runtime, h.app.state.ctx
        ready, transport, sync = owner.ready, owner._transport, ctx.sync
        response = await mutate(client, operation)
        if operation == "delete":
            assert response.status_code == 204, response.text
            assert response.content == b""
            assert response.headers["cache-control"] == "no-store"
            expected_rows = [P2]
        else:
            row = {**P1, "revision": 4}
            if operation == "change_url":
                row["url"] = NEW_URL
            success(response, row, "stored" if operation == "set_credential" else "none")
            expected_rows = [row, P2]
        assert h.app.state.ctx is ctx and ctx.sync is sync and sync.generation == 41
        assert owner.ready is ready and owner._transport is transport
        assert len(h.app.state.project_contexts) == 2
    saved, stored = config(h.home), keys(h.home)
    assert saved["remote_servers"] == expected_rows
    assert {k: v for k, v in saved.items() if k != "remote_servers"} == OPAQUE
    assert stored["profiles"][P2["profile_id"]] == before_keys["profiles"][P2["profile_id"]]
    assert stored["legacy"] == before_keys["legacy"]
    assert stored["profiles"][P1["profile_id"]] == {
        "url": NEW_URL if operation == "change_url" else P1["url"],
        "token": NEW_KEY if operation == "set_credential" else None,
        "revision": 4 if operation == "change_url" else 3}
    assert h.counts["publication"] == 1
    assert h.counts["_begin"] == h.counts["_end"] == h.counts[operation] == 1
    assert h.counts["credential_publication"] == (2 if operation == "change_url" else 1)
    assert not (h.home / "remote-profile-change.json").exists()


async def test_url_patch_normalizes_and_collision_is_no_commit(harness):
    h = harness
    async with client_for(h) as client:
        before = keys(h.home)
        error(await mutate(client, "change_url", body={"expected_revision": 3,
            "url": "WSS://TWO.INVALID/other/prefix///", "clear_credential": True}),
            409, "profile_url_collision")
        assert keys(h.home) == before and h.counts["publication"] == 0
        success(await mutate(client, "change_url", body={"expected_revision": 3,
            "url": " WSS://NEW.INVALID/deployment/prefix/// ", "clear_credential": True}),
            {**P1, "url": NEW_URL, "revision": 4})
    assert h.counts["publication"] == 1


@pytest.mark.parametrize("operation", OPERATIONS[:-1])
async def test_missing_profile_is_proven_not_found_before_s2_writes(harness, operation):
    h = harness
    async with client_for(h, known=False) as client:
        before = snapshot(h.home)
        missing = "33333333-3333-4333-8333-333333333142"
        error(await mutate(client, operation, profile_id=missing), 404, "profile_not_found")
        assert snapshot(h.home) == before
        assert not h.counts[operation] and not h.counts["_begin"]


@pytest.mark.parametrize("operation", OPERATIONS[:-1])
async def test_stale_cas_refuses_before_journal_or_credential_publication(harness, operation):
    h = harness
    before = (config(h.home), keys(h.home))
    async with client_for(h) as client:
        error(await mutate(client, operation, revision=2), 409, "profile_revision_conflict")
    assert (config(h.home), keys(h.home)) == before
    assert not h.counts["_begin"] and not h.counts["publication"]
    assert not h.counts["credential_publication"]


async def test_concurrent_cas_has_one_winner_and_one_publication(harness):
    h = harness
    async with client_for(h) as client:
        responses = await asyncio.gather(mutate(client, "set_credential"),
                                         mutate(client, "clear_credential"))
        winner = next(r for r in responses if r.status_code == 200)
        loser = next(r for r in responses if r.status_code == 409)
        error(loser, 409, "profile_revision_conflict")
        source = "stored" if keys(h.home)["profiles"][P1["profile_id"]]["token"] else "none"
        success(winner, {**P1, "revision": 4}, source)
    assert h.counts["publication"] == h.counts["credential_publication"] == 1
    assert config(h.home)["remote_servers"] == [{**P1, "revision": 4}, P2]


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_default_unknown_inventory_is_unavailable_without_stopping_any_consumer(harness, operation):
    h = harness
    async with client_for(h, known=False) as client:
        if operation == "recover":
            journal(h.home)
        before = snapshot(h.home)
        error(await mutate(client, operation), 503, "consumer_lifecycle_unavailable")
        assert snapshot(h.home) == before
    assert not h.counts["publication"] and not h.counts["_begin"]


@pytest.mark.parametrize("operation,state,code", [
    ("change_url", BindingInventory(state="known", bound_roots=("D:/synthetic/private-unopened",)), "profile_in_use"),
    ("delete", BindingInventory(state="known", bound_roots=("D:/synthetic/private-unopened",)), "profile_in_use"),
    ("set_credential", BindingInventory(state="known", active_roots=("D:/synthetic/active",)), "profile_consumers_not_drained"),
    ("clear_credential", BindingInventory(state="known", active_roots=("D:/synthetic/active",)), "profile_consumers_not_drained"),
    ("recover", BindingInventory(state="known", active_roots=("D:/synthetic/active",)), "profile_consumers_not_drained"),
])
async def test_used_or_non_drained_inventory_refuses_without_force_stop(harness, operation, state, code):
    h = harness
    async with client_for(h) as client:
        h.app.state.remote_catalog.runtime.lifecycle._inventory = lambda _id: state
        if operation == "recover":
            journal(h.home)
        before = snapshot(h.home)
        error(await mutate(client, operation), 409, code)
        assert snapshot(h.home) == before
    assert not h.counts["publication"] and not h.counts["credential_publication"]


@pytest.mark.parametrize("operation", ("change_url", "delete", "set_credential"))
async def test_non_selected_resident_legacy_context_is_a_consumer(harness, operation):
    h = harness
    async with client_for(h) as client:
        ctx = h.app.state.project_contexts[h.other_root]
        ctx.config.sync.url = P1["url"]
        ctx.config.sync.enabled = operation == "set_credential"
        before = snapshot(h.home)
        error(await mutate(client, operation), 409,
              "profile_consumers_not_drained" if ctx.config.sync.enabled else "profile_in_use")
        assert h.app.state.ctx is not ctx and ctx.sync.generation == 41
        assert snapshot(h.home) == before


@pytest.mark.parametrize("active_url", (P1["url"], NEW_URL))
async def test_pending_recovery_considers_both_journal_urls_in_nonselected_context(harness, active_url):
    h = harness
    async with client_for(h) as client:
        ctx = h.app.state.project_contexts[h.other_root]
        ctx.config.sync.url, ctx.config.sync.enabled = active_url, True
        path = journal(h.home, target_url=NEW_URL)
        before = snapshot(h.home)
        error(await mutate(client, "recover"), 409, "profile_consumers_not_drained")
        assert snapshot(h.home) == before and path.exists()
        assert ctx.sync.generation == 41 and h.app.state.ctx is not ctx
        assert not h.counts["publication"] and not h.counts["credential_publication"]


async def test_recovery_rechecks_journal_evidence_under_lifecycle_guard(harness):
    h = harness
    async with client_for(h) as client:
        path = journal(h.home, target_url=NEW_URL)
        def inventory(_profile_id):
            # A new target is not covered by the already captured URL evidence.
            pending = json.loads(path.read_text(encoding="utf-8"))
            pending["target_url"] = "https://changed.invalid"
            path.write_text(json.dumps(pending), encoding="utf-8")
            return BindingInventory(state="known")
        h.app.state.remote_catalog.runtime.lifecycle._inventory = inventory
        before_keys, before_config = keys(h.home), config(h.home)
        error(await mutate(client, "recover"), 409, "profile_revision_conflict")
        assert config(h.home) == before_config and keys(h.home) == before_keys
        assert path.exists() and not h.counts["publication"] and not h.counts["credential_publication"]


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("filename", ("config.yaml", "remote-keys.json"))
@pytest.mark.parametrize("kind", ("dangling-link", "directory"))
async def test_unsafe_store_is_not_proven_absence_and_never_mutated(harness, operation, filename, kind):
    h = harness
    async with client_for(h) as client:
        if operation == "recover":
            journal(h.home)
        path = h.home / filename
        contents = path.read_bytes()
        path.unlink()
        if kind == "dangling-link":
            path.symlink_to(h.home / "absent-store-target")
        else:
            path.mkdir()
        try:
            journal_path = h.home / "remote-profile-change.json"
            pending = journal_path.read_bytes() if journal_path.exists() else None
            error(await mutate(client, operation), 409, "profile_recovery_required")
            assert not h.counts["publication"] and not h.counts["credential_publication"]
            assert not h.counts["_begin"] and not h.counts["recover"]
            assert (journal_path.read_bytes() if journal_path.exists() else None) == pending
            assert path.is_symlink() if kind == "dangling-link" else path.is_dir()
            assert not (h.home / "absent-store-target").exists()
        finally:
            if kind == "dangling-link":
                path.unlink()
            else:
                path.rmdir()
            path.write_bytes(contents)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("filename", ("config.yaml", "remote-keys.json"))
async def test_store_path_is_rechecked_after_inventory_under_shared_guard(harness, operation, filename):
    h = harness
    async with client_for(h) as client:
        if operation == "recover":
            journal(h.home)
        path = h.home / filename
        contents = path.read_bytes()
        pending = (h.home / "remote-profile-change.json")
        journal_before = pending.read_bytes() if pending.exists() else None
        def inventory(_profile_id):
            path.unlink()
            path.symlink_to(h.home / "absent-store-target")
            return BindingInventory(state="known")
        h.app.state.remote_catalog.runtime.lifecycle._inventory = inventory
        try:
            response = await mutate(client, operation)
            # A missing metadata binding is rejected by lifecycle itself; a
            # key-store drift by the mutation's path recheck before S2 writes.
            error(response, 503 if filename == "config.yaml" and operation != "recover" else 409,
                  "consumer_lifecycle_unavailable" if filename == "config.yaml" and operation != "recover"
                  else "profile_recovery_required")
            assert path.is_symlink() and not (h.home / "absent-store-target").exists()
            assert (pending.read_bytes() if pending.exists() else None) == journal_before
            assert not h.counts["publication"] and not h.counts["credential_publication"]
            assert not h.counts["_begin"]
        finally:
            path.unlink()
            path.write_bytes(contents)


async def test_admitted_operation_prevents_key_change_even_with_external_drained_proof(harness):
    h = harness
    async with client_for(h) as client:
        lifecycle = h.app.state.remote_catalog.runtime.lifecycle
        lifecycle._inventory = lambda _id: BindingInventory(state="known", drained=True)
        with lifecycle.operation(P1["profile_id"], kind="create"):
            error(await mutate(client, "set_credential"), 409, "profile_consumers_not_drained")
        success(await mutate(client, "set_credential"), {**P1, "revision": 4}, "stored")
    assert h.counts["publication"] == 1


@pytest.mark.parametrize("stage", ("credential", "metadata"))
async def test_concurrent_admission_refuses_through_both_s2_publication_stages(harness, monkeypatch, stage):
    h = harness
    async with client_for(h) as client:
        lifecycle = h.app.state.remote_catalog.runtime.lifecycle
        target, name = ((credentials, "save_profile_token") if stage == "credential"
                        else (profile_store, "_write_application_config"))
        original = getattr(target, name)
        observations = []

        def admission():
            try:
                lease = lifecycle.attach(root=str(h.other_root), url=P1["url"], profile_id=P1["profile_id"])
            except ProfileError as exc:
                observations.append(exc.args)
            else:
                lease.release()
                observations.append("UNSAFE_ADMISSION")

        def paused(*args, **kwargs):
            pending = json.loads((h.home / "remote-profile-change.json").read_text(encoding="utf-8"))
            assert pending == {"version": 1, "profile_id": P1["profile_id"],
                "expected_revision": 3, "url": P1["url"], "target_url": P1["url"]}
            secret_free(repr(pending))
            worker = Thread(target=admission)
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive(), "Admission must refuse, not block the ACK/event loop"
            return original(*args, **kwargs)

        monkeypatch.setattr(target, name, paused)
        success(await mutate(client, "set_credential"), {**P1, "revision": 4}, "stored")
        assert observations == [("consumer_lifecycle_unavailable",)]
        # A fresh consumer can be admitted only after journal/config/key publication finishes.
        with lifecycle.attach(root=str(h.other_root), url=P1["url"], profile_id=P1["profile_id"]):
            pass
    assert h.counts["publication"] == 1


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("revision", (True, 0, -1, 3.0, "3", None))
async def test_revision_validation_precedes_s2_and_never_echoes_secrets(harness, operation, revision, caplog):
    h = harness
    caplog.set_level(logging.DEBUG)
    body = {**command(operation)[2], "expected_revision": revision}
    before = snapshot(h.home)
    async with client_for(h) as client:
        error(await mutate(client, operation, body=body), 422, "invalid_request")
    assert not h.counts and snapshot(h.home) == before
    secret_free(caplog.text)


@pytest.mark.parametrize("operation,body", [
    ("set_credential", {"expected_revision": 3}),
    *[("set_credential", {"expected_revision": 3, "token": value})
      for value in (None, 1, True, "", "   ", {CANARY: NEW_KEY}, [NEW_KEY])],
    ("set_credential", {"expected_revision": 3, "token": NEW_KEY, CANARY: OLD_KEY}),
    ("clear_credential", {"expected_revision": 3, "token": CANARY}),
    ("delete", {"expected_revision": 3, "url": CANARY}),
    ("change_url", {"expected_revision": 3}),
    ("change_url", {"expected_revision": 3, "url": NEW_URL}),
    *[("change_url", {"expected_revision": 3, "url": NEW_URL, "clear_credential": value})
      for value in (False, 1, "true", None)],
    ("change_url", {"expected_revision": 3, "url": NEW_URL, "clear_credential": True, "name": CANARY}),
    ("change_url", {"expected_revision": 3, "name": "renamed", "clear_credential": True}),
    ("change_url", {"expected_revision": 3, "url": "https://user:" + CANARY + "@host.invalid", "clear_credential": True}),
    ("change_url", {"expected_revision": 3, "url": "https://host.invalid/?token=" + CANARY, "clear_credential": True}),
    ("recover", {"expected_revision": 3}),
    ("recover", {"expected_revision": 3, "confirmation": None}),
    ("recover", {"expected_revision": 3, "confirmation": {"profile_id": P1["profile_id"]}}),
    ("recover", {"expected_revision": 3, "confirmation": {"action": "discard_uncertain_credential"}}),
    ("recover", {"expected_revision": 3, "confirmation": {**confirmation(), "token": CANARY}}),
    ("recover", {"expected_revision": 3, "confirmation": {**confirmation(), "action": 1}}),
])
async def test_strict_body_validation_before_dispatch_is_secret_free(harness, operation, body, caplog):
    h = harness
    caplog.set_level(logging.DEBUG)
    before = snapshot(h.home)
    async with client_for(h) as client:
        error(await mutate(client, operation, body=body), 422, "invalid_request")
    assert not h.counts and snapshot(h.home) == before
    secret_free(caplog.text)


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_missing_revision_or_malformed_json_is_no_write_validation(harness, operation, caplog):
    h = harness
    caplog.set_level(logging.DEBUG)
    async with client_for(h) as client:
        method, suffix, body = command(operation)
        body.pop("expected_revision")
        before = snapshot(h.home)
        error(await mutate(client, operation, body=body), 422, "invalid_request")
        error(await client.request(method, BASE + "/" + P1["profile_id"] + suffix,
            content='{"token":"' + CANARY + NEW_KEY,
            headers={"content-type": "application/json"}), 422, "invalid_request")
        assert snapshot(h.home) == before and not h.counts
    secret_free(caplog.text)


@pytest.mark.parametrize("confirmation_value", [confirmation(P2["profile_id"]),
    {**confirmation(), "action": "different_action"}])
async def test_conflicting_confirmation_is_409_before_s2_even_with_pending_journal(harness, confirmation_value):
    h = harness
    async with client_for(h) as client:
        path = journal(h.home)
        before = snapshot(h.home)
        error(await mutate(client, "recover", body={"expected_revision": 3,
            "confirmation": confirmation_value}), 409, "profile_recovery_confirmation_conflict")
        assert snapshot(h.home) == before and path.exists() and not h.counts


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_extra_query_and_noncanonical_profile_id_precede_s2(harness, operation):
    h = harness
    before = snapshot(h.home)
    async with client_for(h) as client:
        error(await mutate(client, operation, params={"token": CANARY}), 422, "invalid_request")
        for profile_id in ("not-uuid", P1["profile_id"].replace("-", ""),
                           "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"):
            error(await mutate(client, operation, profile_id=profile_id), 422, "invalid_request")
    assert not h.counts and snapshot(h.home) == before


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("headers,status,code", [
    ({"authorization": ""}, 401, "studio_unauthorised"),
    ({"authorization": "Bearer wrong"}, 401, "studio_unauthorised"),
    ({"host": "bad host"}, 403, "studio_forbidden"),
    ({"origin": "https://foreign.invalid"}, 403, "studio_forbidden"),
])
async def test_auth_host_origin_precede_any_s2_or_lifecycle_observation(harness, monkeypatch, operation, headers, status, code):
    h = harness
    async with client_for(h) as client:
        reads = []

        def denied(*_args, **_kwargs):
            reads.append(True)
            raise AssertionError("Auth must precede S2/lifecycle")

        monkeypatch.setattr(RemoteProfiles, "_read", denied)
        monkeypatch.setattr(credentials, "profile_token_source", denied)
        h.app.state.remote_catalog.runtime.lifecycle._inventory = denied
        before = snapshot(h.home)
        error(await mutate(client, operation, headers=headers), status, code)
        assert snapshot(h.home) == before and not reads and not h.counts


@pytest.mark.parametrize("stage", ("credential", "metadata"))
async def test_real_disk_fault_preserves_nonsecret_journal_and_requires_explicit_recovery(harness, monkeypatch, caplog, stage):
    h = harness
    caplog.set_level(logging.DEBUG)
    async with client_for(h) as client:
        original = Path.replace
        target = h.home / ("remote-keys.json" if stage == "credential" else "config.yaml")

        def failed_replace(path, destination):
            if Path(destination) == target:
                raise OSError(CANARY + NEW_KEY)
            return original(path, destination)

        with monkeypatch.context() as fault:
            fault.setattr(Path, "replace", failed_replace)
            response = await mutate(client, "set_credential")
        error(response, 503, "credential_store_unavailable" if stage == "credential" else "profile_store_unavailable")
        pending = h.home / "remote-profile-change.json"
        assert pending.exists()
        secret_free(pending.read_text(encoding="utf-8"))
        assert config(h.home)["remote_servers"] == [P1, P2]
        assert keys(h.home)["profiles"][P1["profile_id"]]["token"] == (OLD_KEY if stage == "credential" else NEW_KEY)
        before = snapshot(h.home)
        error(await client.get(BASE), 409, "profile_recovery_required")
        error(await mutate(client, "set_credential"), 409, "profile_recovery_required")
        error(await mutate(client, "recover", body={"expected_revision": 3}), 422, "invalid_request")
        assert snapshot(h.home) == before
        recovered = await mutate(client, "recover")
        assert recovered.status_code == 200, recovered.text
        assert recovered.headers["cache-control"] == "no-store"
        assert recovered.json() == {"profile": dto({**P1, "revision": 4})}
        secret_free(recovered.text)
        assert not pending.exists()
        assert keys(h.home)["profiles"][P1["profile_id"]]["token"] is None
        assert (await client.get(BASE)).json() == {"items": [dto({**P1, "revision": 4}), dto(P2, "stored")]}
    secret_free(caplog.text)


async def test_journal_publication_failure_precedes_both_stores_and_leaves_no_false_pending(harness, monkeypatch):
    h = harness
    async with client_for(h) as client:
        original = Path.replace
        journal_path = h.home / "remote-profile-change.json"
        before = config(h.home), keys(h.home)

        def failed_replace(path, destination):
            if Path(destination) == journal_path:
                raise OSError(CANARY + NEW_KEY)
            return original(path, destination)

        with monkeypatch.context() as fault:
            fault.setattr(Path, "replace", failed_replace)
            error(await mutate(client, "set_credential"), 503, "profile_store_unavailable")
        assert (config(h.home), keys(h.home)) == before
        assert not journal_path.exists()
        assert not list(h.home.glob(".remote-change-*"))
        assert not h.counts["credential_publication"] and not h.counts["publication"]
        response = await client.get(BASE)
        assert response.status_code == 200 and response.json() == {"items": [dto(P1, "stored"), dto(P2, "stored")]}
        assert not h.counts["recover"]


@pytest.mark.parametrize("stage", ("credential", "metadata"))
async def test_real_disk_errors_are_not_retained_in_runtime_exception_chains(harness, monkeypatch, stage):
    h = harness
    async with client_for(h):
        original = Path.replace
        target = h.home / ("remote-keys.json" if stage == "credential" else "config.yaml")

        def failed_replace(path, destination):
            if Path(destination) == target:
                raise OSError(CANARY + NEW_KEY)
            return original(path, destination)

        monkeypatch.setattr(Path, "replace", failed_replace)
        owner = h.app.state.remote_catalog.runtime
        with pytest.raises(ProfileError) as caught:
            owner.ready.mutations.set_credential(P1["profile_id"], SecretStr(NEW_KEY), expected_revision=3)
        assert caught.value.args == (("credential_store_unavailable" if stage == "credential"
                                     else "profile_store_unavailable"),)
        clean_exception(caught.value)
        assert (h.home / "remote-profile-change.json").exists()


async def test_recovery_existing_metadata_uses_current_cas_and_never_restores_previous_key(harness):
    h = harness
    async with client_for(h) as client:
        path = journal(h.home, target_url=NEW_URL)
        # Synthetic crash after URL/config publication but before journal removal.
        # Use real S2 on the command, not a fake recovery repository/result.
        saved = config(h.home)
        saved["remote_servers"][0] = {**P1, "url": NEW_URL, "revision": 4}
        (h.home / "config.yaml").write_text(json.dumps(saved), encoding="utf-8")
        stored = keys(h.home)
        stored["profiles"][P1["profile_id"]] = {"url": NEW_URL, "token": None, "revision": 4}
        (h.home / "remote-keys.json").write_text(json.dumps(stored), encoding="utf-8")
        before = {name: value for name, value in snapshot(h.home).items() if not name.endswith(".lock")}
        error(await mutate(client, "recover"), 409, "profile_revision_conflict")
        assert {name: value for name, value in snapshot(h.home).items() if not name.endswith(".lock")} == before
        recovered = await mutate(client, "recover", revision=4)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json() == {"profile": dto({**P1, "url": NEW_URL, "revision": 5})}
        assert recovered.headers["cache-control"] == "no-store"
        assert not path.exists()
        assert keys(h.home)["profiles"][P1["profile_id"]] == {"url": NEW_URL, "token": None, "revision": 6}
        assert h.counts["publication"] == 1


async def test_recovery_without_journal_refuses_instead_of_discarding_a_usable_key(harness):
    h = harness
    async with client_for(h) as client:
        before_keys, before_config = keys(h.home), config(h.home)
        error(await mutate(client, "recover"), 503, "profile_recovery_unavailable")
        assert keys(h.home) == before_keys and config(h.home) == before_config
        assert not h.counts["publication"] and not h.counts["credential_publication"]


async def test_failed_delete_after_metadata_commit_recovers_proven_absence_as_null(harness, monkeypatch):
    h = harness
    async with client_for(h) as client:
        original = Path.unlink
        path = h.home / "remote-profile-change.json"

        def fail_end(candidate, *args, **kwargs):
            if candidate == path:
                raise OSError(CANARY + OLD_KEY)
            return original(candidate, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(Path, "unlink", fail_end)
            error(await mutate(client, "delete"), 503, "profile_store_unavailable")
        assert config(h.home)["remote_servers"] == [P2] and path.exists()
        before = path.read_bytes()
        error(await client.get(BASE), 409, "profile_recovery_required")
        error(await mutate(client, "recover", revision=4), 409, "profile_revision_conflict")
        assert path.read_bytes() == before
        recovered = await mutate(client, "recover")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json() == {"profile": None}
        assert recovered.headers["cache-control"] == "no-store"
        assert not path.exists() and config(h.home)["remote_servers"] == [P2]
        assert h.counts["publication"] == 1  # Recovery must not recreate deleted metadata.
        assert keys(h.home)["profiles"][P1["profile_id"]]["token"] is None


@pytest.mark.parametrize("raw", ["{" + CANARY, "[]", "null",
    json.dumps({"version": True, "profile_id": P1["profile_id"], "expected_revision": 3,
                "url": P1["url"], "target_url": P1["url"]})])
async def test_corrupt_journal_is_never_removed_repaired_or_implicitly_recovered(harness, raw, caplog):
    h = harness
    caplog.set_level(logging.DEBUG)
    async with client_for(h) as client:
        path = h.home / "remote-profile-change.json"
        path.write_text(raw, encoding="utf-8")
        before = snapshot(h.home)
        for _ in range(2):
            error(await client.get(BASE), 409, "profile_recovery_required")
        assert not h.counts["recover"]
        error(await mutate(client, "recover"), 503, "profile_recovery_unavailable")
        assert snapshot(h.home) == before and path.read_text(encoding="utf-8") == raw
        assert not h.counts["publication"] and not h.counts["credential_publication"]
    secret_free(caplog.text)


@pytest.mark.parametrize("bound", (True, False))
async def test_delete_stored_credential_never_clears_or_misreports_environment_override(harness, monkeypatch, bound):
    h = harness
    async with client_for(h) as client:
        monkeypatch.setenv(credentials.TOKEN_ENV_VAR, CANARY)
        if bound:
            monkeypatch.setenv(credentials.URL_ENV_VAR, P1["url"])
        response = await mutate(client, "clear_credential")
        if bound:
            success(response, {**P1, "revision": 4}, "environment")
        else:
            error(response, 503, "profile_committed_metadata_unavailable")
        assert os.environ[credentials.TOKEN_ENV_VAR] == CANARY
        assert os.environ.get(credentials.URL_ENV_VAR) == (P1["url"] if bound else None)
        assert keys(h.home)["profiles"][P1["profile_id"]]["token"] is None
        assert config(h.home)["remote_servers"] == [{**P1, "revision": 4}, P2]
        assert h.counts["publication"] == 1


async def test_mutation_response_observes_exact_row_not_unrelated_environment_unbound_profile(harness, monkeypatch):
    h = harness
    async with client_for(h) as client:
        monkeypatch.setenv(credentials.TOKEN_ENV_VAR, CANARY)
        monkeypatch.setenv(credentials.URL_ENV_VAR, P1["url"])
        error(await client.get(BASE), 503, "metadata_unavailable")
        original = credentials.profile_token_source
        observed = []

        def source(profile_id, url):
            observed.append(profile_id)
            return original(profile_id, url)

        def no_list(*_args):
            raise AssertionError("Mutation responses must not list unrelated profiles")

        monkeypatch.setattr(credentials, "profile_token_source", source)
        monkeypatch.setattr(RemoteProfiles, "list", no_list)
        success(await mutate(client, "clear_credential"), {**P1, "revision": 4}, "environment")
        assert observed and set(observed) == {P1["profile_id"]}
    assert h.counts["publication"] == 1


async def test_post_commit_presence_failure_has_no_raw_exception_chain_or_retry(harness, monkeypatch, caplog):
    h = harness
    caplog.set_level(logging.DEBUG)
    async with client_for(h) as client:
        def failed(*_args, **_kwargs):
            raise RuntimeError(CANARY + NEW_KEY)

        monkeypatch.setattr(credentials, "profile_token_source", failed)
        error(await mutate(client, "set_credential"), 503, "profile_committed_metadata_unavailable")
        owner = h.app.state.remote_catalog.runtime
        with pytest.raises(CatalogError, match="profile_committed_metadata_unavailable") as caught:
            owner.ready.mutations.clear_credential(P1["profile_id"], expected_revision=4)
        clean_exception(caught.value)
        assert config(h.home)["remote_servers"] == [{**P1, "revision": 5}, P2]
        assert h.counts["publication"] == 2
    secret_free(caplog.text)


async def test_token_dto_is_strict_secretstr_and_repr_safe(harness):
    h = harness
    async with client_for(h):
        value = CredentialInput.model_validate({"expected_revision": 3, "token": NEW_KEY})
        assert isinstance(value.token, SecretStr)
        assert value.token.get_secret_value() == NEW_KEY
        secret_free(repr(value))
        secret_free(str(value))
        with pytest.raises(ValidationError) as caught:
            CredentialInput.model_validate({"expected_revision": CANARY, "token": NEW_KEY})
        clean_exception(caught.value)


async def test_get_never_enables_recovers_verifies_or_publishes_after_success(harness):
    h = harness
    async with client_for(h) as client:
        success(await mutate(client, "set_credential"), {**P1, "revision": 4}, "stored")
        counts, before = h.counts.copy(), snapshot(h.home)
        for _ in range(3):
            response = await client.get(BASE)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert response.json() == {"items": [dto({**P1, "revision": 4}, "stored"), dto(P2, "stored")]}
        assert h.counts == counts and snapshot(h.home) == before
        assert all(not ctx.config.sync.enabled for ctx in h.app.state.project_contexts.values())


def private_binding(h, *, state="selected", profile_id=None, url=None):
    """An unopened synthetic root: publish through the actual private store CAS."""
    record = BindingRecord(
        canonical_root=str(canonical_root(h.home.parent / "unopened-private-root")),
        profile_id=profile_id or P1["profile_id"], url=url or P1["url"],
        persistent_id="synthetic-i1", identity_revision=1, selection_state=state,
        space_instance_id="0123456789abcdef0123456789abcdef" if state == "confirmed" else None,
    )
    store = RemoteBindings()
    store.save(record, expected_fingerprint=store.read().fingerprint)
    return record


def forbid_key_revision(monkeypatch, h):
    def denied(*_args, **_kwargs):
        h.counts["key_revision"] += 1
        raise AssertionError("I1 refusal must precede even credential revision lookup")
    monkeypatch.setattr(credentials, "profile_token_revision", denied)


def no_sensitive_publication(h):
    assert not any(h.counts[name] for name in (
        "_begin", "_end", "key_revision", "credential_publication", "publication", "forbidden"))


@pytest.mark.parametrize("operation", ("change_url", "delete"))
@pytest.mark.parametrize("state", ("selected", "confirmed"))
@pytest.mark.parametrize("match", ("same-id-different-url", "different-id-same-url"))
async def test_i1_unopened_private_binding_blocks_http_mutation(harness, monkeypatch, operation, state, match):
    h = harness
    record = private_binding(h, state=state,
        profile_id=P2["profile_id"] if match == "different-id-same-url" else P1["profile_id"],
        url=P2["url"] if match == "same-id-different-url" else P1["url"])
    forbid_key_revision(monkeypatch, h)
    async with client_for(h) as client:
        assert record.canonical_root not in {str(root) for root in h.app.state.project_contexts}
        before = snapshot(h.home)
        error(await mutate(client, operation), 409, "profile_in_use")
        assert snapshot(h.home) == before
    no_sensitive_publication(h)


@pytest.mark.parametrize("state", ("selected", "confirmed"))
@pytest.mark.parametrize("binding_url", (P1["url"], NEW_URL, "https://current.invalid/prefix"))
async def test_i1_recovery_http_observes_old_target_current_private_url_union(harness, monkeypatch, state, binding_url):
    h = harness
    record = private_binding(h, state=state, profile_id=P2["profile_id"], url=binding_url)
    async with client_for(h) as client:
        journal(h.home, target_url=NEW_URL)
        saved = config(h.home)
        current = {**P1, "url": "https://current.invalid/prefix", "revision": 4}
        saved["remote_servers"][0] = current
        (h.home / "config.yaml").write_text(json.dumps(saved), encoding="utf-8")
        lifecycle = h.app.state.remote_catalog.runtime.lifecycle
        guard, observed = lifecycle.recovery_guard, []

        @contextmanager
        def observe(profile_id, *, urls):
            with guard(profile_id, urls=urls) as consumers:
                observed.append(consumers)
                yield consumers

        monkeypatch.setattr(lifecycle, "recovery_guard", observe)
        # I14: same URL under another profile is ambiguous. Do not repair it or
        # retain possibly affected confirmed consent while changing credentials.
        before = (config(h.home), keys(h.home), RemoteBindings().read())
        response = await mutate(client, "recover", revision=4)
        error(response, 503, "consumer_lifecycle_unavailable")
        assert observed and record.canonical_root in observed[0].bound_roots
        assert not observed[0].active_roots
        assert (config(h.home), keys(h.home), RemoteBindings().read()) == before
        assert (h.home / "remote-profile-change.json").exists()


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("store_state", ("missing", "empty", "corrupt", "version"))
async def test_i1_private_store_never_proves_legacy_coverage_http(harness, monkeypatch, operation, store_state):
    h = harness
    path = h.home / "remote-bindings.json"
    if store_state != "missing":
        path.write_text({"empty": '{"version":1,"roots":{}}',
                         "corrupt": "{" + CANARY,
                         "version": '{"version":99,"roots":{}}'}[store_state], encoding="utf-8")
    # Exercise first-ever credential installation as well as existing-key writes.
    if operation == "set_credential":
        (h.home / "remote-keys.json").unlink()
    forbid_key_revision(monkeypatch, h)
    async with client_for(h, known=False) as client:
        if operation == "recover":
            journal(h.home, target_url=NEW_URL)
        before = snapshot(h.home)
        error(await mutate(client, operation), 503, "consumer_lifecycle_unavailable")
        assert snapshot(h.home) == before
    no_sensitive_publication(h)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("raw", ("{" + CANARY, '{"version":99,"roots":{}}'))
async def test_i1_bad_private_store_is_not_hidden_by_known_legacy_seam(harness, monkeypatch, operation, raw):
    h = harness
    (h.home / "remote-bindings.json").write_text(raw, encoding="utf-8")
    forbid_key_revision(monkeypatch, h)
    async with client_for(h) as client:
        if operation == "recover":
            journal(h.home)
        before = snapshot(h.home)
        error(await mutate(client, operation), 503, "consumer_lifecycle_unavailable")
        assert snapshot(h.home) == before
    no_sensitive_publication(h)


@pytest.mark.parametrize("operation", ("set_credential", "change_url", "delete", "recover"))
@pytest.mark.parametrize("race", ("unrelated-private-publication", "coverage-drift"))
async def test_i1_final_http_recheck_precedes_journal_and_key_revision(harness, monkeypatch, operation, race):
    h = harness
    private_binding(h, profile_id=P2["profile_id"], url=P2["url"])
    forbid_key_revision(monkeypatch, h)
    async with client_for(h) as client:
        if operation == "recover":
            journal(h.home, target_url=NEW_URL)
        original = profile_store.application_config_transaction
        lifecycle = h.app.state.remote_catalog.runtime.lifecycle
        raced = []

        @contextmanager
        def transaction():
            # The initial guard has yielded, but S2 has not acquired its transaction.
            assert not raced
            if race == "unrelated-private-publication":
                # Still unrelated: whole-file fingerprint, not only matching roots.
                private_binding(h, profile_id=P2["profile_id"], url="https://unrelated.invalid")
            else:
                lifecycle._inventory = lambda _id: BindingInventory(state="known", drained=True)
            raced.append(snapshot(h.home))
            with original():
                yield

        monkeypatch.setattr(profile_store, "application_config_transaction", transaction)
        error(await mutate(client, operation), 503, "consumer_lifecycle_unavailable")
        assert raced and snapshot(h.home) == raced[0]
    no_sensitive_publication(h)


async def test_i1_recovery_url_union_is_rechecked_after_config_transaction_acquisition(harness, monkeypatch):
    h = harness
    forbid_key_revision(monkeypatch, h)
    async with client_for(h) as client:
        path = journal(h.home, target_url=NEW_URL)
        original = profile_store.application_config_transaction
        raced = []

        @contextmanager
        def transaction():
            # Different from the earlier under-lifecycle observation regression:
            # both guard observations have completed before this writer wins.
            pending = json.loads(path.read_text(encoding="utf-8"))
            pending["target_url"] = "https://changed-after-guard.invalid"
            path.write_text(json.dumps(pending), encoding="utf-8")
            raced.append({name: value for name, value in snapshot(h.home).items()
                          if name != "config.yaml.lock"})
            with original():
                yield

        monkeypatch.setattr(profile_store, "application_config_transaction", transaction)
        error(await mutate(client, "recover"), 409, "profile_revision_conflict")
        # S2 may create its stable OS lock file, but no journal/key/config write.
        after = {name: value for name, value in snapshot(h.home).items()
                 if name != "config.yaml.lock"}
        assert len(raced) == 1 and after == raced[0]
    no_sensitive_publication(h)


@pytest.mark.parametrize("kind", ("directory", "dangling-link", "regular-link"))
async def test_i1_private_binding_location_must_be_regular_unlinked_file(harness, monkeypatch, kind):
    h = harness
    path = h.home / "remote-bindings.json"
    target = h.home / "synthetic-binding-target.json"
    if kind == "directory":
        path.mkdir()
    else:
        if kind == "regular-link":
            target.write_text('{"version":1,"roots":{}}', encoding="utf-8")
        path.symlink_to(target)
    forbid_key_revision(monkeypatch, h)
    async with client_for(h) as client:
        before_config, before_keys = config(h.home), keys(h.home)
        error(await mutate(client, "set_credential"), 503, "consumer_lifecycle_unavailable")
        assert config(h.home) == before_config and keys(h.home) == before_keys
        assert not (h.home / "remote-profile-change.json").exists()
        assert path.is_dir() if kind == "directory" else path.is_symlink()
        if kind == "regular-link":
            assert target.read_text(encoding="utf-8") == '{"version":1,"roots":{}}'
        else:
            assert not target.exists()
    no_sensitive_publication(h)


async def test_i1_private_binding_locator_is_rechecked_after_read(harness, monkeypatch):
    h = harness
    forbid_key_revision(monkeypatch, h)
    async with client_for(h) as client:
        original = RemoteBindings.read

        def changed(self):
            result = original(self)
            monkeypatch.setattr(catalog_runtime.bindings, "bindings_file",
                                lambda: h.home / "unexpected-binding-location.json")
            return result

        monkeypatch.setattr(RemoteBindings, "read", changed)
        before = snapshot(h.home)
        error(await mutate(client, "set_credential"), 503, "consumer_lifecycle_unavailable")
        assert snapshot(h.home) == before
    no_sensitive_publication(h)


@pytest.mark.parametrize("environment", (False, True))
async def test_i14_synthetic_http_demotion_preserves_binding_env_and_url_delete_guard(harness, monkeypatch, environment):
    """Synthetic known inventory only: NOT positive production drain/release."""
    h = harness
    record = private_binding(h, state="confirmed")
    async with client_for(h) as client:
        if environment:
            monkeypatch.setenv(credentials.TOKEN_ENV_VAR, CANARY)
            monkeypatch.setenv(credentials.URL_ENV_VAR, P1["url"])
        success(await mutate(client, "clear_credential"), {**P1, "revision": 4},
                "environment" if environment else "none")
        selected = record.model_copy(update={"selection_state": "selected"})
        assert RemoteBindings().read().records == (selected,)
        assert os.environ.get(credentials.TOKEN_ENV_VAR) == (CANARY if environment else None)
        before = (config(h.home), keys(h.home), RemoteBindings().read())
        for operation in ("change_url", "delete"):
            error(await mutate(client, operation, revision=4), 409, "profile_in_use")
            assert (config(h.home), keys(h.home), RemoteBindings().read()) == before
        assert h.counts["credential_publication"] == h.counts["publication"] == 1


async def test_i14_synthetic_http_private_failure_is_redacted_and_precedes_key(harness, monkeypatch, caplog):
    """Fault injection only; not an accepted Remote/editor drain."""
    h = harness
    record = private_binding(h, state="confirmed")
    caplog.set_level(logging.DEBUG)
    async with client_for(h) as client:
        before = (config(h.home), keys(h.home))

        def failed(*args, **kwargs):
            raise RuntimeError(CANARY + OLD_KEY)

        monkeypatch.setattr(RemoteBindings, "_save_in_transaction", failed)
        error(await mutate(client, "set_credential"), 503, "consumer_lifecycle_unavailable")
        assert (config(h.home), keys(h.home)) == before
        assert RemoteBindings().read().records == (record,)
        assert (h.home / "remote-profile-change.json").exists()
        assert not h.counts["credential_publication"] and not h.counts["publication"]
        owner = h.app.state.remote_catalog.runtime
        with pytest.raises(ProfileError) as caught:
            owner.ready.mutations.recover(P1["profile_id"], expected_revision=3)
        clean_exception(caught.value)
    secret_free(caplog.text)
