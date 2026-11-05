"""Qwen Code headless CLI backend (§10.6), protocol checked against 0.26.3.

Each task/chat turn is a subprocess. Auth and provider configuration belong to Qwen;
Studio supplies the role, prompt, permissions and its existing MCP bridge per run.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..changes import edit_stat
from .base import AgentBackend, AgentEvent, AgentRunRequest, register_backend
from .cli_stream import _Oversized, _drain, _frames
from .mcp_bridge import bridge_servers
from .recovery import recovery_prompt

_FOREIGN_MODEL = re.compile(r"^(claude-|opus|sonnet|haiku|fable|gpt-|o[134](?:-|$))", re.I)
_MODES = {
    "plan": "plan", "default": "default", "dontAsk": "default",
    "acceptEdits": "auto-edit", "auto": "auto", "bypassPermissions": "yolo",
}
_TOOLS = {
    "read_file": "Read", "write_file": "Write", "edit": "Edit",
    "run_shell_command": "Bash", "grep_search": "Grep", "glob": "Glob",
    "web_search": "WebSearch", "web_fetch": "WebFetch", "todo_write": "TodoWrite",
    "agent": "Task",
}
_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
)


def _normalise_usage(value: Any) -> dict[str, int]:
    """Qwen's input includes cache reads; Studio accounts for them separately (§10.6)."""
    if not isinstance(value, dict):
        return {}
    usage = {
        key: value[key] for key in _TOKEN_FIELDS
        if isinstance(value.get(key), int) and not isinstance(value[key], bool) and value[key] >= 0
    }
    if "input_tokens" in usage:
        usage["input_tokens"] = max(
            0, usage["input_tokens"] - usage.get("cache_read_input_tokens", 0)
        )
    return usage


def _launcher() -> list[str] | None:
    found = shutil.which("qwen")
    if not found:
        return None
    path = Path(found)
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        # Avoid cmd.exe reinterpreting quotes, Unicode and shell metacharacters.
        package = path.parent / "node_modules" / "@qwen-code" / "qwen-code"
        try:
            metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
            entry = metadata["bin"]
            entry = entry["qwen"] if isinstance(entry, dict) else entry
            script = (package / entry).resolve()
        except (OSError, ValueError, KeyError, TypeError):
            return None
        node = shutil.which("node")
        if node and script.is_file() and script.is_relative_to(package.resolve()):
            return [node, str(script)]
        return None
    return [str(path)]


def suggested_models() -> list[dict[str, str]]:
    # Empty model selects Qwen's configured provider/model, without pinning a release.
    return [{"id": "", "label": "Qwen Code default", "group": "Qwen Code",
             "note": "Uses Qwen Code's configured model", "backend": "qwen"}]


def _command(launcher: list[str], request: AgentRunRequest, config: Path
             ) -> tuple[list[str], str, list[AgentEvent]]:
    argv = [*launcher, "--output-format", "stream-json", "--input-format", "text",
            "--chat-recording", "--approval-mode",
            _MODES.get(request.member.permission_mode, "default"), "--mcp-config", str(config)]
    if request.strict_mcp_config:
        # An empty array explicitly disables all implicit servers; same-name configs
        # supplied by --mcp-config take precedence over Qwen's discovered configs.
        argv += ["--allowed-mcp-server-names", *request.mcp_servers.keys()]
    if request.resume_session_id:
        argv += ["--resume", request.resume_session_id]
    for root in request.writable_roots:
        argv += ["--include-directories", str(root.resolve())]
    allowed = _allowed_tools([*request.member.allowed_tools, *request.extra_allowed_tools])
    if allowed and request.member.permission_mode != "plan":
        argv += ["--allowed-tools", *allowed]
    notes = []
    model = request.model.strip()
    if model and _FOREIGN_MODEL.match(model):
        notes.append(AgentEvent(kind="system", text=f"The model {model} belongs to another "
                               "backend; using Qwen Code's configured model.",
                               data={"subtype": "model_fallback", "model": model}))
    elif model:
        argv += ["--model", model]
    if request.effort or request.member.max_turns:
        notes.append(AgentEvent(kind="system", text="Qwen Code uses its own reasoning and "
                               "turn limits; Studio effort and Max turns do not apply.",
                               data={"subtype": "unsupported_settings"}))
    prompt = request.prompt
    if request.images:
        prompt += "\n\nAttached image files:\n" + "\n".join(
            str(image["path"]) for image in request.images
        )
    if request.system_prompt:
        candidate = [*argv, "--append-system-prompt", request.system_prompt]
        if len(subprocess.list2cmdline(candidate).encode("utf-16-le")) // 2 < 30_000:
            argv = candidate
        else:
            prompt = f"Studio instructions:\n{request.system_prompt}\n\nUser message:\n{prompt}"
            notes.append(AgentEvent(kind="system", text="The Studio instructions exceed "
                                   "the command-line limit and were included in the prompt.",
                                   data={"subtype": "system_prompt_in_message"}))
    return argv, prompt, notes


