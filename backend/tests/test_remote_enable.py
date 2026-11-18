"""I12: the separate Enable/catch-up command against the installed Remote v2.

remote-identity/runtime-integration-plan §4 I12; studio-design §5.2/§6/§7.7.
The Studio app, its catalog owner, the production CatalogHttpTransport, the v2
runner and the Remote are real: catalog GETs reach the Remote app over ASGI,
and the runner's WebSocket reaches the SAME app served by uvicorn on an
ephemeral loopback port. A missing Remote is a failed prerequisite, never a
skip. A backend restart is a new app + AppContext on the same disk and home.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from test_remote_catalog_activation import KEY_A, KEY_B, REAL, RealRemote, secret_free, seed
from voxarium_studio import main
from voxarium_studio.api.sync import RemoteEnable, enable_remote_binding
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.sync import catalog_runtime
from voxarium_studio.sync import service as sync_service
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.protocol import CLOSE_COLLAB_UNAVAILABLE
from voxarium_studio.sync.state import SyncNamespace, SyncStateV2


class Crash(BaseException):
    """The process dies here: no except/cleanup of the command runs (finally does)."""


async def eventually(check, *, timeout=10.0, what=""):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError(f"Timed out waiting for {what or check}")
        await asyncio.sleep(0.01)


@asynccontextmanager
async def enable_studio(project, monkeypatch, home, documents=(("a.md", "# A\n"),)):
    """Selected (disabled) project + a real Remote reachable over HTTP and WS."""
    import uvicorn
    from voxarium_studio.core.context import AppContext
    from voxarium_studio.explorer.watcher import FileWatcher

    monkeypatch.setattr(FileWatcher, "start", lambda self: None)
    monkeypatch.setattr(AppContext, "_start_plugin_hooks", lambda self: None)
    monkeypatch.setattr(sync_service, "PUSH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.02)
    connects: list[str] = []
    real_connect = sync_service.connect

    def counting(address, *args, **kwargs):
        connects.append(address)
        return real_connect(address, *args, **kwargs)

    monkeypatch.setattr(sync_service, "connect", counting)
    config = ProjectPaths.for_project(project).config_file
    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    raw["project_identity"] = {"id": "i9-local-id", "revision": 7}
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    s = RealRemote(monkeypatch, home)
    # A fresh owned transport per application lifespan: a restart closes the old one.
    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport",
                        lambda: CatalogHttpTransport(transport=httpx.ASGITransport(app=s.app)))
    server = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=0, log_level="warning",
                                           lifespan="off"))
    serving = asyncio.create_task(server.serve())
    await eventually(lambda: server.started, what="the remote to listen")
    s.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    s.row = {**REAL, "url": s.url}
    seed(home, rows=(s.row,))
    s.selected = s.project("selected-id", "Selected", documents)
    s.root, s.connects, s.stack = project, connects, None

    async def open_app():
        stack = AsyncExitStack()
        app = main.create_app(str(project))
        await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", trust_env=False))
        scope = (await http.post("/api/projects/open", json={"path": str(project)})).json()
        s.app_under_test, s.http, s.scope, s.stack = app, http, scope, stack
        s.ctx = app.state.project_registry[scope["project_id"]].ctx
        s.base = f"/api/projects/{scope['project_id']}/remote"
        s.docs_dir = Path(s.ctx.docs.directory)

    async def restart():
        await s.stack.aclose()
        s.stack = None
        await open_app()

    s.restart = restart
    try:
        await open_app()
        yield s
    finally:
        if s.stack is not None:
            await s.stack.aclose()
        server.should_exit = True
        await serving
        await s.runtime.close()
        s.store.close()


async def select(s, ctx=None, base=None):
    """I6/I9 Select commit of the real catalog project: disabled + selected (R8)."""
    ctx, base = ctx or s.ctx, base or s.base
    observation = (await s.http.get(base)).json()
    response = await s.http.post(base + "/prepare", json={
        "expected_revision": observation["identity_revision"],
        "config_fingerprint": observation["config_fingerprint"],
        "persistent_id": "selected-id", "operation": "select", "profile_id": s.row["profile_id"],
        "url": s.url, "catalog_id": "selected-id", "catalog_instance_id": s.selected.storage_epoch})
    assert response.status_code == 200, response.text
    prepared = response.json()
    change = f"{base}/changes/{prepared['change_id']}"
    gate = ctx.docs.transitions
    response = await s.http.post(change + "/quiesce", json={"expected_generation": gate.generation})
    assert response.status_code == 200 and response.json()["state"] == "ready", response.text
    observed = gate.expected_change_evidence(prepared["change_id"])["observation"]
    return change + "/commit", {
        "expected_revision": observed["identity_revision"], "expected_generation": gate.generation,
        "config_fingerprint": observed["config_fingerprint"],
        "confirmation": {"url": prepared["to"]["url"], "persistent_id": "selected-id"}}


async def selected(s):
    url, body = await select(s)
    response = await s.http.post(url, json=body)
    assert response.status_code == 200, response.text


async def preview(s):
    observation = (await s.http.get(s.base)).json()
    response = await s.http.post(s.base + "/reconcile-preview", json={
        "expected_revision": observation["identity_revision"],
        "config_fingerprint": observation["config_fingerprint"]})
    assert response.status_code == 200, response.text
    return response.json()


async def enable_body(s, shown=None):
    shown = shown or await preview(s)
    observation = (await s.http.get(s.base)).json()
    return {"expected_revision": observation["identity_revision"],
            "expected_generation": observation["binding_generation"],
            "preview_id": shown["preview_id"], "fingerprint": shown["fingerprint"],
            "confirmation": {"url": s.url, "persistent_id": "selected-id", "action": "reconcile"}}


async def enable(s, body):
    return await s.http.post(s.base + "/enable", json=body)


def refused(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": code, "details": {}}
    secret_free(response.text)


def local(s, path, text):
    target = s.docs_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def text(s, path):
    target = s.docs_dir / path
    return target.read_text(encoding="utf-8") if target.is_file() else None


def journals(s):
    from voxarium_studio.sync.transitions import read_binding_change_journals
    try:
        return read_binding_change_journals(s.root)
    except Exception:
        return None


def enable_rows(s):
    return [row for row in journals(s) or () if row["version"] == 3]


def binding_row(s):
    from voxarium_studio.sync.bindings import RemoteBindings
    return RemoteBindings().read().get(s.root)


def namespace(s):
    return SyncNamespace.for_project(s.root, url=s.url, persistent_id="selected-id")


def state(s):
    """Every byte this command may publish: config, private store, journals, checkpoint, docs."""
    from voxarium_studio.core.paths import user_config_dir
    from voxarium_studio.sync.bindings import bindings_file
    result = {}
    roots = [s.root / ".voxarium_studio" / "config.yaml", bindings_file(),
             user_config_dir() / "remote-changes", user_config_dir() / "sync", s.docs_dir]
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*")) if root.exists() else []
        for path in paths:
            if path.is_file():
                result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def disk_config(s):
    return yaml.safe_load((s.root / ".voxarium_studio" / "config.yaml").read_text(encoding="utf-8"))


def request_for(s):
    return SimpleNamespace(app=s.app_under_test)


# ------------------------------------------------------------------ the full path


async def test_select_preview_enable_connects_and_catches_up(project, monkeypatch, studio_home):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        s.store.put("selected-id", "both.md", "# theirs\n", s.user.id)
        s.store.put("selected-id", "remote-only.md", "# remote\n", s.user.id)
        s.store.put("selected-id", "tomb.md", "# soon gone\n", s.user.id)
        s.store.delete("selected-id", "tomb.md", s.user.id)
        await selected(s)
        local(s, "a.md", "# A\n")
        local(s, "both.md", "# mine\n")
        local(s, "tomb.md", "# kept here\n")
        local(s, "local-only.md", "# local\n")
        shown = await preview(s)
        assert shown["counts"] == {"upload": 2, "download": 1, "delete_local": 0,
                                   "delete_remote": 0, "conflict": 1, "unchanged": 1}
        before = (await s.http.get(s.base)).json()
        assert not before["enabled"] and before["identity_revision"] == 8
        assert s.connects == []
        s.calls.clear()
        response = await enable(s, await enable_body(s, shown))
        assert response.status_code == 200, response.text
        secret_free(response.text)
        result = response.json()
        assert result["enabled"] and result["identity_revision"] == 9
        assert result["persistent_id"] == "selected-id" and result["url"] == s.url
        assert result["space_instance_id"] == s.selected.storage_epoch
        assert result["binding_generation"] == before["binding_generation"] + 1
        # Enable itself reached the Remote with GETs only (snapshot recheck + Verify).
        assert s.calls and all(method == "GET" for method, _ in s.calls), s.calls
        assert ("GET", "/api/v2/me") in s.calls
        assert any(p.endswith("/snapshot") for _, p in s.calls)
        # Durable order: project R+1 enabled, private confirmed R+1, checkpoint, completed.
        assert disk_config(s)["sync"] == {"enabled": True, "url": s.url}
        assert disk_config(s)["project_identity"] == {"id": "selected-id", "revision": 9}
        row = binding_row(s)
        assert (row.selection_state, row.identity_revision, row.space_instance_id) == (
            "confirmed", 9, s.selected.storage_epoch)
        (record,) = enable_rows(s)
        assert record["phase"] == "completed"
        assert record["expected"]["account_id"] == s.user.id
        assert s.ctx.remote_verified_account(s.row["profile_id"], s.url) == s.user.id
        assert len(s.ctx.docs.transitions.reconcile_previews) == 0  # consumed by Enable
        await eventually(lambda: s.ctx.sync.live, what="connected")
        # Catch-up by the §16.3 engine: downloads, uploads, a tombstone vs a local edit
        # keeps the edit, both-moved keeps a conflict copy and pulls theirs.
        await eventually(lambda: text(s, "remote-only.md") == "# remote\n", what="download")
        await eventually(lambda: s.store.doc("selected-id", "local-only.md") is not None, what="upload")
        await eventually(lambda: (s.store.doc("selected-id", "tomb.md").deleted is False), what="tomb")
        assert s.store.doc("selected-id", "tomb.md").content == "# kept here\n"
        assert text(s, "both.md") == "# theirs\n"
        copies = [p for p in s.docs_dir.glob("both (conflict*).md")]
        assert len(copies) == 1 and copies[0].read_text(encoding="utf-8") == "# mine\n"
        assert s.store.doc("selected-id", "both.md").content == "# theirs\n"
        assert text(s, "a.md") == "# A\n"
        observed = (await s.http.get(s.base)).json()
        assert observed["state"] == "connected" and observed["enabled"] and observed["reason"] is None
        assert all("/api/v2/sync?space=selected-id" in a for a in s.connects)
        assert not any("/api/v1/" in a for a in s.connects)
        # Rebuilt from disk after a restart: the same target, the same verified account.
        await s.restart()
        await eventually(lambda: s.ctx.sync.live, what="reconnect after restart")
        assert s.ctx.remote_verified_account(s.row["profile_id"], s.url) == s.user.id


async def test_unrelated_settings_save_keeps_the_connected_target(project, monkeypatch, studio_home):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        assert (await enable(s, await enable_body(s))).status_code == 200
        await eventually(lambda: s.ctx.sync.live, what="connected")
        runner = s.ctx.sync._runner
        response = await s.http.put(f"/api/projects/{s.scope['project_id']}/settings",
                                    json={"project_name": "Renamed game"})
        assert response.status_code in {200, 204}, response.text
        local(s, "after.md", "# After\n")
        s.ctx.sync._dirty.set()
        await eventually(lambda: s.store.doc("selected-id", "after.md") is not None, what="push")
        assert s.ctx.sync.live and s.ctx.sync._runner is runner


# ------------------------------------------------------------------ admission and refusals


async def test_enable_body_and_confirmation_are_exact(project, monkeypatch, studio_home):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        body = await enable_body(s)
        before = state(s)
        for extra in ({"url": s.url}, {"enabled": True}, {"space": "x"}):
            refused(await enable(s, {**body, **extra}), 422, "invalid_request")
        bad = json.loads(json.dumps(body))
        bad["confirmation"]["action"] = "overwrite"
        refused(await enable(s, bad), 422, "invalid_request")
        bad["confirmation"] = {"url": s.url, "persistent_id": "other-id", "action": "reconcile"}
        refused(await enable(s, bad), 409, "confirmation_required")
        refused(await enable(s, {**body, "expected_revision": 7}), 409, "confirmation_required")
        refused(await enable(s, {**body, "config_fingerprint": "0" * 64}), 409, "confirmation_required")
        refused(await enable(s, {**body, "preview_id": "0" * 32}), 409, "preview_not_found")
        refused(await enable(s, {**body, "fingerprint": "0" * 64}), 409, "preview_stale")
        assert state(s) == before and s.connects == [] and s.ctx.sync._runner is None
        assert not s.ctx.config.sync.enabled


@pytest.mark.parametrize("race", ["profile", "credential", "local", "remote", "during_verify"])
async def test_change_between_preview_and_enable_is_stale_and_publishes_nothing(
        project, monkeypatch, studio_home, race):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        local(s, "a.md", "# A\n")
        body = await enable_body(s)
        if race == "profile":
            seed(s.home, rows=({**s.row, "name": "renamed", "revision": s.row["revision"] + 1},))
        elif race == "credential":
            keys = json.loads((s.home / "remote-keys.json").read_text(encoding="utf-8"))
            keys["profiles"][s.row["profile_id"]]["revision"] += 1
            (s.home / "remote-keys.json").write_text(json.dumps(keys), encoding="utf-8")
        elif race == "local":
            local(s, "a.md", "# A edited after the preview\n")
        elif race == "remote":
            s.store.put("selected-id", "b.md", "# B\n", s.user.id)
        else:
            async def hook(request):
                if request.url.path == "/api/v2/me":
                    local(s, "late.md", "# written while Enable verified\n")
            s.hook = hook
        before = state(s)
        refused(await enable(s, body), 409, "preview_stale")
        after = state(s)
        if race == "during_verify":
            # The racing writer's own file; it is what made the observation stale.
            assert after.pop(str(s.docs_dir / "late.md"))
        assert after == before, "nothing published"
        assert enable_rows(s) == [] and binding_row(s).selection_state == "selected"
        assert not namespace(s).path.exists()
        assert len(s.ctx.docs.transitions.reconcile_previews) == 0  # only that preview is gone
        assert s.connects == [] and s.ctx.sync._runner is None
        assert [c for c in s.calls if c[0] != "GET"] == []


async def test_put_sync_and_create_never_enable(project, monkeypatch, studio_home):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        before = state(s)
        for body in ({"enabled": True}, {"enabled": True, "url": s.url, "space": "selected-id"},
                     {"url": s.url}, {"token": "x"}):
            response = await s.http.put("/api/sync", json=body)
            assert response.status_code == 409, response.text
            assert response.json() == {"code": "use_remote_settings", "details": {}}
            scoped = await s.http.put(f"/api/projects/{s.scope['project_id']}/sync", json=body)
            assert scoped.status_code == 409, scoped.text
        response = await s.http.post(f"/api/remote/servers/{s.row['profile_id']}/projects", json={
            "expected_revision": s.row["revision"], "id": "created-id", "display_name": "Created",
            "operation_id": str(uuid.uuid4())})
        assert response.status_code == 201, response.text
        assert response.json()["state"] == "created"
        assert s.store.project("created-id") is not None
        # 201 replaces neither Select nor Preview/Enable: nothing local moved.
        assert state(s) == before and enable_rows(s) == []
        assert not s.ctx.config.sync.enabled and s.ctx.config.project_identity.id == "selected-id"
        assert s.connects == [] and s.ctx.sync._runner is None


# ------------------------------------------------------------------ crash/failure matrix

# (owner, phase, when). Only after "completed" may a runner ever start.
CRASH_POINTS = [
    ("journal", "enabling", "before"), ("journal", "enabling", "after"),
    ("config", None, "after"), ("journal", "config_committed", "after"),
    ("private", None, "after"), ("journal", "binding_committed", "after"),
    ("checkpoint", None, "after"), ("journal", "completed", "before"),
    ("journal", "completed", "after"),
]


def inject(mp, point, error):
    """Raise *error* at one publication boundary of the Enable command only."""
    from voxarium_studio.core import config as config_owner
    from voxarium_studio.sync import transitions
    from voxarium_studio.sync.bindings import RemoteBindings

    owner, phase, when = point
    if owner == "journal":
        original = transitions.publish_enable_journal

        def journal(root, change_id, expected, commit, current):
            if current == phase and when == "before":
                raise error
            row = original(root, change_id, expected, commit, current)
            if current == phase:
                raise error
            return row

        mp.setattr(transitions, "publish_enable_journal", journal)
    elif owner == "config":
        original_write = config_owner._write_project_document_in_transaction

        def write(paths, payload, **kwargs):
            if payload.get("sync", {}).get("enabled") is not True:
                return original_write(paths, payload, **kwargs)
            if when == "instead":
                raise error
            original_write(paths, payload, **kwargs)
            raise error

        mp.setattr(config_owner, "_write_project_document_in_transaction", write)
    elif owner == "private":
        original_save = RemoteBindings._save_in_transaction

        def save(self, record, **kwargs):
            result = original_save(self, record, **kwargs)
            if record.selection_state == "confirmed":
                raise error
            return result

        mp.setattr(RemoteBindings, "_save_in_transaction", save)
    else:
        original_state = SyncStateV2.save

        def save_state(self):
            original_state(self)
            raise error

        mp.setattr(SyncStateV2, "save", save_state)


async def assert_blocked_after_restart(s, reason):
    assert s.connects == []
    status = s.ctx.sync.status()
    assert (status["state"], status["message"]) == ("blocked", reason)
    observed = (await s.http.get(s.base)).json()
    if reason == "activation_unavailable":
        # VS-186: nothing was published, so the current (ID, R) is still the
        # completed disabled Select. GET shows that settled state as disabled;
        # the runner itself (status above) stays refused.
        assert (observed["state"], observed["reason"], observed["enabled"]) == ("disabled", None, False)
    else:
        assert observed["state"] == "blocked" and observed["reason"] == reason
    secret_free(json.dumps(observed))


@pytest.mark.parametrize("point", CRASH_POINTS, ids=lambda p: "-".join(x for x in p if x))
async def test_crash_at_every_step_restarts_old_target_blocked_or_fully_enabled(
        project, monkeypatch, studio_home, point):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        local(s, "local-only.md", "# local\n")
        old_config = disk_config(s)
        body = RemoteEnable.model_validate(await enable_body(s))
        with pytest.MonkeyPatch.context() as mp:
            inject(mp, point, Crash())
            with pytest.raises(Crash):
                await enable_remote_binding(request_for(s), s.ctx, body)
        assert s.connects == [], "no runner before a completed journal"
        crashed = state(s)
        published = {
            "config": disk_config(s)["sync"].get("enabled") is True,
            "private": binding_row(s).selection_state == "confirmed",
            "checkpoint": namespace(s).path.exists(),
        }
        await s.restart()
        index = CRASH_POINTS.index(point)
        if point == ("journal", "completed", "after"):
            # Everything was durable: the restarted backend runs the strict runner.
            await eventually(lambda: s.ctx.sync.live, what="connected after restart")
            await eventually(lambda: s.store.doc("selected-id", "local-only.md") is not None,
                             what="catch-up upload")
            assert all(published.values())
            return
        await asyncio.sleep(0.1)
        # Startup neither repaired, retried, reset nor rolled anything back.
        assert state(s) == crashed
        assert published == {"config": index >= 2, "private": index >= 4, "checkpoint": index >= 6}
        if index == 0:
            # Nothing was published or recorded: the old disabled selection remains.
            assert enable_rows(s) == [] and disk_config(s) == old_config
            await assert_blocked_after_restart(s, "activation_unavailable")
            response = await enable(s, await enable_body(s))  # an explicit new Enable works
            assert response.status_code == 200, response.text
            await eventually(lambda: s.ctx.sync.live, what="connected")
            return
        (record,) = enable_rows(s)
        assert record["phase"] != "completed"
        await assert_blocked_after_restart(s, "recovery_required")
        # Every later command refuses until an explicit recovery; nothing connects.
        response = await s.http.post(s.base + "/reconcile-preview", json={
            "expected_revision": 8, "config_fingerprint": "0" * 64})
        refused(response, 409, "recovery_required")
        await s.ctx.sync.reconfigure()
        assert s.ctx.sync.status()["message"] == "recovery_required" and s.connects == []


@pytest.mark.parametrize("point,status,code,retry", [
    (("journal", "enabling", "before"), 503, "binding_journal_unavailable", True),
    (("config", None, "instead"), 503, "config_store_unavailable", True),
    (("config", None, "after"), 409, "recovery_required", False),
    (("private", None, "after"), 409, "recovery_required", False),
    (("checkpoint", None, "after"), 409, "recovery_required", False),
    (("journal", "completed", "before"), 409, "recovery_required", False),
], ids=lambda p: "-".join(str(x) for x in p if x) if isinstance(p, tuple) else str(p))
async def test_failures_publish_nothing_or_block_without_retry(
        project, monkeypatch, studio_home, point, status, code, retry):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        before = state(s)
        body = await enable_body(s)
        with pytest.MonkeyPatch.context() as mp:
            inject(mp, point, OSError("synthetic durable fault"))
            refused(await enable(s, body), status, code)
        await asyncio.sleep(0.1)
        assert s.connects == [] and s.ctx.sync._runner is None
        if retry:
            # Proven unpublished: config/private/checkpoint untouched; an aborted
            # intent (if any) is terminal evidence, not a pending recovery.
            rows = enable_rows(s)
            assert all(row["phase"] == "aborted" for row in rows)
            assert disk_config(s)["sync"]["enabled"] is False
            assert binding_row(s).selection_state == "selected" and not namespace(s).path.exists()
            if not rows:
                assert state(s) == before
            await s.restart()
            await assert_blocked_after_restart(s, "activation_unavailable")
            response = await enable(s, await enable_body(s))
            assert response.status_code == 200, response.text
            await eventually(lambda: s.ctx.sync.live, what="connected after explicit retry")
        else:
            (record,) = enable_rows(s)
            assert record["phase"] != "completed"
            refused(await enable(s, body), 409, "recovery_required")
            await s.restart()
            await assert_blocked_after_restart(s, "recovery_required")


# ------------------------------------------------------------------ concurrency


async def test_two_enables_of_one_working_copy_have_one_winner(project, monkeypatch, studio_home):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        body = await enable_body(s)
        first, second = await asyncio.gather(enable(s, body), enable(s, body))
        outcomes = sorted([first.status_code, second.status_code])
        assert outcomes == [200, 409], (first.text, second.text)
        loser = first if first.status_code == 409 else second
        assert loser.json()["code"] in {"preview_not_found", "preview_stale", "already_enabled"}
        assert len(enable_rows(s)) == 1 and enable_rows(s)[0]["phase"] == "completed"
        await eventually(lambda: s.ctx.sync.live, what="connected")


async def test_concurrent_copies_have_exactly_one_winner(project, monkeypatch, studio_home, tmp_path):
    from voxarium_studio.core.project import init_project

    copy = tmp_path / "copy"
    copy.mkdir()
    init_project(copy)
    async with enable_studio(project, monkeypatch, studio_home) as s:
        config = ProjectPaths.for_project(copy).config_file
        raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        raw["project_identity"] = {"id": "i9-local-id", "revision": 7}
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")
        await selected(s)
        body = await enable_body(s)
        scope = (await s.http.post("/api/projects/open", json={"path": str(copy)})).json()
        other = s.app_under_test.state.project_registry[scope["project_id"]].ctx
        other_base = f"/api/projects/{scope['project_id']}/remote"
        commit_url, commit_body = await select(s, other, other_base)
        first, second = await asyncio.gather(enable(s, body), s.http.post(commit_url, json=commit_body))
        assert sorted([first.status_code, second.status_code]) == [200, 409], (first.text, second.text)
        if first.status_code == 200:
            # The original won: the copy published no row and cannot select it now.
            assert second.json()["code"] in {"duplicate_local_binding", "stale_binding"}
            from voxarium_studio.sync.bindings import RemoteBindings
            assert RemoteBindings().read().get(copy) is None
            await eventually(lambda: s.ctx.sync.live, what="connected")
        else:
            # The copy's selection won the race: the original enabled nothing.
            assert first.json()["code"] in {"duplicate_local_binding", "preview_stale"}
            assert enable_rows(s) == [] and not namespace(s).path.exists()
            assert s.connects == []


# ------------------------------------------------------------------ hello and rooms


class Browser:
    """The browser's end of /ws/collab, as the backend handler sees it."""

    def __init__(self, path, scope):
        self.incoming = asyncio.Queue()
        self.frames = asyncio.Queue()
        self.closed = None
        self.incoming.put_nowait({"t": "join", "path": path, "client_id": 7, **scope})

    async def receive_json(self):
        message = await self.incoming.get()
        if message is None:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect(1000)
        return message

    async def send_json(self, message):
        self.frames.put_nowait(message)

    async def close(self, code=1000, reason=""):
        if self.closed is None:
            self.closed = (code, reason)
            self.incoming.put_nowait(None)

    async def expect(self, kind):
        while True:
            message = await asyncio.wait_for(self.frames.get(), 5)
            if message["t"] == kind:
                return message


