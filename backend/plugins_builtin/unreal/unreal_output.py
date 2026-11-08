"""Name and publish an Unreal build folder (§9.5)."""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from string import Formatter

DEFAULT_FOLDER_TEMPLATE = "{game}-{platform}-{configuration}"
_BUILD_MARKER = ".voxarium-build"


def build_folder_name(template: str, fields: dict[str, str]) -> str:
    parts = []
    for literal, key, spec, conversion in Formatter().parse(template):
        parts.append(literal)
        if key is not None:
            if key not in fields or spec or conversion:
                raise ValueError(f"Invalid build folder placeholder {{{key}}}. Available: {', '.join(fields)}.")
            parts.append(fields[key])
    name = "".join(parts)
    # The template names one portable leaf, even when the parent is on a network share.
    if (
        not name.strip()
        or name.endswith((".", " "))
        or PureWindowsPath(name).is_reserved()
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
    ):
        raise ValueError(f"Build folder name must be a single valid directory name, got {name!r}")
    return name


def check_destination(folder: Path) -> None:
    if folder.is_symlink() or folder.is_junction():
        raise ValueError(f"Build output is a link: {folder}")
    if folder.exists() and not (
        folder.is_dir() and ((folder / _BUILD_MARKER).is_file() or not any(folder.iterdir()))
    ):
        raise ValueError(f"Build output already exists: {folder}. Choose an empty folder or a previous Studio build.")


def archive_contents(archive: Path, platform: str) -> Path:
    if not archive.is_dir() or not any(path.is_file() for path in archive.rglob("*")):
        raise ValueError("Unreal reported success but produced no archived files.")
    # Older UAT versions can append a cook platform even when -archivedirectory
    # already names it. Unwrap only that layer; a lone .app is part of the build.
    entries = list(archive.iterdir())
    prefixes = ("win64", "windows") if platform == "Win64" else (platform.casefold(),)
    if len(entries) == 1 and entries[0].is_dir():
        name = entries[0].name.casefold()
        if any(
            name in (prefix, prefix + "client", prefix + "server", prefix + "noeditor")
            or name.startswith(prefix + "_")
            for prefix in prefixes
        ):
            return entries[0]
    return archive


def publish(source: Path, destination: Path, temporary: Path) -> None:
    # Rename on the same filesystem. Failed builds never modify the last good result.
    check_destination(destination)
    (source / _BUILD_MARKER).touch()
    backup = temporary / "previous"
    if destination.exists():
        destination.rename(backup)
    try:
        source.rename(destination)
    except OSError:
        if backup.exists():
            try:
                backup.rename(destination)
            except OSError as exc:
                raise RuntimeError(f"Could not restore {destination}. The previous build is at {backup}.") from exc
        raise
