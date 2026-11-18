"""OpenAI-compatible Chat Completions agent runtime (§10.6).

HTTP credentials stay global; histories stay in the managed project's data directory.
MCP tools use the same per-run bridge as Codex, including its bound Studio handlers.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

import httpx

from ...core.api_backends import ApiBackendConfig
from ...core.paths import ProjectPaths
from .base import AgentEvent, AgentRunRequest
from .api_context import (ContextBudgetError, ContextOverflow, ContextUsage, bounded_text, compact_history, context_diagnostic,
                          emergency_handoff, estimate_tokens, is_context_overflow)
from .mcp_bridge import bridge_servers


def _function(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


_STR = {"type": "string"}
_NATIVE = {
    "Read": _function("Read", "Read a bounded UTF-8 file excerpt. Use offset/limit in characters to page through large files.",
                      {"path": _STR, "offset": {"type": "integer", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1}}, ["path"]),
    "Glob": _function("Glob", "Find files by a relative glob pattern", {"pattern": _STR}, ["pattern"]),
    "Grep": _function("Grep", "Search UTF-8 files using a regular expression",
                      {"pattern": _STR, "glob": _STR}, ["pattern"]),
    "Write": _function("Write", "Write a complete UTF-8 file", {"path": _STR, "content": _STR}, ["path", "content"]),
    "Edit": _function("Edit", "Replace one unique exact substring in a UTF-8 file",
                      {"path": _STR, "old_string": _STR, "new_string": _STR}, ["path", "old_string", "new_string"]),
    "Bash": _function("Bash", "Run a shell command in the working directory (PowerShell on Windows)",
                      {"command": _STR}, ["command"]),
}


class OpenAIApiBackend:
    named_agents = False

    def __init__(self, connection: ApiBackendConfig):
        self.connection = connection
        self.name = connection.backend_id
        self.label = connection.name
        self.icon = connection.icon

    def is_available(self):
        # Local OpenAI-compatible servers may deliberately require no API key.
        return True, ""

    def _history_path(self, request, session_id):
        root = request.runtime_key.owner.project_root if request.runtime_key else request.workdir
        return ProjectPaths.for_project(root).data / "api-sessions" / self.connection.id / f"{session_id}.json"

    def _save(self, path, messages, context=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             delete=False, suffix=".tmp") as stream:
                temporary = Path(stream.name)
                json.dump({"version": 1, "messages": messages,
                           "context_usage": (context or ContextUsage()).dump()}, stream, ensure_ascii=False)
            temporary.replace(path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    async def _compact_if_needed(self, client, request, model, messages, schemas, usage,
                                 context, history_path, session_id, *, force=False, window=None):
        window = window or self.connection.context_window or 32768
        estimate = estimate_tokens(request.system_prompt, messages, schemas)
        before = context.tokens(estimate)
        if not force and before * 10 < window * 9:
            return
        latest_user = next((message for message in reversed(messages) if message["role"] == "user"), None)
        fixed = context.tokens(estimate_tokens(request.system_prompt, [latest_user] if latest_user else [], schemas))
        available = window * 9 // 10 - fixed - 128
        if available < 256:
            raise ContextBudgetError("The current message, system prompt and MCP tools leave no room for a context summary. Reduce them; the previous history is unchanged.")
        yield AgentEvent("continued", "Compacting API context at 90% of the configured window.",
                         data={"subtype": "context_compaction_started", "context_tokens": before,
                               "context_window": window}, session_id=session_id)
        headers = {}
        if self.connection.api_key.get_secret_value():
            headers["Authorization"] = "Bearer " + self.connection.api_key.get_secret_value()
        recovered = False
        try:
            if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > window * 8:
                raise ContextOverflow()  # Legacy oversized sessions recover without hundreds of summary calls.
            async with asyncio.timeout(60):
                compacted = await compact_history(client, self.connection.base_url + "/chat/completions",
                                                 headers, model, messages, window, request.max_message_bytes, usage,
                                                 summary_token_limit=max(128, available // 2))
        except Exception:
            # Cancellation (BaseException) still propagates. MCP task groups may wrap
            # provider errors in ExceptionGroup; recovery must happen inside the stack.
            compacted = None
        after_estimate = estimate_tokens(request.system_prompt, compacted, schemas)
        after = context.tokens(after_estimate)
        if compacted is None or after * 10 >= window * 9:
            archive = history_path.with_name(history_path.stem + ".before-recovery-" + uuid4().hex + ".json")
            self._save(archive, messages, context)
            # One UTF-8 byte per token is a conservative bound even for base64/minified
            # text; calibration from normal prose must not enlarge this budget.
            compacted = emergency_handoff(messages, max(256, available // 2), str(archive))
            after_estimate = estimate_tokens(request.system_prompt, compacted, schemas)
            after = context.tokens(after_estimate)
            if after * 10 >= window * 9:
                raise ContextBudgetError("The current instruction, system prompt and tools exceed the context window even after local recovery. Shorten the instruction or reduce enabled tools; exact history is preserved.")
            recovered = True
        # Publish the complete replacement atomically before mutating the in-flight list.
        fresh_context = ContextUsage(after, after_estimate, context.window_limit)
        self._save(history_path, compacted, fresh_context)
        messages[:] = compacted
        context.prompt_tokens, context.request_estimate = after, after_estimate
        yield AgentEvent("continued", ("API context recovered locally; exact history archived. Continuing the same session."
                                      if recovered else "API context compacted; continuing the same session."),
                         data={"subtype": "context_compacted", "context_tokens_before": before,
                               "context_tokens": after, "context_window": window,
                               "local_recovery": recovered}, session_id=session_id)
        yield AgentEvent("system", "usage_update", data={"subtype": "usage_update",
                         "usage": dict(usage), "total_tokens": sum(usage.values()),
                         "context_tokens": after, "context_window": window}, session_id=session_id)

    async def _native(self, name, args, request, *, window=None):
        root = request.workdir.resolve()

        def path(value):
            owner = request.runtime_key.owner.project_root if request.runtime_key else root
            artifacts = (ProjectPaths.for_project(owner).data / "api-sessions" / self.connection.id).resolve()
            if isinstance(value, str) and value.startswith("api-result:"):
                reference = value.removeprefix("api-result:")
                if name != "Read" or not re.fullmatch(r"[0-9a-f]{32}/[0-9a-f]{32}\.txt", reference):
                    raise ValueError("Invalid tool result reference; use the exact reference returned by the tool")
                candidate = (artifacts / "tool-results" / reference).resolve()
                if candidate.is_relative_to(artifacts / "tool-results"):
                    return candidate
                raise ValueError("Tool result path must stay inside the artifact directory")
            candidate = (root / value).resolve()
            if not candidate.is_relative_to(root):
                # Returned evidence stays with its managed project even in an isolated
                # checkout. Read may revisit those artifacts, never write outside root.
                if name == "Read" and candidate.is_relative_to(artifacts) and (
                    candidate.is_relative_to(artifacts / "tool-results") or ".before-recovery-" in candidate.name
                ):
                    return candidate
                raise ValueError("File path must stay inside the working directory")
            return candidate

        if name == "Read":
            target = path(args["path"])
            offset, limit = args.get("offset", 0), args.get("limit", 12000)
            if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
                raise ValueError("offset must be a nonnegative integer and limit a positive integer")
            budget = self._tool_budget(request, window=window)
            def read_excerpt():
                with target.open(encoding="utf-8", newline="") as source:
                    skipped = 0
                    while skipped < offset:
                        part = source.read(min(65536, offset - skipped))
                        if not part:
                            return "[End of file]"
                        skipped += len(part)
                    text = source.read(min(limit, budget) + 1)
                excerpt = bounded_text(text[:limit], max(4, budget - 128))
                if len(excerpt) < len(text):
                    excerpt += f"\n[Read truncated; continue this file with offset={offset + len(excerpt)} and limit. Offsets count characters.]"
                return excerpt
            return await asyncio.to_thread(read_excerpt)
        if name in {"Write", "Edit"}:
            target = path(args["path"])
            content = args.get("content", "")
            if name == "Edit":
                content = await asyncio.to_thread(target.read_text, encoding="utf-8")
                if not args["old_string"] or content.count(args["old_string"]) != 1:
                    raise ValueError("old_string must match exactly once")
                content = content.replace(args["old_string"], args["new_string"], 1)
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_text, content, encoding="utf-8")
            return "Saved"
        if name in {"Glob", "Grep"}:
            def search():
                pattern = args["pattern"] if name == "Glob" else args.get("glob", "**/*")
                if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                    raise ValueError("Use a relative glob inside the working directory")
                result = []
                regex = re.compile(args["pattern"]) if name == "Grep" else None
                for target in root.glob(pattern):
                    if not target.is_file() or not target.resolve().is_relative_to(root):
                        continue
                    relative = str(target.relative_to(root))
                    if regex is None:
                        result.append(relative)
                    elif target.stat().st_size <= request.max_message_bytes:
                        try:
                            for n, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
                                if regex.search(line):
                                    result.append(f"{relative}:{n}: {line}")
                                if len(result) >= 200:
                                    break
                        except (UnicodeError, OSError):
                            continue
                    if len(result) >= 200:
                        break
                return "\n".join(result)
            return await asyncio.to_thread(search)
        if name == "Bash":
            command = args["command"]
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command] if os.name == "nt" else ["/bin/sh", "-c", command]
            with tempfile.TemporaryFile() as output:
                process = await asyncio.create_subprocess_exec(*argv, cwd=root, stdout=output,
                                                               stderr=asyncio.subprocess.STDOUT)
                try:
                    await asyncio.wait_for(process.wait(), 120)
                finally:
                    if process.returncode is None:
                        if os.name == "nt":
                            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                                                                         stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                            await killer.wait()
                        else:
                            process.kill()
                        await process.wait()
                output.seek(0)
                return f"Exit code: {process.returncode}\n" + output.read(request.max_message_bytes).decode("utf-8", errors="replace")
        raise ValueError("Unknown tool")

    def _tool_budget(self, request, *, window=None):
        # §10.6: bound every tool (including MCP/shell), not just file reads.
        return max(256, min(request.max_message_bytes, 16384,
                            (window or self.connection.context_window or 32768) // 8))

    async def _bound_output(self, output, request, history_path, call_id, *, window=None):
        budget = self._tool_budget(request, window=window)
        if len(output.encode("utf-8")) <= budget:
            return output
        root = history_path.parent / "tool-results" / history_path.stem
        root.mkdir(parents=True, exist_ok=True)
        target = root / (hashlib.sha256(call_id.encode()).hexdigest()[:32] + ".txt")
        await asyncio.to_thread(target.write_text, output, encoding="utf-8")
        # A short Read reference also fits tiny windows and works across drive roots.
        reference = f"api-result:{history_path.stem}/{target.name}"
        notice = f"\n[Tool output truncated. Full output: {reference}. Read with offset/limit.]"
        return bounded_text(output, max(0, budget - len(notice.encode("utf-8")))) + notice

    async def _tools(self, request, stack):
        if request.text_only:
            return [], {}
        allowed = set(request.member.allowed_tools + request.extra_allowed_tools)
        unrestricted = request.permission_mode == "bypassPermissions"
        readonly = request.permission_mode == "plan"
        names = [n for n in _NATIVE if (unrestricted or n in allowed)
                 and (not readonly or n in {"Read", "Glob", "Grep"})]
        # Without an interactive approval channel, mutating tools require an explicit
        # member grant or bypassPermissions. Plan never exposes mutating/MCP tools.
        schemas = [_NATIVE[n] for n in names]
        handlers = {}
        if readonly or not request.mcp_servers:
            return schemas, handlers
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamablehttp_client

        servers = await stack.enter_async_context(bridge_servers(request.mcp_servers, request.max_message_bytes))
        for server_name, config in servers.items():
            if config.get("command"):
                transport = stdio_client(StdioServerParameters(command=config["command"], args=config.get("args", []),
                                                               env={**os.environ, **config.get("env", {})}, cwd=str(request.workdir)))
            elif config.get("type") == "sse":
                transport = sse_client(config["url"], headers=config.get("headers"))
            elif config.get("url"):
                transport = streamablehttp_client(config["url"], headers=config.get("headers"))
            else:
                raise ValueError("Unsupported MCP server configuration")
            streams = await stack.enter_async_context(transport)
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
            cursor = None
            while True:
                listing = await session.list_tools(cursor=cursor)
                for tool in listing.tools:
                    full_name = f"mcp__{server_name}__{tool.name}"
                    if not unrestricted and full_name not in allowed and f"mcp__{server_name}__*" not in allowed:
                        continue
                    # Stable short wire names also work with provider limits on names.
                    wire_name = "mcp_" + hashlib.sha256(full_name.encode("utf-8")).hexdigest()[:48]
                    schemas.append({"type": "function", "function": {"name": wire_name,
                                    "description": f"{full_name}: {tool.description or ''}", "parameters": tool.inputSchema}})
                    handlers[wire_name] = (session, tool.name, full_name)
                cursor = listing.nextCursor
                if not cursor:
                    break
        if len(schemas) > 128:
            raise ValueError("OpenAI API supports at most 128 tools; disable unneeded MCP servers")
        return schemas, handlers

    async def run(self, request: AgentRunRequest):
        session_id = request.resume_session_id
        if not session_id or not re.fullmatch(r"[0-9a-f]{32}", session_id):
            session_id = uuid4().hex
        history_path = self._history_path(request, session_id)
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        model = request.model or self.connection.model
        turns = 0
        effective_window = self.connection.context_window or 32768
        context = ContextUsage()
        try:
            messages = []
            if request.resume_session_id and history_path.exists():
                saved = json.loads(history_path.read_text(encoding="utf-8"))
                # Before context-window support the session file was a plain list.
                messages = saved if isinstance(saved, list) else saved["messages"]
                if isinstance(saved, dict):
                    context = ContextUsage.restore(saved.get("context_usage"))
                    if context.window_limit >= 1024:
                        effective_window = min(effective_window, context.window_limit)
                # A cancelled tool round must still be a valid Chat Completions history.
                replied = {m.get("tool_call_id") for m in messages if m["role"] == "tool"}
                for message in list(messages):
                    for call in message.get("tool_calls", []):
                        if call["id"] not in replied:
                            messages.append({"role": "tool", "tool_call_id": call["id"], "content": "Interrupted; inspect current state before retrying."})
            prompt = request.prompt if messages or not request.recovery_prompt else request.recovery_prompt
            content = [{"type": "text", "text": prompt}]
            for attachment in request.images:
                image = Path(attachment["path"])
                if image.stat().st_size > request.max_message_bytes:
                    raise ValueError("Image exceeds the message size limit")
                data = base64.b64encode(image.read_bytes()).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:{attachment['media_type']};base64,{data}"}})
            messages.append({"role": "user", "content": content if request.images else prompt})
            self._save(history_path, messages, context)
            yield AgentEvent("system", "OpenAI API session started", session_id=session_id)
            async with AsyncExitStack() as stack:
                schemas, handlers = await self._tools(request, stack)
                client = await stack.enter_async_context(httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30), follow_redirects=False))
                for turns in range(1, (request.max_turns or 40) + 1):
                    for recovery in range(7):
                        try:
                            async for event in self._compact_if_needed(client, request, model, messages, schemas,
                                                                       usage, context, history_path, session_id,
                                                                       force=recovery > 0, window=effective_window):
                                yield event
                            request_estimate = estimate_tokens(request.system_prompt, messages, schemas)
                            body = {"model": model, "messages": [{"role": "system", "content": request.system_prompt}, *messages],
                                    "stream": True, "stream_options": {"include_usage": True}}
                            output_limit = max(64, min(4096, effective_window // 10))
                            field = "max_completion_tokens" if self.connection.base_url.startswith("https://api.openai.com/") else "max_tokens"
                            body[field] = output_limit
                            if schemas:
                                body["tools"] = schemas
                            if request.effort in {"low", "medium", "high", "xhigh"}:
                                body["reasoning_effort"] = request.effort
                            headers = {}
                            if self.connection.api_key.get_secret_value():
                                headers["Authorization"] = "Bearer " + self.connection.api_key.get_secret_value()
                            answer, calls, finish = "", {}, None
                            response_id = uuid4().hex
                            thinking = ""
                            size = 0
                            async with client.stream("POST", self.connection.base_url + "/chat/completions", json=body, headers=headers) as response:
                                if response.status_code >= 300:
                                    error_body = bytearray()
                                    async for part in response.aiter_bytes():
                                        error_body.extend(part[:65536 - len(error_body)])
                                        if len(error_body) >= 65536:
                                            break
                                    if response.status_code in {400, 413, 422, 500} and is_context_overflow(error_body.decode("utf-8", errors="replace")):
                                        raise ContextOverflow()
                                    # Provider bodies can echo credentials: never publish them.
                                    raise ValueError(f"OpenAI API returned HTTP {response.status_code}. Check API URL, key and model.")
                                async for line in response.aiter_lines():
                                    if not line.startswith("data:"):
                                        continue
                                    raw = line[5:].strip()
                                    if raw == "[DONE]":
                                        break
                                    size += len(raw.encode("utf-8"))
                                    if size > request.max_message_bytes:
                                        raise ValueError("API response exceeds the message size limit")
                                    chunk = json.loads(raw)
                                    if chunk.get("error"):
                                        if is_context_overflow(chunk["error"]):
                                            raise ContextOverflow()
                                        raise ValueError("OpenAI API stream reported an error")
                                    stats = chunk.get("usage")
                                    if stats:
                                        context.observe(stats, request_estimate)
                                        cached = (stats.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                        usage["input_tokens"] += max(0, stats.get("prompt_tokens", 0) - cached)
                                        usage["cache_read_input_tokens"] += cached
                                        usage["output_tokens"] += stats.get("completion_tokens", 0)
                                        yield AgentEvent("system", "usage_update", data={"subtype": "usage_update", "total_tokens": sum(usage.values()), "usage": dict(usage), "context_tokens": stats.get("prompt_tokens", 0),
                                                         **({"context_window": self.connection.context_window} if self.connection.context_window else {})}, session_id=session_id)
                                    for choice in chunk.get("choices", []):
                                        delta = choice.get("delta", {})
                                        finish = choice.get("finish_reason") or finish
                                        text = delta.get("content") or delta.get("refusal")
                                        if text:
                                            answer += text
                                            yield AgentEvent("text", text, data={"stream_id": response_id,
                                                             "stream_text": answer}, session_id=session_id)
                                        if delta.get("reasoning_content"):
                                            thinking += delta["reasoning_content"]
                                            yield AgentEvent("thinking", delta["reasoning_content"],
                                                             data={"stream_id": response_id, "stream_text": thinking},
                                                             session_id=session_id)
                                        for call in delta.get("tool_calls", []):
                                            item = calls.setdefault(call["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                            item["id"] += call.get("id", "")
                                            for key in ("name", "arguments"):
                                                item["function"][key] += call.get("function", {}).get(key, "")
                            break
                        except ContextOverflow:
                            if recovery == 6 or effective_window <= 1024:
                                raise ContextBudgetError("The provider still rejects the context after recovery. Check the configured context window or reduce the current instruction and enabled tools; history is preserved.") from None
                            effective_window = max(1024, effective_window // 2)
                            context.window_limit = effective_window
                            self._save(history_path, messages, context)
                            yield AgentEvent("continued", "Provider rejected the context; reducing its budget and recovering before retrying.",
                                             data={"subtype": "context_overflow_recovery", "context_window": effective_window}, session_id=session_id)
                    if finish not in {"stop", "tool_calls"}:
                        raise ValueError(f"OpenAI API response did not complete (reason: {finish or 'stream interrupted'})")
                    assistant = {"role": "assistant", "content": answer or None}
                    if calls:
                        assistant["tool_calls"] = list(calls.values())
                    messages.append(assistant)
                    self._save(history_path, messages, context)
                    if not calls:
                        async for event in self._compact_if_needed(client, request, model, messages, schemas,
                                                                   usage, context, history_path, session_id, window=effective_window):
                            yield event
                        yield AgentEvent("result", answer, data={"is_error": False, "num_turns": turns, "usage": usage,
                                         "total_tokens": sum(usage.values()), "cost_unavailable": True,
                                         "model": model, "stop_reason": finish}, session_id=session_id, is_final=True)
                        return
                    for call in calls.values():
                        name = call["function"]["name"]
                        display = handlers[name][2] if name in handlers else name
                        args = json.loads(call["function"]["arguments"])
                        yield AgentEvent("tool_use", display, data={"id": call["id"], "name": display, "input": args}, session_id=session_id)
                        failed = False
                        try:
                            if name in handlers:
                                session, tool_name, _ = handlers[name]
                                result = await session.call_tool(tool_name, args)
                                failed = result.isError
                                output = result.model_dump_json(by_alias=True, exclude_none=True)
                            elif any(s["function"]["name"] == name for s in schemas) and name in _NATIVE:
                                output = await self._native(name, args, request, window=effective_window)
                            else:
                                raise ValueError("Tool is not permitted")
                        except Exception as exc:
                            failed = True
                            output = str(exc) if type(exc) is ValueError else f"Tool failed: {type(exc).__name__}"
                            secret = self.connection.api_key.get_secret_value()
                            if secret:
                                output = output.replace(secret, "[redacted]")
                        output = await self._bound_output(output, request, history_path, call["id"], window=effective_window)
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
                        self._save(history_path, messages, context)
                        yield AgentEvent("tool_result", output, data={"tool_use_id": call["id"], "is_error": failed}, session_id=session_id)
                raise ValueError("OpenAI API agent reached the configured maximum number of turns")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Only our deliberately safe validation messages are shown. HTTP/MCP errors
            # may contain request headers or credentials in URLs and exception chains.
            message = context_diagnostic(exc) or (str(exc) if type(exc) is ValueError else f"OpenAI API run failed ({type(exc).__name__})")
            secret = self.connection.api_key.get_secret_value()
            if secret:
                message = message.replace(secret, "[redacted]")
            yield AgentEvent("error", message, session_id=session_id)
            yield AgentEvent("result", message, data={"is_error": True, "errors": [message], "num_turns": turns,
                             "usage": usage, "total_tokens": sum(usage.values()), "cost_unavailable": True},
                             session_id=session_id, is_final=True)
