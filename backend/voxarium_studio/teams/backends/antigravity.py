"""Antigravity CLI backend over the Agent Client Protocol (§10.6), checked against agy 1.5.10.

``agy -p`` has no per-run MCP configuration — its only MCP file is the user's global
``~/.gemini/config/mcp_config.json`` and ``--settings`` rejects ``mcpServers`` — so the
Studio drives ``agy acp`` instead: one process per task/chat turn, speaking JSON-RPC on
stdio. ACP gives everything the Studio hands a session through its own channel: MCP
servers per session, the role appended to Antigravity's system prompt
(``_meta.antigravity.systemPrompt.append``), ``session/load`` to resume, model, mode and
thinking level, and a permission request per tool call that the Studio answers from the
member's permission mode. Auth belongs to Antigravity; the Studio never sees it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..changes import edit_stat
from .base import AgentBackend, AgentEvent, AgentRunRequest, register_backend
from .cli_stream import _Oversized, _drain, _frames, _kill_tree
from .mcp_bridge import bridge_servers
from .recovery import recovery_prompt

#: The ACP version the Studio speaks. agy 1.5.10 answers 1.
_PROTOCOL_VERSION = 1

#: Seconds for the set-up calls; ``session/new`` starts every MCP server before it answers.
_SETUP_TIMEOUT = 180.0

#: Studio permission mode -> Antigravity session mode (§10.3). Antigravity has no
#: "approve everything" mode of its own: bypass and auto are the Studio answering yes to
#: every permission request the session raises (see :func:`_permitted`).
_MODES = {
    "plan": "plan", "default": "default", "dontAsk": "default",
    "acceptEdits": "accept-edits", "auto": "accept-edits", "bypassPermissions": "accept-edits",
}

#: Tool kinds ACP attaches to each call, and the modes that let them through unasked.
_READING = frozenset({"read", "search", "think", "fetch"})
_EDITING = frozenset({"edit", "delete", "move"})

#: Antigravity's tool names in the Studio's (Claude Code's) vocabulary, so the transcript,
#: the change list and the allowed-tools list read the same on every backend (§10.4).
_TOOLS = {
    "read_file": "Read", "view_file": "Read", "write_file": "Write", "write_to_file": "Write",
    "edit_file": "Edit", "replace_file_content": "Edit", "multi_replace_file_content": "Edit",
    "run_command": "Bash", "grep_search": "Grep", "find_by_name": "Glob", "list_dir": "LS",
    "search_web": "WebSearch", "read_url_content": "WebFetch", "notebook_edit": "NotebookEdit",
}

#: Argument names Antigravity uses for what Claude Code calls these, so ``edit_stat`` and
#: the change list find the path and both sides of an edit.
_ARGUMENTS = {
    "Path": "file_path", "AbsolutePath": "file_path", "TargetFile": "file_path",
    "Content": "content", "CodeContent": "content",
    "Target": "old_string", "TargetContent": "old_string",
    "Replacement": "new_string", "ReplacementContent": "new_string",
    "CommandLine": "command", "toolSummary": "description",
}

#: A queue marker of the Studio's own, never an ACP update: a permission it refused.
_DENIED = "_studio_permission_denied"

#: Studio effort -> Antigravity's ``thought_level``, which stops at high.
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


#: Models for the member editor's picker (§10.6), as ``agy models`` lists them. Not a
#: whitelist, exactly like ``SUGGESTED_MODELS`` in ``api/teams.py``: ``member.model``
#: reaches ``session/set_model`` verbatim, and one the session does not offer falls back
#: to Antigravity's own choice with a note rather than failing the run.
_SUGGESTED_MODELS: list[dict[str, str]] = [
    {"id": "", "label": "Antigravity default", "group": "Antigravity",
     "note": "Uses the model selected in Antigravity"},
    {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)", "group": "Antigravity",
     "note": "High thinking level; 1M context"},
    {"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)",
     "group": "Antigravity", "note": "Medium thinking level; 1M context"},
    {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)", "group": "Antigravity",
     "note": "Fast; 1M context"},
    {"id": "claude-opus-4-6-thinking", "label": "Claude Opus 4.6 (Thinking)",
     "group": "Antigravity", "note": "Through Antigravity"},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (Thinking)", "group": "Antigravity",
     "note": "Through Antigravity"},
    {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)", "group": "Antigravity",
     "note": "Open-weight"},
]


def suggested_models() -> list[dict[str, str]]:
    return [dict(entry, backend="antigravity") for entry in _SUGGESTED_MODELS]


def _launcher() -> list[str] | None:
    found = shutil.which("agy")
    return [found] if found else None


class _RpcError(RuntimeError):
    """A JSON-RPC error answer, or the process ending before it answered."""


class _Connection:
    """JSON-RPC 2.0 over the agent's stdio, one object per line (ACP's stdio transport)."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        max_bytes: int,
        on_request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        self._process = process
        self._max_bytes = max_bytes
        self._on_request = on_request
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._replay_id: int | None = None
        self._answers: set[asyncio.Task[None]] = set()
        self.updates: asyncio.Queue[dict[str, Any] | _Oversized | None] = asyncio.Queue()
        self.closed = False

    async def read(self) -> None:
        assert self._process.stdout is not None
        try:
            async for frame in _frames(self._process.stdout, self._max_bytes):
                if isinstance(frame, _Oversized):
                    self.updates.put_nowait(frame)
                elif "method" in frame and "id" in frame:
                    task = asyncio.create_task(self._answer(frame))
                    self._answers.add(task)
                    task.add_done_callback(self._answers.discard)
                elif "method" in frame:
                    if frame["method"] == "session/update" and self._replay_id is None:
                        update = (frame.get("params") or {}).get("update")
                        if isinstance(update, dict):
                            self.updates.put_nowait(update)
                elif frame.get("id") in self._pending:
                    if frame["id"] == self._replay_id:
                        self._replay_id = None
                    future = self._pending.pop(frame["id"])
                    if not future.done():
                        future.set_result(frame)
        finally:
            self.closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(_RpcError("Antigravity exited before answering"))
            self._pending.clear()
            self.updates.put_nowait(None)

    async def call(self, method: str, params: dict[str, Any], *, replay: bool = False,
                   timeout: float | None = None) -> dict[str, Any]:
        """Send a request and wait for its result. ``replay`` mutes the history
        ``session/load`` streams back before it answers — the Studio has it already."""
        if self.closed:
            raise _RpcError("Antigravity is not running")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        if replay:
            self._replay_id = request_id
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        answer = await (asyncio.wait_for(future, timeout) if timeout else future)
        if "error" in answer:
            error = answer["error"] if isinstance(answer["error"], dict) else {}
            detail = error.get("data")
            message = str(error.get("message") or "Antigravity reported an error")
            if isinstance(detail, str) and detail and detail not in message:
                message += f": {detail}"
            raise _RpcError(message)
        result = answer.get("result")
        return result if isinstance(result, dict) else {}

    async def _answer(self, frame: dict[str, Any]) -> None:
        try:
            result = await self._on_request(str(frame["method"]), frame.get("params") or {})
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": frame["id"], "result": result}
        except Exception as exc:  # an unknown method is an answer too, never a hang
            reply = {"jsonrpc": "2.0", "id": frame["id"],
                     "error": {"code": -32601, "message": str(exc) or "Method not found"}}
        with contextlib.suppress(_RpcError, ConnectionError, BrokenPipeError):
            await self._write(reply)

    async def _write(self, message: dict[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            raise _RpcError("Antigravity is not running")
        stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _RpcError("Antigravity is not running") from exc

    async def close(self) -> None:
        for task in list(self._answers):
            task.cancel()
        await asyncio.gather(*self._answers, return_exceptions=True)


def _acp_servers(servers: dict[str, Any]) -> list[dict[str, Any]]:
    """Claude-shaped MCP configs as ACP ``McpServer`` entries: name/value pair lists."""
    result = []
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        kind = server.get("type")
        if kind in {"http", "sse"} or ("url" in server and "command" not in server):
            headers = server.get("headers") or {}
            result.append({"type": "sse" if kind == "sse" else "http", "name": name,
                           "url": str(server.get("url", "")),
                           "headers": [{"name": str(k), "value": str(v)} for k, v in headers.items()]})
        elif "command" in server:
            env = server.get("env") or {}
            result.append({"name": name, "command": str(server["command"]),
                           "args": [str(arg) for arg in server.get("args") or []],
                           "env": [{"name": str(k), "value": str(v)} for k, v in env.items()]})
    return result


def _tool_name(raw: str, servers: list[str]) -> str:
    """``mcp_<server>_<tool>`` back to the Studio's ``mcp__<server>__<tool>``."""
    if raw.startswith("mcp_"):
        for server in sorted(servers, key=len, reverse=True):
            prefix = f"mcp_{server}_"
            if raw.startswith(prefix) and len(raw) > len(prefix):
                return f"mcp__{server}__{raw[len(prefix):]}"
    return _TOOLS.get(raw, raw)


def _tool_input(value: Any) -> dict[str, Any]:
    args = dict(value) if isinstance(value, dict) else {}
    return {_ARGUMENTS.get(key, key): item for key, item in args.items()}


def _permitted(request: AgentRunRequest, name: str, kind: str) -> bool:
    """Whether the Studio approves one tool call Antigravity asked about (§10.3).

    The session is headless, so every permission request is the Studio's to answer, and
    it answers as Claude Code's modes do: plan and default let reads through, acceptEdits
    adds edits, auto and bypassPermissions allow everything. A tool named in the member's
    allowed tools (Studio MCP tools included) is allowed in every mode but plan. Rules
    with an argument pattern — ``Bash(git:*)`` — are not matched against the call and
    allow nothing here, which errs towards asking less of the tool, not more.
    """
    if request.text_only:
        return False
    mode = request.permission_mode or "default"
    if mode in {"bypassPermissions", "auto"}:
        return True
    if kind in _READING:
        return True
    if mode == "plan":
        return False
    if mode == "acceptEdits" and kind in _EDITING:
        return True
    allowed = {entry for entry in (*request.member.allowed_tools, *request.extra_allowed_tools)
               if "(" not in entry}
    if name in allowed:
        return True
    if name.startswith("mcp__"):
        server = name.split("__")[1]
        return bool({f"mcp__{server}", f"mcp__{server}__*"} & allowed)
    return False


def _choose(options: Any, allow: bool) -> dict[str, Any]:
    kinds = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for kind in kinds:
        for option in options if isinstance(options, list) else []:
            if isinstance(option, dict) and option.get("kind") == kind and option.get("optionId"):
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(value: Any) -> dict[str, int]:
    """ACP's prompt usage in the Studio's fields (§10.4). Input excludes cache reads, as
    Claude's does; thought tokens are output the model was billed for."""
    if not isinstance(value, dict):
        return {}
    usage: dict[str, int] = {}
    for source, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                           ("cachedReadTokens", "cache_read_input_tokens"),
                           ("cachedWriteTokens", "cache_creation_input_tokens")):
        count = _count(value.get(source))
        if count is not None:
            usage[target] = count
    thought = _count(value.get("thoughtTokens"))
    if thought:
        usage["output_tokens"] = usage.get("output_tokens", 0) + thought
    return usage


def _content_text(value: Any) -> str:
    """Text out of ACP tool-call content: ``{"type": "content", "content": {...}}`` blocks."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_content_text(item) for item in value)))
    if isinstance(value, dict):
        if value.get("type") == "diff":
            return f"Edited {value.get('path', '')}".strip()
        if "content" in value:
            return _content_text(value["content"])
        return str(value.get("text") or "")
    return ""


class _Turn:
    """One prompt's ``session/update`` stream, translated to Studio events."""

    def __init__(self, session_id: str, servers: list[str]) -> None:
        self.session_id = session_id
        self._servers = servers
        self._text: list[str] = []
        self._thought: list[str] = []
        self._calls: dict[str, str] = {}
        self._finished: set[str] = set()
        self._context: tuple[int | None, int | None] = (None, None)
        self._messages = 0
        self._denied: tuple[str, int] | None = None
        self.last_text = ""

    @property
    def stopped_by_denial(self) -> str:
        """The tool the Studio refused, when the model wrote nothing after it."""
        if self._denied is not None and self._denied[1] == self._messages:
            return self._denied[0]
        return ""

    def update(self, update: dict[str, Any]) -> list[AgentEvent]:
        kind = update.get("sessionUpdate")
        events: list[AgentEvent] = []
        if kind == _DENIED:
            events = self.flush()
            self._denied = (str(update.get("name") or "a tool"), self._messages)
            return events
        # Chunks are gathered into one transcript entry per message, as Claude Code's are.
        if kind == "agent_message_chunk":
            self._thought_done_into(events)
            self._text.append(_content_text(update.get("content")))
            return self._stamp(events)
        if kind == "agent_thought_chunk":
            self._text_done_into(events)
            self._thought.append(_content_text(update.get("content")))
            return self._stamp(events)
        events = self.flush()
        if kind == "tool_call" or (kind == "tool_call_update"
                                   and update.get("toolCallId") not in self._calls):
            events += self._call(update)
        if kind == "tool_call_update":
            events += self._result(update)
        elif kind == "usage_update":
            context = (_count(update.get("used")), _count(update.get("size")))
            if context != self._context and context[0] is not None:
                self._context = context
                data: dict[str, Any] = {"subtype": "usage_update", "context_tokens": context[0]}
                if context[1]:
                    data["context_window"] = context[1]
                events.append(AgentEvent(kind="system", text="usage_update", data=data))
        elif kind == "plan":
            entries = update.get("entries") if isinstance(update.get("entries"), list) else []
            events.append(AgentEvent(kind="system", text="plan", data={
                "subtype": "plan", "entries": [entry for entry in entries if isinstance(entry, dict)],
            }))
        return self._stamp(events)

    def flush(self) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        self._thought_done_into(events)
        self._text_done_into(events)
        return self._stamp(events)

    def _text_done_into(self, events: list[AgentEvent]) -> None:
        text, self._text = "".join(self._text), []
        if text.strip():
            self.last_text = text
            self._messages += 1
            events.append(AgentEvent(kind="text", text=text))

    def _thought_done_into(self, events: list[AgentEvent]) -> None:
        thought, self._thought = "".join(self._thought), []
        if thought.strip():
            events.append(AgentEvent(kind="thinking", text=thought))

    def _call(self, update: dict[str, Any]) -> list[AgentEvent]:
        call_id = str(update.get("toolCallId") or "")
        if not call_id or call_id in self._calls:
            return []
        raw_name = str(update.get("name") or update.get("title") or "tool")
        name = _tool_name(raw_name, self._servers)
        self._calls[call_id] = name
        args = _tool_input(update.get("rawInput", update.get("args")))
        data: dict[str, Any] = {
            "id": call_id, "name": name, "kind": update.get("kind"),
            "input": {key: (value if isinstance(value, str) else
                            json.dumps(value, ensure_ascii=False))[:500]
                      for key, value in args.items()},
        }
        stat = edit_stat(name, args)
        if stat is not None:
            data["edit"] = stat
        return [AgentEvent(kind="tool_use", text=name, data=data)]

    def _result(self, update: dict[str, Any]) -> list[AgentEvent]:
        call_id = str(update.get("toolCallId") or "")
        status = update.get("status")
        if status not in {"completed", "failed"} or call_id in self._finished:
            return []
        self._finished.add(call_id)
        text = _content_text(update.get("content")) or _content_text(update.get("rawOutput"))
        return [AgentEvent(kind="tool_result", text=(text or str(status))[:2000],
                           data={"tool_use_id": call_id, "is_error": status == "failed"})]

    def _stamp(self, events: list[AgentEvent]) -> list[AgentEvent]:
        for event in events:
            event.session_id = self.session_id
        return events


class AntigravityBackend(AgentBackend):
    name = "antigravity"
    #: No ``claude --agent`` equivalent: the role travels in the system prompt (§10.3).
    named_agents = False
    session_recovery = True
    #: Studio effort becomes the session's thinking level (§10.3, §10.11).
    supports_effort = True

    def is_available(self) -> tuple[bool, str]:
        if _launcher() is None:
            return False, ("The `agy` CLI was not found. Install the Antigravity CLI from "
                           "https://antigravity.google, then run `agy` once to sign in.")
        # Google sign-in, an API key and Vertex credentials are all Antigravity's own; only
        # its runtime can tell which one is configured, so it is asked when a run starts.
        return True, "Antigravity CLI installed; sign-in is checked when a run starts."

    async def run(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]:
        available, reason = self.is_available()
        if not available:
            yield AgentEvent(kind="error", text=reason, is_final=True)
            return
        try:
            async with bridge_servers(request.mcp_servers, request.max_message_bytes) as servers:
                async for event in self._run_once(replace(request, mcp_servers=servers)):
                    yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield AgentEvent(kind="error", text=f"{type(exc).__name__}: {exc}", is_final=True)

    async def _run_once(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]:
        started = time.monotonic()
        tail: deque[str] = deque(maxlen=4000)
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        process = await asyncio.create_subprocess_exec(
            *(_launcher() or []), "acp", cwd=str(request.workdir), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=max(65536, request.max_message_bytes),
            start_new_session=os.name != "nt",
        )
        assert process.stderr is not None
        server_names = list(request.mcp_servers)

        async def on_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method != "session/request_permission":
                raise RuntimeError(f"Method not supported by the Studio: {method}")
            call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            name = _tool_name(str(call.get("name") or call.get("title") or ""), server_names)
            allowed = _permitted(request, name, str(call.get("kind") or ""))
            if not allowed:
                # Through the queue, so it lands in order with the turn's own updates.
                connection.updates.put_nowait({"sessionUpdate": _DENIED, "name": name})
            return _choose(params.get("options"), allowed)

        connection = _Connection(process, request.max_message_bytes, on_request)
        reader = asyncio.create_task(connection.read())
        stderr_task = asyncio.create_task(_drain(process.stderr, tail))
        session_id: str | None = None
        errors: list[str] = []
        try:
            try:
                await connection.call("initialize", {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                           "terminal": False},
                    "clientInfo": {"name": "voxarium-studio"},
                }, timeout=_SETUP_TIMEOUT)
                params = self._session_params(request)
                prompt = request.prompt
                session: dict[str, Any] = {}
                if request.resume_session_id:
                    try:
                        session = await connection.call(
                            "session/load", {**params, "sessionId": request.resume_session_id},
                            replay=True, timeout=_SETUP_TIMEOUT)
                        session_id = request.resume_session_id
                    except _RpcError as exc:
                        if connection.closed:
                            raise
                        # Nothing has run yet, so a fresh session with the saved context
                        # loses nothing the member had (§10.6).
                        yield AgentEvent(kind="system", text="The saved Antigravity session is "
                                         f"unavailable ({exc}). Starting a new session with "
                                         "saved context.",
                                         data={"subtype": "session_recovered",
                                               "previous_session_id": request.resume_session_id})
                        prompt = request.recovery_prompt or recovery_prompt(
                            request.prompt, [], request.task)
                if session_id is None:
                    session = await connection.call("session/new", params, timeout=_SETUP_TIMEOUT)
                    session_id = str(session.get("sessionId") or "")
                    if not session_id:
                        raise _RpcError("Antigravity started no session")
                async for note in self._configure(connection, session_id, session, request):
                    yield note
            except _RpcError as exc:
                errors.append(self._explain(str(exc), tail))
                yield AgentEvent(kind="error", text=errors[-1], session_id=session_id)
                yield self._finish(session_id, {}, errors, "", started, process)
                return

            yield AgentEvent(kind="system", text="init", session_id=session_id, data={
                "subtype": "init", "session_id": session_id, "cwd": str(request.workdir),
                "model": request.model or "", "permissionMode": request.permission_mode,
            })
            turn = _Turn(session_id, server_names)
            prompting = asyncio.create_task(connection.call("session/prompt", {
                "sessionId": session_id, "prompt": self._prompt_blocks(prompt, request.images),
            }))
            try:
                while not (prompting.done() and connection.updates.empty()):
                    getter = asyncio.ensure_future(connection.updates.get())
                    await asyncio.wait({getter, prompting}, return_when=asyncio.FIRST_COMPLETED)
                    if not getter.done():
                        getter.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await getter
                        continue
                    item = getter.result()
                    if item is None:
                        continue
                    if isinstance(item, _Oversized):
                        yield AgentEvent(kind="system", session_id=session_id,
                                         text=f"Skipped a {item.size}-byte Antigravity message "
                                         "exceeding teams.max_message_bytes.",
                                         data={"subtype": "oversized_line", "bytes": item.size})
                        continue
                    for event in turn.update(item):
                        yield event
                for event in turn.flush():
                    yield event
                outcome = prompting.result()
            except _RpcError as exc:
                for event in turn.flush():
                    yield event
                errors.append(self._explain(str(exc), tail))
                yield AgentEvent(kind="error", text=errors[-1], session_id=session_id)
                yield self._finish(session_id, {}, errors, turn.last_text, started, process)
                return
            finally:
                if not prompting.done():
                    prompting.cancel()
                    with contextlib.suppress(BaseException):
                        await prompting
            stop = str(outcome.get("stopReason") or "")
            if stop in {"refusal", "cancelled"}:
                errors.append(f"Antigravity stopped the turn: {stop}")
                yield AgentEvent(kind="error", text=errors[-1], session_id=session_id)
            elif turn.stopped_by_denial:
                # Antigravity ends the turn at the first refused tool rather than telling the
                # model and carrying on, so the work stopped half done: say so (§10.6).
                errors.append(
                    f"Antigravity ended the turn when the Studio denied {turn.stopped_by_denial} "
                    f"(permission mode {request.permission_mode}). Antigravity stops at the "
                    "first refused tool: add it to the member's Allowed tools or choose a more "
                    "permissive mode.")
                yield AgentEvent(kind="error", text=errors[-1], session_id=session_id)
            yield self._finish(session_id, outcome, errors, turn.last_text, started, process)
        finally:
            with contextlib.suppress(Exception):
                if process.stdin is not None:
                    process.stdin.close()
            if process.returncode is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), 3)
            if process.returncode is None:
                await _kill_tree(process)
            await connection.close()
            for task in (reader, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, stderr_task, return_exceptions=True)

    @staticmethod
    def _session_params(request: AgentRunRequest) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": str(request.workdir.resolve()),
                                  "mcpServers": _acp_servers(request.mcp_servers)}
        workdir = request.workdir.resolve()
        extra = [str(root.resolve()) for root in request.writable_roots if root.resolve() != workdir]
        if extra:
            params["additionalDirectories"] = extra
        if request.system_prompt:
            # Appended, not replaced: ACP offers no replacement, and Antigravity's own
            # prompt is what teaches the model its tools (§10.6).
            params["_meta"] = {"antigravity": {"systemPrompt": {"append": request.system_prompt}}}
        return params

    @staticmethod
    async def _configure(connection: _Connection, session_id: str, session: dict[str, Any],
                         request: AgentRunRequest) -> AsyncIterator[AgentEvent]:
        """Model, mode and thinking level — each optional, each a note when it cannot apply."""
        def note(subtype: str, text: str, **data: Any) -> AgentEvent:
            return AgentEvent(kind="system", text=text, session_id=session_id,
                              data={"subtype": subtype, **data})

        models = session.get("models") if isinstance(session.get("models"), dict) else {}
        model_ids = {str(entry.get("modelId")) for entry in models.get("availableModels") or []
                     if isinstance(entry, dict)}
        options = {str(option.get("id")): option for option in session.get("configOptions") or []
                   if isinstance(option, dict)}
        model = request.model.strip()
        if model and model != "auto":
            option_values = {str(value.get("value")) for value in
                             (options.get("model") or {}).get("options") or []
                             if isinstance(value, dict)}
            try:
                if model in model_ids or not (model_ids or option_values):
                    await connection.call("session/set_model",
                                          {"sessionId": session_id, "modelId": model})
                elif model in option_values:
                    await connection.call("session/set_config_option", {
                        "sessionId": session_id, "configId": "model", "value": model})
                else:
                    yield note("model_fallback", f"Antigravity does not offer the model {model}; "
                               "using its selected model.", model=model)
            except _RpcError as exc:
                yield note("model_fallback", f"Antigravity rejected the model {model} ({exc}); "
                           "using its selected model.", model=model)

        modes = session.get("modes") if isinstance(session.get("modes"), dict) else {}
        available_modes = {str(mode.get("id")) for mode in modes.get("availableModes") or []
                           if isinstance(mode, dict)}
        mode = "plan" if request.text_only else _MODES.get(request.permission_mode or "", "default")
        if mode in available_modes and mode != modes.get("currentModeId"):
            with contextlib.suppress(_RpcError):
                await connection.call("session/set_mode", {"sessionId": session_id, "modeId": mode})

        if request.effort:
            levels = {str(value.get("value")) for value in
                      (options.get("thought_level") or {}).get("options") or []
                      if isinstance(value, dict)}
            level = request.effort if request.effort in levels else _EFFORT.get(request.effort)
            applied = False
            if level in levels:
                with contextlib.suppress(_RpcError):
                    await connection.call("session/set_config_option", {
                        "sessionId": session_id, "configId": "thought_level", "value": level})
                    applied = True
            if not applied:
                yield note("unsupported_settings", "This Antigravity model has no thinking "
                           f"level for effort {request.effort}; its own default applies.")
        if request.max_turns:
            yield note("unsupported_settings", "Antigravity uses its own turn limits; Studio "
                       "Max turns does not apply.")

    @staticmethod
    def _prompt_blocks(prompt: str, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            try:
                data = Path(image["path"]).read_bytes()
            except (OSError, KeyError, TypeError):
                continue  # the prompt names the file; the agent can still open it
            blocks.append({"type": "image", "mimeType": str(image.get("media_type") or "image/png"),
                           "data": base64.b64encode(data).decode("ascii")})
        return blocks

    @staticmethod
    def _explain(message: str, tail: deque[str]) -> str:
        stderr = "".join(tail).strip()
        if stderr and stderr not in message:
            message = f"{message}: {stderr[-1500:]}"
        if any(word in message.lower() for word in ("auth", "sign in", "log in", "login")):
            message += " — run `agy` once in a terminal to sign in."
        return message

    @staticmethod
    def _finish(session_id: str | None, outcome: dict[str, Any], errors: list[str], text: str,
                started: float, process: asyncio.subprocess.Process) -> AgentEvent:
        usage = _usage(outcome.get("usage"))
        failed = bool(errors) or not outcome
        return AgentEvent(kind="result", text=text, session_id=session_id, is_final=True, data={
            "subtype": "error" if failed else "success", "is_error": failed,
            "stop_reason": outcome.get("stopReason"),
            "num_turns": 1 if outcome else 0, "usage": usage,
            "total_tokens": sum(usage.values()),
            "total_cached_tokens": usage.get("cache_read_input_tokens", 0),
            "total_uncached_tokens": sum(value for key, value in usage.items()
                                         if key != "cache_read_input_tokens"),
            "accounting_version": 2,
            "total_cost_usd": 0.0, "cost_unavailable": True,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "exit_code": process.returncode, "errors": errors,
        })


register_backend("antigravity", AntigravityBackend)
