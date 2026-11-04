"""Qwen Code contract tests with a real subprocess, without model/API calls (§10.6)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from sqlmodel import select

from voxarium_studio.models.teams import AgentChat, AgentMessage, TaskStatus, TeamMember, TeamTask
from voxarium_studio.teams.backends import qwen
from voxarium_studio.teams.backends.base import AgentRunRequest, get_backend
from voxarium_studio.teams.chat import UserContext
from voxarium_studio.teams.tools import build_toolset

TEAM = "team_default"
DEV = f"{TEAM}__dev"
HAPPY = [
    {"type": "system", "subtype": "init", "session_id": "qwen-session", "model": "qwen-model"},
    {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "Check the file."},
        {"type": "tool_use", "id": "tool-1", "name": "edit", "input": {
            "file_path": "main.py", "old_string": "old", "new_string": "new"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": [
            {"type": "text", "text": "Edited."}], "is_error": False},
    ]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "All done."}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "All done.",
     "num_turns": 1, "usage": {"input_tokens": 1000, "cache_read_input_tokens": 200,
                               "output_tokens": 50, "total_tokens": 1050}},
]
FAKE = r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
config_path = Path(args[args.index("--mcp-config") + 1])
record = {"argv": args, "stdin": sys.stdin.read(), "cwd": os.getcwd(),
          "mcp": json.loads(config_path.read_text()), "config_path": str(config_path)}
with open(os.environ["QWEN_TEST_RECORD"], "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\n")
if "--resume" in args and os.environ.get("QWEN_TEST_MISSING"):
    session = args[args.index("--resume") + 1]
    sys.stderr.write('No saved session found with title "' + session + '".'
                     if os.environ["QWEN_TEST_MISSING"] == "title" else
                     "No saved session found with ID " + session + ".")
    sys.exit(1)
sys.stdout.buffer.write(Path(os.environ["QWEN_TEST_STREAM"]).read_bytes())
sys.stdout.flush()
sys.stderr.write(os.environ.get("QWEN_TEST_STDERR", ""))
sys.stderr.flush()
if os.environ.get("QWEN_TEST_HOLD"):
    time.sleep(120)
sys.exit(int(os.environ.get("QWEN_TEST_EXIT", "0")))
'''


class Fake:
    def __init__(self, tmp_path, monkeypatch):
        script = tmp_path / "fake_qwen.py"
        script.write_text(FAKE, encoding="utf-8")
        self.record = tmp_path / "records.jsonl"
        self.stream = tmp_path / "stream.jsonl"
        monkeypatch.setenv("QWEN_TEST_RECORD", str(self.record))
        monkeypatch.setenv("QWEN_TEST_STREAM", str(self.stream))
        monkeypatch.setattr(qwen, "_launcher", lambda: [sys.executable, str(script)])
        self.say(HAPPY)

    def say(self, frames, raw=""):
        self.stream.write_text(raw + "".join(json.dumps(f) + "\n" for f in frames), encoding="utf-8")

    def records(self):
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    return Fake(tmp_path, monkeypatch)


def request(tmp_path, **kwargs):
    member = TeamMember(team_id=TEAM, name="Dev", role_title="Developer", agent_backend="qwen",
                        model="qwen-model", **kwargs.pop("member", {}))
    return AgentRunRequest(member=member, task=TeamTask(team_id=TEAM, title="Test"),
                           workdir=tmp_path, prompt=kwargs.pop("prompt", "Бриф & <test>"),
                           system_prompt=kwargs.pop("system_prompt", "You are Dev."), **kwargs)


async def run(req):
    return [event async for event in qwen.QwenBackend().run(req)]


async def test_protocol_translates_identity_tools_usage_and_result(fake, tmp_path):
    events = await run(request(tmp_path))
    call = next(e for e in events if e.kind == "tool_use")
    assert call.text == "Edit" and call.data["id"] == "tool-1"
    assert call.data["name"] == "Edit" and call.data["input"]["file_path"] == "main.py"
    assert call.data["edit"] == {"insertions": 1, "deletions": 1}
    assert next(e for e in events if e.kind == "tool_result").data["tool_use_id"] == "tool-1"
    assert next(e for e in events if e.kind == "thinking").text == "Check the file."
    result = events[-1]
    assert result.is_final and not result.data["is_error"]
    assert result.text == "All done." and result.session_id == "qwen-session"
    assert result.data["total_tokens"] == 1050
    assert result.data["usage"]["input_tokens"] == 800
    assert result.data["cost_unavailable"] is True
    record = fake.records()[0]
    assert record["stdin"] == "Бриф & <test>"
    assert "You are Dev." in record["argv"]
    assert not Path(record["config_path"]).exists()


@pytest.mark.parametrize("mode,expected", list(qwen._MODES.items()))
async def test_permissions_and_tool_names(fake, tmp_path, mode, expected):
    await run(request(tmp_path, member={"permission_mode": mode,
                                       "allowed_tools": ["Read", "Edit", "Bash"]}))
    argv = fake.records()[0]["argv"]
    assert argv[argv.index("--approval-mode") + 1] == expected
    if mode == "plan":
        assert "--allowed-tools" not in argv
    else:
        assert {"read_file", "edit", "run_shell_command"} <= set(argv)


@pytest.mark.parametrize("resume", [None, "prior-session"])
async def test_resume_roots_and_mcp_configuration(fake, tmp_path, resume):
    servers = {"local": {"command": "test", "args": ["a b"]},
               "remote": {"type": "http", "url": "https://example.test/mcp"}}
    await run(request(tmp_path, resume_session_id=resume, mcp_servers=servers,
                      writable_roots=[tmp_path / "project"],
                      extra_allowed_tools=["mcp__local__search", "Read(/skills/**)"]))
    record = fake.records()[0]
    argv = record["argv"]
    assert ("--resume" in argv) == bool(resume)
    assert str(tmp_path / "project") in argv
    assert "mcp__local__search" in argv
    assert "read_file(/skills/**)" in argv
    assert record["mcp"]["mcpServers"]["remote"] == {"httpUrl": "https://example.test/mcp"}
    assert "--allowed-mcp-server-names" in argv
    assert "type" not in record["mcp"]["mcpServers"]["remote"]


async def test_non_strict_mcp_preserves_discovery(fake, tmp_path):
    await run(request(tmp_path, strict_mcp_config=False))
    assert "--allowed-mcp-server-names" not in fake.records()[0]["argv"]


async def test_model_fallback_long_role_and_image_paths(fake, tmp_path):
    req = request(tmp_path, model="sonnet", system_prompt="role" * 10000,
                  images=[{"path": str(tmp_path / "image.png")}])
    events = await run(req)
    record = fake.records()[0]
    assert "--model" not in record["argv"] and "--append-system-prompt" not in record["argv"]
    assert req.system_prompt in record["stdin"] and "image.png" in record["stdin"]
    assert {"model_fallback", "system_prompt_in_message"} <= {
        e.data.get("subtype") for e in events if e.kind == "system"}


@pytest.mark.parametrize("error_style", ["id", "title"])
async def test_missing_resume_retries_once_with_saved_context(fake, tmp_path, monkeypatch, error_style):
    monkeypatch.setenv("QWEN_TEST_MISSING", error_style)
    events = await run(request(tmp_path, resume_session_id="old", recovery_prompt="Saved history"))
    records = fake.records()
    assert len(records) == 2
    assert "--resume" in records[0]["argv"] and "--resume" not in records[1]["argv"]
    assert records[1]["stdin"] == "Saved history"
    assert not any(e.kind == "error" for e in events)
    assert any(e.data.get("subtype") == "session_recovered" for e in events)


@pytest.mark.parametrize("frames,code,stderr", [
    ([], "0", ""), (HAPPY, "7", "Process failed"),
    ([{"type": "result", "is_error": True, "error": {"message": "Rate limited"}}], "0", ""),
])
async def test_failed_or_truncated_stream_never_succeeds(fake, tmp_path, monkeypatch, frames, code, stderr):
    fake.say(frames)
    monkeypatch.setenv("QWEN_TEST_EXIT", code)
    monkeypatch.setenv("QWEN_TEST_STDERR", stderr)
    events = await run(request(tmp_path))
    assert events[-1].data["is_error"]
    assert any(e.kind == "error" for e in events)
    assert len(fake.records()) == 1


async def test_no_retry_after_work_even_if_error_mentions_missing_session(fake, tmp_path, monkeypatch):
    fake.say(HAPPY[:-1])
    monkeypatch.setenv("QWEN_TEST_EXIT", "1")
    monkeypatch.setenv("QWEN_TEST_STDERR", "No saved session found with ID old.")
    events = await run(request(tmp_path, resume_session_id="old"))
    assert events[-1].data["is_error"] and len(fake.records()) == 1


async def test_oversized_lines_and_noise_do_not_destroy_following_frames(fake, tmp_path):
    fake.say(HAPPY, raw="warning\n" + "x" * 100000 + "\n")
    events = await run(request(tmp_path, max_message_bytes=1000))
    assert any(e.data.get("subtype") == "oversized_line" for e in events)
    assert not events[-1].data["is_error"]


async def test_cancel_kills_process_and_cleans_config(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_TEST_HOLD", "1")
    fake.say(HAPPY[:1])
    started = asyncio.Event()
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(qwen.asyncio, "create_subprocess_exec", capture)

    async def consume():
        async for event in qwen.QwenBackend().run(request(tmp_path)):
            if event.data.get("subtype") == "init":
                started.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert processes[0].returncode is not None
    assert not Path(fake.records()[0]["config_path"]).exists()


def test_windows_launcher_uses_package_bin_without_shell(tmp_path, monkeypatch):
    package = tmp_path / "node_modules" / "@qwen-code" / "qwen-code"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"bin":{"qwen":"cli-entry.js"}}')
    (package / "cli-entry.js").write_text("")
    monkeypatch.setattr(qwen.shutil, "which", lambda name: str(tmp_path / "qwen.cmd")
                        if name == "qwen" else "node.exe")
    assert qwen._launcher() == ["node.exe", str(package / "cli-entry.js")]


def test_missing_cli_has_actionable_status(monkeypatch):
    monkeypatch.setattr(qwen, "_launcher", lambda: None)
    ready, reason = get_backend("qwen").is_available()
    assert not ready and "@qwen-code/qwen-code" in reason


def test_api_lists_and_saves_qwen_backend(client, fake):
    options = client.get("/api/teams/member-options").json()
    assert options["agent_backends"]["qwen"]["available"]
    assert options["agent_backends"]["qwen"]["named_agents"] is False
    response = client.patch(f"/api/teams/{TEAM}/members/{DEV}",
                            json={"agent_backend": "qwen", "model": ""})
    assert response.status_code == 200
    assert response.json()["agent_backend"] == "qwen"


async def test_task_and_mcp_bridge(ctx, fake, wait_for):
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        dev.agent_backend = "qwen"
        dev.model = "qwen-model"
        dev.isolate_worktree = False
        session.add(dev)
    task = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV, title="Test Qwen",
                                       brief="Lower cooldown", auto_start=True)
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await wait_for(task.id, timeout=20)
    async with ctx.db.session() as session:
        stored = await session.get(TeamTask, task.id)
        messages = (await session.exec(select(AgentMessage).where(AgentMessage.task_id == task.id))).all()
    assert stored.status == TaskStatus.NEEDS_REVIEW
    assert stored.agent_session_id == "qwen-session" and stored.total_tokens == 1050
    assert stored.result_summary == "All done."
    assert not any(m.kind == "agent" for m in messages)
    record = fake.records()[0]
    assert "Lower cooldown" in record["stdin"]
    if build_toolset(ctx.teams, member=dev) is not None:
        bridge = record["mcp"]["mcpServers"]["voxarium_studio"]
        assert bridge["command"] == sys.executable
        assert "mcp_proxy.py" in bridge["args"][1]


async def test_chat_recovery_restores_saved_history(ctx, fake, wait_for, monkeypatch):
    monkeypatch.setenv("QWEN_TEST_MISSING", "1")
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        dev.agent_backend = "qwen"
        session.add(dev)
    chat = await ctx.chats.create_chat(DEV)
    await ctx.chats.record(chat.id, DEV, role="user", kind="text", text="Make the model blue")
    async with ctx.db.session() as session:
        stored = await session.get(AgentChat, chat.id)
        stored.agent_session_id = "missing"
        session.add(stored)
    await ctx.chats.send(chat.id, "Continue", UserContext())
    await wait_for(chat.id, timeout=20)
    async with ctx.db.session() as session:
        stored = await session.get(AgentChat, chat.id)
    assert stored.agent_session_id == "qwen-session" and stored.error is None
    assert "Make the model blue" in fake.records()[-1]["stdin"]
