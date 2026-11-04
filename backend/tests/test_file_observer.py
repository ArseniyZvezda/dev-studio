"""Disk evidence for MCP/script writes, without attributing shared writes (§10.4)."""

import asyncio
import os
import subprocess
import sys
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
        "node_modules/a", ".git/index", "voxarium_studio/app.db-wal",
        "voxarium_studio/runs/log", "voxarium_studio/temp/VS-01/nested/probe.py",
        "voxarium_studio/temp/VS-02/result.json",
    ]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ignore")
    for index in range(250):
        (tmp_path / f"asset-{index}.bin").write_bytes(b"asset")
    docs = tmp_path / "voxarium_studio/docs/design.md"
    docs.parent.mkdir(parents=True)
    docs.write_text("include")
    await tracking.finish()
    assert len(set(found)) == 251
    assert "voxarium_studio/docs/design.md" in found
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
