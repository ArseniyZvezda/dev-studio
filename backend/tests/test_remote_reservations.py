"""Local-only reservation fixtures (studio-design §4.3/§8/S4, VS-162).

No AppContext, RemoteSync, real profile/home/key, or server is needed. These are
in-process thread-safety/capability tests, not distributed or durable leases.
"""
from __future__ import annotations

import builtins
from concurrent.futures import ThreadPoolExecutor
import copy
import os
from pathlib import Path
import shutil
import socket
import subprocess
from threading import Barrier, Event

import pytest

from voxarium_studio.core.project_scope import canonical_root
from voxarium_studio.sync import reservations as module
from voxarium_studio.sync.protocol import normalise_url
from voxarium_studio.sync.reservations import (
    ReservationError, ReservationLease, ReservationOwner,
)


URL = "https://reservations.invalid/team"
IDENTITY = "synthetic-reservation-project"


@pytest.fixture(autouse=True)
def private_fixture_io(tmp_path, studio_home, monkeypatch):
    """Forbid even swallowed network/key/profile calls; isolate all home paths."""
    from voxarium_studio.core import application_config, paths
    from voxarium_studio.sync import credentials, profiles

    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("external I/O")
        raise AssertionError("Reservation fixture attempted external I/O")

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_URL", raising=False)
    monkeypatch.setattr(paths, "user_config_dir", lambda: studio_home)
    monkeypatch.setattr(application_config, "user_config_dir", lambda: studio_home)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
                 "getnameinfo", "socket", "socketpair", "create_connection", "create_server"):
        if hasattr(socket, name):
            monkeypatch.setattr(socket, name, forbidden)
    for name in ("keys_file", "load_token", "save_token", "load_profile_token",
                 "save_profile_token", "import_legacy_token"):
        monkeypatch.setattr(credentials, name, forbidden)
    for name in ("get", "list", "load_token"):
        monkeypatch.setattr(profiles.RemoteProfiles, name, forbidden)
    yield
    assert calls == []
    assert list(studio_home.iterdir()) == []


@pytest.fixture
def root(tmp_path):
    result = tmp_path / "project"
    result.mkdir()
    return result


@pytest.fixture
def owner():
    return ReservationOwner()


def reserve(owner, root, **changes):
    return owner.reserve(root=root, url=changes.get("url", URL),
                         persistent_id=changes.get("persistent_id", IDENTITY))


def error(code, call, *, owner_root=None):
    with pytest.raises(ReservationError) as caught:
        call()
    value = caught.value
    assert value.code == code
    assert value.args == (code,)
    assert str(value) == code
    assert value.__cause__ is None
    assert value.__context__ is None
    assert value.owner_root == owner_root
    if owner_root is not None:
        assert isinstance(value.owner_root, Path)
    return value


def test_same_root_and_destination_return_exact_opaque_lease(owner, root):
    lease = reserve(owner, root)
    assert isinstance(lease, ReservationLease)
    assert reserve(owner, str(root)) is lease
    assert not hasattr(lease, "__dict__")
    for name in ("root", "url", "persistent_id", "owner", "owner_root", "token", "secret"):
        assert not hasattr(lease, name)
    for rendered in (str(lease), repr(lease)):
        assert str(root) not in rendered
        assert "reservations.invalid" not in rendered
        assert IDENTITY not in rendered


@pytest.mark.parametrize("url", [
    "https://RESERVATIONS.invalid/team/",
    "wss://reservations.invalid/team///",
    "reservations.invalid/team",
    "  https://reservations.invalid/team/  ",
])
def test_normalized_url_aliases_share_one_destination(owner, root, tmp_path, url):
    assert normalise_url(url) == URL
    lease = reserve(owner, root)
    assert reserve(owner, root, url=url) is lease
    error("duplicate_local_binding", lambda: reserve(owner, tmp_path / "copy", url=url),
          owner_root=canonical_root(root))


@pytest.mark.parametrize("alias_kind", ["parent", "relative", "string", "case"])
def test_lexical_root_aliases_reuse_exact_lease(owner, root, tmp_path, monkeypatch, alias_kind):
    (root / "child").mkdir()
    monkeypatch.chdir(tmp_path)
    aliases = {
        "parent": root / "child" / "..",
        "relative": Path("project"),
        "string": str(root) + os.sep + ".",
        "case": str(root).upper() if os.name == "nt" else root / ".",
    }
    alias = aliases[alias_kind]
    assert canonical_root(alias) == canonical_root(root)
    lease = reserve(owner, root)
    assert reserve(owner, alias) is lease
    error("duplicate_local_binding", lambda: reserve(owner, tmp_path / "other"),
          owner_root=canonical_root(root))


