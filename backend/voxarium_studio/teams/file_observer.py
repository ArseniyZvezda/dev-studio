"""Content observations during a shared-workspace run (§10.4), never authorship.

Independent of Explorer's lossy UI events. Hashes are streamed off the event loop;
unchanged stat signatures reuse their digest. Watcher hints force a rehash, and a final
walk reconciles missed events. No file contents or global state are retained.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from watchfiles import awatch


@dataclass(frozen=True)
class Fingerprint:
    stamp: tuple[int, int, int, int]
    digest: bytes


class FileObserver:
    def __init__(
        self,
        root: Path,
        ignore: list[str],
        record: Callable[[list[str], str | None], Awaitable[None]],
    ) -> None:
        self.root = root.resolve()
        self.ignore = set(ignore) | {".git", ".hg", ".svn", ".plastic"}
        self.record = record
        self.state: dict[str, Fingerprint] = {}
        self.reported: set[str] = set()
        self.warnings: set[str] = set()
        self.unreadable: set[str] = set()
        self.worker: asyncio.Task | None = None
        self.stop = asyncio.Event()

    def included(self, path: Path) -> bool:
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return False
        if any(part in self.ignore for part in parts):
            return False
        if len(parts) > 1 and parts[0] == "voxarium_studio":
            # Temp has its own per-task listing; observing the shared tree would attach
            # another task's disposable files to this run's change evidence (§10.4).
            if parts[1] in {"runs", "assets", "temp", "worktrees"} or parts[1].startswith("app.db"):
                return False
        return True

    def snapshot(self, force: set[str]) -> tuple[dict[str, Fingerprint], list[str]]:
        result: dict[str, Fingerprint] = {}
        errors: list[str] = []

        def unreadable(path: Path) -> None:
            relative = path.relative_to(self.root).as_posix()
            errors.append(relative)
            # Inaccessible is not deleted. Preserve known files below an unreadable tree.
            for name, value in self.state.items():
                if relative == "." or name == relative or name.startswith(relative + "/"):
                    result[name] = value

        def walk(directory: Path) -> None:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        if not self.included(path):
                            continue
                        try:
                            # Do not traverse links/junctions out of the managed project.
                            if path.is_symlink() or path.is_junction():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                walk(path)
                            elif entry.is_file(follow_symlinks=False):
                                name = path.relative_to(self.root).as_posix()
                                # Windows DirEntry.stat() may report st_ino=0 while
                                # Path.stat() reports the real file id; compare like calls.
                                stat = path.stat(follow_symlinks=False)
                                stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
                                previous = self.state.get(name)
                                if previous and previous.stamp == stamp and name not in force:
                                    result[name] = previous
                                    continue
                                with path.open("rb") as source:
                                    digest = hashlib.file_digest(source, "sha256").digest()
                                after = path.stat(follow_symlinks=False)
                                if stamp != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
                                    unreadable(path)
                                    continue
                                result[name] = Fingerprint(stamp, digest)
                        except OSError:
                            unreadable(path)
            except OSError:
                unreadable(directory)

        walk(self.root)
        return result, errors

    async def reconcile(self, force: set[str] | None = None, *, baseline: bool = False) -> None:
        current, errors = await asyncio.to_thread(self.snapshot, force or set())
        changed = sorted(
            name for name in self.state.keys() | current.keys()
            if name not in self.reported
            and not (name not in self.state and any(
                prefix == "." or name == prefix or name.startswith(prefix + "/")
                for prefix in self.unreadable
            ))
            and (self.state.get(name).digest if name in self.state else None)
            != (current.get(name).digest if name in current else None)
        ) if not baseline else []
        new_errors = set(errors) - self.warnings
        if changed or new_errors:
            await self.record(changed, "Could not inspect: " + ", ".join(sorted(new_errors)) if new_errors else None)
        self.reported.update(changed)
        self.warnings.update(errors)
        self.unreadable = set(errors)
        self.state = current

    async def start(self) -> None:
        await self.reconcile(baseline=True)
        self.worker = asyncio.create_task(self._watch(), name="task-file-observer")

    async def _watch(self) -> None:
        try:
            async for batch in awatch(
                self.root, stop_event=self.stop, debounce=300, step=100,
                watch_filter=lambda _change, path: self.included(Path(path)),
                rust_timeout=5000, yield_on_timeout=True,
            ):
                force = {Path(path).relative_to(self.root).as_posix() for _, path in batch}
                await self.reconcile(force)
        except Exception as exc:
            await self.record([], f"File observation stopped: {exc}")

    async def finish(self) -> None:
        # Let an in-flight reconciliation persist before the final walk. Cancellation of
        # the agent must not cancel the independent observer's database write.
        self.stop.set()
        if self.worker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        await self.reconcile(set(self.state))
