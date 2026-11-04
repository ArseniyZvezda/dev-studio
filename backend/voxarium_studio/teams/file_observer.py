"""Content observations during a shared-workspace run (§10.4), never authorship.

Independent of Explorer's lossy UI events. Hashes are streamed off the event loop;
unchanged stat signatures reuse their digest. Bounded polling detects changes, and a
final walk forces a rehash. No file contents or global state are retained.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Fingerprint:
    stamp: tuple[int, int, int, int]
    digest: bytes


class ScanLimit(Exception):
    """An incomplete snapshot is never evidence of a creation or deletion (§10.4)."""


@dataclass
class Scan:
    deadline: float
    cancel: threading.Event = field(default_factory=threading.Event)
    files: int = 0
    entries: int = 0
    bytes_read: int = 0

    def check(self) -> None:
        # Cancelling to_thread alone does not stop its thread. Check between filesystem
        # operations and read chunks as well as enforcing the deadline on the event loop.
        if self.cancel.is_set():
            raise ScanLimit("scan cancelled")
        if time.monotonic() >= self.deadline:
            raise ScanLimit("time limit reached")


class FileObserver:
    # Auxiliary evidence must not indefinitely delay an agent or its cleanup (§10.4).
    SCAN_TIMEOUT = 5.0
    MAX_BYTES = 128 * 1024 * 1024
    MAX_ENTRIES = 100_000
    CHUNK_SIZE = 256 * 1024
    PROGRESS_INTERVAL = 1.0
    POLL_INTERVAL = 1.0

    def __init__(
        self,
        root: Path,
        ignore: list[str],
        record: Callable[[list[str], str | None], Awaitable[None]],
        *,
        progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.ignore = set(ignore) | {".git", ".hg", ".svn", ".plastic"}
        self.record = record
        self.progress = progress
        self.state: dict[str, Fingerprint] = {}
        self.reported: set[str] = set()
        self.warnings: set[str] = set()
        self.unreadable: set[str] = set()
        self.worker: asyncio.Task | None = None
        self.stop = asyncio.Event()
        self.disabled = False
        self._scan: Scan | None = None

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

    def snapshot(self, force: set[str], scan: Scan) -> tuple[dict[str, Fingerprint], list[str]]:
        result: dict[str, Fingerprint] = {}
        errors: list[str] = []
        previous_state = self.state

        def unreadable(path: Path) -> None:
            relative = path.relative_to(self.root).as_posix()
            errors.append(relative)
            # Inaccessible is not deleted. Preserve known files below an unreadable tree.
            for name, value in previous_state.items():
                scan.check()
                if relative == "." or name == relative or name.startswith(relative + "/"):
                    result[name] = value

        def walk(directory: Path) -> None:
            scan.check()
            try:
                with os.scandir(directory) as entries:
                    iterator = iter(entries)
                    while True:
                        scan.check()
                        entry = next(iterator, None)
                        if entry is None:
                            break
                        scan.check()
                        scan.entries += 1
                        if scan.entries > self.MAX_ENTRIES:
                            raise ScanLimit(f"entry limit reached ({self.MAX_ENTRIES:,})")
                        path = Path(entry.path)
                        if not self.included(path):
                            continue
                        try:
                            # Do not traverse links/junctions out of the managed project.
                            if path.is_symlink():
                                continue
                            scan.check()
                            if path.is_junction():
                                continue
                            scan.check()
                            if entry.is_dir(follow_symlinks=False):
                                walk(path)
                                continue
                            scan.check()
                            if entry.is_file(follow_symlinks=False):
                                name = path.relative_to(self.root).as_posix()
                                # Windows DirEntry.stat() may report st_ino=0 while
                                # Path.stat() reports the real file id; compare like calls.
                                scan.check()
                                stat = path.stat(follow_symlinks=False)
                                stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
                                scan.check()
                                previous = previous_state.get(name)
                                if previous and previous.stamp == stamp and name not in force:
                                    result[name] = previous
                                    scan.files += 1
                                    continue
                                if stat.st_size > self.MAX_BYTES - scan.bytes_read:
                                    raise ScanLimit(f"read limit reached ({self.MAX_BYTES:,} bytes)")
                                with path.open("rb") as source:
                                    digest = hashlib.sha256()
                                    remaining = stat.st_size
                                    while remaining:
                                        scan.check()
                                        chunk = source.read(min(self.CHUNK_SIZE, remaining))
                                        scan.bytes_read += len(chunk)
                                        scan.check()
                                        if not chunk:
                                            break
                                        digest.update(chunk)
                                        remaining -= len(chunk)
                                scan.check()
                                after = path.stat(follow_symlinks=False)
                                if stamp != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
                                    unreadable(path)
                                    continue
                                result[name] = Fingerprint(stamp, digest.digest())
                                scan.files += 1
                        except OSError:
                            unreadable(path)
            except OSError:
                unreadable(directory)

        walk(self.root)
        scan.check()
        return result, errors

    async def _progress(self, stage: str, scan: Scan, started: float) -> None:
        if self.progress is None:
            return
        elapsed = time.monotonic() - started
        data = {
            "stage": stage, "elapsed_s": round(elapsed, 1), "files": scan.files,
            "bytes_read": scan.bytes_read, "entries": scan.entries,
            "timeout_s": self.SCAN_TIMEOUT,
        }
        if stage == "preparing":
            text = (f"Preparing file observation before agent launch "
                    f"(up to {self.SCAN_TIMEOUT:g}s, {self.MAX_BYTES // (1024 * 1024)} MiB read).")
        elif stage == "ready":
            text = f"File observation ready after {elapsed:.1f}s ({scan.files:,} files). Starting agent."
        else:
            text = (f"Preparing file observation: scanning and hashing the project, "
                    f"{elapsed:.1f}s elapsed, {scan.files:,} files, "
                    f"{scan.bytes_read / (1024 * 1024):.1f} MiB read; "
                    f"agent launch waits at most {self.SCAN_TIMEOUT:g}s for this scan.")
        await self.progress(text, data)

    async def reconcile(self, force: set[str] | None = None, *, baseline: bool = False) -> bool:
        if self.disabled:
            return False
        started = time.monotonic()
        scan = Scan(deadline=started + self.SCAN_TIMEOUT)
        self._scan = scan
        job: asyncio.Task | None = None
        try:
            if baseline:
                await self._progress("preparing", scan, started)
            job = asyncio.create_task(asyncio.to_thread(self.snapshot, force or set(), scan))
            while not job.done():
                remaining = scan.deadline - time.monotonic()
                if remaining <= 0:
                    raise ScanLimit(f"time limit reached ({self.SCAN_TIMEOUT:g}s)")
                done, _ = await asyncio.wait({job}, timeout=min(remaining, self.PROGRESS_INTERVAL))
                if not done and baseline and time.monotonic() < scan.deadline:
                    await self._progress("waiting", scan, started)
            current, errors = job.result()
        except ScanLimit as exc:
            self.disabled = True
            self.stop.set()
            scan.cancel.set()
            phase = "Preparation" if baseline else "Reconciliation"
            await self.record([], (
                f"File observation incomplete: {phase.lower()} stopped because {exc} "
                f"after {time.monotonic() - started:.1f}s "
                f"({scan.files:,} files, {scan.bytes_read:,} bytes read). "
                "Disk observation is disabled for this run; the incomplete snapshot was discarded. "
                "The agent can continue; changes reported by tools remain available."
            ))
            return False
        except BaseException:
            self.disabled = True
            self.stop.set()
            raise
        finally:
            scan.cancel.set()
            # Never wait for a blocked OS call. Its worker checks cancel before the next
            # operation and cannot publish or replace the baseline after this returns.
            if job is not None:
                if not job.done():
                    job.cancel()
                # Also consume an exception if the scan finished while a progress
                # callback was being cancelled, before job.result() could be reached.
                await asyncio.gather(job, return_exceptions=True)
            self._scan = None

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
        if baseline:
            await self._progress("ready", scan, started)
        return True

    async def start(self) -> None:
        if await self.reconcile(baseline=True):
            self.worker = asyncio.create_task(self._watch(), name="task-file-observer")

    async def _watch(self) -> None:
        try:
            # §10.4: awatch constructs its recursive native watcher synchronously and
            # its event filter does not prune directories during registration. That
            # unbounded second traversal would bypass the preparation budget entirely.
            while not self.stop.is_set():
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=self.POLL_INTERVAL)
                    break
                except TimeoutError:
                    pass
                if not await self.reconcile():
                    break
        except Exception as exc:
            await self.record([], f"File observation stopped: {exc}")

    async def finish(self) -> None:
        # Let an in-flight reconciliation persist before the bounded final walk.
        self.stop.set()
        if self.worker is not None:
            try:
                await self.worker
            except asyncio.CancelledError:
                # A previously cancelled watcher is harmless; cancelling the task that
                # is finishing must still stop cleanup instead of starting a new scan.
                if asyncio.current_task().cancelling():
                    raise
        await self.reconcile(set(self.state))

    async def cancel(self) -> None:
        """Stop observation without starting a final traversal on task cancellation."""
        self.disabled = True
        self.stop.set()
        if self._scan is not None:
            self._scan.cancel.set()
        if self.worker is not None:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
