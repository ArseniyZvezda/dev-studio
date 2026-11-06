"""Plastic SCM / Unity Version Control, driven through the ``cm`` command line.

This is a :class:`~voxarium_studio.vcs.base.VcsBackend`, so registering it teaches the whole
Studio about Plastic at once: the project header reports it, the settings page shows
whether ``cm`` is installed, and the built-in checkin nodes run against the same
object rather than shelling out on their own.

Two things shape the implementation:

* **Plastic separates "checked out" from "changed".** Its normal workflow is to check an
  item out before editing it, and a plain ``cm checkin`` only takes checked-out items. A
  pipeline node never checks anything out — it writes the file and moves on — so every
  checkin here passes ``--all`` and ``--applychanged``, which is what picks up items that
  merely changed on disk (the second is what the CLI documents as additionally required
  when the path is a directory, and ``.`` always is). Without them a regenerated asset
  would commit nothing and report success.
* **``cm``'s machine-readable output is not stable across versions.** Parsing is
  deliberately forgiving: an unrecognised status code costs the change's *kind*, never
  its path, and the whole status call falls back to ``--short`` (paths only) if the
  machine-readable form is rejected.

The command lines were checked against a real ``cm`` 11.0 (``cm help <command>`` plus a
parse-only run outside a workspace). The status codes below and the shape of a successful
checkin's output come from the documentation, which is the other reason parsing stays
forgiving; ``status --machinereadable`` and ``cat`` have since been read against a live
workspace (see :meth:`PlasticBackend.workspace_patch`).

Isolation is left unsupported. Git worktrees are a local, instant second checkout;
Plastic's equivalent is a server-side workspace with its own selector and disk copy,
which is far too heavy to create per agent task. Team tasks on a Plastic project
therefore fall back to explicit write-path locks in §10.5.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from .base import (
    CommitResult,
    FileChange,
    LogFn,
    VcsBackend,
    VcsError,
    VcsStatus,
)

WORKSPACE_MARKER = ".plastic"
"""Directory ``cm`` drops at the root of a workspace, the way git has ``.git``."""

FIELD_SEPARATOR = ";"

# Two-letter codes cm uses in machine-readable status, mapped onto the abstraction's
# vocabulary (`ChangeKind`). Anything missing here degrades to "unknown", never to a lost
# path. "ignored" is the one value below that is *not* in that vocabulary: it is a local
# sentinel for items cm mentions and the Studio does not consider changes, and `_changes`
# drops them before any of them can leave this module.
STATUS_CODES = {
    "AD": "added",
    "AP": "added",
    "CH": "modified",
    "CO": "modified",
    "CP": "copied",
    "DE": "deleted",
    "LD": "deleted",
    "LM": "renamed",
    "MV": "renamed",
    "PR": "untracked",
    "IG": "ignored",
    "RP": "modified",
    "CA": "conflicted",
}

_CHANGESET = re.compile(r"\bcs:(\d+)")
_BRANCH_SPEC = re.compile(r"\bbr:(/[^@\s]*)")
_BRANCH_BARE = re.compile(r"(?:^|\s)(/[^@\s]*)@")


class PlasticBackend(VcsBackend):
    id: ClassVar[str] = "plastic"
    settings_fields = ({"key": "workspace", "type": "path"},)
    name: ClassVar[str] = "Plastic SCM (UVC)"
    program: ClassVar[str] = "cm"

    supports_isolation: ClassVar[bool] = False
    """See the module docstring — a per-task workspace would be a server round trip."""

    def __init__(self, root: Path, settings: dict[str, Any] | None = None) -> None:
        super().__init__(root, settings)
        self.project_root = Path(root)
        # The managed project is normally the workspace, but a studio that keeps one big
        # Plastic workspace with the Unity project inside it can point at the real root.
        configured = str(self.settings.get("workspace") or "").strip()
        if configured:
            workspace = Path(configured).expanduser()
            self.root = (workspace if workspace.is_absolute() else self.project_root / workspace).resolve()

    def _targets(self, paths: Sequence[str]) -> list[str]:
        """Explicit paths stay project-relative when the CLI runs at another root (§9.6)."""
        targets = _clean(paths)
        if self.root == self.project_root:
            return targets
        return [
            str(self.project_root / path) if not Path(path).is_absolute() else path
            for path in targets
        ]

    # ---- discovery -------------------------------------------------------------

    def workspace_root(self) -> Path | None:
        """Nearest ancestor holding ``.plastic``, or ``None`` if there is none."""
        for candidate in (self.root, *self.root.parents):
            if (candidate / WORKSPACE_MARKER).is_dir():
                return candidate
        return None

    async def detect(self) -> bool:
        if self.workspace_root() is not None:
            return True
        available, _ = self.is_available()
        if not available or not self.root.exists():
            return False
        # No marker found — ask cm, which also covers a workspace mounted oddly.
        result = await self.run("getworkspacefrompath", str(self.root), timeout=30)
        return result.ok and bool(result.stdout.strip())

    # ---- inspection ------------------------------------------------------------

    async def current_branch(self) -> str | None:
        branch, _ = await self._workspace_header()
        return branch

    async def revision(self) -> str | None:
        """The changeset the workspace sits on — Plastic's answer to a commit sha.

        ``status()`` has always reported it; this is the same value under the name the
        abstraction asks for, so ``/api/vcs`` can show a Plastic project's position
        without knowing it is Plastic.
        """
        _, changeset = await self._workspace_header()
        return changeset

    async def status(self) -> VcsStatus:
        branch, changeset = await self._workspace_header()
        return VcsStatus(
            backend=self.id,
            root=self.root,
            branch=branch,
            revision=changeset,
            changes=await self._changes(),
        )

    async def diff(self, path: Path | str | None = None) -> str:
        """A summary of pending changes.

        ``cm diff`` compares two changesets, which is not what a reviewer looking at
        uncommitted work wants, so this reports the pending change list instead.
        """
        changes = await self._changes(paths=self._targets([str(path)]) if path else None)
        return "\n".join(f"{change.kind:>10}  {change.path}" for change in changes)

    async def workspace_patch(self, path: Path | str, file: str | None = None) -> str:
        """One file's change as a unified diff: the loaded revision against the disk.

        ``cm`` prints no textual patch for a pending change — ``cm diff`` on a file opens
        the Diff *window* — so the patch is made here: the revision the workspace loaded
        comes from ``cm cat``, and :mod:`difflib` compares it with the working copy. Two
        things were measured against a live workspace rather than read in the docs:

        * ``cm cat <path>`` with no ``#spec`` answers the **loaded** revision, which is
          the base of a pending change; a private file answers "revision was not found"
          (no base: every line is an addition) and a checked-out add answers an empty
          revision, which amounts to the same patch.
        * The content has to go through ``--file=``. Printed to stdout it is re-encoded
          through the console codepage, so a UTF-8 file with one non-ASCII character
          already differs from itself, and a binary one is mangled outright.

        The whole workspace is still the summary :meth:`diff` gives: a patch per file is
        a ``cm`` process per file, and nothing asks for it on Plastic — the per-task
        checkout that would is exactly what ``supports_isolation`` says does not exist.
        """
        inner = self._at(path)
        if file is None:
            return await inner.diff()

        target = Path(file)
        if not target.is_absolute():
            target = Path(path) / target
        base = await inner._loaded_revision(target)
        work = target.read_bytes() if target.is_file() else None
        return _unified_patch(base, work, inner._display_path(target))

    def _at(self, path: Path | str) -> PlasticBackend:
        """This backend pointed at ``path`` — itself when that is already its workspace.

        With no isolation (see the module docstring) the only workspace a caller can
        name is this one, and a second instance would only re-read the settings.
        """
        if _key(path) in {_key(self.root), _key(self.project_root)}:
            return self
        return type(self)(Path(path), dict(self.settings))

    async def _loaded_revision(self, target: Path) -> bytes | None:
        """The bytes of ``target``'s loaded revision, or ``None`` if it has none."""
        with tempfile.TemporaryDirectory(prefix="voxarium_studio-cm-") as scratch:
            out = Path(scratch) / "base"
            result = await self.run("cat", str(target), f"--file={out}", timeout=300)
            if not result.ok or not out.is_file():
                return None
            return out.read_bytes()

    def _display_path(self, target: Path) -> str:
        """The name a patch header carries: inside the workspace, relative to it."""
        try:
            return target.resolve().relative_to(self.root.resolve()).as_posix()
        except (ValueError, OSError):
            return target.as_posix()

    # ---- writing ---------------------------------------------------------------

    async def add(
        self, paths: Sequence[str], *, recursive: bool = True, log_fn: LogFn | None = None
    ) -> list[str]:
        private = [change.path for change in await self._changes() if change.kind == "untracked"]
        targets = self._targets(paths) or private
        if not targets:
            return []

        # `cm add` refuses an item whose parent directory is not under control yet, and
        # has no --parents of its own — a pipeline writing into a brand new
        # Assets/Generated hits that every time.
        parents = self._private_parents(targets, private)
        if parents:
            # Never recursive: these directories only need to exist under source control
            # so their child can be added. Recursing would sweep in every other private
            # file underneath, which is not what the caller named.
            await self._add(parents, recursive=False, log_fn=log_fn)
        await self._add(targets, recursive=recursive, log_fn=log_fn)
        return [*parents, *targets]

    async def _add(self, items: Sequence[str], *, recursive: bool, log_fn: LogFn | None) -> None:
        args = ["add", "--ignorefailed"]
        if recursive:
            args.append("-R")
        result = await self.run(*args, *items, timeout=600, log_fn=log_fn)
        if not result.ok:
            raise VcsError(f"cm add failed:\n{result.output or '(no output)'}")

    def _private_parents(self, targets: Sequence[str], private: Sequence[str]) -> list[str]:
        """Uncontrolled directories between the workspace root and each target, outermost first."""
        known = {_key(path): path for path in private}
        named = {_key(target) for target in targets}
        ordered: list[str] = []
        seen: set[str] = set()

        for target in targets:
            absolute = Path(target)
            if not absolute.is_absolute():
                absolute = self.root / absolute
            ancestors: list[str] = []
            for parent in absolute.parents:
                if parent == self.root or self.root not in parent.parents:
                    break
                match = known.get(_key(parent))
                if match is not None and _key(match) not in named:
                    ancestors.append(match)
            for path in reversed(ancestors):
                if _key(path) not in seen:
                    seen.add(_key(path))
                    ordered.append(path)
        return ordered

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
            raise VcsError("A checkin comment is required")

        targets = self._targets(paths or [])
        pending = await self._changes(paths=targets or None, include_private=add_untracked)
        branch, _ = await self._workspace_header()
        if not pending:
            return CommitResult(committed=False, branch=branch, message=message)

        # `-c=<comment>` is one argument, which is the form the CLI documents; --all takes
        # locally changed/moved/deleted items, and --applychanged is what the docs say is
        # additionally needed when the path is a directory, which "." always is. There is
        # no --recursive: checkin is always recursive from the given path.
        args = ["checkin", *(targets or ["."]), f"-c={message}", "--all", "--applychanged"]
        if add_untracked:
            args.append("--private")

        result = await self.run(*args, timeout=timeout or 1800, log_fn=log_fn)
        if not result.ok:
            raise VcsError(f"cm checkin failed:\n{result.output or '(no output)'}")

        changeset = _find_changeset(result.output)
        if changeset is None:
            # Older clients print nothing useful; the workspace header knows where we are.
            _, changeset = await self._workspace_header()
        return CommitResult(
            committed=True,
            revision=changeset,
            branch=branch,
            files=[change.path for change in pending],
            message=message,
        )

    # ---- internals -------------------------------------------------------------

    async def _workspace_header(self) -> tuple[str | None, str | None]:
        """(branch, changeset) from the line ``cm status`` prints above the changes."""
        result = await self.run("status", "--header", timeout=60)
        if not result.ok:
            result = await self.run("status", timeout=120)
        if not result.ok:
            return None, None

        for line in result.stdout.splitlines()[:5]:
            branch_match = _BRANCH_SPEC.search(line) or _BRANCH_BARE.search(line)
            changeset_match = _CHANGESET.search(line)
            if branch_match or changeset_match:
                return (
                    branch_match.group(1) if branch_match else None,
                    f"cs:{changeset_match.group(1)}" if changeset_match else None,
                )
        return None, None

    async def _changes(
        self, *, paths: Sequence[str] | None = None, include_private: bool = True
    ) -> list[FileChange]:
        # No search-type flags: `cm status` shows every change by default, controlled and
        # private alike, ignored ones excluded. --noheader keeps the workspace line out.
        args = ["status", "--machinereadable", f"--fieldseparator={FIELD_SEPARATOR}", "--noheader"]
        if paths:
            args.extend(paths)
        result = await self.run(*args, timeout=300)
        machine_readable = result.ok

        if not machine_readable:
            # Older or differently-built clients reject the flags above; --short is the
            # lowest common denominator and still answers "which files changed".
            fallback = ["status", "--short", *(paths or [])]
            result = await self.run(*fallback, timeout=300)
            if not result.ok:
                raise VcsError(f"cm status failed:\n{result.output or '(no output)'}")

        changes = [
            change
            for change in _parse_status(result.stdout, machine_readable=machine_readable)
            if change.kind != "ignored"
        ]
        if not include_private:
            changes = [change for change in changes if change.kind != "untracked"]
        return changes


