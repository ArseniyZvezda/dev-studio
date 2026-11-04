"""Version control abstraction.

The Studio touches version control in three places: it reports what the managed project
is under, it isolates parallel agent work (§10.5), and pipeline nodes commit generated
assets. All three went straight to ``git`` before this module existed, which made a
Perforce or Plastic project a second-class citizen.

``VcsBackend`` is the seam, following the same shape as ``AgentBackend`` in §10.6: the
Studio ships one implementation (:mod:`voxarium_studio.vcs.git`) and a plugin contributes the
rest through ``registry.add_vcs_backend()``. Everything a backend needs to drive a
command-line tool — running it, killing it on cancellation, turning a non-zero exit into
an error — lives here, so an implementation is mostly argument building and output
parsing.

Isolation is optional and declared with ``supports_isolation``. Git worktrees are cheap
and local; a centralised system's equivalent means a server round trip and a second
checkout on disk, so a backend that cannot do it honestly says so and the caller falls
back to the explicit write-file locks §10.5 describes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

log = logging.getLogger(__name__)

LogFn = Callable[[str], Awaitable[None]]

DEFAULT_TIMEOUT = 120.0
"""Seconds for a query. Committing takes its own, since it moves the actual bytes."""


class VcsError(RuntimeError):
    """The tool is missing, refused the operation, or answered with something unusable."""


ChangeKind = str
"""``added`` | ``modified`` | ``deleted`` | ``renamed`` | ``copied`` | ``untracked`` |
``conflicted`` | ``unknown`` — normalised across backends so the UI and node outputs do
not have to know one system's status letters."""

CHANGE_KINDS: frozenset[str] = frozenset(
    {
        "added",
        "modified",
        "deleted",
        "renamed",
        "copied",
        "untracked",
        "conflicted",
        "unknown",
    }
)
"""The vocabulary above, as data — it is a **closed** set.

``VcsChangeKind`` in ``frontend/src/api/types.ts`` is this union and the Explorer colours
files by it, so ``/api/vcs/status`` maps anything outside this set to ``unknown`` rather
than passing a third-party backend's private word through to a client that cannot render
it."""


@dataclass(slots=True)
class FileChange:
    path: str
    """Relative to :attr:`VcsStatus.root` — **not** necessarily to the managed project.

    That distinction is load-bearing and easy to miss: the two are the same directory in
    the common case and differ the moment the project is a subdirectory of a bigger
    repository. Anything showing these paths against the project's own tree has to
    re-base them first; ``GET /api/vcs/status`` is where that happens for the UI.

    Some backends answer with absolute paths instead (``cm`` does), so a consumer must
    handle both. Forward slashes where the tool gives a choice, otherwise verbatim: a
    path is a key to look a file up by, and rewriting it invents one the tool never said.
    """

    kind: ChangeKind = "unknown"
    staged: bool = False
    """Already marked for the next commit. Always True where the concept does not exist."""

    origin: str | None = None
    """Previous path for a rename or copy."""

    insertions: int | None = None
    deletions: int | None = None
    """Lines added and removed, or ``None`` where the backend does not count them.

    ``None`` and ``0`` are different answers and the UI shows them differently: nothing
    known versus a file that changed without gaining or losing a line. A binary file is
    ``None`` on both, with :attr:`binary` saying why."""

    binary: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "staged": self.staged,
            "origin": self.origin,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "binary": self.binary,
        }


@dataclass(slots=True)
class VcsStatus:
    backend: str

    root: Path
    """The **workspace** root as the backend understands it, which may sit above the
    managed project — the enclosing repository for a project inside a monorepo, or a
    Plastic workspace the project is one directory of. It is the frame every
    :attr:`FileChange.path` below is expressed in, which is the only reason it is here."""

    branch: str | None = None
    revision: str | None = None
    changes: list[FileChange] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.changes

    def to_json(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "root": str(self.root),
            "branch": self.branch,
            "revision": self.revision,
            "clean": self.clean,
            "changes": [change.to_json() for change in self.changes],
        }


@dataclass(slots=True)
class CommitResult:
    """What a commit did — ``committed=False`` when there was nothing to commit.

    A pipeline that regenerates an asset which happens to be byte-identical must not fail
    the run, so "no changes" is an outcome rather than an error.
    """

    committed: bool
    revision: str | None = None
    """Commit sha, changeset id, changelist number — whatever the backend calls it."""

    branch: str | None = None
    files: list[str] = field(default_factory=list)
    message: str = ""
    skipped: list[str] = field(default_factory=list)
    """Paths the caller asked for that the workspace did not list as changed, and so were
    left out rather than handed to a tool that refuses them (a task's commit, §10.4)."""

    def to_json(self) -> dict[str, Any]:
        return {
            "committed": self.committed,
            "revision": self.revision,
            "branch": self.branch,
            "files": list(self.files),
            "message": self.message,
            "skipped": list(self.skipped),
        }


