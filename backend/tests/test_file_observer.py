"""Disk evidence for MCP/script writes, without attributing shared writes (§10.4)."""

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from voxarium_studio.models.teams import TeamMember
from voxarium_studio.teams.backends.base import AgentEvent, AgentRunRequest
from voxarium_studio.teams.file_observer import FileObserver

TEAM = "team_default"
DEV = f"{TEAM}__dev"


def observer(root: Path):
    paths, errors = [], []

    async def record(found, error):
        paths.extend(found)
        if error:
            errors.append(error)

    return FileObserver(root, ["node_modules"], record), paths, errors


def pause_file_read(monkeypatch, target: Path):
    """Hold one OS read; cancellation must prevent the next chunk after it returns."""
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    reads = []
    original_open = Path.open

    class Reader:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            try:
                return self.source.__exit__(*args)
            finally:
                closed.set()

        def __getattr__(self, name):
            return getattr(self.source, name)

        def read(self, size=-1):
            reads.append(size)
            entered.set()
            assert release.wait(10), "test did not release the simulated disk read"
            return self.source.read(size)

        def readinto(self, buffer):
            data = self.read(len(buffer))
            buffer[:len(data)] = data
            return len(data)

    def open_file(path, *args, **kwargs):
        source = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        return Reader(source) if path == target and mode == "rb" else source

    monkeypatch.setattr(Path, "open", open_file)
    return entered, release, closed, reads


async def test_content_changes_not_existing_dirt_or_timestamps(tmp_path):
    existing = tmp_path / "dirty.txt"
    existing.write_text("already changed")
    deleted = tmp_path / "deleted.txt"
    deleted.write_bytes(b"gone")
    renamed = tmp_path / "old.txt"
    renamed.write_text("rename")
    tracking, found, errors = observer(tmp_path)
    await tracking.start()
    os.utime(existing, None)
    deleted.unlink()
    renamed.rename(tmp_path / "new.txt")
    (tmp_path / "binary.bin").write_bytes(b"\x00\xff")
    await tracking.finish()
    assert set(found) == {"deleted.txt", "old.txt", "new.txt", "binary.bin"}
    assert not errors


async def test_final_reconciliation_catches_scripts_and_preserved_mtime(tmp_path):
    target = tmp_path / "script.txt"
    target.write_text("before")
    stamp = target.stat()
    tracking, found, errors = observer(tmp_path)
    await tracking.start()
    # Deliberately remove live watching: the final pass must close this gap by itself.
    tracking.worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tracking.worker
    subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; Path('script.txt').write_text('after!')"],
        cwd=tmp_path, check=True,
    )
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    await tracking.finish()
    assert found == ["script.txt"]
    assert not errors


async def test_live_observations_are_retained_after_revert(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("before")
    tracking, found, errors = observer(tmp_path)
    await tracking.start()
    target.write_text("during")
    async with asyncio.timeout(10):
        while not found:
            await asyncio.sleep(0.05)
    target.write_text("before")
    await tracking.finish()
    assert found == ["file.txt"]
    assert not errors


async def test_exclusions_and_more_than_one_ui_batch(tmp_path):
    tracking, found, errors = observer(tmp_path)
    await tracking.start()
    for name in [
        "node_modules/a", ".git/index", ".voxarium_studio/app.db-wal",
        ".voxarium_studio/runs/log", ".voxarium_studio/temp/VS-01/nested/probe.py",
        ".voxarium_studio/temp/VS-02/result.json",
    ]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ignore")
    for index in range(250):
        (tmp_path / f"asset-{index}.bin").write_bytes(b"asset")
    docs = tmp_path / ".voxarium_studio/docs/design.md"
    docs.parent.mkdir(parents=True)
    docs.write_text("include")
    await tracking.finish()
    assert len(set(found)) == 251
    assert ".voxarium_studio/docs/design.md" in found
    assert not errors


async def request_for(ctx):
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="disk writes", brief="b", created_by=None,
    )
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
    return AgentRunRequest(member=member, task=task, workdir=ctx.paths.root, prompt="b", system_prompt="")


