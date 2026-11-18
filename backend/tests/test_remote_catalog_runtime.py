"""VS-140 C1/C4 composition, not a repeat of S2/observer acceptance.

Standalone: --noconftest, plugin autoload off, --basetemp under TASK_TEMP.
Unchanged owners execute under a private namespace to avoid heavy api/sync init.
Real services and CatalogHttpTransport use only controlled httpx.MockTransport.
No app, listeners, production homes or writable S2 APIs are used.
"""
from __future__ import annotations

import asyncio
import builtins
from collections import Counter
from contextlib import AsyncExitStack, contextmanager
from dataclasses import FrozenInstanceError
import importlib.abc
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback
from types import ModuleType, SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import routing as fastapi_routing
from starlette.requests import Request


TASK_TEMP = Path(os.environ.get("VOXARIUM_STUDIO_TEST_TASK_TEMP",
                               r"D:\VoxariumStudio\.voxarium_studio\temp\TASK-140\TASK-132"))
assert TASK_TEMP.is_absolute() and ".." not in TASK_TEMP.parts
assert TASK_TEMP.is_relative_to(Path(__file__).resolve().parents[2] / ".voxarium_studio" / "temp")
CANARY = "VS132-SYNTHETIC-PRIVATE-DO-NOT-ECHO-9571"
P1 = dict(profile_id="11111111-1111-4111-8111-111111111111", name="same",
          url="https://one.invalid/deployment/prefix", revision=3)
P2 = dict(profile_id="22222222-2222-4222-8222-222222222222", name="same",
          url="http://two.invalid/other", revision=8)
ZERO = ("network", "process", "s2_mutation", "writes", "transport", "verify", "clock",
        "default_home", "home_lookup", "token_lookup", "fd_io", "lifecycle_io",
        "outside_reads", "startup_or_journal_content")
TOTAL = Counter({key: 0 for key in ZERO})


def _heavy(name):
    return (name in {"voxarium_studio.api", "voxarium_studio.sync"}
            or any(name == root + suffix or name.startswith(root + suffix + ".")
                   for root in ("voxarium_studio", "_vs132_unit")
                   for suffix in (".main", ".core.context", ".core.workspace", ".sync.service")))


class ImportDeny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if _heavy(fullname):
            raise AssertionError("C1 imported heavyweight application runtime")


_root = Path(next(iter(importlib.util.find_spec("voxarium_studio").submodule_search_locations)))
for _suffix in ("", ".api", ".sync"):
    _package = ModuleType("_vs132_unit" + _suffix)
    _package.__path__ = [str(_root / _suffix.lstrip("."))]
    _package.__package__ = _package.__name__
    sys.modules[_package.__name__] = _package
_import_guard = ImportDeny()
sys.meta_path.insert(0, _import_guard)
try:
    from _vs132_unit.core import application_config as app, paths
    from _vs132_unit.core.security import AuthSettings
    from _vs132_unit.sync import credentials, profiles, catalog_runtime
    from _vs132_unit.sync.catalog import CatalogClient, CatalogError
    from _vs132_unit.sync.catalog_http import CatalogHttpTransport
    from _vs132_unit.sync.catalog_metadata import CatalogMetadataObserver
    from _vs132_unit.sync.catalog_providers import PendingCatalogProviders, S2CatalogReaders
    from _vs132_unit.sync.catalog_service import CatalogService
    from _vs132_unit.sync.catalog_verification import CatalogVerificationService
    from _vs132_unit.sync.profiles import ProfileError, RemoteProfiles
    from _vs132_unit.api.remote_metadata import CatalogMetadataAdapter
    from _vs132_unit.api.remote import create_remote_router
finally:
    sys.meta_path.remove(_import_guard)


class SocketFreeLoop(getattr(asyncio, "ProactorEventLoop", asyncio.SelectorEventLoop)):
    """Do not create even asyncio's local self-pipe sockets on Windows."""
    def _make_self_pipe(self):
        self._ssock = None
        self._csock = SimpleNamespace(fileno=lambda: -1)

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        raise AssertionError("No cross-thread I/O in C1 unit tests")

    def _loop_self_reading(self, *_args):
        pass


def clean_error(error, code=None):
    assert type(error) in {ProfileError, CatalogError}
    if code is not None:
        assert error.args == (code,)
    assert error.__cause__ is None and error.__context__ is None
    assert CANARY not in "".join(traceback.format_exception(error))


