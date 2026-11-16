"""The one global desktop manifest (dev spec §5.3.6), without native restore."""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from voxarium_studio.api import desktop
from voxarium_studio.core.project_scope import canonical_root


URL = "/api/desktop/session"


def manifest(root):
    return {
        "version": 1,
        "windows": [{
            "tabs": [{"root": str(root), "page": {"path": "/tasks", "query": {"task": "VS-58"}}}],
            "active_tab": 0,
            "geometry": {"x": -1200, "y": 0, "width": 1200, "height": 800, "maximized": False},
        }],
        "active_window": 0,
    }


def test_roundtrip_canonical_roots_order_and_only_home_storage(client, project, studio_home):
    assert client.get(URL).json() == {"detail": {"code": "desktop_session_missing"}}
    assert client.get(URL).status_code == 404
    selected = client.app.state.ctx
    value = manifest(project / ".." / project.name)
    # A missing root remains a locator, not an implicit create/open or fallback.
    missing = project.parent / "missing"
    value["windows"].append({
        "tabs": [{"root": str(missing), "page": {"path": "/explorer", "query": {"path": "assets/a#b%2F%25.vox"}}}],
        "active_tab": 0,
    })
    value["windows"].append({"tabs": [], "active_tab": None})
    value["active_window"] = 1
    response = client.put(URL, json=value)
    assert response.status_code == 200, response.text
    value["windows"][0]["tabs"][0]["root"] = str(canonical_root(project))
    value["windows"][1]["tabs"][0]["root"] = str(canonical_root(missing))
    assert response.json() == value
    response = client.get(URL)
    assert response.json() == value
    assert response.headers["cache-control"] == "no-store"
    assert json.loads((studio_home / "desktop-session.json").read_bytes()) == value
    assert not missing.exists()
    assert not list(project.rglob("desktop-session*"))
    assert client.app.state.ctx is selected
    assert len(client.app.state.project_registry) == 1
    assert client.get("/api/projects/unknown/desktop/session").status_code == 404
    empty = {"version": 1, "windows": [], "active_window": None}
    assert client.put(URL, json=empty).json() == empty
    assert client.get(URL).json() == empty


def test_validation_rejects_non_topology_payloads_without_overwriting(client, project, studio_home):
    good = manifest(project)
    assert client.put(URL, json=good).status_code == 200
    before = (studio_home / "desktop-session.json").read_bytes()
    invalid = []
    for field in ("token", "project_id", "epoch", "cursor", "yjs", "draft", "File"):
        value = copy.deepcopy(good)
        value["windows"][0]["tabs"][0][field] = "not topology"
        invalid.append(value)
    value = copy.deepcopy(good)
    value["windows"][0]["tabs"].append(copy.deepcopy(value["windows"][0]["tabs"][0]))
    value["windows"][0]["tabs"][1]["root"] = str(project / ".." / project.name)
    invalid.append(value)  # Canonical aliases are also duplicates.
    for page in ({"path": "/p/old-id/tasks"}, {"path": "https://example.com/tasks"},
                 {"path": "/tasks?token=secret"}, {"path": "/%2e%2e/tasks"},
                 {"path": "/tasks", "query": {"token": "secret"}},
                 {"path": "/explorer", "query": {"path": "../outside"}},
                 {"path": "/explorer", "query": {"path": "/outside"}},
                 {"path": "/explorer", "query": {"path": "assets/a\nb.vox"}},
                 {"path": "/explorer", "query": {"path": "https://example.com/a.vox"}}):
        value = copy.deepcopy(good)
        value["windows"][0]["tabs"][0]["page"] = page
        invalid.append(value)
    value = copy.deepcopy(good)
    value["windows"][0]["geometry"]["width"] = True
    invalid.append(value)
    value = copy.deepcopy(good)
    value["windows"][0]["geometry"]["height"] = 0
    invalid.append(value)
    value = copy.deepcopy(good)
    value["windows"][0]["active_tab"] = 1
    invalid.append(value)
    value = copy.deepcopy(good)
    value["windows"][0]["tabs"][0]["root"] = "relative/root"
    invalid.append(value)
    for value in invalid:
        response = client.put(URL, json=value)
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": {"code": "desktop_session_invalid"}}
    # Bounded input and duplicate keys cannot bypass the schema.
    assert client.put(URL, content=b" " * (desktop.MAX_BYTES + 1)).status_code == 413
    assert client.put(URL, content=b'{"version":1,"version":1}').status_code == 422
    assert (studio_home / "desktop-session.json").read_bytes() == before


