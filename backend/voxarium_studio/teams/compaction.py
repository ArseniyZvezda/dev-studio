"""Task context compaction and the small audit facts it must preserve (§10.4)."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import replace
from typing import Any

from ..models.teams import AgentMessage
from .backends.base import AgentRunRequest

CHUNK_CHARS = 40_000
SUMMARY_CHARS = 12_000
INPUT_KINDS = frozenset({"text", "note", "summary"})
SUMMARY_KIND = "compressed summary"
SUMMARY_KINDS = frozenset({SUMMARY_KIND, "context_summary"})  # Read older saved handoffs too.


def has_dialogue(messages: list[AgentMessage]) -> bool:
    """Only new, nonempty dialogue can start another compression (§10.4)."""
    return any(m.kind in INPUT_KINDS and m.text.strip() for m in messages)


def transcript_text(messages: list[AgentMessage]) -> str:
    # Only prose participates (§10.4). A previous compressed handoff is a summary,
    # including the legacy spelling; its audit metadata is never model input.
    return "\n\n".join(
        f"[{m.role}/{'summary' if m.kind in SUMMARY_KINDS else m.kind}] {m.text}"
        for m in messages
        if m.kind in INPUT_KINDS | SUMMARY_KINDS and m.text.strip()
    )


def context_tokens(messages: list[AgentMessage]) -> int:
    """Latest request usage, never cumulative run usage; estimate when unreported."""
    for message in reversed(messages):
        if message.kind in SUMMARY_KINDS:
            break
        if message.role != "assistant":
            continue
        usage = message.data.get("usage")
        if not isinstance(usage, dict):
            continue
        keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        if any(key in usage for key in keys):
            return sum(max(0, int(usage[key])) for key in keys
                       if isinstance(usage.get(key), (int, float)))
    return math.ceil(len(transcript_text(messages)) / 4)


def expand_evidence(messages: list[AgentMessage]) -> list[AgentMessage]:
    """Read retained accounting/file facts, without resurrecting the old dialogue."""
    expanded = []
    for message in messages:
        if message.kind not in SUMMARY_KINDS:
            expanded.append(message)
            continue
        for record in message.data.get("evidence", []):
            expanded.append(AgentMessage.model_validate({
                **record, "task_id": message.task_id, "member_id": message.member_id,
            }))
    return expanded


def retain_evidence(messages: list[AgentMessage]) -> list[dict[str, Any]]:
    """Keep accounting, file evidence and its limitations, not dialogue/tool payloads."""
    from .changes import WRITING_TOOLS, tool_name

    records = []
    for message in expand_evidence(messages):
        data = message.data
        if message.kind == "result":
            data = {key: data[key] for key in ("total_cost_usd", "model") if key in data}
        elif message.kind in {"model", "system"}:
            if message.kind == "system" and message.text != "init":
                continue
            data = {"model": data.get("model")}
        elif message.kind in {"workdir", "file_observation"}:
            data = {key: data[key] for key in ("path", "paths", "root") if key in data}
        elif message.kind == "file_observation_error":
            # §10.4: retaining partial file evidence without its warning would make a
            # compressed task appear to have been observed completely.
            data = {}
        elif message.kind == "tool_use" and tool_name(data).casefold() in WRITING_TOOLS:
            inputs = data.get("input")
            if not isinstance(inputs, dict):
                continue
            data = {"name": data.get("name"), "edit": data.get("edit"),
                    "input": {key: inputs[key] for key in ("path", "file_path", "notebook_path")
                              if key in inputs}}
        else:
            continue
        records.append({"kind": message.kind, "role": message.role,
                        "text": (message.text if message.kind == "file_observation_error"
                                 else "init" if message.kind == "system" else ""),
                        "data": data, "created_at": message.created_at.isoformat()})
    return records


async def summarize(backend: Any, request: AgentRunRequest, messages: list[AgentMessage],
                    instruction: str, on_result: Callable[[dict[str, Any]], Awaitable[None]]
                    ) -> tuple[str, list[dict[str, Any]]]:
    """Summarize all chunks, then reduce them to one bounded handoff message."""
    results: list[dict[str, Any]] = []
    source = transcript_text(messages)
    if not source:
        raise ValueError("There is no dialogue to compress")
    summary_request = replace(
        request,
        member=request.member.model_copy(update={"max_turns": 2, "allowed_tools": [],
                                                  "permission_mode": "plan"}),
        session_key=f"{request.session_key}:summary", resume_session_id=None,
        recovery_prompt="", system_prompt=instruction, agent_name=None,
        mcp_servers={}, extra_allowed_tools=[], plugin_dirs=[], images=[],
        max_continuations=0, writable_roots=[], text_only=True,
    )
    # Each call has a deadline; no writes to the task until every chunk succeeds.
    for _round in range(8):
        summaries = []
        for offset in range(0, len(source), CHUNK_CHARS):
            parts: list[str] = []
            final = ""
            async with asyncio.timeout(180), aclosing(backend.run(replace(
                    summary_request, prompt=f"{instruction}\n\nTranscript:\n{source[offset:offset + CHUNK_CHARS]}",
            ))) as stream:
                async for event in stream:
                    if event.kind == "result":
                        final = event.text or final
                        results.append(event.data)
                        # Even a failed/empty summary was billed; keep the task's totals honest.
                        await on_result(event.data)
                    if event.kind == "error" or (event.kind == "result" and event.data.get("is_error")):
                        raise RuntimeError(event.text or "Context compression failed")
                    if event.kind == "text" and event.text:
                        parts.append(event.text)
            summary = (final or "\n".join(parts)).strip()
            if not summary:
                raise RuntimeError("Context compression returned an empty summary")
            summaries.append(summary)
        combined = "\n\n".join(summaries)
        if len(summaries) == 1 and len(combined) <= SUMMARY_CHARS:
            return combined, results
        if len(combined) >= len(source):
            raise RuntimeError("Context compression did not reduce the history")
        source = combined
    raise RuntimeError("Context compression could not produce a bounded summary")
