"""MCP project authoring writes the same files the pages read (§10.7)."""

import json

import pytest
import yaml
from sqlmodel import select

from voxarium_studio.api.mcp import BUILTIN_TOOLS
from voxarium_studio.models.teams import TeamMember
from voxarium_studio.plugins.registry import Registry
from voxarium_studio.teams.project_skills import MAX_SKILL_BYTES
from voxarium_studio.teams.tools import build_toolset
from voxarium_studio.ws.events import EventType

pytest.importorskip("claude_agent_sdk")


@pytest.fixture
async def toolset(ctx):
    async with ctx.db.session() as session:
        member = (await session.exec(select(TeamMember))).first()
    return build_toolset(ctx.teams, member=member)


@pytest.fixture
def call(toolset):
    handlers = {tool.name: tool.handler for tool in toolset.definitions}

    async def invoke(name, args=None, *, error=False):
        result = await handlers[name](args or {})
        assert bool(result.get("is_error")) == error, result
        text = result["content"][0]["text"]
        return text if error else json.loads(text)

    return invoke


def pipeline():
    return {
        "id": "mcp-test", "name": "Тест MCP",
        "nodes": [{"id": "read", "type": "core.read_text_file", "params": {"path": "notes.txt"}}],
    }


def automation(**changes):
    return {
        "id": "mcp-notify", "trigger": {"kind": "task.created"},
        "action": {"kind": "notify", "message": "Готово"}, **changes,
    }


SKILL = "---\nname: game-notes\ndescription: Write game notes.\n---\n\n# Notes\n\nПиши заметки.\n"


async def test_catalog_matches_server_and_allowed_tools(toolset):
    names = [tool.name for tool in toolset.definitions]
    assert names == BUILTIN_TOOLS
    assert toolset.allowed_tools == [f"mcp__voxarium_studio__{name}" for name in names]


async def test_pipeline_create_read_replace_and_live_registry(ctx, call):
    types = await call("list_node_types")
    assert any(t["id"] == "core.read_text_file" and t["inputs"] for t in types)
    body = pipeline()
    result = await call("create_pipeline", {"pipeline": body})
    assert result["valid"] and ctx.pipelines.get(body["id"]).name == "Тест MCP"
    assert any(p["id"] == body["id"] for p in await call("list_pipelines"))
    read = await call("read_pipeline", {"pipeline_id": body["id"]})
    read["pipeline"]["description"] = "Обновлено"
    await call("write_pipeline", {"pipeline": read["pipeline"]})
    assert ctx.pipelines.get(body["id"]).description == "Обновлено"
    await call("create_pipeline", {"pipeline": body}, error=True)
    # A session already built must see a plugin registry replaced by reload_plugins.
    ctx.registry = Registry(ctx.paths.root)
    result = await call("validate_pipeline", {"pipeline": body})
    assert not result["valid"] and "Unknown node type" in result["issues"][0]["message"]


@pytest.mark.parametrize("change", ["type", "required", "port", "cycle", "duplicate", "escape"])
async def test_bad_pipeline_never_replaces_valid_file(ctx, call, change):
    body = pipeline()
    await call("create_pipeline", {"pipeline": body})
    path = ctx.pipelines.directory / "mcp-test.yaml"
    before = path.read_bytes()
    if change == "type":
        body["nodes"][0]["type"] = "missing.node"
    elif change == "required":
        body["nodes"][0]["params"] = {}
    elif change == "duplicate":
        body["nodes"].append(body["nodes"][0].copy())
    elif change == "escape":
        body["id"] = "../mcp-test"
    else:
        body["nodes"].append({"id": "other", "type": "core.read_text_file", "params": {"path": "x"}})
        body["edges"] = [{"from": {"node": "read", "port": "bad" if change == "port" else "text"},
                          "to": {"node": "other", "port": "path"}}]
        if change == "cycle":
            body["edges"].append({"from": {"node": "other", "port": "text"},
                                  "to": {"node": "read", "port": "path"}})
    await call("write_pipeline", {"pipeline": body}, error=True)
    assert path.read_bytes() == before


