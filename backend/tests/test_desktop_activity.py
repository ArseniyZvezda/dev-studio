"""App-level exit guard sees all resident work, not UI selection (§5.3.1 D3)."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import httpx
import pytest

from voxarium_studio.core import workspace
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.project import init_project
from voxarium_studio.core.project_scope import ProjectDescriptor, resolve_project
from voxarium_studio.engine.jobs import JobEngine
from voxarium_studio.main import create_app


COUNT_KEYS = ("pipeline_runs", "tasks", "chats", "subagents", "other_jobs", "commit_drafts")


def counts(**values):
    return dict.fromkeys(COUNT_KEYS, 0) | values


def fail(*args, **kwargs):
    raise RuntimeError("private diagnostic must not escape in API data")


class BrokenMapping(dict):
    def values(self):
        return fail()

    def items(self):
        return fail()


def unit_state(tmp_path, *, jobs=None):
    """Minimal published descriptors for deterministic source fault injection."""
    ctx = SimpleNamespace(
        jobs=jobs if jobs is not None else JobEngine(),
        teams=SimpleNamespace(subagents=SimpleNamespace(runs={}), _commit_jobs={}),
        config=SimpleNamespace(project_name="Game A"),
        paths=SimpleNamespace(root=tmp_path / "a"),
    )
    descriptor = ProjectDescriptor("A", "epoch", tmp_path / "a", ctx)
    return SimpleNamespace(project_epoch="epoch", project_registry={"A": descriptor}), ctx


def test_idle_snapshot_is_synchronous_and_has_complete_schema(tmp_path):
    state, _ = unit_state(tmp_path)
    assert not inspect.iscoroutinefunction(workspace.workspace_activity)
    assert workspace.workspace_activity(state) == {
        "epoch": "epoch", "status": "idle", "requires_confirmation": False,
        "errors": [], "projects": [{
            "project_id": "A", "root": str(tmp_path / "a"), "name": "Game A",
            "status": "idle", "counts": counts(), "errors": [],
        }],
    }


@pytest.mark.parametrize("registry", [None, {}, BrokenMapping()])
def test_unavailable_or_empty_registry_is_unknown(tmp_path, registry):
    state = SimpleNamespace(project_epoch="epoch")
    if registry is not None:
        state.project_registry = registry
    result = workspace.workspace_activity(state)
    assert result == {
        "epoch": "epoch", "status": "unknown", "requires_confirmation": True,
        "projects": [], "errors": ["project_registry"],
    }


def test_before_startup_has_no_epoch_and_is_not_idle():
    result = workspace.workspace_activity(SimpleNamespace())
    assert result["epoch"] is None
    assert result["status"] == "unknown" and result["requires_confirmation"]


@pytest.mark.parametrize("kind,key", [
    ("pipeline_run", "pipeline_runs"), ("agent_task", "tasks"),
    ("agent_chat", "chats"), ("subagent", "subagents"),
    ("node", "other_jobs"), ("future_kind", "other_jobs"),
])
def test_registered_handles_count_without_task_or_running_predicate(tmp_path, kind, key):
    state, ctx = unit_state(tmp_path)
    ctx.jobs.register("owner", kind)
    ctx.jobs.is_running = fail
    result = workspace.workspace_activity(state)
    assert result["status"] == "active" and result["requires_confirmation"]
    assert result["projects"][0]["counts"] == counts(**{key: 1})
    assert not result["projects"][0]["errors"]


async def test_finishing_and_removed_handles_remain_conservative_in_captured_snapshot(tmp_path, monkeypatch):
    state, ctx = unit_state(tmp_path)
    finished = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    handle = ctx.jobs.register("ending", "agent_task")
    handle.task = finished
    original_jobs = ctx.jobs.jobs

    def snapshot_then_unregister():
        captured = original_jobs()
        ctx.jobs._jobs.clear()
        return captured

    monkeypatch.setattr(ctx.jobs, "jobs", snapshot_then_unregister)
    first = workspace.workspace_activity(state)
    assert first["projects"][0]["counts"] == counts(tasks=1)
    assert first["requires_confirmation"]
    assert workspace.workspace_activity(state)["status"] == "idle"
    assert first["projects"][0]["counts"] == counts(tasks=1)


async def test_subagent_union_and_commit_drafts_ignore_done_but_not_pending(tmp_path):
    state, ctx = unit_state(tmp_path)
    loop = asyncio.get_running_loop()
    pending, done = loop.create_future(), loop.create_future()
    done.set_result(None)
    ctx.jobs.register("shared", "subagent")
    ctx.teams.subagents.runs = {
        "owner": SimpleNamespace(jobs={"shared": pending, "extra": pending, "done": done}),
        "other": SimpleNamespace(jobs={"extra": pending}),
    }
    ctx.teams._commit_jobs = {"draft": pending, "finished": done}
    result = workspace.workspace_activity(state)
    assert result["projects"][0]["counts"] == counts(subagents=2, commit_drafts=1)
    pending.set_result(None)
    ctx.jobs._jobs.clear()
    assert workspace.workspace_activity(state)["status"] == "idle"


@pytest.mark.parametrize("source", ["jobs", "subagents", "commit_drafts", "name"])
@pytest.mark.parametrize("active", [False, True])
def test_source_failure_never_reports_idle_and_preserves_known_work(tmp_path, source, active):
    state, ctx = unit_state(tmp_path)
    if active:
        # This independent source survives even when the job registry fails.
        ctx.teams._commit_jobs["draft"] = SimpleNamespace(done=lambda: False)
        ctx.jobs.register("pipeline", "pipeline_run")
    if source == "jobs":
        ctx.jobs.jobs = fail
    elif source == "subagents":
        ctx.teams.subagents.runs = BrokenMapping()
    elif source == "commit_drafts":
        ctx.teams._commit_jobs = BrokenMapping()
    else:
        ctx.config = None
    result = workspace.workspace_activity(state)
    project = result["projects"][0]
    assert result["status"] == project["status"] == ("active" if active else "unknown")
    assert result["requires_confirmation"]
    assert project["errors"] == [source]
    assert result["errors"] == []
    assert "private diagnostic" not in str(result)


def test_broken_context_does_not_hide_other_residents(tmp_path):
    state, ctx = unit_state(tmp_path)
    ctx.jobs.register("work", "agent_chat")
    state.project_registry = {
        "broken": ProjectDescriptor("broken", "epoch", tmp_path / "broken", None),
        **state.project_registry,
    }
    result = workspace.workspace_activity(state)
    assert result["status"] == "active" and result["requires_confirmation"]
    projects = {p["project_id"]: p for p in result["projects"]}
    assert projects["broken"]["status"] == "unknown"
    assert projects["A"]["counts"] == counts(chats=1)


@pytest.fixture
async def residents(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    init_project(other)
    app = create_app(str(project))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
        ) as http:
            a = next(iter(app.state.project_registry.values()))
            response = await http.post("/api/projects/open", json={"path": str(other)})
            assert response.status_code == 200, response.text
            b = resolve_project(app.state, response.json()["project_id"])
            response = await http.post("/api/project/open", json={"path": str(other)})
            assert response.status_code == 200, response.text
            yield app, http, a, b


async def test_real_running_and_slot_queued_jobs_include_hidden_resident_with_copied_ids(residents):
    app, http, a, b = residents
    assert isinstance(a.ctx, AppContext) and isinstance(b.ctx, AppContext)
    assert app.state.ctx is b.ctx and a.ctx.bus.forward_to is None
    gates, tasks = [], []
    try:
        for scope in (a, b):
            scope.ctx.jobs.update_limits({"activity_test": 1})
            release, entered, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
            gates.append(release)

            async def worker(_handle, *, engine=scope.ctx.jobs, release=release, entered=entered):
                async with engine.slot("activity_test"):
                    entered.set()
                    await release.wait()

            async def waiter(_handle, *, engine=scope.ctx.jobs, release=release, queued=queued):
                async def on_wait(_limit):
                    queued.set()
                async with engine.slot("activity_test", on_wait=on_wait):
                    await release.wait()

            tasks.append(scope.ctx.jobs.submit("copied-owner", "agent_task", worker).task)
            await asyncio.wait_for(entered.wait(), 3)
            tasks.append(scope.ctx.jobs.submit("copied-chat", "agent_chat", waiter).task)
            await asyncio.wait_for(queued.wait(), 3)
            for job_id, kind in (("copied-pipeline", "pipeline_run"), ("copied-subagent", "subagent")):
                started = asyncio.Event()

                async def gated(_handle, *, release=release, started=started):
                    started.set()
                    await release.wait()

                handle = scope.ctx.jobs.submit(job_id, kind, gated)
                tasks.append(handle.task)
                await asyncio.wait_for(started.wait(), 3)
                assert scope.ctx.jobs.is_running(job_id)
        response = await http.get("/api/projects/activity")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        assert result["epoch"] == app.state.project_epoch
        assert result["status"] == "active" and result["requires_confirmation"]
        assert len(result["projects"]) == 2
        assert {p["project_id"] for p in result["projects"]} == {a.project_id, b.project_id}
        for p in result["projects"]:
            assert p["counts"] == counts(pipeline_runs=1, tasks=1, chats=1, subagents=1)
            assert p["status"] == "active" and not p["errors"]
        # Completion/removal in A does not erase B's identical owner IDs.
        gates[0].set()
        await asyncio.wait_for(asyncio.gather(*tasks[:4]), 3)
        snapshot = (await http.get("/api/projects/activity")).json()
        by_id = {p["project_id"]: p for p in snapshot["projects"]}
        assert by_id[a.project_id]["status"] == "idle"
        assert by_id[a.project_id]["counts"] == counts()
        assert by_id[b.project_id]["counts"] == counts(pipeline_runs=1, tasks=1, chats=1, subagents=1)
        assert all(not task.done() for task in tasks[4:])
        gates[1].set()
        await asyncio.wait_for(asyncio.gather(*tasks[4:]), 3)
    finally:
        for gate in gates:
            gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
    result = (await http.get("/api/projects/activity")).json()
    assert result["status"] == "idle" and not result["requires_confirmation"]


@pytest.mark.parametrize("known_work", [False, True])
async def test_transition_guard_returns_without_waiting_for_lock(residents, known_work):
    app, http, a, b = residents
    lock = app.state.project_lock
    registry = dict(app.state.project_registry)
    epoch = app.state.project_epoch
    assert not lock.locked()
    before = (await http.get("/api/projects/activity")).json()
    assert before["status"] == "idle" and not before["requires_confirmation"]
    if known_work:
        a.ctx.jobs.register("transition-work", "pipeline_run")
    try:
        async with lock:
            # Direct snapshot and ASGI GET must both complete while we own the
            # lock: waiting for a startup/publication transition would deadlock.
            direct = workspace.workspace_activity(app.state)
            response = await asyncio.wait_for(http.get("/api/projects/activity"), 3)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == direct
            assert direct["errors"] == ["project_transition"]
            assert direct["status"] == ("active" if known_work else "unknown")
            assert direct["requires_confirmation"]
            by_id = {p["project_id"]: p for p in direct["projects"]}
            assert by_id[a.project_id]["counts"] == counts(pipeline_runs=int(known_work))
            assert by_id[b.project_id]["counts"] == counts()
            assert all(not p["errors"] for p in direct["projects"])
            assert lock.locked()
        assert not lock.locked()
        after = (await http.get("/api/projects/activity")).json()
        assert after["errors"] == []
        assert after["status"] == ("active" if known_work else "idle")
        assert after["requires_confirmation"] is known_work
        assert app.state.project_registry == registry
        assert app.state.project_epoch == epoch
        assert app.state.ctx is b.ctx
    finally:
        a.ctx.jobs._jobs.pop("transition-work", None)
    assert (await http.get("/api/projects/activity")).json()["status"] == "idle"


async def test_activity_is_read_only_and_legacy_and_scoped_bindings_are_unchanged(residents, monkeypatch):
    app, http, a, b = residents
    selected, bus = app.state.ctx, app.state.browser_bus
    registry = dict(app.state.project_registry)
    before = {str(s.root): {str(p.relative_to(s.root)): p.stat().st_mtime_ns
                           for p in s.root.rglob("*") if p.is_file()} for s in (a, b)}
    with monkeypatch.context() as patches:
        for name in ("ensure_project", "open_project", "initialise_workspace", "load_recent", "record_recent", "read_task_counts"):
            patches.setattr(workspace, name, fail)
        patches.setattr(AppContext, "create", fail)
        patches.setattr(AppContext, "startup", fail)
        for scope in (a, b):
            patches.setattr(scope.ctx.db, "session", fail)
            patches.setattr(scope.ctx.jobs, "is_running", fail)
        for _ in range(2):
            response = await http.get("/api/projects/activity")
            assert response.status_code == 200
            assert response.json()["status"] == "idle"
    assert app.state.ctx is selected and app.state.browser_bus is bus
    assert app.state.project_registry == registry
    after = {str(s.root): {str(p.relative_to(s.root)): p.stat().st_mtime_ns
                          for p in s.root.rglob("*") if p.is_file()} for s in (a, b)}
    assert before == after
    assert (await http.get("/api/project")).json()["root"] == str(b.ctx.paths.root)
    for scope in (a, b):
        response = await http.get(f"/api/projects/{scope.project_id}/project")
        assert response.status_code == 200
        assert response.json()["project_id"] == scope.project_id
        for suffix in ("activity", "projects/activity", "project/activity"):
            assert (await http.get(f"/api/projects/{scope.project_id}/{suffix}")).status_code == 404
    assert app.state.ctx is b.ctx


async def test_http_partial_failure_preserves_residents_and_requires_confirmation(residents, monkeypatch):
    app, http, a, b = residents
    with monkeypatch.context() as patches:
        patches.setattr(a.ctx.jobs, "jobs", fail)
        response = await http.get("/api/projects/activity")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        assert result["status"] == "unknown" and result["requires_confirmation"]
        by_id = {p["project_id"]: p for p in result["projects"]}
        assert by_id[a.project_id]["errors"] == ["jobs"]
        assert by_id[b.project_id]["status"] == "idle"
        assert app.state.ctx is b.ctx
    assert (await http.get("/api/projects/activity")).json()["status"] == "idle"


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"}, {"Host": "localhost.attacker.example"},
    {"Origin": "https://attacker.example"}, {"Origin": "null"},
    {"Origin": "http://127.0.0.1:9999"},
])
def test_activity_uses_existing_host_and_origin_guards(client, headers):
    assert client.get("/api/projects/activity", headers=headers).status_code == 403


def test_activity_uses_existing_token_guard(token_client):
    assert token_client.get("/api/projects/activity").status_code == 401
    assert token_client.get("/api/projects/activity", headers={"Authorization": "Bearer wrong"}).status_code == 401
    headers = {"Authorization": "Bearer t0ken", "Origin": "http://127.0.0.1"}
    assert token_client.get("/api/projects/activity", headers=headers).status_code == 200
    headers["Origin"] = "https://attacker.example"
    assert token_client.get("/api/projects/activity", headers=headers).status_code == 403