@pytest.fixture
def linked_root(root, tmp_path):
    """Real directory alias, without a privilege-dependent skipped symlink test.

    Windows junction creation needs no symlink privilege. Both paths are owned
    by tmp_path; rmdir removes only the junction, never its target recursively.
    """
    alias = tmp_path / "directory-alias"
    assert alias.parent == root.parent == tmp_path
    if os.name == "nt":
        result = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J",
             str(alias), str(root)],
            capture_output=True, text=True, check=False, timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        alias.symlink_to(root, target_is_directory=True)
    try:
        assert canonical_root(alias) == canonical_root(root)
        yield alias
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()
        assert root.is_dir()
        assert not alias.exists()


@pytest.mark.parametrize("alias_first", [False, True])
def test_filesystem_alias_is_not_a_second_consumer(owner, root, linked_root, tmp_path, alias_first):
    first, second = (linked_root, root) if alias_first else (root, linked_root)
    lease = reserve(owner, first)
    assert reserve(owner, second) is lease
    error("duplicate_local_binding", lambda: reserve(owner, tmp_path / "different-copy"),
          owner_root=canonical_root(root))
    assert owner.release(lease) is True
    replacement = reserve(owner, second)
    assert replacement is not lease
    assert reserve(owner, first) is replacement
    assert owner.release(lease) is False


def test_identical_copied_metadata_does_not_merge_roots(owner, root, tmp_path):
    data = root / ".voxarium_studio"
    data.mkdir()
    (data / "config.yaml").write_text(
        "project_name: Same name\nproject_identity:\n  id: " + IDENTITY + "\n  revision: 1\n",
        encoding="utf-8",
    )
    (data / "app.db").write_bytes(b"synthetic copied database identifiers")
    clone = tmp_path / "copied-project"
    shutil.copytree(root, clone)
    assert canonical_root(clone) != canonical_root(root)
    lease = reserve(owner, root)
    error("duplicate_local_binding", lambda: reserve(owner, clone), owner_root=canonical_root(root))
    assert reserve(owner, root) is lease
    assert owner.release(lease) is True
    clone_lease = reserve(owner, clone)
    error("duplicate_local_binding", lambda: reserve(owner, root), owner_root=canonical_root(clone))
    assert owner.release(lease) is False
    assert reserve(owner, clone) is clone_lease


@pytest.mark.parametrize(("url", "persistent_id"), [
    (URL, "other-project"),
    ("https://other.invalid/team", IDENTITY),
    ("https://reservations.invalid/other-prefix", IDENTITY),
    ("https://reservations.invalid/team/nested", IDENTITY),
    ("https://reservations.invalid/Team", IDENTITY),
    ("http://reservations.invalid/team", IDENTITY),
    ("https://reservations.invalid:8443/team", IDENTITY),
    # normalise_url deliberately preserves explicit ports, including defaults.
    ("https://reservations.invalid:443/team", IDENTITY),
])
def test_destination_tuple_keeps_independent_url_id_prefix_scheme_port(
    owner, root, tmp_path, url, persistent_id,
):
    assert (normalise_url(url), persistent_id) != (URL, IDENTITY)
    first = reserve(owner, root)
    second_root = tmp_path / "other"
    second = reserve(owner, second_root, url=url, persistent_id=persistent_id)
    assert first is not second
    assert reserve(owner, root) is first
    assert reserve(owner, second_root, url=url, persistent_id=persistent_id) is second
    assert owner.release(first) is True
    assert reserve(owner, second_root, url=url, persistent_id=persistent_id) is second


def test_same_root_can_own_independent_destinations(owner, root):
    first = reserve(owner, root)
    second = reserve(owner, root, persistent_id="fork")
    third = reserve(owner, root, url="https://other.invalid/team")
    assert len({id(first), id(second), id(third)}) == 3
    assert owner.release(second) is True
    assert reserve(owner, root) is first
    assert reserve(owner, root, url="https://other.invalid/team") is third


@pytest.mark.parametrize("iteration", range(5))
def test_concurrent_different_roots_have_exactly_one_winner(owner, tmp_path, iteration):
    count = 12
    roots = [tmp_path / f"root-{iteration}-{index}" for index in range(count)]
    barrier = Barrier(count)

    def attempt(index):
        barrier.wait(timeout=10)
        try:
            url = URL if index % 2 else "wss://RESERVATIONS.invalid/team/"
            return index, reserve(owner, roots[index], url=url)
        except ReservationError as failure:
            return index, failure

    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(attempt, index) for index in range(count)]
        results = [future.result(timeout=15) for future in futures]
    winners = [(index, value) for index, value in results if isinstance(value, ReservationLease)]
    assert len(winners) == 1
    winner_index, lease = winners[0]
    expected_root = canonical_root(roots[winner_index])
    losers = [(index, value) for index, value in results if isinstance(value, ReservationError)]
    assert len(losers) == count - 1
    for _, failure in losers:
        assert failure.code == "duplicate_local_binding"
        assert failure.args == ("duplicate_local_binding",)
        assert str(failure) == "duplicate_local_binding"
        assert failure.owner_root == expected_root
        assert isinstance(failure.owner_root, Path)
        assert failure.__cause__ is failure.__context__ is None
    assert reserve(owner, roots[winner_index]) is lease
    assert owner.release(lease) is True
    successor_root = roots[(winner_index + 1) % count]
    successor = reserve(owner, successor_root)
    assert successor is not lease
    assert owner.release(lease) is False
    assert reserve(owner, successor_root) is successor


