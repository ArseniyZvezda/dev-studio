"""VS-187 Gate L on a real Remote v2 (dev spec §16.11).

A clean personal home: create a profile, see that the first key write is refused
(503, nothing written) until the user explicitly confirms the projects using the
exact URL, then save the key and Verify against the actual ``remote/`` server.
Rotating or clearing the key of an enabled project is refused without stopping
it; after Disable it passes, and the Remote then refuses the old key (4401).

Skipped unless the remote package is installed into the backend's environment
(``uv pip install -e ../remote``), like ``test_remote_sync.py`` whose per-person
home helpers it reuses.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

remote = pytest.importorskip("voxarium_studio_remote.app")
pytest.importorskip("pycrdt")

import uvicorn  # noqa: E402
from voxarium_studio_remote.auth import hash_token, new_token  # noqa: E402
from voxarium_studio_remote.store import Store  # noqa: E402
from websockets.asyncio.client import connect as ws_connect  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from test_remote_sync import (  # noqa: E402
    I16_ID, _I16_HOME, _PersonEnviron, _PersonTransport, _i16_binding, _i16_change,
    _i16_observe, _i16_preview, _i16_settled, as_person, eventually,
)
from voxarium_studio import main as studio_main  # noqa: E402
from voxarium_studio.core.context import AppContext  # noqa: E402
from voxarium_studio.core.project import init_project  # noqa: E402
from voxarium_studio.core import workspace  # noqa: E402
from voxarium_studio.explorer.watcher import FileWatcher  # noqa: E402
from voxarium_studio.sync import catalog_runtime  # noqa: E402
from voxarium_studio.sync import service as sync_service  # noqa: E402
from voxarium_studio.sync.catalog_http import CatalogHttpTransport  # noqa: E402
from voxarium_studio.sync.protocol import CLOSE_UNAUTHORISED, normalise_url, socket_url_v2  # noqa: E402

SERVERS = "/api/remote/servers"
CONFIRMATIONS = "remote-consumer-confirmations.json"


@pytest.fixture(autouse=True)
def no_ambient_remote_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_TOKEN", raising=False)
    monkeypatch.delenv("VOXARIUM_STUDIO_REMOTE_URL", raising=False)


def home_files(home: Path) -> dict[str, str]:
    """Every byte of the personal home except the shared transaction's lock file."""
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(home.rglob("*")) if p.is_file() and p.name != "config.yaml.lock"}


