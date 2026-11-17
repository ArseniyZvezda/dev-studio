"""Opening another managed project at runtime (§5), and the folder picking behind it."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from voxarium_studio.core import browse, demo, workspace
from voxarium_studio.core.config import load_config
from voxarium_studio.core.paths import DATA_DIRNAME, HOME_ENV_VAR, ProjectPaths
from voxarium_studio.core.project import init_project
from voxarium_studio.core.project_scope import resolve_project
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.main import create_app
from voxarium_studio.models.teams import TaskStatus, TeamMember, TeamTask
from voxarium_studio.teams.backends.base import AgentEvent, register_backend
from voxarium_studio.sync.reservations import ReservationError

# ---------------------------------------------------------------- switching projects


async def test_bootstrap_identity_is_immutable_and_initialisation_is_idempotent(ctx) -> None:
    state = SimpleNamespace(ctx=ctx, project_lock=asyncio.Lock())
    workspace.initialise_workspace(state)
    descriptor = await workspace.ensure_project(state, ctx.paths.root)
    bus = state.browser_bus
    reservations = state.remote_reservations
    lease = reservations.reserve(root=ctx.paths.root, url="remote.example", persistent_id="fixture")
    workspace.initialise_workspace(state)
    assert resolve_project(state, descriptor.project_id) is descriptor
    assert state.browser_bus is bus
    assert state.remote_reservations is reservations
    assert ctx.remote_reservations is reservations
    assert reservations.reserve(
        root=ctx.paths.root, url="https://remote.example/", persistent_id="fixture",
    ) is lease
    assert reservations.release(lease)
    assert len(state.project_contexts) == len(state.project_registry) == 1
    with pytest.raises(FrozenInstanceError):
        descriptor.ctx = None


async def test_reservation_owner_survives_selection_without_stopping_either_context(
    ctx, tmp_path: Path,
) -> None:
    from voxarium_studio.core.project_scope import canonical_root

    state = SimpleNamespace(ctx=ctx, project_lock=asyncio.Lock())
    workspace.initialise_workspace(state)
    owner = state.remote_reservations
    lease = owner.reserve(root=ctx.paths.root, url="remote.example", persistent_id="shared")
    other = tmp_path / "copy"
    other.mkdir()
    try:
        replacement = await workspace.open_project(state, other)
        workspace.initialise_workspace(state)
        assert state.ctx is replacement
        assert replacement.remote_reservations is ctx.remote_reservations is owner
        assert state.remote_reservations is owner
        assert len(state.project_contexts) == 2
        with pytest.raises(ReservationError) as caught:
            owner.reserve(root=other, url="https://remote.example/", persistent_id="shared")
        assert caught.value.code == "duplicate_local_binding"
        assert caught.value.owner_root == canonical_root(ctx.paths.root)
        assert await workspace.open_project(state, ctx.paths.root / ".") is ctx
        workspace.initialise_workspace(state)
        assert state.remote_reservations is owner
        assert len(state.project_contexts) == 2
        assert owner.reserve(
            root=ctx.paths.root, url="remote.example", persistent_id="shared",
        ) is lease
        # Opening a copy or encountering a collision never retires a context.
        assert all(context.db is not None for context in state.project_contexts.values())
    finally:
        assert owner.release(lease)
        await workspace.shutdown_workspace(state)


@pytest.mark.parametrize("failure", [False, True])
async def test_shutdown_fences_reservations_before_await_and_keeps_live_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from voxarium_studio.sync.reservations import ReservationOwner
    from voxarium_studio.ws.bus import EventBus

    owner = ReservationOwner()
    root = tmp_path / "resident"
    lease = owner.reserve(root=root, url="remote.example", persistent_id="shared")
    admission_closed, stopping, finish_stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    close = owner.close_admission

    def close_admission():
        close()
        admission_closed.set()

    monkeypatch.setattr(owner, "close_admission", close_admission)

    fail_stop = failure

    async def shutdown():
        stopping.set()
        await finish_stop.wait()
        if fail_stop:
            raise RuntimeError("fixture shutdown failure")

    ctx = SimpleNamespace(
        shutdown=shutdown, auth=AuthSettings(), bus=EventBus(),
        paths=ProjectPaths.for_project(root),
    )
    lifecycle = SimpleNamespace(unregister_context=Mock(), register_context=Mock())
    state = SimpleNamespace(
        ctx=ctx, project_lock=asyncio.Lock(), remote_reservations=owner,
        project_contexts={root: ctx},
        project_registry={"fixture": SimpleNamespace(project_id="fixture", ctx=ctx)},
        project_roots={root: SimpleNamespace(project_id="fixture", ctx=ctx)},
        desktop_events=SimpleNamespace(close=AsyncMock()), remote_profile_lifecycle=lifecycle,
    )
    plugin_stop = AsyncMock()
    monkeypatch.setattr(workspace.plugin_envs, "shutdown", plugin_stop)
    await state.project_lock.acquire()
    task = asyncio.create_task(workspace.shutdown_workspace(state))
    try:
        await asyncio.wait_for(admission_closed.wait(), 2)
        assert not stopping.is_set()
        state.desktop_events.close.assert_not_awaited()
        with pytest.raises(ReservationError, match="reservation_admission_closed"):
            owner.reserve(root=tmp_path / "another", url="other.example", persistent_id="new")
        state.project_lock.release()
        await asyncio.wait_for(stopping.wait(), 2)
        assert state.project_contexts == {root: ctx}
        assert not task.done()
        finish_stop.set()
        if failure:
            with pytest.raises(RuntimeError, match="fixture shutdown failure"):
                await task
            lifecycle.unregister_context.assert_not_called()
        else:
            await task
            lifecycle.unregister_context.assert_called_once_with(ctx)
        plugin_stop.assert_awaited_once()
        if failure:
            assert state.project_contexts == {root: ctx}
            assert state.project_roots[root].ctx is ctx
            # A later successful retry joins the SAME context and retires it.
            fail_stop = False
            await workspace.shutdown_workspace(state)
            lifecycle.unregister_context.assert_called_once_with(ctx)
            assert not state.project_contexts
            assert not state.project_registry
            assert not state.project_roots
        else:
            assert not state.project_contexts
        assert state.remote_reservations is owner
        # Neither entering shutdown, stopping contexts nor failed stop releases it.
        assert owner.release(lease)
        assert not owner.release(lease)
        with pytest.raises(ReservationError, match="reservation_admission_closed"):
            owner.reserve(root=root, url="remote.example", persistent_id="shared")
        # An ordinary repeated initialization, even after shutdown, keeps the
        # closed owner. A failed stop also fences a NEW bootstrap context.
        if failure:
            state.ctx = SimpleNamespace(**vars(ctx))
        monkeypatch.setattr(
            workspace, "DesktopEventSource", lambda _state: SimpleNamespace(attach=Mock()),
        )
        workspace.initialise_workspace(state)
        assert state.remote_reservations is owner
        with pytest.raises(ReservationError, match="reservation_admission_closed"):
            state.remote_reservations.reserve(root=root, url="remote.example", persistent_id="shared")
    finally:
        finish_stop.set()
        if state.project_lock.locked():
            state.project_lock.release()
        if not task.done():
            await task


def test_a_new_lifespan_does_not_reuse_shutdown_identities(project: Path) -> None:
    """Epochs also rotate when an embedded host reuses its ASGI app (§5.3.2)."""
    app = create_app(str(project))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        previous = client.post("/api/projects/open", json={"path": str(project)}).json()
        old_owner = app.state.remote_reservations
        old_lease = old_owner.reserve(root=project, url="remote.example", persistent_id="fixture")
    assert not app.state.project_contexts
    with pytest.raises(ReservationError, match="reservation_admission_closed"):
        old_owner.reserve(root=project, url="remote.example", persistent_id="fixture")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        current = client.post("/api/projects/open", json={"path": str(project)}).json()
        assert current["epoch"] != previous["epoch"]
        assert current["project_id"] != previous["project_id"]
        assert len(app.state.project_contexts) == 1
        assert client.get(f"/api/projects/{previous['project_id']}/project").status_code == 404
        owner = app.state.remote_reservations
        assert owner is not old_owner
        lease = owner.reserve(root=project, url="remote.example", persistent_id="fixture")
        assert not owner.release(old_lease)
        assert old_owner.release(old_lease)  # The old capability was never revoked.
        assert owner.release(lease)


def test_failed_bootstrap_never_publishes_a_project_identity(project: Path, monkeypatch) -> None:
    from voxarium_studio.core.context import AppContext

    original_start, original_stop = AppContext.startup, AppContext.shutdown
    stopped = []

    async def fail(ctx):
        await original_start(ctx)
        raise OSError("bootstrap failure")

    async def stop(ctx):
        stopped.append(ctx)
        await original_stop(ctx)

    monkeypatch.setattr(AppContext, "startup", fail)
    monkeypatch.setattr(AppContext, "shutdown", stop)
    app = create_app(str(project))
    with pytest.raises(OSError, match="bootstrap failure"):
        with TestClient(app, base_url="http://127.0.0.1"):
            pytest.fail("Failed bootstrap must not serve requests")
    assert stopped == [app.state.ctx]
    assert not app.state._failed_project_contexts
    assert not hasattr(app.state, "project_registry")
    with pytest.raises(ReservationError, match="reservation_admission_closed"):
        app.state.remote_reservations.reserve(
            root=project, url="remote.example", persistent_id="fixture",
        )


def test_bootstrap_keeps_legacy_event_replay_without_early_registry(project: Path, monkeypatch) -> None:
    from voxarium_studio.core.context import AppContext

    original = AppContext.startup
    app = create_app(str(project))

    async def start(ctx):
        assert not hasattr(app.state, "project_registry")
        assert ctx.remote_reservations is app.state.remote_reservations
        assert ctx.remote_profile_lifecycle is app.state.remote_profile_lifecycle
        await original(ctx)
        await ctx.bus.publish("test.bootstrap")

    monkeypatch.setattr(AppContext, "startup", start)
    with TestClient(app, base_url="http://127.0.0.1"):
        assert app.state.ctx.bus.forward_to is app.state.browser_bus
        assert any(event.type == "test.bootstrap" for event in app.state.browser_bus.replay(0))


def test_one_app_owner_is_supplied_before_each_startup_and_alias_reuses_it(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    from voxarium_studio.core.context import AppContext

    copy = tmp_path / "copied-project"
    copy.mkdir()
    paths = init_project(copy)
    shutil.copyfile(ProjectPaths.for_project(project).config_file, paths.config_file)
    original = AppContext.startup
    started = []
    app = create_app(str(project))

    async def start(ctx):
        assert ctx.remote_reservations is app.state.remote_reservations
        assert ctx.remote_profile_lifecycle is app.state.remote_profile_lifecycle
        started.append(ctx)
        await original(ctx)

    monkeypatch.setattr(AppContext, "startup", start)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        owner = app.state.remote_reservations
        bootstrap = app.state.ctx
        response = client.post("/api/project/open", json={"path": str(copy)})
        assert response.status_code == 200, response.text
        subsequent = app.state.ctx
        assert subsequent is not bootstrap
        assert subsequent.config.project_identity == bootstrap.config.project_identity
        assert subsequent.remote_reservations is bootstrap.remote_reservations is owner
        for root, expected in ((copy / ".", subsequent), (project / ".", bootstrap)):
            response = client.post("/api/project/open", json={"path": str(root)})
            assert response.status_code == 200, response.text
            assert app.state.ctx is expected
        workspace.initialise_workspace(app.state)
        assert app.state.remote_reservations is owner
        assert started == [bootstrap, subsequent]
        assert len(app.state.project_contexts) == 2


def test_runtime_start_failure_also_closes_reservation_admission(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from voxarium_studio import main

    runtime = SimpleNamespace(
        lifecycle=Mock(), start=AsyncMock(side_effect=OSError("fixture runtime failure")),
        close=AsyncMock(),
    )
    monkeypatch.setattr(main, "create_remote_catalog_runtime", lambda: runtime)
    app = create_app(str(project))
    with pytest.raises(OSError, match="fixture runtime failure"):
        with TestClient(app, base_url="http://127.0.0.1"):
            pytest.fail("Failed runtime cannot serve requests")
    runtime.close.assert_awaited_once()
    with pytest.raises(ReservationError, match="reservation_admission_closed"):
        app.state.remote_reservations.reserve(
            root=project, url="remote.example", persistent_id="fixture",
        )


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_bootstrap_failure_retains_exact_context_until_cleanup_retry(
    project: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool,
) -> None:
    from voxarium_studio.core.context import AppContext

    app = create_app(str(project))
    leases, stopped = [], []

    async def start(ctx):
        assert ctx.remote_reservations is app.state.remote_reservations
        leases.append(ctx.remote_reservations.reserve(
            root=project, url="remote.example", persistent_id="fixture",
        ))
        raise OSError("fixture bootstrap failure")

    async def stop(ctx):
        stopped.append(ctx)
        # Failed cleanup has not joined the consumer: keep its capability live.
        if cleanup_fails:
            raise RuntimeError("fixture cleanup failure")

    monkeypatch.setattr(AppContext, "startup", start)
    monkeypatch.setattr(AppContext, "shutdown", stop)
    with pytest.raises(OSError, match="fixture bootstrap failure"):
        with TestClient(app, base_url="http://127.0.0.1"):
            pytest.fail("Failed bootstrap must not serve requests")
    ctx = app.state.ctx
    owner = app.state.remote_reservations
    assert stopped == [ctx]
    assert not hasattr(app.state, "project_registry")
    assert app.state._failed_project_contexts == ({ctx.paths.root: ctx} if cleanup_fails else {})
    with pytest.raises(ReservationError, match="reservation_admission_closed"):
        owner.reserve(root=project, url="remote.example", persistent_id="fixture")
    if cleanup_fails:
        # A failed lifespan cannot silently replace its context/owner on reentry.
        with pytest.raises(RuntimeError, match="Workspace cleanup is incomplete"):
            with TestClient(app, base_url="http://127.0.0.1"):
                pytest.fail("Unjoined context must fence a fresh lifespan")
        assert app.state.remote_reservations is owner
        cleanup_fails = False
        asyncio.run(workspace.shutdown_workspace(app.state))
        assert stopped == [ctx, ctx]
        assert not app.state._failed_project_contexts
    # Workspace never revokes a consumer's reservation, even during retry.
    assert owner.release(leases[0])


async def test_failed_subsequent_cleanup_preserves_b_jobs_and_fences_same_root(
    ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voxarium_studio.core.context import AppContext
    from voxarium_studio.core.project_scope import canonical_root

    state = SimpleNamespace(ctx=ctx, project_lock=asyncio.Lock())
    workspace.initialise_workspace(state)
    owner = state.remote_reservations
    root = tmp_path / "failed-a"
    root.mkdir()
    started, finished = asyncio.Event(), asyncio.Event()
    attempts, stopped, leases = [], [], []
    cleanup_fails = True
    original_stop = AppContext.shutdown

    async def job(_handle):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    ctx.jobs.submit("b-job", "agent", job)
    await started.wait()

    async def start(candidate):
        attempts.append(candidate)
        assert candidate.remote_reservations is owner
        assert candidate.remote_profile_lifecycle is ctx.remote_profile_lifecycle
        leases.append(owner.reserve(root=root, url="remote.example", persistent_id="copy"))
        raise OSError("fixture subsequent failure")

    async def stop(candidate):
        if candidate is not ctx:
            stopped.append(candidate)
            if cleanup_fails:
                raise RuntimeError("fixture cleanup failure")
        await original_stop(candidate)

    monkeypatch.setattr(AppContext, "startup", start)
    monkeypatch.setattr(AppContext, "shutdown", stop)
    try:
        with pytest.raises(OSError, match="fixture subsequent failure"):
            await workspace.open_project(state, root)
        failed_ctx = attempts[0]
        assert state.ctx is ctx
        assert state._failed_project_contexts == {canonical_root(root): failed_ctx}
        assert canonical_root(root) not in state.project_roots
        assert ctx.jobs.is_running("b-job") and not finished.is_set()
        assert await workspace.open_project(state, ctx.paths.root / ".") is ctx
        workspace.initialise_workspace(state)
        assert state.remote_reservations is owner
        with pytest.raises(RuntimeError, match="Project cleanup is incomplete"):
            await workspace.open_project(state, root / ".")
        assert attempts == [failed_ctx]
        with pytest.raises(ReservationError, match="duplicate_local_binding"):
            owner.reserve(root=tmp_path / "other-copy", url="remote.example", persistent_id="copy")
        assert ctx.jobs.is_running("b-job") and not finished.is_set()
    finally:
        cleanup_fails = False
        await workspace.shutdown_workspace(state)
    assert stopped == [failed_ctx, failed_ctx]
    assert not state._failed_project_contexts
    assert finished.is_set()
    assert owner.release(leases[0])


def test_open_existing_project_switches_context(client: TestClient, tmp_path: Path) -> None:
    other = tmp_path / "other-game"
    other.mkdir()
    init_project(other)

    response = client.post("/api/project/open", json={"path": str(other), "create": False})

    assert response.status_code == 200
    assert Path(response.json()["root"]) == other.resolve()
    assert Path(client.get("/api/project").json()["root"]) == other.resolve()


def test_open_uninitialised_folder_creates_the_data_directory(
    client: TestClient, tmp_path: Path
) -> None:
    fresh = tmp_path / "fresh-game"
    fresh.mkdir()

    response = client.post("/api/project/open", json={"path": str(fresh)})

    assert response.status_code == 200
    assert (fresh / DATA_DIRNAME).is_dir()
    assert (fresh / DATA_DIRNAME / "config.yaml").exists()
    # Same treatment `voxarium_studio init` gives it, samples included — opening a folder that is
    # not a project yet is the GUI spelling of initialising one.
    assert list((fresh / DATA_DIRNAME / "pipelines").iterdir())
    assert response.json()["name"] == "fresh-game"


def test_browsing_a_legacy_project_is_read_only_and_open_renames_it(client, tmp_path):
    from contextlib import closing
    import json
    import sqlite3

    root = tmp_path / "old-game"
    root.mkdir()
    paths = init_project(root)
    legacy = root / "voxarium_studio"
    (paths.docs / "design.md").write_text("design")
    (paths.assets / "mesh.bin").write_bytes(b"artifact")
    # Seed a real old run, with an absolute output and a literal text input that
    # happens to spell the same path. Only the file-typed output should move.
    with TestClient(create_app(str(root)), base_url="http://127.0.0.1") as previous:
        created = previous.post("/api/teams/team_default/tasks", json={
            "assignee_id": "team_default__dev", "title": "Saved task",
        })
        assert created.status_code == 201, created.text
        task = created.json()
    paths.data.rename(legacy)
    old_file = str(legacy / "assets" / "mesh.bin")
    with closing(sqlite3.connect(legacy / "app.db")) as db:
        db.execute("PRAGMA user_version = 0")  # A database written by the old Studio.
        db.execute("UPDATE team_task SET status='needs_review', write_paths=?, attachments=? WHERE id=?",
                   (json.dumps(["voxarium_studio/docs/design.md"]),
                    json.dumps([{"name": "mesh.bin", "path": "voxarium_studio/assets/mesh.bin"}]),
                    task["id"]))
        db.execute("INSERT INTO run (id,pipeline_id,pipeline_name,status,trigger,use_cache,params,created_at) "
                   "VALUES ('old','saved','Saved','success','manual',1,'{}',CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO node_run (id,run_id,node_id,node_type,status,from_cache,inputs,outputs,log_path) "
                   "VALUES ('node','old','write','core.write_text_file','success',0,?,?,?)",
                   (json.dumps({"text": old_file}), json.dumps({"file": old_file}), str(legacy / "runs/old/write.log")))
        db.commit()

    assert client.get("/api/fs/inspect", params={"path": str(root)}).json()["is_project"]
    recent = next(e for e in client.get("/api/project/recent").json() if Path(e["root"]) == root)
    assert recent["initialised"] and recent["task_counts"]["needs_review"] == 1
    assert legacy.is_dir() and not paths.data.exists()

    opened = client.post("/api/project/open", json={"path": str(root), "create": False})
    assert opened.status_code == 200, opened.text
    assert Path(opened.json()["data_dir"]) == paths.data
    assert not legacy.exists()
    assert (paths.docs / "design.md").read_text() == "design"
    with closing(sqlite3.connect(paths.db_file)) as db:
        writes, attachments = db.execute("SELECT write_paths, attachments FROM team_task WHERE id=?",
                                         (task["id"],)).fetchone()
    assert json.loads(writes) == [".voxarium_studio/docs/design.md"]
    assert json.loads(attachments)[0]["path"] == ".voxarium_studio/assets/mesh.bin"
    node = client.get("/api/runs/old").json()["nodes"][0]
    assert node["inputs"]["text"] == old_file
    assert Path(node["outputs"]["file"]).read_bytes() == b"artifact"
    assert Path(node["log_path"]) == paths.runs / "old/write.log"
    assert init_project(root).data == paths.data
    assert not legacy.exists()


def test_conflicting_data_directories_leave_both_and_the_open_project_intact(client, tmp_path):
    current = client.get("/api/project").json()["root"]
    for name in ("voxarium_studio", ".voxarium_studio"):
        data = tmp_path / name
        data.mkdir()
        (data / "notes").write_text(name)
    response = client.post("/api/project/open", json={"path": str(tmp_path), "create": False})
    assert response.status_code == 400 and "both paths exist" in response.json()["detail"]
    assert client.get("/api/project").json()["root"] == current
    for name in ("voxarium_studio", ".voxarium_studio"):
        assert (tmp_path / name / "notes").read_text() == name


def test_open_refuses_to_initialise_when_the_caller_said_not_to(
    client: TestClient, tmp_path: Path
) -> None:
    """The dialog preflights with `create: false` so it can say what it is about to
    create; a 409 is the answer it renders, not a failure."""
    fresh = tmp_path / "not-a-project"
    fresh.mkdir()

    response = client.post("/api/project/open", json={"path": str(fresh), "create": False})

    assert response.status_code == 409
    assert not (fresh / DATA_DIRNAME).exists()


def test_open_rejects_a_path_that_is_not_a_directory(
    client: TestClient, project: Path, tmp_path: Path
) -> None:
    missing = client.post("/api/project/open", json={"path": str(tmp_path / "nope")})
    assert missing.status_code == 404

    file_path = tmp_path / "readme.txt"
    file_path.write_text("not a project", encoding="utf-8")
    assert client.post("/api/project/open", json={"path": str(file_path)}).status_code == 404

    # The original project is still open — a bad request must not unseat it.
    assert Path(client.get("/api/project").json()["root"]) == project.resolve()


def test_reopening_the_current_project_is_a_no_op(client: TestClient, project: Path) -> None:
    before = client.get("/api/status").json()["event_seq"]

    response = client.post("/api/project/open", json={"path": str(project)})

    assert response.status_code == 200
    # No swap happened, so nothing was published: an idle Studio should not churn.
    assert client.get("/api/status").json()["event_seq"] == before


def test_switching_carries_the_event_bus_over(client: TestClient, tmp_path: Path) -> None:
    """A client is subscribed to the bus *object*. Replacing it would leave every open
    tab on a live socket that never delivers again, so the sequence must keep counting."""
    other = tmp_path / "second"
    other.mkdir()
    seq_before = client.get("/api/status").json()["event_seq"]

    client.post("/api/project/open", json={"path": str(other)})

    assert client.get("/api/status").json()["event_seq"] > seq_before


def test_switching_announces_itself_on_the_socket(client: TestClient, tmp_path: Path) -> None:
    other = tmp_path / "announced"
    other.mkdir()

    with client.websocket_connect("ws://127.0.0.1/ws") as socket:
        assert socket.receive_json()["type"] == "system.hello"
        client.post("/api/project/open", json={"path": str(other)})
        # Events already queued for the current project may precede the announcement.
        for _ in range(20):
            event = socket.receive_json()
            if event["type"] == "project.opened":
                break

    assert event["type"] == "project.opened"
    assert Path(event["data"]["root"]) == other.resolve()
    assert event["data"]["created"] is True


def test_the_new_project_serves_its_own_data(client: TestClient, tmp_path: Path) -> None:
    """The whole context is replaced, not just the paths — the pipeline store and the
    document store both have to come from the project that was just opened."""
    other = tmp_path / "other-game"
    other.mkdir()
    init_project(other, with_samples=False)
    (other / DATA_DIRNAME / "pipelines" / "only-here.yaml").write_text(
        "id: only-here\nname: Only here\nnodes: []\nedges: []\n", encoding="utf-8"
    )
    client.put("/api/docs/first-only.md", json={"content": "# Only in the first project"})

    assert [p["id"] for p in client.get("/api/pipelines").json()] == ["prompt-to-voxel-asset"]
    client.post("/api/project/open", json={"path": str(other)})

    assert [p["id"] for p in client.get("/api/pipelines").json()] == ["only-here"]
    assert client.get("/api/docs").json() == []
    assert client.get("/api/status").json()["database"] is True


def test_a_failed_switch_leaves_the_current_project_open(
    client: TestClient, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()

    async def explode(self) -> None:
        raise RuntimeError("plugin registry is on fire")

    monkeypatch.setattr("voxarium_studio.core.context.AppContext.startup", explode)
    with pytest.raises(RuntimeError):
        client.post("/api/project/open", json={"path": str(broken)})

    monkeypatch.undo()
    assert Path(client.get("/api/project").json()["root"]) == project.resolve()
    assert client.get("/api/pipelines").status_code == 200


async def test_concurrent_opens_are_serialised(ctx, tmp_path: Path) -> None:
    """Two clicks on Open must not build two contexts over each other.

    Driven against the context directly rather than through `TestClient`, whose app runs
    its lifespan on a portal thread with its own event loop — the file watcher would then
    be cancelled from a loop it was never created on.
    """
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    state = SimpleNamespace(ctx=ctx, project_lock=asyncio.Lock())
    try:
        await asyncio.gather(
            workspace.open_project(state, first),
            workspace.open_project(state, second),
        )
        assert state.ctx.paths.root in (first.resolve(), second.resolve())
        assert not state.project_lock.locked()
    finally:
        await workspace.shutdown_workspace(state)


async def test_agent_finishes_in_background_and_return_reuses_its_context(
    ctx, tmp_path: Path,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    class HeldBackend:
        name = "workspace-held"

        def is_available(self):
            return True, "held"

        async def run(self, request):
            requests.append(request)
            yield AgentEvent(kind="text", text="Working", session_id="workspace-session")
            started.set()
            await release.wait()
            yield AgentEvent(
                kind="result", text="Finished in background", is_final=True,
                data={"is_error": False, "num_turns": 1},
            )

    register_backend(HeldBackend.name, HeldBackend)
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, "team_default__dev")
        member.agent_backend = HeldBackend.name
        member.isolate_worktree = False
        session.add(member)
    task = await ctx.teams.create_task(
        team_id="team_default", assignee_id=member.id, title="Survive switching", brief="Work",
    )
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await ctx.teams.start_task(task.id)
    await asyncio.wait_for(started.wait(), 5)
    job = ctx.jobs.get(task.id)
    state = SimpleNamespace(ctx=ctx, project_lock=asyncio.Lock())
    other = tmp_path / "background-test"
    other.mkdir()
    try:
        replacement = await workspace.open_project(state, other)
        assert ctx.jobs.is_running(task.id)
        async with ctx.db.session() as session:
            assert (await session.get(TeamTask, task.id)).status == TaskStatus.IN_PROGRESS
        # Restart guards must include tasks outside the currently selected project.
        assert replacement.updates._busy() is not None
        assert await workspace.open_project(state, ctx.paths.root) is ctx
        assert ctx.jobs.get(task.id) is job
        assert ctx.jobs.is_running(task.id)
        assert await workspace.open_project(state, other) is replacement
        release.set()
        await asyncio.wait_for(asyncio.shield(job.task), 5)
        async with ctx.db.session() as session:
            assert (await session.get(TeamTask, task.id)).status == TaskStatus.NEEDS_REVIEW
        async with replacement.db.session() as session:
            assert await session.get(TeamTask, task.id) is None
        assert await workspace.open_project(state, ctx.paths.root) is ctx
        assert await workspace.open_project(state, other) is replacement
        assert len(requests) == 1
    finally:
        release.set()
        await workspace.shutdown_workspace(state)


async def test_background_events_stay_local_and_browser_replay_changes_projects(
    ctx, tmp_path: Path,
) -> None:
    state = SimpleNamespace(ctx=ctx, project_lock=asyncio.Lock())
    workspace.initialise_workspace(state)
    browser = state.browser_bus.subscribe()
    first_local = ctx.bus.subscribe()
    other = tmp_path / "event-isolation"
    other.mkdir()
    try:
        await ctx.bus.publish("test.first")
        first_seq = (await browser.get()).seq
        assert (await first_local.get()).type == "test.first"
        replacement = await workspace.open_project(state, other)
        second_local = replacement.bus.subscribe()
        try:
            await ctx.bus.publish("test.background")
            await replacement.bus.publish("test.foreground")
            assert (await first_local.get()).type == "test.background"
            assert (await second_local.get()).type == "test.foreground"
            assert (await browser.get()).type == "project.opened"
            assert (await browser.get()).type == "test.foreground"
            assert [e.type for e in state.browser_bus.replay(0)] == [
                "project.opened", "test.foreground",
            ]
            assert state.browser_bus.current_seq > first_seq
            # Returning switches forwarding back without replacing the browser's bus.
            await workspace.open_project(state, ctx.paths.root)
            await replacement.bus.publish("test.now-background")
            await ctx.bus.publish("test.returned")
            assert (await browser.get()).type == "project.opened"
            assert (await browser.get()).type == "test.returned"
        finally:
            second_local.close()
    finally:
        browser.close()
        first_local.close()
        await workspace.shutdown_workspace(state)


async def test_server_shutdown_cancels_work_in_all_opened_projects(ctx, tmp_path: Path) -> None:
    state = SimpleNamespace(ctx=ctx, project_lock=asyncio.Lock())
    other = tmp_path / "shutdown-test"
    other.mkdir()
    replacement = await workspace.open_project(state, other)
    cancelled = set()
    started = [asyncio.Event(), asyncio.Event()]

    async def work(index):
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(index)

    for index, context in enumerate((ctx, replacement)):
        context.jobs.submit(str(index), "agent", lambda _handle, i=index: work(i))
    await asyncio.gather(*(event.wait() for event in started))
    await workspace.shutdown_workspace(state)
    assert cancelled == {0, 1}
    assert not state.project_contexts
    assert not ctx.jobs.jobs()
    assert not replacement.jobs.jobs()


def test_the_auth_posture_survives_a_switch(project: Path, tmp_path: Path) -> None:
    """The token belongs to the running server, not to the project it happens to have
    open — a swap that dropped it would silently unlock the API."""
    other = tmp_path / "guarded"
    other.mkdir()
    headers = {"Authorization": "Bearer t0ken"}

    with TestClient(create_app(str(project), auth=AuthSettings(token="t0ken")), base_url="http://127.0.0.1") as client:
        opened = client.post("/api/project/open", json={"path": str(other)}, headers=headers)

        assert opened.status_code == 200
        assert opened.json()["auth_required"] is True
        assert client.get("/api/project").status_code == 401
        assert client.get("/api/project", headers=headers).status_code == 200


# ---------------------------------------------------------------- recent projects


def test_the_open_project_is_recorded_as_recent(client: TestClient, project: Path) -> None:
    entries = client.get("/api/project/recent").json()

    assert Path(entries[0]["root"]) == project.resolve()
    assert entries[0]["exists"] is True
    assert entries[0]["initialised"] is True


def test_recent_moves_the_newest_to_the_front_without_duplicating(
    client: TestClient, project: Path, tmp_path: Path
) -> None:
    other = tmp_path / "recent-other"
    other.mkdir()

    client.post("/api/project/open", json={"path": str(other)})
    client.post("/api/project/open", json={"path": str(project)})
    roots = [Path(entry["root"]) for entry in client.get("/api/project/recent").json()]

    assert roots[0] == project.resolve()
    assert roots.count(project.resolve()) == 1


def test_recent_can_be_forgotten(client: TestClient, project: Path) -> None:
    remaining = client.delete("/api/project/recent", params={"path": str(project)}).json()

    assert all(Path(entry["root"]) != project.resolve() for entry in remaining)


def test_recent_reports_a_project_that_has_since_gone(tmp_path: Path) -> None:
    workspace.record_recent(tmp_path / "deleted-since", "Deleted")

    entry = workspace.load_recent()[0].to_json()

    assert entry["exists"] is False
    assert entry["initialised"] is False


def test_recent_survives_a_corrupt_file() -> None:
    path = workspace.recent_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    assert workspace.load_recent() == []

    workspace.record_recent(Path.cwd(), "Recovered")
    assert len(workspace.load_recent()) == 1


# ------------------------------------------------------------- the project a start opens


@pytest.fixture
def own_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine-level directory this test alone writes into.

    The session fixture gives every test in a worker the *same* one, and these tests are
    about what an empty recent list does — which any test that ran before them would have
    filled in. It also keeps the demo project each of them creates out of the others' way.
    """
    home = tmp_path / "studio-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    return home


