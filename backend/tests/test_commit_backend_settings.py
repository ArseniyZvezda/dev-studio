"""Commit description routing and project settings (§10.4); no paid model calls."""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voxarium_studio.core.config import load_config
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.teams import commit_proposal
from voxarium_studio.teams.backends import AgentEvent
from voxarium_studio.teams.backends import base


def test_settings_persist_and_merge(client: TestClient, project: Path) -> None:
    before = client.get("/api/settings").json()["teams"]
    assert before["commit_backend"] == "claude_code"
    response = client.put("/api/settings", json={"teams": {
        "commit_backend": "codex", "commit_model": "custom-model",
    }})
    assert response.status_code == 200
    teams = response.json()["teams"]
    assert teams == {**before, "commit_backend": "codex", "commit_model": "custom-model"}
    assert load_config(ProjectPaths(project)).teams.commit_model == "custom-model"
    assert client.put("/api/settings", json={"teams": {"commit_backend": "missing"}}).status_code == 400
    assert client.get("/api/settings").json()["teams"] == teams


async def test_claude_keeps_existing_sdk_call(monkeypatch, tmp_path: Path) -> None:
    async def fake(prompt, model, cwd):
        assert (prompt, model, cwd) == ("diff", "haiku", tmp_path)
        return "Description"
    monkeypatch.setattr(commit_proposal, "describe_with_claude", fake)
    assert await commit_proposal.describe_with_backend("diff", "haiku", tmp_path, "claude_code") == "Description"


@pytest.mark.parametrize("name", ["codex", "qwen"])
@pytest.mark.parametrize("outcome", ["success", "error", "empty", "timeout"])
async def test_backend_routing_and_cleanup(monkeypatch, tmp_path: Path, name, outcome) -> None:
    requests = []
    closed = []

    class Fake:
        def is_available(self):
            return True, ""

        async def run(self, request):
            requests.append(request)
            try:
                if outcome == "timeout":
                    await asyncio.sleep(10)
                if outcome == "error":
                    yield AgentEvent(kind="result", text="Failed", data={"is_error": True})
                elif outcome == "success":
                    yield AgentEvent(kind="text", text="Draft")
                    yield AgentEvent(kind="result", text="Final description")
            finally:
                closed.append(True)

    monkeypatch.setitem(base._BACKENDS, name, Fake)
    monkeypatch.setattr(commit_proposal, "DESCRIBE_TIMEOUT", 0.05)
    if outcome == "success":
        assert await commit_proposal.describe_with_backend("diff", "chosen-model", tmp_path, name) == "Final description"
    else:
        with pytest.raises(RuntimeError):
            await commit_proposal.describe_with_backend("diff", "chosen-model", tmp_path, name)
    request = requests[0]
    assert request.model == "chosen-model"
    assert request.member.agent_backend == name
    assert request.member.permission_mode == "plan"
    assert request.member.allowed_tools == []
    assert request.mcp_servers == {} and request.strict_mcp_config
    assert request.workdir != tmp_path and not request.workdir.exists()
    assert closed == [True]
