"""Fixture-only v2 bases (remote-identity/studio-design §4.3/§6/§8).

No live service, credentials, managed project or real application home is used.
The unwired v2 format must not change or implicitly import the live v1 state.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

from voxarium_studio.core.paths import user_config_dir
from voxarium_studio.core.project_scope import canonical_root
from voxarium_studio.sync import state as state_module
from voxarium_studio.sync.protocol import PROTOCOL_VERSION, content_hash
from voxarium_studio.sync.state import Base, SyncNamespace, SyncState, SyncStateError, SyncStateV2


URL = "https://state.invalid/deployment/team"
ID = "alpha"
EPOCH = "0123456789abcdef0123456789abcdef"
OTHER_EPOCH = "fedcba9876543210fedcba9876543210"
DIGEST = content_hash("fixture document\n")


@pytest.fixture(autouse=True)
def fixture_only(studio_home, tmp_path, monkeypatch):
    """Conftest isolates VOXARIUM_STUDIO_HOME; additionally forbid networking."""
    assert user_config_dir().resolve().is_relative_to(tmp_path.resolve())
    assert user_config_dir() == studio_home

    def forbidden(*args, **kwargs):
        pytest.fail("State fixtures must not open a network connection or resolve DNS")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "managed-project"
    root.mkdir()
    return root


def fresh(root, *, url=URL, persistent_id=ID, space_instance_id=EPOCH):
    return SyncStateV2.fresh(root, url=url, persistent_id=persistent_id,
                             space_instance_id=space_instance_id)


def load(root, *, url=URL, persistent_id=ID):
    return SyncStateV2.load(root, url=url, persistent_id=persistent_id)


def populated(root):
    return replace(fresh(root), cursor=7,
                   docs={"folder/live.md": Base(5, DIGEST), "deleted.txt": Base(7, None)})


def payload(root):
    return {
        "version": 2,
        "namespace": {"canonical_root": str(canonical_root(root)),
                      "url": URL, "persistent_id": ID},
        "space_instance_id": EPOCH,
        "cursor": 7,
        "docs": {"folder/live.md": {"rev": 5, "hash": DIGEST},
                 "deleted.txt": {"rev": 7, "hash": None}},
    }


def write_fixture(root, raw):
    path = fresh(root).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def assert_error(code, call):
    with pytest.raises(SyncStateError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def test_roundtrip_exact_schema_and_tombstone(root):
    state = populated(root)
    assert state.save() is None
    assert json.loads(state.path.read_text(encoding="utf-8")) == payload(root)
    loaded = load(root)
    assert loaded == state
    assert loaded.path == state.namespace.path == state.path
    assert loaded.docs["deleted.txt"] == Base(7, None)
    # A loaded state can advance, but never mutates its predecessor implicitly.
    advanced = replace(loaded, cursor=8, docs={**loaded.docs, "new.markdown": Base(8, DIGEST)})
    advanced.save()
    assert load(root) == advanced
    assert loaded.cursor == 7 and "new.markdown" not in loaded.docs


def test_fresh_is_frozen_empty_independent_and_read_only(root, studio_home):
    first, second = fresh(root), fresh(root)
    assert first.cursor == 0 and first.docs == {}
    assert first.docs is not second.docs
    assert first.space_instance_id == EPOCH
    with pytest.raises(FrozenInstanceError):
        first.cursor = 1
    with pytest.raises(FrozenInstanceError):
        first.namespace.url = "https://other.invalid"
    assert load(root) is None
    assert not (studio_home / "sync").exists()
    assert list(root.iterdir()) == []


def test_namespace_uses_complete_compact_json_tuple_and_sha256(root, studio_home):
    namespace = SyncNamespace.for_project(root, url=URL, persistent_id=ID)
    serialized = json.dumps([str(canonical_root(root)), URL, ID], ensure_ascii=False,
                            separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    assert namespace.canonical_root == str(canonical_root(root))
    assert namespace.url == URL and namespace.persistent_id == ID
    assert namespace.path == studio_home / "sync" / "v2" / digest / "state.json"
    assert not namespace.path.parent.exists()


def test_unicode_roots_and_document_paths_roundtrip_without_alias_loss(tmp_path, studio_home):
    root = tmp_path / "проект-日本語"
    root.mkdir()
    state = replace(fresh(root), cursor=1, docs={"заметки/日本語.md": Base(1, DIGEST)})
    serialized = json.dumps([str(canonical_root(root)), URL, ID], ensure_ascii=False,
                            separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    assert state.path == studio_home / "sync" / "v2" / digest / "state.json"
    state.save()
    assert load(root) == state


def test_root_dot_relative_and_windows_case_aliases_share_namespace(root, monkeypatch):
    state = populated(root)
    state.save()
    aliases = [root / ".", root / "child" / "..", str(root)]
    monkeypatch.chdir(root.parent)
    aliases.append(Path(root.name))
    if os.name == "nt":
        aliases.extend([str(root).upper(), str(root).lower()])
    for alias in aliases:
        assert fresh(alias).namespace == state.namespace
        assert load(alias) == state


def test_symlink_or_windows_junction_alias_shares_namespace(root, tmp_path):
    alias = tmp_path / "project-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.fail("Alias acceptance requires symlink support on this platform")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(root)],
                                capture_output=True, check=False)
        assert result.returncode == 0, "Alias acceptance requires a symlink or Windows junction"
    try:
        state = populated(root)
        state.save()
        assert fresh(alias).namespace == state.namespace
        assert load(alias) == state
    finally:
        # Remove only the alias, never recursively traverse a junction's target.
        if alias.is_symlink():
            alias.unlink()
        else:
            alias.rmdir()


@pytest.mark.parametrize("alias", [
    "state.invalid/deployment/team", "https://STATE.INVALID/deployment/team/",
    "wss://state.invalid/deployment/team/", "  https://state.invalid/deployment/team///  ",
])
def test_normalise_url_aliases_share_state(root, alias):
    state = populated(root)
    state.save()
    assert fresh(root, url=alias).namespace == state.namespace
    assert load(root, url=alias) == state


@pytest.mark.parametrize("different_url", [
    "http://state.invalid/deployment/team", "https://state.invalid/deployment/other",
    "https://state.invalid:443/deployment/team", "https://other.invalid/deployment/team",
])
def test_endpoint_scheme_host_explicit_port_and_prefix_remain_separate(root, different_url):
    original = populated(root)
    original.save()
    old_bytes = original.path.read_bytes()
    other = fresh(root, url=different_url)
    assert other.path != original.path and load(root, url=different_url) is None
    other.save()
    assert load(root) == original
    assert original.path.read_bytes() == old_bytes
    assert load(root, url=different_url) == other


@pytest.mark.parametrize("axis", ["root", "url", "persistent_id"])
def test_new_namespace_starts_empty_without_moving_or_overwriting_old_bases(root, tmp_path, axis):
    original = populated(root)
    original.save()
    old_bytes = original.path.read_bytes()
    destination = tmp_path / "copied-project" if axis == "root" else root
    destination.mkdir(exist_ok=True)
    url = "https://other.invalid/team" if axis == "url" else URL
    project_id = "beta" if axis == "persistent_id" else ID
    assert load(destination, url=url, persistent_id=project_id) is None
    other = fresh(destination, url=url, persistent_id=project_id)
    assert other.path != original.path
    assert other.docs == {} and other.cursor == 0
    other.save()
    assert original.path.read_bytes() == old_bytes and load(root) == original
    assert load(destination, url=url, persistent_id=project_id) == other


@pytest.mark.parametrize("field,value", [
    ("canonical_root", "another-root"), ("url", "https://other.invalid"),
    ("persistent_id", "beta"),
    # These would normalise to the requested tuple; the stored tuple is literal.
    ("url", "wss://STATE.INVALID/deployment/team/"),
    ("persistent_id", " Alpha "),
])
def test_stored_namespace_must_match_literally_before_any_reuse(root, field, value):
    raw = payload(root)
    raw["namespace"][field] = value
    path = write_fixture(root, raw)
    before = path.read_bytes()
    assert_error("namespace_mismatch", lambda: load(root))
    assert_error("namespace_mismatch", lambda: populated(root).save())
    assert path.read_bytes() == before


def test_stored_root_dot_alias_is_not_recanonicalized(root):
    raw = payload(root)
    raw["namespace"]["canonical_root"] += os.sep + "."
    write_fixture(root, raw)
    assert_error("namespace_mismatch", lambda: load(root))


def test_copying_state_to_another_namespace_does_not_import_bases(root, tmp_path):
    original = populated(root)
    original.save()
    destination_root = tmp_path / "clone"
    destination_root.mkdir()
    destination = fresh(destination_root).path
    destination.parent.mkdir(parents=True)
    shutil.copyfile(original.path, destination)
    before = destination.read_bytes()
    assert_error("namespace_mismatch", lambda: load(destination_root))
    assert_error("namespace_mismatch", lambda: fresh(destination_root).save())
    assert destination.read_bytes() == before
    assert load(root) == original


def test_digest_collision_still_checks_full_tuple_on_load_and_save(root, monkeypatch):
    class Collision:
        def hexdigest(self):
            return "a" * 64

    monkeypatch.setattr(state_module.hashlib, "sha256", lambda *args, **kwargs: Collision())
    original = populated(root)
    original.save()
    before = original.path.read_bytes()
    other = fresh(root, persistent_id="beta")
    assert other.path == original.path
    assert_error("namespace_mismatch", lambda: load(root, persistent_id="beta"))
    assert_error("namespace_mismatch", other.save)
    assert original.path.read_bytes() == before and load(root) == original


@pytest.mark.parametrize("seq", [7, 8, 100])
def test_valid_hello_checks_do_not_mutate_or_persist(root, seq):
    state = populated(root)
    state.save()
    before = state.path.read_bytes()
    snapshot = deepcopy(state)
    assert state.validate_hello(persistent_id=ID, space_instance_id=EPOCH, seq=seq) is None
    assert state == snapshot and state.path.read_bytes() == before


@pytest.mark.parametrize("changes,code", [
    ({"persistent_id": "beta"}, "identity_mismatch"),
    ({"space_instance_id": OTHER_EPOCH}, "instance_mismatch"),
    ({"seq": 6}, "cursor_rollback"),
    ({"persistent_id": "Alpha"}, "invalid_hello"),
    ({"persistent_id": ""}, "invalid_hello"),
    ({"persistent_id": None}, "invalid_hello"),
    ({"space_instance_id": EPOCH.upper()}, "invalid_hello"),
    ({"space_instance_id": "not-an-epoch"}, "invalid_hello"),
    ({"space_instance_id": None}, "invalid_hello"),
    ({"seq": True}, "invalid_hello"), ({"seq": -1}, "invalid_hello"),
    ({"seq": 7.0}, "invalid_hello"), ({"seq": "7"}, "invalid_hello"),
    ({"seq": None}, "invalid_hello"),
])
def test_hello_failures_preserve_cursor_docs_and_disk(root, changes, code):
    state = populated(root)
    state.save()
    before = state.path.read_bytes()
    snapshot = deepcopy(state)
    arguments = {"persistent_id": ID, "space_instance_id": EPOCH, "seq": 7} | changes
    assert_error(code, lambda: state.validate_hello(**arguments))
    assert state == snapshot and state.path.read_bytes() == before


@pytest.mark.parametrize("version", [1, 3, 0, True, 2.0, "2", None])
def test_version_is_strict_and_never_replaced(root, version):
    raw = payload(root)
    raw["version"] = version
    path = write_fixture(root, raw)
    before = path.read_bytes()
    assert_error("unsupported_version", lambda: load(root))
    assert_error("unsupported_version", lambda: populated(root).save())
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["version", "namespace", "space_instance_id", "cursor", "docs"])
def test_top_level_fields_are_required(root, field):
    raw = payload(root)
    del raw[field]
    write_fixture(root, raw)
    assert_error("invalid_state", lambda: load(root))


@pytest.mark.parametrize("location", ["top", "namespace", "base"])
def test_unknown_fields_are_rejected_at_every_schema_level(root, location):
    raw = payload(root)
    target = {"top": raw, "namespace": raw["namespace"],
              "base": raw["docs"]["folder/live.md"]}[location]
    target["unexpected"] = "must-not-be-ignored"
    write_fixture(root, raw)
    assert_error("invalid_state", lambda: load(root))


@pytest.mark.parametrize("field", ["canonical_root", "url", "persistent_id"])
def test_namespace_fields_are_required(root, field):
    raw = payload(root)
    del raw["namespace"][field]
    write_fixture(root, raw)
    assert_error("invalid_state", lambda: load(root))


@pytest.mark.parametrize("field,value", [
    ("namespace", None), ("namespace", []), ("namespace", "namespace"),
    ("space_instance_id", None), ("space_instance_id", ""),
    ("space_instance_id", EPOCH.upper()), ("space_instance_id", "0" * 31),
    ("space_instance_id", "g" * 32), ("space_instance_id", 123),
    ("cursor", True), ("cursor", -1), ("cursor", "7"),
    ("cursor", 7.0), ("cursor", None),
    ("docs", None), ("docs", []), ("docs", "docs"), ("docs", 7),
])
def test_invalid_state_fields_fail_closed(root, field, value):
    raw = payload(root)
    raw[field] = value
    path = write_fixture(root, raw)
    before = path.read_bytes()
    assert_error("invalid_state", lambda: load(root))
    assert_error("invalid_state", lambda: populated(root).save())
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["canonical_root", "url", "persistent_id"])
@pytest.mark.parametrize("value", [None, 7, True, [], {}])
def test_nonstring_namespace_values_never_match_requested_tuple(root, field, value):
    raw = payload(root)
    raw["namespace"][field] = value
    write_fixture(root, raw)
    assert_error("namespace_mismatch", lambda: load(root))


@pytest.mark.parametrize("entry", [
    None, [], "base", 1, {}, {"rev": 1}, {"hash": DIGEST},
    {"rev": True, "hash": DIGEST}, {"rev": 0, "hash": DIGEST},
    {"rev": -1, "hash": DIGEST}, {"rev": 8, "hash": DIGEST},
    {"rev": "5", "hash": DIGEST}, {"rev": 5.0, "hash": DIGEST},
    {"rev": 5, "hash": ""}, {"rev": 5, "hash": "a" * 63},
    {"rev": 5, "hash": "g" * 64}, {"rev": 5, "hash": DIGEST.upper()},
    {"rev": 5, "hash": 7}, {"rev": 5, "hash": False},
])
def test_document_base_schema_is_strict(root, entry):
    raw = payload(root)
    raw["docs"]["folder/live.md"] = entry
    write_fixture(root, raw)
    assert_error("invalid_state", lambda: load(root))


UNSAFE_PATHS = [
    "", " a.md", "a.md ", "/a.md", "../a.md", "a/../b.md", "./a.md",
    "a//b.md", "a/./b.md", "a\\b.md", "a\x00.md", "a\n.md", "a.pdf",
    "a" * 510 + ".md", "\ud800.md",
]


@pytest.mark.parametrize("path", UNSAFE_PATHS)
def test_unsafe_document_paths_fail_closed_on_load_and_save(root, path):
    raw = payload(root)
    raw["docs"] = {path: {"rev": 1, "hash": DIGEST}}
    write_fixture(root, raw)
    assert_error("invalid_state", lambda: load(root))
    # In-memory invalid bases must not reach a fresh destination either.
    state = replace(fresh(root, persistent_id="new-target"), cursor=1,
                    docs={path: Base(1, DIGEST)})
    assert_error("invalid_state", state.save)
    assert not state.path.exists()


@pytest.mark.parametrize("text", ["", "{", "null", "[]", '"state"', "7", "true"])
def test_malformed_json_or_nonobject_is_not_missing_state(root, text):
    path = write_fixture(root, payload(root))
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()
    assert_error("invalid_state", lambda: load(root))
    assert_error("invalid_state", lambda: fresh(root).save())
    assert path.read_bytes() == before


@pytest.mark.parametrize("location", ["top", "namespace", "docs", "base"])
def test_duplicate_json_keys_are_rejected_not_last_value_wins(root, location):
    raw = json.dumps(payload(root))
    if location == "top":
        raw = raw.replace('"version": 2', '"version": 2, "version": 2')
    elif location == "namespace":
        raw = raw.replace('"persistent_id": "alpha"',
                          '"persistent_id": "alpha", "persistent_id": "alpha"')
    elif location == "docs":
        entry = json.dumps(payload(root)["docs"]["deleted.txt"])
        raw = raw.replace('"deleted.txt": ' + entry,
                          '"deleted.txt": ' + entry + ', "deleted.txt": ' + entry)
    else:
        raw = raw.replace('"rev": 5', '"rev": 5, "rev": 5')
    path = write_fixture(root, payload(root))
    path.write_text(raw, encoding="utf-8")
    before = path.read_bytes()
    assert_error("invalid_state", lambda: load(root))
    assert_error("invalid_state", lambda: fresh(root).save())
    assert path.read_bytes() == before


def test_invalid_utf8_is_a_typed_failure_and_not_overwritten(root):
    path = write_fixture(root, payload(root))
    path.write_bytes(b"\xff\xfeinvalid-utf8")
    assert_error("invalid_state", lambda: load(root))
    assert_error("invalid_state", lambda: fresh(root).save())
    assert path.read_bytes() == b"\xff\xfeinvalid-utf8"


@pytest.mark.parametrize("exception", [PermissionError("fixture denied"), OSError("fixture I/O")])
def test_read_io_error_is_typed_never_fresh_or_silently_overwritten(root, monkeypatch, exception):
    state = populated(root)
    state.save()
    before = state.path.read_bytes()

    def fail(*args, **kwargs):
        raise exception

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fail)
        assert_error("state_unreadable", lambda: load(root))
        assert_error("state_unreadable", state.save)
    assert state.path.read_bytes() == before


def test_directory_at_state_path_is_unreadable_not_missing(root):
    path = fresh(root).path
    path.mkdir(parents=True)
    assert_error("state_unreadable", lambda: load(root))
    assert_error("state_unreadable", lambda: fresh(root).save())
    assert path.is_dir()


def test_file_not_found_read_error_on_an_existing_checkpoint_is_not_a_reset(root, monkeypatch):
    state = populated(root)
    state.save()
    before = state.path.read_bytes()

    def vanished_read(*args, **kwargs):
        raise FileNotFoundError("fixture transient read failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", vanished_read)
        assert_error("state_unreadable", lambda: load(root))
        assert_error("state_unreadable", state.save)
    assert state.path.read_bytes() == before


def test_missing_read_with_unreadable_lstat_is_not_treated_as_fresh(root, monkeypatch):
    state = populated(root)
    state.save()
    before = state.path.read_bytes()

    def missing(*args, **kwargs):
        raise FileNotFoundError("fixture dangling target")

    def unreadable(*args, **kwargs):
        raise PermissionError("fixture inaccessible link")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", missing)
        patch.setattr(Path, "lstat", unreadable)
        assert_error("state_unreadable", lambda: load(root))
        assert_error("state_unreadable", state.save)
    assert state.path.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"cursor": True}, {"cursor": -1}, {"cursor": 1.0}, {"docs": None},
    {"space_instance_id": EPOCH.upper()},
    {"docs": {"a.md": Base(1, DIGEST)}},  # rev cannot exceed zero cursor
    {"cursor": 1, "docs": {"a.md": Base(True, DIGEST)}},
    {"cursor": 1, "docs": {"a.md": Base(1, "invalid-hash")}},
    {"cursor": 1, "docs": {"a.md": {"rev": 1, "hash": DIGEST}}},
    {"cursor": 1, "docs": {1: Base(1, DIGEST)}},
])
def test_invalid_in_memory_state_does_not_create_file(root, changes):
    state = replace(fresh(root), **changes)
    assert_error("invalid_state", state.save)
    assert not state.path.exists()


@pytest.mark.parametrize("changes,code", [
    ({"space_instance_id": OTHER_EPOCH}, "instance_mismatch"),
    ({"cursor": 6, "docs": {"a.md": Base(6, DIGEST)}}, "cursor_rollback"),
])
def test_save_cannot_reset_an_existing_incarnation_or_cursor(root, changes, code):
    state = populated(root)
    state.save()
    before = state.path.read_bytes()
    assert_error(code, lambda: replace(state, **changes).save())
    assert state.path.read_bytes() == before and load(root) == state


@pytest.mark.parametrize("stage", ["mkdir", "temporary", "dump", "write", "flush", "fsync", "replace"])
@pytest.mark.parametrize("existing", [False, True])
def test_atomic_write_failures_are_typed_preserve_previous_bytes_and_remove_temp(
    root, monkeypatch, stage, existing,
):
    state = populated(root)
    if existing:
        state.save()
    before = state.path.read_bytes() if existing else None
    replacement = replace(state, cursor=8)

    def fail(*args, **kwargs):
        raise OSError("fixture write failure")

    real_temporary = state_module.tempfile.NamedTemporaryFile

    class FailingStream:
        """Fail a real temporary stream, preserving its context/close semantics."""

        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            if stage == "write":
                self.stream.write(data[:1])
                fail()
            return self.stream.write(data)

        def flush(self):
            if stage == "flush":
                fail()
            return self.stream.flush()

    with monkeypatch.context() as patch:
        if stage == "mkdir":
            patch.setattr(Path, "mkdir", fail)
        elif stage == "temporary":
            patch.setattr(state_module.tempfile, "NamedTemporaryFile", fail)
        elif stage == "dump":
            patch.setattr(state_module.json, "dump", fail)
        elif stage in {"write", "flush"}:
            patch.setattr(state_module.tempfile, "NamedTemporaryFile",
                          lambda *args, **kwargs: FailingStream(real_temporary(*args, **kwargs)))
        elif stage == "fsync":
            patch.setattr(state_module.os, "fsync", fail)
        else:
            patch.setattr(Path, "replace", fail)
        assert_error("state_write_failed", replacement.save)
    if existing:
        assert state.path.read_bytes() == before
        assert load(root) == state
    else:
        assert not state.path.exists() and load(root) is None
    if state.path.parent.exists():
        assert sorted(p.name for p in state.path.parent.iterdir()) == (["state.json"] if existing else [])


def test_atomic_save_fsyncs_a_flushed_same_directory_temp_before_replace(root, monkeypatch):
    state = populated(root)
    state.save()
    before = state.path.read_bytes()
    events = []
    real_fsync, real_replace = os.fsync, Path.replace

    def fsync(fd):
        # A second reader must see complete JSON before durability/replacement.
        temps = [path for path in state.path.parent.iterdir() if path != state.path]
        assert len(temps) == 1
        expected = payload(root) | {"cursor": 8}
        assert json.loads(temps[0].read_text(encoding="utf-8")) == expected
        assert state.path.read_bytes() == before
        events.append("fsync")
        return real_fsync(fd)

    def atomic_replace(source, destination):
        assert source.parent == state.path.parent
        assert source != state.path and Path(destination) == state.path
        assert events == ["fsync"]
        assert state.path.read_bytes() == before
        events.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(state_module.os, "fsync", fsync)
    monkeypatch.setattr(Path, "replace", atomic_replace)
    replace(state, cursor=8).save()
    assert events == ["fsync", "replace"]
    assert load(root).cursor == 8
    assert list(state.path.parent.iterdir()) == [state.path]


@pytest.mark.parametrize("replace_fails", [False, True])
def test_cleanup_failure_does_not_mask_publication_result(root, monkeypatch, caplog, replace_fails):
    state = populated(root)
    state.save()
    before = state.path.read_bytes()
    replacement = replace(state, cursor=8)
    real_unlink = Path.unlink

    def cleanup_error(path, *args, **kwargs):
        if path.name.startswith(".sync-v2-"):
            raise PermissionError("fixture cleanup refused")
        return real_unlink(path, *args, **kwargs)

    def replace_error(*args, **kwargs):
        raise OSError("fixture replace refused")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", cleanup_error)
        if replace_fails:
            patch.setattr(Path, "replace", replace_error)
            assert_error("state_write_failed", replacement.save)
        else:
            assert replacement.save() is None
    assert "Could not remove a v2 sync temporary file" in caplog.text
    if replace_fails:
        assert state.path.read_bytes() == before and load(root) == state
    else:
        assert load(root) == replacement
    # A failed cleanup can leave only disposable temp data, never a changed base.
    for path in state.path.parent.iterdir():
        if path.name.startswith(".sync-v2-"):
            path.unlink()


@pytest.mark.parametrize("project_id", ["", "Alpha", " alpha", "alpha ", "a/b", "a" * 65, "я", None, 1])
def test_namespace_requires_canonical_persistent_id_without_normalization(root, project_id):
    assert_error("invalid_namespace", lambda: fresh(root, persistent_id=project_id))
    assert_error("invalid_namespace", lambda: load(root, persistent_id=project_id))


@pytest.mark.parametrize("url", ["", "ftp://state.invalid", "https://user:secret@state.invalid",
                                 "https://state.invalid?q=1", "https://state.invalid/#part", None, 1])
def test_invalid_namespace_url_is_typed_without_filesystem_side_effects(root, url, studio_home):
    assert_error("invalid_namespace", lambda: fresh(root, url=url))
    assert_error("invalid_namespace", lambda: load(root, url=url))
    assert not (studio_home / "sync").exists()


def test_v1_path_load_save_and_protocol_remain_unchanged(root, studio_home):
    expected = studio_home / "sync" / (hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:16] + ".json")
    assert SyncState.file_for(root) == expected
    assert state_module.STATE_VERSION == PROTOCOL_VERSION == 1
    legacy = SyncState.load(root, url=URL, space_id="legacy-incarnation")
    assert legacy.path == expected and legacy.cursor == 0 and legacy.docs == {}
    legacy.cursor = 3
    legacy.docs = {"old.md": Base(2, DIGEST), "gone.txt": Base(3, None)}
    legacy.save()
    raw = {"version": 1, "url": URL, "space_id": "legacy-incarnation", "cursor": 3,
           "docs": {"old.md": {"rev": 2, "hash": DIGEST}, "gone.txt": {"rev": 3, "hash": None}}}
    assert json.loads(expected.read_text(encoding="utf-8")) == raw
    assert SyncState.load(root, url=URL, space_id="legacy-incarnation") == legacy
    assert SyncState.load(root, url="https://other.invalid", space_id="legacy-incarnation").docs == {}
    assert SyncState.load(root, url=URL, space_id="other-incarnation").cursor == 0
    before = expected.read_bytes()
    assert load(root) is None  # no implicit migration, even with matching URL/root
    current = populated(root)
    current.save()
    assert current.path != expected and expected.read_bytes() == before
    assert SyncState.load(root, url=URL, space_id="legacy-incarnation") == legacy


def test_v2_does_not_discover_move_or_repair_malformed_v1_state(root):
    legacy_path = SyncState.file_for(root)
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"broken v1 state; leave it alone")
    assert load(root) is None
    fresh(root).save()
    assert legacy_path.read_bytes() == b"broken v1 state; leave it alone"


def test_v1_keeps_its_existing_corrupt_file_fallback(root):
    path = SyncState.file_for(root)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    legacy = SyncState.load(root, url=URL, space_id=EPOCH)
    assert legacy.path == path and legacy.cursor == 0 and legacy.docs == {}
    assert path.read_text(encoding="utf-8") == "{broken"
