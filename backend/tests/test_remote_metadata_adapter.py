"""VS-125 scoped offline wiring: real S2 -> observer -> adapter -> unmounted GET.

Run standalone with --noconftest, disabled plugin autoload and --basetemp
inside TASK_TEMP. No API mounting, ASGI client, listener or Studio lifecycle.
Only fixtures and explicit race actors may write synthetic task-private files.
"""
from __future__ import annotations

import asyncio
import builtins
from collections import Counter
from contextlib import AsyncExitStack, contextmanager
from dataclasses import FrozenInstanceError, asdict
import importlib.abc
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import traceback
from types import ModuleType, SimpleNamespace
import warnings

import pytest
from starlette.requests import Request


TASK_TEMP = Path(r"D:\VoxariumStudio\.voxarium_studio\temp\TASK-125")
CANARY = "VS125-SYNTHETIC-PRIVATE-DO-NOT-ECHO-7231"
P1 = dict(profile_id="11111111-1111-4111-8111-111111111111", name="same",
          url="https://one.invalid/deployment/prefix", revision=3)
P2 = dict(profile_id="22222222-2222-4222-8222-222222222222", name="same",
          url="http://two.invalid/other", revision=8)
ROOT = "/api/remote/servers"
ZERO = ("network", "dns", "listeners", "runtime", "runtime_import", "verify",
        "clock", "transport", "token_lookup", "writes", "locks", "cache",
        "outside_reads", "journal_reads", "adapter_io")
TOTAL = Counter({name: 0 for name in ZERO})
COLLECTION = Counter(runtime_import=0)


def _runtime(name):
    roots = ("voxarium_studio", "_vs125_unit")
    return (name in ("voxarium_studio.api", "voxarium_studio.sync")
            or any(name == root + suffix or name.startswith(root + suffix + ".")
                   for root in roots for suffix in
                   (".main", ".core.context", ".core.workspace", ".sync.service")))


class ImportDeny(importlib.abc.MetaPathFinder):
    def __init__(self, denied):
        self.denied = denied

    def find_spec(self, fullname, path=None, target=None):
        if _runtime(fullname):
            self.denied()


def _collection_denied():
    COLLECTION["runtime_import"] += 1
    raise AssertionError("VS-125 collection attempted runtime composition")


# Public api/sync initializers eagerly compose runtime. Execute the *unchanged*
# owning files under a private namespace, as test_remote_api.py does. No mocked
# production modules, no public identities polluted for other test modules.
_root = next(iter(importlib.util.find_spec("voxarium_studio").submodule_search_locations))
for _suffix in ("", ".api", ".sync"):
    _package = ModuleType("_vs125_unit" + _suffix)
    _package.__path__ = [str(Path(_root) / _suffix.lstrip("."))]
    _package.__package__ = _package.__name__
    sys.modules[_package.__name__] = _package
_guard = ImportDeny(_collection_denied)
sys.meta_path.insert(0, _guard)
try:
    from _vs125_unit.api.remote_metadata import CatalogMetadataAdapter
    from _vs125_unit.api.remote import ServerProfile, UnavailableVerification, create_remote_router
    from _vs125_unit.core import application_config as app, config, paths
    from _vs125_unit.core.security import AuthSettings
    from _vs125_unit.sync import credentials, profiles, catalog_metadata
    from _vs125_unit.sync.catalog import CatalogClient
    from _vs125_unit.sync.catalog_metadata import CatalogMetadataObserver, CatalogMetadataRow
    from _vs125_unit.sync.catalog_providers import PendingCatalogProviders, S2CatalogReaders
    from _vs125_unit.sync.catalog_service import CatalogService
    from _vs125_unit.sync.catalog_verification import CatalogVerificationService
    from _vs125_unit.sync.profiles import ProfileError, RemoteProfiles
finally:
    sys.meta_path.remove(_guard)


class SocketFreeLoop(getattr(asyncio, "ProactorEventLoop", asyncio.SelectorEventLoop)):
    """Windows' usual asyncio self-pipe creates sockets; these tests need none."""
    def _make_self_pipe(self):
        self._ssock = None
        self._csock = SimpleNamespace(fileno=lambda: -1)

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        raise AssertionError("No cross-thread I/O in endpoint-unit tests")

    def _loop_self_reading(self, *_args):
        pass


