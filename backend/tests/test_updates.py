"""The Studio updating itself (§12.1).

The git half runs against real repositories in ``tmp_path``, for the reason
``test_build_info.py`` and ``test_vcs.py`` both give: the value of the code is the exact
arguments it sends to git, and a mocked subprocess would only assert that the code says
what the code says. What is stubbed is the last step alone — spawning the relauncher and
stopping the server — because a test that let those run would end the test session.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voxarium_studio.core.config import UpdatesConfig
from voxarium_studio.updates import RelaunchTarget, UpdateRefused, UpdateService, running_work
from voxarium_studio.updates.service import UpdateState
from voxarium_studio.updates.versions import Release, latest_release, parse_tag, parse_tags
from voxarium_studio.ws.bus import EventBus

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# ------------------------------------------------------------------- reading tags


def test_a_release_tag_parses_with_or_without_its_v() -> None:
    assert parse_tag("v1.0") == parse_tag("1.0")
    assert parse_tag("v2.3.1").tag == "v2.3.1"


def test_the_same_release_written_two_ways_compares_equal() -> None:
    """`v1` and `v1.0` name one release, so neither may be offered as an update to the other."""
    assert parse_tag("v1") == parse_tag("v1.0") == parse_tag("v1.0.0")


def test_versions_order_by_number_and_not_as_text() -> None:
    ordered = [release.tag for release in parse_tags(["v0.9", "v0.10", "v1.0", "v0.2"])]
    assert ordered == ["v0.2", "v0.9", "v0.10", "v1.0"]


def test_a_tag_that_is_not_a_finished_release_is_ignored() -> None:
    """An unfinished or unorderable tag is not something to offer as an update."""
    assert parse_tag("v1.1-rc1") is None
    assert parse_tag("nightly") is None
    assert parse_tag("release/2026-05") is None
    assert latest_release(["v1.0", "v2.0-rc1", "nightly"]) == parse_tag("v1.0")


def test_no_tags_at_all_is_not_a_release() -> None:
    assert latest_release([]) is None
    assert latest_release(["nightly"]) is None


def test_running_work_names_what_a_restart_would_kill() -> None:
    assert running_work([]) is None
    assert running_work(["pipeline_run"]) == "1 pipeline run still running"
    assert (
        running_work(["pipeline_run", "agent_task", "agent_task"])
        == "1 pipeline run and 2 agent sessions still running"
    )


# ------------------------------------------------------------------- a repository to update


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def commit(root: Path, name: str, message: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(message, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", message)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A repository standing in for `origin`, holding one release."""
    root = tmp_path / "origin"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Studio Test")
    git(root, "config", "commit.gpgsign", "false")
    # The two directories this repository also ignores, and for the same reasons: the built
    # SPA is generated, and `.scratch/` is where the update writes its own transcript. Both
    # would otherwise read as uncommitted changes and refuse every update.
    (root / ".gitignore").write_text("backend/voxarium_studio/static/\n.scratch/\n", encoding="utf-8")
    commit(root, "README.md", "first")
    git(root, "tag", "v1.0")
    return root


@pytest.fixture
def studio(tmp_path: Path, upstream: Path) -> Path:
    """A clone of it, laid out like this repository and already built."""
    root = tmp_path / "studio"
    git(tmp_path, "clone", "--quiet", str(upstream), str(root))
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Studio Test")
    git(root, "config", "commit.gpgsign", "false")
    # A built SPA, so the rebuild step has nothing to do: what these tests are about is the
    # pull and the restart, and `npm` has no business inside the suite.
    static = root / "backend" / "voxarium_studio" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return root


class Restart:
    """What the last step did, in place of doing it."""

    def __init__(self) -> None:
        self.payloads: list[Path] = []
        self.stopped = False

    def spawn(self, payload_file: Path, env: dict[str, str] | None = None) -> None:
        self.payloads.append(payload_file)

    def shutdown(self) -> None:
        self.stopped = True


def build(studio: Path, restart: Restart, *, running: str | None = None) -> UpdateService:
    """A service whose "running build" is a commit this test chose.

    Pinned rather than left to `build_info()`, which answers with the commit of *this*
    repository — meaningless inside a `tmp_path` clone, so every test would otherwise fall
    through to the HEAD fallback and the anchor itself would go unexercised.
    """
    updates = UpdateService(
        bus=EventBus(),
        config=UpdatesConfig(),
        root=studio,
        running_commit=running or git(studio, "rev-parse", "HEAD"),
    )
    updates.spawn_relauncher = restart.spawn  # type: ignore[method-assign]
    updates.request_shutdown = restart.shutdown  # type: ignore[method-assign]
    return updates


@pytest.fixture
def service(studio: Path) -> Iterator[tuple[UpdateService, Restart]]:
    """The ordinary case: the Studio was started from the checkout as it stands."""
    restart = Restart()
    yield build(studio, restart), restart