@pytest.mark.parametrize("raw,code", [
    (b'{"version":1,"windows":', "desktop_session_corrupt"),
    (b'{"version":99,"windows":[],"active_window":null}', "desktop_session_unsupported_version"),
])
def test_corrupt_or_newer_file_is_not_an_empty_success_or_silently_replaced(client, project, studio_home, raw, code):
    path = studio_home / "desktop-session.json"
    path.write_bytes(raw)
    for response in (client.get(URL), client.put(URL, json=manifest(project))):
        assert response.status_code == 409
        assert response.json() == {"detail": {"code": code}}
    assert path.read_bytes() == raw


@pytest.mark.parametrize("stage", ["fsync", "replace"])
def test_failed_atomic_save_keeps_last_snapshot_and_removes_temporary(client, project, studio_home, monkeypatch, stage):
    value = manifest(project)
    assert client.put(URL, json=value).status_code == 200
    path = studio_home / "desktop-session.json"
    before = path.read_bytes()

    def fail(*args):
        raise OSError("injected write failure")

    monkeypatch.setattr(desktop.os, stage, fail)
    value["windows"][0]["tabs"][0]["page"]["query"]["task"] = "VS-59"
    response = client.put(URL, json=value)
    assert response.status_code == 500
    assert response.json() == {"detail": {"code": "desktop_session_write_failed"}}
    assert path.read_bytes() == before
    assert not list(studio_home.glob(".desktop-session-*.tmp"))
    assert client.get(URL).json() == json.loads(before)


def test_parallel_saves_publish_complete_snapshots(studio_home, tmp_path, monkeypatch):
    snapshots = [manifest(canonical_root(tmp_path / f"project-{i}")) for i in range(24)]
    for i, value in enumerate(snapshots):
        value["windows"][0]["tabs"][0]["page"]["query"]["task"] = f"VS-{i}"
    original_replace = desktop.os.replace
    path = studio_home / "desktop-session.json"
    published = []

    def observe_replace(source, target):
        # The commit point sees complete old AND new JSON, even with simultaneous
        # writers. No reader could see the tempfile's incremental writes.
        new = json.loads(source.read_bytes())
        if path.exists():
            assert json.loads(path.read_bytes()) in snapshots
        original_replace(source, target)
        assert json.loads(path.read_bytes()) == new
        published.append(new)

    monkeypatch.setattr(desktop.os, "replace", observe_replace)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda value: desktop.write_session(json.dumps(value).encode()), snapshots))
    assert results == snapshots
    assert len(published) == len(snapshots)
    assert desktop.read_session() == published[-1]
    assert not list(studio_home.glob(".desktop-session-*.tmp"))


def test_home_override_is_resolved_by_backend_for_each_operation(tmp_path, monkeypatch):
    first, second = tmp_path / "home-one", tmp_path / "home-two"
    value = manifest(canonical_root(tmp_path / "missing-project"))
    monkeypatch.setenv("VOXARIUM_STUDIO_HOME", str(first))
    desktop.write_session(json.dumps(value).encode())
    monkeypatch.setenv("VOXARIUM_STUDIO_HOME", str(second))
    with pytest.raises(desktop.SessionError, match="desktop_session_missing"):
        desktop.read_session()
    empty = {"version": 1, "windows": [], "active_window": None}
    desktop.write_session(json.dumps(empty).encode())
    assert desktop.read_session() == empty
    assert json.loads((first / "desktop-session.json").read_bytes()) == value
    assert json.loads((second / "desktop-session.json").read_bytes()) == empty


def test_manifest_uses_existing_token_host_and_origin_guards(token_client, project, studio_home):
    value = manifest(canonical_root(project))
    for method in ("GET", "PUT"):
        for headers in ({}, {"Authorization": "Bearer wrong"},
                        {"Authorization": "Bearer t0ken", "Origin": "https://attacker.example"}):
            response = token_client.request(method, URL, headers=headers, json=value if method == "PUT" else None)
            assert response.status_code in {401, 403}
    assert not (studio_home / "desktop-session.json").exists()
    headers = {"Authorization": "Bearer t0ken", "Origin": "http://127.0.0.1"}
    assert token_client.put(URL, json=value, headers=headers).status_code == 200
    assert token_client.get(URL, headers=headers).json() == value


def test_manifest_uses_existing_loopback_host_guard(client):
    for method in ("GET", "PUT"):
        assert client.request(method, URL, headers={"Host": "attacker.example"}).status_code == 403
