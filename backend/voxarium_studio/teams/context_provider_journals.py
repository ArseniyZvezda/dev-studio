"""Read the active Claude Code or Qwen transcript by source field (§10.4)."""
from __future__ import annotations

import json
from typing import Any, Iterator

from .context_sources import JournalContext, count, prompt_sources, source, user_sources


def _items(value: Any, origin: str) -> Iterator[tuple[Any, str]]:
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield item, f"{origin}[{index}]"


def _branch(rows: list[dict]) -> list[tuple[int, dict]]:
    """Follow transcript UUIDs. Diagnostic records can carry copies of a message."""
    eligible = [(i, row) for i, row in enumerate(rows) if not row.get("isSidechain")]
    indexed = {}
    for i, row in eligible:
        if isinstance(key := row.get("uuid"), str):
            indexed.setdefault(key, (i, row))
    if not indexed:
        return eligible  # Older flat recordings have no UUIDs.
    chain = []
    # Newer Qwen versions append standalone records for managing the recording.
    # They have their own UUIDs and can arrive after the last conversation turn.
    non_conversation = ("session_sources_snapshot", "session_artifact_event", "session_artifact_snapshot",
                        "managed_session_header_v1", "managed_session_event_v1", "managed_session_commit_v1",
                        "managed_session_snapshot_base_v1")
    key = next((row["uuid"] for _, row in reversed(eligible) if isinstance(row.get("uuid"), str)
                and not (row.get("type") == "system" and row.get("subtype") in non_conversation)), None)
    visited = set()
    while isinstance(key, str) and key in indexed:
        if key in visited:
            return []
        visited.add(key)
        i, row = indexed[key]
        chain.append((i, row))
        key = row.get("parentUuid")
    return chain[::-1]


def _field(parts: list, category: str, value: Any, origin: str, label: str = "") -> None:
    if isinstance(value, str):
        parts.append(source(category, value, label, origin=origin))
    elif value is not None:
        parts.append(source(category, json.dumps(value, ensure_ascii=False), label, origin=origin,
                            representation="json"))


def _category(message: dict) -> str:
    if message.get("role") in ("system", "developer"):
        return message["role"]
    return "task" if message.get("role") == "user" else "history"


def _claude_tool_result(parts: list, block: dict, origin: str) -> None:
    content = block.get("content")
    label = block.get("tool_use_id") or ""
    if isinstance(content, str):
        _field(parts, "tools", content, origin, label)
    for item, path in _items(content, origin):
        if isinstance(item, dict) and item.get("type") == "text":
            _field(parts, "tools", item.get("text"), path + ".text", label)
        elif isinstance(item, dict) and item.get("type") == "tool_reference":
            _field(parts, "tools", item.get("tool_name"), path + ".tool_name", label)


def _claude_message(parts: list, message: dict, origin: str, category: str) -> None:
    content = message.get("content")
    origin += ".content"
    if isinstance(content, str):
        parts.extend(prompt_sources(content, category, origin=origin, instructions=category in ("system", "developer")))
        return
    for block, path in _items(content, origin):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("tool_use", "server_tool_use"):
            _field(parts, "tools", block.get("input"), path + ".input", block.get("name") or "")
        elif kind == "tool_result":
            _claude_tool_result(parts, block, path + ".content")
        elif kind == "thinking":
            _field(parts, "thinking", block.get("thinking"), path + ".thinking")
        elif kind == "text" and isinstance(block.get("text"), str):
            parts.extend(prompt_sources(block["text"], category, origin=path + ".text",
                                        instructions=category in ("system", "developer")))


def _claude_attachment(parts: list, attachment: dict, origin: str) -> None:
    kind = attachment.get("type")
    if kind == "prompt_snapshot":
        if isinstance(attachment.get("cliPrefix"), str):
            _field(parts, "system", attachment["cliPrefix"], origin + ".cliPrefix")
        for text, path in _items(attachment.get("systemPrompt"), origin + ".systemPrompt"):
            if isinstance(text, str):
                parts.extend(prompt_sources(text, "system", origin=path, instructions=True))
        for tool, path in _items(attachment.get("tools"), origin + ".tools"):
            if isinstance(tool, dict):
                _field(parts, "tool_definitions", tool, path, tool.get("name") or "")
    elif kind == "instructions":
        for item, path in _items(attachment.get("files"), origin + ".files"):
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                _field(parts, "memory", item["content"], path + ".content", item.get("path") or "")
    elif kind == "nested_memory":
        item = attachment.get("content")
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            # rawContent and displayPath describe the disk version. content is what
            # Claude retained when the attachment was made.
            _field(parts, "memory", item["content"], origin + ".content.content", attachment.get("path") or "")
    elif kind == "skill_listing" and isinstance(attachment.get("content"), str):
        _field(parts, "skills", attachment["content"], origin + ".content")