class Synthetic:
    def __init__(self, tmp, mp):
        self.mp, self.home = mp, tmp / "synthetic-home"
        self.config = self.home / "config.yaml"
        self.keys = self.home / "remote-keys.json"
        self.journal = self.home / "remote-profile-change.json"
        self.phase, self.actor = None, False
        self.calls, self.forbidden = Counter(), Counter({key: 0 for key in ZERO})
        self.hook = lambda *_: None
        self.http_handler = self.deny("transport")
        self.requests = []
        self.pools = []
        mp.setenv("VOXARIUM_STUDIO_HOME", str(self.home))
        for name in (credentials.TOKEN_ENV_VAR, credentials.URL_ENV_VAR):
            mp.delenv(name, raising=False)
        self.loop = SocketFreeLoop()
        self.install_guards()
        self.runtime = self.construct()

    def deny(self, category):
        def denied(*_args, **_kwargs):
            self.forbidden[category] += 1
            raise AssertionError("C1 forbidden " + category)
        return denied

    @contextmanager
    def acting(self):
        old, self.actor = self.actor, True
        try:
            yield
        finally:
            self.actor = old

    @contextmanager
    def guard(self, phase):
        old, self.phase = self.phase, phase
        try:
            yield
        finally:
            self.phase = old
            assert not any(self.forbidden.values()), dict(self.forbidden)

    def seed(self, rows=(P1,), *, yaml_config=False, tokens=None):
        with self.acting():
            self.home.mkdir(parents=True, exist_ok=True)
            raw = {"remote_servers": list(rows), "opaque": {"keep": "untouched"}}
            self.config.write_text(yaml.safe_dump(raw) if yaml_config else json.dumps(raw), encoding="utf-8")
            self.keys.write_text(json.dumps({"version": 2, "profiles": {
                p["profile_id"]: {"url": p["url"], "token": (tokens or {}).get(p["profile_id"], CANARY),
                                  "revision": 2}
                for p in rows}, "legacy": {}}), encoding="utf-8")

    def pending(self):
        with self.acting():
            self.journal.write_text(CANARY, encoding="utf-8")

    def construct(self):
        with self.guard("construct"):
            return catalog_runtime.create_remote_catalog_runtime()

    def start(self, runtime=None):
        with self.guard("start"):
            return self.loop.run_until_complete((runtime or self.runtime).start())

    def close(self, runtime=None):
        with self.guard("close"):
            return self.loop.run_until_complete((runtime or self.runtime).close())

    def listing(self, ready=None):
        with self.guard("list"):
            return (ready or self.runtime.ready).metadata.list()

    def capture(self, row=P1):
        with self.guard("provider"):
            return self.runtime.providers_for(row["profile_id"], expected_revision=row["revision"])

    def invoke(self, callback, *args, **kwargs):
        with self.guard("provider"):
            return callback(*args, **kwargs)

    def read_failure(self, code):
        with pytest.raises((ProfileError, CatalogError)) as caught:
            self.listing()
        clean_error(caught.value, code)

    def install_guards(self):
        # Spies delegate to the real S2 readers. Only explicit race actors write.
        for owner, name, label in ((RemoteProfiles, "list", "list"), (RemoteProfiles, "get", "get"),
                                    (credentials, "profile_token_source", "source"),
                                    (credentials, "load_profile_token", "token"),
                                    (CatalogMetadataObserver, "observe", "observe"),
                                    (CatalogMetadataAdapter, "list", "adapter"),
                                    (S2CatalogReaders, "check", "s2_check")):
            original = getattr(owner, name)
            def reader(*args, _original=original, _label=label, **kwargs):
                self.calls[_label] += 1
                if _label == "token" and self.phase not in {"provider", "verify"}:
                    return self.deny("token_lookup")()
                n = self.calls[_label]
                with self.acting():
                    self.hook(_label, n, "before", args)
                value = _original(*args, **kwargs)
                with self.acting():
                    self.hook(_label, n, "after", args)
                return value
            self.mp.setattr(owner, name, reader)

        for owner, names, category in (
            (socket, ("socket", "socketpair", "create_connection", "create_server", "getaddrinfo",
                      "gethostbyname", "gethostbyname_ex", "gethostbyaddr"), "network"),
            (subprocess, ("Popen",), "process"),
            (RemoteProfiles, ("load_token", "create", "rename", "delete", "change_url", "set_credential",
                              "clear_credential", "recover", "import_legacy_credential"), "s2_mutation"),
            (app, ("application_config_transaction", "save_application_config", "_write_application_profiles"), "writes"),
            (profiles, ("application_config_transaction", "_write_application_config"), "writes"),
            (credentials, ("_mutation", "_write_store", "save_profile_token", "save_token", "load_token"), "writes"),
        ):
            for name in names:
                self.mp.setattr(owner, name, self.deny(category))

        # Only the low-level pool is substituted; all service guards, HTTP
        # encoding/decoding and lifecycle cleanup remain production code.
        def pool(**kwargs):
            if self.phase != "start":
                return self.deny("transport")()
            assert kwargs == dict(verify=True, trust_env=False, retries=0, http1=True, http2=False)
            self.calls["pool"] += 1
            transport = httpx.MockTransport(send)
            self.pools.append(transport)
            return transport

        async def send(request):
            if self.phase != "verify":
                return self.deny("transport")()
            self.requests.append(request)
            return self.http_handler(request)

        self.mp.setattr(httpx, "AsyncHTTPTransport", pool)
        for owner, names, category in (
            (CatalogHttpTransport, ("request",), "transport"),
            (CatalogClient, ("__init__",), "transport"),
            (CatalogService, ("__init__",), "transport"),
            (CatalogVerificationService, ("__init__", "verify"), "verify"),
        ):
            for name in names:
                original = getattr(owner, name)
                def explicit(*args, _original=original, _category=category, **kwargs):
                    if self.phase != "verify":
                        return self.deny(_category)()
                    self.calls[_category] += 1
                    return _original(*args, **kwargs)
                self.mp.setattr(owner, name, explicit)

        real_datetime = catalog_runtime.datetime
        class ExplicitClock:
            @staticmethod
            def now(tz):
                if self.phase != "verify":
                    return self.deny("clock")()
                self.calls["clock"] += 1
                return real_datetime.now(tz)
        self.mp.setattr(catalog_runtime, "datetime", ExplicitClock)

        original_time = time.time
        self.mp.setattr(time, "time", lambda: self.deny("clock")() if self.phase and self.phase != "verify" else original_time())
        original_home = Path.home
        self.mp.setattr(Path, "home", classmethod(
            lambda cls: self.deny("default_home")() if self.phase else original_home()))

        def check(path, mode="r", *, metadata=False):
            if not self.phase:
                return
            if isinstance(path, int):
                return self.deny("fd_io")()
            path = Path(path).absolute()
            if self.actor:
                assert path.is_relative_to(TASK_TEMP), "race actor escaped TASK-132"
                return
            if any(c in mode for c in "wax+"):
                return self.deny("writes")()
            if self.phase in {"construct", "import", "close"}:
                return self.deny("lifecycle_io")()
            if path not in {self.home, self.config, self.keys, self.journal}:
                return self.deny("outside_reads")()
            if not metadata and (self.phase == "start" or path == self.journal):
                return self.deny("startup_or_journal_content")()

        for owner, name in ((builtins, "open"), (io, "open")):
            original = getattr(owner, name)
            def opened(path, mode="r", *args, _original=original, **kwargs):
                check(path, mode)
                return _original(path, mode, *args, **kwargs)
            self.mp.setattr(owner, name, opened)
        original_open = os.open
        def opened_os(path, flags, *args, **kwargs):
            check(path, "w" if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) else "r")
            return original_open(path, flags, *args, **kwargs)
        self.mp.setattr(os, "open", opened_os)
        for name in ("stat", "lstat"):
            original = getattr(os, name)
            def stated(path, *args, _original=original, **kwargs):
                check(path, metadata=True)
                return _original(path, *args, **kwargs)
            self.mp.setattr(os, name, stated)
        for name in ("mkdir", "makedirs", "unlink", "remove", "rename", "replace", "rmdir", "write",
                     "chmod", "fsync", "symlink", "link"):
            original = getattr(os, name)
            def mutate(*args, _original=original, **kwargs):
                if self.phase and not self.actor:
                    return self.deny("writes")()
                if self.phase and self.actor:
                    assert Path(args[0]).absolute().is_relative_to(TASK_TEMP)
                return _original(*args, **kwargs)
            self.mp.setattr(os, name, mutate)


