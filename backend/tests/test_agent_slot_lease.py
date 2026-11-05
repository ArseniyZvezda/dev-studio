"""A parent waiting on its own workers must not occupy their last agent slot (§10.3)."""

from __future__ import annotations

import asyncio

import pytest

from voxarium_studio.engine.jobs import JobEngine
from voxarium_studio.teams.slots import agent_slot


async def record(*_args, **_kwargs) -> None:
    pass


async def test_wait_lends_single_slot_to_child_and_reclaims_it() -> None:
    jobs = JobEngine({"agent": 1})
    child_entered = asyncio.Event()

    async def child() -> None:
        async with jobs.slot("agent"):
            child_entered.set()

    async with agent_slot(jobs, record, "parent", "member") as lease:
        worker = asyncio.create_task(child())
        async with lease.suspended():
            await asyncio.wait_for(worker, timeout=1)
        assert child_entered.is_set()
        assert not jobs._semaphore("agent").available
    assert jobs._semaphore("agent").available


async def test_overlapping_waits_resume_only_after_last_wait_reacquires() -> None:
    jobs = JobEngine({"agent": 1})
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    returned: list[int] = []

    async with agent_slot(jobs, record, "parent", "member") as lease:
        async def wait(index: int) -> None:
            async with lease.suspended():
                entered[index].set()
                await release[index].wait()
            returned.append(index)

        first, second = [asyncio.create_task(wait(index)) for index in range(2)]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
        release[0].set()
        await asyncio.sleep(0)
        assert returned == []
        assert jobs._semaphore("agent").available
        release[1].set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert sorted(returned) == [0, 1]
        assert not jobs._semaphore("agent").available
    assert jobs._semaphore("agent").available


async def test_nested_wait_in_same_tool_does_not_wait_for_itself() -> None:
    jobs = JobEngine({"agent": 1})
    async with agent_slot(jobs, record, "parent", "member") as lease:
        async def nested() -> None:
            async with lease.suspended():
                async with lease.suspended():
                    async with jobs.slot("agent"):
                        pass
        await asyncio.wait_for(nested(), 1)
        assert not jobs._semaphore("agent").available
    assert jobs._semaphore("agent").available


async def test_cancelled_waiter_does_not_strand_other_waiters() -> None:
    jobs = JobEngine({"agent": 1})
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async with agent_slot(jobs, record, "parent", "member") as lease:
        async def wait(index: int) -> None:
            async with lease.suspended():
                entered[index].set()
                await release.wait()

        first, second = [asyncio.create_task(wait(index)) for index in range(2)]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        assert jobs._semaphore("agent").available
        release.set()
        await asyncio.wait_for(second, 1)
        assert not jobs._semaphore("agent").available
    assert jobs._semaphore("agent").available


async def test_parent_cancel_during_wait_does_not_wait_for_busy_child_slot() -> None:
    jobs = JobEngine({"agent": 1})
    parent_waiting = asyncio.Event()
    child_holding = asyncio.Event()
    release_child = asyncio.Event()

    async def parent() -> None:
        async with agent_slot(jobs, record, "parent", "member") as lease:
            async with lease.suspended():
                parent_waiting.set()
                await asyncio.Event().wait()

    async def child() -> None:
        async with jobs.slot("agent"):
            child_holding.set()
            await release_child.wait()

    parent_job = asyncio.create_task(parent())
    await asyncio.wait_for(parent_waiting.wait(), 1)
    child_job = asyncio.create_task(child())
    await asyncio.wait_for(child_holding.wait(), 1)
    parent_job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(parent_job, 1)
    assert not jobs._semaphore("agent").available
    release_child.set()
    await asyncio.wait_for(child_job, 1)
    assert jobs._semaphore("agent").available


async def test_close_cancels_pending_reacquisition_and_wakes_waiter() -> None:
    jobs = JobEngine({"agent": 1})
    waiting = asyncio.Event()
    finish_wait = asyncio.Event()
    reacquiring = asyncio.Event()

    async def capture(*_args, **kwargs) -> None:
        if kwargs.get("data", {}).get("resource") == "agent":
            reacquiring.set()

    async with agent_slot(jobs, capture, "parent", "member") as lease:
        async def wait() -> None:
            async with lease.suspended():
                waiting.set()
                await finish_wait.wait()

        waiter = asyncio.create_task(wait())
        await asyncio.wait_for(waiting.wait(), 1)
        async with jobs.slot("agent"):
            finish_wait.set()
            await asyncio.wait_for(reacquiring.wait(), 1)
            await asyncio.wait_for(lease.close(), 1)
            with pytest.raises(RuntimeError, match="ended"):
                await asyncio.wait_for(waiter, 1)
            assert not jobs._semaphore("agent").available
    assert jobs._semaphore("agent").available


async def test_cancellation_while_initially_queued_does_not_leak_slot() -> None:
    jobs = JobEngine({"agent": 1})
    queued = asyncio.Event()

    async def capture(*_args, **_kwargs) -> None:
        queued.set()

    async def parent() -> None:
        async with agent_slot(jobs, capture, "parent", "member"):
            pytest.fail("Parent entered while the only slot was busy")

    async with jobs.slot("agent"):
        parent_job = asyncio.create_task(parent())
        await asyncio.wait_for(queued.wait(), 1)
        parent_job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(parent_job, 1)
        assert not jobs._semaphore("agent").available
    assert jobs._semaphore("agent").available


async def test_failed_queue_notification_releases_slot_and_preserves_error() -> None:
    jobs = JobEngine({"agent": 1})
    queued = asyncio.Event()

    async def capture(*_args, **kwargs) -> None:
        if "waited_s" in kwargs.get("data", {}):
            raise ValueError("Transcript unavailable")
        queued.set()

    async def parent() -> None:
        async with agent_slot(jobs, capture, "parent", "member"):
            pytest.fail("Failed notification must abort entry")

    async with jobs.slot("agent"):
        parent_job = asyncio.create_task(parent())
        await asyncio.wait_for(queued.wait(), 1)
    with pytest.raises(ValueError, match="Transcript unavailable"):
        await asyncio.wait_for(parent_job, 1)
    assert jobs._semaphore("agent").available
