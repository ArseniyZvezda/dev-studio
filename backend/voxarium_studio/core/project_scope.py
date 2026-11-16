"""Runtime project identities shared by scoped requests and the legacy adapter (§5.3).

An identity belongs to one backend lifetime, never to a database copied between folders.
Only workspace startup/open publishes descriptors; lookup never opens or selects a project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import AppContext


@dataclass(frozen=True, slots=True)
class ProjectDescriptor:
    """An immutable binding to a successfully started resident context (§5.3.2)."""

    project_id: str
    epoch: str
    root: Path
    ctx: AppContext


def canonical_root(root: str | Path) -> Path:
    """Resolve filesystem aliases, then fold Windows case for registry keys (§5.3.2)."""
    # Path.resolve follows both Windows junctions and symbolic links. normcase must
    # follow it: alias spelling (including drive-letter case) is not project identity.
    return Path(os.path.normcase(str(Path(root).expanduser().resolve())))


def resolve_project(state: Any, project_id: str) -> ProjectDescriptor:
    """Resolve only a published runtime id, failing closed without a legacy fallback."""
    registry = getattr(state, "project_registry", {})
    if not isinstance(project_id, str):
        raise KeyError(project_id)
    return registry[project_id]


async def ensure_project(
    state: Any, root: str | Path, *, create: bool = False,
) -> ProjectDescriptor:
    """Start/reuse a context without changing legacy selection (§5.3.2).

    Workspace owns lifecycle and its single settings/open lock. The local import keeps
    this identity module usable by request dependencies without a context import cycle.
    """
    from .workspace import ensure_project as ensure

    return await ensure(state, root, create=create)