class Synthetic:
    def __init__(self, tmp, mp):
        self.mp, self.home = mp, tmp / "synthetic-home"
        self.config = self.home / "config.yaml"
        self.keys = self.home / "remote-keys.json"
        self.journal = self.home / "remote-profile-change.json"
        self.active = self.actor = self.in_observer = False
        self.calls = Counter()
        self.counts = Counter({name: 0 for name in ZERO})
        self.hook = lambda *_: None
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                     "VOXARIUM_STUDIO_HOME", "CODEX_HOME"):
            mp.setenv(name, str(self.home))
        for name in (credentials.TOKEN_ENV_VAR, credentials.URL_ENV_VAR, "VOXARIUM_STUDIO_TOKEN"):
            mp.delenv(name, raising=False)
        mp.setattr(app, "application_config_file", lambda: self.config)
        mp.setattr(profiles, "application_config_file", lambda: self.config)
        mp.setattr(credentials, "keys_file", lambda: self.keys)
        self.repo = RemoteProfiles()
        self.loop = SocketFreeLoop()
        self.install_guards()
        self.observer = CatalogMetadataObserver(
            home=self.home,
            list_reader=lambda: self.read("list", self.repo.list),
            profile_reader=lambda pid: self.read("get", self.repo.get, pid),
            source_reader=lambda pid, url: self.read("source", credentials.profile_token_source, pid, url),
            application_file=lambda: self.config, credential_file=lambda: self.keys,
            journal_file=lambda: self.journal,
        )
        self.adapter = CatalogMetadataAdapter(observer=self.observer)
        # Mandatory services are deliberately unusable; GET must touch neither.
        forbidden_service = SimpleNamespace(verify=self.deny("verify"),
                                            list_projects=self.deny("transport"),
                                            clock=self.deny("clock"))
        self.router = create_remote_router(metadata=self.adapter, profiles=self.repo,
                                           verification=forbidden_service, catalog=forbidden_service)
        route = next(r for r in self.router.routes if r.path == ROOT and "GET" in r.methods)
        self.handler = route.get_route_handler()

    def deny(self, category):
        def denied(*_args, **_kwargs):
            self.counts[category] += 1
            raise AssertionError("VS-125 forbidden " + category)
        return denied

    @contextmanager
    def race_actor(self):
        previous, self.actor = self.actor, True
        try:
            yield
        finally:
            self.actor = previous

    def seed(self, rows=(P1,), token=CANARY):
        with self.race_actor():
            self.home.mkdir(parents=True, exist_ok=True)
            self.config.write_text(json.dumps({"remote_servers": list(rows)}), encoding="utf-8")
            self.keys.write_text(json.dumps({"version": 2, "profiles": {
                p["profile_id"]: {"url": p["url"], "token": token, "revision": 2}
                for p in rows}, "legacy": {}}), encoding="utf-8")

    def env(self, token=CANARY, url=P1["url"]):
        self.mp.setenv(credentials.TOKEN_ENV_VAR, token)
        self.mp.setenv(credentials.URL_ENV_VAR, url)

    def read(self, kind, reader, *args):
        self.calls[kind] += 1
        n = self.calls[kind]
        with self.race_actor():
            self.hook(kind, n, "before", args)
        value = reader(*args)
        with self.race_actor():
            self.hook(kind, n, "after", args)
        return value

    def install_guards(self):
        original_import = builtins.__import__
        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            resolved = importlib.util.resolve_name("." * level + name, globals["__package__"]) if level else name
            if _runtime(resolved) or any(_runtime(resolved + "." + part) for part in (fromlist or ())):
                return self.deny("runtime_import")()
            return original_import(name, globals, locals, fromlist, level)
        self.mp.setattr(builtins, "__import__", guarded_import)
        self.import_guard = ImportDeny(self.deny("runtime_import"))
        sys.meta_path.insert(0, self.import_guard)

        for owner, names, category in (
            (socket, ("socket", "create_connection", "socketpair"), "network"),
            (socket, ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"), "dns"),
            (socket, ("create_server",), "listeners"),
            (subprocess, ("Popen",), "runtime"),
            (credentials, ("load_profile_token", "load_token", "profile_token_revision"), "token_lookup"),
            (RemoteProfiles, ("load_token",), "token_lookup"),
            (RemoteProfiles, ("create", "rename", "delete", "change_url", "set_credential",
                              "clear_credential", "recover", "import_legacy_credential"), "writes"),
            (app, ("application_config_transaction",), "locks"),
            (profiles, ("application_config_transaction",), "locks"),
            (credentials, ("_mutation",), "locks"),
            (app, ("save_application_config", "_write_application_profiles"), "writes"),
            (profiles, ("_write_application_config",), "writes"),
            (credentials, ("_write_store", "save_profile_token", "save_token"), "writes"),
            (CatalogVerificationService, ("__init__", "verify"), "verify"),
            (CatalogService, ("__init__",), "transport"),
            (CatalogClient, ("__init__",), "transport"),
            (paths, ("user_config_dir",), "outside_reads"),
            (Path, ("home",), "outside_reads"),
            (config, ("load_config", "save_config"), "outside_reads"),
        ):
            for name in names:
                self.mp.setattr(owner, name, self.deny(category))
        original_time = time.time
        def clock():
            return self.deny("clock")() if self.active else original_time()
        self.mp.setattr(time, "time", clock)

        original_observe = CatalogMetadataObserver.observe
        def observe(observer):
            self.calls["observe"] += 1
            self.in_observer = True
            try:
                return original_observe(observer)
            finally:
                self.in_observer = False
        self.mp.setattr(CatalogMetadataObserver, "observe", observe)
        for cls, name, label in ((S2CatalogReaders, "check", "s2_check"),
                                 (PendingCatalogProviders, "get", "pending_get")):
            original = getattr(cls, name)
            def counted(instance, *args, _original=original, _label=label, **kwargs):
                self.calls[_label] += 1
                return _original(instance, *args, **kwargs)
            self.mp.setattr(cls, name, counted)

        def check(path, mode="r", *, metadata=False):
            if not self.active:
                return
            if isinstance(path, int):
                return self.deny("outside_reads")()
            path = Path(path).absolute()
            if self.actor:
                assert path.is_relative_to(TASK_TEMP), "race actor escaped task-private temp"
                return
            if not self.in_observer:
                return self.deny("adapter_io")()
            if any(c in mode for c in "wax+"):
                return self.deny("writes")()
            if path not in (self.home, self.config, self.keys, self.journal):
                return self.deny("outside_reads")()
            if path == self.journal and not metadata:
                return self.deny("journal_reads")()
        for owner, name in ((builtins, "open"), (io, "open")):
            original = getattr(owner, name)
            def opened(path, mode="r", *args, _original=original, **kwargs):
                check(path, mode)
                return _original(path, mode, *args, **kwargs)
            self.mp.setattr(owner, name, opened)
        original_open = os.open
        def os_open(path, flags, *args, **kwargs):
            check(path, "w" if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) else "r")
            return original_open(path, flags, *args, **kwargs)
        self.mp.setattr(os, "open", os_open)
        for name in ("mkdir", "makedirs", "unlink", "remove", "rename", "replace", "rmdir", "write", "symlink", "link"):
            original = getattr(os, name)
            def mutation(*args, _original=original, **kwargs):
                if self.active:
                    if not self.actor:
                        return self.deny("writes")()
                    assert Path(args[0]).absolute().is_relative_to(TASK_TEMP)
                return _original(*args, **kwargs)
            self.mp.setattr(os, name, mutation)
        original_stat = Path.stat
        def observed_stat(path, *args, **kwargs):
            check(path, metadata=True)
            return original_stat(path, *args, **kwargs)
        self.mp.setattr(Path, "stat", observed_stat)

    @contextmanager
    def observing(self):
        state = vars(self.adapter).copy()
        self.active = True
        try:
            yield
        finally:
            self.active = False
            if vars(self.adapter) != state:
                self.counts["cache"] += 1
            assert not any(self.counts.values()), dict(self.counts)

    def list(self):
        with self.observing():
            return self.adapter.list()

    def get(self, *, auth=None, query=b"", body=b""):
        async def invoke():
            request = Request({"type": "http", "method": "GET", "scheme": "http",
                               "path": ROOT, "root_path": "", "query_string": query,
                               "headers": [(b"host", b"127.0.0.1")],
                               "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 1),
                               "app": SimpleNamespace(state=SimpleNamespace(auth=auth or AuthSettings()))})
            request._body = body
            # Supply only FastAPI's dependency cleanup scopes, not an ASGI app
            # or middleware/lifespan. New FastAPI versions require these even
            # when the standalone endpoint has no yield dependencies.
            async with AsyncExitStack() as stack:
                for name in ("fastapi_middleware_astack", "fastapi_inner_astack", "fastapi_function_astack"):
                    request.scope[name] = stack
                with self.observing():
                    return await self.handler(request)
        response = self.loop.run_until_complete(invoke())
        assert response.headers["cache-control"] == "no-store"
        assert CANARY not in response.body.decode()
        return response.status_code, json.loads(response.body)

    def failure(self, code="metadata_unavailable", status=503):
        assert self.get() == (status, {"code": code, "details": {}})

    def fake_observe(self, result=None, error=None):
        def observe(_observer):
            self.calls["observe"] += 1
            if error is not None:
                raise error
            return result
        self.mp.setattr(CatalogMetadataObserver, "observe", observe)


