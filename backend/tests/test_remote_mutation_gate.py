"""I3: one context-owned gate for every Documents writer, without Remote activation.

All trees, buses and remote revisions below are synthetic. In particular, exercising the
existing remote apply and MCP handlers does not start a runner or read a credential.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from voxarium_studio.core.config import AppConfig
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.models.teams import TeamMember
from voxarium_studio.sync.service import RemoteSync, _Remote
from voxarium_studio.sync.state import SyncState
from voxarium_studio.sync.transitions import (
    BindingTransitions,
    MutationBusy,
    StaleBindingGeneration,
)
from voxarium_studio.teams import docs as docs_module
from voxarium_studio.teams.docs import DocStore, SnapshotChangedOnDisk
from voxarium_studio.teams.tools import build_toolset


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[object, dict[str, object]]] = []
        self.callback: Callable[[], Awaitable[None]] | None = None

    async def publish(self, kind: object, data: dict[str, object]) -> None:
        self.events.append((kind, dict(data)))
        callback, self.callback = self.callback, None
        if callback is not None:
            await callback()


@pytest.fixture
def store(tmp_path: Path) -> DocStore:
    paths = ProjectPaths.for_project(tmp_path / "project-a")
    paths.docs.mkdir(parents=True)
    return DocStore(paths, RecordingBus())  # type: ignore[arg-type]


async def seed(store: DocStore) -> None:
    await store.write("source/doc.md", "# Original\n")
    await store.write("links.md", "# Links\n\n[[source/doc]]\n")
    (store.directory / "source" / "image.png").write_bytes(b"synthetic image")
    (store.directory / "source" / "vxs-folder.json").write_bytes(b'{"color":"green"}\n')
    store.list_docs()  # A refused mutation must not even invalidate the warm index.
    store.bus.events.clear()  # type: ignore[union-attr]


def raw_tree(store: DocStore) -> dict[str, tuple[bytes | None, int]]:
    return {
        path.relative_to(store.directory).as_posix(): (
            path.read_bytes() if path.is_file() else None,
            path.stat().st_mtime_ns,
        )
        for path in (store.directory, *store.directory.rglob("*"))
    }


MUTATIONS = (
    "write", "create", "exclusive_create", "exclusive_existing", "delete", "move",
    "rename_document", "rename_folder", "delete_folder", "folder_style",
)


async def mutate(store: DocStore, operation: str, **kwargs: object) -> object:
    if operation == "write":
        return await store.write("source/doc.md", "# Changed\n", **kwargs)
    if operation == "create":
        return await store.write("absent/nested/new.md", "# New\n", **kwargs)
    if operation == "exclusive_create":
        return await store.write("absent/nested/new.md", "# New\n", exclusive=True, **kwargs)
    if operation == "exclusive_existing":
        return await store.write("source/doc.md", "# Wrong\n", exclusive=True, **kwargs)
    if operation == "delete":
        return await store.delete("source/doc.md", **kwargs)
    if operation == "move":
        return await store.move("source/doc.md", "absent/nested/moved.md", **kwargs)
    if operation == "rename_document":
        return await store.move("source/doc.md", "source/renamed.md", **kwargs)
    if operation == "rename_folder":
        operation_result = await store.rename_folder("source", "renamed", **kwargs)
        return [frame async for frame in operation_result.frames()]
    if operation == "delete_folder":
        return await store.delete_folder("source", **kwargs)
    if operation == "folder_style":
        return await store.set_folder_style("source", {"color": "blue"}, **kwargs)
    raise AssertionError(operation)


@pytest.mark.parametrize("operation", MUTATIONS)
async def test_stale_generation_precedes_every_mutation_and_filesystem_check(
    store: DocStore, operation: str,
) -> None:
    await seed(store)
    old = store.transitions.generation
    async with store.transitions.final_snapshot() as fence:
        fence.advance_generation()
    before, cached = raw_tree(store), store._cached

    with pytest.raises(StaleBindingGeneration) as error:
        await mutate(store, operation, expected_binding_generation=old)

    assert error.value.code == "stale_binding_generation"
    assert raw_tree(store) == before
    assert store._cached is cached
    assert store.bus.events == []


@pytest.mark.parametrize("operation", MUTATIONS)
@pytest.mark.parametrize("explicit_generation", [False, True], ids=["legacy", "current"])
async def test_current_or_missing_generation_preserves_existing_mutation_contract(
    store: DocStore, operation: str, explicit_generation: bool,
) -> None:
    await seed(store)
    # Missing G remains usable even after the in-memory generation has changed.
    async with store.transitions.final_snapshot() as fence:
        fence.advance_generation()
    kwargs = {"expected_binding_generation": store.transitions.generation} if explicit_generation else {}
    if operation == "exclusive_existing":
        before = raw_tree(store)
        with pytest.raises(FileExistsError):
            await mutate(store, operation, **kwargs)
        assert raw_tree(store) == before
        assert store.bus.events == []
    else:
        before = raw_tree(store)
        result = await mutate(store, operation, **kwargs)
        assert raw_tree(store) != before
        assert store.bus.events
        if operation == "rename_folder":
            assert result[-1]["type"] == "done"
            assert "[[renamed/doc]]" in store.read("links.md")["content"]
    # Any leaked admission would hang the next snapshot.
    async with asyncio.timeout(2):
        async with store.transitions.final_snapshot():
            pass


@pytest.mark.parametrize("operation", MUTATIONS)
async def test_active_final_snapshot_refuses_every_mutation_retryably(
    store: DocStore, operation: str,
) -> None:
    await seed(store)
    before, cached = raw_tree(store), store._cached
    async with store.transitions.final_snapshot():
        with pytest.raises(MutationBusy) as error:
            await mutate(store, operation)
        assert error.value.code == "mutation_busy"
        assert error.value.retryable is True
        assert raw_tree(store) == before
        assert store._cached is cached
        assert store.bus.events == []


async def test_admission_is_logical_and_snapshot_waits_before_closing_the_gate() -> None:
    transitions = BindingTransitions()
    assert transitions.generation == 0
    admission = await transitions.admit(expected_binding_generation=0)
    requested, entered = asyncio.Event(), asyncio.Event()

    async def snapshot() -> None:
        requested.set()
        async with transitions.final_snapshot() as fence:
            entered.set()
            fence.advance_generation()

    task = asyncio.create_task(snapshot())
    try:
        await requested.wait()
        await asyncio.sleep(0)
        assert not entered.is_set()
        with pytest.raises(MutationBusy) as error:
            await transitions.admit()
        assert error.value.retryable is True
        # An already admitted continuation may still enter critical after snapshot queued.
        async with admission.critical():
            assert not entered.is_set()
    finally:
        admission.release()
        await asyncio.wait_for(task, 2)
    assert entered.is_set()
    assert transitions.generation == 1
    with pytest.raises(StaleBindingGeneration):
        await transitions.admit(expected_binding_generation=0)


async def test_cancelling_pending_snapshot_reopens_admission() -> None:
    transitions = BindingTransitions()
    admission = await transitions.admit()
    requested = asyncio.Event()

    async def snapshot() -> None:
        requested.set()
        async with transitions.final_snapshot():
            pytest.fail("snapshot passed an unreleased admission")

    task = asyncio.create_task(snapshot())
    try:
        await requested.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        next_admission = await transitions.admit()
        next_admission.release()
    finally:
        admission.release()
    async with asyncio.timeout(2):
        async with transitions.final_snapshot():
            pass


async def test_final_snapshot_exception_releases_fence_without_implicit_generation_change(
    store: DocStore,
) -> None:
    with pytest.raises(RuntimeError, match="synthetic"):
        async with store.transitions.final_snapshot():
            raise RuntimeError("synthetic snapshot failure")
    assert store.transitions.generation == 0
    await store.write("after.md", "# After\n", expected_binding_generation=0)


@pytest.mark.parametrize("operation", ["write", "delete", "move", "rename_folder", "delete_folder"])
async def test_validation_exception_releases_mutation_admission(
    store: DocStore, operation: str,
) -> None:
    # No source exists. Each mutation must release even when its first disk check fails.
    if operation == "write":
        with pytest.raises(ValueError):
            await store.write("bad.exe", "not a document")
    else:
        with pytest.raises(FileNotFoundError):
            await mutate(store, operation)
    async with asyncio.timeout(2):
        async with store.transitions.final_snapshot():
            pass


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_publish_exception_or_cancellation_does_not_leak_admission(
    store: DocStore, failure: str,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def publish() -> None:
        started.set()
        if failure == "error":
            raise RuntimeError("synthetic subscriber failure")
        await release.wait()

    store.bus.callback = publish
    task = asyncio.create_task(store.write("landed.md", "# Landed\n"))
    await started.wait()
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="subscriber"):
            await task
    assert store.resolve("landed.md").read_bytes() == b"# Landed\n"
    async with asyncio.timeout(2):
        async with store.transitions.final_snapshot():
            pass


@pytest.mark.parametrize("outer", ["write", "move", "rename_folder", "delete_folder"])
@pytest.mark.parametrize("nested", ["write", "move", "rename_folder", "delete_folder"])
async def test_publish_can_reenter_docstore_without_critical_or_folder_lock_deadlock(
    store: DocStore, outer: str, nested: str,
) -> None:
    await seed(store)
    await store.write("other/item.md", "# Other\n")

    async def callback() -> None:
        if nested == "write":
            await store.write("nested.md", "# Nested\n")
        elif nested == "move":
            await store.move("other/item.md", "other/moved.md")
        elif nested == "delete_folder":
            await store.delete_folder("other")
        else:
            result = await store.rename_folder("other", "other-renamed")
            assert [frame async for frame in result.frames()][-1]["type"] == "done"

    store.bus.callback = callback
    async with asyncio.timeout(2):
        await mutate(store, outer)
        async with store.transitions.final_snapshot():
            pass


@pytest.mark.parametrize("outer", ["write", "move", "delete_folder", "rename_folder"])
@pytest.mark.parametrize("spawn_task", [False, True], ids=["inline", "child-task"])
async def test_publish_snapshot_cannot_wait_for_its_own_continuation(
    store: DocStore, outer: str, spawn_task: bool,
) -> None:
    await seed(store)
    observed = False

    async def snapshot() -> None:
        nonlocal observed
        if outer == "rename_folder":
            # Its bytes are mended, but this awaited event is still part of the admission.
            assert store.transitions.active_mutations == 1
            with pytest.raises(MutationBusy) as error:
                async with store.final_snapshot():
                    pytest.fail("Continuation snapshotted itself")
            assert error.value.retryable
        else:
            # Ordinary writes have already released admission before publishing.
            async with store.final_snapshot() as captured:
                captured.verify()
        observed = True

    async def callback() -> None:
        if spawn_task:
            await asyncio.create_task(snapshot())
        else:
            await snapshot()

    store.bus.callback = callback
    async with asyncio.timeout(2):
        result = await mutate(store, outer)
        if outer == "rename_folder":
            assert result[-1]["type"] == "done"
        async with store.final_snapshot():
            pass
    assert observed
    assert store.transitions.active_mutations == 0


@pytest.mark.parametrize("spawn_task", [False, True], ids=["inline", "child-task"])
async def test_final_snapshot_reentry_is_retryable_not_a_mutex_deadlock(
    store: DocStore, spawn_task: bool,
) -> None:
    async def nested() -> None:
        with pytest.raises(MutationBusy):
            async with store.final_snapshot():
                pytest.fail("Nested snapshot acquired the same identity ownership")

    async with asyncio.timeout(2):
        async with store.final_snapshot():
            if spawn_task:
                await asyncio.create_task(nested())
            else:
                await nested()


async def test_folder_mending_outlives_cancelled_stream_and_pending_snapshot(
    store: DocStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(store)
    mending_started, continue_mending = asyncio.Event(), asyncio.Event()
    requested, snapshot_entered = asyncio.Event(), asyncio.Event()
    mended: list[str] = []
    real_mend_file = docs_module._mend_file
    real_mend = store._mend

    async def hold_mending(*args: object, **kwargs: object) -> None:
        mending_started.set()
        await continue_mending.wait()
        await real_mend(*args, **kwargs)

    def tracked_mend_file(path: Path, mend: Callable[[str], str | None]) -> int:
        assert not snapshot_entered.is_set(), "snapshot overtook admitted _mend_file"
        changed = real_mend_file(path, mend)
        mended.append(path.name)
        return changed

    monkeypatch.setattr(docs_module, "_mend_file", tracked_mend_file)
    monkeypatch.setattr(store, "_mend", hold_mending)
    operation = await store.rename_folder("source", "renamed")

    async def consume() -> None:
        async for _frame in operation.frames():
            pass

    consumer = asyncio.create_task(consume())
    await mending_started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    async def snapshot() -> None:
        requested.set()
        async with store.transitions.final_snapshot() as fence:
            assert mended == ["links.md"]
            assert "[[renamed/doc]]" in store.read("links.md")["content"]
            snapshot_entered.set()
            fence.advance_generation()

    task = asyncio.create_task(snapshot())
    try:
        await requested.wait()
        await asyncio.sleep(0)
        assert not snapshot_entered.is_set()
        before = raw_tree(store)
        with pytest.raises(MutationBusy) as error:
            await store.write("must-not-land.md", "# Refused\n")
        assert error.value.retryable is True
        assert raw_tree(store) == before
    finally:
        continue_mending.set()
        await asyncio.wait_for(task, 2)
    assert snapshot_entered.is_set()
    assert store.transitions.generation == 1
    assert not store._mending or all(task.done() for task in store._mending)


@pytest.mark.parametrize("when", ["before_start", "during_continuation"])
async def test_cancelled_mending_task_releases_its_transferred_admission(
    store: DocStore, when: str,
) -> None:
    await seed(store)
    started, never = asyncio.Event(), asyncio.Event()

    async def pause_publish() -> None:
        started.set()
        await never.wait()

    store.bus.callback = pause_publish
    operation = await store.rename_folder("source", "renamed")
    task = next(iter(store._mending))
    if when == "during_continuation":
        await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.bus.callback = None
    # The before-start path never enters _mend's finally: its done callback owns cleanup.
    async with asyncio.timeout(2):
        async with store.transitions.final_snapshot():
            pass
        await store.delete_folder("renamed")
        assert [frame async for frame in operation.frames()]


@pytest.mark.parametrize("operation", ["rename_folder", "delete_folder"])
async def test_cancellation_in_folder_filesystem_phase_releases_both_locks(
    store: DocStore, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    await seed(store)
    entered = asyncio.Event()
    real_rename = docs_module._rename_directory_once

    def blocked_rename(*args: object, **kwargs: object) -> None:
        entered.set()
        raise docs_module.FolderInUse("synthetic busy folder")

    monkeypatch.setattr(docs_module, "_rename_directory_once", blocked_rename)
    monkeypatch.setattr(docs_module, "RENAME_RETRY_SECONDS", 60)
    task = asyncio.create_task(mutate(store, operation))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(docs_module, "_rename_directory_once", real_rename)
    async with asyncio.timeout(2):
        async with store.transitions.final_snapshot():
            pass
        await store.delete_folder("source")


async def test_injected_gate_is_preserved_and_defaults_are_not_shared(store: DocStore) -> None:
    gate = BindingTransitions()
    injected = DocStore(store.paths, transitions=gate)
    assert injected.transitions is gate
    assert DocStore(store.paths).transitions is not store.transitions
    async with gate.final_snapshot():
        with pytest.raises(MutationBusy):
            await injected.write("refused.md", "# Refused\n")


@pytest.mark.parametrize("external_change", ["same_size_mtime", "create", "delete", "rename"])
async def test_final_raw_snapshot_detects_external_fs_races_without_overwriting_them(
    store: DocStore, external_change: str,
) -> None:
    await seed(store)
    target = store.resolve("source/doc.md")
    with pytest.raises(SnapshotChangedOnDisk):
        async with store.final_snapshot() as snapshot:
            captured = dict(snapshot.docs)
            assert captured["source/doc.md"] == b"# Original\n"
            if external_change == "same_size_mtime":
                before = target.stat()
                target.write_bytes(b"# External\n")
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
                assert target.stat().st_size == before.st_size
            elif external_change == "create":
                (store.directory / "external.markdown").write_bytes(b"# External\r\n")
            elif external_change == "delete":
                target.unlink()
            else:
                target.rename(target.with_name("external.md"))
            external_tree = raw_tree(store)
            # A generation commit must do the same content-based recheck as normal exit.
            with pytest.raises(SnapshotChangedOnDisk):
                snapshot.advance_generation()
            assert store.transitions.generation == 0
    assert raw_tree(store) == external_tree
    assert store.bus.events == []
    async with asyncio.timeout(2):
        await store.write("after.md", "# Gate reopened\n")


async def test_final_snapshot_is_raw_read_only_and_its_capabilities_expire(store: DocStore) -> None:
    # Invalid UTF-8/CRLF must survive the snapshot; opening it creates no bytes or events.
    (store.directory / "raw.txt").write_bytes(b"\xff\r\nraw\r\n")
    before = raw_tree(store)
    async with store.final_snapshot() as snapshot:
        assert snapshot.docs == (("raw.txt", b"\xff\r\nraw\r\n"),)
        assert len(snapshot.fingerprint) == 64
        snapshot.verify()
        with pytest.raises(MutationBusy):
            await store.write("refused.md", "# Refused\n")
        assert raw_tree(store) == before
        assert store.bus.events == []
    with pytest.raises(RuntimeError):
        snapshot.verify()
    with pytest.raises(RuntimeError):
        snapshot.advance_generation()
    assert store.transitions.generation == 0


@pytest.mark.parametrize("operation", MUTATIONS)
async def test_stale_generation_wins_even_when_the_entire_source_tree_is_missing(
    store: DocStore, operation: str,
) -> None:
    async with store.transitions.final_snapshot() as fence:
        fence.advance_generation()
    before = raw_tree(store)
    with pytest.raises(StaleBindingGeneration):
        await mutate(store, operation, expected_binding_generation=0)
    assert raw_tree(store) == before
    assert store._cached is None
    assert store.bus.events == []


async def test_cancelling_writer_waiting_for_critical_section_releases_admission(
    store: DocStore,
) -> None:
    first = await store.transitions.admit()
    try:
        async with first.critical():
            writer = asyncio.create_task(store.write("not-written.md", "# Cancelled\n"))
            await asyncio.sleep(0)
            assert store.transitions.active_mutations == 2
            writer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await writer
            assert store.transitions.active_mutations == 1
    finally:
        first.release()
    async with asyncio.timeout(2):
        async with store.final_snapshot() as snapshot:
            assert snapshot.docs == ()
    assert store.bus.events == []


async def test_same_relative_paths_in_two_roots_have_independent_gates(store: DocStore) -> None:
    other_paths = ProjectPaths.for_project(store.paths.root.parent / "project-b")
    other_paths.docs.mkdir(parents=True)
    other = DocStore(other_paths, RecordingBus())  # type: ignore[arg-type]
    await seed(store)
    await seed(other)
    assert store.transitions is not other.transitions
    before = raw_tree(store)
    async with store.transitions.final_snapshot() as fence:
        fence.advance_generation()
        await other.write("source/doc.md", "# B only\n", expected_binding_generation=0)
        with pytest.raises(MutationBusy):
            await store.write("source/doc.md", "# A blocked\n")
    assert raw_tree(store) == before
    assert other.read("source/doc.md")["content"] == "# B only\n"
    assert other.transitions.generation == 0


def test_context_uses_one_gate_for_docs_teams_and_remote(project: Path) -> None:
    context = AppContext.create(str(project), AuthSettings())
    assert context.remote_transitions is context.docs.transitions
    assert context.teams.docs is context.docs
    assert context.sync.docs is context.docs


@pytest.mark.parametrize("delete", [False, True], ids=["write", "delete"])
async def test_existing_remote_apply_cannot_bypass_bound_docstore_snapshot(
    store: DocStore, delete: bool,
) -> None:
    await seed(store)
    config = AppConfig()
    service = RemoteSync(store.paths, lambda: config, store, store.bus, token=lambda _: None)
    service._state = SyncState(path=store.paths.root / "synthetic-state.json")
    revision = _Remote(rev=7, hash=None if delete else "synthetic-hash", content=None if delete else "# Remote\n")
    before, cached = raw_tree(store), store._cached
    async with store.transitions.final_snapshot():
        with pytest.raises(MutationBusy):
            await service._apply("source/doc.md", revision, None)
    assert raw_tree(store) == before
    assert store._cached is cached
    assert store.bus.events == []
    assert service._state.docs == {}, "a refused apply is not an agreed revision"
    await service._apply("source/doc.md", revision, None)
    assert service._state.docs["source/doc.md"].rev == 7
    assert store.resolve("source/doc.md").exists() is not delete


async def test_existing_mcp_handler_stays_bound_to_its_original_context(store: DocStore) -> None:
    await seed(store)
    config = AppConfig()
    config.teams.memory_enabled = False
    orchestrator = SimpleNamespace(config=config, docs=store, db=None, authoring=None)
    member = TeamMember(id="synthetic-agent", name="Synthetic agent", role="Developer")
    toolset = build_toolset(orchestrator, member=member)
    assert toolset is not None, "install the backend teams test dependencies; do not skip MCP coverage"
    handler = next(tool.handler for tool in toolset.definitions if tool.name == "write_doc")
    # Replacing the selection after tool construction must not retarget the captured docs.
    other_paths = ProjectPaths.for_project(store.paths.root.parent / "project-b")
    other_paths.docs.mkdir(parents=True)
    other = DocStore(other_paths)
    orchestrator.docs = other
    before = raw_tree(store)
    async with store.transitions.final_snapshot():
        with pytest.raises(MutationBusy):
            await handler({"path": "source/doc.md", "content": "# From MCP\n"})
    assert raw_tree(store) == before
    assert store.bus.events == []
    assert not other.resolve("source/doc.md").exists()
    result = await handler({"path": "source/doc.md", "content": "# From MCP\n"})
    assert not result.get("is_error")
    assert store.read("source/doc.md")["content"] == "# From MCP\n"
    assert not other.resolve("source/doc.md").exists()