@pytest.fixture
async def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    monkeypatch.setattr(os, "environ", _PersonEnviron(os.environ))
    monkeypatch.setattr(FileWatcher, "start", lambda self: None)
    monkeypatch.setattr(AppContext, "_start_plugin_hooks", lambda self: None)
    monkeypatch.setattr(sync_service, "RECONNECT_MIN_SECONDS", 0.02)
    store = Store(":memory:")
    remote_app = remote.create_app(store=store)
    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport",
                        lambda: CatalogHttpTransport(transport=httpx.ASGITransport(app=remote_app)))
    server = uvicorn.Server(uvicorn.Config(remote_app, host="127.0.0.1", port=0,
                                           log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve())
    await eventually(lambda: server.started, what="the remote to listen")
    url = normalise_url(f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}")

    key = new_token()
    user = store.add_user("Alice", hash_token(key))
    home, root = tmp_path / "home-alice", tmp_path / "alice"
    home.mkdir()
    root.mkdir()
    init_project(root)
    p = SimpleNamespace(name="alice", key=key, user=user, home=home, root=root, stack=None, row=None)

    async def open_app() -> None:
        stack = AsyncExitStack()
        with as_person(p):
            app = studio_main.create_app(str(root))
            await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(httpx.AsyncClient(
            transport=_PersonTransport(p, httpx.ASGITransport(app=app)),
            base_url="http://127.0.0.1", trust_env=False))
        scope = (await http.post("/api/projects/open", json={"path": str(root)})).json()
        p.app, p.http, p.scope, p.stack = app, http, scope, stack
        p.ctx = app.state.project_registry[scope["project_id"]].ctx
        p.base = f"/api/projects/{scope['project_id']}/remote"
        p.docs_dir = Path(p.ctx.docs.directory)

    async def close() -> None:
        if p.stack is not None:
            stack, p.stack = p.stack, None
            with as_person(p):
                await stack.aclose()

    p.open_app, p.close = open_app, close
    try:
        await open_app()
        yield SimpleNamespace(p=p, store=store, url=url)
    finally:
        await close()
        server.should_exit = True
        await serving
        store.close()


async def census(p: Any) -> dict[str, Any]:
    response = await p.http.get(f"{SERVERS}/{p.row['profile_id']}/consumers")
    assert response.status_code == 200, response.text
    return response.json()


async def put_key(p: Any, key: str) -> httpx.Response:
    return await p.http.put(f"{SERVERS}/{p.row['profile_id']}/credential",
                            json={"expected_revision": p.row["revision"], "token": key})


async def verify(p: Any) -> dict[str, Any]:
    response = await p.http.post(f"{SERVERS}/{p.row['profile_id']}/verify",
                                 json={"expected_revision": p.row["revision"]})
    assert response.status_code == 200, response.text
    return response.json()


async def old_key_close_code(url: str, key: str) -> int | None:
    async with ws_connect(socket_url_v2(url, I16_ID),
                          additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        try:
            await asyncio.wait_for(ws.recv(), 5)
        except ConnectionClosed:
            pass
        return ws.close_code


async def enable(p: Any) -> None:
    shown = await _i16_preview(p)
    observation = await _i16_observe(p)
    response = await p.http.post(p.base + "/enable", json={
        "expected_revision": observation["identity_revision"],
        "expected_generation": observation["binding_generation"],
        "preview_id": shown["preview_id"], "fingerprint": shown["fingerprint"],
        "confirmation": {"url": p.row["url"], "persistent_id": I16_ID, "action": "reconcile"}})
    assert response.status_code == 200, response.text
    await eventually(lambda: p.ctx.sync.live, what="sync to connect")
    await eventually(lambda: _i16_settled(p), what="sync to settle")


async def test_clean_home_profile_confirmation_key_verify_and_rotation_after_disable(
    clean: SimpleNamespace,
) -> None:
    p, store, url = clean.p, clean.store, clean.url
    # Clean personal home: opening the project wrote only app settings/recents.
    assert set(home_files(p.home)) <= {"config.yaml", "recent-projects.json"}
    assert "remote_servers" not in (p.home / "config.yaml").read_text(encoding="utf-8")

    # --- profile
    response = await p.http.post(SERVERS, json={"name": "Team", "url": url})
    assert response.status_code == 201, response.text
    p.row = {k: response.json()[k] for k in ("profile_id", "url", "revision")}
    assert response.json()["has_token"] is False

    # --- no confirmation: 503 and not a single byte written
    before = home_files(p.home)
    response = await put_key(p, p.key)
    assert response.status_code == 503 and response.json()["code"] == "consumer_lifecycle_unavailable"
    assert home_files(p.home) == before

    # Recents, an empty private store and a Verify do not make it known either.
    with as_person(p):
        workspace.record_recent(p.root, "alice")
    (p.home / "remote-bindings.json").write_text('{"version":1,"roots":{}}', encoding="utf-8")
    response = await p.http.post(f"{SERVERS}/{p.row['profile_id']}/verify",
                                 json={"expected_revision": p.row["revision"]})
    assert response.status_code == 409 and response.json()["code"] == "credential_required"
    shown = await census(p)
    assert (shown["state"], shown["bound_roots"], shown["active_roots"]) == ("unknown", [], [])
    before = home_files(p.home)
    response = await put_key(p, p.key)
    assert response.status_code == 503 and home_files(p.home) == before

    # --- the user's explicit confirmation of exactly this URL, then the key
    response = await p.http.post(f"{SERVERS}/{p.row['profile_id']}/consumers/confirmation", json={
        "expected_revision": p.row["revision"], "url": shown["url"], "bound_roots": shown["bound_roots"]})
    assert response.status_code == 200 and response.json()["state"] == "known", response.text
    assert json.loads((p.home / CONFIRMATIONS).read_text(encoding="utf-8"))["urls"][url]
    response = await put_key(p, p.key)
    assert response.status_code == 200 and response.json()["has_token"] is True, response.text
    p.row["revision"] = response.json()["revision"]
    checked = await verify(p)
    assert checked["state"] == "valid" and checked["user"]["display_name"] == "Alice", checked

    # --- the confirmation is durable across a restart
    await p.close()
    await p.open_app()
    assert (await census(p))["state"] == "known"

    # --- Create -> Select -> Preview -> Enable on the real Remote
    response = await p.http.post(f"{SERVERS}/{p.row['profile_id']}/projects", json={
        "expected_revision": p.row["revision"], "id": I16_ID, "display_name": "Gate L",
        "operation_id": str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    await _i16_change(p, "select", I16_ID, profile_id=p.row["profile_id"], url=url,
                      catalog_id=I16_ID, catalog_instance_id=store.project(I16_ID).storage_epoch)
    await enable(p)
    assert _i16_binding(p).selection_state == "confirmed"

    # --- rotate/clear while enabled: refused, nothing stopped, nothing written
    busy = await census(p)
    canonical = str(Path(p.root).resolve())
    assert busy["active_roots"] and busy["bound_roots"] == [str(_i16_binding(p).canonical_root)], busy
    assert all(Path(r).resolve() == Path(canonical) for r in busy["active_roots"])
    rotated = new_token()
    before = home_files(p.home)
    response = await put_key(p, rotated)
    assert response.status_code == 409 and response.json()["code"] == "profile_consumers_not_drained"
    response = await p.http.request("DELETE", f"{SERVERS}/{p.row['profile_id']}/credential",
                                    json={"expected_revision": p.row["revision"]})
    assert response.status_code == 409 and response.json()["code"] == "profile_consumers_not_drained"
    for verb, suffix, body in (("PATCH", "", {"url": url + "/other", "clear_credential": True}),
                               ("DELETE", "", {})):
        response = await p.http.request(verb, f"{SERVERS}/{p.row['profile_id']}{suffix}",
                                        json={"expected_revision": p.row["revision"], **body})
        assert response.status_code == 409 and response.json()["code"] == "profile_in_use", response.text
    assert home_files(p.home) == before
    assert p.ctx.sync.live, "a refusal never stops the running project"

    # --- Disable, then rotate on the Remote and in the Studio
    result = await _i16_change(p, "disable", I16_ID)
    assert result["enabled"] is False and not p.ctx.sync.live
    user = store.user(p.user.id)
    store.rotate_user(user.id, hash_token(rotated), user.metadata_revision, user.credential_version)
    assert (await verify(p))["state"] == "unauthorised", "the Remote no longer accepts the old key"
    assert await old_key_close_code(url, p.key) == CLOSE_UNAUTHORISED
    response = await put_key(p, rotated)
    assert response.status_code == 200, response.text
    p.row["revision"] = response.json()["revision"]
    assert (await verify(p))["state"] == "valid"
    assert _i16_binding(p).selection_state == "selected", "a key change never keeps consent"

    # Re-enable with the new key works; the old one stays refused.
    await enable(p)
    assert await old_key_close_code(url, p.key) == CLOSE_UNAUTHORISED

    # --- clear: refused while enabled, passes after Disable
    response = await p.http.request("DELETE", f"{SERVERS}/{p.row['profile_id']}/credential",
                                    json={"expected_revision": p.row["revision"]})
    assert response.status_code == 409 and response.json()["code"] == "profile_consumers_not_drained"
    await _i16_change(p, "disable", I16_ID)
    response = await p.http.request("DELETE", f"{SERVERS}/{p.row['profile_id']}/credential",
                                    json={"expected_revision": p.row["revision"]})
    assert response.status_code == 200 and response.json()["has_token"] is False, response.text
    assert _I16_HOME.get() is None
