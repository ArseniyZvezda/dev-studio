"""Version mismatch recovery never starts a real server during tests (§2.5)."""
from types import SimpleNamespace

import pytest

from voxarium_studio.api import runtime


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "build_info", lambda: SimpleNamespace(version="4.5.0"))
    monkeypatch.setattr(runtime, "available_versions", lambda **_: ("4.6.0", "4.6.0", tmp_path))
    launches = []

    async def restart(service, target):
        launches.append(target)

    monkeypatch.setattr(runtime.UpdateService, "restart_current_files", restart)
    return launches


def test_mismatch_restarts_once_and_preserves_project(client, recovery):
    body = {"frontend_version": "4.6.0", "backend_version": "4.5.0"}
    for _ in range(2):
        response = client.post("/api/runtime/recover", json=body)
        assert response.status_code == 200
        assert response.json() == {"action": "restart", "version": "4.6.0"}
    assert len(recovery) == 1
    assert recovery[0].project == client.app.state.ctx.paths.root


def test_old_browser_reloads_without_killing_a_current_backend(client, recovery, monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "available_versions", lambda **_: ("4.5.0", "4.5.0", tmp_path))
    response = client.post("/api/runtime/recover", json={"frontend_version": "4.4.0", "backend_version": "4.5.0"})
    assert response.json() == {"action": "reload", "version": "4.5.0"}
    assert not recovery


def test_changed_or_matching_version_cannot_kill_the_server(client, recovery):
    for frontend, observed in [("4.5.0", "4.5.0"), ("4.6.0", "4.4.0")]:
        response = client.post("/api/runtime/recover", json={"frontend_version": frontend, "backend_version": observed})
        assert response.json()["action"] == "reload"
    assert not recovery


def test_incompatible_installed_files_are_reported_without_restart(client, recovery, monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "available_versions", lambda **_: ("4.6.0", "4.5.0", tmp_path))
    response = client.post("/api/runtime/recover", json={"frontend_version": "4.6.0", "backend_version": "4.5.0"})
    assert response.status_code == 409
    assert not recovery


def test_recovery_never_downgrades_a_newer_running_server(client, recovery, monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "available_versions", lambda **_: ("4.4.0", "4.4.0", tmp_path))
    result = client.post("/api/runtime/recover", json={"frontend_version": "4.4.0", "backend_version": "4.5.0"})
    assert result.status_code == 409
    assert not recovery


def test_restart_uses_the_existing_relauncher_and_preserves_credentials(token_client, monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(runtime, "build_info", lambda: SimpleNamespace(version="4.5.0"))
    monkeypatch.setattr(runtime, "available_versions", lambda **_: ("4.7.0", "4.7.0", tmp_path))
    (tmp_path / "run.ps1").write_text("# test launcher", encoding="utf-8")
    monkeypatch.setenv("VOXARIUM_STUDIO_RUN_DIR", str(tmp_path / "profile-run"))
    launched, stopped = [], []
    monkeypatch.setattr(runtime.UpdateService, "spawn_relauncher",
                        lambda self, payload_file, env=None: launched.append((json.loads(payload_file.read_text()), env)))
    monkeypatch.setattr(runtime.UpdateService, "request_shutdown", lambda self: stopped.append(True))
    response = token_client.post("/api/runtime/recover",
                                 json={"frontend_version": "4.7.0", "backend_version": "4.5.0"},
                                 headers={"Authorization": "Bearer t0ken"})
    assert response.status_code == 200 and len(launched) == 1 and stopped == [True]
    payload, env = launched[0]
    assert str(token_client.app.state.ctx.paths.root) in payload["command"]
    assert payload["cwd"] == str(tmp_path)
    assert str(tmp_path / "profile-run" / "recovery") in payload["log"]
    assert "t0ken" not in json.dumps(payload)
    assert env == {"VOXARIUM_STUDIO_TOKEN": "t0ken"}


def test_recovery_requires_the_existing_server_token(token_client, recovery):
    body = {"frontend_version": "4.6.0", "backend_version": "4.5.0"}
    assert token_client.post("/api/runtime/recover", json=body).status_code == 401
    result = token_client.post("/api/runtime/recover", json=body, headers={"Authorization": "Bearer t0ken"})
    assert result.status_code == 200
    assert recovery[0].token == "t0ken"


def test_failed_restart_is_reported_and_not_repeated(client, recovery, monkeypatch):
    async def fail(service, target):
        raise OSError("launcher unavailable")
    monkeypatch.setattr(runtime.UpdateService, "restart_current_files", fail)
    body = {"frontend_version": "4.6.0", "backend_version": "4.5.0"}
    assert client.post("/api/runtime/recover", json=body).status_code == 200
    repeated = client.post("/api/runtime/recover", json=body)
    assert repeated.status_code == 409 and "launcher unavailable" in repeated.text
