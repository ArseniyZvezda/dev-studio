"""AI control registry (ai-access design §3, §4, §6, §8, §9; R-A0/R-A1).

The registry is its own SQLite file (``ai-control.db``) so the Documents schema never
changes when ACP is off; every mirror is CAS by revision + semantic hash, and every
tombstone (revoke, unpublish, credential change) is terminal.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager

import pytest

from voxarium_studio_remote import ai_protocol as p
from voxarium_studio_remote.ai_auth import hash_device_token, new_device_token
from voxarium_studio_remote.ai_store import (
    APPLICATION_ID, SCHEMA_VERSION, AiSchemaError, AiStore, CapacityExceeded, NotFound,
    RegistrationExists,
)
from voxarium_studio_remote import ai_store as ai_store_module
from voxarium_studio_remote.auth import hash_token
from voxarium_studio_remote.store import AI_CONTROL_DB, Store

NOW = 1_800_000_000_000
DAY = 24 * 3600 * 1000
POLICY = "a" * 64


def uid() -> str:
    return str(uuid.uuid4())


def user_id() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def ai():
    value = AiStore(":memory:")
    yield value
    value.close()


def register(ai: AiStore, owner: str, *, version: int = 1, client_id: str | None = None,
             kind: str = "studio", token: str | None = None):
    token = token or new_device_token()
    return ai.register(owner, version, client_id or uid(), "Device", kind, hash_device_token(token), NOW)


def export_body(export_id: str, *, persistent_id: str = "game", storage_epoch: str = "epoch-1",
                revision: int = 1, expected: int | None = None, display_name: str = "Game",
                policy: str = POLICY) -> dict:
    body = {"export_id": export_id, "persistent_id": persistent_id, "storage_epoch": storage_epoch,
            "revision": revision, "expected_revision": revision - 1 if expected is None else expected,
            "policy_digest": policy, "display_name": display_name}
    body["hash"] = p.semantic_hash(p.export_record(body))
    return body


def unpublish_body(export_id: str, revision: int, expected: int | None = None) -> dict:
    return {"export_id": export_id, "revision": revision,
            "expected_revision": revision - 1 if expected is None else expected,
            "hash": p.semantic_hash(p.unpublish_record(export_id, revision))}


def target_of(ai: AiStore, export) -> dict:
    return {"server_instance_id": ai.server_instance_id,
            "executor_registration_id": export.executor_registration_id,
            "export_id": export.export_id, "persistent_id": export.persistent_id,
            "storage_epoch": export.storage_epoch, "export_revision": export.revision}


def grant_value(ai: AiStore, export, grantee, *, grant_id: str | None = None, revision: int = 1,
                operations=("view_status",), team_ids=("team-a",), member_ids=(),
                scope: str = "created_by_this_grantee", resource_ids=(), issued_at: int = NOW,
                expires_at: int = NOW + DAY, target: dict | None = None,
                grantee_user_id: str | None = None) -> dict:
    return {"grant_id": grant_id or uid(), "revision": revision,
            "target": target or target_of(ai, export),
            "grantee_user_id": grantee_user_id or grantee.user_id,
            "grantee_registration_id": grantee.id, "operations": list(operations),
            "team_ids": list(team_ids), "member_ids": list(member_ids), "resource_scope": scope,
            "resource_ids": list(resource_ids), "execution_policy_digest": "b" * 64,
            "issued_at": issued_at, "expires_at": expires_at, "revoked_at": None}


def put_body(export, grant: dict, *, expected: int | None = None) -> dict:
    revision = grant["revision"]
    return {"export_id": export.export_id, "export_revision": export.revision,
            "grant_id": grant["grant_id"], "revision": revision,
            "expected_revision": revision - 1 if expected is None else expected, "grant": grant,
            "hash": p.semantic_hash(p.grant_record(export.export_id, export.revision, grant))}


def revoke_body(export, grant_id: str, revision: int, *, expected: int | None = None,
                export_revision: int | None = None) -> dict:
    export_revision = export.revision if export_revision is None else export_revision
    return {"export_id": export.export_id, "export_revision": export_revision, "grant_id": grant_id,
            "revision": revision, "expected_revision": revision - 1 if expected is None else expected,
            "hash": p.semantic_hash(p.revoke_record(export.export_id, export_revision, grant_id, revision))}


def row(resource_id: str, *, team: str = "team-a", member: str | None = None, origin=None,
        revision: int = 1, kind: str = "task") -> dict:
    return {"resource_id": resource_id, "kind": kind, "team_id": team, "member_id": member,
            "remote_origin": origin, "revision": revision}


def put_index(ai: AiStore, export, rows: list[dict], index_revision: int = 1) -> dict:
    return ai.replace_index(export.executor_registration_id, export.export_id, export.revision,
                            index_revision, rows, p.index_rows_hash(rows))


@pytest.fixture
def world(ai):
    """alice publishes one export; bob holds one registration to grant to."""
    alice, bob = user_id(), user_id()
    executor = register(ai, alice)
    grantee = register(ai, bob, kind="android")
    export = ai.publish(executor.id, export_body(uid()), NOW)
    return {"ai": ai, "alice": alice, "bob": bob, "executor": executor, "grantee": grantee,
            "export": export}


@contextmanager
def rejected(code: str):
    with pytest.raises(p.RequestRejected) as info:
        yield info
    assert info.value.code == code


# ------------------------------------------------------------------ file and schema

def _pragmas(path) -> tuple[int, int, list]:
    db = sqlite3.connect(str(path))
    try:
        return (db.execute("PRAGMA application_id").fetchone()[0],
                db.execute("PRAGMA user_version").fetchone()[0],
                sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master")))
    finally:
        db.close()


def test_fresh_file_bootstrap_and_stable_server_instance_id(tmp_path):
    path = tmp_path / AI_CONTROL_DB
    first = AiStore(path)
    sid = first.server_instance_id
    first.close()
    assert p.check_uuid4(sid) == sid
    application, version, names = _pragmas(path)
    assert (application, version) == (APPLICATION_ID, SCHEMA_VERSION)
    assert {"ai_registrations", "ai_exports", "ai_grants", "ai_audit"} <= set(names)
    again = AiStore(path)
    try:
        assert again.server_instance_id == sid
    finally:
        again.close()
    other = AiStore(tmp_path / "other" / AI_CONTROL_DB)
    try:
        assert other.server_instance_id != sid  # a reset/replaced registry is a new instance
    finally:
        other.close()


def test_documents_database_is_refused_and_not_modified(tmp_path):
    documents = tmp_path / "remote.db"
    Store(documents).close()
    before = documents.read_bytes()
    with pytest.raises(AiSchemaError):
        AiStore(documents)
    assert documents.read_bytes() == before
    application, _, names = _pragmas(documents)
    assert application != APPLICATION_ID and not [n for n in names if n.startswith("ai_")]


@pytest.mark.parametrize("tamper", [
    "CREATE TABLE extra (x INTEGER)",
    "DROP INDEX ai_registrations_active",
    "PRAGMA user_version=2",
    "PRAGMA application_id=7",
])
def test_tampered_ai_schema_is_refused_and_not_modified(tmp_path, tamper):
    path = tmp_path / AI_CONTROL_DB
    AiStore(path).close()
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute(tamper)
    db.commit()
    db.close()
    before = path.read_bytes()
    with pytest.raises(AiSchemaError):
        AiStore(path)
    assert path.read_bytes() == before


def test_foreign_non_empty_sqlite_file_is_refused(tmp_path):
    path = tmp_path / AI_CONTROL_DB
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE notes (body TEXT)")
    db.commit()
    db.close()
    before = path.read_bytes()
    with pytest.raises(AiSchemaError):
        AiStore(path)
    assert path.read_bytes() == before


def test_enabling_ai_leaves_documents_schema_untouched(tmp_path):
    store = Store(tmp_path / "remote.db")
    ai = AiStore(tmp_path / AI_CONTROL_DB)
    owner = store.create_user("Анна", hash_token("vxs_a"))
    register(ai, owner.id)
    ai.close()
    store.close()
    _, _, names = _pragmas(tmp_path / "remote.db")
    assert not [n for n in names if n.startswith("ai_")]
    Store(tmp_path / "remote.db").close()  # the strict Documents signature still opens


# ------------------------------------------------------------------ registrations

def test_registration_is_hash_only(ai):
    token = new_device_token()
    reg = register(ai, user_id(), token=token)
    assert reg.token_hash == hash_device_token(token) and reg.active
    dump = "\n".join(ai._db.iterdump())
    assert token not in dump and token.removeprefix("vxd_") not in dump
    assert hash_device_token(token) not in repr(reg) and reg.token_hash not in str(reg.to_json())
    columns = {r[1] for r in ai._db.execute("PRAGMA table_info(ai_registrations)")}
    assert "token_hash" in columns and not columns & {"token", "secret", "device_token"}


def test_one_active_registration_per_user_and_client(ai):
    owner, client = user_id(), uid()
    first = register(ai, owner, client_id=client)
    with pytest.raises(RegistrationExists) as info:
        register(ai, owner, client_id=client, kind="android")
    assert info.value.registration_id == first.id
    assert ai.registration(first.id) == first  # token and kind unchanged
    other_user = register(ai, user_id(), client_id=client)
    assert other_user.id != first.id and other_user.active


def test_registration_capacity_per_user(ai):
    owner = user_id()
    for _ in range(p.MAX_REGISTRATIONS_PER_USER):
        register(ai, owner)
    with pytest.raises(CapacityExceeded):
        register(ai, owner)
    assert register(ai, user_id()).active  # another user is unaffected


def test_revoke_registration_owner_only_and_new_id_after_revoke(ai):
    owner, client = user_id(), uid()
    reg = register(ai, owner, client_id=client)
    with pytest.raises(NotFound):
        ai.revoke_registration(user_id(), reg.id, NOW)
    with pytest.raises(NotFound):
        ai.revoke_registration(owner, uid(), NOW)
    assert ai.registration(reg.id).active
    revoked = ai.revoke_registration(owner, reg.id, NOW + 1)
    assert not revoked.active and revoked.token_hash is None and revoked.revoked_at == NOW + 1
    assert ai.revoke_registration(owner, reg.id, NOW + 2).revoked_at == NOW + 1  # idempotent
    again = register(ai, owner, client_id=client)
    assert again.id != reg.id and again.active
    assert ai.registration_by_client(owner, client).id == again.id


@pytest.mark.parametrize("change", [
    {"client_id": str(uuid.uuid1())}, {"client_id": str(uuid.uuid4()).upper()},
    {"kind": "browser"}, {"display_name": " padded "}, {"token_hash": "not-a-hash"},
])
def test_register_validates_fields(ai, change):
    args = {"client_id": uid(), "display_name": "Device", "kind": "studio",
            "token_hash": hash_device_token(new_device_token())}
    args.update(change)
    with pytest.raises(p.AcpError):
        ai.register(user_id(), 1, args["client_id"], args["display_name"], args["kind"],
                    args["token_hash"], NOW)


# ------------------------------------------------------------------ credential generation

def _cross_world(ai):
    alice, bob = user_id(), user_id()
    ra, rb = register(ai, alice), register(ai, bob)
    ea = ai.publish(ra.id, export_body(uid(), persistent_id="alpha"), NOW)
    eb = ai.publish(rb.id, export_body(uid(), persistent_id="beta"), NOW)
    to_bob = grant_value(ai, ea, rb)
    to_alice = grant_value(ai, eb, ra)
    ai.put_grant(ra.id, put_body(ea, to_bob), NOW, rb)
    ai.put_grant(rb.id, put_body(eb, to_alice), NOW, ra)
    put_index(ai, ea, [row("t1")])
    return alice, bob, ra, rb, ea, eb, to_bob["grant_id"], to_alice["grant_id"]


def test_invalidate_user_tombstones_and_cascades(ai):
    alice, bob, ra, rb, ea, eb, g_on_export, g_as_grantee = _cross_world(ai)
    assert ai.invalidate_user(alice, 1, NOW) == []  # same generation: nothing older
    assert ai.invalidate_user(alice, 2, NOW + 5) == [ra.id]
    dead = ai.registration(ra.id)
    assert dead.token_hash is None and dead.revoked_at == NOW + 5 and not dead.active
    assert ai.grant(g_on_export).state == "revoked"
    assert ai.grant(g_as_grantee).state == "revoked"
    assert ai.export(ea.export_id).state == "deleted"
    assert ai.index_row(ea.export_id, ea.revision, "t1") is None
    # bob is untouched except for the grant alice held
    assert ai.registration(rb.id).active and ai.export(eb.export_id).state == "published"
    assert ai.invalidate_user(alice, 3, NOW + 9) == []  # tombstones are not re-stamped
    assert ai.registration(ra.id).revoked_at == NOW + 5


def test_reregistration_never_resurrects_grants_or_exports(ai):
    alice, bob, ra, rb, ea, eb, g_on_export, g_as_grantee = _cross_world(ai)
    ai.invalidate_user(alice, 2, NOW)
    fresh = register(ai, alice, version=2, client_id=ra.client_id)
    assert fresh.id != ra.id
    assert ai.grant(g_on_export).state == ai.grant(g_as_grantee).state == "revoked"
    assert ai.grants_for(fresh.id) == []
    assert ai.export(ea.export_id).state == "deleted"
    with pytest.raises(NotFound):  # the old export_id belongs to the dead registration
        ai.publish(fresh.id, export_body(ea.export_id, persistent_id="alpha", revision=2), NOW)
    with rejected("revision_conflict"):  # and the dead one cannot republish it either
        ai.publish(ra.id, export_body(ea.export_id, persistent_id="alpha", revision=2), NOW)


def test_register_tombstones_stale_generation_registrations(ai):
    owner = user_id()
    old = register(ai, owner, version=1)
    other = register(ai, owner, version=1)
    new = register(ai, owner, version=2)
    assert not ai.registration(old.id).active and not ai.registration(other.id).active
    assert ai.registration(old.id).revoke_reason == "credential_changed"
    assert [r.id for r in ai.list_registrations() if r.user_id == owner] == [new.id]


# ------------------------------------------------------------------ export publish

def test_publish_create_and_identical_replay(world):
    ai, executor, export = world["ai"], world["executor"], world["export"]
    assert (export.revision, export.state) == (1, "published")
    body = export_body(export.export_id)
    assert ai.publish(executor.id, body, NOW + 1).ack() == export.ack()
    assert ai.export(export.export_id).updated_at == NOW  # replay wrote nothing
    with rejected("revision_conflict"):
        ai.publish(executor.id, export_body(uid(), persistent_id="x", revision=2, expected=1), NOW)
    with rejected("revision_conflict"):
        ai.publish(executor.id, export_body(uid(), persistent_id="y", revision=1, expected=1), NOW)


@pytest.mark.parametrize("revision,expected", [(1, 0), (1, 1), (3, 2), (3, 1), (2, 0), (2, 2)])
def test_publish_cas_conflicts(world, revision, expected):
    ai, executor, export = world["ai"], world["executor"], world["export"]
    with rejected("revision_conflict") as info:
        ai.publish(executor.id, export_body(export.export_id, revision=revision, expected=expected,
                                            display_name="Renamed"), NOW)
    assert info.value.result == export.ack()
    assert ai.export(export.export_id) == export


def test_publish_lower_revision_after_update_conflicts(world):
    ai, executor, export = world["ai"], world["executor"], world["export"]
    ai.publish(executor.id, export_body(export.export_id, revision=2, display_name="Two"), NOW)
    with rejected("revision_conflict"):
        ai.publish(executor.id, export_body(export.export_id, revision=1, display_name="Old"), NOW)


def test_publish_wrong_hash_is_protocol_error(world):
    body = export_body(uid())
    body["display_name"] = "Changed after hashing"
    with pytest.raises(p.AcpError):
        world["ai"].publish(world["executor"].id, body, NOW)
    body = export_body(uid())
    body["unexpected"] = 1
    with pytest.raises(p.AcpError):
        world["ai"].publish(world["executor"].id, body, NOW)


def test_foreign_executor_sees_not_found_and_cannot_overwrite(world):
    ai, export = world["ai"], world["export"]
    stranger = register(ai, user_id())
    with pytest.raises(NotFound):
        ai.publish(stranger.id, export_body(export.export_id), NOW)
    with pytest.raises(NotFound):
        ai.publish(stranger.id, export_body(export.export_id, revision=2, display_name="Mine"), NOW)
    with pytest.raises(NotFound):
        ai.unpublish(stranger.id, unpublish_body(export.export_id, 2), NOW)
    assert ai.export(export.export_id) == export


def test_duplicate_export_for_same_persistent_id(world):
    ai, executor = world["ai"], world["executor"]
    with rejected("duplicate_export"):
        ai.publish(executor.id, export_body(uid()), NOW)
    # another executor may publish the same project: two PCs are two executors
    assert ai.publish(world["grantee"].id, export_body(uid()), NOW).state == "published"


def test_export_capacity_per_executor(ai):
    executor = register(ai, user_id())
    for n in range(p.MAX_EXPORTS_PER_EXECUTOR):
        ai.publish(executor.id, export_body(uid(), persistent_id=f"p{n}"), NOW)
    with rejected("capacity_exceeded"):
        ai.publish(executor.id, export_body(uid(), persistent_id="one-more"), NOW)


def test_revision_bump_revokes_older_grants_and_drops_index(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    grant = grant_value(ai, export, grantee)
    ai.put_grant(executor.id, put_body(export, grant), NOW, grantee)
    put_index(ai, export, [row("t1")])
    bumped = ai.publish(executor.id, export_body(export.export_id, revision=2, display_name="Two"), NOW + 1)
    assert bumped.revision == 2 and bumped.display_name == "Two"
    stored = ai.grant(grant["grant_id"])
    assert stored.state == "revoked" and stored.revoke_reason == "export_revision"
    assert ai.index_row(export.export_id, 1, "t1") is None
    assert ai.index_row(export.export_id, 2, "t1") is None
    # the old grant cannot be put again at the new revision: revoked is terminal
    regrant = dict(grant, revision=2, target=target_of(ai, bumped))
    with rejected("revision_conflict"):
        ai.put_grant(executor.id, put_body(bumped, regrant), NOW, grantee)


@pytest.mark.parametrize("change", [{"persistent_id": "moved"}, {"storage_epoch": "epoch-2"}])
def test_identity_change_on_update_is_stale_target(world, change):
    ai, executor, export = world["ai"], world["executor"], world["export"]
    with rejected("stale_target"):
        ai.publish(executor.id, export_body(export.export_id, revision=2, **change), NOW)
    assert ai.export(export.export_id) == export


# ------------------------------------------------------------------ unpublish

def test_unpublish_tombstone_and_republish(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    grant = grant_value(ai, export, grantee)
    ai.put_grant(executor.id, put_body(export, grant), NOW, grantee)
    put_index(ai, export, [row("t1")])
    with rejected("revision_conflict"):
        ai.unpublish(executor.id, unpublish_body(export.export_id, 3, expected=2), NOW)
    gone = ai.unpublish(executor.id, unpublish_body(export.export_id, 2), NOW + 1)
    assert (gone.state, gone.revision) == ("unpublished", 2)
    assert ai.grant(grant["grant_id"]).state == "revoked"
    assert ai.index_row(export.export_id, 1, "t1") is None
    assert ai.unpublish(executor.id, unpublish_body(export.export_id, 2), NOW + 2) == gone
    with rejected("revision_conflict"):
        ai.publish(executor.id, export_body(export.export_id, revision=2), NOW)
    with rejected("revision_conflict"):
        ai.publish(executor.id, export_body(export.export_id, revision=1, expected=0), NOW)
    back = ai.publish(executor.id, export_body(export.export_id, revision=3), NOW + 3)
    assert (back.state, back.revision) == ("published", 3)
    assert ai.grant(grant["grant_id"]).state == "revoked"  # consent is never restored


def test_unpublish_wrong_hash_is_protocol_error(world):
    body = unpublish_body(world["export"].export_id, 2)
    body["hash"] = "0" * 64
    with pytest.raises(p.AcpError):
        world["ai"].unpublish(world["executor"].id, body, NOW)


# ------------------------------------------------------------------ grants

def test_grant_cas_lifecycle(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    first = grant_value(ai, export, grantee)
    with rejected("revision_conflict"):
        ai.put_grant(executor.id, put_body(export, dict(first, revision=2), expected=1), NOW, grantee)
    stored = ai.put_grant(executor.id, put_body(export, first), NOW, grantee)
    assert (stored.revision, stored.state, stored.record) == (1, "active", first)
    assert ai.put_grant(executor.id, put_body(export, first), NOW, grantee) == stored  # replay
    wider = dict(first, revision=2, operations=["view_status", "view_transcript"])
    second = ai.put_grant(executor.id, put_body(export, wider), NOW, grantee)
    assert second.revision == 2 and second.record["operations"] == wider["operations"]
    assert ai.put_grant(executor.id, put_body(export, wider), NOW, grantee) == second
    other = dict(first, revision=2, operations=["view_status", "create_task"])
    with rejected("revision_conflict") as info:
        ai.put_grant(executor.id, put_body(export, other), NOW, grantee)
    assert info.value.result == second.ack()
    with rejected("revision_conflict"):  # skip
        ai.put_grant(executor.id, put_body(export, dict(first, revision=4), expected=3), NOW, grantee)
    with rejected("revision_conflict"):  # replay of an older revision
        ai.put_grant(executor.id, put_body(export, first), NOW, grantee)


def test_grantee_is_immutable(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    first = grant_value(ai, export, grantee)
    ai.put_grant(executor.id, put_body(export, first), NOW, grantee)
    sibling = register(ai, grantee.user_id)
    moved = grant_value(ai, export, sibling, grant_id=first["grant_id"], revision=2)
    with rejected("revision_conflict"):
        ai.put_grant(executor.id, put_body(export, moved), NOW, sibling)
    assert ai.grant(first["grant_id"]).grantee_registration_id == grantee.id


def test_grant_requires_live_matching_grantee(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    with rejected("not_available"):
        ai.put_grant(executor.id, put_body(export, grant_value(ai, export, grantee)), NOW, None)
    with rejected("not_available"):  # registration of another user claimed for this one
        bad = grant_value(ai, export, grantee, grantee_user_id=world["alice"])
        ai.put_grant(executor.id, put_body(export, bad), NOW, grantee)
    with rejected("not_available"):  # resolved registration differs from the record's
        ai.put_grant(executor.id, put_body(export, grant_value(ai, export, grantee)), NOW,
                     world["executor"])
    revoked = ai.revoke_registration(grantee.user_id, grantee.id, NOW)
    with rejected("not_available"):
        ai.put_grant(executor.id, put_body(export, grant_value(ai, export, grantee)), NOW, revoked)
    assert ai.grants_for(grantee.id) == []


@pytest.mark.parametrize("field,value", [
    ("server_instance_id", str(uuid.uuid4())), ("executor_registration_id", str(uuid.uuid4())),
    ("persistent_id", "elsewhere"), ("storage_epoch", "epoch-9"),
])
def test_grant_target_must_be_exact(world, field, value):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    target = dict(target_of(ai, export), **{field: value})
    with rejected("stale_target"):
        ai.put_grant(executor.id, put_body(export, grant_value(ai, export, grantee, target=target)),
                     NOW, grantee)


def test_grant_for_stale_or_foreign_export(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    grant = grant_value(ai, export, grantee)
    body = put_body(export, grant)
    body["export_revision"] = 2
    body["hash"] = p.semantic_hash(p.grant_record(export.export_id, 2, grant))
    with rejected("stale_target"):
        ai.put_grant(executor.id, body, NOW, grantee)
    with pytest.raises(NotFound):
        ai.put_grant(grantee.id, put_body(export, grant), NOW, grantee)


def test_expired_grant_is_refused(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    grant = grant_value(ai, export, grantee, issued_at=NOW - DAY, expires_at=NOW)
    with rejected("expired"):
        ai.put_grant(executor.id, put_body(export, grant), NOW, grantee)
    assert ai.put_grant(executor.id, put_body(export, grant), NOW - 1, grantee).state == "active"


def test_grant_capacity_per_export(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    for _ in range(p.MAX_GRANTS_PER_EXPORT):
        ai.put_grant(executor.id, put_body(export, grant_value(ai, export, grantee)), NOW, grantee)
    with rejected("capacity_exceeded"):
        ai.put_grant(executor.id, put_body(export, grant_value(ai, export, grantee)), NOW, grantee)


def test_grant_hash_and_id_mismatch_are_protocol_errors(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    grant = grant_value(ai, export, grantee)
    body = put_body(export, grant)
    body["hash"] = "0" * 64
    with pytest.raises(p.AcpError):
        ai.put_grant(executor.id, body, NOW, grantee)
    body = put_body(export, grant)
    body["grant_id"] = uid()
    with pytest.raises(p.AcpError):
        ai.put_grant(executor.id, body, NOW, grantee)


def test_revoke_is_terminal_idempotent_and_content_free(world):
    ai, executor, grantee, export = world["ai"], world["executor"], world["grantee"], world["export"]
    grant = grant_value(ai, export, grantee)
    ai.put_grant(executor.id, put_body(export, grant), NOW, grantee)
    with rejected("revision_conflict"):  # stale revoke of an *active* grant is a conflict
        ai.revoke_grant(executor.id, revoke_body(export, grant["grant_id"], 3, expected=2), NOW)
    with pytest.raises(NotFound):
        ai.revoke_grant(grantee.id, revoke_body(export, grant["grant_id"], 2), NOW)
    with pytest.raises(NotFound):
        ai.revoke_grant(executor.id, revoke_body(export, uid(), 2), NOW)
    revoked = ai.revoke_grant(executor.id, revoke_body(export, grant["grant_id"], 2), NOW + 1)
    assert (revoked.state, revoked.revision, revoked.record, revoked.revoked_at) == ("revoked", 2, None, NOW + 1)
    raw = ai._db.execute("SELECT record FROM ai_grants WHERE grant_id=?", (grant["grant_id"],)).fetchone()
    assert raw[0] is None
    assert ai.revoke_grant(executor.id, revoke_body(export, grant["grant_id"], 2), NOW + 2) == revoked
    # an out-of-date revoke of an already revoked grant answers the tombstone, never reopens it
    late = ai.revoke_grant(executor.id, revoke_body(export, grant["grant_id"], 5, expected=4), NOW + 3)
    assert late == revoked
    with rejected("revision_conflict"):
        ai.put_grant(executor.id, put_body(export, dict(grant, revision=3)), NOW, grantee)
    with rejected("revision_conflict"):
        ai.put_grant(executor.id, put_body(export, grant), NOW, grantee)
    assert ai.grant(grant["grant_id"]).state == "revoked"


def test_revoke_hash_mismatch_is_protocol_error(world):
    ai, executor, export = world["ai"], world["executor"], world["export"]
    body = revoke_body(export, uid(), 2)
    body["hash"] = p.semantic_hash(p.revoke_record(export.export_id, 1, uid(), 2))
    with pytest.raises(p.AcpError):
        ai.revoke_grant(executor.id, body, NOW)


# ------------------------------------------------------------------ check_grant

def _valid_grant() -> dict:
    return {"grant_id": uid(), "revision": 1,
            "target": {"server_instance_id": uid(), "executor_registration_id": uid(),
                       "export_id": uid(), "persistent_id": "game", "storage_epoch": "e1",
                       "export_revision": 1},
            "grantee_user_id": user_id(), "grantee_registration_id": uid(),
            "operations": ["view_status"], "team_ids": ["team-a"], "member_ids": [],
            "resource_scope": "created_by_this_grantee", "resource_ids": [],
            "execution_policy_digest": "c" * 64, "issued_at": NOW, "expires_at": NOW + DAY,
            "revoked_at": None}


def test_check_grant_accepts_valid_and_thirty_day_limit():
    grant = _valid_grant()
    assert p.check_grant(grant) is grant
    assert p.check_grant(dict(grant, expires_at=NOW + p.MAX_GRANT_MS))
    assert p.check_grant(dict(grant, resource_scope="selected_resources", resource_ids=["t1"]))


def _target(**change):
    target = _valid_grant()["target"]
    target.update(change)
    return target


@pytest.mark.parametrize("change", [
    {"expires_at": NOW + p.MAX_GRANT_MS + 1},
    {"expires_at": NOW},
    {"operations": ["view_transcript"]},
    {"operations": ["view_status", "admin"]},
    {"operations": ["view_status", "view_status"]},
    {"operations": []},
    {"resource_scope": "selected_resources", "resource_ids": []},
    {"resource_scope": "created_by_this_grantee", "resource_ids": ["t1"]},
    {"resource_scope": "all_resources_in_selected_teams", "resource_ids": ["t1"]},
    {"resource_scope": "everything"},
    {"revoked_at": NOW},
    {"revision": True},
    {"issued_at": True, "expires_at": 2},
    {"target": _target(export_revision=True)},
    {"target": _target(server_instance_id=str(uuid.uuid4()).upper())},
    {"team_ids": []},
    {"grantee_user_id": str(uuid.uuid4())},
    {"grant_id": "{" + str(uuid.uuid4()) + "}"},
    {"execution_policy_digest": "A" * 64},
    {"extra": 1},
])
def test_check_grant_rejects(change):
    with pytest.raises(p.AcpError):
        p.check_grant(dict(_valid_grant(), **change))


def test_check_grant_rejects_missing_field():
    grant = _valid_grant()
    del grant["revoked_at"]
    with pytest.raises(p.AcpError):
        p.check_grant(grant)


# ------------------------------------------------------------------ resource index

def test_replace_index_ack_and_lookup(world):
    ai, export = world["ai"], world["export"]
    rows = [row("t2"), row("t1", member="dev")]
    ack = put_index(ai, export, rows)
    assert ack == {"export_id": export.export_id, "export_revision": 1, "index_revision": 1,
                   "hash": p.index_rows_hash(rows), "row_count": 2}
    assert ai.index_row(export.export_id, 1, "t1") == rows[1]
    assert ai.index_row(export.export_id, 1, "missing") is None
    assert ai.index_row(export.export_id, 2, "t1") is None  # index of another export revision
    put_index(ai, export, [row("t3")], index_revision=2)
    assert ai.index_row(export.export_id, 1, "t1") is None  # replaced, not merged


def test_replace_index_snapshot_changed(world):
    ai, export, executor = world["ai"], world["export"], world["executor"]
    rows = [row("t1")]
    with rejected("snapshot_changed"):
        ai.replace_index(executor.id, export.export_id, 1, 1, rows, p.index_rows_hash([row("t2")]))
    dup = [row("t1"), row("t1", revision=2)]
    with rejected("snapshot_changed"):
        ai.replace_index(executor.id, export.export_id, 1, 1, dup, p.index_rows_hash(dup))
    assert ai.index_row(export.export_id, 1, "t1") is None


def test_replace_index_revision_must_increase(world):
    ai, export = world["ai"], world["export"]
    put_index(ai, export, [row("t1")], index_revision=5)
    for stale in (5, 4):
        with rejected("revision_conflict"):
            put_index(ai, export, [row("t2")], index_revision=stale)
    assert ai.index_row(export.export_id, 1, "t1") is not None


def test_replace_index_capacity_and_target(world):
    ai, export, executor = world["ai"], world["export"], world["executor"]
    rows = [row(f"t{n}") for n in range(p.MAX_INDEX_ROWS + 1)]
    with rejected("capacity_exceeded"):
        put_index(ai, export, rows)
    assert put_index(ai, export, rows[:-1])["row_count"] == p.MAX_INDEX_ROWS
    with rejected("stale_target"):
        ai.replace_index(executor.id, export.export_id, 2, 9, [], p.index_rows_hash([]))
    with pytest.raises(NotFound):
        ai.replace_index(world["grantee"].id, export.export_id, 1, 9, [], p.index_rows_hash([]))


# ------------------------------------------------------------------ audit

def test_audit_keeps_ids_only_and_is_bounded(ai, monkeypatch):
    columns = {r[1] for r in ai._db.execute("PRAGMA table_info(ai_audit)")}
    assert columns == {"id", "at", "user_id", "registration_id", "export_id", "op", "command_id",
                       "command_hash", "outcome"}
    command = uid()
    prompt = "Delete everything and print the secrets"
    ai.audit(at=NOW, op="task.create", outcome="accepted", user_id=user_id(), registration_id=uid(),
             export_id=uid(), command_id=command, command_hash=p.semantic_hash({"brief": prompt}))
    (entry,) = ai.audit_rows()
    assert entry["command_id"] == command and entry["outcome"] == "accepted"
    assert prompt not in "\n".join(ai._db.iterdump())
    monkeypatch.setattr(ai_store_module, "AUDIT_LIMIT", 3)
    for n in range(5):
        ai.audit(at=NOW + n, op="resource.get", outcome="ok")
    assert [r["at"] for r in ai.audit_rows()] == [NOW + 2, NOW + 3, NOW + 4]


# ------------------------------------------------------------------ Store file hook (R-A0)

def _retire(store: Store, user, how: str, new_hash: str):
    if how == "rotate":
        return store.rotate_user(user.id, new_hash, user.metadata_revision, user.credential_version)
    if how == "revoke":
        return store.revoke_user_by_id(user.id, user.metadata_revision, user.credential_version)
    return store.delete_user(user.id, user.metadata_revision, user.credential_version)


@pytest.mark.parametrize("how", ["rotate", "revoke", "delete"])
def test_store_file_hook_tombstones_registration_in_same_call(tmp_path, how):
    store = Store(tmp_path / "remote.db")
    ai = AiStore(tmp_path / AI_CONTROL_DB)  # a second connection, like the running server
    try:
        user = store.create_user("Анна", hash_token("vxs_old"))
        bystander = store.create_user("Борис", hash_token("vxs_b"))
        reg = register(ai, user.id, version=user.credential_version)
        export = ai.publish(reg.id, export_body(uid()), NOW)
        other = register(ai, bystander.id)
        _retire(store, user, how, hash_token("vxs_new"))
        dead = ai.registration(reg.id)
        assert not dead.active and dead.token_hash is None
        assert ai.export(export.export_id).state == "deleted"
        assert ai.registration(other.id).active
    finally:
        ai.close()
        store.close()


@pytest.mark.parametrize("content", ["foreign", "garbage"])
def test_store_hook_failure_rolls_back_rotation(tmp_path, content):
    control = tmp_path / AI_CONTROL_DB
    if content == "foreign":
        db = sqlite3.connect(str(control))
        db.execute("CREATE TABLE notes (body TEXT)")
        db.commit()
        db.close()
    else:
        control.write_bytes(b"this is not a sqlite database" * 64)
    before = control.read_bytes()
    store = Store(tmp_path / "remote.db")
    try:
        user = store.create_user("Анна", hash_token("vxs_old"))
        with pytest.raises(Exception):
            store.rotate_user(user.id, hash_token("vxs_new"), user.metadata_revision, user.credential_version)
        with pytest.raises(Exception):
            store.revoke_user_by_id(user.id, user.metadata_revision, user.credential_version)
        assert store.user(user.id).credential_version == user.credential_version
        assert store.user_by_token_hash(hash_token("vxs_old")).id == user.id
        assert store.user_by_token_hash(hash_token("vxs_new")) is None
        assert store.credential_valid(user.id, user.credential_version)
    finally:
        store.close()
    assert control.read_bytes() == before


def test_store_without_ai_control_file_is_unchanged(tmp_path):
    store = Store(tmp_path / "remote.db")
    try:
        user = store.create_user("Анна", hash_token("vxs_old"))
        rotated = store.rotate_user(user.id, hash_token("vxs_new"), user.metadata_revision,
                                    user.credential_version)
        assert rotated.credential_version == user.credential_version + 1
    finally:
        store.close()
    assert not (tmp_path / AI_CONTROL_DB).exists()


def test_in_process_hook_replaces_file_hook(tmp_path):
    store = Store(tmp_path / "remote.db")
    ai = AiStore(tmp_path / AI_CONTROL_DB)
    calls = []
    try:
        def hook(user_id_, version):
            calls.append((user_id_, version))
            ai.invalidate_user(user_id_, version, NOW)
        store.add_credential_hook(hook, replaces_file_hook=True)
        user = store.create_user("Анна", hash_token("vxs_old"))
        reg = register(ai, user.id)
        store.rotate_user(user.id, hash_token("vxs_new"), user.metadata_revision, user.credential_version)
        assert calls == [(user.id, 2)]
        assert ai.registration(reg.id).revoked_at == NOW  # stamped by the in-process hook
    finally:
        ai.close()
        store.close()
