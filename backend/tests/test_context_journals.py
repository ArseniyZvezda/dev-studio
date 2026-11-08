"""Provider journal shapes and native /context accounting, without provider calls."""
import json
from unittest.mock import AsyncMock

import pytest

from voxarium_studio.models.task_context import TaskContext
from voxarium_studio.models.teams import AgentMessage
from voxarium_studio.teams import context_journals as journals
from voxarium_studio.teams.backends import base
from voxarium_studio.teams.backends.claude_code import _SessionControl
from voxarium_studio.teams.context_viewer import inventory, native_inventory, source


def codex_item(kind, **kwargs):
    return {"type": "response_item", "payload": {"type": kind, **kwargs}}


def test_codex_reads_runtime_instructions_tools_and_full_outputs_once():
    output = "full output " * 2000
    item = codex_item("custom_tool_call_output", id="output1", output=[{"type": "text", "text": output}])
    result = journals.codex_context([
        {"type": "session_meta", "payload": {"base_instructions": {"text": "base instructions"}, "context_window": 200000}},
        codex_item("additional_tools", tools=[{"name": "shell", "description": "schema"}]),
        codex_item("message", role="developer", content=[{"type": "input_text", "text": "role instructions"}]),
        codex_item("message", role="user", content=[{"type": "input_text", "text": "brief"}]),
        item, item, {"type": "event_msg", "payload": {"type": "item_completed", "item": item["payload"]}},
        codex_item("reasoning", summary=[{"type": "summary_text", "text": "visible"}], encrypted_content="secret" * 10000),
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 7000, "cached_input_tokens": 6000},
            "total_token_usage": {"input_tokens": 1000000}, "model_context_window": 200000}}},
    ])
    assert result.reported_tokens == 7000  # Cache is a subset; cumulative spending is irrelevant.
    assert result.context_window == 200000
    assert [p["category"] for p in result.sources] == ["system", "tool_definitions", "developer", "task", "tools", "thinking"]
    assert result.sources[-2]["text"] == output
    assert result.sources[-1]["text"] == "visible"
    assert not result.partial


def test_codex_compaction_replaces_history_and_invalidates_usage():
    result = journals.codex_context([
        codex_item("message", role="user", content="old prompt"),
        {"type": "token_usage_record", "payload": {"usage": {"input_tokens": 99999}}},
        {"type": "compacted", "payload": {"replacement_history": [{"type": "message", "role": "user", "content": "summary"}]}},
        codex_item("message", role="assistant", content="new answer"),
    ])
    messages = [AgentMessage(task_id="task", member_id="member", role="system", kind="system", text="usage_update", data={"context_tokens": 99999})]
    view = inventory(None, messages, result, backend="codex")
    assert [p["text"] for p in view["sources"]] == ["summary", "new answer"]
    assert view["reported_tokens"] is None


def test_codex_base_metadata_does_not_duplicate_actual_messages():
    base_text = "You are Codex."
    header = {"type": "session_meta", "payload": {"base_instructions": {"text": base_text}}}
    item = codex_item("message", role="developer", content=[{"type": "input_text", "text": base_text}])
    result = journals.codex_context([
        header, header, item,
        codex_item("message", role="user", content=base_text),
        item,  # Two actual developer messages without IDs are both retained.
    ])
    view = inventory(None, [], result, backend="codex")
    assert [(p["category"], p["text"]) for p in view["sources"]] == [
        ("system", base_text), ("task", base_text), ("system", base_text),
    ]
    assert view["estimated_tokens"] == 3 * source("system", base_text)["tokens"]


@pytest.mark.parametrize("retain_base", [False, True])
def test_codex_uses_active_header_only_if_replacement_history_needs_it(retain_base):
    base_text = "Current base"
    compacted = [{"type": "message", "role": "user", "content": base_text}]
    if retain_base:
        compacted.insert(0, {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": base_text}, {"type": "input_text", "text": "Extra"},
        ]})
    result = journals.codex_context([
        {"type": "session_meta", "payload": {"base_instructions": {"text": base_text}}},
        {"type": "session_meta", "payload": {"base_instructions": {"text": "Parent base"}}},
        codex_item("message", role="developer", content=base_text),
        {"type": "compacted", "payload": {"replacement_history": compacted}},
    ])
    expected = [("system", base_text)]
    if retain_base:
        expected.append(("developer", "Extra"))
    assert [(p["category"], p["text"]) for p in result.sources] == expected + [("task", base_text)]


