"""Local AI-access consent: grants, mirror ordering and the effective check (§16.12).

Contract: ``.voxarium_studio/docs/remote-identity/ai-access-design.md`` §4, §6, §7, §9.
Grants live in the same private registry as exports (:mod:`.identity`), never in the
managed project. Nothing is granted by default: no registration, export or grant — deny.

Ordering (ai-access-design §4 step 5, §6 "Lifecycle"):

* **Expansion** (a new grant, or an update that adds operations, teams, members,
  resources, a wider scope, a later expiry or another execution policy) is saved as
  ``pending``. The PC keeps honouring only the last acknowledged revision; the new one
  becomes effective only after :func:`apply_grant_ack` with the same revision and hash.
* **Narrowing and revoke** are applied locally *first*: the record's honoured revision
  moves at once (a revoke clears it), then the mirror frame is built. A CAS conflict or a
  lost ACK never restores the previous rights. Revoke tombstones are kept forever.
* A grant update increments only ``grant.revision``; ``export_revision`` belongs to the
  export (:func:`.identity.update_export`) and a change there makes every grant bound to
  the old target ineffective.

A narrowed revision is honoured only after its ACK too: until then Remote still forwards
the old revision, which no longer matches — the check fails closed, never open.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from . import protocol
from .identity import (
    AckOutcome,
    AiAccessConflict,
    AiAccessError,
    GrantRecord,
    RegistryStore,
    export_published,
    export_target,
    load_registry,
    mutate_registry,
    replace_model,
)
from .protocol import (
    ErrorCode,
    Grant,
    GrantPut,
    GrantRevoke,
    MirrorAck,
    MirrorState,
    Operation,
    Permission,
    ProtocolError,
    RemoteActor,
    ResourceScope,
    Target,
)


class GrantDenied(AiAccessError):
    """The effective check refused. ``code`` is a per-request :class:`ErrorCode` value."""


# --------------------------------------------------------------------------- policy digest


def policy_digest(
    *, backend: str, model: str, permission_mode: str, tools_policy: Any, mcp_policy: Any,
    max_turns: int | None, limits: dict[str, int] | None = None,
) -> str:
    """``execution_policy_digest``: SHA-256 of the canonical execution policy (§4).

    Compared before every run dispatch; a changed policy suspends run permissions until a
    new local consent — it never widens a grant on its own.
    """
    return protocol.canonical_hash({
        "backend": backend, "model": model, "permission_mode": permission_mode,
        "tools_policy": tools_policy, "mcp_policy": mcp_policy, "max_turns": max_turns,
        "limits": dict(limits or {}),
    })


# --------------------------------------------------------------------------- frames


def _put_semantics(grant: Grant) -> dict[str, Any]:
    return {
        "export_id": grant.target.export_id, "export_revision": grant.target.export_revision,
        "grant_id": grant.grant_id, "expected_revision": grant.revision - 1,
        "revision": grant.revision, "grant": grant.model_dump(mode="json"),
    }


def _revoke_semantics(grant: Grant) -> dict[str, Any]:
    return {
        "export_id": grant.target.export_id, "export_revision": grant.target.export_revision,
        "grant_id": grant.grant_id, "expected_revision": grant.revision - 1,
        "revision": grant.revision,
    }


def grant_put_frame(grant: Grant) -> GrantPut:
    """``grant.put`` with the full strict grant; hash over the record without ``hash``."""
    if grant.revoked_at is not None:
        raise AiAccessError("grant_revoked")
    semantics = _put_semantics(grant)
    return GrantPut.from_wire({**semantics, "hash": protocol.record_hash(semantics)})


def grant_revoke_frame(grant: Grant) -> GrantRevoke:
    """``grant.revoke`` — a tombstone without content (§6)."""
    if grant.revoked_at is None:
        raise AiAccessError("grant_not_revoked")
    semantics = _revoke_semantics(grant)
    return GrantRevoke.from_wire({**semantics, "hash": protocol.record_hash(semantics)})


def mirror_frame(record: GrantRecord) -> GrantPut | GrantRevoke:
    """The frame that mirrors the record's latest revision (identical on every resend)."""
    if record.grant.revoked_at is not None:
        return grant_revoke_frame(record.grant)
    return grant_put_frame(record.grant)


