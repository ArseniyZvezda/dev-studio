"""Private root consent fixtures (studio-design §4.3/§8, S4 / VS-159).

Only synthetic task-local homes and paths are used. There is no AppContext,
server, real credential lookup, catalog, runtime reservation or network. CAS
tests cover threads and independent processes, not crash/power-loss durability.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Barrier
import time

import pytest
from pydantic import ValidationError

from voxarium_studio.core import application_config, paths
from voxarium_studio.core.application_config import RemoteServerProfile
from voxarium_studio.core.project_identity import ProjectIdentity
from voxarium_studio.core.project_scope import canonical_root
from voxarium_studio.sync import bindings as module
from voxarium_studio.sync.bindings import (
    BindingError, BindingRecord, BindingSnapshot, RemoteBindings, bindings_file, root_key,
)


PROFILE = "c503da2b-f8fe-4fb0-b945-86bc119495b3"
OTHER_PROFILE = "81ccda2b-f8fe-4fb0-b945-86bc119495b3"
URL = "https://bindings.invalid/team"
EPOCH = "0123456789abcdef0123456789abcdef"
OTHER_EPOCH = "fedcba9876543210fedcba9876543210"
FIELDS = {"canonical_root", "profile_id", "url", "persistent_id",
          "identity_revision", "selection_state", "space_instance_id"}


@pytest.fixture(autouse=True)
def private_fixture_io(studio_home, monkeypatch):
    """Redirect every config entry point, and count even swallowed external I/O."""
    from voxarium_studio.sync import credentials, profiles

    calls = {"network": 0, "credentials": 0, "profiles": 0}

    def denied(kind):
        def fail(*_args, **_kwargs):
            calls[kind] += 1
            raise AssertionError("Binding fixture attempted forbidden " + kind)
        return fail

    monkeypatch.setattr(paths, "user_config_dir", lambda: studio_home)
    monkeypatch.setattr(application_config, "user_config_dir", lambda: studio_home)
    monkeypatch.setattr(module, "user_config_dir", lambda: studio_home)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_URL", raising=False)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
                 "getnameinfo", "socket", "socketpair", "create_connection", "create_server"):
        if hasattr(socket, name):
            monkeypatch.setattr(socket, name, denied("network"))
    for name in ("keys_file", "load_token", "save_token", "load_profile_token",
                 "save_profile_token", "import_legacy_token"):
        monkeypatch.setattr(credentials, name, denied("credentials"))
    for name in ("get", "list", "load_token"):
        monkeypatch.setattr(profiles.RemoteProfiles, name, denied("profiles"))
    yield
    assert calls == {"network": 0, "credentials": 0, "profiles": 0}


@pytest.fixture
def root(tmp_path):
    # A managed project need not be opened, initialized, or even exist to hash it.
    return tmp_path / "synthetic-project"


def profile(**changes):
    return RemoteServerProfile(**{
        "profile_id": PROFILE, "name": "Synthetic profile", "url": URL, **changes,
    })


def identity(**changes):
    return ProjectIdentity(**{"id": "synthetic-project", "revision": 7, **changes})


def record(root, **changes):
    options = {"profile": profile(), "identity": identity(),
               "selection_state": "confirmed", "space_instance_id": EPOCH, **changes}
    return BindingRecord.for_root(root, **options)


def require(snapshot, root, **changes):
    return snapshot.require_confirmed(root, **{
        "profile": profile(), "identity": identity(), "url": URL,
        "space_instance_id": EPOCH, **changes,
    })


def error(code, call):
    with pytest.raises(BindingError) as caught:
        call()
    assert isinstance(caught.value, ValueError)
    assert caught.value.code == code
    assert str(caught.value) == code
    return caught.value


def document(root, **changes):
    data = record(root).model_dump()
    data.update(changes)
    return {"version": 1, "roots": {root_key(root): data}}


def write_raw(raw):
    target = bindings_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = raw if isinstance(raw, bytes) else json.dumps(raw).encode("utf-8")
    target.write_bytes(content)
    return content


def saved(root):
    store = RemoteBindings()
    snapshot = store.save(record(root), expected_fingerprint=None)
    return store, snapshot, bindings_file().read_bytes()


def test_private_location_root_hash_and_no_runtime_identity(root, studio_home):
    assert bindings_file() == studio_home / "remote-bindings.json"
    canonical = str(canonical_root(root))
    assert root_key(root) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert root_key(str(root)) == root_key(root)
    assert set(BindingRecord.model_fields) == FIELDS
    value = record(root)
    assert value.canonical_root == canonical
    assert value.persistent_id == identity().id
    assert value.identity_revision == identity().revision
    assert "project_id" not in value.model_dump()  # runtime handles never persist
    assert list(studio_home.iterdir()) == []


def test_root_hash_uses_exact_utf8_of_existing_canonicalizer(tmp_path):
    root = tmp_path / "Синтетика-Cafe\u0301"
    assert root_key(root) == hashlib.sha256(str(canonical_root(root)).encode("utf-8")).hexdigest()
    assert not root.exists()


@pytest.mark.parametrize("state,epoch", [("selected", None), ("selected", EPOCH),
                                        ("confirmed", EPOCH)])
def test_selected_and_confirmed_exact_roundtrip(root, state, epoch):
    value = record(root, selection_state=state, space_instance_id=epoch)
    store = RemoteBindings()
    snapshot = store.save(value, expected_fingerprint=None)
    content = bindings_file().read_bytes()
    assert isinstance(snapshot, BindingSnapshot)
    assert snapshot.fingerprint == hashlib.sha256(content).hexdigest()
    assert snapshot.records == (value,)
    assert isinstance(snapshot.records, tuple)
    assert snapshot.get(root) == value
    assert RemoteBindings().read() == snapshot
    assert json.loads(content) == {"version": 1, "roots": {
        root_key(root): value.model_dump(),
    }}
    if state == "confirmed":
        assert require(snapshot, root) == value
    else:
        error("binding_required", lambda: require(snapshot, root))


def test_selected_epoch_defaults_to_none_and_dtos_are_immutable(root):
    value = BindingRecord.for_root(root, profile=profile(), identity=identity(),
                                   selection_state="selected")
    assert value.space_instance_id is None
    snapshot = RemoteBindings().save(value, expected_fingerprint=None)
    for obj, name, replacement in ((value, "selection_state", "confirmed"),
                                    (snapshot, "fingerprint", None),
                                    (snapshot, "records", ())):
        with pytest.raises((ValidationError, FrozenInstanceError, AttributeError, TypeError)):
            setattr(obj, name, replacement)


@pytest.mark.parametrize("extra", ["token", "token_hash", "authorization", "credentials",
                                   "runtime_project_id", "binding_generation"])
def test_record_forbids_unknown_fields_and_secrets(root, extra):
    with pytest.raises(ValueError):
        BindingRecord.model_validate({**record(root).model_dump(), extra: "synthetic-only"})


@pytest.mark.parametrize("field,value", [
    ("canonical_root", "relative-root"), ("canonical_root", ""),
    ("canonical_root", None), ("canonical_root", 1),
    ("profile_id", "not-a-profile"), ("profile_id", PROFILE.upper()),
    ("profile_id", PROFILE.replace("-", "")), ("profile_id", " " + PROFILE),
    ("profile_id", None), ("profile_id", 1),
    ("url", "wss://BINDINGS.invalid/team/"), ("url", "bindings.invalid/team"),
    ("url", URL + "/"), ("url", "ftp://bindings.invalid/team"),
    ("url", "https://user:synthetic@bindings.invalid/team"),
    ("url", URL + "?synthetic=1"), ("url", URL + "#synthetic"),
    ("url", None), ("url", 1),
    ("persistent_id", "Synthetic-project"), ("persistent_id", " synthetic-project"),
    ("persistent_id", "../project"), ("persistent_id", "a/b"),
    ("persistent_id", ""), ("persistent_id", "x" * 65),
    ("persistent_id", "проект"), ("persistent_id", None), ("persistent_id", 1),
    ("identity_revision", 0), ("identity_revision", -1),
    ("identity_revision", True), ("identity_revision", False),
    ("identity_revision", 7.0), ("identity_revision", "7"),
    ("identity_revision", None),
    ("selection_state", "enabled"), ("selection_state", "Confirmed"),
    ("selection_state", True), ("selection_state", None),
    ("space_instance_id", None), ("space_instance_id", ""),
    ("space_instance_id", EPOCH.upper()), ("space_instance_id", EPOCH[:-1]),
    ("space_instance_id", EPOCH + "0"), ("space_instance_id", "g" * 32),
    ("space_instance_id", "01234567-89ab-cdef-0123-456789abcdef"),
    ("space_instance_id", 1), ("space_instance_id", True),
])
def test_record_schema_is_strict_and_canonical(root, field, value):
    with pytest.raises(ValueError):
        BindingRecord.model_validate({**record(root).model_dump(), field: value})


@pytest.mark.parametrize("epoch", ["", EPOCH.upper(), "x" * 32, 1, False])
def test_selected_does_not_allow_malformed_optional_epoch(root, epoch):
    with pytest.raises(ValueError):
        BindingRecord.model_validate({**record(root).model_dump(),
                                      "selection_state": "selected", "space_instance_id": epoch})


def test_factory_normalizes_only_existing_root_and_url_rules(root):
    alias = root / "nested" / ".."
    raw_profile = profile().model_copy(update={"url": "wss://BINDINGS.invalid/team/"})
    value = record(alias, profile=raw_profile)
    assert value.canonical_root == str(canonical_root(root))
    assert value.url == URL
    assert root_key(alias) == root_key(root)
    assert not root.exists()


@pytest.mark.parametrize("case", ["root", "profile", "profile-id", "profile-url", "profile-revision",
                                  "identity", "identity-id", "identity-revision", "state", "epoch"])
def test_factory_revalidates_untrusted_objects(root, case):
    args = {"profile": profile(), "identity": identity(), "selection_state": "confirmed",
            "space_instance_id": EPOCH}
    if case == "root":
        root = None
    elif case == "profile":
        args["profile"] = {"profile_id": PROFILE, "url": URL}
    elif case == "profile-id":
        args["profile"] = profile().model_copy(update={"profile_id": "not-a-uuid"})
    elif case == "profile-url":
        args["profile"] = profile().model_copy(update={"url": "https://user:synthetic@host"})
    elif case == "profile-revision":
        args["profile"] = profile().model_copy(update={"revision": True})
    elif case == "identity":
        args["identity"] = {"id": "synthetic-project", "revision": 7}
    elif case == "identity-id":
        args["identity"] = identity().model_copy(update={"id": "WRONG"})
    elif case == "identity-revision":
        args["identity"] = identity().model_copy(update={"revision": True})
    elif case == "state":
        args["selection_state"] = "enabled"
    else:
        args["space_instance_id"] = None
    error("invalid_binding", lambda: BindingRecord.for_root(root, **args))


@pytest.mark.parametrize("case", ["root", "profile-id", "profile-url", "url", "http", "prefix",
                                  "persistent-id", "identity-revision", "epoch"])
def test_confirmation_requires_every_exact_tuple_component(root, tmp_path, case):
    store, snapshot, before = saved(root)
    expected = {}
    target = root
    if case == "root":
        target = tmp_path / "independent-copy"
    elif case == "profile-id":
        expected["profile"] = profile(profile_id=OTHER_PROFILE)
    elif case == "profile-url":
        expected["profile"] = profile(url=URL + "/other")
    elif case == "url":
        expected["url"] = "https://other-bindings.invalid/team"
    elif case == "http":
        expected["url"] = "http://bindings.invalid/team"
    elif case == "prefix":
        expected["url"] = URL + "/other"
    elif case == "persistent-id":
        expected["identity"] = identity(id="another-id")
    elif case == "identity-revision":
        expected["identity"] = identity(revision=8)
    else:
        expected["space_instance_id"] = OTHER_EPOCH
    error("binding_required", lambda: require(snapshot, target, **expected))
    assert bindings_file().read_bytes() == before
    assert store.read() == snapshot


def test_profile_label_and_profile_revision_are_not_project_identity(root):
    _, snapshot, _ = saved(root)
    renamed = profile(name="Another local label", revision=2)
    assert require(snapshot, root, profile=renamed) == snapshot.get(root)


@pytest.mark.parametrize("label", ["", "  historical  ", "legacy\nlabel"])
def test_stored_profile_labels_do_not_participate_in_consent(root, label):
    # The metadata owner intentionally preserves legacy labels literally on read.
    stored = RemoteServerProfile.model_validate(
        {**profile().model_dump(), "name": label},
        context={"stored_profile_metadata": True},
    )
    store, snapshot, before = saved(root)
    assert record(root, profile=stored) == snapshot.get(root)
    assert require(snapshot, root, profile=stored) == snapshot.get(root)
    assert stored.name == label
    assert store.read() == snapshot
    assert bindings_file().read_bytes() == before


def test_expected_url_uses_existing_normalization_not_new_endpoint_equivalences(root):
    _, snapshot, _ = saved(root)
    assert require(snapshot, root, url="wss://BINDINGS.invalid/team/") == snapshot.get(root)
    assert require(snapshot, root, url="bindings.invalid/team") == snapshot.get(root)
    error("binding_required", lambda: require(snapshot, root, url="https://bindings.invalid:443/team"))


@pytest.mark.parametrize("case", ["root", "profile", "profile-id", "profile-url", "profile-revision",
                                  "identity", "identity-id", "identity-revision", "url", "epoch"])
def test_malformed_expected_values_fail_closed_even_without_a_record(root, case):
    snapshot = RemoteBindings().read()
    expected = {}
    if case == "root":
        root = None
    elif case == "profile":
        expected["profile"] = None
    elif case == "profile-id":
        expected["profile"] = profile().model_copy(update={"profile_id": "invalid"})
    elif case == "profile-url":
        expected["profile"] = profile().model_copy(update={"url": "ftp://synthetic.invalid"})
    elif case == "profile-revision":
        expected["profile"] = profile().model_copy(update={"revision": True})
    elif case == "identity":
        expected["identity"] = None
    elif case == "identity-id":
        expected["identity"] = identity().model_copy(update={"id": "INVALID"})
    elif case == "identity-revision":
        expected["identity"] = identity().model_copy(update={"revision": True})
    elif case == "url":
        expected["url"] = "https://user:synthetic@bindings.invalid/team"
    else:
        expected["space_instance_id"] = "invalid"
    error("invalid_binding", lambda: require(snapshot, root, **expected))
    assert not bindings_file().exists()


def test_root_aliases_reuse_consent_but_copies_and_moves_never_inherit(root, tmp_path):
    root.mkdir()
    (root / "nested").mkdir()
    store, snapshot, before = saved(root)
    alias = root / "nested" / ".."
    assert snapshot.get(alias) == snapshot.get(root)
    assert require(snapshot, alias) == snapshot.get(root)
    if os.name == "nt":
        assert require(snapshot, str(root).swapcase()) == snapshot.get(root)
        assert root_key(str(root).swapcase()) == root_key(root)
    copy = tmp_path / "copied-project"
    copy.mkdir()
    assert snapshot.get(copy) is None
    error("binding_required", lambda: require(snapshot, copy))
    moved = tmp_path / "moved-project"
    root.rename(moved)
    assert snapshot.get(moved) is None
    error("binding_required", lambda: require(snapshot, moved))
    assert store.read() == snapshot  # no guessed root migration or repair
    assert bindings_file().read_bytes() == before


def test_missing_read_is_empty_without_mkdir_write_lock_or_recovery(root, tmp_path, monkeypatch):
    home = tmp_path / "not-created-home"
    monkeypatch.setattr(module, "user_config_dir", lambda: home)
    monkeypatch.setattr(application_config, "user_config_dir", lambda: home)

    def forbidden(*_args, **_kwargs):
        pytest.fail("A read attempted mkdir, publication, or the application writer lock")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "replace", forbidden)
    monkeypatch.setattr(application_config, "application_config_transaction", forbidden)
    monkeypatch.setattr(module, "application_config_transaction", forbidden)
    snapshot = RemoteBindings().read()
    assert snapshot.fingerprint is None
    assert snapshot.records == ()
    assert snapshot.get(root) is None
    error("binding_required", lambda: require(snapshot, root))
    assert not home.exists()


@pytest.mark.parametrize("content", ["valid", "corrupt", "future"])
def test_existing_read_never_locks_rewrites_or_repairs(root, studio_home, monkeypatch, content):
    if content == "valid":
        before = write_raw(document(root))
    elif content == "future":
        before = write_raw({"version": 2, "roots": {}})
    else:
        before = write_raw(b"{synthetic-corrupt")
    members = set(studio_home.iterdir())

    def forbidden(*_args, **_kwargs):
        pytest.fail("Read attempted mutation or a writer/recovery lock")

    with monkeypatch.context() as patch:
        for name in ("mkdir", "write_bytes", "write_text", "replace", "rename", "unlink"):
            patch.setattr(Path, name, forbidden)
        patch.setattr(application_config, "application_config_transaction", forbidden)
        patch.setattr(module, "application_config_transaction", forbidden)
        if content == "valid":
            snapshot = RemoteBindings().read()
            assert snapshot.get(root) == record(root)
            assert snapshot.fingerprint == hashlib.sha256(before).hexdigest()
        else:
            code = "binding_version_mismatch" if content == "future" else "binding_store_unavailable"
            error(code, RemoteBindings().read)
    assert bindings_file().read_bytes() == before
    assert set(studio_home.iterdir()) == members


def test_explicit_empty_document_has_a_revision_but_grants_no_consent(root):
    before = write_raw({"version": 1, "roots": {}})
    store = RemoteBindings()
    snapshot = store.read()
    assert snapshot.records == ()
    assert snapshot.fingerprint == hashlib.sha256(before).hexdigest()
    error("binding_required", lambda: require(snapshot, root))
    error("binding_revision_conflict", lambda: store.save(record(root), expected_fingerprint=None))
    assert bindings_file().read_bytes() == before
    assert store.save(record(root), expected_fingerprint=snapshot.fingerprint).get(root) == record(root)


@pytest.mark.parametrize("version", [0, 2, -1, 999, True, 1.0, "1", None])
def test_unsupported_or_noninteger_schema_version_is_not_empty_success(root, version):
    before = write_raw({"version": version, "roots": {}})
    error("binding_version_mismatch", RemoteBindings().read)
    error("binding_version_mismatch", lambda: RemoteBindings().save(
        record(root), expected_fingerprint=hashlib.sha256(before).hexdigest()))
    assert bindings_file().read_bytes() == before


@pytest.mark.parametrize("raw", [
    b"", b" ", b"null", b"[]", b"true", b"42", b"{", b"\xff",
    {}, {"version": 1}, {"roots": {}}, {"version": 1, "roots": []},
    {"version": 1, "roots": None}, {"version": 1, "roots": {}, "token": "synthetic"},
])
def test_malformed_document_never_falls_back_to_empty_or_overwrites(root, raw):
    before = write_raw(raw)
    error("binding_store_unavailable", RemoteBindings().read)
    error("binding_store_unavailable", lambda: RemoteBindings().save(
        record(root), expected_fingerprint=hashlib.sha256(before).hexdigest()))
    assert bindings_file().read_bytes() == before


@pytest.mark.parametrize("field,value", [
    ("canonical_root", "relative"), ("canonical_root", None),
    ("profile_id", "invalid"), ("profile_id", PROFILE.upper()),
    ("profile_id", PROFILE.replace("-", "")), ("profile_id", 42),
    ("url", "wss://BINDINGS.invalid/team/"), ("url", URL + "/"),
    ("url", "https://user:synthetic@bindings.invalid"), ("url", None),
    ("persistent_id", "INVALID"), ("persistent_id", "x" * 65),
    ("persistent_id", "../project"), ("persistent_id", None),
    ("identity_revision", True), ("identity_revision", 7.0),
    ("identity_revision", "7"), ("identity_revision", 0),
    ("selection_state", "enabled"), ("selection_state", None),
    ("space_instance_id", None), ("space_instance_id", EPOCH.upper()),
    ("space_instance_id", "bad"), ("space_instance_id", True),
    ("token", "synthetic-not-a-key"),
])
def test_every_stored_record_is_validated_without_coercion(root, field, value):
    before = write_raw(document(root, **{field: value}))
    error("binding_store_unavailable", RemoteBindings().read)
    assert bindings_file().read_bytes() == before


@pytest.mark.parametrize("case", ["wrong-hash", "uppercase-hash", "short-hash", "foreign-root",
                                  "extra-root", "null-record", "missing-field"])
def test_root_hash_is_verified_against_every_canonical_root(root, tmp_path, case):
    raw = document(root)
    key = root_key(root)
    if case == "wrong-hash":
        raw["roots"]["0" * 64] = raw["roots"].pop(key)
    elif case == "uppercase-hash":
        raw["roots"][key.upper()] = raw["roots"].pop(key)
    elif case == "short-hash":
        raw["roots"][key[:32]] = raw["roots"].pop(key)
    elif case == "foreign-root":
        raw["roots"][key]["canonical_root"] = str(canonical_root(tmp_path / "other"))
    elif case == "extra-root":
        raw["roots"][root_key(tmp_path / "other")] = {"bad": "record"}
    elif case == "null-record":
        raw["roots"][key] = None
    else:
        del raw["roots"][key]["identity_revision"]
    before = write_raw(raw)
    error("binding_store_unavailable", RemoteBindings().read)
    error("binding_store_unavailable", lambda: RemoteBindings().save(
        record(root), expected_fingerprint=hashlib.sha256(before).hexdigest()))
    assert bindings_file().read_bytes() == before


@pytest.mark.parametrize("location", ["top", "roots", "record"])
def test_duplicate_json_fields_are_rejected_not_last_wins(root, location):
    key = json.dumps(root_key(root))
    value = json.dumps(record(root).model_dump())
    if location == "top":
        content = '{"version":1,"version":1,"roots":{}}'
    elif location == "roots":
        content = '{"version":1,"roots":{' + key + ':' + value + ',' + key + ':' + value + '}}'
    else:
        value = value[:-1] + ',"identity_revision":7}'
        content = '{"version":1,"roots":{' + key + ':' + value + '}}'
    before = write_raw(content.encode("utf-8"))
    error("binding_store_unavailable", RemoteBindings().read)
    assert bindings_file().read_bytes() == before


def test_whole_file_bytes_are_the_cas_revision_and_other_roots_are_preserved(root, tmp_path):
    store, first, before = saved(root)
    second_root = tmp_path / "second-project"
    second = record(second_root, identity=identity(id="second-project"),
                    selection_state="selected", space_instance_id=None)
    current = store.save(second, expected_fingerprint=first.fingerprint)
    assert current.get(root) == first.get(root)
    assert current.get(second_root) == second
    assert len(current.records) == 2
    # A formatting-only external edit is still a change to the accepted evidence.
    content = bindings_file().read_bytes()
    bindings_file().write_bytes(content + b"\n ")
    reformatted = store.read()
    assert reformatted.records == current.records
    assert reformatted.fingerprint != current.fingerprint
    error("binding_revision_conflict", lambda: store.save(
        record(root, identity=identity(revision=8)), expected_fingerprint=current.fingerprint))
    assert bindings_file().read_bytes() == content + b"\n "
    updated = store.save(record(root, identity=identity(revision=8)),
                         expected_fingerprint=reformatted.fingerprint)
    assert updated.get(root).identity_revision == 8
    assert updated.get(second_root) == second
    assert before != bindings_file().read_bytes()


def test_missing_none_fingerprint_is_not_wildcard_and_deleted_store_conflicts(root):
    store = RemoteBindings()
    initial = store.read()
    first = store.save(record(root), expected_fingerprint=initial.fingerprint)
    before = bindings_file().read_bytes()
    error("binding_revision_conflict", lambda: store.save(record(root), expected_fingerprint=None))
    assert bindings_file().read_bytes() == before
    bindings_file().unlink()
    error("binding_revision_conflict", lambda: store.save(
        record(root), expected_fingerprint=first.fingerprint))
    assert not bindings_file().exists()


@pytest.mark.parametrize("invalid", [None, {}, "binding", 1])
def test_save_requires_a_validated_record_before_any_write(root, invalid):
    _, _, before = saved(root)
    error("invalid_binding", lambda: RemoteBindings().save(
        invalid, expected_fingerprint=hashlib.sha256(before).hexdigest()))
    assert bindings_file().read_bytes() == before


@pytest.mark.parametrize("fingerprint", [True, 0, b"0" * 64, "", "0" * 63,
                                        "0" * 65, "A" * 64, "g" * 64])
def test_invalid_cas_fingerprint_is_not_wildcard_or_an_implicit_revision(root, fingerprint):
    _, _, before = saved(root)
    error("invalid_binding", lambda: RemoteBindings().save(
        record(root), expected_fingerprint=fingerprint))
    assert bindings_file().read_bytes() == before


@pytest.mark.parametrize("field,value", [("identity_revision", True), ("profile_id", "invalid"),
                                         ("canonical_root", "relative"), ("url", URL + "/"),
                                         ("selection_state", "enabled"), ("space_instance_id", None)])
def test_save_revalidates_model_copy_bypass(root, field, value):
    _, snapshot, before = saved(root)
    invalid = record(root).model_copy(update={field: value})
    error("invalid_binding", lambda: RemoteBindings().save(
        invalid, expected_fingerprint=snapshot.fingerprint))
    assert bindings_file().read_bytes() == before


@pytest.mark.parametrize("same_root", [False, True], ids=["different-roots", "same-root"])
@pytest.mark.parametrize("existing", [False, True], ids=["missing-store", "existing-store"])
def test_threads_cas_has_one_winner_and_explicit_retry_preserves_roots(root, tmp_path, same_root,
                                                                   existing):
    target = root if same_root else tmp_path / "second-project"
    values = (record(root), record(target, identity=identity(id="second-project")))
    anchor = record(tmp_path / "unrelated-project") if existing else None
    if anchor is not None:
        RemoteBindings().save(anchor, expected_fingerprint=None)
    initial = RemoteBindings().read()
    barrier = Barrier(2)

    def attempt(value):
        store = RemoteBindings()
        observed = store.read()
        assert observed == initial
        barrier.wait(timeout=10)
        try:
            return store.save(value, expected_fingerprint=observed.fingerprint)
        except BindingError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, values))
    assert sum(isinstance(item, BindingSnapshot) for item in results) == 1
    assert results.count("binding_revision_conflict") == 1
    loser = results.index("binding_revision_conflict")
    current = RemoteBindings().read()
    assert len(current.records) == 1 + int(existing)
    retried = RemoteBindings().save(values[loser], expected_fingerprint=current.fingerprint)
    assert retried.get(target if loser else root) == values[loser]
    if anchor is not None:
        assert current.get(anchor.canonical_root) == anchor
        assert retried.get(anchor.canonical_root) == anchor
    if not same_root:
        assert set(retried.records) == set(values) | ({anchor} if anchor is not None else set())


# Independent interpreters share only the fixture's stable application lock.
_PROCESS_CAS = r'''
import json, os, socket, sys, time
from pathlib import Path
from voxarium_studio.core.application_config import RemoteServerProfile
from voxarium_studio.core.project_identity import ProjectIdentity
from voxarium_studio.sync import credentials
from voxarium_studio.sync.bindings import BindingError, BindingRecord, RemoteBindings
def denied(*args, **kwargs):
    raise AssertionError("External I/O forbidden in synthetic binding process")
for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "getnameinfo",
             "socket", "socketpair", "create_connection", "create_server"):
    if hasattr(socket, name):
        setattr(socket, name, denied)
for name in ("keys_file", "load_token", "save_token", "load_profile_token",
             "save_profile_token", "import_legacy_token"):
    setattr(credentials, name, denied)
root, project_id, ready, go, fingerprint = sys.argv[1:]
record = BindingRecord.for_root(root,
    profile=RemoteServerProfile(profile_id="c503da2b-f8fe-4fb0-b945-86bc119495b3",
                                name="Synthetic", url="https://bindings.invalid/team"),
    identity=ProjectIdentity(id=project_id, revision=7), selection_state="confirmed",
    space_instance_id="0123456789abcdef0123456789abcdef")
store = RemoteBindings()
snapshot = store.read()
assert snapshot.fingerprint == (fingerprint or None)
Path(ready).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 25
while not Path(go).exists():
    assert time.monotonic() < deadline, "Fixture process rendezvous timeout"
    time.sleep(0.01)
try:
    store.save(record, expected_fingerprint=snapshot.fingerprint)
    result = "saved"
except BindingError as exc:
    result = exc.code
print(json.dumps(result))
'''


@pytest.mark.parametrize("same_root", [False, True], ids=["different-roots", "same-root"])
@pytest.mark.parametrize("existing", [False, True], ids=["missing-store", "existing-store"])
def test_independent_processes_obey_same_file_cas(root, tmp_path, studio_home, same_root, existing):
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("VOXARIUM_STUDIO_REMOTE_"):
            del env[name]
    env.update(VOXARIUM_STUDIO_HOME=str(studio_home), PYTHONDONTWRITEBYTECODE="1",
               PYTHONNOUSERSITE="1", PYDANTIC_DISABLE_PLUGINS="__all__")
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                 "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        directory = tmp_path / ("child-" + name)
        directory.mkdir()
        env[name] = str(directory)
    target = root if same_root else tmp_path / "second-project"
    values = (record(root), record(target, identity=identity(id="second-project")))
    anchor = record(tmp_path / "unrelated-project") if existing else None
    if anchor is not None:
        RemoteBindings().save(anchor, expected_fingerprint=None)
    fingerprint = RemoteBindings().read().fingerprint or ""
    go = tmp_path / "publish-now"
    children = []
    try:
        for index, value in enumerate(values):
            children.append(subprocess.Popen(
                [sys.executable, "-c", _PROCESS_CAS, value.canonical_root,
                 value.persistent_id, str(tmp_path / f"ready-{index}"), str(go), fingerprint],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 25
        while not all((tmp_path / f"ready-{index}").exists() for index in range(2)):
            assert all(child.poll() is None for child in children), "Fixture child exited before CAS"
            assert time.monotonic() < deadline, "Fixture parent rendezvous timeout"
            time.sleep(0.01)
        go.write_text("go", encoding="utf-8")
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr
            results.append(json.loads(stdout))
        assert sorted(results) == ["binding_revision_conflict", "saved"]
        loser = results.index("binding_revision_conflict")
        current = RemoteBindings().read()
        assert len(current.records) == 1 + int(existing)
        updated = RemoteBindings().save(values[loser], expected_fingerprint=current.fingerprint)
        assert updated.get(values[loser].canonical_root) == values[loser]
        if anchor is not None:
            assert current.get(anchor.canonical_root) == anchor
            assert updated.get(anchor.canonical_root) == anchor
        if not same_root:
            assert set(updated.records) == set(values) | ({anchor} if anchor is not None else set())
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_save_rereads_cas_and_publishes_inside_application_transaction(root, monkeypatch):
    store, snapshot, _ = saved(root)
    original_transaction = module.application_config_transaction
    original_read = Path.read_bytes
    original_replace = Path.replace
    active = False
    observations = {"lock": 0, "read": 0, "replace": 0}

    @contextmanager
    def transaction():
        nonlocal active
        with original_transaction():
            assert not active
            active = True
            observations["lock"] += 1
            try:
                yield
            finally:
                active = False

    def read_bytes(path):
        if path == bindings_file():
            assert active, "CAS read escaped the cross-process metadata transaction"
            observations["read"] += 1
        return original_read(path)

    def replace(path, target):
        if Path(target) == bindings_file():
            assert active, "Publication escaped the cross-process metadata transaction"
            observations["replace"] += 1
        return original_replace(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(module, "application_config_transaction", transaction)
        patch.setattr(Path, "read_bytes", read_bytes)
        patch.setattr(Path, "replace", replace)
        updated = store.save(record(root, identity=identity(revision=8)),
                             expected_fingerprint=snapshot.fingerprint)
    assert updated.get(root).identity_revision == 8
    assert observations["lock"] == 1
    assert observations["read"] >= 1
    assert observations["replace"] == 1


@pytest.mark.parametrize("fault", [PermissionError, OSError])
def test_stat_or_read_io_errors_are_not_empty_missing_success(root, monkeypatch, fault):
    _, _, before = saved(root)
    target = bindings_file()
    originals = {name: getattr(Path, name) for name in ("stat", "open", "read_bytes")}

    def fail_at_target(name):
        def call(path, *args, **kwargs):
            if path == target:
                raise fault("Synthetic binding read fault")
            return originals[name](path, *args, **kwargs)
        return call

    with monkeypatch.context() as patch:
        for name in originals:
            patch.setattr(Path, name, fail_at_target(name))
        error("binding_store_unavailable", RemoteBindings().read)
        error("binding_store_unavailable", lambda: RemoteBindings().save(
            record(root), expected_fingerprint=hashlib.sha256(before).hexdigest()))
    assert target.read_bytes() == before


def test_a_directory_at_the_store_path_is_not_missing(root):
    bindings_file().mkdir()
    error("binding_store_unavailable", RemoteBindings().read)
    error("binding_store_unavailable", lambda: RemoteBindings().save(
        record(root), expected_fingerprint=None))
    assert bindings_file().is_dir()


@pytest.mark.parametrize("lstat_error", [None, PermissionError])
def test_missing_read_with_existing_or_unstatable_entry_is_not_empty(root, monkeypatch, lstat_error):
    store, snapshot, before = saved(root)
    target = bindings_file()
    original_read = Path.read_bytes
    original_lstat = Path.lstat

    def vanished_read(path):
        if path == target:
            # Includes a dangling file link, without requiring symlink privileges.
            raise FileNotFoundError("Synthetic missing link target")
        return original_read(path)

    def uncertain_entry(path):
        if path == target and lstat_error is not None:
            raise lstat_error("Synthetic unavailable entry")
        return original_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", vanished_read)
        patch.setattr(Path, "lstat", uncertain_entry)
        error("binding_store_unavailable", store.read)
        error("binding_store_unavailable", lambda: store.save(
            record(root, identity=identity(revision=8)), expected_fingerprint=snapshot.fingerprint))
    assert target.read_bytes() == before


def test_short_write_without_exception_cannot_publish_a_partial_document(root, monkeypatch):
    store, snapshot, before = saved(root)
    original_tempfile = module.tempfile.NamedTemporaryFile

    @contextmanager
    def short_file(*args, **kwargs):
        with original_tempfile(*args, **kwargs) as stream:
            class Stream:
                def __getattr__(self, name):
                    return getattr(stream, name)

                def write(self, content):
                    return stream.write(content[:len(content) // 2])
            yield Stream()

    with monkeypatch.context() as patch:
        patch.setattr(module.tempfile, "NamedTemporaryFile", short_file)
        error("binding_store_unavailable", lambda: store.save(
            record(root, identity=identity(revision=8)), expected_fingerprint=snapshot.fingerprint))
    assert bindings_file().read_bytes() == before
    assert store.read() == snapshot
    assert not list(bindings_file().parent.glob("*.tmp"))


@pytest.mark.parametrize("boundary", ["lock", "mkdir", "tempfile", "serialize", "write", "fsync", "replace"])
def test_publication_fault_preserves_exact_old_bytes_and_other_roots(root, tmp_path, monkeypatch,
                                                                  boundary):
    store, first, _ = saved(root)
    other = tmp_path / "other-project"
    snapshot = store.save(record(other), expected_fingerprint=first.fingerprint)
    target = bindings_file()
    before = target.read_bytes()
    original_tempfile = module.tempfile.NamedTemporaryFile

    def fail(*_args, **_kwargs):
        raise OSError("Synthetic publication fault")

    with monkeypatch.context() as patch:
        if boundary == "lock":
            patch.setattr(module, "application_config_transaction", fail)
        elif boundary == "mkdir":
            patch.setattr(Path, "mkdir", fail)
        elif boundary == "tempfile":
            patch.setattr(module.tempfile, "NamedTemporaryFile", fail)
        elif boundary == "serialize":
            patch.setattr(module.json, "dumps", fail)
        elif boundary == "write":
            @contextmanager
            def failing_file(*args, **kwargs):
                with original_tempfile(*args, **kwargs) as stream:
                    class Stream:
                        def __getattr__(self, name):
                            return getattr(stream, name)

                        def write(self, content):
                            stream.write(content[:max(1, len(content) // 2)])
                            stream.flush()
                            raise OSError("Synthetic partial staging write")
                    yield Stream()
            patch.setattr(module.tempfile, "NamedTemporaryFile", failing_file)
        elif boundary == "fsync":
            patch.setattr(module.os, "fsync", fail)
        else:
            patch.setattr(Path, "replace", fail)
        error("binding_store_unavailable", lambda: store.save(
            record(root, identity=identity(revision=8)), expected_fingerprint=snapshot.fingerprint))
    assert target.read_bytes() == before
    assert store.read() == snapshot
    assert store.read().get(other) == record(other)
    assert not list(target.parent.glob("*.tmp")), "Failed staging must not leave a second store"


@pytest.mark.parametrize("boundary", ["tempfile", "fsync", "replace"])
def test_failed_first_publication_does_not_create_an_empty_binding_store(root, monkeypatch, boundary):
    def fail(*_args, **_kwargs):
        raise OSError("Synthetic first-write fault")

    with monkeypatch.context() as patch:
        if boundary == "tempfile":
            patch.setattr(module.tempfile, "NamedTemporaryFile", fail)
        elif boundary == "fsync":
            patch.setattr(module.os, "fsync", fail)
        else:
            patch.setattr(Path, "replace", fail)
        error("binding_store_unavailable", lambda: RemoteBindings().save(
            record(root), expected_fingerprint=None))
    assert not bindings_file().exists()
    assert RemoteBindings().read().records == ()

# I6: public removal and the existing-store transaction seam (§3.2).
def test_i6_remove_is_exact_cas_and_preserves_unrelated_root(root, tmp_path):
    store, first, _ = saved(root)
    other = tmp_path / "unopened-other"
    current = store.save(record(other), expected_fingerprint=first.fingerprint)
    before = bindings_file().read_bytes()
    error("binding_revision_conflict", lambda: store.remove(root, expected_fingerprint=first.fingerprint))
    assert bindings_file().read_bytes() == before
    removed = store.remove(root, expected_fingerprint=current.fingerprint)
    assert removed.get(root) is None and removed.get(other) == record(other)
    assert removed == store.read()
    assert removed.fingerprint == hashlib.sha256(bindings_file().read_bytes()).hexdigest()
    assert store.remove(root, expected_fingerprint=removed.fingerprint) == removed
    assert store.remove(other, expected_fingerprint=removed.fingerprint).records == ()
    assert bindings_file().is_file(), "Revocation must not manufacture store loss"


def test_i6_remove_missing_does_not_create_store_or_ignore_cas(root):
    store = RemoteBindings()
    error("binding_revision_conflict", lambda: store.remove(root, expected_fingerprint="a" * 64))
    assert store.remove(root, expected_fingerprint=None) == BindingSnapshot(None)
    assert not bindings_file().exists()


@pytest.mark.parametrize("fingerprint", ["", "*", 1, True, "g" * 64, "A" * 64, "a" * 63])
def test_i6_remove_rejects_invalid_cas_even_for_missing_root(root, fingerprint):
    error("invalid_binding", lambda: RemoteBindings().remove(root, expected_fingerprint=fingerprint))
    assert not bindings_file().exists()


@pytest.mark.parametrize("root", [None, 1, "", "bad\0root"])
def test_i6_remove_validates_root_without_a_store(root):
    error("invalid_binding", lambda: RemoteBindings().remove(root, expected_fingerprint=None))


def test_i6_transaction_seam_counts_real_os_acquisition(root, monkeypatch):
    # The Python RLock is insufficient: count the OS lock call itself.
    calls = []
    if os.name == "nt":
        import msvcrt
        locking = msvcrt.locking

        def counted(fd, mode, count):
            if mode == msvcrt.LK_LOCK:
                calls.append(mode)
            return locking(fd, mode, count)
        monkeypatch.setattr(msvcrt, "locking", counted)
    else:
        import fcntl
        flock = fcntl.flock

        def counted(fd, mode):
            if mode == fcntl.LOCK_EX:
                calls.append(mode)
            return flock(fd, mode)
        monkeypatch.setattr(fcntl, "flock", counted)
    store = RemoteBindings()
    first = store.save(record(root), expected_fingerprint=None)
    assert len(calls) == 1
    absent = store.remove(root, expected_fingerprint=first.fingerprint)
    assert len(calls) == 2

    def nested_lock_forbidden():
        pytest.fail("Internal publication reacquired the OS transaction")
    monkeypatch.setattr(module, "application_config_transaction", nested_lock_forbidden)
    with application_config.application_config_transaction():
        selected = store._save_in_transaction(record(root, selection_state="selected"),
                                              expected_fingerprint=absent.fingerprint)
        with pytest.raises(BindingError, match="binding_revision_conflict"):
            store._remove_in_transaction(root, expected_fingerprint=first.fingerprint)
        removed = store._remove_in_transaction(root, expected_fingerprint=selected.fingerprint)
    assert removed.records == () and len(calls) == 3


@pytest.mark.parametrize("boundary", ["lock", "tempfile", "fsync", "replace"])
def test_i6_remove_publication_fault_keeps_consent_bytes(root, monkeypatch, boundary):
    store, snapshot, before = saved(root)

    def fail(*_args, **_kwargs):
        raise OSError("private-fixture-canary")
    with monkeypatch.context() as patch:
        if boundary == "lock":
            patch.setattr(module, "application_config_transaction", fail)
        elif boundary == "tempfile":
            patch.setattr(module.tempfile, "NamedTemporaryFile", fail)
        elif boundary == "fsync":
            patch.setattr(module.os, "fsync", fail)
        else:
            patch.setattr(Path, "replace", fail)
        caught = error("binding_store_unavailable", lambda: store.remove(
            root, expected_fingerprint=snapshot.fingerprint))
        assert caught.__context__ is None
    assert bindings_file().read_bytes() == before and store.read() == snapshot


def test_i6_remove_corrupt_store_never_repairs_to_empty(root):
    before = write_raw(b'{"version":1,"roots":broken-private-fixture-canary}')
    error("binding_store_unavailable", lambda: RemoteBindings().remove(
        root, expected_fingerprint=hashlib.sha256(before).hexdigest()))
    assert bindings_file().read_bytes() == before


def test_i6_remove_racing_save_has_one_winner(root):
    store, current, _ = saved(root)
    barrier = Barrier(2)

    def mutate(remove):
        barrier.wait(timeout=10)
        try:
            if remove:
                return store.remove(root, expected_fingerprint=current.fingerprint)
            return store.save(record(root, identity=identity(revision=8)),
                              expected_fingerprint=current.fingerprint)
        except BindingError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(mutate, [True, False]))
    assert sum(isinstance(result, BindingSnapshot) for result in results) == 1
    assert results.count("binding_revision_conflict") == 1
