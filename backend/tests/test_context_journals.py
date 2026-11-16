"""Provider journal shapes and native /context accounting, without provider calls."""
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
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


def test_api_only_requests_runtime_on_explicit_post(client, project):
    task = client.post("/api/teams/team_default/tasks", json={"assignee_id": "team_default__dev", "title": "Context", "auto_start": False}).json()
    native = AsyncMock(return_value=native_report())
    control = type("Control", (), {"get_context_usage": native})()
    key = base.RuntimeSessionKey(base.runtime_session_owner(project, "task", task["id"]))
    base.register_live_session(key, control)
    url = f"/api/teams/team_default/tasks/{task['id']}/context"
    try:
        assert client.get(url).json()["can_inspect"]
        native.assert_not_called()
        assert client.post(url.replace("team_default", "wrong-team") + "/inspect").status_code == 404
        native.assert_not_called()
        response = client.post(url + "/inspect")
        assert response.status_code == 200
        assert response.json()["basis"] == "runtime"
        native.assert_awaited_once()
    finally:
        base.forget_live_session(key)
    assert client.post(url + "/inspect").status_code == 409


async def test_claude_uses_public_sdk_context_method():
    sdk = type("Client", (), {"get_context_usage": AsyncMock(return_value=native_report())})()
    assert await _SessionControl(sdk).get_context_usage() == native_report()
    sdk.get_context_usage.assert_awaited_once()


def _native_message(backend, role, text, *, uuid="first", parent=None):
    if backend == "codex":
        return codex_item("message", role=role, content=[{"type": "input_text", "text": text}])
    field = "parts" if backend == "qwen" else "content"
    block = {"text": text} if backend == "qwen" else {"type": "text", "text": text}
    return {"uuid": uuid, "parentUuid": parent, "message": {"role": role, field: [block]}}


