"""Live task spend survives reload/cancellation and reconciles final provider totals."""

import asyncio

import pytest
from sqlmodel import select

from voxarium_studio.models.teams import AgentMessage, TeamMember, TeamTask
from voxarium_studio.teams.backends.base import AgentEvent, AgentRunRequest


async def setup_task(ctx, *, previous=0):
    task = await ctx.teams.create_task(
        team_id="team_default", assignee_id="team_default__dev",
        title="Usage", brief="Work", auto_start=False,
    )
    async with ctx.db.session() as session:
        stored = await session.get(TeamTask, task.id)
        stored.total_tokens = previous
        session.add(stored)
        member = await session.get(TeamMember, task.assignee_id)
    return task, AgentRunRequest(member=member, task=task, workdir=ctx.paths.root,
                                 prompt="Work", system_prompt="")


async def total(ctx, task):
    async with ctx.db.session() as session:
        return (await session.get(TeamTask, task.id)).total_tokens


async def telemetry(ctx, task):
    async with ctx.db.session() as session:
        return list((await session.exec(select(AgentMessage).where(
            AgentMessage.task_id == task.id, AgentMessage.text == "usage_update",
        ))).all())


async def test_response_usage_is_live_and_results_only_add_uncredited_tokens(ctx):
    task, request = await setup_task(ctx, previous=50)

    class Backend:
        async def run(self, _request):
            yield AgentEvent(kind="thinking", data={"usage": {
                "input_tokens": 100, "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 30, "output_tokens": 10,
            }})
            assert await total(ctx, task) == 210
            first = (await telemetry(ctx, task))[0]
            assert first.data["task_total_tokens"] == 210
            assert first.data["context_tokens"] == 150
            yield AgentEvent(kind="tool_use", text="Read")
            assert await total(ctx, task) == 210  # another block of the same API response
            yield AgentEvent(kind="result", data={"total_tokens": 200,
                "num_turns": 1, "total_cost_usd": 0.01, "continued": True})
            assert await total(ctx, task) == 250
            yield AgentEvent(kind="text", data={"usage": {"input_tokens": 60, "output_tokens": 4}})
            assert await total(ctx, task) == 314
            yield AgentEvent(kind="result", data={"total_tokens": 70,
                "num_turns": 1, "total_cost_usd": 0.02})

    await ctx.teams._drain_events(Backend(), request, task.id, task.assignee_id)
    assert await total(ctx, task) == 320
    rows = await telemetry(ctx, task)
    assert len(rows) == 1
    assert rows[0].data == {"subtype": "usage_update", "total_tokens": 70,
                           "task_total_tokens": 320, "context_tokens": 60,
                           "total_uncached_tokens": 64, "total_cached_tokens": 0,
                           "total_input_tokens": 60, "total_output_tokens": 4,
                           "task_total_uncached_tokens": 204, "task_total_cached_tokens": 20,
                           "task_total_input_tokens": 190, "task_total_output_tokens": 14,
                           "accounting_version": 2}
    async with ctx.db.session() as session:
        stored = await session.get(TeamTask, task.id)
        assert stored.total_cost_usd == pytest.approx(0.03)
        assert stored.num_turns == 2


async def test_cumulative_usage_replay_and_final_result_do_not_double_count(ctx):
    task, request = await setup_task(ctx, previous=500)
    state_ids = []

    class Backend:
        async def run(self, _request):
            for count, expected in [(100, 600), (100, 600), (80, 600), (150, 650)]:
                data = {"subtype": "usage_update", "total_tokens": count}
                if count == 100:
                    data.update(context_tokens=42, context_window=1000)
                yield AgentEvent(kind="system", text="usage_update", data=data)
                assert await total(ctx, task) == expected
                rows = await telemetry(ctx, task)
                assert len(rows) == 1
                assert rows[0].data["context_tokens"] == 42
                assert rows[0].data["context_window"] == 1000
                state_ids.append(rows[0].id)
            yield AgentEvent(kind="result", data={"total_tokens": 160})

    await ctx.teams._drain_events(Backend(), request, task.id, task.assignee_id)
    assert await total(ctx, task) == 660
    assert len(set(state_ids)) == 1
    assert (await telemetry(ctx, task))[0].data["task_total_tokens"] == 660


async def test_spend_survives_cancellation_and_next_run_adds_only_new_usage(ctx):
    task, request = await setup_task(ctx)

    class Cancelled:
        async def run(self, _request):
            yield AgentEvent(kind="text", data={"usage": {"input_tokens": 100, "output_tokens": 9}})
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ctx.teams._drain_events(Cancelled(), request, task.id, task.assignee_id)
    assert await total(ctx, task) == 109
    first = (await telemetry(ctx, task))[0].data
    assert (first["task_total_input_tokens"], first["task_total_output_tokens"]) == (100, 9)

    class Resumed:
        async def run(self, _request):
            yield AgentEvent(kind="system", text="usage_update", data={
                "subtype": "usage_update", "total_tokens": 30,
            })
            assert await total(ctx, task) == 139
            yield AgentEvent(kind="result", data={"total_tokens": 40})

    await ctx.teams._drain_events(Resumed(), request, task.id, task.assignee_id)
    assert await total(ctx, task) == 149
    latest = (await telemetry(ctx, task))[0].data
    assert (latest["task_total_input_tokens"], latest["task_total_output_tokens"]) == (100, 9)
    assert "context_tokens" not in (await telemetry(ctx, task))[0].data


async def test_malformed_usage_does_not_break_work_or_turn_output_into_context(ctx):
    task, request = await setup_task(ctx)

    class Backend:
        async def run(self, _request):
            yield AgentEvent(kind="text", data={"usage": {
                "input_tokens": -10, "cache_read_input_tokens": "100",
                "cache_creation_input_tokens": True, "output_tokens": 8,
            }})
            assert await total(ctx, task) == 8
            assert "context_tokens" not in (await telemetry(ctx, task))[0].data
            yield AgentEvent(kind="result", data={})  # interrupted provider has no final usage

    await ctx.teams._drain_events(Backend(), request, task.id, task.assignee_id)
    assert await total(ctx, task) == 8
