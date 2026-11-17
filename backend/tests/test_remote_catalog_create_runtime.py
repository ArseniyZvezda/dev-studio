"""VS-146 C8: app-owned Create, with decoded fake transport only.

Run from backend with ``python -m pytest -n0 tests/test_remote_catalog_create_runtime.py``;
set --basetemp and -o cache_dir below
``D:\\VoxariumStudio\\.voxarium_studio\\temp\\TASK-146`` (and disable bytecode writes).
The private package also permits --noconftest/-o addopts='' without importing the
application, sync runner or server. Services, providers, lifecycle and journal
are real; only profile/key readers and the decoded wire boundary are synthetic.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import fields
import hashlib
import importlib.util
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import traceback
from types import ModuleType, SimpleNamespace

import pytest


_ROOT = Path(next(iter(importlib.util.find_spec("voxarium_studio").submodule_search_locations)))
for _suffix in ("", ".sync", ".api"):
    _package = ModuleType("_vs146_create_unit" + _suffix)
    _package.__path__ = [str(_ROOT / _suffix.lstrip("."))]
    _package.__package__ = _package.__name__
    sys.modules[_package.__name__] = _package

from _vs146_create_unit.core.application_config import RemoteServerProfile
from _vs146_create_unit.sync import credentials, catalog_runtime
from _vs146_create_unit.sync import catalog_create_runtime as owner
from _vs146_create_unit.sync.catalog import CatalogError, CatalogResponse
from _vs146_create_unit.sync.catalog_create_service import CatalogCreateService
from _vs146_create_unit.sync.catalog_intents import CatalogIntent, CatalogIntentJournal
from _vs146_create_unit.sync.catalog_providers import PendingCatalogProviders, S2CatalogReaders
from _vs146_create_unit.sync.profile_lifecycle import BindingInventory, ProfileLifecycle
from _vs146_create_unit.sync.profiles import ProfileError


PROFILE = "00000000-0000-4000-8000-000000000146"
OTHER = "00000000-0000-4000-8000-000000000147"
URL = "https://create.invalid/deploy/prefix"
OTHER_URL = "https://other.invalid/deploy/prefix"
TOKEN = "VS146-SYNTHETIC-PRIVATE-CANARY-NEVER-PERSIST-146"
OP = "vs146-explicit-command"
LABEL = "  Cafe\u0301 / Project  "
WIRE_LABEL = "Caf\u00e9 / Project"
JOURNAL = "remote-catalog-intents.sqlite3"


class SocketFreeLoop(getattr(asyncio, "ProactorEventLoop", asyncio.SelectorEventLoop)):
    """Events/timers only: even asyncio's wakeup socket cannot listen."""

    def _make_self_pipe(self):
        self._ssock = None
        self._csock = SimpleNamespace(fileno=lambda: -1)

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        raise AssertionError("No cross-thread or socket work in C8 tests")

    def _loop_self_reading(self, *_args):
        pass


def response(body, status=200):
    return CatalogResponse(status, deepcopy(body))


def health():
    return response({"service": "voxarium_studio_remote", "version": "synthetic-v2",
                     "protocol": 2, "supported_protocols": [2], "catalog_api": 2})


def identity():
    return response({"protocol": 2, "catalog_api": 2,
                     "capabilities": ["project_catalog", "explicit_project_create", "strict_connect"],
                     "user": {"id": "synthetic-user", "display_name": "Same", "color": "#3e63dd"}})


def project(status=201, *, id="alpha"):
    return response({"protocol": 2, "project": {
        "id": id, "display_name": WIRE_LABEL,
        "storage_epoch": "0123456789abcdef0123456789abcdef",
        "metadata_revision": 1, "created_at": "2026-11-16T12:00:00Z",
    }}, status)


def missing():
    return response({"error": {"code": "project_not_found", "message": TOKEN,
                               "request_id": "synthetic"}}, 404)


class DecodedTransport:
    def __init__(self):
        self.calls = []
        self.hook = None
        self.post_response = project()
        self.get_response = project(200)

    async def request(self, method, url, *, headers, params, json, follow_redirects):
        call = dict(method=method, url=url, headers=dict(headers), params=dict(params),
                    json=deepcopy(json), follow_redirects=follow_redirects)
        self.calls.append(call)
        assert follow_redirects is False and not params
        if self.hook is not None:
            await self.hook(call)
        if url.endswith("/healthz"):
            return health()
        if url.endswith("/api/v2/me"):
            return identity()
        reply = self.post_response if method == "POST" else self.get_response
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def posts(self):
        return [call for call in self.calls if call["method"] == "POST"]


