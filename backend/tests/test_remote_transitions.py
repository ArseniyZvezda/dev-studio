"""I5 acceptance: local prepare/flush/fence, never commit or Enable.

runtime-integration-plan §§3.2–3.5 and studio-design §§5.2/7. All homes,
projects, checkpoints and transports below are synthetic; no real Remote runs.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from voxarium_studio.core.config import AppConfig
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project import init_project
from voxarium_studio.explorer.watcher import FileWatcher
from voxarium_studio.main import create_app
from voxarium_studio.sync import service
from voxarium_studio.sync.protocol import content_hash
from voxarium_studio.sync.service import RemoteSync, _LiveWrite, _Pending, _Session
from voxarium_studio.sync.state import Base, SyncState
from voxarium_studio.sync.transitions import MutationGateError
from voxarium_studio.teams.docs import DocStore
from voxarium_studio.ws.bus import EventBus


async def ready_commit(p, **changes):
    response = await prepare(p, **changes)
    assert response.status_code == 200, response.text
    prepared = response.json()
    old_generation = p.ca.docs.transitions.generation
    response = await p.http.post(remote_url(p.a, f"/changes/{prepared['change_id']}/quiesce"),
                                 json={"expected_generation": old_generation})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ready", response.text
    observed = p.ca.docs.transitions.expected_change_evidence(prepared["change_id"])["observation"]
    return prepared, {"expected_revision": observed["identity_revision"],
        "expected_generation": p.ca.docs.transitions.generation,
        "config_fingerprint": observed["config_fingerprint"],
        "confirmation": {"url": prepared["to"]["url"],
                         "persistent_id": prepared["to"]["persistent_id"]}}


async def commit(p, prepared, body):
    return await p.http.post(remote_url(p.a, f"/changes/{prepared['change_id']}/commit"), json=body)


async def test_i6_edit_commit_is_disabled_canonical_idempotent_and_metadata_only(projects):
    from voxarium_studio.sync.transitions import read_binding_change_journals
    p = projects
    ctx = p.ca
    descriptor = p.app.state.project_registry[p.a["project_id"]]
    original = (ctx.paths, ctx.jobs, ctx.docs, ctx.teams, descriptor)
    other = disk_bytes(p.cb.paths.config_file, p.cb.paths.docs)
    docs = disk_bytes(ctx.paths.docs)
    prepared, body = await ready_commit(p)
    response = await commit(p, prepared, body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["persistent_id"] == "i5-new-id" and result["identity_revision"] == 8
    assert not result["enabled"] and result["committed"]
    assert result["binding_generation"] != body["expected_generation"]
    config = yaml.safe_load(ctx.paths.config_file.read_text(encoding="utf-8"))
    assert config["sync"] == {"url": "", "enabled": False}
    assert "space" not in config["sync"]
    before = disk_bytes(ctx.paths.config_file)
    again = await commit(p, prepared, body)
    assert again.status_code == 200 and again.json()["identity_revision"] == 8
    assert disk_bytes(ctx.paths.config_file) == before
    assert (ctx.paths, ctx.jobs, ctx.docs, ctx.teams,
            p.app.state.project_registry[p.a["project_id"]]) == original
    assert disk_bytes(ctx.paths.docs) == docs
    assert disk_bytes(p.cb.paths.config_file, p.cb.paths.docs) == other
    rows = read_binding_change_journals(ctx.paths.root)
    assert len(rows) == 1 and rows[0]["phase"] == "completed"
    assert rows[0]["commit"]["binding"] is None
    cancelled = await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
    assert_error(cancelled, "already_committed")


@pytest.mark.parametrize("operation", ["edit_id", "unbind"])
@pytest.mark.parametrize("journal_damage", [None, "corrupt", "lost", "directory_lost"])
async def test_i6_restart_never_revives_restored_legacy_after_binding_removal(
    projects, monkeypatch, operation, journal_damage,
):
    from voxarium_studio.core.config import load_config
    from voxarium_studio.sync import credentials
    from voxarium_studio.sync.bindings import RemoteBindings
    from voxarium_studio.sync.transitions import binding_change_journal_path
    p = projects
    prepared, body = await ready_commit(p, operation=operation,
        persistent_id="i5-shared-id" if operation == "unbind" else "edited-local")
    response = await commit(p, prepared, body)
    assert response.status_code == 200, response.text
    assert RemoteBindings().read().get(p.ca.paths.root) is None
    journal = binding_change_journal_path(p.ca.paths.root, prepared["change_id"])
    canonical = yaml.safe_load(p.ca.paths.config_file.read_text(encoding="utf-8"))
    # A returned legacy config is not portable activation evidence; durable
    # root history takes priority even with no remaining private binding row.
    legacy = {**canonical, "project_identity": {"id": "vcs-old-id", "revision": 2},
              "sync": {"enabled": True, "url": "https://legacy.invalid", "space": "old-space"}}
    if journal_damage == "corrupt":
        journal.write_bytes(b'{"version":2,"version":2}')
    elif journal_damage in {"lost", "directory_lost"}:
        journal.unlink()
        if journal_damage == "directory_lost":
            journal.parent.rmdir()
            # Canonical config itself is the only observable evidence left
            # when ALL local private history was externally removed.
            legacy = canonical
    p.ca.paths.config_file.write_text(yaml.safe_dump(legacy), encoding="utf-8")
    calls = []

    def denied(*args, **kwargs):
        calls.append(True)
        raise AssertionError("activated restart must not inspect credentials/WS")

    for name in ("load_token", "load_profile_token", "profile_token_revision", "profile_token_source"):
        monkeypatch.setattr(credentials, name, denied)
    monkeypatch.setattr(service, "connect", denied)
    monkeypatch.setattr(RemoteSync, "_run", denied)
    restarted = AppContext(p.ca.paths, load_config(p.ca.paths), p.ca.auth)
    before = disk_bytes(p.ca.paths.config_file, p.ca.paths.docs)
    await restarted.sync.start()
    try:
        assert restarted.sync.status()["state"] == "blocked"
        assert not restarted.sync.status()["has_token"]
        await restarted.sync.reconfigure()
        assert restarted.sync.status()["state"] == "blocked"
        assert calls == [] and disk_bytes(p.ca.paths.config_file, p.ca.paths.docs) == before
    finally:
        await restarted.sync.stop()


@pytest.mark.parametrize("field", ["confirmation", "revision", "generation", "config", "private", "markdown"])
async def test_i6_final_cas_refuses_without_target_write(projects, monkeypatch, field):
    from voxarium_studio.sync.bindings import BindingRecord, RemoteBindings
    p = projects
    prepared, body = await ready_commit(p)
    if field == "confirmation":
        body["confirmation"]["persistent_id"] = "different"
    elif field == "revision":
        body["expected_revision"] += 1
    elif field == "generation":
        body["expected_generation"] -= 1
    elif field == "config":
        p.ca.paths.config_file.write_bytes(p.ca.paths.config_file.read_bytes() + b"\n# external writer\n")
    elif field == "private":
        # A valid unrelated root publication must invalidate WHOLE-file CAS.
        from voxarium_studio.core.application_config import RemoteServerProfile
        row = BindingRecord.for_root(p.cb.paths.root,
            profile=RemoteServerProfile(profile_id="786acc4b-a995-41fd-b8dc-6ec64a9866dd",
                name="Other", url="https://other.invalid"),
            identity=p.cb.config.project_identity, selection_state="selected")
        RemoteBindings().save(row, expected_fingerprint=RemoteBindings().read().fingerprint)
    else:
        p.ca.docs.resolve("same.md").write_text("# External changed\n", encoding="utf-8")
    before = disk_bytes(p.ca.paths.config_file, p.ca.paths.docs)
    response = await commit(p, prepared, body)
    assert_error(response)
    assert disk_bytes(p.ca.paths.config_file, p.ca.paths.docs) == before


@pytest.mark.parametrize("fault", ["journal", "config", "private", "journal_config", "journal_complete", "response"])
async def test_i6_fault_restart_disabled_no_rollback_or_automatic_repair(projects, monkeypatch, fault):
    from voxarium_studio.core import config as config_owner
    from voxarium_studio.core.config import load_config
    from voxarium_studio.sync import credentials, transitions
    from voxarium_studio.sync.bindings import RemoteBindings
    p = projects
    if fault == "private":
        seed_private_selection(p)
    prepared, body = await ready_commit(p)
    before = p.ca.paths.config_file.read_bytes()
    docs = disk_bytes(p.ca.paths.docs)
    publish = transitions.publish_binding_change_commit
    config_commit = config_owner._commit_disabled_identity_in_transaction
    remove = RemoteBindings._remove_in_transaction
    event = p.ca.bus.publish

    def failed(*args, **kwargs):
        raise OSError("synthetic durable fault")

    if fault.startswith("journal"):
        phase = {"journal": "committing", "journal_config": "config_committed",
                 "journal_complete": "completed"}[fault]
        def fail_phase(*args, **kwargs):
            if args[-1] == phase:
                raise OSError("synthetic phase fault")
            return publish(*args, **kwargs)
        monkeypatch.setattr(transitions, "publish_binding_change_commit", fail_phase)
    elif fault == "config":
        monkeypatch.setattr(config_owner, "_commit_disabled_identity_in_transaction", failed)
    elif fault == "private":
        monkeypatch.setattr(RemoteBindings, "_remove_in_transaction", failed)
    else:
        monkeypatch.setattr(p.ca.bus, "publish", AsyncMock(side_effect=OSError("lost response")))
    if fault == "response":
        with pytest.raises(OSError, match="lost response"):
            await commit(p, prepared, body)
    else:
        assert_error(await commit(p, prepared, body), "recovery_required")
    monkeypatch.setattr(transitions, "publish_binding_change_commit", publish)
    monkeypatch.setattr(config_owner, "_commit_disabled_identity_in_transaction", config_commit)
    monkeypatch.setattr(RemoteBindings, "_remove_in_transaction", remove)
    monkeypatch.setattr(p.ca.bus, "publish", event)
    content = p.ca.paths.config_file.read_bytes()
    if fault in {"journal", "config"}:
        assert content == before
    else:
        assert content != before
        assert yaml.safe_load(content)["sync"]["enabled"] is False
    assert disk_bytes(p.ca.paths.docs) == docs
    calls = []
    def denied(*args, **kwargs):
        calls.append(True)
        raise AssertionError("no startup repair/keys/network")
    monkeypatch.setattr(credentials, "load_token", denied)
    monkeypatch.setattr(credentials, "load_profile_token", denied)
    monkeypatch.setattr(service, "connect", denied)
    restarted = AppContext(p.ca.paths, load_config(p.ca.paths), p.ca.auth)
    state = disk_bytes(p.ca.paths.config_file, p.ca.paths.docs)
    await restarted.sync.start()
    try:
        assert restarted.sync.status()["state"] == "blocked"
        assert calls == [] and disk_bytes(p.ca.paths.config_file, p.ca.paths.docs) == state
    finally:
        await restarted.sync.stop()
    # A matching explicit retry is permitted, not performed by startup.
    retry = await commit(p, prepared, body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["identity_revision"] == 8


def seed_private_selection(p):
    from voxarium_studio.core.config import save_config
    from voxarium_studio.sync.bindings import BindingRecord, RemoteBindings
    from voxarium_studio.sync.profiles import RemoteProfiles
    profile = RemoteProfiles().create("I6 synthetic", "https://i6.invalid/team")
    p.ca.config.sync.url = profile.url
    save_config(p.ca.paths, p.ca.config)
    row = BindingRecord.for_root(p.ca.paths.root, profile=profile,
        identity=p.ca.config.project_identity, selection_state="confirmed", space_instance_id="1" * 32)
    RemoteBindings().save(row, expected_fingerprint=RemoteBindings().read().fingerprint)
    return profile


@pytest.mark.parametrize("drift", ["expiry", "markdown", "private"])
async def test_i6_staging_rechecks_before_actual_config_replace(projects, monkeypatch, drift):
    from voxarium_studio.sync import transitions
    from voxarium_studio.sync.bindings import BindingRecord, RemoteBindings
    from voxarium_studio.core.application_config import RemoteServerProfile
    p = projects
    prepared, body = await ready_commit(p)
    before = p.ca.paths.config_file.read_bytes()
    real = transitions.publish_binding_change_commit
    def staged(*args, **kwargs):
        row = real(*args, **kwargs)
        if args[-1] == "committing":
            if drift == "expiry":
                p.ca.docs.transitions._change.deadline = -1
            elif drift == "markdown":
                p.ca.docs.resolve("same.md").write_text("# Outside writer\n", encoding="utf-8")
            else:
                record = BindingRecord.for_root(p.cb.paths.root,
                    profile=RemoteServerProfile(profile_id="786acc4b-a995-41fd-b8dc-6ec64a9866dd",
                        name="Other", url="https://other.invalid"),
                    identity=p.cb.config.project_identity, selection_state="selected")
                # This is a simulated process that doesn't share lifecycle but
                # honors the already-held transaction, not a nested OS lock.
                RemoteBindings()._save_in_transaction(record,
                    expected_fingerprint=RemoteBindings().read().fingerprint)
        return row
    monkeypatch.setattr(transitions, "publish_binding_change_commit", staged)
    assert_error(await commit(p, prepared, body))
    assert p.ca.paths.config_file.read_bytes() == before
    assert p.ca.docs.transitions._change.fenced


async def test_i6_completed_history_survives_expiry_and_later_changes(projects, monkeypatch):
    from voxarium_studio.sync.transitions import read_binding_change_journals
    p = projects
    first, first_body = await ready_commit(p, persistent_id="first-completed")
    a = await commit(p, first, first_body)
    assert a.status_code == 200, a.text
    second, second_body = await ready_commit(p, persistent_id="second-completed")
    b = await commit(p, second, second_body)
    assert b.status_code == 200, b.text
    monkeypatch.setattr(p.ca.docs.transitions, "_monotonic", lambda: float("inf"))
    again = await commit(p, first, first_body)
    assert again.status_code == 200 and again.json()["identity_revision"] == 8
    assert again.json()["persistent_id"] == "first-completed"
    assert p.ca.config.project_identity.id == "second-completed"
    assert len(read_binding_change_journals(p.ca.paths.root)) == 2
    assert_error(await p.http.delete(remote_url(p.a, f"/changes/{first['change_id']}")), "already_committed")


async def test_i6_concurrent_same_change_behind_settings_lock_is_idempotent(projects):
    p = projects
    prepared, body = await ready_commit(p)
    await p.app.state.project_lock.acquire()
    tasks = [asyncio.create_task(commit(p, prepared, body)) for _ in range(2)]
    try:
        await asyncio.sleep(0)
        p.app.state.project_lock.release()
        results = await asyncio.gather(*tasks)
        assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
        assert results[0].json() == results[1].json()
        assert p.ca.config.project_identity.revision == 8
    finally:
        if p.app.state.project_lock.locked():
            p.app.state.project_lock.release()
        await asyncio.gather(*tasks, return_exceptions=True)


def inject_select(p):
    from voxarium_studio.sync.profiles import RemoteProfiles
    from voxarium_studio.sync import credentials
    profile = RemoteProfiles().create("Injected catalog", "https://catalog.invalid/team")
    credentials.save_profile_token(profile.profile_id, profile.url, "synthetic-i6-key", expected_revision=0)
    source = {"profile": profile.model_dump(), "credential": {"revision": 1, "source": "stored"},
              "catalog": {"persistent_id": "selected-id", "space_instance_id": "a" * 32, "snapshot_seq": 12}}
    p.ca.remote_select_observer = lambda profile_id, identity: source
    return source


@pytest.mark.parametrize("drift", [None, "profile", "credential", "catalog", "confirmation_url"])
async def test_i6_select_exact_injected_snapshot_only(projects, monkeypatch, drift):
    from voxarium_studio.sync import credentials
    from voxarium_studio.sync.bindings import RemoteBindings
    from voxarium_studio.sync.profiles import RemoteProfiles
    p = projects
    source = inject_select(p)
    prepared, body = await ready_commit(p, operation="select", persistent_id="selected-id",
        profile_id=source["profile"]["profile_id"], url=source["profile"]["url"],
        catalog_id="selected-id", catalog_instance_id="a" * 32)
    if drift == "profile":
        RemoteProfiles().rename(source["profile"]["profile_id"], "Changed",
                                expected_revision=source["profile"]["revision"])
    elif drift == "credential":
        credentials.save_profile_token(source["profile"]["profile_id"], source["profile"]["url"],
            "synthetic-revision-two", expected_revision=1)
    elif drift == "catalog":
        source["catalog"]["snapshot_seq"] += 1
    elif drift == "confirmation_url":
        body["confirmation"]["url"] += "/"
    before = p.ca.paths.config_file.read_bytes()
    response = await commit(p, prepared, body)
    if drift:
        assert_error(response)
        assert p.ca.paths.config_file.read_bytes() == before
    else:
        assert response.status_code == 200, response.text
        row = RemoteBindings().read().get(p.ca.paths.root)
        assert row.selection_state == "selected" and row.identity_revision == 8
        assert row.persistent_id == "selected-id" and row.space_instance_id == "a" * 32
        assert not p.ca.config.sync.enabled


async def test_i6_disable_demotes_confirmed_and_one_real_os_lock(projects, monkeypatch):
    from voxarium_studio.core import application_config, config as config_owner
    from voxarium_studio.sync import credentials
    from voxarium_studio.sync.bindings import RemoteBindings
    p = projects
    seed_private_selection(p)
    prepared, body = await ready_commit(p, operation="disable", persistent_id="i5-shared-id")
    acquisitions = []
    if os.name == "nt":
        import msvcrt
        locking = msvcrt.locking
        def measured(fd, mode, count):
            if mode == msvcrt.LK_LOCK:
                acquisitions.append(True)
            return locking(fd, mode, count)
        monkeypatch.setattr(msvcrt, "locking", measured)
    else:
        import fcntl
        flock = fcntl.flock
        def measured(fd, mode):
            if mode == fcntl.LOCK_EX:
                acquisitions.append(True)
            return flock(fd, mode)
        monkeypatch.setattr(fcntl, "flock", measured)
    real = config_owner._commit_disabled_identity_in_transaction
    def ordered(*args, **kwargs):
        assert p.ca.docs.transitions.identity_lock.locked()
        assert p.ca.docs.transitions._mutation_lock.locked()
        assert p.ca.remote_profile_lifecycle._lock.locked()
        assert application_config._file_lock._is_owned()
        assert credentials._file_lock._is_owned()
        return real(*args, **kwargs)
    monkeypatch.setattr(config_owner, "_commit_disabled_identity_in_transaction", ordered)
    response = await commit(p, prepared, body)
    assert response.status_code == 200, response.text
    assert acquisitions == [True]
    row = RemoteBindings().read().get(p.ca.paths.root)
    assert row.selection_state == "selected" and row.identity_revision == 8
    assert p.ca.config.project_identity.id == "i5-shared-id"
    assert p.ca.config.sync.url == "https://i6.invalid/team"


@pytest.mark.parametrize("drift", ["expiry", "markdown", "private", "config"])
async def test_i6_config_temp_staging_still_rechecks_commit_fences(projects, monkeypatch, drift):
    from voxarium_studio.core import config as config_owner
    from voxarium_studio.sync.bindings import RemoteBindings
    p = projects
    prepared, body = await ready_commit(p)
    before = p.ca.paths.config_file.read_bytes()
    external = before + b"\n# external config must win\n"
    write = config_owner._write_project_document_in_transaction
    def stage(*args, **kwargs):
        check = kwargs["before_replace"]
        def after_staging():
            if drift == "expiry":
                p.ca.docs.transitions._change.deadline = -1
            elif drift == "markdown":
                p.ca.docs.resolve("same.md").write_text("# Final FS race\n", encoding="utf-8")
            elif drift == "config":
                p.ca.paths.config_file.write_bytes(external)
            else:
                # A valid empty whole-file publication differs from missing.
                from voxarium_studio.sync.bindings import bindings_file
                bindings_file().write_text('{"version":1,"roots":{}}', encoding="utf-8")
            check()
        return write(*args, **{**kwargs, "before_replace": after_staging})
    monkeypatch.setattr(config_owner, "_write_project_document_in_transaction", stage)
    assert_error(await commit(p, prepared, body), "recovery_required")
    assert p.ca.paths.config_file.read_bytes() == (external if drift == "config" else before)


@pytest.mark.parametrize("failure", ["write", "flush", "fsync", "replace"])
async def test_i6_actual_journal_io_fault_precedes_config_commit(projects, monkeypatch, failure):
    import tempfile
    from pathlib import Path
    from voxarium_studio.sync.bindings import RemoteBindings
    p = projects
    prepared, body = await ready_commit(p)
    before = disk_bytes(p.ca.paths.config_file, p.ca.paths.docs)
    private = RemoteBindings().read()
    ntf, fsync, replace_path = tempfile.NamedTemporaryFile, os.fsync, Path.replace
    class BrokenFile:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def write(self, value):
            if failure == "write":
                raise OSError("journal write")
            return self.stream.write(value)
        def flush(self):
            if failure == "flush":
                raise OSError("journal flush")
            return self.stream.flush()
    def temporary(*args, **kwargs):
        result = ntf(*args, **kwargs)
        return BrokenFile(result) if kwargs.get("prefix") == ".binding-change-" else result
    def failed_fsync(fd):
        raise OSError("journal fsync")
    def failed_replace(path, target):
        if path.name.startswith(".binding-change-"):
            raise OSError("journal replace")
        return replace_path(path, target)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", temporary)
    if failure == "fsync":
        monkeypatch.setattr(os, "fsync", failed_fsync)
    if failure == "replace":
        monkeypatch.setattr(Path, "replace", failed_replace)
    try:
        assert_error(await commit(p, prepared, body), "recovery_required")
        assert disk_bytes(p.ca.paths.config_file, p.ca.paths.docs) == before
        assert RemoteBindings().read() == private
    finally:
        monkeypatch.setattr(tempfile, "NamedTemporaryFile", ntf)
        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(Path, "replace", replace_path)


async def test_capable_client_without_backend_editor_inventory_is_not_clean(coordinator):
    c = coordinator
    c.gate.register_client("advertised-only", capable=True)
    prepared = plan(c)
    result = await c.gate.quiesce_change(prepared["change_id"], 0, timeout=.1)
    assert result["state"] == "blocked" and not result["fenced"]
    assert c.gate.generation == 0


async def test_unleased_legacy_room_cannot_use_clean_disk_as_editor_flush(coordinator):
    c = coordinator
    c.gate.register_collab_session("legacy-sid", "same.md", 0)
    prepared = plan(c)
    result = await c.gate.quiesce_change(prepared["change_id"], 0, timeout=.1)
    assert result["state"] == "blocked" and result["reason"] == "pending_edits"
    assert not result["fenced"] and c.gate.generation == 0
    assert "legacy-sid" in c.gate._sessions


async def test_real_coordinator_nack_conflict_copy_is_acked_without_opening_writers(transport, monkeypatch):
    sync = transport
    gate = sync.docs.transitions
    await sync.docs.write("same.md", "# Mine\n")
    sync._state.docs["same.md"] = Base(1, content_hash("# Base\n"))
    sync.set_binding_evidence_sink(lambda evidence: None)

    async def drain(change_id, timeout):
        proof = await sync.drain_binding_change(change_id, timeout)
        return {"drained": sync.validate_binding_drain(proof, change_id),
                "fenced": proof.fenced, "reason": proof.reason}

    gate.set_change_callbacks(begin_quiesce=sync.begin_binding_quiesce, drain=drain,
                              cancel_quiesce=sync.cancel_binding_quiesce,
                              fence_status=lambda: sync.binding_fenced)
    prepared = gate.prepare_change({"binding_generation": 0})
    ws = sync._ws
    task = asyncio.create_task(gate.quiesce_change(prepared["change_id"], 0, timeout=1))
    try:
        await asyncio.wait_for(ws.sent_event.wait(), .5)
        original = json.loads(ws.sent[0])
        blocked_callbacks = []
        original_publish = sync.bus.publish

        async def publish(kind, *args, **kwargs):
            # A reconciliation mutation may publish, but cannot grant an agent
            # or reentrant event callback the old pending-ID admission capability.
            with pytest.raises(MutationGateError):
                await sync.docs.write("not-admitted.md", "must not write")
            with pytest.raises(MutationGateError):
                await sync._apply("same.md", service._Remote(99, content_hash("injected"), "injected"), None)
            blocked_callbacks.append(kind)
            return await original_publish(kind, *args, **kwargs)

        monkeypatch.setattr(sync.bus, "publish", publish)
        ws.sent_event.clear()
        theirs = "# Theirs\n"
        await sync._handle({"t": "nack", "id": original["id"], "current": {
            "rev": 2, "deleted": False, "content": theirs, "hash": content_hash(theirs),
        }})
        await asyncio.wait_for(ws.sent_event.wait(), .5)
        frames = [json.loads(raw) for raw in ws.sent[1:]]
        copies = [frame for frame in frames if frame.get("t") == "put"]
        assert len(copies) == 1 and copies[0]["content"] == "# Mine\n"
        assert sync.docs.read("same.md")["content"] == theirs
        assert not task.done() and blocked_callbacks
        sync._on_ack({"id": copies[0]["id"], "rev": 3})
        result = await asyncio.wait_for(task, .5)
        assert result["state"] == "ready" and result["fenced"]
        assert not sync.docs.resolve("not-admitted.md").exists()
        assert sync.docs.read(copies[0]["path"])["content"] == "# Mine\n"
    finally:
        if not task.done():
            gate.cancel_change(prepared["change_id"])
            await asyncio.gather(task, return_exceptions=True)


async def test_service_admission_closes_before_first_quiesce_yield(coordinator):
    c = coordinator
    began = []
    c.gate._begin_quiesce = lambda: began.append(True)
    lease(c)
    prepared = plan(c)
    task = asyncio.create_task(c.gate.quiesce_change(prepared["change_id"], 0, timeout=.1))

    async def observer():
        while not c.gate.binding_quiescing:
            await asyncio.sleep(0)
        assert began, "logical quiesce yielded before closing transport admission"

    try:
        await asyncio.wait_for(observer(), 1)
    finally:
        c.gate.cancel_change(prepared["change_id"])
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("during_close", [False, True])
async def test_same_stat_external_markdown_change_cannot_mint_drain_proof(transport, monkeypatch, during_close):
    sync = transport
    await sync.docs.write("same.md", "# Before\n")
    path = sync.docs.resolve("same.md")
    stat = path.stat()
    sync._state.docs["same.md"] = Base(1, content_hash("# Before\n"))
    sync._scan()  # Deliberately prime the ordinary mtime/size cache.

    def change_without_stat():
        path.write_bytes(b"# After!\n")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    if during_close:
        original_close = sync._ws.close

        async def close(*args, **kwargs):
            change_without_stat()
            await original_close(*args, **kwargs)

        monkeypatch.setattr(sync._ws, "close", close)
    else:
        change_without_stat()
    sync.begin_binding_quiesce()
    proof = await sync.drain_binding_change("external-fs", .03)
    assert not sync.validate_binding_drain(proof, "external-fs")
    assert path.read_bytes() == b"# After!\n"


def disk_bytes(*roots):
    """Ignore unrelated SQLite activity; callers choose only contract-owned paths."""
    return {str(p): p.read_bytes() for root in roots
            for p in ([root] if root.is_file() else root.rglob("*")) if p.is_file()}


def remote_url(scope, suffix=""):
    return f"/api/projects/{scope['project_id']}/remote{suffix}"


def captured(ctx):
    return {"backend_epoch": ctx.docs.transitions.backend_epoch,
            "expected_binding_generation": ctx.docs.transitions.generation}


def assert_error(response, code=None):
    assert response.status_code == 409, response.text
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"code", "details"}
    if code is not None:
        assert response.json()["code"] == code


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("I5 tests must never open a real Remote connection")
    monkeypatch.setattr(service, "connect", denied)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_URL", raising=False)


@pytest.fixture
async def projects(project, tmp_path, monkeypatch):
    monkeypatch.setattr(FileWatcher, "start", lambda self: None)
    monkeypatch.setattr(AppContext, "_start_plugin_hooks", lambda self: None)
    root_b = tmp_path / "project-b"
    root_b.mkdir()
    init_project(root_b)
    for root in (project, root_b):
        path = ProjectPaths.for_project(root).config_file
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # Identical business IDs and doc paths must never conflate resident Ctxs.
        raw["project_identity"] = {"id": "i5-shared-id", "revision": 7}
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    app = create_app(str(project))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as http:
            a = (await http.post("/api/projects/open", json={"path": str(project)})).json()
            b = (await http.post("/api/projects/open", json={"path": str(root_b)})).json()
            ca = app.state.project_registry[a["project_id"]].ctx
            cb = app.state.project_registry[b["project_id"]].ctx
            await ca.docs.write("same.md", "# A original\n")
            await cb.docs.write("same.md", "# B original\n")
            yield SimpleNamespace(app=app, http=http, a=a, b=b, ca=ca, cb=cb)


async def prepare(p, *, ctx=None, scope=None, **changes):
    ctx, scope = ctx or p.ca, scope or p.a
    observation = (await p.http.get(remote_url(scope))).json()
    body = {"expected_revision": observation["identity_revision"],
            "config_fingerprint": observation["config_fingerprint"],
            "persistent_id": "i5-new-id", "operation": "edit_id", **changes}
    return await p.http.post(remote_url(scope, "/prepare"), json=body)


async def register(p, *, ctx=None, scope=None, client="editor-a", path="same.md"):
    ctx, scope = ctx or p.ca, scope or p.a
    response = await p.http.post(remote_url(scope, "/editors"), json={
        **captured(ctx), "document_path": path, "client_instance_id": client,
    })
    assert response.status_code == 201, response.text
    return response.json()


async def save(p, editor, *, version=1, content="# Saved final text\n", sid=None):
    response = await p.http.put(f"/api/projects/{p.a['project_id']}/docs/same.md", json={
        **captured(p.ca), "editor_id": editor["editor_id"], "version": version,
        "content": content, **({"sid": sid} if sid else {}),
    })
    assert response.status_code == 200, response.text
    return response.json()["last_saved_version"]


@dataclass
class FakeSocket:
    sent: list = field(default_factory=list)
    closed: list = field(default_factory=list)
    sent_event: asyncio.Event = field(default_factory=asyncio.Event)

    async def send(self, message):
        self.sent.append(message)
        self.sent_event.set()

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, *args, **kwargs):
        self.closed.append((args, kwargs))


@pytest.fixture
def transport(tmp_path):
    paths = ProjectPaths.for_project(tmp_path / "fake-transport")
    paths.data.mkdir(parents=True)
    bus = EventBus()
    docs = DocStore(paths, bus)
    config = AppConfig()
    sync = RemoteSync(paths, lambda: config, docs, bus, token=lambda _: None)
    sync._url = "https://old-target.invalid"
    sync._space = "old-id"
    sync._ws = FakeSocket()
    sync._synced = True
    sync._state_name = "connected"
    sync._state = SyncState(tmp_path / "old-checkpoint.json", url=sync._url, space_id="old-incarnation")
    return sync


async def pending_put(sync, text="# Final scribe text\n"):
    await sync.docs.write("same.md", text)
    sync.begin_binding_quiesce()
    ws = sync._ws
    task = asyncio.create_task(sync.drain_binding_change("change-a", 2))
    await asyncio.wait_for(ws.sent_event.wait(), 1)
    frame = json.loads(ws.sent[0])
    assert frame["t"] == "put" and frame["content"] == text
    return task, frame, ws


async def test_transport_exact_delayed_ack_without_identity_doc_send_state_locks(transport):
    sync = transport
    task, frame, ws = await pending_put(sync)
    try:
        gate = sync.docs.transitions
        for lock in (gate.identity_lock, gate._mutation_lock, sync._send_lock, sync._lock):
            assert not lock.locked()
            await asyncio.wait_for(lock.acquire(), .5)
            lock.release()
        # _settled and unrelated ACKs are only wakeups, not drain receipts.
        sync._settled.set()
        sync._on_ack({"id": frame["id"] + 1, "rev": 100})
        await asyncio.sleep(0)
        assert not task.done() and frame["id"] in sync._pending
        sync._on_ack({"id": frame["id"], "rev": 8})
        proof = await asyncio.wait_for(task, 1)
        assert proof.reason is None and proof.reconciled and proof.fenced and proof.runner_stopped
        assert sync.validate_binding_drain(proof, "change-a")
        assert not sync.validate_binding_drain(replace(proof), "change-a")
        assert not sync.validate_binding_drain(proof, "different-change")
        assert sync._state.docs["same.md"] == Base(8, content_hash(frame["content"]))
        assert sync.docs.read("same.md")["content"] == frame["content"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_transport_timeout_retains_pending_room_live_text_and_old_target(transport):
    sync = transport
    text = "# Unsaved room screen retained\n"
    await sync.docs.write("same.md", text)
    browser = FakeSocket()
    sync._sessions["scribe"] = _Session("scribe", "same.md", browser, 1)
    sync._live_writes["same.md"] = _LiveWrite(content_hash(text), "scribe")
    sync.begin_binding_quiesce()
    before = sync._target_tuple()
    proof = await sync.drain_binding_change("timeout", .03)
    assert proof.reason == "drain_timeout" and not proof.fenced
    assert not sync.validate_binding_drain(proof, "timeout")
    assert sync._pending and sync._live_writes and sync._sessions
    assert not browser.closed and sync._target_tuple() == before
    assert sync.docs.read("same.md")["content"] == text
    assert not any(json.loads(x)["t"] == "leave" for x in sync._ws.sent)


async def test_disconnect_during_drain_cannot_clear_pending_or_destroy_editor(transport):
    sync = transport
    task, frame, ws = await pending_put(sync)
    browser = FakeSocket()
    sync._sessions["scribe"] = _Session("scribe", "same.md", browser, 1)
    sync._live_writes["same.md"] = _LiveWrite(content_hash(frame["content"]), "scribe")
    before = sync.binding_evidence()
    await sync._disconnected()
    proof = await asyncio.wait_for(task, 1)
    assert proof.reason in {"offline_unresolved", "unresolved_old_target"}
    assert not sync.validate_binding_drain(proof, "change-a")
    assert sync._pending and sync._pending_paths and sync._live_writes
    assert sync._sessions and not browser.closed
    evidence = sync.binding_evidence()
    assert evidence["target"] == before["target"]
    assert evidence["pending"] == before["pending"] and evidence["live"] == before["live"]
    assert evidence["retained"]


async def test_transport_error_never_launders_empty_pending_into_ready(transport):
    sync = transport
    task, frame, ws = await pending_put(sync)
    sync._on_error({"id": frame["id"], "message": "fixture rejected"})
    proof = await asyncio.wait_for(task, 1)
    assert proof.reason is not None and not proof.fenced
    assert not sync.validate_binding_drain(proof, "change-a")
    assert sync.docs.read("same.md")["content"] == frame["content"]
    assert sync.binding_evidence()["retained"] or sync._skip


async def test_nack_requires_second_exact_ack_after_reconciliation(transport):
    sync = transport
    task, frame, ws = await pending_put(sync)
    try:
        # A remote deletion loses to our edit; reconciliation must re-PUT from
        # its new base, and the first message's nack is not a success receipt.
        ws.sent_event.clear()
        await sync._on_nack({"id": frame["id"], "current": {"rev": 4, "deleted": True}})
        await asyncio.wait_for(ws.sent_event.wait(), 1)
        second = json.loads(ws.sent[-1])
        assert second["t"] == "put" and second["base_rev"] == 4
        assert second["id"] != frame["id"] and not task.done()
        sync._on_ack({"id": frame["id"], "rev": 5})
        await asyncio.sleep(0)
        assert not task.done() and second["id"] in sync._pending
        sync._on_ack({"id": second["id"], "rev": 6})
        proof = await asyncio.wait_for(task, 1)
        assert sync.validate_binding_drain(proof, "change-a")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancel_while_ack_pending_preserves_evidence_and_resumes_only_before_fence(transport):
    sync = transport
    task, frame, ws = await pending_put(sync)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sync._pending and sync.binding_evidence()["retained"]
    sync.cancel_binding_quiesce(fenced=False)
    assert not sync._binding_quiescing and not sync._binding_fenced
    assert frame["id"] in sync._pending and sync._ws is ws
    assert sync._binding_proof is None


async def test_quiesce_blocks_join_rejoin_reconnect_without_consuming_old_text(transport, monkeypatch):
    sync = transport
    await sync.docs.write("same.md", "# Original\n")
    sync._state.docs["same.md"] = Base(3, content_hash("# Original\n"))
    session = _Session("new-sid", "same.md", FakeSocket(), 2,
                       backend_epoch=sync.docs.transitions.backend_epoch)
    sync.begin_binding_quiesce()
    monkeypatch.setattr(sync, "_token", lambda _: pytest.fail("reconnect must not inspect credentials"))
    assert not await sync._join(session)
    await sync._rejoin(session)
    await sync._start_runner()
    assert sync._runner is None and not sync._ws.sent and not sync._sessions
    assert sync.docs.read("same.md")["content"] == "# Original\n"


async def test_stop_cleanup_failure_has_no_drain_proof_and_no_old_lease_resume(transport, monkeypatch):
    sync = transport
    sync.begin_binding_quiesce()
    monkeypatch.setattr(sync, "_stop_runner", AsyncMock(side_effect=OSError("synthetic close failure")))
    proof = await sync.drain_binding_change("stop-failed", .3)
    assert proof.reason == "drain_failed" and proof.fenced and not proof.runner_stopped
    assert not sync.validate_binding_drain(proof, "stop-failed")
    sync.cancel_binding_quiesce(fenced=True)
    assert sync._binding_quiescing and sync._binding_fenced


async def test_activated_drain_without_durable_sink_refuses_before_leave(transport):
    sync = transport
    sync.docs.transitions.observe_activated_model()
    browser = FakeSocket()
    sync._sessions["scribe"] = _Session("scribe", "same.md", browser, 1)
    sync.begin_binding_quiesce()
    proof = await sync.drain_binding_change("no-journal", .3)
    assert proof.reason == "binding_journal_unavailable" and not proof.fenced
    assert sync._sessions and not browser.closed and not sync._ws.sent


async def test_evidence_sink_failure_precedes_destructive_leave(transport):
    sync = transport
    sync.docs.transitions.observe_activated_model()
    sync.set_binding_evidence_sink(lambda _: (_ for _ in ()).throw(OSError("synthetic full disk")))
    browser = FakeSocket()
    sync._sessions["scribe"] = _Session("scribe", "same.md", browser, 1)
    sync.begin_binding_quiesce()
    # A persistence refusal may propagate; neither representation authorizes
    # destructive cleanup or changes the Markdown/room.
    try:
        proof = await sync.drain_binding_change("journal-failed", .3)
    except OSError:
        pass
    else:
        assert proof.reason is not None and not sync.validate_binding_drain(proof, "journal-failed")
    assert not sync._binding_fenced and sync._sessions and not browser.closed
    assert not sync._ws.sent


@pytest.fixture
def coordinator(transport):
    gate = transport.docs.transitions
    started = asyncio.Event()
    events = []

    async def publish(data):
        assert not gate.identity_lock.locked() and not gate._mutation_lock.locked()
        events.append(data)
        started.set()

    async def drain(change_id, timeout):
        assert not gate.identity_lock.locked() and not gate._mutation_lock.locked()
        return {"drained": True}

    gate.set_change_callbacks(begin_quiesce=lambda: None, drain=drain,
                              cancel_quiesce=lambda **kwargs: None, event=publish)
    return SimpleNamespace(gate=gate, docs=transport.docs, started=started, events=events)


def plan(c, **values):
    return c.gate.prepare_change({"backend_epoch": c.gate.backend_epoch,
                                  "binding_generation": c.gate.generation, **values})


def lease(c, client="editor-a", sid=None):
    gate = c.gate
    if sid is not None:
        gate.register_collab_session(sid, "same.md", gate.generation)
    return gate.register_editor("same.md", client, gate.backend_epoch, gate.generation, sid=sid)


async def save_receipt(c, row, *, version=1, sid=None, fail=False):
    gate = c.gate
    ticket = gate.begin_editor_save(row["editor_id"], gate.backend_epoch, gate.generation,
                                    sid=sid, version=version)
    if fail:
        gate.fail_editor_save(ticket)
        return None
    with gate.http_mutation_guard(lambda: gate.require_http(
            row["binding_generation"], backend_epoch=gate.backend_epoch,
            editor_id=row["editor_id"], document_path="same.md", sid=sid), save=ticket):
        await c.docs.write("same.md", f"# {row['client_instance_id']} version {version}\n",
                           expected_binding_generation=row["binding_generation"])
    return gate.record_saved(ticket)["last_saved_version"]


async def start_quiesce(c, prepared, timeout=1):
    task = asyncio.create_task(c.gate.quiesce_change(prepared["change_id"],
                                                   prepared["expected_generation"], timeout=timeout))
    await asyncio.wait_for(c.started.wait(), .5)
    return task


def ack(c, prepared, row, receipt):
    return c.gate.editor_ack(prepared["change_id"], row["editor_id"],
                            row["binding_generation"], receipt, backend_epoch=c.gate.backend_epoch)


async def test_prepare_captures_deep_immutable_expected_evidence_and_exact_ttl(coordinator, monkeypatch):
    c = coordinator
    now = [10.0]
    monkeypatch.setattr(c.gate, "_clock", lambda: 1000.0)
    monkeypatch.setattr(c.gate, "_monotonic", lambda: now[0])
    original = {"to": {"id": "target-id", "catalog": [1, 2]}, "revision": 7}
    before = disk_bytes(c.docs.paths.root)
    prepared = plan(c, details=original)
    assert prepared["state"] == "prepared" and prepared["expires_at"] == 1300.0
    assert c.gate.generation == 0 and not c.gate.binding_quiescing and not c.gate.activated
    original["to"]["catalog"].append(3)
    evidence = c.gate.expected_change_evidence(prepared["change_id"])
    assert evidence["details"]["to"]["catalog"] == (1, 2)
    with pytest.raises(TypeError):
        evidence["details"]["to"]["id"] = "other"
    assert disk_bytes(c.docs.paths.root) == before and not c.events
    now[0] = 309.99
    assert c.gate.change_status(prepared["change_id"])["state"] == "prepared"
    now[0] = 310.0
    assert c.gate.change_status(prepared["change_id"])["state"] == "expired"
    assert c.gate.generation == 0 and not c.gate.binding_quiescing


@pytest.mark.parametrize("bad", [{"binding_generation": -1}, {"binding_generation": True},
                                 {"backend_epoch": "previous-backend"}])
async def test_prepare_rejects_stale_generation_epoch_without_adoption(coordinator, bad):
    c = coordinator
    with pytest.raises(MutationGateError):
        plan(c, **bad)
    assert c.gate.generation == 0 and not c.gate.binding_quiescing and not c.events


async def test_all_ordinary_and_scribe_editors_need_exact_server_save_and_explicit_ack(coordinator):
    c = coordinator
    first = lease(c, "ordinary-a")
    scribe = lease(c, "scribe", sid="room-a")
    prepared = plan(c)
    # Prepare does not freeze admission, so this third instance is owed an ACK too.
    last = lease(c, "ordinary-b")
    task = await start_quiesce(c, prepared)
    try:
        assert not c.gate.identity_lock.locked() and not c.gate._mutation_lock.locked()
        for row, sid in [(first, None), (scribe, "room-a"), (last, None)]:
            receipt = await save_receipt(c, row, sid=sid)
            with pytest.raises(MutationGateError):
                ack(c, prepared, row, receipt + 1)
            result = ack(c, prepared, row, receipt)
            if row is not last:
                assert result["missing_acks"] and not task.done()
        ready = await asyncio.wait_for(task, 1)
        assert ready["state"] == "ready" and ready["fenced"]
        assert ready["binding_generation"] != prepared["binding_generation"]
        assert not ready["missing_acks"] and c.gate.generation == ready["binding_generation"]
        assert c.docs.read("same.md")["content"] == "# ordinary-b version 1\n"
        # Ready is not commit/Enable. It remains paused for all ordinary writers.
        assert not c.gate.activated
        with pytest.raises(MutationGateError):
            c.gate.register_editor("same.md", "new-browser", c.gate.backend_epoch, c.gate.generation)
    finally:
        if not task.done():
            c.gate.cancel_change(prepared["change_id"])
            await task


@pytest.mark.parametrize("bad_receipt", [None, True, 1, 999999])
async def test_dirty_editor_cannot_forge_clean_or_ack_a_failed_save(coordinator, bad_receipt):
    c = coordinator
    row = lease(c)
    await save_receipt(c, row, fail=True)
    prepared = plan(c)
    task = await start_quiesce(c, prepared)
    try:
        c.gate.heartbeat_editor(row["editor_id"], c.gate.backend_epoch, 0,
                                dirty=False, version=1, last_saved_version=bad_receipt)
        with pytest.raises(MutationGateError):
            ack(c, prepared, row, bad_receipt)
        assert not task.done() and c.gate.generation == 0
        assert c.gate.change_status(prepared["change_id"])["missing_acks"] == [row["editor_id"]]
    finally:
        cancelled = c.gate.cancel_change(prepared["change_id"])
        assert cancelled["state"] == "cancelled" and not cancelled["fenced"]
        await task


@pytest.mark.parametrize("field,value", [("version", 1), ("version", True), ("revision", 1),
                                         ("revision", False), ("sid", "other"),
                                         ("document_path", "other.md"),
                                         ("client_instance_id", "other")])
def test_materialization_cas_never_adopts_client_version_or_identity(field, value):
    from voxarium_studio.sync.transitions import BindingTransitions

    gate = BindingTransitions(backend_epoch="proof-epoch")
    gate.register_collab_session("sid", "same.md", 0)
    row = gate.register_editor("same.md", "browser", gate.backend_epoch, 0, sid="sid")
    captured = dict(document_path="same.md", client_instance_id="browser", sid="sid", version=0, revision=0)
    captured[field] = value
    with pytest.raises(MutationGateError):
        gate.confirm_materialized(row["editor_id"], gate.backend_epoch, 0, check=lambda: True, **captured)
    lease = gate._editors[row["editor_id"]]
    assert lease.version == 0 and lease.revision == 0 and lease.last_saved_version is None


async def test_materialized_receipt_is_rechecked_for_heartbeat_close_and_ack(coordinator):
    c = coordinator
    row = lease(c, "proof-browser", sid="proof-sid")
    current = c.gate._editors[row["editor_id"]]
    valid = [True]
    captured = dict(document_path=current.document_path, client_instance_id=current.client_instance_id,
                    sid=current.sid, version=current.version, revision=current.revision)
    receipt = c.gate.confirm_materialized(row["editor_id"], c.gate.backend_epoch, 0,
                                         check=lambda: valid[0], **captured)
    prepared = plan(c)
    task = await start_quiesce(c, prepared)
    try:
        valid[0] = False
        c.gate.heartbeat_editor(row["editor_id"], c.gate.backend_epoch, 0, sid=current.sid,
                                dirty=False, version=0, last_saved_version=receipt["last_saved_version"])
        with pytest.raises(MutationGateError):
            ack(c, prepared, row, receipt["last_saved_version"])
        with pytest.raises(MutationGateError):
            c.gate.close_editor(row["editor_id"], c.gate.backend_epoch, 0, sid=current.sid,
                                version=0, dirty=False, last_saved_version=receipt["last_saved_version"])
        assert row["editor_id"] in c.gate.change_status(prepared["change_id"])["missing_acks"]
        assert not task.done() and c.gate.generation == 0
    finally:
        c.gate.cancel_change(prepared["change_id"])
        await task


@pytest.mark.parametrize("kind", ["expired_dirty", "expired_clean", "legacy_client", "unknown_inventory", "broken_inventory"])
async def test_unreachable_editor_or_unknown_client_is_blocked_not_silently_dropped(coordinator, monkeypatch, kind):
    c = coordinator
    now = [0.0]
    monkeypatch.setattr(c.gate, "_monotonic", lambda: now[0])
    if kind.startswith("expired"):
        row = lease(c)
        if kind == "expired_clean":
            await save_receipt(c, row)
        now[0] = 61.0
    elif kind == "legacy_client":
        c.gate.register_client("legacy-ws")
        c.gate.close_client("legacy-ws")
    elif kind == "unknown_inventory":
        c.gate.set_client_inventory(lambda: 1)
    else:
        c.gate.set_client_inventory(lambda: (_ for _ in ()).throw(RuntimeError("inventory lost")))
    prepared = plan(c)
    result = await c.gate.quiesce_change(prepared["change_id"], 0, timeout=.1)
    assert result["state"] == "blocked" and not result["fenced"]
    assert c.gate.generation == 0
    if kind.startswith("expired"):
        assert result["missing_acks"] == [row["editor_id"]]
    assert c.gate.cancel_change(prepared["change_id"])["state"] == "cancelled"


async def test_timeout_then_cancel_before_fence_resumes_original_lease_generation(coordinator):
    c = coordinator
    row = lease(c)
    prepared = plan(c)
    result = await c.gate.quiesce_change(prepared["change_id"], 0, timeout=.02)
    assert result["state"] == "blocked" and result["reason"] == "drain_timeout"
    assert not result["fenced"] and c.gate.generation == 0
    c.gate.cancel_change(prepared["change_id"])
    receipt = await save_receipt(c, row)
    assert isinstance(receipt, int) and c.gate.generation == 0


async def test_cancel_after_ready_never_revives_old_leases_or_old_generation(coordinator):
    c = coordinator
    row = lease(c)
    prepared = plan(c)
    task = await start_quiesce(c, prepared)
    receipt = await save_receipt(c, row)
    ack(c, prepared, row, receipt)
    ready = await asyncio.wait_for(task, 1)
    new_g = ready["binding_generation"]
    assert new_g != row["binding_generation"]
    cancelled = c.gate.cancel_change(prepared["change_id"])
    assert cancelled["state"] == "paused" and cancelled["binding_generation"] == new_g
    assert cancelled["reason"] == "resume_required" and c.gate.binding_quiescing
    for old_g in (row["binding_generation"], new_g):
        with pytest.raises(MutationGateError):
            c.gate.begin_editor_save(row["editor_id"], c.gate.backend_epoch, old_g, version=2)
    assert c.gate.cancel_change(prepared["change_id"]) == cancelled


async def test_expiry_during_editor_wait_cancels_without_changing_generation(coordinator, monkeypatch):
    c = coordinator
    now = [0.0]
    monkeypatch.setattr(c.gate, "_monotonic", lambda: now[0])
    row = lease(c)
    prepared = plan(c)
    task = await start_quiesce(c, prepared)
    now[0] = 300.0
    expired = c.gate.change_status(prepared["change_id"])
    assert expired["state"] == "expired" and expired["binding_generation"] == 0
    await asyncio.wait_for(task, 1)
    assert not c.gate.binding_quiescing and row["editor_id"] in c.gate._editors


async def http_drain_started(p, prepared):
    sub = p.ca.bus.subscribe()
    task = asyncio.create_task(p.http.post(remote_url(p.a, f"/changes/{prepared['change_id']}/quiesce"),
                                           json={"expected_generation": prepared["expected_generation"]}))
    async def wait_for_freeze():
        while True:
            event = await sub.get()
            if event.type == "remote.binding_change" and event.data.get("state") == "draining":
                return
    listener = asyncio.create_task(wait_for_freeze())
    try:
        done, _ = await asyncio.wait((task, listener), timeout=1, return_when=asyncio.FIRST_COMPLETED)
        if listener in done:
            return task
        if task in done:
            result = task.result()  # Surface an endpoint exception, not an orphan task.
            raise AssertionError(f"quiesce returned before a freeze event: {result.status_code} {result.text}")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise AssertionError("quiesce did not publish its Ctx freeze request")
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        sub.close()


async def http_ack(p, prepared, editor, receipt):
    return await p.http.post(remote_url(p.a, f"/changes/{prepared['change_id']}/editor-ack"), json={
        "editor_id": editor["editor_id"], "old_generation": editor["binding_generation"],
        "last_saved_version": receipt,
    })


async def test_http_prepare_cancel_is_readonly_and_does_not_reserve_or_publish(projects, studio_home):
    p = projects
    paths = (p.ca.paths.config_file, p.cb.paths.config_file, p.ca.paths.docs, p.cb.paths.docs, studio_home)
    before = disk_bytes(*paths)
    seq_a, seq_b = p.ca.bus.current_seq, p.cb.bus.current_seq
    reservations = dict(p.app.state.remote_reservations._reservations)
    response = await prepare(p)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    prepared = response.json()
    assert prepared["state"] == "prepared" and prepared["plan"]["enabled"] is False
    assert p.ca.bus.current_seq == seq_a and p.cb.bus.current_seq == seq_b
    assert disk_bytes(*paths) == before
    assert p.app.state.remote_reservations._reservations == reservations
    cancelled = await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert disk_bytes(*paths) == before and p.ca.docs.transitions.generation == 0


@pytest.mark.parametrize("changes,code", [({"expected_revision": 6}, "stale_binding"),
                                         ({"config_fingerprint": "0" * 64}, "stale_binding"),
                                         ({"backend_epoch": "old-backend"}, "stale_backend_epoch")])
async def test_http_prepare_rejects_stale_evidence_no_side_effects(projects, changes, code):
    p = projects
    before = disk_bytes(p.ca.paths.config_file, p.ca.paths.docs)
    assert_error(await prepare(p, **changes), code)
    assert disk_bytes(p.ca.paths.config_file, p.ca.paths.docs) == before
    assert p.ca.docs.transitions.generation == 0 and not p.ca.docs.transitions.binding_quiescing


async def test_http_ordinary_save_receipt_ack_and_ctx_only_ready_keep_b_agent_running(projects):
    p = projects
    editor = await register(p)
    # A clean initial read is evidence, but still needs an explicit flush ACK.
    assert type(editor["last_saved_version"]) is int
    prepared_response = await prepare(p)
    assert prepared_response.status_code == 200, prepared_response.text
    prepared = prepared_response.json()
    before_config = disk_bytes(p.ca.paths.config_file, p.cb.paths.config_file)
    seq_b = p.cb.bus.current_seq
    step, finish = asyncio.Event(), asyncio.Event()

    async def active_b_agent():
        await step.wait()
        await p.cb.docs.write("agent.md", "# Agent B continued\n")
        finish.set()
        await asyncio.Event().wait()

    job = asyncio.create_task(active_b_agent())
    task = await http_drain_started(p, prepared)
    try:
        # No long application/identity/doc lock while waiting for editor ACK.
        for lock in (p.app.state.project_lock, p.ca.docs.transitions.identity_lock,
                     p.ca.docs.transitions._mutation_lock, p.ca.sync._send_lock, p.ca.sync._lock):
            assert not lock.locked()
        receipt = await save(p, editor)
        assert_error(await http_ack(p, prepared, editor, receipt + 1), "invalid_editor_ack")
        step.set()
        await asyncio.wait_for(finish.wait(), 1)
        accepted = await http_ack(p, prepared, editor, receipt)
        assert accepted.status_code == 200, accepted.text
        result = await asyncio.wait_for(task, 2)
        assert result.status_code == 200, result.text
        assert result.json()["state"] == "ready" and result.json()["fenced"]
        assert not job.done() and not job.cancelling()
        assert p.cb.docs.read("same.md")["content"] == "# B original\n"
        assert p.cb.docs.read("agent.md")["content"] == "# Agent B continued\n"
        assert p.cb.docs.transitions.generation == 0 and not p.cb.docs.transitions.binding_quiescing
        assert disk_bytes(p.ca.paths.config_file, p.cb.paths.config_file) == before_config
        events_b = p.cb.bus.replay(seq_b)
        assert not any(event.type == "remote.binding_change" for event in events_b)
        assert p.ca.config.project_identity.id == "i5-shared-id" and not p.ca.config.sync.enabled
        # A post-fence cancel is paused/new G, never resurrecting this editor.
        cancel = await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
        assert cancel.status_code == 200 and cancel.json()["state"] == "paused"
        stale = await p.http.put(f"/api/projects/{p.a['project_id']}/docs/same.md", json={
            "backend_epoch": editor["backend_epoch"],
            "expected_binding_generation": editor["binding_generation"],
            "editor_id": editor["editor_id"], "version": 2, "content": "must not write",
        })
        assert_error(stale)
        assert p.ca.docs.read("same.md")["content"] == "# Saved final text\n"
    finally:
        if not task.done():
            await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
            await asyncio.gather(task, return_exceptions=True)
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


async def test_http_cancel_during_editor_wait_resumes_same_lease_no_new_generation(projects):
    p = projects
    editor = await register(p)
    response = await prepare(p)
    assert response.status_code == 200, response.text
    prepared = response.json()
    task = await http_drain_started(p, prepared)
    cancelled = await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled" and not cancelled.json()["fenced"]
    await asyncio.wait_for(task, 1)
    assert p.ca.docs.transitions.generation == editor["binding_generation"]
    receipt = await save(p, editor, content="# Saved after safe cancel\n")
    assert type(receipt) is int


async def test_http_repeated_quiesce_ready_is_idempotent_not_a_second_sink_install(projects):
    p = projects
    # Synthetic old checkpoint is already equal to local Markdown; no wire ACK
    # needed and an offline edit_id may become ready (not committed).
    p.ca.sync._state = SyncState(p.ca.paths.root / "checkpoint.json", url="", space_id="")
    p.ca.sync._state.docs["same.md"] = Base(2, content_hash("# A original\n"))
    response = await prepare(p)
    assert response.status_code == 200, response.text
    prepared = response.json()
    url = remote_url(p.a, f"/changes/{prepared['change_id']}/quiesce")
    body = {"expected_generation": prepared["expected_generation"]}
    first = await p.http.post(url, json=body)
    assert first.status_code == 200 and first.json()["state"] == "ready", first.text
    second = await p.http.post(url, json=body)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()


async def test_http_physical_fence_failure_cancel_cannot_reopen_old_editor(projects, monkeypatch):
    p = projects
    editor = await register(p)
    p.ca.sync._state = SyncState(p.ca.paths.root / "checkpoint.json", url="", space_id="")
    p.ca.sync._state.docs["same.md"] = Base(2, content_hash("# A original\n"))
    p.ca.sync._ws = FakeSocket()
    p.ca.sync._synced = True
    response = await prepare(p)
    assert response.status_code == 200, response.text
    prepared = response.json()
    with monkeypatch.context() as m:
        m.setattr(p.ca.sync, "_stop_runner", AsyncMock(side_effect=OSError("stop failed after fence")))
        task = await http_drain_started(p, prepared)
        try:
            accepted = await http_ack(p, prepared, editor, editor["last_saved_version"])
            assert accepted.status_code == 200, accepted.text
            result = await asyncio.wait_for(task, 1)
            assert result.status_code == 200, result.text
            assert result.json()["state"] in {"blocked", "paused"} and p.ca.sync._binding_fenced
            cancelled = await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["state"] == "paused" and cancelled.json()["fenced"]
            assert cancelled.json()["binding_generation"] != editor["binding_generation"]
            late = await p.http.put(f"/api/projects/{p.a['project_id']}/docs/same.md", json={
                "backend_epoch": editor["backend_epoch"], "expected_binding_generation": editor["binding_generation"],
                "editor_id": editor["editor_id"], "version": 1, "content": "old lease revived",
            })
            assert_error(late)
            assert p.ca.docs.read("same.md")["content"] == "# A original\n"
        finally:
            if not task.done():
                await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("endpoint", ["quiesce", "editor-ack"])
async def test_http_config_byte_drift_blocks_later_phases_but_never_cancel(projects, endpoint):
    p = projects
    response = await prepare(p)
    assert response.status_code == 200, response.text
    prepared = response.json()
    # No semantic change: evidence must still be exact config bytes, not just R.
    before = p.ca.paths.config_file.read_bytes()
    p.ca.paths.config_file.write_bytes(before + b"\n# another writer\n")
    payload = ({"expected_generation": 0} if endpoint == "quiesce" else
               {"editor_id": "unknown", "old_generation": 0, "last_saved_version": 1})
    result = await p.http.post(remote_url(p.a, f"/changes/{prepared['change_id']}/{endpoint}"), json=payload)
    assert_error(result, "stale_binding")
    cancelled = await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert p.ca.paths.config_file.read_bytes() == before + b"\n# another writer\n"
    assert p.ca.docs.transitions.generation == 0


async def test_http_change_id_does_not_cross_ctx_and_quiesce_has_no_legacy_escape(projects):
    p = projects
    editor = await register(p)
    response = await prepare(p)
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert_error(await p.http.delete(remote_url(p.b, f"/changes/{prepared['change_id']}")), "unknown_change")
    task = await http_drain_started(p, prepared)
    try:
        before = p.ca.paths.config_file.read_bytes()
        new_editor = await p.http.post(remote_url(p.a, "/editors"), json={
            **captured(p.ca), "document_path": "same.md", "client_instance_id": "new-browser",
        })
        assert_error(new_editor)
        for url in (f"/api/projects/{p.a['project_id']}/sync", "/api/sync"):
            # The selected legacy context is B. Only the explicit A URL must be
            # fenced; do not bind the legacy adapter to A by changing selection.
            if url == "/api/sync":
                continue
            assert_error(await p.http.put(url, json={"enabled": False}), "use_remote_settings")
        assert p.ca.paths.config_file.read_bytes() == before
        assert p.cb.docs.transitions.generation == 0
    finally:
        await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
        await asyncio.wait_for(task, 1)


async def test_closed_clean_editor_does_not_replace_explicit_change_ack(coordinator):
    c = coordinator
    row = lease(c)
    receipt = await save_receipt(c, row)
    prepared = plan(c)
    c.gate.close_editor(row["editor_id"], c.gate.backend_epoch, 0,
                         dirty=False, version=1, last_saved_version=receipt)
    task = await start_quiesce(c, prepared)
    try:
        assert c.gate.change_status(prepared["change_id"])["missing_acks"] == [row["editor_id"]]
        ack(c, prepared, row, receipt)
        assert (await asyncio.wait_for(task, 1))["state"] == "ready"
    finally:
        if not task.done():
            c.gate.cancel_change(prepared["change_id"])
            await task


@pytest.mark.parametrize("reply", [None, {}, {"drained": False}, {"drained": 1}])
async def test_coordinator_never_uses_false_empty_or_truthy_nonboolean_drain_proof(coordinator, reply):
    c = coordinator
    c.gate.set_change_callbacks(begin_quiesce=lambda: None,
                                drain=AsyncMock(return_value=reply), cancel_quiesce=lambda **kwargs: None)
    prepared = plan(c)
    result = await c.gate.quiesce_change(prepared["change_id"], 0, timeout=.1)
    assert result["state"] == "blocked" and not result["fenced"]
    assert c.gate.generation == 0


async def test_evidence_writer_is_lazy_atomic_nonsecret_and_retains_old_ambiguous_send(transport, studio_home):
    from voxarium_studio.sync.transitions import binding_change_evidence_writer
    sync = transport
    change_id = "a" * 32
    before = disk_bytes(studio_home)
    sink = binding_change_evidence_writer(sync.paths.root, change_id,
                                           {"from": {"id": "old"}, "to": {"id": "new"}, "revision": 7})
    assert disk_bytes(studio_home) == before  # Prepare only constructs the callback.
    sync._pending[42] = _Pending("same.md", "a" * 64, base_rev=4, generation=0,
                                 target=sync._target_tuple())
    old_evidence = sync.binding_evidence()
    sink(old_evidence)
    files = list((studio_home / "remote-changes").rglob("*.json"))
    assert len(files) == 1
    first = json.loads(files[0].read_text(encoding="utf-8"))
    assert first["change_id"] == change_id and first["evidence"]
    sync._pending.clear()
    sink(sync.binding_evidence())
    final = json.loads(files[0].read_text(encoding="utf-8"))
    assert any(item["pending"] for item in final["evidence"])
    assert first["expected"] == final["expected"]
    written = files[0].read_bytes()
    with pytest.raises((MutationGateError, ValueError, OSError)):
        sink({**old_evidence, "content": "SECRET-MARKDOWN-CANARY"})
    assert files[0].read_bytes() == written
    assert b"SECRET-MARKDOWN-CANARY" not in written


async def test_evidence_writer_preserves_corrupt_prior_journal(transport, studio_home):
    from voxarium_studio.sync.transitions import binding_change_evidence_writer
    sync = transport
    sink = binding_change_evidence_writer(sync.paths.root, "b" * 32, {"revision": 7})
    sink(sync.binding_evidence())
    path = next((studio_home / "remote-changes").rglob("*.json"))
    path.write_bytes(b'{"version":1,"version":2,broken')
    with pytest.raises((MutationGateError, ValueError, OSError)):
        sink(sync.binding_evidence())
    assert path.read_bytes() == b'{"version":1,"version":2,broken'


async def test_http_cancel_between_physical_fence_and_stop_join_revokes_old_generation(projects, monkeypatch):
    from voxarium_studio.sync import transitions as transitions_module
    p = projects
    editor = await register(p)
    p.ca.sync._state = SyncState(p.ca.paths.root / "checkpoint.json", url="", space_id="")
    p.ca.sync._state.docs["same.md"] = Base(2, content_hash("# A original\n"))
    p.ca.sync._ws = FakeSocket()
    p.ca.sync._synced = True
    response = await prepare(p)
    assert response.status_code == 200, response.text
    prepared = response.json()
    stopping = asyncio.Event()

    async def slow_stop():
        stopping.set()
        await asyncio.Event().wait()

    with monkeypatch.context() as m:
        # Isolate cancellation/fence wiring from durable journal schema, which
        # is independently exercised above with the real writer.
        m.setattr(transitions_module, "binding_change_evidence_writer", lambda *a, **k: lambda _: None)
        m.setattr(p.ca.sync, "_stop_runner", slow_stop)
        task = await http_drain_started(p, prepared)
        try:
            accepted = await http_ack(p, prepared, editor, editor["last_saved_version"])
            assert accepted.status_code == 200, accepted.text
            await asyncio.wait_for(stopping.wait(), 1)
            assert p.ca.sync._binding_fenced
            cancelled = await p.http.delete(remote_url(p.a, f"/changes/{prepared['change_id']}"))
            assert cancelled.status_code == 200, cancelled.text
            result = await asyncio.wait_for(task, 1)
            assert result.status_code == 200, result.text
            status = p.ca.docs.transitions.change_status(prepared["change_id"])
            assert status["state"] == "paused" and status["fenced"]
            assert status["binding_generation"] != editor["binding_generation"]
            late = await p.http.put(f"/api/projects/{p.a['project_id']}/docs/same.md", json={
                "backend_epoch": editor["backend_epoch"],
                "expected_binding_generation": editor["binding_generation"],
                "editor_id": editor["editor_id"], "version": 1, "content": "late after physical fence",
            })
            assert_error(late)
            assert p.ca.docs.read("same.md")["content"] == "# A original\n"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

# VS-177 / I6-I7: G is safe-integer monotonic within a backend epoch.
@pytest.mark.parametrize("bad", [True, -1, 0, 1, 2**53, 2**62])
async def test_i6_complete_rejects_arbitrary_or_nonadvancing_generation(coordinator, bad):
    c = coordinator
    prepared = plan(c)
    ready = await c.gate.quiesce_change(prepared["change_id"], 0)
    assert ready["state"] == "ready" and c.gate.generation == 1
    with pytest.raises(MutationGateError) as caught:
        c.gate.complete_commit(prepared["change_id"], bad)
    assert caught.value.code == "stale_binding"
    assert c.gate.generation == 1
    assert c.gate.change_status(prepared["change_id"])["state"] == "ready"
    c.gate.cancel_change(prepared["change_id"])


@pytest.mark.parametrize("start", [2**53 - 2, 2**53 - 1])
async def test_i6_overflow_refuses_before_service_quiesce_or_drain(coordinator, start):
    c = coordinator
    c.gate._generation = start
    calls = []
    c.gate._begin_quiesce = lambda: calls.append("quiesce")
    async def drain(*args):
        calls.append("drain")
        return {"drained": True}
    c.gate._drain_change = drain
    prepared = plan(c)
    with pytest.raises(MutationGateError) as caught:
        await c.gate.quiesce_change(prepared["change_id"], start)
    assert caught.value.code == "binding_generation_exhausted"
    assert calls == [] and c.gate.generation == start
    assert c.gate.change_status(prepared["change_id"])["state"] == "prepared"
    c.gate.cancel_change(prepared["change_id"])


async def test_i6_last_safe_increment_pair_and_snapshot_overflow(coordinator):
    from voxarium_studio.sync.transitions import MAX_BINDING_GENERATION
    c = coordinator
    c.gate._generation = MAX_BINDING_GENERATION - 2
    prepared = plan(c)
    ready = await c.gate.quiesce_change(prepared["change_id"], MAX_BINDING_GENERATION - 2)
    assert ready["state"] == "ready" and c.gate.generation == MAX_BINDING_GENERATION - 1
    committed = c.gate.complete_commit(prepared["change_id"])
    assert committed["binding_generation"] == MAX_BINDING_GENERATION
    async with c.gate.final_snapshot() as fence:
        with pytest.raises(MutationGateError) as caught:
            fence.advance_generation()
        assert caught.value.code == "binding_generation_exhausted"
    assert c.gate.generation == MAX_BINDING_GENERATION
