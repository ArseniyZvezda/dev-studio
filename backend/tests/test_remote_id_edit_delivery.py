"""VS-177 offline ID-edit delivery through real Ctx/config/Markdown owners.

No Remote/runner/drain mocks: disabled startup and empty offline binding use the
production coordinator. UI/Yjs/online interop and R3/L are separate evidence.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import yaml

from voxarium_studio.core.config import load_config
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.explorer.watcher import FileWatcher
from voxarium_studio.main import create_app
from voxarium_studio.sync.transitions import MAX_BINDING_GENERATION, read_binding_change_journals


def url(p, suffix=""):
    return f"/api/projects/{p.scope['project_id']}/remote{suffix}"


def documents(p):
    return {str(path.relative_to(p.ctx.paths.docs)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in p.ctx.paths.docs.rglob("*") if path.is_file()}


@pytest.fixture
async def offline(project, monkeypatch):
    # Only peripheral watcher/plugin processes are suppressed. Sync, context,
    # lifecycle, config transactions, journal, DocStore and HTTP are all real.
    monkeypatch.setattr(FileWatcher, "start", lambda self: None)
    monkeypatch.setattr(AppContext, "_start_plugin_hooks", lambda self: None)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_URL", raising=False)
    paths = ProjectPaths.for_project(project)
    raw = yaml.safe_load(paths.config_file.read_text(encoding="utf-8"))
    raw["project_identity"] = {"id": "original-id", "revision": 7}
    raw["sync"] = {"enabled": False, "url": ""}
    paths.config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    app = create_app(str(project))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
            scope = (await http.post("/api/projects/open", json={"path": str(project)})).json()
            ctx = app.state.project_registry[scope["project_id"]].ctx
            # Exact non-ASCII/CRLF corpus makes a hidden normalization observable.
            path = ctx.docs.resolve("same.md")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes("# Привет\r\n\r\nuntouched  \r\n".encode("utf-8"))
            assert ctx.sync._runner is None and ctx.sync._ws is None
            yield SimpleNamespace(app=app, http=http, scope=scope, ctx=ctx)


async def prepare(p, target="edited-id"):
    observed = (await p.http.get(url(p))).json()
    response = await p.http.post(url(p, "/prepare"), json={
        "expected_revision": observed["identity_revision"],
        "config_fingerprint": observed["config_fingerprint"],
        "backend_epoch": observed["backend_epoch"],
        "persistent_id": target, "operation": "edit_id",
    })
    assert response.status_code == 200, response.text
    return observed, response.json()


async def ready(p, target="edited-id"):
    observed, change = await prepare(p, target)
    response = await p.http.post(url(p, f"/changes/{change['change_id']}/quiesce"),
        json={"expected_generation": observed["binding_generation"]})
    assert response.status_code == 200 and response.json()["state"] == "ready", response.text
    assert response.json()["binding_generation"] == observed["binding_generation"] + 1
    # Captured oldG+1; no latest-G read/adoption after the await.
    body = {"expected_revision": observed["identity_revision"],
        "config_fingerprint": observed["config_fingerprint"],
        "expected_generation": observed["binding_generation"] + 1,
        "confirmation": {"url": "", "persistent_id": target}}
    return observed, change, body


async def commit(p, change, body):
    return await p.http.post(url(p, f"/changes/{change['change_id']}/commit"), json=body)


def refused(response, code):
    assert response.status_code == 409, response.text
    assert response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"


async def test_offline_disabled_edit_is_safe_monotonic_metadata_only_and_clean_zero_writes(offline):
    p = offline
    ctx = p.ctx
    descriptor = p.app.state.project_registry[p.scope["project_id"]]
    runtime = (ctx.paths, ctx.jobs, ctx.docs, ctx.teams, descriptor, descriptor.epoch)
    mcp = ctx.paths.root / ".mcp.json"
    mcp_before = mcp.read_bytes() if mcp.exists() else None
    before = documents(p)
    # Actual document GET and untouched ordinary lease registration write nothing.
    opened = await p.http.get(f"/api/projects/{p.scope['project_id']}/docs/same.md")
    assert opened.status_code == 200
    observed = (await p.http.get(url(p))).json()
    lease = await p.http.post(url(p, "/editors"), json={
        "backend_epoch": observed["backend_epoch"],
        "expected_binding_generation": observed["binding_generation"],
        "document_path": "same.md", "client_instance_id": "clean-reader"})
    assert lease.status_code == 201, lease.text
    receipt = lease.json()
    observed, change = await prepare(p)
    task = asyncio.create_task(p.http.post(url(p, f"/changes/{change['change_id']}/quiesce"),
        json={"expected_generation": observed["binding_generation"]}))
    try:
        async def await_quiesce():
            while not ctx.docs.transitions.binding_quiescing:
                if task.done():
                    response = task.result()
                    pytest.fail(f"quiesce stopped before ACK: {response.status_code} {response.text}")
                await asyncio.sleep(0)
        await asyncio.wait_for(await_quiesce(), 2)
        ack = await p.http.post(url(p, f"/changes/{change['change_id']}/editor-ack"), json={
            "editor_id": receipt["editor_id"], "old_generation": observed["binding_generation"],
            "last_saved_version": receipt["last_saved_version"]})
        assert ack.status_code == 200, ack.text
        drained = await asyncio.wait_for(task, 2)
        assert drained.json()["state"] == "ready", drained.text
    finally:
        if not task.done():
            ctx.docs.transitions.cancel_change(change["change_id"])
            await asyncio.gather(task, return_exceptions=True)
    body = {"expected_revision": observed["identity_revision"],
        "config_fingerprint": observed["config_fingerprint"],
        "expected_generation": observed["binding_generation"] + 1,
        "confirmation": {"url": "", "persistent_id": "edited-id"}}
    response = await commit(p, change, body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["binding_generation"] == observed["binding_generation"] + 2
    assert 0 <= result["binding_generation"] <= MAX_BINDING_GENERATION
    assert result["backend_epoch"] == observed["backend_epoch"]
    assert result["persistent_id"] == "edited-id" and result["identity_revision"] == 8
    assert result["enabled"] is False and result["state"] == "disabled"
    assert result["project_id"] == p.scope["project_id"] and result["epoch"] == p.scope["epoch"]
    current = (await p.http.get(url(p))).json()
    assert current["binding_generation"] == result["binding_generation"]
    assert current["persistent_id"] == "edited-id"
    disk = yaml.safe_load(ctx.paths.config_file.read_bytes())
    assert disk["project_identity"] == {"id": "edited-id", "revision": 8}
    assert disk["sync"] == {"enabled": False, "url": ""}
    assert (ctx.paths, ctx.jobs, ctx.docs, ctx.teams, descriptor, descriptor.epoch) == runtime
    assert p.app.state.project_registry[p.scope["project_id"]] is descriptor
    assert (mcp.read_bytes() if mcp.exists() else None) == mcp_before
    assert ctx.sync._runner is None and ctx.sync._ws is None
    assert documents(p) == before


async def test_historical_receipt_cannot_rewind_runtime_generation(offline):
    p = offline
    _, first, body = await ready(p, "first-id")
    first_result = await commit(p, first, body)
    assert first_result.status_code == 200, first_result.text
    _, second, second_body = await ready(p, "second-id")
    second_result = await commit(p, second, second_body)
    assert second_result.status_code == 200, second_result.text
    generation = second_result.json()["binding_generation"]
    assert generation > first_result.json()["binding_generation"]
    before = (p.ctx.paths.config_file.read_bytes(), documents(p))
    retry = await commit(p, first, body)
    assert retry.status_code == 200 and retry.json() == first_result.json()
    assert p.ctx.docs.transitions.generation == generation
    assert (await p.http.get(url(p))).json()["persistent_id"] == "second-id"
    assert (p.ctx.paths.config_file.read_bytes(), documents(p)) == before


async def test_restart_never_relabels_prior_epoch_receipt_or_adopts_journal_generation(offline):
    p = offline
    observed, change, body = await ready(p)
    result = await commit(p, change, body)
    assert result.status_code == 200, result.text
    records = read_binding_change_journals(p.ctx.paths.root)
    restarted = AppContext(p.ctx.paths, load_config(p.ctx.paths), p.ctx.auth)
    old_descriptor = p.app.state.project_registry[p.scope["project_id"]]
    from dataclasses import replace
    p.app.state.project_registry[p.scope["project_id"]] = replace(old_descriptor, ctx=restarted)
    before = (p.ctx.paths.config_file.read_bytes(), documents(p))
    await restarted.sync.start()
    try:
        assert restarted.docs.transitions.generation == 0
        assert restarted.docs.transitions.backend_epoch != observed["backend_epoch"]
        refused(await commit(p, change, body), "stale_backend_epoch")
        # Even coincident numeric G=0 is fenced by the epoch after restart.
        response = await p.http.post(url(p, "/editors"), json={
            "backend_epoch": observed["backend_epoch"], "expected_binding_generation": 0,
            "document_path": "same.md", "client_instance_id": "late-old-browser"})
        refused(response, "stale_backend_epoch")
        assert restarted.docs.transitions.generation == 0
        assert restarted.sync._runner is None and restarted.sync._ws is None
        assert read_binding_change_journals(p.ctx.paths.root) == records
        assert (p.ctx.paths.config_file.read_bytes(), documents(p)) == before
    finally:
        p.app.state.project_registry[p.scope["project_id"]] = old_descriptor
        await restarted.sync.stop()


@pytest.mark.parametrize("phase", ["prepared", "ready"])
async def test_cancel_preserves_text_and_does_not_claim_rollback_after_fence(offline, phase):
    p = offline
    before = (p.ctx.paths.config_file.read_bytes(), documents(p))
    if phase == "ready":
        _, change, body = await ready(p)
    else:
        _, change = await prepare(p)
    result = await p.http.delete(url(p, f"/changes/{change['change_id']}"))
    assert result.status_code == 200, result.text
    assert result.json()["state"] == ("paused" if phase == "ready" else "cancelled")
    assert p.ctx.config.project_identity.id == "original-id"
    assert (p.ctx.paths.config_file.read_bytes(), documents(p)) == before
    if phase == "ready":
        refused(await commit(p, change, body), "change_not_ready")
        assert p.ctx.docs.transitions.generation == 1


@pytest.mark.parametrize("fault", ["confirmation", "config_cas", "disk_error"])
async def test_errors_preserve_corpus_and_disabled_identity(offline, monkeypatch, fault):
    from voxarium_studio.core import config as config_owner
    p = offline
    _, change, body = await ready(p)
    if fault == "confirmation":
        body["confirmation"]["persistent_id"] = "not-confirmed"
    elif fault == "config_cas":
        p.ctx.paths.config_file.write_bytes(p.ctx.paths.config_file.read_bytes() + b"\n# external\n")
    else:
        def failed(*args, **kwargs):
            raise OSError("synthetic disk failure")
        monkeypatch.setattr(config_owner, "_commit_disabled_identity_in_transaction", failed)
    before = (p.ctx.paths.config_file.read_bytes(), documents(p))
    response = await commit(p, change, body)
    assert response.status_code == 409, response.text
    assert p.ctx.config.project_identity.id == "original-id"
    assert p.ctx.config.sync.enabled is False
    assert (p.ctx.paths.config_file.read_bytes(), documents(p)) == before
    assert p.ctx.docs.transitions.generation == 1


@pytest.mark.parametrize("captured_generation", [0, 1])
async def test_late_captured_generation_never_saves_under_committed_target(offline, captured_generation):
    p = offline
    observed, change, body = await ready(p)
    result = await commit(p, change, body)
    assert result.status_code == 200, result.text
    before = documents(p)
    response = await p.http.put(f"/api/projects/{p.scope['project_id']}/docs/same.md", json={
        "content": "LATE MUST NOT WRITE", "backend_epoch": observed["backend_epoch"],
        "expected_binding_generation": captured_generation})
    refused(response, "stale_binding_generation")
    assert documents(p) == before
    assert p.ctx.docs.transitions.generation == observed["binding_generation"] + 2

@pytest.mark.parametrize("start", [7, MAX_BINDING_GENERATION - 2])
async def test_offline_commit_roundtrips_increasing_safe_integer_boundary(offline, start):
    p = offline
    p.ctx.docs.transitions._generation = start
    observed, change, body = await ready(p)
    assert observed["binding_generation"] == start
    response = await commit(p, change, body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["binding_generation"] == start + 2 <= MAX_BINDING_GENERATION
    assert (await p.http.get(url(p))).json()["binding_generation"] == start + 2


async def test_durable_completion_before_ram_fault_does_not_mint_success_or_adopt_G(offline, monkeypatch):
    p = offline
    _, change, body = await ready(p)
    def failed(*args, **kwargs):
        raise OSError("completion lost before RAM publication")
    monkeypatch.setattr(p.ctx.docs.transitions, "complete_commit", failed)
    refused(await commit(p, change, body), "recovery_required")
    records = read_binding_change_journals(p.ctx.paths.root)
    assert records[0]["phase"] == "completed"
    assert records[0]["commit"]["binding_generation"] == 2
    assert p.ctx.docs.transitions.generation == 1
    before = (p.ctx.paths.config_file.read_bytes(), documents(p))
    refused(await commit(p, change, body), "recovery_required")
    assert p.ctx.docs.transitions.generation == 1
    assert (p.ctx.paths.config_file.read_bytes(), documents(p)) == before


@pytest.mark.parametrize("tamper", ["generation", "epoch"])
async def test_partial_durable_retry_never_restores_foreign_fence(offline, monkeypatch, tamper):
    import json
    from voxarium_studio.sync import transitions
    p = offline
    _, change, body = await ready(p)
    publish = transitions.publish_binding_change_commit
    def failed(*args, **kwargs):
        if args[-1] == "config_committed":
            raise OSError("partial publication")
        return publish(*args, **kwargs)
    monkeypatch.setattr(transitions, "publish_binding_change_commit", failed)
    refused(await commit(p, change, body), "recovery_required")
    monkeypatch.setattr(transitions, "publish_binding_change_commit", publish)
    path = transitions.binding_change_journal_path(p.ctx.paths.root, change["change_id"])
    original = path.read_bytes()
    journal = json.loads(original)
    if tamper == "generation":
        journal["commit"]["binding_generation"] = 0
    else:
        journal["expected"]["backend_epoch"] = "prior-backend-epoch"
    path.write_text(json.dumps(journal), encoding="utf-8")
    before = (p.ctx.paths.config_file.read_bytes(), documents(p), path.read_bytes())
    try:
        refused(await commit(p, change, body),
                "stale_binding" if tamper == "generation" else "stale_backend_epoch")
        assert p.ctx.docs.transitions.generation == 1
        assert (p.ctx.paths.config_file.read_bytes(), documents(p), path.read_bytes()) == before
    finally:
        # Undo only the test's external corruption, so normal fixture shutdown
        # can retain old-target evidence under its original captured epoch.
        path.write_bytes(original)
