"""Personal profile credentials (§5.2.1), using task-local homes and no network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from voxarium_studio.sync import credentials

PROFILE = "c503da2b-f8fe-4fb0-b945-86bc119495b3"
OTHER_PROFILE = "81ccda2b-f8fe-4fb0-b945-86bc119495b3"
URL = "https://remote.example/team"
SECRET = "vxs_fixture-secret-not-for-output"


@pytest.fixture(autouse=True)
def no_remote_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)


def _put_raw(raw: object) -> bytes:
    path = credentials.keys_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path.read_bytes()


def _store() -> dict:
    return json.loads(credentials.keys_file().read_text(encoding="utf-8"))


def test_credentials_reads_do_not_create_or_upgrade(studio_home: Path) -> None:
    assert credentials.load_profile_token(PROFILE, URL) is None
    assert credentials.profile_token_revision(PROFILE, URL) == 0
    assert credentials.profile_token_source(PROFILE, URL) == "missing"
    assert credentials.load_token(URL) is None
    assert list(studio_home.iterdir()) == []
    before = _put_raw({URL: SECRET})
    assert credentials.load_profile_token(PROFILE, URL) is None
    assert credentials.load_token(URL) == SECRET
    assert credentials.keys_file().read_bytes() == before
    assert list(studio_home.iterdir()) == [credentials.keys_file()]


def test_credentials_require_profile_and_normalized_url(studio_home: Path) -> None:
    revision = credentials.save_profile_token(PROFILE, "wss://REMOTE.example/team/", SECRET,
                                              expected_revision=0)
    assert revision == 1
    assert credentials.load_profile_token(PROFILE, URL) == SECRET
    assert credentials.load_profile_token(PROFILE, "remote.example/team/") == SECRET
    assert credentials.load_profile_token(OTHER_PROFILE, URL) is None
    assert credentials.load_profile_token(PROFILE, URL + "/other") is None
    assert credentials.load_profile_token(PROFILE, "http://remote.example/team") is None
    assert credentials.profile_token_revision(PROFILE, URL + "/other") == 0
    assert credentials.profile_token_source(PROFILE, URL) == "stored"
    assert credentials.load_token(URL) is None, "new keys do not enter the legacy runtime"
    assert credentials.keys_file().is_relative_to(studio_home)
    assert _store() == {"version": 2, "profiles": {
        PROFILE: {"url": URL, "token": SECRET, "revision": 1}}, "legacy": {}}


@pytest.mark.parametrize("profile", ["", "profile", PROFILE.upper(), PROFILE.replace("-", ""),
                                    " " + PROFILE, None, True])
def test_credentials_profile_id_validation_is_exact(profile: str) -> None:
    with pytest.raises(credentials.CredentialsError, match="invalid_profile_id"):
        credentials.save_profile_token(profile, URL, SECRET, expected_revision=0)
    assert not credentials.keys_file().exists()


@pytest.mark.parametrize("url", ["", "ftp://" + SECRET, "https://user:" + SECRET + "@host",
                                "https://host?" + SECRET, "https://host/#" + SECRET,
                                "https://[invalid", "https://host:" + SECRET, None])
def test_credentials_invalid_url_is_redacted(url: str, caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(credentials.CredentialsError) as caught:
        credentials.save_profile_token(PROFILE, url, SECRET, expected_revision=0)
    assert SECRET not in str(caught.value) + repr(caught.value) + caplog.text
    assert caught.value.__suppress_context__ or caught.value.__context__ is None
    assert not credentials.keys_file().exists()


@pytest.mark.parametrize("token", ["", " ", 42, False, []])
def test_credentials_empty_input_is_not_deletion(token: object) -> None:
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    before = credentials.keys_file().read_bytes()
    with pytest.raises(credentials.CredentialsError, match="invalid_credential"):
        credentials.save_profile_token(PROFILE, URL, token, expected_revision=1)
    assert credentials.keys_file().read_bytes() == before


@pytest.mark.parametrize("revision", [None, True, -1, 1.0, "0"])
def test_credentials_cas_requires_explicit_integer(revision: object) -> None:
    with pytest.raises(credentials.CredentialsError, match="invalid_credential_revision"):
        credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=revision)
    assert not credentials.keys_file().exists()


def test_credentials_cas_delete_and_aba_protection() -> None:
    assert credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0) == 1
    with pytest.raises(credentials.CredentialConflict):
        credentials.save_profile_token(PROFILE, URL, "replacement", expected_revision=0)
    assert credentials.save_profile_token(PROFILE, URL, "replacement", expected_revision=1) == 2
    assert credentials.save_profile_token(PROFILE, URL, None, expected_revision=2) == 3
    assert credentials.load_profile_token(PROFILE, URL) is None
    assert credentials.profile_token_revision(PROFILE, URL) == 3
    assert credentials.profile_token_source(PROFILE, URL) == "missing"
    for stale_revision in (0, 1, 2):
        with pytest.raises(credentials.CredentialConflict):
            credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=stale_revision)
    assert credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=3) == 4


def test_credentials_never_transfer_live_key_to_new_url() -> None:
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    with pytest.raises(credentials.CredentialConflict):
        credentials.save_profile_token(PROFILE, URL + "/new", SECRET, expected_revision=1)
    credentials.save_profile_token(PROFILE, URL, None, expected_revision=1)
    assert credentials.save_profile_token(PROFILE, URL + "/new", "new", expected_revision=2) == 3
    assert credentials.load_profile_token(PROFILE, URL) is None
    assert credentials.load_profile_token(PROFILE, URL + "/new") == "new"


def test_credentials_explicit_upgrade_preserves_all_legacy_data() -> None:
    legacy = {URL: SECRET, "wss://REMOTE.example/team/": SECRET, "odd old address": "other"}
    before = _put_raw(legacy)
    assert credentials.load_token(URL) == SECRET
    assert credentials.keys_file().read_bytes() == before
    assert credentials.import_legacy_token(PROFILE, "remote.example/team") == 1
    assert _store()["legacy"] == legacy
    assert credentials.load_token(URL) == SECRET
    upgraded = credentials.keys_file().read_bytes()
    assert credentials.import_legacy_token(PROFILE, URL) == 1
    assert credentials.keys_file().read_bytes() == upgraded, "replay must not rewrite"
    assert credentials.load_profile_token(PROFILE, URL) == SECRET


def test_credentials_explicit_save_upgrades_without_import() -> None:
    _put_raw({URL: "old"})
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    assert credentials.load_token(URL) == "old"
    assert credentials.load_profile_token(PROFILE, URL) == SECRET
    assert _store()["legacy"] == {URL: "old"}


def test_credentials_legacy_api_preserves_v2_profiles_and_old_empty_semantics() -> None:
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    profile = _store()["profiles"]
    credentials.save_token(URL, "legacy-new")
    assert credentials.load_token(URL) == "legacy-new"
    assert credentials.load_profile_token(PROFILE, URL) == SECRET
    assert _store()["profiles"] == profile
    credentials.save_token(URL, "")
    assert credentials.load_token(URL) is None
    assert _store()["profiles"] == profile


def test_credentials_legacy_save_does_not_silently_upgrade() -> None:
    credentials.save_token(URL, SECRET)
    assert _store() == {URL: SECRET}
    credentials.save_token(URL, None)
    assert _store() == {}


def test_credentials_import_absent_is_noop() -> None:
    assert credentials.import_legacy_token(PROFILE, URL) == 0
    assert not credentials.keys_file().exists()
    before = _put_raw({URL + "/other": SECRET})
    assert credentials.import_legacy_token(PROFILE, URL) == 0
    assert credentials.keys_file().read_bytes() == before


def test_credentials_import_conflicting_normalized_urls_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = _put_raw({URL: SECRET, "wss://REMOTE.example/team/": "different-secret"})
    with pytest.raises(credentials.CredentialConflict) as caught:
        credentials.import_legacy_token(PROFILE, URL)
    assert credentials.keys_file().read_bytes() == before
    output = str(caught.value) + repr(caught.value) + caplog.text
    assert SECRET not in output and "different-secret" not in output


def test_credentials_import_never_overwrites_existing_key() -> None:
    _put_raw({URL: "legacy"})
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    before = credentials.keys_file().read_bytes()
    with pytest.raises(credentials.CredentialConflict):
        credentials.import_legacy_token(PROFILE, URL, expected_revision=1)
    assert credentials.keys_file().read_bytes() == before


def test_credentials_import_checks_cas_after_deletion() -> None:
    _put_raw({URL: SECRET})
    credentials.import_legacy_token(PROFILE, URL)
    credentials.save_profile_token(PROFILE, URL, None, expected_revision=1)
    with pytest.raises(credentials.CredentialConflict):
        credentials.import_legacy_token(PROFILE, URL)
    assert credentials.import_legacy_token(PROFILE, URL, expected_revision=2) == 3


@pytest.mark.parametrize("raw", [
    "{\"secret\":\"" + SECRET, "[]", "null", "{\"version\":3}",
    '{"version":2,"profiles":[]}', '{"version":true,"profiles":{}}',
    '{"version":2,"profiles":{},"legacy":[]}',
    '{"version":2,"profiles":{},"unexpected":"' + SECRET + '"}',
    '{"version":2,"version":2,"profiles":{}}',
    json.dumps({URL: 42}),
    json.dumps({"version": 2, "profiles": {PROFILE: {"url": URL, "token": SECRET,
                                                   "revision": True}}}),
    json.dumps({"version": 2, "profiles": {PROFILE: {"url": "REMOTE.example/team/",
                                                   "token": SECRET, "revision": 1}}}),
])
def test_credentials_corrupt_never_overwritten_and_errors_are_redacted(
    raw: str, caplog: pytest.LogCaptureFixture,
) -> None:
    path = credentials.keys_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    before = path.read_bytes()
    for mutation in (
        lambda: credentials.save_token(URL, "new"),
        lambda: credentials.save_profile_token(PROFILE, URL, "new", expected_revision=0),
        lambda: credentials.import_legacy_token(PROFILE, URL),
        lambda: credentials.load_profile_token(PROFILE, URL),
    ):
        with pytest.raises(credentials.CredentialsError) as caught:
            mutation()
        assert SECRET not in str(caught.value) + repr(caught.value)
        assert path.read_bytes() == before
    assert credentials.load_token(URL) is None
    assert SECRET not in caplog.text and raw not in caplog.text


def test_credentials_invalid_utf8_is_not_replaced() -> None:
    path = credentials.keys_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe" + SECRET.encode())
    with pytest.raises(credentials.CredentialsError):
        credentials.save_token(URL, "new")
    assert path.read_bytes() == b"\xff\xfe" + SECRET.encode()


@pytest.mark.parametrize("env_url", [None, "", "https://different.example", URL + "/other",
                                    "ftp://" + SECRET])
def test_credentials_env_unbound_never_falls_back(
    env_url: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials.save_profile_token(PROFILE, URL, "stored", expected_revision=0)
    before = credentials.keys_file().read_bytes()
    monkeypatch.setenv(credentials.TOKEN_ENV_VAR, SECRET)
    if env_url is not None:
        monkeypatch.setenv(credentials.URL_ENV_VAR, env_url)
    assert credentials.load_profile_token(PROFILE, URL) is None
    assert credentials.profile_token_source(PROFILE, URL) == "environment_unbound"
    assert credentials.profile_token_revision(PROFILE, URL) == 1
    assert credentials.load_token(URL) == SECRET, "legacy runtime ENV remains unchanged"
    assert credentials.keys_file().read_bytes() == before


def test_credentials_env_matching_is_volatile_and_url_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(credentials.TOKEN_ENV_VAR, SECRET)
    monkeypatch.setenv(credentials.URL_ENV_VAR, "wss://REMOTE.example/team/")
    assert credentials.load_profile_token(PROFILE, URL) == SECRET
    assert credentials.profile_token_source(PROFILE, URL) == "environment"
    assert credentials.load_profile_token(OTHER_PROFILE, URL + "/other") is None
    assert credentials.profile_token_revision(PROFILE, URL) == 0
    assert not credentials.keys_file().exists()


def test_credentials_empty_env_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    monkeypatch.setenv(credentials.TOKEN_ENV_VAR, "")
    monkeypatch.setenv(credentials.URL_ENV_VAR, URL)
    assert credentials.load_profile_token(PROFILE, URL) is None


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_credentials_atomic_failure_keeps_complete_store_and_cleans_temp(
    failure: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    before = credentials.keys_file().read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("sensitive system error " + SECRET)

    if failure == "fsync":
        monkeypatch.setattr(credentials.os, "fsync", fail)
    else:
        monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(credentials.CredentialsError) as caught:
        credentials.save_profile_token(PROFILE, URL, "replacement", expected_revision=1)
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert credentials.keys_file().read_bytes() == before
    assert not list(credentials.keys_file().parent.glob(".remote-keys-*.tmp"))


def test_credentials_fsync_precedes_atomic_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real_fsync, real_replace = os.fsync, Path.replace

    def fsync(fd):
        calls.append("fsync")
        real_fsync(fd)

    def replace(self, target):
        assert calls == ["fsync"]
        calls.append("replace")
        assert json.loads(self.read_text(encoding="utf-8"))["profiles"][PROFILE]["token"] == SECRET
        return real_replace(self, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(Path, "replace", replace)
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    assert calls[:2] == ["fsync", "replace"]
    if os.name != "nt":
        assert credentials.keys_file().stat().st_mode & 0o777 == 0o600
        assert calls[-1] == "fsync", "POSIX directory must be durable too"


def test_credentials_thread_cas_has_one_winner() -> None:
    barrier = Barrier(8)

    def write(i):
        barrier.wait()
        try:
            return credentials.save_profile_token(PROFILE, URL, f"key-{i}", expected_revision=0)
        except credentials.CredentialConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(write, range(8)))
    assert results.count(1) == 1 and results.count("conflict") == 7
    assert credentials.profile_token_revision(PROFILE, URL) == 1


def test_credentials_concurrent_different_profiles_do_not_lose_updates() -> None:
    ids = [str(uuid4()) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(
            lambda profile_id: credentials.save_profile_token(profile_id, URL, SECRET,
                                                                expected_revision=0), ids))
    assert results == [1] * 12
    assert set(_store()["profiles"]) == set(ids)


def test_credentials_process_cas_has_one_winner(tmp_path: Path) -> None:
    # Independent interpreters exercise the OS lock, not just the module's RLock.
    script = """
