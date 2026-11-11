"""Isolation for parallel agent work (§10.5).

Two members editing the same files at once corrupt each other's work, so an active task
gets its own checkout when the project's version control system can provide one — a git
worktree, or whatever the detected :class:`~voxarium_studio.vcs.base.VcsBackend` offers — and
merging back happens through the review step. Where it cannot (no version control at all,
or a system whose second checkout means a server round trip), we fall back to serialising
tasks whose explicit write declarations — files, or directories as ``dir/*`` — overlap. Read-only references never take locks.

The module is still called ``worktree`` because ``TeamTask.worktree_path`` and the
``DELETE …/worktree`` endpoint are, and those are a stored column and a published API.
Nothing in here is git-specific any more.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..vcs import IsolatedWorkspace, VcsBackend, VcsError

log = logging.getLogger(__name__)

DIRECTORY_SUFFIX = "/*"
"""``foo/bar/*`` claims everything inside ``foo/bar``, at any depth (§10.5)."""


def split_write_path(path: str) -> tuple[str, bool]:
    """A normalised declaration as ``(path, is_directory)``: ``foo/bar/*`` → ``foo/bar``."""
    if path.endswith(DIRECTORY_SUFFIX):
        return path[: -len(DIRECTORY_SUFFIX)], True
    return path, False


def normalize_write_paths(paths: Sequence[str], *, root: Path | None = None) -> list[str]:
    """Validate explicit write declarations, never infer them from prose (§10.5).

    An entry is an exact file, or a directory spelled ``foo/bar/*`` — everything inside
    it. That trailing ``/*`` is the only pattern there is: no other glob is accepted.

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
        path, directory = split_write_path(value.strip().replace("\\", "/"))
        if (not path or any(char in path for char in "\x00\r\n*?[]")
                or "://" in path or path.endswith("/")):
            raise ValueError(
                "write_paths must contain exact file paths or a directory as dir/*, "
                "not blanks, URLs or other patterns"
            )
        windows = PureWindowsPath(path)
        if windows.drive and not windows.root:
            raise ValueError("write_paths cannot contain drive-relative paths")
        # Preserve '..' until filesystem-aware resolution: link/../file can point
        # outside this directory on POSIX, even when lexical normpath says otherwise.
        path = PurePosixPath(path).as_posix()
        if path in {".", "..", "/"} or path.endswith("/.."):
            raise ValueError(
                "write_paths must name files or a directory as dir/*, "
                "not the project root or a parent directory"
            )
        if root is not None and not directory and (root / path).is_dir():
            raise ValueError(
                f"write_paths must name files: {path} is a directory — "
                f"declare everything inside it as {path}{DIRECTORY_SUFFIX}"
            )
        if root is not None and directory and (root / path).is_file():
            raise ValueError(f"write_paths: {path} is a file, not a directory")
        if directory:
            path += DIRECTORY_SUFFIX
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
        relative = Path(split_write_path(path)[0])
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
    """Give one task its own checkout under ``.voxarium_studio/worktrees/<task_id>``.

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


class PathBusy(ValueError):
    """A path cannot be claimed right now: another running task holds it (§10.5)."""

    def __init__(self, path: str, owner: str | None) -> None:
        self.path, self.owner = path, owner
        holder = f"task {owner}" if owner else "another task"
        super().__init__(f"{path} is held by {holder}, which is running")


@dataclass(eq=False)
class PathClaim:
    """One held declaration: a file, or a directory and everything inside it."""

    key: str
    display: str
    directory: bool
    owner: str | None
    holder: object = field(repr=False)
    manager: PathLockManager = field(repr=False)

    def locked(self) -> bool:
        return any(claim is self for claim in self.manager._held)

    def overlaps(self, other: PathClaim) -> bool:
        if self.key == other.key:
            return True
        return (
            (self.directory and other.key.startswith(self.key + os.sep))
            or (other.directory and self.key.startswith(other.key + os.sep))
        )


class PathLockManager:
    """Serialises tasks declaring writes to the same file or directory (§10.5).

    Used when checkout isolation is unavailable. This is coordination between tasks,
    not filesystem enforcement: callers must declare their intended writes.

    A task takes its whole declaration at once or waits holding nothing, so two tasks
    can never deadlock on each other's halves — which per-path ordering stopped
    guaranteeing once a directory claim could overlap a file claim under another key.
    """

    def __init__(self) -> None:
        self._held: list[PathClaim] = []
        self._changed = asyncio.Event()

    def _claims(
        self, paths: Sequence[str], root: Path, owner: str | None, holder: object,
    ) -> list[PathClaim]:
        claims: dict[tuple[str, bool], PathClaim] = {}
        for path in normalize_write_paths(paths, root=root):
            base, directory = split_write_path(path)
            resolved = (root / base).resolve()
            key = os.path.normcase(str(resolved))
            # Relative names remain readable in queued messages; aliases share one lock.
            shown = resolved.relative_to(root) if resolved.is_relative_to(root) else resolved
            display = shown.as_posix() + (DIRECTORY_SUFFIX if directory else "")
            claims.setdefault(
                (key, directory), PathClaim(key, display, directory, owner, holder, self),
            )
        return [claims[index] for index in sorted(claims)]

    def _blocker(
        self, wanted: Sequence[PathClaim], holder: object,
    ) -> tuple[str, str | None] | None:
        """The first wanted path somebody else holds, and that holder's owner."""
        for claim in wanted:
            for held in self._held:
                if held.holder is not holder and claim.overlaps(held):
                    return claim.display, held.owner
        return None

    async def acquire(
        self,
        paths: Sequence[str],
        *,
        root: Path,
        owner: str | None = None,
        on_wait: Callable[[str, str | None], Awaitable[None]] | None = None,
    ) -> list[PathClaim]:
        """Lock every declared file and directory; no inference or truncation.

        The returned list is the holder's identity: pass that same object to
        :meth:`replace` and :meth:`release`.
        """
        root = root.resolve()
        wanted: list[PathClaim] = []
        wanted.extend(self._claims(paths, root, owner, wanted))
        told: tuple[str, str | None] | None = None
        while (blocker := self._blocker(wanted, wanted)) is not None:
            if on_wait is not None and blocker != told:
                told = blocker
                await on_wait(*blocker)
                continue  # the world may have moved while the notice was being recorded
            await self._changed.wait()
        # Nothing was held while queued, so cancellation there has nothing to undo (§10.5).
        self._held.extend(wanted)
        return wanted

    def replace(
        self, held: list[PathClaim], paths: Sequence[str], *, root: Path,
        owner: str | None = None,
    ) -> None:
        """Swap a running task's claims for a new declaration, in place and without waiting.

        All or nothing: :class:`PathBusy` names the first path another task holds, and the
        task then keeps exactly what it had. ``held`` is the list :meth:`acquire` returned;
        it is mutated so whoever releases it later releases the new set.
        """
        wanted = self._claims(paths, root.resolve(), owner, held)
        blocker = self._blocker(wanted, held)
        if blocker is not None:
            raise PathBusy(*blocker)
        self._drop(held)
        held[:] = wanted
        self._held.extend(wanted)

    def _drop(self, claims: Sequence[PathClaim]) -> None:
        gone = {id(claim) for claim in claims}
        kept = [claim for claim in self._held if id(claim) not in gone]
        if len(kept) == len(self._held):
            return
        self._held = kept
        # Wake every waiter to re-check; the next wait gets a fresh event.
        self._changed.set()
        self._changed = asyncio.Event()

    def release(self, locks: Sequence[PathClaim]) -> None:
        self._drop(locks)


