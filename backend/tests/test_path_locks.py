"""Fallback isolation must recognise files without serialising ordinary prose (§10.5)."""

import asyncio
from pathlib import Path

import pytest

from voxarium_studio.teams.worktree import PathLockManager


@pytest.mark.parametrize("text", [
    "Решение пользователя 03.11.2026; ревью 03.11; версия v4.9.7.",
    "BT-561/563 и аварийный spawn/destroy запрещены.",
    "Сеть/save-load/LOD/регрессия, готово/FAIL/НП, владелец/dirty/реальные.",
    "15 вещей, 3 LOD/helpers; Body/Headwear_A; HTTP 403/read-only.",
    "https://example.com/docs/file.md и `https://example.com/assets/`",
])
def test_prose_is_not_a_path(text: str, tmp_path: Path) -> None:
    assert PathLockManager.extract_paths(text, roots=(tmp_path,)) == set()


@pytest.mark.parametrize(("text", "expected"), [
    ("Edit src/player.py, then README.md.", {"src/player.py", "readme.md"}),
    (r"Script\SampleGame\Player.as and ./Script/SampleGame/Player.as", {"script/samplegame/player.as"}),
    ("Create ./src/new and src/other/", {"src/new", "src/other"}),
    ("../engine/source /opt/game/source", {"../engine/source", "/opt/game/source"}),
    (r'"D:\Game Project\Source\Player.as"', {"d:/game project/source/player.as"}),
    ("`docs/design notes.md` and 'docs/art notes.md'", {"docs/design notes.md", "docs/art notes.md"}),
    ("Edit [design](docs/design.md)", {"docs/design.md"}),
    ("Файл:«src/player.py», `src/player.py:42:3`", {"src/player.py"}),
    ("See `docs/design.md#requirements`", {"docs/design.md"}),
])
def test_filenames_and_explicit_paths(text: str, expected: set[str]) -> None:
    assert PathLockManager.extract_paths(text) == expected


def test_existing_extensionless_paths_use_project_and_launch_roots(tmp_path: Path) -> None:
    project = tmp_path / "game"
    (project / "Script" / "SampleGame").mkdir(parents=True)
    (tmp_path / "engine" / "Source").mkdir(parents=True)
    (project / "Makefile").touch()
    assert PathLockManager.extract_paths(
        "Script/SampleGame engine/Source Makefile spawn/destroy",
        roots=(project, tmp_path),
    ) == {"script/samplegame", "engine/source", "makefile"}


async def test_same_file_waits_and_names_its_owner() -> None:
    manager = PathLockManager()
    held = await manager.acquire("src/shared.py", owner="BT-546")
    waiting = asyncio.Event()
    notices = []

    async def on_wait(path, owner):
        notices.append((path, owner))
        waiting.set()

    contender = asyncio.create_task(manager.acquire(
        r"src\shared.py", owner="BT-543", on_wait=on_wait,
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not contender.done()
        assert notices == [("src/shared.py", "BT-546")]
        independent = await asyncio.wait_for(manager.acquire("src/other.py"), 1)
        manager.release(independent)
        manager.release(held)
        acquired = await asyncio.wait_for(contender, 1)
        manager.release(acquired)
    finally:
        if not contender.done():
            contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        manager.release(held)


@pytest.mark.parametrize("callback_error", [False, True])
async def test_interrupted_wait_releases_partial_acquisitions(callback_error: bool) -> None:
    manager = PathLockManager()
    held = await manager.acquire("z.py", owner="holder")
    waiting = asyncio.Event()

    async def on_wait(path, owner):
        waiting.set()
        if callback_error:
            raise RuntimeError("could not record wait")

    contender = asyncio.create_task(manager.acquire("a.py z.py", on_wait=on_wait))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        if not callback_error:
            contender.cancel()
        with pytest.raises(RuntimeError if callback_error else asyncio.CancelledError):
            await contender
        # a.py was acquired before z.py blocked: it must not leak after cancellation.
        acquired = await asyncio.wait_for(manager.acquire("a.py"), 1)
        manager.release(acquired)
        assert held[0].locked(), "the other task still owns z.py"
    finally:
        if not contender.done():
            contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)
        manager.release(held)
