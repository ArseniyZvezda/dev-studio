"""Context Viewer source records (§10.4). Classification changes boundaries, never text."""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JournalContext:
    sources: list[dict[str, Any]] = field(default_factory=list)
    reported_tokens: int | None = None
    context_window: int | None = None
    partial: bool = True
    compacted: bool = False
    journal_path: str | None = None


def count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def message_source_kind(role: Any) -> str:
    if role in ("assistant", "model"):
        return "assistant_message"
    if role in ("user", "system", "developer"):
        return f"{role}_message"
    return "tool_result" if role == "tool" else "message"


def bounded_detail(detail: str) -> str:
    """Display metadata from a journal must not become another unbounded text preview (§10.4)."""
    return detail if len(detail) <= 1024 else detail[:1023] + "…"


def source(category: str, text: str, label: str = "", *, origin: str = "",
           representation: str = "text", provided_by: str | None = None,
           source_kind: str = "", source_detail: str = "") -> dict[str, Any]:
    record = {"category": category, "label": label, "text": text, "tokens": (len(text) + 3) // 4,
              "origin": origin, "representation": representation}
    if provided_by:
        record["provided_by"] = provided_by
    if source_kind:
        record["source_kind"] = source_kind
    if source_detail:
        record["source_detail"] = bounded_detail(source_detail)
    return record


_REQUIRED = re.compile(r"^--- BEGIN REQUIRED (SKILL|DOCUMENT): ([^\r\n]+) ---\r?$")
_MEMORY = re.compile(r"^#{1,2} Memory[ \t]*$")
_SECTION = re.compile(r"^#{1,2} ")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_MEMORY_TAGS = {"<memory_instructions>", "<memory_context>", "<memory_summary>"}
_AGENTS_HEADING = "# AGENTS.md instructions for "