def test_a_path_on_the_command_line_wins(own_home: Path, project: Path, tmp_path: Path) -> None:
    workspace.record_recent(project, "Recently opened")
    named = tmp_path / "named-on-the-command-line"
    named.mkdir()

    chosen = workspace.resolve_startup_project(named)

    assert chosen.root == named.resolve()
    assert chosen.source == "argument"


def test_a_start_with_no_path_reopens_the_last_project(
    own_home: Path, project: Path, tmp_path: Path
) -> None:
    earlier = tmp_path / "opened-earlier"
    earlier.mkdir()
    workspace.record_recent(earlier, "Earlier")
    workspace.record_recent(project, "Last")

    chosen = workspace.resolve_startup_project()

    assert chosen.root == project.resolve()
    assert chosen.source == "recent"


def test_a_recent_project_that_has_gone_is_skipped(
    own_home: Path, project: Path, tmp_path: Path
) -> None:
    """A project moved or deleted since must not hold the Studio hostage on every start."""
    workspace.record_recent(project, "Still here")
    workspace.record_recent(tmp_path / "moved-away", "Gone")

    assert workspace.resolve_startup_project().root == project.resolve()


def test_with_nothing_to_reopen_the_demo_project_is_opened(own_home: Path) -> None:
    chosen = workspace.resolve_startup_project()

    assert chosen.source == "demo"
    assert chosen.root == (own_home / demo.DEMO_DIRNAME).resolve()
    paths = ProjectPaths.for_project(chosen.root)
    assert paths.config_file.exists()
    # Initialised the way `voxarium_studio init` would, samples included, plus what makes it a demo.
    assert list(paths.pipelines.iterdir())
    assert list(paths.teams.iterdir())
    assert (paths.docs / "welcome.md").exists()
    assert load_config(paths).project_name == demo.DEMO_PROJECT_NAME


