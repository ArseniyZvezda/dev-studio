"""Disposable files belonging to one task, outside its isolated checkout (§10.4)."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from ..core.paths import ProjectPaths
from ..models.teams import TeamTask
from .skill import rule_path


def task_temp_dir(paths: ProjectPaths, task: TeamTask) -> Path:
    """Return the absolute, stable task directory without following a redirected parent.

    Task numbers are project-wide and never reused. The displayed project code can be
    renamed, so it must not decide where an existing task keeps its temporary files.
    """
    if task.number <= 0:
        raise ValueError("A task needs a persistent number before using temporary files")
    directory = paths.temp / f"TASK-{task.number}"
    for parent in (paths.data, paths.temp, directory):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError(f"Task temporary directory must not use a link: {parent}")
    # Check the final absolute target before any mkdir or recursive deletion, including
    # on Windows where a junction may redirect an otherwise innocent-looking path.
    if directory.resolve() != directory or directory.parent != paths.temp:
        raise ValueError("Task temporary directory is outside the project's temp directory")
    return directory


def ensure_task_temp(paths: ProjectPaths, task: TeamTask) -> Path:
    directory = task_temp_dir(paths, task)
    directory.mkdir(parents=True, exist_ok=True)
    return task_temp_dir(paths, task)


def remove_task_temp(paths: ProjectPaths, task: TeamTask) -> None:
    directory = task_temp_dir(paths, task)
    if directory.exists():
        def remove_readonly(function, name, error):
            candidate = Path(name)
            if (not isinstance(error, PermissionError)
                    or candidate.is_symlink() or candidate.is_junction()
                    or not candidate.resolve().is_relative_to(directory)):
                raise error
            # Git objects and downloaded inputs can carry Windows' read-only bit.
            # Only clear it on a verified entry inside this task's disposable tree.
            os.chmod(candidate, candidate.stat().st_mode | stat.S_IWRITE)
            function(name)

        # Python 3.12 rmtree removes nested symlinks/junctions without descending into
        # their targets. The task directory itself and its parents were checked above.
        shutil.rmtree(directory, onexc=remove_readonly)


def allowed_tools(directory: Path) -> list[str]:
    """Let Claude's file tools use this task's directory from an isolated checkout."""
    where = rule_path(directory)
    return [f"{tool}({where}/**)" for tool in ("Read", "Write", "Edit")] if where else []