def project_relative(raw: str, *, workspace: Path, project: Path) -> str | None:
    """One path in the workspace's frame, as a project-relative one — or ``None`` if it
    lies outside the project.

    A backend reports paths against its own workspace root (:attr:`VcsStatus.root`), which
    is not always the managed project: ``git status --porcelain`` is repository-relative
    whatever directory it ran in, so a project at ``<repo>/game/`` gets a ``game/`` on the
    front of everything; ``cm status`` prints **absolute** paths. Both reach the UI and the
    task commit in the *project's* frame, and this is the one rule that puts them there.
    When the workspace does not contain the project at all there is no relationship to
    re-base along, and the path is left as it is.
    """
    try:
        offset = project.relative_to(workspace)
    except ValueError:
        return raw  # the workspace does not contain the project — nothing to do
    # `relative_to` of a path against itself is `.`, whose parts are empty: no prefix to
    # strip, and every relative path is already in the project's frame.
    prefix = offset.as_posix() + "/" if offset.parts else ""

    if Path(raw).is_absolute():
        # Comparison through Path is what makes the Windows case rules apply rather than
        # a naive string match.
        try:
            return Path(raw).relative_to(project).as_posix()
        except ValueError:
            return None

    candidate = raw.replace("\\", "/")
    # normcase folds case *and* separators on Windows, so both sides get the same
    # treatment and the slice below still lands on the original string's boundary.
    if not os.path.normcase(candidate).startswith(os.path.normcase(prefix)):
        return None
    return candidate[len(prefix) :]


@dataclass(slots=True)
class IsolatedWorkspace:
    """A second checkout one agent task works in, merged back through review (§10.5)."""

    path: Path
    branch: str


@dataclass(slots=True)
class ToolResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def output(self) -> str:
        """Both streams, for an error message — tools disagree on which one they use."""
        parts = [part for part in (self.stdout.strip(), self.stderr.strip()) if part]
        return "\n".join(parts)

    def lines(self) -> list[str]:
        return [line for line in self.stdout.splitlines() if line.strip()]


async def run_tool(
    program: str,
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float | None = DEFAULT_TIMEOUT,
    log_fn: LogFn | None = None,
    env: dict[str, str] | None = None,
) -> ToolResult:
    """Run a VCS command line and collect its output.

    Output is captured whole rather than pumped line by line: these tools print a bounded
    handful of lines, and ``communicate()`` sidesteps the ``readline`` buffer trap that
    ``engine/executors.py`` documents. Cancelling the surrounding task — a cancelled run,
    a shutdown — kills the process rather than orphaning it, which is the guarantee an
    out-of-process node would get from the engine for free.
    """
    command = [program, *args]
    if log_fn is not None:
        await log_fn(f"$ {' '.join(command)}")

    try:
        process = await asyncio.create_subprocess_exec(
            program,
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(env or {})} if env else None,
        )
    except FileNotFoundError as exc:
        raise VcsError(f"{program!r} is not on PATH") from exc
    except OSError as exc:
        raise VcsError(f"Could not run {program!r}: {exc}") from exc

    async def terminate() -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    try:
        raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError as exc:
        await terminate()
        raise VcsError(f"{program} {' '.join(args)} did not finish within {timeout}s") from exc
    except asyncio.CancelledError:
        await terminate()
        raise

    result = ToolResult(
        code=process.returncode or 0,
        stdout=raw_out.decode("utf-8", errors="replace"),
        stderr=raw_err.decode("utf-8", errors="replace"),
    )
    if log_fn is not None:
        for line in result.output.splitlines():
            await log_fn(line)
    return result