@pytest.mark.parametrize("legacy", [False, True])
def test_the_demo_project_is_never_written_over(own_home: Path, legacy: bool) -> None:
    """It is the user's project once it exists — only the seed is skipped, not their edits."""
    first = demo.ensure_demo_project()
    welcome = first.docs / "welcome.md"
    welcome.write_text("# Mine now", encoding="utf-8")
    (first.root / "README.md").unlink()
    if legacy:
        first.data.rename(first.root / "voxarium_studio")

    again = demo.ensure_demo_project()

    assert again.root == first.root
    assert welcome.read_text(encoding="utf-8") == "# Mine now"
    assert not (first.root / "README.md").exists()


def test_the_demo_project_is_recorded_as_recent_once_it_is_opened(own_home: Path) -> None:
    """Which is what makes the *second* start reopen it rather than create it again."""
    root = workspace.resolve_startup_project().root
    with TestClient(create_app(str(root))):
        pass

    assert workspace.resolve_startup_project() == workspace.StartupProject(root, "recent")


# ---------------------------------------------------------------- browsing and picking


def test_browse_lists_only_directories_and_flags_projects(
    client: TestClient, project: Path
) -> None:
    (project.parent / "plain-folder").mkdir()
    (project.parent / "a-file.txt").write_text("x", encoding="utf-8")

    listing = client.get("/api/fs/browse", params={"path": str(project.parent)}).json()

    by_name = {entry["name"]: entry for entry in listing["entries"]}
    assert "a-file.txt" not in by_name
    assert by_name["game"]["is_project"] is True
    assert by_name["plain-folder"]["is_project"] is False
    assert Path(listing["parent"]) == project.parent.parent


