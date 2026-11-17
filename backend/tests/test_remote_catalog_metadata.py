"""Targeted offline M acceptance (catalog-metadata-contract В§7), not AвЂ“D.

Run ONLY this file, with --noconftest and --basetemp below TASK_TEMP. All
storage is synthetic; race writers are test actors, never observer writes.
"""
from __future__ import annotations

import builtins
from collections import Counter
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, asdict, fields
import errno
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import traceback
import warnings

import pytest

from voxarium_studio.core import application_config as app
from voxarium_studio.sync import credentials, profiles
from voxarium_studio.sync.catalog_metadata import CatalogMetadataObserver
from voxarium_studio.sync.catalog_providers import PendingCatalogProviders, S2CatalogReaders
from voxarium_studio.sync.profiles import ProfileError, RemoteProfiles


# Keep the standalone harness's containment guard, without forcing every later
# verification to write into a different board task's disposable directory.
TASK_TEMP = Path(os.environ.get("VOXARIUM_STUDIO_TEST_TASK_TEMP",
                               r"D:\VoxariumStudio\.voxarium_studio\temp\TASK-122"))
assert TASK_TEMP.is_absolute() and ".." not in TASK_TEMP.parts
assert TASK_TEMP.is_relative_to(Path(__file__).resolve().parents[2] / ".voxarium_studio" / "temp")
CANARY = "M-PRIVATE-CANARY-7391-do-not-publish"
P1 = dict(profile_id="11111111-1111-4111-8111-111111111111", name="same",
          url="https://one.invalid/prefix", revision=3)
P2 = dict(profile_id="22222222-2222-4222-8222-222222222222", name="same",
          url="http://two.invalid/other", revision=8)
FIXED = {"application_store_unavailable", "credential_store_unavailable",
         "profile_recovery_required", "profile_revision_conflict",
         "invalid_profile_metadata", "metadata_unavailable",
         "invalid_catalog_readers", "profile_not_found"}
ZERO = ("network", "dns", "listeners", "runtime", "verify", "transport",
        "token_reads", "writes", "dirs", "locks", "outside_reads", "journal_reads")


