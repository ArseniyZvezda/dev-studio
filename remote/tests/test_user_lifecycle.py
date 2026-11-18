from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from voxarium_studio_remote.store import (
    Store, CredentialInvalid, InvalidRequest, MetadataConflict, UserDeleted, UserExists, UnknownUser,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def store():
    value = Store(":memory:")
    yield value
    value.close()


def test_uuid_crud_duplicate_display_names_hash_only_and_public_dto(store):
    proposed = uuid4().hex
    a = store.create_user(" Анна ", digest("secret-a"), user_id=proposed, color="#ABCDEF")
    b = store.create_user("Анна", digest("secret-b"))
    assert a.id == proposed and a.name == proposed and b.id != a.id
    assert a.display_name == b.display_name == "Анна" and a.color == "#abcdef"
    assert a.to_json() == {"id": a.id, "display_name": "Анна", "color": "#abcdef"}
    assert a.to_admin_json()["status"] == "active"
    assert "token_hash" not in repr(a) and "secret" not in json.dumps(a.to_admin_json())
    columns = {r[1] for r in store._db.execute("PRAGMA table_info(users)")}
    assert "token_hash" in columns and not columns & {"token", "key", "issued_key", "encrypted_key"}
    assert store.user_by_token_hash(digest("secret-a")) == a
    assert store.user_by_token_hash("secret-a") is None
    with pytest.raises(UserExists):
        store.create_user("Other", digest("new"), user_id=a.id)
    with pytest.raises(UserExists):
        store.create_user("Other", digest("secret-a"))
    assert store.user(a.id) == a


def test_metadata_is_not_credential_and_noop_does_not_bump(store):
    user = store.create_user("Old", digest("key"))
    store.create_project("p", "Name")
    doc = store.put("p", "a.md", "Content", user.id)
    changed = store.update_user(user.id, display_name="Новое", color="#123ABC", expected_revision=1)
    assert changed.metadata_revision == 2 and changed.credential_version == 1
    assert store.credential_valid(user.id, 1)
    assert store.user_by_token_hash(digest("key")) == changed
    assert store.doc("p", "a.md") == doc
    assert store.update_user(user.id, display_name="Новое", expected_revision=2) == changed
    with pytest.raises(MetadataConflict):
        store.update_user(user.id, display_name="Stale", expected_revision=1)


def test_revoke_rotate_delete_generations_tombstone_and_last_user(store):
    user = store.create_user("Old", digest("old"))
    store.create_project("p", "Name")
    old_doc = store.put("p", "a.md", "keep", user.id)
    revoked = store.revoke_user_by_id(user.id, 1, 1)
    assert revoked.status == "revoked" and revoked.credential_version == 2 and revoked.metadata_revision == 2
    assert not store.is_active(user.id) and not store.credential_valid(user.id, 1)
    assert store.user_by_token_hash(digest("old")) is None
    assert store._db.execute("SELECT token_hash FROM users WHERE id=?", (user.id,)).fetchone()[0] is None
    # Retrying an already completed revoke has no mutation or generation bump.
    assert store.revoke_user_by_id(user.id, 1, 1) == revoked
    restored = store.rotate_user(user.id, digest("new"), 2, 2)
    assert restored.status == "active" and restored.credential_version == 3
    assert store.credential_valid(user.id, 3) and not store.credential_valid(user.id, 1)
    assert not store.credential_valid(user.id, 2)
    # An old revoke must not invalidate the newly restored key.
    with pytest.raises(MetadataConflict):
        store.revoke_user_by_id(user.id, 1, 1)
    deleted = store.delete_user(user.id, 3, 3)
    assert deleted.status == "deleted" and deleted.credential_version == 4
    assert deleted.revoked_at and deleted.deleted_at
    assert store.delete_user(user.id, 3, 3) == deleted
    assert store.revoke_user_by_id(user.id, 1, 1) == deleted
    assert store.list_users() == []
    assert store.list_users(include_deleted=True) == [deleted]
    assert store.doc("p", "a.md") == old_doc
    with pytest.raises(UserDeleted):
        store.rotate_user(user.id, digest("not-restored"), 4, 4)
    with pytest.raises(UserDeleted):
        store.update_user(user.id, display_name="No", expected_revision=4)
    new = store.create_user("Old", digest("another-human"))
    assert new.id != user.id and new.status == "active"


def test_rotate_active_invalidates_open_generation_and_duplicate_hash_rolls_back(store):
    a = store.create_user("A", digest("a"))
    b = store.create_user("B", digest("b"))
    with pytest.raises(UserExists):
        store.rotate_user(a.id, digest("b"), 1, 1)
    assert store.user(a.id) == a and store.user(b.id) == b
    rotated = store.rotate_user(a.id, digest("c"), 1, 1)
    assert rotated.credential_version == 2 and rotated.metadata_revision == 2
    assert not store.credential_valid(a.id, 1)
    with pytest.raises(CredentialInvalid):
        with store.credential_guard(a.id, 1):
            pytest.fail("old generation must not enter the handler")
    assert store.credential_valid(a.id, 2)


@pytest.mark.parametrize("params", [
    {"display_name": "bad\nname"}, {"color": "red"},
    {"display_name": "x" * 121}, {"display_name": "\u202eevil"},
])
def test_new_metadata_validation_does_not_mutate(store, params):
    user = store.create_user("Good", digest("key"))
    with pytest.raises(ValueError):
        store.update_user(user.id, expected_revision=1, **params)
    assert store.user(user.id) == user


@pytest.mark.parametrize("revision,version", [(True, 1), (1, True), (0, 1), (1, 0), ("1", 1), (1, None)])
def test_bad_guards_never_mutate(store, revision, version):
    user = store.create_user("Good", digest("key"))
    for operation in (lambda: store.rotate_user(user.id, digest("new"), revision, version),
                      lambda: store.revoke_user_by_id(user.id, revision, version),
                      lambda: store.delete_user(user.id, revision, version)):
        with pytest.raises(InvalidRequest):
            operation()
        assert store.user(user.id) == user


def test_legacy_cli_handles_survive_metadata_edits(store):
    user = store.add_user("legacy-name", digest("old"))
    with pytest.raises(UserExists):
        store.add_user("legacy-name", digest("different"))
    store.update_user(user.id, display_name="New label", expected_revision=1)
    revoked = store.revoke_user("legacy-name")
    assert revoked.name == "legacy-name" and revoked.display_name == "New label"
    assert store.revoke_user("legacy-name") == revoked
    restored = store.set_token_hash("legacy-name", digest("new"))
    assert restored.credential_version == 3 and restored.id == user.id
    with pytest.raises(UnknownUser):
        store.revoke_user("New label")


def test_guard_wraps_nested_write_and_rolls_back_as_a_unit(store):
    user = store.create_user("User", digest("key"))
    store.create_project("p", "Project")
    with pytest.raises(RuntimeError):
        with store.credential_guard(user.id, 1):
            store.put("p", "a.md", "not committed", user.id)
            raise RuntimeError("handler failed")
    assert store.doc("p", "a.md") is None and store.seq("p") == 0
    with store.credential_guard(user.id, 1):
        store.put("p", "a.md", "committed", user.id)
    assert store.seq("p") == 1


def test_separate_connections_guard_serializes_whole_handler_with_rotate(tmp_path):
    path = tmp_path / "guard.db"
    a, b = Store(path), Store(path)
    try:
        user = a.create_user("User", digest("old"))
        a.create_project("p", "Project")
        attempted, completed = threading.Event(), threading.Event()

        def rotate():
            attempted.set()
            result = b.rotate_user(user.id, digest("new"), 1, 1)
            completed.set()
            return result

        with ThreadPoolExecutor(max_workers=1) as pool:
            with a.credential_guard(user.id, 1):
                future = pool.submit(rotate)
                assert attempted.wait(2)
                assert not completed.wait(0.1), "rotate cannot commit in a guarded synchronous frame"
                a.put("p", "a.md", "frame before rotate", user.id)
            rotated = future.result(timeout=3)
        assert rotated.credential_version == 2 and completed.is_set()
        old = a.doc("p", "a.md")
        with pytest.raises(CredentialInvalid):
            with a.credential_guard(user.id, 1):
                a.put("p", "a.md", "forbidden after rotate", user.id)
        assert a.doc("p", "a.md") == old
        # Revoke -> restore between ticks cannot resurrect either older binding.
        revoked = b.revoke_user_by_id(user.id, 2, 2)
        restored = b.rotate_user(user.id, digest("restored"), revoked.metadata_revision, revoked.credential_version)
        assert restored.credential_version == 4
        assert not a.credential_valid(user.id, 1) and not a.credential_valid(user.id, 2)
        assert a.credential_valid(user.id, 4)
    finally:
        a.close()
        b.close()


def test_two_connections_cas_user_updates_have_one_winner(tmp_path):
    path = tmp_path / "user-race.db"
    a, b = Store(path), Store(path)
    try:
        user = a.create_user("User", digest("key"))
        barrier = threading.Barrier(2)

        def worker(store):
            barrier.wait()
            try:
                store.update_user(user.id, display_name=threading.current_thread().name, expected_revision=1)
                return "ok"
            except MetadataConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(worker, (a, b))) == ["conflict", "ok"]
        assert a.user(user.id).metadata_revision == 2
        assert a.user(user.id).credential_version == 1
    finally:
        a.close()
        b.close()


