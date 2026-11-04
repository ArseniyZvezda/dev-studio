"""Live task scratch files, independent of transcript and version control (§10.4)."""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any

from ..core.paths import DATA_DIRNAME, ProjectPaths
from ..models.teams import TeamTask
from .temp import task_temp_dir

PREVIEW_LIMIT = 512 * 1024


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def is_temp_path(path: str, paths: ProjectPaths, root: Path) -> bool:
    """Exclude scratch paths from historical evidence and commit candidates, too."""
    candidate = Path(path.replace("\\", "/"))
    absolute = candidate if candidate.is_absolute() else root / candidate
    for base in (paths.temp, root / DATA_DIRNAME / "temp"):
        # Lexical containment also catches removed files and links pointing out of temp.
        normal = Path(os.path.abspath(absolute))
        if normal.is_relative_to(base) or absolute.resolve().is_relative_to(base.resolve()):
            return True
    return False


def list_temp_files(paths: ProjectPaths, task: TeamTask) -> dict[str, Any]:
    """List all regular files including hidden/ignored ones; never follow links."""
    errors: list[str] = []
    files: list[dict[str, Any]] = []
    try:
        directory = task_temp_dir(paths, task)
    except (ValueError, OSError) as exc:
        return {"temp_root": None, "temp_files": [], "temp_errors": [str(exc)]}

    def onerror(exc: OSError) -> None:
        errors.append(str(exc))

    if directory.exists():
        for parent, dirs, names in os.walk(directory, followlinks=False, onerror=onerror):
            base = Path(parent)
            dirs[:] = [name for name in dirs if not _linked(base / name)]
            for name in names:
                file = base / name
                try:
                    if _linked(file) or not file.is_file():
                        continue
                    files.append({
                        "path": file.relative_to(paths.root).as_posix(),
                        "kind": "untracked",
                        "staged": False,
                        "insertions": None,
                        "deletions": None,
                        "outside": False,
                    })
                except OSError as exc:
                    onerror(exc)
    return {
        "temp_root": str(directory),
        "temp_files": sorted(files, key=lambda file: file["path"]),
        "temp_errors": errors,
    }


def temp_patch(paths: ProjectPaths, task: TeamTask, path: str) -> str:
    """Render a bounded text scratch file in the existing patch viewer, without VCS."""
    directory = task_temp_dir(paths, task)
    candidate = Path(path.replace("\\", "/"))
    file = Path(os.path.abspath(candidate if candidate.is_absolute() else paths.root / candidate))
    if not file.is_relative_to(directory) or file == directory:
        raise ValueError("This file is outside the task's temporary directory.")
    for part in (file, *file.parents):
        if part == directory:
            break
        if _linked(part):
            raise ValueError("Temporary file previews do not follow symbolic links or junctions.")
    if not file.resolve().is_relative_to(directory):
        raise ValueError("This file is outside the task's temporary directory.")
    try:
        with file.open("rb") as stream:
            raw = stream.read(PREVIEW_LIMIT + 1)
    except OSError as exc:
        raise ValueError(f"Could not read temporary file: {exc}") from exc
    if len(raw) > PREVIEW_LIMIT:
        raise ValueError("This temporary file is too large to preview (maximum 512 KiB).")
    label = file.relative_to(paths.root).as_posix()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return f"Binary files /dev/null and b/{label} differ\n"
    if "\0" in content:
        return f"Binary files /dev/null and b/{label} differ\n"
    return "\n".join(difflib.unified_diff(
        [], content.splitlines(), fromfile="/dev/null", tofile=f"b/{label}", lineterm="",
    ))