@pytest.fixture
def synthetic(tmp_path, monkeypatch, caplog):
    assert tmp_path.absolute().is_relative_to(TASK_TEMP), "--basetemp must be in TASK-125"
    s = Synthetic(tmp_path, monkeypatch)
    yield s
    s.loop.close()
    sys.meta_path.remove(s.import_guard)
    TOTAL.update(s.counts)
    assert not any(s.counts.values()), dict(s.counts)
    assert CANARY not in caplog.text


@pytest.fixture(scope="module", autouse=True)
def report():
    yield
    assert COLLECTION == {"runtime_import": 0}
    assert not any(TOTAL.values())
    print("\nVS125_FORBIDDEN_COUNTERS=" + json.dumps(dict(TOTAL), sort_keys=True))
    print("VS125_COLLECTION_COUNTERS=" + json.dumps(dict(COLLECTION), sort_keys=True))


def row(**changes):
    value = CatalogMetadataRow(**dict(P1, has_token=True, credential_source="stored"))
    for name, content in changes.items():
        object.__setattr__(value, name, content)
    return value


def assert_clean(error, code):
    assert type(error) is ProfileError and error.args == (code,)
    assert error.__cause__ is None and error.__context__ is None
    assert CANARY not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("kind", ["missing_home", "missing_config", "empty"])