class VcsBackend(ABC):
    """One version control system, driven against one workspace root.

    Instances are cheap: ``__init__`` only records where and how to run, so the resolver
    can build every registered backend and ask each whether it claims the project.
    """

    id: ClassVar[str]
    """Stable identifier — ``git``, ``plastic``. Matches ``config.vcs.backend``."""

    name: ClassVar[str]
    """Shown in the UI."""

    program: ClassVar[str] = ""
    """Command-line tool this backend drives, overridable per project in settings."""

    executable_setting: ClassVar[str] = "executable"
    """Settings key holding an absolute path to ``program`` when it is not on PATH."""

    supports_isolation: ClassVar[bool] = False
    """Whether :meth:`create_workspace` can give an agent task its own checkout (§10.5)."""

    def __init__(self, root: Path, settings: dict[str, Any] | None = None) -> None:
        self.root = Path(root)
        self.settings = dict(settings or {})

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<{type(self).__name__} root={self.root}>"

    # ---- discovery -------------------------------------------------------------

    @property
    def executable(self) -> str:
        configured = str(self.settings.get(self.executable_setting) or "").strip()
        return configured or self.program

    def is_available(self) -> tuple[bool, str]:
        """(usable, human-readable reason) — the tool being installed, not the project."""
        program = self.executable
        if not program:
            return False, f"{self.name} declares no command-line tool"
        if Path(program).is_absolute():
            if Path(program).exists():
                return True, ""
            return False, f"{program} does not exist"
        if shutil.which(program) is None:
            return False, f"{program!r} is not on PATH"
        return True, ""

    @abstractmethod
    async def detect(self) -> bool:
        """Whether :attr:`root` is under this system. Must not raise."""

    # ---- inspection ------------------------------------------------------------

    @abstractmethod
    async def current_branch(self) -> str | None:
        """Branch name, or ``None`` where the backend has no such concept."""

    async def revision(self) -> str | None:
        """Where the workspace currently sits — commit sha, changeset id, changelist.

        Declared here, and deliberately not abstract: it is genuinely optional, and a
        backend that has no such concept (or cannot answer without a server round trip)
        returns ``None`` and the UI shows no revision. What the declaration buys is that
        ``/api/vcs`` can ask *any* backend — before this, ``revision`` existed only on
        :class:`~voxarium_studio.vcs.git.GitBackend`, so a plugin backend that had one was never
        asked for it.
        """
        return None

    @abstractmethod
    async def status(self) -> VcsStatus:
        """Everything changed in the workspace since the last commit."""

    @abstractmethod
    async def diff(self, path: Path | str | None = None) -> str:
        """A human-readable summary of uncommitted changes, for a reviewer to read."""

    # ---- writing ---------------------------------------------------------------

    @abstractmethod
    async def add(
        self, paths: Sequence[str], *, recursive: bool = True, log_fn: LogFn | None = None
    ) -> list[str]:
        """Put new files under version control. Returns what is now pending commit."""

    @abstractmethod
    async def commit(
        self,
        message: str,
        *,
        paths: Sequence[str] | None = None,
        add_untracked: bool = False,
        timeout: float | None = None,
        log_fn: LogFn | None = None,
    ) -> CommitResult:
        """Commit the workspace, or just ``paths``.

        Returns ``CommitResult(committed=False)`` when nothing matched — see the note on
        :class:`CommitResult`. Anything the tool actually rejected raises :class:`VcsError`.
        """

    async def workspace_changes(self, path: Path | str) -> VcsStatus:
        """What one isolated workspace has done, for a reviewer to read (§10.5).

        The default answer is that workspace's own uncommitted state, obtained by pointing
        a second instance of this backend at it — which is exactly what an isolated
        workspace is, a checkout of its own, so the ordinary :meth:`status` is already the
        right question asked in the right place.

        A backend that can also see work the agent *committed* inside the workspace should
        override this and include it. The distinction matters more than it sounds: an agent
        that commits its own work would otherwise leave a reviewer looking at an empty list
        under the heading "changed files", which reads as "this task did nothing".
        """
        return await type(self)(Path(path), dict(self.settings)).status()

    async def workspace_patch(self, path: Path | str, file: str | None = None) -> str:
        """The change itself, as text — the whole workspace, or one file of it.

        Distinct from :meth:`diff`, which is a *summary* for a human to glance at; this is
        meant to be rendered line by line, so an implementation that can produce a real
        patch should. The default falls back to the summary rather than to nothing.
        """
        inner = type(self)(Path(path), dict(self.settings))
        return await inner.diff(Path(path) / file if file else None)

    # ---- isolation (§10.5) -----------------------------------------------------

    async def create_workspace(
        self, path: Path, branch: str
    ) -> IsolatedWorkspace | None:  # pragma: no cover - overridden where supported
        """Check the project out again at ``path`` on its own branch.

        ``None`` means unavailable — no isolation for this project, so the caller
        serialises overlapping tasks instead. Backends that leave
        ``supports_isolation`` False never get called.
        """
        return None

    async def remove_workspace(
        self, path: Path, *, force: bool = False
    ) -> None:  # pragma: no cover - overridden where supported
        return None

    # ---- helpers for implementations -------------------------------------------

    async def run(
        self,
        *args: str,
        cwd: Path | None = None,
        timeout: float | None = DEFAULT_TIMEOUT,
        log_fn: LogFn | None = None,
        check: bool = False,
    ) -> ToolResult:
        result = await run_tool(
            self.executable, args, cwd=cwd or self.root, timeout=timeout, log_fn=log_fn
        )
        if check and not result.ok:
            raise VcsError(
                f"{self.executable} {' '.join(args)} failed with code {result.code}:\n"
                f"{result.output or '(no output)'}"
            )
        return result

    def relative(self, path: Path | str) -> str:
        """Project-relative, forward-slashed — what node outputs and the UI show."""
        candidate = Path(path)
        with contextlib.suppress(ValueError):
            candidate = candidate.relative_to(self.root)
        return candidate.as_posix()

    def to_json(self) -> dict[str, Any]:
        available, reason = self.is_available()
        return {
            "id": self.id,
            "name": self.name,
            "root": str(self.root),
            "available": available,
            "reason": reason,
            "supports_isolation": self.supports_isolation,
        }


__all__ = [
    "CHANGE_KINDS",
    "ChangeKind",
    "CommitResult",
    "DEFAULT_TIMEOUT",
    "FileChange",
    "IsolatedWorkspace",
    "LogFn",
    "ToolResult",
    "VcsBackend",
    "VcsError",
    "VcsStatus",
    "project_relative",
    "run_tool",
]
