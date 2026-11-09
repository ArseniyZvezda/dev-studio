"""Required Teams materials persist and reach real orchestration before model dispatch (§10.10)."""

from pathlib import Path

import pytest
import yaml

from voxarium_studio.core.config import TeamsConfig, load_config
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.models.teams import AgentMessage, TaskStatus, TeamMember
from voxarium_studio.teams.backends import base
from voxarium_studio.teams.backends.base import AgentEvent
from voxarium_studio.teams.chat import UserContext
from voxarium_studio.teams.docs import DocStore
from voxarium_studio.teams.installed_skills import InstalledSkill
from voxarium_studio.teams.project_skills import SkillStore
from voxarium_studio.teams.required_context import checked_documents, load_required_context
from voxarium_studio.teams import skill as studio_skill
from voxarium_studio.teams.skill import rule_path

TEAM = "team_default"
DEV = f"{TEAM}__dev"


def materials(project: Path):
    paths = ProjectPaths(project)
    for name in ("shared-skill", "member-skill", "optional-skill"):
        folder = paths.skills / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\ndescription: {name}\n---\nACTIVATE {name}", encoding="utf-8")
    paths.docs.mkdir(parents=True, exist_ok=True)
    (paths.docs / "required.md").write_text("# Required\nДокумент целиком\n", encoding="utf-8")
    (paths.docs / "local.md").write_text("LOCAL DOCUMENT", encoding="utf-8")
    return paths


def test_global_and_member_requirements_persist_and_deduplicate(client, project):
    paths = materials(project)
    response = client.put("/api/settings", json={"teams": {
        "required_skills": ["shared-skill", "shared-skill"],
        "required_documents": ["required", "required.md"],
    }})
    assert response.status_code == 200, response.text
    config = load_config(paths).teams
    assert config.required_skills == ["shared-skill"]
    assert config.required_documents == ["required.md"]
    endpoint = f"/api/teams/{TEAM}/members/{DEV}"
    response = client.patch(endpoint, json={
        "required_skills": ["member-skill", "member-skill"],
        "required_documents": ["local.md"],
    })
    assert response.status_code == 200, response.text
    assert response.json()["required_skills"] == ["member-skill"]
    roster = yaml.safe_load((paths.teams / "default-team.yaml").read_text(encoding="utf-8"))
    member = next(row for row in roster["members"] if row["id"] == "dev")
    assert member["required_skills"] == ["member-skill"]
    assert member["required_documents"] == ["local.md"]
    # Partial settings writes retain requirements; absent fields never clear them.
    assert client.put("/api/settings", json={"teams": {"max_turns": 41}}).status_code == 200
    assert load_config(paths).teams.required_documents == ["required.md"]
    assert client.patch(endpoint, json={"required_skills": None}).json()["required_skills"] == []
    assert client.get(f"/api/teams/{TEAM}").json()["members"]


@pytest.mark.parametrize("patch", [
    {"required_skills": ["missing-skill"]},
    {"required_documents": ["missing.md"]},
    {"required_documents": ["../secret.md"]},
    {"required_documents": ["/secret.md"]},
    {"required_documents": ["C:\\secret.md"]},
])
def test_invalid_new_requirements_fail_atomically(client, patch):
    before = client.get("/api/settings").json()
    assert client.put("/api/settings", json={"teams": patch, "project_name": "Do not save"}).status_code == 400
    assert client.get("/api/settings").json() == before
    endpoint = f"/api/teams/{TEAM}/members/{DEV}"
    assert client.patch(endpoint, json=patch).status_code == 400


def test_missing_requirement_can_be_kept_or_removed_and_required_skill_cannot_be_deleted(client, project):
    paths = materials(project)
    endpoint = f"/api/teams/{TEAM}/members/{DEV}"
    assert client.patch(endpoint, json={"required_skills": ["member-skill"],
        "required_documents": ["required.md", "local.md"]}).status_code == 200
    assert client.put("/api/settings", json={"teams": {"required_skills": ["shared-skill"]}}).status_code == 200
    listing = client.get("/api/skills").json()["skills"]
    assert DEV in {m["id"] for s in listing if s["name"] == "member-skill" for m in s["used_by"]}
    assert DEV in {m["id"] for s in listing if s["name"] == "shared-skill" for m in s["used_by"]}
    assert client.delete("/api/skills/member-skill").status_code == 409
    assert client.delete("/api/skills/shared-skill").status_code == 409
    assert (paths.skills / "member-skill" / "SKILL.md").exists()
    (paths.docs / "required.md").unlink()
    # An unavailable old selection must not prevent removing another item.
    assert client.patch(endpoint, json={"required_documents": ["required.md"]}).status_code == 200
    assert client.patch(endpoint, json={"required_documents": []}).status_code == 200


