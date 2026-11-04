"""Compression replaces task history only after a usable handoff exists (§10.4)."""

import asyncio

import pytest
from sqlmodel import select

from voxarium_studio.models.teams import AgentMessage, TeamMember, TeamTask, TaskStatus
from voxarium_studio.teams import compaction, orchestrator
from voxarium_studio.teams.backends.base import AgentEvent, AgentRunRequest
from voxarium_studio.teams.backends.recovery import recovery_prompt

TEAM = "team_default"
DEV = f"{TEAM}__dev"


class Backend:
    named_agents = False
    session_recovery = True

    def __init__(self):
        self.requests = []
        self.error = None
        self.empty = False
        self.gate = None
        self.entered = asyncio.Event()
        self.usage = 0

    async def run(self, request):
        self.requests.append(request)
        if request.session_key.endswith(":summary"):
            self.entered.set()
            if self.gate:
                await self.gate.wait()
            if self.error:
                yield AgentEvent(kind="error", text=self.error)
                return
            yield AgentEvent(kind="result", text="" if self.empty else "Implemented movement. Test jumping next.",
                             data={"total_cost_usd": 0.01, "num_turns": 1, "total_tokens": 50})
        else:
            yield AgentEvent(kind="text", text="Movement implemented", session_id="new-session",
                             data={"usage": {"input_tokens": self.usage}})
            yield AgentEvent(kind="result", text="Done", session_id="new-session")


@pytest.fixture
async def setup(ctx, monkeypatch):
    backend = Backend()
    monkeypatch.setattr(orchestrator, "get_backend", lambda _: backend)
    ctx.config.teams.commit_proposals = False
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.isolate_worktree = False
        session.add(member)
    task = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV,
                                       title="Movement", brief="Keep keyboard support", auto_start=False)
    await ctx.teams.record_message(task.id, DEV, role="user", kind="note", text="Keep jump height at 2m")
    async with ctx.db.session() as session:
        stored = await session.get(TeamTask, task.id)
        stored.agent_session_id = "old-session"
        stored.result_summary = "Implementation in progress"
        session.add(stored)
    return ctx, task, backend


async def test_manual_replaces_every_old_message_and_preserves_task_and_audit(setup):
    ctx, task, backend = setup
    await ctx.teams.record_message(task.id, DEV, role="assistant", kind="tool_use", data={
        "name": "Edit", "input": {"file_path": "player.py", "old_string": "old secret payload"},
        "edit": {"insertions": 3, "deletions": 2},
    })
    await ctx.teams.record_message(task.id, DEV, role="system", kind="file_observation",
                                   data={"paths": ["player.py", "other.py"]})
    await ctx.teams.record_message(task.id, DEV, role="system", kind="result",
                                   data={"model": "test-model", "total_cost_usd": 0.5})
    await ctx.teams._store_result_stats(task.id, {"total_cost_usd": 0.5, "total_tokens": 500})
    before = await ctx.teams.task_changes(task.id)
    ctx.config.teams.context_compression_tokens = 0  # manual bypasses the disabled threshold
    compressed = await ctx.teams.compress_task(task.id)
    messages = await ctx.teams._messages_of(task.id)
    assert len(messages) == 1 and messages[0].kind == "compressed summary"
    assert messages[0].text == "Implemented movement. Test jumping next."
    assert "old secret payload" not in str(messages[0].data)
    assert (await ctx.teams.task_changes(task.id)) == before
    assert compressed.agent_session_id is None
    assert compressed.status == TaskStatus.TODO
    assert compressed.brief == task.brief
    assert compressed.result_summary == "Implementation in progress"
    assert compressed.total_cost_usd == pytest.approx(0.51)
    assert compressed.total_tokens == 550
    stats = await ctx.teams.stats(TEAM)
    assert stats["totals"]["spent_usd"] == pytest.approx(0.51)
    assert {entry["model"]: entry["spent_usd"] for entry in stats["by_model"]}["test-model"] == 0.5
    summary_request = backend.requests[0]
    assert summary_request.resume_session_id is None
    assert not summary_request.mcp_servers and not summary_request.plugin_dirs
    assert "Keep keyboard support" not in summary_request.prompt
    assert "Implementation in progress" not in summary_request.prompt
    assert "old secret payload" not in summary_request.prompt
    assert "player.py" not in summary_request.prompt
    assert "Keep jump height at 2m" in summary_request.prompt
    with pytest.raises(ValueError, match="no new dialogue"):
        await ctx.teams.compress_task(task.id)


