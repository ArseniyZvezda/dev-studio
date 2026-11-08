"""Provider ownership of retained Claude Code and Qwen fragments (§10.4)."""
from voxarium_studio.teams.context_provider_journals import claude_context, qwen_context
from voxarium_studio.teams.context_viewer import native_inventory


def test_claude_cli_prefix_is_distinguished_from_identical_custom_prompt_and_tools():
    text = "You are Claude Code."
    parts = claude_context([{"attachment": {
        "type": "prompt_snapshot", "cliPrefix": text, "systemPrompt": [text],
        "tools": [{"name": "Read"}],
    }}, {"message": {"role": "system", "content": text}}]).sources
    assert [part.get("provided_by") for part in parts] == ["claude_code", None, None, None]
    assert [parts[i]["text"] for i in (0, 1, 3)] == [text] * 3
    assert parts[0]["origin"] == "journal[0].attachment.cliPrefix"


def test_claude_native_marks_its_own_tools_and_agents_from_sdk_provenance():
    report = {
        "totalTokens": 62,
        "categories": [
            {"id": "system_tools", "name": "System tools", "tokens": 5},
            {"id": "mcp_tools", "name": "MCP tools", "tokens": 5},
            {"name": "Other tools", "tokens": 5},
            {"name": "Agents", "tokens": 20},
            {"name": "System prompt", "tokens": 20},
            {"name": "Memory files", "tokens": 5},
        ],
        "systemTools": [{"name": "Read", "tokens": 5}],
        "mcpTools": [{"name": "Read", "tokens": 5}],
        "agents": [{"agentType": "Explore", "source": "built-in", "tokens": 12},
                   {"agentType": "Explore", "source": "project", "tokens": 8}],
        "systemPromptSections": [{"name": "Appended instructions", "tokens": 20}],
        "memoryFiles": [{"path": "CLAUDE.md", "tokens": 5}],
    }
    parts = native_inventory(report)["sources"]
    assert [p["origin"] for p in parts if p.get("provided_by") == "claude_code"] == [
        "runtime.systemTools[0]", "runtime.agents[0]",
    ]
    assert any(p["label"] == "Other tools" for p in parts)

    # A category aggregate inherits ownership only where the SDK supplies
    # its specific system_tools category ID.
    report.pop("systemTools")
    parts = native_inventory(report)["sources"]
    assert parts[0]["origin"] == "runtime.categories[0]" and parts[0]["provided_by"] == "claude_code"
    report["categories"][0].pop("id")
    assert "provided_by" not in native_inventory(report)["sources"][0]


def test_qwen_recording_system_role_and_memory_catalog_do_not_prove_builtin_content():
    parts = qwen_context([
        {"message": {"role": "system", "parts": [{"text": "Custom system instructions"}]}},
        {"type": "system", "subtype": "memory_catalog_snapshot", "message": {"parts": [{
            "text": "Project memory", "partMetadata": {"qwen-code:memory-catalog-snapshot": True},
        }]}},
    ]).sources
    assert [p["category"] for p in parts] == ["system", "memory"]
    assert all("provided_by" not in p for p in parts)