def test_full_content_deduplicated_and_read_fresh_without_editor_or_summary_limits(project):
    paths = materials(project)
    body = "# Large document\r\n" + "Полный текст.\r\n" * 90_000 + "FINAL DOCUMENT LINE"
    with (paths.docs / "required.md").open("w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    skill_body = "# Required instructions\n" + "Do every step.\n" * 90_000 + "FINAL SKILL LINE"
    (paths.skills / "shared-skill" / "SKILL.md").write_text(skill_body, encoding="utf-8")
    settings = TeamsConfig(required_skills=["shared-skill"], required_documents=["required.md"])
    member = TeamMember(team_id=TEAM, name="Dev", required_skills=["shared-skill", "member-skill"],
                        required_documents=["required", "local.md"])
    skills, docs = SkillStore(paths), DocStore(paths)
    context = load_required_context(settings, member, skills, docs)
    assert [s.name for s in context.skills.found] == ["shared-skill", "member-skill"]
    assert context.documents == ("required.md", "local.md")
    assert context.text.index("BEGIN REQUIRED SKILL: voxarium_studio:studio") < context.text.index(
        "BEGIN REQUIRED SKILL: shared-skill")
    assert "# Working inside Voxarium Studio" in context.text
    assert body in context.text and skill_body in context.text
    assert context.text.count("FINAL DOCUMENT LINE") == context.text.count("FINAL SKILL LINE") == 1
    assert context.prepend("Assignment").endswith("\n\nAssignment")
    (paths.docs / "required.md").write_text("CURRENT CONTENT", encoding="utf-8")
    assert "CURRENT CONTENT" in load_required_context(settings, member, skills, docs).text


def test_invalid_old_document_requirement_can_be_removed(project):
    paths = materials(project)
    assert checked_documents(DocStore(paths), [], ["../outside.md", "C:/secret.md"]) == []
    assert checked_documents(DocStore(paths), ["required.md"], ["../outside.md"]) == ["required.md"]


def test_required_installed_skill_survives_backend_change(project):
    paths = materials(project)
    name = "installed:" + "a" * 32
    installed = InstalledSkill(name=name, directory=paths.skills / "member-skill",
        source="codex", scope="global", backends=("codex",), display_name="Installed skill")
    skills = SkillStore(paths, installed=lambda: {name: installed})
    member = TeamMember(team_id=TEAM, name="Dev", agent_backend="qwen", required_skills=[name])
    context = load_required_context(TeamsConfig(), member, skills, DocStore(paths))
    assert "ACTIVATE member-skill" in context.text
    assert context.skills.found[0].name == name


@pytest.fixture
async def recorded(ctx, monkeypatch):
    requests = []

    class Backend:
        named_agents = False
        session_recovery = True

        def is_available(self):
            return True, "test backend"

        async def run(self, request):
            requests.append(request)
            yield AgentEvent(kind="result", text="Done", is_final=True, session_id="required-session")

    monkeypatch.setitem(base._BACKENDS, "required_test", Backend)
    ctx.config.teams.context_compression_tokens = 0
    ctx.config.teams.result_follow_up = False
    materials(ctx.paths.root)
    ctx.config.teams.required_skills = ["shared-skill"]
    ctx.config.teams.required_documents = ["required.md"]
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.agent_backend = "required_test"
        member.isolate_worktree = False
        member.skills = ["optional-skill"]
        member.required_skills = ["shared-skill", "member-skill"]
        member.required_documents = ["required.md", "local.md"]
        session.add(member)
    return ctx, requests


async def launch(ctx, surface):
    if surface == "chat":
        job = await ctx.chats.create_chat(DEV)
        await ctx.chats.send(job.id, "Start assignment", UserContext())
    else:
        job = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV,
            title="Required materials", brief="Start assignment", auto_start=False)
        await ctx.teams.update_task(job.id, {"status": TaskStatus.PENDING})
        await ctx.teams.start_task(job.id)
    return job


@pytest.mark.parametrize("surface", ["task", "chat"])
async def test_required_context_initial_only_and_complete_on_recovery(recorded, wait_for, surface):
    ctx, requests = recorded
    job = await launch(ctx, surface)
    await wait_for(job.id)
    first = requests[-1]
    assert first.resume_session_id is None
    for text in ("ACTIVATE shared-skill", "ACTIVATE member-skill", "Документ целиком", "LOCAL DOCUMENT"):
        assert first.prompt.count(text) == 1
    assert first.prompt.count("--- BEGIN REQUIRED SKILL: voxarium_studio:studio ---") == 1
    assert "ACTIVATE optional-skill" not in first.prompt
    assert "optional-skill" in first.system_prompt
    permission = rule_path(ctx.paths.skills / "member-skill")
    if permission:
        assert f"Read({permission}/**)" in first.extra_allowed_tools
    if surface == "chat":
        await ctx.chats.send(job.id, "Continue assignment", UserContext())
    else:
        await ctx.teams.request_changes(job.id, "Continue assignment")
    await wait_for(job.id)
    resumed = requests[-1]
    assert resumed.resume_session_id == "required-session"
    assert "ACTIVATE shared-skill" not in resumed.prompt
    assert "Документ целиком" not in resumed.prompt
    assert resumed.recovery_prompt.count("ACTIVATE shared-skill") == 1
    assert resumed.recovery_prompt.count("Документ целиком") == 1
    assert resumed.recovery_prompt.count("--- BEGIN REQUIRED SKILL: voxarium_studio:studio ---") == 1