class Synthetic:
    def __init__(self, home, monkeypatch):
        self.home = home
        self.config = home / "config.yaml"
        self.keys = home / "remote-keys.json"
        self.journal = home / "remote-profile-change.json"
        self.events = []
        self.calls = Counter()
        self.counts = Counter({name: 0 for name in ZERO})
        self.hook = lambda *_: None
        self.active = False
        self.actor = False
        self.mp = monkeypatch
        monkeypatch.setattr(app, "application_config_file", lambda: self.config)
        monkeypatch.setattr(profiles, "application_config_file", lambda: self.config)
        monkeypatch.setattr(credentials, "keys_file", lambda: self.keys)
        monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
        monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)
        self.repo = RemoteProfiles()
        self.install_guards()

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

    def forbidden(self, name):
        def fail(*_args, **_kwargs):
            self.counts[name] += 1
            raise AssertionError("forbidden " + name)
        return fail

    def install_guards(self):
        # Loaded owners are patched before observation; imports/pytest are outside
        # the observation window, not mistaken for metadata file reads.
        for owner, name, category in (
            (socket, "socket", "network"), (socket, "create_connection", "network"),
            (socket, "getaddrinfo", "dns"), (socket, "gethostbyname", "dns"),
            (socket, "create_server", "listeners"),
            (subprocess, "Popen", "runtime"),
            (credentials, "load_profile_token", "token_reads"),
            (credentials, "load_token", "token_reads"),
            (credentials, "profile_token_revision", "token_reads"),
            (RemoteProfiles, "load_token", "token_reads"),
            (app, "application_config_transaction", "locks"),
            (profiles, "application_config_transaction", "locks"),
            (credentials, "_mutation", "locks"),
        ):
            self.mp.setattr(owner, name, self.forbidden(category))
        from voxarium_studio.sync.catalog_verification import CatalogVerificationService
        from voxarium_studio.sync.catalog_service import CatalogService
        self.mp.setattr(CatalogVerificationService, "verify", self.forbidden("verify"))
        self.mp.setattr(CatalogService, "__init__", self.forbidden("transport"))

        def check_path(path, mode="r"):
            if not self.active or self.actor or isinstance(path, int):
                return
            if any(c in mode for c in "wax+"):
                self.counts["writes"] += 1
                raise AssertionError("observer attempted write")
            path = Path(path).absolute()
            if path not in (self.home, self.config, self.keys, self.journal):
                self.counts["outside_reads"] += 1
                raise AssertionError("observer attempted outside read")
            if path == self.journal:
                self.counts["journal_reads"] += 1
                raise AssertionError("journal contents must not be read")
            self.counts["reads:" + path.name] += 1

        for owner, name in ((builtins, "open"), (io, "open")):
            original = getattr(owner, name)
            def opened(path, mode="r", *args, _original=original, **kwargs):
                check_path(path, mode)
                return _original(path, mode, *args, **kwargs)
            self.mp.setattr(owner, name, opened)
        original_os_open = os.open
        def os_open(path, flags, *args, **kwargs):
            check_path(path, "w" if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) else "r")
            return original_os_open(path, flags, *args, **kwargs)
        self.mp.setattr(os, "open", os_open)
        for name in ("mkdir", "makedirs", "unlink", "remove", "rename", "replace", "rmdir",
                     "write", "symlink", "link"):
            original = getattr(os, name)
            def os_mutate(*args, _original=original, _name=name, **kwargs):
                if self.active and not self.actor:
                    self.counts["dirs" if _name in ("mkdir", "makedirs") else "writes"] += 1
                    raise AssertionError("observer OS mutation")
                return _original(*args, **kwargs)
            self.mp.setattr(os, name, os_mutate)
        for name in ("mkdir", "unlink", "rename", "replace", "rmdir", "symlink_to", "touch"):
            original = getattr(Path, name)
            def mutate(path, *args, _original=original, _name=name, **kwargs):
                if self.active and not self.actor:
                    self.counts["dirs" if _name == "mkdir" else "writes"] += 1
                    raise AssertionError("observer mutation")
                return _original(path, *args, **kwargs)
            self.mp.setattr(Path, name, mutate)
        original_stat = Path.stat
        def observed_stat(path, *args, **kwargs):
            if self.active and not self.actor:
                if path.absolute() not in (self.home, self.config, self.keys, self.journal):
                    self.counts["outside_reads"] += 1
                    raise AssertionError("outside stat")
                if kwargs.get("follow_symlinks", True) is False:
                    self.events.append(("lstat", path.name))
            return original_stat(path, *args, **kwargs)
        self.mp.setattr(Path, "stat", observed_stat)
        for cls, method, label in ((S2CatalogReaders, "check", "s2_check"),
                                   (PendingCatalogProviders, "get", "pending_get")):
            original = getattr(cls, method)
            def counted(instance, *args, _original=original, _label=label, **kwargs):
                self.counts[_label] += 1
                return _original(instance, *args, **kwargs)
            self.mp.setattr(cls, method, counted)

    def reader(self, kind, read, *args):
        self.calls[kind] += 1
        n = self.calls[kind]
        self.events.append(("begin", kind, n))
        try:
            with self.race_actor():
                self.hook(kind, n, "before", args)
            result = read(*args)
            with self.race_actor():
                self.hook(kind, n, "after", args)
            return result
        finally:
            self.events.append(("end", kind, n))

    def observer(self, **overrides):
        args = dict(home=self.home,
                    list_reader=lambda: self.reader("list", self.repo.list),
                    profile_reader=lambda pid: self.reader("get", self.repo.get, pid),
                    source_reader=lambda pid, url: self.reader(
                        "source", credentials.profile_token_source, pid, url),
                    application_file=lambda: self.config,
                    credential_file=lambda: self.keys, journal_file=lambda: self.journal)
        args.update(overrides)
        return CatalogMetadataObserver(**args)

    def observe(self, **overrides):
        self.events.clear()
        self.calls.clear()
        self.active = True
        try:
            return self.observer(**overrides).observe()
        finally:
            self.active = False
            assert all(self.counts[k] == 0 for k in ZERO), dict(self.counts)

    def failure(self, code=None, **overrides):
        with pytest.raises(ProfileError) as caught:
            self.observe(**overrides)
        error = caught.value
        assert error.args[0] in FIXED
        if code:
            assert error.args == (code,)
        assert error.__cause__ is None
        assert error.__context__ is None
        assert CANARY not in "".join(traceback.format_exception(error))
        return error

    def assert_fences(self):
        # Every callback is bracketed by both store-type observations, including
        # error and ENV paths. S2 checks are mandatory, not a copied new fence.
        spans = [i for i, event in enumerate(self.events) if event[0] in ("begin", "end")]
        for left, right in zip([-1] + spans, spans + [len(self.events)]):
            if left >= 0 and self.events[left][0] == "begin":
                continue  # The trusted reader itself need not perform lstat.
            seen = {event[1] for event in self.events[left + 1:right] if event[0] == "lstat"}
            assert {"config.yaml", "remote-keys.json"} <= seen, self.events
        assert self.counts["s2_check"] > 0
        if self.calls["source"]:
            assert self.counts["pending_get"] > 0