def _write_native(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf8")


@pytest.fixture(params=["claude_code", "codex", "qwen"])
def bound_native(request, tmp_path, monkeypatch):
    """A provider home, not the developer's actual journal, with two captured turns."""
    backend = request.param
    home = tmp_path / "provider"
    workdir = tmp_path / "external-shared-workdir"
    workdir.mkdir()
    sid = "bound-session"
    token = "a" * 32
    prompts = (journals.dispatch_marker(token) + "Первый dispatch\r\nexact body\n",
               journals.dispatch_marker(token) + "Resumed dispatch\nsecond body\n")
    binding = journals.NativeJournalBinding(
        project_root=tmp_path / "managed-project", owner_kind="task", owner_id="task-own",
        workdir=workdir, token=token,
        dispatches=tuple(journals.dispatch_digest(text) for text in prompts),
    )
    if backend == "codex":
        monkeypatch.setenv("CODEX_HOME", str(home))
        path = home / "sessions" / "2026" / f"rollout-own-{sid}.jsonl"
        metadata = {"type": "session_meta", "payload": {"cwd": str(workdir), "id": sid}}
    else:
        monkeypatch.setenv("QWEN_RUNTIME_DIR" if backend == "qwen" else "CLAUDE_CONFIG_DIR", str(home))
        path = home / "projects" / "own" / ("chats" if backend == "qwen" else "") / f"{sid}.jsonl"
        metadata = {"type": "system", "cwd": str(workdir), "sessionId": sid}
    rows = [metadata,
            _native_message(backend, "user", prompts[0]),
            _native_message(backend, "assistant", "First answer", uuid="answer", parent="first"),
            _native_message(backend, "user", prompts[1], uuid="resume", parent="answer"),
            _native_message(backend, "assistant", "Latest answer", uuid="last", parent="resume")]
    journals._read.cache_clear()
    yield SimpleNamespace(backend=backend, path=path, sid=sid, binding=binding,
                          prompts=prompts, rows=rows, metadata=metadata)
    journals._read.cache_clear()


@pytest.mark.parametrize("resume", [False, True])
def test_bound_native_accepts_exact_own_genesis_and_resume_with_source_addresses(bound_native, resume):
    native = bound_native
    rows = native.rows if resume else native.rows[:3]
    binding = native.binding if resume else replace(native.binding, dispatches=native.binding.dispatches[:1])
    _write_native(native.path, rows)
    # The binding itself supplies cwd; requiring a separate workdir is unnecessary.
    result = journals.read_session(native.backend, native.sid, binding=binding)
    assert result is not None
    expected = [native.prompts[0], "First answer"]
    if resume:
        expected += [native.prompts[1], "Latest answer"]
    assert [part["text"] for part in result.sources] == expected
    assert result.journal_path == str(native.path.resolve())
    if native.backend == "codex":
        origins = [f"response_item[{i}].payload.content[0].text" for i in range(1, len(rows))]
    else:
        field = "parts" if native.backend == "qwen" else "content"
        origins = [f"journal[{i}].message.{field}[0].text" for i in range(1, len(rows))]
    assert [part["origin"] for part in result.sources] == origins


def test_native_workdir_alone_fails_closed_but_unbound_legacy_remains_readable(bound_native):
    native = bound_native
    rows = [native.metadata, _native_message(native.backend, "user", "Unmarked legacy prompt")]
    _write_native(native.path, rows)
    assert journals.read_session(native.backend, native.sid, workdir=native.binding.workdir) is None
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None
    legacy = journals.read_session(native.backend, native.sid)
    assert legacy is not None
    assert [part["text"] for part in legacy.sources] == ["Unmarked legacy prompt"]
    # Neither a warmed unbound cache nor the same cwd grants dispatch ownership.
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


@pytest.mark.parametrize("mismatch", ["token", "digest", "latest_dispatch", "workdir"])
def test_native_cache_isolated_by_binding_even_for_same_file_and_cwd(bound_native, mismatch):
    native = bound_native
    _write_native(native.path, native.rows)
    assert journals.read_session(native.backend, native.sid) is not None
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is not None
    changes = {
        "token": {"token": "b" * 32},
        "digest": {"dispatches": (journals.dispatch_digest("not the dispatched body"),)},
        "latest_dispatch": {"dispatches": (*native.binding.dispatches, journals.dispatch_digest("missing turn"))},
        "workdir": {"workdir": native.binding.workdir.parent / "wrong-workdir"},
    }[mismatch]
    foreign = replace(native.binding, project_root=native.binding.project_root.parent / "other-project",
                      owner_kind="chat", owner_id="chat-foreign", **changes)
    assert journals.read_session(native.backend, native.sid, binding=foreign) is None
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is not None


@pytest.mark.parametrize("mutation", ["wrong_token", "wrong_body", "leading_space", "newline", "unknown_prefix"])
def test_bound_native_requires_exact_genesis_not_marker_substring(bound_native, mutation):
    native = bound_native
    prompt = native.prompts[0]
    prompt = {
        "wrong_token": prompt.replace("a" * 32, "b" * 32),
        "wrong_body": prompt + "modified",
        "leading_space": " " + prompt,
        "newline": prompt.replace("\r\n", "\n"),
        "unknown_prefix": "Unrecorded prior input\n" + prompt,
    }[mutation]
    rows = [native.metadata, _native_message(native.backend, "user", prompt), *native.rows[2:]]
    _write_native(native.path, rows)
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_bound_native_rejects_unknown_conversation_before_genesis(bound_native, role):
    native = bound_native
    prefix = _native_message(native.backend, role, "Unknown earlier conversation", uuid="unknown")
    _write_native(native.path, [native.metadata, prefix, *native.rows[1:]])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


@pytest.mark.parametrize("receipt_location", ["assistant", "tool", "metadata"])
def test_bound_native_does_not_count_nonuser_dispatch_marker_as_receipt(bound_native, receipt_location):
    native = bound_native
    if receipt_location == "metadata":
        receipt = {"type": "diagnostic", "text": native.prompts[1]}
    elif receipt_location == "assistant":
        receipt = _native_message(native.backend, "assistant", native.prompts[1], uuid="quoted", parent="answer")
    elif native.backend == "codex":
        receipt = codex_item("function_call_output", output=native.prompts[1])
    elif native.backend == "claude_code":
        receipt = {"uuid": "quoted", "parentUuid": "answer", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "read", "content": native.prompts[1]}]}}
    else:
        receipt = {"uuid": "quoted", "parentUuid": "answer", "message": {"role": "user", "parts": [
            {"functionResponse": {"name": "read", "response": {"output": native.prompts[1]}}}]}}
    _write_native(native.path, [*native.rows[:3], receipt])
    # Genesis is genuine, but the captured resume exists only in quoted output.
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


