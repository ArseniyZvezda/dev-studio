"""What one task changed (§10.4, §10.5) — the column beside a task's transcript.

Two sources with two different truth values, so they are tested apart: a real git worktree
for the workspace source, and a transcript with no worktree at all for the fallback.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from voxarium_studio.core.context import AppContext
from voxarium_studio.models.teams import TeamMember, TeamTask
from voxarium_studio.teams.changes import edit_stat, edit_totals, files_touched, tool_name
from voxarium_studio.vcs.git import GitBackend

pytestmark = pytest.mark.real_vcs
"""These build a real repository, so they mean the machine's real answer — see
`no_machine_version_control` in conftest, which every other module gets."""

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TEAM = "team_default"
DEV = f"{TEAM}__dev"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def git_project(project: Path) -> Path:
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    (project / "keep.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Studio Test")
    _git(project, "config", "commit.gpgsign", "false")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "first")
    return project


# --------------------------------------------------------------- the workspace source


@requires_git
async def test_a_workspace_reports_committed_and_uncommitted_work_alike(
    git_project: Path,
) -> None:
    """The whole reason this is not `status()`: an agent that commits is still working.

    A worktree is created the way §10.5 creates one, then given one committed change and
    one that is still only on disk. Both have to appear, or a reviewer reads "no files
    changed" about a task that rewrote the project.
    """
    backend = GitBackend(git_project)
    workspace = await backend.create_workspace(git_project / "wt", "voxarium_studio/dev/task_1")
    assert workspace is not None

    (workspace.path / "keep.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
    _git(workspace.path, "add", "keep.txt")
    _git(workspace.path, "commit", "-m", "committed inside the worktree")
    (workspace.path / "README.md").write_text("hello\nand more\n", encoding="utf-8")

    status = await backend.workspace_changes(workspace.path)
    changes = {change.path: change for change in status.changes}

    assert status.branch == "voxarium_studio/dev/task_1"
    assert changes["keep.txt"].kind == "modified"
    assert (changes["keep.txt"].insertions, changes["keep.txt"].deletions) == (2, 1)
    assert changes["README.md"].insertions == 1, "the uncommitted half counts too"


@requires_git
async def test_a_workspace_reports_new_files_git_has_never_heard_of(git_project: Path) -> None:
    """`git diff` cannot see an untracked file, and a new asset is what a task produces."""
    backend = GitBackend(git_project)
    workspace = await backend.create_workspace(git_project / "wt", "voxarium_studio/dev/task_2")
    assert workspace is not None
    (workspace.path / "fresh.txt").write_text("new file\n", encoding="utf-8")

    status = await backend.workspace_changes(workspace.path)
    changes = {change.path: change for change in status.changes}

    assert changes["fresh.txt"].kind == "untracked"
    assert changes["fresh.txt"].insertions is None, "nothing to diff against, so nothing counted"


@requires_git
async def test_a_workspace_patch_is_a_patch_and_not_a_summary(git_project: Path) -> None:
    """`diff()` answers `--stat`; a column that renders lines needs the lines."""
    backend = GitBackend(git_project)
    workspace = await backend.create_workspace(git_project / "wt", "voxarium_studio/dev/task_3")
    assert workspace is not None
    (workspace.path / "keep.txt").write_text("one\nchanged\nthree\n", encoding="utf-8")

    patch = await backend.workspace_patch(workspace.path, "keep.txt")

    assert "@@" in patch and "-two" in patch and "+changed" in patch


@requires_git
async def test_an_untracked_file_still_has_a_patch(git_project: Path) -> None:
    """It is in no diff, because git has never heard of it — so diff it against nothing."""
    backend = GitBackend(git_project)
    workspace = await backend.create_workspace(git_project / "wt", "voxarium_studio/dev/task_4")
    assert workspace is not None
    (workspace.path / "fresh.txt").write_text("first line\n", encoding="utf-8")

    patch = await backend.workspace_patch(workspace.path, "fresh.txt")

    assert "+first line" in patch


@requires_git
async def test_the_default_implementation_is_that_workspaces_status(git_project: Path) -> None:
    """What a backend with no override answers — uncommitted work, from inside the path.

    Checked through the base class rather than by writing a second backend: it is the
    behaviour every plugin backend inherits, and getting the root wrong there would report
    the *project's* changes as the task's.
    """
    from voxarium_studio.vcs.base import VcsBackend

    backend = GitBackend(git_project)
    workspace = await backend.create_workspace(git_project / "wt", "voxarium_studio/dev/task_5")
    assert workspace is not None
    (workspace.path / "keep.txt").write_text("edited in the worktree\n", encoding="utf-8")
    (git_project / "README.md").write_text("edited in the project\n", encoding="utf-8")

    status = await VcsBackend.workspace_changes(backend, workspace.path)

    assert [change.path for change in status.changes] == ["keep.txt"]


# --------------------------------------------------------------- the transcript source


def test_only_tools_that_write_are_counted(project: Path) -> None:
    """A read touches nothing, and a shell command *might* have touched anything."""

    class Message:
        def __init__(self, kind: str, data: dict) -> None:
            self.kind, self.data = kind, data

    root = project
    messages = [
        Message("tool_use", {"name": "Read", "input": {"file_path": str(root / "a.txt")}}),
        Message("tool_use", {"name": "Edit", "input": {"file_path": str(root / "src/b.ts")}}),
        Message("tool_use", {"name": "Bash", "input": {"command": "rm -rf src/c.ts"}}),
        Message("tool_use", {"name": "Write", "input": {"file_path": str(root / "src/b.ts")}}),
        Message("text", {"name": "Edit", "input": {"file_path": str(root / "never.txt")}}),
        Message(
            "tool_use",
            {"name": "mcp__voxarium_studio__write_doc", "input": {"path": "plans/dash.md"}},
        ),
    ]

    assert files_touched(messages, root) == ["src/b.ts", "plans/dash.md"]


def test_line_counts_are_summed_per_file_and_withheld_where_a_write_went_unmeasured(
    project: Path,
) -> None:
    """The column shows one figure per file: every measured call added up, or nothing."""

    class Message:
        def __init__(self, name: str, path: str, edit: dict | None = None) -> None:
            self.kind = "tool_use"
            self.data = {"name": name, "input": {"file_path": path}}
            if edit is not None:
                self.data["edit"] = edit

    root = project
    dash, hook, old = str(root / "src/dash.ts"), str(root / "src/hook.ts"), str(root / "old.md")
    messages = [
        Message("Edit", dash, {"insertions": 4, "deletions": 1}),
        Message("Edit", dash, {"insertions": 2, "deletions": 3}),
        Message("Write", hook, {"insertions": 20, "deletions": 0}),
        # One call on this file was never counted, so the file has no figure at all —
        # not a figure that is quietly the half of it somebody did count.
        Message("Edit", old, {"insertions": 1, "deletions": 1}),
        Message("Edit", old),
        Message("Edit", old, {"insertions": 5, "deletions": 5}),
    ]

    assert edit_totals(messages, root) == {
        "src/dash.ts": {"insertions": 6, "deletions": 4},
        "src/hook.ts": {"insertions": 20, "deletions": 0},
        "old.md": None,
    }


def test_the_mcp_prefix_is_stripped_before_matching() -> None:
    assert tool_name({"name": "mcp__voxarium_studio__write_doc"}) == "write_doc"
    assert tool_name({"name": "Edit"}) == "Edit"
    assert tool_name({}) == ""


# ------------------------------------------------------- how big one call's edit was


def test_an_edit_is_measured_as_a_diff_not_as_two_strings() -> None:
    """An `Edit` carries context so its match is unique — counting both strings whole would
    report a one-word fix as the whole block rewritten."""
    assert edit_stat("Edit", {"old_string": "a\nb\nc", "new_string": "a\nB\nc"}) == {
        "insertions": 1,
        "deletions": 1,
    }
    # An edit that only adds has nothing on the old side, and says so rather than nothing.
    assert edit_stat("Edit", {"old_string": "", "new_string": "one\ntwo"}) == {
        "insertions": 2,
        "deletions": 0,
    }


def test_a_write_counts_what_it_wrote_and_claims_nothing_about_what_it_replaced() -> None:
    """The file may not have existed, and the call carries no old side to diff against."""
    assert edit_stat("Write", {"file_path": "a.ts", "content": "one\ntwo\nthree\n"}) == {
        "insertions": 3,
        "deletions": 0,
    }
    # The MCP document tool writes a whole file too, and is matched on its own name.
    assert edit_stat("mcp__voxarium_studio__write_doc", {"path": "qa/notes.md", "content": "hi"}) == {
        "insertions": 1,
        "deletions": 0,
    }


def test_a_multi_edit_answers_once_for_the_whole_call_or_not_at_all() -> None:
    """The transcript shows one line for it, so it gets one figure — and an entry that
    cannot be read makes the total wrong rather than smaller."""
    assert edit_stat(
        "MultiEdit",
        {
            "edits": [
                {"old_string": "a", "new_string": "b"},
                {"old_string": "c\nd", "new_string": "c"},
            ]
        },
    ) == {"insertions": 1, "deletions": 2}
    assert edit_stat("MultiEdit", {"edits": [{"old_string": "a"}]}) is None
    assert edit_stat("MultiEdit", {"edits": []}) is None


def test_a_call_that_says_nothing_about_lines_answers_nothing() -> None:
    """`None` and `0` are different answers: one is "not counted", the other "changed
    without gaining or losing a line" — the same distinction `FileChange` draws."""
    assert edit_stat("Bash", {"command": "npm test"}) is None
    assert edit_stat("Read", {"file_path": "a.ts"}) is None
    # A writing tool whose payload never arrived is still not a measurement.
    assert edit_stat("Edit", {"file_path": "a.ts"}) is None
    assert edit_stat("Edit", "not a dict") is None


