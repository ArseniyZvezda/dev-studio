"""Scratch files stay task-scoped, visible without tool evidence, and out of commits."""

from pathlib import Path

import pytest

from voxarium_studio.core.context import AppContext
from voxarium_studio.models.teams import TeamTask
from voxarium_studio.teams.temp import ensure_task_temp
from voxarium_studio.teams.temp_changes import PREVIEW_LIMIT, is_temp_path
from voxarium_studio.vcs import FileChange, VcsStatus
from voxarium_studio.vcs.git import GitBackend

TEAM = "team_default"
DEV = f"{TEAM}__dev"


async def make_task(ctx: AppContext) -> TeamTask:
    return await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="temporary work", brief="", created_by=None,
    )


async def test_temp_lists_all_own_files_without_transcript_or_vcs(ctx: AppContext) -> None:
    task = await make_task(ctx)
    other = await make_task(ctx)
    directory = ensure_task_temp(ctx.paths, task)
    (directory / "nested").mkdir()
    (directory / "nested" / "probe.py").write_text("print('hello')\n")
    (directory / ".hidden").write_text("hidden")
    (ensure_task_temp(ctx.paths, other) / "other.txt").write_text("other task")

    payload = await ctx.teams.task_changes(task.id)
    assert payload["files"] == []
    assert payload["temp_root"] == str(directory)
    assert [f["path"] for f in payload["temp_files"]] == [
        f"voxarium_studio/temp/TASK-{task.number}/.hidden",
        f"voxarium_studio/temp/TASK-{task.number}/nested/probe.py",
    ]
    assert payload["temp_errors"] == []


async def test_temp_evidence_does_not_duplicate_or_survive_deleted_files(ctx: AppContext) -> None:
    task = await make_task(ctx)
    file = ensure_task_temp(ctx.paths, task) / "probe.py"
    file.write_text("pass")
    await ctx.teams.record_message(
        task.id, DEV, role="assistant", kind="tool_use", text="Write",
        data={"name": "Write", "input": {"file_path": str(file)}},
    )
    payload = await ctx.teams.task_changes(task.id)
    assert payload["files"] == []
    assert len(payload["temp_files"]) == 1
    await ctx.teams._draft_commit(task.id)
    assert (await ctx.teams.find_task(task.id)).commit_files == []
    file.unlink()
    payload = await ctx.teams.task_changes(task.id)
    assert payload["files"] == payload["temp_files"] == []


async def test_temp_preview_reads_managed_project_even_with_worktree(ctx: AppContext) -> None:
    task = await make_task(ctx)
    file = ensure_task_temp(ctx.paths, task) / "probe.py"
    file.write_text("print('temp')\n")
    worktree = ctx.paths.worktrees / "checkout"
    worktree.mkdir(parents=True)
    async with ctx.db.session() as session:
        row = await session.get(TeamTask, task.id)
        row.worktree_path = str(worktree)
        session.add(row)
    payload = await ctx.teams.task_changes(task.id)
    assert payload["source"] == "workspace"
    assert payload["root"] == str(worktree)
    assert len(payload["temp_files"]) == 1
    # This fixture has no VCS at all; the preview must still read the shared temp file.
    patch = await ctx.teams.task_patch(task.id, file.relative_to(ctx.paths.root).as_posix())
    assert "+print('temp')" in patch


async def test_temp_preview_rejects_other_task_and_traversal(ctx: AppContext) -> None:
    task, other = await make_task(ctx), await make_task(ctx)
    file = ensure_task_temp(ctx.paths, other) / "secret.txt"
    file.write_text("other task")
    for path in (
        str(file),
        f"voxarium_studio/temp/TASK-{task.number}/../TASK-{other.number}/secret.txt",
    ):
        with pytest.raises(ValueError, match="outside the task"):
            await ctx.teams.task_patch(task.id, path)


async def test_temp_preview_bounds_binary_and_large_files(ctx: AppContext) -> None:
    task = await make_task(ctx)
    file = ensure_task_temp(ctx.paths, task) / "data.bin"
    file.write_bytes(b"\x00\xff")
    assert "Binary files" in await ctx.teams.task_patch(task.id, str(file))
    file.write_bytes(b"x" * (PREVIEW_LIMIT + 1))
    with pytest.raises(ValueError, match="too large"):
        await ctx.teams.task_patch(task.id, str(file))


async def test_temp_links_are_not_listed_or_previewed(ctx: AppContext) -> None:
    task = await make_task(ctx)
    target = ctx.paths.root / "outside.txt"
    target.write_text("outside")
    link = ensure_task_temp(ctx.paths, task) / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("This machine does not permit symlinks")
    assert (await ctx.teams.task_changes(task.id))["temp_files"] == []
    with pytest.raises(ValueError, match="symbolic links"):
        await ctx.teams.task_patch(task.id, str(link))


async def test_temp_paths_are_excluded_from_workspace_and_explicit_commits(ctx: AppContext) -> None:
    task = await make_task(ctx)
    worktree = ctx.paths.worktrees / "checkout"
    worktree.mkdir(parents=True)
    temp_path = f"voxarium_studio/temp/TASK-{task.number}/old.txt"

    class Backend(GitBackend):
        async def workspace_changes(self, path):
            return VcsStatus(backend="git", root=Path(path), changes=[
                FileChange(path="source.py", kind="modified"),
                FileChange(path=temp_path, kind="untracked"),
            ])

    ctx.teams.vcs = Backend(ctx.paths.root)
    async with ctx.db.session() as session:
        row = await session.get(TeamTask, task.id)
        row.worktree_path = str(worktree)
        session.add(row)
    assert [f["path"] for f in (await ctx.teams.task_changes(task.id))["files"]] == ["source.py"]
    with pytest.raises(ValueError, match="Temporary task files cannot be committed"):
        await ctx.teams.commit_task(task.id, files=[temp_path])


async def test_temp_detection_handles_absolute_and_normalized_paths(ctx: AppContext) -> None:
    root = ctx.paths.root
    assert is_temp_path(str(root / "voxarium_studio/temp/TASK-1/a.txt"), ctx.paths, root)
    assert is_temp_path("./voxarium_studio/temp/TASK-1/a.txt", ctx.paths, root)
    assert is_temp_path("voxarium_studio\\temp\\TASK-1\\a.txt", ctx.paths, root)
    assert not is_temp_path("voxarium_studio/templates/a.txt", ctx.paths, root)