def test_bound_native_rejects_missing_latest_captured_dispatch(bound_native):
    native = bound_native
    _write_native(native.path, native.rows[:3])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


def test_bound_native_rejects_modified_resume_with_correct_token(bound_native):
    native = bound_native
    altered = _native_message(native.backend, "user", native.prompts[1] + "tampered",
                              uuid="resume", parent="answer")
    _write_native(native.path, [*native.rows[:3], altered, native.rows[-1]])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


def test_bound_native_joins_text_blocks_for_digest_without_losing_source_boundaries(bound_native):
    native = bound_native
    first = json.loads(json.dumps(native.rows[1]))
    message = first["payload"] if native.backend == "codex" else first["message"]
    field = "parts" if native.backend == "qwen" else "content"
    kind = {} if native.backend == "qwen" else {"type": "input_text" if native.backend == "codex" else "text"}
    marker = journals.dispatch_marker(native.binding.token)
    body = native.prompts[0][len(marker):]
    message[field] = [{**kind, "text": marker}, {**kind, "text": body}]
    # Non-object JSONL records still consume a source address; they must not shift it.
    _write_native(native.path, [native.metadata, [], first, *native.rows[2:]])
    result = journals.read_session(native.backend, native.sid, binding=native.binding)
    assert result is not None
    assert [part["text"] for part in result.sources[:2]] == [marker, body]
    prefix = "response_item[2].payload" if native.backend == "codex" else "journal[2].message"
    assert [part["origin"] for part in result.sources[:2]] == [
        f"{prefix}.{field}[0].text", f"{prefix}.{field}[1].text"]


def test_bound_native_rejects_explicit_wrong_sid(bound_native):
    native = bound_native
    if native.backend == "codex":
        metadata = {**native.metadata, "payload": {**native.metadata["payload"], "id": "other-session"}}
    else:
        metadata = {**native.metadata, "sessionId": "other-session"}
    _write_native(native.path, [metadata, *native.rows[1:]])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


@pytest.mark.parametrize("metadata", ["missing", "wrong", "conflicting", "relative"])
def test_bound_native_requires_consistent_explicit_native_cwd(bound_native, metadata):
    native = bound_native
    header = json.loads(json.dumps(native.metadata))
    target = header["payload"] if native.backend == "codex" else header
    if metadata == "missing":
        target.pop("cwd")
    else:
        target["cwd"] = "relative-path" if metadata == "relative" else str(native.binding.workdir.parent / "foreign")
    rows = [native.metadata, header] if metadata == "conflicting" else [header]
    _write_native(native.path, [*rows, *native.rows[1:]])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


def test_bound_native_rejects_conflicting_explicit_requested_workdir(bound_native):
    native = bound_native
    _write_native(native.path, native.rows)
    assert journals.read_session(native.backend, native.sid, binding=native.binding,
                                 workdir=native.binding.workdir.parent / "foreign") is None


def test_bound_native_rejects_two_proven_candidates_as_ambiguous(bound_native):
    native = bound_native
    _write_native(native.path, native.rows)
    if native.backend == "codex":
        other = native.path.parent / f"rollout-copy-{native.sid}.jsonl"
    else:
        other = native.path.parent.parent / "copy" / native.path.name
        if native.backend == "qwen":
            other = native.path.parents[2] / "copy" / "chats" / native.path.name
    _write_native(other, native.rows)
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


