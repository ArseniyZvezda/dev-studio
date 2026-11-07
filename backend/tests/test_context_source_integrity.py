"""The map can classify evidence without amending it or changing the next read (§10.4)."""
import copy
import json

import pytest

from voxarium_studio.api import session_context
from voxarium_studio.models import AgentMessage, ChatSessionContext, TaskContext
from voxarium_studio.teams.context_sources import JournalContext, prompt_sources, source
from voxarium_studio.teams.context_viewer import inventory, native_inventory, present


def test_classification_is_a_lossless_partition_of_one_field():
    prefix = "  📦\r\nRules\r\n"
    memory = ("## Memory\r\nUse the following record.\r\n========= MEMORY_SUMMARY BEGINS =========\r\n"
              "## Prior decisions\r\n  точный текст\t\r\n========= MEMORY_SUMMARY ENDS =========\r\n")
    tail = "\r\nOther instructions  "
    text = prefix + memory + tail
    blocks = prompt_sources(text, "developer", instructions=True, origin="journal[7].text")
    assert [(b["category"], b["text"]) for b in blocks] == [
        ("developer", prefix), ("memory", memory), ("developer", tail)]
    assert len({b["origin"] for b in blocks}) == 3
    assert [b["text"] for b in prompt_sources(text, "history")] == [text]
    quoted = "```markdown\n" + text + "\n```"
    assert prompt_sources(quoted, "system", instructions=True) == [source("system", quoted)]
    clipped = memory.partition("========= MEMORY_SUMMARY ENDS")[0]
    assert prompt_sources(clipped, "developer", instructions=True) == [source("memory", clipped)]


def test_files_and_environment_share_a_scalar_but_do_not_become_one_instruction():
    agents = "# AGENTS.md instructions for a\n\n<INSTRUCTIONS>\nbody\n</INSTRUCTIONS>\n"
    env = "<environment_context>\n  cwd\n</environment_context>"
    result = prompt_sources(agents + env, "task", instructions=True)
    assert [(p["category"], p["text"]) for p in result] == [("memory", agents), ("environment", env)]


def test_presentation_is_idempotent_and_empty_and_repeated_records_remain_addressable():
    parts = [source("task", "", origin="j[0]"), source("task", "  ", origin="j[1]"),
             source("task", "  ", origin="j[2]")]
    original = copy.deepcopy(parts)
    def read():
        return present(parts, 100, 1000, origin="codex_journal", partial=True)
    a, b = read(), read()
    assert a == b and parts == original
    assert [(s["id"], s["text"]) for s in a["sources"][:-1]] == [("0", ""), ("1", "  "), ("2", "  ")]
    assert a["sources"][-1]["tokens"] == 98


def test_tool_arguments_are_not_a_synthetic_prompt_and_worker_usage_is_independent():
    def message(kind, text, data):
        return AgentMessage(task_id="task", member_id="dev", role="assistant", kind=kind, text=text, data=data)
    call = message("tool_use", "Read", {"name": "Read", "input": {"path": " a "}})
    parent = message("system", "usage_update", {"context_tokens": 90})
    child = message("system", "usage_update", {"context_tokens": 5000, "parent_tool_use_id": "worker"})
    view = inventory(None, [call, parent, child])
    part = view["sources"][0]
    assert view["reported_tokens"] == 90
    assert part["label"] == "Read" and part["representation"] == "json"
    assert json.loads(part["text"]) == call.data["input"]
    assert part["origin"].endswith(".data.input")


def test_old_studio_timeline_uses_message_roles_without_inventing_a_system_prompt():
    def entry(role, text):
        return AgentMessage(task_id="task", member_id="dev", role=role, kind="text", text=text)
    view = inventory(None, [entry("user", "  Hello"), entry("assistant", "Hi"), entry("system", "UI notice")])
    assert [(p["category"], p["text"]) for p in view["sources"]] == [("task", "  Hello"), ("history", "Hi")]