def test_concurrent_same_root_is_idempotent(owner, root):
    count = 12
    barrier = Barrier(count)

    def attempt(index):
        barrier.wait(timeout=10)
        return reserve(owner, root if index % 2 else str(root),
                       url=URL if index % 2 else "wss://RESERVATIONS.invalid/team/")

    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(attempt, index) for index in range(count)]
        leases = [future.result(timeout=15) for future in futures]
    assert all(lease is leases[0] for lease in leases)
    assert owner.release(leases[0]) is True
    assert all(owner.release(lease) is False for lease in leases)


def test_concurrent_independent_destinations_all_succeed(owner, tmp_path):
    count = 12
    barrier = Barrier(count)

    def attempt(index):
        barrier.wait(timeout=10)
        return reserve(owner, tmp_path / f"root-{index}", persistent_id=f"project-{index}")

    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(attempt, index) for index in range(count)]
        leases = [future.result(timeout=15) for future in futures]
    assert len({id(lease) for lease in leases}) == count
    assert all(owner.release(lease) is True for lease in leases)


def test_concurrent_release_succeeds_only_once(owner, root, tmp_path):
    lease = reserve(owner, root)
    count = 12
    barrier = Barrier(count)

    def release():
        barrier.wait(timeout=10)
        return owner.release(lease)

    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(release) for _ in range(count)]
        results = [future.result(timeout=15) for future in futures]
    assert sum(result is True for result in results) == 1
    assert sum(result is False for result in results) == count - 1
    successor = reserve(owner, tmp_path / "successor")
    assert owner.release(lease) is False
    assert reserve(owner, tmp_path / "successor") is successor


@pytest.mark.parametrize("same_root", [False, True])
def test_stale_release_does_not_revoke_reacquired_destination(owner, root, tmp_path, same_root):
    old = reserve(owner, root)
    assert owner.release(old) is True
    assert owner.release(old) is False
    successor_root = root if same_root else tmp_path / "successor"
    successor = reserve(owner, successor_root)
    assert successor is not old
    assert owner.release(old) is False
    assert reserve(owner, successor_root) is successor
    error("duplicate_local_binding", lambda: reserve(owner, tmp_path / "contender"),
          owner_root=canonical_root(successor_root))
    assert owner.release(successor) is True


@pytest.mark.parametrize("fake", [None, "lease", 1, [], {}, object(), ReservationLease()])
def test_fake_or_unhashable_release_is_harmless(owner, root, tmp_path, fake):
    lease = reserve(owner, root)
    assert owner.release(fake) is False
    assert reserve(owner, root) is lease
    error("duplicate_local_binding", lambda: reserve(owner, tmp_path / "contender"),
          owner_root=canonical_root(root))


def test_copied_and_equality_spoofed_leases_are_not_capabilities(owner, root):
    lease = reserve(owner, root)

    class Impostor:
        def __eq__(self, _other):
            return True

        def __hash__(self):
            return hash(lease)

    for fake in (copy.copy(lease), copy.deepcopy(lease), Impostor()):
        assert fake is not lease
        assert owner.release(fake) is False
    assert reserve(owner, root) is lease
    assert owner.release(lease) is True


def test_independent_backend_owners_do_not_share_or_accept_leases(owner, root):
    foreign = ReservationOwner()
    local_lease = reserve(owner, root)
    foreign_lease = reserve(foreign, root)
    assert local_lease is not foreign_lease
    assert owner.release(foreign_lease) is False
    assert foreign.release(local_lease) is False
    assert reserve(owner, root) is local_lease
    assert reserve(foreign, root) is foreign_lease
    assert owner.release(local_lease) is True
    assert reserve(foreign, root) is foreign_lease
    assert foreign.release(foreign_lease) is True


def test_close_admission_is_idempotent_and_retains_live_leases(owner, root, tmp_path):
    lease = reserve(owner, root)
    other = reserve(owner, tmp_path / "other", persistent_id="other-id")
    owner.close_admission()
    owner.close_admission()
    for candidate, changes in (
        (root, {}),  # Even an idempotent reserve cannot admit after shutdown.
        (tmp_path / "contender", {}),
        (tmp_path / "new", {"persistent_id": "new-id"}),
    ):
        error("reservation_admission_closed", lambda: reserve(owner, candidate, **changes))
    assert owner.release(lease) is True  # Closing did not release it prematurely.
    assert owner.release(lease) is False
    assert owner.release(other) is True
    error("reservation_admission_closed", lambda: reserve(owner, root))