@pytest.fixture
def synthetic(tmp_path, monkeypatch, caplog):
    assert tmp_path.is_relative_to(TASK_TEMP), "use --basetemp under the declared task temp"
    value = Synthetic(tmp_path / "synthetic-home", monkeypatch)
    yield value
    assert CANARY not in caplog.text
    assert all(value.counts[k] == 0 for k in ZERO)
    print("M_COUNTERS", json.dumps(dict(value.counts), sort_keys=True))


@pytest.mark.parametrize("kind", ["missing_home", "missing_config", "empty"])
def test_empty_is_known_read_only_and_final_fenced(synthetic, kind):
    s = synthetic
    if kind == "missing_config":
        s.home.mkdir()
    elif kind == "empty":
        s.seed(())
    assert s.observe() == ()
    assert s.calls == {"list": 2}
    s.assert_fences()
    # Two list reads have four pre/post observations; the fifth is the distinct
    # final fence, not merely the post-L1 check incorrectly called "final".
    assert s.events.count(("lstat", "config.yaml")) >= 5
    assert s.events.count(("lstat", "remote-keys.json")) >= 5
    assert not s.journal.exists()
    if kind == "missing_home":
        assert not s.home.exists()


def test_full_exact_immutable_rows_and_historical_names(synthetic, caplog):
    s = synthetic
    historical = dict(P2, name=" \t" + "old" * 60 + "\n")
    s.seed((P1, P2, dict(historical, profile_id="33333333-3333-4333-8333-333333333333",
                             url="https://three.invalid")))
    rows = s.observe()
    assert type(rows) is tuple and len(rows) == 3
    for row, profile in zip(rows, (P1, P2, dict(historical, profile_id=rows[2].profile_id,
                                              url="https://three.invalid"))):
        assert asdict(row) == dict(profile, has_token=True, credential_source="stored",
                                   verification="unavailable")
        assert {f.name for f in fields(row)} == set(asdict(row))
        with pytest.raises((FrozenInstanceError, AttributeError)):
            row.name = "changed"
    assert rows[2].name == historical["name"]
    assert CANARY not in repr(rows) + caplog.text
    assert s.calls["list"] == 2 and s.calls["source"] == 6
    s.assert_fences()


@pytest.mark.parametrize("case,presence,source", [
    ("stored", True, "stored"), ("missing_store", False, "none"),
    ("missing_entry", False, "none"), ("tombstone", False, "none"),
    ("legacy", False, "none"), ("wrong_url", False, "none"),
    ("environment", True, "environment"), ("empty_env", None, "environment_unbound"),
    ("unbound_env", None, "environment_unbound"),
    ("invalid_env_url", None, "environment_unbound"),
    ("mismatched_env", None, "environment_unbound"),
])
def test_single_secret_free_source_outcomes(synthetic, case, presence, source):
    s = synthetic
    s.seed()
    if case == "missing_store":
        s.keys.unlink()
    elif case in ("missing_entry", "legacy", "wrong_url", "tombstone"):
        raw = json.loads(s.keys.read_text())
        if case == "legacy":
            raw = {P1["url"]: CANARY}
        elif case == "missing_entry":
            raw["profiles"] = {}
        elif case == "wrong_url":
            raw["profiles"][P1["profile_id"]]["url"] = P2["url"]
        else:
            raw["profiles"][P1["profile_id"]]["token"] = None
        s.keys.write_text(json.dumps(raw))
    elif "env" in case:
        s.env(token="" if case == "empty_env" else CANARY,
              url={"unbound_env": "", "invalid_env_url": "not a URL " + CANARY,
                   "mismatched_env": P2["url"]}.get(case, P1["url"]))
    before = s.keys.read_bytes() if s.keys.exists() else None
    row, = s.observe()
    assert (row.has_token, row.credential_source, row.verification) == (presence, source, "unavailable")
    assert s.calls["source"] == 2
    s.assert_fences()
    assert (s.keys.read_bytes() if s.keys.exists() else None) == before


