"""File/resource URLs keep their request's project binding (dev spec §5.3.2).

Real in-process ASGI requests, no second backend and no changes to the running Studio.
The colliding relative paths, plugin ids and asset stamps are deliberate.
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import httpx
import pytest
import yaml
from starlette.datastructures import UploadFile

from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project import init_project
from voxarium_studio.main import create_app


def _plugin(root: Path, label: str) -> None:
    directory = ProjectPaths.for_project(root).plugins / "resource_probe"
    directory.mkdir(parents=True)
    manifest = {
        "id": "resource_probe", "name": "Resource probe", "version": "1.0.0",
        "backend": {"entry": "plugin.py:register"},
        "frontend": {
            "pages": [{"id": "probe", "title": "Probe", "route": "/probe",
                       "component": "frontend/Page.js"}],
            "nodes": [{"node_type": "resource_probe.node", "component": "frontend/Node.js"}],
            "previewers": [{"id": "probe", "extensions": [".probe"],
                            "component": "frontend/Preview.js"}],
            "grammars": [{"id": "probe", "name": "Probe", "extensions": [".probe"],
                          "scope_name": "source.probe", "grammar": "frontend/Grammar.json"}],
        },
    }
    (directory / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (directory / "plugin.py").write_text(
        "import importlib\n"
        "from voxarium_studio.schemas.nodes import NodeTypeSchema\n"
        f"MARKER = {label!r}\n"
        "async def lookup(host, payload):\n"
        "    return {'marker': importlib.import_module(__name__).MARKER, 'root': str(host.project_root)}\n"
        "def register(registry):\n"
        "    registry.add_node_type(NodeTypeSchema(id='resource_probe.node', name='Probe', "
        "execution={'kind': 'python_script', 'entry': 'unused.py'}))\n"
        "    registry.add_action('lookup', lookup)\n",
        encoding="utf-8",
    )
    frontend = directory / "frontend"
    frontend.mkdir()
    for name in ("Page.js", "Node.js", "Preview.js", "late.js", "Grammar.json"):
        target = frontend / name
        body = '{"project":"' + label + '"}' if name.endswith(".json") else (
            f"export const project = '{label}';\n"
            "export const late = () => import('./late.js');\n"
        )
        target.write_text(body, encoding="utf-8")
        # A/B labels are equal length: identical stamps must not imply identical URLs.
        os.utime(target, ns=(1_700_000_000_000_000_000,) * 2)


def _url(scope: dict, suffix: str) -> str:
    return f"/api/projects/{scope['project_id']}/{suffix}"


@pytest.fixture
async def resources(project: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    init_project(other)
    for root, label in ((project, "Alpha"), (other, "Bravo")):
        _plugin(root, label)
        (root / "same.txt").write_text(label, encoding="utf-8")
        (root / "same.png").write_bytes(label.encode())
        (root / "same.probe").write_bytes(label.encode())
    app = create_app(str(project))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
        ) as http:
            scopes = []
            for root in (project, other):
                opened = await http.post("/api/projects/open", json={"path": str(root)})
                assert opened.status_code == 200, opened.text
                scopes.append(opened.json())
            yield app, http, scopes


async def _select(http, scope):
    selected = await http.post("/api/project/open", json={"path": scope["root"], "create": False})
    assert selected.status_code == 200, selected.text


async def test_raw_preview_images_and_file_operations_use_their_own_root(resources):
    app, http, (a, b) = resources
    await _select(http, b)
    for scope, label in ((a, "Alpha"), (b, "Bravo")):
        raw = await http.get(_url(scope, "explorer/raw"), params={"path": "same.txt"})
        assert raw.status_code == 200 and raw.text == label
        assert raw.headers["cache-control"] == "no-cache"
        assert raw.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in raw.headers["content-security-policy"]
        preview = await http.get(_url(scope, "explorer/file"), params={"path": "same.txt"})
        assert preview.status_code == 200 and preview.json()["text"] == label
        image = await http.get(_url(scope, "explorer/file"), params={"path": "same.png"})
        assert image.status_code == 200
        assert base64.b64decode(image.json()["data_url"].split(",", 1)[1]) == label.encode()
        created = await http.post(_url(scope, "explorer/create"), json={"name": "new.txt"})
        assert created.status_code == 200, created.text
        assert (Path(scope["root"]) / "new.txt").is_file()
    removed = await http.post(_url(a, "explorer/delete"), json={"paths": ["new.txt"]})
    assert removed.status_code == 200
    assert not (Path(a["root"]) / "new.txt").exists()
    assert (Path(b["root"]) / "new.txt").is_file()
    (Path(b["root"]) / "only-b.txt").write_text("B only", encoding="utf-8")
    assert (await http.get(_url(a, "explorer/raw"), params={"path": "only-b.txt"})).status_code == 404
    assert (await http.get("/api/explorer/raw", params={"path": "same.txt"})).text == "Bravo"
    assert (await http.get("/api/explorer/file", params={"path": "same.txt"})).json()["text"] == "Bravo"
    assert app.state.ctx.paths.root == Path(b["root"])


async def test_docs_image_uploads_and_relative_references_are_project_local(resources):
    _, http, (a, b) = resources
    await _select(http, b)
    for scope, label in ((a, "Alpha"), (b, "Bravo")):
        written = await http.put(_url(scope, "docs/guide/note.md"), json={"content": f"# {label}\n"})
        assert written.status_code == 200, written.text
        added = await http.post(_url(scope, "docs/asset"), data={"path": "guide/note.md"},
                                files={"file": ("same shot.png", label.encode(), "image/png")})
        assert added.status_code == 201, added.text
        assert added.json()["src"] == "same%20shot.png"
        for src in (added.json()["src"], "../../../same.png"):
            image = await http.get(_url(scope, "docs/asset"), params={"path": "guide/note.md", "src": src})
            assert image.status_code == 200 and image.content == label.encode()
            assert image.headers["content-disposition"].startswith("inline")
            assert image.headers["x-content-type-options"] == "nosniff"
            assert "sandbox" in image.headers["content-security-policy"]
            assert (await http.get(_url(scope, "docs/asset"),
                                   params={"path": "guide/note.md", "src": src},
                                   headers={"If-None-Match": image.headers["etag"]})).status_code == 304
        assert (await http.get(_url(scope, "docs/guide/note.md"))).json()["content"] == f"# {label}\n"
    legacy = await http.get("/api/docs/asset", params={"path": "guide/note.md", "src": "same%20shot.png"})
    assert legacy.status_code == 200 and legacy.content == b"Bravo"
    legacy_upload = await http.post("/api/docs/asset", data={"path": "guide/note.md"},
                                    files={"file": ("legacy.png", b"legacy", "image/png")})
    assert legacy_upload.status_code == 201, legacy_upload.text
    assert not (ProjectPaths.for_project(Path(a["root"])).docs / "guide/legacy.png").exists()


async def test_colliding_upload_references_download_from_only_their_project(resources, monkeypatch):
    from voxarium_studio.api import attachments

    _, http, (a, b) = resources
    monkeypatch.setattr(attachments, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    await _select(http, b)
    references = []
    for scope, label in ((a, "Alpha"), (b, "Bravo")):
        for name, mime in (("same.png", "image/png"), ("same.txt", "text/plain")):
            added = await http.post(_url(scope, "attachments"), files={"file": (name, label.encode(), mime)})
            assert added.status_code == 201, added.text
            record = added.json()
            assert record["reference"] == f"upload_{'a' * 32}/{name}"
            assert (Path(scope["root"]) / record["path"]).read_bytes() == label.encode()
            downloaded = await http.get(_url(scope, "attachments/" + record["reference"]))
            assert downloaded.status_code == 200 and downloaded.content == label.encode()
            assert downloaded.headers["content-disposition"].startswith("inline" if name.endswith("png") else "attachment")
            assert downloaded.headers["x-content-type-options"] == "nosniff"
            assert "sandbox" in downloaded.headers["content-security-policy"]
            references.append(record["reference"])
    assert references[:2] == references[2:]
    legacy = await http.get("/api/attachments/" + references[0])
    assert legacy.status_code == 200 and legacy.content == b"Bravo"
    legacy_upload = await http.post("/api/attachments", files={"file": ("legacy.txt", b"legacy")})
    assert legacy_upload.status_code == 201
    ref = legacy_upload.json()["reference"]
    assert (await http.get(_url(a, "attachments/" + ref))).status_code == 404
    assert (await http.get(_url(b, "attachments/" + ref))).content == b"legacy"


async def test_review_attachments_with_copied_task_ids_keep_their_project(resources):
    from voxarium_studio.models.teams import TeamTask

    app, http, (a, b) = resources
    suffix = "teams/team_default/tasks/task_copied/attachments"
    for scope in (a, b):
        ctx = app.state.project_registry[scope["project_id"]].ctx
        async with ctx.db.session() as session:
            session.add(TeamTask(
                id="task_copied", number=999, team_id="team_default",
                assignee_id="team_default__dev", title="Copied task", auto_start=False,
            ))
    await _select(http, b)
    for scope, label in ((a, b"Alpha"), (b, b"Bravo")):
        stored = await http.post(_url(scope, suffix), files={"file": ("same.png", label, "image/png")})
        assert stored.status_code == 201, stored.text
        assert (Path(scope["root"]) / stored.json()["path"]).read_bytes() == label
        fetched = await http.get(_url(scope, suffix + "/same.png"))
        assert fetched.status_code == 200 and fetched.content == label
        assert fetched.headers["content-disposition"].startswith("inline")
        assert fetched.headers["x-content-type-options"] == "nosniff"
    assert (await http.get("/api/" + suffix + "/same.png")).content == b"Bravo"
    assert (await http.delete(_url(a, suffix + "/same.png"))).status_code == 204
    assert (await http.get(_url(a, suffix + "/same.png"))).status_code == 404
    assert (await http.get(_url(b, suffix + "/same.png"))).content == b"Bravo"


@pytest.mark.parametrize("suffix", ["attachments", "docs/asset"])
async def test_awaited_upload_read_retains_context_after_legacy_switch(resources, monkeypatch, suffix):
    _, http, (a, b) = resources
    started, resume = asyncio.Event(), asyncio.Event()
    original = UploadFile.read

    async def paused_read(file, size=-1):
        if file.filename == "delayed.png":
            started.set()
            await resume.wait()
        return await original(file, size)

    monkeypatch.setattr(UploadFile, "read", paused_read)
    request = asyncio.create_task(http.post(
        _url(a, suffix), data={"path": "note.md"} if suffix == "docs/asset" else {},
        files={"file": ("delayed.png", b"Alpha delayed", "image/png")},
    ))
    try:
        await asyncio.wait_for(started.wait(), 10)
        await _select(http, b)
        assert not request.done()
        resume.set()
        added = await asyncio.wait_for(request, 10)
        assert added.status_code == 201, added.text
        if suffix == "attachments":
            stored = Path(a["root"]) / added.json()["path"]
            assert not (Path(b["root"]) / added.json()["path"]).exists()
        else:
            stored = ProjectPaths.for_project(Path(a["root"])).docs / "delayed.png"
            assert not (ProjectPaths.for_project(Path(b["root"])).docs / "delayed.png").exists()
        assert stored.read_bytes() == b"Alpha delayed"
    finally:
        resume.set()
        await asyncio.gather(request, return_exceptions=True)


async def test_generated_plugin_urls_and_relative_late_import_keep_project_scope(resources):
    _, http, (a, b) = resources
    await _select(http, b)
    urls_by_project = []
    for scope, label in ((a, "Alpha"), (b, "Bravo")):
        urls = []
        for suffix, key in (("plugins/pages", "component_url"), ("plugins/previewers", "component_url"),
                            ("plugins/grammars", "grammar_url")):
            response = await http.get(_url(scope, suffix))
            assert response.status_code == 200, response.text
            urls.append(next(row[key] for row in response.json() if row["plugin_id"] == "resource_probe"))
        nodes = await http.get(_url(scope, "node-types"))
        assert nodes.status_code == 200, nodes.text
        urls.append(next(row["ui_component_url"] for row in nodes.json()["node_types"]
                         if row["id"] == "resource_probe.node"))
        preview = await http.get(_url(scope, "explorer/file"), params={"path": "same.probe"})
        assert preview.status_code == 200, preview.text
        assert preview.json()["previewer"]["component_url"] == urls[1]
        for url in urls:
            assert url.startswith(_url(scope, "plugins/resource_probe/assets/"))
            assert urlsplit(url).query.startswith("v=")
            asset = await http.get(url)
            assert asset.status_code == 200 and label in asset.text
            assert "no-cache" in asset.headers["cache-control"]
            if urlsplit(url).path.endswith(".js"):
                late = await http.get(urljoin(url, "./late.js"))
                assert late.status_code == 200 and label in late.text
        urls_by_project.append(urls)
    for first, second in zip(*urls_by_project):
        assert first != second
        assert urlsplit(first).query == urlsplit(second).query
    # The same registry still answers legacy URLs, without a stale scoped default.
    legacy = await http.get("/api/explorer/file", params={"path": "same.probe"})
    url = legacy.json()["previewer"]["component_url"]
    assert url.startswith("/api/plugins/")
    assert "Bravo" in (await http.get(url)).text


async def test_scoped_plugin_disable_and_reload_do_not_rebind_other_project(resources):
    _, http, (a, b) = resources
    await _select(http, b)

    async def lookup(scope, label):
        response = await http.post(_url(scope, "plugins/resource_probe/actions/lookup"), json={})
        assert response.status_code == 200, response.text
        assert response.json()["marker"] == label
        assert Path(response.json()["root"]) == Path(scope["root"])

    await asyncio.gather(lookup(a, "Alpha"), lookup(b, "Bravo"))
    assert (await http.post(_url(a, "plugins/resource_probe/disable"))).status_code == 200
    assert (await http.post(_url(a, "plugins/resource_probe/actions/lookup"), json={})).status_code == 404
    await lookup(b, "Bravo")
    assert (await http.post(_url(a, "plugins/resource_probe/enable"))).status_code == 200
    await asyncio.gather(lookup(a, "Alpha"), lookup(b, "Bravo"))
    assert (await http.put(_url(a, "plugins/resource_probe/settings"), json={"settings": {}})).status_code == 200
    await asyncio.gather(lookup(a, "Alpha"), lookup(b, "Bravo"))


async def test_resource_traversal_and_unknown_scope_never_fall_back(resources):
    _, http, (a, b) = resources
    await _select(http, b)
    for prefix in (f"/api/projects/{a['project_id']}", "/api"):
        for suffix in ("explorer/raw", "explorer/file"):
            assert (await http.get(f"{prefix}/{suffix}", params={"path": "../outside.png"})).status_code == 403
        assert (await http.get(f"{prefix}/docs/asset", params={"path": "note.md", "src": "../../../outside.png"})).status_code == 403
        assert (await http.post(f"{prefix}/docs/asset", data={"path": "../../outside.md"},
                                files={"file": ("x.png", b"x", "image/png")})).status_code == 403
        assert (await http.get(f"{prefix}/attachments/upload_{'a' * 32}/%2e%2e%5cescape.png")).status_code == 404
        assert (await http.get(f"{prefix}/plugins/resource_probe/assets/%2e%2e%5cplugin.yaml")).status_code == 403
    for suffix in ("explorer/raw?path=same.txt", "explorer/file?path=same.txt",
                   "docs/asset?path=note.md&src=shot.png", "plugins/pages",
                   f"attachments/upload_{'a' * 32}/same.png"):
        assert (await http.get("/api/projects/expired/" + suffix)).status_code == 404
    for suffix, data in (("attachments", {}), ("docs/asset", {"path": "note.md"})):
        assert (await http.post("/api/projects/expired/" + suffix, data=data,
                                files={"file": ("x.png", b"x", "image/png")})).status_code == 404


def test_resource_auth_token_is_still_required(token_client, project):
    headers = {"Authorization": "Bearer t0ken"}
    opened = token_client.post("/api/projects/open", json={"path": str(project)}, headers=headers)
    assert opened.status_code == 200, opened.text
    scope = opened.json()
    (project / "same.txt").write_text("secret", encoding="utf-8")
    for prefix in (f"/api/projects/{scope['project_id']}", "/api"):
        asset = f"{prefix}/plugins/vox_preview/assets/frontend/VoxPreview.js"
        assert token_client.get(asset).status_code == 401
        assert token_client.get(asset, params={"token": "t0ken"}).status_code == 200
        for suffix in ("explorer/raw?path=same.txt", "explorer/file?path=same.txt", "plugins/pages",
                       "docs/asset?path=note.md&src=shot.png", f"attachments/upload_{'a' * 32}/same.png"):
            assert token_client.get(f"{prefix}/{suffix}").status_code == 401
        for suffix, data in (("attachments", {}), ("docs/asset", {"path": "note.md"})):
            refused = token_client.post(f"{prefix}/{suffix}", data=data,
                                        files={"file": ("blocked.png", b"no", "image/png")})
            assert refused.status_code == 401
        assert token_client.get(f"{prefix}/explorer/raw?path=same.txt", headers=headers).text == "secret"
    paths = ProjectPaths.for_project(project)
    assert not list(paths.attachments.rglob("blocked.png"))
    assert not list(paths.docs.rglob("blocked.png"))
