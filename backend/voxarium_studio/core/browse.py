"""Read-only directory listing for the in-app folder browser.

This is the fallback behind the native dialog (:mod:`voxarium_studio.core.picker`) and the only
way to open a project on a Studio that is not sitting on the user's own desktop. It
ranges over the whole filesystem on purpose — the project being opened is by definition
outside the one already open, so `ExplorerService`'s containment rules cannot apply here.

What keeps that in proportion: it returns **directory names only**. No file contents, no
file listing, nothing writable. The token that guards the rest of the API guards this
too, and on a loopback run it shows the user their own folder names.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import has_project_data

log = logging.getLogger(__name__)

MAX_ENTRIES = 2000
"""A directory with more subfolders than this is a machine-generated one; the browser
truncates rather than shipping a list nobody will scroll."""


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    path: Path
    name: str
    is_project: bool
    """Holds a ``.voxarium_studio/`` directory, so opening it resumes rather than initialises."""

    hidden: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.name,
            "is_project": self.is_project,
            "hidden": self.hidden,
        }


def home_directory() -> Path:
    return Path.home()


def list_roots() -> list[DirectoryEntry]:
    """Drive letters on Windows; the filesystem root and the home directory elsewhere."""
    if os.name == "nt":
        return [_entry(Path(drive), label=drive.rstrip("\\")) for drive in _drives()]
    return [_entry(Path("/")), _entry(home_directory())]


def list_directory(path: str | Path | None) -> dict[str, Any]:
    """Subdirectories of ``path``; an empty path lists the roots.

    Unreadable children are skipped rather than failing the whole listing — one
    permission-denied system folder must not make a drive unbrowsable.
    """
    if not path:
        roots = list_roots()
        return {
            "path": "",
            "name": "This computer" if os.name == "nt" else "Filesystem",
            "parent": None,
            "is_project": False,
            "entries": [entry.to_json() for entry in roots],
            "truncated": False,
        }

    target = Path(path).expanduser()
    try:
        target = target.resolve()
    except OSError as exc:
        raise FileNotFoundError(f"Cannot resolve {path}: {exc}") from exc
    if not target.is_dir():
        raise NotADirectoryError(f"Not a directory: {target}")

    entries: list[DirectoryEntry] = []
    truncated = False
    try:
        with os.scandir(target) as scanner:
            for item in scanner:
                try:
                    if not item.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                entries.append(_entry(Path(item.path), hidden=_is_hidden(item)))
    except PermissionError as exc:
        raise PermissionError(f"Not allowed to read {target}") from exc
    except OSError as exc:
        raise FileNotFoundError(f"Cannot read {target}: {exc}") from exc

    entries.sort(key=lambda entry: entry.name.casefold())
    parent = target.parent
    return {
        "path": str(target),
        "name": target.name or str(target),
        # A drive root's parent is itself, which would give the browser an "up" link that
        # goes nowhere; the empty string sends it to the list of drives instead.
        "parent": "" if parent == target else str(parent),
        "is_project": _is_project(target),
        "entries": [entry.to_json() for entry in entries],
        "truncated": truncated,
    }


def inspect(path: str | Path) -> dict[str, Any]:
    """What the Open dialog needs to know about a typed path before enabling its button."""
    candidate = Path(str(path)).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        return {
            "path": str(path),
            "name": "",
            "exists": False,
            "is_dir": False,
            "is_project": False,
        }
    exists = candidate.exists()
    is_dir = candidate.is_dir()
    return {
        "path": str(candidate),
        "name": candidate.name or str(candidate),
        "exists": exists,
        "is_dir": is_dir,
        "is_project": is_dir and _is_project(candidate),
    }


def _entry(path: Path, *, label: str | None = None, hidden: bool = False) -> DirectoryEntry:
    return DirectoryEntry(
        path=path,
        name=label or path.name or str(path),
        is_project=_is_project(path),
        hidden=hidden,
    )


def _is_project(path: Path) -> bool:
    try:
        return has_project_data(path)
    except OSError:
        return False


def _is_hidden(item: os.DirEntry[str]) -> bool:
    if item.name.startswith("."):
        return True
    try:
        attributes = item.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


def _drives() -> list[str]:
    if hasattr(os, "listdrives"):
        try:
            return list(os.listdrives())
        except OSError:
            log.debug("os.listdrives() failed; falling back to probing letters", exc_info=True)
    found: list[str] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:\\"
        try:
            if os.path.exists(drive):
                found.append(drive)
        except OSError:
            continue
    return found
