"""Parallel token/cost statistics preserve accounting and daily task denominators."""

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from voxarium_studio.models.teams import AgentMessage, TeamMember, TeamTask, TaskStatus
from voxarium_studio.teams import compaction, orchestrator
from voxarium_studio.teams.backends.base import AgentEvent, AgentRunRequest
from voxarium_studio.teams.stats import cost_stats


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def task(task_id: str, **kwargs) -> TeamTask:
    return TeamTask(id=task_id, team_id="team", assignee_id="member", title=task_id,
                    created_at=at(1), **kwargs)


def message(task_id: str, kind: str, day: int, data: dict, *, text: str = "", hour=12):
    return AgentMessage(task_id=task_id, member_id="member", role="system", kind=kind,
                        data=data, text=text, created_at=at(day, hour))


def test_token_history_preserves_totals_archives_and_run_models():
    tasks = [
        task("done", total_tokens=350, total_cost_usd=2, status=TaskStatus.DONE,
             finished_at=at(3), archived=True, model="new-current-model"),
        task("cancelled", total_tokens=40, status=TaskStatus.CANCELLED, finished_at=at(4)),
        task("live", total_tokens=10, status=TaskStatus.IN_PROGRESS, started_at=at(5)),
    ]
    messages = [
        message("done", "system", 1, {"model": "old"}, text="init", hour=10),
        message("done", "result", 1, {"total_tokens": 100}),
        message("done", "model", 2, {"model": "changed"}, hour=10),
        message("done", "result", 2, {"total_tokens": 150}),
        message("done", "result", 3, {"total_tokens": 50, "model": "reported"}),
        message("other-team", "result", 1, {"total_tokens": 999, "model": "ignored"}),
    ]
    stats = cost_stats(tasks, list(reversed(messages)), [])
    assert [(r["day"], r["spent_tokens"], r["cumulative_tokens"]) for r in stats["token_series"]] == [
        ("2026-10-01", 100, 100), ("2026-10-02", 150, 250),
        ("2026-10-03", 100, 350), ("2026-10-04", 40, 390), ("2026-10-05", 10, 400),
    ]
    totals = stats["totals"]
    assert (totals["spent_tokens"], totals["done_tokens"], totals["cancelled_tokens"],
            totals["in_flight_tokens"]) == (400, 350, 40, 10)
    assert totals["spent_tokens"] == sum(row["spent_tokens"] for row in stats["by_member"])
    assert stats["by_model_tokens"] == [
        {"model": "changed", "spent_tokens": 150},
        {"model": None, "spent_tokens": 100},
        {"model": "old", "spent_tokens": 100},
        {"model": "reported", "spent_tokens": 50},
    ]
    assert totals["spent_tokens"] == sum(row["spent_tokens"] for row in stats["activity"])
    assert totals["archived"] == 1


def test_live_snapshot_dates_only_unexplained_tokens_and_never_counts_twice():
    tasks = [task("running", total_tokens=160, started_at=at(1))]
    messages = [
        message("running", "result", 1, {"total_tokens": 100, "model": "known"}),
        message("running", "system", 2, {
            "subtype": "usage_update", "total_tokens": 60, "task_total_tokens": 160,
        }, text="usage_update"),
    ]
    stats = cost_stats(tasks, messages, [])
    assert [(row["day"], row["spent_tokens"]) for row in stats["token_series"]] == [
        ("2026-10-01", 100), ("2026-10-02", 60),
    ]
    assert stats["series"] == []  # Subscription usage can have tokens without API cost.
    assert stats["activity"][-1]["spent_tokens"] == 60
    # The final result covers the snapshot; it must replace its contribution completely.
    messages.append(message("running", "result", 2, {"total_tokens": 60, "model": "known"}))
    stats = cost_stats(tasks, messages, [])
    assert stats["token_series"][-1]["cumulative_tokens"] == 160
    assert stats["by_model_tokens"] == [{"model": "known", "spent_tokens": 160}]


def test_inconsistent_history_and_stale_snapshot_fall_back_to_task_total():
    tasks = [task("reset", total_tokens=50, finished_at=at(4))]
    messages = [
        message("reset", "result", 1, {"total_tokens": 999, "model": "untrusted"}),
        message("reset", "system", 2, {
            "subtype": "usage_update", "total_tokens": 999, "task_total_tokens": 999,
        }),
    ]
    stats = cost_stats(tasks, messages, [])
    assert stats["token_series"] == [{"day": "2026-10-04", "spent_tokens": 50,
                                       "cumulative_tokens": 50, "runs": 1}]
    assert stats["by_model_tokens"] == [{"model": None, "spent_tokens": 50}]