def test_env_is_not_stored_integrity_evidence(synthetic):
    s = synthetic
    s.seed()
    s.keys.write_text("corrupt " + CANARY)
    s.env()
    assert s.observe()[0].credential_source == "environment"
    assert s.counts["reads:remote-keys.json"] == 0
    s.assert_fences()
    s.mp.delenv(credentials.TOKEN_ENV_VAR)
    s.failure("credential_store_unavailable")


@pytest.mark.parametrize("store", ["config", "keys"])
@pytest.mark.parametrize("damage", ["corrupt", "duplicate", "unreadable", "directory", "dangling", "symlink", "lstat_error"])
@pytest.mark.parametrize("env", [False, True])
def test_store_damage_fail_closed_including_env(synthetic, store, damage, env):
    s = synthetic
    s.seed()
    if env:
        s.env()
    path = getattr(s, store)
    if damage == "corrupt":
        path.write_text("{ broken " + CANARY)
    elif damage == "duplicate":
        path.write_text('{"remote_servers":' + json.dumps([P1, P1]) + '}' if store == "config"
                        else '{"version":2,"version":2,"profiles":{}}')
    elif damage in ("directory", "dangling", "symlink"):
        path.unlink()
        if damage == "directory":
            path.mkdir()
        else:
            # Synthetic lstat simulation is portable on Windows without symlink
            # privileges; no actual link can escape the task-private home.
            original = Path.lstat
            def link(p, *args, **kwargs):
                if p == path:
                    return os.stat_result((stat.S_IFLNK | 0o777,) + (0,) * 9)
                return original(p, *args, **kwargs)
            s.mp.setattr(Path, "lstat", link)
    elif damage == "lstat_error":
        original = Path.lstat
        def unavailable(p, *args, **kwargs):
            if p == path:
                raise PermissionError(CANARY)
            return original(p, *args, **kwargs)
        s.mp.setattr(Path, "lstat", unavailable)
    else:
        original = Path.read_text
        def unreadable(p, *args, **kwargs):
            if p == path:
                raise PermissionError(CANARY)
            return original(p, *args, **kwargs)
        s.mp.setattr(Path, "read_text", unreadable)
    if env and store == "keys" and damage in ("corrupt", "duplicate", "unreadable"):
        assert s.observe()[0].credential_source == "environment"
    else:
        s.failure("application_store_unavailable" if store == "config" else "credential_store_unavailable")


@pytest.mark.parametrize("location", ["application_file", "credential_file", "journal_file"])
def test_live_path_drift_is_global_refusal(synthetic, location):
    s = synthetic
    s.seed()
    s.failure("profile_recovery_required", **{location: lambda: s.home / "wrong"})
    assert not s.calls


@pytest.mark.parametrize("kind", ["other_profile", "directory", "dangling", "unreadable"])
def test_preexisting_global_journal_blocks_all_readers(synthetic, kind):
    s = synthetic
    s.seed()
    if kind == "directory":
        s.journal.mkdir()
    elif kind == "other_profile":
        s.journal.write_text(json.dumps({"profile_id": P2["profile_id"], "raw": CANARY}))
    else:
        original = Path.lstat
        def journal(p, *args, **kwargs):
            if p == s.journal:
                if kind == "unreadable":
                    raise PermissionError(CANARY)
                return os.stat_result((stat.S_IFLNK,) + (0,) * 9)
            return original(p, *args, **kwargs)
        s.mp.setattr(Path, "lstat", journal)
    s.failure("profile_recovery_required")
    assert not s.calls


@pytest.mark.parametrize("env", [False, True])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("failed", [False, True])
def test_journal_inside_every_read_including_failed_and_empty(synthetic, env, empty, phase, failed):
    s = synthetic
    s.seed(() if empty else (P1,))
    if env:
        s.env()
    s.observe()
    boundaries = [event[1:] for event in s.events if event[0] == "begin"]
    for target in boundaries:
        def race(kind, n, when, _args):
            if (kind, n) == target and when == phase:
                s.journal.write_text(CANARY)
                if failed:
                    raise RuntimeError(CANARY)
        s.hook = race
        s.failure("profile_recovery_required")
        s.journal.unlink()
    s.hook = lambda *_: None


