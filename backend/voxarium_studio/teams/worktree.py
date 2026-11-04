"""Isolation for parallel agent work (§10.5).

Two members editing the same files at once corrupt each other's work, so an active task
gets its own checkout when the project's version control system can provide one — a git
worktree, or whatever the detected :class:`~voxarium_studio.vcs.base.VcsBackend` offers — and
merging back happens through the review step. Where it cannot (no version control at all,
or a system whose second checkout means a server round trip), we fall back to serialising
tasks whose briefs mention overlapping paths.

The module is still called ``worktree`` because ``TeamTask.worktree_path`` and the
``DELETE …/worktree`` endpoint are, and those are a stored column and a published API.
Nothing in here is git-specific any more.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from ..vcs import IsolatedWorkspace, VcsBackend, VcsError

log = logging.getLogger(__name__)

_PATH_TOKEN = re.compile(
    r'''`([^`\r\n]+)`|"([^"\r\n]+)"|'([^'\r\n]+)'|((?:[A-Za-z]:[/\\])?[\w./\\-]+)'''
)
_FILE_SUFFIX = re.compile(r"\.[A-Za-z][A-Za-z0-9_-]{0,15}$")
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s`\"'<>]+")


def branch_name(task_id: str, member_id: str) -> str:
    return f"voxarium_studio/{member_id}/{task_id}"


async def create_isolated_workspace(
    vcs: VcsBackend | None, worktrees_dir: Path, task_id: str, member_id: str
) -> IsolatedWorkspace | None:
    """Give one task its own checkout under ``voxarium_studio/worktrees/<task_id>``.

    ``None`` whenever isolation is unavailable, for any reason — the caller treats that
    as "fall back to path locks", never as an error worth failing the task over.
    """
    if vcs is None or not vcs.supports_isolation:
        return None
    try:
        return await vcs.create_workspace(worktrees_dir / task_id, branch_name(task_id, member_id))
    except VcsError as exc:
        log.warning("Could not isolate task %s with %s: %s", task_id, vcs.id, exc)
        return None


async def remove_isolated_workspace(
    vcs: VcsBackend | None, path: Path, *, force: bool = False
) -> None:
    if vcs is None or not path.exists():
        return
    try:
        await vcs.remove_workspace(path, force=force)
    except VcsError as exc:
        log.warning("Could not remove isolated workspace %s: %s", path, exc)


async def workspace_diff(vcs: VcsBackend | None, path: Path) -> str:
    """Uncommitted changes in an isolated workspace, for the reviewer to look at."""
    if vcs is None:
        return ""
    try:
        return await vcs.diff(path)
    except VcsError as exc:
        log.warning("Could not diff %s: %s", path, exc)
        return ""


class PathLockManager:
    """Serialises tasks whose briefs reference overlapping paths.

    Used when checkout isolation is unavailable. The heuristic is deliberately crude —
    it only has to stop two agents from editing the same obviously-named file at once.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._owners: dict[asyncio.Lock, str | None] = {}

    @staticmethod
    def extract_paths(text: str, roots: Sequence[Path] = ()) -> set[str]:
        """Recognise filenames, explicit paths and existing extensionless paths (§10.5).

        A slash alone is not evidence of a path: task references and prose such as
        ``BT-561/563`` or ``spawn/destroy`` otherwise serialise unrelated tasks.
        """
        paths: set[str] = set()
        for match in _PATH_TOKEN.finditer(_URL.sub("", text)):
            token = next(value for value in match.groups() if value is not None)
            token = token.strip().strip("*").rstrip(".,:!?").replace("\\", "/")
            token = re.sub(r":\d+(?::\d+)?$", "", token.partition("#")[0])
            if not token or "\x00" in token:
                continue
            filename = token.rstrip("/").rsplit("/", 1)[-1]
            explicit = token.startswith(("./", "../", "/")) or bool(
                re.match(r"^[A-Za-z]:/", token)
            ) or token.endswith("/")
            existing = False
            if not explicit and not _FILE_SUFFIX.search(filename):
                for root in roots:
                    try:
                        if (root / token).exists():
                            existing = True
                            break
                    except OSError:
                        continue
            if explicit or _FILE_SUFFIX.search(filename) or existing:
                key = posixpath.normpath(token).lower()
                if key not in {".", "..", "/"}:
                    paths.add(key)
        return paths

    async def acquire(
        self,
        text: str,
        *,
        roots: Sequence[Path] = (),
        owner: str | None = None,
        on_wait: Callable[[str, str | None], Awaitable[None]] | None = None,
    ) -> list[asyncio.Lock]:
        """Acquire locks for every referenced path, in a stable order to avoid deadlock."""
        keys = sorted(self.extract_paths(text, roots))[:20]
        acquired: list[asyncio.Lock] = []
        try:
            for key in keys:
                lock = self._locks[key]
                if lock.locked() and on_wait is not None:
                    await on_wait(key, self._owners.get(lock))
                await lock.acquire()
                acquired.append(lock)
                self._owners[lock] = owner
        except BaseException:
            # Cancellation while queued must release earlier acquisitions too (§10.5).
            self.release(acquired)
            raise
        return acquired

    def release(self, locks: list[asyncio.Lock]) -> None:
        for lock in reversed(locks):
            if lock.locked():
                self._owners.pop(lock, None)
                lock.release()