@pytest.mark.parametrize("bad", [None, -20, True, "100", 1.5, {}, []])
def test_malformed_token_telemetry_does_not_break_statistics(bad):
    stats = cost_stats([task("a", total_tokens=10, finished_at=at(2))], [
        message("a", "result", 1, {"total_tokens": bad}),
    ], [])
    assert stats["token_series"][-1]["spent_tokens"] == 10
    assert stats["token_series"][-1]["day"] == "2026-10-02"


def test_daily_average_uses_completed_task_lifetime_totals_and_shared_denominator():
    tasks = [
        task("resumed", total_cost_usd=9, total_tokens=901, status=TaskStatus.DONE,
             started_at=at(1), finished_at=at(3), archived=True),
        task("free", status=TaskStatus.DONE, finished_at=at(3)),
        task("later", total_cost_usd=2, total_tokens=80, status=TaskStatus.DONE,
             finished_at=at(5)),
        task("undated", total_cost_usd=100, total_tokens=10000, status=TaskStatus.DONE),
        task("cancelled", total_cost_usd=100, status=TaskStatus.CANCELLED, finished_at=at(3)),
        task("review", total_cost_usd=100, status=TaskStatus.NEEDS_REVIEW, finished_at=at(3)),
    ]
    messages = [
        message("resumed", "result", 1, {"total_cost_usd": 3, "total_tokens": 301}),
        message("resumed", "result", 2, {"total_cost_usd": 6, "total_tokens": 600}),
    ]
    stats = cost_stats(tasks, messages, [])
    assert stats["average_task_series"] == [
        {"day": "2026-10-03", "tasks": 2, "average_usd": 4.5, "average_tokens": 450.5},
        {"day": "2026-10-05", "tasks": 1, "average_usd": 2, "average_tokens": 80},
    ]


def test_empty_statistics_have_no_fabricated_days():
    stats = cost_stats([], [], [])
    assert stats["token_series"] == stats["by_model_tokens"] == stats["average_task_series"] == []
    assert stats["totals"]["spent_tokens"] == 0


def test_compression_retains_token_results_and_live_date_across_repeated_compression():
    tasks = [task("a", total_tokens=150, started_at=at(1))]
    messages = [
        message("a", "system", 1, {"model": "original"}, text="init", hour=10),
        message("a", "result", 1, {"total_tokens": 100, "usage": {"private": "discard"}}),
        message("a", "system", 3, {"subtype": "usage_update", "total_tokens": 50,
                "task_total_tokens": 150, "context_window": 100000}, text="usage_update"),
    ]
    expected = cost_stats(tasks, messages, [])
    for _ in range(2):
        evidence = compaction.retain_evidence(messages)
        assert "private" not in str(evidence)
        assert "context_window" not in str(evidence)
        summary = message("a", "compressed summary", 4, {"evidence": evidence})
        messages = compaction.expand_evidence([summary])
        actual = cost_stats(tasks, messages, [])
        assert actual["token_series"] == expected["token_series"]
        assert actual["by_model_tokens"] == expected["by_model_tokens"]


def test_retained_usage_snapshot_does_not_reset_the_historical_run_model():
    tasks = [task("a", total_tokens=150, total_cost_usd=3, finished_at=at(3))]
    messages = [
        message("a", "system", 1, {"model": "original"}, text="init", hour=10),
        message("a", "result", 1, {"total_tokens": 100, "total_cost_usd": 1}),
        message("a", "system", 2, {"subtype": "usage_update", "total_tokens": 50,
                "task_total_tokens": 150}, text="usage_update"),
        message("a", "result", 3, {"total_tokens": 50, "total_cost_usd": 2}),
    ]
    summary = message("a", compaction.SUMMARY_KIND, 4,
                      {"evidence": compaction.retain_evidence(messages)})
    stats = cost_stats(tasks, compaction.expand_evidence([summary]), [])
    assert stats["by_model_tokens"] == [{"model": "original", "spent_tokens": 150}]
    assert stats["by_model"] == [{"model": "original", "spent_usd": 3}]