def test_separate_connections_user_creation_has_exactly_one_winner(tmp_path):
    path = tmp_path / "user-create-race.db"
    a, b = Store(path), Store(path)
    proposed = uuid4().hex
    barrier = threading.Barrier(2)
    try:
        def worker(store):
            barrier.wait()
            try:
                store.create_user("Same label", digest(threading.current_thread().name), user_id=proposed)
                return "ok"
            except UserExists:
                return "exists"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(worker, (a, b))) == ["exists", "ok"]
        assert len(a.list_users()) == 1 and a.user(proposed).credential_version == 1
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("proposed", ["0" * 32, "BAD", " abc ", True, 1])
def test_create_requires_canonical_v4_uuid_without_rewriting(store, proposed):
    with pytest.raises(InvalidRequest):
        store.create_user("Good", digest("key"), user_id=proposed)
    assert store.list_users() == []


# ------------------------------------------------------------------ R-A0 (VS-191)
# Credential generation changes notify AI control mirrors *inside* the credential
# transaction (a failing hook means no new key) and close stale sockets after commit.


def _record(store):
    hooks, listeners = [], []
    store.add_credential_hook(lambda user_id, version: hooks.append((user_id, version)))
    store.add_credential_listener(lambda user_id, version: listeners.append((user_id, version)))
    return hooks, listeners


