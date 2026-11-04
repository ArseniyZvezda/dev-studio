"""Opening another managed project without restarting the server (§5).

The Studio is pointed at one managed project at a time, and everything it knows about
that project hangs off a single :class:`~voxarium_studio.core.context.AppContext`. Switching
projects selects another context while keeping previous contexts running (§5.1). Returning
to a project reuses its context, including running sessions and their database connections.

This module also holds the recently-opened list. It is the second piece of per-machine
state after the global plugin registry (§9.4), and deliberately the *only* thing about a
project that lives outside the project: it is a list of paths and nothing else, so
deleting the file loses no project data.

That list is what a start with no path on the command line resolves against
(:func:`resolve_startup_project`), which is why it is a feature rather than a convenience:
losing it no longer only costs a menu, it sends the next start to the demo project.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models.base import utcnow
from ..ws.bus import EventBus
from ..ws.events import EventType
from .context import AppContext
from .demo import ensure_demo_project
from .paths import DATA_DIRNAME, ProjectPaths, has_project_data, user_config_dir
from .task_counts import read_task_counts

log = logging.getLogger(__name__)

RECENT_FILENAME = "recent-projects.json"
MAX_RECENT = 12


class ProjectNotFound(ValueError):
    """The path does not exist, or is not a directory."""


class ProjectNotInitialised(ValueError):
    """The directory has no ``voxarium_studio/`` and the caller asked not to create one."""


# ---------------------------------------------------------------- opening a project


def initialise_workspace(state: Any) -> None:
    """Keep project services alive independently of the selected UI project (§5.1)."""
    state.project_contexts = {state.ctx.paths.root: state.ctx}
    state.browser_bus = EventBus()
    state.ctx.bus.forward_to = state.browser_bus
    state.ctx.work_contexts = lambda: list(state.project_contexts.values())


async def shutdown_workspace(state: Any) -> None:
    """Only server shutdown cancels work in every resident project (§5.1)."""
    async with state.project_lock:
        contexts = list(state.project_contexts.values())
        results = await asyncio.gather(*(ctx.shutdown() for ctx in contexts), return_exceptions=True)
        state.project_contexts.clear()
        for result in results:
            if isinstance(result, BaseException):
                raise result


async def open_project(state: Any, root: str | Path, *, create: bool = True) -> AppContext:
    """Point the running Studio at ``root``, initialising it if it is not a project yet.

    ``state`` is the ASGI app's state object — it carries the live context and the lock
    that serialises two clicks on "Open project" arriving at once.

    Two orderings matter here:

    * A new context is **built and started before** the selected one changes.
      A config that will not parse or a plugin that will not import must leave the Studio
      on the project it already had, not on nothing at all.
    * The browser bus stays alive across switches. Each context keeps a separate local
      bus for its plugins and automations, including while its tasks run in the background.
    """
    target = ProjectPaths.for_project(root)
    if not target.root.exists():
        raise ProjectNotFound(f"No such directory: {target.root}")
    if not target.root.is_dir():
        raise ProjectNotFound(f"Not a directory: {target.root}")

    existed = has_project_data(target.root)
    if not existed and not create:
        raise ProjectNotInitialised(str(target.root))

    lock: asyncio.Lock = state.project_lock
    async with lock:
        current: AppContext = state.ctx
        if not hasattr(state, "project_contexts"):
            initialise_workspace(state)
        if target.root == current.paths.root:
            record_recent(current.paths.root, project_label(current))
            return current

        replacement = state.project_contexts.get(target.root)
        if replacement is None:
            replacement = AppContext.create(str(target.root), current.auth)
            replacement.work_contexts = lambda: list(state.project_contexts.values())
            try:
                await replacement.startup()
            except BaseException:
                # Release whatever the half-built context did manage to open.
                await replacement.shutdown()
                raise
            state.project_contexts[target.root] = replacement
        current.bus.forward_to = None
        state.browser_bus.clear_history()
        replacement.bus.forward_to = state.browser_bus
        state.ctx = replacement
        log.info("Opened project %s (was %s)", replacement.paths.root, current.paths.root)
        record_recent(replacement.paths.root, project_label(replacement))
        await state.browser_bus.publish(
            EventType.PROJECT_OPENED,
            {
                "root": str(replacement.paths.root),
                "name": project_label(replacement),
                "data_dir": str(replacement.paths.data),
                "created": not existed,
            },
        )
    return replacement


def project_label(ctx: AppContext) -> str:
    return ctx.config.project_name or ctx.paths.root.name


# ---------------------------------------------------------------- recently opened


@dataclass(frozen=True, slots=True)
class RecentProject:
    root: Path
    name: str
    opened_at: datetime

    def to_json(self) -> dict[str, Any]:
        # `exists` and `initialised` are resolved on read, not stored: a project the user
        # has since moved or deleted should come back as a dead entry they can clear,
        # never as a path the UI happily offers to open.
        return {
            "root": str(self.root),
            "name": self.name,
            "opened_at": self.opened_at.isoformat(),
            "exists": _is_dir(self.root),
            "initialised": has_project_data(self.root),
            "task_counts": read_task_counts(self.root),
        }


def recent_file() -> Path:
    return user_config_dir() / RECENT_FILENAME


def load_recent() -> list[RecentProject]:
    """Most recently opened first. A malformed file is an empty list, never an error —
    this is a convenience list, and losing it must not stop the Studio from starting."""
    path = recent_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        log.warning("Ignoring unreadable recent-projects file at %s", path, exc_info=True)
        return []

    entries: list[RecentProject] = []
    for item in raw.get("projects", []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict) or not item.get("root"):
            continue
        try:
            opened_at = datetime.fromisoformat(str(item.get("opened_at")))
        except ValueError:
            opened_at = utcnow()
        root = Path(str(item["root"]))
        entries.append(
            RecentProject(root=root, name=str(item.get("name") or root.name), opened_at=opened_at)
        )
    return entries[:MAX_RECENT]


def record_recent(root: Path, name: str) -> list[RecentProject]:
    """Move ``root`` to the front of the list, de-duplicating what is already there."""
    entry = RecentProject(root=Path(root), name=name, opened_at=utcnow())
    kept = [item for item in load_recent() if not _same_path(item.root, entry.root)]
    return _write_recent([entry, *kept][:MAX_RECENT])


def forget_recent(root: Path | str) -> list[RecentProject]:
    target = Path(root)
    return _write_recent([item for item in load_recent() if not _same_path(item.root, target)])


def _write_recent(entries: list[RecentProject]) -> list[RecentProject]:
    path = recent_file()
    payload = {
        "projects": [
            {"root": str(e.root), "name": e.name, "opened_at": e.opened_at.isoformat()}
            for e in entries
        ]
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written through a temporary file so an interrupted write cannot leave a
        # truncated list behind — `load_recent` would then silently start from scratch.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            temp_name = handle.name
        os.replace(temp_name, path)
    except OSError:
        log.warning("Could not write the recent-projects list to %s", path, exc_info=True)
    return entries


def _same_path(left: Path, right: Path) -> bool:
    """Windows paths differ only in case; comparing the raw strings would list one
    project twice the first time the user typed the drive letter in lower case."""
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        # A disconnected network share raises rather than answering False.
        return False


# ---------------------------------------------------------------- which project starts


@dataclass(frozen=True, slots=True)
class StartupProject:
    """Where the Studio starts, and how that was decided."""

    root: Path
    source: str
    """``"argument"``, ``"recent"`` or ``"demo"`` — what the CLI reports it did."""


def resolve_startup_project(explicit: str | Path | None = None) -> StartupProject:
    """Which managed project a starting Studio opens (§5.1).

    A path on the command line wins, and is returned unchecked: whether it exists is the
    caller's error to report, not something to quietly paper over with another project.

    With no path, the Studio reopens what it had open last. That is the top of the recent
    list, and entries are skipped until one is still on disk — a project moved or deleted
    since must not hold the Studio hostage on every later start. Nothing left to reopen
    means a first run, and that opens the built-in demo project instead of the working
    directory: a shell that happens to be somewhere is not a statement about which project
    to initialise.
    """
    if explicit is not None:
        return StartupProject(ProjectPaths.for_project(explicit).root, "argument")

    for entry in load_recent():
        if _is_dir(entry.root):
            return StartupProject(ProjectPaths.for_project(entry.root).root, "recent")
        log.info("Skipping recently opened %s: it is not there any more", entry.root)

    return StartupProject(ensure_demo_project().root, "demo")
