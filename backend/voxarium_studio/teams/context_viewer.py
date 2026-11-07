"""Source inventory, not an invented provider token breakdown (§10.4).

Persist what Studio actually dispatched, rather than re-reading today's settings as
historical input. Providers do not expose their complete rendered request; text sizes
are estimates and the unexplained part of reported occupancy stays unattributed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ..models.base import utcnow
from ..models.task_context import ChatSessionContext, SessionContext, TaskContext
from .backends.base import AgentRunRequest
from .context_sources import JournalContext, count, prompt_sources, source


async def capture_request(db, owner_id: str, request: AgentRunRequest, *, input_at: datetime | None = None) -> None:
    """One row per task or chat; resumes retain the original startup materials (§10.4, §10.9)."""
    # Stopping while SQLite is flushing must release its writer before the caller
    # writes the cancellation to the transcript on another connection.
    write = asyncio.create_task(_save_request(db, owner_id, request, input_at))
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        await write
        raise


async def _save_request(db, owner_id: str, request: AgentRunRequest, input_at: datetime | None) -> None:
    async with db.session() as session:
        is_chat = request.task is None
        previous = await session.get(ChatSessionContext if is_chat else TaskContext, owner_id)
        fresh = (not request.resume_session_id or previous is None
                 or bool(previous.backend and previous.backend != request.member.agent_backend))
        row = previous or (ChatSessionContext(chat_id=owner_id) if is_chat else TaskContext(task_id=owner_id))
        if fresh:
            # On the first captured resume the raw user message is already in the chat
            # transcript. It belongs to this prompt, not to the unsnapshotted history.
            row.started_at = input_at or utcnow()
            row.partial = bool(request.resume_session_id)
        parts = [] if fresh else [part for part in row.sources if part["category"] != "system"]
        # Both Codex developer_instructions and Claude's appended system prompt are
        # supplied again on resume; required materials inside the first user turn stay.
        # Store raw fields. Only the read path classifies them, so updated readers
        # can inspect earlier dispatches without rewriting either field (§10.4).
        key = "studio.request[" + utcnow().isoformat() + "]"
        if request.system_prompt:
            parts.insert(0, source("system", request.system_prompt, origin=key + ".system_prompt"))
        parts.append(source("task", request.prompt, origin=key + ".prompt"))
        row.sources = parts
        row.backend = request.member.agent_backend
        row.captured_at = utcnow()
        session.add(row)


def _snapshot_sources(snapshot: SessionContext) -> list[dict]:
    parts = []
    for index, item in enumerate(snapshot.sources):
        origin = item.get("origin") or f"studio.snapshot.sources[{index}].text"
        category = item["category"]
        # Previous snapshots stored required materials as separate entries. Keep
        # those boundaries; reconstructing the earlier request would be guesswork.
        if category in {"system", "task"}:
            parts.extend(prompt_sources(item["text"], category, origin=origin, instructions=category == "system"))
        else:
            parts.append({**item, "origin": origin})
    return parts


def retained_context(snapshot: SessionContext | None, messages: Sequence, journal: JournalContext | None = None,
                     *, backend: str = "") -> tuple[list[dict], dict]:
    """Use one evidence source per view. A journal is never amended from the Studio timeline."""
    summary_kinds = {"compressed summary", "context_summary", "producer_compacted"}
    summary = next((m for m in reversed(messages) if m.kind in summary_kinds
                    and not m.data.get("parent_tool_use_id")), None)
    if snapshot and summary and summary.created_at > snapshot.captured_at:
        snapshot = journal = None  # A manual compaction invalidated the captured session.
    captured_at = snapshot.captured_at.isoformat() if snapshot else None
    if journal is not None:
        return journal.sources, {"reported": journal.reported_tokens, "window": journal.context_window,
                                 "origin": f"{backend}_journal", "partial": journal.partial,
                                 "has_studio_view": snapshot is not None}

    cutoff = snapshot.started_at if snapshot and not snapshot.partial else summary.created_at if summary else None
    parts = _snapshot_sources(snapshot) if snapshot else []
    reported = window = None
    for message in reversed(messages):
        if cutoff and message.created_at < cutoff:
            break
        if message.data.get("parent_tool_use_id"):
            continue
        if message.kind in summary_kinds:
            break
        if message.kind == "system" and message.text == "usage_update":
            reported = count(message.data.get("context_tokens"))
            window = count(message.data.get("context_window"))
            break
        if message.role == "assistant" and message.kind != "result":
            usage = message.data.get("usage")
            if isinstance(usage, dict):
                counts = [count(usage.get(key)) for key in
                          ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
                if any(count is not None for count in counts):
                    reported = sum(count or 0 for count in counts)
                    break
    for message in messages:
        if (cutoff and message.created_at < cutoff) or message.data.get("parent_tool_use_id"):
            continue
        category = None
        if message.kind in summary_kinds:
            # On a fresh dispatch this summary is already embedded in request.prompt.
            if not snapshot:
                category = "summary"
        elif message.kind in {"tool_use", "tool_result"}:
            category = "tools"
        elif message.kind == "thinking":
            category = "thinking"
        elif message.kind == "text" and message.role == "assistant":
            category = "history"
        elif (message.kind == "text" and message.role in {"user", "human"}
              and (not snapshot or snapshot.partial and message.created_at < snapshot.started_at)):
            # Messages before the first captured resume are historical evidence,
            # not additional fields of the captured dispatch.
            category = "task" if message.role == "user" and snapshot is None else "history"
        elif not snapshot and message.kind in {"note", "review", "summary"}:
            category = "history"
        if category:
            locator = f"studio.messages[{message.id}]"
            name = message.data.get("name", "") if category == "tools" else ""
            if message.kind == "tool_use" and "input" in message.data:
                # The label is metadata, not a prefix of the tool's arguments.
                value = message.data["input"]
                is_text = isinstance(value, str)
                parts.append(source(category, value if is_text else json.dumps(value, ensure_ascii=False),
                                    name or message.text, origin=locator + ".data.input",
                                    representation="text" if is_text else "json"))
            else:
                parts.append(source(category, message.text, name, origin=locator + ".text"))
    return parts, {"reported": reported, "window": window, "origin": "studio",
                   "partial": snapshot is None or snapshot.partial,
                   "captured_at": captured_at, "has_studio_view": snapshot is not None}


def inventory(snapshot: SessionContext | None, messages: list, journal=None, *, backend: str = "") -> dict[str, Any]:
    parts, metrics = retained_context(snapshot, messages, journal, backend=backend)
    return present(parts, **metrics)


def fragment_key(part: dict) -> str:
    """Guard a deferred read against a different record occupying the same position."""
    digest = hashlib.sha256()
    for key in ("origin", "category", "label", "representation", "text"):
        value = part.get(key, "").encode("utf-8", errors="surrogatepass")
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def present(parts: list[dict], reported: int | None, window: int | None, *,
            origin: str, partial: bool, captured_at: str | None = None, basis: str = "estimated",
            has_studio_view: bool = False) -> dict[str, Any]:
    estimated = sum(part["tokens"] for part in parts)
    # Size the blocks from the complete text, but keep opening a long-running session
    # cheap for the browser. The preview budget never changes its token estimate.
    budget = 512_000
    previews = []
    for index, part in enumerate(parts):
        text = part["text"]
        # The explicit live inspection is a one-time report; keep its supplied
        # metadata readable without repeating a provider call when a tile is opened.
        if basis != "runtime":
            text = text[:min(32_000, budget)]
            budget -= len(text)
        shortened = len(text) < len(part["text"])
        preview = {**part, "id": str(index), "text": text, "truncated": shortened}
        if shortened:
            preview["text_key"] = fragment_key(part)
        previews.append(preview)
    # Accounting is a display row, never appended to the cached source inventory.
    if reported is not None and reported > estimated:
        previews.append({"category": "unattributed", "label": "", "text": "", "tokens": reported - estimated,
                         "id": "unattributed", "truncated": False})
    return {
        "sources": previews, "has_studio_view": has_studio_view,
        "reported_tokens": reported, "context_window": window or None,
        "estimated_tokens": estimated,
        "captured_at": captured_at, "partial": partial, "origin": origin, "basis": basis,
    }


def _native_fragments(category: str, item: dict, index: int, details: dict) -> list[dict]:
    """Only break down a runtime count if its loaded records account for it exactly."""
    def metadata(row, label, tokens, path):
        return {**source(category, json.dumps(row, ensure_ascii=False, indent=2), label,
                         origin="runtime." + path, representation="metadata"), "tokens": tokens}

    tokens = item["tokens"]
    if len(details) == 1:
        key, records = next(iter(details.items()))
        if isinstance(records, dict):
            return [metadata(records, item["name"], tokens, key)]
        if isinstance(records, list) and all(isinstance(row, dict) for row in records):
            loaded = [(i, row) for i, row in enumerate(records)
                      if row.get("isLoaded") is not False and not row.get("isDeferred")]
            counts = [count(row.get("tokens")) for _, row in loaded]
            if None not in counts and sum(counts) == tokens:
                return [metadata(row, row.get("name") or row.get("path") or row.get("agentType") or "",
                                 row["tokens"], f"{key}[{i}]") for i, row in loaded]
    # No defensible per-record counts. The category itself is one original SDK
    # record: don't manufacture a new document out of several detail collections.
    return [metadata(item, item["name"], tokens, f"categories[{index}]")]


def native_inventory(report: dict) -> dict[str, Any]:
    """Claude's /context categories are authoritative; do not add our estimates again."""
    total = count(report.get("totalTokens"))
    if total is None or not isinstance(report.get("categories"), list):
        raise ValueError("The runtime returned no context breakdown")
    parts = []
    for index, item in enumerate(report["categories"]):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = item["name"]
        key = re.sub(r"[^a-z]", "", name.lower())
        tokens = count(item.get("tokens"))
        if not tokens or item.get("isDeferred") or any(s in key for s in ("freespace", "autocompact", "buffer", "available")):
            continue
        category = "runtime"
        detail_keys: tuple[str, ...] = ()
        if "systemprompt" in key:
            category, detail_keys = "system", ("systemPromptSections",)
        elif "tool" in key:
            category = "tool_definitions"
            detail_keys = ("mcpTools",) if "mcp" in key else ("systemTools",)
        elif "memor" in key:
            category, detail_keys = "memory", ("memoryFiles",)
        elif "skill" in key or "command" in key:
            category, detail_keys = "skills", ("skills", "slashCommands")
        elif "agent" in key:
            category, detail_keys = "agents", ("agents",)
        elif "message" in key or "conversation" in key:
            category, detail_keys = "history", ("messageBreakdown",)
        details = {key: report[key] for key in detail_keys if report.get(key)}
        parts.extend(_native_fragments(category, item, index, details))
    if sum(part["tokens"] for part in parts) > total:
        raise ValueError("The runtime context categories exceed its reported total")
    return present(parts, total, count(report.get("rawMaxTokens")) or count(report.get("maxTokens")),
                   origin="claude_code_runtime", partial=False, captured_at=utcnow().isoformat(), basis="runtime")