@pytest.mark.parametrize("failure", ["error", "empty", "cancel"])
async def test_failed_compression_keeps_old_history_and_session(setup, failure):
    ctx, task, backend = setup
    original = [m.model_dump() for m in await ctx.teams._messages_of(task.id)]
    if failure == "error":
        backend.error = "Service unavailable"
    elif failure == "empty":
        backend.empty = True
    else:
        backend.gate = asyncio.Event()
    call = asyncio.create_task(ctx.teams.compress_task(task.id))
    await backend.entered.wait()
    if failure == "cancel":
        await ctx.jobs.cancel(task.id)
    with pytest.raises((RuntimeError, ValueError, asyncio.CancelledError)):
        await call
    assert [m.model_dump() for m in await ctx.teams._messages_of(task.id)] == original
    assert (await ctx.teams._reload(task.id)).agent_session_id == "old-session"
    assert not ctx.jobs.is_running(task.id)


async def test_concurrent_note_survives_and_conflicting_actions_are_refused(setup):
    ctx, task, backend = setup
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    backend.gate = asyncio.Event()
    call = asyncio.create_task(ctx.teams.compress_task(task.id))
    await backend.entered.wait()
    for action in (ctx.teams.compress_task, ctx.teams.start_task,
                   ctx.teams.reset_task, ctx.teams.delete_task):
        with pytest.raises(ValueError, match="running"):
            await action(task.id)
    await ctx.teams.add_note(task.id, "New: check gamepad too")
    backend.gate.set()
    await call
    messages = await ctx.teams._messages_of(task.id)
    assert [m.kind for m in messages] == ["compressed summary", "note"]
    assert messages[-1].text == "New: check gamepad too"


async def test_auto_compresses_at_threshold_then_starts_fresh_with_summary(setup, wait_for):
    ctx, task, backend = setup
    ctx.config.teams.context_compression_tokens = 100
    backend.usage = 100
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await ctx.teams.start_task(task.id)
    await wait_for(task.id)
    messages = await ctx.teams._messages_of(task.id)
    assert [m.kind for m in messages] == ["compressed summary"]
    assert (await ctx.teams._reload(task.id)).agent_session_id is None
    assert (await ctx.teams._reload(task.id)).status == TaskStatus.NEEDS_REVIEW
    ctx.config.teams.context_compression_tokens = 0
    await ctx.teams.add_note(task.id, "Now test gamepad")
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await ctx.teams.start_task(task.id)
    await wait_for(task.id)
    fresh = [r for r in backend.requests if not r.session_key.endswith(":summary")][-1]
    assert fresh.resume_session_id is None
    assert "Implemented movement" in fresh.prompt and "Now test gamepad" in fresh.prompt
    assert (await ctx.teams._reload(task.id)).agent_session_id == "new-session"
    await ctx.teams.compress_task(task.id)
    assert len(await ctx.teams._messages_of(task.id)) == 1
    assert "Implemented movement" in backend.requests[-1].prompt


async def test_existing_context_is_compressed_before_resume(setup, wait_for):
    ctx, task, backend = setup
    ctx.config.teams.context_compression_tokens = 1000
    await ctx.teams.record_message(task.id, DEV, role="assistant", kind="text", text="Completed design",
                                   data={"usage": {"input_tokens": 800, "cache_read_input_tokens": 200}})
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await ctx.teams.start_task(task.id)
    await wait_for(task.id)
    assert backend.requests[0].session_key.endswith(":summary")
    assert backend.requests[1].resume_session_id is None
    assert "Implemented movement" in backend.requests[1].prompt


@pytest.mark.parametrize("override", [None, "opus"])
@pytest.mark.parametrize("automatic", [False, True])
async def test_compression_uses_the_same_model_as_the_task_run(setup, wait_for, override, automatic):
    ctx, task, backend = setup
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.model = "haiku"
        member.effort = "low"
        session.add(member)
        stored = await session.get(TeamTask, task.id)
        stored.model = override
        stored.effort = "high" if override else None
        session.add(stored)
    if automatic:
        ctx.config.teams.context_compression_tokens = 1000
        backend.usage = 1000
        await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
        await ctx.teams.start_task(task.id)
        await wait_for(task.id)
    else:
        await ctx.teams.compress_task(task.id)
    summaries = [r for r in backend.requests if r.session_key.endswith(":summary")]
    assert summaries
    for request in summaries:
        assert request.model == (override or "haiku")
        assert request.effort == ("high" if override else "low")
        assert request.text_only


