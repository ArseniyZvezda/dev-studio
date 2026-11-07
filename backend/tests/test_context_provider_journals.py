"""Source boundaries, identities and provider mirrors in Claude and Qwen journals."""
from copy import deepcopy
import json

from voxarium_studio.teams.context_provider_journals import claude_context, qwen_context


def test_claude_distinct_records_and_nested_result_fields_are_lossless():
    def row(key, parent, message):
        return {"uuid": key, "parentUuid": parent, "message": message}
    same = {"id": "response", "role": "assistant", "content": [{"type": "text", "text": " repeat\n"}]}
    args = {"script": "x\r\ny", "amount": 4}
    rows = [
        row("a", None, same), row("b", "a", same),
        row("c", "b", {"role": "assistant", "content": [{"type": "tool_use", "input": args}]}),
        row("d", "c", {"role": "user", "content": [{"type": "tool_result", "content": [
            {"type": "text", "text": "first\r\n"}, {"type": "image", "data": "wire bytes"},
            {"type": "text", "text": "  "}, {"type": "text", "text": "last\n"},
        ]}]}),
        {"type": "api-request-blob", "hash": "blob", "message": same},
    ]
    original = deepcopy(rows)
    parts = claude_context(rows).sources
    assert rows == original
    assert [part["text"] for part in parts[:2]] == [" repeat\n"] * 2
    assert parts[2]["representation"] == "json"
    assert json.loads(parts[2]["text"]) == args
    assert [(part["text"], part["origin"]) for part in parts[3:]] == [
        ("first\r\n", "journal[3].message.content[0].content[0].text"),
        ("  ", "journal[3].message.content[0].content[2].text"),
        ("last\n", "journal[3].message.content[0].content[3].text"),
    ]


def test_claude_updates_active_snapshot_and_separates_labeled_memory():
    def attachment(key, parent, **data):
        return {"type": "attachment", "uuid": key, "parentUuid": parent, "attachment": data}
    tools = [{"name": "Read", "input_schema": {"type": "object"}}]
    rows = [
        attachment("a", None, type="prompt_snapshot", systemPrompt=["old"]),
        attachment("b", "a", type="instructions", files=[{"path": "CLAUDE.md", "content": "guidance\n"}]),
        attachment("c", "b", type="prompt_snapshot", systemPrompt=["one", "two"], tools=tools),
        attachment("fork", "b", type="prompt_snapshot", systemPrompt=["abandoned"]),
        attachment("d", "c", type="nested_memory", path="MEMORY.md",
                   content={"rawContent": "disk version", "content": "retained version"}),
        {"uuid": "e", "parentUuid": "d", "type": "assistant", "isApiErrorMessage": True,
         "message": {"role": "assistant", "content": "error for UI"}},
    ]
    parts = claude_context(rows).sources
    assert [p["category"] for p in parts] == ["memory", "system", "system", "tool_definitions", "memory"]
    assert [p["text"] for p in parts[:3]] == ["guidance\n", "one", "two"]
    assert parts[-1]["text"] == "retained version"
    assert json.loads(parts[3]["text"]) == tools[0]
    assert parts[-1]["origin"] == "journal[4].attachment.content.content"


def test_qwen_reads_memory_and_tool_structure_and_ignores_display_events():
    call = {"command": "echo \"x\"\r\n"}
    memory = {"text": "memory", "partMetadata": {"qwen-code:memory-catalog-snapshot": True}}
    rows = [
        {"subtype": "chat_compression", "systemPayload": {"compressedHistory": [
            {"role": "user", "parts": [{"text": "brief\r\n"}, {"text": "  "}, memory]}]}},
        {"message": {"role": "model", "parts": [
            {"functionCall": {"name": "Shell", "args": call}},
            {"thought": True, "text": "thinking\n"}, {"text": "same"}, {"text": "same"}]}},
        {"type": "system", "subtype": "memory_catalog_snapshot", "message": {
            "role": "user", "parts": [memory]}},
        {"type": "system", "subtype": "memory_catalog_snapshot", "message": {
            "role": "user", "parts": [{**memory, "text": "new memory"}]}},
        {"subtype": "realtime_message", "message": {"role": "user", "parts": [{"text": "display mirror"}]}},
        {"type": "tool_result", "subtype": "code_mode_tool_result",
         "message": {"role": "user", "parts": [{"text": "internal copy"}]}},
    ]
    original = deepcopy(rows)
    parts = qwen_context(rows).sources
    assert rows == original
    assert [p["category"] for p in parts] == [
        "task", "task", "memory", "tools", "thinking", "history", "history", "memory",
    ]
    assert parts[0]["text"] == "brief\r\n" and parts[1]["text"] == "  "
    assert parts[0]["origin"] == "journal[0].systemPayload.compressedHistory[0].parts[0].text"
    assert json.loads(parts[3]["text"]) == call
    assert parts[3]["origin"] == "journal[1].message.parts[0].functionCall.args"
    assert parts[4]["text"] == "thinking\n"
    assert parts[5]["origin"] != parts[6]["origin"]


def test_qwen_managed_record_does_not_replace_active_conversation_leaf():
    rows = [
        {"uuid": "message", "type": "user", "message": {"role": "user", "parts": [{"text": "hello"}]}},
        {"uuid": "header", "type": "system", "subtype": "managed_session_header_v1"},
    ]
    assert [p["text"] for p in qwen_context(rows).sources] == ["hello"]


def test_qwen_studio_overflow_needs_evidence_even_in_compressed_history():
    text = "Studio instructions:\n  exact\r\nprompt  \n\nUser message:\nbrief\r\n"
    rows = [{"subtype": "chat_compression", "systemPayload": {"compressedHistory": [
        {"role": "user", "parts": [{"text": text}]}]}}]
    unchanged = qwen_context(rows, studio_system="different prompt").sources
    assert [(p["category"], p["text"]) for p in unchanged] == [("task", text)]
    parts = qwen_context(rows, studio_system="  exact\r\nprompt  ").sources
    assert [p["category"] for p in parts] == ["system", "task"]
    assert "".join(p["text"] for p in parts) == text
