"""Retained text fields from a Codex rollout (§10.4), including compacted histories."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from .context_sources import (
    JournalContext, bounded_detail, count, message_source_kind, prompt_sources, source, user_sources,
)

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


def _message_fields(item: dict, path: str) -> Iterator[tuple[str, str, str | None, str, bool]]:
    """Content metadata is positional and can be absent in older rollouts (§10.4)."""
    content = item.get("content")
    if not isinstance(content, list):
        for text, origin in _text_fields(content, path):
            yield text, origin, None, "", True
        return
    info = item.get("internal_chat_message_metadata_passthrough")
    kinds = info.get("content_item_kinds") if isinstance(info, dict) else None
    metadata = info.get("content_item_metadata") if isinstance(info, dict) else None
    for index, block in enumerate(content):
        kind = kinds[index] if isinstance(kinds, list) and len(kinds) == len(content) else None
        entry = metadata[index] if isinstance(metadata, list) and len(metadata) == len(content) else None
        provenance = entry.get("provenance") if isinstance(entry, dict) else None
        # With positional metadata, require the runtime's base field and exclude hooks.
        # Older messages can still match the model-attributed session header verbatim.
        model_field = (not isinstance(info, dict) or
                       (kind == "model.base_instructions" and isinstance(provenance, dict)
                        and provenance.get("type") == "harness" and provenance.get("hook") is False))
        detail = [kind] if isinstance(kind, str) and kind != "unknown" else []
        if isinstance(provenance, dict):
            if isinstance(provenance.get("type"), str):
                detail.append(provenance["type"])
            if provenance.get("type") == "tool" and isinstance(provenance.get("namespace"), str):
                detail.append(provenance["namespace"])
            if provenance.get("hook") is True:
                detail.append("hook")
        source_detail = bounded_detail(" · ".join(detail))
        for text, origin in _text_fields(block, f"{path}[{index}]"):
            yield text, origin, kind if isinstance(kind, str) else None, source_detail, model_field


def _same_base(text: str, base: str) -> bool:
    # A header can include a trailing newline that the message serializer dropped.
    # Normalize only for the comparison. The UI always receives the message's own field.
    return bool(base) and text.strip().replace("\r\n", "\n") == base.strip().replace("\r\n", "\n")


def _instruction_parts(text: str, role: str, path: str, base: str, detail: str) -> tuple[list[dict], bool]:
    if _same_base(text, base):
        return prompt_sources(text, "system", origin=path, instructions=True,
                              source_kind="base_instructions", source_detail=detail), True

    # Some serializers put a marked instruction section after the base in the same
    # field. Preserve the separator in the first slice and keep both source offsets.
    core = base.strip()
    if core and text.lstrip().startswith(core):
        suffix = text.lstrip()[len(core):]
        section = suffix.lstrip()
        if "\n" in suffix[:len(suffix) - len(section)] and _MARKED_SECTION.match(section):
            offset = len(text) - len(section)
            first = prompt_sources(text[:offset], "system", origin=f"{path}[0:{offset}]", instructions=True,
                                   source_kind="base_instructions", source_detail=detail)
            rest, _ = _instruction_parts(section, role, f"{path}[{offset}:{len(text)}]", "", detail)
            return first + rest, True

    for tag, category in _INSTRUCTION_WRAPPERS.items():
        if text.lstrip().startswith(tag) and text.rstrip().endswith("</" + tag[1:]):
            kind = "skill_listing" if category == "skills" else "tool_definition"
            return [source(category, text, origin=path, source_kind=kind, source_detail=detail)], False

    parts = prompt_sources(text, role, origin=path, instructions=True,
                           source_kind=message_source_kind(role), source_detail=detail)
    # A single scalar can hold both the base and a marked memory section. Only a
    # complete classified span may cover the header, never an arbitrary substring.
    has_base = False
    for part in parts:
        if part["category"] == role and _same_base(part["text"], base):
            part["category"] = "system"
            part["source_kind"] = "base_instructions"
            has_base = True
    return parts, has_base


def _structured(category: str, value: Any, path: str, label: str, source_kind: str, detail: str) -> dict:
    if isinstance(value, str):
        return source(category, value, label, origin=path, source_kind=source_kind, source_detail=detail)
    return source(category, json.dumps(value, ensure_ascii=False), label, origin=path,
                  representation="json", source_kind=source_kind, source_detail=detail)


def _item_sources(item: dict, path: str, base: str, model: str, studio_system: str,
                  calls: dict[str, str]) -> tuple[list[dict], bool]:
    kind = item.get("type")
    parts = []
    has_base = False
    if kind == "message":
        role = item.get("role")
        for text, origin, part_kind, detail, model_field in _message_fields(item, f"{path}.content"):
            if role in ("developer", "system"):
                # A single developer message carries model, Studio, and memory fields.
                # A generic override that repeats the model prompt is still a separate input.
                if part_kind == "memories.instructions":
                    parts.append(source("memory", text, origin=origin, source_kind="memory", source_detail=detail))
                    continue
                base_field = part_kind in (None, "unknown", "model.base_instructions")
                spans, matched = _instruction_parts(text, role, origin, base if base_field else "", detail)
                if matched and model and model_field:
                    for span in spans:
                        if span["category"] == "system" and _same_base(span["text"], base):
                            span["provided_by"] = "codex"
                            span["source_detail"] = model
                parts.extend(spans)
                has_base |= matched
            elif role == "user":
                parts.extend(user_sources(text, origin=origin, backend="codex", studio_system=studio_system,
                                          source_detail=detail))
            else:
                parts.append(source("tools" if role == "tool" else "history", text, origin=origin,
                                    source_kind=message_source_kind(role), source_detail=detail))
    elif kind == "additional_tools" and isinstance(item.get("tools"), list):
        for index, tool in enumerate(item["tools"]):
            if isinstance(tool, dict):
                name = tool.get("name") if isinstance(tool.get("name"), str) else ""
                parts.append(_structured("tool_definitions", tool, f"{path}.tools[{index}]",
                                         name, "tool_definition", name))
    elif kind in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
        key = {"function_call": "arguments", "custom_tool_call": "input"}.get(kind, "action")
        name = item.get("name") if isinstance(item.get("name"), str) else ""
        namespace = item.get("namespace")
        detail = f"{namespace}.{name}" if isinstance(namespace, str) and namespace and name else name or kind
        if isinstance(item.get("call_id"), str) and item["call_id"]:
            calls[item["call_id"]] = detail
        if item.get(key) is not None:
            parts.append(_structured("tools", item[key], f"{path}.{key}", name, "tool_call", detail))
    elif kind in ("function_call_output", "custom_tool_call_output"):
        call_id = item.get("call_id") if isinstance(item.get("call_id"), str) else ""
        for text, origin in _text_fields(item.get("output"), f"{path}.output"):
            parts.append(source("tools", text, call_id, origin=origin, source_kind="tool_result",
                                source_detail=calls.get(call_id, call_id)))
    elif kind in ("agent_message", "reasoning"):
        fields = ("summary", "content") if kind == "reasoning" else ("content",)
        category = "thinking" if kind == "reasoning" else "agents"
        author = item.get("author")
        detail = author if kind == "agent_message" and isinstance(author, str) else ""
        source_kind = "thinking" if kind == "reasoning" else "agent_message"
        for key in fields:
            for text, origin in _text_fields(item.get(key), f"{path}.{key}"):
                parts.append(source(category, text, origin=origin, source_kind=source_kind, source_detail=detail))
    return parts, has_base


def codex_context(rows: list[dict], *, studio_system: str = "") -> JournalContext:
    result = JournalContext()
    base = base_origin = ""
    model = base_detail = ""
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
                provenance = value.get("provenance")
                # The field name by itself also covers customized base instructions.
                # Use the journal's contemporaneous attribution, not today's model catalog.
                if isinstance(provenance, dict) and isinstance(provenance.get("type"), str):
                    base_detail = provenance["type"]
                    if base_detail == "model" and isinstance(provenance.get("model"), str):
                        model = bounded_detail(provenance["model"].strip())
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
                summary = source("summary", payload["message"], origin=f"{path}.message", source_kind="compaction")
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
    calls: dict[str, str] = {}
    for path, item in history:
        parts, matched = _item_sources(item, path, base, model, studio_system, calls)
        result.sources.extend(parts)
        has_base |= matched
    if base and not has_base:
        header = prompt_sources(base, "system", origin=base_origin, instructions=True,
                                source_kind="base_instructions", source_detail=model or base_detail)
        if model:
            for part in header:
                if part["category"] == "system":
                    part["provided_by"] = "codex"
        result.sources[:0] = header
    result.partial = not base or missing_history
    return result