def clean_error(error, code):
    assert type(error) is CatalogError
    assert error.code == code and error.args == (code,)
    assert error.__context__ is None and error.__cause__ is None
    assert TOKEN not in "".join(traceback.format_exception(error))


class Synthetic:
    def __init__(self, tmp, mp):
        self.home = tmp / "synthetic-home"
        self.home.mkdir()
        self.path = self.home / JOURNAL
        self.mp = mp
        self.loop = SocketFreeLoop()
        self.calls = Counter()
        self.rows = {
            PROFILE: RemoteServerProfile(profile_id=PROFILE, name="Same", url=URL, revision=1),
            OTHER: RemoteServerProfile(profile_id=OTHER, name="Same", url=OTHER_URL, revision=3),
        }
        self.token = TOKEN
        self.transport = DecodedTransport()
        self.check_error = None
        self.runtimes = []
        self.lifecycles = []
        self.forbidden = Counter()
        mp.setenv("VOXARIUM_STUDIO_HOME", str(self.home))
        mp.delenv(credentials.TOKEN_ENV_VAR, raising=False)
        mp.delenv(credentials.URL_ENV_VAR, raising=False)
        for name in ("getaddrinfo", "create_connection", "gethostbyname", "gethostbyaddr"):
            mp.setattr(socket, name, self.deny("network"))
        for name in ("connect", "connect_ex", "bind", "listen"):
            mp.setattr(socket.socket, name, self.deny("network"))
        mp.setattr(subprocess, "Popen", self.deny("process"))
        for name in ("load_profile_token", "load_token", "save_profile_token", "save_token"):
            mp.setattr(credentials, name, self.deny("real_credentials"))
        mp.setattr(Path, "home", classmethod(lambda cls: self.deny("default_home")()))

    def deny(self, category):
        def forbidden(*_args, **_kwargs):
            self.forbidden[category] += 1
            raise AssertionError("C8 forbidden " + category)
        return forbidden

    def check(self):
        self.calls["check"] += 1
        if self.check_error:
            raise ProfileError(self.check_error)

    def read(self, profile_id):
        self.calls["profile"] += 1
        if profile_id not in self.rows:
            raise ProfileError("profile_not_found")
        return self.rows[profile_id]

    def key(self, profile_id, url):
        self.calls["key"] += 1
        assert self.rows[profile_id].url == url
        return self.token

    def providers(self, profile_id, *, expected_revision):
        self.calls["providers"] += 1
        row = self.read(profile_id)
        readers = S2CatalogReaders(
            self.home, self.read, self.key, lambda: self.home / "config.yaml",
            lambda: self.home / "remote-keys.json", lambda: self.home / "remote-profile-change.json",
        )
        return PendingCatalogProviders(readers, profile_id=profile_id,
                                       expected_url=row.url, expected_revision=expected_revision)

    def make(self, *, transport=True):
        lifecycle = ProfileLifecycle(profile_reader=self.read, inventory=lambda _: BindingInventory(
            state="known", drained=True))
        runtime = owner.CatalogCreateRuntime(
            home=self.home, check=self.check, providers=self.providers, lifecycle=lifecycle,
            transport=self.transport if transport else None,
        )
        self.runtimes.append(runtime)
        self.lifecycles.append(lifecycle)
        return runtime

    def run(self, coroutine):
        return self.loop.run_until_complete(asyncio.wait_for(coroutine, timeout=15))

    def active(self, lifecycle=None):
        with (lifecycle or self.lifecycles[-1]).consumer_guard(PROFILE) as consumers:
            return consumers.active_roots

    def record(self, state="reserved"):
        intent = CatalogIntent(OP, PROFILE, URL, 1, "alpha", LABEL)
        journal = CatalogIntentJournal(self.path)
        journal.reserve(intent)
        if state != "reserved":
            journal.record_state(intent, state=state)
        return intent


