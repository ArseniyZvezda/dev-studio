"""ACP/1 authentication and per-request authorisation (ai-access design §4–§6, §9; R-A1).

Two credential levels (personal Remote key + device registration) are re-checked on every
frame, and a grant mirror decides what one device may do on one exact target. Foreign or
unknown objects answer ``not_available`` — no existence oracle.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from starlette.datastructures import Headers

from voxarium_studio_remote import ai_protocol as p
from voxarium_studio_remote.ai_auth import (
    Actor, AiAuth, CLIENT_HEADER, DEVICE_HEADER, hash_device_token, new_device_token,
)
from voxarium_studio_remote.ai_store import AiStore
from voxarium_studio_remote.auth import build_authenticator, hash_token, new_token
from voxarium_studio_remote.store import Store

NOW = 1_800_000_000_000
DAY = 24 * 3600 * 1000


def uid() -> str:
    return str(uuid.uuid4())


@contextmanager
def rejected(code: str):
    with pytest.raises(p.RequestRejected) as info:
        yield info
    assert info.value.code == code


class Person:
    def __init__(self, world: "World", name: str) -> None:
        self.world = world
        self.key = new_token()
        self.row = world.store.create_user(name, hash_token(self.key))
        self.id = self.row.id

    def rotate(self) -> None:
        row = self.world.store.user(self.id)
        self.key = new_token()
        self.world.store.rotate_user(self.id, hash_token(self.key), row.metadata_revision,
                                     row.credential_version)

    def revoke(self) -> None:
        row = self.world.store.user(self.id)
        self.world.store.revoke_user_by_id(self.id, row.metadata_revision, row.credential_version)

    @property
    def version(self) -> int:
        return self.world.store.user(self.id).credential_version


class Device:
    def __init__(self, world: "World", owner: Person, kind: str = "studio",
                 client_id: str | None = None) -> None:
        self.owner, self.token = owner, new_device_token()
        self.client_id = client_id or uid()
        self.reg = world.ai.register(owner.id, owner.version, self.client_id, "Device", kind,
                                     hash_device_token(self.token), NOW)
        self.id = self.reg.id

    def headers(self, *, key: str | None = None, client_id: str | None = None,
                token: str | None = None) -> dict:
        return {"authorization": f"Bearer {key or self.owner.key}",
                CLIENT_HEADER: client_id or self.client_id, DEVICE_HEADER: token or self.token}


class World:
    """alice's PC exports one project; bob's phone holds a grant on it."""

    def __init__(self, *, hook: bool = False) -> None:
        self.store = Store(":memory:")
        self.ai = AiStore(":memory:")
        self.auth = AiAuth(self.store, self.ai, build_authenticator(self.store))
        if hook:  # the server's in-process R-A0 wiring (app.py)
            self.store.add_credential_hook(lambda u, v: self.ai.invalidate_user(u, v, NOW),
                                           replaces_file_hook=True)
        self.alice, self.bob, self.carol = (Person(self, n) for n in ("Alice", "Bob", "Carol"))
        self.pc = Device(self, self.alice)
        self.phone = Device(self, self.bob, "android")
        self.tablet = Device(self, self.bob, "android")
        self.carol_phone = Device(self, self.carol, "android")
        self.export = self.publish()

    def close(self) -> None:
        self.ai.close()
        self.store.close()

    def publish(self, revision: int = 1, display_name: str = "Game"):
        body = {"export_id": getattr(self, "export", None) and self.export.export_id or uid(),
                "persistent_id": "game", "storage_epoch": "epoch-1", "revision": revision,
                "expected_revision": revision - 1, "policy_digest": "a" * 64,
                "display_name": display_name}
        body["hash"] = p.semantic_hash(p.export_record(body))
        self.export = self.ai.publish(self.pc.id, body, NOW)
        return self.export

    def target(self, **change) -> dict:
        return dict(self.auth.target_of(self.export), **change)

    def grant(self, device: Device | None = None, *, operations=("view_status",),
              team_ids=("team-a",), member_ids=("dev",), scope="all_resources_in_selected_teams",
              resource_ids=(), expires_at: int = NOW + DAY) -> dict:
        device = device or self.phone
        grant = {"grant_id": uid(), "revision": 1, "target": self.target(),
                 "grantee_user_id": device.owner.id, "grantee_registration_id": device.id,
                 "operations": list(operations), "team_ids": list(team_ids),
                 "member_ids": list(member_ids), "resource_scope": scope,
                 "resource_ids": list(resource_ids), "execution_policy_digest": "b" * 64,
                 "issued_at": NOW - 1000, "expires_at": expires_at, "revoked_at": None}
        body = {"export_id": self.export.export_id, "export_revision": self.export.revision,
                "grant_id": grant["grant_id"], "revision": 1, "expected_revision": 0, "grant": grant,
                "hash": p.semantic_hash(p.grant_record(self.export.export_id, self.export.revision, grant))}
        self.ai.put_grant(self.pc.id, body, NOW - 1000, device.reg)
        return grant

    def revoke(self, grant: dict) -> None:
        body = {"export_id": self.export.export_id, "export_revision": self.export.revision,
                "grant_id": grant["grant_id"], "revision": 2, "expected_revision": 1,
                "hash": p.semantic_hash(p.revoke_record(self.export.export_id, self.export.revision,
                                                        grant["grant_id"], 2))}
        self.ai.revoke_grant(self.pc.id, body, NOW)

    def index(self, *rows: dict, index_revision: int = 1) -> None:
        self.ai.replace_index(self.pc.id, self.export.export_id, self.export.revision, index_revision,
                              list(rows), p.index_rows_hash(list(rows)))

    def origin(self, device: Device) -> dict:
        return {"user_id": device.owner.id, "registration_id": device.id}

    def actor(self, device: Device | None = None) -> Actor:
        actor = self.auth.device((device or self.phone).headers())
        assert actor is not None
        return actor


def row(resource_id: str, *, team: str = "team-a", member: str | None = "dev", origin=None) -> dict:
    return {"resource_id": resource_id, "kind": "task", "team_id": team, "member_id": member,
            "remote_origin": origin, "revision": 1}


def request(world: World, grant: dict, op: str = "resource.get", *, resource_id: str | None = "t-other",
            payload: dict | None = None, target: dict | None = None, grant_revision: int = 1,
            issued_at: int = NOW) -> dict:
    _, mutation, needs_resource = p.OPERATIONS[op]
    body = {"target": target or world.target(), "grant_id": grant["grant_id"],
            "grant_revision": grant_revision, "op": op, "payload": {} if payload is None else payload}
    if needs_resource:
        body["resource_id"] = resource_id
    if mutation:
        body.update(command_id=uid(), issued_at=issued_at, expected_executor_epoch=uid())
    if op in ("task.edit_draft", "task.start", "chat.send"):
        body["expected_resource_revision"] = 1
    if op in ("task.cancel", "chat.cancel"):
        body["expected_run_id"] = uid()
    return p.check_request(body)


@pytest.fixture
def world():
    value = World()
    yield value
    value.close()


@pytest.fixture
def indexed(world):
    world.index(row("t-own", origin=world.origin(world.phone)), row("t-other"),
                row("t-tablet", origin=world.origin(world.tablet)),
                row("t-team-b", team="team-b"), row("t-qa", member="qa"),
                row("t-unassigned", member=None))
    return world


# ------------------------------------------------------------------ personal key

def test_personal_accepts_valid_bearer(world):
    user = world.auth.personal({"authorization": f"Bearer {world.alice.key}"})
    assert user is not None and user.id == world.alice.id and user.credential_version == 1
    raw = Headers(raw=[(b"authorization", f"bearer {world.alice.key}".encode())])
    assert world.auth.personal(raw).id == world.alice.id


@pytest.mark.parametrize("headers", [
    {}, {"authorization": ""}, {"authorization": "Bearer "}, {"authorization": "Bearer vxs_unknown"},
    {"authorization": "Basic {key}"}, {"authorization": "Token {key}"}, {"authorization": "{key}"},
])
def test_personal_refuses_bad_headers(world, headers):
    headers = {k: v.format(key=world.alice.key) for k, v in headers.items()}
    assert world.auth.personal(headers) is None


def test_personal_refuses_duplicate_authorization(world):
    value = f"Bearer {world.alice.key}".encode()
    assert world.auth.personal(Headers(raw=[(b"authorization", value), (b"authorization", value)])) is None


def test_personal_refuses_rotated_and_revoked_keys(world):
    old = world.alice.key
    world.alice.rotate()
    assert world.auth.personal({"authorization": f"Bearer {old}"}) is None
    assert world.auth.personal({"authorization": f"Bearer {world.alice.key}"}).credential_version == 2
    current = world.alice.key
    world.alice.revoke()
    assert world.auth.personal({"authorization": f"Bearer {current}"}) is None


# ------------------------------------------------------------------ device registration

def test_device_resolves_actor(world):
    actor = world.auth.device(world.phone.headers())
    assert actor == Actor(world.bob.id, 1, world.phone.id, world.phone.client_id, "android")
    assert actor.source() == {"user_id": world.bob.id, "registration_id": world.phone.id}
    raw = Headers(raw=[(k.encode(), v.encode()) for k, v in world.phone.headers().items()])
    assert world.auth.device(raw) == actor


def test_device_refuses_wrong_or_missing_credentials(world):
    phone = world.phone
    cases = [
        phone.headers(token=new_device_token()),
        phone.headers(token=world.tablet.token),               # sibling's secret
        phone.headers(key=world.carol.key),                     # other user's key
        world.pc.headers(key=world.bob.key),                    # other user's client id
        phone.headers(client_id=str(uuid.uuid1())),
        phone.headers(client_id=phone.client_id.upper()),
        phone.headers(client_id="{" + phone.client_id + "}"),
        phone.headers(token="vxd_" + "x" * 300),
        phone.headers(key="vxs_unknown"),
    ]
    for headers in cases:
        assert world.auth.device(headers) is None, headers
    for missing in (CLIENT_HEADER, DEVICE_HEADER, "authorization"):
        headers = phone.headers()
        del headers[missing]
        assert world.auth.device(headers) is None, missing


@pytest.mark.parametrize("duplicate", [CLIENT_HEADER, DEVICE_HEADER])
def test_device_refuses_duplicate_device_headers(world, duplicate):
    headers = world.phone.headers()
    raw = [(k.encode(), v.encode()) for k, v in headers.items()]
    raw.append((duplicate.encode(), headers[duplicate].encode()))
    assert world.auth.device(Headers(raw=raw)) is None


def test_device_refuses_revoked_registration(world):
    world.ai.revoke_registration(world.bob.id, world.phone.id, NOW)
    assert world.auth.device(world.phone.headers()) is None
    assert world.auth.device(world.tablet.headers()) is not None


@pytest.mark.parametrize("hook", [False, True])
def test_device_from_older_key_generation_fails_with_new_key(hook):
    world = World(hook=hook)
    try:
        world.bob.rotate()
        assert world.auth.device(world.phone.headers()) is None
        fresh = Device(world, world.bob, "android", client_id=world.phone.client_id)
        actor = world.auth.device(fresh.headers())
        assert actor is not None and actor.registration_id != world.phone.id
        assert actor.credential_version == 2
        assert world.auth.device(fresh.headers(token=world.phone.token)) is None
    finally:
        world.close()


# ------------------------------------------------------------------ per-frame guard

def test_current_tracks_both_credentials(world):
    actor = world.actor()
    assert world.auth.current(actor)
    other = world.actor(world.tablet)
    world.ai.revoke_registration(world.bob.id, world.tablet.id, NOW)
    assert not world.auth.current(other) and world.auth.current(actor)
    world.bob.rotate()
    assert not world.auth.current(actor)


def test_current_false_after_revoke_then_reactivate(world):
    actor = world.actor()
    world.bob.revoke()
    assert not world.auth.current(actor)
    world.bob.rotate()  # reactivation issues a new key generation
    assert world.bob.version == 3
    assert not world.auth.current(actor)


def test_current_refuses_forged_actor(world):
    actor = world.actor()
    assert not world.auth.current(Actor(world.carol.id, 1, actor.registration_id, actor.client_id, actor.kind))
    assert not world.auth.current(Actor(actor.user_id, 2, actor.registration_id, actor.client_id, actor.kind))
    assert not world.auth.current(Actor(actor.user_id, 1, uid(), actor.client_id, actor.kind))


def test_fingerprint_changes_with_credential_version(world):
    before = world.actor()
    world.bob.rotate()
    after = world.actor(Device(world, world.bob, "android", client_id=world.phone.client_id))
    assert before.fingerprint == (world.bob.id, 1, world.phone.id)
    assert after.fingerprint[1] == 2 and after.fingerprint != before.fingerprint


# ------------------------------------------------------------------ authorize

def test_authorize_happy_read(indexed):
    grant = indexed.grant()
    result = indexed.auth.authorize(indexed.actor(), request(indexed, grant), NOW)
    assert result.grant.grant_id == grant["grant_id"] and result.export == indexed.export
    assert result.resource == row("t-other") and not result.created_by_grantee
    for op in ("catalog.list", "resource.list"):
        assert indexed.auth.authorize(indexed.actor(), request(indexed, grant, op), NOW).resource is None


def test_authorize_wrong_server_instance_is_stale(indexed):
    grant = indexed.grant()
    with rejected("stale_target"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, target=indexed.target(
            server_instance_id=uid())), NOW)


def test_authorize_foreign_or_unknown_grant_is_not_available(indexed):
    grant = indexed.grant()
    tablet_grant = indexed.grant(indexed.tablet)
    carol_grant = indexed.grant(indexed.carol_phone)
    for body in (request(indexed, dict(grant, grant_id=uid())), request(indexed, tablet_grant),
                 request(indexed, carol_grant)):
        with rejected("not_available"):
            indexed.auth.authorize(indexed.actor(), body, NOW)
    with rejected("not_available"):  # carol presenting bob's grant
        indexed.auth.authorize(indexed.actor(indexed.carol_phone), request(indexed, grant), NOW)
    with rejected("not_available"):  # grant presented against another export_id
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, target=indexed.target(
            export_id=uid())), NOW)


def test_authorize_revoked_grant_is_denied(indexed):
    grant = indexed.grant()
    indexed.revoke(grant)
    with rejected("denied"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, grant_revision=2), NOW)


def test_authorize_expiry_is_exclusive(indexed):
    grant = indexed.grant(expires_at=NOW + 10)
    assert indexed.auth.authorize(indexed.actor(), request(indexed, grant), NOW + 9)
    for when in (NOW + 10, NOW + 11):
        with rejected("expired"):
            indexed.auth.authorize(indexed.actor(), request(indexed, grant), when)


@pytest.mark.parametrize("change", [
    {"export_revision": 2}, {"persistent_id": "other"}, {"storage_epoch": "epoch-2"},
    {"executor_registration_id": str(uuid.uuid4())},
])
def test_authorize_stale_target_fields(indexed, change):
    grant = indexed.grant()
    with rejected("stale_target"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, target=indexed.target(**change)), NOW)


def test_authorize_stale_grant_revision(indexed):
    grant = indexed.grant()
    with rejected("stale_target"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, grant_revision=2), NOW)


def test_authorize_after_export_revision_bump(indexed):
    old_target = indexed.target()
    grant = indexed.grant()
    indexed.publish(revision=2, display_name="Two")
    with rejected("denied"):  # the bump revoked every grant bound to revision 1
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, target=old_target), NOW)
    fresh = indexed.grant()
    with rejected("stale_target"):
        indexed.auth.authorize(indexed.actor(), request(indexed, fresh, target=old_target), NOW)
    with rejected("not_available"):  # new revision, no index yet: fail closed
        indexed.auth.authorize(indexed.actor(), request(indexed, fresh), NOW)


def test_authorize_permissions(indexed):
    status_only = indexed.grant(operations=["view_status"])
    actor = indexed.actor()
    for op, payload in (("transcript.page", {}), ("stream.subscribe", {"topic": "transcript"}),
                        ("task.start", {}), ("task.create", {
                            "team_id": "team-a", "member_id": "dev", "title": "T", "brief": "",
                            "write_paths": [], "locale": "en"})):
        with rejected("denied"):
            indexed.auth.authorize(actor, request(indexed, status_only, op, payload=payload), NOW)
    assert indexed.auth.authorize(actor, request(indexed, status_only, "stream.subscribe",
                                                 payload={"topic": "status"}), NOW)
    reader = indexed.grant(operations=["view_status", "view_transcript"])
    assert indexed.auth.authorize(actor, request(indexed, reader, "transcript.page"), NOW)
    assert indexed.auth.authorize(actor, request(indexed, reader, "stream.subscribe",
                                                 payload={"topic": "transcript"}), NOW)


@pytest.mark.parametrize("team,member,code", [
    ("team-a", "dev", None), ("team-b", "dev", "denied"), ("team-a", "qa", "denied"),
])
def test_authorize_create_respects_team_and_member_allowlist(indexed, team, member, code):
    grant = indexed.grant(operations=["view_status", "create_task", "create_chat"])
    for op, payload in (("task.create", {"team_id": team, "member_id": member, "title": "Fix",
                                         "brief": "b", "write_paths": ["src/a.py"], "locale": "ru"}),
                        ("chat.create", {"team_id": team, "member_id": member, "title": "Ask",
                                         "locale": "en"})):
        body = request(indexed, grant, op, payload=payload)
        if code is None:
            assert indexed.auth.authorize(indexed.actor(), body, NOW).resource is None
        else:
            with rejected(code):
                indexed.auth.authorize(indexed.actor(), body, NOW)


@pytest.mark.parametrize("resource_id", ["missing", "t-team-b", "t-qa"])
def test_authorize_resource_outside_index_or_scope(indexed, resource_id):
    grant = indexed.grant()
    with rejected("not_available"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, resource_id=resource_id), NOW)


def test_authorize_without_index_is_not_available(world):
    grant = world.grant()
    with rejected("not_available"):
        world.auth.authorize(world.actor(), request(world, grant), NOW)


def test_authorize_member_unrestricted_and_unassigned(indexed):
    open_members = indexed.grant(member_ids=[])
    assert indexed.auth.authorize(indexed.actor(), request(indexed, open_members, resource_id="t-qa"), NOW)
    restricted = indexed.grant()
    assert indexed.auth.authorize(indexed.actor(), request(indexed, restricted, resource_id="t-unassigned"), NOW)


def test_authorize_resource_list_other_team_is_not_available(indexed):
    grant = indexed.grant()
    with rejected("not_available"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, "resource.list",
                                                        payload={"team_id": "team-b"}), NOW)
    assert indexed.auth.authorize(indexed.actor(), request(indexed, grant, "resource.list",
                                                           payload={"team_id": "team-a"}), NOW)


def test_authorize_created_by_this_grantee_scope(indexed):
    grant = indexed.grant(scope="created_by_this_grantee")
    own = indexed.auth.authorize(indexed.actor(), request(indexed, grant, resource_id="t-own"), NOW)
    assert own.created_by_grantee and own.resource["remote_origin"] == indexed.origin(indexed.phone)
    for other in ("t-other", "t-tablet"):  # a sibling device of the same user is not "this grantee"
        with rejected("not_available"):
            indexed.auth.authorize(indexed.actor(), request(indexed, grant, resource_id=other), NOW)


def test_authorize_selected_resources_scope(indexed):
    grant = indexed.grant(scope="selected_resources", resource_ids=["t-other"])
    assert indexed.auth.authorize(indexed.actor(), request(indexed, grant, resource_id="t-other"), NOW)
    with rejected("not_available"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, resource_id="t-own"), NOW)


def test_authorize_mutation_needs_operate_existing_for_foreign_resource(indexed):
    plain = indexed.grant(operations=["view_status", "run_task", "cancel_task"])
    with rejected("denied"):
        indexed.auth.authorize(indexed.actor(), request(indexed, plain, "task.start"), NOW)
    with rejected("denied"):
        indexed.auth.authorize(indexed.actor(), request(indexed, plain, "task.cancel"), NOW)
    mine = indexed.auth.authorize(indexed.actor(), request(indexed, plain, "task.start", resource_id="t-own"), NOW)
    assert mine.created_by_grantee
    wider = indexed.grant(operations=["view_status", "run_task", "operate_existing"])
    allowed = indexed.auth.authorize(indexed.actor(), request(indexed, wider, "task.start"), NOW)
    assert not allowed.created_by_grantee and allowed.resource["resource_id"] == "t-other"


def test_authorize_issued_at_skew(indexed):
    grant = indexed.grant(operations=["view_status", "run_task", "operate_existing"])
    limit = NOW + p.MAX_ISSUED_SKEW_MS
    assert indexed.auth.authorize(indexed.actor(), request(indexed, grant, "task.start", issued_at=limit), NOW)
    with rejected("expired"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant, "task.start", issued_at=limit + 1), NOW)


def test_authorize_executor_owner_key_rotated_is_not_available(indexed):
    grant = indexed.grant()
    indexed.alice.rotate()  # no in-process hook: the live-export check still fails closed
    assert indexed.ai.grant(grant["grant_id"]).state == "active"
    with rejected("not_available"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant), NOW)


def test_authorize_executor_owner_key_rotated_with_hook():
    world = World(hook=True)
    try:
        world.index(row("t-other"))
        grant = world.grant()
        world.alice.rotate()
        # the hook cascades the revocation onto the grant mirror (design §9)
        assert world.ai.grant(grant["grant_id"]).state == "revoked"
        with rejected("denied"):
            world.auth.authorize(world.actor(), request(world, grant), NOW)
    finally:
        world.close()


def test_authorize_executor_registration_revoked(indexed):
    grant = indexed.grant()
    indexed.ai.revoke_registration(indexed.alice.id, indexed.pc.id, NOW)
    assert indexed.ai.export(indexed.export.export_id).state == "deleted"
    # The cascade revokes the grant mirror itself, and a revoked grant answers "denied"
    # before the (now deleted) export is looked up.
    assert indexed.ai.grant(grant["grant_id"]).state == "revoked"
    with rejected("denied"):
        indexed.auth.authorize(indexed.actor(), request(indexed, grant), NOW)


# ------------------------------------------------------------------ visible grants

def test_visible_grants_lists_only_effective_ones(world):
    live = world.grant()
    revoked = world.grant()
    world.revoke(revoked)
    world.grant(expires_at=NOW)          # expired at NOW
    world.grant(world.tablet)            # another device of the same user
    world.grant(world.carol_phone)
    listed = world.auth.visible_grants(world.actor(), NOW)
    assert [(g.grant_id, e.export_id) for g, e in listed] == [(live["grant_id"], world.export.export_id)]
    assert world.auth.visible_grants(world.actor(), NOW + DAY) == []


def test_visible_grants_hides_dead_exports(world):
    world.grant()
    world.alice.rotate()  # executor credentials dead -> export not live
    assert world.auth.visible_grants(world.actor(), NOW) == []
    other = World()
    try:
        other.grant()
        other.publish(revision=2, display_name="Two")
        assert other.auth.visible_grants(other.actor(), NOW) == []
    finally:
        other.close()