@pytest.fixture
def synthetic(tmp_path, monkeypatch, caplog):
    assert tmp_path.absolute().is_relative_to(TASK_TEMP), "--basetemp must be under TASK-132"
    s = Synthetic(tmp_path, monkeypatch)
    sys.meta_path.insert(0, _import_guard)
    yield s
    s.close()
    s.loop.close()
    sys.meta_path.remove(_import_guard)
    assert CANARY not in caplog.text
    assert not any(s.forbidden.values()), dict(s.forbidden)
    TOTAL.update(s.forbidden)


@pytest.fixture(scope="module", autouse=True)
def report():
    yield
    assert not any(TOTAL.values()), dict(TOTAL)
    print("\nVS132_FORBIDDEN_COUNTERS=" + json.dumps(dict(TOTAL), sort_keys=True))


def test_import_and_factory_do_not_select_home_or_perform_io(synthetic, monkeypatch):
    s = synthetic
    source = Path(catalog_runtime.__file__).read_text(encoding="utf-8")
    probe = ModuleType("_vs132_unit.sync._runtime_probe")
    probe.__package__ = "_vs132_unit.sync"
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    with monkeypatch.context() as mp:
        for module in (paths, app, credentials):
            mp.setattr(module, "user_config_dir", s.deny("home_lookup"))
        with s.guard("import"):
            exec(compile(source, catalog_runtime.__file__, "exec"), probe.__dict__)
            first = probe.create_remote_catalog_runtime()
            second = probe.RemoteCatalogRuntime()
            assert first is not second
    assert not s.home.exists() and not s.calls


