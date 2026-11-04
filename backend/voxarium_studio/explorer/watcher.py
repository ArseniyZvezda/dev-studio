"""Filesystem watcher that keeps the Explorer tree live (§6.2).

Events are coalesced per batch by ``watchfiles`` already; on top of that the watcher
ignores the Studio's own high-churn directories (runs/, assets/, the SQLite WAL) so a
pipeline writing logs does not flood the UI with change events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from watchfiles import Change, awatch

from ..core.config import AppConfig
from ..core.paths import ProjectPaths
from ..ws.bus import EventBus
from ..ws.events import EventType

log = logging.getLogger(__name__)

CHANGE_NAMES = {Change.added: "added", Change.modified: "modified", Change.deleted: "deleted"}
MAX_EVENT_PATHS = 200


class FileWatcher:
    def __init__(self, paths: ProjectPaths, config: AppConfig, bus: EventBus) -> None:
        self.paths = paths
        self.config = config
        self.bus = bus
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def _should_ignore(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.paths.root)
        except (ValueError, OSError):
            return True
        parts = relative.parts
        if not parts:
            return True
        ignore = set(self.config.explorer.ignore)
        if any(part in ignore for part in parts):
            return True
        if parts[0] == self.paths.data.name:
            # Watch user-editable definitions; ignore the churny runtime directories.
            if len(parts) < 2:
                return True
            return parts[1] in {"runs", "assets", "worktrees"} or parts[1].startswith("app.db")
        return False

    def _filter(self, change: Change, path: str) -> bool:
        return not self._should_ignore(Path(path))

    async def _run(self) -> None:
        log.info("Watching %s for changes", self.paths.root)
        try:
            async for batch in awatch(
                self.paths.root,
                watch_filter=self._filter,
                stop_event=self._stop,
                debounce=300,
                step=100,
            ):
                changes = []
                for change, raw_path in list(batch)[:MAX_EVENT_PATHS]:
                    path = Path(raw_path)
                    try:
                        relative = path.resolve().relative_to(self.paths.root).as_posix()
                    except (ValueError, OSError):
                        continue
                    changes.append(
                        {
                            "change": CHANGE_NAMES.get(change, "modified"),
                            "path": relative,
                            "is_dir": path.is_dir(),
                        }
                    )
                if changes:
                    await self.bus.publish(
                        EventType.EXPLORER_FILE_CHANGED,
                        {
                            "changes": changes,
                            "truncated": len(batch) > MAX_EVENT_PATHS,
                            # Paths are relative, and during a project switch two watchers
                            # share the bus (§5.1) — so the event says whose they are.
                            "root": str(self.paths.root),
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - watcher must never kill the server
            log.exception("File watcher stopped unexpectedly")

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="explorer-watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