async def test_a_task_with_no_workspace_falls_back_to_its_own_tool_calls(
    ctx: AppContext,
) -> None:
    """Every task answers, and the payload says which question it answered."""
    async with ctx.db.session() as session:
        for member in (await session.exec(select(TeamMember))).all():
            member.isolate_worktree = False
            session.add(member)

    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="no checkout", brief="b", created_by=None
    )
    hook = str(ctx.paths.root / "Source/Hook.cpp")
    for edit in ({"insertions": 4, "deletions": 1}, {"insertions": 2, "deletions": 0}):
        await ctx.teams.record_message(
            task.id,
            DEV,
            role="assistant",
            kind="tool_use",
            text="Edit",
            data={"name": "Edit", "input": {"file_path": hook}, "edit": edit},
        )

    payload = await ctx.teams.task_changes(task.id)

    assert payload["source"] == "tools"
    assert [entry["path"] for entry in payload["files"]] == ["Source/Hook.cpp"]
    # The lines the calls asked for ride along, summed — the column draws them beside the
    # name, as it does for a workspace's measured ones.
    assert payload["files"][0]["insertions"] == 6
    assert payload["files"][0]["deletions"] == 1
    assert "no version control" in payload["reason"]


async def test_an_unknown_task_is_a_404_rather_than_a_subprocess(client: TestClient) -> None:
    response = client.get(f"/api/teams/{TEAM}/tasks/task_nope/changes")
    assert response.status_code == 404