def test_ready_identity_frozen_and_start_idempotent(synthetic):
    s = synthetic
    with pytest.raises(CatalogError) as caught:
        _ = s.runtime.ready
    clean_error(caught.value, "service_unavailable")
    ready = s.start()
    assert type(ready) is catalog_runtime.RemoteCatalogReady
    assert s.runtime.ready is ready and s.start() is ready
    with pytest.raises(FrozenInstanceError):
        ready.metadata = None
    assert not any(s.calls[k] for k in ("list", "get", "source", "token", "observe"))


@pytest.mark.parametrize("kind", ["missing_home", "missing_config", "empty"])
def test_real_known_empty_is_read_only(synthetic, kind):
    s = synthetic
    if kind == "missing_config":
        s.home.mkdir()
    elif kind == "empty":
        s.seed(())
    s.start()
    assert s.listing() == ()
    assert s.calls["adapter"] == s.calls["observe"] == 1
    assert s.calls["list"] == 2 and s.calls["s2_check"] > 0 and s.calls["token"] == 0
    assert not s.journal.exists()
    if kind == "missing_home":
        assert not s.home.exists()


@pytest.mark.parametrize("yaml_config", [False, True])
@pytest.mark.parametrize("name", ["same", "", " \t" + "history" * 30 + "\n"])
def test_real_two_profiles_preserve_labels_and_unavailable(synthetic, yaml_config, name):
    s = synthetic
    rows = (P1, dict(P2, name=name))
    s.seed(rows, yaml_config=yaml_config)
    before = (s.config.read_bytes(), s.keys.read_bytes())
    s.start()
    values = s.listing()
    assert [v.model_dump() for v in values] == [
        dict(row, has_token=True, credential_source="stored", verification={"state": "unavailable"})
        for row in rows]
    assert s.calls["list"] == 2 and s.calls["get"] > 0 and s.calls["source"] == 4
    assert s.calls["adapter"] == s.calls["observe"] == 1 and s.calls["token"] == 0
    assert (s.config.read_bytes(), s.keys.read_bytes()) == before
    assert CANARY not in repr(values) + repr(s.runtime.ready)


@pytest.mark.parametrize("source", ["stored", "missing", "environment"])
def test_presence_uses_real_source_not_token_loader(synthetic, source):
    s = synthetic
    s.seed()
    if source == "missing":
        s.keys.unlink()
    elif source == "environment":
        s.mp.setenv(credentials.TOKEN_ENV_VAR, CANARY)
        s.mp.setenv(credentials.URL_ENV_VAR, P1["url"])
    s.start()
    value, = s.listing()
    assert value.has_token is (source != "missing")
    assert value.credential_source == ("none" if source == "missing" else source)
    assert value.verification.model_dump() == {"state": "unavailable"}
    assert s.calls["source"] == 2 and s.calls["token"] == 0


@pytest.mark.parametrize("kind,code", [("config", "application_store_unavailable"),
                                        ("credentials", "credential_store_unavailable"),
                                        ("environment_unbound", "metadata_unavailable")])
def test_start_does_not_read_corruption_and_list_never_returns_partial(synthetic, kind, code):
    s = synthetic
    s.seed((P1, P2))
    if kind == "config":
        s.config.write_text("remote_servers: [" + CANARY, encoding="utf-8")
    elif kind == "credentials":
        # A valid first profile must not hide corruption in another store entry.
        raw = json.loads(s.keys.read_text())
        raw["profiles"][P2["profile_id"]]["revision"] = CANARY
        s.keys.write_text(json.dumps(raw), encoding="utf-8")
    else:
        s.mp.setenv(credentials.TOKEN_ENV_VAR, CANARY)
        s.mp.delenv(credentials.URL_ENV_VAR, raising=False)
    ready = s.start()
    assert s.calls["list"] == s.calls["source"] == 0
    s.read_failure(code)
    assert s.runtime.ready is ready and s.calls["token"] == 0