def test_browse_with_no_path_lists_the_roots(client: TestClient) -> None:
    listing = client.get("/api/fs/browse").json()

    assert listing["parent"] is None
    assert listing["entries"], "a machine always has at least one root"


def test_browse_reports_a_missing_directory(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/api/fs/browse", params={"path": str(tmp_path / "gone")}).status_code == 404


def test_inspect_answers_for_paths_that_do_not_exist(client: TestClient, tmp_path: Path) -> None:
    result = client.get("/api/fs/inspect", params={"path": str(tmp_path / "ghost")}).json()

    assert result["exists"] is False
    assert result["is_dir"] is False
    assert result["is_project"] is False


def test_inspect_recognises_a_project(client: TestClient, project: Path) -> None:
    result = client.get("/api/fs/inspect", params={"path": str(project)}).json()

    assert result["is_dir"] is True
    assert result["is_project"] is True
    assert result["name"] == project.name


def test_picker_is_offered_on_a_loopback_run(client: TestClient) -> None:
    from voxarium_studio.core import picker

    status = client.get("/api/fs/picker").json()
    supported, _ = picker.dialog_supported()

    assert status["available"] is supported
    assert Path(status["default_start"]).exists()


def test_picker_is_withheld_from_a_remote_studio(project: Path) -> None:
    """The dialog would open on the server's desktop, not the caller's."""
    auth = AuthSettings(token="t0ken", host="0.0.0.0")
    with TestClient(create_app(str(project), auth=auth), base_url="http://127.0.0.1") as client:
        headers = {"Authorization": "Bearer t0ken"}

        status = client.get("/api/fs/picker", headers=headers).json()
        attempt = client.post("/api/fs/pick", json={}, headers=headers)

    assert status["available"] is False
    assert "not running on this machine" in status["reason"]
    assert attempt.status_code == 409


class _WaitingDialog:
    """A dialog subprocess that sits there until it is answered or killed."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self._answer = b""
        self._finished = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        await self._finished.wait()
        return self._answer, b""

    def kill(self) -> None:
        self.returncode = -9
        self._finished.set()

    async def wait(self) -> int | None:
        await self._finished.wait()
        return self.returncode

    def answer(self, stdout: bytes) -> None:
        self.returncode = 0
        self._answer = stdout
        self._finished.set()


def _spawns_waiting_dialogs(monkeypatch: pytest.MonkeyPatch) -> list[_WaitingDialog]:
    from voxarium_studio.core import picker

    spawned: list[_WaitingDialog] = []

    async def fake_exec(*argv: str, **kwargs: object) -> _WaitingDialog:
        spawned.append(_WaitingDialog())
        return spawned[-1]

    monkeypatch.setattr(picker, "dialog_supported", lambda: (True, ""))
    monkeypatch.setattr(picker.asyncio, "create_subprocess_exec", fake_exec)
    return spawned


async def test_a_second_dialog_supersedes_the_one_already_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing the second click was the original rule, and it was a dead end.

    The dialog that gets asked for twice is the one that never surfaced, and the refusal
    made that state last until the dialog's own 15-minute timeout — long after the tab
    that opened it had been reloaded.
    """
    from voxarium_studio.core import picker

    spawned = _spawns_waiting_dialogs(monkeypatch)

    first = asyncio.create_task(picker.pick_directory())
    await asyncio.sleep(0.05)
    second = asyncio.create_task(picker.pick_directory())
    await asyncio.sleep(0.05)

    assert len(spawned) == 2, "the second request should have opened its own dialog"
    assert spawned[0].returncode is not None, "the first window should have been taken down"

    abandoned_result = await first
    assert abandoned_result.cancelled is True
    assert "replaced" in abandoned_result.reason
    assert abandoned_result.available is True, "nothing here is unavailable — it was superseded"

    chosen = str(Path.cwd())
    spawned[1].answer(b"PICKED:" + base64.b64encode(chosen.encode()))
    assert (await second).path == Path(chosen)


async def test_the_dialog_closes_when_the_page_that_asked_for_it_goes_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uvicorn does not cancel a handler on disconnect, so nothing else would close it."""
    from voxarium_studio.core import picker

    monkeypatch.setattr(picker, "ABANDON_POLL_SECONDS", 0.01)
    spawned = _spawns_waiting_dialogs(monkeypatch)

    still_there = True

    async def is_disconnected() -> bool:
        return not still_there

    pick = asyncio.create_task(picker.pick_directory(abandoned=is_disconnected))
    await asyncio.sleep(0.05)
    assert spawned[0].returncode is None, "a connected client must keep its dialog open"

    still_there = False
    result = await asyncio.wait_for(pick, timeout=1)

    assert spawned[0].returncode is not None
    assert result.cancelled is True
    assert "went away" in result.reason
    # And the lock came back with it, which is the half that stranded the whole feature.
    assert picker._dialog_lock.locked() is False


async def test_a_cancelled_request_does_not_leave_a_dialog_on_the_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voxarium_studio.core import picker

    spawned = _spawns_waiting_dialogs(monkeypatch)

    pick = asyncio.create_task(picker.pick_directory())
    await asyncio.sleep(0.05)
    pick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pick

    assert spawned[0].returncode is not None
    assert picker._dialog_lock.locked() is False


async def test_a_cancelled_dialog_reports_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    from voxarium_studio.core import picker

    async def cancelling(*args: object, **kwargs: object) -> picker.PickResult:
        return picker.PickResult(cancelled=True)

    monkeypatch.setattr(picker, "dialog_supported", lambda: (True, ""))
    monkeypatch.setattr(picker, "_run_dialog", cancelling)

    result = await picker.pick_directory()

    assert result.cancelled is True
    assert result.path is None


def _fake_dialog_process(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes, stderr: bytes = b"", returncode: int = 0
) -> None:
    """Stand in for the spawned dialog so `_run_dialog`'s own decoding is what runs."""
    from voxarium_studio.core import picker

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return stdout, stderr

    async def fake_exec(*argv: str, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(picker.asyncio, "create_subprocess_exec", fake_exec)


async def test_a_chosen_folder_survives_a_non_ascii_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The answer is base64 precisely because the console code page would mangle it."""
    from voxarium_studio.core import picker

    chosen = "D:\\Игры\\Мой проект"
    _fake_dialog_process(
        monkeypatch, b"PICKED:" + base64.b64encode(chosen.encode("utf-8"))
    )

    result = await picker._run_dialog(["fake"], 5)

    assert result.path == Path(chosen)


async def test_a_dialog_that_reports_an_error_surfaces_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dialog_process(
        monkeypatch, b"ERROR:" + base64.b64encode("no display available".encode())
    )
    from voxarium_studio.core import picker

    result = await picker._run_dialog(["fake"], 5)

    assert result.available is False
    assert result.reason == "no display available"


async def test_a_dialog_killed_mid_flight_does_not_leak_clixml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows PowerShell serialises a redirected stderr as CLIXML, so what comes back
    from a killed dialog is markup, not a message. The UI showed `#< CLIXML` to the user
    before this was handled."""
    from voxarium_studio.core import picker

    _fake_dialog_process(
        monkeypatch, b"", b"#< CLIXML\n<Objs Version='1.1.0.1'>...</Objs>", returncode=1
    )

    result = await picker._run_dialog(["fake"], 5)

    assert result.available is False
    assert "CLIXML" not in result.reason
    assert "closed without returning a folder" in result.reason


def test_a_real_stderr_message_still_gets_through() -> None:
    from voxarium_studio.core import picker

    assert picker._clean_stderr(b"  powershell:  command\n  not found  ") == (
        "powershell: command not found"
    )
    assert picker._clean_stderr(b"#< CLIXML\n<Objs/>") == ""


def test_hidden_directories_are_flagged_not_dropped(tmp_path: Path) -> None:
    """The browser hides them by default; the backend still reports them, because a
    project living under a dotted directory is the user's business, not ours."""
    (tmp_path / ".hidden-project").mkdir()
    (tmp_path / "visible").mkdir()

    entries = {e["name"]: e for e in browse.list_directory(tmp_path)["entries"]}

    assert entries[".hidden-project"]["hidden"] is True
    assert entries["visible"]["hidden"] is False