@pytest.fixture
def s(tmp_path, monkeypatch, caplog):
    fixture = Synthetic(tmp_path, monkeypatch)
    yield fixture
    for runtime in reversed(fixture.runtimes):
        fixture.run(runtime.close())
    assert not asyncio.all_tasks(fixture.loop)
    fixture.loop.close()
    assert not fixture.forbidden, dict(fixture.forbidden)
    assert TOKEN not in caplog.text
    for path in fixture.home.glob(JOURNAL + "*"):
        if path.is_file():
            raw = path.read_bytes()
            assert TOKEN.encode() not in raw and TOKEN.encode("utf-16-le") not in raw
            for digest in (hashlib.sha256, hashlib.sha1, hashlib.md5):
                assert digest(TOKEN.encode()).digest() not in raw
                assert digest(TOKEN.encode()).hexdigest().encode() not in raw


async def create(runtime, **changes):
    args = dict(expected_revision=1, id="alpha", display_name=LABEL, operation_id=OP)
    profile_id = changes.pop("profile_id", PROFILE)
    args.update(changes)
    return await runtime.create_project(profile_id, **args)


def fails(s, coroutine, code):
    with pytest.raises(CatalogError) as caught:
        s.run(coroutine)
    clean_error(caught.value, code)


def test_constructor_and_close_are_lazy_but_explicit_recovery_may_open_journal(s):
    runtime = s.make()
    assert not s.path.exists() and not s.calls and not s.transport.calls
    s.run(runtime.close())
    assert not s.path.exists() and not s.transport.calls
    runtime = s.make()
    fails(s, runtime.recover_create(PROFILE, OP, expected_revision=1), "operation_not_found")
    assert s.path.is_file() and s.calls["key"] == 0
    s.run(runtime.close())
    assert not s.transport.calls


@pytest.mark.parametrize("action", ["create", "recover"])
def test_default_r3_gate_precedes_journal_providers_and_keys(s, action):
    runtime = s.make(transport=False)
    # Even corrupt local data cannot move the production gate behind storage.
    s.path.write_bytes(b"not a journal")
    before = s.path.read_bytes()
    call = create(runtime) if action == "create" else runtime.recover_create(PROFILE, OP, expected_revision=1)
    fails(s, call, "service_unavailable")
    assert s.path.read_bytes() == before and not s.calls and not s.transport.calls


def test_remote_ready_composes_closed_gate_without_opening_journal(s, monkeypatch):
    events = []

    class Pool:
        def __init__(self):
            events.append("pool")

        async def aclose(self):
            events.append("transport_closed")

        request = s.deny("production_transport")

    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport", Pool)
    runtime = catalog_runtime.RemoteCatalogRuntime()

    async def scenario():
        ready = await runtime.start()
        assert isinstance(ready.create, owner.CatalogCreateRuntime)
        assert (await runtime.start()).create is ready.create
        assert ready.metadata.list() == ()
        assert not s.path.exists()
        with pytest.raises(CatalogError) as caught:
            await create(ready.create)
        clean_error(caught.value, "service_unavailable")
        with pytest.raises(CatalogError) as caught:
            await ready.create.recover_create(PROFILE, OP, expected_revision=1)
        clean_error(caught.value, "service_unavailable")
        assert not s.path.exists()
        original = ready.create.close

        async def close_create():
            events.append("create_closing")
            await original()
            events.append("create_closed")

        monkeypatch.setattr(ready.create, "close", close_create)
        await runtime.close()
        with pytest.raises(CatalogError) as caught:
            await create(ready.create)
        clean_error(caught.value, "service_unavailable")

    try:
        s.run(scenario())
    finally:
        s.run(runtime.close())
    assert events == ["pool", "create_closing", "create_closed", "transport_closed"]


