"""Journal repair overrides survive archive cleanup and future context compression."""

from datetime import datetime, timezone

import pytest

from voxarium_studio.models.teams import AgentMessage, TeamTask, TaskStatus
from voxarium_studio.teams import compaction, orchestrator
from voxarium_studio.teams.backends.base import AgentEvent


def at(day):
    return datetime(2026, 10, day, 12, tzinfo=timezone.utc)


def evidence(tokens, day=1):
    return {"kind": "result", "role": "system", "text": "", "created_at": at(day).isoformat(),
            "data": {"total_tokens": tokens, "total_uncached_tokens": tokens // 10,
                     "total_cached_tokens": tokens - tokens // 10, "total_cost_usd": tokens / 100,
                     "model": "model", "accounting_version": 2, "repair_source": "original_journals"}}


def carriers(task_id="task"):
    old = AgentMessage(task_id=task_id, member_id="member", role="assistant",
        kind=compaction.SUMMARY_KIND, created_at=at(2), text="Old handoff",
        data={"evidence": [evidence(1000)]})
    repair = AgentMessage(task_id=task_id, member_id="member", role="system",
        kind="accounting", created_at=at(3), data={"accounting_version": 2,
            "repair": {"version": 1, "cutoff_at": at(2).isoformat()}, "evidence": [evidence(100)]})
    newer = AgentMessage.model_validate({**evidence(20, 4), "task_id": task_id, "member_id": "member"})
    return [old, repair, newer]


def test_repair_overrides_nested_old_evidence_but_keeps_new_results():
    messages = carriers()
    for _ in range(2):
        expanded = compaction.expand_evidence(messages)
        assert sorted(m.data["total_tokens"] for m in expanded if m.kind == "result") == [20, 100]
        retained = compaction.retain_evidence(messages)
        assert all(row["data"]["repair_source"] == "original_journals" for row in retained)
        messages = [AgentMessage(task_id="task", member_id="member", role="assistant",
            kind=compaction.SUMMARY_KIND, data={"evidence": retained})]


def test_repair_precedence_is_per_task_and_does_not_erase_other_tasks():
    untouched = AgentMessage.model_validate({**evidence(300), "task_id": "untouched", "member_id": "member"})
    messages = [*carriers("a"), *carriers("b"), untouched]
    expanded = compaction.expand_evidence(messages)
    totals = {}
    for row in expanded:
        if row.kind == "result":
            totals[row.task_id] = totals.get(row.task_id, 0) + row.data["total_tokens"]
    assert totals == {"a": 120, "b": 120, "untouched": 300}


@pytest.mark.parametrize("operation", ["archive", "compress"])
async def test_repaired_accounting_survives_archive_or_compression(ctx, monkeypatch, operation):
    task = await ctx.teams.create_task(team_id="team_default", assignee_id="team_default__dev",
        title="Repaired history", brief="Work", auto_start=False)
    async with ctx.db.session() as session:
        saved = await session.get(TeamTask, task.id)
        saved.total_tokens, saved.total_uncached_tokens, saved.total_cached_tokens = 120, 12, 108
        saved.total_cost_usd = 1.2
        saved.status = TaskStatus.DONE if operation == "archive" else TaskStatus.TODO
        saved.finished_at = at(4)
        session.add(saved)
        for row in carriers(task.id):
            session.add(row)

    if operation == "archive":
        for _ in range(2):
            await ctx.teams.update_task(task.id, {"archived": True})
            rows = compaction.expand_evidence(await ctx.teams._messages_of(task.id))
            assert sum(row.data.get("total_tokens", 0) for row in rows if row.kind == "result") == 120
            await ctx.teams.update_task(task.id, {"archived": False})
    else:
        class Backend:
            async def run(self, _request):
                yield AgentEvent(kind="result", text="New handoff", data={"total_tokens": 5,
                    "total_uncached_tokens": 5, "total_cached_tokens": 0, "total_cost_usd": 0.1})
        monkeypatch.setattr(orchestrator, "get_backend", lambda _: Backend())
        await ctx.teams.record_message(task.id, task.assignee_id, role="user", kind="note", text="Continue")
        await ctx.teams.compress_task(task.id)
        rows = compaction.expand_evidence(await ctx.teams._messages_of(task.id))
        assert sum(row.data.get("total_tokens", 0) for row in rows if row.kind == "result") == 125

    stats = await ctx.teams.stats("team_default")
    expected = 120 if operation == "archive" else 125
    assert stats["totals"]["spent_tokens"] == expected
    assert stats["token_series"][-1]["cumulative_tokens"] == expected