def test_real_empty_get_is_success_only_after_full_observation(synthetic, kind):
    s = synthetic
    if kind == "missing_config":
        s.home.mkdir()
    elif kind == "empty":
        s.seed(())
    assert s.get() == (200, {"items": []})
    assert s.calls["observe"] == 1 and s.calls["list"] == 2
    assert s.calls["s2_check"] >= 10 and s.calls["source"] == 0
    assert not s.journal.exists()
    if kind == "missing_home":
        assert not s.home.exists()


@pytest.mark.parametrize("name", ["same", "", " \t" + "history" * 30 + "\n"])
def test_two_exact_profiles_preserve_historical_names_and_unavailable(synthetic, name):
    s = synthetic
    second = dict(P2, name=name)
    s.seed((P1, second))
    expected = [dict(p, has_token=True, credential_source="stored", verification={"state": "unavailable"})
                for p in (P1, second)]
    assert s.get() == (200, {"items": expected})
    assert s.calls["observe"] == 1 and s.calls["list"] == 2 and s.calls["source"] == 4
    assert s.calls["pending_get"] > 0 and s.calls["s2_check"] > 0
    values = s.list()
    assert type(values) is tuple
    for value in values:
        assert type(value) is ServerProfile
        assert type(value.verification) is UnavailableVerification
        assert value.verification.model_dump() == {"state": "unavailable"}
    assert CANARY not in repr(values) + repr(s.adapter)


