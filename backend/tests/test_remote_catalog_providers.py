"""Synthetic S2 storage -> unmounted S3 reads (studio-design §5.1/§6).

Only actual read-only S2 parsers, explicitly bound to one disposable home, and a
scripted transport are used. Fixture/interleaving edits are deliberately exempt
from the operation observer; provider operations may not write, mkdir or lock.
Run with --basetemp and Python/pytest caches beneath the VS-118 task temp root.
Discrete fences do not prove continuous isolation or cold credential continuity.
"""
from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, asdict, fields, replace
import io
import json
import os
from pathlib import Path
import socket
import stat
import traceback
from types import SimpleNamespace

import pytest

from voxarium_studio.core import application_config
from voxarium_studio.sync import credentials, profiles
from voxarium_studio.sync.catalog import CatalogError
from voxarium_studio.sync.catalog_providers import PendingCatalogProviders, S2CatalogReaders
from voxarium_studio.sync.catalog_read_service import CatalogReadService
from voxarium_studio.sync.catalog_service import CatalogService
from voxarium_studio.sync.profiles import ProfileError

# Import only the socket-free loop fixture and pure fakes, NOT synthetic_only:
# that fixture deliberately forbids the actual S2 readers exercised here.
from test_remote_catalog_service import (
    BASE_A, BASE_B, CAPABILITIES, KEY_A, KEY_B, PROFILE_A, PROFILE_B,
    Transport, event_loop_policy, health, identity, page, project, response,
)
from test_remote_catalog_reads import document, legacy, page as snapshot_page


COUNTER_NAMES = ("dns", "socket", "store_mutation", "dirs", "locks", "writes", "outside_reads")
CAPS = CAPABILITIES + ["project_snapshot", "legacy_resolution"]
MOVED_URL = "https://catalog.invalid/deploy/gamma"


def assert_safe(value, caplog):
    diagnostic = repr(value)
    if isinstance(value, BaseException):
        diagnostic += repr((value.args, value.__dict__))
        diagnostic += "".join(traceback.format_exception(value))
        assert value.__cause__ is None and value.__context__ is None
    for key in (KEY_A, KEY_B):
        assert key not in diagnostic
        assert key not in caplog.text


