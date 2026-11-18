"""Antigravity contract tests: a real subprocess speaking ACP, without model/API calls (§10.6)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from sqlmodel import select

from voxarium_studio.core.config import default_orchestrator_models, validate_orchestrator_models
from voxarium_studio.models.teams import AgentMessage, TaskStatus, TeamMember, TeamTask
from voxarium_studio.teams.backends import antigravity
from voxarium_studio.teams.backends.base import AgentRunRequest, get_backend
from voxarium_studio.teams.tools import build_toolset

TEAM = "team_default"
DEV = f"{TEAM}__dev"
SID = "agy-session"

# What `agy acp` 1.5.10 sent for one turn, trimmed: chunks, an MCP call, an edit, a shell
# command that asks for permission, and context usage.
HAPPY = [
    {"sessionUpdate": "usage_update", "used": 0, "size": 1048576},
    {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "Check "}},
    {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "the file."}},
    {"sessionUpdate": "tool_call", "toolCallId": "call-mcp", "name": "mcp_voxarium_studio_list_tasks",
     "args": {"toolSummary": "List"}, "status": "in_progress", "kind": "other",
     "rawInput": {"toolSummary": "List"}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "call-mcp", "status": "completed",
     "kind": "other", "content": [{"type": "content", "content": {"type": "text", "text": "3 tasks"}}]},
    {"sessionUpdate": "tool_call", "toolCallId": "call-edit", "name": "edit_file", "status": "in_progress",
     "kind": "edit", "rawInput": {"Path": "main.py", "Target": "old", "Replacement": "new"}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "call-edit", "status": "completed", "kind": "edit"},
    {"permission": {"toolCallId": "call-run", "name": "run_command", "kind": "execute",
                    "rawInput": {"CommandLine": "echo hi"}}},
    {"sessionUpdate": "usage_update", "used": 9656, "size": 1048576},
    {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "All "}},
    {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done."}},
]
USAGE = {"inputTokens": 1000, "outputTokens": 40, "cachedReadTokens": 200,
         "cachedWriteTokens": 0, "thoughtTokens": 10, "totalTokens": 1250}

FAKE = r'''
import json, os, sys
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
record_path = os.environ["AGY_TEST_RECORD"]
script = json.loads(Path(os.environ["AGY_TEST_SCRIPT"]).read_text(encoding="utf-8"))

def record(entry):
    with open(record_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

def update(session, body):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": session, "update": body}})

record({"argv": sys.argv[1:], "cwd": os.getcwd()})
session_response = {
    "sessionId": script.get("session_id", "agy-session"),
    "models": {"availableModels": [{"modelId": "gemini-test-high", "name": "Test"}],
               "currentModelId": "gemini-test-high"},
    "modes": {"currentModeId": "default", "availableModes": [
        {"id": "default"}, {"id": "accept-edits"}, {"id": "plan"}]},
    "configOptions": [
        {"id": "model", "options": [{"value": "gemini-test"}]},
        {"id": "thought_level", "options": [{"value": "low"}, {"value": "medium"}, {"value": "high"}]},
    ],
}
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    record({"in": message})
    if method is None:
        continue  # an answer to our permission request, already recorded
    reply = {"jsonrpc": "2.0", "id": message["id"]}
    params = message.get("params") or {}
    if method == "initialize":
        reply["result"] = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
    elif method == "session/new":
        if script.get("new_error"):
            reply["error"] = {"code": -32000, "message": script["new_error"]}
        else:
            reply["result"] = session_response
    elif method == "session/load":
        if script.get("missing"):
            reply["error"] = {"code": -32002, "message": "Resource not found"}
        else:
            # The history is replayed before the answer, as agy does.
            update(params["sessionId"], {"sessionUpdate": "user_message_chunk",
                                         "content": {"type": "text", "text": "old brief"}})
            update(params["sessionId"], {"sessionUpdate": "agent_message_chunk",
                                         "content": {"type": "text", "text": "OLD ANSWER"}})
            reply["result"] = {**session_response, "sessionId": params["sessionId"]}
    elif method == "session/prompt":
        session = params["sessionId"]
        if script.get("crash"):
            sys.stderr.write("fatal: boom")
            sys.exit(3)
        for body in script["updates"]:
            if "permission" in body:
                call = dict(body["permission"], sessionUpdate="tool_call", status="pending")
                update(session, call)
                send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission",
                      "params": {"sessionId": session, "toolCall": call, "options": [
                          {"optionId": "allow", "kind": "allow_once"},
                          {"optionId": "deny", "kind": "reject_once"}]}})
                answer = json.loads(sys.stdin.readline())
                record({"in": answer})
                allowed = answer["result"]["outcome"].get("optionId") == "allow"
                update(session, {"sessionUpdate": "tool_call_update",
                                 "toolCallId": call["toolCallId"],
                                 "status": "completed" if allowed else "failed"})
            else:
                update(session, body)
        if script.get("hold"):
            import time; time.sleep(120)
        if script.get("prompt_error"):
            reply["error"] = {"code": -32000, "message": script["prompt_error"]}
        else:
            reply["result"] = {"stopReason": script.get("stop", "end_turn"), "usage": script["usage"]}
    else:
        reply["result"] = {}
    send(reply)
'''


class Fake:
    def __init__(self, tmp_path, monkeypatch):
        script = tmp_path / "fake_agy.py"
        script.write_text(FAKE, encoding="utf-8")
        self.record = tmp_path / "records.jsonl"
        self.script = tmp_path / "script.json"
        monkeypatch.setenv("AGY_TEST_RECORD", str(self.record))
        monkeypatch.setenv("AGY_TEST_SCRIPT", str(self.script))
        monkeypatch.setattr(antigravity, "_launcher", lambda: [sys.executable, str(script)])
        self.say()

    def say(self, updates=None, **options):
        self.script.write_text(json.dumps({
            "updates": HAPPY if updates is None else updates, "usage": USAGE, **options}),
            encoding="utf-8")

    def records(self):
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def calls(self, method=None):
        calls = [r["in"] for r in self.records() if "in" in r and "method" in r["in"]]
        return [c for c in calls if method is None or c["method"] == method]

    def permission_answers(self):
        return [r["in"]["result"]["outcome"] for r in self.records()
                if "in" in r and r["in"].get("id") == "perm-1"]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    return Fake(tmp_path, monkeypatch)


def request(tmp_path, **kwargs):
    # Members default to bypassPermissions; these tests start from the strictest headless mode.
    member = TeamMember(team_id=TEAM, name="Dev", role_title="Developer",
                        agent_backend="antigravity", model="gemini-test-high",
                        **{"permission_mode": "default", **kwargs.pop("member", {})})
    return AgentRunRequest(member=member, task=TeamTask(team_id=TEAM, title="Test"),
                           workdir=tmp_path, prompt=kwargs.pop("prompt", "Бриф & <test>"),
                           system_prompt=kwargs.pop("system_prompt", "You are Dev."), **kwargs)


async def run(req):
    return [event async for event in antigravity.AntigravityBackend().run(req)]


async def test_protocol_translates_identity_tools_usage_and_result(fake, tmp_path):
    servers = {"voxarium_studio": {"command": "py", "args": ["a b"], "env": {"K": "v"}},
               "remote": {"type": "http", "url": "https://example.test/mcp",
                          "headers": {"Authorization": "x"}}}
    events = await run(request(tmp_path, mcp_servers=servers, effort="xhigh",
                               extra_allowed_tools=["mcp__voxarium_studio"]))

    assert [e.text for e in events if e.kind == "thinking"] == ["Check the file."]
    assert [e.text for e in events if e.kind == "text"] == ["All done."], \
        "chunks gather into one entry per message"
    calls = [e for e in events if e.kind == "tool_use"]
    assert [c.data["name"] for c in calls] == ["mcp__voxarium_studio__list_tasks", "Edit", "Bash"]
    edit = calls[1]
    assert edit.data["input"] == {"file_path": "main.py", "old_string": "old", "new_string": "new"}
    assert edit.data["edit"] == {"insertions": 1, "deletions": 1}
    results = {e.data["tool_use_id"]: e for e in events if e.kind == "tool_result"}
    assert results["call-mcp"].text == "3 tasks"
    assert results["call-run"].data["is_error"], "default mode does not run shell commands"
    contexts = [e.data for e in events if e.data.get("subtype") == "usage_update"]
    assert contexts[-1] == {"subtype": "usage_update", "context_tokens": 9656,
                            "context_window": 1048576}
    assert all(e.session_id == SID for e in events if e.kind != "error")
    result = events[-1]
    assert result.is_final and not result.data["is_error"] and result.text == "All done."
    assert result.data["usage"] == {"input_tokens": 1000, "output_tokens": 50,
                                    "cache_read_input_tokens": 200,
                                    "cache_creation_input_tokens": 0}
    assert result.data["total_tokens"] == 1250 and result.data["total_cached_tokens"] == 200
    assert result.data["cost_unavailable"] is True

    assert fake.records()[0]["argv"] == ["acp"]
    [new] = fake.calls("session/new")
    params = new["params"]
    assert params["_meta"] == {"antigravity": {"systemPrompt": {"append": "You are Dev."}}}
    assert {"name": "voxarium_studio", "command": "py", "args": ["a b"],
            "env": [{"name": "K", "value": "v"}]} in params["mcpServers"]
    assert {"type": "http", "name": "remote", "url": "https://example.test/mcp",
            "headers": [{"name": "Authorization", "value": "x"}]} in params["mcpServers"]
    [prompt] = fake.calls("session/prompt")
    assert prompt["params"]["prompt"] == [{"type": "text", "text": "Бриф & <test>"}]
    assert fake.calls("session/set_model")[0]["params"]["modelId"] == "gemini-test-high"
    assert fake.calls("session/set_config_option")[0]["params"] == {
        "sessionId": SID, "configId": "thought_level", "value": "high"}
    assert not fake.calls("session/set_mode"), "default is already the session's mode"


@pytest.mark.parametrize("mode,allowed,acp_mode", [
    ("default", False, None), ("dontAsk", False, None), ("plan", False, "plan"),
    ("acceptEdits", False, "accept-edits"), ("auto", True, "accept-edits"),
    ("bypassPermissions", True, "accept-edits"),
])
async def test_permission_modes_answer_requests(fake, tmp_path, mode, allowed, acp_mode):
    fake.say([HAPPY[7]])
    await run(request(tmp_path, member={"permission_mode": mode}))
    [outcome] = fake.permission_answers()
    assert outcome["optionId"] == ("allow" if allowed else "deny")
    modes = [c["params"]["modeId"] for c in fake.calls("session/set_mode")]
    assert modes == ([acp_mode] if acp_mode else [])


async def test_a_denial_that_ends_the_turn_is_an_error(fake, tmp_path):
    """agy 1.5.10 ends the turn at the first refused tool; the run must not read as done."""
    fake.say([HAPPY[9], HAPPY[7]])
    events = await run(request(tmp_path, member={"permission_mode": "acceptEdits"}))
    assert events[-1].data["is_error"]
    assert any(e.kind == "error" and "denied Bash" in e.text and "acceptEdits" in e.text
               for e in events)
    # The model carrying on after the refusal is an ordinary, successful turn.
    fake.say()
    assert not (await run(request(tmp_path)))[-1].data["is_error"]


@pytest.mark.parametrize("tools,allowed", [
    (["Bash"], True), (["Bash(git:*)"], False), (["Read"], False),
])
async def test_allowed_tools_open_a_call_outside_plan(fake, tmp_path, tools, allowed):
    fake.say([HAPPY[7]])
    await run(request(tmp_path, member={"allowed_tools": tools}))
    assert fake.permission_answers()[0]["optionId"] == ("allow" if allowed else "deny")


def test_permission_policy_by_kind():
    req = request(Path("."), member={"permission_mode": "acceptEdits"})
    assert antigravity._permitted(req, "Edit", "edit")
    assert antigravity._permitted(req, "Read", "read")
    assert not antigravity._permitted(req, "Bash", "execute")
    plan = request(Path("."), member={"permission_mode": "plan", "allowed_tools": ["Bash"]})
    assert not antigravity._permitted(plan, "Bash", "execute")
    assert antigravity._permitted(plan, "Grep", "search")
    text_only = request(Path("."), member={"permission_mode": "bypassPermissions"}, text_only=True)
    assert not antigravity._permitted(text_only, "Read", "read")
    servers = request(Path("."), extra_allowed_tools=["mcp__voxarium_studio"])
    assert antigravity._permitted(servers, "mcp__voxarium_studio__write_doc", "other")
    assert not antigravity._permitted(servers, "mcp__other__tool", "other")


def test_tool_names_map_to_the_studio_vocabulary():
    servers = ["voxarium_studio", "voxarium"]
    assert antigravity._tool_name("mcp_voxarium_studio_write_doc", servers) == \
        "mcp__voxarium_studio__write_doc", "the longest server name wins"
    assert antigravity._tool_name("mcp_voxarium_x", servers) == "mcp__voxarium__x"
    assert antigravity._tool_name("write_file", servers) == "Write"
    assert antigravity._tool_name("browser_click_element", servers) == "browser_click_element"


async def test_resume_mutes_replayed_history(fake, tmp_path):
    events = await run(request(tmp_path, resume_session_id="prior", writable_roots=[tmp_path / "p"]))
    assert "OLD ANSWER" not in [e.text for e in events]
    assert [e.text for e in events if e.kind == "text"] == ["All done."]
    [load] = fake.calls("session/load")
    assert load["params"]["sessionId"] == "prior"
    assert load["params"]["additionalDirectories"] == [str((tmp_path / "p").resolve())]
    assert not fake.calls("session/new")
    assert events[-1].session_id == "prior" and not events[-1].data["is_error"]


async def test_missing_resume_starts_a_new_session_with_saved_context(fake, tmp_path):
    fake.say(missing=True)
    events = await run(request(tmp_path, resume_session_id="gone", recovery_prompt="Saved history"))
    assert any(e.data.get("subtype") == "session_recovered" for e in events)
    assert len(fake.calls("session/new")) == 1
    assert fake.calls("session/prompt")[0]["params"]["prompt"][0]["text"] == "Saved history"
    assert not any(e.kind == "error" for e in events)
    assert events[-1].session_id == SID


async def test_model_not_offered_falls_back_with_a_note(fake, tmp_path):
    events = await run(request(tmp_path, model="claude-fable-5-2", max_turns=5))
    assert not fake.calls("session/set_model")
    subtypes = {e.data.get("subtype") for e in events if e.kind == "system"}
    assert {"model_fallback", "unsupported_settings"} <= subtypes
    await run(request(tmp_path, model="gemini-test"))
    assert fake.calls("session/set_config_option")[-1]["params"]["configId"] == "model"


async def test_images_travel_as_blocks(fake, tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG")
    await run(request(tmp_path, images=[{"path": str(image), "media_type": "image/png"},
                                        {"path": str(tmp_path / "missing.png")}]))
    blocks = fake.calls("session/prompt")[0]["params"]["prompt"]
    assert [b["type"] for b in blocks] == ["text", "image"]
    assert blocks[1] == {"type": "image", "mimeType": "image/png", "data": "iVBORw=="}


@pytest.mark.parametrize("options,needle", [
    ({"crash": True}, "boom"),
    ({"prompt_error": "Quota exhausted"}, "Quota exhausted"),
    ({"new_error": "Authentication required"}, "run `agy` once"),
    ({"stop": "refusal"}, "refusal"),
])
async def test_failures_never_succeed(fake, tmp_path, options, needle):
    fake.say(**options)
    events = await run(request(tmp_path))
    assert events[-1].is_final and events[-1].data["is_error"]
    assert any(e.kind == "error" and needle in e.text for e in events)


async def test_cancel_kills_the_process(fake, tmp_path, monkeypatch):
    fake.say(HAPPY[:2], hold=True)
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(antigravity.asyncio, "create_subprocess_exec", capture)
    started = asyncio.Event()

    async def consume():
        async for event in antigravity.AntigravityBackend().run(request(tmp_path)):
            if event.data.get("subtype") == "usage_update":
                started.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert processes and processes[0].returncode is not None


def test_missing_cli_has_actionable_status(monkeypatch):
    monkeypatch.setattr(antigravity, "_launcher", lambda: None)
    ready, reason = get_backend("antigravity").is_available()
    assert not ready and "agy" in reason


def test_suggested_models_are_antigravity_rows():
    models = antigravity.suggested_models()
    assert models[0]["id"] == "" and all(m["backend"] == "antigravity" for m in models)
    models[0]["id"] = "mutated"
    assert antigravity.suggested_models()[0]["id"] == ""


def test_saved_orchestrator_table_gains_the_antigravity_column():
    saved = {name: tiers for name, tiers in default_orchestrator_models().items()
             if name != "antigravity"}
    saved["codex"] = {"1": "a", "2": "b", "3": "c"}
    table = validate_orchestrator_models(saved)
    assert table["antigravity"] == default_orchestrator_models()["antigravity"]
    assert table["codex"] == {"1": "a", "2": "b", "3": "c"}
    custom = validate_orchestrator_models({**saved, "antigravity": {"1": "x", "2": "y", "3": "z"}})
    assert custom["antigravity"] == {"1": "x", "2": "y", "3": "z"}


def test_api_lists_and_saves_antigravity_backend(client, fake):
    options = client.get("/api/teams/member-options").json()
    assert options["agent_backends"]["antigravity"]["available"]
    assert options["agent_backends"]["antigravity"]["named_agents"] is False
    assert any(m["backend"] == "antigravity" for m in options["suggested_models"])
    response = client.patch(f"/api/teams/{TEAM}/members/{DEV}",
                            json={"agent_backend": "antigravity", "model": ""})
    assert response.status_code == 200 and response.json()["agent_backend"] == "antigravity"
    assert "antigravity" in client.get("/api/status").json()["agent_backends"]


async def test_task_runs_through_the_mcp_bridge(ctx, fake, wait_for):
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        dev.agent_backend = "antigravity"
        dev.model = "gemini-test-high"
        dev.isolate_worktree = False
        session.add(dev)
    task = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV, title="Test Antigravity",
                                       brief="Lower cooldown", auto_start=True)
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await wait_for(task.id, timeout=20)
    async with ctx.db.session() as session:
        stored = await session.get(TeamTask, task.id)
        messages = (await session.exec(select(AgentMessage).where(AgentMessage.task_id == task.id))).all()
    assert stored.status == TaskStatus.NEEDS_REVIEW
    assert stored.agent_session_id == SID and stored.total_tokens == 1250
    assert stored.result_summary == "All done."
    assert any(m.kind == "tool_use" and m.data.get("name") == "Edit" for m in messages)
    [prompt] = fake.calls("session/prompt")
    assert "Lower cooldown" in prompt["params"]["prompt"][0]["text"]
    if build_toolset(ctx.teams, member=dev) is not None:
        servers = {s["name"]: s for s in fake.calls("session/new")[0]["params"]["mcpServers"]}
        bridge = servers["voxarium_studio"]
        assert bridge["command"] == sys.executable and "mcp_proxy.py" in bridge["args"][1]