def target_for(studio: Path) -> RelaunchTarget:
    return RelaunchTarget(project=studio / "game", port=8765)


async def run_update(updates: UpdateService, studio: Path, **kwargs: object) -> None:
    await updates.apply(target_for(studio), **kwargs)  # type: ignore[arg-type]
    assert updates._apply_task is not None
    await updates._apply_task


# ------------------------------------------------------------------- checking


@requires_git
async def test_a_checkout_on_the_newest_tag_has_nothing_to_offer(
    service: tuple[UpdateService, Restart],
) -> None:
    updates, _ = service
    status = await updates.check()
    assert status["supported"] is True
    assert status["current"] == "v1.0"
    assert status["latest"] == "v1.0"
    assert status["available"] is False
    assert status["branch"] == "main"
    assert status["upstream"] == "origin/main"


@requires_git
async def test_a_tag_pushed_upstream_becomes_an_available_update(
    service: tuple[UpdateService, Restart], upstream: Path
) -> None:
    updates, _ = service
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")

    status = await updates.check()
    assert status["available"] is True
    assert status["current"] == "v1.0"
    assert status["latest"] == "v1.1"


@requires_git
async def test_an_untagged_commit_upstream_is_not_an_update(
    service: tuple[UpdateService, Restart], upstream: Path
) -> None:
    """The branch moving is not a release; only a tag on it is (§12.1)."""
    updates, _ = service
    commit(upstream, "wip.txt", "not a release")

    status = await updates.check()
    assert status["available"] is False
    assert status["latest"] == "v1.0"


@requires_git
async def test_a_release_on_another_branch_is_not_offered(
    service: tuple[UpdateService, Restart], upstream: Path
) -> None:
    """`git pull` could not deliver it, so the button must not promise it."""
    updates, _ = service
    git(upstream, "checkout", "--quiet", "-b", "side")
    commit(upstream, "side.txt", "a side release")
    git(upstream, "tag", "v9.9")
    git(upstream, "checkout", "--quiet", "main")

    status = await updates.check()
    assert status["latest"] == "v1.0"
    assert status["available"] is False


@requires_git
async def test_a_checkout_ahead_of_its_last_tag_is_still_on_that_release(
    service: tuple[UpdateService, Restart], studio: Path, upstream: Path
) -> None:
    """Three commits past v1.0 is v1.0 as far as updating is concerned."""
    updates, _ = service
    commit(studio, "local.txt", "local work")
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")

    status = await updates.check()
    assert status["current"] == "v1.0"
    assert status["available"] is True


@requires_git
async def test_a_checkout_moved_without_a_restart_is_still_an_update(
    studio: Path, upstream: Path
) -> None:
    """The question is what is *running*, not what is on disk.

    Somebody pulls in a terminal while the Studio is up: the checkout is at v1.1 and the
    process is still the v1.0 one answering requests. Reading HEAD reported the Studio up to
    date — the one answer that is both wrong and reassuring.
    """
    was_running = git(studio, "rev-parse", "HEAD")
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")
    git(studio, "pull", "--ff-only", "--tags", "--quiet")

    restart = Restart()
    updates = build(studio, restart, running=was_running)
    status = await updates.check()

    assert status["current"] == "v1.0", "the build is what it is, whatever the tree says"
    assert status["latest"] == "v1.1"
    assert status["available"] is True


@requires_git
async def test_updating_a_moved_checkout_rebuilds_and_restarts_without_pulling_anything(
    studio: Path, upstream: Path
) -> None:
    """The pull has nothing to bring and there is still everything to do."""
    was_running = git(studio, "rev-parse", "HEAD")
    # A backend-only release, so the rebuild has nothing to do and `npm` stays out of the
    # suite — what this check is about is the restart happening at all.
    commit(upstream, "backend/voxarium_studio/engine/runner.py", "second")
    git(upstream, "tag", "v1.1")
    git(studio, "pull", "--ff-only", "--tags", "--quiet")

    restart = Restart()
    updates = build(studio, restart, running=was_running)
    await updates.check()
    await run_update(updates, studio)

    assert restart.stopped is True, "the process is still the old build until it restarts"
    assert updates.to_json()["state"] == UpdateState.RESTARTING


@requires_git
async def test_a_build_commit_git_cannot_resolve_falls_back_to_head(
    studio: Path, upstream: Path
) -> None:
    """A rebase or a force-push can take the running build's commit out of the repository.

    The fallback is HEAD, never "every tag" — a list holding releases this build does not
    have would put `current` at the newest of them and report the Studio up to date.
    """
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")

    restart = Restart()
    updates = build(studio, restart, running="0" * 40)
    status = await updates.check()

    assert status["current"] == "v1.0"
    assert status["available"] is True


