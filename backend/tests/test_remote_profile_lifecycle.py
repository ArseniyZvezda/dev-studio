"""VS-141 / activation plan C5 (§§2, 4): fail-closed admission, NOT S4/S5.

Real S2 writes use only pytest's synthetic homes. Authoritative inventory below
is explicit *test evidence* for the future binding/drain owners, never inferred
from an empty resident registry. HTTP Verify uses the real HTTP/guard services
with MockTransport; no Remote installation, listeners or real keys are needed.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from voxarium_studio import main
from voxarium_studio.build_info import BuildInfo
from voxarium_studio.core import context as context_module, workspace
from voxarium_studio.core.config import AppConfig
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project_identity import ProjectIdentity
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.sync import RemoteSync, catalog_runtime, credentials, profiles
from voxarium_studio.sync.bindings import BindingRecord, RemoteBindings, bindings_file
from voxarium_studio.sync.catalog import CatalogError
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.profile_lifecycle import BindingInventory, ProfileLifecycle
from voxarium_studio.sync.profiles import ProfileError, RemoteProfiles
from voxarium_studio.ws.bus import EventBus


URL = "https://one.invalid/deployment/prefix"
OTHER_URL = "https://two.invalid/other"
SECRET = "VS141-SYNTHETIC-NOT-A-REAL-KEY"
MUTATIONS = ("credential", "clear", "url", "delete", "import", "recover")


@pytest.fixture(autouse=True)
async def safety(monkeypatch):
    """Count forbidden attempts even when an application error mapper swallows them.

    The pytest loop is already constructed before socket guards are installed;
    asyncio's private wakeup pipe is not an application listener or connection.
    """
    attempts = Counter()

    def deny(kind):
        def forbidden(*_args, **_kwargs):
            attempts[kind] += 1
            raise AssertionError("VS-141 forbidden " + kind)
        return forbidden

    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)
    for method in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, method, deny("network/listener"))
    for method in ("create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, method, deny("network/DNS"))
    monkeypatch.setattr(subprocess, "Popen", deny("process"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny("HTTP"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny("HTTP"))
    yield attempts
    assert not attempts, attempts


class Store:
    def __init__(self, home):
        self.home = home
        self.evidence = BindingInventory(state="known")
        self.lifecycle = ProfileLifecycle(
            profile_reader=lambda pid: self.repo.get(pid),
            inventory=lambda _pid: self.evidence,
        )
        self.repo = RemoteProfiles(consumer_guard=self.lifecycle.consumer_guard)
        self.profile = self.repo.create("Synthetic A", URL)
        # Fixture setup, not lifecycle authorization: no consumer exists yet.
        credentials.save_profile_token(self.profile.profile_id, URL, SECRET, expected_revision=0)

    @property
    def pid(self):
        return self.profile.profile_id

    def mutate(self, kind, *, repo=None):
        repo = repo or self.repo
        revision = self.profile.revision
        if kind == "credential":
            return repo.set_credential(self.pid, SECRET + "-new", expected_revision=revision)
        if kind == "clear":
            return repo.clear_credential(self.pid, expected_revision=revision)
        if kind == "url":
            return repo.change_url(self.pid, OTHER_URL, expected_revision=revision,
                                   clear_credential=True)
        if kind == "delete":
            return repo.delete(self.pid, expected_revision=revision)
        if kind == "import":
            return repo.import_legacy_credential(self.pid, expected_revision=revision)
        if kind == "recover":
            return repo.recover(self.pid, expected_revision=revision)
        raise AssertionError(kind)

    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.home.rglob("*") if p.is_file()}


@pytest.fixture
def store(studio_home):
    return Store(studio_home)


def failure(callback, code="consumer_lifecycle_unavailable"):
    with pytest.raises(ProfileError) as caught:
        callback()
    assert caught.value.args == (code,)
    assert SECRET not in repr(caught.value)
    return caught.value


def snapshot(lifecycle, pid):
    with lifecycle.consumer_guard(pid) as state:
        return state


def resident(root, *, url=URL, enabled=True):
    return SimpleNamespace(
        paths=SimpleNamespace(root=Path(root)),
        config=SimpleNamespace(sync=SimpleNamespace(url=url, enabled=enabled)),
    )


@pytest.mark.parametrize("inventory", [None, BindingInventory(), BindingInventory(state="recovering")])
def test_explicit_recovery_guard_still_requires_authoritative_inventory(inventory):
    lifecycle = ProfileLifecycle(profile_reader=lambda _pid: pytest.fail("pending metadata read"),
        inventory=None if inventory is None else lambda _pid: inventory)
    def attempt():
        with lifecycle.recovery_guard("synthetic-id", urls=(URL, OTHER_URL)):
            pytest.fail("unknown inventory admitted recovery")
    failure(attempt)


def test_recovery_guard_covers_both_journal_urls_and_same_id_lease(tmp_path):
    lifecycle = ProfileLifecycle(profile_reader=lambda _pid: pytest.fail("pending metadata read"),
        inventory=lambda _pid: BindingInventory(state="known", drained=True))
    old = resident(tmp_path / "old-url", url=URL, enabled=False)
    target = resident(tmp_path / "target-url", url=OTHER_URL)
    unrelated = resident(tmp_path / "unrelated", url="https://unrelated.invalid")
    for ctx in (old, target, unrelated):
        lifecycle.register_context(ctx)
    lease = lifecycle.attach(root=str(tmp_path / "inflight"), url=OTHER_URL)
    with lifecycle.recovery_guard("synthetic-id", urls=(URL, OTHER_URL)) as state:
        assert set(state.bound_roots) == {str(old.paths.root.resolve()), str(target.paths.root.resolve())}
        assert set(state.active_roots) == {str(target.paths.root.resolve()), str((tmp_path / "inflight").resolve())}
        assert not state.drained
        failure(lambda: lifecycle.attach(root=str(tmp_path / "racing"), url=URL))
    lease.release()
    lifecycle.unregister_context(target)
    with lifecycle.recovery_guard("synthetic-id", urls=(URL, OTHER_URL)) as state:
        assert state.bound_roots == (str(old.paths.root.resolve()),)
        assert not state.active_roots and state.drained


@pytest.mark.parametrize("urls", [(), ("HTTPS://ONE.INVALID/",), ("ftp://invalid",), (None,), [URL]])
def test_recovery_guard_rejects_unproven_url_shapes(urls):
    lifecycle = ProfileLifecycle(profile_reader=lambda _pid: pytest.fail("metadata read"),
        inventory=lambda _pid: BindingInventory(state="known"))
    def attempt():
        with lifecycle.recovery_guard("synthetic-id", urls=urls):
            pytest.fail("invalid URLs admitted")
    failure(attempt)


@pytest.mark.parametrize("kind", MUTATIONS)
@pytest.mark.parametrize("inventory", [None, BindingInventory(), BindingInventory(state="recovering")],
                         ids=["no-S4-owner", "unknown-empty", "recovering-empty"])
def test_unknown_inventory_never_means_unbound_and_refuses_before_writes(store, kind, inventory):
    lifecycle = ProfileLifecycle(profile_reader=store.repo.get,
                                 inventory=None if inventory is None else lambda _pid: inventory)
    repo = RemoteProfiles(consumer_guard=lifecycle.consumer_guard)
    before = store.snapshot()
    failure(lambda: store.mutate(kind, repo=repo))
    assert store.snapshot() == before


@pytest.mark.parametrize("evidence", [
    None, {}, BindingInventory(state="invalid"), BindingInventory(state="known", drained=1),
    BindingInventory(state="known", bound_roots=[]),
    BindingInventory(state="known", active_roots=("",)),
    BindingInventory(state="known", bound_roots=(None,)),
])
def test_malformed_inventory_is_not_positive_evidence(store, evidence):
    lifecycle = ProfileLifecycle(profile_reader=store.repo.get, inventory=lambda _pid: evidence)
    before = store.snapshot()
    failure(lambda: snapshot(lifecycle, store.pid))
    assert store.snapshot() == before


def test_inventory_or_profile_observation_failure_is_unknown_not_idle(store):
    def broken(_pid):
        raise RuntimeError(SECRET)

    for lifecycle in (
        ProfileLifecycle(profile_reader=store.repo.get, inventory=broken),
        ProfileLifecycle(profile_reader=broken, inventory=lambda _pid: BindingInventory(state="known")),
        ProfileLifecycle(profile_reader=lambda _pid: object(),
                         inventory=lambda _pid: BindingInventory(state="known")),
    ):
        error = failure(lambda: snapshot(lifecycle, store.pid))
        assert error.__cause__ is None
        assert error.__context__ is None


@pytest.mark.parametrize("kind", ["credential", "clear", "url", "delete"])
def test_only_explicit_known_idle_evidence_allows_real_s2_publication(store, kind):
    result = store.mutate(kind)
    assert not store.repo._journal().exists()
    if kind == "delete":
        failure(lambda: store.repo.get(store.pid), "profile_not_found")
        assert credentials.load_profile_token(store.pid, URL) is None
    else:
        assert result.revision == store.profile.revision + 1
        assert store.repo.get(store.pid) == result
        assert result.url == (OTHER_URL if kind == "url" else URL)
        assert credentials.load_profile_token(store.pid, result.url) == (
            SECRET + "-new" if kind == "credential" else None)


@pytest.mark.parametrize("drained", [False, True])
@pytest.mark.parametrize("kind", ["url", "delete"])
def test_unopened_private_binding_is_still_bound_even_with_drained_ticket(store, tmp_path, kind, drained):
    unopened = str(tmp_path / "never-opened-private-root")
    assert not Path(unopened).exists()
    store.evidence = BindingInventory(state="known", bound_roots=(unopened,), drained=drained)
    assert snapshot(store.lifecycle, store.pid).bound_roots == (unopened,)
    before = store.snapshot()
    failure(lambda: store.mutate(kind), "profile_in_use")
    assert store.snapshot() == before and not Path(unopened).exists()


@pytest.mark.parametrize("kind", ["credential", "clear", "import", "recover"])
def test_authoritative_active_but_not_drained_refuses_without_forced_stop(store, kind):
    store.evidence = BindingInventory(state="known", active_roots=("private-unopened",))
    before = store.snapshot()
    failure(lambda: store.mutate(kind), "profile_consumers_not_drained")
    assert store.snapshot() == before


def test_future_drained_evidence_permits_key_change_not_unbinding(store, tmp_path):
    root = str(tmp_path / "private-binding")
    store.evidence = BindingInventory(state="known", bound_roots=(root,), active_roots=(root,), drained=True)
    new = store.mutate("credential")
    assert new.revision == 2
    assert credentials.load_profile_token(store.pid, URL) == SECRET + "-new"
    assert snapshot(store.lifecycle, store.pid).bound_roots == (root,)
    for callback in (lambda: store.repo.change_url(store.pid, OTHER_URL, expected_revision=2,
                                                  clear_credential=True),
                     lambda: store.repo.delete(store.pid, expected_revision=2)):
        failure(callback, "profile_in_use")


def test_hidden_residents_two_legacy_roots_match_url_not_selection(store, tmp_path):
    selected = resident(tmp_path / "selected-other-server", url=OTHER_URL)
    hidden_a = resident(tmp_path / "hidden-a", url="wss://ONE.invalid/deployment/prefix/")
    hidden_b = resident(tmp_path / "hidden-b")
    for ctx in (selected, hidden_a, hidden_b, hidden_a):
        store.lifecycle.register_context(ctx)  # idempotent, including duplicate registration
    state = snapshot(store.lifecycle, store.pid)
    expected = tuple(sorted(str(ctx.paths.root.resolve()) for ctx in (hidden_a, hidden_b)))
    assert state.bound_roots == state.active_roots == expected
    before = store.snapshot()
    failure(lambda: store.mutate("credential"), "profile_consumers_not_drained")
    # Moving the arbitrary selection marker does not alter lifecycle's inventory.
    selected = hidden_b
    assert snapshot(store.lifecycle, store.pid) == state
    assert selected is hidden_b and store.snapshot() == before
    store.lifecycle.unregister_context(hidden_a)
    assert snapshot(store.lifecycle, store.pid).active_roots == (str(hidden_b.paths.root.resolve()),)


def test_disabled_legacy_context_is_bound_but_not_active(store, tmp_path):
    ctx = resident(tmp_path / "disabled", enabled=False)
    store.lifecycle.register_context(ctx)
    state = snapshot(store.lifecycle, store.pid)
    assert state.bound_roots == (str(ctx.paths.root.resolve()),) and state.active_roots == ()
    failure(lambda: store.mutate("url"), "profile_in_use")
    result = store.mutate("credential")
    assert result.revision == 2
    ctx.config.sync.enabled = True
    assert snapshot(store.lifecycle, store.pid).active_roots == state.bound_roots


def test_local_active_consumer_cannot_borrow_future_inventory_drained_ticket(store, tmp_path):
    store.evidence = BindingInventory(state="known", drained=True)
    ctx = resident(tmp_path / "live")
    store.lifecycle.register_context(ctx)
    assert snapshot(store.lifecycle, store.pid).drained is False
    failure(lambda: store.mutate("credential"), "profile_consumers_not_drained")
    store.lifecycle.unregister_context(ctx)
    lease = store.lifecycle.operation(store.pid, kind="read")
    assert snapshot(store.lifecycle, store.pid).drained is False
    failure(lambda: store.mutate("clear"), "profile_consumers_not_drained")
    lease.release()
    assert snapshot(store.lifecycle, store.pid).drained is True


@pytest.mark.parametrize("bad", [SimpleNamespace(), resident("unused", url="ftp://invalid")])
def test_unobservable_resident_context_is_not_silently_ignored(store, bad):
    store.lifecycle.register_context(bad)
    failure(lambda: snapshot(store.lifecycle, store.pid))
    store.lifecycle.unregister_context(bad)
    assert snapshot(store.lifecycle, store.pid).active_roots == ()


def admission(store, kind, root):
    if kind in {"attach", "reconnect"}:
        return store.lifecycle.attach(root=str(root), url=URL,
                                      profile_id=store.pid if kind == "attach" else None)
    return store.lifecycle.operation(store.pid, kind=kind)


@pytest.mark.parametrize("kind", ["attach", "reconnect", "verify", "read", "create"])
@pytest.mark.parametrize("mutation", ["credential", "url", "delete"])
def test_consumer_wins_race_sensitive_mutation_refuses_until_lease_release(store, tmp_path, kind, mutation):
    before = store.snapshot()
    with admission(store, kind, tmp_path / "root") as lease:
        state = snapshot(store.lifecycle, store.pid)
        assert state.active_roots
        code = "profile_consumers_not_drained" if mutation == "credential" else "profile_in_use"
        failure(lambda: store.mutate(mutation), code)
        assert store.snapshot() == before
    lease.release()  # duplicate cleanup is harmless
    assert snapshot(store.lifecycle, store.pid).active_roots == ()
    store.mutate(mutation)


@pytest.mark.parametrize("phase", ["_begin", "_end"])
@pytest.mark.parametrize("mutation", ["credential", "url", "delete"])
def test_mutation_wins_race_fences_all_admission_through_final_publication(
    store, tmp_path, monkeypatch, phase, mutation,
):
    reached, finish = Event(), Event()
    original = getattr(store.repo, phase)

    def pause(*args, **kwargs):
        reached.set()
        assert finish.wait(5), "test failed to release S2 publication"
        return original(*args, **kwargs)

    monkeypatch.setattr(store.repo, phase, pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(store.mutate, mutation)
        try:
            assert reached.wait(5), "S2 did not reach publication fence"
            # These calls run on the ACK/event-loop thread, not behind an await
            # or a blocking lock. The worker is deliberately waiting for us.
            for kind in ("attach", "reconnect", "verify", "read", "create"):
                failure(lambda kind=kind: admission(store, kind, tmp_path / kind))
            failure(lambda: store.mutate("clear"))
            if phase == "_end":
                assert store.repo._journal().exists()
                if mutation == "delete":
                    failure(lambda: store.repo.get(store.pid), "profile_not_found")
                else:
                    assert store.repo.get(store.pid).revision == 2
        finally:
            finish.set()
        writer.result(timeout=5)
    assert not store.repo._journal().exists()
    if mutation == "delete":
        failure(lambda: store.lifecycle.operation(store.pid, kind="read"), "profile_not_found")
    else:
        with store.lifecycle.operation(store.pid, kind="read"):
            assert snapshot(store.lifecycle, store.pid).active_roots == ("catalog:read",)


def test_ack_lease_release_does_not_wait_for_synchronous_publication(store):
    other = store.repo.create("Other", OTHER_URL)
    lease = store.lifecycle.operation(other.profile_id, kind="verify")
    # S2 may hold a global lifecycle lock while an unrelated ACK completes.
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.lifecycle.consumer_guard(store.pid):
            executor.submit(lease.release).result(timeout=1)
    assert snapshot(store.lifecycle, other.profile_id).active_roots == ()


def test_context_retirement_does_not_block_or_abort_cleanup_during_publication(store, tmp_path):
    ctx = resident(tmp_path / "stopping-other-project", url=OTHER_URL)
    other = store.repo.create("Other", OTHER_URL)
    store.lifecycle.register_context(ctx)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.lifecycle.consumer_guard(store.pid):
            executor.submit(store.lifecycle.unregister_context, ctx).result(timeout=1)
    assert snapshot(store.lifecycle, other.profile_id).bound_roots == ()
    # Reopening the same object after completed retirement is a fresh resident.
    store.lifecycle.register_context(ctx)
    assert snapshot(store.lifecycle, other.profile_id).bound_roots == (str(ctx.paths.root.resolve()),)


def test_exceptional_lease_exit_releases_and_multiple_live_leases_do_not_collapse(store, tmp_path):
    first = admission(store, "attach", tmp_path / "same-root")
    with pytest.raises(RuntimeError, match="synthetic-body-error"):
        with admission(store, "reconnect", tmp_path / "same-root"):
            raise RuntimeError("synthetic-body-error")
    # Root de-duplication in snapshots must not release another lease for it.
    assert snapshot(store.lifecycle, store.pid).active_roots == (str((tmp_path / "same-root").resolve()),)
    first.release()
    first.release()
    assert snapshot(store.lifecycle, store.pid).active_roots == ()


def test_lock_order_is_lifecycle_then_config_then_credentials(store, monkeypatch, tmp_path):
    order = []
    transaction = profiles.application_config_transaction
    save = credentials.save_profile_token

    @contextmanager
    def config_transaction():
        order.append("config-enter")
        failure(lambda: admission(store, "attach", tmp_path / "during-config"))
        with transaction():
            yield
        order.append("config-exit")

    def credential_write(*args, **kwargs):
        order.append("credential-write")
        failure(lambda: store.lifecycle.operation(store.pid, kind="verify"))
        return save(*args, **kwargs)

    monkeypatch.setattr(profiles, "application_config_transaction", config_transaction)
    monkeypatch.setattr(credentials, "save_profile_token", credential_write)
    store.mutate("credential")
    assert order == ["config-enter", "credential-write", "config-exit"]


def test_rename_is_label_only_even_with_unknown_inventory_and_live_consumers(store, tmp_path):
    lifecycle = ProfileLifecycle(profile_reader=store.repo.get)  # production: no S4 reader
    repo = RemoteProfiles(consumer_guard=lifecycle.consumer_guard)
    ctx = resident(tmp_path / "running")
    ctx.sync = SimpleNamespace(reconfigure=Mock(), stop=Mock())
    ctx.jobs = SimpleNamespace(shutdown=Mock())
    lifecycle.register_context(ctx)
    with lifecycle.attach(root=str(ctx.paths.root), url=URL):
        key_before = credentials.keys_file().read_bytes()
        result = repo.rename(store.pid, "New label", expected_revision=1)
        assert result.name == "New label" and result.url == URL and result.revision == 2
        assert credentials.keys_file().read_bytes() == key_before
    ctx.sync.reconfigure.assert_not_called()
    ctx.sync.stop.assert_not_called()
    ctx.jobs.shutdown.assert_not_called()


def test_closed_admission_is_terminal_but_cleanup_is_allowed(store, tmp_path):
    ctx = resident(tmp_path / "already-resident")
    store.lifecycle.register_context(ctx)
    lease = admission(store, "attach", ctx.paths.root)
    store.lifecycle.close_admission()
    store.lifecycle.close_admission()
    for action in (lambda: admission(store, "attach", tmp_path / "new"),
                   lambda: admission(store, "read", tmp_path),
                   lambda: store.lifecycle.register_context(ctx),
                   lambda: snapshot(store.lifecycle, store.pid),
                   lambda: store.mutate("credential")):
        failure(action)
    lease.release()
    store.lifecycle.unregister_context(ctx)
    failure(lambda: admission(store, "create", tmp_path))


def test_close_admission_does_not_wait_for_already_admitted_publication(store, monkeypatch, tmp_path):
    reached, finish = Event(), Event()
    original = store.repo._end

    def finish_publication():
        reached.set()
        assert finish.wait(5)
        original()

    monkeypatch.setattr(store.repo, "_end", finish_publication)
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(store.mutate, "credential")
        try:
            assert reached.wait(5)
            store.lifecycle.close_admission()  # must not wait for the writer
            failure(lambda: admission(store, "attach", tmp_path / "closed"))
        finally:
            finish.set()
        assert writer.result(timeout=5).revision == 2
    failure(lambda: admission(store, "verify", tmp_path))
    assert credentials.load_profile_token(store.pid, URL) == SECRET + "-new"


def test_invalid_operation_and_mismatched_profile_url_never_admit(store, tmp_path):
    failure(lambda: store.lifecycle.operation(store.pid, kind="forced-drain"))
    failure(lambda: store.lifecycle.attach(root=str(tmp_path), url=OTHER_URL, profile_id=store.pid),
            "profile_revision_conflict")
    assert snapshot(store.lifecycle, store.pid).active_roots == ()


class DesktopStub:
    """Keep the real workspace registry; omit unrelated chat snapshot pumps."""
    def __init__(self, state):
        self.attached = []

    def attach(self, descriptor):
        self.attached.append(descriptor)

    async def close(self):
        pass


def context_stub(root, lifecycle, monkeypatch, *, enabled=True, url=URL):
    """Execute real AppContext.startup/shutdown, replacing only service work."""
    root.mkdir(exist_ok=True)
    ctx = AppContext.__new__(AppContext)
    ctx.paths = ProjectPaths.for_project(root)
    ctx.config = AppConfig(sync={"enabled": enabled, "url": url, "space": "synthetic"})
    ctx.auth = AuthSettings()
    ctx.bus = EventBus()
    ctx.remote_profile_lifecycle = lifecycle
    ctx.docs = SimpleNamespace(migrate_folder_styles=Mock())
    ctx.db = SimpleNamespace(create_all=AsyncMock(), create_missing_indexes=AsyncMock(), close=AsyncMock())
    ctx.teams = SimpleNamespace(
        ensure_task_numbers=AsyncMock(), sync_definitions=AsyncMock(),
        subagents=SimpleNamespace(recover=AsyncMock()), resume_limit_waits=AsyncMock(),
        resume_autostart_tasks=AsyncMock(), cancel_limit_waits=Mock(), cancel_commit_jobs=AsyncMock(),
    )
    ctx.chats = SimpleNamespace(producer=SimpleNamespace(
        restore_paused=AsyncMock(), start=AsyncMock(), shutdown=AsyncMock(),
    ))
    ctx.detect_vcs = AsyncMock()
    ctx.watcher = SimpleNamespace(start=Mock(), stop=AsyncMock())
    ctx.sync = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    ctx.updates = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    ctx.automations = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    ctx.jobs = SimpleNamespace(shutdown=AsyncMock())
    ctx._teams_ready = False
    ctx._env_watch = None
    ctx._start_plugin_hooks = Mock()
    ctx._start_plugin_envs = Mock()
    ctx._stop_plugin_hooks = AsyncMock()
    monkeypatch.setattr(context_module, "finish_data_migration", AsyncMock())
    return ctx


@pytest.mark.parametrize("failed_stop", [False, True])
async def test_real_context_registers_before_first_await_and_unregisters_only_after_sync_stop(
    store, tmp_path, monkeypatch, failed_stop,
):
    ctx = context_stub(tmp_path / "context", store.lifecycle, monkeypatch)
    root = str(ctx.paths.root.resolve())

    async def first_service():
        assert snapshot(store.lifecycle, store.pid).active_roots == (root,)

    def first_thread_service():
        # This is run by startup's first await, before db.create_all.
        assert snapshot(store.lifecycle, store.pid).active_roots == (root,)

    ctx.docs.migrate_folder_styles.side_effect = first_thread_service
    ctx.db.create_all.side_effect = first_service
    await ctx.startup()
    entered, complete = asyncio.Event(), asyncio.Event()

    async def stop():
        assert snapshot(store.lifecycle, store.pid).active_roots == (root,)
        entered.set()
        await complete.wait()
        if failed_stop:
            raise RuntimeError("synthetic-stop-failure")

    ctx.sync.stop.side_effect = stop
    stopping = asyncio.create_task(ctx.shutdown())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        failure(lambda: store.mutate("credential"), "profile_consumers_not_drained")
    finally:
        complete.set()
    if failed_stop:
        with pytest.raises(RuntimeError, match="synthetic-stop-failure"):
            await stopping
        assert snapshot(store.lifecycle, store.pid).active_roots == (root,)
        ctx.jobs.shutdown.assert_not_called()
        ctx.sync.stop.side_effect = None
        await ctx.shutdown()
    else:
        await stopping
    assert snapshot(store.lifecycle, store.pid).bound_roots == ()


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_workspace_failed_start_is_not_published_and_uncertain_cleanup_remains_guarded(
    store, tmp_path, monkeypatch, cleanup_fails,
):
    monkeypatch.setattr(workspace, "DesktopEventSource", DesktopStub)
    incumbent = context_stub(tmp_path / "incumbent", store.lifecycle, monkeypatch, url=OTHER_URL)
    state = SimpleNamespace(ctx=incumbent, remote_profile_lifecycle=store.lifecycle)
    workspace.initialise_workspace(state)
    target = context_stub(tmp_path / "failed", store.lifecycle, monkeypatch)

    async def startup_failure():
        assert str(target.paths.root.resolve()) in snapshot(store.lifecycle, store.pid).active_roots
        raise RuntimeError("synthetic-start-failure")

    target.db.create_all.side_effect = startup_failure
    if cleanup_fails:
        target.sync.stop.side_effect = RuntimeError("synthetic-stop-failure")
    monkeypatch.setattr(workspace.AppContext, "create", staticmethod(lambda *_a, **_kw: target))
    with pytest.raises(RuntimeError, match="synthetic-start-failure"):
        await workspace.open_project(state, target.paths.root)
    assert state.ctx is incumbent
    assert list(state.project_contexts.values()) == [incumbent]
    assert target.paths.root.resolve() not in state.project_roots
    assert snapshot(store.lifecycle, store.pid).bound_roots == (
        (str(target.paths.root.resolve()),) if cleanup_fails else ())
    if cleanup_fails:
        failure(lambda: store.mutate("credential"), "profile_consumers_not_drained")
        target.sync.stop.side_effect = None
        await target.shutdown()
    assert store.mutate("credential").revision == 2
    await workspace.shutdown_workspace(state)


async def test_workspace_switch_and_a_mutation_never_cancel_b_jobs_or_retire_hidden_context(
    store, tmp_path, monkeypatch,
):
    monkeypatch.setattr(workspace, "DesktopEventSource", DesktopStub)
    a = context_stub(tmp_path / "a", store.lifecycle, monkeypatch)
    b = context_stub(tmp_path / "b", store.lifecycle, monkeypatch, url=OTHER_URL)
    state = SimpleNamespace(ctx=a, remote_profile_lifecycle=store.lifecycle)
    workspace.initialise_workspace(state)
    monkeypatch.setattr(workspace.AppContext, "create", staticmethod(lambda *_a, **_kw: b))
    assert await workspace.open_project(state, b.paths.root) is b
    b_started = b.sync.start.await_count
    finished = asyncio.Event()
    running_b_job = asyncio.create_task(finished.wait(), name="VS141-synthetic-B-job")
    try:
        for selected in (b, a, b):
            assert await workspace.open_project(state, selected.paths.root) is selected
            assert snapshot(store.lifecycle, store.pid).bound_roots == (str(a.paths.root.resolve()),)
            failure(lambda: store.mutate("credential"), "profile_consumers_not_drained")
            row = store.repo.get(store.pid)
            store.repo.rename(store.pid, "A only", expected_revision=row.revision)
            assert not running_b_job.done() and not running_b_job.cancelling()
            a.jobs.shutdown.assert_not_called()
            b.jobs.shutdown.assert_not_called()
            a.sync.stop.assert_not_called()
            b.sync.stop.assert_not_called()
        assert b.sync.start.await_count == b_started == 1
        assert set(state.project_contexts.values()) == {a, b}
    finally:
        finished.set()
        await running_b_job
        await workspace.shutdown_workspace(state)


@pytest.mark.parametrize("first,second", [("start", "reconfigure"), ("reconfigure", "stop"), ("stop", "start")])
async def test_public_sync_transitions_are_serialized_without_blocking_loop(
    store, tmp_path, monkeypatch, first, second,
):
    sync = context_module._ProfileRemoteSync(
        ProjectPaths.for_project(tmp_path / "transition"), AppConfig, SimpleNamespace(), EventBus(),
        lifecycle=lambda: store.lifecycle,
    )
    entered = {name: asyncio.Event() for name in (first, second)}
    release = {name: asyncio.Event() for name in (first, second)}
    calls = []

    def transition(name):
        async def run(_self):
            calls.append(name)
            entered[name].set()
            await release[name].wait()
            return {}
        return run

    for name in (first, second):
        monkeypatch.setattr(RemoteSync, name, transition(name))
    a = asyncio.create_task(getattr(sync, first)())
    b = None
    try:
        await asyncio.wait_for(entered[first].wait(), 2)
        b = asyncio.create_task(getattr(sync, second)())
        await asyncio.sleep(0)  # second is queued; event/ACK work still runs
        assert calls == [first] and not entered[second].is_set()
        release[first].set()
        await asyncio.wait_for(entered[second].wait(), 2)
        assert calls == [first, second]
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(*(task for task in (a, b) if task is not None))


@pytest.mark.parametrize("url,space", [("ftp://invalid.invalid", "synthetic"), (URL, "bad/name")])
async def test_invalid_legacy_settings_keep_unconfigured_diagnostic_without_admission(
    store, tmp_path, monkeypatch, url, space,
):
    config = AppConfig(sync={"enabled": True, "url": url, "space": space})
    sync = context_module._ProfileRemoteSync(
        ProjectPaths.for_project(tmp_path / "invalid"), lambda: config, SimpleNamespace(), EventBus(),
        lifecycle=lambda: store.lifecycle,
    )
    attach = Mock(wraps=store.lifecycle.attach)
    monkeypatch.setattr(store.lifecycle, "attach", attach)
    sync._token = Mock()
    sync._publish_status = AsyncMock()
    await sync._start_runner()
    assert sync._state_name == "unconfigured" and sync._runner is None
    attach.assert_not_called()
    sync._token.assert_not_called()
    assert snapshot(store.lifecycle, store.pid).active_roots == ()


@pytest.mark.parametrize("failed", [False, True])
async def test_workspace_retires_only_successful_embedded_context_shutdowns(
    store, tmp_path, failed,
):
    ctx = resident(tmp_path / "embedded")
    ctx.shutdown = AsyncMock(side_effect=RuntimeError("synthetic-stop") if failed else None)
    store.lifecycle.register_context(ctx)
    state = SimpleNamespace(
        project_lock=asyncio.Lock(), desktop_events=DesktopStub(None),
        project_contexts={ctx.paths.root: ctx}, project_registry={}, project_roots={},
        remote_profile_lifecycle=store.lifecycle,
    )
    if failed:
        with pytest.raises(RuntimeError, match="synthetic-stop"):
            await workspace.shutdown_workspace(state)
    else:
        await workspace.shutdown_workspace(state)
    assert snapshot(store.lifecycle, store.pid).bound_roots == (
        (str(ctx.paths.root.resolve()),) if failed else ())
    ctx.shutdown.assert_awaited_once()


@pytest.mark.parametrize("failed", ["start", "stop"])
async def test_legacy_sync_retains_logical_lease_until_successful_stop(store, tmp_path, monkeypatch, failed):
    config = AppConfig(sync={"enabled": True, "url": URL, "space": "synthetic"})
    sync = context_module._ProfileRemoteSync(
        ProjectPaths.for_project(tmp_path / "legacy"), lambda: config, SimpleNamespace(), EventBus(),
        lifecycle=lambda: store.lifecycle,
    )
    start = AsyncMock(side_effect=RuntimeError("synthetic-start") if failed == "start" else None)
    stop = AsyncMock(side_effect=RuntimeError("synthetic-stop") if failed == "stop" else None)
    monkeypatch.setattr(RemoteSync, "_start_runner", start)
    monkeypatch.setattr(RemoteSync, "_stop_runner", stop)
    if failed == "start":
        with pytest.raises(RuntimeError, match="synthetic-start"):
            await sync._start_runner()
    else:
        await sync._start_runner()
        with pytest.raises(RuntimeError, match="synthetic-stop"):
            await sync._stop_runner()
    assert snapshot(store.lifecycle, store.pid).active_roots == (str(sync.paths.root.resolve()),)
    failure(lambda: store.mutate("credential"), "profile_consumers_not_drained")
    stop.side_effect = None
    await sync._stop_runner()
    assert snapshot(store.lifecycle, store.pid).active_roots == ()


async def test_legacy_sync_attach_and_reconfigure_use_same_nonblocking_publication_fence(
    store, tmp_path, monkeypatch,
):
    config = AppConfig(sync={"enabled": True, "url": URL, "space": "synthetic"})
    sync = context_module._ProfileRemoteSync(
        ProjectPaths.for_project(tmp_path / "legacy"), lambda: config, SimpleNamespace(), EventBus(),
        lifecycle=lambda: store.lifecycle,
    )
    start, stop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(RemoteSync, "_start_runner", start)
    monkeypatch.setattr(RemoteSync, "_stop_runner", stop)
    sync._token = Mock(return_value=None)
    with store.lifecycle.consumer_guard(store.pid):
        with pytest.raises(ProfileError, match="consumer_lifecycle_unavailable"):
            await sync._start_runner()
    start.assert_not_called()
    await sync._start_runner()
    with store.lifecycle.consumer_guard(store.pid):
        with pytest.raises(ProfileError, match="consumer_lifecycle_unavailable"):
            await sync.reconfigure()
    assert start.await_count == 1 and stop.await_count == 1
    assert snapshot(store.lifecycle, store.pid).active_roots == ()
    await sync.reconfigure()
    assert start.await_count == 2
    await sync._stop_runner()


class VerifyStream(httpx.AsyncByteStream):
    def __init__(self, *, invalid=False):
        self.reading = asyncio.Event()
        self.body_allowed = asyncio.Event()
        self.closing = asyncio.Event()
        self.close_allowed = asyncio.Event()
        self.invalid = invalid
        self.closed = False

    async def __aiter__(self):
        self.reading.set()
        await self.body_allowed.wait()
        yield (b"{}" if self.invalid else json.dumps({
            "protocol": 1, "user": {"id": "synthetic", "name": "Synthetic", "color": "#abcdef"},
        }).encode())

    async def aclose(self):
        self.closing.set()
        await self.close_allowed.wait()
        self.closed = True


def runtime_with_mock_http(store, monkeypatch, *, stream=None, known=True):
    requests = []

    async def reply(request):
        requests.append(request)
        assert request.method == "GET" and not request.content and not request.url.query
        assert request.headers["cache-control"] == "no-store"
        if str(request.url) == URL + "/healthz":
            assert request.headers.get("authorization") is None
            return httpx.Response(200, json=dict(status="ok", service="voxarium_studio_remote",
                                                 version="synthetic", protocol=1))
        assert str(request.url) == URL + "/api/v1/me"
        assert request.headers.get("authorization") == "Bearer " + SECRET
        if stream is not None:
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
        return httpx.Response(200, json=dict(protocol=1, user={
            "id": "synthetic", "name": "Synthetic", "color": "#abcdef",
        }))

    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport",
                        lambda: CatalogHttpTransport(transport=httpx.MockTransport(reply)))
    owner = catalog_runtime.create_remote_catalog_runtime()
    if known:
        # EXPLICIT future S4/S5 evidence, injected only in this test composition.
        owner.lifecycle = ProfileLifecycle(profile_reader=owner._get,
                                           inventory=lambda _pid: store.evidence)
    return owner, requests


@pytest.mark.parametrize("outcome", ["success", "invalid", "cancel", "shutdown"])
async def test_runtime_verify_holds_logical_lease_through_http_cleanup_without_thread_lock(
    store, monkeypatch, outcome,
):
    stream = VerifyStream(invalid=outcome == "invalid")
    owner, requests = runtime_with_mock_http(store, monkeypatch, stream=stream)
    ready = await owner.start()
    repo = owner._repo
    verify = asyncio.create_task(ready.verification.verify(store.pid, expected_revision=1))
    closing = None
    try:
        await asyncio.wait_for(stream.reading.wait(), 2)
        assert snapshot(owner.lifecycle, store.pid).active_roots == ("catalog:verify",)
        failure(lambda: store.mutate("credential", repo=repo), "profile_consumers_not_drained")
        # A guard can enter while HTTP is suspended: no thread lock spans await.
        with owner.lifecycle.consumer_guard(store.pid) as state:
            assert state.active_roots == ("catalog:verify",)
        if outcome == "cancel":
            verify.cancel()
        elif outcome == "shutdown":
            closing = asyncio.create_task(owner.close())
        else:
            stream.body_allowed.set()
        await asyncio.wait_for(stream.closing.wait(), 2)
        assert not verify.done() and not stream.closed
        code = "consumer_lifecycle_unavailable" if outcome == "shutdown" else "profile_consumers_not_drained"
        failure(lambda: store.mutate("credential", repo=repo), code)
        if outcome == "shutdown":
            failure(lambda: owner.lifecycle.operation(store.pid, kind="create"))
            assert not closing.done()
        else:
            assert snapshot(owner.lifecycle, store.pid).active_roots == ("catalog:verify",)
        if outcome == "cancel":
            verify.cancel()  # repeated cancellation still joins response cleanup
            await asyncio.sleep(0)
            assert not verify.done()
        stream.close_allowed.set()
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await verify
        elif outcome == "shutdown":
            with pytest.raises((asyncio.CancelledError, CatalogError, ProfileError)):
                await verify
        else:
            result = await verify
            assert result.state == ("incompatible" if outcome == "invalid" else "valid")
        assert stream.closed
        assert len(requests) == 2
        if closing is not None:
            await closing
        else:
            assert snapshot(owner.lifecycle, store.pid).active_roots == ()
            assert store.mutate("credential", repo=repo).revision == 2
    finally:
        stream.body_allowed.set()
        stream.close_allowed.set()
        if not verify.done():
            verify.cancel()
        await asyncio.gather(verify, return_exceptions=True)
        if closing is not None:
            await closing
        await owner.close()


async def test_runtime_production_inventory_remains_unknown_even_after_valid_verify(store, monkeypatch):
    owner, requests = runtime_with_mock_http(store, monkeypatch, known=False)
    try:
        ready = await owner.start()
        before = store.snapshot()
        for kind in MUTATIONS:
            failure(lambda kind=kind: store.mutate(kind, repo=owner._repo))
        result = await ready.verification.verify(store.pid, expected_revision=1)
        assert result.state == "valid" and len(requests) == 2
        failure(lambda: store.mutate("credential", repo=owner._repo))
        assert store.snapshot() == before
        assert ready.metadata.list()[0].verification.model_dump() == {"state": "unavailable"}
    finally:
        await owner.close()


async def test_runtime_verify_admits_before_binding_token_and_http_and_catalog_stays_disabled(
    store, monkeypatch,
):
    owner, requests = runtime_with_mock_http(store, monkeypatch)
    ready = await owner.start()
    tokens = Mock(wraps=credentials.load_profile_token)
    monkeypatch.setattr(credentials, "load_profile_token", tokens)
    providers = Mock(wraps=owner.providers_for)
    monkeypatch.setattr(owner, "providers_for", providers)
    try:
        with owner.lifecycle.consumer_guard(store.pid):
            with pytest.raises(ProfileError, match="consumer_lifecycle_unavailable"):
                await ready.verification.verify(store.pid, expected_revision=1)
        tokens.assert_not_called()
        providers.assert_not_called()
        assert not requests
        operation = Mock(wraps=owner.lifecycle.operation)
        monkeypatch.setattr(owner.lifecycle, "operation", operation)
        with pytest.raises(CatalogError, match="service_unavailable"):
            await ready.catalog.list_projects(store.pid, expected_revision=1)
        operation.assert_not_called()
        tokens.assert_not_called()
        assert not requests
    finally:
        await owner.close()


async def test_failed_runtime_start_closes_every_retained_admission_handle(store, tmp_path, monkeypatch):
    owner, _requests = runtime_with_mock_http(store, monkeypatch)

    def fail_pool():
        raise RuntimeError(SECRET)

    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport", fail_pool)
    retained = owner.lifecycle
    with pytest.raises(CatalogError, match="service_unavailable") as caught:
        await owner.start()
    assert SECRET not in repr(caught.value)
    for action in (lambda: retained.attach(root=str(tmp_path), url=URL),
                   lambda: retained.operation(store.pid, kind="verify"),
                   lambda: retained.register_context(resident(tmp_path)),
                   lambda: snapshot(retained, store.pid)):
        failure(action)
    with pytest.raises(CatalogError, match="service_unavailable"):
        await owner.start()
    await owner.close()


@pytest.mark.parametrize("failure_phase", [None, "startup", "shutdown"])
async def test_main_lifespan_shares_owner_and_fences_failed_start_or_shutdown(
    store, tmp_path, monkeypatch, failure_phase,
):
    owner, requests = runtime_with_mock_http(store, monkeypatch, known=False)
    ctx = context_stub(tmp_path / "bootstrap", owner.lifecycle, monkeypatch)
    monkeypatch.setattr(main, "create_remote_catalog_runtime", lambda: owner)
    monkeypatch.setattr(main.AppContext, "create", staticmethod(lambda *_a, **_kw: ctx))
    monkeypatch.setattr(main, "build_info", lambda: BuildInfo(version="test", commit="test"))
    monkeypatch.setattr(main, "static_dir", lambda: None)
    monkeypatch.setattr(main, "record_recent", lambda *_a: None)
    monkeypatch.setattr(workspace, "DesktopEventSource", DesktopStub)
    if failure_phase == "startup":
        ctx.db.create_all.side_effect = RuntimeError("synthetic-startup")

    async def stopped():
        if failure_phase != "startup":
            failure(lambda: owner.lifecycle.attach(root=str(ctx.paths.root), url=URL))
        if failure_phase == "shutdown":
            raise RuntimeError("synthetic-shutdown")

    ctx.sync.stop.side_effect = stopped
    app = main.create_app(str(ctx.paths.root))

    async def lifespan():
        async with app.router.lifespan_context(app):
            assert app.state.remote_profile_lifecycle is owner.lifecycle
            assert ctx.remote_profile_lifecycle is owner.lifecycle
            assert app.state.ctx is ctx
            failure(lambda: store.mutate("credential", repo=owner._repo))

    if failure_phase:
        with pytest.raises(RuntimeError, match="synthetic-" + failure_phase):
            await lifespan()
    else:
        await lifespan()
    failure(lambda: owner.lifecycle.operation(store.pid, kind="read"))
    with pytest.raises(CatalogError, match="service_unavailable"):
        _ = owner.ready
    assert not requests


# VS-166 / runtime-integration-plan §2.1, §3.2/I1. Completeness in these
# fixtures is TEST-ONLY; the real private store reader is never replaced by it.
def private_record(store, root, *, url=URL, profile_id=None, selected=False):
    profile = store.profile.model_copy(update={
        "profile_id": profile_id or store.pid, "url": url,
    })
    return BindingRecord.for_root(
        root, profile=profile, identity=ProjectIdentity(id="synthetic-i1", revision=1),
        selection_state="selected" if selected else "confirmed",
        space_instance_id=None if selected else "0123456789abcdef0123456789abcdef",
    )


def save_private(record):
    repo = RemoteBindings()
    return repo.save(record, expected_fingerprint=repo.read().fingerprint)


def test_i1_real_private_rows_union_with_hidden_legacy_and_leases(store, tmp_path):
    same_id = private_record(store, tmp_path / "unopened-selected", url=OTHER_URL, selected=True)
    same_url = private_record(store, tmp_path / "unopened-confirmed",
                              profile_id="33333333-3333-4333-8333-333333333166")
    prefix = private_record(store, tmp_path / "not-exact-prefix", url=URL + "/other",
                            profile_id="44444444-4444-4444-8444-444444444166")
    for row in (same_id, same_url, prefix):
        save_private(row)
    hidden = resident(tmp_path / "hidden-legacy", enabled=False,
                      url="wss://ONE.invalid/deployment/prefix/")
    store.lifecycle.register_context(hidden)
    store.evidence = BindingInventory(state="known", drained=True)
    with store.lifecycle.operation(store.pid, kind="create"):
        state = snapshot(store.lifecycle, store.pid)
        assert set(state.bound_roots) == {same_id.canonical_root, same_url.canonical_root,
                                         str(hidden.paths.root.resolve())}
        assert state.active_roots == ("catalog:create",) and not state.drained
    assert all(not Path(row.canonical_root).exists() for row in (same_id, same_url, prefix))
    for kind in ("url", "delete"):
        failure(lambda: store.mutate(kind), "profile_in_use")


def test_i1_recovery_private_rows_use_full_url_union_not_just_profile_id(store, tmp_path):
    third = "https://current.invalid/prefix"
    rows = [private_record(store, tmp_path / str(index), url=url,
                           profile_id="33333333-3333-4333-8333-333333333166")
            for index, url in enumerate((URL, OTHER_URL, third))]
    rows.append(private_record(store, tmp_path / "same-id", url="https://stale.invalid"))
    for row in rows:
        save_private(row)
    with store.lifecycle.recovery_guard(store.pid, urls=(URL, OTHER_URL, third)) as state:
        assert set(state.bound_roots) == {row.canonical_root for row in rows}
        assert not state.active_roots
        assert state.recheck is not None
        state.recheck()  # read-only; S2 is responsible for the transaction fence.


def test_i1_nonempty_private_store_and_recents_cannot_open_legacy_gate(store, tmp_path, monkeypatch):
    save_private(private_record(store, tmp_path / "unopened-selected", selected=True))
    reader = Mock(wraps=RemoteBindings().read)
    recent = Mock(side_effect=AssertionError("recents are not a legacy census"))
    key_read = Mock(side_effect=AssertionError("unknown inventory attempted key lookup"))
    monkeypatch.setattr(workspace, "load_recent", recent)
    monkeypatch.setattr(credentials, "profile_token_revision", key_read)
    lifecycle = ProfileLifecycle(profile_reader=store.repo.get, binding_reader=reader)
    repo = RemoteProfiles(consumer_guard=lifecycle.consumer_guard)
    before = store.snapshot()
    failure(lambda: store.mutate("credential", repo=repo))
    reader.assert_called_once_with()
    recent.assert_not_called()
    key_read.assert_not_called()
    assert store.snapshot() == before


@pytest.mark.parametrize("enabled", [False, True])
def test_i1_resident_profile_id_matches_even_with_an_old_url(store, tmp_path, enabled):
    ctx = resident(tmp_path / "same-profile-old-url", url=OTHER_URL, enabled=enabled)
    ctx.config.sync.profile_id = store.pid
    store.lifecycle.register_context(ctx)
    state = snapshot(store.lifecycle, store.pid)
    assert state.bound_roots == (str(ctx.paths.root.resolve()),)
    assert state.active_roots == (state.bound_roots if enabled else ())


@pytest.mark.parametrize("content", [None, b'{"version":1,"roots":{}}', b'{',
                                      b'{"version":99,"roots":{}}'])
@pytest.mark.parametrize("known", [False, True])
def test_i1_store_validity_is_not_legacy_completeness(store, content, known, monkeypatch):
    if content is not None:
        bindings_file().write_bytes(content)
    lifecycle = ProfileLifecycle(profile_reader=store.repo.get,
        inventory=(lambda _pid: BindingInventory(state="known")) if known else None)
    repo = RemoteProfiles(consumer_guard=lifecycle.consumer_guard)
    before = store.snapshot()
    keys = Mock(side_effect=AssertionError("key lookup before inventory admission"))
    monkeypatch.setattr(credentials, "profile_token_revision", keys)
    if known and content in (None, b'{"version":1,"roots":{}}'):
        assert not snapshot(lifecycle, store.pid).bound_roots
    else:
        failure(lambda: store.mutate("credential", repo=repo))
    keys.assert_not_called()
    assert store.snapshot() == before


@pytest.mark.parametrize("kind", MUTATIONS)
@pytest.mark.parametrize("drift", ["binding", "fingerprint-only", "coverage", "recovering"])
def test_i1_final_recheck_under_existing_s2_transaction_before_any_key_or_write(
    store, tmp_path, monkeypatch, kind, drift,
):
    # An unrelated row proves that the CAS is whole-file, not matching-roots-only.
    row = private_record(store, tmp_path / "unrelated", url=OTHER_URL,
                         profile_id="33333333-3333-4333-8333-333333333166")
    save_private(row)
    if kind == "recover":
        store.repo._begin(store.profile)
    transaction = profiles.application_config_transaction
    journal = store.repo._journal()
    before_config = (store.home / "config.yaml").read_bytes()
    before_keys = (store.home / "remote-keys.json").read_bytes()
    before_journal = journal.read_bytes() if journal.exists() else None
    observations = []
    reader = store.lifecycle._binding_reader
    in_transaction = False

    def observed():
        observations.append(in_transaction)
        return reader()

    monkeypatch.setattr(store.lifecycle, "_binding_reader", observed)

    @contextmanager
    def raced_transaction():
        nonlocal in_transaction
        # Publication wins after initial C5 observation, before S2 obtains its
        # shared config transaction. No lock nesting, no fake snapshot reader.
        if drift == "binding":
            save_private(private_record(store, tmp_path / "new-binding", selected=True))
        elif drift == "fingerprint-only":
            bindings_file().write_bytes(bindings_file().read_bytes() + b"\n")
        else:
            store.evidence = BindingInventory(state="unknown" if drift == "coverage" else "recovering")
        with transaction():
            in_transaction = True
            try:
                yield
            finally:
                in_transaction = False

    monkeypatch.setattr(profiles, "application_config_transaction", raced_transaction)
    key_read = Mock(side_effect=AssertionError("key lookup before final inventory recheck"))
    begin = Mock(side_effect=AssertionError("journal before final inventory recheck"))
    monkeypatch.setattr(credentials, "profile_token_revision", key_read)
    monkeypatch.setattr(store.repo, "_begin", begin)
    failure(lambda: store.mutate(kind))
    assert observations == [False, True]
    key_read.assert_not_called()
    begin.assert_not_called()
    assert (store.home / "config.yaml").read_bytes() == before_config
    assert (store.home / "remote-keys.json").read_bytes() == before_keys
    assert (journal.read_bytes() if journal.exists() else None) == before_journal


def test_i1_private_writer_between_guard_and_config_lock_is_not_lost(store, tmp_path, monkeypatch):
    transaction = profiles.application_config_transaction
    ready, committed = Event(), Event()
    row = private_record(store, tmp_path / "published-concurrently", selected=True)

    def publish():
        assert ready.wait(5)
        save_private(row)  # real RemoteBindings CAS and real shared config lock
        committed.set()

    @contextmanager
    def wait_for_private_writer():
        ready.set()
        assert committed.wait(5)
        with transaction():
            yield

    monkeypatch.setattr(profiles, "application_config_transaction", wait_for_private_writer)
    key_read = Mock(side_effect=AssertionError("key lookup after binding race"))
    monkeypatch.setattr(credentials, "profile_token_revision", key_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(publish)
        failure(lambda: store.mutate("delete"))
        writer.result(timeout=5)
    assert RemoteBindings().read().records == (row,)
    assert store.repo.get(store.pid) == store.profile
    key_read.assert_not_called()
    assert not store.repo._journal().exists()


def test_i1_final_read_does_not_acquire_nested_os_config_lock(store, monkeypatch):
    # I14 writes through the internal transaction-aware seam, also with multiple
    # rows. These are synthetic known consumers, NOT a production release proof.
    save_private(private_record(store, store.home.parent / "unopened-i14-a"))
    save_private(private_record(store, store.home.parent / "unopened-i14-b"))
    acquisitions = []
    if os.name == "nt":
        import msvcrt
        original = msvcrt.locking

        def locking(fd, mode, count):
            if mode == msvcrt.LK_LOCK:
                acquisitions.append(fd)
                assert len(acquisitions) <= 2, "nested/extra OS transaction"
            return original(fd, mode, count)

        monkeypatch.setattr(msvcrt, "locking", locking)
    else:
        import fcntl
        original = fcntl.flock

        def flock(fd, mode):
            if mode == fcntl.LOCK_EX:
                acquisitions.append(fd)
                assert len(acquisitions) <= 2, "nested/extra OS transaction"
            return original(fd, mode)

        monkeypatch.setattr(fcntl, "flock", flock)
    # One config OS lock + one credential OS lock. Private read/recheck take none.
    assert store.mutate("credential").revision == 2
    assert len(acquisitions) == 2
    assert all(row.selection_state == "selected" for row in RemoteBindings().read().records)


def i14_rows(store, tmp_path):
    """Only synthetic known-inventory fixtures; no real drain or release claim."""
    rows = (private_record(store, tmp_path / "i14-a"),
            private_record(store, tmp_path / "i14-b"),
            private_record(store, tmp_path / "i14-selected", selected=True),
            private_record(store, tmp_path / "i14-unrelated", url=OTHER_URL,
                           profile_id="33333333-3333-4333-8333-333333333166"))
    for row in rows:
        save_private(row)
    return rows


def i14_import_setup(store):
    # Actual secret owner, synthetic data; import must change a credential.
    credentials.save_profile_token(store.pid, URL, None, expected_revision=1)
    credentials.save_token(URL, SECRET + "-legacy")


@pytest.mark.parametrize("kind", ("credential", "clear", "import", "recover"))
def test_i14_synthetic_exact_rows_demoted_before_key_under_durable_fence(store, tmp_path, monkeypatch, kind):
    rows = i14_rows(store, tmp_path)
    if kind == "import":
        i14_import_setup(store)
    if kind == "recover":
        store.repo._begin(store.profile)
    original = credentials.import_legacy_token if kind == "import" else credentials.save_profile_token
    calls = []

    def publish(*args, **kwargs):
        assert store.repo._journal().exists()
        actual = RemoteBindings().read().records
        expected = tuple(row.model_copy(update={"selection_state": "selected"})
                         if row.profile_id == store.pid else row for row in rows)
        assert set(actual) == set(expected)
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(credentials, "import_legacy_token" if kind == "import" else "save_profile_token", publish)
    assert store.mutate(kind).revision == 2
    assert calls and not store.repo._journal().exists()
    # Roots were never opened/created; all IDs/epochs/revisions remain identical.
    assert all(not Path(row.canonical_root).exists() for row in rows)


@pytest.mark.parametrize("phase", ("begin", "private", "durability", "credential", "metadata"))
def test_i14_synthetic_fault_restart_recovery_never_restores_consent(store, tmp_path, monkeypatch, phase):
    rows = i14_rows(store, tmp_path)
    key_before = credentials.load_profile_token(store.pid, URL)
    writes = []
    original = RemoteBindings._save_in_transaction

    def fail_private(self, row, **kwargs):
        writes.append(row.canonical_root)
        if len(writes) == 2:
            raise OSError(SECRET)
        return original(self, row, **kwargs)

    def fail(*args, **kwargs):
        raise OSError(SECRET)

    fsync = profiles._fsync_directory
    flushes = []

    def fail_durability(path):
        flushes.append(path)
        if len(flushes) == 2:  # _begin is durable; private replace is not yet proven.
            raise OSError(SECRET)
        return fsync(path)

    with monkeypatch.context() as fault:
        if phase == "begin":
            fault.setattr(store.repo, "_begin", fail)
        elif phase == "private":
            fault.setattr(RemoteBindings, "_save_in_transaction", fail_private)
        elif phase == "durability":
            fault.setattr(profiles, "_fsync_directory", fail_durability)
        elif phase == "credential":
            fault.setattr(credentials, "save_profile_token", fail)
        else:
            fault.setattr(profiles, "_write_application_config", fail)
        with pytest.raises((ProfileError, OSError)) as caught:
            store.mutate("credential")
        if phase in {"private", "durability"}:
            assert caught.value.args == ("consumer_lifecycle_unavailable",)
            assert caught.value.__context__ is None and caught.value.__cause__ is None
    after = RemoteBindings().read()
    if phase == "begin":
        assert set(after.records) == set(rows) and not store.repo._journal().exists()
        assert credentials.load_profile_token(store.pid, URL) == key_before
        return
    assert store.repo._journal().exists()
    if phase != "metadata":
        assert credentials.load_profile_token(store.pid, URL) == key_before
    selected_before = {r.canonical_root for r in after.records if r.selection_state == "selected"}
    # Cold owner construction/startup does not grant consent or recover implicitly.
    repo = RemoteProfiles(consumer_guard=store.lifecycle.consumer_guard)
    assert RemoteBindings().read() == after
    assert repo.recover(store.pid, expected_revision=1).revision == 2
    recovered = RemoteBindings().read()
    assert selected_before <= {r.canonical_root for r in recovered.records if r.selection_state == "selected"}
    assert all(r.selection_state == "selected" for r in recovered.records if r.profile_id == store.pid)
    assert recovered.get(rows[-1].canonical_root) == rows[-1]
    assert credentials.load_profile_token(store.pid, URL) is None
    # No journal replay after success; refusal must not rewrite private consent.
    failure(lambda: repo.recover(store.pid, expected_revision=2), "profile_recovery_unavailable")
    assert RemoteBindings().read() == recovered


@pytest.mark.parametrize("kind", ("credential", "clear", "import", "recover"))
@pytest.mark.parametrize("mismatch", ("profile", "url"))
def test_i14_synthetic_inconsistent_row_refuses_without_repair(store, tmp_path, kind, mismatch):
    if kind == "import":
        i14_import_setup(store)
    if kind == "recover":
        store.repo._begin(store.profile)
    row = private_record(store, tmp_path / "ambiguous",
        url=OTHER_URL if mismatch == "url" else URL,
        profile_id="33333333-3333-4333-8333-333333333166" if mismatch == "profile" else store.pid)
    save_private(row)
    before = store.snapshot()
    failure(lambda: store.mutate(kind))
    assert store.snapshot() == before


def test_i14_synthetic_stale_cas_noop_import_and_corrupt_store_do_not_demote(store, tmp_path, monkeypatch):
    rows = i14_rows(store, tmp_path)
    before = store.snapshot()
    failure(lambda: store.repo.clear_credential(store.pid, expected_revision=0), "profile_revision_conflict")
    assert store.snapshot() == before
    # With no legacy key, explicit import is a true no-op, including consent bytes.
    assert store.mutate("import") == store.profile
    assert store.snapshot() == before
    # Exercise S2's independent private validation even with an injected guard.
    from voxarium_studio.sync.profiles import ProfileConsumers
    (store.home / "remote-bindings.json").write_text("{" + SECRET, encoding="utf-8")

    @contextmanager
    def synthetic_guard(_pid):
        yield ProfileConsumers()

    repo = RemoteProfiles(consumer_guard=synthetic_guard)
    before = store.snapshot()
    error = failure(lambda: repo.clear_credential(store.pid, expected_revision=1))
    assert error.__context__ is None and error.__cause__ is None
    assert store.snapshot() == before


def test_i14_synthetic_import_fault_preserves_legacy_and_selected_on_recovery(store, tmp_path, monkeypatch):
    rows = i14_rows(store, tmp_path)
    i14_import_setup(store)
    original = credentials.import_legacy_token

    def lost_response(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError(SECRET)

    with monkeypatch.context() as fault:
        fault.setattr(credentials, "import_legacy_token", lost_response)
        with pytest.raises(OSError):
            store.mutate("import")
    selected = RemoteBindings().read()
    assert all(row.selection_state == "selected" for row in selected.records if row.profile_id == store.pid)
    assert store.repo._journal().exists()
    assert credentials.load_token(URL) == SECRET + "-legacy"
    cold = RemoteProfiles(consumer_guard=store.lifecycle.consumer_guard)
    cold.recover(store.pid, expected_revision=1)
    assert RemoteBindings().read() == selected  # Idempotent invalidation, no rewrite.
    assert credentials.load_profile_token(store.pid, URL) is None
    assert credentials.load_token(URL) == SECRET + "-legacy"
    assert RemoteBindings().read().get(rows[-1].canonical_root) == rows[-1]


def test_i14_synthetic_recovery_demotes_exact_old_target_current_url_union(store, tmp_path):
    current_url = "https://current.invalid/deployment/prefix"
    rows = tuple(private_record(store, tmp_path / ("union-" + str(index)), url=url)
                 for index, url in enumerate((URL, OTHER_URL, current_url)))
    for row in rows:
        save_private(row)
    store.repo._begin(store.profile, target_url=OTHER_URL)
    with profiles.application_config_transaction():
        config = store.repo._read()
        store.repo._publish(config, store.profile.model_copy(update={"url": current_url, "revision": 2}))
    before = RemoteBindings().read()
    failure(lambda: store.repo.recover(store.pid, expected_revision=True), "profile_revision_conflict")
    assert RemoteBindings().read() == before
    assert store.repo.recover(store.pid, expected_revision=2).revision == 3
    assert set(RemoteBindings().read().records) == {
        row.model_copy(update={"selection_state": "selected"}) for row in rows}


@pytest.mark.parametrize("phase", ("private", "credential"))
def test_i14_synthetic_interrupted_recovery_can_only_continue_demotion(store, tmp_path, monkeypatch, phase):
    i14_rows(store, tmp_path)
    store.repo._begin(store.profile)
    original = RemoteBindings._save_in_transaction
    calls = []

    def private(self, row, **kwargs):
        calls.append(row.canonical_root)
        if len(calls) == 2:
            raise OSError(SECRET)
        return original(self, row, **kwargs)

    def credential(*args, **kwargs):
        raise OSError(SECRET)

    with monkeypatch.context() as fault:
        if phase == "private":
            fault.setattr(RemoteBindings, "_save_in_transaction", private)
        else:
            fault.setattr(credentials, "save_profile_token", credential)
        with pytest.raises((ProfileError, OSError)):
            store.repo.recover(store.pid, expected_revision=1)
    assert store.repo._journal().exists()
    before = RemoteBindings().read()
    previous = {row.canonical_root for row in before.records if row.selection_state == "selected"}
    cold = RemoteProfiles(consumer_guard=store.lifecycle.consumer_guard)
    cold.recover(store.pid, expected_revision=1)
    after = RemoteBindings().read()
    assert previous <= {row.canonical_root for row in after.records if row.selection_state == "selected"}
    assert all(row.selection_state == "selected" for row in after.records if row.profile_id == store.pid)
    if phase == "credential":
        assert after == before  # Repeat performs zero private writes.
