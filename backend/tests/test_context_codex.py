"""Regressions for the Codex Context Viewer field inventory (§10.4)."""
import json

from voxarium_studio.teams.context_codex import codex_context


def message(role, *fields):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": field} for field in fields]}}


def header(base):
    return {"type": "session_meta", "payload": {"base_instructions": {"text": base}}}


def test_inventory_preserves_independent_instruction_fields_and_mirrors():
    base = "  You are Codex.\r\n"
    memory = "## Memory\r\nLocal memory.\r\n"
    skills = "<skills_instructions>\nSelect skills.\n</skills_instructions>"
    environment = "<environment_context>\nExample environment\n</environment_context>"
    developer = message("developer", base, memory, skills, "  agent role  ")
    rows = [header("You are Codex."), developer,
            {"type": "event_msg", "payload": {"type": "item_completed", "item": developer["payload"]}},
            message("user", environment, base), message("assistant", memory, " ")]
    result = codex_context(rows)
    fields = [(s["category"], s["text"]) for s in result.sources]
    assert fields == [("system", base), ("memory", memory), ("skills", skills), ("developer", "  agent role  "),
                      ("environment", environment), ("task", base), ("history", memory), ("history", " ")]
    assert len({s["origin"] for s in result.sources}) == len(fields)
    assert result.sources[1]["origin"] == "response_item[1].payload.content[1].text"


def test_scalar_base_and_marked_memory_are_disjoint_verbatim_slices():
    sep = "\r\n \r\n"
    dev = "Base." + sep + "## Memory\r\nThe memory.  "
    quoted = "The docs quote Base. and explain it."
    result = codex_context([header("Base."), message("developer", dev, quoted)])
    assert [(p["category"], p["text"]) for p in result.sources] == [
        ("system", "Base." + sep), ("memory", "## Memory\r\nThe memory.  "), ("developer", quoted)]
    assert "".join(p["text"] for p in result.sources[:2]) == dev
    assert codex_context([header("Base."), message("developer", quoted)]).sources[0]["text"] == "Base."


def test_output_blocks_structured_tools_and_agent_messages_keep_their_own_sources():
    schema = {"name": "example", "type": "namespace"}
    raw = '{ "param": 1, "param": 2 }\r\n'
    fields = [{"type": "input_text", "text": "first\r\n"}, {"type": "input_image", "image_url": "opaque"},
              {"type": "input_text", "text": "next"}]
    def item(kind, **extra):
        return {"type": "response_item", "payload": {"type": kind, **extra}}
    output = item("custom_tool_call_output", id="same", output=fields)
    result = codex_context([item("additional_tools", tools=[schema]), item("function_call", arguments=raw),
                            output, output, item("agent_message", id="same", content=[
                                {"type": "encrypted_content", "encrypted_content": "opaque"},
                                {"type": "input_text", "text": "agent message"}]),
                            item("reasoning", summary=[{"text": "summary"}], content=[{"text": "reasoning"}])])
    schema_part, *rest = result.sources
    assert schema_part["representation"] == "json" and json.loads(schema_part["text"]) == schema
    assert [(p["category"], p["text"]) for p in rest] == [
        ("tools", raw), ("tools", "first\r\n"), ("tools", "next"), ("agents", "agent message"),
        ("thinking", "summary"), ("thinking", "reasoning")]
    assert all(p["representation"] == "text" for p in rest)


def test_compaction_does_not_inherit_parent_base_or_earlier_partial_state():
    rows = [header(None), header("unrelated parent settings"),
            {"type": "compacted", "payload": {"message": "previous summary"}},
            {"type": "compacted", "payload": {"replacement_history": [message("user", "retained")["payload"]]}}]
    result = codex_context(rows)
    assert [s["text"] for s in result.sources] == ["retained"]
    assert result.partial
    rows[0] = header("current base")
    result = codex_context(rows)
    assert not result.partial
    assert result.sources[1]["origin"] == "compacted[3].payload.replacement_history[0].content[0].text"