async def test_automation_create_enable_disable_and_preserve_invalid(ctx, call):
    ctx.automation_store.file.write_text("automations:\n- id: broken\n  mystery: keep\n", encoding="utf-8")
    subscription = ctx.bus.subscribe()
    try:
        created = await call("create_automation", {"automation": automation()})
        assert created["enabled"] is False
        event = await subscription.get()
        assert event.type == EventType.AUTOMATION_CHANGED
        read = await call("read_automation", {"automation_id": created["id"]})
        read.pop("problem")
        read["enabled"] = True
        await call("write_automation", {"automation": read})
        assert ctx.automation_store.get(created["id"]).enabled
        read["enabled"] = False
        await call("write_automation", {"automation": read})
        assert not ctx.automation_store.get(created["id"]).enabled
        overview = await call("list_automations")
        assert overview["invalid"][0]["id"] == "broken"
        raw = yaml.safe_load(ctx.automation_store.file.read_text(encoding="utf-8"))
        assert raw["automations"][0] == {"id": "broken", "mystery": "keep"}
        await call("create_automation", {"automation": automation()}, error=True)
        await call("create_automation", {"automation": automation(id="broken")}, error=True)
    finally:
        subscription.close()


@pytest.mark.parametrize("changes", [
    {"trigger": {"kind": "unknown"}},
    {"trigger": {"kind": "path.changed", "path": "../outside"}},
    {"action": {"kind": "pipeline.run", "pipeline_id": "missing"}},
    {"action": {"kind": "plugin.action", "plugin_id": "missing", "action_id": "missing"}},
])
async def test_bad_automation_does_not_write(ctx, call, changes):
    before = ctx.automation_store.file.read_bytes() if ctx.automation_store.file.exists() else None
    await call("create_automation", {"automation": automation(**changes)}, error=True)
    after = ctx.automation_store.file.read_bytes() if ctx.automation_store.file.exists() else None
    assert after == before


async def test_automation_pipeline_reference_and_unreadable_file(ctx, call):
    await call("create_pipeline", {"pipeline": pipeline()})
    await call("create_automation", {"automation": automation(
        action={"kind": "pipeline.run", "pipeline_id": "mcp-test"}, enabled=True,
    )})
    assert ctx.automation_store.get("mcp-notify").enabled
    ctx.automation_store.file.write_text("automations: [", encoding="utf-8")
    await call("create_automation", {"automation": automation(id="another")}, error=True)
    assert ctx.automation_store.file.read_text(encoding="utf-8") == "automations: ["


async def test_missing_records_and_required_arguments(call):
    for name, args in [
        ("read_pipeline", {"pipeline_id": "absent"}),
        ("write_pipeline", {"pipeline": pipeline()}),
        ("read_automation", {"automation_id": "absent"}),
        ("write_automation", {"automation": automation(enabled=False)}),
        ("write_automation", {"automation": {"trigger": {"kind": "task.created"}, "action": {"kind": "notify"}}}),
        ("read_skill", {"name": "absent"}),
        ("create_skill", {"name": "missing-content"}),
    ]:
        await call(name, args, error=True)


async def test_skill_create_read_edit_conflict_and_no_assignment(ctx, call):
    created = await call("create_skill", {"name": "game-notes", "content": SKILL})
    assert created["description"] == "Write game notes."
    read = await call("read_skill", {"name": "game-notes"})
    assert read["content"] == SKILL
    assert any(s["name"] == "game-notes" for s in (await call("list_skills"))["skills"])
    await call("create_skill", {"name": "game-notes", "content": "overwrite"}, error=True)
    await call("write_skill", {"name": "game-notes", "content": "no version"}, error=True)
    changed = SKILL + "\nMore notes.\n"
    await call("write_skill", {"name": "game-notes", "content": changed,
                              "expected_updated_at": read["updated_at"]})
    await call("write_skill", {"name": "game-notes", "content": "stale",
                              "expected_updated_at": "2000-01-01T00:00:00+00:00"}, error=True)
    assert ctx.skills.read_text("game-notes")[1] == changed
    async with ctx.db.session() as session:
        assert all("game-notes" not in m.skills for m in (await session.exec(select(TeamMember))).all())


async def test_skill_builtin_paths_and_size_are_protected(ctx, call):
    builtin = await call("read_skill", {"name": "voxarium_studio:studio"})
    assert builtin["source"] == "voxarium_studio"
    await call("write_skill", {"name": builtin["name"], "content": "overwrite",
                              "expected_updated_at": builtin["updated_at"]}, error=True)
    for name in ["../escape", "C:/escape", "voxarium_studio:studio", "con"]:
        await call("create_skill", {"name": name, "content": SKILL}, error=True)
    await call("create_skill", {"name": "too-big", "content": "x" * (MAX_SKILL_BYTES + 1)}, error=True)
    assert not (ctx.skills.directory / "too-big").exists()


async def test_disabled_server_does_not_expose_authoring(ctx):
    async with ctx.db.session() as session:
        member = (await session.exec(select(TeamMember))).first()
    ctx.config.mcp.disabled = ["voxarium_studio"]
    assert build_toolset(ctx.teams, member=member) is None