def prompt_sources(text: str, category: str = "task", *, origin: str = "",
                   instructions: bool = False, source_kind: str = "", source_detail: str = "") -> list[dict[str, Any]]:
    """Slice only marked material inside one scalar. Offsets refer to the decoded field.

    Assistant prose and tool results are opaque. In particular, a file read from disk
    may *quote* an entire developer message without becoming part of the system prompt.
    """
    def whole():
        return [source(category, text, origin=origin, source_kind=source_kind, source_detail=source_detail)]

    if category != "task" and not instructions:
        return whole()
    markers = ("--- BEGIN REQUIRED ",) if category == "task" else ()
    if instructions:
        markers += ("# Memory", "<memory_", "# AGENTS.md", "<environment_context>", "<user_instructions>")
    if not any(marker in text for marker in markers):
        return whole()

    # index by physical line without universal-newline conversion
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    bare = [line.rstrip("\r\n") for line in lines]
    first = next((i for i, line in enumerate(bare) if line.strip()), -1)
    closing_lines: dict[str, list[int]] = {}
    headings = []
    start_marker = "========= MEMORY_SUMMARY BEGINS ========="
    stop_marker = "========= MEMORY_SUMMARY ENDS ========="
    for n, line in enumerate(bare):
        if line.startswith(("--- END REQUIRED ", "</")) or line == stop_marker:
            closing_lines.setdefault(line, []).append(n)
        if _SECTION.match(line) or line == start_marker:
            headings.append(n)
    spans = []
    i = 0
    fence = ""
    while i < len(lines):
        line = bare[i]
        opening = _FENCE.match(line)
        if opening:
            mark = opening[1]
            if not fence:
                fence = mark
            elif mark.startswith(fence) and not line[opening.end():].strip():
                fence = ""
        if fence or opening:
            i += 1
            continue

        end = None
        label = ""
        detail = ""
        closing = ""
        required = _REQUIRED.fullmatch(line) if category == "task" else None
        if required:
            kind, label = required.groups()
            group = "skills" if kind == "SKILL" else "documents"
            span_kind = "required_skill" if kind == "SKILL" else "required_document"
            detail = label
            # §10.10: only the dispatched skill envelope identifies its instruction file.
            if (kind == "SKILL" and i + 2 < len(bare) and bare[i + 1].startswith("Skill directory: ")
                    and bare[i + 2].startswith("Instructions: ")):
                detail = bare[i + 2].removeprefix("Instructions: ").strip() or label
            closing = f"--- END REQUIRED {kind}: {label} ---"
        elif instructions and category == "task" and i == first and line.startswith(_AGENTS_HEADING):
            group, closing = "memory", "</INSTRUCTIONS>"
            span_kind, detail = "agents_md", line.removeprefix(_AGENTS_HEADING).strip()
        elif instructions and category == "task" and line in {"<environment_context>", "<user_instructions>"} and (
            i == first or (spans and not text[spans[-1][1]:offsets[i]].strip())
        ):
            group = "environment" if line == "<environment_context>" else "memory"
            span_kind = "environment" if group == "environment" else "user_instructions"
            closing = "</" + line[1:]
        elif instructions and category in {"system", "developer"} and line in _MEMORY_TAGS:
            group, closing = "memory", "</" + line[1:]
            span_kind, detail = "memory", line[1:-1]
        elif instructions and category in {"system", "developer"} and _MEMORY.fullmatch(line):
            group = span_kind = "memory"
            # Codex emits a heading, operating instructions, and paired delimiters.
            # The body may contain arbitrary Markdown, including other headings.
            pos = bisect_right(headings, i)
            boundary = headings[pos] if pos < len(headings) else len(lines)
            if boundary < len(lines) and bare[boundary] == start_marker:
                closing = stop_marker
            elif i == first:
                end = boundary
        if closing:
            candidates = closing_lines.get(closing, ())
            pos = bisect_right(candidates, i)
            end = candidates[pos] + 1 if pos < len(candidates) else None
            # Codex may already have truncated its standalone memory field; the
            # missing terminator is not permission to invent the rest of the body.
            if end is None and closing == stop_marker and i == first:
                end = len(lines)
        if end is not None:
            spans.append((offsets[i], offsets[end], group, label, span_kind, detail))
            i = end
        else:
            i += 1

    if not spans:
        return whole()
    pieces = []
    def emit(start, end, group, label="", kind=source_kind, detail=source_detail):
        locator = f"{origin}[{start}:{end}]" if origin and (start or end != len(text)) else origin
        pieces.append(source(group, text[start:end], label, origin=locator, source_kind=kind, source_detail=detail))
    cursor = 0
    for start, end, group, label, span_kind, detail in spans:
        if cursor < start:
            emit(cursor, start, category)
        emit(start, end, group, label, span_kind, detail)
        cursor = end
    if cursor < len(text):
        emit(cursor, len(text), category)
    return pieces


def user_sources(text: str, *, origin: str, backend: str, studio_system: str = "",
                 source_detail: str = "") -> list[dict]:
    """Identify the two adapters' overflow envelopes only with independent dispatch evidence."""
    if studio_system and backend in {"codex", "qwen"}:
        if backend == "codex":
            prefix = f"<instructions>\n{studio_system.strip()}\n</instructions>\n\n"
            category = "developer"
        else:
            prefix = f"Studio instructions:\n{studio_system}\n\nUser message:\n"
            category = "system"
        if text.startswith(prefix):
            end = len(prefix)
            # The hint is only a comparison. Every visible character is sliced from
            # the actual journal scalar, including the adapter's envelope and spacing.
            parts = prompt_sources(text[:end], category, origin=f"{origin}[0:{end}]", instructions=True,
                                   source_kind="studio_system")
            if end < len(text):
                parts.extend(prompt_sources(text[end:], origin=f"{origin}[{end}:{len(text)}]",
                                            source_kind="user_message", source_detail=source_detail))
            return parts
    return prompt_sources(text, origin=origin, instructions=backend == "codex",
                          source_kind="user_message", source_detail=source_detail)
