"""The version control abstraction and its built-in git backend (§10.5).

The git half runs against a real repository in ``tmp_path`` rather than a mocked
subprocess: the whole point of the backend is the exact arguments it sends, and a mock
would only assert that the code says what the code says. Everything else — resolution
order, the plugin extension point, isolation fallback — needs no tool at all.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from voxarium_studio.core.config import VcsConfig
from voxarium_studio.main import create_app
from voxarium_studio.teams import worktree as wt
from voxarium_studio import vcs as vcs_module
from voxarium_studio.vcs import CHANGE_KINDS, GitBackend, candidate_backends, detect_backend
from voxarium_studio.vcs.base import CommitResult, VcsBackend, VcsError, VcsStatus, run_tool
from voxarium_studio.vcs.git import _parse_porcelain

pytestmark = pytest.mark.real_vcs
"""This module is the one that means the machine's real answer — see the
`no_machine_version_control` fixture in conftest, which every other module gets."""

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _init_repo(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Studio Test")
    git("config", "commit.gpgsign", "false")
    git("add", "-A")
    git("commit", "-m", "first")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _init_repo(root)
    return root


@pytest.fixture
def git_project(project: Path) -> Path:
    """A managed project that is *also* a git repository — what `/api/vcs` runs against."""
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _init_repo(project)
    return project


@pytest.fixture
def git_client(git_project: Path) -> Iterator[TestClient]:
    with TestClient(create_app(str(git_project)), base_url="http://127.0.0.1") as test_client:
        yield test_client


# --------------------------------------------------------------------------- git backend


@requires_git
async def test_detects_a_repository_and_reads_its_branch(git_repo: Path, tmp_path: Path) -> None:
    backend = GitBackend(git_repo)
    assert await backend.detect() is True
    assert await backend.current_branch() == "main"
    assert await backend.revision()

    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert await GitBackend(outside).detect() is False


@requires_git
async def test_status_separates_untracked_from_modified(git_repo: Path) -> None:
    (git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    (git_repo / "new asset.png").write_bytes(b"\x89PNG")

    status = await GitBackend(git_repo).status()

    assert status.clean is False
    by_path = {change.path: change for change in status.changes}
    assert by_path["README.md"].kind == "modified"
    # A space in the name is the case `-z` exists for: porcelain would quote it.
    assert by_path["new asset.png"].kind == "untracked"


@requires_git
async def test_a_wholly_untracked_directory_reports_its_files_not_itself(
    git_repo: Path,
) -> None:
    """Git collapses an entirely-untracked directory into one `scripts/` entry.

    The Explorer colours rows by exact path, so a collapsed entry leaves every file in a
    newly added folder unmarked — and that folder's own row unmatched too, because of the
    trailing slash. `-uall` is what makes `FileChange.path` always name a file.
    """
    nested = git_repo / "scripts" / "ai"
    nested.mkdir(parents=True)
    (nested / "Enemy.as").write_text("class Enemy {}\n", encoding="utf-8")
    (git_repo / "scripts" / "Weapon.as").write_text("class Weapon {}\n", encoding="utf-8")

    status = await GitBackend(git_repo).status()
    paths = {change.path: change.kind for change in status.changes}

    assert paths["scripts/ai/Enemy.as"] == "untracked"
    assert paths["scripts/Weapon.as"] == "untracked"
    # Neither the collapsed form nor a slash-suffixed directory survives.
    assert "scripts/" not in paths
    assert not any(path.endswith("/") for path in paths)


@requires_git
async def test_status_is_reported_in_the_repositorys_frame(tmp_path: Path) -> None:
    """`git status --porcelain` prints repository-relative paths wherever it ran.

    A managed project one level inside a monorepo is a layout `detect` accepts, so the
    backend has to say which frame it is answering in — `VcsStatus.root` — and the paths
    stay as git gave them. Re-basing onto the project is the API layer's job.
    """
    repo = tmp_path / "monorepo"
    (repo / "game").mkdir(parents=True)
    (repo / "engine").mkdir()
    (repo / "game" / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / "engine" / "notes.txt").write_text("elsewhere\n", encoding="utf-8")
    _init_repo(repo)
    (repo / "game" / "README.md").write_text("edited\n", encoding="utf-8")
    (repo / "engine" / "notes.txt").write_text("also edited\n", encoding="utf-8")

    backend = GitBackend(repo / "game")
    status = await backend.status()

    assert await backend.detect() is True
    assert status.root == repo
    assert {change.path for change in status.changes} == {
        "game/README.md",
        "engine/notes.txt",
    }
    # The project at the repository root pays no extra call to find that out.
    assert await GitBackend(repo).repository_root() == repo


@requires_git
async def test_commit_records_a_revision_and_the_files_it_took(git_repo: Path) -> None:
    (git_repo / "Assets").mkdir()
    (git_repo / "Assets" / "golem.vox").write_bytes(b"VOX ")

    backend = GitBackend(git_repo)
    result = await backend.commit("add the golem", add_untracked=True)

    assert result.committed is True
    assert result.revision and result.branch == "main"
    assert result.files == ["Assets/golem.vox"]
    assert (await backend.status()).clean is True


@requires_git
async def test_untracked_files_stay_out_unless_asked_for(git_repo: Path) -> None:
    (git_repo / "scratch.tmp").write_text("junk", encoding="utf-8")

    result = await GitBackend(git_repo).commit("nothing tracked changed")

    assert result.committed is False
    assert result.revision is None


@requires_git
async def test_commit_scoped_to_paths_leaves_the_rest_pending(git_repo: Path) -> None:
    (git_repo / "wanted.txt").write_text("a", encoding="utf-8")
    (git_repo / "unwanted.txt").write_text("b", encoding="utf-8")

    backend = GitBackend(git_repo)
    result = await backend.commit("just the one", paths=["wanted.txt"], add_untracked=True)

    assert result.files == ["wanted.txt"]
    assert [change.path for change in (await backend.status()).changes] == ["unwanted.txt"]


@requires_git
async def test_commit_without_a_message_is_refused(git_repo: Path) -> None:
    with pytest.raises(VcsError):
        await GitBackend(git_repo).commit("   ")


@requires_git
async def test_isolated_workspace_round_trip(git_repo: Path, tmp_path: Path) -> None:
    backend = GitBackend(git_repo)
    worktrees = tmp_path / "worktrees"

    created = await wt.create_isolated_workspace(backend, worktrees, "task-1", "dev")

    assert created is not None
    assert created.path.is_dir()
    assert created.branch == "voxarium_studio/dev/task-1"
    assert (created.path / "README.md").exists()

    await wt.remove_isolated_workspace(backend, created.path)
    assert not created.path.exists()


def test_parse_porcelain_reads_renames_and_staging() -> None:
    raw = "\0".join(
        [
            "R  new/name.txt",
            "old/name.txt",
            " M edited.txt",
            "?? untracked.txt",
            "UU conflicted.txt",
            "A  added.txt",
        ]
    )

    changes = {change.path: change for change in _parse_porcelain(raw)}

    assert changes["new/name.txt"].kind == "renamed"
    assert changes["new/name.txt"].origin == "old/name.txt"
    assert changes["edited.txt"].staged is False
    assert changes["untracked.txt"].kind == "untracked"
    assert changes["conflicted.txt"].kind == "conflicted"
    assert changes["added.txt"].staged is True


# --------------------------------------------------------------------------- the runner


async def test_run_tool_kills_a_process_that_overruns_its_timeout() -> None:
    with pytest.raises(VcsError, match="did not finish"):
        await run_tool(
            sys.executable,
            ["-c", "import time; time.sleep(30)"],
            cwd=Path.cwd(),
            timeout=0.5,
        )


async def test_run_tool_reports_a_missing_program() -> None:
    with pytest.raises(VcsError, match="not on PATH"):
        await run_tool("voxarium_studio-no-such-tool", ["--version"], cwd=Path.cwd())


async def test_run_tool_streams_output_to_the_log() -> None:
    lines: list[str] = []

    async def collect(line: str) -> None:
        lines.append(line)

    result = await run_tool(
        sys.executable, ["-c", "print('hello')"], cwd=Path.cwd(), log_fn=collect
    )

    assert result.ok
    assert lines[0].startswith("$ ")  # the command itself, so a log reads as a transcript
    assert "hello" in lines


# --------------------------------------------------------------------------- resolution


class FakeBackend(VcsBackend):
    """Minimal entity for testing catalog resolution."""

    id: ClassVar[str] = "fake"
    name: ClassVar[str] = "Fake VCS"
    program: ClassVar[str] = sys.executable
    supports_isolation: ClassVar[bool] = False

    claims = True

    async def detect(self) -> bool:
        return self.claims

    async def current_branch(self) -> str | None:
        return "trunk"

    async def status(self) -> VcsStatus:
        return VcsStatus(backend=self.id, root=self.root)

    async def diff(self, path: Path | str | None = None) -> str:
        return ""

    async def add(self, paths: Sequence[str], *, recursive: bool = True, log_fn=None) -> list[str]:
        return list(paths)

    async def commit(self, message: str, **kwargs) -> CommitResult:
        return CommitResult(committed=True, revision="1", message=message)


@requires_git
async def test_a_specific_workspace_is_asked_before_git(git_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(vcs_module, "BUILTIN_BACKENDS", (FakeBackend, GitBackend))
    assert isinstance(await detect_backend(git_repo), FakeBackend)
    monkeypatch.setattr(FakeBackend, "claims", False)
    assert isinstance(await detect_backend(git_repo), GitBackend)


async def test_configured_backend_wins_over_detection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(vcs_module, "BUILTIN_BACKENDS", (FakeBackend, GitBackend))
    assert isinstance(await detect_backend(tmp_path, configured="git"), GitBackend)


async def test_unknown_configured_backend_falls_back_to_detection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(vcs_module, "BUILTIN_BACKENDS", (FakeBackend, GitBackend))
    assert isinstance(await detect_backend(tmp_path, configured="mercurial"), FakeBackend)


async def test_a_backend_that_raises_while_detecting_is_skipped(tmp_path: Path, monkeypatch) -> None:
    class Exploding(FakeBackend):
        id: ClassVar[str] = "exploding"

        async def detect(self) -> bool:
            raise RuntimeError("no server")

    monkeypatch.setattr(vcs_module, "BUILTIN_BACKENDS", (Exploding, FakeBackend))
    assert isinstance(await detect_backend(tmp_path), FakeBackend)


async def test_nothing_detected_is_not_an_error(tmp_path: Path) -> None:
    assert await detect_backend(tmp_path) is None


def test_catalog_always_contains_all_entities_in_detection_order() -> None:
    assert [backend.id for backend in candidate_backends()] == ["plastic", "perforce", "git"]


def test_config_reads_none_as_disabled() -> None:
    assert VcsConfig(backend="none").disabled is True
    assert VcsConfig(backend="auto").selected() is None
    assert VcsConfig(backend="plastic").selected() == "plastic"
    assert VcsConfig().disabled is False


# --------------------------------------------------------------------------- isolation fallback


async def test_isolation_is_skipped_when_the_backend_cannot_do_it(tmp_path: Path) -> None:
    assert await wt.create_isolated_workspace(None, tmp_path, "t1", "dev") is None
    assert await wt.create_isolated_workspace(FakeBackend(tmp_path), tmp_path, "t1", "dev") is None
    # Removal has to stay a no-op too, so cleanup never fails a task.
    await wt.remove_isolated_workspace(None, tmp_path / "missing")


async def test_isolation_failure_is_downgraded_to_no_isolation(tmp_path: Path) -> None:
    class Refusing(FakeBackend):
        id: ClassVar[str] = "refusing"
        supports_isolation: ClassVar[bool] = True

        async def create_workspace(self, path: Path, branch: str):
            raise VcsError("server unreachable")

    assert await wt.create_isolated_workspace(Refusing(tmp_path), tmp_path, "t1", "dev") is None


# --------------------------------------------------------------------------- REST surface


def test_project_and_status_report_version_control(client) -> None:
    project = client.get("/api/project").json()
    assert "vcs" in project  # None for a tmp_path project, which is under nothing

    backends = client.get("/api/status").json()["vcs_backends"]
    assert "git" in backends
    assert set(backends["git"]) == {"name", "available", "reason", "active"}


def test_asyncio_detection_does_not_leak_processes(git_repo: Path) -> None:
    """`voxarium_studio info` resolves the backend outside a running loop — keep that working."""
    resolved = asyncio.run(detect_backend(git_repo))
    assert resolved is not None and resolved.id == "git"


OVERVIEW_KEYS = {"active", "configured", "backends", "branch", "revision"}
OPTION_KEYS = {
    "id",
    "name",
    "root",
    "available",
    "reason",
    "supports_isolation",
    "settings",
    "settings_fields",
    "program",
    "executable",
    "detected",
    "active",
}
STATUS_KEYS = {"backend", "root", "branch", "revision", "clean", "changes"}


@requires_git
def test_overview_answers_the_frozen_shape(git_client: TestClient) -> None:
    """`VcsOverview` / `VcsBackendOption` in src/api/types.ts are the specification."""
    overview = git_client.get("/api/vcs").json()

    assert set(overview) == OVERVIEW_KEYS
    assert overview["configured"] == "auto"
    assert overview["active"]["id"] == "git"
    assert overview["branch"] == "main"
    assert overview["revision"]

    git = next(option for option in overview["backends"] if option["id"] == "git")
    assert set(git) == OPTION_KEYS
    assert git["detected"] is True and git["active"] is True
    assert git["settings"] == {}
    assert git["settings_fields"] == [{"key": "executable", "type": "path", "default": "git"}]
    assert git["program"] == "git" and git["executable"] == "git"


def test_every_vcs_entity_is_listed_as_a_candidate(client: TestClient) -> None:
    """Plastic is not installed on the machine, but its backend is still on offer."""
    overview = client.get("/api/vcs").json()

    plastic = next(option for option in overview["backends"] if option["id"] == "plastic")
    assert set(plastic) == OPTION_KEYS
    assert plastic["settings"] == {}
    assert {field["key"] for field in plastic["settings_fields"]} == {"executable", "workspace"}
    assert plastic["active"] is False
    assert [option["id"] for option in overview["backends"]] == ["plastic", "perforce", "git"]


@requires_git
def test_status_maps_every_working_copy_state_onto_a_kind(
    git_client: TestClient, git_project: Path
) -> None:
    (git_project / "README.md").write_text("edited\n", encoding="utf-8")
    (git_project / "untracked.png").write_bytes(b"\x89PNG")
    (git_project / "staged.txt").write_text("new\n", encoding="utf-8")
    (git_project / "gone.txt").write_text("bye\n", encoding="utf-8")
    subprocess.run(["git", "add", "gone.txt"], cwd=git_project, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "second"], cwd=git_project, check=True, capture_output=True
    )
    (git_project / "gone.txt").unlink()
    subprocess.run(["git", "add", "staged.txt"], cwd=git_project, check=True, capture_output=True)

    status = git_client.get("/api/vcs/status").json()

    assert set(status) == STATUS_KEYS
    assert status["backend"] == "git" and status["clean"] is False
    changes = {change["path"]: change for change in status["changes"]}
    assert changes["README.md"]["kind"] == "modified"
    assert changes["README.md"]["staged"] is False
    assert changes["untracked.png"]["kind"] == "untracked"
    assert changes["staged.txt"] == {
        "path": "staged.txt",
        "kind": "added",
        "staged": True,
        "origin": None,
        # `status` counts no lines — the fields exist for `workspace_changes`, which does,
        # and a caller must be able to tell "not counted" from "changed by zero lines".
        "insertions": None,
        "deletions": None,
        "binary": False,
    }
    assert changes["gone.txt"]["kind"] == "deleted"
    assert all(change["kind"] in CHANGE_KINDS for change in status["changes"])


@requires_git
def test_status_is_answered_in_the_projects_frame(tmp_path: Path) -> None:
    """A game project inside a monorepo — the Explorer only knows project-relative paths.

    Offset paths would not colour one file wrongly; they would colour nothing, which
    reads as a broken feature. Changes elsewhere in the repository are dropped for the
    same reason: there is nowhere in this project's tree to show them.
    """
    from voxarium_studio.core.project import init_project

    repo = tmp_path / "monorepo"
    game = repo / "game"
    game.mkdir(parents=True)
    (repo / "engine").mkdir()
    init_project(game)
    (game / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / "engine" / "notes.txt").write_text("elsewhere\n", encoding="utf-8")
    _init_repo(repo)

    (game / "README.md").write_text("edited\n", encoding="utf-8")
    (game / "new.png").write_bytes(b"\x89PNG")
    (repo / "engine" / "notes.txt").write_text("also edited\n", encoding="utf-8")

    with TestClient(create_app(str(game)), base_url="http://127.0.0.1") as nested_client:
        status = nested_client.get("/api/vcs/status").json()

    assert status["backend"] == "git"
    # `root` names the frame the paths are in, so it is the project's, not the repo's.
    assert Path(status["root"]) == game.resolve()
    # Whatever the Studio itself writes is beside the point here — its data directory, and
    # the roster projected into `.claude/agents/` when the app started (§10.3). Both are
    # deliberately left visible to git rather than ignored: the Studio is a guest at the
    # project root and does not edit anyone's `.gitignore`.
    paths = {
        change["path"]
        for change in status["changes"]
        if not change["path"].startswith(("voxarium_studio/", ".claude/"))
    }
    assert paths == {"README.md", "new.png"}




def test_a_project_under_nothing_is_a_200_not_a_404(client: TestClient, project: Path) -> None:
    """The Explorer calls this on every refresh and must not have to handle a 404."""
    response = client.get("/api/vcs/status")

    assert response.status_code == 200
    payload = response.json()
    assert Path(payload.pop("root")) == project.resolve()
    assert payload == {
        "backend": None,
        "branch": None,
        "revision": None,
        "clean": True,
        "changes": [],
    }


@requires_git
def test_choosing_none_then_auto_round_trips(git_client: TestClient) -> None:
    off = git_client.put("/api/vcs/settings", json={"backend": "none"})

    assert off.status_code == 200
    assert off.json()["configured"] == "none"
    assert off.json()["active"] is None
    # Switched off is still a normal status, and still not an error.
    assert git_client.get("/api/vcs/status").json()["backend"] is None
    # …but an operation says what to do about it rather than failing obscurely.
    refused = git_client.post("/api/vcs/commit", json={"message": "nope"})
    assert refused.status_code == 409
    assert "switched off" in refused.json()["detail"]

    back = git_client.put("/api/vcs/settings", json={"backend": "auto"})

    assert back.json()["configured"] == "auto"
    assert back.json()["active"]["id"] == "git"
    assert git_client.get("/api/vcs/status").json()["backend"] == "git"


@requires_git
def test_settings_merge_key_by_key(git_client: TestClient, git_project: Path) -> None:
    """A patch that names one key must leave every other key standing."""
    import yaml

    git_client.put("/api/vcs/settings", json={"settings": {"plastic": {"workspace": "/elsewhere"}}})
    git_client.put(
        "/api/vcs/settings", json={"settings": {"git": {"executable": shutil.which("git")}}}
    )

    stored = git_client.get("/api/settings").json()["vcs"]["settings"]
    on_disk = yaml.safe_load((git_project / "voxarium_studio" / "config.yaml").read_text(encoding="utf-8"))

    assert stored["plastic"]["workspace"] == "/elsewhere"  # the unrelated key survived
    assert stored["git"]["executable"] == shutil.which("git")
    assert on_disk["vcs"]["settings"] == stored  # …and it is in config.yaml, not just memory

    # The merged value is also what the backend would actually run.
    overview = git_client.get("/api/vcs").json()
    git = next(option for option in overview["backends"] if option["id"] == "git")
    assert git["executable"] == shutil.which("git")


@requires_git
def test_an_unusable_executable_is_503_and_clearing_it_recovers(git_client: TestClient) -> None:
    git_client.put(
        "/api/vcs/settings", json={"settings": {"git": {"executable": "C:/nowhere/git.exe"}}}
    )

    broken = git_client.get("/api/vcs/status")
    assert broken.status_code == 503
    assert "C:/nowhere/git.exe" in broken.json()["detail"]

    # Clearing a key removes it rather than storing "", which would shadow a plugin's own
    # setting for the same key while looking cleared.
    git_client.put("/api/vcs/settings", json={"settings": {"git": {"executable": ""}}})

    assert "git" not in git_client.get("/api/settings").json()["vcs"]["settings"]
    assert git_client.get("/api/vcs/status").status_code == 200


def test_an_unknown_backend_is_refused(client: TestClient) -> None:
    chosen = client.put("/api/vcs/settings", json={"backend": "mercurial"})
    assert chosen.status_code == 400
    assert "mercurial" in chosen.json()["detail"]

    configured = client.put("/api/vcs/settings", json={"settings": {"mercurial": {"x": "1"}}})
    assert configured.status_code == 400

    # Nothing was written on the way to either refusal.
    assert client.get("/api/settings").json()["vcs"] == {"backend": None, "settings": {}}


@requires_git
def test_commit_over_http_and_the_nothing_to_commit_case(
    git_client: TestClient, git_project: Path
) -> None:
    (git_project / "Assets").mkdir()
    (git_project / "Assets" / "golem.vox").write_bytes(b"VOX ")

    committed = git_client.post(
        "/api/vcs/commit", json={"message": "add the golem", "add_untracked": True}
    ).json()

    assert set(committed) == {"committed", "revision", "branch", "files", "message", "skipped"}
    assert committed["committed"] is True
    assert "Assets/golem.vox" in committed["files"]
    assert committed["branch"] == "main" and committed["revision"]

    # Committing again is an outcome, not an error — the same rule a pipeline node relies
    # on when it regenerates a byte-identical asset (§9.6).
    again = git_client.post("/api/vcs/commit", json={"message": "nothing left"})
    assert again.status_code == 200
    assert again.json()["committed"] is False
    assert again.json()["revision"] is None


@requires_git
def test_diff_takes_one_file_and_refuses_an_escape(
    git_client: TestClient, git_project: Path
) -> None:
    (git_project / "README.md").write_text("edited\n", encoding="utf-8")

    whole = git_client.get("/api/vcs/diff").json()
    one = git_client.get("/api/vcs/diff", params={"path": "README.md"}).json()

    assert set(one) == {"diff"}
    assert "README.md" in whole["diff"] and "README.md" in one["diff"]
    # `..` is normalised out of a URL by httpx, so a traversal test has to encode it.
    assert git_client.get("/api/vcs/diff?path=%2e%2e%2fescape.txt").status_code == 403


def test_commit_and_diff_without_a_backend_are_409(client: TestClient) -> None:
    assert client.post("/api/vcs/commit", json={"message": "x"}).status_code == 409
    assert client.get("/api/vcs/diff").status_code == 409
    assert "not under any version control" in client.get("/api/vcs/diff").json()["detail"]


# --------------------------------------------------------------------------- the status gate


class SlowBackend(FakeBackend):
    """Counts how many times the tool was actually asked."""

    id: ClassVar[str] = "slow"

    def __init__(self, root: Path, settings=None) -> None:
        super().__init__(root, settings)
        self.calls = 0

    async def status(self) -> VcsStatus:
        self.calls += 1
        await asyncio.sleep(0.05)
        return VcsStatus(backend=self.id, root=self.root)


async def test_a_burst_of_status_calls_runs_one_tool(ctx) -> None:
    """The Explorer refreshes on file-change events, which arrive in bursts."""
    from voxarium_studio.api import vcs as vcs_api

    ctx.vcs = SlowBackend(ctx.paths.root)

    answers = await asyncio.gather(*[vcs_api.get_vcs_status(ctx) for _ in range(6)])

    assert ctx.vcs.calls == 1
    assert all(answer["backend"] == "slow" for answer in answers)


async def test_the_cached_status_expires(ctx, monkeypatch) -> None:
    """A very short TTL, not a permanent cache — a saved file has to show up."""
    from voxarium_studio.api import vcs as vcs_api

    ctx.vcs = SlowBackend(ctx.paths.root)
    monkeypatch.setattr(vcs_api, "STATUS_TTL", 0.0)

    await vcs_api.get_vcs_status(ctx)
    await vcs_api.get_vcs_status(ctx)

    assert ctx.vcs.calls == 2


async def test_a_new_backend_invalidates_the_cached_status(ctx) -> None:
    from voxarium_studio.api import vcs as vcs_api

    ctx.vcs = SlowBackend(ctx.paths.root)
    await vcs_api.get_vcs_status(ctx)

    ctx.vcs = None
    assert (await vcs_api.get_vcs_status(ctx))["backend"] is None


async def test_an_unrecognised_kind_is_reported_as_unknown(ctx) -> None:
    """`VcsChangeKind` is a closed union; a third-party word must not reach the UI."""
    from voxarium_studio.api import vcs as vcs_api
    from voxarium_studio.vcs.base import FileChange

    class Inventive(SlowBackend):
        id: ClassVar[str] = "inventive"

        async def status(self) -> VcsStatus:
            return VcsStatus(
                backend=self.id,
                root=self.root,
                changes=[FileChange(path="a.png", kind="checked-out")],
            )

    ctx.vcs = Inventive(ctx.paths.root)

    assert (await vcs_api.get_vcs_status(ctx))["changes"][0]["kind"] == "unknown"


async def test_clean_describes_the_project_and_not_the_whole_workspace(ctx) -> None:
    """`clean` has to agree with the list beside it, and the list is the project's.

    A backend answers about its whole workspace, and re-basing then drops everything
    outside the managed project — so a project inside a monorepo whose *sibling* directory
    is dirty answered `clean: false` next to an empty `changes`, a payload contradicting
    itself. The tree colours nothing there, correctly; a client trusting `clean` would say
    the project has uncommitted work when it has none.
    """
    from voxarium_studio.api import vcs as vcs_api
    from voxarium_studio.vcs.base import FileChange

    workspace = ctx.paths.root.parent
    project_name = ctx.paths.root.name

    class Monorepo(SlowBackend):
        id: ClassVar[str] = "monorepo"
        changes: ClassVar[list[str]] = ["engine/notes.txt"]

        async def status(self) -> VcsStatus:
            return VcsStatus(
                backend=self.id,
                root=workspace,
                changes=[FileChange(path=path, kind="modified") for path in self.changes],
            )

    ctx.vcs = Monorepo(workspace)
    payload = await vcs_api.get_vcs_status(ctx)

    assert payload["changes"] == []
    assert payload["clean"] is True

    # The other direction still holds: something inside the project is not clean.
    Monorepo.changes = ["engine/notes.txt", f"{project_name}/Assets/rock.png"]
    ctx.vcs = Monorepo(workspace)  # a new instance drops the cached answer
    payload = await vcs_api.get_vcs_status(ctx)

    assert [change["path"] for change in payload["changes"]] == ["Assets/rock.png"]
    assert payload["clean"] is False


# --------------------------------------------------------------------------- re-basing


def test_absolute_paths_are_rebased_or_dropped(tmp_path: Path) -> None:
    """`cm` answers with absolute paths, and the project can be one folder of a workspace."""
    from voxarium_studio.api.vcs import _rebase

    workspace, project = tmp_path / "wk", tmp_path / "wk" / "game"

    def change(path: Path) -> dict[str, object]:
        return {"path": str(path), "kind": "added", "staged": True, "origin": None}

    changes = [change(project / "Assets" / "a.png"), change(workspace / "engine" / "b.png")]

    assert [change["path"] for change in _rebase(changes, workspace, project)] == ["Assets/a.png"]


def test_absolute_paths_are_rebased_when_the_project_is_the_workspace(tmp_path: Path) -> None:
    """The common Plastic layout: `.plastic` sits in the project itself, and `cm status`
    still prints absolute paths. Equal roots used to skip the pass entirely, which left every
    path absolute and the Explorer colouring nothing (observed in a sample project)."""
    from voxarium_studio.api.vcs import _rebase

    def change(path: str, kind: str) -> dict[str, object]:
        return {"path": path, "kind": kind, "staged": True, "origin": None}

    # `cm` prints the drive letter in lower case, whatever the project root says.
    root = str(tmp_path)
    if tmp_path.drive:
        root = tmp_path.drive.lower() + root[len(tmp_path.drive) :]
    changes = [
        change(str(Path(root, "Script", "Items", "a.as")), "modified"),
        change(str(Path(root, "Script", "b.as")), "untracked"),
        change("Assets/c.png", "added"),  # a relative path is already in the frame
        change(str(tmp_path.parent / "elsewhere.txt"), "modified"),  # outside: dropped
    ]

    assert [change["path"] for change in _rebase(changes, tmp_path, tmp_path)] == [
        "Script/Items/a.as",
        "Script/b.as",
        "Assets/c.png",
    ]


def test_a_rename_from_outside_the_project_keeps_no_origin(tmp_path: Path) -> None:
    from voxarium_studio.api.vcs import _rebase

    changes = [
        {"path": "game/new.png", "kind": "renamed", "staged": True, "origin": "engine/old.png"}
    ]

    assert _rebase(changes, tmp_path, tmp_path / "game") == [
        {"path": "new.png", "kind": "renamed", "staged": True, "origin": None}
    ]


def test_a_workspace_that_does_not_contain_the_project_is_left_alone(tmp_path: Path) -> None:
    """No relationship to re-base along — inventing one would empty the list."""
    from voxarium_studio.api.vcs import _rebase

    changes = [{"path": "Assets/a.png", "kind": "modified", "staged": False, "origin": None}]

    assert _rebase(changes, tmp_path / "elsewhere", tmp_path / "project") == changes


# --------------------------------------------------------------------------- settings merge


def test_revision_is_declared_on_the_base_and_defaults_to_nothing(tmp_path: Path) -> None:
    """So `/api/vcs` can ask any backend, and a plugin backend that has one is used."""

    async def check() -> None:
        assert await FakeBackend(tmp_path).revision() is None

    asyncio.run(check())


def test_config_holds_per_backend_settings_for_the_built_in_ones() -> None:
    config = VcsConfig(settings={"git": {"executable": "C:/Git/cmd/git.exe"}})
    assert config.settings["git"]["executable"] == "C:/Git/cmd/git.exe"
    assert VcsConfig().settings == {}


def test_a_built_in_backend_reads_only_the_config_section(ctx) -> None:
    """Git ships with the Studio, so it has no plugin settings to merge with."""
    ctx.config.vcs.settings["git"] = {"executable": "C:/Git/cmd/git.exe"}

    assert ctx.vcs_settings("git") == {"executable": "C:/Git/cmd/git.exe"}
    assert ctx.vcs_settings("nothing-registered") == {}