@pytest.mark.parametrize("ending", ["success", "error", "cancel"])
async def test_run_lifecycle_persists_observations_and_excludes_them_from_commit(ctx, ending):
    request = await request_for(ctx)

    class Writer:
        async def run(self, request):
            (request.workdir / "from-mcp.txt").write_text("output")
            if ending == "cancel":
                # Cancellation preserves persisted evidence but must not start a new
                # final disk scan. Wait for a live observation before cancelling.
                async with asyncio.timeout(10):
                    while not any(
                        file["path"] == "from-mcp.txt"
                        for file in (await ctx.teams.task_changes(request.task.id))["files"]
                    ):
                        await asyncio.sleep(0.05)
                raise asyncio.CancelledError()
            if ending == "error":
                raise RuntimeError("MCP failed after saving")
            yield AgentEvent(kind="text", text="done")

    if ending == "success":
        await ctx.teams._drain(Writer(), request, request.task.id, DEV)
    else:
        with pytest.raises(asyncio.CancelledError if ending == "cancel" else RuntimeError):
            await ctx.teams._drain(Writer(), request, request.task.id, DEV)
    # This reads persisted messages, not the observer's in-memory state.
    changes = await ctx.teams.task_changes(request.task.id)
    entry = next(file for file in changes["files"] if file["path"] == "from-mcp.txt")
    assert entry["evidence"] == "observed"
    assert entry["insertions"] is None
    await ctx.teams._draft_commit(request.task.id)
    assert (await ctx.teams.find_task(request.task.id)).commit_files == []


async def test_parallel_runs_do_not_claim_authorship(ctx):
    first, second = await request_for(ctx), await request_for(ctx)
    ready = 0
    both = asyncio.Event()

    class Writer:
        async def run(self, request):
            nonlocal ready
            ready += 1
            if ready == 2:
                both.set()
            await both.wait()
            (request.workdir / "shared.txt").write_text("concurrent")
            yield AgentEvent(kind="text", text="done")

    await asyncio.gather(*(
        ctx.teams._drain(Writer(), request, request.task.id, DEV) for request in (first, second)
    ))
    for request in (first, second):
        files = (await ctx.teams.task_changes(request.task.id))["files"]
        assert next(f for f in files if f["path"] == "shared.txt")["evidence"] == "observed"


async def test_resumed_runs_merge_paths_and_tool_evidence(ctx):
    request = await request_for(ctx)

    class Writer:
        async def run(self, request):
            (request.workdir / "shared.txt").write_text(request.prompt)
            yield AgentEvent(kind="tool_use", text="Edit", data={
                "name": "Edit", "input": {"file_path": "shared.txt"},
                "edit": {"insertions": 1, "deletions": 0},
            })

    for prompt in ("first", "second"):
        request.prompt = prompt
        await ctx.teams._drain(Writer(), request, request.task.id, DEV)
    entries = (await ctx.teams.task_changes(request.task.id))["files"]
    assert len(entries) == 1
    assert entries[0]["evidence"] == "tools_and_observed"
    assert entries[0]["insertions"] is None


async def test_slow_baseline_has_visible_progress_and_does_not_block_backend(ctx, monkeypatch):
    request = await request_for(ctx)
    existing = request.workdir / "large.bin"
    existing.write_bytes(b"x" * 64)
    monkeypatch.setattr(FileObserver, "SCAN_TIMEOUT", 0.2)
    monkeypatch.setattr(FileObserver, "PROGRESS_INTERVAL", 0.03)
    monkeypatch.setattr(FileObserver, "CHUNK_SIZE", 8)
    entered, release, closed, reads = pause_file_read(monkeypatch, existing)
    backend_started = asyncio.Event()

    class Writer:
        async def run(self, request):
            backend_started.set()
            (request.workdir / "written.txt").write_text("output")
            yield AgentEvent(kind="tool_use", text="Write", data={
                "name": "Write", "input": {"file_path": "written.txt"},
            })

    run = asyncio.create_task(ctx.teams._drain(Writer(), request, request.task.id, DEV))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        async with asyncio.timeout(2):
            await backend_started.wait()
            await run
        assert not release.is_set(), "backend launch waited for the disk read"
        messages = await ctx.teams._messages_of(request.task.id)
        progress = [m for m in messages if m.kind == "file_observation_status"]
        assert len(progress) >= 2  # Preparing and at least one elapsed-progress update.
        assert progress[0].data["stage"] == "preparing"
        assert any(m.data["stage"] == "waiting" and m.data["elapsed_s"] > 0 for m in progress)
        assert not any(m.data["stage"] == "ready" for m in progress)
        changes = await ctx.teams.task_changes(request.task.id)
        assert any("time limit" in error for error in changes["observation_errors"])
        assert {f["path"] for f in changes["files"]} == {"written.txt"}
        assert changes["files"][0]["evidence"] == "tools"
    finally:
        release.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        assert await asyncio.to_thread(closed.wait, 2)
    assert len(reads) == 1
    # A worker returning late must never publish a partial baseline as changed files.
    changes = await ctx.teams.task_changes(request.task.id)
    assert {f["path"] for f in changes["files"]} == {"written.txt"}


