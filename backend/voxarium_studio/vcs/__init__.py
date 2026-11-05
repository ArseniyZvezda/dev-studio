"""Independent VCS entities and deterministic project detection (§9.6)."""

from __future__ import annotations

import logging
from collections.abc import Callable
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
from .plastic import PlasticBackend
from .perforce import PerforceBackend

log = logging.getLogger(__name__)

BUILTIN_BACKENDS: tuple[type[VcsBackend], ...] = (PlasticBackend, PerforceBackend, GitBackend)

SettingsFn = Callable[[str], dict[str, Any]]
"""Per-entity settings from config.vcs.settings."""


def candidate_backends() -> list[type[VcsBackend]]:
    """All VCS entities, always available; specific workspaces precede Git (§9.6)."""
    return list(BUILTIN_BACKENDS)


async def detect_backend(
    root: Path,
    *,
    configured: str | None = None,
    settings_for: SettingsFn | None = None,
) -> VcsBackend | None:
    """Which system the project at ``root`` is under, or ``None`` for none of them.

    ``configured`` names a backend explicitly; an unknown name is logged and ignored
    rather than raising, because a hand-edited ``config.yaml`` must not stop the Studio
    from starting.
    """
    backends = candidate_backends()

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
    "PlasticBackend",
    "PerforceBackend",
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