@pytest.mark.parametrize("phase", ["before", "after"])
def test_pending_around_source_refuses_whole_list(synthetic, phase):
    s = synthetic
    s.seed((P1, P2))
    s.start()
    def race(kind, n, when, _args):
        if (kind, n, when) == ("source", 1, phase):
            s.pending()
    s.hook = race
    s.read_failure("profile_recovery_required")
    assert s.journal.read_text() == CANARY and s.calls["token"] == 0


@pytest.mark.parametrize("when", ["start", "list", "profile"])
def test_pending_is_not_opened_recovered_or_ignored(synthetic, when):
    s = synthetic
    s.seed()
    if when != "start":
        ready = s.start()
    s.pending()
    with pytest.raises(ProfileError) as caught:
        if when == "start":
            s.start()
        elif when == "list":
            s.listing()
        else:
            s.invoke(ready.profiles.get, P1["profile_id"])
    clean_error(caught.value, "profile_recovery_required")
    assert s.journal.read_text() == CANARY
    assert s.calls["token"] == s.calls["source"] == 0


@pytest.mark.parametrize("locator", ["application", "profiles", "credentials", "journal"])
@pytest.mark.parametrize("when", ["start", "list"])
def test_actual_live_locators_are_checked_not_pinned_lambdas(synthetic, monkeypatch, locator, when):
    s = synthetic
    s.seed()
    if when == "list":
        s.start()
    owner, name = {"application": (app, "application_config_file"),
                   "profiles": (profiles, "application_config_file"),
                   "credentials": (credentials, "keys_file"),
                   "journal": (RemoteProfiles, "_journal")}[locator]
    wrong = s.home / (CANARY + ".json")
    monkeypatch.setattr(owner, name, staticmethod(lambda: wrong) if locator == "journal" else lambda: wrong)
    with pytest.raises(ProfileError) as caught:
        s.start() if when == "start" else s.listing()
    clean_error(caught.value, "profile_recovery_required")
    assert s.calls["token"] == s.calls["source"] == 0


def test_home_is_selected_at_start_then_env_drift_fails_before_wrong_home_read(synthetic):
    s = synthetic
    # Factory was created with another ENV value; construction must not pin it.
    s.mp.setenv("VOXARIUM_STUDIO_HOME", str(s.home / ".." / "not-selected"))
    other = s.construct()
    s.mp.setenv("VOXARIUM_STUDIO_HOME", str(s.home))
    s.seed()
    ready = s.start(other)
    assert s.listing(ready)[0].url == P1["url"]
    s.mp.setenv("VOXARIUM_STUDIO_HOME", str(s.home.parent / "other-home"))
    with pytest.raises(ProfileError) as caught:
        s.listing(ready)
    clean_error(caught.value, "profile_recovery_required")
    s.close(other)


@pytest.mark.parametrize("kind", ["relative_home", "dotdot_home", "pending"])
def test_failed_start_has_no_ready_and_is_terminal_even_after_repair(synthetic, kind):
    s = synthetic
    if kind == "relative_home":
        s.mp.setenv("VOXARIUM_STUDIO_HOME", "relative-synthetic-home")
    elif kind == "dotdot_home":
        s.mp.setenv("VOXARIUM_STUDIO_HOME", str(s.home / ".." / "ambiguous"))
    else:
        s.seed()
        s.pending()
    with pytest.raises((ProfileError, CatalogError)) as caught:
        s.start()
    clean_error(caught.value)
    s.mp.setenv("VOXARIUM_STUDIO_HOME", str(s.home))
    s.journal.unlink(missing_ok=True)
    with pytest.raises(CatalogError) as caught:
        _ = s.runtime.ready
    clean_error(caught.value, "service_unavailable")
    with pytest.raises(CatalogError) as caught:
        s.start()
    clean_error(caught.value, "service_unavailable")
    replacement = s.construct()
    assert s.start(replacement) is replacement.ready
    s.close(replacement)


