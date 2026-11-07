"""Retained text fields from a Codex rollout (§10.4), including compacted histories."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from .context_sources import JournalContext, count, prompt_sources, source, user_sources

_INSTRUCTION_WRAPPERS = {"<skills_instructions>": "skills", "<tools>": "tool_definitions"}
_MARKED_SECTION = re.compile(
    r"(?:#{1,2} Memory[ \t]*(?:\r?\n|$)|<(?:memory_(?:instructions|context|summary)|skills_instructions|tools)>)"
)


def _text_fields(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Content blocks have independent boundaries. Ignore images and encrypted fields."""
    if isinstance(value, str):
        yield value, path
        return
    if isinstance(value, list):
        for index, block in enumerate(value):
            yield from _text_fields(block, f"{path}[{index}]")
    elif isinstance(value, dict) and isinstance(value.get("text"), str):
        yield value["text"], f"{path}.text"


def _same_base(text: str, base: str) -> bool:
    # A header can include a trailing newline that the message serializer dropped.
    # Normalize only for the comparison. The UI always receives the message's own field.
    return bool(base) and text.strip().replace("\r\n", "\n") == base.strip().replace("\r\n", "\n")


def _instruction_parts(text: str, role: str, path: str, base: str) -> tuple[list[dict], bool]:
    if _same_base(text, base):
        return prompt_sources(text, "system", origin=path, instructions=True), True

    # Some serializers put a marked instruction section after the base in the same
    # field. Preserve the separator in the first slice and keep both source offsets.
    core = base.strip()
    if core and text.lstrip().startswith(core):
        suffix = text.lstrip()[len(core):]
        section = suffix.lstrip()
        if "\n" in suffix[:len(suffix) - len(section)] and _MARKED_SECTION.match(section):
            offset = len(text) - len(section)
            first = prompt_sources(text[:offset], "system", origin=f"{path}[0:{offset}]", instructions=True)
            rest, _ = _instruction_parts(section, role, f"{path}[{offset}:{len(text)}]", "")
            return first + rest, True

    for tag, category in _INSTRUCTION_WRAPPERS.items():
        if text.lstrip().startswith(tag) and text.rstrip().endswith("</" + tag[1:]):
            return [source(category, text, origin=path)], False

    parts = prompt_sources(text, role, origin=path, instructions=True)
    # A single scalar can hold both the base and a marked memory section. Only a
    # complete classified span may cover the header, never an arbitrary substring.
    has_base = False
    for part in parts:
        if part["category"] == role and _same_base(part["text"], base):
            part["category"] = "system"
            has_base = True
    return parts, has_base


def _structured(category: str, value: Any, path: str, label: str = "") -> dict:
    if isinstance(value, str):
        return source(category, value, label, origin=path)
    return source(category, json.dumps(value, ensure_ascii=False), label, origin=path,
                  representation="json")


def _item_sources(item: dict, path: str, base: str, studio_system: str) -> tuple[list[dict], bool]:
    kind = item.get("type")
    parts = []
    has_base = False
    if kind == "message":
        role = item.get("role")
        for text, origin in _text_fields(item.get("content"), f"{path}.content"):
            if role in ("developer", "system"):
                spans, matched = _instruction_parts(text, role, origin, base)
                parts.extend(spans)
                has_base |= matched
            elif role == "user":
                parts.extend(user_sources(text, origin=origin, backend="codex", studio_system=studio_system))
            else:
                parts.append(source("tools" if role == "tool" else "history", text, origin=origin))
    elif kind == "additional_tools" and isinstance(item.get("tools"), list):
        for index, tool in enumerate(item["tools"]):
            if isinstance(tool, dict):
                parts.append(_structured("tool_definitions", tool, f"{path}.tools[{index}]",
                                         str(tool.get("name") or "")))
    elif kind in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
        key = {"function_call": "arguments", "custom_tool_call": "input"}.get(kind, "action")
        if item.get(key) is not None:
            parts.append(_structured("tools", item[key], f"{path}.{key}", str(item.get("name") or "")))
    elif kind in ("function_call_output", "custom_tool_call_output"):
        for text, origin in _text_fields(item.get("output"), f"{path}.output"):
            parts.append(source("tools", text, str(item.get("call_id") or ""), origin=origin))
    elif kind in ("agent_message", "reasoning"):
        fields = ("summary", "content") if kind == "reasoning" else ("content",)
        category = "thinking" if kind == "reasoning" else "agents"
        for key in fields:
            for text, origin in _text_fields(item.get(key), f"{path}.{key}"):
                parts.append(source(category, text, origin=origin))
    return parts, has_base


def codex_context(rows: list[dict], *, studio_system: str = "") -> JournalContext:
    result = JournalContext()
    base = base_origin = ""
    has_header = missing_history = False
    history: list[tuple[str, dict]] = []
    seen: dict[tuple[str, str], dict] = {}
    summary = None

    def retain(item: dict, path: str):
        # Ignore the repeated record, never a different field that quotes its text.
        kind, identity = item.get("type"), item.get("id")
        if isinstance(kind, str) and isinstance(identity, str) and identity:
            key = (kind, identity)
            if seen.get(key) == item:
                return
            seen[key] = item
        history.append((path, item))

    for index, row in enumerate(rows):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = row.get("type")
        path = f"{kind}[{index}].payload"
        if kind == "session_meta" and not has_header:
            # Forks can append the parent's header after this session's own header.
            has_header = True
            value = payload.get("base_instructions")
            base_origin = f"{path}.base_instructions"
            if isinstance(value, dict):
                value = value.get("text")
                base_origin += ".text"
            if isinstance(value, str):
                base = value
            result.context_window = count(payload.get("context_window"))
        elif kind == "response_item":
            retain(payload, path)
        elif kind == "compacted":
            result.compacted = True
            result.reported_tokens = None
            history.clear()
            seen.clear()
            summary = None
            replacement = payload.get("replacement_history")
            missing_history = not isinstance(replacement, list)
            if isinstance(replacement, list):
                for position, item in enumerate(replacement):
                    if isinstance(item, dict):
                        retain(item, f"{path}.replacement_history[{position}]")
            elif isinstance(payload.get("message"), str):
                summary = source("summary", payload["message"], origin=f"{path}.message")
        elif kind == "token_usage_record":
            usage = payload.get("usage")
            if isinstance(usage, dict) and (tokens := count(usage.get("input_tokens"))) is not None:
                result.reported_tokens = tokens
        elif kind == "event_msg" and payload.get("type") == "token_count":
            # Other event_msg records mirror response_items; they are never model input.
            info = payload.get("info")
            if isinstance(info, dict):
                usage = info.get("last_token_usage")
                if isinstance(usage, dict) and (tokens := count(usage.get("input_tokens"))) is not None:
                    result.reported_tokens = tokens
                result.context_window = count(info.get("model_context_window")) or result.context_window

    has_base = False
    if summary is not None:
        result.sources.append(summary)
    for path, item in history:
        parts, matched = _item_sources(item, path, base, studio_system)
        result.sources.extend(parts)
        has_base |= matched
    if base and not has_base:
        result.sources[:0] = prompt_sources(base, "system", origin=base_origin, instructions=True)
    result.partial = not base or missing_history
    return result