def test_every_generation_change_runs_hook_then_listener_once(store):
    hooks, listeners = _record(store)
    user = store.create_user("User", digest("a"))
    store.update_user(user.id, display_name="Renamed", expected_revision=1)
    assert hooks == listeners == []  # metadata is not a credential change
    store.rotate_user(user.id, digest("b"), 2, 1)
    store.revoke_user_by_id(user.id, 3, 2)
    store.revoke_user_by_id(user.id, 3, 2)  # completed retry: no bump, no notice
    store.rotate_user(user.id, digest("c"), 4, 3)  # reactivate
    store.delete_user(user.id, 5, 4)
    expected = [(user.id, 2), (user.id, 3), (user.id, 4), (user.id, 5)]
    assert hooks == expected and listeners == expected
    assert [version for _, version in hooks] == sorted({version for _, version in hooks})


def test_failing_hook_rolls_back_the_credential_change(store):
    user = store.create_user("User", digest("old"))
    listeners = []
    store.add_credential_listener(lambda *args: listeners.append(args))

    def refuse(_user_id, _version):
        raise RuntimeError("AI mirror invalidation failed")

    store.add_credential_hook(refuse)
    for change in (lambda: store.rotate_user(user.id, digest("new"), 1, 1),
                   lambda: store.revoke_user_by_id(user.id, 1, 1),
                   lambda: store.delete_user(user.id, 1, 1)):
        with pytest.raises(RuntimeError):
            change()
        assert store.user(user.id) == user and store.credential_valid(user.id, 1)
        assert store.user_by_token_hash(digest("old")) == user
        assert store.user_by_token_hash(digest("new")) is None
    assert listeners == []