@pytest.mark.parametrize("changes", [
    {"url": "https://user:synthetic-secret@reservations.invalid"},
    {"persistent_id": "NOT-CANONICAL"},
    {"root": None},
])
def test_closed_admission_rejects_all_reserves_including_malformed(owner, root, changes):
    owner.close_admission()
    options = {"root": root, "url": URL, "persistent_id": IDENTITY, **changes}
    error("reservation_admission_closed", lambda: owner.reserve(**options))


def test_close_admission_fences_reserve_already_validating(owner, root, monkeypatch):
    entered = Event()
    proceed = Event()
    real_canonical_root = module.canonical_root

    def delayed(candidate):
        entered.set()
        assert proceed.wait(timeout=10)
        return real_canonical_root(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(module, "canonical_root", delayed)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reserve, owner, root)
            try:
                assert entered.wait(timeout=10)
                owner.close_admission()
            finally:
                proceed.set()
            error("reservation_admission_closed", lambda: future.result(timeout=15))


@pytest.mark.parametrize("url", [
    "", "  ", None, 1, b"https://reservations.invalid", [], {},
    "ftp://user:synthetic-secret@reservations.invalid/team",
    "https://user:synthetic-secret@reservations.invalid/team",
    "https://reservations.invalid/team?token=synthetic-secret",
    "https://reservations.invalid/team#synthetic-secret",
    "https://reservations.invalid:synthetic-secret/team",
    "https://reservations.invalid:65536/team",
    "https://[synthetic-secret/team",
    "https:///synthetic-secret",
])
def test_invalid_urls_are_sanitized_and_do_not_claim_destination(owner, root, tmp_path, url):
    value = error("invalid_reservation", lambda: reserve(owner, root, url=url))
    assert "synthetic-secret" not in repr(value)
    lease = reserve(owner, tmp_path / "valid")
    assert owner.release(lease) is True


@pytest.mark.parametrize("persistent_id", [
    "", "UPPER", " padded", "padded ", "newline\n", ".", "-id", "a/b", "a\\b",
    "a%2fb", "синтетика", "x" * 65, None, 1, True, b"id", [], {},
])
def test_identity_is_strict_and_never_silently_normalized(owner, root, tmp_path, persistent_id):
    error("invalid_reservation", lambda: reserve(owner, root, persistent_id=persistent_id))
    assert owner.release(reserve(owner, tmp_path / "valid")) is True


@pytest.mark.parametrize("persistent_id", ["a", "0", "a._-09", "x" * 64])
def test_full_canonical_identity_domain_is_accepted(owner, root, persistent_id):
    lease = reserve(owner, root, persistent_id=persistent_id)
    assert reserve(owner, root, persistent_id=persistent_id) is lease
    assert owner.release(lease) is True


@pytest.mark.parametrize("bad_root", [None, "", "root\0synthetic-secret", 1, True, [], {}, b"root"])
def test_malformed_roots_are_sanitized(owner, root, bad_root):
    error("invalid_reservation", lambda: reserve(owner, bad_root))
    assert owner.release(reserve(owner, root)) is True


@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError, TypeError])
def test_canonical_root_failures_drop_raw_error_and_context(owner, root, monkeypatch, failure):
    def fail(_root):
        raise failure("synthetic-secret / private root / credential")

    with monkeypatch.context() as patch:
        patch.setattr(module, "canonical_root", fail)
        value = error("invalid_reservation", lambda: reserve(owner, root))
    assert "synthetic-secret" not in repr(value)
    assert owner.release(reserve(owner, root)) is True


def test_memory_only_lifecycle_never_reads_or_writes_storage(root, tmp_path, monkeypatch):
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("storage")
        raise AssertionError("Reservations must be memory-only")

    nonexistent = tmp_path / "not-initialized-or-opened"
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(os, "mkdir", forbidden)
        patch.setattr(os, "replace", forbidden)
        patch.setattr(os, "rename", forbidden)
        patch.setattr(os, "unlink", forbidden)
        local = ReservationOwner()
        lease = reserve(local, nonexistent)
        assert reserve(local, nonexistent) is lease
        error("duplicate_local_binding", lambda: reserve(local, root),
              owner_root=canonical_root(nonexistent))
        assert local.release(lease) is True
        next_lease = reserve(local, root)
        local.close_admission()
        assert local.release(next_lease) is True
    assert calls == []
    assert not nonexistent.exists()
    assert list(root.iterdir()) == []
