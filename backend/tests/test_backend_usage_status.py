"""The status endpoint exposes only normalized account quota snapshots (§6.1)."""

from fastapi.testclient import TestClient

from voxarium_studio.api import project


def test_status_includes_backend_usage_without_changing_readiness(client: TestClient, monkeypatch):
    limits = [{"window": "primary", "used_percent": 42.5, "resets_at": None}]
    monkeypatch.setattr(project, "get_usage_limits", lambda name: limits if name == "codex" else [])
    response = client.get("/api/status")
    assert response.status_code == 200
    backends = response.json()["agent_backends"]
    assert backends["codex"]["usage_limits"] == limits
    assert backends["claude_code"]["usage_limits"] == []
    assert isinstance(backends["codex"]["available"], bool)
    assert isinstance(backends["codex"]["reason"], str)
