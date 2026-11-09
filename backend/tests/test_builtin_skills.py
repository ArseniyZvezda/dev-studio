"""Built-in instructions share the Skills catalog and roster without project copies (§10.10)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from voxarium_studio.core.context import AppContext
from voxarium_studio.main import create_app
from voxarium_studio.teams import skill as studio_skill
from voxarium_studio.teams.project_skills import prompt_list

MEMBER = "/api/teams/team_default/members/team_default__dev"


def test_builtin_skills_are_readable_assignable_and_protected(client: TestClient, project: Path) -> None:
    listed = {entry["name"]: entry for entry in client.get("/api/skills").json()["skills"]}
    assert {"graphify", "voxarium_studio:studio", "voxarium_studio:producer"} <= listed.keys()
    assert listed["graphify"]["source"] == "graphify"
    assert listed["voxarium_studio:studio"]["source"] == "voxarium_studio"
    assert listed["voxarium_studio:producer"]["source"] == "voxarium_studio"
    assert "documents.md" in listed["voxarium_studio:studio"]["files"]
    assert not list((project / ".voxarium_studio" / "skills").iterdir()), "no stale package copies"

    assigned = client.patch(MEMBER, json={"skills": ["graphify", "voxarium_studio:studio"]})
    assert assigned.status_code == 200, assigned.text
    for name in ("graphify", "voxarium_studio:studio"):
        detail = client.get(f"/api/skills/{name}")
        assert detail.status_code == 200, detail.text
        data = detail.json()
        assert data["warning"] is None and data["description"]
        assert Path(data["path"], "SKILL.md").read_text(encoding="utf-8") == data["content"]
        assert [user["id"] for user in data["used_by"]] == ["team_default__dev"]
        assert client.put(f"/api/skills/{name}", json={"content": "overwrite"}).status_code == 403
        assert client.delete(f"/api/skills/{name}").status_code == 403
        assert client.get(f"/api/skills/{name}").json()["content"] == data["content"]
    assert client.post("/api/skills", json={"name": "graphify"}).status_code == 409
    assert client.get("/api/skills/VoxariumStudio:studio").status_code == 400

    # A new process still reads the roster by public ids, including the namespaced skill.
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as reopened:
        team = reopened.get("/api/teams/team_default").json()
        member = next(item for item in team["members"] if item["id"] == "team_default__dev")
        assert member["skills"] == ["graphify", "voxarium_studio:studio"]


def test_plugin_reload_withdraws_and_restores_its_skill(client: TestClient) -> None:
    assert client.patch(MEMBER, json={"skills": ["graphify"]}).status_code == 200
    assert client.post("/api/plugins/graphify/disable").status_code == 200
    page = client.get("/api/skills").json()
    assert "graphify" not in [entry["name"] for entry in page["skills"]]
    assert [entry["name"] for entry in page["missing"]] == ["graphify"]
    assert "voxarium_studio:studio" in [entry["name"] for entry in page["skills"]]
    assert client.post("/api/plugins/graphify/enable").status_code == 200
    assert client.get("/api/skills/graphify").status_code == 200
    assert client.get("/api/skills").json()["missing"] == []


async def test_sessions_resolve_builtin_paths_and_reference_permissions(ctx: AppContext) -> None:
    resolved = ctx.skills.for_session([])
    assert {skill.name for skill in resolved.found} == {"graphify", "voxarium_studio:studio"}
    assert len(ctx.skills.for_session(["graphify", "voxarium_studio:studio"]).found) == 2
    assert {skill.name for skill in ctx.skills.for_session([], producer_mode=True).found} == {
        "graphify", "voxarium_studio:studio", "voxarium_studio:producer",
    }
    assert not resolved.missing
    prompt = prompt_list(resolved.found)
    for skill in resolved.found:
        assert str(skill.skill_file) in prompt
        assert skill.skill_file.is_file()
        rule = studio_skill.rule_path(skill.directory)
        if rule:
            assert f"Read({rule}/**)" in resolved.allowed_tools()

    ctx.config.plugin_state("graphify").enabled = False
    await ctx.reload_plugins()
    assert [skill.name for skill in ctx.skills.for_session([]).found] == ["voxarium_studio:studio"]
    assert ctx.skills.for_session(["graphify"]).missing == ("graphify",)


def test_plugin_skill_cannot_escape_its_package(project: Path) -> None:
    root = project / ".voxarium_studio" / "plugins" / "skills_test"
    root.mkdir(parents=True)
    outside = project / ".voxarium_studio" / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\ndescription: Outside.\n---\n", encoding="utf-8")
    (root / "plugin.yaml").write_text(
        "id: skills_test\nname: Skills test\nskills: [../../outside]\n", encoding="utf-8",
    )
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        names = [entry["name"] for entry in client.get("/api/skills").json()["skills"]]
        assert "outside" not in names