def _allowed_tools(names: list[str]) -> list[str]:
    reverse = {value: key for key, value in _TOOLS.items()}
    result = []
    for name in names:
        tool, separator, rule = name.partition("(")
        result.append(reverse.get(tool, tool) + separator + rule)
    return result


class QwenBackend(AgentBackend):
    name = "qwen"
    named_agents = False
    session_recovery = True

    def is_available(self) -> tuple[bool, str]:
        if _launcher() is None:
            return False, ("The `qwen` CLI was not found. Install it with "
                           "`npm install -g @qwen-code/qwen-code`, then run `qwen` "
                           "once to configure authentication.")
        # Qwen supports OAuth, several API providers and project .env files. Only its
        # runtime can validate those without falsely rejecting a configured provider.
        return True, "Qwen Code installed; authentication is checked when a run starts."

    async def run(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]:
        available, reason = self.is_available()
        if not available:
            yield AgentEvent(kind="error", text=reason, is_final=True)
            return
        try:
            async with bridge_servers(request.mcp_servers, request.max_message_bytes) as servers:
                current = replace(request, mcp_servers=servers)
                for attempt in range(2):
                    pending: list[AgentEvent] = []
                    retryable = bool(current.resume_session_id)
                    async for event in self._run_once(current):
                        if event.kind in {"text", "thinking", "tool_use", "tool_result"}:
                            retryable = False
                            for previous in pending:
                                yield previous
                            pending.clear()
                        if retryable and event.kind in {"error", "result"}:
                            pending.append(event)
                        else:
                            yield event
                    missing = any(
                        any(marker in event.text for marker in (
                            f"No saved session found with ID {current.resume_session_id}.",
                            f'No saved session found with title "{current.resume_session_id}".',
                        ))
                        for event in pending if event.kind == "error"
                    )
                    if not attempt and retryable and missing:
                        yield AgentEvent(kind="system", text="The saved Qwen session is "
                                         "unavailable. Starting a new session with saved context.",
                                         data={"subtype": "session_recovered",
                                               "previous_session_id": current.resume_session_id})
                        current = replace(current, resume_session_id=None,
                                          prompt=request.recovery_prompt or recovery_prompt(
                                              request.prompt, [], request.task))
                        continue
                    for event in pending:
                        yield event
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield AgentEvent(kind="error", text=f"{type(exc).__name__}: {exc}", is_final=True)

    async def _run_once(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]:
        state = _RunState(request.resume_session_id)
        started = time.monotonic()
        process = None
        stderr_task = None
        writer_task = None
        tail: deque[str] = deque(maxlen=4000)
        # Ephemeral config avoids command-line limits and does not modify the user's
        # .qwen/settings.json or managed project's .mcp.json (§10.6, §10.7).
        with tempfile.TemporaryDirectory(prefix="voxarium_studio-qwen-") as temporary:
            config = Path(temporary) / "mcp.json"
            config.write_text(json.dumps({"mcpServers": _mcp_servers(request.mcp_servers)}),
                              encoding="utf-8")
            argv, prompt, notes = _command(_launcher() or [], request, config)
            for note in notes:
                yield note
            env = dict(os.environ)
            env.pop("CLAUDECODE", None)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(request.workdir), env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=max(65536, request.max_message_bytes),
                    start_new_session=os.name != "nt",
                )
                assert process.stdin and process.stdout and process.stderr
                stderr_task = asyncio.create_task(_drain(process.stderr, tail))
                # Read stdout while feeding long prompts so neither pipe can deadlock.
                writer_task = asyncio.create_task(_write_prompt(process.stdin, prompt))
                async for frame in _frames(process.stdout, request.max_message_bytes):
                    if isinstance(frame, _Oversized):
                        yield AgentEvent(kind="system", text=f"Skipped a {frame.size}-byte "
                                         "Qwen message exceeding teams.max_message_bytes.",
                                         data={"subtype": "oversized_line", "bytes": frame.size})
                    else:
                        for event in state.events(frame):
                            yield event
                exit_code = await process.wait()
                await stderr_task
                await writer_task
                if exit_code or state.result is None:
                    reason = (f"qwen exited with code {exit_code}" if exit_code else
                              "Qwen Code ended without a result event")
                    stderr = "".join(tail).strip()
                    state.errors.append(reason + (f": {stderr}" if stderr else ""))
                    yield AgentEvent(kind="error", text=state.errors[-1],
                                     session_id=state.session_id)
                yield state.finish(exit_code, round((time.monotonic() - started) * 1000))
            finally:
                if process is not None and process.returncode is None:
                    await _kill_tree(process)
                pending = [task for task in (stderr_task, writer_task) if task is not None]
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)


