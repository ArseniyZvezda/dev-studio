"""I8 synthetic local materialization/room proof (no servers or new Remote wire).

Room evidence is observed by the real relay owner; live-save evidence traverses
DocStore, pending put and ACK owners. Never manufacture ``_room_anchors`` here.
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest

from test_remote_editor_leases import remote_http, fence, p  # noqa: F401
from voxarium_studio.core.config import AppConfig
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.sync.protocol import content_hash
from voxarium_studio.sync.service import RemoteSync, _Session
from voxarium_studio.sync.state import Base, SyncState
from voxarium_studio.sync.transitions import MutationGateError
from voxarium_studio.teams.docs import DocStore
from voxarium_studio.ws.bus import EventBus


PATH = "guide/spec.md"
ORIGINAL = "# Original\n\nПривет, room.\n"
FINAL = "# Final\n\nОба editor видят этот Markdown.\n"


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self, *args, **kwargs):
        self.closed.append((args, kwargs))


def connect(sync, checkpoint):
    """Inject only transport/checkpoint, not causal room or save evidence."""
    sync._url = "https://synthetic.invalid"
    sync._space = "space"
    sync._ws = Socket()
    sync._synced = True
    sync._state_name = "connected"
    sync._state = SyncState(checkpoint, url=sync._url, space_id="incarnation")
    sync._state.docs[PATH] = Base(7, content_hash(ORIGINAL))


@pytest.fixture
async def room(tmp_path):
    paths = ProjectPaths.for_project(tmp_path / "room-project")
    paths.docs.mkdir(parents=True)
    bus = EventBus()
    docs = DocStore(paths, bus)
    sync = RemoteSync(paths, lambda: AppConfig(), docs, bus, token=lambda _: None)
    await docs.write(PATH, ORIGINAL)
    connect(sync, tmp_path / "checkpoint.json")
    docs.transitions.activate()
    yield sync
    for task in tuple(sync._tasks):
        task.cancel()
    await asyncio.gather(*sync._tasks, return_exceptions=True)


def add_editor(sync, client="browser-a", sid="sid-a"):
    gate = sync.docs.transitions
    row = gate.register_editor(PATH, client, gate.backend_epoch, gate.generation)
    gate.register_collab_session(sid, PATH, gate.generation, editor_id=row["editor_id"])
    session = _Session(sid, PATH, Socket(), 1, editor_id=row["editor_id"],
                       backend_epoch=gate.backend_epoch, client_instance_id=client,
                       binding_generation=gate.generation, registered=True, content=ORIGINAL)
    sync._sessions[sid] = session
    return session


async def joined(sync, session, **fields):
    await sync._to_browser({"t": "joined", "sid": session.sid, "epoch": "room-epoch",
                            "rev": 7, "seeded": True, "dirty": False, **fields})


def captured(sync, session):
    gate = sync.docs.transitions
    row = gate._editor_dto(gate._editors[session.editor_id])
    return {"backend_epoch": row["backend_epoch"],
            "expected_binding_generation": row["binding_generation"],
            "document_path": row["document_path"],
            "client_instance_id": row["client_instance_id"], "sid": session.sid,
            "version": row["version"], "revision": row["revision"]}


def prove(sync, session, content=ORIGINAL):
    body = captured(sync, session)
    ticket = sync.materialization(session.editor_id, **body)
    return sync.materialization(session.editor_id, **body,
                                proof_id=ticket["proof_id"], content=content)


def rejects(call, code=None):
    with pytest.raises(MutationGateError) as exc:
        call()
    if code is not None:
        assert exc.value.code == code


async def live_save(sync, writer, content=FINAL, version=1):
    """Mirror the HTTP save owner, including its pre-write EditorSave capture."""
    gate = sync.docs.transitions
    save = gate.begin_editor_save(writer.editor_id, gate.backend_epoch, gate.generation,
                                 document_path=PATH, client_instance_id=writer.client_instance_id,
                                 sid=writer.sid, version=version)
    def mark():
        sync.note_live_write(PATH, writer.sid, content,
                             expected_binding_generation=save.generation,
                             editor_id=writer.editor_id)
    with gate.http_mutation_guard(lambda: sync._require_collab_session(writer),
                                  save=save, before_mutation=mark):
        await sync.docs.write(PATH, content, expected_binding_generation=save.generation)
    row = gate.record_saved(save)
    await sync._push_local()
    frame = sync._ws.sent[-1]
    assert frame["t"] == "put" and frame["live"] == writer.sid
    assert frame["content"] == content
    return row, frame


def ack_live(sync, frame, rev=8):
    sync._on_ack({"t": "ack", "id": frame["id"], "path": PATH,
                  "hash": content_hash(frame["content"]), "rev": rev})


async def dirty_peers(sync):
    writer = add_editor(sync)
    peer = add_editor(sync, "browser-b", "sid-b")
    await joined(sync, writer, dirty=True)
    await joined(sync, peer, dirty=True)
    gate = sync.docs.transitions
    gate.heartbeat_editor(peer.editor_id, gate.backend_epoch, gate.generation,
                          sid=peer.sid, version=1, dirty=True)
    return writer, peer


async def test_clean_seeded_room_proof_has_own_receipt_and_zero_markdown_writes(room, monkeypatch):
    session = add_editor(room)
    await joined(room, session)
    before = room.docs.resolve(PATH).read_bytes()
    seq = room.bus.current_seq
    writes = AsyncMock(side_effect=AssertionError("proof must not write Markdown"))
    monkeypatch.setattr(room.docs, "write", writes)
    row = prove(room, session)
    assert row["confirmed"] and not row["dirty"]
    assert type(row["last_saved_version"]) is int
    assert row["revision"] == captured(room, session)["revision"]
    assert room.docs.resolve(PATH).read_bytes() == before
    assert room.bus.current_seq == seq and not room._ws.sent
    writes.assert_not_called()
    room.docs.transitions.close_editor(session.editor_id, row["backend_epoch"], 0,
                                       sid=session.sid, version=row["version"], dirty=False,
                                       last_saved_version=row["last_saved_version"])


async def test_initial_unseeded_room_requires_accepted_seed_handshake(room):
    session = add_editor(room)
    await joined(room, session, seeded=False)
    rejects(lambda: prove(room, session), "pending_edits")
    await room._send({"t": "seed", "sid": session.sid, "update": "opaque-initial-seed"})
    await room._to_browser({"t": "seeded", "sid": session.sid, "ok": True})
    assert prove(room, session)["confirmed"]
    assert [frame["t"] for frame in room._ws.sent] == ["seed"]


async def test_two_local_sessions_live_final_save_and_ack_mint_distinct_peer_receipt(room):
    writer, peer = await dirty_peers(room)
    row, frame = await live_save(room, writer)
    assert room.docs.resolve(PATH).read_text(encoding="utf-8") == FINAL
    assert row["confirmed"]
    rejects(lambda: prove(room, peer, FINAL), "pending_edits")
    ack_live(room, frame)
    peer_row = prove(room, peer, FINAL)
    assert peer_row["confirmed"] and not peer_row["dirty"]
    assert peer_row["editor_id"] != row["editor_id"]
    assert peer_row["last_saved_version"] != row["last_saved_version"]
    assert peer_row["version"] == 1
    assert room._state.docs[PATH] == Base(8, content_hash(FINAL))
    assert not room._pending and not room._live_writes


@pytest.mark.parametrize("when", ["ticket", "confirm", "close"])
@pytest.mark.parametrize("divergence", ["same-stat-disk", "base-hash", "base-rev"])
async def test_fresh_disk_and_exact_base_rechecked_at_every_boundary(room, when, divergence):
    session = add_editor(room)
    await joined(room, session)
    body = captured(room, session)
    if when != "ticket":
        ticket = room.materialization(session.editor_id, **body)
    if when == "close":
        row = room.materialization(session.editor_id, **body,
                                   proof_id=ticket["proof_id"], content=ORIGINAL)
    if divergence == "same-stat-disk":
        path = room.docs.resolve(PATH)
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace(b"Original", b"External"))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert path.stat().st_size == stat.st_size
        assert path.stat().st_mtime_ns == stat.st_mtime_ns
    else:
        room._state.docs[PATH] = Base(8 if divergence == "base-rev" else 7,
                                     content_hash("other") if divergence == "base-hash"
                                     else content_hash(ORIGINAL))
    if when == "ticket":
        rejects(lambda: room.materialization(session.editor_id, **body), "pending_edits")
    elif when == "confirm":
        rejects(lambda: room.materialization(session.editor_id, **body,
                proof_id=ticket["proof_id"], content=ORIGINAL), "pending_edits")
    else:
        rejects(lambda: room.docs.transitions.close_editor(session.editor_id,
                body["backend_epoch"], 0, sid=session.sid, version=body["version"],
                dirty=False, last_saved_version=row["last_saved_version"]), "pending_edits")


@pytest.mark.parametrize("field,bad", [
    ("backend_epoch", "stale-backend"), ("expected_binding_generation", -1),
    ("document_path", "other.md"), ("client_instance_id", "foreign-client"),
    ("sid", "foreign-sid"), ("version", 9), ("revision", -1),
])
@pytest.mark.parametrize("confirm", [False, True])
async def test_exact_editor_tuple_never_adopts_latest_or_foreign_values(room, field, bad, confirm):
    session = add_editor(room)
    await joined(room, session)
    body = captured(room, session)
    extra = {}
    if confirm:
        ticket = room.materialization(session.editor_id, **body)
        extra = {"proof_id": ticket["proof_id"], "content": ORIGINAL}
    body[field] = bad
    rejects(lambda: room.materialization(session.editor_id, **body, **extra))
    assert not room.docs.transitions._editors[session.editor_id].confirmed


@pytest.mark.parametrize("change", ["dirty", "unversioned", "reused-version"])
async def test_dirty_announcement_cannot_launder_clean_join_or_reuse_version(room, change):
    session = add_editor(room)
    await joined(room, session)
    gate = room.docs.transitions
    if change == "reused-version":
        prove(room, session)
    gate.heartbeat_editor(session.editor_id, gate.backend_epoch, 0, sid=session.sid,
                          dirty=True, version=1 if change == "dirty" else
                          (0 if change == "reused-version" else None))
    rejects(lambda: prove(room, session))
    assert gate._editors[session.editor_id].dirty
    if change != "dirty":
        assert gate._editors[session.editor_id].minimum_save_version == 1


@pytest.mark.parametrize("phase", ["ticket", "confirmed"])
@pytest.mark.parametrize("event", ["update", "outgoing-update", "outgoing-seed", "reset", "disconnect", "rejoin", "seed-race"])
async def test_observed_room_activity_invalidates_tickets_and_receipts(room, phase, event):
    session = add_editor(room)
    await joined(room, session)
    body = captured(room, session)
    ticket = room.materialization(session.editor_id, **body)
    if phase == "confirmed":
        row = room.materialization(session.editor_id, **body,
                                   proof_id=ticket["proof_id"], content=ORIGINAL)
    if event == "disconnect":
        await room._disconnected()
    elif event == "rejoin":
        await joined(room, session, dirty=True)
    elif event == "seed-race":
        await room._to_browser({"t": "seeded", "sid": session.sid, "ok": False})
    elif event.startswith("outgoing-"):
        await room._send({"t": event.removeprefix("outgoing-"),
                          "sid": session.sid, "update": "new-opaque-yjs"})
    else:
        await room._to_browser({"t": event, "sid": session.sid, "update": "opaque-yjs"})
    if phase == "ticket":
        rejects(lambda: room.materialization(session.editor_id, **captured(room, session),
                proof_id=ticket["proof_id"], content=ORIGINAL))
    else:
        rejects(lambda: room.docs.transitions.close_editor(session.editor_id,
                body["backend_epoch"], 0, sid=session.sid, version=body["version"],
                dirty=False, last_saved_version=row["last_saved_version"]))


@pytest.mark.parametrize("bad", ["missing-epoch", "missing-rev", "wrong-rev", "unknown-dirty", "dirty", "seed-race"])
async def test_incomplete_or_unknown_join_evidence_is_not_clean_room_proof(room, bad):
    session = add_editor(room)
    fields = {"epoch": "room-epoch", "rev": 7, "dirty": False, "seeded": True}
    if bad.startswith("missing-"):
        fields.pop(bad.removeprefix("missing-"))
    elif bad == "wrong-rev":
        fields["rev"] = 6
    elif bad == "unknown-dirty":
        fields.pop("dirty")
    elif bad == "dirty":
        fields["dirty"] = True
    else:
        fields["seeded"] = False
    await room._to_browser({"t": "joined", "sid": session.sid, **fields})
    if bad == "seed-race":
        await room._to_browser({"t": "seeded", "sid": session.sid, "ok": False})
    rejects(lambda: prove(room, session), "pending_edits")


@pytest.mark.parametrize("busy", ["save", "foreign-save", "overlap", "admission", "snapshot"])
async def test_inflight_overlapping_mutations_and_snapshot_are_never_proof(room, busy):
    session = add_editor(room)
    await joined(room, session)
    gate = room.docs.transitions
    saves = []
    admission = None
    try:
        if busy in {"save", "overlap", "foreign-save"}:
            target = add_editor(room, "foreign", "sid-foreign") if busy == "foreign-save" else session
            saves.append(gate.begin_editor_save(target.editor_id, gate.backend_epoch, 0,
                                                sid=target.sid, version=0))
            if busy == "overlap":
                saves.append(gate.begin_editor_save(target.editor_id, gate.backend_epoch, 0,
                                                    sid=target.sid, version=0))
        elif busy == "admission":
            admission = await gate.admit(0)
        else:
            async with gate.final_snapshot():
                rejects(lambda: prove(room, session))
            return
        rejects(lambda: prove(room, session))
    finally:
        for save in saves:
            gate.fail_editor_save(save)
        if admission is not None:
            admission.release()


@pytest.mark.parametrize("phase", ["ticket", "confirm", "close"])
async def test_expired_lease_never_revived_by_materialization(room, monkeypatch, phase):
    session = add_editor(room)
    await joined(room, session)
    body = captured(room, session)
    if phase != "ticket":
        ticket = room.materialization(session.editor_id, **body)
    if phase == "close":
        row = room.materialization(session.editor_id, **body,
                                   proof_id=ticket["proof_id"], content=ORIGINAL)
    gate = room.docs.transitions
    monkeypatch.setattr(gate, "_monotonic", lambda: gate._editors[session.editor_id].deadline)
    if phase == "close":
        rejects(lambda: gate.close_editor(session.editor_id, body["backend_epoch"], 0,
                sid=session.sid, version=body["version"], dirty=False,
                last_saved_version=row["last_saved_version"]), "editor_lease_expired")
    else:
        extra = {} if phase == "ticket" else {"proof_id": ticket["proof_id"], "content": ORIGINAL}
        rejects(lambda: room.materialization(session.editor_id, **body, **extra), "editor_lease_expired")


@pytest.mark.parametrize("bad", ["wrong-content", "foreign-token", "consumed-token", "replaced-token"])
async def test_proof_tokens_are_one_shot_and_bind_full_materialized_bytes(room, bad):
    session = add_editor(room)
    await joined(room, session)
    body = captured(room, session)
    ticket = room.materialization(session.editor_id, **body)
    token, content = ticket["proof_id"], ORIGINAL
    if bad == "wrong-content":
        content += "not shown in this editor"
    elif bad == "foreign-token":
        token = "foreign-context-ticket"
    elif bad == "consumed-token":
        room.materialization(session.editor_id, **body, proof_id=token, content=content)
    else:
        replacement = room.materialization(session.editor_id, **body)
        assert replacement["proof_id"] != token
    rejects(lambda: room.materialization(session.editor_id, **body, proof_id=token, content=content), "pending_edits")
    # A refused confirm must not leave an old ticket retryable.
    rejects(lambda: room.materialization(session.editor_id, **body,
            proof_id=ticket["proof_id"], content=ORIGINAL), "pending_edits")


@pytest.mark.parametrize("race", ["update", "foreign-ack", "writer-dirty", "overlap"])
async def test_live_ack_candidate_needs_exact_pending_and_unchanged_successful_writer(room, race):
    writer, peer = await dirty_peers(room)
    gate = room.docs.transitions
    row, frame = await live_save(room, writer)
    if race == "update":
        await room._to_browser({"t": "update", "sid": peer.sid, "update": "after-save"})
    elif race == "writer-dirty":
        gate.heartbeat_editor(writer.editor_id, gate.backend_epoch, 0,
                              sid=writer.sid, dirty=True, version=2)
    elif race == "overlap":
        first = gate.begin_editor_save(writer.editor_id, gate.backend_epoch, 0, sid=writer.sid, version=2)
        second = gate.begin_editor_save(writer.editor_id, gate.backend_epoch, 0, sid=writer.sid, version=3)
        gate.record_saved(second)
        gate.record_saved(first)
    if race == "foreign-ack":
        room._on_ack({"t": "ack", "id": frame["id"] + 99, "rev": 8})
    else:
        ack_live(room, frame)
    rejects(lambda: prove(room, peer, FINAL), "pending_edits")
    assert gate._editors[peer.editor_id].last_saved_version is None


async def test_peer_cannot_close_using_writers_receipt_without_own_materialization(room):
    writer, peer = await dirty_peers(room)
    row, frame = await live_save(room, writer)
    ack_live(room, frame)
    gate = room.docs.transitions
    rejects(lambda: gate.close_editor(peer.editor_id, gate.backend_epoch, 0, sid=peer.sid,
            version=1, dirty=False, last_saved_version=row["last_saved_version"]), "pending_edits")
    assert prove(room, peer, FINAL)["last_saved_version"] != row["last_saved_version"]


def coordinator(sync, *, before_drain=None):
    gate = sync.docs.transitions
    started = asyncio.Event()
    async def event(_):
        started.set()
    async def drain(change_id, timeout):
        if before_drain is not None:
            await before_drain()
        proof = await sync.drain_binding_change(change_id, timeout)
        return {"drained": sync.validate_binding_drain(proof, change_id),
                "fenced": proof.fenced, "reason": proof.reason}
    gate.set_change_callbacks(begin_quiesce=sync.begin_binding_quiesce, drain=drain,
                              cancel_quiesce=sync.cancel_binding_quiesce,
                              fence_status=lambda: sync.binding_fenced, event=event)
    sync._binding_evidence_sink = lambda _: None
    prepared = gate.prepare_change({"backend_epoch": gate.backend_epoch,
                                    "binding_generation": gate.generation})
    return prepared["change_id"], started


async def test_peer_own_materialization_and_both_acks_allow_real_room_drain(room):
    writer, peer = await dirty_peers(room)
    row, frame = await live_save(room, writer)
    ack_live(room, frame)
    change_id, started = coordinator(room)
    gate = room.docs.transitions
    task = asyncio.create_task(gate.quiesce_change(change_id, 0, timeout=1))
    ws = room._ws
    try:
        await asyncio.wait_for(started.wait(), .5)
        gate.editor_ack(change_id, writer.editor_id, 0, row["last_saved_version"])
        rejects(lambda: gate.editor_ack(change_id, peer.editor_id, 0,
                row["last_saved_version"]), "invalid_editor_ack")
        assert not gate.room_drain_ready(peer.sid, PATH, peer.editor_id, 0)
        peer_row = prove(room, peer, FINAL)
        gate.editor_ack(change_id, peer.editor_id, 0, peer_row["last_saved_version"])
        assert gate.room_drain_ready(peer.sid, PATH, peer.editor_id, 0)
        result = await asyncio.wait_for(task, 1)
        assert result["state"] == "ready", result
        assert result["fenced"] and gate.generation == 1
        assert {frame["sid"] for frame in ws.sent if frame["t"] == "leave"} == {writer.sid, peer.sid}
        assert ws.closed and not room._sessions
        assert room.docs.resolve(PATH).read_text(encoding="utf-8") == FINAL
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("race", ["disk", "update"])
async def test_post_ack_pre_drain_race_never_destructively_leaves_room(room, race):
    session = add_editor(room)
    await joined(room, session)
    row = prove(room, session)
    async def change_after_ack():
        if race == "disk":
            room.docs.resolve(PATH).write_text("# Changed after ACK\n", encoding="utf-8")
        else:
            await room._to_browser({"t": "update", "sid": session.sid, "update": "after-ack"})
    change_id, started = coordinator(room, before_drain=change_after_ack)
    gate = room.docs.transitions
    ws = room._ws
    task = asyncio.create_task(gate.quiesce_change(change_id, 0, timeout=.1))
    try:
        await asyncio.wait_for(started.wait(), .5)
        gate.editor_ack(change_id, session.editor_id, 0, row["last_saved_version"])
        result = await asyncio.wait_for(task, .5)
        assert result["state"] == "blocked", result
        assert not result["fenced"], result
        assert not ws.closed and not session.websocket.closed
        assert session.sid in room._sessions
        assert not any(frame["t"] == "leave" for frame in ws.sent)
        assert gate.generation == 0
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_unknown_seed_race_cannot_restore_old_clean_join_with_late_success(room):
    session = add_editor(room)
    await joined(room, session, seeded=False)
    await room._send({"t": "seed", "sid": session.sid, "update": "competing-seed"})
    await room._to_browser({"t": "seeded", "sid": session.sid, "ok": False})
    rejects(lambda: prove(room, session), "pending_edits")
    # A late/duplicate result is not a fresh observation of the winning room.
    await room._to_browser({"t": "seeded", "sid": session.sid, "ok": True})
    rejects(lambda: prove(room, session), "pending_edits")


@pytest.mark.parametrize("when", ["before-seed", "after-seed"])
async def test_seed_handshake_cannot_launder_unversioned_dirty_announcement(room, when):
    session = add_editor(room)
    await joined(room, session, seeded=False)
    gate = room.docs.transitions
    if when == "before-seed":
        gate.heartbeat_editor(session.editor_id, gate.backend_epoch, 0, sid=session.sid,
                              version=0, dirty=True)
    await room._send({"t": "seed", "sid": session.sid, "update": "initial-seed"})
    if when == "after-seed":
        gate.heartbeat_editor(session.editor_id, gate.backend_epoch, 0, sid=session.sid,
                              version=0, dirty=True)
    await room._to_browser({"t": "seeded", "sid": session.sid, "ok": True})
    rejects(lambda: prove(room, session), "pending_edits")


async def test_outgoing_update_waiting_for_send_lock_is_already_unconfirmed(room):
    session = add_editor(room)
    await joined(room, session)
    body = captured(room, session)
    ticket = room.materialization(session.editor_id, **body)
    await room._send_lock.acquire()
    task = asyncio.create_task(room._send({"t": "update", "sid": session.sid, "update": "queued"}))
    try:
        await asyncio.sleep(0)
        rejects(lambda: room.materialization(session.editor_id, **captured(room, session),
                proof_id=ticket["proof_id"], content=ORIGINAL))
        rejects(lambda: prove(room, session), "pending_edits")
    finally:
        room._send_lock.release()
        await task


async def test_generic_remote_change_is_not_peer_room_save_evidence(room):
    session = add_editor(room)
    await joined(room, session)
    prove(room, session)
    # Even identical bytes and a newer agreed revision lack room provenance.
    await room._on_change({"t": "change", "path": PATH, "rev": 8,
                           "hash": content_hash(ORIGINAL), "content": ORIGINAL, "deleted": False})
    rejects(lambda: prove(room, session), "pending_edits")


async def test_peer_final_save_during_quiesce_then_own_proof_and_ack(room):
    writer, peer = await dirty_peers(room)
    change_id, started = coordinator(room)
    gate = room.docs.transitions
    task = asyncio.create_task(gate.quiesce_change(change_id, 0, timeout=1))
    try:
        await asyncio.wait_for(started.wait(), .5)
        row, frame = await live_save(room, writer)
        rejects(lambda: prove(room, peer, FINAL), "pending_edits")
        ack_live(room, frame)
        peer_row = prove(room, peer, FINAL)
        # A writer can also re-prove the same snapshot; this is not another
        # Markdown save and must not invalidate the peer's evidence.
        writer_row = prove(room, writer, FINAL)
        assert writer_row["last_saved_version"] != row["last_saved_version"]
        gate.editor_ack(change_id, peer.editor_id, 0, peer_row["last_saved_version"])
        gate.editor_ack(change_id, writer.editor_id, 0, writer_row["last_saved_version"])
        result = await asyncio.wait_for(task, 1)
        assert result["state"] == "ready" and not result["missing_acks"]
        assert room.docs.resolve(PATH).read_text(encoding="utf-8") == FINAL
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_room_race_in_last_evidence_publication_is_blocked_before_fence(room):
    session = add_editor(room)
    await joined(room, session)
    row = prove(room, session)
    change_id, started = coordinator(room)
    gate = room.docs.transitions
    ws = room._ws
    room._binding_evidence_sink = lambda _: room._room_activity(PATH)
    task = asyncio.create_task(gate.quiesce_change(change_id, 0, timeout=.2))
    try:
        await asyncio.wait_for(started.wait(), .5)
        gate.editor_ack(change_id, session.editor_id, 0, row["last_saved_version"])
        result = await asyncio.wait_for(task, .5)
        assert result["state"] == "blocked" and not result["fenced"]
        assert not ws.closed and session.sid in room._sessions
        assert not any(frame["t"] == "leave" for frame in ws.sent)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancel_before_fence_preserves_clean_proof_and_rejects_late_old_generation(room):
    session = add_editor(room)
    await joined(room, session)
    body = captured(room, session)
    row = prove(room, session)
    change_id, started = coordinator(room)
    gate = room.docs.transitions
    task = asyncio.create_task(gate.quiesce_change(change_id, 0, timeout=1))
    await asyncio.wait_for(started.wait(), .5)
    cancelled = gate.cancel_change(change_id)
    await task
    assert cancelled["state"] == "cancelled" and not cancelled["fenced"]
    assert gate.generation == 0 and not room._binding_quiescing
    assert not session.websocket.closed and session.sid in room._sessions
    # The retained proof remains closable after a non-destructive cancel.
    gate.close_editor(session.editor_id, body["backend_epoch"], 0, sid=session.sid,
                      version=0, dirty=False, last_saved_version=row["last_saved_version"])
    async with gate.final_snapshot() as snapshot:
        snapshot.advance_generation()
    rejects(lambda: room.materialization(session.editor_id, **body), "stale_binding_generation")
    assert room.docs.resolve(PATH).read_text(encoding="utf-8") == ORIGINAL


async def http_room(remote_http):
    app, http, a, b, ca, cb = remote_http
    await ca.docs.write(PATH, ORIGINAL)
    connect(ca.sync, ca.paths.data / "synthetic-checkpoint.json")
    ca.docs.transitions.activate()
    session = add_editor(ca.sync)
    await joined(ca.sync, session)
    return app, http, a, b, ca, cb, session


async def test_http_materialization_project_bound_revision_dto_no_write_no_activation(remote_http, monkeypatch):
    app, http, a, b, ca, cb, session = await http_room(remote_http)
    body = captured(ca.sync, session)
    url = p(a, f"remote/editors/{session.editor_id}/materialization")
    # The globally selected project is B; immutable A routing must still own it.
    from voxarium_studio.core.workspace import open_project
    await open_project(app.state, cb.paths.root)
    assert app.state.ctx is cb
    writes = AsyncMock(side_effect=AssertionError("materialization must not write"))
    monkeypatch.setattr(ca.docs, "write", writes)
    before = ca.docs.resolve(PATH).read_bytes(), ca.bus.current_seq, ca.paths.config_file.read_bytes()
    ticket = await http.post(url, json=body)
    assert ticket.status_code == 200, ticket.text
    assert ticket.headers["cache-control"] == "no-store"
    assert ticket.json()["revision"] == body["revision"]
    confirmed = await http.post(url + "/confirm", json={**body,
                                "proof_id": ticket.json()["proof_id"], "content": ORIGINAL})
    assert confirmed.status_code == 200, confirmed.text
    own = confirmed.json()
    assert own["editor_id"] == session.editor_id and own["confirmed"] and not own["dirty"]
    assert type(own["last_saved_version"]) is int
    assert confirmed.headers["cache-control"] == "no-store"
    assert (ca.docs.resolve(PATH).read_bytes(), ca.bus.current_seq, ca.paths.config_file.read_bytes()) == before
    assert not cb.docs.transitions.activated and not cb.docs.list_docs()
    assert not ca.sync._ws.sent
    writes.assert_not_called()


async def test_http_live_scribe_save_real_docstore_ack_and_peer_materialization(remote_http):
    app, http, a, b, ca, cb, writer = await http_room(remote_http)
    sync = ca.sync
    peer = add_editor(sync, "browser-b", "sid-b")
    await joined(sync, peer, dirty=True)
    gate = ca.docs.transitions
    gate.heartbeat_editor(peer.editor_id, gate.backend_epoch, 0,
                          sid=peer.sid, dirty=True, version=1)
    seq = ca.bus.current_seq
    saved = await http.put(p(a, "docs/" + PATH), json={
        **fence(ca), "editor_id": writer.editor_id, "client_instance_id": writer.client_instance_id,
        "sid": writer.sid, "version": 1, "content": FINAL,
    })
    assert saved.status_code == 200, saved.text
    assert ca.docs.resolve(PATH).read_text(encoding="utf-8") == FINAL
    assert ca.bus.current_seq > seq
    await sync._push_local()
    frame = sync._ws.sent[-1]
    assert frame["live"] == writer.sid and frame["content"] == FINAL
    ack_live(sync, frame)
    body = captured(sync, peer)
    url = p(a, f"remote/editors/{peer.editor_id}/materialization")
    ticket = await http.post(url, json=body)
    assert ticket.status_code == 200, ticket.text
    before = ca.bus.current_seq
    confirmed = await http.post(url + "/confirm", json={**body,
                                "proof_id": ticket.json()["proof_id"], "content": FINAL})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed"] and not confirmed.json()["dirty"]
    assert confirmed.json()["last_saved_version"] != saved.json()["last_saved_version"]
    assert ca.bus.current_seq == before


@pytest.mark.parametrize("bad", ["root", "bool-version", "missing-revision", "unknown-editor", "foreign-root", "foreign-token"])
async def test_http_materialization_schema_scope_and_foreign_capabilities(remote_http, bad):
    app, http, a, b, ca, cb, session = await http_room(remote_http)
    body = captured(ca.sync, session)
    url = p(a, f"remote/editors/{session.editor_id}/materialization")
    status = 409
    if bad == "root":
        body["root"] = str(cb.paths.root)
        status = 422
    elif bad == "bool-version":
        body["version"] = True
        status = 422
    elif bad == "missing-revision":
        body.pop("revision")
        status = 422
    elif bad == "unknown-editor":
        url = p(a, "remote/editors/foreign-editor/materialization")
    elif bad == "foreign-root":
        url = p(b, f"remote/editors/{session.editor_id}/materialization")
        body.update(fence(cb))
    else:
        ticket = await http.post(url, json=body)
        assert ticket.status_code == 200, ticket.text
        url += "/confirm"
        body.update(proof_id="foreign-proof", content=ORIGINAL)
    response = await http.post(url, json=body)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert ca.docs.transitions._editors[session.editor_id].last_saved_version is None
    assert not ca.sync._ws.sent and not cb.docs.list_docs()