def open_room(s, path):
    gate = s.ctx.docs.transitions
    client = "browser-" + uuid.uuid4().hex[:8]
    lease = gate.register_editor(path, client, gate.backend_epoch, gate.generation)
    browser = Browser(path, {"editor_id": lease["editor_id"], "backend_epoch": gate.backend_epoch,
                             "expected_binding_generation": gate.generation,
                             "client_instance_id": client})
    return browser, asyncio.create_task(s.ctx.sync.serve_collab(browser))


def hold_hello(monkeypatch):
    """Hold each accepted socket before its hello is read; optionally drop it."""
    held = SimpleNamespace(release=asyncio.Event(), reached=asyncio.Event(), drop=False)
    original = sync_service.RemoteSync._converse_v2

    async def gated(self, ws, link):
        held.reached.set()
        await held.release.wait()
        if held.drop:
            held.drop = False
            raise OSError("synthetic drop before hello")  # reconnect with what is current now
        return await original(self, ws, link)

    monkeypatch.setattr(sync_service.RemoteSync, "_converse_v2", gated)
    return held


async def test_rooms_are_offered_only_once_connected(project, monkeypatch, studio_home):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        held = hold_hello(monkeypatch)
        response = await enable(s, await enable_body(s))
        assert response.status_code == 200, response.text
        await asyncio.wait_for(held.reached.wait(), 5)
        assert not s.ctx.sync.live and s.ctx.sync.status()["state"] == "connecting"
        early, task = open_room(s, "a.md")
        await task
        assert early.closed == (CLOSE_COLLAB_UNAVAILABLE, "unavailable")
        assert s.ctx.sync._sessions == {}
        held.release.set()
        await eventually(lambda: s.ctx.sync.live, what="connected")
        await eventually(lambda: text(s, "a.md") == "# A\n", what="catch-up download")
        browser, task = open_room(s, "a.md")
        joined = await browser.expect("joined")
        assert joined["content"] == "# A\n"
        browser.incoming.put_nowait(None)
        await task


