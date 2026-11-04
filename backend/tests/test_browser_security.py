"""The local API must not be usable by an unrelated website (§2.5)."""

from __future__ import annotations

import os
import runpy

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

from voxarium_studio.cli import app as cli
from voxarium_studio.core.security import InsecureBindError, verify_token
from voxarium_studio.main import create_app


@pytest.mark.parametrize("origin", ["https://attacker.example", "null", "http://127.0.0.1:9999"])
def test_cross_origin_requests_cannot_read_or_mutate(client, origin):
    assert client.get("/api/project", headers={"Origin": origin}).status_code == 403
    response = client.post("/api/plugins/core/disable", headers={"Origin": origin})
    assert response.status_code == 403
    assert client.app.state.ctx.config.plugins.get("core") is None


@pytest.mark.parametrize("host", ["attacker.example", "127.0.0.1.attacker.example", "localhost.attacker.example"])
def test_rebinding_host_is_denied_even_without_origin(client, host):
    assert client.get("/api/project", headers={"Host": host}).status_code == 403
    assert client.get("/api/project", headers={"Host": host, "Origin": f"http://{host}"}).status_code == 403


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1", "http://127.0.0.1:80"])
def test_local_browser_and_cli_requests_are_allowed(client, origin):
    assert client.get("/api/project", headers={"Origin": origin} if origin else {}).status_code == 200


@pytest.mark.parametrize("origin", ["https://attacker.example", "null"])
def test_cross_origin_websocket_cannot_read_transcripts(client, origin):
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("ws://127.0.0.1/ws", headers={"Origin": origin}):
            pytest.fail("untrusted browser received a WebSocket")
    assert caught.value.code == 1008


def test_rebinding_websocket_is_denied(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("ws://attacker.example/ws", headers={"Origin": "http://attacker.example"}):
            pytest.fail("DNS rebinding received a WebSocket")


def test_same_origin_websocket_works(client):
    with client.websocket_connect("ws://127.0.0.1/ws", headers={"Origin": "http://127.0.0.1"}) as socket:
        assert socket.receive_json()["type"] == "system.hello"


def test_vite_origin_is_only_allowed_in_dev(project):
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        assert client.get("/api/project", headers={"Origin": "http://localhost:5173"}).status_code == 403
    app = create_app(str(project), dev_origins=["http://localhost:5173"])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/project", headers={"Origin": "http://localhost:5173"}).status_code == 200
        with client.websocket_connect("ws://127.0.0.1/ws", headers={"Origin": "http://localhost:5173"}) as socket:
            assert socket.receive_json()["type"] == "system.hello"


def test_token_auth_still_applies_and_rejects_cross_origin(token_client):
    assert token_client.get("/api/project", headers={"Origin": "http://127.0.0.1"}).status_code == 401
    assert token_client.get("/api/project", headers={
        "Origin": "https://attacker.example", "Authorization": "Bearer t0ken",
    }).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with token_client.websocket_connect("ws://127.0.0.1/ws", headers={"Origin": "http://127.0.0.1"}):
            pytest.fail("WebSocket omitted the required token")
    with token_client.websocket_connect("ws://127.0.0.1/ws?token=t0ken", headers={"Origin": "http://127.0.0.1"}) as socket:
        assert socket.receive_json()["type"] == "system.hello"


def test_non_ascii_token_is_compared_without_a_server_error():
    assert verify_token("secret", "неверный") is False


def test_asgi_entry_point_refuses_unauthenticated_network_bind(project, monkeypatch):
    monkeypatch.setenv("VOXARIUM_STUDIO_PROJECT", str(project))
    monkeypatch.setenv("VOXARIUM_STUDIO_HOST", "0.0.0.0")
    monkeypatch.delenv("VOXARIUM_STUDIO_TOKEN", raising=False)
    monkeypatch.delenv("VOXARIUM_STUDIO_ALLOW_INSECURE", raising=False)
    with pytest.raises(InsecureBindError):
        runpy.run_module("voxarium_studio.asgi")


@pytest.mark.parametrize("insecure", [False, True])
def test_reload_preserves_the_auth_posture(project, monkeypatch, insecure):
    monkeypatch.delenv("VOXARIUM_STUDIO_TOKEN", raising=False)
    monkeypatch.setenv("VOXARIUM_STUDIO_PROJECT", str(project))
    monkeypatch.setenv("VOXARIUM_STUDIO_HOST", "127.0.0.1")
    monkeypatch.setenv("VOXARIUM_STUDIO_ALLOW_INSECURE", "0")
    monkeypatch.setenv("VOXARIUM_STUDIO_DEV", "1")
    captured = {}
    monkeypatch.setattr("voxarium_studio.cli.uvicorn.run", lambda *args, **kwargs: captured.update(os.environ))
    result = CliRunner().invoke(cli, ["run", str(project), "--reload", "--no-open", "--host", "0.0.0.0",
                                     *(["--allow-insecure"] if insecure else ["--token", "test-token"])])
    assert result.exit_code == 0, result.output
    assert captured["VOXARIUM_STUDIO_HOST"] == "0.0.0.0"
    assert captured["VOXARIUM_STUDIO_DEV"] == "0"
    assert captured["VOXARIUM_STUDIO_ALLOW_INSECURE"] == ("1" if insecure else "0")
    assert captured.get("VOXARIUM_STUDIO_TOKEN") == (None if insecure else "test-token")


@pytest.mark.parametrize("name", ["report.html", "diagram.svg"])
def test_review_active_content_is_downloaded_and_sandboxed(client, name):
    task = client.post("/api/teams/team_default/tasks", json={
        "assignee_id": "team_default__dev", "title": "Attachment",
    }).json()
    url = f"/api/teams/team_default/tasks/{task['id']}/attachments"
    assert client.post(url, files={"file": (name, b"<script>alert(1)</script>")}).status_code == 201
    response = client.get(f"{url}/{name}")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_svg_document_images_are_sandboxed_even_when_opened_as_a_page(client):
    directory = client.app.state.ctx.paths.docs
    (directory / "diagram.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    response = client.get("/api/docs/asset", params={"path": "guide.md", "src": "diagram.svg"})
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("inline")
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
