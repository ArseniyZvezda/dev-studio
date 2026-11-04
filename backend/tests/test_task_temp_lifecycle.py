"""Task scratch directories survive review/resume and disappear only on Done (§10.4)."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.prompts import AgentPromptsConfig
from voxarium_studio.models.teams import TaskStatus, TeamMember, TeamTask
from voxarium_studio.teams import temp as task_temp
from voxarium_studio.teams.backends.base import AgentEvent, register_backend
from voxarium_studio.vcs import IsolatedWorkspace

TEAM = "team_default"
DEV = f"{TEAM}__dev"
DIRECTOR = f"{TEAM}__director"


def test_directory_uses_stable_task_number_and_rejects_unnumbered_tasks(tmp_path: Path) -> None:
    paths = ProjectPaths.for_project(tmp_path)
    task = TeamTask(team_id=TEAM, assignee_id=DEV, title="Task", number=7)
    assert task_temp.ensure_task_temp(paths, task) == paths.temp / "TASK-7"
    task.number = 0
    with pytest.raises(ValueError, match="persistent number"):
        task_temp.task_temp_dir(paths, task)


@pytest.mark.parametrize("override", [None, "", "Keep your work tidy.", "Use {temp_dir}."])
def test_absolute_directory_survives_custom_prompt_templates(tmp_path: Path, override) -> None:
    directory = str(tmp_path / "voxarium_studio" / "temp" / "TASK-1")
    prompt = AgentPromptsConfig(task_temp=override).text("task_temp", temp_dir=directory)
    assert directory in prompt
    assert "{temp_dir}" not in prompt


def test_cleanup_does_not_follow_redirected_task_directory(tmp_path: Path) -> None:
    paths = ProjectPaths.for_project(tmp_path / "project")
    task = TeamTask(team_id=TEAM, assignee_id=DEV, title="Task", number=1)
    paths.temp.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    try:
        (paths.temp / "TASK-1").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This host cannot create directory symlinks")
    with pytest.raises(ValueError, match="must not use a link"):
        task_temp.remove_task_temp(paths, task)
    assert keep.read_text(encoding="utf-8") == "keep"


def test_cleanup_removes_nested_links_without_touching_their_targets(tmp_path: Path) -> None:
    paths = ProjectPaths.for_project(tmp_path / "project")
    task = TeamTask(team_id=TEAM, assignee_id=DEV, title="Task", number=1)
    directory = task_temp.ensure_task_temp(paths, task)
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    try:
        (directory / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This host cannot create directory symlinks")
    task_temp.remove_task_temp(paths, task)
    assert not directory.exists()
    assert keep.read_text(encoding="utf-8") == "keep"


def test_cleanup_removes_readonly_working_files(tmp_path: Path) -> None:
    paths = ProjectPaths.for_project(tmp_path)
    task = TeamTask(team_id=TEAM, assignee_id=DEV, title="Task", number=1)
    directory = task_temp.ensure_task_temp(paths, task)
    source = directory / "readonly-object"
    source.write_bytes(b"git object")
    source.chmod(stat.S_IREAD)
    task_temp.remove_task_temp(paths, task)
    assert not directory.exists()


async def new_task(ctx: AppContext, assignee: str = DEV) -> TeamTask:
    return await ctx.teams.create_task(
        team_id=TEAM, assignee_id=assignee, title="Temporary files", brief="Do the work",
        auto_start=False,
    )


@pytest.mark.parametrize("route", ["move", "approve", "complete"])
async def test_every_done_route_removes_only_this_task_directory(ctx: AppContext, route: str) -> None:
    task = await new_task(ctx, DIRECTOR if route == "complete" else DEV)
    other = await new_task(ctx)
    directory = task_temp.ensure_task_temp(ctx.paths, task)
    neighbor = task_temp.ensure_task_temp(ctx.paths, other)
    (directory / "probe.txt").write_text("temporary", encoding="utf-8")
    (neighbor / "keep.txt").write_text("still needed", encoding="utf-8")
    await ctx.teams.update_task(task.id, {"status": TaskStatus.NEEDS_REVIEW})
    assert directory.is_dir(), "review must retain evidence and intermediate artifacts"

    if route == "move":
        await ctx.teams.update_task(task.id, {"status": TaskStatus.DONE})
    elif route == "approve":
        await ctx.teams.approve(task.id)
    else:
        await ctx.teams.complete_task(task.id, result_type="done")

    assert not directory.exists()
    assert (neighbor / "keep.txt").read_text(encoding="utf-8") == "still needed"
    assert ctx.paths.temp.is_dir()


@pytest.mark.parametrize("isolated", [False, True])
async def test_launch_resume_and_reopen_use_the_same_absolute_directory(
    ctx: AppContext, wait_for, monkeypatch: pytest.MonkeyPatch, isolated: bool,
) -> None:
    seen = []

    class Backend:
        named_agents = False

        def is_available(self):
            return True, "ready"

        async def run(self, request):
            directory = task_temp.task_temp_dir(ctx.paths, request.task)
            assert directory.is_dir()
            assert str(directory) in request.system_prompt
            assert "Files touched / Temp" in request.system_prompt
            (directory / "probe.txt").write_text("temporary", encoding="utf-8")
            seen.append(request)
            yield AgentEvent(kind="result", text="Finished", is_final=True, session_id="temp-test")

    name = f"temp-test-{isolated}"
    register_backend(name, Backend)
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.agent_backend = name
        member.isolate_worktree = isolated
        session.add(member)
    ctx.teams.config.teams.context_compression_tokens = 0
    checkout = ctx.paths.worktrees / "isolated"
    checkout.mkdir(parents=True)

    async def create_workspace(*_args):
        return IsolatedWorkspace(path=checkout, branch="task-branch")

    if isolated:
        monkeypatch.setattr("voxarium_studio.teams.orchestrator.wt.create_isolated_workspace", create_workspace)
    task = await new_task(ctx)
    directory = task_temp.task_temp_dir(ctx.paths, task)
    for iteration in range(3):
        await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
        await ctx.teams.start_task(task.id)
        await wait_for(task.id)
        assert directory.is_dir()
        assert seen[-1].workdir == (checkout if isolated else ctx.paths.root)
        assert seen[-1].writable_roots == ([checkout, directory] if isolated else [ctx.paths.root])
        if iteration == 1:
            await ctx.teams.approve(task.id)
            assert not directory.exists()
    assert seen[1].resume_session_id == "temp-test"
    assert len(seen) == 3


async def test_approval_waits_for_active_backend_before_deleting_files(ctx: AppContext, wait_for) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Backend:
        named_agents = False

        def is_available(self):
            return True, "ready"

        async def run(self, request):
            directory = task_temp.task_temp_dir(ctx.paths, request.task)
            (directory / "probe.txt").write_text("needed while running", encoding="utf-8")
            entered.set()
            await release.wait()
            assert directory.is_dir()
            yield AgentEvent(kind="result", text="Finished", is_final=True)

    register_backend("temp-deferred", Backend)
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.agent_backend = "temp-deferred"
        member.isolate_worktree = False
        session.add(member)
    ctx.teams.config.teams.context_compression_tokens = 0
    task = await new_task(ctx)
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await ctx.teams.start_task(task.id)
    await asyncio.wait_for(entered.wait(), timeout=5)
    directory = task_temp.task_temp_dir(ctx.paths, task)
    await ctx.teams.approve(task.id)
    assert directory.is_dir()
    release.set()
    await wait_for(task.id)
    assert not directory.exists()
    async with ctx.db.session() as session:
        assert (await session.get(TeamTask, task.id)).status == TaskStatus.DONE
