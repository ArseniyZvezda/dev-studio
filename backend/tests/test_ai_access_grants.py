"""Local AI-access consent and the effective check (ai-access-design §4, §6, §7, §9).

Pure local storage under the per-test Studio home; no network, Remote or AI. Remote
ACKs are simulated with :class:`MirrorAck` built from the frames the PC would send.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from voxarium_studio.ai_access import grants as g
from voxarium_studio.ai_access import identity as ident
from voxarium_studio.ai_access import protocol as p
from voxarium_studio.ai_access.identity import AckOutcome, AiAccessConflict, AiAccessError
from voxarium_studio.ai_access.protocol import ErrorCode, MirrorState, Operation, RemoteActor

NOW = 1_800_000_000_000
POLICY = g.policy_digest(backend="claude", model="opus", permission_mode="default",
                         tools_policy=["Read"], mcp_policy={"studio": True}, max_turns=20,
                         limits={"max_jobs": 1})


def uid() -> str:
    return str(uuid.uuid4())


@dataclass
class Setup:
    registration: ident.Registration
    export: ident.ExportRecord
    target: p.Target
    user: str
    device: str


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    root = tmp_path / "managed-project"
    root.mkdir()
    ident.ensure_client_id()
    registration = ident.record_registration(
        profile_id=uid(), url="https://remote.example.com", server_instance_id=uid(),
        user_id=uid(), registration_id=uid(), display_name="Executor PC",
    )
    registration = ident.set_publication_enabled(registration.registration_id, True,
                                                 expected_revision=1)
    export = ident.create_export(
        root=root, registration_id=registration.registration_id, persistent_id="demo",
        storage_epoch="e" * 32, identity_revision=1, display_name="Demo",
        policy_digest="d" * 64,
    )
    frame = ident.publish_export(export.export_id, expected_revision=1)
    ident.apply_export_ack(p.MirrorAck(export_id=frame.export_id, revision=frame.revision,
                                       hash=frame.hash, state=MirrorState.EFFECTIVE))
    export = ident.get_export(export.export_id)
    return Setup(registration, export, ident.export_target(export), uid(), uid())


def create(s: Setup, **overrides) -> ident.GrantRecord:
    data = {
        "grantee_user_id": s.user, "grantee_registration_id": s.device,
        "operations": ["view_status"], "team_ids": ["team-a"], "member_ids": ["dev"],
        "execution_policy_digest": POLICY, "now_ms": NOW,
    }
    data.update(overrides)
    return g.create_grant(s.export.export_id, **data)


def ack(record: ident.GrantRecord, *, hash_: str | None = None,
        state: MirrorState | None = None, revision: int | None = None) -> AckOutcome:
    frame = g.mirror_frame(record)
    revoked = record.grant.revoked_at is not None
    return g.apply_grant_ack(p.MirrorAck(
        export_id=frame.export_id, grant_id=frame.grant_id,
        revision=frame.revision if revision is None else revision,
        hash=frame.hash if hash_ is None else hash_,
        state=state or (MirrorState.REVOKED if revoked else MirrorState.EFFECTIVE),
    ))


def acked(s: Setup, **overrides) -> ident.GrantRecord:
    record = create(s, **overrides)
    assert ack(record) is AckOutcome.APPLIED
    return g.get_grant(record.grant.grant_id)


def actor(s: Setup, grant_id: str, revision: int, *, user: str | None = None,
          device: str | None = None) -> RemoteActor:
    return RemoteActor(user_id=user or s.user, registration_id=device or s.device,
                       grant_id=grant_id, grant_revision=revision, command_id=uid())


def check(s: Setup, grant_id: str, revision: int, op: Operation = Operation.RESOURCE_GET, *,
          now: int = NOW + 1, target: p.Target | None = None, who: RemoteActor | None = None,
          team: str | None = "team-a", member: str | None = "dev",
          resource: str | None = "task_1", created: bool = True, **extra) -> p.Grant:
    return g.effective_grant(
        grant_id, now_ms=now, target=target or s.target,
        actor=who or actor(s, grant_id, revision), op=op, team_id=team, member_id=member,
        resource_id=resource, created_by_grantee=created, **extra,
    )


def denied(code: ErrorCode = ErrorCode.DENIED):
    return pytest.raises(g.GrantDenied, match=f"^{code.value}$")


# --------------------------------------------------------------------------- defaults


def test_default_deny_without_any_record() -> None:
    fake = p.Target(server_instance_id=uid(), executor_registration_id=uid(), export_id=uid(),
                    persistent_id="demo", storage_epoch="e" * 32, export_revision=1)
    grant_id = uid()
    who = RemoteActor(user_id=uid(), registration_id=uid(), grant_id=grant_id,
                      grant_revision=1)
    with denied(ErrorCode.NOT_AVAILABLE):
        g.effective_grant(grant_id, now_ms=NOW, target=fake, actor=who,
                          op=Operation.CATALOG_LIST)
    assert not ident.registry_file().exists()


def test_grant_requires_published_export(tmp_path: Path) -> None:
    ident.ensure_client_id()
    registration = ident.record_registration(
        profile_id=uid(), url="https://remote.example.com", server_instance_id=uid(),
        user_id=uid(), registration_id=uid(), display_name="PC")
    root = tmp_path / "p"
    root.mkdir()
    export = ident.create_export(
        root=root, registration_id=registration.registration_id, persistent_id="demo",
        storage_epoch="e" * 32, identity_revision=1, display_name="Demo",
        policy_digest="d" * 64)
    with pytest.raises(AiAccessError) as caught:
        g.create_grant(export.export_id, grantee_user_id=uid(), grantee_registration_id=uid(),
                       operations=["view_status"], team_ids=["t"], member_ids=["m"],
                       execution_policy_digest=POLICY, now_ms=NOW)
    assert caught.value.code == "export_not_published"


def test_create_validation_defaults_and_no_wildcards(setup: Setup) -> None:
    record = create(setup)
    grant = record.grant
    assert grant.revision == 1 and grant.target == setup.target
    assert grant.expires_at - grant.issued_at == p.GRANT_DEFAULT_MS
    assert grant.resource_scope is p.ResourceScope.CREATED_BY_THIS_GRANTEE
    assert record.mirror is MirrorState.PENDING and record.active is None
    for bad in ({"grantee_user_id": "*"}, {"grantee_registration_id": "all"},
                {"team_ids": []}, {"member_ids": []}, {"operations": []},
                {"operations": ["root"]}, {"team_ids": "team-a"},
                {"expires_at": NOW + p.GRANT_MAX_MS + 1}, {"expires_at": NOW},
                {"resource_scope": "selected_resources"}, {"execution_policy_digest": "x"}):
        with pytest.raises(AiAccessError) as caught:
            create(setup, **bad)
        assert caught.value.code == "invalid_grant"
        assert "root" not in str(caught.value) and "*" not in str(caught.value)
    assert create(setup, expires_at=NOW + p.GRANT_MAX_MS).grant.expires_at == (
        NOW + p.GRANT_MAX_MS)


# --------------------------------------------------------------------------- ACK ordering


def test_new_grant_is_pending_until_matching_ack(setup: Setup) -> None:
    record = create(setup)
    grant_id = record.grant.grant_id
    with denied():
        check(setup, grant_id, 1)
    frame = g.grant_put_frame(record.grant)
    assert frame.hash == p.record_hash(frame) == record.mirror_hash
    assert (frame.revision, frame.expected_revision, frame.export_revision) == (1, 0, 1)
    assert ack(record, hash_="0" * 64) is AckOutcome.CONFLICT
    assert g.get_grant(grant_id).mirror is MirrorState.CONFLICT
    with denied():
        check(setup, grant_id, 1)
    assert ack(record, state=MirrorState.REVOKED) is AckOutcome.CONFLICT
    assert ack(record, revision=2) is AckOutcome.CONFLICT
    assert ack(record) is AckOutcome.APPLIED
    assert check(setup, grant_id, 1) == record.grant
    assert ack(record) is AckOutcome.DUPLICATE


def test_expansion_waits_for_ack_and_keeps_old_rights(setup: Setup) -> None:
    record = acked(setup)
    grant_id = record.grant.grant_id
    updated = g.update_grant(grant_id, expected_revision=1, now_ms=NOW + 10,
                             operations=["view_status", "view_transcript"])
    assert updated.grant.revision == 2 and updated.mirror is MirrorState.PENDING
    assert updated.active == record.grant  # old acknowledged rights only
    assert g.get_grant(grant_id).grant.target.export_revision == 1  # export untouched
    assert ident.get_export(setup.export.export_id).export_revision == 1
    assert check(setup, grant_id, 1, now=NOW + 20).revision == 1
    with denied():
        check(setup, grant_id, 1, Operation.TRANSCRIPT_PAGE, now=NOW + 20)
    with denied():
        check(setup, grant_id, 2, now=NOW + 20)
    assert ack(updated) is AckOutcome.APPLIED
    assert check(setup, grant_id, 2, Operation.TRANSCRIPT_PAGE, now=NOW + 20).revision == 2
    with denied():
        check(setup, grant_id, 1, now=NOW + 20)  # old grant revision


def test_narrowing_applies_locally_first(setup: Setup) -> None:
    record = acked(setup, operations=["view_status", "view_transcript"])
    grant_id = record.grant.grant_id
    assert check(setup, grant_id, 1, Operation.TRANSCRIPT_PAGE)
    narrowed = g.update_grant(grant_id, expected_revision=1, now_ms=NOW + 10,
                              operations=["view_status"])
    assert narrowed.active == narrowed.grant
    # The old revision's rights end immediately, even without any ACK.
    with denied():
        check(setup, grant_id, 1, Operation.TRANSCRIPT_PAGE, now=NOW + 20)
    with denied():
        check(setup, grant_id, 1, now=NOW + 20)
    assert ack(narrowed) is AckOutcome.APPLIED
    assert check(setup, grant_id, 2, now=NOW + 20).revision == 2
    with denied():
        check(setup, grant_id, 2, Operation.TRANSCRIPT_PAGE, now=NOW + 20)


def test_mixed_update_honours_nothing_until_ack(setup: Setup) -> None:
    record = acked(setup, team_ids=["team-a", "team-b"])
    grant_id = record.grant.grant_id
    mixed = g.update_grant(grant_id, expected_revision=1, now_ms=NOW + 10,
                           team_ids=["team-a", "team-c"])
    assert mixed.active is None
    with denied():
        check(setup, grant_id, 1, now=NOW + 20)
    assert ack(mixed) is AckOutcome.APPLIED
    assert check(setup, grant_id, 2, now=NOW + 20, team="team-c")


def test_cas_conflict_and_failed_ack_never_restore_rights(setup: Setup) -> None:
    record = acked(setup, operations=["view_status", "view_transcript"])
    grant_id = record.grant.grant_id
    narrowed = g.update_grant(grant_id, expected_revision=1, now_ms=NOW + 10,
                              operations=["view_status"])
    with pytest.raises(AiAccessConflict):
        g.update_grant(grant_id, expected_revision=1, now_ms=NOW + 11,
                       operations=["view_status", "view_transcript"])
    assert g.get_grant(grant_id) == narrowed
    assert ack(narrowed, hash_="0" * 64) is AckOutcome.CONFLICT
    for revision in (1, 2):
        with denied():
            check(setup, grant_id, revision, Operation.TRANSCRIPT_PAGE, now=NOW + 20)
    with denied():
        check(setup, grant_id, 1, now=NOW + 20)


def test_revoke_is_immediate_survives_reload_and_is_kept_forever(setup: Setup) -> None:
    record = acked(setup)
    grant_id = record.grant.grant_id
    assert check(setup, grant_id, 1)
    frame = g.revoke_grant(grant_id, now_ms=NOW + 5)
    assert (frame.revision, frame.expected_revision) == (2, 1)
    assert frame.hash == p.record_hash(frame)
    # No ACK ever arrives: the deny is already in force, also after a reload.
    for revision in (1, 2):
        with denied():
            check(setup, grant_id, revision, now=NOW + 6)
    stored = ident.load_registry().grants[grant_id]
    assert stored.grant.revoked_at == NOW + 5 and stored.active is None
    assert stored.mirror is MirrorState.PENDING
    assert g.revoke_grant(grant_id, now_ms=NOW + 50) == frame  # identical tombstone
    with pytest.raises(AiAccessError) as caught:
        g.update_grant(grant_id, expected_revision=2, now_ms=NOW + 60,
                       operations=["view_status"])
    assert caught.value.code == "grant_revoked"
    assert ack(stored) is AckOutcome.APPLIED
    after = g.get_grant(grant_id)
    assert after.mirror is MirrorState.REVOKED and after.grant.revoked_at == NOW + 5
    with denied():
        check(setup, grant_id, 2, now=NOW + 60)
    assert [r.grant.grant_id for r in g.list_grants(setup.export.export_id)] == [grant_id]


def test_revoke_before_ack_of_creation(setup: Setup) -> None:
    record = create(setup)
    g.revoke_grant(record.grant.grant_id, now_ms=NOW + 1)
    assert ack(record) is AckOutcome.STALE  # the late ACK of the put cannot enable it
    with denied():
        check(setup, record.grant.grant_id, 1)


# --------------------------------------------------------------------------- identity rules


def test_expiry_boundary(setup: Setup) -> None:
    record = acked(setup, expires_at=NOW + 1_000)
    grant_id = record.grant.grant_id
    assert check(setup, grant_id, 1, now=NOW + 999)
    with denied(ErrorCode.EXPIRED):
        check(setup, grant_id, 1, now=NOW + 1_000)
    with denied():
        check(setup, grant_id, 1, now=NOW - 1)  # before issued_at


def test_only_the_exact_grantee_device(setup: Setup) -> None:
    record = acked(setup)
    grant_id = record.grant.grant_id
    with denied():
        check(setup, grant_id, 1, who=actor(setup, grant_id, 1, device=uid()))
    with denied():
        check(setup, grant_id, 1, who=actor(setup, grant_id, 1, user=uid()))
    other_grant = uid()
    with denied():
        check(setup, grant_id, 1, who=actor(setup, other_grant, 1))


def test_export_revision_change_makes_old_grants_ineffective(setup: Setup) -> None:
    record = acked(setup)
    grant_id = record.grant.grant_id
    changed = ident.update_export(setup.export.export_id, expected_revision=1,
                                  storage_epoch="f" * 32)
    with denied(ErrorCode.NOT_AVAILABLE):
        check(setup, grant_id, 1)
    frame = ident.publish_export(changed.export_id, expected_revision=2)
    ident.apply_export_ack(p.MirrorAck(export_id=frame.export_id, revision=frame.revision,
                                       hash=frame.hash, state=MirrorState.EFFECTIVE))
    new_target = ident.export_target(ident.get_export(changed.export_id))
    with denied(ErrorCode.STALE_TARGET):
        check(setup, grant_id, 1)  # old target
    with denied(ErrorCode.STALE_TARGET):
        check(setup, grant_id, 1, target=new_target)  # old grant on the new target
    with pytest.raises(AiAccessError) as caught:
        g.update_grant(grant_id, expected_revision=1, now_ms=NOW + 1, operations=["view_status"])
    assert caught.value.code == "stale_target"
    fresh = acked(Setup(setup.registration, changed, new_target, setup.user, setup.device))
    assert check(setup, fresh.grant.grant_id, 1, target=new_target)


def test_publication_switch_and_unpublish_deny(setup: Setup) -> None:
    record = acked(setup)
    grant_id = record.grant.grant_id
    ident.set_publication_enabled(setup.registration.registration_id, False,
                                  expected_revision=2)
    with denied(ErrorCode.NOT_AVAILABLE):
        check(setup, grant_id, 1)
    ident.set_publication_enabled(setup.registration.registration_id, True,
                                  expected_revision=3)
    assert check(setup, grant_id, 1)
    ident.unpublish_export(setup.export.export_id)
    with denied(ErrorCode.NOT_AVAILABLE):
        check(setup, grant_id, 1)


# --------------------------------------------------------------------------- scope and ops


def test_created_by_this_grantee_scope(setup: Setup) -> None:
    grant_id = acked(setup, operations=["view_status", "run_task", "operate_existing"]
                     ).grant.grant_id
    assert check(setup, grant_id, 1, created=True)
    with denied():
        check(setup, grant_id, 1, created=False)
    with denied():  # operate_existing does not widen the default scope
        check(setup, grant_id, 1, Operation.TASK_START, created=False,
              current_policy_digest=POLICY)


def test_selected_resources_scope_and_operate_existing(setup: Setup) -> None:
    grant_id = acked(setup, operations=["view_status", "run_task"],
                     resource_scope="selected_resources", resource_ids=["task_1"]
                     ).grant.grant_id
    assert check(setup, grant_id, 1, created=False)
    with denied():
        check(setup, grant_id, 1, resource="task_2", created=False)
    with denied():  # foreign object: run_task alone is not enough
        check(setup, grant_id, 1, Operation.TASK_START, created=False,
              current_policy_digest=POLICY)
    grant_id = acked(setup, operations=["view_status", "run_task", "operate_existing"],
                     resource_scope="selected_resources", resource_ids=["task_1"]
                     ).grant.grant_id
    assert check(setup, grant_id, 1, Operation.TASK_START, created=False,
                 current_policy_digest=POLICY)


def test_all_resources_in_selected_teams_and_allowlists(setup: Setup) -> None:
    grant_id = acked(setup, resource_scope="all_resources_in_selected_teams",
                     operations=["view_status", "view_transcript"]).grant.grant_id
    assert check(setup, grant_id, 1, resource="local_task", created=False)
    assert check(setup, grant_id, 1, Operation.TRANSCRIPT_PAGE, created=False)
    with denied():
        check(setup, grant_id, 1, team="team-b", created=False)
    with denied():
        check(setup, grant_id, 1, member="producer", created=False)
    with denied():
        check(setup, grant_id, 1, team=None, created=False)
    assert check(setup, grant_id, 1, Operation.CATALOG_LIST, team=None, member=None,
                 resource=None)


def test_derived_rights_are_never_implied(setup: Setup) -> None:
    grant_id = acked(setup, operations=["view_status", "run_task"]).grant.grant_id
    assert check(setup, grant_id, 1, Operation.TASK_START, current_policy_digest=POLICY)
    with denied():
        check(setup, grant_id, 1, Operation.TASK_CREATE, resource=None)
    with denied():
        check(setup, grant_id, 1, Operation.TRANSCRIPT_PAGE)
    with denied():
        check(setup, grant_id, 1, Operation.TASK_CANCEL)
    with denied():
        check(setup, grant_id, 1, Operation.CHAT_SEND, current_policy_digest=POLICY)
    with denied():
        check(setup, grant_id, 1, Operation.STREAM_SUBSCRIBE, topic="transcript")
    assert check(setup, grant_id, 1, Operation.STREAM_SUBSCRIBE, topic="status")
    with denied():
        check(setup, grant_id, 1, "shell.exec")


def test_create_operations_take_no_resource(setup: Setup) -> None:
    grant_id = acked(setup, operations=["view_status", "create_task"]).grant.grant_id
    assert check(setup, grant_id, 1, Operation.TASK_CREATE, resource=None)
    with denied():
        check(setup, grant_id, 1, Operation.TASK_CREATE, resource="task_1")
    with denied():
        check(setup, grant_id, 1, Operation.TASK_CREATE, resource=None, member="other")


def test_policy_digest_mismatch_suspends_runs(setup: Setup) -> None:
    grant_id = acked(setup, operations=["view_status", "run_task", "run_chat"]).grant.grant_id
    assert check(setup, grant_id, 1, Operation.TASK_START, current_policy_digest=POLICY)
    changed = g.policy_digest(backend="claude", model="opus", permission_mode="bypassPermissions",
                              tools_policy=["Read"], mcp_policy={"studio": True}, max_turns=20,
                              limits={"max_jobs": 1})
    assert changed != POLICY
    for op in (Operation.TASK_START, Operation.CHAT_SEND):
        with denied():
            check(setup, grant_id, 1, op, current_policy_digest=changed)
        with denied():
            check(setup, grant_id, 1, op)
    assert check(setup, grant_id, 1, current_policy_digest=changed)  # reads unaffected


def test_policy_digest_is_canonical() -> None:
    a = g.policy_digest(backend="b", model="m", permission_mode="p", tools_policy={"x": 1, "y": 2},
                        mcp_policy=None, max_turns=None, limits={"b": 1, "a": 2})
    b = g.policy_digest(backend="b", model="m", permission_mode="p", tools_policy={"y": 2, "x": 1},
                        mcp_policy=None, max_turns=None, limits={"a": 2, "b": 1})
    assert a == b and len(a) == 64
    with pytest.raises(p.ProtocolError):
        g.policy_digest(backend="b", model="m", permission_mode="p", tools_policy=None,
                        mcp_policy=None, max_turns=None, limits={"budget": 1.5})


# --------------------------------------------------------------------------- capacity


def test_capacity_per_export(setup: Setup) -> None:
    records = [create(setup) for _ in range(p.MAX_GRANTS_PER_EXPORT)]
    with pytest.raises(AiAccessError) as caught:
        create(setup)
    assert caught.value.code == "capacity_exceeded"
    g.revoke_grant(records[0].grant.grant_id, now_ms=NOW + 1)
    assert create(setup).grant.revision == 1
    assert len(g.list_grants(setup.export.export_id)) == p.MAX_GRANTS_PER_EXPORT + 1


def test_delete_export_revokes_grants_and_keeps_tombstones(setup: Setup) -> None:
    record = acked(setup)
    ident.delete_export(setup.export.export_id)
    stored = g.get_grant(record.grant.grant_id)
    assert stored.grant.revoked_at is not None and stored.active is None
    with denied(ErrorCode.NOT_AVAILABLE):
        check(setup, record.grant.grant_id, 1)
