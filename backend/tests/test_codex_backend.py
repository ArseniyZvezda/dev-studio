"""The Codex CLI backend (§10.6), against a fake ``codex`` executable.

Nothing here reaches OpenAI: ``fake_codex.py`` is a stand-in launched the way the real
binary is — a subprocess fed the prompt on stdin — that records what it was given and
prints a scripted JSONL stream. What is under test is the translation in both directions:
the request into a command line, and Codex's events into the Studio's.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

from voxarium_studio.core.context import AppContext
from voxarium_studio.models.teams import AgentChat, AgentMessage, TaskStatus, TeamMember, TeamTask
from voxarium_studio.teams.backends import codex
from voxarium_studio.teams.backends.base import AgentEvent, AgentRunRequest, get_backend
from voxarium_studio.teams.chat import UserContext
from voxarium_studio.teams.tools import build_toolset

TEAM = "team_default"
DEV = f"{TEAM}__dev"

FAKE_CODEX = '''
import json, os, sys
record = os.environ["FAKE_CODEX_RECORD"]
stdin = sys.stdin.read() if "-" in sys.argv[1:] else ""
with open(record, "w", encoding="utf-8") as fh:
    json.dump({"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd()}, fh)
with open(record + ".history", "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\\n")
with open(record + ".history", encoding="utf-8") as fh:
    attempt = len(fh.readlines())
if "resume" in sys.argv and attempt <= int(os.environ.get("FAKE_CODEX_BUSY_ATTEMPTS", "0")):
    error = "Error: thread/resume: thread/resume failed: thread " + sys.argv[-2] + " already has an active writer (code -32600)"
    if os.environ.get("FAKE_CODEX_BUSY_FORMAT") == "json":
        print(json.dumps({"type": "error", "message": error}))
    else:
        sys.stderr.write("ERROR failed to refresh available models: request timed out\\n" + error)
    sys.exit(1)
if "resume" in sys.argv and os.environ.get("FAKE_CODEX_MISSING_RESUME"):
    error = "Error: thread/resume: thread/resume failed: no rollout found for thread id " + sys.argv[-2] + " (code -32600)"
    if os.environ["FAKE_CODEX_MISSING_RESUME"] == "json":
        print(json.dumps({"type": "error", "message": error}))
    else:
        sys.stderr.write(error)
    sys.exit(1)
script = os.environ.get("FAKE_CODEX_SCRIPT")
if script:
    with open(script, "rb") as fh:
        sys.stdout.buffer.write(fh.read())
    sys.stdout.flush()
sys.stderr.write(os.environ.get("FAKE_CODEX_STDERR", ""))
sys.stderr.flush()
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
'''

#: One ordinary run, every item type the CLI emits, as it prints them with ``--json``.
HAPPY_RUN = [
    {"type": "thread.started", "thread_id": "thr_1"},
    {"type": "turn.started"},
    {
        "type": "item.started",
        "item": {"id": "item_0", "type": "command_execution", "command": "ls", "status": "in_progress"},
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "command_execution",
            "command": "ls",
            "aggregated_output": "a.txt\n",
            "exit_code": 0,
            "status": "completed",
        },
    },
    {"type": "item.completed", "item": {"id": "item_1", "type": "reasoning", "text": "thinking"}},
    {
        "type": "item.completed",
        "item": {
            "id": "item_2",
            "type": "file_change",
            "changes": [{"path": "src/a.py", "kind": "update"}, {"path": "src/b.py", "kind": "add"}],
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_3",
            "type": "mcp_tool_call",
            "server": "docs",
            "tool": "search",
            "arguments": {"q": "dash"},
            "result": {"content": [{"type": "text", "text": "found it"}]},
            "status": "completed",
        },
    },
    {"type": "item.completed", "item": {"id": "item_4", "type": "agent_message", "text": "All done."}},
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 50},
    },
]


class Fake:
    """The fake CLI: where it records, and what it will say."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.script = tmp_path / "fake_codex.py"
        self.script.write_text(FAKE_CODEX, encoding="utf-8")
        self.record = tmp_path / "record.json"
        self.stream = tmp_path / "stream.jsonl"
        monkeypatch.setenv("FAKE_CODEX_RECORD", str(self.record))
        monkeypatch.setenv("FAKE_CODEX_SCRIPT", str(self.stream))
        monkeypatch.delenv("FAKE_CODEX_EXIT", raising=False)
        monkeypatch.delenv("FAKE_CODEX_STDERR", raising=False)
        monkeypatch.setattr(codex, "_launcher", lambda: [sys.executable, str(self.script)])
        monkeypatch.setattr(codex, "_has_credentials", lambda: True)
        self.say(HAPPY_RUN)

    def say(self, frames: list[Any], *, raw: str = "") -> None:
        text = "".join(json.dumps(frame) + "\n" for frame in frames) + raw
        self.stream.write_text(text, encoding="utf-8")

    def recorded(self) -> dict[str, Any]:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def argv(self) -> list[str]:
        return self.recorded()["argv"]

    def config(self, key: str) -> str | None:
        """The TOML value passed as ``-c key=value``, or ``None`` when it was not."""
        argv = self.argv()
        for flag, value in zip(argv, argv[1:]):
            if flag == "-c" and value.startswith(f"{key}="):
                return value[len(key) + 1 :]
        return None


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    return Fake(tmp_path, monkeypatch)


