"""I2 synthetic runtime admission fixtures (studio-design §4.3; runtime plan §3.1).

No production activation reader, v2 transport, key, or legacy checkpoint is used.
The protected runner seam only supplies local coroutines; accepting these tests
does not remove R3 or implement Enable/Preview, generation fencing, or drain.
"""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, dataclass, replace
import hashlib
import json
from pathlib import Path
import socket
from typing import Any

import pytest
import yaml

from voxarium_studio.core.application_config import RemoteServerProfile
from voxarium_studio.core.config import AppConfig
from voxarium_studio.core.context import _ProfileRemoteSync
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project_scope import canonical_root
from voxarium_studio.engine.jobs import JobEngine
from voxarium_studio.sync import credentials, service
from voxarium_studio.sync.bindings import BindingError, RemoteBindings, bindings_file, root_key
from voxarium_studio.sync.profile_lifecycle import ProfileLifecycle
from voxarium_studio.sync.profiles import ProfileError
from voxarium_studio.sync.reservations import ReservationError, ReservationOwner
from voxarium_studio.sync.service import ActivatedRemoteTarget, RemoteSync, preflight_remote_binding
from voxarium_studio.sync.state import SyncNamespace, SyncState, SyncStateError, SyncStateV2
from voxarium_studio.teams.docs import DocStore
from voxarium_studio.ws.bus import EventBus


PROFILE = "c503da2b-f8fe-4fb0-b945-86bc119495b3"
OTHER_PROFILE = "81ccda2b-f8fe-4fb0-b945-86bc119495b3"
URL = "https://runtime.invalid/team"
OTHER_URL = "https://other-runtime.invalid/team"
INSTANCE = "0123456789abcdef0123456789abcdef"
OTHER_INSTANCE = "fedcba9876543210fedcba9876543210"


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    """Count forbidden calls even when production catches their exceptions."""
    calls = []

    def denied(*_args, **_kwargs):
        calls.append("forbidden I/O")
        raise AssertionError("Runtime fixture attempted keys, v1, network or persistence")

    for name in ("keys_file", "load_token", "save_token", "load_profile_token",
                 "save_profile_token", "profile_token_revision", "import_legacy_token"):
        monkeypatch.setattr(credentials, name, denied)
    monkeypatch.setattr(service, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(SyncState, "load", denied)
    monkeypatch.setattr(SyncState, "save", denied)
    monkeypatch.setattr(SyncStateV2, "save", denied)
    monkeypatch.setattr(RemoteBindings, "save", denied)
    monkeypatch.setattr(RemoteSync, "_run", denied)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_URL", raising=False)
    yield denied
    assert calls == []


def tree_snapshot(*roots):
    """Reads must preserve bytes, mtimes, files AND directories (no lock creation)."""
    return {
        str(path): (path.is_dir(), path.stat().st_mtime_ns,
                    None if path.is_dir() else path.read_bytes())
        for root in roots for path in [root, *root.rglob("*")] if path.exists()
    }


@dataclass
class Fixture:
    paths: ProjectPaths
    home: Path
    raw: dict[str, Any]
    config: AppConfig
    target: ActivatedRemoteTarget
    profile: RemoteServerProfile
    profile_reads: int = 0

    def read_profile(self, profile_id):
        self.profile_reads += 1
        assert profile_id == PROFILE
        return self.profile

    def write_config(self):
        self.paths.config_file.write_text(yaml.safe_dump(self.raw), encoding="utf-8")
        self.target = replace(self.target, config_fingerprint=hashlib.sha256(
            self.paths.config_file.read_bytes()).hexdigest())

    def row(self, **changes):
        return {
            "canonical_root": self.target.canonical_root, "profile_id": self.target.profile_id,
            "url": self.target.url, "persistent_id": self.target.persistent_id,
            "identity_revision": self.target.identity_revision,
            "selection_state": "confirmed", "space_instance_id": self.target.space_instance_id, **changes,
        }

    def write_rows(self, *rows):
        bindings_file().write_text(json.dumps({"version": 1, "roots": {
            root_key(row["canonical_root"]): row for row in rows
        }}), encoding="utf-8")

    @property
    def namespace(self):
        return SyncNamespace.for_project(self.paths.root, url=self.target.url,
                                         persistent_id=self.target.persistent_id)

    def state_payload(self, **changes):
        return {"version": 2, "namespace": self.namespace.as_dict(),
                "space_instance_id": self.target.space_instance_id, "cursor": 3,
                "docs": {"note.md": {"rev": 2, "hash": "a" * 64}}, **changes}

    def write_state(self, **changes):
        path = self.namespace.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.state_payload(**changes)), encoding="utf-8")

    def preflight(self, target=None):
        return preflight_remote_binding(self.paths, self.config, target or self.target,
                                        home=self.home, profile_reader=self.read_profile)