@pytest.mark.parametrize("kind,presence,source", [
    ("stored", True, "stored"), ("environment", True, "environment"),
    ("missing", False, "none"), ("tombstone", False, "none"),
    ("legacy", False, "none"), ("wrong_binding", False, "none"),
])
def test_real_presence_sources_are_not_verification(synthetic, kind, presence, source):
    s = synthetic
    s.seed(token=None if kind == "tombstone" else CANARY)
    if kind == "environment":
        s.env()
        # ENV wins but is not proof of stored-file integrity.
        s.keys.write_text("{ corrupt " + CANARY)
    elif kind == "missing":
        s.keys.unlink()
    elif kind == "legacy":
        s.keys.write_text(json.dumps({P1["url"]: CANARY}))
    elif kind == "wrong_binding":
        raw = json.loads(s.keys.read_text())
        raw["profiles"][P1["profile_id"]]["url"] = P2["url"]
        s.keys.write_text(json.dumps(raw))
    before = s.keys.read_bytes() if s.keys.exists() else None
    assert s.get() == (200, {"items": [dict(P1, has_token=presence, credential_source=source,
                                           verification={"state": "unavailable"})]})
    assert s.calls["observe"] == 1 and s.calls["source"] == 2
    assert (s.keys.read_bytes() if s.keys.exists() else None) == before


@pytest.mark.parametrize("token,url", [("", P1["url"]), (CANARY, ""),
                                      (CANARY, "not a URL " + CANARY), (CANARY, P2["url"])])
def test_unbound_environment_rejects_whole_list_no_stored_fallback(synthetic, token, url):
    s = synthetic
    s.seed((P1, P2))
    s.env(token, url)
    s.failure()
    assert s.calls["observe"] == 1 and s.calls["list"] == 2 and s.calls["source"] == 4


@pytest.mark.parametrize("store,code", [("config", "application_store_unavailable"),
                                       ("keys", "credential_store_unavailable")])
@pytest.mark.parametrize("damage", ["corrupt", "directory", "unreadable", "symlink"])
def test_real_damaged_store_is_fixed_failure_not_empty(synthetic, store, code, damage):
    s = synthetic
    s.seed()
    path = getattr(s, store)
    if damage == "corrupt":
        path.write_text("{ malformed " + CANARY)
    elif damage == "directory":
        path.unlink()
        path.mkdir()
    elif damage == "unreadable":
        original = Path.read_text
        def read(p, *args, **kwargs):
            if p == path:
                raise PermissionError(CANARY)
            return original(p, *args, **kwargs)
        s.mp.setattr(Path, "read_text", read)
    else:
        original = Path.lstat
        def lstat(p, *args, **kwargs):
            if p == path:
                return os.stat_result((stat.S_IFLNK,) + (0,) * 9)
            return original(p, *args, **kwargs)
        s.mp.setattr(Path, "lstat", lstat)
    s.failure(code)
    assert s.calls["observe"] == 1


@pytest.mark.parametrize("kind", ["other_profile", "directory", "dangling"])
def test_pending_global_journal_refuses_before_reads(synthetic, kind):
    s = synthetic
    s.seed()
    if kind == "directory":
        s.journal.mkdir()
    elif kind == "other_profile":
        s.journal.write_text(json.dumps({"profile_id": P2["profile_id"], "secret": CANARY}))
    else:
        original = Path.lstat
        def lstat(p, *args, **kwargs):
            if p == s.journal:
                return os.stat_result((stat.S_IFLNK,) + (0,) * 9)
            return original(p, *args, **kwargs)
        s.mp.setattr(Path, "lstat", lstat)
    s.failure("profile_recovery_required", 409)
    assert s.calls["observe"] == 1
    assert not any(s.calls[k] for k in ("list", "get", "source"))


@pytest.mark.parametrize("reader,code", [("list", "application_store_unavailable"),
                                        ("get", "application_store_unavailable"),
                                        ("source", "credential_store_unavailable")])
def test_failed_real_reader_is_sanitized_through_adapter_and_get(synthetic, reader, code):
    s = synthetic
    s.seed()
    def hook(kind, _n, phase, _args):
        if kind == reader and phase == "before":
            raise RuntimeError(CANARY)
    s.hook = hook
    s.failure(code)
    with pytest.raises(ProfileError) as caught:
        s.list()
    assert_clean(caught.value, code)
    assert s.calls["observe"] == 2


