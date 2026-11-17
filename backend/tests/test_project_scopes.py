"""Immutable REST bindings over resident contexts (dev spec §5.3.2–5.3.3).

All requests use ASGI in process. Gates, not timing sleeps, place legacy switches
inside uploads, concurrent startup and the real folder-rename stream/continuation.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from voxarium_studio.core.application_config import ApplicationConfig, application_config_file
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import DATA_DIRNAME, ProjectPaths
from voxarium_studio.core.project import init_project
from voxarium_studio.main import create_app
from voxarium_studio.teams.docs import DocStore


@pytest.fixture
async def persistent_identity_http(project: Path, tmp_path: Path):
    """Two fixture roots share portable metadata, never a runtime binding (§5.2.1)."""
    paths = ProjectPaths.for_project(project)
    config = yaml.safe_load(paths.config_file.read_text(encoding="utf-8"))
    config["project_identity"] = {"id": "fixture.shared-project", "revision": 1}
    config["sync"] = {"enabled": False, "url": "", "space": ""}
    paths.config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    # Copy before either context starts: no open SQLite database or live watcher is copied.
    copied = tmp_path / "copied-game"
    shutil.copytree(project, copied)
    app = create_app(str(project))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
        ) as http:
            a, b = await _open(http, project), await _open(http, copied)
            assert not _ctx(app, a).config.sync.enabled
            assert not _ctx(app, b).config.sync.enabled
            yield app, http, a, b


@pytest.fixture
async def scoped_http(project: Path):
    app = create_app(str(project))
    app.state.rename_frame_sent = asyncio.Event()

    async def observed_app(scope, receive, send):
        async def observed_send(message):
            if message["type"] == "http.response.body" and b'"type": "renamed"' in message.get("body", b""):
                app.state.rename_frame_sent.set()
            await send(message)

        await app(scope, receive, observed_send)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=observed_app), base_url="http://127.0.0.1",
        ) as http:
            yield app, http


def _project(tmp_path: Path, name: str = "other") -> Path:
    root = tmp_path / name
    root.mkdir()
    init_project(root)
    return root


async def _open(http: httpx.AsyncClient, root: Path) -> dict:
    response = await http.post("/api/projects/open", json={"path": str(root)})
    assert response.status_code == 200, response.text
    opened = response.json()
    assert opened["project_id"] and opened["epoch"]
    assert Path(opened["root"]) == root.resolve()
    return opened


def _url(opened: dict, suffix: str) -> str:
    return f"/api/projects/{opened['project_id']}/{suffix}"


def _ctx(app, opened: dict) -> AppContext:
    from voxarium_studio.core.project_scope import resolve_project

    descriptor = resolve_project(app.state, opened["project_id"])
    assert descriptor.project_id == opened["project_id"]
    assert descriptor.epoch == opened["epoch"]
    assert Path(descriptor.root) == Path(opened["root"])
    return descriptor.ctx


async def test_ensure_does_not_select_and_legacy_reuses_same_resident(
    scoped_http, project: Path, tmp_path: Path,
) -> None:
    app, http = scoped_http
    incumbent, browser_bus = app.state.ctx, app.state.browser_bus
    a = await _open(http, project)
    b = await _open(http, _project(tmp_path))
    assert a["project_id"] != b["project_id"]
    assert a["epoch"] == b["epoch"]
    assert app.state.ctx is incumbent
    assert app.state.browser_bus is browser_bus
    assert incumbent.bus.forward_to is browser_bus
    assert _ctx(app, b).bus.forward_to is None
    assert Path((await http.get("/api/project")).json()["root"]) == project.resolve()

    selected = await http.post("/api/project/open", json={"path": b["root"], "create": False})
    assert selected.status_code == 200, selected.text
    assert app.state.ctx is _ctx(app, b)
    assert _ctx(app, a) is incumbent
    assert (await _open(http, project))["project_id"] == a["project_id"]
    assert app.state.ctx is _ctx(app, b), "ensure must not undo the legacy selection"
    for scope in (a, b):
        metadata = await http.get(_url(scope, "project"))
        assert metadata.status_code == 200, metadata.text
        assert Path(metadata.json()["root"]) == Path(scope["root"])


async def test_copied_persistent_identity_keeps_distinct_runtime_scopes(
    persistent_identity_http,
) -> None:
    """Portable identity is not the resident registry key (dev spec §5.3.2)."""
    app, http, a, b = persistent_identity_http
    ca, cb = _ctx(app, a), _ctx(app, b)
    assert a["project_id"] != b["project_id"]
    assert a["epoch"] == b["epoch"]
    assert a["root"] != b["root"]
    assert ca is not cb
    assert len(app.state.project_registry) == len(app.state.project_roots) == 2
    for scope in (a, b):
        before = _ctx(app, scope).paths.config_file.read_bytes()
        metadata = await http.get(_url(scope, "project"))
        assert metadata.status_code == 200, metadata.text
        body = metadata.json()
        assert body["persistent_id"] == "fixture.shared-project"
        assert body["config"]["project_identity"] == {
            "id": "fixture.shared-project", "revision": 1,
        }
        assert all(body[key] == scope[key] for key in ("project_id", "epoch", "root"))
        assert _ctx(app, scope).paths.config_file.read_bytes() == before
        reopened = await _open(http, Path(scope["root"]))
        assert all(reopened[key] == scope[key] for key in ("project_id", "epoch", "root"))
    assert app.state.ctx is ca


@pytest.mark.parametrize("legacy", [False, True], ids=["scoped-reload", "legacy-reload"])
async def test_identity_metadata_reload_preserves_descriptor_epoch_and_context(
    persistent_identity_http, legacy: bool,
) -> None:
    app, http, a, b = persistent_identity_http
    ca, cb = _ctx(app, a), _ctx(app, b)
    descriptor = app.state.project_registry[a["project_id"]]
    registry = dict(app.state.project_registry)
    roots = dict(app.state.project_roots)
    config = yaml.safe_load(ca.paths.config_file.read_text(encoding="utf-8"))
    config["project_identity"] = {"id": "fixture.changed-project", "revision": 2}
    ca.paths.config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    url = "/api/settings/reload" if legacy else _url(a, "settings/reload")
    response = await http.post(url)
    assert response.status_code == 200, response.text
    for url in ("/api/project", _url(a, "project")):
        metadata = await http.get(url)
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["persistent_id"] == "fixture.changed-project"
        assert metadata.json()["config"]["project_identity"]["revision"] == 2
    assert (await http.get(_url(b, "project"))).json()["persistent_id"] == "fixture.shared-project"
    assert app.state.project_registry[a["project_id"]] is descriptor
    assert app.state.project_registry == registry
    assert app.state.project_roots == roots
    assert _ctx(app, a) is ca
    assert _ctx(app, b) is cb
    assert app.state.ctx is ca
    reopened = await _open(http, Path(a["root"]))
    assert all(reopened[key] == a[key] for key in ("project_id", "epoch", "root"))
    assert descriptor.epoch == a["epoch"]
    assert descriptor.root == ca.paths.root


@pytest.mark.parametrize("legacy", [False, True], ids=["scoped-settings", "legacy-settings"])
@pytest.mark.parametrize("patch", [
    {"project_identity": {"id": "fixture.forbidden", "revision": 2}},
    {"project_identity": None},
    {"persistent_id": "fixture.forbidden"},
], ids=["replace-identity", "clear-identity", "metadata-alias"])
async def test_settings_cannot_mutate_persistent_identity(
    persistent_identity_http, legacy: bool, patch: dict,
) -> None:
    """S1 exposes identity only as metadata; settings are not a mutation API."""
    app, http, a, b = persistent_identity_http
    configs = {scope["project_id"]: _ctx(app, scope).paths.config_file.read_bytes() for scope in (a, b)}
    url = "/api/settings" if legacy else _url(a, "settings")
    response = await http.put(url, json={"project_name": "must-not-be-applied", **patch})
    assert response.status_code == 422, response.text
    for scope in (a, b):
        resident = _ctx(app, scope)
        assert resident.paths.config_file.read_bytes() == configs[scope["project_id"]]
        assert resident.config.project_identity.model_dump() == {
            "id": "fixture.shared-project", "revision": 1,
        }
        metadata = await http.get(_url(scope, "project"))
        assert metadata.json()["persistent_id"] == "fixture.shared-project"
        assert metadata.json()["name"] != "must-not-be-applied"


async def test_concurrent_docs_and_settings_are_project_local(
    scoped_http, project: Path, tmp_path: Path,
) -> None:
    app, http = scoped_http
    a, b = await _open(http, project), await _open(http, _project(tmp_path))

    async def exercise(scope: dict, name: str, accent: str) -> None:
        for index in range(3):
            content = f"# {name}\n\nwrite {index}\n"
            written, configured = await asyncio.gather(
                http.put(_url(scope, "docs/shared.md"), json={"content": content}),
                http.put(_url(scope, "settings"), json={
                    "project_name": name, "theme": {"accent": accent},
                    "concurrency": {"agents": index + 2},
                }),
            )
            assert written.status_code == configured.status_code == 200
            document, settings = await asyncio.gather(
                http.get(_url(scope, "docs/shared.md")),
                http.get(_url(scope, "settings")),
            )
            assert document.json()["content"] == content
            assert settings.json()["project_name"] == name
            assert settings.json()["theme"]["accent"] == accent
            assert settings.json()["concurrency"]["agents"] == index + 2

    await asyncio.gather(exercise(a, "Alpha", "#123456"), exercise(b, "Beta", "#abcdef"))
    for scope, name in ((a, "Alpha"), (b, "Beta")):
        resident = _ctx(app, scope)
        assert name in (resident.paths.docs / "shared.md").read_text(encoding="utf-8")
        assert yaml.safe_load(resident.paths.config_file.read_text(encoding="utf-8"))["project_name"] == name
    assert app.state.ctx is _ctx(app, a)


@pytest.mark.parametrize("multipart", [False, True], ids=["json-body", "multipart-upload"])
async def test_delayed_request_body_survives_legacy_switch(
    scoped_http, project: Path, tmp_path: Path, multipart: bool,
) -> None:
    app, http = scoped_http
    a, b = await _open(http, project), await _open(http, _project(tmp_path))
    body_started, resume = asyncio.Event(), asyncio.Event()
    if multipart:
        body = (
            b'--scope-boundary\r\nContent-Disposition: form-data; name="path"\r\n\r\nupload.md\r\n'
            b'--scope-boundary\r\nContent-Disposition: form-data; name="file"; filename="picture.png"\r\n'
            b'Content-Type: image/png\r\n\r\nscoped-image\r\n--scope-boundary--\r\n'
        )
        suffix, method = "docs/asset", http.post
        content_type = "multipart/form-data; boundary=scope-boundary"
    else:
        body = json.dumps({"content": "# Delayed Alpha\n"}).encode()
        suffix, method, content_type = "docs/upload.md", http.put, "application/json"

    async def chunks():
        yield body[:20]
        body_started.set()
        await resume.wait()
        yield body[20:]

    request = asyncio.create_task(method(
        _url(a, suffix), content=chunks(), headers={"Content-Type": content_type},
    ))
    try:
        await asyncio.wait_for(body_started.wait(), 10)
        switched = await http.post("/api/project/open", json={"path": b["root"], "create": False})
        assert switched.status_code == 200, switched.text
        assert not request.done()
        resume.set()
        response = await asyncio.wait_for(request, 10)
        assert response.status_code == (201 if multipart else 200), response.text
        a_docs, b_docs = _ctx(app, a).paths.docs, _ctx(app, b).paths.docs
        if multipart:
            assert any(p.read_bytes() == b"scoped-image" for p in a_docs.rglob("*.png"))
            assert not list(b_docs.rglob("*.png"))
        else:
            assert (a_docs / "upload.md").read_text(encoding="utf-8") == "# Delayed Alpha\n"
            assert not (b_docs / "upload.md").exists()
    finally:
        resume.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("abandon_stream", [False, True], ids=["stream", "abandoned-stream"])
async def test_real_rename_stream_and_background_mending_stay_in_project(
    scoped_http, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    abandon_stream: bool,
) -> None:
    app, http = scoped_http
    a, b = await _open(http, project), await _open(http, _project(tmp_path))
    for scope in (a, b):
        for path, content in (("old/target.md", "# Target\n"), ("ref.md", "# Ref\n[[old/target]]\n")):
            response = await http.put(_url(scope, f"docs/{path}"), json={"content": content})
            assert response.status_code == 200, response.text
    started, resume = asyncio.Event(), asyncio.Event()
    original = DocStore._mend

    async def delayed_mend(store, *args):
        if store is _ctx(app, a).docs:
            started.set()
            await resume.wait()
        return await original(store, *args)

    monkeypatch.setattr(DocStore, "_mend", delayed_mend)
    request = asyncio.create_task(http.post(
        _url(a, "docs/folders/rename"), json={"path": "old", "name": "new"},
    ))
    try:
        await asyncio.wait_for(started.wait(), 10)
        await asyncio.wait_for(app.state.rename_frame_sent.wait(), 10)
        pending_mending = list(_ctx(app, a).docs._mending)
        assert pending_mending and not request.done()
        switched = await http.post("/api/project/open", json={"path": b["root"], "create": False})
        assert switched.status_code == 200, switched.text
        if abandon_stream:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        resume.set()
        await asyncio.wait_for(asyncio.gather(*pending_mending), 10)
        if not abandon_stream:
            response = await asyncio.wait_for(request, 10)
            assert response.status_code == 200, response.text
            frames = [json.loads(line) for line in response.text.splitlines()]
            assert frames[0]["type"] == "renamed"
            assert frames[-1]["type"] == "done"
            assert frames[-1]["updated"] == 1
        assert (await http.get(_url(a, "docs/new/target.md"))).status_code == 200
        assert "[[new/target]]" in (await http.get(_url(a, "docs/ref.md"))).json()["content"]
        assert (await http.get(_url(b, "docs/old/target.md"))).status_code == 200
        assert "[[old/target]]" in (await http.get(_url(b, "docs/ref.md"))).json()["content"]
        assert (await http.get(_url(b, "docs/new/target.md"))).status_code == 404
    finally:
        resume.set()
        await asyncio.gather(request, return_exceptions=True)


async def test_concurrent_alias_opens_coalesce_one_startup(
    scoped_http, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, http = scoped_http
    root = _project(tmp_path)
    started, resume = asyncio.Event(), asyncio.Event()
    original = AppContext.startup
    creations = []

    async def gated_startup(ctx):
        if ctx.paths.root == root.resolve():
            creations.append(ctx)
            started.set()
            await resume.wait()
        await original(ctx)

    monkeypatch.setattr(AppContext, "startup", gated_startup)
    first = asyncio.create_task(_open(http, root))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 10)
        alias = root / ".." / root.name / "."
        second = asyncio.create_task(_open(http, alias))
        resume.set()
        a, b = await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert (a["project_id"], a["epoch"]) == (b["project_id"], b["epoch"])
        assert len(creations) == 1
        assert _ctx(app, a) is creations[0]
        assert len(app.state.project_contexts) == 2
    finally:
        resume.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive canonical roots")
async def test_windows_case_alias_has_same_identity(scoped_http, tmp_path: Path) -> None:
    app, http = scoped_http
    root = _project(tmp_path, "MiXeD")
    normal = await _open(http, root)
    alias = await _open(http, Path(str(root).swapcase()))
    assert alias["project_id"] == normal["project_id"]
    assert _ctx(app, alias) is _ctx(app, normal)


async def test_directory_symlink_alias_has_same_identity(scoped_http, tmp_path: Path) -> None:
    app, http = scoped_http
    root, alias = _project(tmp_path), tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks unavailable: {exc}")
    normal, linked = await _open(http, root), await _open(http, alias)
    assert normal["project_id"] == linked["project_id"]
    assert _ctx(app, normal) is _ctx(app, linked)


async def test_failed_startup_rolls_back_and_retry_gets_clean_context(
    scoped_http, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, http = scoped_http
    a, root = await _open(http, project), _project(tmp_path)
    original_start, original_stop = AppContext.startup, AppContext.shutdown
    failed, stopped = [], []

    async def fail_after_start(ctx):
        await original_start(ctx)
        if ctx.paths.root == root.resolve():
            failed.append(ctx)
            raise OSError("injected startup failure")

    async def record_shutdown(ctx):
        stopped.append(ctx)
        await original_stop(ctx)

    monkeypatch.setattr(AppContext, "startup", fail_after_start)
    monkeypatch.setattr(AppContext, "shutdown", record_shutdown)
    published_before = dict(app.state.project_registry)
    response = await http.post("/api/projects/open", json={"path": str(root)})
    assert response.status_code == 400, response.text
    assert failed and failed[0] in stopped
    assert root.resolve() not in app.state.project_contexts
    assert app.state.project_registry == published_before
    assert app.state.ctx is _ctx(app, a)
    assert (await http.get(_url(a, "project"))).status_code == 200
    monkeypatch.setattr(AppContext, "startup", original_start)
    retried = await _open(http, root)
    assert _ctx(app, retried) is not failed[0]


async def test_open_requires_explicit_create_and_bad_paths_do_not_mutate_selection(
    scoped_http, tmp_path: Path,
) -> None:
    app, http = scoped_http
    incumbent = app.state.ctx
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    for payload in ({"path": str(fresh)}, {"path": str(fresh), "create": False}):
        response = await http.post("/api/projects/open", json=payload)
        assert response.status_code == 409, response.text
        assert not (fresh / DATA_DIRNAME).exists()
    file = tmp_path / "file.txt"
    file.write_text("not a directory", encoding="utf-8")
    for root in (file, tmp_path / "missing"):
        response = await http.post("/api/projects/open", json={"path": str(root), "create": True})
        assert response.status_code == 404, response.text
        assert not (root / DATA_DIRNAME).exists()
    created = await http.post("/api/projects/open", json={"path": str(fresh), "create": True})
    assert created.status_code == 200, created.text
    assert (fresh / DATA_DIRNAME).is_dir()
    assert app.state.ctx is incumbent


async def test_unknown_ids_fail_closed_without_fallback_or_creation(scoped_http) -> None:
    app, http = scoped_http
    incumbent = app.state.ctx
    before = dict(app.state.project_contexts)
    unknown = {"project_id": "unknown-or-expired"}
    for suffix in ("project", "docs", "settings", "status"):
        response = await http.get(_url(unknown, suffix))
        assert response.status_code == 404, response.text
    response = await http.put(_url(unknown, "docs/should-not-exist.md"), json={"content": "wrong"})
    assert response.status_code == 404, response.text
    response = await http.put(_url(unknown, "settings"), json={"project_name": "wrong"})
    assert response.status_code == 404, response.text
    assert not (incumbent.paths.docs / "should-not-exist.md").exists()
    assert app.state.ctx is incumbent
    assert app.state.project_contexts == before


def test_epoch_and_runtime_ids_are_fresh_after_backend_restart(project: Path) -> None:
    def open_scope(http):
        response = http.post("/api/projects/open", json={"path": str(project)})
        assert response.status_code == 200, response.text
        return response.json()

    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as first:
        previous = open_scope(first)
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as second:
        current = open_scope(second)
        assert previous["epoch"] != current["epoch"]
        assert previous["project_id"] != current["project_id"]
        assert second.get(_url(previous, "project")).status_code == 404
        assert second.put(_url(previous, "docs/expired.md"), json={"content": "wrong"}).status_code == 404
        assert second.get(_url(current, "project")).status_code == 200
        assert not (ProjectPaths.for_project(project).docs / "expired.md").exists()


@pytest.mark.parametrize("method,suffix,payload", [
    ("GET", "settings/application", None),
    ("PUT", "settings/application", {"language": "ru"}),
    ("GET", "fs/inspect?path=.", None),
    ("GET", "updates", None),
    ("GET", "project/recent", None),
    ("POST", "project/open", {"path": ".", "create": True}),
    ("POST", "projects/open", {"path": ".", "create": True}),
])
async def test_global_operations_are_not_exposed_in_project_scope(
    scoped_http, project: Path, method: str, suffix: str, payload,
) -> None:
    app, http = scoped_http
    scope = await _open(http, project)
    if payload and "path" in payload:
        # A regression must never attempt to initialize the checkout/current directory.
        payload = {**payload, "path": scope["root"], "create": False}
    response = await http.request(method, _url(scope, suffix), json=payload)
    assert response.status_code in (404, 405), response.text
    assert app.state.ctx is _ctx(app, scope)


@pytest.mark.parametrize("global_patch", [
    {"language": "ru"}, {"respond_language": "Russian"},
    {"show_data_dir": True}, {"node_cache_enabled": False},
    {"updates": {"enabled": False}}, {"theme": {"mode": "light"}},
    {"theme": {"text_size": 18}},
])
async def test_scoped_settings_reject_global_fields_atomically(
    scoped_http, project: Path, global_patch: dict,
) -> None:
    app, http = scoped_http
    scope = await _open(http, project)
    resident = _ctx(app, scope)
    files = (resident.paths.config_file, application_config_file())
    before_files = {p: p.read_bytes() if p.exists() else None for p in files}
    before_config = resident.config.model_dump(mode="json")
    response = await http.put(_url(scope, "settings"), json={
        "project_name": "must-not-be-saved", **global_patch,
    })
    assert response.status_code in (400, 422), response.text
    assert resident.config.model_dump(mode="json") == before_config
    assert all((p.read_bytes() if p.exists() else None) == value for p, value in before_files.items())


async def test_application_settings_propagate_without_writing_any_project_config(
    scoped_http, project: Path, tmp_path: Path,
) -> None:
    app, http = scoped_http
    a, b = await _open(http, project), await _open(http, _project(tmp_path))
    files = {scope["project_id"]: _ctx(app, scope).paths.config_file.read_bytes() for scope in (a, b)}
    response = await http.put("/api/settings/application", json={
        "language": "ru", "theme": {"mode": "light"},
        "show_data_dir": True, "node_cache_enabled": False, "updates": {"enabled": False},
    })
    assert response.status_code == 200, response.text
    for scope in (a, b):
        resident = _ctx(app, scope)
        assert resident.config.language == "ru"
        assert resident.config.theme.mode == "light"
        assert resident.config.node_cache_enabled is False
        assert resident.updates.config.enabled is False
        assert resident.paths.config_file.read_bytes() == files[scope["project_id"]]
        effective = (await http.get(_url(scope, "project"))).json()["config"]
        assert effective["language"] == "ru"
        assert effective["theme"]["mode"] == "light"
        changed = await http.put(_url(scope, "settings"), json={"project_name": "Project only"})
        assert changed.status_code == 200, changed.text
        persisted = yaml.safe_load(resident.paths.config_file.read_text(encoding="utf-8"))
        for global_key in ("language", "respond_language", "updates", "show_data_dir", "node_cache_enabled"):
            assert global_key not in persisted
        assert "mode" not in persisted.get("theme", {})
        assert "show_data_dir" not in persisted.get("explorer", {})
    saved = yaml.safe_load(application_config_file().read_text(encoding="utf-8"))
    assert saved["language"] == "ru" and saved["theme"]["mode"] == "light"
    c = await _open(http, _project(tmp_path, "third"))
    assert _ctx(app, c).config.language == "ru"
    assert _ctx(app, c).config.theme.mode == "light"


@pytest.mark.parametrize("operation", ["save", "reload"])
async def test_project_save_and_reload_do_not_adopt_external_application_changes(
    scoped_http, project: Path, tmp_path: Path, operation: str,
) -> None:
    """Project operations preserve live globals; only an explicit global PUT adopts disk (§5.3.3)."""
    app, http = scoped_http
    a, b = await _open(http, project), await _open(http, _project(tmp_path))
    configured = await http.put("/api/settings/application", json={
        "language": "en", "respond_language": "English", "theme": {"mode": "dark"},
        "show_data_dir": False, "node_cache_enabled": True, "updates": {"enabled": False},
    })
    assert configured.status_code == 200, configured.text
    baseline = ApplicationConfig.from_effective(_ctx(app, a).config).model_dump(mode="json")
    assert ApplicationConfig.from_effective(_ctx(app, b).config).model_dump(mode="json") == baseline

    external = {**baseline, "language": "ru", "respond_language": "Russian",
                "show_data_dir": True, "node_cache_enabled": False,
                "theme": {**baseline["theme"], "mode": "light"},
                "updates": {**baseline["updates"], "enabled": True}}
    application_file = application_config_file()
    application_file.write_text(
        "# Changed by a different application-settings writer\n" + yaml.safe_dump(external),
        encoding="utf-8",
    )
    external_bytes = application_file.read_bytes()

    for scope, name, accent in ((a, "Alpha scoped", "#123456"), (b, "Beta scoped", "#abcdef")):
        resident = _ctx(app, scope)
        if operation == "save":
            response = await http.put(_url(scope, "settings"), json={
                "project_name": name, "theme": {"accent": accent},
            })
            effective = response.json()
        else:
            project_config = yaml.safe_load(resident.paths.config_file.read_text(encoding="utf-8"))
            project_config.update(project_name=name, theme={"accent": accent})
            resident.paths.config_file.write_text(yaml.safe_dump(project_config), encoding="utf-8")
            response = await http.post(_url(scope, "settings/reload"))
            effective = response.json().get("config", {})
        assert response.status_code == 200, response.text
        assert effective["project_name"] == name
        assert effective["theme"]["accent"] == accent
        assert effective["language"] == baseline["language"]
        assert effective["theme"]["mode"] == baseline["theme"]["mode"]
        assert application_file.read_bytes() == external_bytes
        for unchanged in (a, b):
            context = _ctx(app, unchanged)
            assert ApplicationConfig.from_effective(context.config).model_dump(mode="json") == baseline
            assert context.updates.config.model_dump(mode="json") == baseline["updates"]

    project_bytes = {scope["project_id"]: _ctx(app, scope).paths.config_file.read_bytes() for scope in (a, b)}
    # A global-only save explicitly reads/adopts the external file and updates every resident.
    response = await http.put("/api/settings/application", json={"language": "ru"})
    assert response.status_code == 200, response.text
    expected = ApplicationConfig.model_validate(external).model_dump(mode="json")
    for scope in (a, b):
        context = _ctx(app, scope)
        assert ApplicationConfig.from_effective(context.config).model_dump(mode="json") == expected
        assert context.updates.config.model_dump(mode="json") == expected["updates"]
        assert context.paths.config_file.read_bytes() == project_bytes[scope["project_id"]]
    assert yaml.safe_load(application_file.read_text(encoding="utf-8")) == expected
    assert app.state.ctx is _ctx(app, a)


async def test_all_context_and_control_routes_are_available_in_scoped_and_legacy_api(
    scoped_http, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VS-24 exposes every Context Viewer/control method on both adapters."""
    from voxarium_studio.api import chats, session_context, teams

    # Keep the inventory explicit: every current Teams/Chats context/control method,
    # including fragments and DELETE, must be tested rather than just the main GET.
    routes = {
        ("GET", "/teams/{team_id}/tasks/{task_id}/context"): "task_read",
        ("GET", "/teams/{team_id}/tasks/{task_id}/context/fragments/{source_id}"): "task_read",
        ("POST", "/teams/{team_id}/tasks/{task_id}/context/inspect"): "inspect",
        ("POST", "/teams/{team_id}/tasks/{task_id}/remote-control"): "enable",
        ("DELETE", "/teams/{team_id}/tasks/{task_id}/remote-control"): "disable",
        ("GET", "/chats/{chat_id}/context"): "chat_read",
        ("GET", "/chats/{chat_id}/context/fragments/{source_id}"): "chat_read",
        ("POST", "/chats/{chat_id}/context/inspect"): "inspect",
    }
    actual = {
        (method, route.path)
        for module in (teams, chats) for route in module.router.routes
        if {"context", "remote-control"}.intersection(route.path.split("/"))
        for method in route.methods
    }
    assert actual == set(routes), "Extend the scoped regression for every new runtime route"

    app, http = scoped_http
    a, b = await _open(http, project), await _open(http, _project(tmp_path))
    owners = {}
    for scope in (a, b):
        task = await http.post(_url(scope, "teams/team_default/tasks"), json={
            "title": "Context route owner", "assignee_id": "team_default__dev",
        })
        chat = await http.post(_url(scope, "chats"), json={"member_id": "team_default__dev"})
        assert task.status_code == chat.status_code == 201
        owners[scope["project_id"]] = {
            "team_id": "team_default", "task_id": task.json()["id"],
            "chat_id": chat.json()["id"], "source_id": 0,
        }

    # Sentinel the sensitive read/control boundaries, not the HTTP handlers. Valid
    # owners make both adapters reach each boundary; an omitted scoped route cannot
    # hide behind an unrelated "owner not found" 404 before runtime lookup.
    probes = {
        name: AsyncMock(side_effect=HTTPException(418, "runtime boundary reached"))
        for name in ("task_read", "chat_read", "inspect", "enable", "disable")
    }
    monkeypatch.setattr(teams, "_retained_task_context", probes["task_read"])
    monkeypatch.setattr(chats, "_retained_chat_context", probes["chat_read"])
    monkeypatch.setattr(session_context, "inspect_session", probes["inspect"])
    monkeypatch.setattr(chats, "inspect_session", probes["inspect"])
    lookup = Mock(side_effect=AssertionError("unscoped runtime lookup must not run"))
    monkeypatch.setattr(session_context, "live_session", lookup)
    monkeypatch.setattr(session_context, "context_inspector", lookup)
    for scope in (a, b):
        monkeypatch.setattr(_ctx(app, scope).teams, "enable_remote_control", probes["enable"])
        monkeypatch.setattr(_ctx(app, scope).teams, "disable_remote_control", probes["disable"])
        for method, template in routes:
            suffix = template.format(**owners[scope["project_id"]]).lstrip("/")
            before = probes[routes[method, template]].await_count
            response = await http.request(method, _url(scope, suffix), params={"key": "a" * 64})
            assert response.status_code == 418, (method, suffix, response.text)
            assert probes[routes[method, template]].await_count == before + 1
    lookup.assert_not_called()

    for (method, template), boundary in routes.items():
        path = "/api" + template.format(**owners[a["project_id"]])
        before = probes[boundary].await_count
        response = await http.request(method, path, params={"key": "a" * 64})
        assert response.status_code == 418, (method, path, response.text)
        assert probes[boundary].await_count == before + 1
    lookup.assert_not_called()