@requires_git
async def test_an_unreachable_remote_leaves_the_last_answer_standing(
    service: tuple[UpdateService, Restart], studio: Path
) -> None:
    """Offline is the ordinary reason a fetch fails, and the local tags still answer."""
    updates, _ = service
    git(studio, "remote", "set-url", "origin", str(studio / "nowhere"))

    status = await updates.check()
    assert status["error"] is not None
    assert status["current"] == "v1.0"
    assert status["state"] == "idle"


async def test_an_installed_studio_says_why_it_cannot_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel has no git to ask and no source to rebuild, so the feature says so."""
    from voxarium_studio.updates import service as service_module

    monkeypatch.setattr(service_module, "studio_repo_root", lambda: None)
    updates = UpdateService(bus=EventBus(), config=UpdatesConfig())
    status = updates.to_json()
    assert status["supported"] is False
    assert status["reason"]
    assert status["available"] is False
    with pytest.raises(UpdateRefused):
        await updates.apply(RelaunchTarget(project=Path.cwd()))


# ------------------------------------------------------------------- refusing


@requires_git
async def test_nothing_to_update_to_is_a_refusal(
    service: tuple[UpdateService, Restart], studio: Path
) -> None:
    updates, _ = service
    with pytest.raises(UpdateRefused, match="already on the newest"):
        await updates.apply(target_for(studio))


@requires_git
async def test_uncommitted_changes_stop_an_update_until_forced(
    service: tuple[UpdateService, Restart], studio: Path, upstream: Path
) -> None:
    updates, restart = service
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")
    await updates.check()
    (studio / "README.md").write_text("edited by hand", encoding="utf-8")
    await updates.check()

    with pytest.raises(UpdateRefused, match="uncommitted changes"):
        await updates.apply(target_for(studio))

    # Forced, git still gets the last word — and here it has none, since the local edit
    # does not collide with what is being pulled.
    await run_update(updates, studio, force=True)
    assert restart.stopped is True


@requires_git
async def test_running_work_stops_an_update_until_forced(
    studio: Path, upstream: Path
) -> None:
    restart = Restart()
    updates = build(studio, restart)
    updates._busy = lambda: running_work(["agent_task"])

    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")
    await updates.check()

    with pytest.raises(UpdateRefused, match="1 agent session still running"):
        await updates.apply(target_for(studio))

    await run_update(updates, studio, force=True)
    assert restart.stopped is True


# ------------------------------------------------------------------- updating


@requires_git
async def test_an_update_pulls_and_then_restarts(
    service: tuple[UpdateService, Restart], studio: Path, upstream: Path
) -> None:
    updates, restart = service
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")
    await updates.check()

    await run_update(updates, studio)

    assert (studio / "feature.txt").read_text(encoding="utf-8") == "second"
    assert git(studio, "rev-parse", "HEAD") == git(upstream, "rev-parse", "HEAD")
    assert updates.to_json()["state"] == UpdateState.RESTARTING
    assert restart.stopped is True

    payload = restart.payloads[0]
    assert payload.exists()
    assert (studio / ".scratch" / "update" / "update.log").exists()


@requires_git
async def test_a_pull_that_cannot_fast_forward_fails_the_update_and_says_so(
    service: tuple[UpdateService, Restart], studio: Path, upstream: Path
) -> None:
    """git has the last word even under `force`, and its own message is what is shown."""
    updates, restart = service
    commit(upstream, "README.md", "theirs")
    git(upstream, "tag", "v1.1")
    commit(studio, "README.md", "mine")
    await updates.check()

    await run_update(updates, studio, force=True)

    status = updates.to_json()
    assert status["state"] == UpdateState.FAILED
    assert "git pull failed" in status["error"]
    assert restart.stopped is False
    assert (studio / "README.md").read_text(encoding="utf-8") == "mine"


@requires_git
async def test_the_transcript_is_published_as_it_happens(
    service: tuple[UpdateService, Restart], studio: Path, upstream: Path
) -> None:
    updates, _ = service
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")
    await updates.check()

    subscription = updates.bus.subscribe()
    await run_update(updates, studio)

    seen = []
    while True:
        try:
            seen.append(subscription._queue.get_nowait())
        except Exception:
            break
    types = [event.type for event in seen]
    assert "update.log" in types
    assert "update.status" in types
    assert any("git pull" in event.data.get("line", "") for event in seen)


@requires_git
async def test_a_release_the_running_build_already_has_costs_no_rebuild_and_no_restart(
    studio: Path, upstream: Path
) -> None:
    """Nothing to bring **and** nothing to become: restarting would change nothing.

    The distinction against the test above is the whole fix. A release sitting in the
    checkout is not a release the Studio is running — only a process started from that
    commit is, and only then is there nothing to do.
    """
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")
    git(studio, "pull", "--ff-only", "--tags", "--quiet")

    restart = Restart()
    updates = build(studio, restart)  # started *after* the pull, so it runs v1.1
    await updates.check()

    with pytest.raises(UpdateRefused, match="already on the newest"):
        await updates.apply(target_for(studio))
    assert restart.stopped is False
    assert updates.to_json()["current"] == "v1.1"


@requires_git
async def test_a_second_update_is_refused_while_one_is_running(
    service: tuple[UpdateService, Restart], studio: Path, upstream: Path
) -> None:
    updates, _ = service
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")
    await updates.check()

    await updates.apply(target_for(studio))
    with pytest.raises(UpdateRefused, match="already running"):
        await updates.apply(target_for(studio))
    assert updates._apply_task is not None
    await updates._apply_task


# ------------------------------------------------------------------- coming back up


@requires_git
def test_the_relaunch_command_prefers_the_projects_own_launcher(studio: Path) -> None:
    """`run.ps1` owns the fixed ports and the pid files; a second route would strand them."""
    updates = UpdateService(bus=EventBus(), config=UpdatesConfig(), root=studio)
    (studio / "run.ps1").write_text("# launcher", encoding="utf-8")
    target = RelaunchTarget(project=studio / "game", port=8799, dev=True)

    command, wait = updates._relaunch_command(target)
    if command[0] == "powershell":  # only on Windows, where run.ps1 can run at all
        assert wait is True
        assert str(studio / "run.ps1") in command
        assert command[command.index("-Port") + 1] == "8799"
        assert "-Dev" in command
        assert "-NoOpen" in command


@requires_git
def test_without_a_launcher_it_starts_the_server_itself(studio: Path) -> None:
    updates = UpdateService(bus=EventBus(), config=UpdatesConfig(), root=studio)
    command, wait = updates._relaunch_command(RelaunchTarget(project=studio / "game", port=8799))

    assert wait is False, "a bare `voxarium_studio run` never returns, so nothing may wait on it"
    assert command[1:4] == ["-m", "voxarium_studio.cli", "run"]
    assert command[command.index("--port") + 1] == "8799"
    assert "--no-open" in command


@requires_git
async def test_the_relaunch_payload_says_where_to_come_back_up(
    service: tuple[UpdateService, Restart], studio: Path, upstream: Path
) -> None:
    import json

    updates, restart = service
    commit(upstream, "feature.txt", "second")
    git(upstream, "tag", "v1.1")
    await updates.check()
    await run_update(updates, studio)

    payload = json.loads(restart.payloads[0].read_text(encoding="utf-8"))
    assert payload["port"] == 8765
    assert payload["command"]
    assert payload["cwd"] == str(studio)
    assert payload["log"].endswith("relaunch.log")


def test_the_relauncher_leaves_the_process_tree_before_it_does_anything() -> None:
    """The first hop's whole job is to spawn a detached copy and exit (§12.1)."""
    import json

    from voxarium_studio.updates import relauncher

    spawned: list[list[str]] = []

    def record(command: list[str], **_kwargs: object) -> None:
        spawned.append(command)

    relauncher._spawn = record  # type: ignore[assignment]

    payload = Path(__file__).parent / "_relaunch.json"
    payload.write_text(json.dumps({"command": ["echo", "hi"]}), encoding="utf-8")
    try:
        assert relauncher.main([str(payload)]) == 0
    finally:
        payload.unlink()

    assert len(spawned) == 1
    assert spawned[0][-1] == relauncher.DETACH_FLAG
    assert "echo" not in spawned[0], "the launch itself belongs to the second hop"


# ------------------------------------------------------------------- over HTTP


def test_the_endpoint_reports_what_the_studio_knows(client: TestClient) -> None:
    response = client.get("/api/updates")
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"supported", "state", "available", "current", "latest", "log"}
    assert body["state"] == "idle"


def test_applying_when_there_is_nothing_to_apply_is_a_409(client: TestClient) -> None:
    response = client.post("/api/updates/apply", json={"force": False})
    assert response.status_code == 409
    assert response.json()["detail"]


def test_the_update_settings_merge_like_every_other_section(client: TestClient) -> None:
    """One field of a patch must not reset the rest of its section (see backend-python.md)."""
    client.put("/api/settings", json={"updates": {"check_interval_minutes": 720}})
    config = client.put("/api/settings", json={"updates": {"enabled": False}}).json()

    assert config["updates"]["enabled"] is False
    assert config["updates"]["check_interval_minutes"] == 720
    assert client.get("/api/settings").json()["updates"]["check_interval_minutes"] == 720


def test_the_check_interval_has_a_floor(client: TestClient) -> None:
    """A shorter interval cannot learn anything — a release is tagged a few times a day."""
    response = client.put("/api/settings", json={"updates": {"check_interval_minutes": 1}})
    assert response.status_code == 422