def make_request(tmp_path: Path, **overrides: Any) -> AgentRunRequest:
    member_fields = {
        "team_id": TEAM,
        "name": "Dev",
        "role_title": "Developer",
        "agent_backend": "codex",
        "model": "gpt-6-luna",
        "permission_mode": "acceptEdits",
    }
    member_fields.update(overrides.pop("member", {}))
    fields: dict[str, Any] = dict(
        member=TeamMember(**member_fields),
        task=TeamTask(team_id=TEAM, title="t", brief="b"),
        workdir=tmp_path,
        prompt="the brief",
        system_prompt="You are Dev, the developer.",
    )
    fields.update(overrides)
    return AgentRunRequest(**fields)


async def run(request: AgentRunRequest) -> list[AgentEvent]:
    return [event async for event in codex.CodexBackend().run(request)]


def of_kind(events: list[AgentEvent], kind: str) -> list[AgentEvent]:
    return [event for event in events if event.kind == kind]


# ------------------------------------------------------------------ the stream


async def test_a_run_is_read_as_the_studio_events(fake: Fake, tmp_path: Path) -> None:
    events = await run(make_request(tmp_path))

    calls = of_kind(events, "tool_use")
    assert [call.text for call in calls] == ["Bash", "Edit", "Write", "mcp__docs__search"]
    assert calls[0].data["input"] == {"command": "ls"}
    assert calls[1].data["input"] == {"file_path": "src/a.py"}
    assert calls[3].data["input"] == {"q": "dash"}

    results = of_kind(events, "tool_result")
    assert [result.text for result in results] == ["a.txt\n", "found it"]
    assert results[0].data == {"tool_use_id": "item_0", "is_error": False, "exit_code": 0}

    assert [event.text for event in of_kind(events, "thinking")] == ["thinking"]
    assert [event.text for event in of_kind(events, "text")] == ["All done."]
    assert not of_kind(events, "error")

    # Every event after the thread opened carries its id, which is what a resume needs.
    assert all(event.session_id == "thr_1" for event in events[1:])

    final = events[-1]
    assert final.kind == "result" and final.is_final
    assert final.text == "All done."
    assert final.data["is_error"] is False
    assert final.data["num_turns"] == 1
    assert final.data["total_tokens"] == 1050
    assert final.data["total_cost_usd"] == pytest.approx(0.000107)
    assert final.data["cost_estimated"] is True
    assert final.data["cost_unavailable"] is False
    assert final.data["duration_ms"] > 0
    assert final.data["usage"] == {
        "input_tokens": 800,
        "cache_read_input_tokens": 200,
        "output_tokens": 50,
    }
    # The transcript's token line sums `usage.output_tokens` wherever it finds one.
    turn = next(event for event in events if event.text == "turn.completed")
    assert turn.data["usage"]["output_tokens"] == 50


async def test_repeated_cumulative_usage_is_not_charged_twice(fake: Fake, tmp_path: Path) -> None:
    fake.say([*HAPPY_RUN, HAPPY_RUN[-1]])
    events = await run(make_request(tmp_path))
    final = events[-1].data
    assert final["total_tokens"] == 1050
    assert final["num_turns"] == 2
    assert final["total_cost_usd"] == pytest.approx(0.000107)
    assert final["usage"]["cache_read_input_tokens"] == 200


async def test_unknown_models_are_unpriced_instead_of_claiming_free_usage(
    fake: Fake, tmp_path: Path
) -> None:
    events = await run(make_request(tmp_path, model="gpt-future"))
    assert events[-1].data["cost_unavailable"] is True
    assert events[-1].data["cost_estimated"] is False
    assert events[-1].data["total_tokens"] == 1050


async def test_default_model_cost_uses_the_active_codex_profile(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'model="gpt-6-luna"\nprofile="work"\n[profiles.work]\nmodel="gpt-6.1-sol"\n',
        encoding="utf-8",
    )
    events = await run(make_request(tmp_path, model="sonnet"))
    assert "-m" not in fake.argv()
    assert events[-1].data["model"] == "gpt-6.1-sol"
    assert events[-1].data["total_cost_usd"] == pytest.approx(0.00212)


async def test_a_missing_default_model_keeps_cost_unknown(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = await run(make_request(tmp_path, model="sonnet"))
    assert events[-1].data["cost_unavailable"] is True


async def test_a_failed_turn_preserves_the_usage_already_reported(
    fake: Fake, tmp_path: Path
) -> None:
    fake.say([*HAPPY_RUN, {"type": "turn.failed", "error": {"message": "Limit reached"}}])
    events = await run(make_request(tmp_path))
    assert events[-1].data["is_error"] is True
    assert events[-1].data["total_cost_usd"] == pytest.approx(0.000107)
    assert events[-1].data["duration_ms"] > 0


async def test_a_command_is_one_call_row_even_though_codex_reports_it_twice(
    fake: Fake, tmp_path: Path
) -> None:
    events = await run(make_request(tmp_path))
    bash = [event for event in of_kind(events, "tool_use") if event.text == "Bash"]
    assert len(bash) == 1


async def test_a_failed_turn_is_an_error_with_codex_own_words(fake: Fake, tmp_path: Path) -> None:
    fake.say(
        [
            {"type": "thread.started", "thread_id": "thr_2"},
            {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "You have hit your usage limit."}},
        ]
    )
    events = await run(make_request(tmp_path))
    errors = of_kind(events, "error")
    assert [error.text for error in errors] == ["You have hit your usage limit."]
    assert events[-1].kind == "result" and events[-1].data["is_error"] is True
    assert events[-1].data["errors"] == ["You have hit your usage limit."]


