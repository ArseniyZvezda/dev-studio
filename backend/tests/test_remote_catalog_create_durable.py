"""Offline durable Create integration (studio-design §5.1/§6/S3, VS-115).

Only synthetic providers/transport and an explicitly injected local journal are
used. Run with the task's no-socket, isolated-home/temp harness, NOT ordinary
pytest: even pytest's temporary files must stay inside TASK-115. These tests
prove local at-most-once permission, not remote replay or power-loss durability.
No credential provider can prove a previous ENV binding after service restart.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, fields
import hashlib
import inspect
import sqlite3

import pytest

from voxarium_studio.sync.catalog import CatalogError
from voxarium_studio.sync.catalog_create_service import CatalogCreateService
from voxarium_studio.sync.catalog_intents import CatalogIntent, CatalogIntentJournal

from test_remote_catalog_service import (
    BASE_A, KEY_A, KEY_B, PROFILE_A, PROFILE_B,
    Credentials, Profiles, Transport, assert_secret_free, event_loop_policy,
    health, identity, response, synthetic_only,
)
from test_remote_catalog_read_service import external_io_counters
from test_remote_catalog_create_service import (
    ACTIONS, LABEL, MUTATIONS, OP, WIRE_LABEL, assert_dto_safe, create, envelope,
    failure, invoke, mutate, success,
)


@pytest.fixture
def path(tmp_path):
    # The task's diagnostic runner places pytest's basetemp under TASK-115.
    return tmp_path / "synthetic-durable-create.sqlite3"


def original(**changes):
    return CatalogIntent(**{
        "operation_id": OP, "profile_id": PROFILE_A, "url": BASE_A,
        "revision": 1, "project_id": "alpha", "display_name": LABEL, **changes,
    })


def build(path, *replies, journal=None, profiles=None, credentials=None):
    profiles = Profiles() if profiles is None else profiles
    credentials = Credentials() if credentials is None else credentials
    transport = Transport(*replies)
    journal = CatalogIntentJournal(path) if journal is None else journal
    service = CatalogCreateService(profiles=profiles, credentials=credentials,
                                   transport=transport, mode="durable", journal=journal)
    return service, profiles, credentials, transport, journal


def assert_record(path, *, state=None):
    # Reopen independently; the original in-memory journal is not evidence.
    journal = CatalogIntentJournal(path)
    record = journal.lookup(OP)
    assert record.intent == original()
    if state is not None:
        assert record.state == state
    assert journal.reserve(original()).post_permitted is False
    return record


def assert_disk_secret_free(path, caplog):
    for candidate in path.parent.iterdir():
        if candidate.is_file():
            data = candidate.read_bytes()
            for key in (KEY_A, KEY_B):
                assert key.encode() not in data
                assert key.encode("utf-16-le") not in data
                for digest in (hashlib.sha256, hashlib.sha1, hashlib.md5):
                    assert digest(key.encode()).digest() not in data
                    assert digest(key.encode()).hexdigest().encode() not in data
    for key in (KEY_A, KEY_B):
        assert key not in caplog.text
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(catalog_intents)")}
        assert columns == {field.name for field in fields(CatalogIntent)} | {"state"}
        assert KEY_A not in repr(db.execute("SELECT * FROM catalog_intents").fetchall())
        assert KEY_B not in repr(db.execute("SELECT * FROM catalog_intents").fetchall())


def test_explicit_mode_requires_exact_injected_journal(path):
    kwargs = {"profiles": Profiles(), "credentials": Credentials(), "transport": Transport()}
    signature = inspect.signature(CatalogCreateService)
    assert signature.parameters["mode"].default == "instance"
    assert signature.parameters["journal"].default is None
    journal = CatalogIntentJournal(path)
    for changes in ({"mode": "durable"}, {"journal": journal},
                    {"mode": "instance", "journal": journal},
                    {"mode": "durable", "journal": object()},
                    {"mode": "unknown", "journal": journal},
                    {"mode": None}, {"mode": True}):
        with pytest.raises(CatalogError, match="^invalid_intent_mode$") as caught:
            CatalogCreateService(**kwargs, **changes)
        assert_secret_free(caught.value)
    CatalogCreateService(**kwargs)
    CatalogCreateService(**kwargs, mode="durable", journal=journal)
    assert kwargs["profiles"].calls == kwargs["credentials"].calls == kwargs["transport"].calls == []


async def test_exact_raw_metadata_and_normalized_wire_are_distinct(path, caplog):
    service, profiles, credentials, transport, _ = build(path, *success())
    old_profiles, old_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    result = await create(service)
    assert result.state == "created"
    record = assert_record(path, state="created")
    assert record.intent.display_name == LABEL != WIRE_LABEL
    assert transport.calls[2]["json"] == {"id": "alpha", "display_name": WIRE_LABEL}
    assert [call["method"] for call in transport.calls] == ["GET", "GET", "POST"]
    assert [call["url"] for call in transport.calls] == [
        BASE_A + suffix for suffix in ("/healthz", "/api/v2/me", "/api/v2/projects")]
    for call in transport.calls:
        assert call["follow_redirects"] is False and call["params"] == {}
        assert call["headers"]["Cache-Control"] == "no-store"
        assert OP not in repr((call["url"], call["headers"], call["params"], call["json"]))
    assert profiles.items == old_profiles and credentials.keys == old_keys
    assert await create(service) == result
    assert len(transport.calls) == 3
    assert_dto_safe(result, caplog)
    assert_disk_secret_free(path, caplog)


@pytest.mark.parametrize("state", ["reserved", "created", "conflict", "unknown", "rejected", "found", "absent"])
@pytest.mark.parametrize("action", ACTIONS)
async def test_existing_row_never_captures_current_key_or_automatically_gets(path, state, action, caplog):
    journal = CatalogIntentJournal(path)
    assert journal.reserve(original()).post_permitted is True
    if state != "reserved":
        journal.record_state(original(), state=state)
    for key in (KEY_A, KEY_B, None):
        service, _, credentials, transport, _ = build(path)
        credentials.keys[(PROFILE_A, BASE_A)] = key
        for _ in range(2):
            with pytest.raises(CatalogError, match="^credential_binding_unproven$") as caught:
                await invoke(service, action)
            assert_secret_free(caught.value, caplog)
        assert credentials.calls == [] and transport.calls == []
        assert_record(path, state=state)


@pytest.mark.parametrize("changed", [
    {"profile_id": PROFILE_B}, {"profile_id": None}, {"revision": 2},
    {"revision": True}, {"revision": "1"}, {"id": "beta"}, {"id": "INVALID"},
    {"id": None}, {"display_name": None}, {"display_name": "\n"},
    {"display_name": LABEL.strip()}, {"display_name": "  Caf\u00e9 / Project  "},
    {"display_name": WIRE_LABEL},
])
@pytest.mark.parametrize("fresh", [False, True])
async def test_changed_raw_input_conflicts_even_invalid_or_equivalent(path, changed, fresh):
    service, _, credentials, transport, _ = build(path, *success())
    await create(service)
    if fresh:
        service, _, credentials, transport, _ = build(path)
    calls, credentials_before = len(transport.calls), len(credentials.calls)
    with pytest.raises(CatalogError, match="^operation_conflict$"):
        await create(service, **changed)
    assert len(transport.calls) == calls and len(credentials.calls) == credentials_before
    assert_record(path, state="created")


@pytest.mark.parametrize("invalid,code", [
    ({"id": "INVALID"}, "invalid_id"), ({"id": None}, "invalid_id"),
    ({"display_name": "\n"}, "invalid_display_name"),
    ({"display_name": None}, "invalid_display_name"),
    ({"revision": True}, "profile_revision_conflict"),
    ({"revision": "1"}, "profile_revision_conflict"),
])
async def test_invalid_commands_keep_local_latch_without_invalid_journal_row(path, invalid, code):
    service, _, credentials, transport, journal = build(path)
    for _ in range(2):
        with pytest.raises(CatalogError, match="^" + code + "$"):
            await create(service, **invalid)
    with pytest.raises(CatalogError, match="^operation_not_found$"):
        journal.lookup(OP)
    with pytest.raises(CatalogError, match="^operation_conflict$"):
        await create(service)
    with pytest.raises(CatalogError, match="^" + code + "$"):
        await invoke(service, "recover", revision=invalid.get("revision", 1))
    assert credentials.calls == [] and transport.calls == []


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("mutation,code", [
    ("revision", "profile_revision_conflict"), ("url", "profile_revision_conflict"),
    ("delete", "profile_not_found"), ("profile_id", "invalid_profile_metadata"),
    ("profile_error", "profile_store_unavailable"), ("recovery", "profile_recovery_required"),
    ("key", "credential_binding_unproven"), ("missing_key", "credential_binding_unproven"),
])
async def test_cold_operation_checks_recorded_profile_before_any_credentials(path, action, mutation, code):
    journal = CatalogIntentJournal(path)
    journal.reserve(original())
    service, profiles, credentials, transport, _ = build(path)
    mutate(profiles, credentials, mutation)
    with pytest.raises(CatalogError, match="^" + code + "$") as caught:
        await invoke(service, action)
    assert_secret_free(caught.value)
    assert credentials.calls == [] and transport.calls == []
    assert_record(path, state="reserved")


@pytest.mark.parametrize("profile_id,revision", [(PROFILE_B, 1), (PROFILE_A, 2), (PROFILE_A, True)])
async def test_cold_recovery_cannot_change_original_profile_or_revision(path, profile_id, revision):
    journal = CatalogIntentJournal(path)
    journal.reserve(original())
    service, _, credentials, transport, _ = build(path)
    with pytest.raises(CatalogError, match="^operation_conflict$"):
        await invoke(service, "recover", profile_id=profile_id, revision=revision)
    assert credentials.calls == [] and transport.calls == []


async def test_unknown_recovery_does_not_reserve_or_invent_command(path):
    service, _, credentials, transport, journal = build(path)
    with pytest.raises(CatalogError, match="^operation_not_found$"):
        await invoke(service, "recover")
    with pytest.raises(CatalogError, match="^operation_not_found$"):
        journal.lookup(OP)
    assert credentials.calls == [] and transport.calls == []


@pytest.mark.parametrize("initial", [RuntimeError(KEY_A), failure("project_exists", 409)])
async def test_live_get_only_recovery_404_never_releases_post_or_selects(path, initial, caplog):
    service, profiles, credentials, transport, _ = build(
        path, response(health()), response(identity()), initial)
    old_profiles, old_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    assert (await create(service)).state in {"unknown", "conflict"}
    for reply, state in ((failure("project_not_found", 404), "absent"),
                         (response(envelope()), "found")):
        transport.replies[:] = [response(health()), response(identity()), reply]
        before = len(transport.calls)
        result = await invoke(service, "recover")
        assert result.state == state
        assert_dto_safe(result, caplog)
        assert await create(service) == result
        assert [c["url"] for c in transport.calls[before:]] == [
            BASE_A + suffix for suffix in ("/healthz", "/api/v2/me", "/api/v2/projects/alpha")]
        assert all(c["method"] == "GET" and c["json"] is None
                   for c in transport.calls[before:])
        assert_record(path, state=state)
    assert sum(call["method"] == "POST" for call in transport.calls) == 1
    assert profiles.items == old_profiles and credentials.keys == old_keys
    for method in ("select", "select_project", "enable", "connect", "activate"):
        assert not hasattr(service, method)
    assert_disk_secret_free(path, caplog)


@pytest.mark.parametrize("stage", [1, 2])
async def test_recovery_preflight_404_is_not_project_absent(path, stage):
    service, _, _, transport, _ = build(path, response(health()), response(identity()), RuntimeError(KEY_A))
    await create(service)
    transport.calls.clear()
    transport.replies[:] = success("recover")
    transport.replies[stage - 1] = failure("project_not_found", 404)
    with pytest.raises(CatalogError, match="^project_not_found$"):
        await invoke(service, "recover")
    assert_record(path, state="unknown")
    assert len(transport.calls) == stage
    assert all(c["method"] == "GET" for c in transport.calls)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("mutation,code", MUTATIONS)
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_durable_live_binding_fenced_and_latched_at_every_await(
        path, action, stage, mutation, code, outcome, caplog):
    service, profiles, credentials, transport, _ = build(path)
    if action == "recover":
        transport.replies[:] = [response(health()), response(identity()), RuntimeError(KEY_A)]
        await create(service)
        transport.calls.clear()
    saved_profiles, saved_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    transport.replies[:] = success(action)
    if outcome != "success":
        transport.replies[stage - 1] = (RuntimeError(KEY_A) if outcome == "failure"
                                       else asyncio.CancelledError(KEY_A))

    async def change(index):
        if index == stage:
            mutate(profiles, credentials, mutation)
        await asyncio.sleep(0)

    transport.hook = change
    with pytest.raises(CatalogError, match="^" + code + "$") as caught:
        await invoke(service, action)
    assert_secret_free(caught.value, caplog)
    assert len(transport.calls) == stage
    profiles.items, credentials.keys = saved_profiles, saved_keys
    transport.hook = None
    for retry in ACTIONS:
        with pytest.raises(CatalogError, match="^" + code + "$"):
            await invoke(service, retry)
    assert len(transport.calls) == stage
    assert set(credentials.calls) <= {(PROFILE_A, BASE_A)}
    assert_record(path, state="unknown")
    assert_disk_secret_free(path, caplog)


@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("action", ACTIONS)
async def test_actual_task_cancel_retains_durable_reservation_and_get_only_recovery(path, stage, action, caplog):
    service, _, _, transport, _ = build(path)
    if action == "recover":
        transport.replies[:] = [response(health()), response(identity()), RuntimeError(KEY_A)]
        await create(service)
        transport.calls.clear()
    transport.replies[:] = success(action)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    task = asyncio.create_task(invoke(service, action))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert_record(path, state="reserved" if action == "create" else "unknown")
        task.cancel(KEY_A)
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert_secret_free(caught.value, caplog)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert_record(path, state="unknown")
    assert len(transport.calls) == stage
    before_posts = sum(c["method"] == "POST" for c in transport.calls)
    before = len(transport.calls)
    assert (await create(service)).state == "unknown"
    assert len(transport.calls) == before
    transport.hook = None
    transport.replies[:] = success("recover")
    assert (await invoke(service, "recover")).state == "found"
    assert sum(c["method"] == "POST" for c in transport.calls) == before_posts
    cold, _, credentials, other_transport, _ = build(path)
    with pytest.raises(CatalogError, match="^credential_binding_unproven$"):
        await create(cold)
    assert credentials.calls == [] and other_transport.calls == []
    assert_disk_secret_free(path, caplog)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("mutation,code", [
    ("revision", "profile_revision_conflict"), ("key", "credential_binding_conflict"),
])
async def test_actual_task_cancel_still_fences_durable_live_binding(path, action, stage, mutation, code):
    service, profiles, credentials, transport, _ = build(path)
    if action == "recover":
        transport.replies[:] = [response(health()), response(identity()), RuntimeError(KEY_A)]
        await create(service)
        transport.calls.clear()
    transport.replies[:] = success(action)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    task = asyncio.create_task(invoke(service, action))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        mutate(profiles, credentials, mutation)
        task.cancel(KEY_A)
        with pytest.raises(CatalogError, match="^" + code + "$") as caught:
            await task
        assert_secret_free(caught.value)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(transport.calls) == stage
    assert_record(path, state="unknown")


@pytest.mark.parametrize("reply,state", [
    (response(envelope(), status=201), "created"),
    (failure("project_exists", 409), "conflict"),
    (failure("unauthorised", 401), "rejected"),
    (RuntimeError(KEY_A), "unknown"),
])
async def test_recorded_post_outcomes_are_not_authority_to_retry_after_reopen(path, reply, state, caplog):
    service, _, _, transport, _ = build(path, response(health()), response(identity()), reply)
    result = await create(service)
    assert result.state == state
    assert_dto_safe(result, caplog)
    assert_record(path, state=state)
    cold, _, credentials, other_transport, _ = build(path)
    for action in ACTIONS:
        with pytest.raises(CatalogError, match="^credential_binding_unproven$"):
            await invoke(cold, action)
    assert credentials.calls == [] and other_transport.calls == []
    assert sum(c["method"] == "POST" for c in transport.calls) == 1
    assert_disk_secret_free(path, caplog)


class _Boundary:
    def __await__(self):
        yield "synthetic-first-suspension"


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_synchronous_reservation_precedes_first_suspension_and_abandoned_stack(path, stage):
    """Drop the owner stack without outcome persistence (not a power-loss test)."""
    service, _, _, transport, _ = build(path, *success())

    async def pause(index):
        if index == stage:
            await _Boundary()

    transport.hook = pause
    command = create(service)
    try:
        assert command.send(None) == "synthetic-first-suspension"
        # No event loop or Task has run: reservation is already committed.
        assert_record(path, state="reserved")
        assert len(transport.calls) == stage
    finally:
        command.close()
    assert_record(path, state="reserved")
    assert sum(c["method"] == "POST" for c in transport.calls) == (1 if stage == 3 else 0)
    cold, _, credentials, other_transport, _ = build(path)
    for action in ACTIONS:
        recovery = invoke(cold, action)
        try:
            with pytest.raises(CatalogError, match="^credential_binding_unproven$"):
                recovery.send(None)
        finally:
            recovery.close()
    assert credentials.calls == [] and other_transport.calls == []


@pytest.mark.parametrize("stage", [1, 2, 3])
async def test_two_live_services_racing_same_id_never_make_second_request(path, stage):
    first, _, _, transport, _ = build(path, *success())
    second, _, credentials, other_transport, _ = build(path, *success())
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    task = asyncio.create_task(create(first))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert_record(path, state="reserved")
        assert (await create(first)).state == "unknown"
        with pytest.raises(CatalogError, match="^operation_in_progress$"):
            await invoke(first, "recover")
        for action in ACTIONS:
            with pytest.raises(CatalogError, match="^credential_binding_unproven$"):
                await invoke(second, action)
        assert credentials.calls == [] and other_transport.calls == []
        resume.set()
        assert (await task).state == "created"
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert sum(c["method"] == "POST" for c in transport.calls + other_transport.calls) == 1
    assert_record(path, state="created")


async def test_reserve_race_after_missing_lookup_cannot_capture_another_grant(path, caplog):
    """A competing writer inserts after lookup but before our reserve."""
    class RaceJournal(CatalogIntentJournal):
        def reserve(self, intent):
            winner = CatalogIntentJournal(path)
            assert winner.reserve(intent).post_permitted is True
            return super().reserve(intent)

    service, _, _, transport, _ = build(path, journal=RaceJournal(path))
    with pytest.raises(CatalogError, match="^credential_binding_unproven$") as caught:
        await create(service)
    assert_secret_free(caught.value, caplog)
    assert transport.calls == []
    assert_record(path, state="reserved")


@pytest.mark.parametrize("phase", ["lookup", "reserve", "reserve_committed", "result", "result_committed"])
async def test_injected_disk_faults_never_fall_back_or_release_operation(path, phase, caplog):
    class FaultJournal(CatalogIntentJournal):
        fault = phase

        def lookup(self, operation_id):
            if self.fault == "lookup":
                raise OSError(KEY_A + KEY_B)
            return super().lookup(operation_id)

        def reserve(self, intent):
            if self.fault == "reserve":
                raise OSError(KEY_A + KEY_B)
            result = super().reserve(intent)
            if self.fault == "reserve_committed":
                raise OSError(KEY_A + KEY_B)
            return result

        def record_state(self, intent, *, state):
            if self.fault == "result":
                raise OSError(KEY_A + KEY_B)
            result = super().record_state(intent, state=state)
            if self.fault == "result_committed":
                raise OSError(KEY_A + KEY_B)
            return result

    journal = FaultJournal(path)
    service, _, _, transport, _ = build(path, *success(), journal=journal)
    with pytest.raises(CatalogError, match="^intent_store_unavailable$") as caught:
        await create(service)
    assert_secret_free(caught.value, caplog)
    expected_calls = 3 if phase.startswith("result") else 0
    assert len(transport.calls) == expected_calls
    journal.fault = None
    for action in ACTIONS:
        with pytest.raises(CatalogError, match="^intent_store_unavailable$") as repeated:
            await invoke(service, action)
        assert_secret_free(repeated.value, caplog)
    assert len(transport.calls) == expected_calls
    if phase in {"reserve_committed", "result", "result_committed"}:
        assert_record(path, state="created" if phase == "result_committed" else "reserved")
        cold, _, credentials, other_transport, _ = build(path)
        for action in ACTIONS:
            with pytest.raises(CatalogError, match="^credential_binding_unproven$"):
                await invoke(cold, action)
        assert credentials.calls == [] and other_transport.calls == []
    else:
        with pytest.raises(CatalogError, match="^operation_not_found$"):
            CatalogIntentJournal(path).lookup(OP)
    assert_disk_secret_free(path, caplog)


@pytest.mark.parametrize("phase,state,calls", [
    ("reserved", "reserved", 0), ("before_result", "reserved", 3),
    ("after_result", "created", 3),
])
async def test_abrupt_owner_exit_at_committed_boundaries_cannot_regrant_post(path, phase, state, calls):
    """BaseException models lost owner control, not an actual OS/process crash."""
    class OwnerLost(BaseException):
        pass

    class InterruptedJournal(CatalogIntentJournal):
        def reserve(self, intent):
            result = super().reserve(intent)
            if phase == "reserved":
                raise OwnerLost()
            return result

        def record_state(self, intent, *, state):
            if phase == "before_result":
                raise OwnerLost()
            result = super().record_state(intent, state=state)
            if phase == "after_result":
                raise OwnerLost()
            return result

    service, _, _, transport, _ = build(path, *success(), journal=InterruptedJournal(path))
    with pytest.raises(OwnerLost):
        await create(service)
    assert len(transport.calls) == calls
    assert_record(path, state=state)
    cold, _, credentials, other_transport, _ = build(path)
    for action in ACTIONS:
        with pytest.raises(CatalogError, match="^credential_binding_unproven$"):
            await invoke(cold, action)
    assert credentials.calls == [] and other_transport.calls == []


@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("action", ACTIONS)
async def test_corruption_during_await_fails_closed_and_preserves_file(path, stage, action, caplog):
    service, _, _, transport, _ = build(path)
    if action == "recover":
        transport.replies[:] = [response(health()), response(identity()), RuntimeError(KEY_A)]
        await create(service)
        transport.calls.clear()
    transport.replies[:] = success(action)
    damaged = []

    async def corrupt(index):
        if index == stage:
            with sqlite3.connect(path) as db:
                db.execute("UPDATE catalog_intents SET state='corrupt'")
            damaged.append(path.read_bytes())
        await asyncio.sleep(0)

    transport.hook = corrupt
    with pytest.raises(CatalogError, match="^intent_store_invalid$") as caught:
        await invoke(service, action)
    assert_secret_free(caught.value, caplog)
    assert len(transport.calls) == stage
    assert path.read_bytes() == damaged[0]
    for retry in ACTIONS:
        with pytest.raises(CatalogError, match="^intent_store_invalid$"):
            await invoke(service, retry)
    assert len(transport.calls) == stage
    with pytest.raises(CatalogError, match="^intent_store_invalid$"):
        CatalogIntentJournal(path)
    assert path.read_bytes() == damaged[0]


@pytest.mark.parametrize("action", ACTIONS)
async def test_corrupt_database_before_command_never_reaches_providers(path, action):
    service, profiles, credentials, transport, _ = build(path)
    path.write_bytes(b"synthetic not a SQLite database")
    damaged = path.read_bytes()
    with pytest.raises(CatalogError, match="^intent_store_invalid$") as caught:
        await invoke(service, action)
    assert_secret_free(caught.value)
    assert profiles.calls == credentials.calls == transport.calls == []
    assert path.read_bytes() == damaged


@pytest.mark.parametrize("surface", ["label", "operation_id", "response", "transport"])
async def test_secret_canaries_never_enter_journal_dtos_errors_or_logs(path, surface, caplog):
    service, _, _, transport, journal = build(path, *success())
    if surface in {"label", "operation_id"}:
        kwargs = {"display_name": KEY_A} if surface == "label" else {"operation_id": KEY_A}
        with pytest.raises(CatalogError, match="^invalid_intent$") as caught:
            await create(service, **kwargs)
        assert_secret_free(caught.value, caplog)
        assert transport.calls == []
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM catalog_intents").fetchone() == (0,)
    else:
        transport.replies[2] = (response({"protocol": 2, "project": {
            **envelope()["project"], "display_name": KEY_A}}, status=201)
            if surface == "response" else RuntimeError(KEY_A + KEY_B))
        if surface == "response":
            with pytest.raises(CatalogError, match="^invalid_response$") as caught:
                await create(service)
            assert_secret_free(caught.value, caplog)
        else:
            result = await create(service)
            assert result.state == "unknown"
            assert_dto_safe(result, caplog)
        record = journal.lookup(OP)
        assert KEY_A not in repr(asdict(record)) and KEY_B not in repr(asdict(record))
    assert_disk_secret_free(path, caplog)