def _clean(paths: Sequence[str]) -> list[str]:
    return [str(path).strip() for path in paths if str(path).strip()]


def _unified_patch(base: bytes | None, work: bytes | None, name: str) -> str:
    """A ``git diff``-shaped patch between two versions of one file.

    ``None`` on a side is "no such revision": an addition reads ``--- /dev/null`` and a
    deletion ``+++ /dev/null``, which is the spelling the Studio's patch pane already
    colours. A NUL byte on either side makes the file binary, and a binary file gets the
    one line ``git`` prints for it rather than a diff of replacement characters.
    """
    if base is None and work is None:
        return ""
    if base == work:
        return ""
    old_name = "/dev/null" if base is None else f"a/{name}"
    new_name = "/dev/null" if work is None else f"b/{name}"
    if b"\0" in (base or b"") or b"\0" in (work or b""):
        return f"Binary files {old_name} and {new_name} differ\n"

    old_lines = (base or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
    new_lines = (work or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
    lines = difflib.unified_diff(old_lines, new_lines, fromfile=old_name, tofile=new_name)
    # difflib keeps each line's own ending, so a file whose last line has none would run
    # into the next patch line; the one newline every line gets here is the format's.
    return "".join(line if line.endswith("\n") else f"{line}\n" for line in lines)


def _key(path: str | Path) -> str:
    """Comparable form of a path — Windows is case-insensitive and cm returns absolutes."""
    return os.path.normcase(os.path.normpath(str(path)))


def _find_changeset(text: str) -> str | None:
    """Last ``cs:<n>`` in the checkin output — that is the one it just created."""
    matches = _CHANGESET.findall(text)
    return f"cs:{matches[-1]}" if matches else None


def _is_status_code(token: str) -> bool:
    """A leading status code, and not the start of a path.

    ``C:`` and ``D:`` are Windows drive letters, but they carry a colon and so fail
    ``isalpha`` — which is exactly the case this has to survive.
    """
    return 2 <= len(token) <= 3 and token.isalpha() and token.isupper()


def _parse_status(raw: str, *, machine_readable: bool) -> list[FileChange]:
    changes: list[FileChange] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("STATUS"):
            continue  # the header, when a client emits it alongside the changes

        if not machine_readable:
            changes.append(FileChange(path=line, kind="unknown", staged=True))
            continue

        fields = (
            [part.strip() for part in line.split(FIELD_SEPARATOR)]
            if FIELD_SEPARATOR in line
            else line.split(None, 1)
        )
        head = fields[0].strip()
        if len(fields) > 1 and _is_status_code(head):
            kind = STATUS_CODES.get(head.upper(), "unknown")
            path = fields[1].strip()
        else:
            kind, path = "unknown", line
        if path:
            # Plastic has no staging area: everything pending goes in the next checkin.
            changes.append(FileChange(path=path, kind=kind, staged=kind != "untracked"))
    return changes


__all__ = ["PlasticBackend", "STATUS_CODES", "WORKSPACE_MARKER"]