@pytest.mark.parametrize("mismatch,code", [("epoch", "instance_mismatch"), ("account", "account_mismatch")])
async def test_hello_mismatch_after_enable_blocks_and_keeps_the_namespace(
        project, monkeypatch, studio_home, mismatch, code):
    from voxarium_studio_remote.auth import hash_token

    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        local(s, "mine.md", "# mine\n")
        held = hold_hello(monkeypatch)
        response = await enable(s, await enable_body(s))
        assert response.status_code == 200, response.text
        await asyncio.wait_for(held.reached.wait(), 5)
        checkpoint = namespace(s).path.read_bytes()
        seq = s.store.seq("selected-id")
        if mismatch == "epoch":
            # The operator re-created the project storage under the same ID.
            s.store._db.execute("UPDATE spaces SET storage_epoch=? WHERE id=?", ("e" * 32, "selected-id"))
        else:
            # The profile's key now authenticates ANOTHER account than Enable verified.
            s.store.add_user("Somebody else", hash_token(KEY_A))
            keys = json.loads((s.home / "remote-keys.json").read_text(encoding="utf-8"))
            keys["profiles"][s.row["profile_id"]]["token"] = KEY_A
            (s.home / "remote-keys.json").write_text(json.dumps(keys), encoding="utf-8")
        held.drop = True
        held.release.set()
        await eventually(lambda: s.ctx.sync._runner is not None and s.ctx.sync._runner.done(),
                         what="terminal stop")
        status = s.ctx.sync.status()
        assert (status["state"], status["message"]) == ("blocked", code)
        await asyncio.sleep(0.2)
        assert len(s.connects) == 2, "the refused hello is terminal: no automatic retry"
        # Old namespace/evidence kept: no fresh reset, nothing synced or pushed.
        assert namespace(s).path.read_bytes() == checkpoint
        assert s.store.seq("selected-id") == seq and s.store.doc("selected-id", "mine.md") is None
        assert text(s, "mine.md") == "# mine\n" and not s.ctx.sync._sessions
        assert (await s.http.get(s.base)).json()["reason"] == code
        secret_free(json.dumps(s.ctx.sync.status()))
        assert KEY_B not in json.dumps(enable_rows(s))


