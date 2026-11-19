"""Auto is a routing policy, never a model name sent to a runtime (§10.11)."""
from __future__ import annotations

import pytest

from voxarium_studio.models.teams import TaskStatus, TeamMember
from voxarium_studio.teams import orchestrator as runtime

from test_task_effort_solver import (  # noqa: F401
    DEV, TEAM, Worker, _dev, _messages, _run, solver, teams,
)


@pytest.fixture
def mapping(monkeypatch):
    calls = []

    def resolve(backend, strength):
        calls.append((backend, strength))
        return f"{backend}-strength-{strength}"

    monkeypatch.setattr(runtime, "resolve_model", resolve)
    return calls


async def auto_member(ctx, *, effort=None, backend="effort_worker"):
    await _dev(ctx, effort=effort, backend=backend)
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.model = "auto"
        session.add(member)


@pytest.mark.parametrize("fixed, expected", [(None, "xhigh"), ("low", "low")])
async def test_auto_model_shares_evaluation_and_preserves_fixed_effort(teams, wait_for, solver, mapping, fixed, expected):
    await auto_member(teams, effort=fixed)
    task = await _run(teams, wait_for)
    assert task.status == TaskStatus.NEEDS_REVIEW
    assert task.model == "effort_worker-strength-2"
    assert Worker.requests[0].model == Worker.requests[0].member.model == task.model
    assert Worker.efforts == [expected]
    assert task.effort == ("xhigh" if fixed is None else None)
    assert len(solver.requests) == 1
    assert mapping == [("effort_worker", 2)]
    [selection] = await _messages(teams, task.id, "effort_solver")
    assert selection.data == {"model": 2, "effort": 4, "effort_level": "xhigh"}


async def test_auto_model_is_scored_even_when_backend_has_no_effort(teams, wait_for, solver, mapping):
    await auto_member(teams, backend="effort_worker_qwenlike")
    task = await _run(teams, wait_for)
    assert task.model == "effort_worker_qwenlike-strength-2"
    assert task.effort is None
    assert Worker.efforts == [None]
    assert len(solver.requests) == 1


async def test_auto_model_resume_keeps_concrete_selection_but_new_tasks_evaluate(teams, wait_for, solver, mapping, monkeypatch):
    await auto_member(teams, effort="low")
    first = await _run(teams, wait_for)
    monkeypatch.setattr(runtime, "resolve_model", lambda *_: "new-mapping")
    await teams.teams.request_changes(first.id, "Check the edge case.")
    await wait_for(first.id, timeout=20)
    assert [r.model for r in Worker.requests] == [first.model, first.model]
    assert Worker.requests[-1].resume_session_id == "sess_auto"
    assert len(solver.requests) == 1
    second = await _run(teams, wait_for)
    assert second.model == "new-mapping"
    assert len(solver.requests) == 2


@pytest.mark.parametrize("manual", [False, True])
async def test_backend_switch_remaps_only_auto_models(teams, wait_for, solver, mapping, manual):
    await auto_member(teams, effort="low")
    task = await _run(teams, wait_for)
    assert task.auto_model_backend == "effort_worker"
    assert task.auto_model_strength == 2
    if manual:
        await teams.teams.update_task(task.id, {"model": task.model})
    async with teams.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.agent_backend = "effort_worker_qwenlike"

    await teams.teams.request_changes(task.id, "Check the edge case.")
    await wait_for(task.id, timeout=20)
    request = Worker.requests[-1]
    saved = await teams.teams._reload(task.id)
    assert request.member.agent_backend == "effort_worker_qwenlike"
    assert request.resume_session_id is None
    assert request.model == saved.model == (task.model if manual else "effort_worker_qwenlike-strength-2")
    assert saved.auto_model_backend == (None if manual else "effort_worker_qwenlike")
    assert saved.auto_model_strength == (None if manual else 2)
    assert len(solver.requests) == 1
    assert mapping == ([("effort_worker", 2)] if manual else [
        ("effort_worker", 2), ("effort_worker_qwenlike", 2),
    ])


@pytest.mark.parametrize("model", ["chosen-model", "auto"])
async def test_task_model_override_wins_over_member(teams, wait_for, solver, mapping, model):
    await _dev(teams, effort="max")
    task = await teams.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="Task override", brief="Instructions", model=model,
    )
    await teams.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await teams.teams.start_task(task.id)
    await wait_for(task.id, timeout=20)
    expected = "effort_worker-strength-2" if model == "auto" else model
    assert Worker.requests[0].model == expected
    assert Worker.efforts == ["max"]
    assert len(solver.requests) == (1 if model == "auto" else 0)


async def test_model_evaluation_failure_blocks_without_default(teams, wait_for, solver, mapping):
    await auto_member(teams, effort="max")
    solver.error = "offline"
    task = await _run(teams, wait_for)
    assert task.status == TaskStatus.BLOCKED
    assert "Orchestrator" in task.error
    assert not Worker.requests
    assert task.model is None


async def test_missing_mapping_blocks_without_leaking_auto(teams, wait_for, solver, monkeypatch):
    await auto_member(teams, effort="low")

    def missing(*_):
        raise ValueError("Missing model mapping")

    monkeypatch.setattr(runtime, "resolve_model", missing)
    task = await _run(teams, wait_for)
    assert task.status == TaskStatus.BLOCKED
    assert "Missing model mapping" in task.error
    assert not Worker.requests
    assert task.model is None


@pytest.mark.parametrize("model_policy", ["member_auto", "task_auto", "pinned"])
async def test_manual_compaction_resolves_auto_but_keeps_pinned_task_model(teams, solver, mapping, model_policy):
    await _dev(teams, effort="low")
    task = await teams.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="Existing dialogue", brief="Implementation instructions",
        model={"member_auto": None, "task_auto": "auto", "pinned": "task-concrete"}[model_policy],
    )
    await teams.teams.record_message(task.id, DEV, role="assistant", kind="text", text="Existing implementation")
    if model_policy != "task_auto":
        # Changing the member after previous dialogue must not send literal Auto to
        # the summary backend, including when the task itself has a concrete override.
        async with teams.db.session() as session:
            member = await session.get(TeamMember, DEV)
            member.model = "auto"
            session.add(member)
    compressed = await teams.teams.compress_task(task.id)
    expected = "task-concrete" if model_policy == "pinned" else "effort_worker-strength-2"
    [request] = Worker.requests
    assert request.session_key == f"{task.id}:summary"
    assert request.model == request.member.model == compressed.model == expected
    assert request.effort == "low"
    assert compressed.effort is None, "model selection must preserve fixed member effort"
    assert len(solver.requests) == (0 if model_policy == "pinned" else 1)
    assert mapping == ([] if model_policy == "pinned" else [("effort_worker", 2)])
    assert [m.kind for m in await teams.teams._messages_of(task.id)] == ["compressed summary"]


async def test_manual_compaction_evaluation_failure_keeps_dialogue_and_never_uses_auto(teams, solver, mapping):
    await auto_member(teams, effort="low")
    task = await teams.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="Existing dialogue", brief="Implementation instructions",
    )
    await teams.teams.record_message(task.id, DEV, role="assistant", kind="text", text="Keep this dialogue")
    before = await teams.teams._messages_of(task.id)
    solver.error = "evaluation unavailable"
    with pytest.raises(RuntimeError, match="Orchestrator"):
        await teams.teams.compress_task(task.id)
    assert not Worker.requests
    assert [m.id for m in await teams.teams._messages_of(task.id)] == [m.id for m in before]
    assert (await teams.teams._reload(task.id)).model is None