def _frame_hash(grant: Grant) -> str:
    semantics = _revoke_semantics(grant) if grant.revoked_at is not None else _put_semantics(grant)
    return protocol.record_hash(semantics)


# --------------------------------------------------------------------------- comparison


def _scope_change(old: Grant, new: Grant) -> tuple[bool, bool]:
    before, after = old.resource_scope, new.resource_scope
    if before is after:
        if before is ResourceScope.SELECTED_RESOURCES:
            return (bool(set(new.resource_ids) - set(old.resource_ids)),
                    bool(set(old.resource_ids) - set(new.resource_ids)))
        return False, False
    if after is ResourceScope.ALL_RESOURCES_IN_SELECTED_TEAMS:
        return True, False
    if before is ResourceScope.ALL_RESOURCES_IN_SELECTED_TEAMS:
        return False, True
    return True, True  # own creations <-> an explicit list: each side has something new


def compare_grants(old: Grant, new: Grant) -> tuple[bool, bool]:
    """``(expands, narrows)`` of *new* relative to *old*; both may be true (§4)."""
    expands = narrows = False
    for before, after in ((old.operations, new.operations), (old.team_ids, new.team_ids),
                          (old.member_ids, new.member_ids)):
        expands |= bool(set(after) - set(before))
        narrows |= bool(set(before) - set(after))
    scope_expands, scope_narrows = _scope_change(old, new)
    expands |= scope_expands or new.expires_at > old.expires_at
    narrows |= scope_narrows or new.expires_at < old.expires_at
    # An update re-issues the consent at "now": a later issued_at only removes the past,
    # so it is not a narrowing that should suspend the acknowledged rights.
    if new.issued_at < old.issued_at:
        expands = True
    if new.execution_policy_digest != old.execution_policy_digest:
        expands = narrows = True
    return expands, narrows


def _next_active(active: Grant | None, new: Grant) -> Grant | None:
    if active is None:
        return None
    expands, narrows = compare_grants(active, new)
    if not expands:
        return new  # narrowing: the old rights end now
    if not narrows:
        return active  # pure expansion: keep the acknowledged rights until the ACK
    return None  # mixed: neither the old nor the new set may be honoured yet


# --------------------------------------------------------------------------- mutations


def _grant(data: dict[str, Any]) -> Grant:
    try:
        return Grant.from_wire(data)
    except (ValidationError, ProtocolError):
        raise AiAccessError("invalid_grant") from None


def _published_export(store: RegistryStore, export_id: str) -> Target:
    export = store.exports.get(export_id)
    if export is None or export.deleted:
        raise AiAccessError("export_not_found")
    if not export_published(store, export):
        raise AiAccessError("export_not_published")
    return export_target(export)