async def test_enable_survives_transient_journal_replace_refusal(project, monkeypatch, studio_home):
    """A foreign Windows handle on the previous phase's journal is not recovery_required."""
    import os
    from voxarium_studio.sync import transitions

    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        body = await enable_body(s)
        real = transitions._os_replace
        refused = set()

        def flaky(source, target):
            if "remote-changes" in str(target) and os.path.exists(target) and source not in refused:
                refused.add(source)
                error = PermissionError(13, "Access is denied", None, 5)
                if getattr(error, "winerror", None) != 5:
                    error.winerror = 5
                raise error
            return real(source, target)

        monkeypatch.setattr(transitions, "_os_replace", flaky)
        response = await enable(s, body)
        assert response.status_code == 200, response.text
        assert len(refused) == 3  # config_committed, binding_committed, completed
        (record,) = enable_rows(s)
        assert record["phase"] == "completed"
        await eventually(lambda: s.ctx.sync.live, what="connected")


# ------------------------------------------------------------------ VS-188 explicit recovery
#
# Startup and GET only classify an unresolved record; POST …/recover settles it
# after an exact confirmation of the sync-off target. Never Enable or push.


def recovery_body(observed, **extra):
    shown = observed["recovery"]
    body = {"change_id": shown["change_id"],
            "expected_revision": observed["identity_revision"] or 0,
            "config_fingerprint": observed["config_fingerprint"],
            "confirmation": {"url": shown["target"]["url"],
                             "persistent_id": shown["target"]["persistent_id"], "action": "recover"},
            "backend_epoch": observed["backend_epoch"]}
    return {**body, **extra}


