"""Shared Team Settings and explicitly enabled member overrides (§10.3)."""

from pathlib import Path

import pytest
import yaml

from voxarium_studio.core.config import load_config
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.models.teams import TaskStatus, TeamMember, TeamTask
from voxarium_studio.teams.backends import base, codex, qwen
from voxarium_studio.teams.backends.base import AgentEvent, AgentRunRequest
from voxarium_studio.teams.chat import UserContext


TEAM = "team_default"
DEV = f"{TEAM}__dev"


def test_shared_settings_persist_and_partial_updates_preserve_prompts(client, project: Path):
    before = client.get("/api/settings").json()["teams"]
    assert (before["permission_mode"], before["max_turns"]) == ("bypassPermissions", 40)
    response = client.put("/api/settings", json={"teams": {
        "permission_mode": "plan", "max_turns": 83,
    }})
    assert response.status_code == 200
    assert response.json()["teams"] == {**before, "permission_mode": "plan", "max_turns": 83}
    loaded = load_config(ProjectPaths(project)).teams
    assert (loaded.permission_mode, loaded.max_turns) == ("plan", 83)
    after = client.put("/api/settings", json={"teams": {"max_turns": 18}}).json()["teams"]
    assert after == {**before, "permission_mode": "plan", "max_turns": 18}


@pytest.mark.parametrize("patch", [
    {"max_turns": 0}, {"max_turns": 1001}, {"max_turns": None},
    {"permission_mode": "unknown"}, {"permission_mode": None},
])
def test_invalid_shared_settings_are_not_saved(client, patch):
    before = client.get("/api/settings").json()["teams"]
    assert client.put("/api/settings", json={"teams": patch}).status_code == 422
    assert client.get("/api/settings").json()["teams"] == before


def test_override_switch_round_trips_without_erasing_local_values(client, project: Path):
    endpoint = f"/api/teams/{TEAM}/members/{DEV}"
    edited = client.patch(endpoint, json={
        "permission_mode": "acceptEdits", "max_turns": 9,
    })
    assert edited.status_code == 200
    assert edited.json()["run_settings_override"] is False
    enabled = client.patch(endpoint, json={"run_settings_override": True})
    assert enabled.status_code == 200 and enabled.json()["run_settings_override"] is True
    roster_path = ProjectPaths(project).teams / "default-team.yaml"
    member = next(row for row in yaml.safe_load(roster_path.read_text(encoding="utf-8"))["members"]
                  if row["id"] == "dev")
    assert (member["run_settings_override"], member["permission_mode"], member["max_turns"]) == (
        True, "acceptEdits", 9,
    )
    disabled = client.patch(endpoint, json={"run_settings_override": False}).json()
    assert (disabled["run_settings_override"], disabled["permission_mode"], disabled["max_turns"]) == (
        False, "acceptEdits", 9,
    )
    assert client.patch(endpoint, json={"run_settings_override": None}).status_code == 422
    assert client.patch(endpoint, json={"max_turns": None}).status_code == 400


@pytest.mark.parametrize("surface", ["task", "chat"])
@pytest.mark.parametrize("override", [False, True])
async def test_tasks_and_chats_resolve_live_shared_settings(ctx, wait_for, monkeypatch, surface, override):
    requests = []

    class Backend:
        named_agents = False

        def is_available(self):
            return True, "test backend"

        async def run(self, request):
            requests.append(request)
            yield AgentEvent(kind="result", text="Done", is_final=True)

    monkeypatch.setitem(base._BACKENDS, "shared_settings_test", Backend)
    ctx.config.teams.context_compression_tokens = 0
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.agent_backend = "shared_settings_test"
        member.isolate_worktree = False
        member.run_settings_override = override
        member.permission_mode = "acceptEdits"
        member.max_turns = 7
        session.add(member)

    for mode, turns in (("plan", 91), ("default", 22)):
        ctx.config.teams.permission_mode = mode
        ctx.config.teams.max_turns = turns
        if surface == "task":
            job = await ctx.teams.create_task(
                team_id=TEAM, assignee_id=DEV, title="Shared defaults", brief="Inspect settings",
                auto_start=False,
            )
            await ctx.teams.update_task(job.id, {"status": TaskStatus.PENDING})
            await ctx.teams.start_task(job.id)
        else:
            job = await ctx.chats.create_chat(DEV)
            await ctx.chats.send(job.id, "Inspect settings", UserContext())
        await wait_for(job.id)
        request = requests[-1]
        expected = ("acceptEdits", 7) if override else (mode, turns)
        assert (request.permission_mode, request.max_turns) == expected
        assert (request.member.permission_mode, request.member.max_turns) == ("acceptEdits", 7)


def test_codex_and_qwen_use_effective_permissions_not_dormant_member_values(tmp_path):
    request = AgentRunRequest(
        member=TeamMember(team_id=TEAM, name="Dev", permission_mode="bypassPermissions"),
        task=TeamTask(team_id=TEAM, title="Inspect"), workdir=tmp_path, prompt="Inspect",
        system_prompt="", permission_mode="plan", max_turns=11,
    )
    _, argv, _ = codex._build_command(["codex"], request)
    assert 'sandbox_mode="read-only"' in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    argv, _, _ = qwen._command(["qwen"], request, tmp_path / "mcp.json")
    assert argv[argv.index("--approval-mode") + 1] == "plan"
    assert "--allowed-tools" not in argv
