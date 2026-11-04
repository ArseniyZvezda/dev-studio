"""Version control abstraction — see :mod:`voxarium_studio.vcs.base`.

Resolution order for "what is this project under": an explicit ``vcs.backend`` in
``config.yaml`` wins; otherwise plugin-contributed backends get asked before the built-in
git one. A plugin backend is only present because someone installed and enabled that
plugin for this project, so it is the more specific answer when a Plastic workspace or a
Perforce client happens to also sit inside a git repository.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .base import (
    CHANGE_KINDS,
    ChangeKind,
    CommitResult,
    FileChange,
    IsolatedWorkspace,
    LogFn,
    ToolResult,
    VcsBackend,
    VcsError,
    VcsStatus,
    project_relative,
    run_tool,
)
from .git import GitBackend

log = logging.getLogger(__name__)

BUILTIN_BACKENDS: tuple[type[VcsBackend], ...] = (GitBackend,)

SettingsFn = Callable[[str], dict[str, Any]]
"""Per-backend settings lookup — for a plugin backend, that plugin's project settings."""


def candidate_backends(
    extra: Sequence[type[VcsBackend]] = (),
) -> list[type[VcsBackend]]:
    """Plugin backends first, then the built-ins, de-duplicated by id."""
    ordered: dict[str, type[VcsBackend]] = {}
    for backend in (*extra, *BUILTIN_BACKENDS):
        ordered.setdefault(backend.id, backend)
    return list(ordered.values())


async def detect_backend(
    root: Path,
    *,
    extra: Sequence[type[VcsBackend]] = (),
    configured: str | None = None,
    settings_for: SettingsFn | None = None,
) -> VcsBackend | None:
    """Which system the project at ``root`` is under, or ``None`` for none of them.

    ``configured`` names a backend explicitly; an unknown name is logged and ignored
    rather than raising, because a hand-edited ``config.yaml`` must not stop the Studio
    from starting.
    """
    backends = candidate_backends(extra)

    def build(backend_cls: type[VcsBackend]) -> VcsBackend:
        settings = settings_for(backend_cls.id) if settings_for else {}
        return backend_cls(root, settings)

    if configured:
        match = next((b for b in backends if b.id == configured), None)
        if match is None:
            log.warning(
                "config.yaml selects unknown version control backend %r; auto-detecting instead. "
                "Available: %s",
                configured,
                ", ".join(b.id for b in backends),
            )
        else:
            # An explicit choice is honoured even if detection would disagree — the user
            # may be pointing at a workspace the tool cannot see from here yet.
            return build(match)

    for backend_cls in backends:
        backend = build(backend_cls)
        try:
            if await backend.detect():
                return backend
        except Exception as exc:  # a third-party backend must not break start-up
            log.warning("Version control backend %r failed to detect: %s", backend_cls.id, exc)
    return None


__all__ = [
    "BUILTIN_BACKENDS",
    "CHANGE_KINDS",
    "ChangeKind",
    "CommitResult",
    "FileChange",
    "GitBackend",
    "IsolatedWorkspace",
    "LogFn",
    "ToolResult",
    "VcsBackend",
    "VcsError",
    "VcsStatus",
    "candidate_backends",
    "detect_backend",
    "project_relative",
    "run_tool",
]