async def recover(s, body):
    return await s.http.post(s.base + "/recover", json=body)


def journal_files(s):
    from voxarium_studio.core.paths import user_config_dir
    return sorted(p.name for p in (user_config_dir() / "remote-changes").rglob("*.json"))


async def crash_enable(s, point):
    body = RemoteEnable.model_validate(await enable_body(s))
    with pytest.MonkeyPatch.context() as mp:
        inject(mp, point, Crash())
        with pytest.raises(Crash):
            await enable_remote_binding(request_for(s), s.ctx, body)


# Every point that leaves an unresolved Enable record (the first publishes
# nothing, the last is fully enabled; both are covered above).
RECOVERABLE = CRASH_POINTS[1:-1]


@pytest.mark.parametrize("point", RECOVERABLE, ids=lambda p: "-".join(x for x in p if x))
async def test_every_enable_crash_point_recovers_explicitly_to_a_working_root(
        project, monkeypatch, studio_home, point):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        local(s, "local-only.md", "# local\n")
        await crash_enable(s, point)
        await s.restart()
        index = CRASH_POINTS.index(point)
        published = index >= 2  # the project config R+1 enabled is on disk
        crashed = state(s)
        observed = (await s.http.get(s.base)).json()
        assert (observed["state"], observed["reason"]) == ("blocked", "recovery_required")
        shown = observed["recovery"]
        assert shown["available"] and shown["code"] is None
        assert (shown["operation"], shown["outcome"]) == ("enable", "previous")
        assert shown["target"] == {"persistent_id": "selected-id", "url": s.url,
                                   "identity_revision": 10 if published else 8, "enabled": False}
        secret_free(json.dumps(observed))
        # GET and startup classify only: not one byte was written.
        assert state(s) == crashed and s.connects == []
        # The confirmation is the exact target; anything else writes nothing.
        body = recovery_body(observed)
        bad = json.loads(json.dumps(body))
        bad["confirmation"]["persistent_id"] = "other-id"
        refused(await recover(s, bad), 409, "confirmation_required")
        bad["confirmation"] = {**body["confirmation"], "url": s.url + "/x"}
        refused(await recover(s, bad), 409, "confirmation_required")
        refused(await recover(s, {**body, "config_fingerprint": "0" * 64}), 409, "stale_binding")
        refused(await recover(s, {**body, "change_id": "f" * 32}), 409, "unknown_change")
        for extra in ({"enabled": True}, {"preview_id": "0" * 32}):
            refused(await recover(s, {**body, **extra}), 422, "invalid_request")
        assert state(s) == crashed
        files = journal_files(s)
        response = await recover(s, body)
        assert response.status_code == 200, response.text
        secret_free(response.text)
        result = response.json()
        assert result["recovered"] and result["outcome"] == "previous"
        assert (result["persistent_id"], result["url"], result["enabled"]) == ("selected-id", s.url, False)
        assert result["identity_revision"] == shown["target"]["identity_revision"]
        assert result["state"] == "disabled" and result["reason"] is None
        # Back at the previous destination, sync off, consent never above selected.
        assert disk_config(s)["sync"] == {"enabled": False, "url": s.url}
        assert disk_config(s)["project_identity"] == {"id": "selected-id",
                                                      "revision": shown["target"]["identity_revision"]}
        row = binding_row(s)
        assert (row.selection_state, row.identity_revision, row.space_instance_id) == (
            "selected", shown["target"]["identity_revision"], s.selected.storage_epoch)
        # The journal is advanced in place and kept, never deleted.
        assert journal_files(s) == files
        (record,) = enable_rows(s)
        assert record["phase"] == ("recovered" if published else "aborted")
        assert s.connects == [] and s.ctx.sync._runner is None
        assert text(s, "local-only.md") == "# local\n"
        observed = (await s.http.get(s.base)).json()
        assert observed["state"] == "disabled" and observed["recovery"] is None
        # Repeat: idempotent receipt, nothing written again.
        settled = state(s)
        again = await recover(s, body)
        assert again.status_code == 200, again.text
        assert again.json()["identity_revision"] == result["identity_revision"]
        assert state(s) == settled
        # A restart keeps it settled and sync-off: nothing connects by itself.
        await s.restart()
        await asyncio.sleep(0.1)
        observed = (await s.http.get(s.base)).json()
        assert (observed["state"], observed["enabled"], observed["recovery"]) == ("disabled", False, None)
        assert s.connects == [] and state(s) == settled
        # Working again on the real Remote: a NEW Preview, then Enable.
        response = await enable(s, await enable_body(s))
        assert response.status_code == 200, response.text
        assert response.json()["identity_revision"] == shown["target"]["identity_revision"] + 1
        await eventually(lambda: s.ctx.sync.live, what="connected after recovery")
        await eventually(lambda: s.store.doc("selected-id", "local-only.md") is not None,
                         what="catch-up upload")
        assert s.store.doc("selected-id", "local-only.md").content == "# local\n"