def test_overlapping_captures_keep_exact_url_revision_and_real_readonly_tokens(synthetic):
    s = synthetic
    s.seed((P1, P2), tokens={P1["profile_id"]: CANARY + "-A", P2["profile_id"]: CANARY + "-B"})
    s.start()
    a, b, a2 = s.capture(P1), s.capture(P2), s.capture(P1)
    assert all(type(p) is PendingCatalogProviders for p in (a, b, a2))
    assert a is not a2
    for provider, row, suffix in ((b, P2, "-B"), (a, P1, "-A"), (a2, P1, "-A")):
        assert (provider.profile_id, provider.expected_url, provider.expected_revision) == (
            row["profile_id"], row["url"], row["revision"])
        assert s.invoke(provider.load_token, row["profile_id"], row["url"]) == CANARY + suffix
    assert s.calls["token"] == 3
    # A new revision/URL is a new binding, not a rebind of retained A handles.
    updated = dict(P1, url=P1["url"] + "/new", revision=4)
    s.seed((updated, P2))
    fresh = s.capture(updated)
    before = s.calls["token"]
    with pytest.raises(ProfileError) as caught:
        s.invoke(a.load_token, P1["profile_id"], P1["url"])
    clean_error(caught.value, "profile_revision_conflict")
    assert s.calls["token"] == before
    assert fresh.expected_url == updated["url"] and fresh.expected_revision == 4
    assert s.invoke(b.load_token, P2["profile_id"], P2["url"]) == CANARY


def test_capture_cas_and_profile_get_are_real_and_pending_safe(synthetic):
    s = synthetic
    s.seed()
    ready = s.start()
    assert s.invoke(ready.profiles.get, P1["profile_id"]).model_dump() == P1
    with pytest.raises(ProfileError) as caught:
        s.capture(dict(P1, revision=2))
    clean_error(caught.value, "profile_revision_conflict")
    with pytest.raises(ProfileError) as caught:
        s.capture(P2)
    clean_error(caught.value, "profile_not_found")
    provider = s.capture()
    s.pending()
    with pytest.raises(ProfileError) as caught:
        s.invoke(provider.load_token, P1["profile_id"], P1["url"])
    clean_error(caught.value, "profile_recovery_required")
    assert s.calls["token"] == 0


def test_capture_refuses_url_change_between_initial_observation_and_provider_publication(synthetic):
    s = synthetic
    s.seed()
    s.start()
    def race(kind, n, when, _args):
        if (kind, n, when) == ("get", 1, "after"):
            # Same revision intentionally: independently prove exact URL capture,
            # not merely the revision comparison already owned by the foundation.
            s.seed((dict(P1, url=P1["url"] + "/changed"),))
    s.hook = race
    with pytest.raises(ProfileError) as caught:
        s.capture()
    clean_error(caught.value, "profile_revision_conflict")
    assert s.calls["token"] == 0


@pytest.mark.parametrize("close_before_start", [False, True])
def test_close_is_terminal_idempotent_and_new_lifespan_is_independent(synthetic, close_before_start):
    s = synthetic
    if not close_before_start:
        s.start()
    s.close()
    s.close()
    for action in (lambda: s.runtime.ready, lambda: s.start(), lambda: s.capture()):
        with pytest.raises(CatalogError) as caught:
            action()
        clean_error(caught.value, "service_unavailable")
    replacement = s.construct()
    assert replacement is not s.runtime
    s.start(replacement)
    assert s.listing(replacement.ready) == ()
    s.close(replacement)


def test_retained_ready_and_provider_callbacks_reject_after_close(synthetic):
    s = synthetic
    s.seed()
    ready = s.start()
    provider = s.capture()
    s.close()
    before = s.calls.copy()
    for action in (ready.metadata.list, lambda: ready.profiles.get(P1["profile_id"]),
                   lambda: provider.get(P1["profile_id"]),
                   lambda: provider.load_token(P1["profile_id"], P1["url"])):
        with pytest.raises((CatalogError, ProfileError)) as caught:
            s.invoke(action)
        clean_error(caught.value)
    assert all(s.calls[k] == before[k] for k in ("list", "get", "source", "token"))


@pytest.mark.parametrize("state,codes", [
    ("ready", ("profile_revision_conflict", "profile_not_found")),
    ("pending", ("profile_recovery_required",) * 2),
    ("corrupt", ("consumer_lifecycle_unavailable",) * 2),
    ("closed", ("service_unavailable",) * 2),
])
def test_catalog_read_refuses_stale_binding_before_token_or_http(synthetic, state, codes):
    """I9 lifted the R3 gate: refusal is now the lease/provider before-guard.

    The ``outbound`` phase still denies any token lookup, service/client
    construction or send, so these typed refusals precede all of them.
    """
    s = synthetic
    s.seed()
    ready = s.start()
    if state == "pending":
        s.pending()
    elif state == "corrupt":
        s.config.write_text(CANARY, encoding="utf-8")
    elif state == "closed":
        s.close()
    before = s.calls.copy()
    for (pid, revision), code in zip(((P1["profile_id"], 2), (P2["profile_id"], 8)), codes):
        with pytest.raises(CatalogError) as caught:
            with s.guard("outbound"):
                s.loop.run_until_complete(ready.catalog.list_projects(
                    pid, expected_revision=revision, after="alpha", limit=20))
        clean_error(caught.value, code)
    assert not s.requests
    assert all(s.calls[k] == before[k] for k in ("token", "transport", "verify", "pool"))
    assert not s.runtime._requests


