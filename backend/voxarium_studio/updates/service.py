"""Checking the Studio's own repository for a newer release, and installing it (§12.1).

The Studio is run from a checkout of this repository, so "update" means what a developer
would type by hand: fetch, see whether a newer **tag** exists on the branch this checkout
tracks, pull it, rebuild what the pull changed, and start again. This module is that
sequence, with the two properties a button needs and a shell session does not — it says
what it is doing while it does it, and it refuses when the tree is not in a state where
pulling is safe.

Three deliberate limits:

* **Only tagged releases count.** ``origin/main`` moving is not an update; a new tag on it
  is (:mod:`voxarium_studio.updates.versions`). That is what makes "Update → v1.1" a promise about
  what you get rather than about when you asked.
* **"Where we are" is the running build, not the working tree** (:attr:`UpdateService
  .running_commit`). The two part company the moment somebody pulls in a terminal without
  restarting, and only one of them is the code answering the request.
* **Only a checkout can update itself.** A wheel has no git to ask and no source to
  rebuild, so the feature reports itself unsupported there instead of half-working.
* **Refuse rather than repair.** Uncommitted changes, a pipeline mid-run, an agent session
  in flight — each is a reason to stop and say so. ``force`` is the caller's way to
  overrule that, and it never bypasses git's own refusals.

The restart itself is :mod:`voxarium_studio.updates.relauncher`; see there for why it takes two
hops to survive being restarted by the process it is restarting.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..build_info import build_info, studio_repo_root
from ..core.config import UpdatesConfig
from ..models.base import utcnow

# `run_tool` is the codebase's one careful answer to "run a command line, stream what it
# says, and kill it if this task is cancelled". It was written for version control tools
# and every command here is one of git, uv or npm — reimplementing the cancellation and
# timeout handling beside it would only give the two copies a chance to disagree.
from ..vcs.base import ToolResult, VcsError, run_tool
from ..ws.bus import EventBus
from ..ws.events import EventType
from .versions import Release, is_newer, latest_release

log = logging.getLogger(__name__)

GIT_TIMEOUT = 60.0
FETCH_TIMEOUT = 180.0
"""Seconds. A fetch goes to the network, and an offline machine must fail rather than hang."""

BUILD_TIMEOUT = 1800.0
"""Seconds for `npm ci` / `npm run build` / a dependency sync — minutes on a cold cache."""

FIRST_CHECK_DELAY = 15.0
"""Seconds before the first check after startup. The first seconds belong to the project
being loaded and to the first paint, not to a network round trip nobody asked for."""

LOG_LINES = 400
"""How much of the transcript is kept in memory for a client that connects mid-update.
The full text is on disk in ``.scratch/update/update.log``."""

RELAUNCH_DELAY = 1.0
"""Seconds the relauncher waits before it starts watching the port, so that this process
has begun shutting down and cannot be mistaken for the server that already went away."""


class UpdateState(StrEnum):
    IDLE = "idle"
    CHECKING = "checking"
    UPDATING = "updating"
    RESTARTING = "restarting"
    """The rebuild is done and the relauncher is spawned — this process is on its way out.
    Clients hold this state until ``/healthz`` answers again, then reload."""

    FAILED = "failed"


class UpdateError(RuntimeError):
    """A step of the update did not work. Carries what to show the user."""


class UpdateRefused(UpdateError):
    """The update was not attempted — the tree, or the Studio, is not in a state for it."""


@dataclass(frozen=True, slots=True)
class RelaunchTarget:
    """Everything the new process needs to come back up where this one was.

    Taken from the running server rather than from configuration: the address is the one
    uvicorn actually bound and the project is the one currently open, so a restart cannot
    silently move the Studio to another port or another project.
    """

    project: Path
    host: str = "127.0.0.1"
    port: int = 8765
    dev: bool = False
    """Started with ``--dev``, which also means Vite is serving the SPA on :5173."""

    token: str | None = None
    """The shared secret the API requires, when one is configured (§12). The new process
    has to answer with the same one or every open tab is locked out of a Studio it can
    see running."""


def running_work(kinds: Sequence[str]) -> str | None:
    """Describe what is still in flight, for the refusal message. ``None`` when nothing is.

    A restart kills pipeline runs and agent sessions outright, and an agent session is
    billed — so this is the difference between an update and a bill nobody meant to pay.
    """
    runs = sum(1 for kind in kinds if kind == "pipeline_run")
    agents = sum(1 for kind in kinds if kind in ("agent_task", "agent_chat"))
    parts = []
    if runs:
        parts.append(f"{runs} pipeline run{'s' if runs > 1 else ''}")
    if agents:
        parts.append(f"{agents} agent session{'s' if agents > 1 else ''}")
    if not parts:
        return None
    return " and ".join(parts) + " still running"


class UpdateService:
    """Owns the answer to "is there a newer version", and the act of installing it.

    One per :class:`~voxarium_studio.core.context.AppContext`: the poll interval is a project
    setting, and opening another project builds a new context. The state it holds is a
    cache of what git said, so losing it on a project switch costs one fetch.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        config: UpdatesConfig,
        root: Path | None = None,
        busy: Callable[[], str | None] | None = None,
        running_commit: str | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self.bus = bus
        self.config = config
        self.root = root if root is not None else studio_repo_root()
        self._busy = busy or (lambda: None)
        self._work_directory = work_dir

        self.running_commit = (
            running_commit if running_commit is not None else build_info().commit
        )
        """The commit this **process** was imported from, which is what "the version we are
        on" has to mean.

        Deliberately not ``HEAD``. The two are the same only until somebody moves the
        checkout without restarting — a `git pull` in a terminal, a branch switch — and from
        then on ``HEAD`` describes code that is on disk and not running. Anchoring to it made
        a Studio running v1.0 beside a checkout at v1.2 report itself up to date, which is
        the one answer that is both wrong and reassuring. Resolved once, because the running
        code cannot change under this process.
        """

        self._state = UpdateState.IDLE
        self._current: Release | None = None
        self._latest: Release | None = None
        self._checked_at: datetime | None = None
        self._branch: str | None = None
        self._upstream: str | None = None
        self._remote: str | None = None
        self._dirty = False
        self._error: str | None = None
        self._step: str | None = None
        self._log: deque[str] = deque(maxlen=LOG_LINES)

        self._lock = asyncio.Lock()
        self._poller: asyncio.Task[None] | None = None
        self._apply_task: asyncio.Task[None] | None = None

    # ---- what the Studio can say about itself ---------------------------------------

    @property
    def supported(self) -> bool:
        return self.root is not None and _have_git()

    @property
    def reason(self) -> str | None:
        """Why updating is unavailable, phrased for the person reading it."""
        if self.root is None:
            source = build_info().source
            if source == "stamp":
                return (
                    "This Studio was installed as a package, so it has no repository to "
                    "pull from — update it with pip or uv."
                )
            return "The Studio is not running from a checkout of its own repository."
        if not _have_git():
            return "git is not on PATH, so the Studio cannot check for updates."
        return None

    @property
    def available(self) -> bool:
        """A newer release exists on the branch this checkout tracks."""
        return self._latest is not None and is_newer(self._latest, self._current)

    def to_json(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "reason": self.reason,
            "state": str(self._state),
            "enabled": self.config.enabled,
            "interval_minutes": self.config.check_interval_minutes,
            # The release the **running build** contains — see `running_commit`, and note
            # that this is not a tag *on* that commit, which is what `/api/project`'s
            # `version.tag` reports: a build three commits past v1.0 is on v1.0 as far as
            # updating is concerned.
            "current": self._current.tag if self._current else None,
            "latest": self._latest.tag if self._latest else None,
            "available": self.available,
            "checked_at": self._checked_at.isoformat() if self._checked_at else None,
            "branch": self._branch,
            "upstream": self._upstream,
            "dirty": self._dirty,
            "step": self._step,
            "error": self._error,
            "log": list(self._log),
        }

    # ---- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        """Begin checking periodically, if this Studio can update itself at all."""
        if not self.supported:
            log.debug("Updates unavailable: %s", self.reason)
            return
        if not self.config.enabled:
            return
        if self._poller is not None and not self._poller.done():
            return
        self._poller = asyncio.create_task(self._poll(), name="updates:poll")

    async def configure(self, config: UpdatesConfig) -> None:
        """Adopt a changed setting.

        A new interval is picked up by the poller's next iteration rather than cutting the
        current sleep short: the loop re-reads the config every round, and waking it early
        would spend a fetch to save at most one period.
        """
        was_enabled = self.config.enabled
        self.config = config
        if not config.enabled:
            await self._stop_poller()
        elif not was_enabled:
            await self.start()

    async def stop(self) -> None:
        await self._stop_poller()
        await _cancel(self._apply_task)

    async def _stop_poller(self) -> None:
        await _cancel(self._poller)
        self._poller = None

    async def _poll(self) -> None:
        await asyncio.sleep(FIRST_CHECK_DELAY)
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # a background check must never take the server with it
                log.warning("Update check failed", exc_info=True)
            await asyncio.sleep(max(60, self.config.check_interval_minutes * 60))

    # ---- checking ---------------------------------------------------------------------

    async def check(self) -> dict[str, Any]:
        """Fetch and recompute what the newest available release is.

        A failure here is recorded, not raised: being offline is the ordinary reason, and
        the last known answer is still the best one to show.
        """
        if not self.supported or self._state in (UpdateState.UPDATING, UpdateState.RESTARTING):
            return self.to_json()

        async with self._lock:
            await self._set_state(UpdateState.CHECKING)
            try:
                await self._probe()
            except (VcsError, UpdateError, OSError) as exc:
                self._error = str(exc)
                log.warning("Could not check for updates: %s", exc)
            self._checked_at = utcnow()
            self._state = UpdateState.IDLE

        await self._publish(EventType.UPDATE_STATUS)
        return self.to_json()

    async def _probe(self) -> None:
        """Ask git the four questions the button is made of."""
        assert self.root is not None
        self._error = None

        branch = _line(await self._git("rev-parse", "--abbrev-ref", "HEAD"))
        # "HEAD" is what a detached checkout answers, and that is not a branch to track.
        self._branch = None if branch in (None, "HEAD") else branch
        self._upstream = _line(
            await self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        )
        self._remote = None
        if self._branch:
            self._remote = _line(
                await self._git("config", "--get", f"branch.{self._branch}.remote")
            )
        if self._remote is None:
            self._remote = _line(await self._git("remote"))

        if self._remote:
            fetched = await self._git(
                "fetch", "--tags", "--prune", self._remote, timeout=FETCH_TIMEOUT
            )
            if not fetched.ok:
                # Offline, or no credentials. The local tags still answer the question, just
                # as of the last successful fetch — so this is reported beside an answer.
                self._error = f"Could not reach {self._remote}: {fetched.output.strip()}"

        # What a `git pull` could actually bring in. Every tag would include releases on
        # branches this checkout does not track, and offering one of those would produce a
        # button that cannot keep its promise.
        candidates = await self._tags_in(self._upstream) if self._upstream else None
        if candidates is None:
            candidates = (await self._git("tag", "--list")).lines()
        self._latest = latest_release(candidates)
        self._current = latest_release(await self._tags_running())
        self._dirty = bool((await self._git("status", "--porcelain")).stdout.strip())

    async def _tags_running(self) -> list[str]:
        """Release tags contained in the commit this process is running.

        The fallback to ``HEAD`` is for a build commit git cannot resolve — a checkout
        rebased or force-pushed out from under a long-running process, and the wheel case
        where there is no commit at all. It deliberately does **not** fall through to every
        tag, the way the candidate side does: a list containing releases this build does not
        have would put ``current`` at the newest of them and report the Studio up to date,
        which is the failure this whole method exists to avoid.
        """
        if self.running_commit:
            tags = await self._tags_in(self.running_commit)
            if tags is not None:
                return tags
        return await self._tags_in("HEAD") or []

    async def _tags_in(self, committish: str) -> list[str] | None:
        """Tags contained in ``committish``; ``None`` when git cannot resolve the ref."""
        merged = await self._git("tag", "--merged", committish)
        return merged.lines() if merged.ok else None

    # ---- updating ----------------------------------------------------------------------

    async def apply(self, target: RelaunchTarget, *, force: bool = False) -> dict[str, Any]:
        """Start the update. Returns as soon as it is running; progress arrives as events."""
        if not self.supported:
            raise UpdateRefused(self.reason or "This Studio cannot update itself.")
        if self._state in (UpdateState.UPDATING, UpdateState.RESTARTING):
            raise UpdateRefused("An update is already running.")
        if self._checked_at is None:
            await self.check()
        if not self.available:
            raise UpdateRefused("The Studio is already on the newest release.")
        if not self._upstream:
            raise UpdateRefused(
                f"The branch {self._branch or 'HEAD'} tracks no remote branch, so there is "
                "nothing to pull. Set an upstream and try again."
            )
        if not force:
            if self._dirty:
                raise UpdateRefused(
                    "The Studio's checkout has uncommitted changes. Commit or stash them "
                    "first — updating anyway would ask git to pull over your work."
                )
            busy = self._busy()
            if busy:
                raise UpdateRefused(
                    f"{busy} — a restart would kill them. Wait for them to finish, or "
                    "update anyway."
                )

        assert self._latest is not None
        self._log.clear()
        self._error = None
        self._step = None
        await self._set_state(UpdateState.UPDATING)
        self._apply_task = asyncio.create_task(
            self._run_update(self._latest, target), name="updates:apply"
        )
        return self.to_json()

    async def _run_update(self, target_release: Release, target: RelaunchTarget) -> None:
        assert self.root is not None
        try:
            await self._say(f"Updating to {target_release.tag}.")
            # Everything below is measured from the commit that is **running**, not from the
            # one on disk. When the checkout was already moved by hand the pull brings
            # nothing and there is still a rebuild and a restart to do — that gap is exactly
            # what this update closes.
            before = self.running_commit or _line(await self._git("rev-parse", "HEAD"))

            await self._pull()

            after = _line(await self._git("rev-parse", "HEAD"))
            if before and after and before == after:
                # This process is already running that commit, so a rebuild and a restart
                # would cost a minute to change nothing.
                await self._say("Already up to date — nothing to rebuild.")
                await self._finish_without_restart()
                return

            changed = await self._changed_files(before, after)
            await self._say(f"{len(changed)} file(s) changed.")

            if any(path.startswith("backend/pyproject.toml") for path in changed):
                await self._sync_python_dependencies()
            if self._needs_frontend_build(changed):
                await self._build_frontend(changed)

            await self._restart(target)
        except asyncio.CancelledError:
            await self._fail("The update was interrupted — the checkout may be half-updated.")
            raise
        except (VcsError, UpdateError, OSError) as exc:
            await self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - the button must always end in a message
            log.exception("Unexpected failure while updating")
            await self._fail(f"Unexpected failure: {exc}")

    async def _pull(self) -> None:
        await self._begin_step("git pull")
        result = await self._git(
            "pull", "--ff-only", "--tags", timeout=FETCH_TIMEOUT, stream=True
        )
        if not result.ok:
            raise UpdateError(
                "git pull failed, so nothing was changed:\n"
                + (result.output.strip() or "(no output)")
            )

    async def _changed_files(self, before: str | None, after: str | None) -> list[str]:
        """Paths that differ between the running build and what it is becoming.

        Not "what the pull touched": when the checkout was already ahead, the pull touched
        nothing and this is still the list that decides whether the SPA gets rebuilt.
        """
        if not before or not after:
            return []
        result = await self._git("diff", "--name-only", before, after)
        return result.lines() if result.ok else []

    def _needs_frontend_build(self, changed: Sequence[str]) -> bool:
        """Rebuild when the SPA's sources moved, or when there is no built SPA at all.

        `npm run build` writes into the Python package (``backend/voxarium_studio/static``), which
        is generated and gitignored — so a fresh clone has none and every checkout has one
        that only these files can invalidate.
        """
        assert self.root is not None
        if any(path.startswith("frontend/") for path in changed):
            return True
        return not (self.root / "backend" / "voxarium_studio" / "static" / "index.html").exists()

    async def _sync_python_dependencies(self) -> None:
        """Reinstall the backend, because the pull changed what it depends on.

        The extras are the ones this environment already has rather than a fixed list: a
        Studio installed without ``teams`` must not gain the Agent SDK from an update, and
        one installed with it must not silently lose it.
        """
        assert self.root is not None
        extras = _installed_extras()
        spec = f".[{','.join(extras)}]" if extras else "."
        await self._begin_step(f"uv pip install -e {spec}")
        result = await self._run(
            sys.executable,
            ["-m", "uv", "pip", "install", "-e", spec, "--python", sys.executable],
            cwd=self.root / "backend",
            timeout=BUILD_TIMEOUT,
        )
        if not result.ok:
            raise UpdateError(
                "The backend's dependencies could not be installed:\n"
                + (result.output.strip() or "(no output)")
            )

    async def _build_frontend(self, changed: Sequence[str]) -> None:
        assert self.root is not None
        frontend = self.root / "frontend"
        npm = shutil.which("npm")
        if npm is None:
            raise UpdateError(
                "npm is not on PATH, so the interface could not be rebuilt. The code was "
                "updated; install Node and run `npm run build` in frontend/."
            )

        if any(path.startswith("frontend/package") for path in changed):
            lockfile = (frontend / "package-lock.json").exists()
            await self._begin_step("npm ci" if lockfile else "npm install")
            result = await self._run(
                npm, ["ci"] if lockfile else ["install"], cwd=frontend, timeout=BUILD_TIMEOUT
            )
            if not result.ok:
                raise UpdateError(
                    "Installing the interface's dependencies failed:\n"
                    + (result.output.strip() or "(no output)")
                )

        await self._begin_step("npm run build")
        result = await self._run(npm, ["run", "build"], cwd=frontend, timeout=BUILD_TIMEOUT)
        if not result.ok:
            raise UpdateError(
                "Building the interface failed, so the code is updated but the pages are "
                "not:\n" + (result.output.strip() or "(no output)")
            )

    # ---- the restart ---------------------------------------------------------------------

    async def restart_current_files(self, target: RelaunchTarget) -> None:
        """Apply already-installed code after a verified version mismatch (§2.5).

        This path deliberately skips git, builds and the update button's busy refusal:
        automatic replacement of the old process is the mismatch recovery contract.
        """
        async with self._lock:
            await self._restart(target)

    async def _restart(self, target: RelaunchTarget) -> None:
        assert self.root is not None
        await self._begin_step("restart")
        command, wait = self._relaunch_command(target)
        payload = {
            "command": command,
            "cwd": str(self.root),
            "wait": wait,
            "host": target.host,
            "port": target.port,
            "delay": RELAUNCH_DELAY,
            "log": str(self._work_dir() / "relaunch.log"),
        }
        payload_file = self._work_dir() / "relaunch.json"
        payload_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        await self._say(f"Restarting: {' '.join(command)}")
        # The token travels in the environment, not on the command line or in the payload
        # file: `voxarium_studio run` falls back to `$VOXARIUM_STUDIO_TOKEN`, and `run.ps1` passes no token
        # of its own, so this is the one channel that reaches the new process either way.
        self.spawn_relauncher(
            payload_file, env={"VOXARIUM_STUDIO_TOKEN": target.token} if target.token else None
        )
        await self._set_state(UpdateState.RESTARTING)
        # Everything after this point happens in the *next* process, so the transcript on
        # disk is the only record of it — see .scratch/update/relaunch.log.
        self.request_shutdown()

    def _relaunch_command(self, target: RelaunchTarget) -> tuple[list[str], bool]:
        """How to start the Studio again, and whether that command comes back.

        ``run.ps1`` is preferred wherever it exists because it is what starts this project
        (it owns the fixed ports, clears orphans and waits for ``/healthz``), and a restart
        that used a different route would leave its pid files pointing at a dead process.
        """
        assert self.root is not None
        launcher = self.root / "run.ps1"
        if os.name == "nt" and launcher.exists():
            command = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-Project",
                str(target.project),
                "-Port",
                str(target.port),
                "-NoOpen",
            ]
            if target.dev:
                command.append("-Dev")
            return command, True

        command = [
            sys.executable,
            "-m",
            "voxarium_studio.cli",
            "run",
            str(target.project),
            "--host",
            target.host,
            "--port",
            str(target.port),
            "--no-open",
        ]
        if target.dev:
            command.append("--dev")
        return command, False

    def spawn_relauncher(self, payload_file: Path, env: dict[str, str] | None = None) -> None:
        """Start the process that will bring the Studio back up. Overridden in tests.

        Spawned as an ordinary child — it is the relauncher's own job to leave this
        process's tree, and it cannot do that from here (see :mod:`.relauncher`).
        """
        assert self.root is not None
        script = Path(__file__).resolve().parent / "relauncher.py"
        subprocess.Popen(  # noqa: S603 - a fixed command, built here
            [sys.executable, str(script), str(payload_file)],
            cwd=str(self.root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **env} if env else None,
            close_fds=True,
        )

    def request_shutdown(self) -> None:
        """Stop this server the way Ctrl+C would — uvicorn then unwinds the lifespan.

        Deliberately graceful: the managed project's database, the file watcher and any
        agent subprocess all get their shutdown, and the port is released, which is the
        signal the relauncher is waiting for. Overridden in tests.
        """
        try:
            signal.raise_signal(signal.SIGINT)
        except (OSError, ValueError):  # pragma: no cover - no signal handling here
            log.error("Could not signal this process to stop; the restart needs a manual one.")

    # ---- running commands ----------------------------------------------------------------

    async def _git(
        self, *args: str, timeout: float = GIT_TIMEOUT, stream: bool = False
    ) -> ToolResult:
        assert self.root is not None
        return await self._run("git", list(args), cwd=self.root, timeout=timeout, stream=stream)

    async def _run(
        self,
        program: str,
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
        stream: bool = True,
    ) -> ToolResult:
        try:
            return await run_tool(
                program, args, cwd=cwd, timeout=timeout, log_fn=self._say if stream else None
            )
        except VcsError as exc:
            raise UpdateError(str(exc)) from exc

    # ---- state and events -------------------------------------------------------------

    async def _set_state(self, state: UpdateState) -> None:
        self._state = state
        await self._publish(EventType.UPDATE_STATUS)

    async def _begin_step(self, step: str) -> None:
        self._step = step
        await self._say(f"--- {step}")
        await self._publish(EventType.UPDATE_STATUS)

    async def _finish_without_restart(self) -> None:
        self._step = None
        self._state = UpdateState.IDLE
        await self._probe()
        self._checked_at = utcnow()
        await self._publish(EventType.UPDATE_STATUS)

    async def _fail(self, message: str) -> None:
        self._error = message
        self._state = UpdateState.FAILED
        await self._say(message)
        await self._publish(EventType.UPDATE_STATUS)

    async def _say(self, line: str) -> None:
        """One line of the transcript: kept in memory, appended to disk, sent to clients."""
        self._log.append(line)
        self._append_to_file(line)
        await self.bus.publish(EventType.UPDATE_LOG, {"line": line, "step": self._step})

    async def _publish(self, event: EventType) -> None:
        await self.bus.publish(event, self.to_json())

    def _work_dir(self) -> Path:
        """Where the update writes what outlives it — the same scratch space `run.ps1` uses."""
        assert self.root is not None
        directory = self._work_directory or self.root / ".scratch" / "update"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _append_to_file(self, line: str) -> None:
        if self.root is None:
            return
        try:
            with (self._work_dir() / "update.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{utcnow().isoformat()}  {line}\n")
        except OSError:
            # The transcript on screen is the one that matters; a scratch directory that
            # cannot be written to is not a reason to fail an update.
            pass


@lru_cache(maxsize=1)
def _have_git() -> bool:
    """Cached: `supported` is read on every request and on every published status, and a
    PATH scan per read is a lot of syscalls for an answer that changes when git is
    installed — which does not happen while the Studio is running."""
    return shutil.which("git") is not None


async def _cancel(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _line(result: ToolResult) -> str | None:
    """First line of a command's output, or ``None`` when it failed or said nothing."""
    if not result.ok:
        return None
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else None


def _installed_extras() -> list[str]:
    """Which optional dependency groups this environment has, by looking for them.

    Reading the installed distribution's metadata would say which extras were *requested*,
    which is not the same thing after somebody adds a package by hand — and the question
    here is what to keep working, not what was once typed.
    """
    probes = {"teams": "claude_agent_sdk", "dev": "pytest"}
    found = []
    for extra, module in probes.items():
        try:
            if importlib.util.find_spec(module) is not None:
                found.append(extra)
        except (ImportError, ValueError):
            continue
    return found