import sys, time
from pathlib import Path
from voxarium_studio.sync import credentials
ready, go, profile, url = sys.argv[1:]
Path(ready).touch()
deadline = time.monotonic() + 20
while not Path(go).exists():
    if time.monotonic() > deadline: raise RuntimeError('barrier timeout')
    time.sleep(.01)
try:
    print(credentials.save_profile_token(profile, url, 'process-fixture', expected_revision=0))
except credentials.CredentialConflict:
    print('conflict')
"""
    script_file = tmp_path / "credential-worker.py"
    script_file.write_text(script, encoding="utf-8")
    go = tmp_path / "go"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    processes = [subprocess.Popen(
        [sys.executable, str(script_file), str(tmp_path / f"ready-{i}"), str(go), PROFILE, URL],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    ) for i in range(3)]
    try:
        import time

        deadline = time.monotonic() + 20
        while len(list(tmp_path.glob("ready-*"))) < 3:
            assert time.monotonic() < deadline, "independent workers did not start"
            time.sleep(.01)
        go.touch()
        results = [process.communicate(timeout=30) for process in processes]
        assert all(process.returncode == 0 for process in processes), results
        assert sorted(stdout.strip() for stdout, _ in results) == ["1", "conflict", "conflict"]
        assert all(not stderr for _, stderr in results)
        assert credentials.profile_token_revision(PROFILE, URL) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()


@pytest.mark.parametrize("after_replace", [False, True])
def test_credentials_abrupt_process_exit_never_leaves_partial_store(
    tmp_path: Path, after_replace: bool,
) -> None:
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    # Exit at the publication boundary rather than raising an exception: finally blocks
    # cannot clean up or disguise a crash. Both sides must leave a complete valid store.
    script = """