RECONNECTING = {"type": "error", "message": "Reconnecting... 4/5 (request timed out)"}


async def test_reconnecting_is_progress_until_the_cli_finishes(fake: Fake, tmp_path: Path) -> None:
    fake.say([*HAPPY_RUN[:2], RECONNECTING, *HAPPY_RUN[2:]])
    events = await run(make_request(tmp_path))
    assert not of_kind(events, "error")
    assert events[-1].data["is_error"] is False
    assert events[-1].data["errors"] == []
    notice = next(event for event in events if event.text == RECONNECTING["message"])
    assert notice.kind == "system" and notice.data["subtype"] == "reconnecting"
    assert notice.session_id == "thr_1"


@pytest.mark.skipif(os.name != "nt", reason="Windows agent process tree")
async def test_cancelling_codex_also_stops_its_tools(fake, tmp_path):
    fake.script.write_text('''
import json, os, subprocess, sys, time
sys.stdin.read()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open(os.environ['FAKE_CODEX_RECORD'], 'w') as f:
    json.dump({'child': child.pid}, f)
print(json.dumps({'type': 'thread.started', 'thread_id': 'cancel-me'}), flush=True)
time.sleep(120)
''', encoding="utf-8")
    started = asyncio.Event()

    async def consume():
        async for event in codex.CodexBackend().run(make_request(tmp_path)):
            if event.session_id == "cancel-me":
                started.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 10)
        child_pid = fake.recorded()["child"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        with pytest.raises(OSError):
            os.kill(child_pid, 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if fake.record.exists():
            try:
                os.kill(fake.recorded()["child"], 9)
            except OSError:
                pass


@pytest.mark.parametrize("terminal", ["turn.failed", "exit", "eof"])
async def test_exhausted_reconnecting_is_still_an_error(fake, tmp_path, monkeypatch, terminal):
    frames = [*HAPPY_RUN[:2], RECONNECTING]
    if terminal == "turn.failed":
        frames.append({"type": "turn.failed", "error": {"message": "stream disconnected"}})
    elif terminal == "exit":
        monkeypatch.setenv("FAKE_CODEX_EXIT", "1")
    fake.say(frames)
    events = await run(make_request(tmp_path))
    assert len(of_kind(events, "error")) == 1
    assert events[-1].data["is_error"] is True
    assert events[-1].data["errors"]


@pytest.mark.parametrize("target", ["task", "chat"])
@pytest.mark.parametrize("failed", [False, True])
async def test_reconnected_task_and_chat_keep_correct_status(ctx, fake, wait_for, target, failed):
    fake.say([*HAPPY_RUN[:2], RECONNECTING, *(
        [{"type": "turn.failed", "error": {"message": "stream disconnected"}}]
        if failed else HAPPY_RUN[2:]
    )])
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        dev.agent_backend = "codex"
        dev.model = "gpt-6-luna"
        dev.isolate_worktree = False
        session.add(dev)
    if target == "task":
        item = await ctx.teams.create_task(
            team_id=TEAM, assignee_id=DEV, title="Reconnect", brief="Continue", auto_start=False,
        )
        await ctx.teams.update_task(item.id, {"status": TaskStatus.PENDING})
        await ctx.teams.start_task(item.id)
        model = TeamTask
    else:
        item = await ctx.chats.create_chat(DEV)
        await ctx.chats.send(item.id, "Continue", UserContext())
        model = AgentChat
    await wait_for(item.id, timeout=20)
    async with ctx.db.session() as session:
        stored = await session.get(model, item.id)
    assert stored.error == ("stream disconnected" if failed else None)
    if target == "task":
        assert stored.status == (TaskStatus.BLOCKED if failed else TaskStatus.NEEDS_REVIEW)
        async with ctx.db.session() as session:
            messages = (await session.exec(select(AgentMessage).where(AgentMessage.task_id == item.id))).all()
        blocked = [m for m in messages if m.kind == "status" and m.data.get("to") == "blocked"]
        assert len(blocked) == int(failed)


async def test_a_nonzero_exit_reports_stderr(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.say([{"type": "thread.started", "thread_id": "thr_3"}])
    monkeypatch.setenv("FAKE_CODEX_EXIT", "2")
    monkeypatch.setenv("FAKE_CODEX_STDERR", "error: not logged in\n")
    events = await run(make_request(tmp_path))
    errors = of_kind(events, "error")
    assert len(errors) == 1
    assert "code 2" in errors[0].text and "not logged in" in errors[0].text
    assert events[-1].data["is_error"] is True and events[-1].data["exit_code"] == 2


async def test_an_oversized_line_is_skipped_and_the_run_goes_on(fake: Fake, tmp_path: Path) -> None:
    huge = {"type": "item.completed", "item": {"id": "x", "type": "agent_message", "text": "x" * 90_000}}
    fake.say([HAPPY_RUN[0], huge, *HAPPY_RUN[1:]])
    events = await run(make_request(tmp_path, max_message_bytes=70_000))
    skipped = [event for event in events if event.data.get("subtype") == "oversized_line"]
    assert len(skipped) == 1 and skipped[0].data["bytes"] > 70_000
    assert [event.text for event in of_kind(events, "text")] == ["All done."]
    assert events[-1].kind == "result" and events[-1].data["is_error"] is False


async def test_a_non_json_line_on_stdout_is_ignored(fake: Fake, tmp_path: Path) -> None:
    fake.say(HAPPY_RUN, raw="warning: something about the sandbox\n")
    events = await run(make_request(tmp_path))
    assert events[-1].kind == "result" and events[-1].data["is_error"] is False


# ------------------------------------------------------------------ the command


async def test_the_command_line_carries_the_run_settings(fake: Fake, tmp_path: Path) -> None:
    request = make_request(tmp_path, effort="high")
    await run(request)
    recorded = fake.recorded()
    argv = recorded["argv"]

    assert argv[:2] == ["exec", "--json"]
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("--color") + 1] == "never"
    assert argv[-1] == "-", "the prompt is read from stdin"
    assert recorded["stdin"] == "the brief"
    assert Path(recorded["cwd"]).resolve() == tmp_path.resolve()

    assert argv[argv.index("-m") + 1] == "gpt-6-luna"
    assert fake.config("model_reasoning_effort") == '"high"'
    assert fake.config("approval_policy") == '"never"'
    assert fake.config("sandbox_mode") == '"workspace-write"'
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    # The system prompt is layered over Codex's own instructions, as a TOML string.
    assert fake.config("developer_instructions") == json.dumps("You are Dev, the developer.")
    assert "resume" not in argv


@pytest.mark.parametrize(
    ("mode", "sandbox", "bypass"),
    [
        ("plan", '"read-only"', False),
        ("default", '"workspace-write"', False),
        ("dontAsk", '"workspace-write"', False),
        ("bypassPermissions", None, True),
    ],
)
async def test_the_permission_mode_picks_the_sandbox(
    fake: Fake, tmp_path: Path, mode: str, sandbox: str | None, bypass: bool
) -> None:
    await run(make_request(tmp_path, member={"permission_mode": mode}))
    assert fake.config("sandbox_mode") == sandbox
    assert ("--dangerously-bypass-approvals-and-sandbox" in fake.argv()) is bypass


async def test_a_claude_model_name_is_left_to_codex_and_said(fake: Fake, tmp_path: Path) -> None:
    """The roster default is `sonnet`; a member switched to Codex must still run."""
    events = await run(make_request(tmp_path, member={"model": "sonnet"}))
    assert "-m" not in fake.argv()
    note = next(event for event in events if event.data.get("subtype") == "model_fallback")
    assert note.kind == "system" and "sonnet" in note.text and note.data["model"] == "sonnet"


@pytest.mark.parametrize("resume", [None, "thr_9"])
async def test_the_project_is_an_explicit_write_root_even_below_cwd(
    fake: Fake, tmp_path: Path, resume: str | None
) -> None:
    import tomllib

    project = tmp_path / "game project"
    project.mkdir()
    await run(make_request(tmp_path, writable_roots=[project], resume_session_id=resume))
    value = fake.config("sandbox_workspace_write.writable_roots")
    assert value is not None
    assert tomllib.loads(f"roots={value}")["roots"] == [str(project.resolve())]
    assert Path(fake.recorded()["cwd"]).resolve() == tmp_path.resolve()


@pytest.mark.parametrize("mode", ["plan", "bypassPermissions"])
async def test_explicit_roots_do_not_change_read_only_or_bypass_modes(
    fake: Fake, tmp_path: Path, mode: str
) -> None:
    await run(make_request(tmp_path, writable_roots=[tmp_path], member={"permission_mode": mode}))
    assert fake.config("sandbox_workspace_write.writable_roots") is None


async def test_no_effort_sends_no_effort(fake: Fake, tmp_path: Path) -> None:
    await run(make_request(tmp_path, effort=None, member={"effort": None}))
    assert fake.config("model_reasoning_effort") is None
    assert fake.config("features.multi_agent_v2.multi_agent_mode_hint_text") is None


@pytest.mark.parametrize(("resume", "effort"), [(None, "low"), ("thr_9", None)])
async def test_proactive_delegation_is_independent_of_effort(
    fake: Fake, tmp_path: Path, resume: str | None, effort: str | None,
) -> None:
    await run(make_request(tmp_path, resume_session_id=resume, effort=effort,
                           member={"effort": None}, proactive_subagents=True))
    assert fake.config("features.multi_agent") == "true"
    hint = json.loads(fake.config("features.multi_agent_v2.multi_agent_mode_hint_text"))
    assert "on your own initiative" in hint and "spawn_subagent" in hint
    assert fake.config("model_reasoning_effort") == (json.dumps(effort) if effort else None)


async def test_project_mcp_servers_become_overrides_and_unknown_configs_are_reported(
    fake: Fake, tmp_path: Path
) -> None:
    class SdkServer:  # the Agent SDK's in-process server object: not a dict, no process
        pass

    events = await run(
        make_request(
            tmp_path,
            mcp_servers={
                "docs": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "docs-mcp"],
                    "env": {"DOCS_ROOT": "C:\\docs"},
                },
                "remote": {"type": "http", "url": "https://example.test/mcp", "headers": {"X-Key": "k"}},
                "voxarium_studio": SdkServer(),
            },
        )
    )
    assert fake.config("mcp_servers.docs.command") == '"npx"'
    assert fake.config("mcp_servers.docs.args") == '["-y", "docs-mcp"]'
    assert fake.config("mcp_servers.docs.env") == '{DOCS_ROOT = "C:\\\\docs"}'
    assert fake.config("mcp_servers.remote.url") == '"https://example.test/mcp"'
    assert fake.config("mcp_servers.remote.http_headers") == '{X-Key = "k"}'
    assert fake.config("mcp_servers.voxarium_studio.command") is None
    assert fake.config("mcp_servers.docs.default_tools_approval_mode") is None

    note = next(event for event in events if event.data.get("subtype") == "studio_tools_unavailable")
    assert note.data["servers"] == ["voxarium_studio"] and "voxarium_studio" in note.text