def test_listener_runs_only_after_outer_commit_and_not_on_rollback(store):
    user = store.create_user("User", digest("old"))
    other = store.create_user("Other", digest("taken"))
    seen = []
    store.add_credential_listener(lambda user_id, version: seen.append(
        (user_id, version, store.credential_valid(user_id, version))))
    # Duplicate credential: the update fails before any notice is queued.
    with pytest.raises(UserExists):
        store.rotate_user(user.id, digest("taken"), 1, 1)
    # A notice queued inside an outer transaction that later fails is dropped.
    with pytest.raises(RuntimeError):
        with store._tx():
            store.rotate_user(user.id, digest("inner"), 1, 1)
            raise RuntimeError("outer failure")
    assert seen == [] and store.credential_valid(user.id, 1)
    # Legacy nested CLI path: one notice, delivered after the outer commit is visible.
    store.add_user("legacy", digest("legacy-old"))
    legacy = store._legacy_user("legacy")
    store.set_token_hash("legacy", digest("legacy-new"))
    assert seen == [(legacy.id, 2, True)]
    assert store.user(other.id).credential_version == 1


def test_listener_failure_never_undoes_a_committed_rotation(store):
    user = store.create_user("User", digest("old"))
    store.add_credential_listener(lambda *_: (_ for _ in ()).throw(RuntimeError("socket gone")))
    rotated = store.rotate_user(user.id, digest("new"), 1, 1)
    assert rotated.credential_version == 2 and store.credential_valid(user.id, 2)


def test_file_store_without_ai_registry_rotates_unchanged(tmp_path):
    store = Store(tmp_path / "remote.db")
    try:
        user = store.create_user("User", digest("old"))
        assert store.rotate_user(user.id, digest("new"), 1, 1).credential_version == 2
        assert not (tmp_path / "ai-control.db").exists()
    finally:
        store.close()


def test_hub_closes_only_old_generation_sockets(store):
    import asyncio
    from voxarium_studio_remote.auth import User
    from voxarium_studio_remote.hub import Hub

    async def scenario():
        alice = store.create_user("Alice", digest("a"))
        bob = store.create_user("Bob", digest("b"))
        store.create_project("p", "Project")
        hub = Hub(store)
        old = hub.connect(User.from_row(alice), "p")
        other = hub.connect(User.from_row(bob), "p")
        rotated = store.rotate_user(alice.id, digest("a2"), 1, 1)
        fresh = hub.connect(User.from_row(rotated), "p")
        assert hub.close_user(alice.id, rotated.credential_version) == 1
        assert old.closed and not other.closed and not fresh.closed
        # The writer wakes on the sentinel instead of waiting for the watcher; nothing
        # queued for the old generation is released.
        assert old.queue.get_nowait() is None and old.queue.empty()
        assert hub.close_user(alice.id, rotated.credential_version) == 0
        with pytest.raises(CredentialInvalid):
            hub.handle(old, '{"t":"sync","since":0}')

    asyncio.run(scenario())