async def test_cancel_during_preparation_stops_reading_and_never_launches_backend(ctx, monkeypatch):
    request = await request_for(ctx)
    existing = request.workdir / "large.bin"
    existing.write_bytes(b"x" * 64)
    monkeypatch.setattr(FileObserver, "CHUNK_SIZE", 8)
    entered, release, closed, reads = pause_file_read(monkeypatch, existing)
    backend_started = False

    class Backend:
        async def run(self, request):
            nonlocal backend_started
            backend_started = True
            yield AgentEvent(kind="text", text="unexpected")

    run = asyncio.create_task(ctx.teams._drain(Backend(), request, request.task.id, DEV))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        run.cancel()
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await run
        assert not backend_started
    finally:
        release.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        assert await asyncio.to_thread(closed.wait, 2)
    assert len(reads) == 1
    assert not (await ctx.teams.task_changes(request.task.id))["files"]


@pytest.mark.parametrize("budget", ["bytes", "entries"])
async def test_exhausted_scan_budget_never_treats_unseen_files_as_changes(tmp_path, monkeypatch, budget):
    for index in range(20):
        (tmp_path / f"existing-{index}.txt").write_bytes(b"original")
    monkeypatch.setattr(FileObserver, "MAX_BYTES" if budget == "bytes" else "MAX_ENTRIES", 16)
    tracking, found, errors = observer(tmp_path)
    async with asyncio.timeout(2):
        await tracking.start()
        (tmp_path / "existing-19.txt").write_bytes(b"changed")
        (tmp_path / "new.txt").write_bytes(b"new")
        await tracking.finish()
    expected_limit = "read limit" if budget == "bytes" else "entry limit"
    assert any(expected_limit in error for error in errors)
    assert not found


async def test_final_scan_timeout_preserves_prior_evidence_without_false_deletions(tmp_path, monkeypatch):
    known = tmp_path / "known.txt"
    known.write_text("before")
    slow = tmp_path / "slow.bin"
    slow.write_bytes(b"x" * 64)
    tracking, found, errors = observer(tmp_path)
    await tracking.start()
    tracking.worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tracking.worker
    known.write_text("after")
    await tracking.reconcile()
    assert found == ["known.txt"]
    monkeypatch.setattr(FileObserver, "SCAN_TIMEOUT", 0.1)
    monkeypatch.setattr(FileObserver, "CHUNK_SIZE", 8)
    entered, release, closed, reads = pause_file_read(monkeypatch, slow)
    finishing = asyncio.create_task(tracking.finish())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        async with asyncio.timeout(1):
            await finishing
        assert errors
        assert found == ["known.txt"]
    finally:
        release.set()
        if not finishing.done():
            finishing.cancel()
        await asyncio.gather(finishing, return_exceptions=True)
        assert await asyncio.to_thread(closed.wait, 2)
    assert len(reads) == 1
    assert found == ["known.txt"]


async def test_cancel_during_final_scan_stops_worker_without_publishing_partial_changes(ctx, monkeypatch):
    request = await request_for(ctx)
    existing = request.workdir / "large.bin"
    existing.write_bytes(b"x" * 64)
    monkeypatch.setattr(FileObserver, "CHUNK_SIZE", 8)
    paused = None

    class Writer:
        async def run(self, request):
            nonlocal paused
            # The baseline completed; only the forced final read will be held.
            paused = pause_file_read(monkeypatch, existing)
            yield AgentEvent(kind="text", text="done")

    run = asyncio.create_task(ctx.teams._drain(Writer(), request, request.task.id, DEV))
    try:
        async with asyncio.timeout(2):
            while paused is None:
                await asyncio.sleep(0.01)
        entered, release, closed, reads = paused
        assert await asyncio.to_thread(entered.wait, 2)
        run.cancel()
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await run
    finally:
        if paused is not None:
            paused[1].set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        if paused is not None:
            assert await asyncio.to_thread(paused[2].wait, 2)
    assert len(reads) == 1
    assert not (await ctx.teams.task_changes(request.task.id))["files"]


async def test_cancel_while_finishing_live_observer_is_not_swallowed(tmp_path, monkeypatch):
    watching = asyncio.Event()
    stopped = asyncio.Event()

    async def watch(self):
        watching.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(FileObserver, "_watch", watch)
    tracking, found, errors = observer(tmp_path)
    await tracking.start()
    await watching.wait()
    finishing = asyncio.create_task(tracking.finish())
    await asyncio.sleep(0)  # finish is waiting for the already-running observer.
    finishing.cancel()
    try:
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await finishing
    finally:
        await tracking.cancel()
        await asyncio.gather(finishing, return_exceptions=True)
    assert stopped.is_set()
    assert not found and not errors
