"""Private AI-access identity store (ai-access-design §3, §6, §9; dev spec §16.12).

Pure local storage under the per-test ``$VOXARIUM_STUDIO_HOME``: no network, no server,
no real keys. Every write goes to the Studio home, never to the managed project.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from voxarium_studio.ai_access import identity as ident
from voxarium_studio.ai_access import protocol as p
from voxarium_studio.ai_access.identity import AckOutcome, AiAccessConflict, AiAccessError

SECRET = "device-secret-canary-" + "Z" * 32
URL = "https://remote.example.com/studio"
DIGEST = "d" * 64
EPOCH = "e" * 32


def uid() -> str:
    return str(uuid.uuid4())


def home_files(home: Path) -> set[str]:
    return {path.name for path in home.iterdir()}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "managed-project"
    root.mkdir()
    return root


def register(*, url: str = URL, server: str | None = None, user: str | None = None,
             enable: bool = False) -> ident.Registration:
    ident.ensure_client_id()
    registration = ident.record_registration(
        profile_id=uid(), url=url, server_instance_id=server or uid(), user_id=user or uid(),
        registration_id=uid(), display_name="Work PC",
    )
    if enable:
        registration = ident.set_publication_enabled(
            registration.registration_id, True, expected_revision=registration.revision)
    return registration


def make_export(root: Path, registration: ident.Registration, **overrides) -> ident.ExportRecord:
    data = {
        "root": root, "registration_id": registration.registration_id,
        "persistent_id": "demo-project", "storage_epoch": EPOCH, "identity_revision": 1,
        "display_name": "Demo", "policy_digest": DIGEST,
    }
    data.update(overrides)
    return ident.create_export(**data)


def publish_and_ack(export: ident.ExportRecord) -> ident.ExportRecord:
    frame = ident.publish_export(export.export_id, expected_revision=export.export_revision)
    ack = p.MirrorAck(export_id=frame.export_id, revision=frame.revision, hash=frame.hash,
                      state=p.MirrorState.EFFECTIVE)
    assert ident.apply_export_ack(ack) is AckOutcome.APPLIED
    current = ident.get_export(export.export_id)
    assert current is not None
    return current


# --------------------------------------------------------------------------- defaults


def test_defaults_are_off_and_reads_write_nothing(studio_home: Path) -> None:
    before = home_files(studio_home)
    assert ident.client_id() is None
    store = ident.load_registry()
    assert store.registrations == {} and store.exports == {} and store.grants == {}
    assert ident.list_registrations() == [] and ident.list_exports() == []
    assert ident.get_export(uid()) is None
    assert ident.device_secret_source(URL, uid(), uid(), uid()) == "missing"
    assert ident.load_device_secret(URL, uid(), uid(), uid()) is None
    assert home_files(studio_home) == before
    assert not ident.registry_file().exists() and not ident.secrets_file().exists()


def test_client_id_only_on_explicit_call_and_stable(studio_home: Path, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    assert ident.client_id() is None
    first = ident.ensure_client_id()
    assert uuid.UUID(first).version == 4 and str(uuid.UUID(first)) == first
    assert ident.ensure_client_id() == first
    assert ident.client_id() == first
    assert ident.load_registry().client_id == first
    other = tmp_path / "other-home"
    other.mkdir()
    monkeypatch.setenv("VOXARIUM_STUDIO_HOME", str(other))
    assert ident.client_id() is None
    assert ident.ensure_client_id() != first  # random per installation, never derived


def test_registration_requires_client_id() -> None:
    with pytest.raises(AiAccessError) as caught:
        ident.record_registration(
            profile_id=uid(), url=URL, server_instance_id=uid(), user_id=uid(),
            registration_id=uid(), display_name="PC")
    assert caught.value.code == "client_id_missing"


# --------------------------------------------------------------------------- registrations


def test_registration_defaults_and_exact_url() -> None:
    server, user = uid(), uid()
    registration = register(url="HTTPS://Remote.Example.com/studio/", server=server, user=user)
    assert registration.url == URL
    assert registration.publication_enabled is False and registration.revision == 1
    assert registration.client_id == ident.client_id() and registration.kind == "studio"
    assert ident.find_registration("remote.example.com/studio", server, user) == registration
    assert ident.find_registration("https://remote.example.com", server, user) is None
    assert ident.find_registration("https://remote.example.com/other", server, user) is None
    assert ident.find_registration(URL, uid(), user) is None


def test_invalid_url_error_is_fixed_and_free_of_input() -> None:
    ident.ensure_client_id()
    canary = "ftp://user:canary-pw@example.com"
    with pytest.raises(AiAccessError) as caught:
        ident.record_registration(profile_id=uid(), url=canary, server_instance_id=uid(),
                                  user_id=uid(), registration_id=uid(), display_name="PC")
    assert caught.value.code == "invalid_remote_url"
    assert "canary" not in str(caught.value) and caught.value.__cause__ is None


def test_server_instance_mismatch_is_refused_not_rebound() -> None:
    ident.ensure_client_id()
    profile, user = uid(), uid()
    ident.record_registration(profile_id=profile, url=URL, server_instance_id=uid(),
                              user_id=user, registration_id=uid(), display_name="PC")
    with pytest.raises(AiAccessError) as caught:
        ident.record_registration(profile_id=profile, url=URL, server_instance_id=uid(),
                                  user_id=user, registration_id=uid(), display_name="PC")
    assert caught.value.code == "server_instance_mismatch"
    assert len(ident.list_registrations()) == 1


def test_distinct_tuples_are_distinct_records_and_duplicates_conflict() -> None:
    first = register()
    second = register(server=first.server_instance_id)  # same URL+server, other user
    third = register(url="https://other.example.com", server=first.server_instance_id,
                     user=first.user_id)
    assert len({first.registration_id, second.registration_id, third.registration_id}) == 3
    with pytest.raises(AiAccessConflict) as caught:
        ident.record_registration(
            profile_id=uid(), url=URL, server_instance_id=first.server_instance_id,
            user_id=first.user_id, registration_id=uid(), display_name="PC")
    assert caught.value.code == "registration_exists"


def test_publication_switch_is_cas() -> None:
    registration = register()
    enabled = ident.set_publication_enabled(registration.registration_id, True,
                                            expected_revision=1)
    assert enabled.publication_enabled and enabled.revision == 2
    with pytest.raises(AiAccessConflict):
        ident.set_publication_enabled(registration.registration_id, False, expected_revision=1)
    assert ident.get_registration(registration.registration_id).publication_enabled


# --------------------------------------------------------------------------- malformed files


@pytest.mark.parametrize("content", [
    b"{not json",
    b'{"version":1,"version":1,"revision":0}',
    b'{"version":1,"revision":0,"unknown":true}',
    b'{"version":2,"revision":0}',
    b'{"version":true,"revision":0}',
    b'{"version":1,"revision":0.0}',
    b'{"revision":0}',
    b"\xef\xbb\xbf{\"version\":1,\"revision\":0}",
    b'{"version":1,"revision":0,"client_id":"NOT-A-UUID"}',
    b"[]",
    b"\xff\xfe",
])
def test_malformed_registry_is_never_overwritten(content: bytes) -> None:
    path = ident.registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    with pytest.raises(ident.AiAccessStoreError) as caught:
        ident.load_registry()
    assert caught.value.code == "ai_access_store_corrupt"
    for mutation in (ident.ensure_client_id,
                     lambda: ident.set_publication_enabled(uid(), True, expected_revision=1)):
        with pytest.raises(ident.AiAccessStoreError):
            mutation()
    assert path.read_bytes() == content


def test_malformed_secret_store_is_never_overwritten() -> None:
    registration = register()
    path = ident.secrets_file()
    content = b'{"version":1,"secrets":{},"extra":1}'
    path.write_bytes(content)
    with pytest.raises(ident.AiAccessStoreError):
        ident.save_device_secret(URL, registration.server_instance_id, registration.user_id,
                                 registration.registration_id, SECRET, expected_revision=0)
    assert path.read_bytes() == content
    assert ident.device_secret_source(
        URL, registration.server_instance_id, registration.user_id,
        registration.registration_id) == "missing"


def test_atomic_writes_leave_no_temp_files_and_restrict_the_temp_file(
    studio_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    restricted: list[tuple[Path, int]] = []
    original = ident._restrict_secret_file

    def spy(path: Path) -> None:
        restricted.append((path, path.stat().st_size))
        original(path)

    monkeypatch.setattr(ident, "_restrict_secret_file", spy)
    registration = register()
    ident.save_device_secret(URL, registration.server_instance_id, registration.user_id,
                             registration.registration_id, SECRET, expected_revision=0)
    assert restricted, "every write must restrict its temp file"
    for path, size in restricted:
        assert path.parent == studio_home and path.suffix == ".tmp" and size == 0
        assert path.name.startswith((".ai-access-", ".ai-access-secrets-"))
    assert home_files(studio_home) == {
        "ai-access.json", "ai-access.lock", "ai-access-secrets.json", "ai-access-secrets.lock",
    }


def test_replace_retries_transient_windows_sharing_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}
    real = os.replace

    def flaky(source: str, target: str) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real(source, target)

    monkeypatch.setattr(ident.os, "replace", flaky)
    monkeypatch.setattr(ident, "_REPLACE_DELAY_SECONDS", 0)
    assert ident.ensure_client_id()
    assert calls["n"] == 3


# --------------------------------------------------------------------------- device secrets


def test_device_secret_separate_exact_and_never_exposed(studio_home: Path) -> None:
    registration = register()
    key = (URL, registration.server_instance_id, registration.user_id,
           registration.registration_id)
    assert ident.registration_view(registration).has_device_token is False
    assert ident.save_device_secret(*key, SECRET, expected_revision=0) == 1
    assert ident.load_device_secret(*key) == SECRET
    assert ident.load_device_secret("REMOTE.example.com/studio/", *key[1:]) == SECRET
    # Every element of the 4-tuple must match exactly.
    assert ident.load_device_secret("https://remote.example.com", *key[1:]) is None
    assert ident.load_device_secret(URL, uid(), *key[2:]) is None
    assert ident.load_device_secret(URL, key[1], uid(), key[3]) is None
    assert ident.load_device_secret(URL, key[1], key[2], uid()) is None

    view = ident.registration_view(ident.get_registration(registration.registration_id))
    assert view.has_device_token is True
    assert SECRET not in repr(view) and SECRET not in repr(registration)
    assert SECRET not in repr(ident._load_secrets())
    assert SECRET not in ident.registry_file().read_text(encoding="utf-8")
    assert SECRET in ident.secrets_file().read_text(encoding="utf-8")
    assert not (studio_home / "remote-keys.json").exists()

    with pytest.raises(AiAccessConflict) as caught:
        ident.save_device_secret(*key, SECRET + "x", expected_revision=0)
    assert SECRET not in str(caught.value)
    with pytest.raises(AiAccessError) as caught:
        ident.save_device_secret(*key, SECRET + " with space", expected_revision=1)
    assert caught.value.code == "invalid_device_secret" and SECRET not in str(caught.value)
    with pytest.raises(AiAccessConflict):  # a live secret never moves to another URL
        ident.save_device_secret("https://other.example.com", *key[1:], SECRET,
                                 expected_revision=1)


def test_device_secret_cas_and_tombstone() -> None:
    registration = register()
    key = (URL, registration.server_instance_id, registration.user_id,
           registration.registration_id)
    assert ident.device_secret_revision(*key) == 0
    ident.save_device_secret(*key, SECRET, expected_revision=0)
    assert ident.save_device_secret(*key, None, expected_revision=1) == 2
    assert ident.load_device_secret(*key) is None
    assert ident.device_secret_revision(*key) == 2
    assert ident.device_secret_source(*key) == "missing"
    with pytest.raises(AiAccessConflict):  # ABA: a stale writer cannot resurrect it
        ident.save_device_secret(*key, SECRET, expected_revision=0)
    assert ident.save_device_secret(*key, SECRET, expected_revision=2) == 3


def test_remove_registration_deletes_secret_and_fences_exports(project: Path) -> None:
    registration = register(enable=True)
    key = (URL, registration.server_instance_id, registration.user_id,
           registration.registration_id)
    ident.save_device_secret(*key, SECRET, expected_revision=0)
    export = publish_and_ack(make_export(project, registration))
    with pytest.raises(AiAccessConflict):
        ident.remove_registration(registration.registration_id, expected_revision=1)
    ident.remove_registration(registration.registration_id, expected_revision=2)
    assert ident.get_registration(registration.registration_id) is None
    assert ident.load_device_secret(*key) is None
    assert SECRET not in ident.secrets_file().read_text(encoding="utf-8")
    fenced = ident.get_export(export.export_id)
    assert fenced.enabled is False and fenced.export_revision == export.export_revision + 1
    assert not ident.export_published(ident.load_registry(), fenced)


# --------------------------------------------------------------------------- exports


def test_export_defaults_disabled_and_target(project: Path) -> None:
    registration = register()
    export = make_export(project, registration)
    assert export.enabled is False and export.export_revision == 1
    assert export.mirror is p.MirrorState.UNPUBLISHED and export.acked_revision == 0
    assert export.root == str(Path(os.path.normcase(str(project.resolve()))))
    target = ident.export_target(export)
    assert target == p.Target(
        server_instance_id=registration.server_instance_id,
        executor_registration_id=registration.registration_id,
        export_id=export.export_id, persistent_id="demo-project", storage_epoch=EPOCH,
        export_revision=1,
    )
    assert not ident.export_published(ident.load_registry(), export)


def test_export_requires_existing_root_and_valid_identity(tmp_path: Path) -> None:
    registration = register()
    with pytest.raises(AiAccessError) as caught:
        make_export(tmp_path / "missing", registration)
    assert caught.value.code == "invalid_root"
    root = tmp_path / "p"
    root.mkdir()
    for bad in ({"persistent_id": "Bad ID"}, {"storage_epoch": "x"},
                {"identity_revision": 0}, {"policy_digest": "nope"}):
        with pytest.raises(AiAccessError):
            make_export(root, registration, **bad)


def test_duplicate_export_is_refused(tmp_path: Path) -> None:
    registration = register()
    first_root, second_root = tmp_path / "one", tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    make_export(first_root, registration)
    with pytest.raises(AiAccessError) as caught:
        make_export(second_root, registration)  # same registration + persistent_id
    assert caught.value.code == "duplicate_export"
    with pytest.raises(AiAccessError) as caught:
        make_export(first_root, registration, persistent_id="other")  # same root
    assert caught.value.code == "duplicate_export"
    other = register(server=uid())
    assert make_export(second_root, other).persistent_id == "demo-project"


def test_publish_needs_publication_switch_and_matching_ack(project: Path) -> None:
    registration = register()
    export = make_export(project, registration)
    with pytest.raises(AiAccessError) as caught:
        ident.publish_export(export.export_id, expected_revision=1)
    assert caught.value.code == "publication_disabled"
    ident.set_publication_enabled(registration.registration_id, True, expected_revision=1)
    with pytest.raises(AiAccessConflict):
        ident.publish_export(export.export_id, expected_revision=2)
    frame = ident.publish_export(export.export_id, expected_revision=1)
    assert (frame.revision, frame.expected_revision) == (1, 0)
    assert frame.hash == p.record_hash(frame)
    assert frame.display_name == "Demo" and frame.policy_digest == DIGEST
    assert ident.publish_frame(ident.get_export(export.export_id)) == frame
    pending = ident.get_export(export.export_id)
    assert pending.enabled and pending.mirror is p.MirrorState.PENDING
    assert not ident.export_published(ident.load_registry(), pending)

    wrong = p.MirrorAck(export_id=frame.export_id, revision=1, hash="0" * 64,
                        state=p.MirrorState.EFFECTIVE)
    assert ident.apply_export_ack(wrong) is AckOutcome.CONFLICT
    assert not ident.export_published(ident.load_registry(), ident.get_export(export.export_id))
    ahead = p.MirrorAck(export_id=frame.export_id, revision=2, hash=frame.hash,
                        state=p.MirrorState.EFFECTIVE)
    assert ident.apply_export_ack(ahead) is AckOutcome.CONFLICT
    wrong_state = p.MirrorAck(export_id=frame.export_id, revision=1, hash=frame.hash,
                              state=p.MirrorState.UNPUBLISHED)
    assert ident.apply_export_ack(wrong_state) is AckOutcome.CONFLICT

    good = p.MirrorAck(export_id=frame.export_id, revision=1, hash=frame.hash,
                       state=p.MirrorState.EFFECTIVE)
    assert ident.apply_export_ack(good) is AckOutcome.APPLIED
    assert ident.export_published(ident.load_registry(), ident.get_export(export.export_id))
    assert ident.apply_export_ack(good) is AckOutcome.DUPLICATE
    # Re-publishing an unchanged export returns the identical frame and stays effective.
    assert ident.publish_export(export.export_id, expected_revision=1) == frame
    assert ident.export_published(ident.load_registry(), ident.get_export(export.export_id))


def test_rebind_or_epoch_change_disables_and_bumps(project: Path, tmp_path: Path) -> None:
    registration = register(enable=True)
    export = publish_and_ack(make_export(project, registration))
    assert ident.update_export(export.export_id, expected_revision=1,
                               storage_epoch=EPOCH) == export  # no change, no fence
    changed = ident.update_export(export.export_id, expected_revision=1, storage_epoch="f" * 32)
    assert changed.enabled is False and changed.export_revision == 2
    assert not ident.export_published(ident.load_registry(), changed)
    assert ident.export_target(changed) != ident.export_target(export)
    with pytest.raises(AiAccessConflict):
        ident.update_export(export.export_id, expected_revision=1, display_name="Again")
    # A second change before any publish keeps the revision: no gap for Remote's CAS.
    other_root = tmp_path / "moved"
    other_root.mkdir()
    moved = ident.update_export(export.export_id, expected_revision=2, root=other_root)
    assert moved.export_revision == 2 and moved.enabled is False
    # A replayed ACK of the old revision neither re-enables nor rewrites anything.
    old = p.MirrorAck(export_id=export.export_id, revision=1, hash=export.mirror_hash,
                      state=p.MirrorState.EFFECTIVE)
    before = ident.registry_file().read_bytes()
    assert ident.apply_export_ack(old) is AckOutcome.DUPLICATE
    assert ident.registry_file().read_bytes() == before
    assert not ident.export_published(ident.load_registry(), ident.get_export(export.export_id))
    frame = ident.publish_export(export.export_id, expected_revision=2)
    assert (frame.revision, frame.expected_revision) == (2, 1)
    before = ident.registry_file().read_bytes()
    assert ident.apply_export_ack(old) is AckOutcome.STALE  # lower than the sent revision
    assert ident.registry_file().read_bytes() == before
    ack = p.MirrorAck(export_id=export.export_id, revision=2, hash=frame.hash,
                      state=p.MirrorState.EFFECTIVE)
    assert ident.apply_export_ack(ack) is AckOutcome.APPLIED
    assert ident.export_published(ident.load_registry(), ident.get_export(export.export_id))


def test_unpublish_denies_locally_first_and_republish_needs_new_revision(
    project: Path,
) -> None:
    registration = register(enable=True)
    export = publish_and_ack(make_export(project, registration))
    frame = ident.unpublish_export(export.export_id)
    assert frame is not None and (frame.revision, frame.expected_revision) == (2, 1)
    assert frame.hash == p.record_hash(frame)
    current = ident.get_export(export.export_id)
    assert current.enabled is False and current.mirror is p.MirrorState.PENDING
    assert not ident.export_published(ident.load_registry(), current)
    assert ident.unpublish_export(export.export_id) == frame  # idempotent, identical
    ack = p.MirrorAck(export_id=export.export_id, revision=2, hash=frame.hash,
                      state=p.MirrorState.UNPUBLISHED)
    assert ident.apply_export_ack(ack) is AckOutcome.APPLIED
    again = ident.publish_export(export.export_id, expected_revision=2)
    assert (again.revision, again.expected_revision) == (3, 2)


def test_unpublish_of_never_sent_export_has_no_frame(project: Path) -> None:
    registration = register()
    export = make_export(project, registration)
    assert ident.unpublish_export(export.export_id) is None


def test_deleted_export_id_is_never_reused(project: Path, tmp_path: Path) -> None:
    registration = register(enable=True)
    export = publish_and_ack(make_export(project, registration))
    frame = ident.delete_export(export.export_id)
    assert frame is not None and frame.revision == 2
    deleted = ident.get_export(export.export_id)
    assert deleted.deleted and not deleted.enabled
    assert ident.list_exports() == [] and ident.list_exports(include_deleted=True) == [deleted]
    with pytest.raises(AiAccessError) as caught:
        make_export(project, registration, export_id=export.export_id)
    assert caught.value.code == "export_id_retired"
    with pytest.raises(AiAccessError) as caught:
        ident.publish_export(export.export_id, expected_revision=2)
    assert caught.value.code == "export_deleted"
    # The root and persistent ID are free again under a new export id.
    fresh = make_export(project, registration)
    assert fresh.export_id != export.export_id


def test_export_capacity(tmp_path: Path) -> None:
    registration = register()
    for index in range(p.MAX_EXPORTS_PER_EXECUTOR):
        root = tmp_path / f"p{index}"
        root.mkdir()
        make_export(root, registration, persistent_id=f"project-{index}")
    root = tmp_path / "overflow"
    root.mkdir()
    with pytest.raises(AiAccessError) as caught:
        make_export(root, registration, persistent_id="overflow")
    assert caught.value.code == "capacity_exceeded"


def test_nothing_is_written_into_the_managed_project(project: Path) -> None:
    (project / ".voxarium_studio").mkdir()
    (project / ".voxarium_studio" / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    snapshot = sorted(str(path.relative_to(project)) for path in project.rglob("*"))
    registration = register(enable=True)
    ident.save_device_secret(URL, registration.server_instance_id, registration.user_id,
                             registration.registration_id, SECRET, expected_revision=0)
    export = publish_and_ack(make_export(project, registration))
    ident.update_export(export.export_id, expected_revision=1, display_name="Renamed")
    ident.delete_export(export.export_id)
    assert sorted(str(path.relative_to(project)) for path in project.rglob("*")) == snapshot
    assert (project / ".voxarium_studio" / "config.yaml").read_text(encoding="utf-8") == "x: 1\n"
