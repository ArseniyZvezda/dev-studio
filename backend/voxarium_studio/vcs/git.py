"""Git backend — the one the Studio ships with.

Git is what the rest of the design already assumes (`.voxarium_studio/` is meant to be committed,
pipelines and team rosters are YAML so they diff), so it stays built in rather than
becoming a plugin. Everything here used to be inline in ``teams/worktree.py`` and
``core/project.py``; it moved so the same operations exist for any other system.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from ..core.paths import LEGACY_DATA_DIRNAME
from .base import (
    CommitResult,
    FileChange,
    IsolatedWorkspace,
    LogFn,
    VcsBackend,
    VcsError,
    VcsStatus,
)

log = logging.getLogger(__name__)

_KIND_BY_LETTER = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "modified",  # type change: symlink <-> file
    "U": "conflicted",
}

_UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

_NULL_PATH = "/dev/null"
"""git's own name for "nothing", on every platform it runs on — it is not a filesystem
path here but a token git understands, so it stays a forward-slashed literal on Windows."""


class GitBackend(VcsBackend):
    id: ClassVar[str] = "git"
    name: ClassVar[str] = "Git"
    program: ClassVar[str] = "git"
    supports_isolation: ClassVar[bool] = True
    """Worktrees are local and cheap, which is what makes per-task isolation viable."""

    def __init__(self, root: Path, settings: dict[str, Any] | None = None) -> None:
        super().__init__(root, settings)
        self._toplevel: Path | None = None

    async def detect(self) -> bool:
        if (self.root / ".git").exists():
            return True
        available, _ = self.is_available()
        if not available:
            return False
        result = await self.run("rev-parse", "--is-inside-work-tree", timeout=10)
        return result.ok and result.stdout.strip() == "true"

    # ---- inspection ------------------------------------------------------------

    async def current_branch(self) -> str | None:
        result = await self.run("rev-parse", "--abbrev-ref", "HEAD", timeout=15)
        if not result.ok:
            return None
        branch = result.stdout.strip()
        # "HEAD" means detached, which is not a branch name anyone can act on.
        if not branch or branch == "HEAD":
            return None
        return branch

    async def repair_moved_worktrees(self, directory: Path) -> None:
        """Refresh Git's backlinks to task checkouts moved with the data directory (§5)."""
        if not any(directory.glob("*/.git")):
            return
        registered = await self.run("worktree", "list", "--porcelain", "-z", check=True)
        previous = self.root / LEGACY_DATA_DIRNAME / "worktrees"
        for record in registered.stdout.split("\0\0"):
            old = Path(record.partition("\0")[0].removeprefix("worktree "))
            if old.parent != previous:
                continue
            moved = directory / old.name
            if (moved / ".git").is_file() and not moved.is_symlink() and not moved.is_junction():
                await self.run("worktree", "repair", str(moved), check=True)

    async def revision(self) -> str | None:
        result = await self.run("rev-parse", "HEAD", timeout=15)
        return result.stdout.strip() if result.ok else None

    async def repository_root(self) -> Path:
        """The repository's own root, which can sit **above** the managed project.

        Pointing the Studio at ``<repo>/game/`` inside a monorepo is a normal layout, and
        ``detect`` accepts it. It matters because ``git status --porcelain`` reports every
        path relative to the **repository** root whatever directory it ran in — so this is
        the frame those paths are in, and it is what :attr:`VcsStatus.root` carries. The
        caller re-bases from there onto whatever frame it needs.

        Asked at most once per instance, and not at all in the common case: a ``.git``
        here means this directory *is* the root.
        """
        if self._toplevel is None:
            if (self.root / ".git").exists():
                self._toplevel = self.root
            else:
                result = await self.run("rev-parse", "--show-toplevel", timeout=15)
                raw = result.stdout.strip()
                self._toplevel = Path(raw) if result.ok and raw else self.root
        return self._toplevel

    async def status(self) -> VcsStatus:
        # `-uall` lists every untracked *file*. Without it git collapses a directory that
        # is untracked in its entirety into one entry with a trailing slash — `scripts/`
        # — and a file tree colouring by path then leaves everything inside it unmarked,
        # which is the state a newly added folder is always in. The cost is walking those
        # directories; ignored ones are not walked, so it is bounded by what the project
        # genuinely has yet to commit. This is what `FileChange.path` naming a file rather
        # than sometimes a directory depends on.
        result = await self.run("status", "--porcelain", "-z", "-uall", timeout=60)
        if not result.ok:
            raise VcsError(f"git status failed:\n{result.output or '(no output)'}")
        return VcsStatus(
            backend=self.id,
            root=await self.repository_root(),
            branch=await self.current_branch(),
            revision=await self.revision(),
            changes=_parse_porcelain(result.stdout),
        )

    async def diff(self, path: Path | str | None = None) -> str:
        if path is None:
            result = await self.run("diff", "--stat", "HEAD", timeout=60)
            return result.stdout.strip() if result.ok else ""

        target = Path(path)
        if not target.exists():
            return ""
        if target.is_dir():
            # A directory may be a checkout of its own — that is what §10.5's isolated
            # workspaces are, and `workspace_diff` passes one straight in here. Filtering
            # the main tree by that path would diff the wrong files (or none: `worktrees/`
            # is in the generated .gitignore), so run *inside* it.
            result = await self.run("diff", "--stat", "HEAD", cwd=target, timeout=60)
        else:
            # A single file cannot be a cwd — the Explorer asks for exactly this.
            result = await self.run(
                "diff", "--stat", "HEAD", "--", self.relative(target), timeout=60
            )
        return result.stdout.strip() if result.ok else ""

    # ---- one task's workspace (§10.5) ------------------------------------------

    async def workspace_changes(self, path: Path | str) -> VcsStatus:
        """Everything an isolated worktree has done since it was branched.

        Not :meth:`status`, which is uncommitted work alone: a worktree is handed to an
        agent, and an agent that commits its own work would leave a reviewer reading an
        empty list under "changed files". ``git diff <base>`` compares a commit with the
        *working tree*, so one command covers both halves — what was committed on the
        branch and what is still only on disk.

        Untracked files are the exception and come from :meth:`status`: ``git diff`` cannot
        see a file git has never been told about, and a brand new asset is exactly the kind
        of thing a task produces.
        """
        inner = self._at(path)
        base = await inner._branch_point(self)
        if base is None:
            return await inner.status()

        changes: dict[str, FileChange] = {}
        kinds = await inner.run("diff", "--name-status", "-z", base, timeout=120)
        if kinds.ok:
            for letter, target, origin in _parse_name_status(kinds.stdout):
                changes[target] = FileChange(
                    path=target,
                    kind=_KIND_BY_LETTER.get(letter, "unknown"),
                    staged=False,
                    origin=origin,
                )
        counts = await inner.run("diff", "--numstat", "-z", base, timeout=120)
        if counts.ok:
            for target, insertions, deletions in _parse_numstat(counts.stdout):
                change = changes.get(target) or FileChange(path=target, kind="modified")
                change.insertions = insertions
                change.deletions = deletions
                change.binary = insertions is None
                changes[target] = change

        for change in (await inner.status()).changes:
            changes.setdefault(change.path, change)

        return VcsStatus(
            backend=self.id,
            root=Path(path),
            branch=await inner.current_branch(),
            revision=await inner.revision(),
            changes=sorted(changes.values(), key=lambda change: change.path),
        )

    async def workspace_patch(self, path: Path | str, file: str | None = None) -> str:
        """A real patch, not the ``--stat`` summary :meth:`diff` answers with."""
        inner = self._at(path)
        base = await inner._branch_point(self) or "HEAD"
        args = ["diff", base] + (["--", file] if file else [])
        result = await inner.run(*args, timeout=120)
        if result.stdout.strip():
            return result.stdout
        if file:
            # An untracked file is in no diff, because git has never heard of it. Against
            # the empty tree it is what it is — an addition of every line it has.
            empty = await inner.run("diff", "--no-index", "--", _NULL_PATH, file, timeout=120)
            # `--no-index` exits 1 when the files differ, which is the answer, not a failure.
            return empty.stdout
        return ""

    def _at(self, path: Path | str) -> GitBackend:
        """This backend pointed at another checkout — same tool, same settings."""
        return type(self)(Path(path), dict(self.settings))

    async def _branch_point(self, project: GitBackend) -> str | None:
        """The commit this workspace was branched from, or ``None`` if it cannot be told.

        A worktree is created at the project's ``HEAD`` (see :meth:`create_workspace`), and
        the project moves on afterwards, so the branch point is the merge base of the two
        rather than either end. They share an object database, so this is a local lookup.
        """
        head = await project.revision()
        if not head:
            return None
        result = await self.run("merge-base", "HEAD", head, timeout=30)
        base = result.stdout.strip()
        return base if result.ok and base else None

    # ---- writing ---------------------------------------------------------------

    async def add(
        self, paths: Sequence[str], *, recursive: bool = True, log_fn: LogFn | None = None
    ) -> list[str]:
        targets = [str(p) for p in paths if str(p).strip()]
        args = ["add", "--"] + targets if targets else ["add", "-A"]
        await self.run(*args, log_fn=log_fn, check=True)
        status = await self.status()
        return [change.path for change in status.changes if change.staged]

    async def commit(
        self,
        message: str,
        *,
        paths: Sequence[str] | None = None,
        add_untracked: bool = False,
        timeout: float | None = None,
        log_fn: LogFn | None = None,
    ) -> CommitResult:
        if not message.strip():
            raise VcsError("A commit message is required")

        targets = [str(p) for p in (paths or []) if str(p).strip()]
        if targets:
            stage = ["add", "--"] + targets if add_untracked else ["add", "-u", "--"] + targets
        else:
            # No paths: the whole workspace, tracked-only unless untracked files are wanted.
            stage = ["add", "-A"] if add_untracked else ["add", "-u"]
        await self.run(*stage, timeout=timeout, log_fn=log_fn, check=True)

        staged = [change.path for change in (await self.status()).changes if change.staged]
        branch = await self.current_branch()
        if not staged:
            return CommitResult(committed=False, branch=branch, message=message)

        result = await self.run("commit", "-m", message, timeout=timeout, log_fn=log_fn)
        if not result.ok:
            raise VcsError(f"git commit failed:\n{result.output or '(no output)'}")

        return CommitResult(
            committed=True,
            revision=await self.revision(),
            branch=branch,
            files=staged,
            message=message,
        )

    # ---- isolation (§10.5) -----------------------------------------------------

    async def create_workspace(self, path: Path, branch: str) -> IsolatedWorkspace | None:
        available, reason = self.is_available()
        if not available:
            log.info("No worktree isolation: %s", reason)
            return None
        if not await self.revision():
            log.info("Repository has no commits yet — running the agent in the main tree")
            return None
        if path.exists():
            return IsolatedWorkspace(path=path, branch=branch)

        path.parent.mkdir(parents=True, exist_ok=True)
        result = await self.run("worktree", "add", "-b", branch, str(path), "HEAD", timeout=180)
        if not result.ok:
            log.warning("Could not create worktree %s: %s", path, result.output)
            return None
        log.info("Created worktree %s on branch %s", path, branch)
        return IsolatedWorkspace(path=path, branch=branch)

    async def remove_workspace(self, path: Path, *, force: bool = False) -> None:
        if not path.exists():
            return
        args = ["worktree", "remove", str(path)]
        if force:
            args.append("--force")
        result = await self.run(*args, timeout=180)
        if not result.ok:
            log.warning("Could not remove worktree %s: %s", path, result.output)


