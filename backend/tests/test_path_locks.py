"""Fallback isolation locks declared write targets, never prose or read references (§10.5)."""

import asyncio
import os
from pathlib import Path

import pytest

from voxarium_studio.teams.worktree import PathLockManager, normalize_write_paths


@pytest.mark.parametrize("paths", [
    "src/player.py",
    [""],
    ["   "],
    ["."],
    [".."],
    ["src/"],
    ["src/*.py"],
    ["src/file?.py"],
    ["src/player.py", 42],
])
def test_invalid_write_declarations_are_rejected(paths) -> None:
    with pytest.raises(ValueError):
        normalize_write_paths(paths)


async def test_empty_write_list_acquires_nothing(tmp_path: Path) -> None:
    manager = PathLockManager()
    assert await manager.acquire([], root=tmp_path) == []


async def test_explicit_new_extensionless_file_and_spaces_are_locked(tmp_path: Path) -> None:
    manager = PathLockManager()
    held = await manager.acquire(["Makefile", "docs/design notes.md"], root=tmp_path)
    assert len(held) == 2
    assert all(lock.locked() for lock in held)
    manager.release(held)


async def test_same_file_waits_and_names_its_owner(tmp_path: Path) -> None:
    manager = PathLockManager()
    held = await manager.acquire(["src/shared.py"], root=tmp_path, owner="BT-546")
    waiting = asyncio.Event()
    notices = []

    async def on_wait(path, owner):
        notices.append((path, owner))
        waiting.set()

    contender = asyncio.create_task(manager.acquire(
        [r"src\shared.py"], root=tmp_path, owner="BT-543", on_wait=on_wait,
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not contender.done()
        assert notices == [("src/shared.py", "BT-546")]
        independent = await asyncio.wait_for(manager.acquire(["src/other.py"], root=tmp_path), 1)
        manager.release(independent)
        manager.release(held)
        held = []
        acquired = await asyncio.wait_for(contender, 1)
        manager.release(acquired)
    finally:
        if not contender.done():
            contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        manager.release(held)


async def test_relative_absolute_and_dotted_aliases_share_one_lock(tmp_path: Path) -> None:
    manager = PathLockManager()
    absolute = str(tmp_path / "src" / "shared.py")
    held = await asyncio.wait_for(manager.acquire(
        ["src/shared.py", "./src/../src/shared.py", absolute], root=tmp_path,
    ), 1)
    assert len(held) == 1, "aliases must not deadlock a task against itself"
    waiting = asyncio.Event()

    async def on_wait(_path, _owner):
        waiting.set()

    contender = asyncio.create_task(manager.acquire([absolute], root=tmp_path, on_wait=on_wait))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not contender.done()
        manager.release(held)
        held = []
        manager.release(await asyncio.wait_for(contender, 1))
    finally:
        if not contender.done():
            contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        manager.release(held)


@pytest.mark.skipif(os.name == "nt", reason="POSIX resolves '..' after following symlinks")
async def test_symlink_parent_path_shares_lock_with_resolved_file(tmp_path: Path) -> None:
    project = tmp_path / "game"
    project.mkdir()
    target = tmp_path / "outside" / "subdir"
    target.mkdir(parents=True)
    try:
        (project / "link").symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")
    manager = PathLockManager()
    held = await manager.acquire(["link/../shared.py"], root=project)
    waiting = asyncio.Event()

    async def on_wait(_path, _owner):
        waiting.set()

    contender = asyncio.create_task(manager.acquire(
        [str(target.parent / "shared.py")], root=project, on_wait=on_wait,
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not contender.done(), "lexical removal of '..' would lock the wrong file"
        manager.release(held)
        held = []
        manager.release(await asyncio.wait_for(contender, 1))
    finally:
        if not contender.done():
            contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        manager.release(held)


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case insensitive")
async def test_windows_case_aliases_share_one_lock(tmp_path: Path) -> None:
    manager = PathLockManager()
    held = await asyncio.wait_for(manager.acquire(
        [r"Src\Shared.py", "src/shared.py"], root=tmp_path,
    ), 1)
    assert len(held) == 1
    manager.release(held)


async def test_every_declared_target_is_locked_beyond_twenty(tmp_path: Path) -> None:
    manager = PathLockManager()
    paths = [f"src/file_{index:02}.py" for index in range(25)]
    held = await manager.acquire(paths, root=tmp_path)
    assert len(held) == 25
    waiting = asyncio.Event()

    async def on_wait(_path, _owner):
        waiting.set()

    contender = asyncio.create_task(manager.acquire([paths[-1]], root=tmp_path, on_wait=on_wait))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not contender.done()
    finally:
        contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        manager.release(held)


@pytest.mark.parametrize("callback_error", [False, True])
async def test_interrupted_wait_releases_partial_acquisitions(
    tmp_path: Path, callback_error: bool,
) -> None:
    manager = PathLockManager()
    held = await manager.acquire(["z.py"], root=tmp_path, owner="holder")
    waiting = asyncio.Event()

    async def on_wait(path, owner):
        waiting.set()
        if callback_error:
            raise RuntimeError("could not record wait")

    contender = asyncio.create_task(manager.acquire(["a.py", "z.py"], root=tmp_path, on_wait=on_wait))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        if not callback_error:
            contender.cancel()
        with pytest.raises(RuntimeError if callback_error else asyncio.CancelledError):
            await contender
        # a.py was acquired before z.py blocked: it must not leak after cancellation.
        acquired = await asyncio.wait_for(manager.acquire(["a.py"], root=tmp_path), 1)
        manager.release(acquired)
        assert held[0].locked(), "the other task still owns z.py"
    finally:
        if not contender.done():
            contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        manager.release(held)