@requires_git
async def test_the_endpoint_answers_from_the_workspace_when_there_is_one(
    git_project: Path,
) -> None:
    """End to end over HTTP, which is what the column actually calls."""
    with TestClient(_app(git_project), base_url="http://127.0.0.1") as client:
        response = client.post(
            f"/api/teams/{TEAM}/tasks",
            json={"assignee_id": DEV, "title": "isolated", "brief": "b"},
        )
        assert response.status_code == 201, response.text
        task_id = response.json()["id"]

        backend = GitBackend(git_project)
        workspace = await backend.create_workspace(
            git_project / "voxarium_studio/worktrees" / task_id, f"voxarium_studio/dev/{task_id}"
        )
        assert workspace is not None
        (workspace.path / "keep.txt").write_text("one\nchanged\nthree\n", encoding="utf-8")

        async with _context(client).db.session() as session:
            task = await session.get(TeamTask, task_id)
            task.worktree_path = str(workspace.path)
            session.add(task)

        payload = client.get(f"/api/teams/{TEAM}/tasks/{task_id}/changes").json()
        assert payload["source"] == "workspace"
        assert [entry["path"] for entry in payload["files"]] == ["keep.txt"]
        assert payload["files"][0]["insertions"] == 1

        patch = client.get(
            f"/api/teams/{TEAM}/tasks/{task_id}/changes/patch", params={"path": "keep.txt"}
        ).json()
        assert "+changed" in patch["patch"]


def _app(root: Path):
    from voxarium_studio.main import create_app

    return create_app(str(root))


def _context(client: TestClient) -> AppContext:
    return client.app.state.ctx