@pytest.mark.parametrize("resume", [None, "thr_9"])
async def test_full_access_approves_project_mcp_writes_on_start_and_resume(
    fake: Fake, tmp_path: Path, resume: str | None
) -> None:
    await run(
        make_request(
            tmp_path,
            member={"permission_mode": "bypassPermissions"},
            resume_session_id=resume,
            mcp_servers={
                "voxel editor": {"command": "voxarium-mcp"},
                "remote": {"url": "https://example.test/mcp"},
                "voxarium_studio": object(),
            },
        )
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in fake.argv()
    assert fake.config("approval_policy") == '"never"'
    assert fake.config('mcp_servers."voxel editor".default_tools_approval_mode') == '"approve"'
    assert fake.config("mcp_servers.remote.default_tools_approval_mode") == '"approve"'
    assert fake.config("mcp_servers.voxarium_studio.default_tools_approval_mode") is None


@pytest.mark.parametrize("resume", [None, "thr_9"])
async def test_pictures_ride_the_image_flag(
    fake: Fake, tmp_path: Path, resume: str | None
) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG")
    await run(
        make_request(
            tmp_path, images=[{"path": str(shot), "media_type": "image/png"}], resume_session_id=resume
        )
    )
    argv = fake.argv()
    assert argv[argv.index("-i") + 1] == str(shot)


async def test_a_resume_opens_the_same_thread(fake: Fake, tmp_path: Path) -> None:
    await run(make_request(tmp_path, resume_session_id="thr_9", prompt="carry on"))
    recorded = fake.recorded()
    argv = recorded["argv"]
    assert argv[:3] == ["exec", "resume", "--json"]
    assert "--color" not in argv, "exec resume does not accept exec's color option"
    assert argv[-2:] == ["thr_9", "-"]
    assert recorded["stdin"] == "carry on"
    # The options a resume shares with a first run are still there.
    assert fake.config("developer_instructions") is not None


@pytest.mark.parametrize("error_format", ["stderr", "json"])
async def test_missing_resume_recovers_once_with_saved_context(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_format: str
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MISSING_RESUME", error_format)
    request = make_request(
        tmp_path, resume_session_id="lost-thread", prompt="continue",
        recovery_prompt="Original brief. Previous work. Latest review. Continue.",
        images=[{"path": str(tmp_path / "review.png")}], effort="high",
    )
    events = await run(request)
    assert not of_kind(events, "error")
    assert len(of_kind(events, "result")) == 1
    assert events[-1].session_id == "thr_1" and not events[-1].data["is_error"]
    assert events[-1].data["num_turns"] == 1
    assert len([e for e in events if e.data.get("subtype") == "session_recovered"]) == 1
    history = [json.loads(line) for line in Path(str(fake.record) + ".history").read_text().splitlines()]
    assert len(history) == 2
    assert "resume" in history[0]["argv"] and "resume" not in history[1]["argv"]
    assert history[0]["stdin"] == "continue" and history[1]["stdin"] == request.recovery_prompt
    assert "-i" in history[1]["argv"] and fake.config("model_reasoning_effort") == '"high"'
    assert request.resume_session_id == "lost-thread", "the caller's request is not mutated"


@pytest.mark.parametrize("started", [False, True])
async def test_resume_does_not_retry_other_failures_or_a_turn_that_started(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: bool
) -> None:
    message = "no rollout found for thread id lost-thread" if started else "authentication failed"
    fake.say([{"type": "turn.started"}] if started else [])
    monkeypatch.setenv("FAKE_CODEX_STDERR", message)
    monkeypatch.setenv("FAKE_CODEX_EXIT", "1")
    events = await run(make_request(tmp_path, resume_session_id="lost-thread"))
    assert of_kind(events, "error") and events[-1].data["is_error"]
    assert not [e for e in events if e.data.get("subtype") == "session_recovered"]
    assert len(Path(str(fake.record) + ".history").read_text().splitlines()) == 1


async def test_recovery_failure_is_terminal(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MISSING_RESUME", "stderr")
    monkeypatch.setenv("FAKE_CODEX_EXIT", "1")
    monkeypatch.setenv("FAKE_CODEX_STDERR", "connection refused")
    fake.say([])
    events = await run(make_request(tmp_path, resume_session_id="lost-thread"))
    assert len(of_kind(events, "error")) == 1
    assert "connection refused" in of_kind(events, "error")[0].text
    assert events[-1].data["is_error"]
    assert len(Path(str(fake.record) + ".history").read_text().splitlines()) == 2


@pytest.mark.parametrize("error_format", ["stderr", "json"])
async def test_busy_resume_retries_same_request_without_false_errors(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_format: str
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BUSY_ATTEMPTS", "2")
    monkeypatch.setenv("FAKE_CODEX_BUSY_FORMAT", error_format)
    monkeypatch.setattr(codex, "_WRITER_RETRY_DELAYS", (0, 0, 0))
    fake.say([{**HAPPY_RUN[0], "thread_id": "busy-thread"}, *HAPPY_RUN[1:]])
    request = make_request(tmp_path, resume_session_id="busy-thread", prompt="continue",
                           recovery_prompt="Do not start over.", effort="high",
                           images=[{"path": str(tmp_path / "review.png")}])
    events = await run(request)
    assert not of_kind(events, "error")
    assert len(of_kind(events, "result")) == 1
    assert not events[-1].data["is_error"] and events[-1].session_id == "busy-thread"
    assert events[-1].data["num_turns"] == 1
    waits = [e for e in events if e.data.get("subtype") == "session_waiting"]
    assert [e.data["retry"] for e in waits] == [1, 2]
    assert not [e for e in events if e.data.get("subtype") == "session_recovered"]
    history = [json.loads(line) for line in Path(str(fake.record) + ".history").read_text().splitlines()]
    assert len(history) == 3 and all(entry == history[0] for entry in history)
    assert "resume" in history[0]["argv"] and history[0]["stdin"] == "continue"


async def test_busy_resume_stops_after_bounded_retries_and_preserves_session(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BUSY_ATTEMPTS", "99")
    monkeypatch.setattr(codex, "_WRITER_RETRY_DELAYS", (0, 0, 0))
    events = await run(make_request(tmp_path, resume_session_id="busy-thread"))
    errors = of_kind(events, "error")
    assert len(errors) == 1 and errors[0].data["subtype"] == "session_busy"
    assert "already has an active writer" in errors[0].text
    assert "Wait for that run to finish" in errors[0].text
    assert len(of_kind(events, "result")) == 1 and events[-1].data["is_error"]
    assert "saved session has been preserved" in events[-1].text
    assert events[-1].session_id == "busy-thread"
    history = [json.loads(line) for line in Path(str(fake.record) + ".history").read_text().splitlines()]
    assert len(history) == 4 and all("resume" in entry["argv"] for entry in history)


@pytest.mark.parametrize("progress", [
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "msg", "type": "agent_message", "text": "working"}},
    HAPPY_RUN[2],
])
async def test_writer_conflict_after_work_started_is_never_retried(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, progress: dict
) -> None:
    fake.say([progress])
    monkeypatch.setenv("FAKE_CODEX_STDERR", "thread busy-thread already has an active writer")
    monkeypatch.setenv("FAKE_CODEX_EXIT", "1")
    events = await run(make_request(tmp_path, resume_session_id="busy-thread"))
    assert of_kind(events, "error") and events[-1].data["is_error"]
    assert not [e for e in events if e.data.get("subtype") == "session_waiting"]
    assert len(Path(str(fake.record) + ".history").read_text().splitlines()) == 1


async def test_writer_conflict_for_another_thread_is_not_retried(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.say([])
    monkeypatch.setenv("FAKE_CODEX_STDERR", "thread other-thread already has an active writer")
    monkeypatch.setenv("FAKE_CODEX_EXIT", "1")
    events = await run(make_request(tmp_path, resume_session_id="busy-thread"))
    assert of_kind(events, "error")
    assert len(Path(str(fake.record) + ".history").read_text().splitlines()) == 1


async def test_busy_resume_wait_can_be_cancelled_without_starting_again(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BUSY_ATTEMPTS", "99")
    monkeypatch.setattr(codex, "_WRITER_RETRY_DELAYS", (60,))
    waiting = asyncio.Event()

    async def consume():
        async for event in codex.CodexBackend().run(make_request(tmp_path, resume_session_id="busy-thread")):
            if event.data.get("subtype") == "session_waiting":
                waiting.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(waiting.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(Path(str(fake.record) + ".history").read_text().splitlines()) == 1


async def test_missing_session_recovery_still_works_after_a_writer_conflict(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BUSY_ATTEMPTS", "1")
    monkeypatch.setenv("FAKE_CODEX_MISSING_RESUME", "stderr")
    monkeypatch.setattr(codex, "_WRITER_RETRY_DELAYS", (0,))
    events = await run(make_request(tmp_path, resume_session_id="lost-thread"))
    assert not of_kind(events, "error") and not events[-1].data["is_error"]
    assert len([e for e in events if e.data.get("subtype") == "session_recovered"]) == 1
    assert len(Path(str(fake.record) + ".history").read_text().splitlines()) == 3


async def test_a_system_prompt_too_long_for_the_command_line_rides_the_message(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex, "_MAX_COMMAND_LINE", 400)
    long_prompt = "You are Dev. " * 60
    events = await run(make_request(tmp_path, system_prompt=long_prompt))
    assert fake.config("developer_instructions") is None
    stdin = fake.recorded()["stdin"]
    assert stdin.startswith("<instructions>\n" + long_prompt.strip())
    assert stdin.endswith("</instructions>\n\nthe brief")
    assert any(event.data.get("subtype") == "instructions_in_prompt" for event in events)


# ------------------------------------------------------------------ availability


def test_native_windows_install_is_available_with_a_stale_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app_data = tmp_path / "Local App Data"
    native = local_app_data / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    monkeypatch.setattr(codex, "_has_credentials", lambda: True)

    assert codex._launcher() == [str(native)]
    assert codex.CodexBackend().is_available() == (True, "Ready")

    # An explicitly selected PATH installation still takes precedence.
    selected = tmp_path / "custom" / "codex.exe"
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(selected))
    assert codex._launcher() == [str(selected)]


@pytest.mark.parametrize("entry", ["missing", "directory"])
def test_windows_install_fallback_requires_an_executable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    native = tmp_path / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    if entry == "directory":
        native.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    assert codex._launcher() is None


@pytest.mark.parametrize("local_app_data", [None, ""])
def test_windows_install_fallback_without_local_app_data_does_not_search_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_app_data: str | None
) -> None:
    native = tmp_path / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")
    monkeypatch.chdir(tmp_path)
    if local_app_data is None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    else:
        monkeypatch.setenv("LOCALAPPDATA", local_app_data)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    assert codex._launcher() is None


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_do_not_use_a_windows_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    native = tmp_path / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    assert codex._launcher() is None


def test_is_available_names_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = codex.CodexBackend()
    monkeypatch.setattr(codex, "_launcher", lambda: None)
    usable, reason = backend.is_available()
    assert not usable and "codex" in reason and "npm install" in reason

    monkeypatch.setattr(codex, "_launcher", lambda: ["codex"])
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    usable, reason = backend.is_available()
    assert not usable and "codex login" in reason

    (home / "auth.json").write_text("{}", encoding="utf-8")
    assert backend.is_available() == (True, "Ready")

    (home / "auth.json").unlink()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert backend.is_available() == (True, "Ready")


async def test_an_unavailable_backend_fails_the_run_before_starting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex, "_launcher", lambda: None)
    events = await run(make_request(tmp_path))
    assert len(events) == 1 and events[0].kind == "error" and events[0].is_final


def test_suggested_models_come_from_the_codex_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert codex.suggested_models() == [], "no cache, no suggestions — the field still takes a name"

    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide", "priority": 1},
                    {
                        "slug": "gpt-5.6-terra",
                        "display_name": "GPT-5.6-Terra",
                        "description": "Balanced",
                        "visibility": "list",
                        "priority": 8,
                    },
                    {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list", "priority": 5},
                ]
            }
        ),
        encoding="utf-8",
    )
    models = codex.suggested_models()
    assert [model["id"] for model in models] == ["gpt-6-luna", "gpt-5.6-terra"]
    assert models[1] == {
        "id": "gpt-5.6-terra",
        "label": "GPT-5.6-Terra",
        "group": "Codex",
        "note": "Balanced",
        "backend": "codex",
    }


def test_the_launcher_prefers_the_native_binary_over_the_npm_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "codex.cmd"
    shim.write_text("@echo off\r\n", encoding="ascii")
    native = (
        tmp_path / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
        / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(shim))
    assert codex._launcher() == [str(native)]

    monkeypatch.setattr(codex.shutil, "which", lambda name: str(tmp_path / "codex"))
    assert codex._launcher() == [str(tmp_path / "codex")]


# ------------------------------------------------------------------ through the Studio


def test_member_options_list_codex_beside_claude_code(client) -> None:
    body = client.get("/api/teams/member-options").json()
    assert body["agent_backends"]["codex"]["named_agents"] is False
    assert body["agent_backends"]["claude_code"]["named_agents"] is True
    assert all(entry.get("backend") for entry in body["suggested_models"])
    assert {entry["backend"] for entry in body["suggested_models"]} >= {"claude_code"}


async def test_a_codex_member_runs_a_task_with_its_identity_in_the_prompt(
    ctx: AppContext, fake: Fake, wait_for
) -> None:
    """The run goes through the board like any other, and nothing Claude-specific leaks in:
    no `--agent` line in the transcript, and the member's role in the system prompt."""
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        dev.agent_backend = "codex"
        dev.model = "gpt-6-luna"
        dev.isolate_worktree = False
        session.add(dev)
        for member in (await session.exec(select(TeamMember))).all():
            if member.id != DEV and member.kind == "agent":
                member.agent_backend = "codex"
                session.add(member)

    task = await ctx.teams.create_task(
        team_id=TEAM,
        assignee_id=DEV,
        title="Tune the dash",
        brief="Lower the cooldown.",
        auto_start=True,
    )
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await wait_for(task.id, timeout=20)

    async with ctx.db.session() as session:
        stored = await session.get(TeamTask, task.id)
    assert stored.status == TaskStatus.NEEDS_REVIEW
    assert stored.result_summary == "All done."
    assert stored.agent_session_id == "thr_1"
    assert stored.total_tokens == 1050
    assert stored.num_turns == 1
    assert stored.total_cost_usd == pytest.approx(0.000107)
    assert stored.started_at is not None and stored.finished_at >= stored.started_at

    async with ctx.db.session() as session:
        messages = list(
            (await session.exec(select(AgentMessage).where(AgentMessage.task_id == task.id))).all()
        )
    assert not [m for m in messages if m.kind == "agent"], "no `--agent` line: Codex has none"
    kinds = [m.kind for m in messages]
    assert "tool_use" in kinds and "text" in kinds and "result" in kinds

    instructions = fake.config("developer_instructions")
    assert instructions is not None
    prompt = json.loads(instructions)
    assert dev.name in prompt and "Lower the cooldown." in fake.recorded()["stdin"]


@pytest.mark.parametrize("resume", [None, "thr_9"])
async def test_real_studio_server_is_given_to_codex_as_stdio(ctx, fake, resume):
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
    toolset = build_toolset(ctx.teams, member=dev)
    if toolset is None:
        pytest.skip("claude-agent-sdk is not installed")
    events = await run(make_request(
        ctx.paths.root, mcp_servers=toolset.mcp_servers, resume_session_id=resume,
        member={"permission_mode": "bypassPermissions"},
    ))
    assert not any(e.data.get("subtype") == "studio_tools_unavailable" for e in events)
    assert json.loads(fake.config("mcp_servers.voxarium_studio.command")) == sys.executable
    assert "mcp_proxy.py" in fake.config("mcp_servers.voxarium_studio.args")
    assert fake.config("mcp_servers.voxarium_studio.default_tools_approval_mode") == '"approve"'


async def test_task_recovery_keeps_brief_review_notes_and_stores_new_session(
    ctx, fake, wait_for, monkeypatch
):
    monkeypatch.setenv("FAKE_CODEX_MISSING_RESUME", "stderr")
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        dev.agent_backend = "codex"
        dev.model = "gpt-6-luna"
        dev.isolate_worktree = False
        session.add(dev)
    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="Recover task", brief="Original requirements",
        auto_start=False,
    )
    async with ctx.db.session() as session:
        stored = await session.get(TeamTask, task.id)
        stored.agent_session_id = "old-claude-session"
        stored.result_summary = "Already created the model"
        stored.review_comment = "Fix the dimensions"
        session.add(stored)
    await ctx.teams.add_note(task.id, "Keep the saved colours")
    await ctx.teams.update_task(task.id, {"status": TaskStatus.PENDING})
    await ctx.teams.start_task(task.id)
    await wait_for(task.id, timeout=20)
    async with ctx.db.session() as session:
        stored = await session.get(TeamTask, task.id)
    assert stored.status == TaskStatus.NEEDS_REVIEW and stored.error is None
    assert stored.agent_session_id == "thr_1" and stored.num_turns == 1
    for text in ["Original requirements", "Already created the model", "Fix the dimensions", "Keep the saved colours"]:
        assert text in fake.recorded()["stdin"]


