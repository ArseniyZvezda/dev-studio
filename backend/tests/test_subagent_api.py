"""Durable worker details remain available after the parent transcript is trimmed (§10.3)."""

import pytest
from sqlalchemy import delete

from voxarium_studio.api.teams import get_task
from voxarium_studio.models.base import utcnow
from voxarium_studio.models.subagents import TaskSubagent
from voxarium_studio.models.teams import AgentMessage, TaskStatus, TeamTask


@pytest.mark.parametrize("trim", ["archive", "compression"])
async def test_task_details_keep_workers_after_transcript_cleanup(ctx, trim):
    task = await ctx.teams.create_task(
        team_id="team_default", assignee_id="team_default__dev", title="Parent", brief="Work",
    )
    other = await ctx.teams.create_task(
        team_id="team_default", assignee_id="team_default__dev", title="Other", brief="Other work",
    )
    worker = TaskSubagent(
        task_id=task.id, member_id=task.assignee_id, description="Review", backend="codex",
        model="gpt-test", status="completed", summary="Reviewed.", total_tokens=1500,
        total_cost_usd=0.03, tool_uses=4, finished_at=utcnow(),
    )
    async with ctx.db.session() as session:
        session.add(worker)
        session.add(TaskSubagent(
            task_id=other.id, member_id=other.assignee_id, description="Other worker",
            backend="qwen", model="qwen-test",
        ))
        session.add(AgentMessage(
            task_id=task.id, member_id=task.assignee_id, role="system", kind="system",
            text="task_started", data={"task_id": worker.id, "task_type": "studio_agent"},
        ))
        saved = await session.get(TeamTask, task.id)
        saved.status = TaskStatus.DONE
        saved.total_tokens = 3300
        saved.total_cost_usd = 0.12
        session.add(saved)

    if trim == "archive":
        await ctx.teams.update_task(task.id, {"archived": True})
    else:
        async with ctx.db.session() as session:
            await session.exec(delete(AgentMessage).where(AgentMessage.task_id == task.id))
            session.add(AgentMessage(
                task_id=task.id, member_id=task.assignee_id, role="assistant",
                kind="compressed summary", text="Task summary.",
            ))

    details = await get_task("team_default", task.id, ctx)
    assert all(message["text"] != "task_started" for message in details["messages"])
    assert details["total_tokens"] == 3300
    assert details["total_cost_usd"] == 0.12
    assert len(details["subagents"]) == 1
    restored = details["subagents"][0]
    assert restored["id"] == worker.id
    assert restored["status"] == "completed"
    assert restored["backend"] == "codex"
    assert restored["model"] == "gpt-test"
    assert restored["summary"] == "Reviewed."
    assert restored["total_tokens"] == 1500
    assert restored["total_cost_usd"] == 0.03
    assert restored["tool_uses"] == 4
    assert restored["updated_at"]
