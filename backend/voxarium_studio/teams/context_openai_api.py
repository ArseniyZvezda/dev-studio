"""Read the project-owned retained OpenAI API context (§10.4, §10.6)."""
from __future__ import annotations

import json
import re
from threading import Lock
from uuid import UUID

from ..core.paths import ProjectPaths
from .backends.base import RuntimeSessionOwner
from .context_journals import MAX_JOURNAL_BYTES
from .context_sources import JournalContext, count, message_source_kind, prompt_sources, source

# Windows can reject an atomic replace while a reader has the file open.
api_history_io = Lock()


def _content(parts, content, role, origin, *, summary=False, detail=""):
    category = {"system": "system", "developer": "developer", "user": "task",
                "assistant": "history", "tool": "tools"}[role]
    kind = message_source_kind(role)
    if summary:
        category, kind = "summary", "compaction"
    if isinstance(content, list):
        for index, block in enumerate(content):
            field = {"text": "text", "output_text": "text", "refusal": "refusal"}.get(block["type"])
            if field:
                _content(parts, block[field], role, f"{origin}[{index}].{field}", summary=summary, detail=detail)
        return
    if isinstance(content, str) and content:
        parts.extend(prompt_sources(content, category, origin=origin, source_kind=kind,
                                    source_detail=detail, instructions=role in {"system", "developer"}))


def _call(parts, call, origin):
    parts.append(source("tools", call["arguments"], origin=origin + ".arguments",
                        source_kind="tool_call", source_detail=call["name"]))


def _response_output(parts, items, origin):
    for index, item in enumerate(items):
        field = f"{origin}[{index}]"
        match item["type"]:
            case "message":
                _content(parts, item["content"], item["role"], field + ".content")
            case "function_call":
                _call(parts, item, field)
            case "reasoning":
                for i, summary in enumerate(item.get("summary") or []):
                    if isinstance(text := summary.get("text"), str) and text:
                        parts.append(source("thinking", text, origin=f"{field}.summary[{i}].text",
                                            source_kind="thinking"))


def read_api_context(backend: str, session_id: str, owner: RuntimeSessionOwner) -> JournalContext | None:
    if not re.fullmatch(r"[0-9a-f]{32}", session_id):
        return None
    try:
        connection = backend.removeprefix("openai_api:")
        if str(UUID(connection)) != connection:
            return None
        data = ProjectPaths.for_project(owner.project_root).data
        path = data / "api-sessions" / connection / f"{session_id}.json"
        if path.resolve() != path:
            return None
        with api_history_io, path.open("rb") as stream:
            raw = stream.read(MAX_JOURNAL_BYTES + 1)
        if len(raw) > MAX_JOURNAL_BYTES:
            return None
        saved = json.loads(raw)
        viewer = saved["viewer"]
        if (saved["version"] != 1 or viewer["mode"] not in {"chat_completions", "responses"}
                or viewer["project_root"] != str(owner.project_root) or viewer["session_id"] != session_id
                or viewer["owner"] != {"kind": owner.owner_kind, "id": owner.owner_id}):
            return None
        parts = []
        _content(parts, viewer["system"], "system", "viewer.system")
        for index, message in enumerate(saved["messages"]):
            field = f"messages[{index}]"
            role = message["role"]
            if viewer["mode"] == "responses" and role == "assistant" and message.get("responses_output"):
                _response_output(parts, message["responses_output"], field + ".responses_output")
                continue
            _content(parts, message.get("content"), role, field + ".content",
                     summary=index == 0 and viewer["compacted"], detail=message.get("tool_call_id", ""))
            for i, call in enumerate(message.get("tool_calls", [])):
                _call(parts, call["function"], f"{field}.tool_calls[{i}].function")
        for index, tool in enumerate(viewer["tools"]):
            definition = tool["function"]
            parts.append(source("tool_definitions", json.dumps(definition, ensure_ascii=False),
                                origin=f"viewer.tools[{index}].function", representation="json",
                                source_kind="tool_definition", source_detail=definition["name"]))
        return JournalContext(parts, reported_tokens=count(viewer.get("reported_tokens")),
                              context_window=count(viewer["window"]), partial=True,
                              compacted=viewer["compacted"], journal_path=str(path))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return None