async def test_application_save_and_ensure_are_serialized(
    scoped_http, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, http = scoped_http
    root = _project(tmp_path)
    started, resume, save_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = AppContext.startup

    async def gated_startup(ctx):
        if ctx.paths.root == root.resolve():
            started.set()
            await resume.wait()
        await original(ctx)

    async def save():
        save_started.set()
        return await http.put("/api/settings/application", json={
            "language": "ru", "theme": {"mode": "light"}, "updates": {"enabled": False},
        })

    monkeypatch.setattr(AppContext, "startup", gated_startup)
    opening = asyncio.create_task(_open(http, root))
    saving = None
    try:
        await asyncio.wait_for(started.wait(), 10)
        saving = asyncio.create_task(save())
        await asyncio.wait_for(save_started.wait(), 10)
        assert not saving.done()
        resume.set()
        opened, saved = await asyncio.wait_for(asyncio.gather(opening, saving), 10)
        assert saved.status_code == 200, saved.text
        for resident in app.state.project_contexts.values():
            assert resident.config.language == "ru"
            assert resident.config.theme.mode == "light"
            assert resident.updates.config.enabled is False
        assert app.state.ctx is not _ctx(app, opened)
    finally:
        resume.set()
        await asyncio.gather(opening, *([saving] if saving else []), return_exceptions=True)


async def test_same_named_asset_reads_stay_bound_after_legacy_switch(
    scoped_http, project: Path, tmp_path: Path,
) -> None:
    app, http = scoped_http
    a, b = await _open(http, project), await _open(http, _project(tmp_path))
    for scope, data in ((a, b"Alpha image"), (b, b"Beta image")):
        (_ctx(app, scope).paths.docs / "same.png").write_bytes(data)
    response = await http.post("/api/project/open", json={"path": b["root"], "create": False})
    assert response.status_code == 200, response.text
    first, second = await asyncio.gather(*(
        http.get(_url(scope, "docs/asset"), params={"path": "note.md", "src": "same.png"})
        for scope in (a, b)
    ))
    assert first.status_code == second.status_code == 200
    assert first.content == b"Alpha image"
    assert second.content == b"Beta image"
    assert first.headers["etag"] != second.headers["etag"]


async def test_scoped_status_uses_own_bus_not_legacy_browser_bus(
    scoped_http, project: Path, tmp_path: Path,
) -> None:
    app, http = scoped_http
    a, b = await _open(http, project), await _open(http, _project(tmp_path))
    ca, cb = _ctx(app, a), _ctx(app, b)
    subscription = cb.bus.subscribe()
    try:
        for index in range(4):
            await cb.bus.publish("test.scope", {"index": index})
        scoped = await http.get(_url(b, "status"))
        legacy = await http.get("/api/status")
        assert scoped.status_code == legacy.status_code == 200
        assert scoped.json()["event_seq"] == cb.bus.current_seq
        assert scoped.json()["ws_clients"] == cb.bus.subscriber_count
        assert legacy.json()["event_seq"] == app.state.browser_bus.current_seq
        assert legacy.json()["ws_clients"] == app.state.browser_bus.subscriber_count
        assert cb.bus.current_seq != app.state.browser_bus.current_seq
        assert app.state.ctx is ca
    finally:
        subscription.close()


def test_token_is_still_required_for_ensure_and_scoped_requests(token_client, project: Path) -> None:
    headers = {"Authorization": "Bearer t0ken"}
    for bad_headers in ({}, {"Authorization": "Bearer wrong"}):
        response = token_client.post("/api/projects/open", json={"path": str(project)}, headers=bad_headers)
        assert response.status_code == 401, response.text
    response = token_client.post("/api/projects/open", json={"path": str(project)}, headers=headers)
    assert response.status_code == 200, response.text
    scope = response.json()
    assert token_client.get(_url(scope, "project")).status_code == 401
    assert token_client.get(_url(scope, "project"), headers=headers).status_code == 200
    assert token_client.put(_url(scope, "docs/no.md"), json={"content": "wrong"}).status_code == 401
    assert not (ProjectPaths.for_project(project).docs / "no.md").exists()


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"}, {"Origin": "https://attacker.example"},
    {"Origin": "null"}, {"Host": "attacker.example", "X-Forwarded-Host": "127.0.0.1"},
])
async def test_host_and_origin_guards_apply_to_new_routes(
    scoped_http, project: Path, tmp_path: Path, headers: dict,
) -> None:
    app, http = scoped_http
    scope = await _open(http, project)
    fresh = tmp_path / "untrusted-create"
    fresh.mkdir()
    response = await http.post("/api/projects/open", json={"path": str(fresh), "create": True}, headers=headers)
    assert response.status_code == 403, response.text
    assert not (fresh / DATA_DIRNAME).exists()
    assert (await http.get(_url(scope, "project"), headers=headers)).status_code == 403
    write = await http.put(_url(scope, "docs/no.md"), json={"content": "wrong"}, headers=headers)
    assert write.status_code == 403, write.text
    assert not (_ctx(app, scope).paths.docs / "no.md").exists()


async def test_scoped_paths_cannot_escape_project_and_asset_headers_remain_sandboxed(
    scoped_http, project: Path, tmp_path: Path,
) -> None:
    app, http = scoped_http
    scope = await _open(http, project)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    for path in ("%2e%2e%5coutside.md", "%2e%2e%2f%2e%2e%2foutside.md"):
        response = await http.put(_url(scope, f"docs/{path}"), json={"content": "wrong"})
        assert response.status_code == 403, response.text
    escaped = await http.get(_url(scope, "docs/asset"), params={
        "path": "note.md", "src": "../../../../outside.md",
    })
    assert escaped.status_code == 403, escaped.text
    assert outside.read_text(encoding="utf-8") == "secret"
    asset = _ctx(app, scope).paths.docs / "picture.svg"
    asset.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    served = await http.get(_url(scope, "docs/asset"), params={"path": "note.md", "src": "picture.svg"})
    assert served.status_code == 200, served.text
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in served.headers["content-security-policy"]