def claude_context(rows: list[dict]) -> JournalContext:
    result = JournalContext()
    chain = _branch(rows)
    boundary = next((n for n in range(len(chain) - 1, -1, -1)
                     if chain[n][1].get("subtype") == "compact_boundary"), None)
    if boundary is not None:
        result.compacted = True
        chain = chain[boundary:]
    # prompt_snapshot is a full replacement. A parallel branch's snapshot is not
    # evidence about this one, even when it appears later in the file.
    snapshots = [(i, row) for i, row in chain if isinstance(row.get("attachment"), dict)
                 and row["attachment"].get("type") == "prompt_snapshot"]
    latest_snapshot = snapshots[-1][0] if snapshots else None
    for i, row in chain:
        path = f"journal[{i}]"
        attachment = row.get("attachment")
        if isinstance(attachment, dict) and (attachment.get("type") != "prompt_snapshot" or i == latest_snapshot):
            _claude_attachment(result.sources, attachment, path + ".attachment")
        if (row.get("type") not in (None, "assistant", "user", "system")
                or row.get("isVisibleInTranscriptOnly") or row.get("isApiErrorMessage")):
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        category = "summary" if row.get("isCompactSummary") else _category(message)
        _claude_message(result.sources, message, path + ".message", category)
        usage = message.get("usage")
        if isinstance(usage, dict):
            counts = [count(usage.get(k)) for k in
                      ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
            if any(c is not None for c in counts):
                result.reported_tokens = sum(c or 0 for c in counts)
    return result


_QWEN_MEMORY = "qwen-code:memory-catalog-snapshot"


def _qwen_catalog(block: dict) -> bool:
    metadata = block.get("partMetadata")
    return isinstance(metadata, dict) and metadata.get(_QWEN_MEMORY) is True and isinstance(block.get("text"), str)


def _qwen_message(parts: list, message: dict, origin: str, category: str,
                  last_catalog: str | None, studio_system: str) -> str | None:
    for block, path in _items(message.get("parts"), origin + ".parts"):
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("functionCall"), dict):
            call = block["functionCall"]
            _field(parts, "tools", call.get("args"), path + ".functionCall.args", call.get("name") or "")
        elif isinstance(block.get("functionResponse"), dict):
            reply = block["functionResponse"]
            _field(parts, "tools", reply.get("response"), path + ".functionResponse.response", reply.get("name") or "")
        elif isinstance(block.get("text"), str):
            group = category
            if _qwen_catalog(block):
                group = "memory"
                last_catalog = block["text"]
            if block.get("thought"):
                group = "thinking"
            if group == "task":
                parts.extend(user_sources(block["text"], origin=path + ".text", backend="qwen",
                                          studio_system=studio_system))
            else:
                parts.extend(prompt_sources(block["text"], group, origin=path + ".text",
                                            instructions=group in ("system", "developer")))
    return last_catalog


def qwen_context(rows: list[dict], *, studio_system: str = "") -> JournalContext:
    result = JournalContext()
    last_catalog = None
    for i, row in _branch(rows):
        path = f"journal[{i}]"
        kind = row.get("subtype")
        if kind == "chat_compression":
            result.compacted = True
            result.sources.clear()
            result.reported_tokens = None
            last_catalog = None
            payload = row.get("systemPayload")
            if isinstance(payload, dict):
                for message, origin in _items(payload.get("compressedHistory"), path + ".systemPayload.compressedHistory"):
                    if isinstance(message, dict):
                        last_catalog = _qwen_message(result.sources, message, origin, _category(message),
                                                     last_catalog, studio_system)
        elif kind not in ("realtime_message", "code_mode_tool_result"):
            message = row.get("message")
            if isinstance(message, dict) and kind == "memory_catalog_snapshot":
                # Qwen emits snapshot receipts on resume but only appends a catalog
                # to model history if its identified contents changed.
                blocks = message.get("parts")
                block = blocks[0] if isinstance(blocks, list) and blocks else None
                if isinstance(block, dict) and _qwen_catalog(block) and block["text"] != last_catalog:
                    last_catalog = block["text"]
                    _field(result.sources, "memory", last_catalog, path + ".message.parts[0].text")
            elif isinstance(message, dict) and row.get("type") != "system":
                last_catalog = _qwen_message(result.sources, message, path + ".message", _category(message),
                                             last_catalog, studio_system)
        usage = row.get("usageMetadata")
        if isinstance(usage, dict) and (reported := count(usage.get("promptTokenCount"))) is not None:
            result.reported_tokens = reported
        result.context_window = count(row.get("contextWindowSize")) or result.context_window
    return result