def test_bound_native_rejects_foreign_receipt_in_raw_abandoned_history(bound_native):
    native = bound_native
    foreign = _native_message(native.backend, "user", journals.dispatch_marker("b" * 32) + "Foreign dispatch",
                              uuid="abandoned", parent="first")
    rows = [*native.rows[:2], foreign, *native.rows[2:]]
    _write_native(native.path, rows)
    # Claude/Qwen reconstruction follows the other branch, not this raw user row.
    assert journals.read_session(native.backend, native.sid).sources[-1]["text"] == "Latest answer"
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


def _native_compaction(backend, messages):
    if backend == "codex":
        return {"type": "compacted", "payload": {"replacement_history": [row["payload"] for row in messages]}}
    if backend == "qwen":
        return {"uuid": "compact", "parentUuid": "last", "subtype": "chat_compression",
                "systemPayload": {"compressedHistory": [row["message"] for row in messages]}}
    return {"uuid": "compact", "parentUuid": "last", "type": "system", "subtype": "compact_boundary"}


def test_bound_native_compaction_preserves_proof_and_exact_nested_source_addresses(bound_native):
    native = bound_native
    retained = [_native_message(native.backend, "user", "Retained summary")]
    compact = _native_compaction(native.backend, retained)
    rows = [*native.rows, compact]
    if native.backend == "claude_code":
        rows.append({**_native_message(native.backend, "user", "Retained summary", uuid="summary", parent="compact"),
                     "isCompactSummary": True})
        origin = "journal[6].message.content[0].text"
    elif native.backend == "codex":
        origin = "compacted[5].payload.replacement_history[0].content[0].text"
    else:
        origin = "journal[5].systemPayload.compressedHistory[0].parts[0].text"
    _write_native(native.path, rows)
    result = journals.read_session(native.backend, native.sid, binding=native.binding)
    assert result is not None and result.compacted
    assert [(part["text"], part["origin"]) for part in result.sources] == [("Retained summary", origin)]


def test_bound_native_rejects_foreign_receipt_in_compaction_replacement(bound_native):
    native = bound_native
    if native.backend == "claude_code":
        compact = _native_compaction(native.backend, [])
        foreign = _native_message(native.backend, "user", journals.dispatch_marker("b" * 32) + "Foreign",
                                  uuid="summary", parent="compact")
        rows = [*native.rows, compact, {**foreign, "isCompactSummary": True}]
    else:
        foreign = _native_message(native.backend, "user", journals.dispatch_marker("b" * 32) + "Foreign")
        rows = [*native.rows, _native_compaction(native.backend, [foreign])]
    _write_native(native.path, rows)
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


@pytest.mark.parametrize("bound_native", ["codex", "qwen"], indirect=True)
def test_bound_native_rejects_rewritten_history_without_raw_genesis(bound_native):
    native = bound_native
    _write_native(native.path, [native.metadata, _native_compaction(native.backend, native.rows[1:])])
    result = journals.read_session(native.backend, native.sid, binding=native.binding)
    assert result is None  # A replacement history alone cannot prove a fresh genesis.


@pytest.mark.parametrize("bound_native", ["codex", "qwen"], indirect=True)
@pytest.mark.parametrize("role", ["assistant", "tool"])
def test_nested_compaction_nonuser_marker_cannot_supply_missing_dispatch(bound_native, role):
    native = bound_native
    if role == "tool" and native.backend == "codex":
        quoted = codex_item("function_call_output", output=native.prompts[1])
    elif role == "tool":
        quoted = {"message": {"role": "user", "parts": [
            {"functionResponse": {"name": "read", "response": {"output": native.prompts[1]}}}]}}
    else:
        quoted = _native_message(native.backend, role, native.prompts[1])
    _write_native(native.path, [*native.rows[:3], _native_compaction(native.backend, [quoted])])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


def test_bound_native_rejects_unmarked_primary_user_tail_from_terminal_or_old_client(bound_native):
    native = bound_native
    unknown = _native_message(native.backend, "user", "B PRIVATE UNMARKED", uuid="unknown", parent="last")
    _write_native(native.path, [*native.rows, unknown])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


