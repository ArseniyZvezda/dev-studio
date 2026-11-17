"""I4 local-only lease/HTTP boundary; no Remote transport or activation UI."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from voxarium_studio.core.context import AppContext
from voxarium_studio.core.project import init_project
from voxarium_studio.explorer.watcher import FileWatcher
from voxarium_studio.main import create_app
from voxarium_studio.sync.service import InvalidCollabSession, RemoteSync, _Session
from voxarium_studio.sync.transitions import BindingTransitions, MutationGateError, StaleBindingGeneration
from voxarium_studio.core.config import AppConfig
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.teams.docs import DocStore
from voxarium_studio.ws.bus import EventBus
from voxarium_studio.ws.routes import _editor_socket_inventory, legacy_editor_blockers


@pytest.fixture
async def remote_http(project: Path, tmp_path: Path, monkeypatch):
    # Only deliberate document events are relevant to no-side-effect assertions.
    monkeypatch.setattr(FileWatcher, "start", lambda self: None)
    monkeypatch.setattr(AppContext, "_start_plugin_hooks", lambda self: None)
    app = create_app(str(project))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as http:
            root_b = tmp_path / "second-project"
            root_b.mkdir()
            init_project(root_b)
            a = (await http.post("/api/projects/open", json={"path": str(project)})).json()
            b = (await http.post("/api/projects/open", json={"path": str(root_b)})).json()
            ca = app.state.project_registry[a["project_id"]].ctx
            cb = app.state.project_registry[b["project_id"]].ctx
            yield app, http, a, b, ca, cb


def p(scope, path="remote"):
    return f"/api/projects/{scope['project_id']}/{path}"


def fence(ctx):
    return {"backend_epoch": ctx.docs.transitions.backend_epoch,
            "expected_binding_generation": ctx.docs.transitions.generation}


async def editor(http, scope, ctx, path="guide/spec.md"):
    response = await http.post(p(scope, "remote/editors"), json={
        **fence(ctx), "document_path": path, "client_instance_id": "browser-a",
    })
    assert response.status_code == 201, response.text
    return response.json()


def error(response, code):
    assert response.status_code == 409, response.text
    assert response.json() == {"code": code, "details": {}}
    assert response.headers["cache-control"] == "no-store"


async def test_remote_metadata_registration_do_not_activate_or_write(remote_http):
    app, http, a, b, ca, cb = remote_http
    before = ca.paths.config_file.read_bytes()
    seq = ca.bus.current_seq
    response = await http.get(p(a))
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == a["project_id"] and body["epoch"] == a["epoch"]
    assert body["binding_generation"] == 0
    assert body["backend_epoch"] == ca.docs.transitions.backend_epoch
    assert response.headers["cache-control"] == "no-store"
    registered = await editor(http, a, ca)
    assert registered["last_saved_version"] is None
    assert not ca.docs.transitions.activated and not cb.docs.transitions.activated
    assert ca.paths.config_file.read_bytes() == before and ca.bus.current_seq == seq
    assert not ca.docs.list_docs()
    # No global duplicate, nor accidental /sync/remote nesting.
    assert (await http.get("/api/remote")).status_code == 404
    assert (await http.get(p(a, "sync/remote"))).status_code == 404


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("operation", ["write", "create", "delete", "move", "rename", "folder_delete", "style"])
@pytest.mark.parametrize("bad", ["missing", "stale", "epoch"])
async def test_all_markdown_http_mutations_no_write(remote_http, legacy, operation, bad):
    app, http, a, b, ca, cb = remote_http
    await ca.docs.write("guide/spec.md", "# Original\n")
    ca.docs.transitions.activate()
    fields = fence(ca)
    code = "reload_required"
    if bad == "missing":
        fields.pop("expected_binding_generation")
    elif bad == "stale":
        fields["expected_binding_generation"] = -1
        code = "stale_binding_generation"
    else:
        fields["backend_epoch"] = "previous-backend"
        code = "stale_backend_epoch"
    prefix = "/api/docs" if legacy else p(a, "docs")
    before = ca.docs.resolve("guide/spec.md").read_bytes()
    seq = ca.bus.current_seq
    if operation in {"write", "create"}:
        target = "new.md" if operation == "create" else "guide/spec.md"
        response = await http.put(f"{prefix}/{target}", json={**fields, "content": "# Rejected", "exclusive": operation == "create"})
    elif operation == "delete":
        response = await http.delete(f"{prefix}/guide/spec.md", params=fields)
    else:
        endpoint, data = {
            "move": ("move", {"source": "guide/spec.md", "target": "elsewhere.md"}),
            "rename": ("folders/rename", {"path": "guide", "name": "renamed"}),
            "folder_delete": ("folders/delete", {"path": "guide"}),
            "style": ("folders/style", {"path": "guide", "color": "#abcdef"}),
        }[operation]
        response = await http.post(f"{prefix}/{endpoint}", json={**fields, **data})
    error(response, code)
    assert ca.docs.resolve("guide/spec.md").read_bytes() == before
    assert not ca.docs.resolve("new.md").exists()
    assert not ca.docs.resolve("elsewhere.md").exists()
    assert ca.bus.current_seq == seq and ca.docs.transitions.active_mutations == 0


async def test_lease_server_receipt_heartbeat_close_and_wrong_scope(remote_http):
    app, http, a, b, ca, cb = remote_http
    ca.docs.transitions.activate()
    cb.docs.transitions.activate()
    row = await editor(http, a, ca)
    eid = row["editor_id"]
    url = p(a, f"remote/editors/{eid}")
    body = {**fence(ca), "version": 0, "dirty": False}
    heartbeat = await http.post(url + "/heartbeat", json=body)
    assert heartbeat.status_code == 200 and heartbeat.json()["dirty"] is True
    error(await http.request("DELETE", url, json=body), "pending_edits")
    error(await http.put(p(a, "docs/other.md"), json={**fence(ca), "editor_id": eid, "content": "bad"}), "editor_mismatch")
    error(await http.put(p(b, "docs/guide/spec.md"), json={**fence(cb), "editor_id": eid, "content": "bad"}), "unknown_editor")
    saved = await http.put(p(a, "docs/guide/spec.md"), json={
        **fence(ca), "editor_id": eid, "version": 1, "content": "# Saved\n",
    })
    assert saved.status_code == 200, saved.text
    receipt = saved.json()["last_saved_version"]
    assert type(receipt) is int
    error(await http.request("DELETE", url, json={**fence(ca), "last_saved_version": receipt + 1}), "pending_edits")
    closed = await http.request("DELETE", url, json={**fence(ca), "last_saved_version": receipt, "version": 1, "dirty": False})
    assert closed.status_code == 200 and closed.json()["closed"]
    error(await http.post(url + "/heartbeat", json=fence(ca)), "unknown_editor")


@pytest.mark.parametrize("activated", [False, True])
@pytest.mark.parametrize("field", ["sid", "collab_session"])
async def test_unknown_collab_sid_never_plain_save(remote_http, activated, field):
    app, http, a, b, ca, cb = remote_http
    if activated:
        ca.docs.transitions.activate()
    seq = ca.bus.current_seq
    response = await http.put(p(a, "docs/spec.md"), json={**fence(ca), field: "unknown", "content": "bad"})
    error(response, "unknown_sid")
    assert not ca.docs.resolve("spec.md").exists()
    assert not ca.sync._live_writes and ca.bus.current_seq == seq


async def test_failed_save_no_receipt_and_expired_dirty_no_write(remote_http, monkeypatch):
    app, http, a, b, ca, cb = remote_http
    ca.docs.transitions.activate()
    now = [0.0]
    monkeypatch.setattr(ca.docs.transitions, "_monotonic", lambda: now[0])
    row = await editor(http, a, ca)
    eid = row["editor_id"]
    original = ca.docs.write
    monkeypatch.setattr(ca.docs, "write", AsyncMock(side_effect=OSError("synthetic save failure")))
    result = await http.put(p(a, "docs/guide/spec.md"), json={**fence(ca), "editor_id": eid, "version": 1, "content": "not saved"})
    assert result.status_code == 500
    heartbeat = await http.post(p(a, f"remote/editors/{eid}/heartbeat"), json={**fence(ca), "dirty": False})
    assert heartbeat.json()["last_saved_version"] is None and heartbeat.json()["dirty"]
    monkeypatch.setattr(ca.docs, "write", original)
    now[0] = 1000
    error(await http.put(p(a, "docs/guide/spec.md"), json={**fence(ca), "editor_id": eid, "content": "expired"}), "editor_lease_expired")
    assert not ca.docs.resolve("guide/spec.md").exists()
    assert "expired_editors" in ca.docs.transitions.cutover_blockers()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("patch", [{"url": "https://example.invalid"}, {"space": "target"}, {"token": "synthetic-key"}, {"enabled": True}, {"enabled": False}, {}])
async def test_activated_sync_put_never_reconfigures(remote_http, monkeypatch, legacy, patch):
    app, http, a, b, ca, cb = remote_http
    ca.docs.transitions.activate()
    before = ca.paths.config_file.read_bytes()
    reconfigure = AsyncMock(side_effect=AssertionError("must not cancel/reconfigure"))
    monkeypatch.setattr(ca.sync, "reconfigure", reconfigure)
    url = "/api/sync" if legacy else p(a, "sync")
    error(await http.put(url, json=patch), "use_remote_settings")
    assert ca.paths.config_file.read_bytes() == before
    reconfigure.assert_not_called()
    assert (await http.get(url)).status_code == 200


async def test_browser_save_needs_editor_but_rest_generation_is_explicit(remote_http):
    app, http, a, b, ca, cb = remote_http
    ca.docs.transitions.activate()
    body = {**fence(ca), "content": "# Explicit REST\n"}
    error(await http.put(p(a, "docs/spec.md"), json=body, headers={"Origin": "http://127.0.0.1"}), "unknown_editor")
    response = await http.put(p(a, "docs/spec.md"), json=body)
    assert response.status_code == 200
    deleted = await http.delete(p(a, "docs/spec.md"), params=fence(ca))
    assert deleted.status_code == 200, deleted.text


@pytest.mark.parametrize("headers,status", [({"Origin": "https://evil.invalid"}, 403), ({"Host": "evil.invalid"}, 403), ({}, 401)])
async def test_remote_auth_origin_and_invalid_handle_no_store(remote_http, headers, status):
    app, http, a, b, ca, cb = remote_http
    if "Host" not in headers:
        app.state.auth.token = "synthetic-local-token"
    if status == 403 and "Host" not in headers:
        headers = {**headers, "Authorization": "Bearer synthetic-local-token"}
    response = await http.get(p(a), headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    app.state.auth.token = "synthetic-local-token"
    before = dict(app.state.project_registry)
    missing = await http.get("/api/projects/invalid-handle/remote", headers={"Authorization": "Bearer synthetic-local-token"})
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    assert app.state.project_registry == before


def test_overlapping_save_and_new_dirty_heartbeat_never_fake_clean():
    transitions = BindingTransitions()
    row = transitions.register_editor("spec.md", "browser", transitions.backend_epoch, 0)
    args = (row["editor_id"], transitions.backend_epoch, 0)
    first = transitions.begin_editor_save(*args, version=1)
    second = transitions.begin_editor_save(*args, version=2)
    assert transitions.record_saved(second)["dirty"]
    receipt = transitions.record_saved(first)
    assert receipt["dirty"]
    with pytest.raises(MutationGateError):
        transitions.close_editor(*args, dirty=False, version=2, last_saved_version=receipt["last_saved_version"])
    isolated = transitions.begin_editor_save(*args, version=3)
    clean = transitions.record_saved(isolated)
    assert not clean["dirty"]
    transitions.heartbeat_editor(*args, dirty=True, version=4)
    with pytest.raises(MutationGateError):
        transitions.close_editor(*args, dirty=False, version=4, last_saved_version=clean["last_saved_version"])


def test_unknown_socket_capability_and_close_not_activation_proof():
    transitions = BindingTransitions()
    transitions.register_client("legacy")
    assert "unknown_clients" in transitions.cutover_blockers()
    transitions.close_client("legacy")
    with pytest.raises(MutationGateError):
        transitions.activate()
    assert not transitions.activated


@pytest.mark.parametrize("invalidate", ["expiry", "disconnect", "dirty_version", "dirty_same_version"])
async def test_waiting_http_save_revalidates_lease_at_bytes(remote_http, monkeypatch, invalidate):
    app, http, a, b, ca, cb = remote_http
    transitions = ca.docs.transitions
    transitions.activate()
    now = [0.0]
    monkeypatch.setattr(transitions, "_monotonic", lambda: now[0])
    row = await editor(http, a, ca)
    body = {**fence(ca), "editor_id": row["editor_id"], "version": 1, "content": "must not reach disk"}
    if invalidate == "disconnect":
        sid = "captured-sid"
        transitions.register_collab_session(sid, "guide/spec.md", 0, editor_id=row["editor_id"])
        ca.sync._sessions[sid] = _Session(sid, "guide/spec.md", None, 1, editor_id=row["editor_id"],
                                         backend_epoch=transitions.backend_epoch)
        body["sid"] = sid
    admitted = asyncio.Event()
    original = transitions.admit

    async def observe(*args, **kwargs):
        result = await original(*args, **kwargs)
        admitted.set()
        return result

    monkeypatch.setattr(transitions, "admit", observe)
    await transitions._mutation_lock.acquire()
    pending = asyncio.create_task(http.put(p(a, "docs/guide/spec.md"), json=body))
    try:
        await asyncio.wait_for(admitted.wait(), 2)
        if invalidate == "expiry":
            now[0] = 1000.0
        elif invalidate == "disconnect":
            transitions.close_collab_session(sid)
            ca.sync._sessions.pop(sid)
        else:
            transitions.heartbeat_editor(row["editor_id"], transitions.backend_epoch, 0,
                                          dirty=True, version=2 if invalidate == "dirty_version" else 1)
    finally:
        transitions._mutation_lock.release()
    error(await pending, {"expiry": "editor_lease_expired", "disconnect": "unknown_sid"}.get(invalidate, "stale_editor_version"))
    assert not ca.docs.resolve("guide/spec.md").exists()
    assert not ca.sync._live_writes and transitions.active_mutations == 0


async def test_clean_open_initial_server_evidence_writes_zero(remote_http):
    app, http, a, b, ca, cb = remote_http
    await ca.docs.write("guide/spec.md", "# Already saved\n")
    seq = ca.bus.current_seq
    content = ca.docs.resolve("guide/spec.md").read_bytes()
    row = await editor(http, a, ca)
    assert not row["dirty"] and row["loaded_document"]["content"] == content.decode()
    closed = await http.request("DELETE", p(a, f"remote/editors/{row['editor_id']}"), json={
        **fence(ca), "version": 0, "dirty": False, "last_saved_version": row["last_saved_version"],
    })
    assert closed.status_code == 200, closed.text
    assert ca.docs.resolve("guide/spec.md").read_bytes() == content and ca.bus.current_seq == seq


async def test_save_without_captured_editor_version_rejected(remote_http):
    app, http, a, b, ca, cb = remote_http
    row = await editor(http, a, ca)
    error(await http.put(p(a, "docs/guide/spec.md"), json={
        **fence(ca), "editor_id": row["editor_id"], "content": "no version",
    }), "reload_required")
    assert not ca.docs.resolve("guide/spec.md").exists()


async def test_client_identity_and_late_generation_callback_never_adopt_latest(remote_http):
    app, http, a, b, ca, cb = remote_http
    row = await editor(http, a, ca)
    body = {**fence(ca), "editor_id": row["editor_id"], "version": 1, "content": "stale buffer"}
    error(await http.put(p(a, "docs/guide/spec.md"), json={**body, "client_instance_id": "foreign-browser"}), "editor_mismatch")
    async with ca.docs.final_snapshot() as snapshot:
        snapshot.advance_generation()
    error(await http.put(p(a, "docs/guide/spec.md"), json=body), "stale_binding_generation")
    assert not ca.docs.resolve("guide/spec.md").exists()


async def test_cancelled_http_save_has_no_server_success_evidence(remote_http, monkeypatch):
    app, http, a, b, ca, cb = remote_http
    row = await editor(http, a, ca)
    entered = asyncio.Event()

    async def waiting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ca.docs, "write", waiting)
    pending = asyncio.create_task(http.put(p(a, "docs/guide/spec.md"), json={
        **fence(ca), "editor_id": row["editor_id"], "version": 1, "content": "not confirmed",
    }))
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    lease = ca.docs.transitions._editors[row["editor_id"]]
    assert not lease.saves and lease.dirty and lease.last_saved_version is None
    assert not ca.docs.resolve("guide/spec.md").exists()


@pytest.mark.parametrize("existing_marker", [False, True])
async def test_overlapping_rejected_collab_saves_preserve_live_metadata(remote_http, monkeypatch, existing_marker):
    from voxarium_studio.sync.service import _LiveWrite

    app, http, a, b, ca, cb = remote_http
    tr = ca.docs.transitions
    tr.activate()
    now = [0.0]
    monkeypatch.setattr(tr, "_monotonic", lambda: now[0])
    row = await editor(http, a, ca)
    sid, eid = "overlapping-sid", row["editor_id"]
    tr.register_collab_session(sid, "guide/spec.md", 0, editor_id=eid)
    ca.sync._sessions[sid] = _Session(sid, "guide/spec.md", AsyncMock(), 1, editor_id=eid,
                                     backend_epoch=tr.backend_epoch)
    if existing_marker:
        ca.sync._live_writes["guide/spec.md"] = _LiveWrite("previous-confirmed-hash", sid)
    before = dict(ca.sync._live_writes)
    events = [asyncio.Event(), asyncio.Event()]
    original = tr.admit
    count = 0

    async def observe(*args, **kwargs):
        nonlocal count
        admission = await original(*args, **kwargs)
        events[count].set()
        count += 1
        return admission

    monkeypatch.setattr(tr, "admit", observe)
    await tr._mutation_lock.acquire()
    pending = []
    try:
        for version in (1, 2):
            pending.append(asyncio.create_task(http.put(p(a, "docs/guide/spec.md"), json={
                **fence(ca), "editor_id": eid, "sid": sid, "version": version,
                "content": f"rejected {version}",
            })))
            await asyncio.wait_for(events[version - 1].wait(), 2)
        now[0] = 1000.0
    finally:
        tr._mutation_lock.release()
    for response in await asyncio.gather(*pending):
        error(response, "editor_lease_expired")
    assert ca.sync._live_writes == before and not ca.docs.resolve("guide/spec.md").exists()


async def test_collab_http_success_and_disk_refusal_keep_correct_marker(remote_http):
    app, http, a, b, ca, cb = remote_http
    tr = ca.docs.transitions
    tr.activate()
    row = await editor(http, a, ca)
    sid, eid = "accepted-sid", row["editor_id"]
    tr.register_collab_session(sid, "guide/spec.md", 0, editor_id=eid)
    ca.sync._sessions[sid] = _Session(sid, "guide/spec.md", AsyncMock(), 1, editor_id=eid,
                                     backend_epoch=tr.backend_epoch)
    body = {**fence(ca), "editor_id": eid, "sid": sid, "version": 1, "content": "# Room saved\n"}
    response = await http.put(p(a, "docs/guide/spec.md"), json=body)
    assert response.status_code == 200 and type(response.json()["last_saved_version"]) is int
    marker = ca.sync._live_writes["guide/spec.md"]
    refused = await http.put(p(a, "docs/guide/spec.md"), json={**body, "version": 2, "content": "# Refused", "exclusive": True})
    assert refused.status_code == 409
    assert ca.sync._live_writes["guide/spec.md"] is marker
    assert ca.docs.read("guide/spec.md")["content"] == "# Room saved\n"


async def test_live_legacy_socket_follows_selection_and_blocks_common_gate(remote_http, tmp_path):
    from voxarium_studio.ws.routes import bind_editor_inventory

    app, http, a, b, ca, cb = remote_http
    with _editor_socket_inventory(app.state):
        for ctx in (ca, cb):
            assert "unknown_clients" in ctx.docs.transitions.cutover_blockers()
            with pytest.raises(MutationGateError):
                ctx.docs.transitions.activate()
        selected = await http.post("/api/project/open", json={"path": b["root"]})
        assert selected.status_code == 200
        third = tmp_path / "opened-while-socket-live"
        third.mkdir()
        init_project(third)
        scope = (await http.post("/api/projects/open", json={"path": str(third)})).json()
        ctx = app.state.project_registry[scope["project_id"]].ctx
        assert (await http.get(p(scope))).status_code == 200
        with pytest.raises(MutationGateError):
            ctx.docs.transitions.activate()
        assert not ctx.docs.transitions.activated
    for ctx in (ca, cb, ctx):
        # Socket close/reload is not proof that an unknown buffer reached disk.
        bind_editor_inventory(app.state, ctx)
        assert "unknown_clients" in ctx.docs.transitions.cutover_blockers()


async def test_unactivated_sync_keeps_legacy_and_unknown_fields_do_not_activate(remote_http, monkeypatch):
    app, http, a, b, ca, cb = remote_http
    reconfigure = AsyncMock(return_value={"state": "disabled"})
    monkeypatch.setattr(ca.sync, "reconfigure", reconfigure)
    response = await http.put(p(a, "sync"), json={"space": "legacy-selector", "enabled": False})
    assert response.status_code == 200 and ca.config.sync.space == "legacy-selector"
    assert reconfigure.await_count == 1 and not ca.docs.transitions.activated
    rejected = await http.put(p(a, "sync"), json={"activated": True})
    assert rejected.status_code == 422 and not ca.docs.transitions.activated


def _make_sid_sync(tmp_path):
    paths = ProjectPaths.for_project(tmp_path)
    paths.docs.mkdir(parents=True)
    bus = EventBus()
    docs = DocStore(paths, bus)
    return RemoteSync(paths, lambda: AppConfig(), docs, bus, token=lambda _: None)


def test_sid_path_editor_and_context(tmp_path):
    sync = _make_sid_sync(tmp_path / 'a')
    transitions = sync.docs.transitions
    row = transitions.register_editor('doc.md', 'browser', transitions.backend_epoch, 0)
    eid = row['editor_id']
    transitions.register_collab_session('sid', 'doc.md', 0, editor_id=eid)
    sync._sessions['sid'] = _Session('sid', 'doc.md', None, 1, editor_id=eid,
                                    backend_epoch=transitions.backend_epoch)
    for path, sid, editor in [('doc.md', 'unknown', None), ('other.md', 'sid', None),
                              ('doc.md', 'sid', 'foreign-editor')]:
        with pytest.raises(InvalidCollabSession):
            sync.note_live_write(path, sid, '# Wrong', editor_id=editor)
        assert sync._live_writes == {}
        assert not tuple(sync.paths.docs.iterdir())
    sync.note_live_write('doc.md', 'sid', '# Good', expected_binding_generation=0,
                         editor_id=eid)
    assert sync._live_writes['doc.md'].sid == 'sid'
    other = _make_sid_sync(tmp_path / 'b')
    with pytest.raises(InvalidCollabSession):
        other.note_live_write('doc.md', 'sid', '# Wrong')
    assert not tuple(other.paths.docs.iterdir())


@pytest.mark.asyncio
async def test_session_never_adopts_latest_generation(tmp_path):
    sync = _make_sid_sync(tmp_path)
    sync._sessions['sid'] = _Session('sid', 'doc.md', None, 1)
    with pytest.raises(StaleBindingGeneration):
        sync.validate_live_write('doc.md', 'sid', expected_binding_generation=1)
    async with sync.docs.transitions.final_snapshot() as fence:
        fence.advance_generation()
    with pytest.raises(StaleBindingGeneration):
        sync.note_live_write('doc.md', 'sid', '# Wrong', expected_binding_generation=1)
    assert sync._live_writes == {}


@pytest.mark.asyncio
async def test_generation_rechecked_after_send_lock(tmp_path):
    sync = _make_sid_sync(tmp_path)
    sync._sessions['sid'] = _Session('sid', 'doc.md', None, 1)
    sent = []

    class Outbound:
        async def send(self, value):
            sent.append(json.loads(value))

    sync._ws = Outbound()
    await sync._send_lock.acquire()
    task = asyncio.create_task(sync._send({'t': 'update', 'sid': 'sid', 'update': 'fake'}))
    await asyncio.sleep(0)
    async with sync.docs.transitions.final_snapshot() as fence:
        fence.advance_generation()
    sync._send_lock.release()
    with pytest.raises(StaleBindingGeneration):
        await task
    assert sent == []


def test_unknown_legacy_visibility_tracks_switch_and_new_context():
    a, b, c = object(), object(), object()
    state = SimpleNamespace(ctx=a)
    with _editor_socket_inventory(state):
        assert legacy_editor_blockers(state, a) == 1
        state.ctx = b
        assert legacy_editor_blockers(state, a) == 1
        assert legacy_editor_blockers(state, b) == 1
        # Newly created Ctx never received the initial hello or pump event.
        assert legacy_editor_blockers(state, c) == 1
        with _editor_socket_inventory(state, b):
            assert legacy_editor_blockers(state, b) == 2
            assert legacy_editor_blockers(state, a) == 1
        assert legacy_editor_blockers(state, b) == 1
    assert legacy_editor_blockers(state, a) == 0
    assert legacy_editor_blockers(state, b) == 0


def test_scoped_socket_cleanup_on_exception():
    state, a, b = SimpleNamespace(), object(), object()
    with pytest.raises(RuntimeError):
        with _editor_socket_inventory(state, a):
            assert legacy_editor_blockers(state, a) == 1
            assert legacy_editor_blockers(state, b) == 0
            raise RuntimeError('synthetic teardown')
    assert legacy_editor_blockers(state, a) == 0


@pytest.mark.parametrize('scoped', [False, True])
def test_actual_event_socket_lifetime_and_selection(tmp_path, monkeypatch, scoped):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from voxarium_studio.core.project_scope import ProjectDescriptor
    from voxarium_studio.ws import routes

    async def allow(_):
        return True

    monkeypatch.setattr(routes, 'authorise_websocket', allow)
    app = FastAPI()
    app.include_router(routes.router)
    a = SimpleNamespace(paths=SimpleNamespace(root=tmp_path / 'a'), bus=EventBus())
    b = SimpleNamespace(paths=SimpleNamespace(root=tmp_path / 'b'), bus=EventBus())
    app.state.ctx = a
    app.state.browser_bus = EventBus()
    app.state.project_registry = {'p': ProjectDescriptor('p', 'e', a.paths.root, a)}
    with TestClient(app) as http:
        with http.websocket_connect('/ws/projects/p' if scoped else '/ws') as ws:
            assert ws.receive_json()['type'] == 'system.hello'
            assert legacy_editor_blockers(app.state, a) == 1
            app.state.ctx = b
            assert legacy_editor_blockers(app.state, a) == 1
            assert legacy_editor_blockers(app.state, b) == (0 if scoped else 1)
            ws.send_json({'action': 'ping'})
            assert ws.receive_json()['type'] == 'system.pong'
    assert legacy_editor_blockers(app.state, a) == 0
    assert legacy_editor_blockers(app.state, b) == 0


class Browser:
    def __init__(self, first):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(first)
        self.closed = None

    async def receive_json(self):
        from fastapi import WebSocketDisconnect
        value = await self.incoming.get()
        if value is None:
            raise WebSocketDisconnect(1000)
        return value

    async def close(self, code=1000, reason=''):
        self.closed = code, reason

    async def send_json(self, message):
        pass


async def live_sync(tmp_path):
    from unittest.mock import AsyncMock
    from voxarium_studio.sync.protocol import content_hash
    sync = _make_sid_sync(tmp_path)
    await sync.docs.write('doc.md', '# Original\n')
    sync._state_name = 'connected'
    sync._state = SimpleNamespace(docs={'doc.md': SimpleNamespace(rev=1, hash=content_hash('# Original\n'))})
    sync._flush = AsyncMock()
    sync.sent = []
    sync.joined = asyncio.Event()

    class Outbound:
        async def send(self, value):
            message = json.loads(value)
            sync.sent.append(message)
            if message['t'] == 'join':
                sync.joined.set()

    sync._ws = Outbound()
    return sync


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing', 'epoch', 'generation', 'editor', 'path'])
async def test_activated_join_rejects_before_any_send_or_flush(tmp_path, bad):
    sync = await live_sync(tmp_path)
    gate = sync.docs.transitions
    gate.activate()
    row = gate.register_editor('doc.md', 'browser', gate.backend_epoch, 0)
    first = {'path': 'doc.md', 'client_id': 1, 'backend_epoch': gate.backend_epoch,
             'expected_binding_generation': 0, 'editor_id': row['editor_id'],
             'client_instance_id': 'browser', 'saved': True, 'capable': True}
    if bad == 'missing':
        first.pop('backend_epoch')
        first.pop('expected_binding_generation')
    elif bad == 'epoch':
        first['backend_epoch'] = 'foreign-epoch'
    elif bad == 'generation':
        first['expected_binding_generation'] = 1
    elif bad == 'editor':
        first['editor_id'] = 'foreign-editor'
    else:
        first['path'] = 'other.md'
    browser = Browser(first)
    await sync.serve_collab(browser)
    assert browser.closed is not None
    assert sync.sent == []
    assert sync._flush.await_count == 0
    assert not gate._sessions and not sync._sessions
    assert gate._editors[row['editor_id']].last_saved_version is None
    assert sync.docs.read('doc.md')['content'] == '# Original\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('activated', [False, True])
async def test_real_collab_lifecycle_registers_rejoins_and_closes_sid(tmp_path, activated):
    sync = await live_sync(tmp_path)
    gate = sync.docs.transitions
    first = {'path': 'doc.md', 'client_id': 1}
    editor = None
    if activated:
        gate.activate()
        editor = gate.register_editor('doc.md', 'browser', gate.backend_epoch, 0)['editor_id']
        first.update(backend_epoch=gate.backend_epoch, expected_binding_generation=0,
                     editor_id=editor, client_instance_id='browser')
    browser = Browser(first)
    task = asyncio.create_task(sync.serve_collab(browser))
    try:
        await asyncio.wait_for(sync.joined.wait(), 2)
        sid = sync.sent[0]['sid']
        session = sync._sessions[sid]
        assert gate.require_http(0 if activated else None,
                                 backend_epoch=gate.backend_epoch if activated else None,
                                 editor_id=editor, document_path='doc.md', sid=sid) == 0
        assert 'active_rooms' in gate.cutover_blockers()
        sync.joined.clear()
        await sync._to_browser({'t': 'join_failed', 'sid': sid, 'reason': 'stale'})
        await asyncio.wait_for(sync.joined.wait(), 2)
        assert [frame['sid'] for frame in sync.sent if frame['t'] == 'join'] == [sid, sid]
        if activated:
            assert gate._editors[editor].dirty
            assert gate._editors[editor].last_saved_version is None
        else:
            editor = gate.register_editor('doc.md', 'late-browser', gate.backend_epoch, 0,
                                          sid=sid)['editor_id']
            sync.bind_collab_editor(sid, editor)
            await sync._send({'t': 'awareness', 'sid': sid, 'update': 'synthetic'})
            assert sync.sent[-1]['t'] == 'awareness'
            assert gate._editors[editor].last_saved_version is None
    finally:
        browser.incoming.put_nowait(None)
        await asyncio.wait_for(task, 2)
    assert not sync._sessions and not gate._sessions
    sent_count = len(sync.sent)
    await sync._rejoin(session)
    assert len(sync.sent) == sent_count
    assert not sync._sessions and not gate._sessions
    assert 'active_rooms' not in gate.cutover_blockers()
    if activated:
        assert 'pending_editors' in gate.cutover_blockers()


def test_live_inventory_callback_and_new_root_tombstones(tmp_path):
    from voxarium_studio.ws.routes import bind_editor_inventory
    a = SimpleNamespace(docs=_make_sid_sync(tmp_path / 'a').docs)
    state = SimpleNamespace(ctx=a, project_contexts={'a': a})
    with _editor_socket_inventory(state):
        assert 'unknown_clients' in a.docs.transitions.cutover_blockers()
        b = SimpleNamespace(docs=_make_sid_sync(tmp_path / 'b').docs)
        state.project_contexts['b'] = b
        state.ctx = b
        bind_editor_inventory(state, b)
        assert 'unknown_clients' in b.docs.transitions.cutover_blockers()
        assert 'unknown_clients' in a.docs.transitions.cutover_blockers()
    assert legacy_editor_blockers(state, a) == 0
    assert 'unknown_clients' in a.docs.transitions.cutover_blockers()
    assert 'unknown_clients' in b.docs.transitions.cutover_blockers()