@pytest.mark.parametrize("surface", ["task", "chat"])
async def test_studio_skill_autoloads_without_selected_requirements(recorded, wait_for, surface):
    ctx, requests = recorded
    ctx.config.teams.required_skills = []
    ctx.config.teams.required_documents = []
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.required_skills = []
        member.required_documents = []
        session.add(member)
    job = await launch(ctx, surface)
    await wait_for(job.id)
    request = requests[-1]
    assert request.prompt.count("--- BEGIN REQUIRED SKILL: voxarium_studio:studio ---") == 1
    assert "--- BEGIN REQUIRED SKILL: voxarium_studio:producer ---" not in request.prompt
    assert "voxarium_studio:producer" not in request.skill_files
    assert "# Working inside Voxarium Studio" in request.prompt
    assert str(studio_skill.BUNDLE / "skills" / "studio" / "SKILL.md") in request.prompt
    assert "BEGIN REQUIRED DOCUMENT" not in request.prompt


def test_selected_studio_skill_is_not_loaded_twice(project):
    paths = materials(project)
    skills = SkillStore(paths, builtins=lambda: {
        studio_skill.SKILL_NAME: (studio_skill.BUNDLE / "skills" / "studio", "voxarium_studio"),
    })
    member = TeamMember(team_id=TEAM, name="Dev", required_skills=[studio_skill.SKILL_NAME])
    context = load_required_context(TeamsConfig(required_skills=[studio_skill.SKILL_NAME]),
                                    member, skills, DocStore(paths))
    assert context.text.count("--- BEGIN REQUIRED SKILL: voxarium_studio:studio ---") == 1


def test_producer_skill_loads_only_for_active_mode_and_is_not_duplicated(project):
    paths = materials(project)
    producer_name = studio_skill.PRODUCER_SKILL_NAME
    skills = SkillStore(paths, builtins=lambda: {
        producer_name: (studio_skill.BUNDLE / "skills" / "producer", "voxarium_studio"),
    })
    member = TeamMember(team_id=TEAM, name="Producer", required_skills=[producer_name])
    context = load_required_context(TeamsConfig(), member, skills, DocStore(paths), producer_mode=True)
    assert context.text.count(f"--- BEGIN REQUIRED SKILL: {producer_name} ---") == 1
    assert context.text.index("voxarium_studio:studio") < context.text.index(producer_name)
    ordinary = load_required_context(TeamsConfig(), TeamMember(team_id=TEAM, name="Dev"),
                                     skills, DocStore(paths))
    assert f"--- BEGIN REQUIRED SKILL: {producer_name} ---" not in ordinary.text


@pytest.mark.parametrize("surface", ["task", "chat"])
@pytest.mark.parametrize("missing", ["skill", "document"])
async def test_missing_requirements_fail_before_backend_call(recorded, wait_for, surface, missing):
    ctx, requests = recorded
    if missing == "skill":
        (ctx.paths.skills / "member-skill" / "SKILL.md").unlink()
    else:
        (ctx.paths.docs / "required.md").unlink()
    job = await launch(ctx, surface)
    await wait_for(job.id)
    assert requests == []
    if surface == "chat":
        saved = await ctx.chats.get_chat(job.id)
        assert "required" in saved.error.lower()
    else:
        saved = await ctx.teams.find_task(job.id)
        assert saved.status == TaskStatus.BLOCKED
        messages = await ctx.teams._messages_of(job.id)
        assert any("required" in message.text.lower() for message in messages if message.kind == "error")


async def test_task_compaction_restores_full_required_context(recorded, wait_for, monkeypatch):
    ctx, requests = recorded
    job = await launch(ctx, "task")
    await wait_for(job.id)

    async def compress(_backend, request):
        async with ctx.db.session() as session:
            task = await ctx.teams.find_task(job.id)
            task.agent_session_id = None
            session.add(task)
            session.add(AgentMessage(task_id=job.id, member_id=DEV, role="system",
                                     kind="compressed summary", text="COMPACTED HISTORY"))

    monkeypatch.setattr(ctx.teams, "_auto_compress", compress)
    await ctx.teams.request_changes(job.id, "After compaction")
    await wait_for(job.id)
    fresh = requests[-1]
    assert fresh.resume_session_id is None
    assert "COMPACTED HISTORY" in fresh.prompt
    assert fresh.prompt.count("ACTIVATE shared-skill") == 1
    assert fresh.prompt.count("Документ целиком") == 1