import os, sys
from pathlib import Path
from voxarium_studio.sync import credentials
real_replace = Path.replace
def crash(self, target):
    if sys.argv[1] == 'after': real_replace(self, target)
    os._exit(71)
Path.replace = crash
credentials.save_profile_token(sys.argv[2], sys.argv[3], 'replacement', expected_revision=1)
"""
    script_file = tmp_path / "credential-crash.py"
    script_file.write_text(script, encoding="utf-8")
    process = subprocess.run(
        [sys.executable, str(script_file), "after" if after_replace else "before", PROFILE, URL],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 71 and not process.stdout and not process.stderr
    assert credentials.load_profile_token(PROFILE, URL) == ("replacement" if after_replace else SECRET)
    revision = credentials.profile_token_revision(PROFILE, URL)
    assert revision == (2 if after_replace else 1)
    # OS lock releases even after os._exit; normal explicit writes remain possible.
    assert credentials.save_profile_token(PROFILE, URL, "recovered", expected_revision=revision) == revision + 1


def test_credentials_os_read_errors_are_redacted(monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture) -> None:
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    real_read = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == credentials.keys_file():
            raise PermissionError(SECRET)
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(credentials.CredentialsError) as caught:
        credentials.save_profile_token(PROFILE, URL, "new", expected_revision=1)
    assert credentials.load_token(URL) is None
    assert SECRET not in repr(caught.value) + str(caught.value) + caplog.text


def test_credentials_permissions_are_restricted_before_any_secret_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def restrict(path):
        assert path != credentials.keys_file()
        assert path.parent == credentials.keys_file().parent
        assert path.read_bytes() == b"", "no secret may precede the ACL attempt"
        calls.append(path)

    monkeypatch.setattr(credentials, "_windows_owner_only", restrict)
    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    credentials.save_token(URL, "legacy")
    assert len(calls) == 2
    assert all(not path.exists() for path in calls)


def test_credentials_acl_failure_is_best_effort_and_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(_path):
        raise OSError("sensitive ACL diagnostic " + SECRET)

    monkeypatch.setattr(credentials, "_windows_owner_only", fail)
    assert credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0) == 1
    assert credentials.load_profile_token(PROFILE, URL) == SECRET
    assert "Could not restrict remote credential file permissions" in caplog.text
    assert SECRET not in caplog.text and str(credentials.keys_file()) not in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="Windows security APIs")
@pytest.mark.parametrize("failure", [None, "owner", "sid", "descriptor", "dacl", "set"])
def test_credentials_windows_acl_calls_and_frees_allocations(
    failure: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    allocations, calls = [], []
    sid_text = ctypes.create_unicode_buffer("S-1-5-21-1-2-3-1001")

    def assign(argument, value, kind=ctypes.c_void_p):
        ctypes.cast(argument, ctypes.POINTER(kind))[0] = kind(value)

    def owner(path, object_type, flags, owner_out, group, dacl, sacl, descriptor_out):
        assert path == str(tmp_path / "empty-secret") and object_type == 1 and flags == 1
        assert group is dacl is sacl is None
        calls.append("owner")
        if failure == "owner":
            return 5
        assign(owner_out, 101)
        assign(descriptor_out, 102)
        allocations.append(102)
        return 0

    def sid(owner, output):
        assert owner.value == 101
        calls.append("sid")
        if failure == "sid":
            return 0
        assign(output, ctypes.addressof(sid_text))
        allocations.append(ctypes.addressof(sid_text))
        return 1

    def descriptor(sddl, version, output, size):
        assert sddl == "D:P(A;;FA;;;S-1-5-21-1-2-3-1001)" and version == 1 and size is None
        calls.append("descriptor")
        if failure == "descriptor":
            return 0
        assign(output, 103)
        allocations.append(103)
        return 1

    def dacl(descriptor, present, output, defaulted):
        assert descriptor.value == 103
        calls.append("dacl")
        if failure == "dacl":
            return 0
        assign(present, 1, wintypes.BOOL)
        assign(defaulted, 0, wintypes.BOOL)
        assign(output, 104)
        return 1

    def set_dacl(path, object_type, flags, owner, group, dacl, sacl):
        assert path == str(tmp_path / "empty-secret") and object_type == 1
        assert flags == 0x80000004, "replace DACL and disable inheritance"
        assert owner is group is sacl is None and dacl.value == 104
        calls.append("set")
        return 5 if failure == "set" else 0

    freed = []
    advapi = SimpleNamespace(
        GetNamedSecurityInfoW=owner, ConvertSidToStringSidW=sid,
        ConvertStringSecurityDescriptorToSecurityDescriptorW=descriptor,
        GetSecurityDescriptorDacl=dacl, SetNamedSecurityInfoW=set_dacl,
    )
    kernel = SimpleNamespace(LocalFree=lambda allocation: freed.append(allocation.value))
    monkeypatch.setattr(credentials, "_windows_acl_api", lambda: (advapi, kernel))
    if failure is None:
        credentials._windows_owner_only(tmp_path / "empty-secret")
        assert calls == ["owner", "sid", "descriptor", "dacl", "set"]
    else:
        with pytest.raises(OSError, match="windows_acl_unavailable"):
            credentials._windows_owner_only(tmp_path / "empty-secret")
        assert calls[-1] == failure
    assert freed == list(reversed(allocations)), "LocalFree every successful API allocation"


@pytest.mark.skipif(os.name != "nt", reason="Windows security APIs")
def test_credentials_real_windows_fixture_has_only_protected_owner_ace(studio_home: Path) -> None:
    import ctypes
    from ctypes import wintypes

    advapi, kernel = credentials._windows_acl_api()
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL

    # The fixture's original inherited ACL may be broad. Set an explicitly permissive,
    # inheritable Everyone ACE on this task-local home to reproduce a relocated home.
    broad, broad_dacl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    try:
        assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;OICI;FA;;;WD)", 1, ctypes.byref(broad), None)
        assert advapi.GetSecurityDescriptorDacl(
            broad, ctypes.byref(present), ctypes.byref(broad_dacl), ctypes.byref(defaulted))
        assert advapi.SetNamedSecurityInfoW(str(studio_home), 1, 0x80000004,
                                           None, None, broad_dacl, None) == 0
    finally:
        if broad.value:
            kernel.LocalFree(broad)

    credentials.save_profile_token(PROFILE, URL, SECRET, expected_revision=0)
    descriptor, owner, owner_text, text = (ctypes.c_void_p() for _ in range(4))
    try:
        assert advapi.GetNamedSecurityInfoW(
            str(credentials.keys_file()), 1, 5, ctypes.byref(owner), None,
            None, None, ctypes.byref(descriptor)) == 0
        assert advapi.ConvertSidToStringSidW(owner, ctypes.byref(owner_text))
        # Include O: too, so Windows uses the same SID alias on both components.
        assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 5, ctypes.byref(text), None)
        sddl = ctypes.wstring_at(text)
        assert sddl.startswith("O:") and "D:P" in sddl
        owner_sid, dacl_text = sddl[2:].split("D:", 1)
        control, ace = dacl_text.split("(", 1)
        # Windows can retain AUTO_INHERITED bookkeeping even on a protected DACL;
        # this is not an inherited ACE (which would carry ID in the ACE flags).
        assert control in {"P", "PAI"}, sddl
        assert ace == f"A;;FA;;;{owner_sid})", sddl
        assert credentials.load_profile_token(PROFILE, URL) == SECRET
    finally:
        for allocation in (text, owner_text, descriptor):
            if allocation.value:
                kernel.LocalFree(allocation)


def test_credentials_import_preflight_is_readonly_and_predicts_revision(studio_home: Path) -> None:
    assert credentials.preflight_legacy_import(PROFILE, URL) == 0
    assert list(studio_home.iterdir()) == []
    before = _put_raw({URL: SECRET})
    assert credentials.preflight_legacy_import(PROFILE, URL) == 1
    assert credentials.keys_file().read_bytes() == before
    assert list(studio_home.iterdir()) == [credentials.keys_file()]
    assert credentials.import_legacy_token(PROFILE, URL) == 1
    before = credentials.keys_file().read_bytes()
    assert credentials.preflight_legacy_import(PROFILE, URL) == 1
    assert credentials.keys_file().read_bytes() == before


@pytest.mark.parametrize("existing_key", [False, True])
def test_credentials_import_preflight_rejects_known_conflict_without_writes(
    existing_key: bool, studio_home: Path,
) -> None:
    _put_raw({URL: SECRET})
    if existing_key:
        credentials.save_profile_token(PROFILE, URL, "another", expected_revision=0)
    else:
        _put_raw({URL: SECRET, "wss://REMOTE.example/team/": "different"})
    before, files = credentials.keys_file().read_bytes(), list(studio_home.iterdir())
    with pytest.raises(credentials.CredentialConflict):
        credentials.preflight_legacy_import(PROFILE, URL, expected_revision=int(existing_key))
    assert credentials.keys_file().read_bytes() == before
    assert list(studio_home.iterdir()) == files


def test_credentials_import_revalidates_after_preflight() -> None:
    _put_raw({URL: SECRET})
    assert credentials.preflight_legacy_import(PROFILE, URL) == 1
    credentials.save_profile_token(PROFILE, URL, "racing-key", expected_revision=0)
    before = credentials.keys_file().read_bytes()
    with pytest.raises(credentials.CredentialConflict):
        credentials.import_legacy_token(PROFILE, URL)
    assert credentials.keys_file().read_bytes() == before