async def test_chat_recovery_keeps_conversation_and_stores_new_session(ctx, fake, wait_for, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MISSING_RESUME", "json")
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        dev.agent_backend = "codex"
        dev.model = "gpt-6-luna"
        session.add(dev)
    chat = await ctx.chats.create_chat(DEV)
    await ctx.chats.record(chat.id, DEV, role="user", kind="text", text="Make the model blue")
    await ctx.chats.record(chat.id, DEV, role="assistant", kind="text", text="Saved in model.vox")
    async with ctx.db.session() as session:
        stored = await session.get(AgentChat, chat.id)
        stored.agent_session_id = "lost-chat"
        session.add(stored)
    await ctx.chats.send(chat.id, "Continue with its icon", UserContext())
    await wait_for(chat.id, timeout=20)
    async with ctx.db.session() as session:
        stored = await session.get(AgentChat, chat.id)
    assert stored.agent_session_id == "thr_1" and stored.error is None
    assert stored.num_turns == 1
    for text in ["Make the model blue", "Saved in model.vox", "Continue with its icon"]:
        assert text in fake.recorded()["stdin"]


async def test_get_backend_knows_codex() -> None:
    backend = get_backend("codex")
    assert backend.name == "codex" and backend.named_agents is False
    assert getattr(get_backend("claude_code"), "named_agents", True) is True


@pytest.mark.parametrize("busy_attempts", [1, 99])
@pytest.mark.parametrize("target", ["chat", "task"])
async def test_writer_conflict_is_handled_in_chat_and_task(
    ctx, fake, wait_for, monkeypatch, busy_attempts, target
):
    monkeypatch.setenv("FAKE_CODEX_BUSY_ATTEMPTS", str(busy_attempts))
    monkeypatch.setattr(codex, "_WRITER_RETRY_DELAYS", (0, 0, 0))
    fake.say([{**HAPPY_RUN[0], "thread_id": "busy-thread"}, *HAPPY_RUN[1:]])
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        dev.agent_backend = "codex"
        dev.model = "gpt-6-luna"
        dev.isolate_worktree = False
        session.add(dev)
    if target == "chat":
        item = await ctx.chats.create_chat(DEV)
        model = AgentChat
    else:
        item = await ctx.teams.create_task(
            team_id=TEAM, assignee_id=DEV, title="Resume task", brief="Continue saved work",
            auto_start=False,
        )
        model = TeamTask
    async with ctx.db.session() as session:
        stored = await session.get(model, item.id)
        stored.agent_session_id = "busy-thread"
        session.add(stored)
    if target == "chat":
        await ctx.chats.send(item.id, "Continue", UserContext())
    else:
        await ctx.teams.update_task(item.id, {"status": TaskStatus.PENDING})
        await ctx.teams.start_task(item.id)
    await wait_for(item.id, timeout=20)
    async with ctx.db.session() as session:
        stored = await session.get(model, item.id)
    assert stored.agent_session_id == "busy-thread"
    if busy_attempts == 1:
        assert stored.error is None and stored.num_turns == 1
        if target == "task":
            assert stored.status == TaskStatus.NEEDS_REVIEW
    else:
        assert "Wait for that run to finish" in stored.error and stored.num_turns == 0
        if target == "task":
            assert stored.status == TaskStatus.BLOCKED