@pytest.mark.parametrize("operation,code,status", [
    ("pending", "profile_recovery_required", 409),
    ("url", "profile_revision_conflict", 409),
    ("revision", "profile_revision_conflict", 409),
    ("source", "metadata_unavailable", 503),
    ("membership", "metadata_unavailable", 503),
    ("rename", "metadata_unavailable", 503),
    ("locator", "profile_recovery_required", 409),
])
def test_detected_race_is_whole_get_failure_without_retry_or_rebinding(synthetic, operation, code, status):
    s = synthetic
    s.seed((P1, P2))
    source_args = []
    def hook(kind, n, phase, args):
        if kind == "source" and phase == "before":
            source_args.append(args)
        if operation in ("membership", "rename"):
            if (kind, n, phase) == ("list", 2, "before"):
                rows = [P1] if operation == "membership" else [P1, dict(P2, name="new label")]
                s.config.write_text(json.dumps({"remote_servers": rows}))
        elif (kind, n, phase) == ("source", 1, "after"):
            if operation == "pending":
                s.journal.write_text(CANARY)
            elif operation == "source":
                raw = json.loads(s.keys.read_text())
                raw["profiles"][P1["profile_id"]]["token"] = None
                s.keys.write_text(json.dumps(raw))
            elif operation == "locator":
                s.keys = s.home / "wrong-keys.json"
            else:
                changes = {"url": "https://changed.invalid/new"} if operation == "url" else {"revision": 4}
                s.config.write_text(json.dumps({"remote_servers": [dict(P1, **changes), P2]}))
    s.hook = hook
    s.failure(code, status)
    assert s.calls["observe"] == 1
    assert all(args in ((P1["profile_id"], P1["url"]), (P2["profile_id"], P2["url"])) for args in source_args)
    assert s.calls["list"] <= 2


def test_key_rotation_same_source_never_proves_continuity_and_repeated_get_has_no_cache(synthetic):
    s = synthetic
    s.seed()
    def rotate(kind, n, phase, _args):
        if (kind, n, phase) == ("source", 1, "after"):
            raw = json.loads(s.keys.read_text())
            raw["profiles"][P1["profile_id"]]["token"] = CANARY + "-rotated"
            s.keys.write_text(json.dumps(raw))
    s.hook = rotate
    expected = (200, {"items": [dict(P1, has_token=True, credential_source="stored",
                                     verification={"state": "unavailable"})]})
    assert s.get() == expected
    assert s.get() == expected
    s.keys.unlink()
    assert s.get() == (200, {"items": [dict(P1, has_token=False, credential_source="none",
                                           verification={"state": "unavailable"})]})
    s.journal.write_text(CANARY)
    s.failure("profile_recovery_required", 409)
    assert s.calls["observe"] == 4 and s.calls["list"] == 6


@pytest.mark.parametrize("field,value", [
    ("profile_id", True), ("profile_id", CANARY), ("name", None), ("name", [CANARY]),
    ("url", CANARY), ("url", P2["url"] + "/"), ("url", P2["url"] + "//"),
    ("revision", True), ("revision", 0), ("revision", "3"), ("revision", 3.0),
    ("has_token", 1), ("has_token", None), ("has_token", False),
    ("credential_source", "none"), ("credential_source", "environment_unbound"),
    ("credential_source", CANARY), ("verification", None), ("verification", "unchecked"),
    ("verification", "checking"), ("verification", {"state": "unavailable"}),
    ("verification", "valid"), ("verification", True),
])
def test_invalid_exact_row_refuses_whole_result_without_default_or_chain(synthetic, field, value):
    s = synthetic
    # Corrupt only one field of an otherwise valid, distinct P2. Even a URL
    # normalized by a faulty adapter remains distinct from P1, so duplicate
    # detection cannot mask missing scalar/discriminant/exact-binding checks.
    damaged = row(**{**P2, field: value})
    assert damaged.profile_id != P1["profile_id"] and damaged.url != P1["url"]
    s.fake_observe((row(), damaged))
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        s.failure()
        with pytest.raises(ProfileError) as caught:
            s.list()
    assert_clean(caught.value, "metadata_unavailable")
    assert not emitted, "unvalidated rows must not reach serializers that echo input"
    assert s.calls["observe"] == 2 and s.calls["list"] == 0


@pytest.mark.parametrize("kind", ["none", "list", "tuple_subclass", "dict", "object", "row_subclass",
                                  "missing_field", "missing_verification", "duplicate_id", "duplicate_url"])