def _ids(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise AiAccessError("invalid_grant")
    return list(values)


def create_grant(
    export_id: str, *, grantee_user_id: str, grantee_registration_id: str,
    operations: Iterable[Permission | str], team_ids: Iterable[str],
    member_ids: Iterable[str], execution_policy_digest: str, now_ms: int,
    resource_scope: ResourceScope | str = ResourceScope.CREATED_BY_THIS_GRANTEE,
    resource_ids: Iterable[str] = (), expires_at: int | None = None,
    grant_id: str | None = None,
) -> GrantRecord:
    """Save explicit consent for one exact grantee device as ``pending`` (§4).

    No wildcards: one Remote ``user_id`` + ``registration_id``, at least one team and
    member. Expiry defaults to 24 h and may not exceed 30 days. The target is the
    export's current, published target. Effective only after a matching ACK.
    """
    try:
        issued = protocol.check_timestamp(now_ms)
        chosen = str(uuid.uuid4()) if grant_id is None else protocol.check_uuid(grant_id)
        data = {
            "grant_id": chosen, "revision": 1,
            "grantee_user_id": grantee_user_id,
            "grantee_registration_id": grantee_registration_id,
            "operations": [str(Permission(op)) for op in _ids(operations)],
            "team_ids": _ids(team_ids), "member_ids": _ids(member_ids),
            "resource_scope": str(ResourceScope(resource_scope)),
            "resource_ids": _ids(resource_ids),
            "execution_policy_digest": execution_policy_digest,
            "issued_at": issued,
            "expires_at": issued + protocol.GRANT_DEFAULT_MS if expires_at is None
            else expires_at,
        }
    except (ValueError, TypeError):  # ProtocolError and unknown enum values: no echo
        raise AiAccessError("invalid_grant") from None

    def change(store: RegistryStore) -> tuple[RegistryStore | None, GrantRecord]:
        target = _published_export(store, protocol.check_uuid(export_id))
        if chosen in store.grants:
            raise AiAccessError("grant_exists")
        live = sum(1 for record in store.grants.values()
                   if record.grant.target.export_id == target.export_id
                   and record.grant.revoked_at is None)
        if live >= protocol.MAX_GRANTS_PER_EXPORT:
            raise AiAccessError(ErrorCode.CAPACITY_EXCEEDED)
        grant = _grant({**data, "target": target.model_dump(mode="json")})
        record = GrantRecord(grant=grant, mirror=MirrorState.PENDING,
                             mirror_hash=_frame_hash(grant))
        return replace_model(store, grants={**store.grants, chosen: record}), record

    try:
        return mutate_registry(change)
    except (ValueError, ProtocolError) as exc:
        if isinstance(exc, AiAccessError):
            raise
        raise AiAccessError("invalid_grant") from None


_UPDATABLE = frozenset({
    "operations", "team_ids", "member_ids", "resource_scope", "resource_ids",
    "execution_policy_digest", "expires_at",
})


def update_grant(
    grant_id: str, *, expected_revision: int, now_ms: int, **changes: Any,
) -> GrantRecord:
    """CAS update of one grant: only ``grant.revision`` increments (§4, §6).

    ``issued_at`` becomes *now_ms* (a renewed consent). The target must still be the
    export's current published target — a fenced export needs a new grant.
    """
    if set(changes) - _UPDATABLE:
        raise AiAccessError("invalid_grant")
    try:
        issued = protocol.check_timestamp(now_ms)
    except ProtocolError:
        raise AiAccessError("invalid_grant") from None

    def change(store: RegistryStore) -> tuple[RegistryStore | None, GrantRecord]:
        record = store.grants.get(protocol.check_uuid(grant_id))
        if record is None:
            raise AiAccessError("grant_not_found")
        current = record.grant
        if current.revoked_at is not None:
            raise AiAccessError("grant_revoked")
        if current.revision != expected_revision:
            raise AiAccessConflict()
        target = _published_export(store, current.target.export_id)
        if target != current.target:
            raise AiAccessError(ErrorCode.STALE_TARGET)
        data = current.model_dump(mode="json")
        for key, value in changes.items():
            if key == "operations":
                value = [str(Permission(op)) for op in _ids(value)]
            elif key == "resource_scope":
                value = str(ResourceScope(value))
            elif key in {"team_ids", "member_ids", "resource_ids"}:
                value = _ids(value)
            data[key] = value
        data.update(revision=current.revision + 1, issued_at=issued)
        grant = _grant(data)
        updated = GrantRecord(
            grant=grant, active=_next_active(record.active, grant),
            mirror=MirrorState.PENDING, mirror_hash=_frame_hash(grant),
            acked_revision=record.acked_revision,
        )
        return replace_model(store, grants={**store.grants, grant.grant_id: updated}), updated

    try:
        return mutate_registry(change)
    except (ValueError, ProtocolError) as exc:
        if isinstance(exc, AiAccessError):
            raise
        raise AiAccessError("invalid_grant") from None


def revoked_record(record: GrantRecord, *, now_ms: int) -> GrantRecord:
    """Pure: the revoke tombstone of *record* (revision+1, ``revoked_at``, no rights)."""
    if record.grant.revoked_at is not None:
        return record
    revoked_at = max(protocol.check_timestamp(now_ms), record.grant.issued_at)
    grant = replace_model(record.grant, revision=record.grant.revision + 1,
                          revoked_at=revoked_at)
    return GrantRecord(grant=grant, active=None, mirror=MirrorState.PENDING,
                       mirror_hash=_frame_hash(grant), acked_revision=record.acked_revision)


def revoke_grant(grant_id: str, *, now_ms: int) -> GrantRevoke:
    """Local deny first, then the mirror frame (§6, §9). No CAS: a revoke never loses.

    Repeating returns the identical tombstone frame; the tombstone is never deleted.
    """

    def change(store: RegistryStore) -> tuple[RegistryStore | None, GrantRevoke]:
        record = store.grants.get(protocol.check_uuid(grant_id))
        if record is None:
            raise AiAccessError("grant_not_found")
        updated = revoked_record(record, now_ms=now_ms)
        frame = grant_revoke_frame(updated.grant)
        if updated is record:
            return None, frame
        return replace_model(store, grants={**store.grants, grant_id: updated}), frame

    try:
        return mutate_registry(change)
    except ProtocolError:
        raise AiAccessError("invalid_grant") from None


def apply_grant_ack(ack: MirrorAck) -> AckOutcome:
    """Make the latest revision effective only on a matching ACK (§6).

    Same export, grant, revision and hash, and the state that revision implies
    (``effective`` for a put, ``revoked`` for a revoke) → applied. Equal revision with
    another hash or state, or a revision never sent → conflict: nothing widens. Lower →
    stale, no change.
    """
    if ack.grant_id is None:
        raise AiAccessError("ack_mismatch")

    def change(store: RegistryStore) -> tuple[RegistryStore | None, AckOutcome]:
        record = store.grants.get(ack.grant_id)
        if record is None or record.grant.target.export_id != ack.export_id:
            raise AiAccessError("ack_mismatch")
        grant = record.grant
        if ack.revision < grant.revision:
            return None, AckOutcome.STALE
        revoked = grant.revoked_at is not None
        expected_state = MirrorState.REVOKED if revoked else MirrorState.EFFECTIVE
        if (ack.revision > grant.revision or ack.hash != record.mirror_hash
                or ack.state is not expected_state):
            if record.mirror is MirrorState.CONFLICT:
                return None, AckOutcome.CONFLICT
            updated = replace_model(record, mirror=MirrorState.CONFLICT)
            return (replace_model(store, grants={**store.grants, ack.grant_id: updated}),
                    AckOutcome.CONFLICT)
        if record.acked_revision == grant.revision and record.mirror is expected_state:
            return None, AckOutcome.DUPLICATE
        updated = GrantRecord(
            grant=grant, active=None if revoked else grant, mirror=expected_state,
            mirror_hash=record.mirror_hash, acked_revision=grant.revision,
        )
        return (replace_model(store, grants={**store.grants, ack.grant_id: updated}),
                AckOutcome.APPLIED)

    return mutate_registry(change)


def get_grant(grant_id: str) -> GrantRecord | None:
    return load_registry().grants.get(protocol.check_uuid(grant_id))


def list_grants(export_id: str) -> list[GrantRecord]:
    export_id = protocol.check_uuid(export_id)
    return sorted(
        (r for r in load_registry().grants.values() if r.grant.target.export_id == export_id),
        key=lambda r: r.grant.grant_id,
    )


# --------------------------------------------------------------------------- effective check

_UNSCOPED_OPS = frozenset({
    Operation.CATALOG_LIST, Operation.RESOURCE_LIST, Operation.COMMAND_GET,
    Operation.STREAM_UNSUBSCRIBE,
})


def effective_grant(
    grant_id: str, *, now_ms: int, target: Target, actor: RemoteActor, op: Operation | str,
    team_id: str | None = None, member_id: str | None = None,
    resource_id: str | None = None, created_by_grantee: bool = False,
    topic: str | None = None, current_policy_digest: str | None = None,
) -> Grant:
    """The honoured grant for one request on this PC, or :class:`GrantDenied` (§4–§7).

    Reads the private registry; a corrupt or missing store denies (the store error is
    an :class:`AiAccessError` as well). See :func:`check_grant` for the rules.
    """
    return check_grant(
        load_registry(), grant_id, now_ms=now_ms, target=target, actor=actor, op=op,
        team_id=team_id, member_id=member_id, resource_id=resource_id,
        created_by_grantee=created_by_grantee, topic=topic,
        current_policy_digest=current_policy_digest,
    )


def check_grant(
    store: RegistryStore, grant_id: str, *, now_ms: int, target: Target, actor: RemoteActor,
    op: Operation | str, team_id: str | None = None, member_id: str | None = None,
    resource_id: str | None = None, created_by_grantee: bool = False,
    topic: str | None = None, current_policy_digest: str | None = None,
) -> Grant:
    """Pure effective check against an already loaded registry snapshot.

    In order: the export is published, enabled and acknowledged on a registration with
    publication on; the target matches exactly, including ``export_revision``; the grant
    is not revoked, its honoured revision was acknowledged and equals the actor's; the
    actor is the exact grantee user **and** device; ``now < expires_at``; every required
    permission (derived rights never implied, ``operate_existing`` for foreign objects)
    is granted; team, member and resource are inside the allowlist and scope; run
    operations need the grant's execution policy digest to equal the current one.
    """

    def deny(code: ErrorCode = ErrorCode.DENIED) -> GrantDenied:
        return GrantDenied(code)

    try:
        operation = Operation(op)
        protocol.check_timestamp(now_ms)
    except (ValueError, ProtocolError):
        raise deny() from None
    if not isinstance(actor, RemoteActor) or actor.grant_id != grant_id:
        raise deny()
    record = store.grants.get(grant_id) if isinstance(grant_id, str) else None
    if record is None:
        raise deny(ErrorCode.NOT_AVAILABLE)
    export = store.exports.get(record.grant.target.export_id)
    if export is None or not export_published(store, export):
        raise deny(ErrorCode.NOT_AVAILABLE)
    if target != export_target(export) or record.grant.target != target:
        raise deny(ErrorCode.STALE_TARGET)
    grant = record.grant
    if grant.revoked_at is not None:
        raise deny()
    if (actor.user_id, actor.registration_id) != (
        grant.grantee_user_id, grant.grantee_registration_id,
    ):
        raise deny()
    active = record.active
    if active is None or active.revision > record.acked_revision:
        raise deny()
    if actor.grant_revision != active.revision:
        raise deny()
    if active.expired(now_ms):
        raise deny(ErrorCode.EXPIRED)
    if not active.effective(now_ms):
        raise deny()

    creating = operation in protocol.CREATING
    if operation not in _UNSCOPED_OPS:
        if team_id is None or member_id is None:
            raise deny()
        if creating != (resource_id is None):
            raise deny()
    if team_id is not None and team_id not in active.team_ids:
        raise deny()
    if member_id is not None and member_id not in active.member_ids:
        raise deny()
    if resource_id is not None:
        scope = active.resource_scope
        if scope is ResourceScope.CREATED_BY_THIS_GRANTEE and created_by_grantee is not True:
            raise deny()
        if scope is ResourceScope.SELECTED_RESOURCES and resource_id not in active.resource_ids:
            raise deny()
    existing_foreign = resource_id is not None and created_by_grantee is not True
    needed = protocol.required_permissions(operation, existing_foreign=existing_foreign,
                                           topic=topic)
    if not needed <= set(active.operations):
        raise deny()
    if operation in protocol.DISPATCHING and (
        current_policy_digest is None
        or current_policy_digest != active.execution_policy_digest
    ):
        raise deny()
    return active
