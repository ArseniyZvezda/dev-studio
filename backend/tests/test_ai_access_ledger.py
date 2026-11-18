"""AI access command ledger, provenance and durable schema (ai-access-design §3, §4, §7; §16.12).

Every database here is a fixture under ``tmp_path`` — never a real project's ``app.db``.
The "effect" is a fixture task/chat-message mutation, and the "backend" is a dispatch
counter: no AI, network or server is involved.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from voxarium_studio.ai_access import ledger
from voxarium_studio.ai_access.ledger import (
    Command,
    EffectResult,
    LedgerError,
    LedgerKey,
    payload_hash,
)
from voxarium_studio.ai_access.protocol import (
    SAFE_RESULT_RETENTION_MS,
    CommandState,
    ErrorCode,
    Operation,
    RemoteActor,
    ResourceKind,
    Target,
)
from voxarium_studio.core.db import Database
from voxarium_studio.models import (
    AgentChat,
    AgentChatMessage,
    AiAuditReceipt,
    AiCommandLedger,
    AiMessageProvenance,
    AiResourceProvenance,
    Team,
    TeamMember,
    TeamTask,
)
from voxarium_studio.models.ai_access import LEDGER_KEY_INDEX

NOW = 1_800_000_000_000
EPOCH = str(uuid.uuid4())
CANARY_PROMPT = "CANARY-PROMPT-7f3a do the secret thing"
CANARY_KEY = "sk-ant-api03-CANARYKEY0123456789abcdef"

TARGET = Target(
    server_instance_id=str(uuid.uuid4()),
    executor_registration_id=str(uuid.uuid4()),
    export_id=str(uuid.uuid4()),
    persistent_id="voxel-game",
    storage_epoch="0" * 31 + "1",
    export_revision=1,
)
USER = str(uuid.uuid4())
SOURCE = str(uuid.uuid4())
GRANT = str(uuid.uuid4())


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "fixture-app.db")
    await database.create_all()
    await database.create_missing_indexes()
    async with database.session() as session:
        session.add(Team(id="team_a", name="A"))
        await session.flush()
        session.add(TeamMember(id="mbr_a", team_id="team_a", name="Dev", role_title="Developer"))
        await session.flush()
        session.add(AgentChat(id="chat_a", member_id="mbr_a", title="Chat"))
        session.add(TeamTask(
            id="task_x", number=1, team_id="team_a", assignee_id="mbr_a", title="Existing",
        ))
    yield database
    await database.close()


def command(
    op: Operation,
    payload: dict[str, Any],
    *,
    command_id: str | None = None,
    source: str = SOURCE,
    target: Target = TARGET,
    epoch: str = EPOCH,
    issued_at: int = NOW,
    resource: tuple[ResourceKind, str] | None = None,
    expected_revision: int | None = None,
) -> Command:
    command_id = command_id or str(uuid.uuid4())
    actor = RemoteActor(
        user_id=USER, registration_id=source, grant_id=GRANT, grant_revision=1,
        command_id=command_id,
    )
    digest = payload_hash(
        op=op, target=target, source_user_id=USER, source_registration_id=source,
        grant_id=GRANT, grant_revision=1, issued_at=issued_at, executor_epoch=epoch,
        payload=payload, expected_resource_revision=expected_revision,
    )
    return Command(
        key=LedgerKey.of(target, source, command_id),
        actor=actor,
        target=target,
        op=op,
        payload_hash=digest,
        executor_epoch=epoch,
        issued_at=issued_at,
        resource_kind=None if resource is None else resource[0],
        resource_id=None if resource is None else resource[1],
        expected_resource_revision=expected_revision,
    )


class Fixture:
    """Fixture effects and a fake backend that counts dispatches."""

    def __init__(self) -> None:
        self.effects = 0
        self.dispatched = 0
        self.seen_synchronous: list[int] = []

    def create_task(self, title: str, brief: str):
        async def effect(session: AsyncSession) -> EffectResult:
            self.effects += 1
            level = (await session.exec(text("PRAGMA synchronous"))).scalar()  # type: ignore[call-overload]
            self.seen_synchronous.append(level)
            task = TeamTask(
                team_id="team_a", assignee_id="mbr_a", title=title, brief=brief,
                number=100 + self.effects, auto_start=False,
            )
            session.add(task)
            await session.flush()
            return EffectResult(
                resource_kind=ResourceKind.TASK, resource_id=task.id,
                safe_result={"status": "todo"},
            )
        return effect

    def chat_send(self, body: str, *, fail: bool = False):
        async def effect(session: AsyncSession) -> EffectResult:
            self.effects += 1
            message = AgentChatMessage(chat_id="chat_a", member_id="mbr_a", role="user", text=body)
            session.add(message)
            await session.flush()
            if fail:
                raise RuntimeError("effect failed after writing")
            return EffectResult(
                resource_kind=ResourceKind.CHAT, resource_id="chat_a",
                message_id=message.id, run_id=str(uuid.uuid4()),
            )
        return effect

    def edit_task(self, task_id: str):
        async def effect(session: AsyncSession) -> EffectResult:
            self.effects += 1
            task = await session.get(TeamTask, task_id)
            assert task is not None
            task.title = "Edited"
            session.add(task)
            return EffectResult(resource_kind=ResourceKind.TASK, resource_id=task_id)
        return effect

    async def run(self, db: Database, key: LedgerKey, token: str) -> bool:
        """Fake runner: start externally only on a successful single-use claim."""
        if await ledger.claim_dispatch(db, key, token, now_ms=NOW):
            self.dispatched += 1
            return True
        return False


async def count(db: Database, model: Any) -> int:
    async with db.session() as session:
        return len(list((await session.exec(select(model))).all()))


async def admit(db: Database, cmd: Command, effect, **kw: Any) -> ledger.Receipt:
    return await ledger.admit_mutation(
        db, cmd, effect, current_executor_epoch=kw.pop("epoch", EPOCH),
        now_ms=kw.pop("now_ms", NOW), **kw,
    )


def chat_send_cmd(body: str = "hello", **kw: Any) -> Command:
    return command(
        Operation.CHAT_SEND, {"resource_id": "chat_a", "text": body},
        resource=(ResourceKind.CHAT, "chat_a"), expected_revision=kw.pop("expected_revision", 1),
        **kw,
    )


# --------------------------------------------------------------------------- schema


def _index_names(path: Path, table: str) -> dict[str, int]:
    with sqlite3.connect(path) as conn:
        return {row[1]: row[2] for row in conn.execute(f"PRAGMA index_list('{table}')")}


def _ai_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ai_%'"
            )
        }


EXPECTED_TABLES = {
    "ai_command_ledger", "ai_resource_provenance", "ai_message_provenance", "ai_audit_receipt",
}


async def test_fresh_database_gets_ai_tables_and_unique_key(db: Database) -> None:
    assert _ai_tables(db.path) == EXPECTED_TABLES
    assert _index_names(db.path, "ai_command_ledger")[LEDGER_KEY_INDEX] == 1  # unique
    with sqlite3.connect(db.path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info('ai_command_ledger')")}
    assert not {c for c in columns if any(w in c for w in ("secret", "token_hash", "key", "text"))}


async def test_old_fixture_database_gains_ai_tables_without_touching_rows(tmp_path: Path) -> None:
    path = tmp_path / "old-app.db"
    old_tables = [t for t in SQLModel.metadata.sorted_tables if not t.name.startswith("ai_")]
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    SQLModel.metadata.create_all(engine, tables=old_tables)
    with Session(engine) as session:
        session.add(Team(id="team_old", name="Old"))
        session.flush()
        session.add(TeamMember(id="mbr_old", team_id="team_old", name="M", role_title="R"))
        session.flush()
        session.add(TeamTask(
            id="task_old", number=7, team_id="team_old", assignee_id="mbr_old",
            title="Old task", brief="old brief",
        ))
        session.add(AgentChat(id="chat_old", member_id="mbr_old", title="Old chat"))
        session.commit()
    engine.dispose()
    assert _ai_tables(path) == set()

    def snapshot() -> list[tuple]:
        with sqlite3.connect(path) as conn:
            return [
                tuple(conn.execute(f"SELECT * FROM {name} ORDER BY id").fetchall())
                for name in ("team", "team_member", "team_task", "agent_chat")
            ]

    before = snapshot()
    database = Database(path)
    await database.create_all()
    await database.create_missing_indexes()
    await database.close()

    assert _ai_tables(path) == EXPECTED_TABLES
    assert _index_names(path, "ai_command_ledger")[LEDGER_KEY_INDEX] == 1
    assert snapshot() == before
    with sqlite3.connect(path) as conn:
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"trg_ai_audit_receipt_no_update", "trg_ai_audit_receipt_no_delete"} <= triggers

    # A second pass is a no-op.
    database = Database(path)
    await database.create_all()
    await database.create_missing_indexes()
    await database.close()
    assert snapshot() == before


# --------------------------------------------------------------------------- durability


async def test_durable_session_runs_full_and_restores_normal(db: Database) -> None:
    async with db.durable_session() as session:
        inside = (await session.exec(text("PRAGMA synchronous"))).scalar()  # type: ignore[call-overload]
        raw = (await session.connection()).sync_connection.connection.dbapi_connection
        assert session.info["synchronous"] == 2
        assert inside == 2
    assert session.info["restored"] == 1
    # The same pooled connection is back at NORMAL.
    async with db.session() as plain:
        conn = await plain.connection()
        assert conn.sync_connection.connection.dbapi_connection is raw
        assert (await conn.exec_driver_sql("PRAGMA synchronous")).scalar() == 1


async def test_durable_session_restores_normal_after_an_error(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.durable_session() as session:
            session.add(Team(id="team_rolled_back", name="X"))
            await session.flush()
            raise RuntimeError("boom")
    assert session.info["restored"] == 1
    async with db.session() as plain:
        assert await plain.get(Team, "team_rolled_back") is None


async def test_durable_session_holds_the_write_lock(db: Database) -> None:
    async with db.durable_session():
        other = sqlite3.connect(db.path, timeout=0.2)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("INSERT INTO team (id, name, description, autostart_tasks, created_at)"
                              " VALUES ('t2', 'n', '', 0, '2026-01-01')")
        finally:
            other.close()


# --------------------------------------------------------------------------- atomic admission


async def test_create_commits_ledger_provenance_audit_and_effect_together(db: Database) -> None:
    fx = Fixture()
    seen_after_commit: list[ledger.Receipt] = []

    async def after_commit(receipt: ledger.Receipt) -> None:
        # Published only once the commit is visible to another session.
        async with db.session() as session:
            assert await session.get(TeamTask, receipt.resource_id) is not None
        seen_after_commit.append(receipt)

    cmd = command(Operation.TASK_CREATE, {"title": "Draft", "brief": "b"})
    receipt = await admit(db, cmd, fx.create_task("Draft", "b"), after_commit=after_commit)

    assert receipt.state is CommandState.ACCEPTED and not receipt.duplicate
    assert fx.seen_synchronous == [2]  # FULL on the very connection the effect wrote with
    assert seen_after_commit == [receipt]
    async with db.session() as session:
        row = (await session.exec(select(AiCommandLedger))).one()
        assert (row.command_id, row.export_id, row.source_registration_id) == (
            cmd.key.command_id, TARGET.export_id, SOURCE,
        )
        assert row.safe_result == {"status": "todo"}
        prov = await session.get(AiResourceProvenance, ("task", receipt.resource_id))
        assert prov is not None and prov.revision == 1
        assert (prov.origin_user_id, prov.origin_registration_id, prov.origin_command_id) == (
            USER, SOURCE, cmd.key.command_id,
        )
        assert prov.origin_export_id == TARGET.export_id and prov.last_actor_kind == "remote"
        audit = (await session.exec(select(AiAuditReceipt))).one()
        assert (audit.outcome, audit.command_id, audit.grant_revision) == ("accepted", cmd.key.command_id, 1)


async def test_failing_effect_leaves_neither_ledger_row_nor_effect(db: Database) -> None:
    fx = Fixture()
    published: list[Any] = []
    with pytest.raises(RuntimeError):
        await admit(db, chat_send_cmd(), fx.chat_send("x", fail=True), after_commit=published.append)
    assert fx.effects == 1 and published == []
    for model in (AiCommandLedger, AgentChatMessage, AiAuditReceipt, AiMessageProvenance,
                  AiResourceProvenance):
        assert await count(db, model) == 0


async def test_disk_full_on_commit_persists_nothing_and_never_dispatches(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = Fixture()

    async def disk_full(self: AsyncSession) -> None:
        raise OperationalError("COMMIT", {}, sqlite3.OperationalError("database or disk is full"))

    monkeypatch.setattr(AsyncSession, "commit", disk_full)
    published: list[Any] = []
    cmd = chat_send_cmd(CANARY_PROMPT)
    with pytest.raises(LedgerError) as caught:
        await admit(db, cmd, fx.chat_send(CANARY_PROMPT), after_commit=published.append)
    assert caught.value.code == ErrorCode.DENIED
    assert "disk" not in str(caught.value) and CANARY_PROMPT not in repr(caught.value)
    monkeypatch.undo()

    assert published == []
    for model in (AiCommandLedger, AgentChatMessage, AiAuditReceipt, AiMessageProvenance):
        assert await count(db, model) == 0
    with pytest.raises(LedgerError):
        await ledger.reserve_dispatch(db, cmd.key, "run", now_ms=NOW)
    assert fx.dispatched == 0


async def test_duplicate_deliveries_have_one_effect_and_the_same_receipt(db: Database) -> None:
    fx = Fixture()
    cmd = chat_send_cmd("only once")
    first = await admit(db, cmd, fx.chat_send("only once"))
    sequential = [await admit(db, cmd, fx.chat_send("only once")) for _ in range(3)]
    concurrent = await asyncio.gather(*(admit(db, cmd, fx.chat_send("only once")) for _ in range(5)))

    assert fx.effects == 1
    assert await count(db, AgentChatMessage) == 1
    assert await count(db, AiCommandLedger) == 1
    for again in [*sequential, *concurrent]:
        assert again.duplicate
        assert (again.state, again.message_id, again.run_id) == (
            first.state, first.message_id, first.run_id,
        )
    looked_up = await ledger.lookup(db, cmd.key, cmd.payload_hash)
    assert looked_up is not None and looked_up.run_id == first.run_id


async def test_same_id_with_another_payload_is_an_idempotency_conflict(db: Database) -> None:
    fx = Fixture()
    cid = str(uuid.uuid4())
    await admit(db, chat_send_cmd("one", command_id=cid), fx.chat_send("one"))
    with pytest.raises(LedgerError) as caught:
        await admit(db, chat_send_cmd("two", command_id=cid), fx.chat_send("two"))
    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    with pytest.raises(LedgerError) as caught:
        await ledger.lookup(db, chat_send_cmd("two", command_id=cid).key, "0" * 64)
    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert fx.effects == 1


async def test_ledger_key_includes_source_registration_and_export(db: Database) -> None:
    fx = Fixture()
    cid = str(uuid.uuid4())
    other_export = TARGET.model_copy(update={"export_id": str(uuid.uuid4())})
    await admit(db, command(Operation.TASK_CREATE, {"t": 1}, command_id=cid), fx.create_task("a", ""))
    await admit(
        db, command(Operation.TASK_CREATE, {"t": 1}, command_id=cid, source=str(uuid.uuid4())),
        fx.create_task("b", ""),
    )
    await admit(
        db, command(Operation.TASK_CREATE, {"t": 1}, command_id=cid, target=other_export),
        fx.create_task("c", ""),
    )
    assert fx.effects == 3 and await count(db, AiCommandLedger) == 3


async def test_known_id_returns_receipt_after_epoch_change_and_deadline(db: Database) -> None:
    fx = Fixture()
    cmd = chat_send_cmd()
    first = await admit(db, cmd, fx.chat_send("hello"))
    later = await admit(
        db, cmd, fx.chat_send("hello"), epoch=str(uuid.uuid4()), now_ms=NOW + 3_600_000,
    )
    assert later.duplicate and later.run_id == first.run_id and fx.effects == 1


@pytest.mark.parametrize("case", ["stale_epoch", "too_old", "future"])
async def test_unknown_id_with_stale_epoch_or_time_is_rejected_without_row(
    db: Database, case: str,
) -> None:
    fx = Fixture()
    if case == "stale_epoch":
        cmd, code = chat_send_cmd(epoch=str(uuid.uuid4())), ErrorCode.STALE_TARGET
    elif case == "too_old":
        cmd, code = chat_send_cmd(issued_at=NOW - 60_001), ErrorCode.EXPIRED
    else:
        cmd, code = chat_send_cmd(issued_at=NOW + 30_001), ErrorCode.EXPIRED
    with pytest.raises(LedgerError) as caught:
        await admit(db, cmd, fx.chat_send("hello"))
    assert caught.value.code == code
    assert fx.effects == 0 and await count(db, AiCommandLedger) == 0


async def test_duplicate_after_revoke_is_denied_not_the_old_result(db: Database) -> None:
    fx = Fixture()
    cmd = chat_send_cmd()
    await admit(db, cmd, fx.chat_send("hello"))
    with pytest.raises(LedgerError) as caught:
        await admit(db, cmd, fx.chat_send("hello"), scope_valid=False)
    assert caught.value.code == ErrorCode.DENIED
    with pytest.raises(LedgerError) as caught:
        await ledger.lookup(db, cmd.key, cmd.payload_hash, scope_valid=False)
    assert caught.value.code == ErrorCode.DENIED
    assert fx.effects == 1


async def test_unique_race_returns_the_winner_receipt(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = Fixture()
    cmd = command(Operation.TASK_CREATE, {"title": "T"})  # no CAS: reaches the unique index
    first = await admit(db, cmd, fx.create_task("T", ""))
    real_find = ledger._find
    calls = {"n": 0}

    async def blind_once(session: AsyncSession, key: LedgerKey):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_find(session, key)

    monkeypatch.setattr(ledger, "_find", blind_once)
    again = await admit(db, cmd, fx.create_task("T", ""))
    assert again.duplicate and again.resource_id == first.resource_id
    assert fx.effects == 2  # the loser's effect ran — and was rolled back
    assert await count(db, TeamTask) == 2  # task_x + the winner
    assert await count(db, AiCommandLedger) == 1 and await count(db, AiAuditReceipt) == 1


# --------------------------------------------------------------------------- dispatch fence


async def test_dispatch_token_is_claimed_once_even_across_reopen(db: Database) -> None:
    fx = Fixture()
    cmd = chat_send_cmd()
    receipt = await admit(db, cmd, fx.chat_send("hello"))
    assert receipt.run_id is not None
    token = await ledger.reserve_dispatch(db, cmd.key, receipt.run_id, now_ms=NOW)
    with pytest.raises(LedgerError):
        await ledger.reserve_dispatch(db, cmd.key, receipt.run_id, now_ms=NOW)

    results = await asyncio.gather(*(fx.run(db, cmd.key, token) for _ in range(6)))
    assert results.count(True) == 1 and fx.dispatched == 1

    path = db.path
    await db.close()
    reopened = Database(path)
    try:
        assert not await fx.run(reopened, cmd.key, token)
        running = await ledger.mark_running(reopened, cmd.key, now_ms=NOW)
        assert running.state is CommandState.RUNNING
        done = await ledger.mark_terminal(
            reopened, cmd.key, CommandState.COMPLETED, now_ms=NOW, safe_result={"status": "idle"},
        )
        assert done.state is CommandState.COMPLETED
        again = await ledger.mark_terminal(reopened, cmd.key, CommandState.FAILED, now_ms=NOW)
        assert again.state is CommandState.COMPLETED  # terminal is never rewritten
    finally:
        await reopened.close()
    assert fx.dispatched == 1


@pytest.mark.parametrize("crash_after", ["accept", "reserve", "claim"])
async def test_crash_recovery_is_outcome_unknown_and_never_redispatches(
    db: Database, crash_after: str,
) -> None:
    fx = Fixture()
    cmd = chat_send_cmd()
    receipt = await admit(db, cmd, fx.chat_send("hello"))
    token = None
    if crash_after in ("reserve", "claim"):
        token = await ledger.reserve_dispatch(db, cmd.key, receipt.run_id, now_ms=NOW)
    if crash_after == "claim":
        assert await fx.run(db, cmd.key, token)
    path = db.path
    await db.close()  # the process dies here — no running receipt was ever written

    reopened = Database(path)
    try:
        recovered = await ledger.recover(reopened, now_ms=NOW + 1)
        assert [r.command_id for r in recovered] == [cmd.key.command_id]
        assert recovered[0].state is CommandState.OUTCOME_UNKNOWN
        assert (recovered[0].resource_id, recovered[0].run_id, recovered[0].message_id) == (
            "chat_a", receipt.run_id, receipt.message_id,
        )
        if token is not None:
            assert not await fx.run(reopened, cmd.key, token)
        with pytest.raises(LedgerError):
            await ledger.reserve_dispatch(reopened, cmd.key, receipt.run_id, now_ms=NOW)
        resend = await admit(reopened, cmd, fx.chat_send("hello"), now_ms=NOW + 10)
        assert resend.duplicate and resend.state is CommandState.OUTCOME_UNKNOWN
        assert await ledger.recover(reopened, now_ms=NOW + 2) == []
        async with reopened.session() as session:
            outcomes = [a.outcome for a in (await session.exec(select(AiAuditReceipt))).all()]
        assert outcomes[-1] == "outcome_unknown"
    finally:
        await reopened.close()
    assert fx.effects == 1
    assert fx.dispatched == (1 if crash_after == "claim" else 0)


async def test_deny_before_dispatch_fences_the_token(db: Database) -> None:
    fx = Fixture()
    cmd = chat_send_cmd()
    receipt = await admit(db, cmd, fx.chat_send("hello"))
    token = await ledger.reserve_dispatch(db, cmd.key, receipt.run_id, now_ms=NOW)
    assert await ledger.deny_before_dispatch(db, cmd.key, now_ms=NOW)
    assert not await fx.run(db, cmd.key, token)
    got = await ledger.lookup(db, cmd.key)
    assert got is not None and got.state is CommandState.DENIED_BEFORE_DISPATCH

    started = chat_send_cmd(expected_revision=2)
    receipt = await admit(db, started, fx.chat_send("again"))
    token = await ledger.reserve_dispatch(db, started.key, receipt.run_id, now_ms=NOW)
    assert await fx.run(db, started.key, token)
    assert not await ledger.deny_before_dispatch(db, started.key, now_ms=NOW)  # already started
    assert fx.dispatched == 1


# --------------------------------------------------------------------------- tombstones


async def test_compaction_keeps_dedupe_tombstones(db: Database) -> None:
    fx = Fixture()
    cmd = command(Operation.TASK_CREATE, {"title": "T"})
    receipt = await admit(db, cmd, fx.create_task("T", ""))
    await ledger.mark_terminal(db, cmd.key, CommandState.COMPLETED, now_ms=NOW)
    pending = chat_send_cmd()
    await admit(db, pending, fx.chat_send("hello"))  # accepted, not terminal

    assert await ledger.compact(db, now_ms=NOW + SAFE_RESULT_RETENTION_MS) == 0
    assert await ledger.compact(db, now_ms=NOW + SAFE_RESULT_RETENTION_MS + 1) == 1

    async with db.session() as session:
        rows = {r.command_id: r for r in (await session.exec(select(AiCommandLedger))).all()}
    tomb = rows[cmd.key.command_id]
    assert tomb.compacted and tomb.safe_result is None and tomb.payload_hash == cmd.payload_hash
    assert not rows[pending.key.command_id].compacted

    late = await admit(db, cmd, fx.create_task("T", ""), now_ms=NOW + 40 * 86_400_000)
    assert late.duplicate and late.compacted and late.safe_result is None
    assert (late.state, late.resource_id) == (CommandState.COMPLETED, receipt.resource_id)
    other = command(Operation.TASK_CREATE, {"title": "changed"}, command_id=cmd.key.command_id)
    with pytest.raises(LedgerError) as caught:
        await admit(db, other, fx.create_task("x", ""), now_ms=NOW + 40 * 86_400_000)
    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert fx.effects == 2


# --------------------------------------------------------------------------- revision / provenance


async def test_resource_revision_cas_and_immutable_origin(db: Database) -> None:
    fx = Fixture()
    create = command(Operation.TASK_CREATE, {"title": "T"})
    created = await admit(db, create, fx.create_task("T", ""))
    task_id = created.resource_id
    assert task_id is not None

    async with db.session() as session:  # a local edit bumps the same sidecar
        assert await ledger.bump_resource_revision(
            session, ResourceKind.TASK, task_id, now_ms=NOW, op="local.edit",
        ) == 2

    stale = command(
        Operation.TASK_EDIT_DRAFT, {"resource_id": task_id, "title": "E"},
        resource=(ResourceKind.TASK, task_id), expected_revision=1,
    )
    with pytest.raises(LedgerError) as caught:
        await admit(db, stale, fx.edit_task(task_id))
    assert caught.value.code == ErrorCode.REVISION_CONFLICT
    assert fx.effects == 1 and await count(db, AiCommandLedger) == 1

    fresh = command(
        Operation.TASK_EDIT_DRAFT, {"resource_id": task_id, "title": "E"},
        resource=(ResourceKind.TASK, task_id), expected_revision=2,
    )
    await admit(db, fresh, fx.edit_task(task_id))
    async with db.session() as session:
        prov = await session.get(AiResourceProvenance, ("task", task_id))
        assert prov is not None and prov.revision == 3
        assert prov.origin_command_id == create.key.command_id  # origin never rewritten
        assert (prov.last_actor_kind, prov.last_command_id, prov.last_op) == (
            "remote", fresh.key.command_id, "task.edit_draft",
        )
        assert (await session.get(TeamTask, task_id)).title == "Edited"  # type: ignore[union-attr]
        # An untracked existing object starts at revision 1.
        assert await ledger.get_resource_revision(session, ResourceKind.TASK, "task_x") == 1


async def test_safe_result_refuses_text_fields(db: Database) -> None:
    async def effect(session: AsyncSession) -> EffectResult:
        return EffectResult(
            resource_kind=ResourceKind.TASK, resource_id="task_x",
            safe_result={"brief": CANARY_PROMPT},
        )

    cmd = command(Operation.TASK_NOTE, {"resource_id": "task_x", "text": CANARY_PROMPT},
                  resource=(ResourceKind.TASK, "task_x"))
    with pytest.raises(LedgerError):
        await admit(db, cmd, effect)
    assert await count(db, AiCommandLedger) == 0


async def test_audit_receipts_are_append_only(db: Database) -> None:
    fx = Fixture()
    await admit(db, command(Operation.TASK_CREATE, {"t": 1}), fx.create_task("T", ""))
    with sqlite3.connect(db.path) as conn:
        for statement in ("UPDATE ai_audit_receipt SET outcome='x'", "DELETE FROM ai_audit_receipt"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(statement)


async def test_no_text_payload_or_secret_is_stored_in_ai_tables(db: Database) -> None:
    fx = Fixture()
    body = f"{CANARY_PROMPT} {CANARY_KEY}"
    await admit(db, command(Operation.TASK_CREATE, {"title": "T", "brief": body}), fx.create_task("T", body))
    send = chat_send_cmd(body)
    receipt = await admit(db, send, fx.chat_send(body))
    token = await ledger.reserve_dispatch(db, send.key, receipt.run_id, now_ms=NOW)
    await fx.run(db, send.key, token)
    await ledger.mark_running(db, send.key, now_ms=NOW)
    await ledger.mark_terminal(db, send.key, CommandState.COMPLETED, now_ms=NOW)

    with sqlite3.connect(db.path) as conn:
        # The fixture objects do hold the text — that is the effect, not the ledger.
        assert conn.execute("SELECT count(*) FROM agent_chat_message WHERE text LIKE ?",
                            (f"%{CANARY_PROMPT}%",)).fetchone()[0] == 1
        for table in EXPECTED_TABLES:
            for row in conn.execute(f"SELECT * FROM {table}"):
                dumped = repr(row)
                assert "CANARY-PROMPT" not in dumped and "CANARYKEY" not in dumped, table