def test_codex_summary_only_compaction_does_not_keep_prior_tools():
    result = journals.codex_context([
        {"type": "session_meta", "payload": {"base_instructions": {"text": "base"}}},
        codex_item("function_call_output", output="old tool"),
        {"type": "compacted", "payload": {"message": "retained summary"}},
    ])
    assert [p["text"] for p in result.sources] == ["base", "retained summary"]
    assert result.partial


def test_claude_follows_current_branch_skips_workers_and_keeps_full_results():
    def row(uuid, parent, text, **kwargs):
        return {"uuid": uuid, "parentUuid": parent, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, **kwargs}
    result = journals.claude_context([
        row("a", None, "start"), row("abandoned", "a", "wrong branch"), row("b", "a", "current"),
        {"uuid": "c", "parentUuid": "b", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x" * 12000}]}},
        row("worker", "c", "child", isSidechain=True),
    ])
    assert [p["text"] for p in result.sources] == ["start", "current", "x" * 12000]
    assert result.sources[-1]["tokens"] == 3000


def test_claude_compaction_and_cache_counts():
    result = journals.claude_context([
        {"uuid": "old", "parentUuid": None, "message": {"role": "user", "content": "old"}},
        {"uuid": "compact", "parentUuid": "old", "type": "system", "subtype": "compact_boundary"},
        {"uuid": "new", "parentUuid": "compact", "message": {"role": "assistant", "content": "summary", "usage": {
            "input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}},
    ])
    assert [p["text"] for p in result.sources] == ["summary"]
    assert result.reported_tokens == 130


def test_qwen_compression_uses_model_history_not_ui_tool_copy():
    result = journals.qwen_context([
        {"uuid": "old", "parentUuid": None, "message": {"role": "user", "parts": [{"text": "old"}]}},
        {"uuid": "compact", "parentUuid": "old", "subtype": "chat_compression", "systemPayload": {
            "compressedHistory": [{"role": "user", "parts": [{"text": "summary"}]}]}},
        {"uuid": "result", "parentUuid": "compact", "message": {"role": "user", "parts": [
            {"functionResponse": {"name": "Read", "response": {"output": "x" * 10000}}}]}, "toolCallResult": {"resultDisplay": "duplicate"}},
        {"uuid": "answer", "parentUuid": "result", "message": {"role": "model", "parts": [{"text": "done"}]},
         "usageMetadata": {"promptTokenCount": 3000, "cachedContentTokenCount": 2500}, "contextWindowSize": 100000},
    ])
    assert [p["category"] for p in result.sources] == ["task", "tools", "history"]
    assert "duplicate" not in str(result.sources) and "old" not in str(result.sources)
    assert result.reported_tokens == 3000
    assert result.context_window == 100000


def test_snapshot_is_not_added_twice_and_preview_does_not_reduce_size():
    snapshot = TaskContext(task_id="task", sources=[source("system", "role"), source("task", "brief")])
    journal = journals.JournalContext(sources=[source("system", "role"), source("tools", "x" * 100000)], partial=False)
    view = inventory(snapshot, [], journal, backend="codex")
    assert view["estimated_tokens"] == 25001
    assert len(view["sources"]) == 2
    assert len(view["sources"][1]["text"]) == 32000
    assert view["sources"][1]["truncated"]
    assert len(journal.sources[1]["text"]) == 100000  # Cached inventory isn't mutated.


def test_journal_lookup_is_bounded_and_never_accepts_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    folder = tmp_path / "sessions" / "2026"
    folder.mkdir(parents=True)
    path = folder / "rollout-test-session.jsonl"
    path.write_text(json.dumps(codex_item("message", role="user", content="brief")) + '\n{"inflight"', encoding="utf8")
    journal = journals.read_session("codex", "session")
    assert journal.sources[0]["text"] == "brief"
    assert inventory(None, [], journal, backend="codex")["journal_path"] == str(path.resolve())
    assert "journal_path" not in inventory(None, [])
    assert journals.read_session("codex", "../session") is None
    monkeypatch.setattr(journals, "MAX_JOURNAL_BYTES", 1)
    journals._read.cache_clear()
    assert journals.read_session("codex", "session") is None


def test_qwen_runtime_directory_override(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_RUNTIME_DIR", str(tmp_path))
    folder = tmp_path / "projects" / "project" / "chats"
    folder.mkdir(parents=True)
    (folder / "session.jsonl").write_text(json.dumps({"message": {"role": "user", "parts": [{"text": "brief"}]}}), encoding="utf8")
    assert journals.read_session("qwen", "session").sources[0]["text"] == "brief"


def native_report():
    return {"totalTokens": 200, "rawMaxTokens": 1000, "categories": [
        {"name": "System prompt", "tokens": 50}, {"name": "MCP tools", "tokens": 20},
        {"name": "Memory files", "tokens": 10}, {"name": "Messages", "tokens": 100},
        {"name": "Free space", "tokens": 700}, {"name": "Autocompact buffer", "tokens": 100},
        {"name": "Deferred tools", "tokens": 40, "isDeferred": True}],
        "memoryFiles": [{"path": "CLAUDE.md", "tokens": 10}]}


def test_native_breakdown_excludes_capacity_and_never_adds_text_estimates():
    view = native_inventory(native_report())
    assert view["basis"] == "runtime"
    assert view["estimated_tokens"] == 180
    assert sum(p["tokens"] for p in view["sources"]) == 200
    assert view["sources"][-1]["category"] == "unattributed"
    assert "CLAUDE.md" in next(p["text"] for p in view["sources"] if p["category"] == "memory")
    with pytest.raises(ValueError):
        native_inventory({**native_report(), "totalTokens": 1})


def test_native_fragments_preserve_parent_size_and_exclude_deferred_tools():
    report = native_report()
    report["mcpTools"] = [
        {"name": "read", "serverName": "project-files", "tokens": 14}, {"name": "list", "tokens": 6},
        {"name": "deferred", "tokens": 80, "isLoaded": False},
    ]
    def tool_fragments():
        return [(part["label"], part["tokens"]) for part in native_inventory(report)["sources"]
                if part["category"] == "tool_definitions"]
    assert tool_fragments() == [("read", 14), ("list", 6)]
    tool = next(p for p in native_inventory(report)["sources"] if p["label"] == "read")
    assert (tool["source_kind"], tool["source_detail"]) == ("sdk_entry", "MCP tools · project-files · read")
    report["mcpTools"][0]["tokens"] = 99
    assert tool_fragments() == [("MCP tools", 20)]
    aggregate = next(p for p in native_inventory(report)["sources"] if p["label"] == "MCP tools")
    assert (aggregate["source_kind"], aggregate["source_detail"]) == ("sdk_category", "MCP tools")


def test_api_only_requests_runtime_on_explicit_post(client, monkeypatch):
    task = client.post("/api/teams/team_default/tasks", json={"assignee_id": "team_default__dev", "title": "Context", "auto_start": False}).json()
    native = AsyncMock(return_value=native_report())
    control = type("Control", (), {"get_context_usage": native})()
    monkeypatch.setitem(base._LIVE_SESSIONS, task["id"], control)
    url = f"/api/teams/team_default/tasks/{task['id']}/context"
    assert client.get(url).json()["can_inspect"]
    native.assert_not_called()
    assert client.post(url.replace("team_default", "wrong-team") + "/inspect").status_code == 404
    native.assert_not_called()
    response = client.post(url + "/inspect")
    assert response.status_code == 200
    assert response.json()["basis"] == "runtime"
    native.assert_awaited_once()
    monkeypatch.delitem(base._LIVE_SESSIONS, task["id"])
    assert client.post(url + "/inspect").status_code == 409


async def test_claude_uses_public_sdk_context_method():
    sdk = type("Client", (), {"get_context_usage": AsyncMock(return_value=native_report())})()
    assert await _SessionControl(sdk).get_context_usage() == native_report()
    sdk.get_context_usage.assert_awaited_once()
