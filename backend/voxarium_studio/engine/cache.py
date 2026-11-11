"""Content-addressed memoisation of node outputs (§8.2).

The key covers node type, node type version, resolved inputs/params and the *content*
of any input files. Re-running a single node therefore reuses upstream results instead
of recomputing the graph — the Make/Bazel-style incrementality called for in §6.3,
implemented as a plain SQLite table rather than an external dependency.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

from sqlmodel import select

from ..models.base import utcnow
from ..models.pipeline import NodeCacheEntry
from ..schemas.nodes import NodeTypeSchema

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_DIGEST_CACHE_SIZE = 4096
_file_digest_cache: OrderedDict[tuple[str, int, int], str] = OrderedDict()
"""Bounded: the key includes mtime, so every rewrite of an artifact adds an entry that
can never be hit again. Unbounded, a long-lived session hashing generated assets grows
this forever."""


def file_digest(path: Path) -> str:
    """SHA-256 of a file, memoised on (path, size, mtime) to avoid repeat hashing."""
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _file_digest_cache.get(key)
    if cached is not None:
        _file_digest_cache.move_to_end(key)
        return cached

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return "unreadable"
    value = digest.hexdigest()
    _file_digest_cache[key] = value
    while len(_file_digest_cache) > _DIGEST_CACHE_SIZE:
        _file_digest_cache.popitem(last=False)
    return value


def _canonical(value: Any, *, project_root: Path) -> Any:
    """Normalise a value for hashing; file paths collapse to their content digest."""
    if isinstance(value, dict):
        return {k: _canonical(value[k], project_root=project_root) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v, project_root=project_root) for v in value]
    if isinstance(value, str) and len(value) < 4096:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        if is_file:
            return {"__file__": candidate.name, "sha256": file_digest(candidate)}
    return value


def compute_cache_key(
    schema: NodeTypeSchema,
    inputs: dict[str, Any],
    params: dict[str, Any],
    *,
    project_root: Path,
) -> str:
    payload = {
        "node_type": schema.id,
        "version": schema.version,
        "execution": schema.execution.model_dump(mode="json"),
        "inputs": _canonical(inputs, project_root=project_root),
        "params": _canonical(params, project_root=project_root),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def lookup(
    session, cache_key: str, *, project_root: Path | None = None
) -> dict[str, Any] | None:
    """Return memoised outputs, dropping the entry if its output files vanished.

    ``project_root`` is what a relative output path is relative to — the same frame
    :func:`_canonical` reads an input path in, and not the server's working directory.
    """
    entry = await session.get(NodeCacheEntry, cache_key)
    if entry is None:
        return None
    if not _outputs_still_valid(entry.outputs, project_root):
        log.debug("Cache entry %s references missing files; discarding", cache_key[:12])
        await session.delete(entry)
        return None
    entry.hit_count += 1
    entry.last_used_at = utcnow()
    session.add(entry)
    return dict(entry.outputs)


def _outputs_still_valid(outputs: dict[str, Any], project_root: Path | None = None) -> bool:
    """A cached artifact path is worthless if the file was deleted since."""
    for value in outputs.values():
        if isinstance(value, str) and value.startswith("__voxarium_studio_file__:"):
            path = Path(value.removeprefix("__voxarium_studio_file__:"))
            if project_root is not None and not path.is_absolute():
                path = project_root / path
            if not path.exists():
                return False
    return True


async def store(
    session,
    cache_key: str,
    node_type: str,
    outputs: dict[str, Any],
    *,
    run_id: str | None = None,
) -> None:
    entry = await session.get(NodeCacheEntry, cache_key)
    if entry is None:
        entry = NodeCacheEntry(cache_key=cache_key, node_type=node_type)
    entry.outputs = outputs
    entry.run_id = run_id
    entry.last_used_at = utcnow()
    session.add(entry)


async def clear(session, node_type: str | None = None) -> int:
    statement = select(NodeCacheEntry)
    if node_type:
        statement = statement.where(NodeCacheEntry.node_type == node_type)
    result = await session.exec(statement)
    entries = list(result.all())
    for entry in entries:
        await session.delete(entry)
    return len(entries)