def test_explicit_verify_uses_real_http_services_and_does_not_cache_metadata(synthetic):
    s = synthetic
    s.seed()
    before_files = (s.config.read_bytes(), s.keys.read_bytes())
    ready = s.start()
    transport = s.runtime._transport
    assert type(transport) is CatalogHttpTransport
    assert s.calls["pool"] == 1 and not s.requests

    generation = [0]
    def remote(request):
        assert request.method == "GET" and not request.content
        assert request.headers["cache-control"] == "no-store"
        assert not request.url.query
        if str(request.url) == P1["url"] + "/healthz":
            assert "authorization" not in request.headers
            return httpx.Response(200, json=dict(status="ok", service="voxarium_studio_remote",
                                                  version="synthetic", protocol=1))
        assert str(request.url) == P1["url"] + "/api/v1/me"
        assert request.headers["authorization"] == "Bearer " + CANARY
        return httpx.Response(200, json={"protocol": 1, "user": {
            "id": "synthetic-user", "name": "User " + str(generation[0]), "color": "#123abc"}})
    s.http_handler = remote
    providers = []
    original = s.runtime.providers_for
    def capture(*args, **kwargs):
        provider = original(*args, **kwargs)
        providers.append(provider)
        return provider
    s.mp.setattr(s.runtime, "providers_for", capture)
    for n in range(2):
        generation[0] = n
        with s.guard("verify"):
            result = s.loop.run_until_complete(ready.verification.verify(
                P1["profile_id"], expected_revision=P1["revision"]))
        assert result.state == "valid" and result.wire_protocol == 1
        assert result.catalog_version is None and result.error_code == "catalog_unavailable"
        assert result.user.display_name == "User " + str(n)
        assert result.checked_at.endswith("Z") and CANARY not in repr(result)
        before = s.calls.copy()
        assert s.listing()[0].verification.model_dump() == {"state": "unavailable"}
        assert len(s.requests) == (n + 1) * 2
        assert s.calls["token"] == before["token"]
        assert s.calls["verify"] == before["verify"]
        assert s.calls["transport"] == before["transport"]
        assert s.calls["clock"] == before["clock"] == n + 1
    assert len(providers) == 2 and providers[0] is not providers[1]
    assert all(type(p) is PendingCatalogProviders for p in providers)
    assert s.calls["verify"] == 4 and s.calls["token"] > 0
    assert s.calls["pool"] == 1
    assert (s.config.read_bytes(), s.keys.read_bytes()) == before_files
    # I9: a v1 Verify stays diagnostic. The catalog read performs its OWN
    # strict-v2 discovery and stops at the v1 health reply: no /api/v1 request,
    # no authenticated send and no catalog GET (v1 catalog/POST = 0).
    with pytest.raises(CatalogError) as caught:
        with s.guard("verify"):
            s.loop.run_until_complete(ready.catalog.list_projects(
                P1["profile_id"], expected_revision=P1["revision"]))
    clean_error(caught.value, "version_mismatch")
    assert len(s.requests) == 5
    assert str(s.requests[-1].url) == P1["url"] + "/healthz"
    assert "authorization" not in s.requests[-1].headers
    assert s.calls["pool"] == 1 and not s.runtime._requests
    s.close()
    assert transport._closed and not s.runtime._requests


@pytest.mark.parametrize("state,code", [("pending", "profile_recovery_required"),
                                         ("drift", "profile_recovery_required"),
                                         ("revision", "profile_revision_conflict"),
                                         ("closed", "service_unavailable")])
def test_explicit_verify_refuses_stale_binding_before_token_and_http(synthetic, state, code):
    s = synthetic
    s.seed()
    ready = s.start()
    revision = P1["revision"]
    if state == "pending":
        s.pending()
    elif state == "drift":
        s.mp.setenv("VOXARIUM_STUDIO_HOME", str(s.home.parent / "other-home"))
    elif state == "revision":
        revision -= 1
    else:
        s.close()
    before = s.calls.copy()
    with pytest.raises((CatalogError, ProfileError)) as caught:
        with s.guard("verify"):
            s.loop.run_until_complete(ready.verification.verify(
                P1["profile_id"], expected_revision=revision))
    clean_error(caught.value, code)
    assert not s.requests and s.calls["token"] == before["token"]
    assert s.calls["verify"] == s.calls["transport"] == 0