def test_stable_real_service_and_provider_per_captured_binding(s, monkeypatch):
    instances = []
    original = CatalogCreateService.__init__

    def constructed(service, **kwargs):
        instances.append((service, kwargs))
        return original(service, **kwargs)

    monkeypatch.setattr(CatalogCreateService, "__init__", constructed)
    runtime = s.make()
    first = s.run(create(runtime))
    second = s.run(create(runtime, operation_id="second-command"))
    assert first.state == second.state == "created"
    assert s.run(create(runtime)) == first
    assert len(instances) == 1
    assert isinstance(instances[0][1]["profiles"], PendingCatalogProviders)
    assert instances[0][1]["mode"] == "durable"
    assert isinstance(instances[0][1]["journal"], CatalogIntentJournal)
    assert len(s.transport.posts) == 2
    assert s.transport.posts[0]["json"] == {"id": "alpha", "display_name": WIRE_LABEL}
    assert all(OP not in repr((call["url"], call["headers"], call["json"])) for call in s.transport.calls)
    assert s.path.is_file() and not s.active()
    with sqlite3.connect(s.path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(catalog_intents)")}
        assert columns == {field.name for field in fields(CatalogIntent)} | {"state"}


def test_concurrent_exact_replays_issue_at_most_one_post(s):
    runtime = s.make()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def hook(call):
            if call["method"] == "POST":
                entered.set()
                await release.wait()

        s.transport.hook = hook
        first = asyncio.create_task(create(runtime))
        await entered.wait()
        assert s.active() == ("catalog:create",)
        repeats = await asyncio.gather(*(create(runtime) for _ in range(24)))
        assert all(result.state == "unknown" for result in repeats)
        assert len(s.transport.posts) == 1
        release.set()
        result = await first
        assert result.state == "created"
        assert await create(runtime) == result
        assert not s.active()

    s.run(scenario())
    assert len(s.transport.posts) == 1


@pytest.mark.parametrize("changes", [{"revision": 2}, {"url": OTHER_URL}])
def test_changed_binding_gets_separate_service_without_rebinding_old_operation(s, monkeypatch, changes):
    instances = []
    original = CatalogCreateService.__init__

    def constructed(service, **kwargs):
        instances.append(service)
        return original(service, **kwargs)

    monkeypatch.setattr(CatalogCreateService, "__init__", constructed)
    runtime = s.make()
    s.run(create(runtime))
    s.rows[PROFILE] = s.rows[PROFILE].model_copy(update=changes)
    before = (s.calls["key"], len(s.transport.calls))
    fails(s, create(runtime), "profile_revision_conflict")
    assert (s.calls["key"], len(s.transport.calls)) == before
    result = s.run(create(runtime, operation_id="new-binding-command",
                          expected_revision=s.rows[PROFILE].revision))
    assert result.state == "created" and len(instances) == 2
    assert len(s.transport.posts) == 2
    assert s.transport.posts[-1]["url"] == s.rows[PROFILE].url + "/api/v2/projects"


@pytest.mark.parametrize("changes", [
    {"profile_id": OTHER}, {"profile_id": "not-a-profile"},
    {"id": "beta"}, {"id": "INVALID ID"}, {"id": None},
    {"display_name": WIRE_LABEL}, {"display_name": ""}, {"display_name": []},
    {"expected_revision": 2}, {"expected_revision": True}, {"expected_revision": None},
])
def test_global_exact_input_conflict_precedes_keys_even_for_invalid_input(s, changes):
    runtime = s.make()
    assert s.run(create(runtime)).state == "created"
    before = (s.calls["key"], len(s.transport.calls), s.calls["providers"])
    fails(s, create(runtime, **changes), "operation_conflict")
    assert (s.calls["key"], len(s.transport.calls), s.calls["providers"]) == before


@pytest.mark.parametrize("changes,code", [
    ({"id": "INVALID ID"}, "invalid_id"),
    ({"display_name": ""}, "invalid_display_name"),
    ({"expected_revision": True}, "invalid_catalog_readers"),
])
def test_invalid_command_retains_global_reservation(s, changes, code):
    runtime = s.make()
    fails(s, create(runtime, **changes), code)
    fails(s, create(runtime, **changes), code)
    fails(s, create(runtime), "operation_conflict")
    fails(s, create(runtime, profile_id=OTHER), "operation_conflict")
    assert s.calls["key"] == 0 and not s.transport.calls


def test_invalid_profile_reservation_is_not_repaired_by_a_valid_profile(s):
    runtime = s.make()
    fails(s, create(runtime, profile_id="not-a-profile"), "profile_not_found")
    fails(s, create(runtime), "operation_conflict")
    assert s.calls["key"] == 0 and not s.transport.calls


@pytest.mark.parametrize("changed", [{"profile_id": OTHER}, {"expected_revision": True}])
def test_recovery_conflicts_are_global_before_keys(s, changed):
    runtime = s.make()
    s.run(create(runtime))
    before = (s.calls["key"], len(s.transport.calls))
    args = dict(profile_id=PROFILE, expected_revision=1)
    args.update(changed)
    profile_id = args.pop("profile_id")
    fails(s, runtime.recover_create(profile_id, OP, **args), "operation_conflict")
    assert (s.calls["key"], len(s.transport.calls)) == before


@pytest.mark.parametrize("found", [True, False])
def test_live_unknown_recovery_is_get_only_and_never_regrants_post(s, found):
    runtime = s.make()
    s.transport.post_response = OSError(TOKEN)
    assert s.run(create(runtime)).state == "unknown"
    s.transport.get_response = project(200) if found else missing()
    before = len(s.transport.calls)
    result = s.run(runtime.recover_create(PROFILE, OP, expected_revision=1))
    assert result.state == ("found" if found else "absent")
    observed = s.transport.calls[before:]
    assert [call["method"] for call in observed] == ["GET", "GET", "GET"]
    assert observed[-1]["url"] == URL + "/api/v2/projects/alpha"
    assert s.run(create(runtime)) == result and len(s.transport.posts) == 1
    assert not CatalogIntentJournal(s.path).reserve(s.record(result.state)).post_permitted


def test_live_recovery_never_captures_replacement_key(s):
    runtime = s.make()
    s.run(create(runtime))
    s.token = "synthetic-replacement-not-the-original"
    before = len(s.transport.calls)
    fails(s, runtime.recover_create(PROFILE, OP, expected_revision=1), "credential_binding_conflict")
    assert len(s.transport.calls) == before


@pytest.mark.parametrize("state", ["reserved", "created", "found", "unknown", "conflict", "rejected", "absent"])
@pytest.mark.parametrize("action", ["create", "recover"])
def test_cold_journal_metadata_never_proves_credentials(s, state, action):
    s.record(state)
    before = s.path.read_bytes()
    runtime = s.make()
    call = create(runtime) if action == "create" else runtime.recover_create(PROFILE, OP, expected_revision=1)
    fails(s, call, "credential_binding_unproven")
    assert s.calls["key"] == 0 and not s.transport.calls
    assert s.path.read_bytes() == before


def test_actual_close_reopen_cannot_turn_created_into_live_proof(s):
    first = s.make()
    assert s.run(create(first)).state == "created"
    s.run(first.close())
    before = (s.calls["key"], len(s.transport.calls))
    second = s.make()
    fails(s, create(second), "credential_binding_unproven")
    fails(s, second.recover_create(PROFILE, OP, expected_revision=1), "credential_binding_unproven")
    assert (s.calls["key"], len(s.transport.calls)) == before


@pytest.mark.parametrize("changes", [{"profile_id": OTHER}, {"id": None}, {"expected_revision": True}])
def test_cold_conflicts_precede_key_lookup(s, changes):
    s.record("created")
    runtime = s.make()
    fails(s, create(runtime, **changes), "operation_conflict")
    assert s.calls["key"] == 0 and not s.transport.calls


def test_capacity_is_1024_without_eviction_or_rebinding(s):
    assert owner.MAX_LIVE_OPERATIONS == 1024
    runtime = s.make()

    async def scenario():
        original = await create(runtime)
        assert original.state == "created"
        for index in range(1023):
            with pytest.raises(CatalogError) as caught:
                await create(runtime, operation_id=f"invalid-{index}", id="INVALID")
            clean_error(caught.value, "invalid_id")
        with pytest.raises(CatalogError) as caught:
            await create(runtime, operation_id="overflow", id="INVALID")
        clean_error(caught.value, "service_unavailable")
        with pytest.raises(CatalogError) as caught:
            await create(runtime, operation_id="valid-overflow")
        clean_error(caught.value, "service_unavailable")
        with pytest.raises(CatalogError) as caught:
            await create(runtime, operation_id="invalid-0", id="INVALID")
        clean_error(caught.value, "invalid_id")
        with pytest.raises(CatalogError) as caught:
            await create(runtime, operation_id="invalid-0")
        clean_error(caught.value, "operation_conflict")
        assert await create(runtime) == original
        assert (await runtime.recover_create(PROFILE, OP, expected_revision=1)).state == "found"

    s.run(scenario())
    assert len(s.transport.posts) == 1


@pytest.mark.parametrize("damage", ["empty", "corrupt", "directory"])
def test_existing_invalid_journal_is_not_repaired_or_replaced(s, damage):
    if damage == "directory":
        s.path.mkdir()
        before = None
    else:
        before = b"" if damage == "empty" else b"not sqlite or an empty catalog"
        s.path.write_bytes(before)
    runtime = s.make()
    with pytest.raises(CatalogError) as caught:
        s.run(create(runtime))
    # Windows may refuse O_EXCL on a directory before file-kind validation.
    expected = {"intent_store_invalid"}
    if damage == "directory":
        expected.add("intent_store_unavailable")
    assert caught.value.code in expected
    clean_error(caught.value, caught.value.code)
    assert s.calls["key"] == 0 and not s.transport.calls
    if before is None:
        assert s.path.is_dir()
    else:
        assert s.path.read_bytes() == before


@pytest.mark.parametrize("damage", ["loss", "corrupt"])
def test_live_journal_loss_or_corruption_poison_owner_without_fallback(s, damage):
    runtime = s.make()
    s.run(create(runtime))
    if damage == "loss":
        s.path.unlink()
    else:
        s.path.write_bytes(b"corruption must survive")
    before = (s.calls["key"], len(s.transport.calls))
    with pytest.raises(CatalogError) as caught:
        s.run(create(runtime))
    assert caught.value.code in {"intent_store_invalid", "intent_store_unavailable"}
    with pytest.raises(CatalogError) as caught:
        s.run(create(runtime, operation_id="new-after-loss"))
    assert caught.value.code in {"intent_store_invalid", "intent_store_unavailable"}
    assert (s.calls["key"], len(s.transport.calls)) == before
    if damage == "loss":
        assert not s.path.exists()
    else:
        assert s.path.read_bytes() == b"corruption must survive"


@pytest.mark.parametrize("damage", ["loss", "corrupt"])
def test_journal_damage_during_await_stops_next_dispatch_and_latches_owner(s, damage):
    runtime = s.make()

    async def hook(call):
        assert call["url"] == URL + "/healthz"
        if damage == "loss":
            s.path.unlink()
        else:
            s.path.write_bytes(b"damaged while health was in flight")
        await asyncio.sleep(0)

    s.transport.hook = hook
    with pytest.raises(CatalogError) as caught:
        s.run(create(runtime))
    assert caught.value.code in {"intent_store_invalid", "intent_store_unavailable"}
    clean_error(caught.value, caught.value.code)
    assert len(s.transport.calls) == 1 and not s.transport.posts
    before = (s.calls["key"], len(s.transport.calls))
    with pytest.raises(CatalogError) as caught:
        s.run(create(runtime, operation_id="after-inflight-damage"))
    assert caught.value.code in {"intent_store_invalid", "intent_store_unavailable"}
    assert (s.calls["key"], len(s.transport.calls)) == before


def test_pending_s2_check_blocks_explicit_command_before_journal_and_key(s):
    runtime = s.make()
    s.check_error = "profile_recovery_required"
    # The direct runtime preserves the trusted owner check's typed ProfileError;
    # no worker, journal, provider or credential can be admitted by this failure.
    with pytest.raises(ProfileError, match="^profile_recovery_required$"):
        s.run(create(runtime))
    assert not s.path.exists() and s.calls["key"] == 0 and not s.transport.calls


def test_missing_home_is_never_created_and_fails_before_keys(s):
    s.home.rmdir()
    runtime = s.make()
    fails(s, create(runtime), "intent_store_unavailable")
    assert not s.home.exists() and s.calls["key"] == 0 and not s.transport.calls


def test_owner_acl_protects_empty_db_and_rollback_sidecar_before_sqlite_write(s, monkeypatch):
    secured = []
    connects = []
    restrict = owner._owner_only
    connect = owner._OwnerOnlyJournal._connect

    def protect(path):
        # Both strict platform ACL policy and real local SQLite remain active.
        secured.append((path, path.stat().st_size))
        restrict(path)

    def connected(journal):
        rollback = Path(str(s.path) + "-journal")
        assert s.path in [path for path, _ in secured]
        assert rollback in [path for path, _ in secured]
        assert rollback.is_file()
        if not connects:
            assert (s.path, 0) in secured and (rollback, 0) in secured
        connects.append(journal)
        return connect(journal)

    monkeypatch.setattr(owner, "_owner_only", protect)
    monkeypatch.setattr(owner._OwnerOnlyJournal, "_connect", connected)
    runtime = s.make()
    assert not secured and not connects
    assert s.run(create(runtime)).state == "created"
    assert connects and secured
    assert all(path.parent == s.home and path.name.startswith(JOURNAL) for path, _ in secured)


def _assert_native_owner_only(path):
    if os.name != "nt":
        info = path.stat()
        assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
        return
    import ctypes
    from ctypes import wintypes

    api, kernel = credentials._windows_acl_api()
    convert = api.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    owner_sid, descriptor, sid_text, sddl = (ctypes.c_void_p() for _ in range(4))
    try:
        assert api.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner_sid), None,
                                        None, None, ctypes.byref(descriptor)) == 0
        assert api.ConvertSidToStringSidW(owner_sid, ctypes.byref(sid_text))
        assert convert(descriptor, 1, 4, ctypes.byref(sddl), None)
        # AI is an informational auto-inherited control bit, not an inherited
        # ACE. Both representations must be protected and contain ONLY owner.
        assert ctypes.wstring_at(sddl) in {
            f"D:P(A;;FA;;;{ctypes.wstring_at(sid_text)})",
            f"D:PAI(A;;FA;;;{ctypes.wstring_at(sid_text)})",
        }
    finally:
        for allocation in (sddl, sid_text, descriptor):
            if allocation.value:
                kernel.LocalFree(allocation)