async def test_stats_query_reads_live_usage_without_final_result(ctx):
    task_row = await ctx.teams.create_task(
        team_id="team_default", assignee_id="team_default__dev",
        title="Live token statistics", brief="Work", auto_start=False,
    )
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, task_row.assignee_id)
    request = AgentRunRequest(member=member, task=task_row, workdir=ctx.paths.root,
                              prompt="Work", system_prompt="")

    class Backend:
        async def run(self, _request):
            yield AgentEvent(kind="text", data={"usage": {"input_tokens": 100, "output_tokens": 30}})

    await ctx.teams._drain_events(Backend(), request, task_row.id, task_row.assignee_id)
    stats = await ctx.teams.stats("team_default")
    assert stats["totals"]["spent_tokens"] == 130
    assert stats["token_series"][-1]["cumulative_tokens"] == 130
    assert stats["activity"][-1]["spent_tokens"] == 130
    async with ctx.db.session() as session:
        rows = list((await session.exec(select(AgentMessage).where(
            AgentMessage.task_id == task_row.id, AgentMessage.text == "usage_update",
        ))).all())
    assert stats["token_series"][-1]["day"] == rows[0].created_at.date().isoformat()


@pytest.mark.parametrize("has_summary", [False, True])
async def test_archiving_preserves_accounting_dates_and_models_without_duplicate_summaries(ctx, has_summary):
    task_row = await ctx.teams.create_task(
        team_id="team_default", assignee_id="team_default__dev",
        title="Archive statistics", brief="Work", auto_start=False,
    )
    first = message(task_row.id, "result", 1, {
        "total_cost_usd": 1, "total_tokens": 100, "model": "first-model",
    })
    later = message(task_row.id, "result", 2, {
        "total_cost_usd": 2, "total_tokens": 200, "model": "later-model",
    })
    async with ctx.db.session() as session:
        stored = await session.get(TeamTask, task_row.id)
        stored.status = TaskStatus.DONE
        stored.total_cost_usd = 3
        stored.total_tokens = 300
        stored.started_at = at(1)
        stored.finished_at = at(3)
        session.add(stored)
        if has_summary:
            summary = message(task_row.id, compaction.SUMMARY_KIND, 1,
                              {"evidence": compaction.retain_evidence([first])}, text="Saved handoff")
            summary.role = "assistant"
            session.add(summary)
        else:
            session.add(first)
        session.add(later)
        session.add(message(task_row.id, "system", 2, {"verbose": "discard me"}))

    expected = await ctx.teams.stats("team_default")
    for _ in range(2):
        await ctx.teams.update_task(task_row.id, {"archived": True})
        stats = await ctx.teams.stats("team_default")
        for key in ("series", "token_series", "by_model", "by_model_tokens", "activity", "average_task_series"):
            assert stats[key] == expected[key]
        assert stats["totals"]["archived"] == 1
        rows = await ctx.teams._messages_of(task_row.id)
        assert not any(row.kind == "result" for row in rows)
        assert "discard me" not in str([row.data for row in rows])
        assert len([row for row in rows if row.kind in compaction.SUMMARY_KINDS]) == int(has_summary)
        assert len([row for row in rows if row.kind == compaction.ACCOUNTING_KIND]) == int(not has_summary)
        assert len([row for row in compaction.expand_evidence(rows) if row.kind == "result"]) == 2
        await ctx.teams.update_task(task_row.id, {"archived": False})


async def test_new_compression_spend_retains_its_tokens_and_model(ctx, monkeypatch):
    class Backend:
        async def run(self, _request):
            yield AgentEvent(kind="result", text="Saved handoff", data={
                "total_cost_usd": 0.02, "total_tokens": 40, "model": "summary-model",
            })

    monkeypatch.setattr(orchestrator, "get_backend", lambda _: Backend())
    task_row = await ctx.teams.create_task(
        team_id="team_default", assignee_id="team_default__dev",
        title="Summary statistics", brief="Work", auto_start=False,
    )
    await ctx.teams.record_message(task_row.id, task_row.assignee_id,
                                   role="user", kind="note", text="Preserve the progress")
    await ctx.teams.compress_task(task_row.id)
    stats = await ctx.teams.stats("team_default")
    assert stats["by_model_tokens"] == [{"model": "summary-model", "spent_tokens": 40}]
    assert stats["token_series"][-1]["cumulative_tokens"] == 40
