"""Conservative removal of unreferenced pipeline outputs (spec §8.2)."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

import yaml

from ..core.paths import ProjectPaths, current_data_path
from ..schemas.pipeline import PipelineDefinition


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def path_value(value: str, root: Path) -> Path | None:
    try:
        value = value.removeprefix("__voxarium_studio_file__:")
        if not value or len(value) > 4096 or "\n" in value:
            return None
        path = Path(current_data_path(value, root))
        return Path(os.path.abspath(path if path.is_absolute() else root / path))
    except (OSError, ValueError):
        return None


def clear_unused(paths: ProjectPaths, outputs: list[dict]) -> dict[str, int]:
    """Only known outputs are cache. Unknown files and linked directories are untouched.

    Read all definitions before deleting anything: an unreadable/invalid definition is
    a refusal, never permission to delete its inputs. References to directories protect
    their descendants, including inputs of for-each-file nodes.
    """
    assets = paths.assets
    if assets.is_symlink() or assets.is_junction():
        raise ValueError("The assets directory is a link; cache cleanup is unavailable")
    base = assets.resolve()
    references: set[Path] = set()
    definitions = list(paths.pipelines.rglob("*.yaml")) + list(paths.pipelines.rglob("*.yml"))
    pipeline_files = set(definitions)
    definitions += [p for p in (paths.config_file, paths.automations_file) if p.exists()]
    for definition in definitions:
        content = yaml.safe_load(definition.read_text(encoding="utf-8"))
        if not isinstance(content, (dict, list)):
            raise ValueError(f"Cannot safely read references in {definition.name}")
        if definition in pipeline_files:
            if not isinstance(content, dict):
                raise ValueError(f"Invalid pipeline: {definition.name}")
            PipelineDefinition.model_validate({"id": definition.stem, "name": definition.stem, **content})
        for value in strings(content):
            path = path_value(value, paths.root)
            if path is not None:
                if definition not in pipeline_files and not path.resolve().is_relative_to(base):
                    continue
                references.add(path)
                references.add(path.resolve())
    docs = "\n".join(unquote(p.read_text(encoding="utf-8")).replace("\\", "/").lower()
                     for p in paths.docs.rglob("*.md"))
    candidates = {p for output in outputs for value in strings(output)
                  if (p := path_value(value, paths.root)) is not None and p.is_relative_to(base)}
    removed = freed = kept = failed = 0
    for path in sorted(candidates):
        # Resolve again at deletion time, and never traverse a symlink/junction.
        if (not path.is_file() or path.is_symlink() or path.is_junction()
                or any(p.is_symlink() or p.is_junction() for p in path.parents if p != base)
                or not path.resolve().is_relative_to(base)):
            continue
        if any(path == ref or path.is_relative_to(ref) for ref in references) or path.name.lower() in docs:
            kept += 1
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            removed += 1
            freed += size
        except OSError:
            failed += 1
    return {"removed": removed, "bytes": freed, "kept": kept, "failed": failed}
