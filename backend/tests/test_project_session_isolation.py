"""Copied owner IDs never join resident projects' runtimes (dev spec §5.3.2)."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from voxarium_studio.main import create_app
from voxarium_studio.core.project import init_project
from voxarium_studio.core.project_scope import resolve_project
from voxarium_studio.models import AgentChat, TeamMember, TeamTask, TaskStatus
from voxarium_studio.models.task_context import ChatSessionContext, TaskContext
from voxarium_studio.teams.backends import base
from voxarium_studio.teams.context_sources import source
from voxarium_studio.teams.context_viewer import fragment_key

TEAM, DEV, OWNER = "team_default", "team_default__dev", "copied_owner"
SID = "copied-native-session"


@pytest.fixture(autouse=True)
def isolated_runtime_registries(monkeypatch):
    monkeypatch.setattr(base, "_LIVE_SESSIONS", {})
    monkeypatch.setattr(base, "_CONTEXT_INSPECTORS", {})


def _control(label: str):
    return SimpleNamespace(
        get_context_usage=AsyncMock(return_value={
            "totalTokens": 100, "maxTokens": 2000,
            "categories": [{"name": label, "tokens": 100}],
        }),
        enable_remote_control=AsyncMock(return_value=base.RemoteControl(
            session_url=f"https://claude.ai/code/{label}",
        )),
        disable_remote_control=AsyncMock(),
    )


def _inspector(label: str):
    return SimpleNamespace(
        ready=True, session_id=SID,
        context_report=AsyncMock(return_value=f"{label} 100 tokens (100%)\nUsed 100 tokens (100%)"),
    )


@pytest.mark.parametrize("kind", ["task", "chat", "subagent"])
def test_runtime_owner_canonical_alias_and_immutable_identity(tmp_path, kind):
    root = tmp_path / "project"
    root.mkdir()
    owner = base.runtime_session_owner(root, kind, OWNER)
    alias = base.runtime_session_owner(root / ".." / "project" / ".", kind, OWNER)
    assert owner == alias and hash(owner) == hash(alias)
    assert owner.project_root == root.resolve()
    with pytest.raises(FrozenInstanceError):
        owner.owner_id = "other"


def test_copied_ids_and_owner_kinds_have_six_independent_runtime_addresses(tmp_path):
    owners = [base.runtime_session_owner(tmp_path / project, kind, OWNER)
              for project in ("a", "b") for kind in ("task", "chat", "subagent")]
    controls, inspectors, keys = [], [], []
    for i, owner in enumerate(owners):
        key = base.RuntimeSessionKey(owner)
        control, inspector = _control(str(i)), _inspector(str(i))
        base.register_live_session(key, control)
        base.register_context_inspector(key, inspector)
        keys.append(key)
        controls.append(control)
        inspectors.append(inspector)
    for owner, control, inspector in zip(owners, controls, inspectors, strict=True):
        assert base.live_session(owner) is control
        assert base.context_inspector(owner, SID) is inspector
        assert base.context_inspector(owner, "different-native-session") is None
    # Completing A, including all its owner kinds, cannot touch B.
    for key in keys[:3]:
        base.forget_live_session(key)
        base.forget_context_inspector(key)
    for owner, control, inspector in zip(owners[3:], controls[3:], inspectors[3:], strict=True):
        assert base.live_session(owner) is control
        assert base.context_inspector(owner, SID) is inspector


@pytest.mark.parametrize("kind", ["task", "chat", "subagent"])
def test_old_generation_cleanup_cannot_remove_new_runtime_or_inspector(tmp_path, kind):
    owner = base.runtime_session_owner(tmp_path, kind, OWNER)
    old, new = base.RuntimeSessionKey(owner), base.RuntimeSessionKey(owner)
    assert old != new and old.generation != new.generation
    base.register_live_session(old, _control("old"))
    base.register_context_inspector(old, _inspector("old"))
    control, inspector = _control("new"), _inspector("new")
    base.register_live_session(new, control)
    base.register_context_inspector(new, inspector)
    base.forget_live_session(old)
    base.forget_context_inspector(old)
    base.forget_live_session(OWNER)
    base.forget_context_inspector(OWNER)
    assert base.live_session(owner) is control
    assert base.context_inspector(owner, SID) is inspector
    base.forget_live_session(new)
    base.forget_context_inspector(new)
    assert base.live_session(owner) is None
    assert base.context_inspector(owner, SID) is None


def test_scoped_lookup_never_falls_back_to_raw_id(tmp_path):
    control, inspector = _control("legacy"), _inspector("legacy")
    base.register_live_session(OWNER, control)
    base.register_context_inspector(OWNER, inspector)
    assert base.live_session(OWNER) is control
    assert base.context_inspector(OWNER, SID) is inspector
    for kind in ("task", "chat", "subagent"):
        owner = base.runtime_session_owner(tmp_path, kind, OWNER)
        assert base.live_session(owner) is None
        assert base.context_inspector(owner, SID) is None


@pytest.fixture
async def residents(project, tmp_path):
    other = tmp_path / "other-project"
    other.mkdir()
    init_project(other)
    app = create_app(str(project))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
            scopes = []
            for label, root in (("A", project), ("B", other)):
                response = await http.post("/api/projects/open", json={"path": str(root)})
                assert response.status_code == 200, response.text
                descriptor = response.json()
                ctx = resolve_project(app.state, descriptor["project_id"]).ctx
                async with ctx.db.session() as session:
                    session.add(TeamTask(id=OWNER, number=1000, team_id=TEAM, assignee_id=DEV,
                                         title=f"Task {label}", auto_start=False, agent_session_id=SID))
                    session.add(AgentChat(id=OWNER, member_id=DEV, title=f"Chat {label}", agent_session_id=SID))
                async with ctx.db.session() as session:
                    for kind, model, field in (("task", TaskContext, "task_id"), ("chat", ChatSessionContext, "chat_id")):
                        session.add(model(**{field: OWNER}, sources=[source("system", f"{label}-{kind}-private", "Instructions")]))
                scopes.append(SimpleNamespace(label=label, ctx=ctx, prefix=f"/api/projects/{descriptor['project_id']}"))
            yield app, http, scopes


def _path(scope, kind, tail="context"):
    owner = f"teams/{TEAM}/tasks/{OWNER}" if kind == "task" else f"chats/{OWNER}"
    return f"{scope.prefix}/{owner}/{tail}"


@pytest.mark.parametrize("runtime", ["live", "inspector"])
async def test_context_viewer_scoped_and_legacy_use_project_and_kind(residents, runtime):
    app, http, scopes = residents
    registered = {}
    for scope in scopes:
        for kind in ("task", "chat"):
            owner = base.runtime_session_owner(scope.ctx.paths.root, kind, OWNER)
            key = base.RuntimeSessionKey(owner)
            label = f"{scope.label}-{kind}"
            obj = _control(label) if runtime == "live" else _inspector(label)
            (base.register_live_session if runtime == "live" else base.register_context_inspector)(key, obj)
            registered[scope.label, kind] = obj
    # Legacy switches to B while immutable scoped A remains fully usable.
    response = await http.post("/api/project/open", json={"path": str(scopes[1].ctx.paths.root)})
    assert response.status_code == 200, response.text
    assert app.state.ctx is scopes[1].ctx
    for scope in scopes:
        for kind in ("task", "chat"):
            inventory = await http.get(_path(scope, kind), params={"view": "studio"})
            assert inventory.status_code == 200, inventory.text
            assert inventory.json()["can_inspect"] is True
            part = inventory.json()["sources"][0]
            assert part["text"] == f"{scope.label}-{kind}-private"
            fragment = await http.get(_path(scope, kind, "context/fragments/0"), params={
                "view": "studio", "key": fragment_key(part),
            })
            assert fragment.status_code == 200, fragment.text
            assert fragment.json() == {"text": part["text"]}
            inspected = await http.post(_path(scope, kind, "context/inspect"))
            assert inspected.status_code == 200, inspected.text
            assert f"{scope.label}-{kind}" in str(inspected.json()["sources"])
    for kind in ("task", "chat"):
        legacy = SimpleNamespace(prefix="/api")
        inventory = await http.get(_path(legacy, kind), params={"view": "studio"})
        assert inventory.status_code == 200 and inventory.json()["can_inspect"]
        assert inventory.json()["sources"][0]["text"] == f"B-{kind}-private"
        inspected = await http.post(_path(legacy, kind, "context/inspect"))
        assert inspected.status_code == 200 and f"B-{kind}" in str(inspected.json()["sources"])
    for (label, _), obj in registered.items():
        method = obj.get_context_usage if runtime == "live" else obj.context_report
        assert method.await_count == (2 if label == "B" else 1)


async def test_context_and_control_do_not_use_other_project_kind_or_raw_id(residents):
    _, http, (a, b) = residents
    control = _control("only-B-task")
    base.register_live_session(OWNER, _control("unscoped-decoy"))
    base.register_live_session(base.RuntimeSessionKey(base.runtime_session_owner(b.ctx.paths.root, "task", OWNER)), control)
    base.register_context_inspector(OWNER, _inspector("unscoped-decoy"))
    for scope, kind in ((a, "task"), (a, "chat"), (b, "chat")):
        inventory = await http.get(_path(scope, kind), params={"view": "studio"})
        assert inventory.status_code == 200 and not inventory.json()["can_inspect"]
        inspected = await http.post(_path(scope, kind, "context/inspect"))
        assert inspected.status_code == 409, inspected.text
    response = await http.post(_path(a, "task", "remote-control"))
    assert response.status_code == 409, response.text
    control.enable_remote_control.assert_not_awaited()


async def test_control_and_completion_are_project_local_including_legacy(residents):
    _, http, (a, b) = residents
    controls, keys = {}, {}
    for scope in (a, b):
        control = _control(scope.label)
        key = base.RuntimeSessionKey(base.runtime_session_owner(scope.ctx.paths.root, "task", OWNER))
        base.register_live_session(key, control)
        controls[scope.label], keys[scope.label] = control, key
    responses = await asyncio.gather(*[http.post(_path(s, "task", "remote-control")) for s in (a, b)])
    for scope, response in zip((a, b), responses, strict=True):
        assert response.status_code == 200, response.text
        assert response.json()["session_url"] == f"https://claude.ai/code/{scope.label}"
        async with scope.ctx.db.session() as session:
            assert (await session.get(TeamTask, OWNER)).remote_control_url == response.json()["session_url"]
    # The old API still follows only the selected project, initially A.
    response = await http.delete(_path(SimpleNamespace(prefix="/api"), "task", "remote-control"))
    assert response.status_code == 200, response.text
    controls["A"].disable_remote_control.assert_awaited_once()
    controls["B"].disable_remote_control.assert_not_awaited()
    base.forget_live_session(keys["A"])
    assert (await http.post(_path(a, "task", "remote-control"))).status_code == 409
    assert (await http.post(_path(a, "task", "context/inspect"))).status_code == 409
    response = await http.post(_path(b, "task", "remote-control"))
    assert response.status_code == 200 and response.json()["session_url"].endswith("/B")
    inspected = await http.post(_path(b, "task", "context/inspect"))
    assert inspected.status_code == 200, inspected.text
    assert (await http.post("/api/project/open", json={"path": str(b.ctx.paths.root)})).status_code == 200
    legacy = SimpleNamespace(prefix="/api")
    response = await http.post(_path(legacy, "task", "remote-control"))
    assert response.status_code == 200 and response.json()["session_url"].endswith("/B")
    assert (await http.delete(_path(legacy, "task", "remote-control"))).status_code == 200
    controls["B"].disable_remote_control.assert_awaited_once()


async def _remote_state(ctx):
    """Include durable run metadata and notices, not just the visible URL."""
    async with ctx.db.session() as session:
        task = await session.get(TeamTask, OWNER)
        task_state = task.model_dump(mode="json")
    messages = await ctx.teams._messages_of(OWNER)
    return task_state, [message.model_dump(mode="json") for message in messages
                        if message.kind == "remote_control"]


@pytest.mark.parametrize("operation", ["enable", "disable"])
async def test_stale_remote_control_response_cannot_change_new_generation(residents, operation):
    """A pending old RPC must not publish state for a replacement run (§5.3.2)."""
    _, _, (a, _) = residents
    service = a.ctx.teams
    owner = base.runtime_session_owner(a.ctx.paths.root, "task", OWNER)
    old_key, new_key = base.RuntimeSessionKey(owner), base.RuntimeSessionKey(owner)
    old_control, new_control = _control("old-run"), _control("new-run")
    new_link = base.RemoteControl(
        session_url="https://claude.ai/code/new-run",
        connect_url="https://claude.ai/code/connect/new-run",
        environment_id="new-run-environment",
    )
    new_control.enable_remote_control.return_value = new_link
    base.register_live_session(old_key, old_control)
    # DELETE starts with an actual old link; the later generation replaces it.
    if operation == "disable":
        await service.enable_remote_control(OWNER)

    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_rpc(*args, **kwargs):
        entered.set()
        await release.wait()
        return base.RemoteControl("https://claude.ai/code/stale-response")

    getattr(old_control, f"{operation}_remote_control").side_effect = delayed_rpc
    pending = asyncio.create_task(getattr(service, f"{operation}_remote_control")(OWNER))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        # Establish the replacement while the old RPC is definitely suspended.
        base.register_live_session(new_key, new_control)
        async with a.ctx.db.session() as session:
            task = await session.get(TeamTask, OWNER)
            task.agent_session_id = "new-generation-native-session"
            task.title = "Replacement generation"
            session.add(task)
        assert await service.enable_remote_control(OWNER) == new_link
        expected = await _remote_state(a.ctx)
        assert expected[0]["remote_control_url"] == new_link.session_url
        assert expected[1][-1]["data"]["environment_id"] == new_link.environment_id
        release.set()
        if operation == "enable":
            # Returning the old URL as success would itself misdirect the caller.
            with pytest.raises(ValueError):
                await asyncio.wait_for(asyncio.shield(pending), 10)
        else:
            # An obsolete DELETE may refuse or become a no-op, never touch the new run.
            try:
                await asyncio.wait_for(asyncio.shield(pending), 10)
            except ValueError:
                pass
        assert await _remote_state(a.ctx) == expected
        base.forget_live_session(old_key)
        assert base.live_session(owner) is new_control
        new_control.enable_remote_control.assert_awaited_once()
        new_control.disable_remote_control.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 10)


async def test_stale_remote_cleanup_preserves_new_generation_metadata(residents):
    """Cleanup scoped to an old control cannot erase the replacement's link."""
    _, _, (a, _) = residents
    owner = base.runtime_session_owner(a.ctx.paths.root, "task", OWNER)
    old_key, new_key = base.RuntimeSessionKey(owner), base.RuntimeSessionKey(owner)
    old_control, new_control = _control("old-run"), _control("new-run")
    base.register_live_session(old_key, old_control)
    await a.ctx.teams.enable_remote_control(OWNER)
    base.register_live_session(new_key, new_control)
    await a.ctx.teams.enable_remote_control(OWNER)
    expected = await _remote_state(a.ctx)
    assert await a.ctx.teams._clear_remote_control(OWNER, expected_runtime_key=old_key) is False
    # The ended-run finally path has no live key either; it must also fail closed.
    assert await a.ctx.teams._clear_remote_control(OWNER) is False
    assert await _remote_state(a.ctx) == expected
    base.forget_live_session(old_key)
    assert base.live_session(owner) is new_control
    new_control.disable_remote_control.assert_not_awaited()