def _parse_name_status(raw: str) -> list[tuple[str, str, str | None]]:
    """Parse ``git diff --name-status -z`` into ``(letter, path, origin)``.

    ``-z`` for the same reason :func:`_parse_porcelain` uses it, and with the same shape:
    a rename or copy spends three fields — the code, the old path, the new one — where
    everything else spends two.
    """
    fields = [field for field in raw.split("\0") if field]
    entries: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(fields) - 1:
        code = fields[index]
        index += 1
        if code[0] in ("R", "C") and index + 1 < len(fields):
            origin, target = fields[index], fields[index + 1]
            index += 2
            entries.append((code[0], target, origin))
            continue
        entries.append((code[0], fields[index], None))
        index += 1
    return entries


def _parse_numstat(raw: str) -> list[tuple[str, int | None, int | None]]:
    """Parse ``git diff --numstat -z`` into ``(path, insertions, deletions)``.

    A binary file counts as ``-`` on both sides and comes back as ``None``, which is a
    different statement from ``0`` and is shown differently. The record for a rename ends
    with an *empty* path field and spends two more on the old and new names, so the paths
    are read from the NUL fields rather than from the tab-separated head.
    """
    fields = [field for field in raw.split("\0") if field != ""] if raw else []
    entries: list[tuple[str, int | None, int | None]] = []
    index = 0
    while index < len(fields):
        parts = fields[index].split("	")
        index += 1
        if len(parts) < 3:
            continue
        added, removed, target = parts[0], parts[1], parts[2]
        if not target and index < len(fields):
            # Rename: the head ended after the two counts, the names follow it.
            index += 1  # the old path
            if index < len(fields):
                target = fields[index]
                index += 1
        if not target:
            continue
        entries.append(
            (
                target,
                None if added == "-" else int(added),
                None if removed == "-" else int(removed),
            )
        )
    return entries


def _parse_porcelain(raw: str) -> list[FileChange]:
    """Parse ``git status --porcelain -z``.

    ``-z`` rather than plain newlines because git otherwise quotes and escapes paths that
    contain spaces or non-ASCII — routine in a game project's asset tree. Rename and copy
    entries carry the source path in the following NUL-separated field.

    A trailing slash is stripped: ``-uall`` means git no longer collapses untracked
    directories, but a submodule still reports as one, and a caller matching by path
    would never recognise it.
    """
    fields = [field for field in raw.split("\0") if field]
    changes: list[FileChange] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:].rstrip("/")
        origin = None
        if code[0] in ("R", "C") and index < len(fields):
            origin = fields[index]
            index += 1

        if code == "??":
            kind, staged = "untracked", False
        elif code in _UNMERGED:
            kind, staged = "conflicted", False
        else:
            # X is the index, Y the working tree; the index wins when both moved.
            letter = code[0] if code[0] != " " else code[1]
            kind = _KIND_BY_LETTER.get(letter, "unknown")
            staged = code[0] not in (" ", "?")
        changes.append(FileChange(path=path, kind=kind, staged=staged, origin=origin))
    return changes


__all__ = ["GitBackend"]
