"""Bounded JSONL and stderr readers shared by CLI backends (§10.6)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)


class _Oversized:
    __slots__ = ("size",)

    def __init__(self, size: int) -> None:
        self.size = size


async def _frames(
    stream: asyncio.StreamReader, max_bytes: int
) -> AsyncIterator[dict[str, Any] | _Oversized]:
    """One JSON object per stdout line, with an over-long line reported rather than fatal.

    ``readuntil`` rather than ``readline``, per ``backend-python.md``: on an over-long line
    ``readline`` discards the buffer before raising, while ``readuntil`` leaves the bytes
    in place so the line can be consumed and skipped as a whole.
    """
    while True:
        try:
            raw = await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            raw = exc.partial
            if not raw:
                return
        except asyncio.LimitOverrunError as exc:
            size = await _skip_line(stream, exc.consumed)
            yield _Oversized(size)
            continue
        if len(raw) > max_bytes > 0:
            yield _Oversized(len(raw))
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            # CLIs may print non-JSON warnings even in structured output mode.
            log.debug("CLI: non-JSON line on stdout: %s", line[:200])
            continue
        if isinstance(frame, dict):
            yield frame
        if not raw.endswith(b"\n"):
            return


async def _skip_line(stream: asyncio.StreamReader, consumed: int) -> int:
    """Drop the rest of a line the buffer could not hold; answers how long it was."""
    size = 0
    chunk = await stream.read(consumed)
    size += len(chunk)
    while True:
        try:
            rest = await stream.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            size += len(await stream.read(exc.consumed))
            continue
        except asyncio.IncompleteReadError as exc:
            return size + len(exc.partial)
        return size + len(rest)


async def _drain(stream: asyncio.StreamReader, tail: deque[str]) -> None:
    """Read stderr to its end, keeping only the tail. Never raises: the pipe may close
    early, and this must not take the run with it."""
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            tail.extend(chunk.decode("utf-8", errors="replace"))
    except Exception:  # pragma: no cover - a closed pipe
        return