@pytest.fixture
def binding(tmp_path, studio_home):
    paths = ProjectPaths.for_project(tmp_path / "project-a")
    paths.data.mkdir(parents=True)
    raw = {"project_identity": {"id": "synthetic-runtime", "revision": 7},
           "sync": {"enabled": True, "url": URL}}
    target = ActivatedRemoteTarget(str(canonical_root(paths.root)), "", PROFILE, URL,
                                   "synthetic-runtime", 7, INSTANCE)
    fixture = Fixture(paths, studio_home, raw, AppConfig.model_validate(raw), target,
                      RemoteServerProfile(profile_id=PROFILE, name="Fixture", url=URL))
    fixture.write_config()
    fixture.write_rows(fixture.row())
    fixture.write_state()
    return fixture


class TrackedLease:
    def __init__(self, lease):
        self.lease = lease
        self.releases = 0

    def release(self):
        self.releases += 1
        self.lease.release()


class TrackedLifecycle(ProfileLifecycle):
    def __init__(self, binding):
        super().__init__(profile_reader=binding.read_profile)
        self.issued = []
        self.after_attach = lambda: None

    def attach(self, **kwargs):
        lease = TrackedLease(super().attach(**kwargs))
        self.issued.append(lease)
        self.after_attach()
        return lease

    @property
    def live_leases(self):
        self._reap_available()
        return len(self._leases)


