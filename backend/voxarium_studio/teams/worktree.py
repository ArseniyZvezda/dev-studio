"""Isolation for parallel agent work (§10.5).

Two members editing the same files at once corrupt each other's work, so an active task
gets its own checkout when the project's version control system can provide one — a git
worktree, or whatever the detected :class:`~voxarium_studio.vcs.base.VcsBackend` offers — and
merging back happens through the review step. Where it cannot (no version control at all,
or a system whose second checkout means a server round trip), we fall back to serialising
tasks whose explicit write-file lists overlap. Read-only references never take locks.

The module is still called ``worktree`` because ``TeamTask.worktree_path`` and the
``DELETE …/worktree`` endpoint are, and those are a stored column and a published API.
Nothing in here is git-specific any more.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..vcs import IsolatedWorkspace, VcsBackend, VcsError

log = logging.getLogger(__name__)

def normalize_write_paths(paths: Sequence[str], *, root: Path | None = None) -> list[str]:
    """Validate exact write-file declarations, never infer them from prose (§10.5).

    Preserve spelling for the editor; identity (absolute/relative aliases and symlinks)
    is resolved against the managed project root when locks are acquired.
    """
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
        raise ValueError("write_paths must be a list of file paths")
    result: list[str] = []
    seen: set[str] = set()
    for value in paths:
        if not isinstance(value, str):
            raise ValueError("write_paths must contain only strings")
        path = value.strip().replace("\\", "/")
        if (not path or any(char in path for char in "\x00\r\n*?[]")
                or "://" in path or path.endswith("/")):
            raise ValueError("write_paths must contain exact file paths, not blanks, URLs or patterns")
        windows = PureWindowsPath(path)
        if windows.drive and not windows.root:
            raise ValueError("write_paths cannot contain drive-relative paths")
        # Preserve '..' until filesystem-aware resolution: link/../file can point
        # outside this directory on POSIX, even when lexical normpath says otherwise.
        path = PurePosixPath(path).as_posix()
        if path in {".", "..", "/"} or path.endswith("/.."):
            raise ValueError("write_paths must name files, not directories")
        if root is not None and (root / path).is_dir():
            raise ValueError(f"write_paths must name files, not directories: {path}")
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def can_isolate_write_paths(paths: Sequence[str], root: Path) -> bool:
    """Only relative files inside the project can follow a checkout (§10.5).

    Absolute paths, parent traversal and links need their original filesystem context;
    moving them to a checkout can change their target or still reach the shared copy.
    Such a run stays in the shared copy and coordinates all declared writes.
    """
    root = root.resolve()
    for path in paths:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        candidate = root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink() or candidate.is_junction():
                return False
        if not candidate.resolve().is_relative_to(root):
            return False
    return True


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
    """Serialises tasks declaring writes to the same file (§10.5).

    Used when checkout isolation is unavailable. This is coordination between tasks,
    not filesystem enforcement: callers must declare their intended writes.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._owners: dict[asyncio.Lock, str | None] = {}

    async def acquire(
        self,
        paths: Sequence[str],
        *,
        root: Path,
        owner: str | None = None,
        on_wait: Callable[[str, str | None], Awaitable[None]] | None = None,
    ) -> list[asyncio.Lock]:
        """Lock every declared file, in a stable order; no inference or truncation."""
        root = root.resolve()
        keys: dict[str, str] = {}
        for path in normalize_write_paths(paths, root=root):
            resolved = (root / path).resolve()
            key = os.path.normcase(str(resolved))
            # Relative names remain readable in queued messages; aliases share one lock.
            display = resolved.relative_to(root) if resolved.is_relative_to(root) else resolved
            keys.setdefault(key, display.as_posix())
        acquired: list[asyncio.Lock] = []
        try:
            for key in sorted(keys):
                lock = self._locks[key]
                if lock.locked() and on_wait is not None:
                    await on_wait(keys[key], self._owners.get(lock))
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
