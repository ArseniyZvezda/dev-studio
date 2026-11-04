"""The Commit section beside a task's transcript (§10.4): the files split by where the
session worked, the draft written when a task arrives in review, and the commit itself
going through the version control abstraction (§9.6).

The git-backed checks mark themselves `real_vcs`, since what they assert is what git says
about a commit; everything else runs on a project under nothing, where the draft and the
split are plain data.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from voxarium_studio.core.context import AppContext
from voxarium_studio.models.teams import AgentMessage, TeamMember, TeamTask, format_task_id
from voxarium_studio.teams.changes import is_outside
from voxarium_studio.teams.commit_proposal import (
    DESCRIBE_EFFORT,
    CommitWork,
    build_prompt,
    commit_title,
    describe_options,
)

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TEAM = "team_default"
DEV = f"{TEAM}__dev"


async def _no_isolation(ctx: AppContext) -> None:
    async with ctx.db.session() as session:
        for member in (await session.exec(select(TeamMember))).all():
            member.isolate_worktree = False
            session.add(member)


async def _edited(ctx: AppContext, task_id: str, path: str) -> None:
    await ctx.teams.record_message(
        task_id,
        DEV,
        role="assistant",
        kind="tool_use",
        text="Edit",
        data={"name": "Edit", "input": {"file_path": path}},
    )


async def _drafted(ctx: AppContext, task_id: str) -> None:
    """The draft is a background task started by the status move; wait for that one."""
    job = ctx.teams._commit_jobs.get(task_id)
    if job is not None:
        await asyncio.wait_for(job, 10)


# ------------------------------------------------------------- project / outside


def test_the_split_is_against_the_directory_the_session_worked_in(tmp_path: Path) -> None:
    root = tmp_path / "game"
    (root / "Source").mkdir(parents=True)
    sibling = tmp_path / "shared"
    sibling.mkdir()

    # Started in the project: a sibling is outside, whatever the path's spelling.
    assert not is_outside("Source/Hook.cpp", root, root)
    assert is_outside(str(sibling / "Lib.cpp"), root, root)
    # Started one level up (`launch_root: ..`): the sibling is part of the session's world.
    assert not is_outside(str(sibling / "Lib.cpp"), root, tmp_path)
    assert not is_outside("Source/Hook.cpp", root, tmp_path)


async def test_the_column_marks_each_file_with_the_side_it_is_on(ctx: AppContext) -> None:
    await _no_isolation(ctx)
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="split", brief="", created_by=None
    )
    outside = ctx.paths.root.parent / "elsewhere" / "notes.md"
    await _edited(ctx, task.id, str(ctx.paths.root / "Source/Hook.cpp"))
    await _edited(ctx, task.id, str(outside))

    payload = await ctx.teams.task_changes(task.id)

    assert payload["workdir"] == str(ctx.paths.root)
    assert [(entry["path"], entry["outside"]) for entry in payload["files"]] == [
        ("Source/Hook.cpp", False),
        (outside.as_posix(), True),
    ]


async def test_a_document_written_through_the_tool_is_named_where_it_lives(
    ctx: AppContext,
) -> None:
    """`write_doc` takes a path under `voxarium_studio/docs`; the list names the file on disk.

    Listing the tool's own frame put `art/keycap.md` in a draft, and the Commit button then
    handed Plastic a path that is not a changed item (observed in a sample project).
    """
    await _no_isolation(ctx)
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="doc", brief="", created_by=None
    )
    await ctx.teams.record_message(
        task.id,
        DEV,
        role="assistant",
        kind="tool_use",
        text="mcp__voxarium_studio__write_doc",
        data={"name": "mcp__voxarium_studio__write_doc", "input": {"path": "art/keycap.md", "content": "#"}},
    )

    payload = await ctx.teams.task_changes(task.id)

    assert [entry["path"] for entry in payload["files"]] == ["voxarium_studio/docs/art/keycap.md"]
    assert payload["files"][0]["outside"] is False


async def test_a_run_started_elsewhere_is_split_in_that_frame(ctx: AppContext) -> None:
    """The `workdir` entry a run writes is the frame, even after the setting changed."""
    await _no_isolation(ctx)
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="up one", brief="", created_by=None
    )
    parent = ctx.paths.root.parent
    await ctx.teams.record_message(
        task.id, DEV, role="system", kind="workdir", text="Starting up one", data={"path": str(parent)}
    )
    await _edited(ctx, task.id, str(parent / "shared" / "Lib.cpp"))
    await _edited(ctx, task.id, str(parent.parent / "far.txt"))

    payload = await ctx.teams.task_changes(task.id)

    assert payload["workdir"] == str(parent)
    assert [entry["outside"] for entry in payload["files"]] == [False, True]


# ------------------------------------------------------------------- the draft


def test_the_title_is_the_board_id_and_one_line() -> None:
    assert commit_title("VS-01", " Add the\ngrapple  hook ") == "VS-01 | Add the grapple hook"


def test_the_prompt_carries_the_work_and_clips_the_diff() -> None:
    work = CommitWork(
        display_id="VS-07",
        title="Hook",
        brief="Make it swing.",
        summary="Added the swing.",
        files=[{"path": "Source/Hook.cpp", "kind": "modified", "insertions": 4, "deletions": 1}],
        patch="+" * 30_000,
    )
    prompt = build_prompt(work)
    assert "Task VS-07: Hook" in prompt
    assert "Make it swing." in prompt and "Added the swing." in prompt
    assert "- Source/Hook.cpp [modified] (+4 −1)" in prompt
    assert "cut short" in prompt and len(prompt) < 26_000


def test_the_description_is_one_quick_turn_on_the_model_asked_for(tmp_path: Path) -> None:
    pytest.importorskip("claude_agent_sdk")
    options = describe_options("haiku", tmp_path)
    assert options.model == "haiku"
    assert options.tools == [] and options.max_turns == 1 and options.setting_sources == []
    # Plan mode answers on Sonnet whatever `model` says, so it must never come back.
    assert options.permission_mode != "plan"
    assert options.effort == DESCRIBE_EFFORT == "low"
    assert options.thinking == {"type": "disabled"}


async def test_arriving_in_review_drafts_a_commit_once(ctx: AppContext) -> None:
    await _no_isolation(ctx)
    prompts: list[tuple[str, str]] = []

    async def fake(prompt: str, model: str, cwd: Path) -> str:
        prompts.append((prompt, model))
        return "Reworked the hook."

    ctx.teams.commit_describer = fake
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="Grapple hook", brief="Swing.", created_by=None
    )
    await _edited(ctx, task.id, str(ctx.paths.root / "Source/Hook.cpp"))
    await _edited(ctx, task.id, str(ctx.paths.root.parent / "outside.txt"))

    await ctx.teams.submit_for_review(task.id, "Added the swing.")
    await _drafted(ctx, task.id)

    proposal = await ctx.teams.commit_proposal(task.id)
    assert proposal["title"] == f"{format_task_id(ctx.teams.task_code, task.number)} | Grapple hook"
    assert proposal["message"] == "Reworked the hook."
    assert proposal["files"] == ["Source/Hook.cpp"], "the file outside the project is not offered"
    assert proposal["generating"] is False and proposal["error"] is None
    assert proposal["vcs"]["backend"] is None, "nothing to commit with, and the payload says so"
    assert len(prompts) == 1
    prompt, model = prompts[0]
    assert model == ctx.config.teams.commit_model
    assert "Source/Hook.cpp" in prompt and "Added the swing." in prompt

    # The status re-sent without moving — a summary landing does it — redrafts nothing.
    await ctx.teams.submit_for_review(task.id, "Added the swing, really.")
    await _drafted(ctx, task.id)
    assert len(prompts) == 1


async def test_a_task_that_changed_nothing_calls_no_model(ctx: AppContext) -> None:
    await _no_isolation(ctx)
    calls = 0

    async def fake(prompt: str, model: str, cwd: Path) -> str:
        nonlocal calls
        calls += 1
        return "never"

    ctx.teams.commit_describer = fake
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="read only", brief="", created_by=None
    )
    await ctx.teams.submit_for_review(task.id, "Looked around.")
    await _drafted(ctx, task.id)

    proposal = await ctx.teams.commit_proposal(task.id)
    assert calls == 0
    assert proposal["files"] == [] and proposal["message"] == ""
    assert proposal["generated_at"] is not None


async def test_a_refused_description_is_written_onto_the_row(ctx: AppContext) -> None:
    await _no_isolation(ctx)

    async def fake(prompt: str, model: str, cwd: Path) -> str:
        raise RuntimeError("claude is not on PATH")

    ctx.teams.commit_describer = fake
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="t", brief="", created_by=None
    )
    await _edited(ctx, task.id, str(ctx.paths.root / "a.txt"))
    await ctx.teams.submit_for_review(task.id, "done")
    await _drafted(ctx, task.id)

    proposal = await ctx.teams.commit_proposal(task.id)
    assert proposal["error"] == "claude is not on PATH"
    assert proposal["message"] == ""
    assert proposal["files"] == ["a.txt"], "the files are still worth listing"


async def test_switched_off_the_files_and_title_are_still_drafted(ctx: AppContext) -> None:
    await _no_isolation(ctx)
    ctx.config.teams.commit_proposals = False
    calls = 0

    async def fake(prompt: str, model: str, cwd: Path) -> str:
        nonlocal calls
        calls += 1
        return "never"

    ctx.teams.commit_describer = fake
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="t", brief="", created_by=None
    )
    await _edited(ctx, task.id, str(ctx.paths.root / "a.txt"))
    await ctx.teams.submit_for_review(task.id, "done")
    await _drafted(ctx, task.id)

    proposal = await ctx.teams.commit_proposal(task.id)
    assert calls == 0 and proposal["enabled"] is False
    assert proposal["files"] == ["a.txt"] and proposal["message"] == ""


async def test_without_version_control_the_button_is_a_409(client: TestClient) -> None:
    created = client.post(
        f"/api/teams/{TEAM}/tasks", json={"assignee_id": DEV, "title": "t", "brief": ""}
    )
    task_id = created.json()["id"]
    response = client.post(f"/api/teams/{TEAM}/tasks/{task_id}/commit", json={})
    assert response.status_code == 409
    assert "no version control" in response.json()["detail"]


# ------------------------------------------------------------------- the commit


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


@pytest.fixture
def git_project(project: Path) -> Path:
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Studio Test")
    _git(project, "config", "commit.gpgsign", "false")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "first")
    return project


@requires_git
@pytest.mark.real_vcs
async def test_the_commit_goes_through_the_backend_with_the_boards_title(
    git_project: Path,
) -> None:
    """End to end over HTTP: the draft's files, the board's title, the reviewer's body."""
    from voxarium_studio.main import create_app

    with TestClient(create_app(str(git_project)), base_url="http://127.0.0.1") as client:
        ctx: AppContext = client.app.state.ctx
        await _no_isolation(ctx)
        created = client.post(
            f"/api/teams/{TEAM}/tasks",
            json={"assignee_id": DEV, "title": "Grapple hook", "brief": "b"},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        display_id = created.json()["display_id"]

        (git_project / "Source").mkdir()
        (git_project / "Source/Hook.cpp").write_text("int hook;\n", encoding="utf-8")
        (git_project / "README.md").write_text("hello\nand more\n", encoding="utf-8")
        (git_project / "untouched.txt").write_text("not this task's\n", encoding="utf-8")
        (git_project / "same.txt").write_text("unchanged\n", encoding="utf-8")
        _git(git_project, "add", "same.txt")
        _git(git_project, "commit", "-m", "a file the task will touch without changing")
        await _edited(ctx, task_id, str(git_project / "Source/Hook.cpp"))
        await _edited(ctx, task_id, str(git_project / "README.md"))
        # Named by a tool call, byte-identical afterwards: in the draft, not in the commit.
        await _edited(ctx, task_id, str(git_project / "same.txt"))

        async def fake(prompt: str, model: str, cwd: Path) -> str:
            assert "+int hook;" in prompt, "the diff reaches the model"
            return "Adds the hook and documents it."

        ctx.teams.commit_describer = fake
        await ctx.teams.submit_for_review(task_id, "Done.")
        await _drafted(ctx, task_id)

        draft = client.get(f"/api/teams/{TEAM}/tasks/{task_id}/commit").json()
        assert draft["files"] == ["Source/Hook.cpp", "README.md", "same.txt"]
        assert draft["message"] == "Adds the hook and documents it."
        assert draft["vcs"]["backend"] == "git" and draft["vcs"]["available"] is True

        # A path outside the task's directory is refused before any tool runs.
        refused = client.post(
            f"/api/teams/{TEAM}/tasks/{task_id}/commit",
            json={"files": [str(git_project.parent / "elsewhere.txt")]},
        )
        assert refused.status_code == 409 and "outside" in refused.json()["detail"]

        response = client.post(
            f"/api/teams/{TEAM}/tasks/{task_id}/commit",
            json={"message": "Adds the hook.\n\nThe reviewer trimmed this."},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["committed"] is True and result["branch"] == "main"
        assert sorted(result["files"]) == ["README.md", "Source/Hook.cpp"]
        assert result["skipped"] == ["same.txt"], (
            "a listed file the workspace does not report as changed is left out, not sent to "
            "a tool that would refuse the whole commit over it"
        )

        body = _git(git_project, "log", "-1", "--format=%B")
        assert body.splitlines()[0] == f"{display_id} | Grapple hook"
        assert "The reviewer trimmed this." in body
        assert "untouched.txt" in _git(git_project, "status", "--porcelain"), (
            "a file the task never named stays uncommitted"
        )

        after = client.get(f"/api/teams/{TEAM}/tasks/{task_id}/commit").json()
        assert after["revision"] == result["revision"]
        async with ctx.db.session() as session:
            row = await session.get(TeamTask, task_id)
            assert row.commit_revision == result["revision"]
            entries = (
                await session.exec(
                    select(AgentMessage)
                    .where(AgentMessage.task_id == task_id)
                    .where(AgentMessage.kind == "commit")
                )
            ).all()
        assert len(entries) == 1 and entries[0].data["revision"] == result["revision"]

        # Nothing left for the same draft: a second press is an answer, not an error, and
        # every file is reported as skipped rather than handed to the tool.
        again = client.post(f"/api/teams/{TEAM}/tasks/{task_id}/commit", json={})
        assert again.status_code == 200 and again.json()["committed"] is False
        assert again.json()["skipped"] == ["Source/Hook.cpp", "README.md", "same.txt"]
