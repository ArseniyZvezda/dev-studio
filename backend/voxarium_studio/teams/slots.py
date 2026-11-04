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


@contextlib.asynccontextmanager
async def agent_slot(
    jobs: JobEngine, record: Record, key: str, member_id: str
) -> AsyncIterator[None]:
    """``jobs.slot("agent")``, with a transcript entry either side of any wait."""
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