@pytest.mark.parametrize("point", [
    ("config", None, "after"), ("private", None, "after"),
    ("checkpoint", None, "after"), ("journal", "completed", "before"),
], ids=lambda p: "-".join(x for x in p if x))
async def test_in_process_enable_failure_recovers_without_restart(
        project, monkeypatch, studio_home, point):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        body = await enable_body(s)
        with pytest.MonkeyPatch.context() as mp:
            inject(mp, point, OSError("synthetic durable fault"))
            refused(await enable(s, body), 409, "recovery_required")
        observed = (await s.http.get(s.base)).json()
        assert (observed["state"], observed["reason"]) == ("blocked", "recovery_required")
        generation = observed["binding_generation"]
        response = await recover(s, recovery_body(observed))
        assert response.status_code == 200, response.text
        assert response.json()["binding_generation"] == generation + 1
        assert not s.ctx.config.sync.enabled and s.ctx.config.project_identity.revision == 10
        assert s.connects == []
        response = await enable(s, await enable_body(s))
        assert response.status_code == 200, response.text
        await eventually(lambda: s.ctx.sync.live, what="connected after in-process recovery")


# A crash INSIDE the recovery itself, at each of its publications.
RECOVERY_STEPS = ["recovering", "config", "private", "recovered"]


def inject_recovery(mp, step):
    from voxarium_studio.core import config as config_owner
    from voxarium_studio.sync import transitions
    from voxarium_studio.sync.bindings import RemoteBindings

    if step in {"recovering", "recovered"}:
        original = transitions.publish_journal_recovery

        def journal(root, change_id, recovery, phase):
            row = original(root, change_id, recovery, phase)
            if phase == step:
                raise Crash()
            return row

        mp.setattr(transitions, "publish_journal_recovery", journal)
    elif step == "config":
        original_write = config_owner._write_project_document_in_transaction

        def write(paths, payload, **kwargs):
            original_write(paths, payload, **kwargs)
            raise Crash()

        mp.setattr(config_owner, "_write_project_document_in_transaction", write)
    else:
        original_save = RemoteBindings._save_in_transaction

        def save(self, record, **kwargs):
            original_save(self, record, **kwargs)
            raise Crash()

        mp.setattr(RemoteBindings, "_save_in_transaction", save)


