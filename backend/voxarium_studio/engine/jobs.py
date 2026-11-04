"""Job Engine — the shared async execution substrate (§8.2).

Both pipeline nodes and Teams agent sessions are scheduled through this one engine, so
they share concurrency limits, cancellation and status streaming. Deliberately built on
plain ``asyncio`` rather than Celery/RQ: a locally-run tool should not require a broker.
The interface is narrow enough that a distributed backend could be slotted in later
without touching the API or UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class JobHandle:
    """A running job: its asyncio task plus any child process to kill on cancel."""

    id: str
    kind: str
    task: asyncio.Task[Any] | None = None
    process: asyncio.subprocess.Process | None = None
    children: set[str] = field(default_factory=set)
    meta: dict[str, Any] = field(default_factory=dict)

    async def cancel(self) -> None:
        """Kill the child process first, then cancel the task awaiting it."""
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self.process.kill()
        if self.task is not None and not self.task.done():
            self.task.cancel()


class ResizableSemaphore:
    """A semaphore whose limit can change while holders are in flight.

    ``asyncio.Semaphore`` cannot be resized, and swapping in a new one lets the old
    holders and the new waiters run past the limit together — which for ``gpu: 1`` means
    two jobs on one GPU. Lowering the limit here never revokes a slot already granted; it
    simply stops handing out more until the count falls back under the new value.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._held = 0
        self._condition = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def available(self) -> bool:
        """Whether an ``acquire`` now would be granted without waiting."""
        return self._held < self._limit

    async def set_limit(self, limit: int) -> None:
        async with self._condition:
            self._limit = max(1, limit)
            self._condition.notify_all()

    async def acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._held < self._limit)
            self._held += 1

    async def release(self) -> None:
        async with self._condition:
            self._held = max(0, self._held - 1)
            self._condition.notify_all()


class JobEngine:
    """Concurrency limiter + cancellable job registry."""

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._limits = dict(limits or {"cpu": 4, "gpu": 1, "io": 8, "agent": 2})
        self._semaphores: dict[str, ResizableSemaphore] = {}
        self._jobs: dict[str, JobHandle] = {}

    def _semaphore(self, resource_class: str) -> ResizableSemaphore:
        """Lazily created so semaphores bind to the running loop, not import time."""
        semaphore = self._semaphores.get(resource_class)
        if semaphore is None:
            limit = self._limits.get(resource_class, self._limits.get("cpu", 4))
            semaphore = ResizableSemaphore(limit)
            self._semaphores[resource_class] = semaphore
        return semaphore

    def update_limits(self, limits: dict[str, int]) -> None:
        """Apply new limits, including to classes that already have jobs running."""
        self._limits.update(limits)
        for resource_class, limit in limits.items():
            semaphore = self._semaphores.get(resource_class)
            if semaphore is None:
                continue
            # Config reloads arrive from a request handler, so there is a loop to schedule on.
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(semaphore.set_limit(limit))

    @property
    def limits(self) -> dict[str, int]:
        return dict(self._limits)

    @contextlib.asynccontextmanager
    async def slot(
        self,
        resource_class: str,
        on_wait: Callable[[int], Awaitable[None]] | None = None,
    ):
        """Hold a slot for the given resource class for the duration of the block.

        ``on_wait`` is awaited with the current limit when every slot is taken, before the
        wait begins — so a caller whose work already looks started (an agent task is
        ``in_progress`` by now) can say why nothing is happening. A wait is otherwise
        indistinguishable from a hang.
        """
        semaphore = self._semaphore(resource_class)
        if on_wait is not None and not semaphore.available:
            await on_wait(semaphore.limit)
        await semaphore.acquire()
        try:
            yield
        finally:
            await semaphore.release()

    # ---- job registry ----

    def register(self, job_id: str, kind: str, **meta: Any) -> JobHandle:
        handle = JobHandle(id=job_id, kind=kind, meta=meta)
        self._jobs[job_id] = handle
        return handle

    def submit(
        self,
        job_id: str,
        kind: str,
        coro_factory: Callable[[JobHandle], Awaitable[Any]],
        **meta: Any,
    ) -> JobHandle:
        """Start a background job and keep a handle for status/cancellation."""
        if job_id in self._jobs:
            raise ValueError(f"Job {job_id!r} is already running")
        handle = self.register(job_id, kind, **meta)

        async def _wrapper() -> Any:
            try:
                return await coro_factory(handle)
            finally:
                self._jobs.pop(job_id, None)

        handle.task = asyncio.create_task(_wrapper(), name=f"{kind}:{job_id}")
        # An immediate Stop can cancel before _wrapper even enters its try/finally.
        # Release that id too, so a chat can be started again (§10.11).
        def forget(_task: asyncio.Task) -> None:
            if self._jobs.get(job_id) is handle:
                self._jobs.pop(job_id, None)
        handle.task.add_done_callback(forget)
        return handle

    def get(self, job_id: str) -> JobHandle | None:
        return self._jobs.get(job_id)

    def is_running(self, job_id: str) -> bool:
        handle = self._jobs.get(job_id)
        return handle is not None and handle.task is not None and not handle.task.done()

    def jobs(self) -> list[JobHandle]:
        return list(self._jobs.values())

    async def cancel(self, job_id: str) -> bool:
        """Cancel a job and every job it spawned. Returns False if it was not running."""
        handle = self._jobs.get(job_id)
        if handle is None:
            return False
        for child_id in list(handle.children):
            child = self._jobs.get(child_id)
            if child is not None:
                await child.cancel()
        await handle.cancel()
        return True

    async def shutdown(self, *, timeout: float = 10.0) -> None:
        """Cancel everything in flight and wait briefly for tasks to unwind."""
        handles = list(self._jobs.values())
        for handle in handles:
            await handle.cancel()
        tasks = [h.task for h in handles if h.task is not None and not h.task.done()]
        if not tasks:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait(tasks, timeout=timeout)
        self._jobs.clear()
