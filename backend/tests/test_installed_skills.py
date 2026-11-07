"""Installed skills join the catalog without acquiring write access to an agent's home."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.teams.installed_skills import discover_installed_skills
from voxarium_studio.teams.project_skills import SkillStore, check_name, prompt_list


def skill(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text("---\ndescription: Installed instructions.\n---\n\nDo work.\n", encoding="utf-8")
    return folder


def test_catalog_covers_shared_backend_project_and_nested_skills(
    project: Path, agent_skills_home: Path,
) -> None:
    home = agent_skills_home
    shared = skill(home / ".agents/skills/synced/package/shared-work")
    for backend in ("claude", "codex", "qwen"):
        skill(home / f".{backend}/skills/same-name")
        skill(project / f".{backend}/skills/same-name")
    skill(home / ".codex/skills/.system/system-work")
    skill(project / ".agents/skills/project-shared")
    skill(project / ".voxarium_studio/skills/same-name")
    store = SkillStore(ProjectPaths.for_project(project))
    catalog, invalid = store.scan()
    assert not invalid
    assert len(catalog) == 10
    assert len({entry.name for entry in catalog}) == 10
    assert sum(entry.display_name == "same-name" for entry in catalog) == 7
    installed = [entry for entry in catalog if entry.readonly]
    assert len(installed) == 9
    assert all(not entry.automatic and entry.source for entry in installed)
    assert {entry.source for entry in installed} == {"shared", "claude_code", "codex", "qwen"}
    common = next(entry for entry in installed if entry.directory == shared)
    assert common.scope == "global" and common.backends == ()
    assert all(check_name(entry.name) == entry.name for entry in installed)
    assert not store.for_session([]).found, "installed skills are offered, never auto-assigned"
    assigned = store.for_session([common.name])
    assert assigned.found == (common,)
    assert not assigned.missing
    assert str(shared / "SKILL.md") in prompt_list(assigned.found)
    assert "shared-work" in prompt_list(assigned.found)
    assert assigned.allowed_tools()


def test_external_skill_api_assignment_roundtrip_readonly_and_missing(
    client: TestClient, project: Path, agent_skills_home: Path,
) -> None:
    directory = skill(agent_skills_home / ".agents/skills/external-work")
    overview = client.get("/api/skills").json()
    entry = next(item for item in overview["skills"] if item["display_name"] == "external-work")
    name = entry["name"]
    assert entry["readonly"] and not entry["automatic"] and entry["backends"] == []
    assert entry["scope"] == "global" and entry["source"] == "shared"
    member = "/api/teams/team_default/members/team_default__dev"
    response = client.patch(member, json={"skills": [name]})
    assert response.status_code == 200, response.text
    detail = client.get(f"/api/skills/{name}")
    assert detail.status_code == 200 and "Do work." in detail.json()["content"]
    assert detail.json()["used_by"][0]["id"] == "team_default__dev"
    assert any(name in path.read_text(encoding="utf-8") for path in (project / ".voxarium_studio/teams").glob("*.yaml"))
    assert client.put(f"/api/skills/{name}", json={"content": "overwrite"}).status_code == 403
    assert client.delete(f"/api/skills/{name}").status_code == 403
    assert "Do work." in (directory / "SKILL.md").read_text(encoding="utf-8")
    (directory / "SKILL.md").unlink()
    assert name in [item["name"] for item in client.get("/api/skills").json()["missing"]]
    assert client.delete(f"/api/skills/{name}").status_code == 200
    assert directory.exists(), "taking off a missing external skill must not delete its folder"


def test_home_overrides_and_qwen_custom_directory(
    project: Path, agent_skills_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = agent_skills_home
    monkeypatch.setenv("CODEX_HOME", str(home / "custom-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "custom-claude"))
    skill(home / "custom-codex/skills/codex-work")
    skill(home / "custom-claude/skills/claude-work")
    skill(home / "custom-shared/qwen-work")
    (home / ".qwen").mkdir()
    (home / ".qwen/settings.json").write_text(json.dumps({"skills": {"directories": ["~/custom-shared"]}}))
    catalog = discover_installed_skills(project)
    assert {entry.display_name for entry in catalog.values()} == {"codex-work", "claude-work", "qwen-work"}


def test_installed_plugin_sources_are_scoped_and_ids_survive_upgrade(
    project: Path, agent_skills_home: Path,
) -> None:
    home = agent_skills_home
    codex = home / ".codex"
    version = codex / "plugins/cache/vendor/doc-tools/1.0"
    skill(version / "skills/writer")
    (version / ".codex-plugin").mkdir()
    (version / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "doc-tools", "skills": "./skills"}))
    (codex / "config.toml").write_text('[plugins."doc-tools@vendor"]\nenabled = true\n')
    skill(codex / "plugins/cache/vendor/uninstalled/1.0/skills/not-installed")
    claude_plugin = home / "claude-plugin"
    skill(claude_plugin / "skills/reviewer")
    (claude_plugin / ".claude-plugin").mkdir()
    (claude_plugin / ".claude-plugin/plugin.json").write_text('{"name":"review-tools"}')
    (home / ".claude/plugins").mkdir(parents=True)
    (home / ".claude/plugins/installed_plugins.json").write_text(json.dumps({"plugins": {
        "review-tools@vendor": [{"scope": "user", "installPath": str(claude_plugin)}],
        "other-project@vendor": [{"scope": "project", "projectPath": str(project / "elsewhere"), "installPath": str(claude_plugin)}],
    }}))
    qwen_plugin = home / ".qwen/extensions/publisher"
    skill(qwen_plugin / "skills/publish")
    skill(qwen_plugin / "skills/proofread")
    (qwen_plugin / "qwen-extension.json").write_text('{"name":"publisher","skills":["skills/publish","skills/proofread"]}')
    before = discover_installed_skills(project)
    assert {entry.display_name for entry in before.values()} == {"writer", "reviewer", "publish", "proofread"}
    writer = next(entry for entry in before.values() if entry.display_name == "writer")
    assert writer.source == "codex:doc-tools@vendor"
    newer = version.with_name("2.0")
    version.rename(newer)
    after = discover_installed_skills(project)
    assert after[writer.name].directory == (newer / "skills/writer").resolve()


def test_symlink_aliases_and_cycles_are_bounded_and_deduplicated(
    project: Path, agent_skills_home: Path,
) -> None:
    home = agent_skills_home
    original = skill(home / ".agents/skills/shared")
    alias = home / ".claude/skills/shared"
    alias.parent.mkdir(parents=True)
    try:
        alias.symlink_to(original, target_is_directory=True)
        (home / ".agents/skills/cycle").symlink_to(home / ".agents/skills", target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires platform privileges")
    catalog = discover_installed_skills(project)
    assert len(catalog) == 1
    entry = next(iter(catalog.values()))
    assert entry.source == "shared" and entry.backends == ()


@pytest.mark.parametrize("name", ["installed:../../secret", "installed:abc", "installed:" + "a" * 33])
def test_external_ids_do_not_allow_paths(name: str) -> None:
    with pytest.raises(ValueError):
        check_name(name)