@pytest.mark.parametrize("step", RECOVERY_STEPS)
async def test_crash_during_recovery_is_finished_by_the_same_command(
        project, monkeypatch, studio_home, step):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        await crash_enable(s, ("private", None, "after"))
        await s.restart()
        observed = (await s.http.get(s.base)).json()
        body = recovery_body(observed)
        with pytest.MonkeyPatch.context() as mp:
            inject_recovery(mp, step)
            with pytest.raises(Crash):
                await s.http.post(s.base + "/recover", json=body)
        await s.restart()
        await asyncio.sleep(0.1)
        assert s.connects == []
        observed = (await s.http.get(s.base)).json()
        if step == "recovered":
            # The terminal record was durable: settled, sync off, nothing to do.
            assert observed["recovery"] is None and observed["state"] == "disabled"
        else:
            # Still blocked; the same target is offered again (resume, not a new plan).
            assert (observed["state"], observed["reason"]) == ("blocked", "recovery_required")
            assert observed["recovery"]["target"]["identity_revision"] == 10
            (record,) = enable_rows(s)
            assert record["phase"] == "recovering"
            response = await recover(s, recovery_body(observed))
            assert response.status_code == 200, response.text
        assert disk_config(s)["project_identity"]["revision"] == 10
        assert disk_config(s)["sync"]["enabled"] is False
        assert binding_row(s).selection_state == "selected" and binding_row(s).identity_revision == 10
        (record,) = enable_rows(s)
        assert record["phase"] == "recovered"
        response = await enable(s, await enable_body(s))
        assert response.status_code == 200, response.text
        await eventually(lambda: s.ctx.sync.live, what="connected")