@pytest.mark.parametrize("env", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_journal_appears_only_before_distinct_final_fence(synthetic, env, empty):
    s = synthetic
    s.seed(() if empty else (P1,))
    if env:
        s.env()
    s.observe()
    baseline_checks = s.counts["s2_check"]
    # _check brackets store lstat with two S2 checks. The penultimate S2 check
    # starts the separate FINAL _check, after the post-L1 _check has finished.
    final_start = baseline_checks - 1
    original = S2CatalogReaders.check  # Retain the fixture's counted wrapper.
    calls = 0
    injected = False
    def final_race(readers):
        nonlocal calls, injected
        calls += 1
        if calls == final_start:
            l1_end = s.events.index(("end", "list", 2))
            observed = {e[1] for e in s.events[l1_end + 1:] if e[0] == "lstat"}
            assert {"config.yaml", "remote-keys.json"} <= observed
            assert s.calls["source"] == (0 if empty else 2)
            with s.race_actor():
                s.journal.write_text(CANARY)
            injected = True
        return original(readers)
    s.mp.setattr(S2CatalogReaders, "check", final_race)
    s.failure("profile_recovery_required")
    assert injected and calls == final_start
    assert s.calls["list"] == 2
    assert s.counts["s2_check"] == baseline_checks + final_start


@pytest.mark.parametrize("operation", ["add", "delete", "rename", "reorder", "url", "revision"])
def test_final_entire_list_is_revalidated(synthetic, operation):
    s = synthetic
    s.seed((P1, P2))
    def race(kind, n, phase, _args):
        if (kind, n, phase) == ("list", 2, "before"):
            rows = [dict(P1), dict(P2)]
            if operation == "add":
                rows.append(dict(P1, profile_id="33333333-3333-4333-8333-333333333333", url="https://added.invalid"))
            elif operation == "delete":
                rows.pop()
            elif operation == "rename":
                rows[1]["name"] = "new name"
            elif operation == "url":
                rows[1]["url"] = "https://changed.invalid/new-prefix"
            elif operation == "revision":
                rows[1]["revision"] += 1
            else:
                rows.reverse()
            s.config.write_text(json.dumps({"remote_servers": rows}))
    s.hook = race
    s.failure("metadata_unavailable")


@pytest.mark.parametrize("field,value", [("profile_id", P2["profile_id"]),
                                        ("url", P2["url"]), ("revision", 4)])
@pytest.mark.parametrize("boundary", ["get", "source"])
@pytest.mark.parametrize("edge", ["before", "after"])
def test_binding_drift_never_looks_up_new_locator(synthetic, field, value, boundary, edge):
    s = synthetic
    s.seed()
    lookups = []
    def race(kind, n, phase, args):
        if kind == "source" and phase == "before":
            lookups.append(args)
        if (kind, n, phase) == (boundary, 1, edge):
            s.config.write_text(json.dumps({"remote_servers": [dict(P1, **{field: value})]}))
    s.hook = race
    s.failure()
    assert all(args == (P1["profile_id"], P1["url"]) for args in lookups)
    # A completed get can return the old snapshot: one ORIGINAL lookup before
    # the subsequent exact get detects drift is within bounded observations.
    assert len(lookups) <= (0 if boundary == "get" and edge == "before" else 1)


def test_repeated_source_change_is_rejected_but_key_rotation_is_presence_only(synthetic):
    s = synthetic
    s.seed()
    def changed(kind, n, phase, _args):
        if (kind, n, phase) == ("source", 1, "after"):
            s.env()
    s.hook = changed
    s.failure("metadata_unavailable")
    s.mp.delenv(credentials.TOKEN_ENV_VAR)
    def rotated(kind, n, phase, _args):
        if (kind, n, phase) == ("source", 1, "after"):
            raw = json.loads(s.keys.read_text())
            raw["profiles"][P1["profile_id"]]["token"] = CANARY + "-B"
            s.keys.write_text(json.dumps(raw))
    s.hook = rotated
    row, = s.observe()
    assert (row.has_token, row.credential_source, row.verification) == (True, "stored", "unavailable")
    assert CANARY not in repr(row)


@pytest.mark.parametrize("reader,code", [("list", "application_store_unavailable"),
                                        ("get", "application_store_unavailable"),
                                        ("source", "credential_store_unavailable")])
def test_raw_reader_error_has_fixed_code_no_echo_or_chain(synthetic, caplog, reader, code):
    s = synthetic
    s.seed()
    def error(kind, _n, phase, _args):
        if kind == reader and phase == "before":
            raise RuntimeError(CANARY)
    s.hook = error
    s.failure(code)
    assert CANARY not in caplog.text
    s.assert_fences()


@pytest.mark.parametrize("bad", [None, [], (object(),), (P1,), (P1, P1)])
def test_invalid_list_is_not_empty_or_partial(synthetic, bad):
    synthetic.seed()
    synthetic.failure("invalid_profile_metadata", list_reader=lambda: bad)


@pytest.mark.parametrize("source", [None, True, "none", "unchecked", CANARY])
def test_unknown_source_is_not_missing(synthetic, source):
    synthetic.seed()
    synthetic.failure("credential_store_unavailable", source_reader=lambda *_: source)


def test_no_status_owner_means_unavailable_even_missing_key_and_new_cache(synthetic):
    s = synthetic
    s.seed(token=None)
    empty_cache = {}
    row, = s.observe()
    assert not empty_cache
    assert row.has_token is False and row.verification == "unavailable"
    assert set(asdict(row)) == {"profile_id", "name", "url", "revision", "has_token",
                               "credential_source", "verification"}
    # The existing REST DTO cannot truthfully map unavailable to checked/unchecked.
    from pydantic import ValidationError
    from voxarium_studio.api.remote import ServerProfile
    with pytest.raises(ValidationError):
        ServerProfile.model_validate(asdict(row))
    from starlette.requests import Request
    from voxarium_studio.api.remote import create_remote_router
    from voxarium_studio.sync.catalog_service import CatalogError
    class Metadata:
        def list(self):
            return (row,)
    # Call only the existing unmounted endpoint, no ASGI client/listener/runtime.
    router = create_remote_router(profiles=s.repo, metadata=Metadata(),
                                  verification=object(), catalog=object())
    endpoint = next(route.endpoint for route in router.routes
                    if route.path == "/api/remote/servers" and "GET" in route.methods)
    request = Request({"type": "http", "query_string": b"", "headers": []})
    request._body = b""
    coroutine = endpoint(request)
    try:
        with pytest.raises(CatalogError) as caught:
            coroutine.send(None)
        assert caught.value.args == ("metadata_unavailable",)
    finally:
        coroutine.close()


@pytest.mark.parametrize("store", ["config", "keys"])
@pytest.mark.parametrize("env", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_store_type_fence_at_every_reader_boundary(synthetic, store, env, empty):
    s = synthetic
    s.seed(() if empty else (P1,))
    if env:
        s.env()
    s.observe()
    boundaries = [event[1:] for event in s.events if event[0] == "begin"]
    path = getattr(s, store)
    original = Path.lstat
    damaged = False
    def lstat(p, *args, **kwargs):
        if damaged and p == path:
            raise PermissionError(CANARY)
        return original(p, *args, **kwargs)
    s.mp.setattr(Path, "lstat", lstat)
    for target in boundaries:
        for edge in ("before", "after"):
            def race(kind, n, phase, _args):
                nonlocal damaged
                if (kind, n) == target and phase == edge:
                    damaged = True
            s.hook = race
            s.failure("application_store_unavailable" if store == "config"
                      else "credential_store_unavailable")
            damaged = False
    s.hook = lambda *_: None


@pytest.mark.parametrize("location", ["config", "keys", "journal"])
@pytest.mark.parametrize("reader", ["list", "get", "source"])
def test_path_drift_during_read_and_failed_read_is_fenced(synthetic, location, reader):
    s = synthetic
    s.seed()
    original = getattr(s, location)
    for failed in (False, True):
        def race(kind, n, phase, _args):
            if (kind, n, phase) == (reader, 1, "after"):
                setattr(s, location, s.home / "outside-locator")
                if failed:
                    raise RuntimeError(CANARY)
        s.hook = race
        s.failure("profile_recovery_required")
        setattr(s, location, original)


@pytest.mark.parametrize("collision", ["profile_id", "url"])
def test_duplicate_real_metadata_fails_before_sources(synthetic, collision):
    s = synthetic
    s.seed((P1, dict(P2, **{collision: P1[collision]})))
    s.failure("application_store_unavailable")
    assert s.calls["source"] == 0


@pytest.mark.parametrize("field,value", [("revision", True), ("revision", 0),
                                        ("profile_id", CANARY), ("url", CANARY),
                                        ("url", P1["url"] + "/")])
def test_constructed_invalid_metadata_cannot_bypass_validation(synthetic, field, value):
    s = synthetic
    s.seed()
    bad = app.RemoteServerProfile.model_construct(**dict(P1, **{field: value}))
    s.failure("invalid_profile_metadata", list_reader=lambda: (bad,))
    assert s.calls["source"] == 0


def test_extended_model_cannot_hide_extra_metadata(synthetic):
    class ExtendedProfile(app.RemoteServerProfile):
        credential: str

    synthetic.seed()
    bad = ExtendedProfile.model_construct(**P1, credential=CANARY)
    synthetic.failure("invalid_profile_metadata", list_reader=lambda: (bad,))
    assert synthetic.calls["source"] == 0


@pytest.mark.parametrize("kind,code", [
    ("none", "application_store_unavailable"),
    ("dict", "invalid_profile_metadata"),
    ("object", "invalid_profile_metadata"),
    ("wrong_id", "invalid_profile_metadata"),
    ("bad_name", "invalid_profile_metadata"),
    ("unnormalized_url", "profile_revision_conflict"),
])
def test_invalid_get_snapshot_cannot_publish_or_read_source(synthetic, kind, code):
    s = synthetic
    s.seed()
    if kind == "none":
        bad = None
    elif kind == "dict":
        bad = dict(P1)
    elif kind == "object":
        bad = object()
    else:
        changes = {"wrong_id": {"profile_id": P2["profile_id"]},
                   "bad_name": {"name": [CANARY]},
                   "unnormalized_url": {"url": P1["url"] + "/"}}[kind]
        bad = app.RemoteServerProfile.model_construct(**dict(P1, **changes))
    source_calls = []
    def source(*args):
        source_calls.append(args)
        return credentials.profile_token_source(*args)
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        s.failure(code, profile_reader=lambda _pid: bad, source_reader=source)
    assert all(CANARY not in str(warning.message) for warning in emitted), (
        "invalid metadata emitted a secret-bearing serializer warning")
    assert source_calls == []


@pytest.mark.parametrize("store", ["config", "keys"])
@pytest.mark.parametrize("error_number", [errno.EACCES, None])
@pytest.mark.parametrize("env", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_only_enoent_means_missing_store(synthetic, store, error_number, env, empty):
    s = synthetic
    s.seed(() if empty else (P1,))
    if env:
        s.env()
    path = getattr(s, store)
    original = Path.lstat
    def unavailable(p, *args, **kwargs):
        if p == path:
            raise FileNotFoundError(error_number, CANARY)
        return original(p, *args, **kwargs)
    s.mp.setattr(Path, "lstat", unavailable)
    s.failure("application_store_unavailable" if store == "config"
              else "credential_store_unavailable")
    assert not s.calls


@pytest.mark.parametrize("argument", ["home", "list_reader", "profile_reader", "source_reader",
                                    "application_file", "credential_file", "journal_file"])
def test_all_dependencies_are_explicit_and_required(synthetic, argument):
    import inspect
    signature = inspect.signature(CatalogMetadataObserver)
    assert signature.parameters[argument].default is inspect.Parameter.empty
    assert signature.parameters[argument].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("argument", ["list_reader", "profile_reader", "source_reader",
                                    "application_file", "credential_file", "journal_file"])
def test_noncallable_dependency_is_fixed_refusal(synthetic, argument):
    synthetic.failure("invalid_catalog_readers", **{argument: None})
    assert not synthetic.calls


@pytest.mark.parametrize("argument", ["application_file", "credential_file", "journal_file"])
def test_raw_location_failure_is_sanitized_before_any_storage_read(synthetic, argument):
    def unavailable():
        raise RuntimeError(CANARY)
    synthetic.failure("profile_recovery_required", **{argument: unavailable})
    assert not synthetic.calls


