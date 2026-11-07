"""Repair stored references after renaming a managed project's data directory (§5)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlmodel import select

from ..engine.executors import FILE_MARKER
from ..models.pipeline import NodeCacheEntry, NodeRun
from ..models.teams import TeamTask
from ..schemas.nodes import PortSchema
from .paths import current_data_path

if TYPE_CHECKING:
    from .context import AppContext


def _port_paths(values: dict[str, Any], ports: list[PortSchema] | None, root: Path) -> dict[str, Any]:
    updated = dict(values)
    keys = values if ports is None else [p.id for p in ports if p.type in ("file", "directory", "path")]
    for key in keys:
        value = values.get(key)
        if not isinstance(value, str):
            continue
        path = current_data_path(value, root)
        if path == value:
            continue
        if ports is None:
            # A disabled or removed plugin has no port schema. Recognise its
            # existing artifacts without interpreting arbitrary output as a path.
            try:
                if not (root / path).exists():
                    continue
            except (OSError, ValueError):
                continue
        updated[key] = path
    return updated


async def finish_data_migration(ctx: AppContext) -> None:
    root = ctx.paths.root
    async with ctx.db.session() as session:
        # Older Studio databases use SQLite's default user_version (0). Keep
        # the version and all row updates in one transaction, independent of
        # when or which process renamed the folder (§5).
        connection = await session.connection()
        if (await connection.exec_driver_sql("PRAGMA user_version")).scalar_one() >= 1:
            return
        # Only operational paths change. Briefs, transcripts and generated text
        # remain the historical record of the task or run.
        async for task in await session.stream_scalars(select(TeamTask).execution_options(yield_per=200)):
            if task.worktree_path:
                task.worktree_path = current_data_path(task.worktree_path, root)
            task.write_paths = [current_data_path(p, root) for p in task.write_paths or []]
            task.commit_files = [current_data_path(p, root) for p in task.commit_files or []]
            task.attachments = [
                {**item, "path": current_data_path(item["path"], root)}
                if isinstance(item, dict) and isinstance(item.get("path"), str) else item
                for item in task.attachments or []
            ]

        async for node in await session.stream_scalars(select(NodeRun).execution_options(yield_per=200)):
            if node.log_path:
                node.log_path = current_data_path(node.log_path, root)
            schema = ctx.registry.node_type(node.node_type)
            node.inputs = _port_paths(node.inputs or {}, schema.inputs if schema else None, root)
            node.outputs = _port_paths(node.outputs or {}, schema.outputs if schema else None, root)

        async for entry in await session.stream_scalars(select(NodeCacheEntry).execution_options(yield_per=200)):
            entry.outputs = {
                key: FILE_MARKER + current_data_path(value.removeprefix(FILE_MARKER), root)
                if isinstance(value, str) and value.startswith(FILE_MARKER) else value
                for key, value in (entry.outputs or {}).items()
            }

        await session.flush()
        await connection.exec_driver_sql("PRAGMA user_version = 1")