def _mcp_servers(servers: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for name, server in servers.items():
        config = dict(server)
        kind = config.pop("type", None)
        if kind == "http" and "url" in config:
            config["httpUrl"] = config.pop("url")
        result[name] = config
    return result


async def _write_prompt(writer: asyncio.StreamWriter, prompt: str) -> None:
    try:
        writer.write(prompt.encode("utf-8"))
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # Early CLI failures are reported by stderr/the exit code.
    finally:
        writer.close()


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        return _text(value.get("text", value.get("message", value.get("content", ""))))
    return str(value or "")


class _RunState:
    def __init__(self, session_id: str | None) -> None:
        self.session_id = session_id
        self.result: dict[str, Any] | None = None
        self.last_text = ""
        self.errors: list[str] = []

    def events(self, frame: dict[str, Any]) -> list[AgentEvent]:
        if frame.get("session_id"):
            self.session_id = str(frame["session_id"])
        kind = frame.get("type")
        events = []
        if kind == "system":
            events.append(AgentEvent(kind="system", data=frame))
        elif kind in {"assistant", "user"}:
            message = frame.get("message") or {}
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and kind == "assistant":
                    text = _text(block.get("text"))
                    if not frame.get("parent_tool_use_id"):
                        self.last_text = text
                    events.append(AgentEvent(kind="text", text=text))
                elif block_type == "thinking":
                    events.append(AgentEvent(kind="thinking", text=_text(block.get("thinking"))))
                elif block_type == "tool_use":
                    name = str(block.get("name", ""))
                    args = dict(block.get("input") or {})
                    if "file_path" not in args and "absolute_path" in args:
                        args["file_path"] = args["absolute_path"]
                    name = _TOOLS.get(name, name)
                    data = {"id": block.get("id"), "name": name,
                            "input": {key: (value if isinstance(value, str) else
                                            json.dumps(value, ensure_ascii=False))[:500]
                                      for key, value in args.items()}}
                    stat = edit_stat(name, args)
                    if stat is not None:
                        data["edit"] = stat
                    events.append(AgentEvent(kind="tool_use", text=name, data=data))
                elif block_type == "tool_result":
                    events.append(AgentEvent(kind="tool_result", text=_text(block.get("content"))[:2000],
                                             data={"tool_use_id": block.get("tool_use_id"),
                                                   "is_error": bool(block.get("is_error"))}))
            if kind == "assistant" and not frame.get("parent_tool_use_id"):
                # §10.4: one API response can yield several transcript entries. Attach
                # its accounting once so the shared live counter does not multiply it.
                # Subagent usage is reconciled by the root result, not counted again.
                usage = _normalise_usage(message.get("usage"))
                # Qwen starts each intermediate block with an all-zero placeholder;
                # it is not a new measurement of an empty context window.
                if any(usage.values()) and events:
                    events[0].data["usage"] = usage
                    if message.get("id"):
                        events[0].data["provider_response_id"] = str(message["id"])
        elif kind == "result" and not frame.get("parent_tool_use_id"):
            self.result = frame
            if frame.get("is_error"):
                self.errors.append(_text(frame.get("error")) or "Qwen Code reported an error")
                events.append(AgentEvent(kind="error", text=self.errors[-1]))
        elif kind == "error":
            self.errors.append(_text(frame.get("error", frame.get("message"))) or "Qwen Code error")
            events.append(AgentEvent(kind="error", text=self.errors[-1]))
        for event in events:
            event.session_id = self.session_id
            if frame.get("parent_tool_use_id"):
                event.data["parent_tool_use_id"] = frame["parent_tool_use_id"]
        return events

    def finish(self, exit_code: int, duration_ms: int) -> AgentEvent:
        result = self.result or {}
        usage = _normalise_usage(result.get("usage"))
        failed = bool(self.errors or result.get("is_error") or exit_code)
        return AgentEvent(kind="result", text=_text(result.get("result")) or self.last_text,
                          session_id=self.session_id, is_final=True, data={
                              "subtype": "error" if failed else "success", "is_error": failed,
                              "num_turns": result.get("num_turns", 0), "usage": usage,
                              "total_tokens": sum(usage.values()),
                              "total_cached_tokens": usage.get("cache_read_input_tokens", 0),
                              "total_uncached_tokens": sum(value for key, value in usage.items()
                                                           if key != "cache_read_input_tokens"),
                              "accounting_version": 2,
                              "total_cost_usd": 0.0, "cost_unavailable": True,
                              "duration_ms": duration_ms, "exit_code": exit_code,
                              "errors": self.errors,
                          })


register_backend("qwen", QwenBackend)