@pytest.mark.parametrize("backend,marker,wire,category", [
    ("qwen", "system_prompt_in_message", "Studio instructions:\n  sys  \n\nUser message:\n", "system"),
    ("codex", "instructions_in_prompt", "<instructions>\nsys\n</instructions>\n\n", "developer"),
])
async def test_recorded_transport_boundary_uses_only_journal_characters(backend, marker, wire, category, tmp_path, monkeypatch):
    if backend == "codex":
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        path = tmp_path / "sessions" / "rollout-t-session.jsonl"
        row = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": wire + "user\t "}}
    else:
        monkeypatch.setenv("QWEN_RUNTIME_DIR", str(tmp_path))
        path = tmp_path / "projects" / "example" / "chats" / "session.jsonl"
        row = {"message": {"role": "user", "parts": [{"text": wire + "user\t "}]}}
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(row), encoding="utf8")
    snapshot = TaskContext(task_id="task", backend=backend, sources=[source("system", "  sys  ")])
    notice = AgentMessage(task_id="task", member_id="dev", role="system", kind="system", data={"subtype": marker})
    parts, _ = await session_context.read_context(snapshot, [notice], None, "session")
    assert [(p["category"], p["text"]) for p in parts] == [(category, wire), ("task", "user\t ")]
    # The same string without the adapter's notice remains an ordinary user field.
    parts, _ = await session_context.read_context(snapshot, [], None, "session")
    assert [(p["category"], p["text"]) for p in parts] == [("task", wire + "user\t ")]


def test_native_category_metadata_is_not_presented_as_prompt_content():
    category = {"name": "System prompt", "tokens": 5}
    part, = native_inventory({"totalTokens": 5, "categories": [category]})["sources"]
    assert part["representation"] == "metadata" and part["origin"] == "runtime.categories[0]"
    assert json.loads(part["text"]) == category


@pytest.mark.parametrize("owner", ["task", "chat"])
def test_api_keeps_journal_and_studio_capture_separate(client, monkeypatch, owner):
    team = "team_default"
    dev = team + "__dev"
    if owner == "task":
        key = client.post(f"/api/teams/{team}/tasks", json={
            "assignee_id": dev, "title": "Context", "auto_start": False}).json()["id"]
        url, snapshot = f"/api/teams/{team}/tasks/{key}/context", TaskContext(task_id=key)
    else:
        key = client.post("/api/chats", json={"member_id": dev}).json()["id"]
        url, snapshot = f"/api/chats/{key}/context", ChatSessionContext(chat_id=key)
    body = " before\r\nafter\t" * 3000
    snapshot.backend = "qwen"
    snapshot.sources = [source("system", body)]
    journal = JournalContext(sources=[source("task", body, origin="journal[0].message.parts[0].text")])
    async def store():
        async with client.app.state.ctx.db.session() as db:
            db.add(snapshot)
    client.portal.call(store)
    def read(backend, session_id, *, studio_system):
        assert backend == "qwen"
        assert not studio_system
        return journal
    monkeypatch.setattr(session_context, "read_session", read)
    response = client.get(url).json()
    assert response["origin"] == "qwen_journal" and response["has_studio_view"]
    part, = response["sources"]
    assert part["category"] == "task" and part["truncated"]
    full = url + "/fragments/0"
    assert client.get(full, params={"key": part["text_key"]}).json() == {"text": body}
    # Identical bytes at the same position do not make these the same source.
    assert client.get(full, params={"key": part["text_key"], "view": "studio"}).status_code == 409
    def unexpected_read(*args):
        pytest.fail("Studio view must not read the provider session")
    monkeypatch.setattr(session_context, "read_session", unexpected_read)
    result = client.get(url, params={"view": "studio"}).json()
    captured, = result["sources"]
    assert result["origin"] == "studio" and captured["category"] == "system"
    assert client.get(full, params={"key": captured["text_key"], "view": "studio"}).json() == {"text": body}