async def test_unresolved_record_with_foreign_config_stays_blocked(project, monkeypatch, studio_home):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        await crash_enable(s, ("private", None, "after"))
        # A manual edit/VCS checkout moved the identity to neither side of the record.
        raw = disk_config(s)
        raw["project_identity"] = {"id": "selected-id", "revision": 42}
        (s.root / ".voxarium_studio" / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
        await s.restart()
        before = state(s)
        observed = (await s.http.get(s.base)).json()
        assert (observed["state"], observed["reason"]) == ("blocked", "recovery_required")
        assert not observed["recovery"]["available"]
        assert observed["recovery"]["code"] == "recovery_conflict"
        refused(await recover(s, recovery_body(observed)), 409, "recovery_conflict")
        assert state(s) == before and s.connects == []


# ------------------------------------------------------------------ VS-188 checkpoint archive


def archive_body(s, observed, shown, **extra):
    body = {"expected_revision": observed["identity_revision"],
            "expected_generation": observed["binding_generation"],
            "preview_id": shown["preview_id"], "fingerprint": shown["fingerprint"],
            "confirmation": {"url": s.url, "persistent_id": "selected-id",
                             "action": "archive_checkpoint"},
            "config_fingerprint": observed["config_fingerprint"],
            "backend_epoch": observed["backend_epoch"]}
    return {**body, **extra}


async def archive(s, body):
    return await s.http.post(s.base + "/checkpoint/archive", json=body)


def archived(s):
    return sorted(namespace(s).path.parent.glob("state.archived-*.json"))


async def test_cursor_rollback_archive_then_new_preview_and_enable(project, monkeypatch, studio_home):
    from voxarium_studio.sync.state import Base

    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        local(s, "mine.md", "# mine\n")
        # A checkpoint of THIS incarnation that is ahead of the Remote: its
        # history went back (restored backup). Enable must never reset it.
        ahead = SyncStateV2(namespace(s), s.selected.storage_epoch, s.store.seq("selected-id") + 5,
                            {"a.md": Base(1, "0" * 64)})
        ahead.save()
        old = namespace(s).path.read_bytes()
        shown = await preview(s)
        assert shown["base"] == "cursor_rollback"
        observed = (await s.http.get(s.base)).json()
        refused(await enable(s, await enable_body(s, shown)), 409, "cursor_rollback")
        before = state(s)
        # Exact confirmation and the shown preview are required; nothing moves otherwise.
        bad = archive_body(s, observed, shown)
        bad["confirmation"]["persistent_id"] = "other-id"
        refused(await archive(s, bad), 409, "confirmation_required")
        refused(await archive(s, archive_body(s, observed, shown, fingerprint="0" * 64)), 409,
                "preview_stale")
        assert state(s) == before and archived(s) == []
        shown = await preview(s)
        observed = (await s.http.get(s.base)).json()
        response = await archive(s, archive_body(s, observed, shown))
        assert response.status_code == 200, response.text
        result = response.json()
        assert (result["archived"], result["base"], result["preview_required"]) == (
            True, "cursor_rollback", True)
        # Renamed, not deleted; Markdown untouched; the preview is consumed.
        (kept,) = archived(s)
        assert kept.read_bytes() == old and not namespace(s).path.exists()
        assert text(s, "mine.md") == "# mine\n"
        refused(await archive(s, archive_body(s, observed, shown)), 409, "preview_not_found")
        assert s.connects == [] and not s.ctx.config.sync.enabled
        shown = await preview(s)
        assert shown["base"] == "none"
        response = await enable(s, await enable_body(s, shown))
        assert response.status_code == 200, response.text
        await eventually(lambda: s.ctx.sync.live, what="connected")
        await eventually(lambda: s.store.doc("selected-id", "mine.md") is not None, what="upload")
        assert text(s, "mine.md") == "# mine\n" and kept.read_bytes() == old


async def test_archive_refuses_a_current_checkpoint_and_keeps_the_preview(project, monkeypatch, studio_home):
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        shown = await preview(s)
        assert shown["base"] == "none"
        observed = (await s.http.get(s.base)).json()
        before = state(s)
        refused(await archive(s, archive_body(s, observed, shown)), 409, "checkpoint_current")
        assert state(s) == before
        response = await enable(s, await enable_body(s, shown))  # the same preview still works
        assert response.status_code == 200, response.text


def recreate_on_remote(s, documents):
    """The operator deleted the project and created it again under the SAME ID."""
    with s.store._tx() as db:
        db.execute("DELETE FROM docs WHERE space_id=?", ("selected-id",))
        db.execute("DELETE FROM legacy_spaces WHERE project_id=?", ("selected-id",))
        db.execute("DELETE FROM spaces WHERE id=?", ("selected-id",))
    old = s.selected.storage_epoch
    s.selected = s.project("selected-id", "Selected again", documents)
    assert s.selected.storage_epoch != old
    return old


async def disable(s):
    """prepare(disable) → quiesce → commit: disabled + selected (§7)."""
    observed = (await s.http.get(s.base)).json()
    response = await s.http.post(s.base + "/prepare", json={
        "expected_revision": observed["identity_revision"],
        "config_fingerprint": observed["config_fingerprint"],
        "persistent_id": "selected-id", "operation": "disable"})
    assert response.status_code == 200, response.text
    prepared = response.json()
    change = f"{s.base}/changes/{prepared['change_id']}"
    gate = s.ctx.docs.transitions
    response = await s.http.post(change + "/quiesce", json={"expected_generation": gate.generation})
    assert response.status_code == 200 and response.json()["state"] == "ready", response.text
    response = await s.http.post(change + "/commit", json={
        "expected_revision": observed["identity_revision"], "expected_generation": gate.generation,
        "config_fingerprint": observed["config_fingerprint"],
        "confirmation": {"url": s.url, "persistent_id": "selected-id"}})
    assert response.status_code == 200, response.text


def markdown(s):
    return {str(p.relative_to(s.docs_dir)).replace("\\", "/"): p.read_text(encoding="utf-8")
            for p in s.docs_dir.rglob("*.md")}


async def test_recreated_remote_project_archive_preview_enable_loses_no_markdown(
        project, monkeypatch, studio_home):
    """Pass VS-188: same ID, new incarnation → explicit archive → Preview → Enable."""
    async with enable_studio(project, monkeypatch, studio_home) as s:
        await selected(s)
        local(s, "a.md", "# A\n")
        local(s, "kept.md", "# kept here\n")
        assert (await enable(s, await enable_body(s))).status_code == 200
        await eventually(lambda: s.ctx.sync.live, what="connected")
        await eventually(lambda: s.store.doc("selected-id", "kept.md") is not None, what="upload")
        await eventually(lambda: namespace(s).path.exists()
                         and SyncStateV2.load(s.root, url=s.url, persistent_id="selected-id").cursor
                         == s.store.seq("selected-id"), what="checkpoint caught up")
        # Offline edits while the Remote is being replaced.
        await s.stack.aclose()
        s.stack = AsyncExitStack()  # Studio is down; restart() opens a new one.
        local(s, "a.md", "# A edited here\n")
        local(s, "offline.md", "# written offline\n")
        old_epoch = recreate_on_remote(s, (("a.md", "# A recreated\n"), ("new-there.md", "# new\n")))
        before = markdown(s)
        checkpoint = namespace(s).path.read_bytes()
        s.connects.clear()
        await s.restart()
        # The runner meets another incarnation: terminal block, nothing reset or sent.
        await eventually(lambda: s.ctx.sync.status()["state"] == "blocked", what="blocked")
        assert s.ctx.sync.status()["message"] == "instance_mismatch"
        await asyncio.sleep(0.1)
        assert len(s.connects) == 1 and namespace(s).path.read_bytes() == checkpoint
        assert markdown(s) == before
        assert s.store.doc("selected-id", "offline.md") is None
        # Explicit path back: Disable, Select of the NEW incarnation, Preview.
        await disable(s)
        await selected(s)
        assert binding_row(s).space_instance_id == s.selected.storage_epoch != old_epoch
        shown = await preview(s)
        assert shown["base"] == "epoch_mismatch"
        # Enable never resets another incarnation's checkpoint by itself.
        refused(await enable(s, await enable_body(s, shown)), 409, "instance_mismatch")
        assert namespace(s).path.read_bytes() == checkpoint and markdown(s) == before
        shown = await preview(s)
        observed = (await s.http.get(s.base)).json()
        response = await archive(s, archive_body(s, observed, shown))
        assert response.status_code == 200, response.text
        assert response.json()["base"] == "epoch_mismatch"
        (kept,) = archived(s)
        assert kept.read_bytes() == checkpoint and not namespace(s).path.exists()
        assert markdown(s) == before, "archive never touches Markdown"
        refused(await enable(s, await enable_body(s, shown)), 409, "preview_not_found")
        shown = await preview(s)
        assert shown["base"] == "none"
        assert shown["counts"]["conflict"] == 1  # a.md moved on both sides, no common base
        response = await enable(s, await enable_body(s, shown))
        assert response.status_code == 200, response.text
        await eventually(lambda: s.ctx.sync.live, what="connected to the new incarnation")
        await eventually(lambda: text(s, "new-there.md") == "# new\n", what="download")
        await eventually(lambda: s.store.doc("selected-id", "offline.md") is not None, what="upload")
        await eventually(lambda: s.store.doc("selected-id", "kept.md") is not None, what="upload")
        # 0 Markdown lost: every local text survives, here and/or as a conflict copy.
        after = markdown(s)
        texts = set(after.values())
        for path, content in before.items():
            assert content in texts, f"lost local text of {path}"
        copies = [p for p in after if p.startswith("a (conflict")]
        assert len(copies) == 1 and after[copies[0]] == "# A edited here\n"
        assert after["a.md"] == "# A recreated\n"
        assert s.store.doc("selected-id", "offline.md").content == "# written offline\n"
        assert s.store.doc("selected-id", "kept.md").content == "# kept here\n"
        assert kept.read_bytes() == checkpoint
        state_now = SyncStateV2.load(s.root, url=s.url, persistent_id="selected-id")
        assert state_now.space_instance_id == s.selected.storage_epoch