@pytest.fixture(autouse=True)
def forbidden_io(monkeypatch):
    """Counters detect even forbidden attempts hidden by sanitized exceptions."""
    counts = dict.fromkeys(COUNTER_NAMES, 0)

    def forbid(kind):
        def fail(*_args, **_kwargs):
            counts[kind] += 1
            raise AssertionError("Synthetic provider attempted forbidden I/O")
        return fail

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, forbid("dns"))
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbid("socket"))
    monkeypatch.setattr(socket, "socket", forbid("socket"))
    monkeypatch.setattr(socket, "socketpair", forbid("socket"))
    for module, names in (
        (application_config, ("application_config_transaction", "save_application_config",
                              "_write_application_profiles")),
        (profiles, ("application_config_transaction", "_write_application_config")),
        (credentials, ("_mutation", "_write_store", "save_token", "save_profile_token",
                       "import_legacy_token", "load_token")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbid("store_mutation"))
    for name in ("load_token", "create", "rename", "change_url", "delete", "recover",
                 "replace_credential", "import_legacy_credential"):
        if hasattr(profiles.RemoteProfiles, name):
            monkeypatch.setattr(profiles.RemoteProfiles, name, forbid("store_mutation"))
    for name in (credentials.URL_ENV_VAR, credentials.TOKEN_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    yield counts
    assert counts == dict.fromkeys(COUNTER_NAMES, 0)


class SyntheticStorage:
    def __init__(self, home, monkeypatch, counts):
        self.home = home
        self.monkeypatch = monkeypatch
        self.counts = counts
        self.active = 0
        self.metadata_calls = []
        self.credential_calls = []
        self.environment_calls = []
        self.store_calls = []
        self.location_calls = []
        self.locations = {}
        self.metadata_hook = None
        self.credential_hook = None
        self.s2_hook = None
        self.records = [
            {"profile_id": PROFILE_A, "name": "Alpha", "url": BASE_A, "revision": 1},
            {"profile_id": PROFILE_B, "name": "Beta", "url": BASE_B, "revision": 1},
        ]
        self.keys = {"version": 2, "profiles": {
            PROFILE_A: {"url": BASE_A, "token": KEY_A, "revision": 1},
            PROFILE_B: {"url": BASE_B, "token": KEY_B, "revision": 1},
        }}

        # Both S2 module aliases and the explicit fence observers share this home.
        monkeypatch.setattr(application_config, "application_config_file", self.application_file)
        monkeypatch.setattr(profiles, "application_config_file", self.application_file)
        monkeypatch.setattr(credentials, "keys_file", self.credential_file)
        original_metadata = application_config._load_application_document
        original_environment = credentials._environment
        original_store = credentials._read_store

        def metadata():
            result = original_metadata()
            if self.metadata_hook:
                self.metadata_hook()
            return result

        def environment(url):
            self.environment_calls.append(url)
            result = original_environment(url)
            if self.s2_hook:
                self.s2_hook("environment")
            return result

        def store():
            self.store_calls.append(self.credential_file())
            result = original_store()
            if self.s2_hook:
                self.s2_hook("stored")
            return result

        monkeypatch.setattr(application_config, "_load_application_document", metadata)
        monkeypatch.setattr(credentials, "_environment", environment)
        monkeypatch.setattr(credentials, "_read_store", store)
        self.repository = profiles.RemoteProfiles()
        self._install_observer()
        self.write_metadata()
        self.write_keys()

    def location(self, kind, filename):
        self.location_calls.append(kind)
        return self.locations.get(kind, self.home / filename)

    def application_file(self):
        return self.location("application", "config.yaml")

    def credential_file(self):
        return self.location("credential", "remote-keys.json")

    def journal_file(self):
        # The actual S2 journal derives from the same live application getter.
        return self.location("journal", self.repository._journal().name)

    def read_profile(self, profile_id):
        self.metadata_calls.append(profile_id)
        return self.repository.get(profile_id)

    def read_credential(self, profile_id, url):
        self.credential_calls.append((profile_id, url))
        token = credentials.load_profile_token(profile_id, url)
        if self.credential_hook:
            self.credential_hook()
        return token

    def readers(self, **changes):
        args = dict(home=self.home, profile_reader=self.read_profile,
                    credential_reader=self.read_credential, application_file=self.application_file,
                    credential_file=self.credential_file, journal_file=self.journal_file)
        args.update(changes)
        with self.observing():
            return S2CatalogReaders(**args)

    def providers(self, *, revision=1, profile_id=PROFILE_A, **changes):
        with self.observing():
            return PendingCatalogProviders(self.readers(**changes), profile_id=profile_id,
                                           expected_revision=revision,
                                           expected_url=BASE_A if profile_id == PROFILE_A else BASE_B)

    @contextmanager
    def observing(self):
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1

    @contextmanager
    def editing(self):
        """Explicit fixture/interleaving changes, never provider permissions."""
        previous, self.active = self.active, 0
        try:
            yield
        finally:
            self.active = previous

    def write_metadata(self, **changes):
        self.records[0].update(changes)
        with self.editing():
            self.home.mkdir(parents=True, exist_ok=True)
            (self.home / "config.yaml").write_text(
                json.dumps({"remote_servers": self.records}), encoding="utf-8")

    def write_keys(self, token=KEY_A):
        self.keys["profiles"][PROFILE_A]["token"] = token
        with self.editing():
            self.home.mkdir(parents=True, exist_ok=True)
            (self.home / "remote-keys.json").write_text(json.dumps(self.keys), encoding="utf-8")

    def pending(self):
        # A pending transaction for B globally blocks A too; contents are not parsed.
        with self.editing():
            (self.home / "remote-profile-change.json").write_text(
                json.dumps({"profile_id": PROFILE_B, "url": BASE_B}), encoding="utf-8")

    def env(self, token=KEY_A, url=BASE_A):
        self.monkeypatch.setenv(credentials.URL_ENV_VAR, url)
        self.monkeypatch.setenv(credentials.TOKEN_ENV_VAR, token)

    def _deny(self, kind):
        self.counts[kind] += 1
        raise AssertionError("Provider operation is not read-only")

    def _check_open(self, path, mode):
        if not self.active:
            return
        if any(flag in mode for flag in "wax+"):
            self._deny("locks" if str(path).endswith(".lock") else "writes")
        if not isinstance(path, int):
            candidate = Path(path).absolute()
            allowed = {self.home / name for name in (
                "config.yaml", "remote-keys.json", "remote-profile-change.json")}
            if candidate not in allowed:
                self._deny("outside_reads")

    def _install_observer(self):
        for module, name in ((builtins, "open"), (io, "open")):
            original = getattr(module, name)

            def opened(path, mode="r", *args, _original=original, **kwargs):
                self._check_open(path, mode)
                return _original(path, mode, *args, **kwargs)

            self.monkeypatch.setattr(module, name, opened)
        original_os_open = os.open

        def os_open(path, flags, *args, **kwargs):
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            self._check_open(path, "w" if flags & write_flags else "r")
            return original_os_open(path, flags, *args, **kwargs)

        self.monkeypatch.setattr(os, "open", os_open)
        for module, name, kind in (
            (Path, "mkdir", "dirs"), (os, "mkdir", "dirs"),
            (Path, "rename", "writes"), (Path, "replace", "writes"),
            (Path, "unlink", "writes"), (Path, "rmdir", "writes"),
            (Path, "chmod", "writes"), (os, "replace", "writes"),
            (os, "rename", "writes"), (os, "unlink", "writes"),
            (os, "remove", "writes"), (os, "rmdir", "writes"),
            (os, "symlink", "writes"), (os, "link", "writes"),
        ):
            original = getattr(module, name)

            def mutation(*args, _original=original, _kind=kind, **kwargs):
                if self.active:
                    self._deny(_kind)
                return _original(*args, **kwargs)

            self.monkeypatch.setattr(module, name, mutation)


@pytest.fixture
def storage(tmp_path, monkeypatch, forbidden_io):
    return SyntheticStorage(tmp_path / "synthetic-s2-home", monkeypatch, forbidden_io)


def replies(action):
    result = [response(health()), response(identity(capabilities=CAPS.copy()))]
    if action == "list":
        result.append(response(page("alpha")))
    elif action == "get":
        result.append(response({"protocol": 2, "project": project()}))
    elif action == "snapshot":
        result.extend(response(snapshot_page(document(path), next_after=after))
                      for path, after in (("a.md", "a.md"), ("b.md", "b.md"), ("c.md", None)))
    elif action == "legacy":
        result.append(response(legacy("Exact legacy")))
    return result


def build(storage, action):
    provider = storage.providers()
    transport = Transport(*replies(action))
    cls = CatalogReadService if action in ("snapshot", "legacy") else CatalogService
    return cls(profiles=provider, credentials=provider.load_token, transport=transport), transport


async def invoke(service, action):
    if action == "snapshot":
        return await service.snapshot(PROFILE_A, "alpha", expected_revision=1, limit=1)
    if action == "legacy":
        return await service.resolve_legacy(PROFILE_A, "Exact legacy", expected_revision=1)
    if action == "list":
        return await service.list_projects(PROFILE_A, expected_revision=1)
    if action == "get":
        return await service.get_project(PROFILE_A, "alpha", expected_revision=1)
    return await service.verify(PROFILE_A, expected_revision=1)


@pytest.mark.parametrize("missing", [field.name for field in fields(S2CatalogReaders)])
def test_every_reader_and_location_binding_is_mandatory(storage, missing):
    values = {field.name: getattr(storage.readers(), field.name) for field in fields(S2CatalogReaders)}
    values.pop(missing)
    with pytest.raises(TypeError):
        S2CatalogReaders(**values)


def test_readers_and_profile_revision_binding_are_frozen(storage):
    readers, provider = storage.readers(), storage.providers()
    for obj, field, value in ((readers, "home", storage.home.parent),
                              (provider, "profile_id", PROFILE_B),
                              (provider, "expected_revision", 2),
                              (provider, "expected_url", BASE_B)):
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            setattr(obj, field, value)


def test_profile_url_and_revision_are_required_keyword_only_bindings(storage):
    readers = storage.readers()
    with storage.observing():
        with pytest.raises(TypeError):
            PendingCatalogProviders(readers)
        with pytest.raises(TypeError):
            PendingCatalogProviders(readers, profile_id=PROFILE_A)
        with pytest.raises(TypeError):
            PendingCatalogProviders(readers, expected_revision=1)
        with pytest.raises(TypeError):
            PendingCatalogProviders(readers, PROFILE_A, 1)
        with pytest.raises(TypeError):
            PendingCatalogProviders(readers, profile_id=PROFILE_A, expected_revision=1)


def test_nested_s3_guard_never_looks_up_new_same_revision_url(storage, caplog):
    """Change A -> B between _check's outer _profile and inner _snapshot.

    S3's final comparison is too late to prevent lookup at B on its own. An
    immutable provider URL binding must reject that inner metadata read.
    """
    provider = storage.providers()
    service = CatalogService(profiles=provider, credentials=provider.load_token,
                             transport=Transport())
    with storage.observing():
        snapshot = service._snapshot(PROFILE_A, 1)
    original_reader = storage.read_profile
    reads = 0

    def change_after_outer_read(profile_id):
        nonlocal reads
        profile = original_reader(profile_id)
        reads += 1
        if reads == 1:
            storage.write_metadata(url=MOVED_URL)  # Deliberately no revision bump.
            storage.keys["profiles"][PROFILE_A]["url"] = MOVED_URL
            storage.write_keys(KEY_B)
        return profile

    pinned = replace(provider, readers=replace(provider.readers, profile_reader=change_after_outer_read))
    service = CatalogService(profiles=pinned, credentials=pinned.load_token, transport=Transport())
    storage.credential_calls.clear()
    storage.environment_calls.clear()
    storage.store_calls.clear()
    with storage.observing(), pytest.raises(CatalogError, match="^profile_store_unavailable$") as caught:
        service._check(snapshot)
    assert reads == 2
    assert storage.credential_calls == storage.environment_calls == storage.store_calls == []
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("changes", [{"revision": 2}, {"url": MOVED_URL}])
async def test_changed_binding_between_service_metadata_and_credential_reads_blocks_new_key(
    storage, changes, caplog,
):
    service, transport = build(storage, "verify")

    def publish_after_first_read():
        storage.metadata_hook = None
        storage.write_metadata(**changes)
        storage.keys["profiles"][PROFILE_A]["url"] = changes.get("url", BASE_A)
        storage.write_keys(KEY_B)

    storage.metadata_hook = publish_after_first_read
    with storage.observing(), pytest.raises(CatalogError, match="^credential_store_unavailable$") as caught:
        await invoke(service, "verify")
    assert storage.metadata_calls == [PROFILE_A, PROFILE_A]
    assert storage.credential_calls == storage.environment_calls == storage.store_calls == transport.calls == []
    assert_safe(caught.value, caplog)


def test_two_immutable_bindings_do_not_share_last_profile_or_revision(storage, caplog):
    first, second = storage.providers(), storage.providers(profile_id=PROFILE_B)
    with storage.observing():
        first.get(PROFILE_A)
        second.get(PROFILE_B)
        assert first.load_token(PROFILE_A, BASE_A) == KEY_A
        assert second.load_token(PROFILE_B, BASE_B) == KEY_B
        storage.pending()
        for provider, profile_id, url in ((first, PROFILE_A, BASE_A), (second, PROFILE_B, BASE_B)):
            before = len(storage.credential_calls)
            with pytest.raises(ProfileError, match="^profile_recovery_required$") as caught:
                provider.load_token(profile_id, url)
            assert len(storage.credential_calls) == before
            assert_safe(caught.value, caplog)
    assert_safe((first, second), caplog)


@pytest.mark.parametrize("name", ["profile_reader", "credential_reader", "application_file",
                                 "credential_file", "journal_file"])
def test_noncallable_dependency_cannot_silently_select_a_default(storage, name):
    with storage.observing(), pytest.raises((ProfileError, TypeError)):
        storage.readers(**{name: None})
    assert storage.metadata_calls == storage.credential_calls == []


@pytest.mark.parametrize("kind", ["application", "credential", "journal"])
@pytest.mark.parametrize("failure", ["mismatch", "raises"])
def test_location_binding_rejected_before_any_s2_reader(storage, kind, failure, caplog):
    def observer():
        if failure == "raises":
            raise OSError(KEY_A)
        return storage.home.parent / (kind + "-wrong-home")

    with storage.observing(), pytest.raises(ProfileError) as caught:
        storage.providers(**{kind + "_file": observer}).load_token(PROFILE_A, BASE_A)
    assert caught.value.args == ("profile_recovery_required",)
    assert storage.metadata_calls == storage.credential_calls == []
    assert storage.environment_calls == storage.store_calls == []
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("kind", ["application", "credential", "journal"])
@pytest.mark.parametrize("during", ["metadata", "credential"])
def test_live_locations_are_rechecked_after_each_reader(storage, kind, during, caplog):
    def relocate():
        storage.locations[kind] = storage.home / "wrong-binding"

    setattr(storage, during + "_hook", relocate)
    with storage.observing(), pytest.raises(ProfileError) as caught:
        storage.providers().load_token(PROFILE_A, BASE_A)
    assert caught.value.args == ("profile_recovery_required",)
    assert len(storage.credential_calls) == (1 if during == "credential" else 0)
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("source", ["stored", "environment"])
@pytest.mark.parametrize("operation", ["get", "load_token", "service"])
async def test_other_profile_pending_blocks_globally_before_stored_or_env_lookup(
    storage, source, operation, caplog,
):
    if source == "environment":
        storage.env()
    storage.pending()
    service, transport = build(storage, "verify")
    provider = storage.providers()
    error = CatalogError if operation == "service" else ProfileError
    with storage.observing(), pytest.raises(error) as caught:
        if operation == "service":
            await invoke(service, "verify")
        elif operation == "get":
            provider.get(PROFILE_A)
        else:
            provider.load_token(PROFILE_A, BASE_A)
    assert str(caught.value) == "profile_recovery_required"
    assert storage.metadata_calls == storage.credential_calls == []
    assert storage.environment_calls == storage.store_calls == transport.calls == []
    assert_safe(caught.value, caplog)


def test_pending_between_get_and_credential_read_rejects_previously_read_metadata(storage, caplog):
    provider = storage.providers()
    with storage.observing():
        assert provider.get(PROFILE_A).url == BASE_A
        storage.pending()
        with pytest.raises(ProfileError, match="^profile_recovery_required$") as caught:
            provider.load_token(PROFILE_A, BASE_A)
    assert storage.metadata_calls == [PROFILE_A]
    assert storage.credential_calls == storage.environment_calls == storage.store_calls == []
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("operation", ["get", "load_token"])
def test_pending_inside_actual_s2_metadata_read_is_fenced_after_read(storage, operation, caplog):
    storage.metadata_hook = storage.pending
    provider = storage.providers()
    with storage.observing(), pytest.raises(ProfileError, match="^profile_recovery_required$") as caught:
        if operation == "get":
            provider.get(PROFILE_A)
        else:
            provider.load_token(PROFILE_A, BASE_A)
    assert storage.metadata_calls == [PROFILE_A]
    assert storage.credential_calls == storage.environment_calls == storage.store_calls == []
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("source", ["stored", "environment"])
def test_pending_inside_actual_s2_credential_read_is_fenced_after_read(storage, source, caplog):
    if source == "environment":
        storage.env()
    storage.s2_hook = lambda where: storage.pending() if where == source else None
    with storage.observing(), pytest.raises(ProfileError, match="^profile_recovery_required$") as caught:
        storage.providers().load_token(PROFILE_A, BASE_A)
    assert storage.credential_calls == [(PROFILE_A, BASE_A)]
    assert len(storage.store_calls) == (1 if source == "stored" else 0)
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("changes", [{"url": MOVED_URL}, {"revision": 2}, {"url": MOVED_URL, "revision": 2}])
@pytest.mark.parametrize("during", ["before", "metadata", "credential"])
def test_url_and_bound_revision_changes_never_read_key_at_new_locator(storage, changes, during, caplog):
    provider = storage.providers()
    if during == "before":
        storage.write_metadata(**changes)
    else:
        setattr(storage, during + "_hook", lambda: storage.write_metadata(**changes))
    with storage.observing(), pytest.raises(ProfileError, match="^profile_revision_conflict$") as caught:
        provider.load_token(PROFILE_A, BASE_A)
    # A change made AFTER the reader captured old metadata is only observable
    # on the next metadata read, not by a journal-absence check. No lookup at
    # the NEW locator is allowed; the original-locator lookup can have occurred.
    assert storage.credential_calls == ([(PROFILE_A, BASE_A)] if during != "before" else [])
    assert_safe(caught.value, caplog)


def test_wrong_url_or_profile_id_never_reaches_credential_reader(storage, caplog):
    provider = storage.providers()
    for profile_id, url in ((PROFILE_A, BASE_B), (PROFILE_B, BASE_B)):
        with storage.observing(), pytest.raises(ProfileError) as caught:
            provider.load_token(profile_id, url)
        assert_safe(caught.value, caplog)
    assert storage.credential_calls == storage.environment_calls == storage.store_calls == []


@pytest.mark.parametrize("problem", [PermissionError(KEY_A), OSError(KEY_B), RuntimeError(KEY_A)])
def test_unreadable_or_uncertain_journal_lstat_is_fail_closed(storage, monkeypatch, problem, caplog):
    original = Path.lstat
    calls = []

    def lstat(path, *args, **kwargs):
        if path == storage.home / "remote-profile-change.json":
            calls.append(path)
            raise problem
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    with storage.observing(), pytest.raises(ProfileError, match="^profile_recovery_required$") as caught:
        storage.providers().load_token(PROFILE_A, BASE_A)
    assert calls and storage.metadata_calls == storage.credential_calls == []
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("kind", ["file", "directory", "dangling_symlink"])
def test_any_journal_entry_including_dangling_link_blocks_without_reading_it(storage, monkeypatch, kind, caplog):
    journal = storage.home / "remote-profile-change.json"
    with storage.editing():
        if kind == "file":
            journal.write_text(KEY_A, encoding="utf-8")
        elif kind == "directory":
            journal.mkdir()
        else:
            try:
                journal.symlink_to(storage.home / "missing-symlink-target")
            except OSError:
                # Windows may lack symlink privileges: still exercise lstat, no skip.
                original = Path.lstat

                def dangling(path, *args, **kwargs):
                    if path == journal:
                        return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
                    return original(path, *args, **kwargs)

                monkeypatch.setattr(Path, "lstat", dangling)
    with storage.observing(), pytest.raises(ProfileError, match="^profile_recovery_required$") as caught:
        storage.providers().load_token(PROFILE_A, BASE_A)
    assert storage.metadata_calls == storage.credential_calls == []
    assert_safe(caught.value, caplog)


async def test_absent_synthetic_home_creates_no_directory_lock_or_store(storage, caplog):
    storage.home = storage.home.parent / "absent-parent" / "absent-home"
    provider = storage.providers()
    transport = Transport()
    service = CatalogService(profiles=provider, credentials=provider.load_token, transport=transport)
    errors = []
    with storage.observing():
        for operation in (lambda: provider.get(PROFILE_A), lambda: provider.load_token(PROFILE_A, BASE_A)):
            with pytest.raises(ProfileError, match="^profile_not_found$") as caught:
                operation()
            errors.append(caught.value)
        with pytest.raises(CatalogError, match="^profile_not_found$") as caught:
            await invoke(service, "verify")
        errors.append(caught.value)
    assert not storage.home.parent.exists()
    assert storage.credential_calls == storage.environment_calls == storage.store_calls == transport.calls == []
    for error in errors:
        assert_safe(error, caplog)


@pytest.mark.parametrize("reader", ["profile_reader", "credential_reader"])
@pytest.mark.parametrize("pending", [False, True])
def test_reader_exceptions_are_sanitized_and_failed_reads_still_fenced(storage, reader, pending, caplog):
    def failed(*_args):
        if pending:
            storage.pending()
        raise RuntimeError(KEY_A)

    provider = storage.providers(**{reader: failed})
    expected = ("profile_recovery_required" if pending else
                "profile_store_unavailable" if reader == "profile_reader" else "credential_store_unavailable")
    with storage.observing(), pytest.raises(ProfileError, match="^" + expected + "$") as caught:
        provider.load_token(PROFILE_A, BASE_A)
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("action", ["verify", "snapshot"])
@pytest.mark.parametrize("reader,code", [("profile_reader", "profile_store_unavailable"),
                                       ("credential_reader", "credential_store_unavailable")])
async def test_both_services_keep_generic_provider_errors_fixed_and_secret_free(storage, action, reader, code, caplog):
    def failed(*_args):
        raise RuntimeError(KEY_B)

    provider = storage.providers(**{reader: failed})
    transport = Transport()
    cls = CatalogReadService if action == "snapshot" else CatalogService
    service = cls(profiles=provider, credentials=provider.load_token, transport=transport)
    with storage.observing(), pytest.raises(CatalogError, match="^" + code + "$") as caught:
        await invoke(service, action)
    assert transport.calls == []
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("which,code", [("metadata", "profile_store_unavailable"),
                                      ("credential", "credential_store_unavailable")])
async def test_actual_s2_corrupt_storage_is_not_defaulted_or_rewritten(storage, which, code, caplog):
    path = storage.home / ("config.yaml" if which == "metadata" else "remote-keys.json")
    with storage.editing():
        path.write_text("[" + KEY_A, encoding="utf-8")
    before = {p.name: p.read_bytes() for p in storage.home.iterdir()}
    service, transport = build(storage, "verify")
    with storage.observing(), pytest.raises(CatalogError, match="^" + code + "$") as caught:
        await invoke(service, "verify")
    assert transport.calls == []
    assert {p.name: p.read_bytes() for p in storage.home.iterdir()} == before
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("source", ["legacy_only", "unbound_environment", "missing"])
async def test_s2_missing_or_unbound_keys_never_fall_back_to_another_key(storage, source, caplog):
    with storage.editing():
        (storage.home / "remote-keys.json").write_text(
            json.dumps({BASE_A: KEY_B} if source == "legacy_only" else
                       storage.keys if source == "unbound_environment" else {}), encoding="utf-8")
    if source == "unbound_environment":
        storage.env(KEY_B, url=BASE_B)
    service, transport = build(storage, "verify")
    with storage.observing(), pytest.raises(CatalogError, match="^credential_required$") as caught:
        await invoke(service, "verify")
    assert transport.calls == []
    assert storage.credential_calls == [(PROFILE_A, BASE_A)]
    if source == "unbound_environment":
        assert storage.store_calls == []
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("source", ["stored", "environment"])
@pytest.mark.parametrize("action", ["verify", "list", "get", "snapshot", "legacy"])
async def test_actual_s2_readers_support_both_s3_services_without_writes(storage, source, action, caplog):
    if source == "environment":
        storage.env()
    before = {path.name: path.read_bytes() for path in storage.home.iterdir()}
    service, transport = build(storage, action)
    with storage.observing():
        result = await invoke(service, action)
    assert storage.metadata_calls and storage.credential_calls
    assert set(storage.credential_calls) == {(PROFILE_A, BASE_A)}
    assert storage.environment_calls
    assert bool(storage.store_calls) == (source == "stored")
    assert {path.name: path.read_bytes() for path in storage.home.iterdir()} == before
    assert_safe((service, storage.providers(), storage.readers(), result, asdict(result)), caplog)
    for call in transport.calls:
        assert call["method"] == "GET" and call["json"] is None
        assert call["url"].startswith(BASE_A + "/")
        assert call["follow_redirects"] is False
        assert call["headers"]["Cache-Control"] == "no-store"
        if call["url"].endswith("/healthz"):
            assert "Authorization" not in call["headers"]
        else:
            assert call["headers"]["Authorization"] == "Bearer " + KEY_A


@pytest.mark.parametrize("action,stage", [("list", i) for i in range(1, 4)] +
                         [("snapshot", i) for i in range(1, 6)])
@pytest.mark.parametrize("mutation,code", [
    ("pending", "profile_recovery_required"),
    # Existing S3 maps provider-side binding conflicts to this fixed code.
    # Do not widen its error API as part of the read-only provider slice.
    ("url", "profile_store_unavailable"),
    ("revision", "profile_store_unavailable"),
    ("stored_key", "credential_binding_conflict"),
    ("env_key", "credential_binding_conflict"),
])
async def test_pending_locator_revision_and_key_changes_during_await_return_no_partial_result(
    storage, action, stage, mutation, code, caplog,
):
    if mutation == "env_key":
        storage.env()
    service, transport = build(storage, action)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    with storage.observing():
        operation = asyncio.create_task(invoke(service, action))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            if mutation == "pending":
                storage.pending()
            elif mutation == "url":
                storage.write_metadata(url=MOVED_URL)
            elif mutation == "revision":
                storage.write_metadata(revision=2)
            elif mutation == "stored_key":
                storage.write_keys(KEY_B)
            else:
                storage.env(KEY_B)
            resume.set()
            sentinel = object()
            result = sentinel
            with pytest.raises(CatalogError) as caught:
                result = await asyncio.wait_for(operation, timeout=2)
            assert caught.value.code == code
            assert result is sentinel  # No DTO or accumulated first-page prefix escapes.
            assert len(transport.calls) == stage
            assert set(storage.credential_calls) == {(PROFILE_A, BASE_A)}
        finally:
            resume.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("action,stage", [("list", 1), ("list", 3), ("snapshot", 4)])
async def test_pending_after_failed_fake_await_still_stops_followup_and_partial_result(storage, action, stage, caplog):
    service, transport = build(storage, action)
    transport.replies[stage - 1] = RuntimeError(KEY_A)

    async def pending(index):
        if index == stage:
            storage.pending()
        await asyncio.sleep(0)

    transport.hook = pending
    with storage.observing(), pytest.raises(CatalogError, match="^profile_recovery_required$") as caught:
        await invoke(service, action)
    assert len(transport.calls) == stage
    assert_safe(caught.value, caplog)