@pytest.mark.parametrize("bound_native", ["codex"], indirect=True)
def test_codex_sidechain_flag_cannot_hide_a_foreign_dispatch_from_authorization(bound_native):
    native = bound_native
    foreign = {**_native_message("codex", "user", journals.dispatch_marker("b" * 32) + "B PRIVATE"),
               "isSidechain": True}
    _write_native(native.path, [*native.rows, foreign])
    assert journals.read_session("codex", native.sid, binding=native.binding) is None


@pytest.mark.parametrize("role", ["developer", "system"])
def test_bound_native_keeps_own_conversation_but_never_publishes_unproven_startup_envelopes(bound_native, role):
    native = bound_native
    prelude = _native_message(native.backend, role, "UNPROVEN STARTUP PRIVATE", uuid="startup")
    _write_native(native.path, [native.metadata, prelude, *native.rows[1:]])
    result = journals.read_session(native.backend, native.sid, binding=native.binding)
    assert result is not None and result.partial
    assert [part["text"] for part in result.sources] == [native.prompts[0], "First answer",
                                                       native.prompts[1], "Latest answer"]
    assert "UNPROVEN STARTUP PRIVATE" not in str(result.sources)
    assert "[2]" in result.sources[0]["origin"]  # Original JSONL addresses did not shift.


@pytest.mark.parametrize("bound_native", ["claude_code", "qwen"], indirect=True)
def test_active_branch_must_reach_own_genesis(bound_native):
    native = bound_native
    foreign = _native_message(native.backend, "assistant", "B PRIVATE", uuid="foreign", parent=None)
    _write_native(native.path, [*native.rows, foreign])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


@pytest.mark.parametrize("bound_native", ["codex", "qwen"], indirect=True)
def test_future_text_block_cannot_be_ignored_by_dispatch_digest(bound_native):
    native = bound_native
    row = copy.deepcopy(native.rows[1])
    message = row["payload"] if native.backend == "codex" else row["message"]
    message["parts" if native.backend == "qwen" else "content"].append({"type": "unknown", "text": "B PRIVATE"})
    _write_native(native.path, [native.metadata, row, *native.rows[2:]])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


def test_unknown_startup_update_after_genesis_is_not_adopted_before_next_receipt(bound_native):
    native = bound_native
    if native.backend == "codex":
        startup = {**native.metadata, "payload": {**native.metadata["payload"], "base_instructions": "B PRIVATE"}}
    elif native.backend == "claude_code":
        startup = {"uuid": "startup", "parentUuid": "last", "attachment": {
            "type": "prompt_snapshot", "systemPrompt": ["B PRIVATE"]}}
    else:
        startup = _native_message("qwen", "system", "B PRIVATE", uuid="startup", parent="last")
    _write_native(native.path, [*native.rows, startup])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


@pytest.mark.parametrize("bound_native", ["claude_code", "qwen"], indirect=True)
def test_diagnostic_message_copy_is_not_a_primary_user_receipt(bound_native):
    native = bound_native
    diagnostic = copy.deepcopy(native.rows[1])
    diagnostic.update({"type": "api-request-blob"} if native.backend == "claude_code"
                      else {"type": "system", "subtype": "ui_telemetry"})
    _write_native(native.path, [native.metadata, diagnostic, *native.rows[2:]])
    assert journals.read_session(native.backend, native.sid, binding=native.binding) is None