def test_ready_dependencies_feed_existing_unmounted_get_handler(synthetic):
    s = synthetic
    s.seed((P1, P2))
    ready = s.start()
    router = create_remote_router(metadata=ready.metadata, profiles=ready.profiles,
                                  verification=ready.verification, catalog=ready.catalog)
    root = "/api/remote/servers"
    route = next(r for r in router.routes if r.path == root and "GET" in r.methods)
    handler = route.get_route_handler()
    # Current FastAPI lazily inspects the endpoint's source for diagnostics.
    # Prepare that framework-only cache at router setup, outside the measured
    # metadata observation; the handler still runs unchanged under full guards.
    extract_context = getattr(fastapi_routing, "_extract_endpoint_context", None)
    if extract_context is not None:
        extract_context(route.endpoint)

    async def invoke():
        # Only FastAPI's standalone request/dependency cleanup scopes. No ASGI
        # application, mounting, lifespan, middleware stack or HTTP transport.
        request = Request({"type": "http", "method": "GET", "scheme": "http",
                           "path": root, "root_path": "", "query_string": b"",
                           "headers": [(b"host", b"127.0.0.1")],
                           "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 1),
                           "app": SimpleNamespace(state=SimpleNamespace(auth=AuthSettings()))})
        request._body = b""
        async with AsyncExitStack() as stack:
            for name in ("fastapi_middleware_astack", "fastapi_inner_astack", "fastapi_function_astack"):
                request.scope[name] = stack
            with s.guard("list"):
                return await handler(request)

    response = s.loop.run_until_complete(invoke())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body) == {"items": [
        dict(row, has_token=True, credential_source="stored", verification={"state": "unavailable"})
        for row in (P1, P2)]}
    assert CANARY not in response.body.decode()
    assert s.calls["adapter"] == s.calls["observe"] == 1 and s.calls["list"] == 2
    assert s.calls["source"] == 4 and s.calls["token"] == 0
    assert not any(s.forbidden.values())


def test_late_start_failure_is_sanitized_terminal_and_new_owner_starts(synthetic, monkeypatch):
    s = synthetic
    s.seed()
    reached = []

    def fail_after_readers_created(_adapter, *, observer):
        assert isinstance(s.runtime._repo, RemoteProfiles)
        assert isinstance(observer._readers, S2CatalogReaders)
        assert observer._readers.home == s.home
        reached.append(True)
        raise RuntimeError(CANARY)

    with monkeypatch.context() as mp:
        mp.setattr(CatalogMetadataAdapter, "__init__", fail_after_readers_created)
        with pytest.raises(CatalogError) as caught:
            s.start()
        clean_error(caught.value, "service_unavailable")
    assert reached == [True]
    # Restoring the dependency cannot revive the partially composed owner.
    for action in (lambda: s.runtime.ready, lambda: s.start()):
        with pytest.raises(CatalogError) as caught:
            action()
        clean_error(caught.value, "service_unavailable")
    replacement = s.construct()
    try:
        ready = s.start(replacement)
        assert replacement.ready is ready
        assert s.listing(ready)[0].model_dump()["profile_id"] == P1["profile_id"]
    finally:
        s.close(replacement)


def test_two_lifespans_pin_distinct_homes_and_closing_a_preserves_b(synthetic):
    s = synthetic
    home_a = s.home
    s.seed((P1,))
    ready_a = s.start()
    assert s.listing(ready_a)[0].profile_id == P1["profile_id"]

    def allow_only(home):
        s.home = home
        s.config = home / "config.yaml"
        s.keys = home / "remote-keys.json"
        s.journal = home / "remote-profile-change.json"

    home_b = home_a.parent / "synthetic-home-b"
    allow_only(home_b)
    s.mp.setenv("VOXARIUM_STUDIO_HOME", str(home_b))
    s.seed((P2,))
    owner_b = s.construct()
    try:
        ready_b = s.start(owner_b)
        assert owner_b is not s.runtime and ready_b is not ready_a
        assert owner_b.ready is ready_b
        assert s.listing(ready_b)[0].profile_id == P2["profile_id"]
        # A may inspect only home A. Any B stat/content read by old A would
        # increment outside_reads, even if swallowed by an outer error mapper.
        allow_only(home_a)
        before = s.calls.copy()
        with pytest.raises(ProfileError) as caught:
            s.listing(ready_a)
        clean_error(caught.value, "profile_recovery_required")
        assert all(s.calls[k] == before[k] for k in ("list", "get", "source", "token"))
        s.close()
        allow_only(home_b)
        assert owner_b.ready is ready_b
        assert [row.profile_id for row in s.listing(ready_b)] == [P2["profile_id"]]
    finally:
        s.close(owner_b)