async def test_existing_run_bound_mcp_toolset_stays_on_creator_project_after_legacy_switch(residents):
    from voxarium_studio.teams.tools import build_toolset

    _, http, (a, b) = residents
    toolsets = []
    for scope in (a, b):
        async with scope.ctx.db.session() as session:
            member, task = await session.get(TeamMember, DEV), await session.get(TeamTask, OWNER)
        toolset = build_toolset(scope.ctx.teams, member=member, task=task, run_id="copied-run-id")
        assert toolset is not None, "MCP regression requires the teams extra"
        toolsets.append(toolset)
    assert (await http.post("/api/project/open", json={"path": str(b.ctx.paths.root)})).status_code == 200
    for scope, toolset in zip((a, b), toolsets, strict=True):
        definition = next(d for d in toolset.definitions if d.name == "get_task")
        result = await definition.handler({"task": OWNER})
        assert not result.get("is_error"), result
        text = str(result["content"])
        assert f"Task {scope.label}" in text
        assert f"Task {'B' if scope.label == 'A' else 'A'}" not in text


@pytest.mark.parametrize("kind", ["task", "chat", "subagent"])
async def test_real_runs_with_copied_ids_keep_b_live_after_a_completes(residents, monkeypatch, kind):
    """Exercise request construction and finally-cleanup, not only manual registration."""
    from voxarium_studio.teams.chat import UserContext
    from voxarium_studio.api.session_context import inspect_session
    from voxarium_studio.models.subagents import TaskSubagent
    from voxarium_studio.teams.subagents import ParentRun

    _, http, scopes = residents
    started = {scope.ctx.paths.root: asyncio.Event() for scope in scopes}
    release = {root: asyncio.Event() for root in started}
    requests, controls = {}, {}

    class GatedBackend:
        named_agents = False

        def is_available(self):
            return True, "test backend"

        async def run(self, request):
            assert request.runtime_key is not None
            root = request.runtime_key.owner.project_root
            assert request.runtime_key.owner == base.runtime_session_owner(root, kind, OWNER)
            assert request.session_key == OWNER, "Runtime identity must not replace the saved session key"
            requests[root] = request
            control = controls[root] = _control(root.name)
            base.register_live_session(request.runtime_session_key, control)
            started[root].set()
            try:
                yield base.AgentEvent(kind="text", text="working", session_id=SID)
                await release[root].wait()
                yield base.AgentEvent(kind="result", text="Complete", session_id=SID, is_final=True)
            finally:
                base.forget_live_session(request.runtime_session_key)

    monkeypatch.setitem(base._BACKENDS, "project_isolation_gate", GatedBackend)
    jobs = []
    try:
        for scope in scopes:
            ctx = scope.ctx
            ctx.config.teams.result_follow_up = False
            ctx.config.teams.context_compression_tokens = 0
            async with ctx.db.session() as session:
                member = await session.get(TeamMember, DEV)
                member.agent_backend = "project_isolation_gate"
                member.isolate_worktree = False
                session.add(member)
            if kind == "task":
                await ctx.teams.update_task(OWNER, {"status": TaskStatus.PENDING})
                await ctx.teams.start_task(OWNER)
            elif kind == "chat":
                await ctx.chats.send(OWNER, "Work on this project", UserContext())
            else:
                # Use the real child request builder with a copied row and a parent
                # address that replace() must NOT accidentally inherit.
                async with ctx.db.session() as session:
                    task = await session.get(TeamTask, OWNER)
                    row = TaskSubagent(id=OWNER, task_id=OWNER, member_id=DEV,
                                       description="Copied child", backend="project_isolation_gate", model="test")
                    session.add(row)
                parent = base.AgentRunRequest(member=member, task=task, workdir=ctx.paths.root,
                                              prompt="Parent", system_prompt="Instructions",
                                              runtime_key=base.RuntimeSessionKey(
                                                  base.runtime_session_owner(ctx.paths.root, "task", OWNER)))
                run = ParentRun(request=parent, lease=None, run_id="copied-parent-run")
                jobs.append(asyncio.create_task(ctx.teams.subagents._execute(run, row, "Child work")))
                continue
            jobs.append(ctx.jobs.get(OWNER).task)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 15)
        a, b = scopes
        assert requests[a.ctx.paths.root].runtime_key != requests[b.ctx.paths.root].runtime_key
        for scope in scopes:
            result = await inspect_session(base.runtime_session_owner(scope.ctx.paths.root, kind, OWNER), SID)
            assert result["can_inspect"]
            if kind != "subagent":
                inspected = await http.post(_path(scope, kind, "context/inspect"))
                assert inspected.status_code == 200, inspected.text
        release[a.ctx.paths.root].set()
        await asyncio.wait_for(asyncio.shield(jobs[0]), 15)
        if kind == "subagent":
            async with a.ctx.db.session() as session:
                completed = await session.get(TaskSubagent, OWNER)
                assert completed.status == "completed" and completed.summary == "Complete"
        assert base.live_session(base.runtime_session_owner(a.ctx.paths.root, kind, OWNER)) is None
        assert not jobs[1].done()
        assert base.live_session(base.runtime_session_owner(b.ctx.paths.root, kind, OWNER)) is controls[b.ctx.paths.root]
        with pytest.raises(HTTPException) as unavailable:
            await inspect_session(base.runtime_session_owner(a.ctx.paths.root, kind, OWNER), SID)
        assert unavailable.value.status_code == 409
        assert (await inspect_session(base.runtime_session_owner(b.ctx.paths.root, kind, OWNER), SID))["can_inspect"]
        if kind != "subagent":
            assert (await http.post(_path(a, kind, "context/inspect"))).status_code == 409
            assert (await http.post(_path(b, kind, "context/inspect"))).status_code == 200
    finally:
        for event in release.values():
            event.set()
        if jobs:
            await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 20)