@pytest.mark.parametrize("bound_native", ["qwen"], indirect=True)
@pytest.mark.parametrize("envelope", ["system", "user", "assistant", "untyped"])
@pytest.mark.parametrize("role", [None, "user", "model"])
def test_qwen_unproven_catalog_snapshot_after_own_genesis_fails_closed(bound_native, envelope, role):
    native = bound_native
    binding = replace(native.binding, dispatches=native.binding.dispatches[:1])
    snapshot = {
        "uuid": "foreign-resume", "parentUuid": "answer", "sessionId": native.sid,
        "subtype": "memory_catalog_snapshot", "message": {"parts": [{
            "text": "B PRIVATE MEMORY CATALOG",
            "partMetadata": {"qwen-code:memory-catalog-snapshot": True},
        }]},
    }
    if envelope != "untyped":
        snapshot["type"] = envelope
    if role is not None:
        snapshot["message"]["role"] = role
    rows = [*native.rows[:3], snapshot]  # Own genesis, then resume before foreign user input.
    _write_native(native.path, rows)
    assert journals._matches_dispatch(rows, "qwen", binding, native.sid) is None
    assert journals.read_session("qwen", native.sid, binding=binding) is None
    # The actual reader exposes this envelope, even with type=system and no role.
    legacy = journals.read_session("qwen", native.sid)
    assert legacy.sources[-1]["text"] == "B PRIVATE MEMORY CATALOG"
    assert legacy.sources[-1]["source_kind"] == "memory_catalog"


@pytest.mark.parametrize("bound_native", ["qwen"], indirect=True)
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("role", ["user", "model"])
def test_qwen_inline_unproven_catalog_is_not_conversation_or_compression_proof(bound_native, compressed, role):
    native = bound_native
    catalog = _native_message("qwen", role, "B PRIVATE MEMORY CATALOG", uuid="catalog", parent="last")
    catalog["message"]["parts"][0]["partMetadata"] = {"qwen-code:memory-catalog-snapshot": True}
    rows = [*native.rows, _native_compaction("qwen", [catalog]) if compressed else catalog]
    _write_native(native.path, rows)
    assert journals._matches_dispatch(rows, "qwen", native.binding, native.sid) is None
    assert journals.read_session("qwen", native.sid, binding=native.binding) is None
    assert journals.read_session("qwen", native.sid).sources[-1]["text"] == "B PRIVATE MEMORY CATALOG"


@pytest.mark.parametrize("bound_native", ["qwen"], indirect=True)
def test_qwen_catalog_snapshot_before_genesis_is_omitted_without_shifting_sources(bound_native):
    native = bound_native
    snapshot = {"type": "system", "subtype": "memory_catalog_snapshot", "message": {"parts": [{
        "text": "UNPROVEN STARTUP CATALOG",
        "partMetadata": {"qwen-code:memory-catalog-snapshot": True},
    }]}}
    _write_native(native.path, [native.metadata, snapshot, *native.rows[1:]])
    result = journals.read_session("qwen", native.sid, binding=native.binding)
    assert result is not None and result.partial
    assert [part["text"] for part in result.sources] == [native.prompts[0], "First answer",
                                                       native.prompts[1], "Latest answer"]
    assert result.sources[0]["origin"] == "journal[2].message.parts[0].text"


@pytest.mark.parametrize("bound_native", ["qwen"], indirect=True)
@pytest.mark.parametrize("ignored", ["system", "realtime", "code-mode", "snapshot-no-flag", "snapshot-second-part"])
def test_qwen_ignored_envelopes_do_not_disable_safe_own_journal(bound_native, ignored):
    native = bound_native
    diagnostic = _native_message("qwen", "model", "IGNORED PRIVATE", uuid="diagnostic", parent="last")
    if ignored in {"system", "realtime", "code-mode"}:
        diagnostic["type"] = "system" if ignored == "system" else "assistant"
        diagnostic["subtype"] = {"system": "ui_telemetry", "realtime": "realtime_message",
                                 "code-mode": "code_mode_tool_result"}[ignored]
    else:
        diagnostic.update(type="system", subtype="memory_catalog_snapshot")
        if ignored == "snapshot-second-part":
            diagnostic["message"]["parts"].append({"text": "IGNORED CATALOG", "partMetadata": {
                "qwen-code:memory-catalog-snapshot": True}})
    _write_native(native.path, [*native.rows, diagnostic])
    result = journals.read_session("qwen", native.sid, binding=native.binding)
    assert result is not None
    assert [part["text"] for part in result.sources] == [native.prompts[0], "First answer",
                                                       native.prompts[1], "Latest answer"]
