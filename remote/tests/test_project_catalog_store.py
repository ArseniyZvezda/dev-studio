from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from voxarium_studio_remote.protocol import ProtocolError
from voxarium_studio_remote.store import (
    Store, ProjectExists, UnknownProject, UnknownLegacySpace, InvalidRequest,
    MetadataConflict, SnapshotChanged, SNAPSHOT_MAX_BYTES,
)


@pytest.fixture
def store():
    value = Store(":memory:")
    yield value
    value.close()


@pytest.mark.parametrize("project_id", ["a", "a" * 64, "boloto-team", "0.1_-", "12345678-1234-4234-8234-123456789abc"])
def test_exact_ids_and_explicit_create(store, project_id):
    with pytest.raises(UnknownProject):
        store.project(project_id)
    assert store.list_projects()["projects"] == []
    project = store.create_project(project_id, " Название ")
    assert project.id == project_id and project.display_name == "Название"
    assert len(project.storage_epoch) == 32 and project.storage_epoch != project.id
    assert project.metadata_revision == 1 and project.seq == 0
    assert set(project.to_json()) == {"id", "display_name", "storage_epoch", "metadata_revision", "created_at"}
    assert store.space(project_id) == project
    with pytest.raises(ProjectExists):
        store.create_project(project_id, "Название")


@pytest.mark.parametrize("project_id", ["", "UPPER", " name", "name ", "а", "a/b", "a\\b", "a%2fb", "a%252fb", "\n", ".a", "a" * 65, True, 1, None])
def test_invalid_ids_never_normalized_or_created(store, project_id):
    for operation in (store.project, store.space, lambda raw: store.create_project(raw, "Name")):
        with pytest.raises(ProtocolError):
            operation(project_id)
    assert store.list_projects()["projects"] == []


def test_duplicate_labels_keyset_and_label_cas_are_not_doc_revisions(store):
    a = store.create_project("a", "Same")
    store.create_project("c", "Same")
    store.create_project("b", "Same")
    first = store.list_projects(limit=2)
    assert [p["id"] for p in first["projects"]] == ["a", "b"]
    assert first["next_after"] == "b"
    assert [p["id"] for p in store.list_projects(after="ba")["projects"]] == ["c"]
    assert store.list_projects(after="z")["next_after"] is None
    new = store.update_project("a", " Изменено ", expected_revision=1)
    assert new.metadata_revision == 2 and new.seq == 0 and new.storage_epoch == a.storage_epoch
    assert store.update_project("a", "Изменено", expected_revision=2) == new
    with pytest.raises(MetadataConflict):
        store.update_project("a", "Stale", expected_revision=1)
    with pytest.raises(UnknownLegacySpace):
        store.resolve_legacy("Same")


@pytest.mark.parametrize("limit", [True, 0, -1, 201, "2", None])
def test_catalog_limit_validation(store, limit):
    with pytest.raises(InvalidRequest):
        store.list_projects(limit=limit)


def test_two_connections_create_and_metadata_races_have_one_winner(tmp_path):
    path = tmp_path / "race.db"
    a, b = Store(path), Store(path)
    try:
        def race(fn):
            barrier = threading.Barrier(2)

            def worker(store):
                barrier.wait()
                try:
                    fn(store)
                    return "ok"
                except (ProjectExists, MetadataConflict) as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                return sorted(pool.map(worker, (a, b)))

        assert race(lambda s: s.create_project("race", "Name")) == ["ok", "project_exists"]
        assert race(lambda s: s.update_project("race", threading.current_thread().name, 1)) == ["metadata_conflict", "ok"]
        assert len(a.list_projects()["projects"]) == 1
        assert b.project("race").metadata_revision == 2
    finally:
        a.close()
        b.close()


def test_snapshot_consistent_seq_epoch_tombstones_and_no_writes(tmp_path):
    path = tmp_path / "snapshot.db"
    a, b = Store(path), Store(path)
    try:
        project = a.create_project("project", "Name")
        a.put(project.id, "z.md", "last", None)
        a.put(project.id, "a.md", "first", None)
        a.delete(project.id, "b.md", None)
        a.put(project.id, "Русский.md", "текст", None)
        before = a._db.total_changes
        first = a.snapshot(project.id, limit=2)
        assert a._db.total_changes == before
        assert [d["path"] for d in first["documents"]] == ["a.md", "b.md"]
        assert first["documents"][1]["deleted"] is True
        assert first["next_after"] == "b.md"
        second = a.snapshot(project.id, limit=2, after=first["next_after"],
                            expected_seq=4, expected_epoch=project.storage_epoch)
        assert [d["path"] for d in second["documents"]] == ["z.md", "Русский.md"]
        assert second["next_after"] is None
        b.put(project.id, "z.md", "changed", None)
        with pytest.raises(SnapshotChanged):
            a.snapshot(project.id, after=first["next_after"], expected_seq=4,
                       expected_epoch=project.storage_epoch)
        with pytest.raises(SnapshotChanged):
            a.snapshot(project.id, expected_epoch="0" * 32)
        # Metadata is a separate revision; it does not invalidate document pages.
        a.update_project(project.id, "Renamed", 1)
        assert a.snapshot(project.id, expected_seq=5)["snapshot"]["seq"] == 5
    finally:
        a.close()
        b.close()


def test_snapshot_read_transaction_fences_a_concurrent_wal_write(tmp_path):
    path = tmp_path / "snapshot-read.db"
    a, b = Store(path), Store(path)
    try:
        a.create_project("project", "Name")
        a.put("project", "a.md", "before", None)
        original = a.project

        def after_identity_read(project_id):
            project = original(project_id)
            b.put(project_id, "a.md", "after", None)
            return project

        a.project = after_identity_read
        page = a.snapshot("project")
        assert page["snapshot"]["seq"] == 1
        assert page["documents"][0]["content"] == "before"
        assert b.doc("project", "a.md").content == "after"
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("params", [
    {"limit": True}, {"limit": 101}, {"limit": 0}, {"after": "x.md"},
    {"expected_seq": True}, {"expected_seq": -1}, {"expected_epoch": "BAD"},
    {"after": "../x.md", "expected_seq": 0, "expected_epoch": "0" * 32},
    {"after": " x.md", "expected_seq": 0, "expected_epoch": "0" * 32},
])
def test_snapshot_parameter_validation(store, params):
    store.create_project("project", "Name")
    with pytest.raises(InvalidRequest):
        store.snapshot("project", **params)


def test_snapshot_byte_budget_cursor_does_not_skip(store):
    store.create_project("large", "Name")
    for i in range(7):
        store.put("large", f"{i}.md", "x" * 1_000_000, None)
    first = store.snapshot("large", limit=100)
    assert len(json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode()) <= SNAPSHOT_MAX_BYTES
    assert len(first["documents"]) == 4
    assert first["next_after"] == "3.md"
    last = store.snapshot("large", after=first["next_after"], expected_seq=7,
                          expected_epoch=first["snapshot"]["storage_epoch"])
    assert [d["path"] for d in last["documents"]] == ["4.md", "5.md", "6.md"]
    assert last["next_after"] is None


def test_unknown_project_operations_are_lookup_only(store):
    for operation in (store.project, store.space, store.snapshot, store.seq,
                      lambda i: store.put(i, "a.md", "hello", None),
                      lambda i: store.delete(i, "a.md", None)):
        with pytest.raises(UnknownProject):
            operation("missing")
    assert store.list_spaces() == []
    assert store._db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0