def test_native_acl_of_db_and_recreated_journal_inside_write_transactions(s, monkeypatch):
    transaction = owner._OwnerOnlyJournal._transaction
    checked = []

    def secured_transaction(journal, action, **kwargs):
        def checked_action(db, records):
            result = action(db, records)
            # Inspect the actual OS ACL AFTER SQLite wrote, BEFORE commit can
            # delete the rollback file. A pre-connect spy alone is insufficient.
            _assert_native_owner_only(s.path)
            sidecar = Path(str(s.path) + "-journal")
            assert sidecar.is_file()
            _assert_native_owner_only(sidecar)
            checked.append((sidecar.stat().st_size, db.in_transaction))
            return result
        return transaction(journal, checked_action, **kwargs)

    monkeypatch.setattr(owner._OwnerOnlyJournal, "_transaction", secured_transaction)
    runtime = s.make()
    assert s.run(create(runtime)).state == "created"
    assert s.run(create(runtime, operation_id="second-native-acl")).state == "created"
    assert any(size > 0 for size, _ in checked) and all(inside for _, inside in checked)


def test_unsupported_wal_header_is_refused_before_sqlite_can_create_sidecars(s, monkeypatch):
    s.record("created")
    with sqlite3.connect(s.path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    # sqlite3's connection context manager doesn't close the connection.
    db.close()
    assert s.path.read_bytes()[18:20] == b"\x02\x02"
    assert not Path(str(s.path) + "-wal").exists()
    assert not Path(str(s.path) + "-shm").exists()
    before = s.path.read_bytes()
    monkeypatch.setattr(owner._OwnerOnlyJournal, "_connect", s.deny("sqlite_on_wal_header"))
    runtime = s.make()
    fails(s, create(runtime), "intent_store_invalid")
    assert s.path.read_bytes() == before
    assert not Path(str(s.path) + "-wal").exists()
    assert not Path(str(s.path) + "-shm").exists()
    assert s.calls["key"] == 0 and not s.transport.calls


@pytest.mark.parametrize("failure", ["open", "provider", "lifecycle"])
def test_failures_before_service_do_not_release_global_command(s, monkeypatch, failure):
    runtime = s.make()
    code = "intent_store_unavailable" if failure == "open" else "profile_store_unavailable"
    if failure == "open":
        def fail(_path):
            raise PermissionError(TOKEN)
        monkeypatch.setattr(owner, "_owner_only", fail)
    elif failure == "provider":
        def fail(*_args, **_kwargs):
            raise OSError(TOKEN)
        monkeypatch.setattr(runtime, "_providers", fail)
    else:
        code = "consumer_lifecycle_unavailable"
        s.lifecycles[-1].close_admission()
    fails(s, create(runtime), code)
    fails(s, create(runtime, id="changed-after-failure"), "operation_conflict")
    fails(s, create(runtime), code)
    assert not s.transport.calls and s.calls["key"] == 0


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_acl_failure_before_write_poison_owner_without_key_or_fallback(s, monkeypatch, suffix):
    if suffix in {"-wal", "-shm"}:
        # SQLite is in DELETE mode, but pre-existing sidecars still require ACL.
        s.record()
        Path(str(s.path) + suffix).write_bytes(b"")
    failed_path = Path(str(s.path) + suffix)
    restrict = owner._owner_only
    hit = []

    def protect(path):
        if path == failed_path:
            hit.append(path)
            raise PermissionError(TOKEN)
        restrict(path)

    monkeypatch.setattr(owner, "_owner_only", protect)
    monkeypatch.setattr(owner._OwnerOnlyJournal, "_connect", s.deny("sqlite_after_acl_failure"))
    runtime = s.make()
    fails(s, create(runtime), "intent_store_unavailable")
    assert hit and s.calls["key"] == 0 and not s.transport.calls
    monkeypatch.setattr(owner, "_owner_only", restrict)
    fails(s, create(runtime, operation_id="new-after-acl-failure"), "intent_store_unavailable")
    assert s.calls["key"] == 0 and not s.transport.calls


@pytest.mark.parametrize("shutdown", [False, True])
def test_repeated_cancellation_joins_cleanup_and_holds_logical_lease(s, shutdown):
    runtime = s.make()

    async def scenario():
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def hook(call):
            if call["method"] == "POST":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup.set()
                    await release.wait()
                    raise

        s.transport.hook = hook
        request = asyncio.create_task(create(runtime))
        await entered.wait()
        waiter = asyncio.create_task(runtime.close()) if shutdown else request
        if not shutdown:
            request.cancel(TOKEN)
        await cleanup.wait()
        assert not waiter.done() and not request.done()
        # A logical lease, not a thread lock, spans the await and cleanup.
        lifecycle = s.lifecycles[-1]
        assert lifecycle._lock.acquire(blocking=False)
        lifecycle._lock.release()
        assert lifecycle._leases
        waiter.cancel(TOKEN)
        await asyncio.sleep(0)
        waiter.cancel(TOKEN)
        await asyncio.sleep(0)
        assert not waiter.done() and not request.done()
        if shutdown:
            with pytest.raises(CatalogError) as caught:
                await create(runtime, operation_id="during-close")
            clean_error(caught.value, "service_unavailable")
        else:
            assert s.active() == ("catalog:create",)
        release.set()
        outcomes = await asyncio.gather(waiter, request, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert all(TOKEN not in str(value) for value in outcomes)
        lifecycle._reap_available()
        assert not lifecycle._leases

    s.run(scenario())
    assert len(s.transport.posts) == 1
    record = CatalogIntentJournal(s.path).lookup(OP)
    assert not CatalogIntentJournal(s.path).reserve(record.intent).post_permitted
    if not shutdown:
        s.transport.hook = None
        assert s.run(runtime.recover_create(PROFILE, OP, expected_revision=1)).state == "found"
        assert len(s.transport.posts) == 1


@pytest.mark.parametrize("observe", [False, True])
@pytest.mark.parametrize("caller_first", [False, True])
def test_overlapping_caller_cancel_and_shutdown_never_recancels_cleanup(s, observe, caller_first):
    runtime = s.make()
    if observe:
        assert s.run(create(runtime)).state == "created"

    async def scenario():
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cleaned = []

        async def hook(call):
            target = call["url"].endswith("/api/v2/projects/alpha") if observe else call["method"] == "POST"
            if target:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup.set()
                    await release.wait()
                    cleaned.append(True)
                    raise

        s.transport.hook = hook
        command = runtime.recover_create(PROFILE, OP, expected_revision=1) if observe else create(runtime)
        request = asyncio.create_task(command)
        await entered.wait()
        if caller_first:
            request.cancel(TOKEN)
            await cleanup.wait()
            closing = asyncio.create_task(runtime.close())
        else:
            closing = asyncio.create_task(runtime.close())
            await cleanup.wait()
            request.cancel(TOKEN)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closing.done() and not request.done() and not cleaned
        assert s.active() == ("catalog:create",)
        release.set()
        outcomes = await asyncio.gather(closing, request, return_exceptions=True)
        assert cleaned == [True] and isinstance(outcomes[1], asyncio.CancelledError)
        assert not s.active()

    s.run(scenario())
    assert len(s.transport.posts) == 1
    # Shutdown drops live proof, not the durable reservation.
    cold = s.make()
    before = (s.calls["key"], len(s.transport.calls))
    fails(s, create(cold), "credential_binding_unproven")
    assert (s.calls["key"], len(s.transport.calls)) == before