def test_malformed_container_or_rows_never_become_empty_or_partial(synthetic, kind):
    s = synthetic
    class ExtendedRow(CatalogMetadataRow):
        pass
    class ExtendedTuple(tuple):
        pass
    if kind == "none":
        result = None
    elif kind == "list":
        result = [row()]
    elif kind == "tuple_subclass":
        result = ExtendedTuple((row(),))
    elif kind == "dict":
        result = (asdict(row()),)
    elif kind == "object":
        result = (object(),)
    elif kind == "row_subclass":
        result = (ExtendedRow(**dict(P1, has_token=True, credential_source="stored")),)
    elif kind.startswith("missing"):
        bad = row()
        object.__delattr__(bad, "name" if kind == "missing_field" else "verification")
        result = (bad,)
    elif kind == "duplicate_id":
        result = (row(), row(url=P2["url"]))
    else:
        result = (row(), row(profile_id=P2["profile_id"]))
    s.fake_observe(result)
    s.failure()
    assert s.calls["observe"] == 1


@pytest.mark.parametrize("source,presence", [("stored", False), ("environment", False),
                                            ("none", True), ("none", None),
                                            ("environment_unbound", None)])
def test_all_disallowed_source_presence_combinations_are_unavailable_failure(synthetic, source, presence):
    synthetic.fake_observe((row(credential_source=source, has_token=presence),))
    synthetic.failure()


@pytest.mark.parametrize("code", sorted(catalog_metadata._CODES))
def test_observer_fixed_codes_are_preserved_without_raw_exception_chains(synthetic, code):
    s = synthetic
    try:
        raise RuntimeError(CANARY)
    except RuntimeError as raw:
        try:
            raise ProfileError(code) from raw
        except ProfileError as error:
            forged = error
    s.fake_observe(error=forged)
    expected = "metadata_unavailable" if code == "invalid_catalog_readers" else code
    with pytest.raises(ProfileError) as caught:
        s.list()
    assert_clean(caught.value, expected)
    status = {"profile_recovery_required": 409, "profile_revision_conflict": 409,
              "profile_not_found": 404, "invalid_profile_metadata": 502}.get(expected, 503)
    s.failure(expected, status)
    assert s.calls["observe"] == 2


@pytest.mark.parametrize("kind", ["raw", "unknown_typed", "multi_args", "non_string", "subclass"])
def test_untrusted_observer_exceptions_cannot_echo_or_choose_wire_code(synthetic, kind):
    class ExtendedError(ProfileError):
        pass
    error = {"raw": RuntimeError(CANARY), "unknown_typed": ProfileError(CANARY),
             "multi_args": ProfileError("profile_not_found", CANARY),
             "non_string": ProfileError([CANARY]),
             "subclass": ExtendedError("profile_not_found")}[kind]
    synthetic.fake_observe(error=error)
    with pytest.raises(ProfileError) as caught:
        synthetic.list()
    assert_clean(caught.value, "metadata_unavailable")
    synthetic.failure()


@pytest.mark.parametrize("case,status,code", [("auth", 401, "studio_unauthorised"),
                                              ("query", 422, "invalid_request"),
                                              ("body", 422, "invalid_request")])
def test_existing_unmounted_auth_and_validation_remain_no_store(synthetic, case, status, code):
    options = {"auth": {"auth": AuthSettings(token=CANARY)},
               "query": {"query": b"unexpected=1"}, "body": {"body": b"{}"}}[case]
    assert synthetic.get(**options) == (status, {"code": code, "details": {}})
    assert synthetic.calls["observe"] == 0


def test_adapter_is_frozen_required_kw_only_and_has_no_implicit_factory(synthetic):
    signature = inspect.signature(CatalogMetadataAdapter)
    assert set(signature.parameters) == {"observer"}
    argument = signature.parameters["observer"]
    assert argument.default is inspect.Parameter.empty
    assert argument.kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        CatalogMetadataAdapter()
    with pytest.raises(TypeError):
        CatalogMetadataAdapter(synthetic.observer)
    with pytest.raises(FrozenInstanceError):
        synthetic.adapter.observer = None
    assert synthetic.calls["observe"] == 0
