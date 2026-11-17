"""Synthetic durable Create reservations (studio-design §5.1/§6/S3).

No runtime, provider, transport, event loop or server is constructed. Run under
VS-114's isolated home/temp/cache harness; every journal is explicitly injected.
These tests establish local at-most-once permission, NOT remote replay, ownership,
GET authorization, selection, consent or successful power-loss persistence.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict, fields, replace
import inspect
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from threading import Barrier, Event
import time

import pytest

from voxarium_studio.sync import catalog_intents as module
from voxarium_studio.sync.catalog import CatalogError
from voxarium_studio.sync.catalog_intents import (
    CatalogIntent, CatalogIntentJournal, IntentRecord, IntentReservation,
)


PROFILE = "00000000-0000-4000-8000-000000000114"
OTHER_PROFILE = "00000000-0000-4000-8000-000000000115"
URL = "https://journal.invalid/deploy/alpha"
RAW_LABEL = "  Cafe\u0301 / Project  "
MAX_REVISION = 2**63 - 1
STATES = ("created", "conflict", "unknown", "rejected", "found", "absent")
INTENT_FIELDS = {"operation_id", "profile_id", "url", "revision", "project_id", "display_name"}
REAL_CONNECT = sqlite3.connect


@pytest.fixture(autouse=True)
def external_io_counters(monkeypatch):
    """Detect swallowed forbidden calls; only the synthetic journal may persist."""
    counts = {"dns": 0, "socket": 0, "store": 0}

    def forbidden(kind):
        def fail(*_args, **_kwargs):
            counts[kind] += 1
            raise AssertionError("Intent journal attempted forbidden external I/O")
        return fail

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "getnameinfo"):
        monkeypatch.setattr(socket, name, forbidden("dns"))
    for name in ("socket", "socketpair", "create_connection", "create_server"):
        if hasattr(socket, name):
            monkeypatch.setattr(socket, name, forbidden("socket"))

    from voxarium_studio.core import application_config, paths
    from voxarium_studio.sync import credentials, profiles

    monkeypatch.setattr(paths, "user_config_dir", forbidden("store"))
    for owner, names in (
        (application_config, ("_load_application_config_strict", "_write_application_profiles",
                              "application_config_transaction", "application_config_file")),
        (credentials, ("load_profile_token", "save_profile_token", "load_token", "save_token",
                       "import_legacy_token", "keys_file")),
        (profiles, ("_load_application_config_strict", "_write_application_config",
                    "application_config_transaction")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden("store"))
    monkeypatch.setattr(profiles.RemoteProfiles, "get", forbidden("store"))
    yield counts
    assert counts == {"dns": 0, "socket": 0, "store": 0}


@pytest.fixture
def path(tmp_path):
    return tmp_path / "synthetic-intents.sqlite3"


def intent(**changes):
    return CatalogIntent(**{
        "operation_id": "synthetic-create-114", "profile_id": PROFILE, "url": URL,
        "revision": 1, "project_id": "alpha", "display_name": RAW_LABEL, **changes,
    })


def error(code, call):
    with pytest.raises(CatalogError) as raised:
        call()
    assert raised.value.code == code
    assert str(raised.value) == code
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    return raised.value


def reserve(journal, value=None):
    return journal.reserve(intent() if value is None else value)


def test_mandatory_path_and_narrow_synchronous_api(path):
    parameters = inspect.signature(CatalogIntentJournal).parameters
    assert set(parameters) == {"path"}
    assert parameters["path"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        CatalogIntentJournal()
    journal = CatalogIntentJournal(path)
    expected = {"reserve": {"intent"}, "lookup": {"operation_id"},
                "record_state": {"intent", "state"}}
    public = {name for name, value in inspect.getmembers(type(journal), callable)
              if not name.startswith("_")}
    assert public == set(expected)
    for name, names in expected.items():
        method = getattr(journal, name)
        assert not inspect.iscoroutinefunction(method)
        assert set(inspect.signature(method).parameters) == names
    assert inspect.signature(journal.record_state).parameters["state"].kind is inspect.Parameter.KEYWORD_ONLY


def test_exact_metadata_and_immutable_secret_free_dtos(path):
    original = intent()
    journal = CatalogIntentJournal(path)
    reservation = reserve(journal, original)
    assert isinstance(reservation, IntentReservation)
    assert isinstance(reservation.record, IntentRecord)
    assert reservation.post_permitted is True
    assert reservation.record.state == "reserved"
    assert reservation.record.intent == original
    assert asdict(reservation.record.intent) == asdict(original)
    assert original.display_name == RAW_LABEL  # Never normalize the operation's input.
    assert {f.name for f in fields(original)} == INTENT_FIELDS
    assert {f.name for f in fields(reservation.record)} == {"intent", "state"}
    assert {f.name for f in fields(reservation)} == {"record", "post_permitted"}
    for obj, field, value in ((original, "revision", 2),
                              (reservation.record, "state", "found"),
                              (reservation, "post_permitted", False)):
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            setattr(obj, field, value)
    for extra in ("token", "token_hash", "authorization", "credentials", "transport"):
        with pytest.raises(TypeError):
            CatalogIntent(**asdict(original), **{extra: "SYNTHETIC-NOT-A-KEY"})
    assert journal.lookup(original.operation_id) == reservation.record


def test_grant_is_already_committed_before_hypothetical_post(path):
    journal = CatalogIntentJournal(path)
    first = reserve(journal)
    assert first.post_permitted is True
    # A separately constructed observer reads committed state BEFORE any caller
    # could take the granted permission and issue a hypothetical POST.
    observer = CatalogIntentJournal(path)
    assert observer.lookup(intent().operation_id) == first.record
    assert reserve(observer).post_permitted is False
    assert reserve(journal).post_permitted is False
    assert reserve(CatalogIntentJournal(path)).post_permitted is False


@pytest.mark.parametrize("state", STATES)
def test_every_recorded_outcome_reopens_without_new_permission(path, state):
    original = intent()
    journal = CatalogIntentJournal(path)
    assert reserve(journal, original).post_permitted is True
    record = journal.record_state(original, state=state)
    assert record == IntentRecord(intent=original, state=state)
    assert journal.record_state(original, state=state) == record
    assert journal.lookup(original.operation_id) == record
    reopened = CatalogIntentJournal(path)
    assert reopened.lookup(original.operation_id) == record
    assert reserve(reopened, original) == IntentReservation(record=record, post_permitted=False)
    assert reserve(journal, original).post_permitted is False


@pytest.mark.parametrize("field,value", [
    ("profile_id", OTHER_PROFILE), ("url", "http://127.0.0.1/deploy/alpha"),
    ("url", "https://journal.invalid/deploy/beta"), ("revision", 2),
    ("project_id", "beta"), ("display_name", "Caf\u00e9 / Project"),
    ("display_name", " Cafe\u0301 / Project "),
])
@pytest.mark.parametrize("action", ["reserve", "record_state"])
def test_changed_original_fields_conflict_without_mutating_existing_row(path, field, value, action):
    journal = CatalogIntentJournal(path)
    initial = reserve(journal).record
    changed = replace(intent(), **{field: value})
    call = (lambda: journal.reserve(changed)) if action == "reserve" else (
        lambda: journal.record_state(changed, state="found"))
    error("operation_conflict", call)
    assert CatalogIntentJournal(path).lookup(intent().operation_id) == initial
    assert reserve(journal).post_permitted is False


@pytest.mark.parametrize("field,value", [("revision", True), ("revision", 1.0),
                                         ("revision", "1"), ("url", None),
                                         ("project_id", b"alpha"), ("display_name", None)])
def test_equal_looking_but_wrong_type_cannot_replay_existing_operation(path, field, value):
    journal = CatalogIntentJournal(path)
    initial = reserve(journal).record
    changed = replace(intent(), **{field: value})
    error("operation_conflict", lambda: journal.reserve(changed))
    error("operation_conflict", lambda: journal.record_state(changed, state="found"))
    assert CatalogIntentJournal(path).lookup(intent().operation_id) == initial
    assert reserve(journal).post_permitted is False


@pytest.mark.parametrize("operation_id", [None, True, 1, b"op", "", "x" * 129,
                                          " op", "op ", "a/b", "a\n", "\u043e\u043f"])
def test_invalid_operation_ids_fail_closed(path, operation_id):
    journal = CatalogIntentJournal(path)
    error("invalid_operation_id", lambda: journal.lookup(operation_id))
    error("invalid_intent", lambda: reserve(journal, intent(operation_id=operation_id)))
    error("invalid_intent", lambda: journal.record_state(
        intent(operation_id=operation_id), state="unknown"))


@pytest.mark.parametrize("field,value", [
    ("profile_id", None), ("profile_id", 1), ("profile_id", "not-a-uuid"),
    ("profile_id", PROFILE.replace("-", "")),
    ("profile_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
    ("url", None), ("url", 1), ("url", "journal.invalid/deploy/alpha"),
    ("url", URL + "/"), ("url", URL.replace("https:", "wss:")),
    ("url", URL.replace("journal", "JOURNAL")), ("url", " " + URL),
    ("url", "https://user:password@journal.invalid"), ("url", URL + "?token=bad"),
    ("url", "http://journal.invalid/deploy/alpha"),
    ("url", URL + "/../other"), ("url", URL + "/%2fother"),
    ("url", URL + "/%252fother"), ("url", URL + "\\other"),
    ("revision", True), ("revision", False), ("revision", 1.0), ("revision", "1"),
    ("revision", None), ("revision", 0), ("revision", -1), ("revision", MAX_REVISION + 1),
    ("project_id", None), ("project_id", 1), ("project_id", "Alpha"),
    ("project_id", " alpha"), ("project_id", "a/b"), ("project_id", "a" * 65),
    ("display_name", None), ("display_name", 1), ("display_name", " "),
    ("display_name", "x" * 121), ("display_name", "label\n"),
    ("display_name", "a\x7fb"), ("display_name", "a\u0085b"),
    ("display_name", "a\u202eb"), ("display_name", "a\u2066b"),
    ("display_name", "\ud800"),
])
@pytest.mark.parametrize("action", ["reserve", "record_state"])
def test_invalid_metadata_types_and_values_never_become_a_reservation(path, field, value, action):
    journal = CatalogIntentJournal(path)
    bad = intent(**{field: value})
    call = (lambda: journal.reserve(bad)) if action == "reserve" else (
        lambda: journal.record_state(bad, state="unknown"))
    error("invalid_intent", call)
    error("operation_not_found", lambda: journal.lookup(intent().operation_id))
    assert reserve(CatalogIntentJournal(path)).post_permitted is True


@pytest.mark.parametrize("revision", [1, MAX_REVISION])
@pytest.mark.parametrize("project_id", ["a", "0", "a" * 64, "0a._-"])
def test_valid_strict_boundaries_round_trip(path, revision, project_id):
    original = intent(revision=revision, project_id=project_id, display_name="\U0001f600" * 120)
    record = reserve(CatalogIntentJournal(path), original).record
    assert CatalogIntentJournal(path).lookup(original.operation_id) == record


@pytest.mark.parametrize("bad", [None, {}, {"operation_id": "synthetic-create-114"}, (), "intent", 1])
def test_only_intent_dto_is_accepted(path, bad):
    journal = CatalogIntentJournal(path)
    error("invalid_intent", lambda: journal.reserve(bad))
    error("invalid_intent", lambda: journal.record_state(bad, state="unknown"))


@pytest.mark.parametrize("state", [None, True, 1, "", "reserved", "CREATED", "created ", "other"])
def test_invalid_state_cannot_reset_or_replace_reservation(path, state):
    journal = CatalogIntentJournal(path)
    initial = reserve(journal).record
    error("invalid_intent_state", lambda: journal.record_state(intent(), state=state))
    assert CatalogIntentJournal(path).lookup(intent().operation_id) == initial
    assert reserve(journal).post_permitted is False


def test_missing_lookup_or_state_update_never_reserves(path):
    journal = CatalogIntentJournal(path)
    error("operation_not_found", lambda: journal.lookup("unknown-operation"))
    error("operation_not_found", lambda: journal.record_state(intent(), state="absent"))
    assert reserve(journal).post_permitted is True


def test_different_operations_and_profile_namespaces_are_not_deduplicated(path):
    journal = CatalogIntentJournal(path)
    originals = [intent(), intent(operation_id="other-operation"),
                 intent(operation_id="other-profile", profile_id=OTHER_PROFILE),
                 intent(operation_id="other-url", url="https://journal.invalid/other")]
    for original in originals:
        assert reserve(journal, original).post_permitted is True
    reopened = CatalogIntentJournal(path)
    for original in originals:
        assert reopened.lookup(original.operation_id).intent == original
        assert reserve(reopened, original).post_permitted is False


def test_competing_independent_instances_issue_exactly_one_grant(path):
    count = 8
    start = Barrier(count)
    journals = [CatalogIntentJournal(path) for _ in range(count)]

    def compete(journal):
        start.wait(timeout=15)
        return reserve(journal)

    with ThreadPoolExecutor(max_workers=count) as workers:
        results = list(workers.map(compete, journals))
    assert sum(result.post_permitted is True for result in results) == 1
    assert all(result.record == results[0].record for result in results)
    assert reserve(CatalogIntentJournal(path)).post_permitted is False


def test_competing_independent_instances_do_not_lose_other_operations(path):
    count = 8
    start = Barrier(count)
    originals = [intent(operation_id=f"operation-{n}") for n in range(count)]
    journals = [CatalogIntentJournal(path) for _ in range(count)]

    def compete(pair):
        journal, original = pair
        start.wait(timeout=15)
        first = journal.reserve(original)
        second = journal.reserve(original)
        return first, second

    with ThreadPoolExecutor(max_workers=count) as workers:
        results = list(workers.map(compete, zip(journals, originals)))
    assert all(first.post_permitted is True and second.post_permitted is False
               for first, second in results)
    reopened = CatalogIntentJournal(path)
    for original in originals:
        assert reopened.lookup(original.operation_id).intent == original
        assert reopened.reserve(original).post_permitted is False


@pytest.mark.parametrize("bad", [None, "synthetic.sqlite3", Path("relative.sqlite3"),
                                  Path("\\\\journal.invalid\\share\\intents.sqlite3")])
def test_path_is_explicit_absolute_local_path_only(bad):
    error("invalid_intent_path", lambda: CatalogIntentJournal(bad))


def test_missing_parent_is_not_created(path):
    missing = path.parent / "does-not-exist" / path.name
    error("intent_store_unavailable", lambda: CatalogIntentJournal(missing))
    assert not missing.parent.exists()


@pytest.mark.parametrize("contents", [b"", b"NOT A DATABASE\x00SYNTHETIC-114", b"SQLite format 3\x00"])
def test_incomplete_or_corrupt_existing_file_is_never_reinitialized(path, contents):
    path.write_bytes(contents)
    error("intent_store_invalid", lambda: CatalogIntentJournal(path))
    assert path.read_bytes() == contents


@pytest.mark.parametrize("mutation", [
    "PRAGMA user_version=999", "PRAGMA application_id=0",
    "CREATE TABLE unrelated (token TEXT)",
    "ALTER TABLE catalog_intents ADD COLUMN unexpected TEXT",
    "DROP TABLE catalog_intents",
    "CREATE TRIGGER unexpected AFTER INSERT ON catalog_intents BEGIN SELECT 1; END",
])
def test_unknown_schema_is_preserved_without_grant_or_repair(path, mutation):
    reserve(CatalogIntentJournal(path))
    with REAL_CONNECT(path) as db:
        db.execute(mutation)
    before = path.read_bytes()
    error("intent_store_invalid", lambda: CatalogIntentJournal(path))
    assert path.read_bytes() == before


def test_unrelated_sqlite_database_is_not_claimed(path):
    with REAL_CONNECT(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('SYNTHETIC-114')")
    before = path.read_bytes()
    error("intent_store_invalid", lambda: CatalogIntentJournal(path))
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [
    ("operation_id", "bad/operation"), ("profile_id", "bad-profile"),
    ("url", URL + "/"), ("revision", 0), ("revision", -1),
    ("project_id", "Alpha"), ("display_name", "bad\nlabel"),
    ("display_name", ""), ("state", "pending"), ("state", ""),
])
@pytest.mark.parametrize("action", ["construct", "lookup", "reserve", "record_state"])
def test_invalid_any_row_blocks_whole_journal_and_preserves_database(path, field, value, action):
    journal = CatalogIntentJournal(path)
    reserve(journal)
    other = intent(operation_id="other-operation")
    reserve(journal, other)
    with REAL_CONNECT(path) as db:
        # field comes only from the fixed parameter table, not caller input.
        db.execute(f"UPDATE catalog_intents SET {field}=? WHERE operation_id=?",
                   (value, other.operation_id))
    before = path.read_bytes()
    calls = {
        "construct": lambda: CatalogIntentJournal(path),
        "lookup": lambda: journal.lookup(intent().operation_id),
        "reserve": lambda: reserve(journal),
        "record_state": lambda: journal.record_state(intent(), state="found"),
    }
    error("intent_store_invalid", calls[action])
    assert path.read_bytes() == before
    error("intent_store_invalid", lambda: CatalogIntentJournal(path))
    assert path.read_bytes() == before


def test_truncated_full_database_is_preserved(path):
    reserve(CatalogIntentJournal(path))
    contents = path.read_bytes()
    path.write_bytes(contents[:len(contents) // 2])
    damaged = path.read_bytes()
    error("intent_store_invalid", lambda: CatalogIntentJournal(path))
    assert path.read_bytes() == damaged


def test_database_carries_exact_metadata_and_no_additional_secret_columns(path):
    original = intent()
    reserve(CatalogIntentJournal(path), original)
    with REAL_CONNECT(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        columns = [row[1] for row in db.execute("PRAGMA table_info(catalog_intents)")]
        assert set(columns) == INTENT_FIELDS | {"state"}
        row = db.execute("SELECT * FROM catalog_intents").fetchone()
        assert dict(zip(columns, row)) == {**asdict(original), "state": "reserved"}
        assert db.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall() == [
            ("catalog_intents",)]


def inject_sqlite_fault(monkeypatch, phase, exception):
    """Intercept actual transaction boundaries without adding production seams.

    The SQL connection subclass still performs real SQLite writes/commits. A
    BaseException models an interrupted caller, not an OS/power-loss simulator;
    OperationalError models an ambiguous disk failure that must latch closed.
    """
    events = []
    fired = []

    def fail_if(target):
        if phase == target and not fired:
            fired.append(target)
            raise exception

    class FaultConnection(sqlite3.Connection):
        touched = False

        def execute(self, sql, parameters=()):
            normalized = " ".join(sql.upper().split())
            events.append(normalized)
            if normalized.startswith(("INSERT INTO CATALOG_INTENTS", "UPDATE CATALOG_INTENTS")):
                fail_if("before_write")
                result = super().execute(sql, parameters)
                self.touched = True
                return result
            return super().execute(sql, parameters)

        def commit(self):
            if self.touched:
                fail_if("before_commit")
            result = super().commit()
            if self.touched:
                fail_if("after_commit")
            return result

    def connect(*args, **kwargs):
        kwargs["factory"] = FaultConnection
        return REAL_CONNECT(*args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    return events, fired


def test_write_uses_immediate_transaction_and_verified_extra_sync(path, monkeypatch):
    journal = CatalogIntentJournal(path)
    events, fired = inject_sqlite_fault(monkeypatch, "never", RuntimeError("unused"))
    assert reserve(journal).post_permitted is True
    assert not fired
    write = next(i for i, sql in enumerate(events) if sql.startswith("INSERT INTO CATALOG_INTENTS"))
    assert events.index("PRAGMA SYNCHRONOUS=EXTRA") < events.index("PRAGMA SYNCHRONOUS")
    assert events.index("PRAGMA SYNCHRONOUS") < events.index("BEGIN IMMEDIATE") < write


class SyntheticCrash(BaseException):
    pass


@pytest.mark.parametrize("phase", ["before_write", "before_commit", "after_commit"])
@pytest.mark.parametrize("action", ["reserve", "record_state"])
def test_interrupted_transaction_boundary_reopens_without_false_permission(path, monkeypatch, phase, action):
    journal = CatalogIntentJournal(path)
    if action == "record_state":
        reserve(journal)
    with monkeypatch.context() as patch:
        _, fired = inject_sqlite_fault(patch, phase, SyntheticCrash())
        call = (lambda: reserve(journal)) if action == "reserve" else (
            lambda: journal.record_state(intent(), state="created"))
        with pytest.raises(SyntheticCrash):
            call()
        assert fired == [phase]
    reopened = CatalogIntentJournal(path)
    committed = phase == "after_commit"
    if action == "reserve":
        if not committed:
            error("operation_not_found", lambda: reopened.lookup(intent().operation_id))
        result = reserve(reopened)
        assert result.post_permitted is (not committed)
        assert reserve(CatalogIntentJournal(path)).post_permitted is False
    else:
        assert reopened.lookup(intent().operation_id).state == ("created" if committed else "reserved")
        assert reserve(reopened).post_permitted is False


@pytest.mark.parametrize("phase", ["before_write", "before_commit", "after_commit"])
@pytest.mark.parametrize("action", ["reserve", "record_state"])
def test_disk_write_or_ambiguous_commit_failure_latches_instance_closed(path, monkeypatch, phase, action, caplog):
    journal = CatalogIntentJournal(path)
    if action == "record_state":
        reserve(journal)
    with monkeypatch.context() as patch:
        _, fired = inject_sqlite_fault(patch, phase, sqlite3.OperationalError("SYNTHETIC-DISK-CANARY-114"))
        call = (lambda: reserve(journal)) if action == "reserve" else (
            lambda: journal.record_state(intent(), state="created"))
        failure = error("intent_store_unavailable", call)
        assert fired == [phase]
        assert "SYNTHETIC-DISK-CANARY-114" not in repr(failure)
    # Restoring disk access must not let the ambiguous original instance grant.
    before = path.read_bytes()
    error("intent_store_unavailable", lambda: reserve(journal))
    error("intent_store_unavailable", lambda: journal.lookup(intent().operation_id))
    error("intent_store_unavailable", lambda: journal.record_state(intent(), state="found"))
    assert path.read_bytes() == before
    assert "SYNTHETIC-DISK-CANARY-114" not in caplog.text
    reopened = CatalogIntentJournal(path)
    if action == "reserve" and phase != "after_commit":
        error("operation_not_found", lambda: reopened.lookup(intent().operation_id))
    else:
        assert reserve(reopened).post_permitted is False


def test_connect_disk_failure_latches_existing_instance(path, monkeypatch):
    journal = CatalogIntentJournal(path)
    reserve(journal)
    before = path.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("SYNTHETIC-DISK-CANARY-114")

    with monkeypatch.context() as patch:
        patch.setattr(module.sqlite3, "connect", fail)
        error("intent_store_unavailable", lambda: reserve(journal))
    error("intent_store_unavailable", lambda: reserve(journal))
    assert path.read_bytes() == before
    assert reserve(CatalogIntentJournal(path)).post_permitted is False


def test_deleted_journal_is_never_silently_recreated_by_existing_instance(path):
    journal = CatalogIntentJournal(path)
    reserve(journal)
    path.unlink()
    error("intent_store_unavailable", lambda: reserve(journal))
    assert not path.exists()
    error("intent_store_unavailable", lambda: journal.lookup(intent().operation_id))
    assert not path.exists()


def test_replaced_file_is_not_treated_as_original_journal(path):
    journal = CatalogIntentJournal(path)
    reserve(journal)
    replacement = path.with_name("replacement.sqlite3")
    CatalogIntentJournal(replacement)
    replacement.replace(path)
    before = path.read_bytes()
    error("intent_store_invalid", lambda: reserve(journal))
    error("intent_store_invalid", lambda: journal.lookup(intent().operation_id))
    assert path.read_bytes() == before


def test_invalid_store_failure_remains_latched_after_external_repair(path):
    journal = CatalogIntentJournal(path)
    reserve(journal)
    with REAL_CONNECT(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        db.execute("PRAGMA user_version=999")
    error("intent_store_invalid", lambda: reserve(journal))
    with REAL_CONNECT(path) as db:
        db.execute(f"PRAGMA user_version={version}")
    before = path.read_bytes()
    error("intent_store_invalid", lambda: reserve(journal))
    assert path.read_bytes() == before
    assert reserve(CatalogIntentJournal(path)).post_permitted is False


@pytest.mark.parametrize("previous", STATES)
@pytest.mark.parametrize("following", STATES)
def test_observation_transitions_never_release_permission(path, previous, following):
    journal = CatalogIntentJournal(path)
    reserve(journal)
    journal.record_state(intent(), state=previous)
    result = journal.record_state(intent(), state=following)
    assert result.state == following
    assert reserve(journal).post_permitted is False
    assert CatalogIntentJournal(path).lookup(intent().operation_id) == result
    error("invalid_intent_state", lambda: journal.record_state(intent(), state="reserved"))


def test_rollback_failure_still_closes_connection_and_poisons_instance(path, monkeypatch, caplog):
    journal = CatalogIntentJournal(path)
    events = []

    class RollbackFailure(sqlite3.Connection):
        def commit(self):
            events.append("commit")
            raise sqlite3.OperationalError("SYNTHETIC-COMMIT-CANARY-114")

        def rollback(self):
            events.append("rollback")
            raise sqlite3.OperationalError("SYNTHETIC-ROLLBACK-CANARY-114")

        def close(self):
            events.append("close")
            return super().close()

    def connect(*args, **kwargs):
        kwargs["factory"] = RollbackFailure
        return REAL_CONNECT(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.sqlite3, "connect", connect)
        error("intent_store_unavailable", lambda: reserve(journal))
    assert events == ["commit", "rollback", "close"]
    error("intent_store_unavailable", lambda: reserve(journal))
    error("intent_store_unavailable", lambda: journal.lookup(intent().operation_id))
    assert events == ["commit", "rollback", "close"]
    observer = CatalogIntentJournal(path)  # The failed connection released its lock.
    error("operation_not_found", lambda: observer.lookup(intent().operation_id))
    assert reserve(observer).post_permitted is True
    assert "CANARY-114" not in caplog.text


def test_close_failure_after_commit_suppresses_grant_and_poisons_instance(path, monkeypatch, caplog):
    journal = CatalogIntentJournal(path)
    events = []

    class CloseFailure(sqlite3.Connection):
        def commit(self):
            events.append("commit")
            return super().commit()

        def close(self):
            events.append("close")
            super().close()
            raise sqlite3.OperationalError("SYNTHETIC-CLOSE-CANARY-114")

    def connect(*args, **kwargs):
        kwargs["factory"] = CloseFailure
        return REAL_CONNECT(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.sqlite3, "connect", connect)
        error("intent_store_unavailable", lambda: reserve(journal))
    assert events == ["commit", "close"]
    error("intent_store_unavailable", lambda: reserve(journal))
    error("intent_store_unavailable", lambda: journal.record_state(intent(), state="found"))
    assert events == ["commit", "close"]
    observer = CatalogIntentJournal(path)
    assert observer.lookup(intent().operation_id).state == "reserved"
    assert reserve(observer).post_permitted is False
    assert "SYNTHETIC-CLOSE-CANARY-114" not in caplog.text


def test_simultaneous_first_initialization_never_claims_an_incomplete_file(path, monkeypatch):
    """An incomplete concurrent initializer may fail closed, never repair/reset.

    Pause the O_EXCL winner before SQLite initialization. A simultaneous opener
    must not treat the empty found file as a fresh grant. This does not promise
    that all initial constructors succeed; that is outside the journal contract.
    """
    real_open = module.os.open
    claimed, release = Event(), Event()

    def paused_open(target, flags, *args, **kwargs):
        fd = real_open(target, flags, *args, **kwargs)
        if Path(target) == path and flags & os.O_EXCL:
            claimed.set()
            if not release.wait(timeout=15):
                os.close(fd)
                raise AssertionError("Synthetic initialization barrier timed out")
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "open", paused_open)
        with ThreadPoolExecutor(max_workers=2) as workers:
            winner = workers.submit(CatalogIntentJournal, path)
            try:
                assert claimed.wait(timeout=15)
                loser = workers.submit(CatalogIntentJournal, path)
                with pytest.raises(CatalogError) as raised:
                    loser.result(timeout=15)
                assert raised.value.code in {"intent_store_invalid", "intent_store_unavailable"}
                assert raised.value.__context__ is None
            finally:
                release.set()
            journal = winner.result(timeout=15)
    assert reserve(journal).post_permitted is True
    assert reserve(CatalogIntentJournal(path)).post_permitted is False


# Each explicit subprocess imports only the unmounted catalog module. No Pipes,
# multiprocessing, sockets, servers or live settings are used. Barriers, scripts,
# logs and counters are local files beneath the harness-provided task tmp_path.
_CHILD_SCRIPT = r'''
import os, sys, pathlib, json, socket, time, sqlite3, asyncio, ssl, types
from importlib.machinery import ModuleSpec

backend, filename, directory, mode, phase, barrier = sys.argv[1:]
root = pathlib.Path(directory)
root.mkdir(exist_ok=True)
for name in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP',
             'VOXARIUM_STUDIO_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
             'PYTHONPYCACHEPREFIX'):
    target = root / name
    target.mkdir(exist_ok=True)
    os.environ[name] = str(target)
for key in list(os.environ):
    if key.startswith('VOXARIUM_STUDIO_REMOTE_') or key in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
        del os.environ[key]
sys.dont_write_bytecode = True
sys.path.insert(0, backend)
counts = {'dns': 0, 'socket': 0, 'listeners': 0, 'store': 0, 'outside_sqlite': 0}

def denied(kind):
    def fail(*a, **kw):
        counts[kind] += 1
        raise AssertionError('synthetic journal child blocked ' + kind)
    return fail

for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr',
             'getnameinfo', 'create_connection'):
    setattr(socket, name, denied('dns'))
for name in ('bind', 'listen'):
    setattr(socket.socket, name, denied('listeners'))
for name in ('connect', 'connect_ex'):
    setattr(socket.socket, name, denied('socket'))
for name in ('socket', 'socketpair', 'create_server'):
    if hasattr(socket, name):
        setattr(socket, name, denied('socket'))

allowed = pathlib.Path(filename).parent
def audit(event, args):
    if event.startswith('socket.'):
        counts['dns' if 'get' in event else 'socket'] += 1
        raise AssertionError('synthetic journal child blocked socket audit')
    if event == 'sqlite3.connect':
        raw = str(args[0])
        if not raw.startswith(str(allowed)) and not raw.startswith(allowed.as_uri()):
            counts['outside_sqlite'] += 1
            raise AssertionError('synthetic journal child blocked outside SQLite')
    if event == 'import' and args[0].startswith(('voxarium_studio.core',
                                              'voxarium_studio.sync.credentials',
                                              'voxarium_studio.sync.profiles')):
        counts['store'] += 1
        raise AssertionError('synthetic journal child blocked external store import')
sys.addaudithook(audit)

def save(data):
    with (root / 'result.json').open('w', encoding='utf-8') as output:
        json.dump({'counts': counts, **data}, output)
        output.flush()
        os.fsync(output.fileno())

def crash(boundary):
    save({'boundary': boundary})
    os._exit(114)

try:
    import voxarium_studio
    # sync.__init__ eagerly imports RemoteSync. Test the unmounted submodule in
    # a namespace with the real source path, deliberately excluding runtime.
    package = types.ModuleType('voxarium_studio.sync')
    package.__path__ = [str(pathlib.Path(backend) / 'voxarium_studio' / 'sync')]
    package.__package__ = 'voxarium_studio.sync'
    package.__spec__ = ModuleSpec('voxarium_studio.sync', loader=None, is_package=True)
    sys.modules['voxarium_studio.sync'] = package
    voxarium_studio.sync = package
    from voxarium_studio.sync import catalog_intents as module
    from voxarium_studio.sync.catalog_intents import CatalogIntent, CatalogIntentJournal
    original = CatalogIntent('synthetic-create-114',
        '00000000-0000-4000-8000-000000000114',
        'https://journal.invalid/deploy/alpha', 1, 'alpha', '  Cafe' + chr(0x301) + ' / Project  ')
    if mode == 'init':
        real_open = module.os.open
        def opened(target, flags, *args, **kwargs):
            fd = real_open(target, flags, *args, **kwargs)
            if flags & os.O_EXCL and phase == 'after_claim':
                crash(phase)
            return fd
        module.os.open = opened
        real_connect = module.sqlite3.connect
        class InitCrashConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql.startswith('CREATE TABLE catalog_intents') and phase == 'after_schema':
                    crash(phase)
                return result
            def commit(self):
                if phase == 'before_init_commit':
                    crash(phase)
                result = super().commit()
                if phase == 'after_init_commit':
                    crash(phase)
                return result
        def connect(*args, **kwargs):
            kwargs['factory'] = InitCrashConnection
            return real_connect(*args, **kwargs)
        module.sqlite3.connect = connect
        CatalogIntentJournal(pathlib.Path(filename))
        if phase == 'after_init_return':
            crash(phase)
        raise AssertionError('synthetic initialization crash boundary was not reached')
    journal = CatalogIntentJournal(pathlib.Path(filename))
    if mode == 'race':
        (root / 'ready').write_text('ready', encoding='utf-8')
        deadline = time.monotonic() + 20
        while not pathlib.Path(barrier).exists():
            if time.monotonic() > deadline:
                raise AssertionError('synthetic child file barrier timed out')
            time.sleep(0.005)
        result = journal.reserve(original)
        save({'post_permitted': result.post_permitted, 'state': result.record.state})
    else:
        real_connect = module.sqlite3.connect
        class CrashConnection(sqlite3.Connection):
            touched = False
            def execute(self, sql, parameters=()):
                normalized = ' '.join(sql.upper().split())
                if normalized.startswith(('INSERT INTO CATALOG_INTENTS', 'UPDATE CATALOG_INTENTS')):
                    if phase == 'before_write':
                        crash(phase)
                    result = super().execute(sql, parameters)
                    self.touched = True
                    return result
                return super().execute(sql, parameters)
            def commit(self):
                if self.touched and phase == 'before_commit':
                    crash(phase)
                result = super().commit()
                if self.touched and phase == 'after_commit':
                    crash(phase)
                return result
        def connect(*args, **kwargs):
            kwargs['factory'] = CrashConnection
            return real_connect(*args, **kwargs)
        module.sqlite3.connect = connect
        if mode == 'reserve':
            journal.reserve(original)
        else:
            journal.record_state(original, state='created')
        if phase == 'after_return':
            crash(phase)
        raise AssertionError('synthetic crash boundary was not reached')
except BaseException:
    save({'failed': True})
    raise
finally:
    if any(counts.values()):
        save({'forbidden_io': True})
        raise AssertionError('synthetic journal child attempted forbidden I/O')
'''


def start_synthetic_child(tmp_path, path, name, *, mode, phase="", barrier=None):
    """Launch an explicit local script with file-only coordination and logs."""
    directory = tmp_path / name
    directory.mkdir()
    script = directory / "synthetic_journal_child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("VOXARIUM_STUDIO_REMOTE_") or key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            del env[key]
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                "VOXARIUM_STUDIO_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX"):
        target = directory / key
        target.mkdir()
        env[key] = str(target)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    backend = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-B", "-s", str(script), str(backend), str(path),
               str(directory), mode, phase, str(barrier or directory / "unused-barrier")]
    with (directory / "stdout.log").open("wb") as output:
        process = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT)
    return process, directory


def finish_synthetic_child(process, directory, expected_code):
    try:
        code = process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        raise AssertionError("Synthetic journal child timed out") from None
    log = (directory / "stdout.log").read_text(encoding="utf-8", errors="replace")
    assert code == expected_code, log
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert result["counts"] == {"dns": 0, "socket": 0, "listeners": 0,
                                "store": 0, "outside_sqlite": 0}, (result, log)
    return result


@pytest.mark.parametrize("phase", ["before_write", "before_commit", "after_commit", "after_return"])
@pytest.mark.parametrize("action", ["reserve", "record_state"])
def test_actual_process_exit_transaction_boundaries_reopen_safely(tmp_path, path, phase, action):
    journal = CatalogIntentJournal(path)
    if action == "record_state":
        reserve(journal)
    process, directory = start_synthetic_child(tmp_path, path, "crashing-child", mode=action, phase=phase)
    result = finish_synthetic_child(process, directory, 114)
    assert result["boundary"] == phase
    reopened = CatalogIntentJournal(path)
    committed = phase in {"after_commit", "after_return"}
    if action == "reserve":
        if not committed:
            error("operation_not_found", lambda: reopened.lookup(intent().operation_id))
        assert reserve(reopened).post_permitted is (not committed)
    else:
        assert reopened.lookup(intent().operation_id).state == ("created" if committed else "reserved")
        assert reserve(reopened).post_permitted is False
    assert reserve(CatalogIntentJournal(path)).post_permitted is False


@pytest.mark.parametrize("phase", ["after_claim", "after_schema", "before_init_commit",
                                   "after_init_commit", "after_init_return"])
def test_actual_initializer_exit_never_repairs_incomplete_schema(tmp_path, path, phase):
    assert not path.exists()
    process, directory = start_synthetic_child(tmp_path, path, "initializing-child",
                                              mode="init", phase=phase)
    assert finish_synthetic_child(process, directory, 114)["boundary"] == phase
    before = path.read_bytes()
    if phase in {"after_claim", "after_schema", "before_init_commit"}:
        error("intent_store_invalid", lambda: CatalogIntentJournal(path))
        assert path.read_bytes() == before
        error("intent_store_invalid", lambda: CatalogIntentJournal(path))
        assert path.read_bytes() == before
    else:
        reopened = CatalogIntentJournal(path)
        error("operation_not_found", lambda: reopened.lookup(intent().operation_id))
        assert path.read_bytes() == before
        # Initialization did not issue any grant or reserve an operation.
        assert reserve(reopened).post_permitted is True
        assert reserve(CatalogIntentJournal(path)).post_permitted is False


def test_independent_processes_racing_same_operation_issue_only_one_grant(tmp_path, path):
    CatalogIntentJournal(path)
    barrier = tmp_path / "release-processes"
    children = []
    try:
        for number in range(4):
            children.append(start_synthetic_child(tmp_path, path, f"racing-child-{number}",
                                                  mode="race", barrier=barrier))
        deadline = time.monotonic() + 25
        while not all((directory / "ready").exists() for _, directory in children):
            assert time.monotonic() < deadline, "Synthetic process initialization timed out"
            assert all(process.poll() is None for process, _ in children), "Synthetic child exited early"
            time.sleep(0.005)
        barrier.write_text("release", encoding="utf-8")
        results = [finish_synthetic_child(process, directory, 0) for process, directory in children]
        assert sum(result["post_permitted"] is True for result in results) == 1
        assert all(result["state"] == "reserved" for result in results)
    finally:
        for process, _ in children:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    assert reserve(CatalogIntentJournal(path)).post_permitted is False