class FakeSync(_ProfileRemoteSync):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.publications = []
        self.started = asyncio.Event()
        self.closing = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.allow_close.set()
        self.fail_publication = False
        self.cleanup_failures = 0

    def _activated_runner(self, observation):
        self.publications.append(observation)
        if self.fail_publication:
            raise RuntimeError("synthetic start failure")
        return self.fake_job()

    async def fake_job(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closing.set()
            await self.allow_close.wait()

    async def _disconnected(self):
        if self.cleanup_failures:
            self.cleanup_failures -= 1
            raise RuntimeError("synthetic cleanup failure")
        await super()._disconnected()


def make_sync(binding, no_external_io, *, owner=None, lifecycle=None,
              cls=FakeSync, paths=None, activation=None):
    owner = owner if owner is not None else ReservationOwner()
    lifecycle = lifecycle if lifecycle is not None else TrackedLifecycle(binding)
    paths = paths or binding.paths
    bus = EventBus()
    sync = cls(paths, lambda: binding.config, DocStore(paths, bus), bus,
               lifecycle=lambda: lifecycle, reservations=lambda: owner,
               activation=activation or (lambda: binding.target),
               profile_reader=binding.read_profile)
    # RemoteSync's default token parameter is bound at import time.
    sync._token = no_external_io
    return sync, owner, lifecycle


async def wait_for(event):
    await asyncio.wait_for(event.wait(), timeout=2)


def assert_owned(owner, binding, consumer):
    with pytest.raises(ReservationError, match="duplicate_local_binding"):
        owner.reserve(root=binding.paths.root, url=URL,
                      persistent_id="synthetic-runtime", consumer=consumer)


def test_preflight_exact_binding_readonly_and_frozen(binding):
    before = tree_snapshot(binding.paths.root, binding.home)
    result = binding.preflight()
    assert result.target is binding.target
    assert result.checkpoint.cursor == 3
    assert result.checkpoint.docs["note.md"].rev == 2
    assert result.namespace == binding.namespace
    assert result.private_fingerprint == hashlib.sha256(bindings_file().read_bytes()).hexdigest()
    assert result.enabled is True
    with pytest.raises(FrozenInstanceError):
        result.target.profile_id = OTHER_PROFILE
    assert before == tree_snapshot(binding.paths.root, binding.home)


@pytest.mark.parametrize("change", [
    "missing_store", "missing_row", "selected", "stale_revision", "profile", "url", "id",
    "incarnation", "root",
])
def test_private_consent_rejects_every_tuple_before_profile_read(binding, change):
    changes = {
        "selected": {"selection_state": "selected"},
        "stale_revision": {"identity_revision": 6}, "profile": {"profile_id": OTHER_PROFILE},
        "url": {"url": OTHER_URL}, "id": {"persistent_id": "another-id"},
        "incarnation": {"space_instance_id": OTHER_INSTANCE},
        "root": {"canonical_root": str(canonical_root(binding.paths.root / "other"))},
    }
    if change == "missing_store":
        bindings_file().unlink()
    elif change == "missing_row":
        binding.write_rows()
    else:
        binding.write_rows(binding.row(**changes[change]))
    before = tree_snapshot(binding.paths.root, binding.home)
    with pytest.raises(BindingError, match="binding_required"):
        binding.preflight()
    assert binding.profile_reads == 0
    assert before == tree_snapshot(binding.paths.root, binding.home)


@pytest.mark.parametrize("field,value,code", [
    ("canonical_root", "other", "activation_unavailable"),
    ("config_fingerprint", "0" * 64, "activation_unavailable"),
    ("url", OTHER_URL, "activation_unavailable"),
    ("persistent_id", "another-id", "activation_unavailable"),
    ("identity_revision", 6, "activation_unavailable"),
    ("profile_id", OTHER_PROFILE, "binding_required"),
    ("space_instance_id", OTHER_INSTANCE, "binding_required"),
])
def test_activation_evidence_tuple_is_not_inferred(binding, field, value, code):
    before = tree_snapshot(binding.paths.root, binding.home)
    with pytest.raises(BindingError, match=code):
        binding.preflight(replace(binding.target, **{field: value}))
    assert binding.profile_reads == 0
    assert before == tree_snapshot(binding.paths.root, binding.home)


@pytest.mark.parametrize("document", [
    b"sync: [", b"[]", b"sync: {}", b"sync: {enabled: true, url: https://runtime.invalid/team}",
    b"project_identity: {id: synthetic-runtime, revision: 7}\nsync: {enabled: true, enabled: false}",
    b"project_identity: {id: synthetic-runtime, revision: 7}\nsync: {enabled: 'true', url: https://runtime.invalid/team}",
    b"project_identity: {id: synthetic-runtime, revision: 7}\nsync: {enabled: true, url: https://runtime.invalid/team, space: ''}",
    b"project_identity: {id: synthetic-runtime, revision: 7}\nsync: {enabled: true, url: https://runtime.invalid/team/}",
])
def test_strict_disk_config_cannot_downgrade_or_coerce(binding, document):
    binding.paths.config_file.write_bytes(document)
    target = replace(binding.target, config_fingerprint=hashlib.sha256(document).hexdigest())
    before = tree_snapshot(binding.paths.root, binding.home)
    with pytest.raises(BindingError, match="activation_unavailable"):
        binding.preflight(target)
    assert binding.profile_reads == 0
    assert before == tree_snapshot(binding.paths.root, binding.home)


@pytest.mark.parametrize("mutation", ["missing_disk", "stale_bytes", "memory_identity", "memory_url",
                                       "memory_enabled", "home_drift"])
def test_disk_memory_and_home_observations_must_agree(binding, monkeypatch, mutation):
    if mutation == "missing_disk":
        binding.paths.config_file.unlink()
    elif mutation == "stale_bytes":
        with binding.paths.config_file.open("a", encoding="utf-8") as stream:
            stream.write("# changed checkout\n")
    elif mutation == "memory_identity":
        binding.config.project_identity = binding.config.project_identity.model_copy(update={"revision": 8})
    elif mutation == "memory_url":
        binding.config.sync.url = OTHER_URL
    elif mutation == "memory_enabled":
        binding.config.sync.enabled = False
    else:
        from voxarium_studio.core import paths
        monkeypatch.setattr(paths, "user_config_dir", lambda: binding.home / "other")
    before = tree_snapshot(binding.paths.root, binding.home)
    with pytest.raises(BindingError, match="activation_unavailable"):
        binding.preflight()
    assert binding.profile_reads == 0
    assert before == tree_snapshot(binding.paths.root, binding.home)


@pytest.mark.parametrize("document,code", [
    (b"{broken", "binding_store_unavailable"),
    (b'{"version":99,"roots":{}}', "binding_version_mismatch"),
    (b'{"version":true,"roots":{}}', "binding_version_mismatch"),
    (b'{"version":1,"version":1,"roots":{}}', "binding_store_unavailable"),
    (b'{"version":1,"roots":{},"secret":"never echo this"}', "binding_store_unavailable"),
])
def test_private_store_damage_is_not_fresh_consent(binding, document, code):
    bindings_file().write_bytes(document)
    before = tree_snapshot(binding.paths.root, binding.home)
    with pytest.raises(BindingError) as raised:
        binding.preflight()
    assert str(raised.value) == code
    assert binding.profile_reads == 0
    assert before == tree_snapshot(binding.paths.root, binding.home)


@pytest.mark.parametrize("mutation,code", [
    ("corrupt", "invalid_state"), ("unknown_version", "unsupported_version"),
    ("bool_version", "unsupported_version"), ("instance", "instance_mismatch"),
    ("namespace_root", "namespace_mismatch"), ("namespace_url", "namespace_mismatch"),
    ("namespace_id", "namespace_mismatch"), ("negative_cursor", "invalid_state"),
    ("base_above_cursor", "invalid_state"), ("directory", "state_unreadable"),
])
def test_state_damage_and_foreign_namespace_never_reset(binding, mutation, code):
    raw = binding.state_payload()
    if mutation == "corrupt":
        binding.namespace.path.write_bytes(b"invalid JSON")
    elif mutation == "directory":
        binding.namespace.path.unlink()
        binding.namespace.path.mkdir()
    else:
        if mutation in {"unknown_version", "bool_version"}:
            raw["version"] = 100 if mutation == "unknown_version" else True
        elif mutation == "instance":
            raw["space_instance_id"] = OTHER_INSTANCE
        elif mutation.startswith("namespace_"):
            key, value = {"namespace_root": ("canonical_root", str(binding.paths.root / "copy")),
                          "namespace_url": ("url", OTHER_URL),
                          "namespace_id": ("persistent_id", "different-id")}[mutation]
            raw["namespace"][key] = value
        elif mutation == "negative_cursor":
            raw["cursor"] = -1
        else:
            raw["docs"]["note.md"]["rev"] = 4
        binding.namespace.path.write_text(json.dumps(raw), encoding="utf-8")
    before = tree_snapshot(binding.paths.root, binding.home)
    with pytest.raises(SyncStateError) as raised:
        binding.preflight()
    assert str(raised.value) == code
    assert before == tree_snapshot(binding.paths.root, binding.home)


@pytest.mark.parametrize("mismatch", ["id", "url", "missing", "recovering"])
def test_profile_metadata_must_match_and_errors_are_sanitized(binding, mismatch):
    if mismatch in {"id", "url"}:
        binding.profile = binding.profile.model_copy(update={
            "profile_id" if mismatch == "id" else "url": OTHER_PROFILE if mismatch == "id" else OTHER_URL})
    else:
        def failed(_profile_id):
            raise ProfileError("profile_recovery_required" if mismatch == "recovering" else "profile_not_found")
        binding.read_profile = failed
    before = tree_snapshot(binding.paths.root, binding.home)
    with pytest.raises(BindingError) as raised:
        binding.preflight()
    assert str(raised.value) in {"binding_required", "activation_unavailable"}
    assert before == tree_snapshot(binding.paths.root, binding.home)


async def test_missing_state_is_only_readonly_preview_candidate(binding, no_external_io):
    binding.namespace.path.unlink()
    before = tree_snapshot(binding.paths.root, binding.home)
    result = binding.preflight()
    assert result.checkpoint is None
    sync, _, lifecycle = make_sync(binding, no_external_io)
    try:
        assert sync.status()["has_token"] is False  # Before start, too.
        await sync.start()
        assert sync.status()["message"] == "preview_required"
        assert sync.publications == []
        assert lifecycle.issued == []
    finally:
        await sync.stop()
    assert before == tree_snapshot(binding.paths.root, binding.home)


@pytest.mark.parametrize("failure,code", [
    ("missing", "binding_required"), ("selected", "binding_required"),
    ("revision", "binding_required"), ("profile", "binding_required"),
    ("url", "binding_required"), ("id", "binding_required"),
    ("incarnation", "binding_required"), ("corrupt_private", "binding_store_unavailable"),
    ("private_version", "binding_version_mismatch"), ("state_version", "unsupported_version"),
    ("state_instance", "instance_mismatch"), ("state_namespace", "namespace_mismatch"),
    ("state_corrupt", "invalid_state"),
])
async def test_runtime_status_cannot_bypass_rejected_preflight(binding, no_external_io, failure, code):
    row_changes = {
        "selected": {"selection_state": "selected"}, "revision": {"identity_revision": 6},
        "profile": {"profile_id": OTHER_PROFILE}, "url": {"url": OTHER_URL},
        "id": {"persistent_id": "other-id"}, "incarnation": {"space_instance_id": OTHER_INSTANCE},
    }
    if failure == "missing":
        bindings_file().unlink()
    elif failure in row_changes:
        binding.write_rows(binding.row(**row_changes[failure]))
    elif failure == "corrupt_private":
        bindings_file().write_bytes(b"{broken")
    elif failure == "private_version":
        bindings_file().write_text('{"version":100,"roots":{}}', encoding="utf-8")
    elif failure == "state_version":
        binding.write_state(version=100)
    elif failure == "state_instance":
        binding.write_state(space_instance_id=OTHER_INSTANCE)
    elif failure == "state_namespace":
        binding.write_state(namespace={**binding.namespace.as_dict(), "persistent_id": "other-id"})
    else:
        binding.namespace.path.write_bytes(b"invalid checkpoint")
    before = tree_snapshot(binding.paths.root, binding.home)
    sync, _, lifecycle = make_sync(binding, no_external_io)
    try:
        assert sync.status()["has_token"] is False
        await sync.start()
        assert sync.status()["state"] == "blocked"
        assert sync.status()["message"] == code
        assert sync.status()["has_token"] is False
        assert sync._runner is None and sync.publications == []
        assert lifecycle.issued == []
    finally:
        await sync.stop()
    assert before == tree_snapshot(binding.paths.root, binding.home)


async def test_production_seam_remains_r3_gated(binding, no_external_io):
    before = tree_snapshot(binding.paths.root, binding.home)
    sync, owner, lifecycle = make_sync(binding, no_external_io, cls=_ProfileRemoteSync)
    try:
        await sync.start()
        assert sync.status()["message"] == "protocol_unavailable"
        assert sync._runner is None
        assert lifecycle.live_leases == 0
        assert lifecycle.issued[0].releases == 1
        lease = owner.reserve(root=binding.paths.root, url=URL, persistent_id="synthetic-runtime")
        assert owner.release(lease)
    finally:
        await sync.stop()
    assert before == tree_snapshot(binding.paths.root, binding.home)


async def test_disabled_activated_config_does_not_admit_consumer(binding, no_external_io):
    binding.raw["sync"]["enabled"] = False
    binding.config.sync.enabled = False
    binding.write_config()
    sync, _, lifecycle = make_sync(binding, no_external_io)
    try:
        await sync.start()
        assert sync.status()["state"] == "disabled"
        assert sync.publications == []
        assert lifecycle.issued == []
    finally:
        await sync.stop()


async def test_initial_unreadable_activation_never_uses_legacy(binding, no_external_io):
    def unreadable():
        raise OSError("synthetic inaccessible evidence")

    sync, _, lifecycle = make_sync(binding, no_external_io, activation=unreadable)
    try:
        assert sync.status()["has_token"] is False
        await sync.start()
        assert sync.status()["message"] == "activation_unavailable"
        assert sync.publications == [] and lifecycle.issued == []
    finally:
        await sync.stop()


@pytest.mark.parametrize("closed", ["reservation", "lifecycle"])
async def test_closed_admission_cannot_publish_or_release_somebody_else(
        binding, no_external_io, closed):
    sync, owner, lifecycle = make_sync(binding, no_external_io)
    if closed == "reservation":
        owner.close_admission()
    else:
        lifecycle.close_admission()
    try:
        if closed == "lifecycle":
            with pytest.raises(ProfileError, match="consumer_lifecycle_unavailable"):
                await sync.start()
            assert lifecycle.issued == []
        else:
            await sync.start()
            assert sync.status()["message"] == "reservation_admission_closed"
            assert lifecycle.issued[0].releases == 1
        assert lifecycle.live_leases == 0
        assert sync._runner is None and sync.publications == []
    finally:
        await sync.stop()


async def test_metadata_alone_keeps_default_legacy_path(binding, no_external_io, monkeypatch):
    """Identity and even a private row are NOT the I6 completed journal evidence."""
    binding.config.sync.space = "legacy-selector"
    sync, _, lifecycle = make_sync(binding, no_external_io, activation=lambda: None)
    legacy_calls = []

    async def legacy_start(self):
        legacy_calls.append(self.paths.root)

    monkeypatch.setattr(RemoteSync, "_start_runner", legacy_start)
    # Status is allowed to consult the legacy provider; the provider is synthetic.
    sync._token = lambda url: None
    try:
        await sync.start()
        assert legacy_calls == [binding.paths.root]
        assert sync.publications == []
        assert len(lifecycle.issued) == 1
    finally:
        await sync.stop()


@pytest.mark.parametrize("evidence", ["missing", "raises"])
async def test_seen_activation_cannot_fall_back_after_evidence_disappears(
        binding, no_external_io, evidence):
    sync, _, _ = make_sync(binding, no_external_io)
    try:
        await sync.start()
        await wait_for(sync.started)
        if evidence == "missing":
            sync._activation = lambda: None
        else:
            def failed():
                raise OSError("unreadable synthetic journal")
            sync._activation = failed
        await sync.reconfigure()
        assert sync.status()["message"] == "activation_unavailable"
        assert len(sync.publications) == 1
        assert sync.status()["has_token"] is False
    finally:
        await sync.stop()


async def test_copied_config_and_checkpoint_are_not_root_consent(binding, tmp_path, no_external_io):
    copy_paths = ProjectPaths.for_project(tmp_path / "copied-project")
    copy_paths.data.mkdir(parents=True)
    copy_paths.config_file.write_bytes(binding.paths.config_file.read_bytes())
    # Even putting copied bases under the new hash must not bypass root consent.
    copy_namespace = SyncNamespace.for_project(copy_paths.root, url=URL, persistent_id="synthetic-runtime")
    copy_namespace.path.parent.mkdir(parents=True)
    copy_namespace.path.write_bytes(binding.namespace.path.read_bytes())
    target = replace(binding.target, canonical_root=str(canonical_root(copy_paths.root)))
    sync, _, lifecycle = make_sync(binding, no_external_io, paths=copy_paths, activation=lambda: target)
    before = tree_snapshot(tmp_path)
    try:
        await sync.start()
        assert sync.status()["message"] == "binding_required"
        assert binding.profile_reads == 0
        assert sync.publications == []
        assert lifecycle.issued == []
    finally:
        await sync.stop()
    assert before == tree_snapshot(tmp_path)


@pytest.mark.parametrize("alias", [False, True], ids=["separate-copy", "root-alias"])
async def test_one_consumer_per_destination_loser_releases_only_own_attach(
        binding, tmp_path, no_external_io, alias):
    first, owner, lifecycle = make_sync(binding, no_external_io)
    if alias:
        # Portable spelling alias requires no Windows symlink privilege.
        paths = ProjectPaths(root=binding.paths.root / ".." / binding.paths.root.name)
        target = binding.target
    else:
        paths = ProjectPaths.for_project(tmp_path / "project-copy")
        paths.data.mkdir(parents=True)
        paths.config_file.write_bytes(binding.paths.config_file.read_bytes())
        target = replace(binding.target, canonical_root=str(canonical_root(paths.root)))
        binding.write_rows(binding.row(), binding.row(canonical_root=target.canonical_root))
        namespace = SyncNamespace.for_project(paths.root, url=URL, persistent_id="synthetic-runtime")
        namespace.path.parent.mkdir(parents=True)
        raw = binding.state_payload(namespace=namespace.as_dict())
        namespace.path.write_text(json.dumps(raw), encoding="utf-8")
    loser, _, _ = make_sync(binding, no_external_io, owner=owner, lifecycle=lifecycle,
                            paths=paths, activation=lambda: target)
    try:
        await first.start()
        await wait_for(first.started)
        runner, reservation = first._runner, first._reservation_lease
        await first.start()  # Same actor is idempotent, not a second publication.
        assert first._runner is runner
        assert first._reservation_lease is reservation
        assert len(first.publications) == 1
        await loser.start()
        assert loser.status()["message"] == "duplicate_local_binding"
        assert loser.publications == []
        assert lifecycle.live_leases == 1
        assert [lease.releases for lease in lifecycle.issued] == [0, 1]
        await loser.stop()
        assert_owned(owner, binding, loser)
        assert not runner.done()
        assert first._reservation_lease is reservation
    finally:
        await loser.stop()
        await first.stop()
    assert lifecycle.live_leases == 0
    assert [lease.releases for lease in lifecycle.issued] == [1, 1]


async def test_stop_retains_both_leases_until_runner_join_even_on_repeated_cancel(
        binding, no_external_io):
    sync, owner, lifecycle = make_sync(binding, no_external_io)
    sync.allow_close.clear()
    await sync.start()
    await wait_for(sync.started)
    runner = sync._runner
    try:
        for _ in range(2):
            stopping = asyncio.create_task(sync.stop())
            await wait_for(sync.closing)
            # Let the second stop reach its shielded join, without time sleeps.
            await asyncio.sleep(0)
            assert not stopping.done()
            assert sync._runner is runner
            assert not runner.done()
            assert runner.cancelling() == 1
            assert lifecycle.live_leases == 1
            assert lifecycle.issued[0].releases == 0
            assert_owned(owner, binding, object())
            stopping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stopping
        assert not runner.done()
    finally:
        sync.allow_close.set()
        await sync.stop()
    assert sync._runner is None
    assert lifecycle.live_leases == 0
    assert lifecycle.issued[0].releases == 1
    lease = owner.reserve(root=binding.paths.root, url=URL, persistent_id="synthetic-runtime")
    assert owner.release(lease)
    await sync.stop()
    assert lifecycle.issued[0].releases == 1


@pytest.mark.parametrize("phase", ["start", "cleanup"])
async def test_failed_start_or_cleanup_retains_ownership_then_stop_and_retry(
        binding, no_external_io, phase):
    sync, owner, lifecycle = make_sync(binding, no_external_io)
    try:
        if phase == "start":
            sync.fail_publication = True
            with pytest.raises(RuntimeError, match="synthetic start failure"):
                await sync.start()
            sync.fail_publication = False
        else:
            await sync.start()
            await wait_for(sync.started)
            sync.cleanup_failures = 1
            with pytest.raises(RuntimeError, match="synthetic cleanup failure"):
                await sync.stop()
            assert sync._runner is not None  # reference survives unsuccessful cleanup
        assert lifecycle.live_leases == 1
        assert lifecycle.issued[0].releases == 0
        assert_owned(owner, binding, object())
        await sync.stop()
        assert lifecycle.live_leases == 0
        assert lifecycle.issued[0].releases == 1
        sync.started.clear()
        await sync.start()
        await wait_for(sync.started)
        assert len(lifecycle.issued) == 2
        assert lifecycle.live_leases == 1
    finally:
        await sync.stop()
    assert [lease.releases for lease in lifecycle.issued] == [1, 1]


@pytest.mark.parametrize("changed", ["fingerprint", "consent", "state", "config"])
async def test_recheck_after_attach_never_publishes_changed_observation(
        binding, no_external_io, changed):
    sync, owner, lifecycle = make_sync(binding, no_external_io)

    def mutate():
        if changed == "fingerprint":
            with bindings_file().open("a", encoding="utf-8") as stream:
                stream.write("\n")
        elif changed == "consent":
            binding.write_rows(binding.row(selection_state="selected"))
        elif changed == "state":
            binding.write_state(space_instance_id=OTHER_INSTANCE)
        else:
            with binding.paths.config_file.open("a", encoding="utf-8") as stream:
                stream.write("# concurrent checkout\n")

    lifecycle.after_attach = mutate
    try:
        await sync.start()
        assert sync.publications == []
        assert sync._runner is None
        assert sync.status()["state"] == "blocked"
        assert lifecycle.live_leases == 0
        assert lifecycle.issued[0].releases == 1
        lease = owner.reserve(root=binding.paths.root, url=URL, persistent_id="synthetic-runtime")
        assert owner.release(lease)
    finally:
        await sync.stop()


async def test_pure_profile_rename_does_not_reconnect_or_change_binding(binding, no_external_io):
    sync, _, lifecycle = make_sync(binding, no_external_io)
    # Rename during admission changes metadata revision, NOT endpoint/credential/consent.
    def rename():
        binding.profile = binding.profile.model_copy(update={"name": "Renamed", "revision": 2})
    lifecycle.after_attach = rename
    try:
        await sync.start()
        await wait_for(sync.started)
        captured, runner, reservation = sync.publications[0], sync._runner, sync._reservation_lease
        binding.profile = binding.profile.model_copy(update={"name": "Again", "revision": 3})
        assert binding.preflight() == captured
        await sync.start()
        await sync.reconfigure()
        assert sync._runner is runner
        assert sync._reservation_lease is reservation
        assert len(sync.publications) == 1
        assert len(lifecycle.issued) == 1
    finally:
        await sync.stop()


async def test_caught_startup_cancellation_can_join_in_the_same_task(binding, no_external_io):
    """A caught CancelledError leaves cancelling()>0, but is not a new cancel."""
    sync, owner, lifecycle = make_sync(binding, no_external_io)

    async def startup_then_cleanup():
        await sync.start()
        await wait_for(sync.started)
        current = asyncio.current_task()
        current.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            assert current.cancelling() == 1
            await sync.stop()

    await asyncio.create_task(startup_then_cleanup())
    assert sync._runner is None and sync._watcher is None
    assert sync._subscription is None and not sync._cleanup_failed
    assert lifecycle.live_leases == 0
    assert lifecycle.issued[0].releases == 1
    lease = owner.reserve(root=binding.paths.root, url=URL, persistent_id="synthetic-runtime")
    assert owner.release(lease)


async def test_watcher_cleanup_failure_retains_ownership_until_full_stop(
        binding, no_external_io, monkeypatch):
    sync, owner, lifecycle = make_sync(binding, no_external_io)
    await sync.start()
    await wait_for(sync.started)
    join = sync._cancel_join
    failed = False

    async def fail_watcher_once(task):
        nonlocal failed
        if task is sync._watcher and not failed:
            failed = True
            raise RuntimeError("synthetic watcher cleanup failure")
        await join(task)

    monkeypatch.setattr(sync, "_cancel_join", fail_watcher_once)
    try:
        with pytest.raises(RuntimeError, match="watcher cleanup failure"):
            await sync.stop()
        assert sync._runner is None and sync._watcher is not None
        assert sync._cleanup_failed and lifecycle.live_leases == 1
        assert_owned(owner, binding, object())
        for operation in (sync.start, sync.reconfigure):
            with pytest.raises(RuntimeError, match="sync_cleanup_required"):
                await operation()
        assert lifecycle.issued[0].releases == 0
    finally:
        await sync.stop()
    assert lifecycle.live_leases == 0 and sync._watcher is None
    assert lifecycle.issued[0].releases == 1


async def test_reservation_exception_releases_only_new_attach(binding, no_external_io, monkeypatch):
    sync, owner, lifecycle = make_sync(binding, no_external_io)

    def failed_reserve(**kwargs):
        raise RuntimeError("synthetic reservation failure")

    monkeypatch.setattr(owner, "reserve", failed_reserve)
    try:
        with pytest.raises(RuntimeError, match="reservation failure"):
            await sync.start()
        assert sync.publications == [] and sync._runner is None
        assert lifecycle.live_leases == 0
        assert lifecycle.issued[0].releases == 1
    finally:
        await sync.stop()


async def test_stopping_a_does_not_cancel_b_runner_or_fake_job(binding, no_external_io, tmp_path):
    """Two independent Ctx-owned task sets, not workspace selection side effects."""
    a, owner, lifecycle = make_sync(binding, no_external_io)
    jobs_b = JobEngine()
    started_b = asyncio.Event()
    end_b = asyncio.Event()

    async def job_b(_handle):
        started_b.set()
        await end_b.wait()

    handle = jobs_b.submit("b-job", "synthetic", job_b)
    # B has its OWN root/config/consent/checkpoint and another destination, but
    # the SAME application owners. No preflight or reservation mock is involved.
    paths_b = ProjectPaths.for_project(tmp_path / "project-b")
    paths_b.data.mkdir(parents=True)
    raw_b = {"project_identity": {"id": "project-b", "revision": 1},
             "sync": {"enabled": True, "url": URL}}
    target_b = replace(binding.target, canonical_root=str(canonical_root(paths_b.root)),
                       persistent_id="project-b", identity_revision=1)
    binding_b = Fixture(paths_b, binding.home, raw_b, AppConfig.model_validate(raw_b),
                        target_b, binding.profile)
    binding_b.write_config()
    binding_b.write_state()
    binding.write_rows(binding.row(), binding_b.row())
    b, _, _ = make_sync(binding_b, no_external_io, owner=owner, lifecycle=lifecycle)
    try:
        await a.start()
        await b.start()
        await wait_for(a.started)
        await wait_for(b.started)
        await wait_for(started_b)
        b_runner, b_lease = b._runner, b._reservation_lease
        await a.stop()
        assert jobs_b.is_running("b-job")
        assert not handle.task.done()
        assert b._runner is b_runner and not b_runner.done()
        assert b._reservation_lease is b_lease
        assert lifecycle.live_leases == 1
    finally:
        end_b.set()
        await handle.task
        await b.stop()
        await a.stop()
