"""The executor's durable command ledger, provenance and dispatch fence (§16.12).

Implements ai-access-design §7 on the project's ``app.db`` (tables in
:mod:`voxarium_studio.models.ai_access`). Not mounted anywhere: the executor admission
path that calls it is a later stage (design §11 S-A3).

Order of checks (design §7.0), and which side owns each step:

1. authentication, current grant/scope and the exact immutable export target — **the
   caller**, always, before this module is reached. A caller that finds the scope no
   longer valid passes ``scope_valid=False``, and a duplicate then gets ``denied``
   rather than the old receipt;
2. ledger lookup — a known ID with the same hash returns its receipt even after the
   executor epoch changed or the 60 s deadline passed; the same ID with another hash is
   ``idempotency_conflict``;
3. only for an unknown ID: current ``executor_epoch``, the issued-at deadline
   (:func:`protocol.check_issued_at`) and the resource revision CAS;
4. one ``synchronous=FULL`` transaction (:meth:`Database.durable_session`) holding the
   ledger row, provenance, audit receipt and the object/message mutation itself; events
   are published by ``after_commit`` only once that commit returned.

For ``task.start``/``chat.send`` the accepted row then gets a durable ``dispatching``
marker and a single-use dispatch token *before* any external start
(:func:`reserve_dispatch`, :func:`claim_dispatch`). :func:`recover` turns every
interrupted dispatch into ``outcome_unknown`` and never dispatches again: a lost start is
possible, a second start is not.

Errors are :class:`LedgerError` with a fixed :class:`protocol.ErrorCode` — never the
payload, a path or the database message.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.db import Database
from ..models.ai_access import (
    AiAuditReceipt,
    AiCommandLedger,
    AiMessageProvenance,
    AiResourceProvenance,
)
from .protocol import (
    CREATING,
    DISPATCHING,
    EXPECTS_RESOURCE_REVISION,
    EXPECTS_RUN_ID,
    MUTATIONS,
    SAFE_RESULT_RETENTION_MS,
    TERMINAL_COMMAND_STATES,
    CommandState,
    ErrorCode,
    ForwardBody,
    Operation,
    ProtocolError,
    RemoteActor,
    ResourceKind,
    Target,
    canonical_hash,
    check_issued_at,
    check_uuid,
)

log = logging.getLogger(__name__)


class LedgerError(ProtocolError):
    """A fixed refusal code (``.code``) and, at most, a fixed ``.reason`` — no echo."""

    def __init__(self, code: ErrorCode | str, reason: str | None = None) -> None:
        super().__init__(str(code))
        self.reason = reason


# --------------------------------------------------------------------------- identity


@dataclass(frozen=True, slots=True)
class LedgerKey:
    """The unique key of design §7.

    It includes the executor registration and the ``export_id`` on purpose: a copied
    ``app.db`` or a republished export never matches the old rows, and the same
    ``command_id`` from another source registration is a different command.
    """

    server_instance_id: str
    executor_registration_id: str
    export_id: str
    source_registration_id: str
    command_id: str

    def __post_init__(self) -> None:
        for value in (
            self.server_instance_id, self.executor_registration_id, self.export_id,
            self.source_registration_id, self.command_id,
        ):
            check_uuid(value)

    @classmethod
    def of(cls, target: Target, source_registration_id: str, command_id: str) -> LedgerKey:
        return cls(
            server_instance_id=target.server_instance_id,
            executor_registration_id=target.executor_registration_id,
            export_id=target.export_id,
            source_registration_id=source_registration_id,
            command_id=command_id,
        )


def payload_hash(
    *,
    op: Operation | str,
    target: Target,
    source_user_id: str,
    source_registration_id: str,
    grant_id: str,
    grant_revision: int,
    issued_at: int,
    executor_epoch: str,
    payload: dict[str, Any],
    expected_resource_revision: int | None = None,
    expected_run_id: str | None = None,
) -> str:
    """SHA-256 of the canonical semantic payload (design §7).

    Covers op, the full target, the source identity, grant id + revision, ``issued_at``,
    ``executor_epoch`` and every precondition; transport ``request_id``/``connection_id``
    are deliberately not inputs, so a resend over a new connection hashes the same.
    """
    return canonical_hash({
        "op": str(op),
        "target": target.model_dump(mode="json"),
        "source_user_id": source_user_id,
        "source_registration_id": source_registration_id,
        "grant_id": grant_id,
        "grant_revision": grant_revision,
        "issued_at": issued_at,
        "executor_epoch": executor_epoch,
        "expected_resource_revision": expected_resource_revision,
        "expected_run_id": expected_run_id,
        "payload": payload,
    })


@dataclass(frozen=True, slots=True)
class Command:
    """Everything the ledger needs about one admitted mutation — no text payload."""

    key: LedgerKey
    actor: RemoteActor
    target: Target
    op: Operation
    payload_hash: str
    executor_epoch: str
    issued_at: int
    resource_kind: ResourceKind | None = None
    """Kind/ID of the existing object a non-creating mutation acts on."""
    resource_id: str | None = None
    expected_resource_revision: int | None = None

    def __post_init__(self) -> None:
        if self.op not in MUTATIONS:
            raise LedgerError(ErrorCode.UNSUPPORTED, "not_a_mutation")
        if self.actor.registration_id != self.key.source_registration_id:
            raise LedgerError(ErrorCode.DENIED, "actor_key_mismatch")
        if self.op not in CREATING and (self.resource_kind is None or self.resource_id is None):
            raise LedgerError(ErrorCode.NOT_AVAILABLE, "resource_required")
        if (self.op in EXPECTS_RESOURCE_REVISION) != (self.expected_resource_revision is not None):
            raise LedgerError(ErrorCode.REVISION_CONFLICT, "expected_revision_shape")

    @classmethod
    def from_forward(
        cls,
        forward: ForwardBody,
        *,
        resource_kind: ResourceKind | None = None,
        resource_id: str | None = None,
    ) -> Command:
        """Build from an authenticated forward frame; the actor comes from Remote, not JSON."""
        request = forward.request
        if request.command_id is None or request.issued_at is None:
            raise LedgerError(ErrorCode.UNSUPPORTED, "not_a_mutation")
        digest = payload_hash(
            op=request.op,
            target=request.target,
            source_user_id=forward.source_user_id,
            source_registration_id=forward.source_registration_id,
            grant_id=request.grant_id,
            grant_revision=request.grant_revision,
            issued_at=request.issued_at,
            executor_epoch=request.expected_executor_epoch,
            payload=request.payload,
            expected_resource_revision=request.expected_resource_revision,
            expected_run_id=request.expected_run_id,
        )
        return cls(
            key=LedgerKey.of(request.target, forward.source_registration_id, request.command_id),
            actor=forward.actor(),
            target=request.target,
            op=request.op,
            payload_hash=digest,
            executor_epoch=request.expected_executor_epoch,
            issued_at=request.issued_at,
            resource_kind=resource_kind,
            resource_id=resource_id,
            expected_resource_revision=request.expected_resource_revision,
        )


@dataclass(frozen=True, slots=True)
class Receipt:
    """What a requester may learn about its own command — states and IDs only."""

    command_id: str
    op: str
    state: CommandState
    error_code: str | None
    resource_kind: str | None
    resource_id: str | None
    message_id: str | None
    run_id: str | None
    safe_result: dict[str, Any] | None
    compacted: bool
    duplicate: bool = False
    """True when this receipt answers a repeated delivery — no effect ran."""

    def to_wire(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "command_id": self.command_id,
            "op": self.op,
            "state": str(self.state),
            "compacted": self.compacted,
        }
        for name in ("error_code", "resource_kind", "resource_id", "message_id", "run_id"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.safe_result is not None:
            result["result"] = self.safe_result
        return result


def _receipt(row: AiCommandLedger, *, duplicate: bool = False) -> Receipt:
    return Receipt(
        command_id=row.command_id,
        op=row.op,
        state=CommandState(row.state),
        error_code=row.error_code,
        resource_kind=row.resource_kind,
        resource_id=row.resource_id,
        message_id=row.message_id,
        run_id=row.run_id,
        safe_result=None if row.compacted else row.safe_result,
        compacted=row.compacted,
        duplicate=duplicate,
    )


@dataclass(slots=True)
class EffectResult:
    """What the object/message mutation reports back, in the same transaction.

    ``safe_result`` must hold IDs/states only; text keys are refused (design §7: the
    ledger never stores a text payload a second time).
    """

    resource_kind: ResourceKind
    resource_id: str
    message_id: str | None = None
    run_id: str | None = None
    safe_result: dict[str, Any] = field(default_factory=dict)


Effect = Callable[[AsyncSession], Awaitable[EffectResult]]
AfterCommit = Callable[[Receipt], Awaitable[None] | None]

_SAFE_RESULT_KEYS = frozenset({
    "resource_id", "resource_kind", "message_id", "run_id", "status", "revision",
    "resource_revision", "team_id", "assignee_id", "member_id", "number",
})


def _check_safe_result(value: dict[str, Any]) -> dict[str, Any]:
    for key, item in value.items():
        if key not in _SAFE_RESULT_KEYS:
            raise LedgerError(ErrorCode.UNSUPPORTED, "unsafe_result_field")
        if isinstance(item, (dict, list)) or (isinstance(item, str) and len(item) > 128):
            raise LedgerError(ErrorCode.UNSUPPORTED, "unsafe_result_value")
    canonical_hash(value)  # integers/strings/bools only — floats refused here
    return dict(value)


# --------------------------------------------------------------------------- lookup


async def _find(session: AsyncSession, key: LedgerKey) -> AiCommandLedger | None:
    result = await session.exec(
        select(AiCommandLedger).where(
            AiCommandLedger.server_instance_id == key.server_instance_id,
            AiCommandLedger.executor_registration_id == key.executor_registration_id,
            AiCommandLedger.export_id == key.export_id,
            AiCommandLedger.source_registration_id == key.source_registration_id,
            AiCommandLedger.command_id == key.command_id,
        )
    )
    return result.first()


def _match(row: AiCommandLedger, digest: str, *, scope_valid: bool) -> Receipt:
    if not scope_valid:
        raise LedgerError(ErrorCode.DENIED)
    if row.payload_hash != digest:
        raise LedgerError(ErrorCode.IDEMPOTENCY_CONFLICT)
    return _receipt(row, duplicate=True)


async def lookup(
    db: Database, key: LedgerKey, digest: str | None = None, *, scope_valid: bool = True,
) -> Receipt | None:
    """Receipt of a known command, ``None`` for an unknown one (design §7.0, ``command.get``).

    ``digest`` given → a resend: the same hash returns the stored receipt, another is
    ``idempotency_conflict``. ``digest`` omitted → ``command.get`` by the original ID.
    ``scope_valid=False`` (grant revoked/expired/out of scope) → ``denied`` whether or not
    the ID is known: a duplicate after revoke never sees its old result.
    """
    if not scope_valid:
        raise LedgerError(ErrorCode.DENIED)
    async with db.session() as session:
        row = await _find(session, key)
    if row is None:
        return None
    if digest is None:
        return _receipt(row, duplicate=True)
    return _match(row, digest, scope_valid=scope_valid)


def check_new_command(
    command: Command, *, current_executor_epoch: str, now_ms: int,
) -> None:
    """Fences that apply only to a command ID the ledger has never seen (design §7.0)."""
    if command.executor_epoch != current_executor_epoch:
        raise LedgerError(ErrorCode.STALE_TARGET, "executor_epoch")
    try:
        check_issued_at(command.issued_at, now_ms)
    except ProtocolError:
        raise LedgerError(ErrorCode.EXPIRED) from None


# --------------------------------------------------------------------------- resource revision


async def _provenance(
    session: AsyncSession, kind: ResourceKind | str, resource_id: str,
) -> AiResourceProvenance | None:
    return await session.get(AiResourceProvenance, (str(kind), resource_id))


async def get_resource_revision(
    session: AsyncSession, kind: ResourceKind | str, resource_id: str,
) -> int:
    """Current durable revision; an object never touched since tracking began is at 1."""
    row = await _provenance(session, kind, resource_id)
    return 1 if row is None else row.revision


async def check_resource_revision(
    session: AsyncSession, kind: ResourceKind | str, resource_id: str, expected: int,
) -> None:
    """CAS of ``expected_resource_revision`` → ``revision_conflict`` (design §7.1)."""
    if await get_resource_revision(session, kind, resource_id) != expected:
        raise LedgerError(ErrorCode.REVISION_CONFLICT)


async def bump_resource_revision(
    session: AsyncSession,
    kind: ResourceKind | str,
    resource_id: str,
    *,
    now_ms: int,
    op: str,
    actor: RemoteActor | None = None,
) -> int:
    """Increase the sidecar in the caller's transaction and record ``last_actor``.

    Every local and remote edit/note/send/start/cancel/status/assignee change must call
    this (design §7.1) — ``actor=None`` is a local change. Returns the new revision.
    """
    row = await _provenance(session, kind, resource_id)
    if row is None:
        row = AiResourceProvenance(
            resource_kind=str(kind), resource_id=resource_id, revision=1, last_at=now_ms,
        )
    row.revision += 1
    _set_last_actor(row, actor, op, now_ms)
    session.add(row)
    return row.revision


def _set_last_actor(
    row: AiResourceProvenance, actor: RemoteActor | None, op: str, now_ms: int,
) -> None:
    row.last_actor_kind = "local" if actor is None else "remote"
    row.last_user_id = None if actor is None else actor.user_id
    row.last_registration_id = None if actor is None else actor.registration_id
    row.last_grant_id = None if actor is None else actor.grant_id
    row.last_grant_revision = None if actor is None else actor.grant_revision
    row.last_command_id = None if actor is None else actor.command_id
    row.last_op = op
    row.last_at = now_ms


def _audit(
    row: AiCommandLedger, outcome: str, now_ms: int, error_code: str | None = None,
) -> AiAuditReceipt:
    return AiAuditReceipt(
        at=now_ms,
        user_id=row.user_id,
        registration_id=row.source_registration_id,
        grant_id=row.grant_id,
        grant_revision=row.grant_revision,
        export_id=row.export_id,
        command_id=row.command_id,
        op=row.op,
        outcome=outcome,
        error_code=error_code,
        resource_kind=row.resource_kind,
        resource_id=row.resource_id,
        run_id=row.run_id,
    )


# --------------------------------------------------------------------------- admission


def _is_unique_violation(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    return isinstance(exc, IntegrityError) and "UNIQUE" in str(orig or "").upper()


async def admit_mutation(
    db: Database,
    command: Command,
    effect: Effect,
    *,
    current_executor_epoch: str,
    now_ms: int,
    scope_valid: bool = True,
    after_commit: AfterCommit | None = None,
) -> Receipt:
    """Ledger row + provenance + audit + the effect, all-or-nothing (design §7.2–7.3).

    ``effect(session)`` performs the fixture object/message mutation in the **same**
    ``synchronous=FULL`` transaction and reports the touched IDs. Any exception in it, a
    refused CAS or a failed durable commit leaves nothing behind — no ledger row without
    an effect and no effect without a ledger row. A repeated delivery returns the stored
    receipt and does not call ``effect``. ``after_commit`` (event publication) runs only
    after the commit returned.

    Dispatching operations end ``accepted`` here, carrying the run id the effect
    reserved; :func:`reserve_dispatch` is the separate durable step before any start.
    """
    if not scope_valid:
        raise LedgerError(ErrorCode.DENIED)
    try:
        receipt, fresh = await _admit(db, command, effect, current_executor_epoch, now_ms)
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise LedgerError(ErrorCode.DENIED, "durable_commit_failed") from None
        # Another writer admitted the same key between our lookup and insert.
        receipt = await lookup(db, command.key, command.payload_hash)
        if receipt is None:  # pragma: no cover - the constraint says it exists
            raise LedgerError(ErrorCode.OUTCOME_UNKNOWN) from None
        return receipt
    except (DBAPIError, sqlite3.Error) as exc:
        # Disk full, I/O error, lock timeout: the durable commit is not confirmed, so
        # nothing may dispatch (design §7). The database message is not echoed.
        log.warning("AI ledger durable commit failed: %s", type(exc).__name__)
        raise LedgerError(ErrorCode.DENIED, "durable_commit_failed") from None
    if fresh and after_commit is not None:
        try:
            outcome = after_commit(receipt)
            if outcome is not None:
                await outcome
        except Exception:  # the commit stands; a failed publish is a stale UI, not a rollback
            log.exception("AI ledger after_commit hook failed")
    return receipt


async def _admit(
    db: Database, command: Command, effect: Effect, current_epoch: str, now_ms: int,
) -> tuple[Receipt, bool]:
    async with db.durable_session() as session:
        existing = await _find(session, command.key)
        if existing is not None:
            return _match(existing, command.payload_hash, scope_valid=True), False

        check_new_command(command, current_executor_epoch=current_epoch, now_ms=now_ms)
        if command.op not in CREATING:
            assert command.resource_kind is not None and command.resource_id is not None
            if command.expected_resource_revision is not None:
                await check_resource_revision(
                    session, command.resource_kind, command.resource_id,
                    command.expected_resource_revision,
                )

        result = await effect(session)
        if command.op not in CREATING and (
            str(result.resource_kind) != str(command.resource_kind)
            or result.resource_id != command.resource_id
        ):
            raise LedgerError(ErrorCode.DENIED, "effect_resource_mismatch")
        if command.op in DISPATCHING and result.run_id is None:
            raise LedgerError(ErrorCode.DENIED, "run_token_required")
        if command.op in EXPECTS_RUN_ID and result.run_id is None:
            raise LedgerError(ErrorCode.DENIED, "run_id_required")
        safe = _check_safe_result(result.safe_result)

        actor = command.actor
        row = AiCommandLedger(
            server_instance_id=command.key.server_instance_id,
            executor_registration_id=command.key.executor_registration_id,
            export_id=command.key.export_id,
            source_registration_id=command.key.source_registration_id,
            command_id=command.key.command_id,
            user_id=actor.user_id,
            grant_id=actor.grant_id,
            grant_revision=actor.grant_revision,
            op=str(command.op),
            payload_hash=command.payload_hash,
            persistent_id=command.target.persistent_id,
            storage_epoch=command.target.storage_epoch,
            export_revision=command.target.export_revision,
            executor_epoch=command.executor_epoch,
            issued_at=command.issued_at,
            resource_kind=str(result.resource_kind),
            resource_id=result.resource_id,
            message_id=result.message_id,
            run_id=result.run_id,
            state=str(CommandState.ACCEPTED),
            safe_result=safe,
            created_at=now_ms,
            updated_at=now_ms,
        )
        session.add(row)

        if command.op in CREATING:
            if await _provenance(session, result.resource_kind, result.resource_id) is not None:
                raise LedgerError(ErrorCode.DENIED, "resource_exists")
            prov = AiResourceProvenance(
                resource_kind=str(result.resource_kind),
                resource_id=result.resource_id,
                revision=1,
                origin_user_id=actor.user_id,
                origin_registration_id=actor.registration_id,
                origin_grant_id=actor.grant_id,
                origin_grant_revision=actor.grant_revision,
                origin_command_id=command.key.command_id,
                origin_export_id=command.key.export_id,
                origin_created_at=now_ms,
                last_at=now_ms,
            )
            _set_last_actor(prov, _with_command(actor, command), str(command.op), now_ms)
            session.add(prov)
        else:
            await bump_resource_revision(
                session, result.resource_kind, result.resource_id,
                now_ms=now_ms, op=str(command.op), actor=_with_command(actor, command),
            )

        if result.message_id is not None:
            session.add(AiMessageProvenance(
                message_id=result.message_id,
                resource_kind=str(result.resource_kind),
                resource_id=result.resource_id,
                user_id=actor.user_id,
                registration_id=actor.registration_id,
                grant_id=actor.grant_id,
                grant_revision=actor.grant_revision,
                command_id=command.key.command_id,
                export_id=command.key.export_id,
                created_at=now_ms,
            ))
        session.add(_audit(row, str(CommandState.ACCEPTED), now_ms))
        await session.flush()
        receipt = _receipt(row)
    return receipt, True


def _with_command(actor: RemoteActor, command: Command) -> RemoteActor:
    if actor.command_id == command.key.command_id:
        return actor
    return RemoteActor(
        user_id=actor.user_id, registration_id=actor.registration_id,
        grant_id=actor.grant_id, grant_revision=actor.grant_revision,
        command_id=command.key.command_id,
    )


# --------------------------------------------------------------------------- dispatch fence


async def _transition(
    db: Database,
    key: LedgerKey,
    *,
    now_ms: int,
    apply: Callable[[AiCommandLedger], str | None],
) -> AiCommandLedger | None:
    """Run ``apply`` on the row under one durable transaction; it returns an audit outcome.

    ``apply`` returning ``None`` means "no change". Returns the row after the change.
    """
    try:
        async with db.durable_session() as session:
            row = await _find(session, key)
            if row is None:
                raise LedgerError(ErrorCode.NOT_AVAILABLE)
            before = (row.state, row.dispatch_claimed)
            outcome = apply(row)
            if outcome is not None:
                row.updated_at = now_ms
                session.add(row)
                session.add(_audit(row, outcome, now_ms, row.error_code))
            elif (row.state, row.dispatch_claimed) != before:  # pragma: no cover - misuse
                raise RuntimeError("transition changed a row without an outcome")
            await session.flush()
            return row
    except LedgerError:
        raise
    except (DBAPIError, sqlite3.Error):
        raise LedgerError(ErrorCode.DENIED, "durable_commit_failed") from None


async def reserve_dispatch(db: Database, key: LedgerKey, run_id: str, *, now_ms: int) -> str:
    """Durably mark an accepted start/send ``dispatching`` and mint its single-use token.

    Must return before any CLI/SDK start (design §7.3). Refused unless the row is a
    dispatching op, still ``accepted`` and reserved for exactly this ``run_id``.
    """
    token = str(uuid.uuid4())

    def apply(row: AiCommandLedger) -> str | None:
        if Operation(row.op) not in DISPATCHING or row.run_id != run_id:
            raise LedgerError(ErrorCode.DENIED, "not_dispatchable")
        if row.state != CommandState.ACCEPTED or row.dispatch_token is not None:
            raise LedgerError(ErrorCode.BUSY, "already_reserved")
        row.state = str(CommandState.DISPATCHING)
        row.dispatch_token = token
        row.dispatching_at = now_ms
        return str(CommandState.DISPATCHING)

    await _transition(db, key, now_ms=now_ms, apply=apply)
    return token


async def claim_dispatch(db: Database, key: LedgerKey, token: str, *, now_ms: int) -> bool:
    """Consume the dispatch token: ``True`` exactly once, ``False`` forever after.

    A conditional UPDATE in a durable transaction, so the claim survives a crash and a
    reopened database still answers ``False``. The runner calls this immediately before
    the external start and starts nothing unless it got ``True``.
    """
    try:
        async with db.durable_session() as session:
            result = await session.exec(  # type: ignore[call-overload]
                update(AiCommandLedger)
                .where(
                    AiCommandLedger.server_instance_id == key.server_instance_id,
                    AiCommandLedger.executor_registration_id == key.executor_registration_id,
                    AiCommandLedger.export_id == key.export_id,
                    AiCommandLedger.source_registration_id == key.source_registration_id,
                    AiCommandLedger.command_id == key.command_id,
                    AiCommandLedger.dispatch_token == token,
                    AiCommandLedger.dispatch_claimed.is_(False),  # type: ignore[attr-defined]
                    AiCommandLedger.state == str(CommandState.DISPATCHING),
                )
                .values(dispatch_claimed=True, updated_at=now_ms)
            )
            return bool(result.rowcount == 1)
    except (DBAPIError, sqlite3.Error):
        return False  # unconfirmed claim → no dispatch


async def mark_running(db: Database, key: LedgerKey, *, now_ms: int) -> Receipt:
    """Record the external start receipt of a claimed dispatch."""

    def apply(row: AiCommandLedger) -> str | None:
        if row.state != CommandState.DISPATCHING or not row.dispatch_claimed:
            raise LedgerError(ErrorCode.BUSY, "not_dispatched")
        row.state = str(CommandState.RUNNING)
        return str(CommandState.RUNNING)

    row = await _transition(db, key, now_ms=now_ms, apply=apply)
    assert row is not None
    return _receipt(row)


async def mark_terminal(
    db: Database,
    key: LedgerKey,
    state: CommandState,
    *,
    now_ms: int,
    error_code: ErrorCode | None = None,
    safe_result: dict[str, Any] | None = None,
) -> Receipt:
    """Move a command to a terminal state once; a terminal row is never rewritten."""
    if state not in TERMINAL_COMMAND_STATES:
        raise LedgerError(ErrorCode.UNSUPPORTED, "not_terminal")
    safe = None if safe_result is None else _check_safe_result(safe_result)

    def apply(row: AiCommandLedger) -> str | None:
        if CommandState(row.state) in TERMINAL_COMMAND_STATES:
            return None
        row.state = str(state)
        row.error_code = None if error_code is None else str(error_code)
        if safe is not None:
            row.safe_result = {**(row.safe_result or {}), **safe}
        row.dispatch_claimed = True  # no token may be claimed after a terminal state
        return str(state)

    row = await _transition(db, key, now_ms=now_ms, apply=apply)
    assert row is not None
    return _receipt(row)


async def deny_before_dispatch(db: Database, key: LedgerKey, *, now_ms: int) -> bool:
    """Lease/grant loss before the external start → ``denied_before_dispatch`` (design §7.4).

    Only a row whose token has not been claimed can be denied; ``False`` means the start
    already happened (or the command is terminal) and the job continues locally.
    """
    denied = False

    def apply(row: AiCommandLedger) -> str | None:
        nonlocal denied
        if row.dispatch_claimed or row.state not in (
            CommandState.ACCEPTED, CommandState.DISPATCHING,
        ) or Operation(row.op) not in DISPATCHING:
            return None
        row.state = str(CommandState.DENIED_BEFORE_DISPATCH)
        row.error_code = str(ErrorCode.DENIED)
        row.dispatch_claimed = True
        denied = True
        return str(CommandState.DENIED_BEFORE_DISPATCH)

    await _transition(db, key, now_ms=now_ms, apply=apply)
    return denied


async def recover(db: Database, *, now_ms: int) -> list[Receipt]:
    """Startup recovery: every interrupted dispatch becomes ``outcome_unknown`` (design §7.5).

    Rows still ``dispatching``, and accepted start/send rows that never reached their
    external start receipt, keep their resource and run IDs, have their token fenced and
    are returned for the local owner to reconcile. Nothing is ever dispatched again.
    """
    dispatch_ops = [str(op) for op in DISPATCHING]
    try:
        async with db.durable_session() as session:
            rows = list((await session.exec(
                select(AiCommandLedger).where(
                    AiCommandLedger.op.in_(dispatch_ops),  # type: ignore[attr-defined]
                    AiCommandLedger.state.in_([  # type: ignore[attr-defined]
                        str(CommandState.ACCEPTED), str(CommandState.DISPATCHING),
                    ]),
                )
            )).all())
            for row in rows:
                row.state = str(CommandState.OUTCOME_UNKNOWN)
                row.error_code = str(ErrorCode.OUTCOME_UNKNOWN)
                row.dispatch_claimed = True
                row.updated_at = now_ms
                session.add(row)
                session.add(_audit(row, str(CommandState.OUTCOME_UNKNOWN), now_ms, row.error_code))
            await session.flush()
            return [_receipt(row) for row in rows]
    except (DBAPIError, sqlite3.Error):
        raise LedgerError(ErrorCode.DENIED, "durable_commit_failed") from None


# --------------------------------------------------------------------------- tombstones


async def compact(
    db: Database, *, now_ms: int, retention_ms: int = SAFE_RESULT_RETENTION_MS,
) -> int:
    """Clear ``safe_result`` of terminal rows older than the retention (design §7).

    Never deletes a row: the key, ``payload_hash``, IDs and state stay as a dedupe
    tombstone for the life of the export, so a late duplicate still gets idempotency.
    Returns the number of rows compacted.
    """
    async with db.durable_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(AiCommandLedger)
            .where(
                AiCommandLedger.created_at < now_ms - retention_ms,
                AiCommandLedger.compacted.is_(False),  # type: ignore[attr-defined]
                AiCommandLedger.state.in_(  # type: ignore[attr-defined]
                    [str(state) for state in TERMINAL_COMMAND_STATES]
                ),
            )
            .values(safe_result=None, compacted=True)
        )
        return int(result.rowcount or 0)