def test_threshold_uses_latest_context_not_cumulative_usage():
    def message(**kwargs):
        return AgentMessage(task_id="task", member_id=DEV, **kwargs)
    history = [message(role="assistant", data={"usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30,
        "output_tokens": 9000,
    }}), message(role="system", kind="result", data={"usage": {"input_tokens": 999999}})]
    assert compaction.context_tokens(history) == 60
    history.append(message(role="assistant", data={"usage": {"input_tokens": 5}}))
    assert compaction.context_tokens(history) == 5
    assert compaction.context_tokens([message(role="user", text="x" * 4000)]) >= 1000


def test_only_dialogue_text_is_sent_to_compression():
    kinds = ["text", "note", "summary", "review", "result", "thinking", "tool_use",
             "tool_result", "status", "edited", "system", "unknown"]
    messages = [AgentMessage(task_id="t", member_id=DEV, role="assistant", kind=kind,
                             text=f"content-{kind}-end", data={"private": "excluded-metadata"})
                for kind in kinds]
    source = compaction.transcript_text(messages)
    for message in messages:
        assert (message.text in source) == (message.kind in {"text", "note", "summary"})
    assert "excluded-metadata" not in source


@pytest.mark.parametrize("kind", ["compressed summary", "context_summary"])
async def test_previous_handoff_is_carried_forward_as_summary(setup, kind):
    ctx, task, backend = setup
    await ctx.teams.record_message(task.id, DEV, role="assistant", kind=kind,
                                   text="Earlier decision", data={"private": "hidden-evidence"})
    await ctx.teams.compress_task(task.id)
    assert "[assistant/summary] Earlier decision" in backend.requests[0].prompt
    assert "hidden-evidence" not in backend.requests[0].prompt
    assert (await ctx.teams._messages_of(task.id))[0].kind == "compressed summary"


async def test_ignored_blocks_cannot_trigger_another_compression(setup):
    ctx, task, backend = setup
    await ctx.teams.compress_task(task.id)
    for kind in ("tool_use", "tool_result", "thinking", "review", "result", "system"):
        await ctx.teams.record_message(task.id, DEV, role="assistant", kind=kind, text="Noise")
    await ctx.teams.record_message(task.id, DEV, role="assistant", kind="text", text="  ")
    with pytest.raises(ValueError, match="no new dialogue"):
        await ctx.teams.compress_task(task.id)
    assert len(backend.requests) == 1


async def test_all_large_history_chunks_are_seen(setup, monkeypatch):
    ctx, task, backend = setup
    monkeypatch.setattr(compaction, "CHUNK_CHARS", 200)
    await ctx.teams.record_message(task.id, DEV, role="assistant", kind="text",
                                   text="a" * 500 + "MIDDLE-DECISION" + "z" * 500)
    await ctx.teams.compress_task(task.id)
    assert any("MIDDLE-DECISION" in r.prompt for r in backend.requests)
    assert len(backend.requests) > 2
    assert len(await ctx.teams._messages_of(task.id)) == 1


def test_recovery_always_keeps_the_compressed_handoff():
    messages = [AgentMessage(task_id="t", member_id=DEV, role="assistant",
                             kind="compressed summary", text="Important early decision")]
    messages.append(AgentMessage(task_id="t", member_id=DEV, role="assistant", text="x" * 40000))
    assert "Important early decision" in recovery_prompt("Continue", messages)


def test_compress_api_checks_team_and_task(client):
    assert client.post(f"/api/teams/{TEAM}/tasks/missing/compress", json={}).status_code == 404
    response = client.post(f"/api/teams/{TEAM}/tasks", json={
        "assignee_id": DEV, "title": "Test", "brief": "", "auto_start": False,
    })
    assert response.status_code == 201
    task_id = response.json()["id"]
    assert client.post(f"/api/teams/wrong/tasks/{task_id}/compress", json={}).status_code == 404
    assert client.post(f"/api/teams/{TEAM}/tasks/{task_id}/compress", json={}).status_code == 409
