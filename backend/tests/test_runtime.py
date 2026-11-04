"""The async substrate: subprocess output, resource limits, the event bus.

These are the pieces where a subtle bug is invisible in the UI until something large or
slow happens — an over-long log line, a lowered GPU limit, a client falling behind.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from voxarium_studio.engine.executors import (
    OUTPUT_SENTINEL,
    PROGRESS_SENTINEL,
    STREAM_LIMIT,
    _pump,
    _SafeFormatter,
)
from voxarium_studio.engine.jobs import JobEngine, ResizableSemaphore
from voxarium_studio.ws.bus import EventBus
from voxarium_studio.ws.events import EventType


# ------------------------------------------------------------------ subprocess output


async def run_script(
    tmp_path: Path, body: str, frames: list[dict] | None = None
) -> tuple[list[str], list[str]]:
    """Run a script and pump its output the way the engine does."""
    script = tmp_path / "node.py"
    script.write_text(body, encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,
    )
    lines: list[str] = []
    sink: list[str] = []

    async def log_fn(line: str, stream: str) -> None:
        lines.append(line)

    async def progress_fn(frame: dict) -> None:
        if frames is not None:
            frames.append(frame)

    assert process.stdout and process.stderr
    await asyncio.wait_for(
        asyncio.gather(
            _pump(process.stdout, "stdout", log_fn, sink, progress_fn),
            _pump(process.stderr, "stderr", log_fn, sink, progress_fn),
        ),
        timeout=60,
    )
    await process.wait()
    return lines, sink


async def test_ordinary_lines_are_forwarded(tmp_path: Path) -> None:
    lines, sink = await run_script(tmp_path, "print('one')\nprint('two')\n")
    assert lines == ["one", "two"]
    assert sink == []


async def test_a_result_larger_than_the_pipe_buffer_survives(tmp_path: Path) -> None:
    """`readline` clears its buffer before raising on a long line — the data would be gone."""
    payload = {"blob": "x" * (STREAM_LIMIT * 2)}
    _, sink = await run_script(
        tmp_path,
        "import json\n"
        f"print({OUTPUT_SENTINEL!r} + json.dumps({payload!r}))\n",
    )
    assert len(sink) == 1
    assert json.loads(sink[0])["blob"] == payload["blob"]


async def test_a_trailing_line_without_a_newline_is_not_lost(tmp_path: Path) -> None:
    lines, _ = await run_script(tmp_path, "import sys\nsys.stdout.write('no newline')\n")
    assert lines == ["no newline"]


async def test_a_progress_frame_leaves_the_log_and_becomes_a_frame(tmp_path: Path) -> None:
    frames: list[dict] = []
    lines, _ = await run_script(
        tmp_path,
        f"print({PROGRESS_SENTINEL!r} + '{{\"percent\": 40, \"label\": \"Baking\"}}')\n"
        "print('ordinary line')\n",
        frames,
    )

    assert frames == [{"percent": 40, "label": "Baking"}]
    assert lines == ["ordinary line"], "the sentinel line is not also logged"


async def test_a_malformed_progress_frame_is_logged_rather_than_swallowed(
    tmp_path: Path,
) -> None:
    """A node that meant to report progress and got the JSON wrong has a bug to see."""
    frames: list[dict] = []
    lines, _ = await run_script(tmp_path, f"print({PROGRESS_SENTINEL!r} + 'not json')\n", frames)

    assert frames == []
    assert lines == [f"{PROGRESS_SENTINEL}not json"]


async def test_a_progress_frame_is_dropped_when_nobody_is_listening(tmp_path: Path) -> None:
    """A node should not have to know whether anything is watching it."""
    lines, _ = await run_script(tmp_path, f"print({PROGRESS_SENTINEL!r} + '{{\"percent\": 1}}')\n")
    assert lines == []


# ------------------------------------------------------------------ http templating


def test_url_template_values_are_not_repeated_in_the_request() -> None:
    formatter = _SafeFormatter()
    url = formatter.vformat("{base}/items", (), {"base": "https://x", "q": "term"})
    assert url == "https://x/items"
    assert formatter.used == {"base"}, "so `base` is not also sent as a query parameter"


def test_unknown_placeholders_are_left_alone_rather_than_raising() -> None:
    formatter = _SafeFormatter()
    assert formatter.vformat("{missing}/x", (), {}) == "{missing}/x"
    assert formatter.used == set()


# ------------------------------------------------------------------ resource limits


async def test_a_semaphore_bounds_concurrency() -> None:
    semaphore = ResizableSemaphore(2)
    inside = peak = 0

    async def worker() -> None:
        nonlocal inside, peak
        await semaphore.acquire()
        inside += 1
        peak = max(peak, inside)
        await asyncio.sleep(0.02)
        inside -= 1
        await semaphore.release()

    await asyncio.gather(*(worker() for _ in range(6)))
    assert peak == 2


async def test_lowering_a_limit_applies_to_a_live_semaphore() -> None:
    """Swapping in a fresh one would let old holders and new waiters run together."""
    engine = JobEngine({"gpu": 2})
    inside = peak = 0

    async def worker() -> None:
        nonlocal inside, peak
        async with engine.slot("gpu"):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(*(worker() for _ in range(4)))
    assert peak == 2

    engine.update_limits({"gpu": 1})
    await asyncio.sleep(0)
    inside = peak = 0
    await asyncio.gather(*(worker() for _ in range(4)))
    assert peak == 1


async def test_on_wait_is_told_only_when_the_slot_is_not_free() -> None:
    """What lets an agent task say it is queued: the callback runs before a wait, with the
    limit, and never for a slot granted at once."""
    engine = JobEngine({"agent": 1})
    told: list[int] = []

    async def on_wait(limit: int) -> None:
        told.append(limit)

    async with engine.slot("agent", on_wait=on_wait):
        assert told == []
        second = asyncio.create_task(_enter(engine, on_wait))
        await asyncio.sleep(0.02)
        assert told == [1] and not second.done(), "told, and still waiting"
    await asyncio.wait_for(second, 1)


async def _enter(engine: JobEngine, on_wait) -> None:
    async with engine.slot("agent", on_wait=on_wait):
        pass


async def test_an_unknown_resource_class_falls_back_to_the_cpu_limit() -> None:
    engine = JobEngine({"cpu": 3})
    assert engine._semaphore("something-new").limit == 3


async def test_cancelling_a_job_cancels_its_task() -> None:
    engine = JobEngine()
    started = asyncio.Event()

    async def body(_handle) -> None:
        started.set()
        await asyncio.sleep(30)

    handle = engine.submit("job1", "test", body)
    await started.wait()
    assert engine.is_running("job1")

    assert await engine.cancel("job1") is True
    await asyncio.sleep(0.05)
    assert handle.task.cancelled()
    assert await engine.cancel("job1") is False, "it is no longer registered"


async def test_a_duplicate_job_id_is_refused() -> None:
    engine = JobEngine()

    async def body(_handle) -> None:
        await asyncio.sleep(5)

    engine.submit("dupe", "test", body)
    with pytest.raises(ValueError, match="already running"):
        engine.submit("dupe", "test", body)
    await engine.shutdown()


# ------------------------------------------------------------------ event bus


async def test_events_are_fanned_out_to_every_subscriber() -> None:
    bus = EventBus()
    first, second = bus.subscribe(), bus.subscribe()
    await bus.publish(EventType.NODE_LOG, {"line": "x"})

    assert (await first.get()).data == {"line": "x"}
    assert (await second.get()).data == {"line": "x"}


async def test_sequence_numbers_increase_and_history_replays() -> None:
    bus = EventBus()
    for index in range(5):
        await bus.publish(EventType.NODE_LOG, {"i": index})

    assert bus.current_seq == 5
    assert [event.data["i"] for event in bus.replay(2)] == [2, 3, 4]


async def test_a_slow_subscriber_loses_the_oldest_events_and_is_told() -> None:
    """A browser that falls behind must never stall a running pipeline (§2.4)."""
    bus = EventBus(queue_size=10, history_size=100)
    subscription = bus.subscribe()
    for index in range(25):
        await bus.publish(EventType.NODE_LOG, {"i": index})

    assert subscription.dropped == 15
    assert subscription.take_dropped() == 15
    assert subscription.take_dropped() == 0, "reported once, then cleared"

    assert (await subscription.get()).data["i"] == 15, "the newest events survived"
    # The gap is recoverable: the bus still holds them for a `replay` from the client.
    assert [event.data["i"] for event in bus.replay(0)][:3] == [0, 1, 2]


async def test_a_late_subscriber_can_resume_from_a_sequence_number() -> None:
    bus = EventBus()
    for index in range(3):
        await bus.publish(EventType.NODE_LOG, {"i": index})

    subscription = bus.subscribe(since_seq=1)
    assert (await subscription.get()).data["i"] == 1
    assert (await subscription.get()).data["i"] == 2


async def test_unsubscribing_stops_delivery() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    subscription.close()
    await bus.publish(EventType.NODE_LOG, {"i": 0})
    assert bus.subscriber_count == 0
