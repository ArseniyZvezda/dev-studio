"""Waiting for an agent slot, out loud (§10.3).

Every agent session — a task run or a chat turn — holds one ``agent`` slot of the job
engine while it talks to the model, and ``concurrency.agents`` (two by default) bounds how
many do at once. Past that a session queues, and by then a task is already ``in_progress``
with its workdir line written, so a queued run looked exactly like one that had hung on
"Starting in …". This says so in the transcript instead: one entry when the wait begins
and one when it ends, which are the tail line on the board card and in the dialog meanwhile.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from ..engine.jobs import JobEngine

#: ``record_message`` / ``ChatService.record`` — the same keyword shape on both feeds.
Record = Callable[..., Awaitable[Any]]


class AgentSlotLease:
    """One session's slot, lent back while it waits on Studio subagents (§10.3).

    MCP may execute several wait calls concurrently. All of their bodies can wait without
    a slot, but none returns normally until the last wait finishes and this lease owns a
    slot again. Acquisition belongs to the lease, rather than one tool-call task, so a
    cancelled waiter cannot abandon an acquired slot or strand the other waiters.
    """

    def __init__(self, acquire: Callable[[], contextlib.AbstractAsyncContextManager[None]]) -> None:
        self._acquire = acquire
        self._slot: contextlib.AbstractAsyncContextManager[None] | None = None
        self._acquiring: asyncio.Task[None] | None = None
        self._releasing: asyncio.Task[Any] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._suspensions = 0
        self._owners: dict[asyncio.Task[Any], int] = {}
        self._closed = False
        self._error: BaseException | None = None

    async def start(self) -> None:
        self._ensure_acquiring()
        await self._wait_ready()

    def _ensure_acquiring(self) -> None:
        if (self._closed or self._error is not None
                or self._slot is not None or self._acquiring is not None):
            return
        if self._suspensions:
            return
        self._acquiring = asyncio.create_task(self._take_slot(), name="agent-slot-acquire")

    async def _take_slot(self) -> None:
        slot = self._acquire()
        entered = False
        try:
            if self._releasing is not None:
                await asyncio.shield(self._releasing)
            await slot.__aenter__()
            entered = True
            # There is no await between this decision and transferring ownership. A
            # simultaneous suspension/close therefore either sees the held slot, or
            # this worker sees it first and gives the newly acquired slot back.
            if not self._closed and not self._suspensions:
                self._slot = slot
                entered = False
                self._ready.set()
        except asyncio.CancelledError:
            if not self._closed:
                self._error = RuntimeError("Agent slot acquisition was cancelled")
                self._ready.set()
            raise
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            if entered:
                self._releasing = asyncio.create_task(slot.__aexit__(None, None, None))
                await _finish_cleanup(self._releasing)
            self._acquiring = None
            if self._error is None:
                self._ensure_acquiring()

    def _release(self) -> asyncio.Task[Any] | None:
        if self._slot is not None:
            slot, self._slot = self._slot, None
            self._releasing = asyncio.create_task(slot.__aexit__(None, None, None))
        return self._releasing

    async def _wait_ready(self) -> None:
        while self._slot is None and not self._closed and self._error is None:
            await self._ready.wait()
        if self._error is not None:
            raise self._error
        if self._closed:
            raise RuntimeError("This agent slot lease has ended")

    @contextlib.asynccontextmanager
    async def suspended(self) -> AsyncIterator[None]:
        """Give the slot to children until all overlapping waits finish."""
        if self._closed:
            raise RuntimeError("This agent slot lease has ended")
        if self._error is not None:
            raise self._error
        owner = asyncio.current_task()
        assert owner is not None
        self._owners[owner] = self._owners.get(owner, 0) + 1
        self._suspensions += 1
        self._ready.clear()
        release = self._release()
        cancelled = False
        try:
            if release is not None:
                await asyncio.shield(release)
            yield
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._suspensions -= 1
            depth = self._owners[owner] - 1
            if depth:
                self._owners[owner] = depth
            else:
                del self._owners[owner]
            self._ensure_acquiring()
            # A cancelled parent must be able to tear down even while every slot is
            # occupied. The lease still owns any reacquisition until close() cancels it.
            if not cancelled and not self._closed and not depth:
                await self._wait_ready()

    async def close(self) -> None:
        """Stop reacquisition and release the slot before the parent run ends."""
        if self._closing is None:
            self._closed = True
            self._ready.set()
            pending = self._acquiring
            if pending is not None:
                pending.cancel()
            release = self._release()

            async def cleanup() -> None:
                if pending is not None:
                    await asyncio.gather(pending, return_exceptions=True)
                # An acquisition cancelled after entry can schedule its own release.
                final_release = self._releasing or release
                if final_release is not None:
                    await final_release

            self._closing = asyncio.create_task(cleanup(), name="agent-slot-close")
        await _finish_cleanup(self._closing)


async def _finish_cleanup(task: asyncio.Task[Any]) -> None:
    """Finish resource release even if cancellation arrives during cleanup."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


@contextlib.asynccontextmanager
async def agent_slot(
    jobs: JobEngine, record: Record, key: str, member_id: str
) -> AsyncIterator[AgentSlotLease]:
    """A suspendable agent slot, with transcript entries around capacity waits."""
    lease = AgentSlotLease(lambda: _recorded_slot(jobs, record, key, member_id))
    try:
        await lease.start()
        yield lease
    finally:
        await lease.close()


@contextlib.asynccontextmanager
async def _recorded_slot(
    jobs: JobEngine, record: Record, key: str, member_id: str
) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    queued_at: float | None = None

    async def waiting(limit: int) -> None:
        nonlocal queued_at
        queued_at = loop.time()
        await record(
            key,
            member_id,
            role="system",
            kind="queued",
            text=(
                f"Waiting for a free agent slot: {limit} of {limit} in use. The session "
                "starts as soon as another run or chat ends — the limit is AI agents → "
                "Parallel sessions in Settings."
            ),
            data={"resource": "agent", "limit": limit},
        )

    async with jobs.slot("agent", on_wait=waiting):
        if queued_at is not None:
            waited = loop.time() - queued_at
            await record(
                key,
                member_id,
                role="system",
                kind="queued",
                text=f"An agent slot is free after {span(waited)} — starting the session.",
                data={"resource": "agent", "waited_s": round(waited, 1)},
            )
        yield


def span(seconds: float) -> str:
    """A wait as a person says it: ``45s``, ``4m 12s``, ``1h 03m``."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
