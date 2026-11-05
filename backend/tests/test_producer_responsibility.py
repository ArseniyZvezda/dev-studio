"""The built-in Producer's responsibility is a projection of Settings (§10.11)."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from voxarium_studio.core.prompts import AgentPromptsConfig, SPECS
from voxarium_studio.main import create_app
from voxarium_studio.models.teams import TeamMember

TEAM = "team_default"
PRODUCER = f"{TEAM}__studio-producer"
MEMBER_URL = f"/api/teams/{TEAM}/members/{PRODUCER}"


def producer_member(client: TestClient) -> dict:
    response = client.get(f"/api/teams/{TEAM}")
    assert response.status_code == 200
    return next(member for member in response.json()["members"] if member["id"] == PRODUCER)


def test_builtin_responsibility_uses_the_effective_settings_template(client, project: Path):
    expected = SPECS["producer_mode"].default
    assert producer_member(client)["responsibility"] == expected
    listed = client.get("/api/teams").json()
    assert next(member for team in listed for member in team["members"]
                if member["id"] == PRODUCER)["responsibility"] == expected
    roster = yaml.safe_load((project / "voxarium_studio/teams/default-team.yaml").read_text("utf-8"))
    assert "responsibility" not in next(m for m in roster["members"] if m["id"] == "studio-producer")


@pytest.mark.parametrize("replacement", ["Follow this custom plan. Speak {language}.", None, "  \n"])
def test_settings_edits_and_reset_immediately_update_responsibility(client, replacement):
    first = client.put("/api/settings", json={"teams": {"prompts": {"producer_mode": "Old prompt"}}})
    assert first.status_code == 200
    response = client.put(
        "/api/settings", json={"teams": {"prompts": {"producer_mode": replacement}}},
    )
    assert response.status_code == 200
    expected = replacement if replacement and replacement.strip() else SPECS["producer_mode"].default
    assert producer_member(client)["responsibility"] == expected
    # Editing a separate member setting must neither restore an old role nor persist a copy.
    changed = client.patch(MEMBER_URL, json={"model": "custom-model"})
    assert changed.status_code == 200
    assert changed.json()["responsibility"] == expected


@pytest.mark.parametrize("value", ["Independent role override", "", None])
def test_responsibility_patch_is_rejected_without_partial_member_changes(client, value):
    before = producer_member(client)
    response = client.patch(MEMBER_URL, json={"responsibility": value, "name": "Should not change"})
    assert response.status_code == 400
    assert "Producer System Prompt" in response.json()["detail"]
    assert producer_member(client) == before


def test_ordinary_member_responsibility_stays_editable(client):
    response = client.patch(
        f"/api/teams/{TEAM}/members/{TEAM}__dev", json={"responsibility": "Implement the agreed design."},
    )
    assert response.status_code == 200
    assert response.json()["responsibility"] == "Implement the agreed design."


async def test_legacy_yaml_override_is_ignored_and_dropped_on_roster_save(ctx):
    store = ctx.teams.store
    path = store.file_for(TEAM)
    roster = yaml.safe_load(path.read_text("utf-8"))
    producer = next(member for member in roster["members"] if member["id"] == "studio-producer")
    producer["responsibility"] = "Old independently edited producer role"
    producer["model"] = "custom-model"
    path.write_text(yaml.safe_dump(roster), encoding="utf-8")
    ctx.config.teams.prompts = AgentPromptsConfig(producer_mode="The current prompt in {language}.")

    await ctx.teams.sync_definitions()

    async with ctx.db.session() as session:
        member = await session.get(TeamMember, PRODUCER)
        assert member.responsibility == "The current prompt in {language}."
        assert member.model == "custom-model"
        await store.save_from_db(session, TEAM)
    saved = yaml.safe_load(path.read_text("utf-8"))
    assert "responsibility" not in next(m for m in saved["members"] if m["id"] == "studio-producer")


async def test_settings_sync_updates_every_producer_including_borrowed_agents(ctx):
    second = ctx.teams.store.directory / "second.yaml"
    second.write_text("id: second\nname: Second\nmembers: []\n", encoding="utf-8")
    await ctx.teams.sync_definitions()
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, PRODUCER)
        member.agent = "user-agent"
        session.add(member)
    ctx.config.teams.prompts = AgentPromptsConfig(producer_mode="Updated shared instructions")

    await ctx.teams.sync_agent_definitions()

    async with ctx.db.session() as session:
        for member_id in (PRODUCER, "second__studio-producer"):
            member = await session.get(TeamMember, member_id)
            assert member.responsibility == "Updated shared instructions"


def test_settings_binding_survives_project_restart(project: Path):
    custom = "Stored Producer System Prompt. Speak {language}."
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        response = client.put("/api/settings", json={"teams": {"prompts": {"producer_mode": custom}}})
        assert response.status_code == 200
        assert client.patch(MEMBER_URL, json={"model": "custom-model"}).status_code == 200

    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        member = producer_member(client)
        assert member["responsibility"] == custom
        assert member["model"] == "custom-model"
        roster = yaml.safe_load((project / "voxarium_studio/teams/default-team.yaml").read_text("utf-8"))
        assert "responsibility" not in next(m for m in roster["members"] if m["id"] == "studio-producer")


@pytest.mark.parametrize("replacement", ["Reloaded prompt. Speak {language}.", None])
def test_reload_of_hand_edited_config_refreshes_responsibility(client, project: Path, replacement):
    assert client.put(
        "/api/settings", json={"teams": {"prompts": {"producer_mode": "Previous prompt"}}},
    ).status_code == 200
    path = project / "voxarium_studio/config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["teams"]["prompts"]["producer_mode"] = replacement
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    response = client.post("/api/settings/reload")

    assert response.status_code == 200
    assert producer_member(client)["responsibility"] == (replacement or SPECS["producer_mode"].default)
