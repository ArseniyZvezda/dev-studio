"""Read the selected backend session, rather than Studio's shortened timeline (§10.4).

These are local estimates of retained text. Journal formats are not provider request
dumps: encrypted reasoning, images and undisclosed tool schemas remain unaccounted for.
Never start or resume an agent merely to inspect its context.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..core.project_scope import canonical_root
from .context_codex import _text_fields, codex_context
from .context_provider_journals import _branch, _qwen_catalog, claude_context, qwen_context
from .context_sources import JournalContext

MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024

# A captured, random per-owner/session dispatch receipt, not a cwd or SID guess (§5.3.2).
DISPATCH_PREFIX = "[voxarium-studio-dispatch:"


@dataclass(frozen=True)
class NativeJournalBinding:
    project_root: Path
    owner_kind: str
    owner_id: str
    workdir: Path
    token: str
    dispatches: tuple[str, ...]
    generation: str = ""


def dispatch_marker(token: str) -> str:
    return f"{DISPATCH_PREFIX}{token}]\n"


def dispatch_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=8)
def _read(path: str, backend: str, mtime: int, size: int, studio_system: str = "",
          workdir: str | None = None, binding: NativeJournalBinding | None = None,
          session_id: str | None = None) -> JournalContext | None:
    if size > MAX_JOURNAL_BYTES:
        return None
    rows = []
    with Path(path).open("rb") as stream:
        consumed = 0
        while line := stream.readline(MAX_LINE_BYTES + 1):
            consumed += len(line)
            if len(line) > MAX_LINE_BYTES or consumed > MAX_JOURNAL_BYTES:
                return None
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                # The final record may still be in flight; internal corrupt records
                # make reconstruction uncertain, so fall back to Studio in that case.
                if not line.endswith(b"\n"):
                    break
                return None
            rows.append(row if isinstance(row, dict) else {})  # Keep addresses aligned with JSONL line indexes.
    if workdir is not None and not _matches_workdir(rows, backend, Path(workdir)):
        return None
    omitted_startup = False
    if binding is not None:
        genesis = _matches_dispatch(rows, backend, binding, session_id)
        if genesis is None:
            return None
        # Native startup instructions are not owner receipts. Do not publish an
        # unproven developer/system prelude merely because the user genesis is
        # ours. Preserve JSONL addresses rather than shifting the remaining rows.
        omitted_startup = any(rows[:genesis])
        rows = [{} for _ in range(genesis)] + rows[genesis:]
    if backend == "claude_code":
        result = claude_context(rows)
    else:
        reader = codex_context if backend == "codex" else qwen_context
        result = reader(rows, studio_system=studio_system)
    result.journal_path = path
    result.partial = result.partial or omitted_startup
    return result


def _matches_dispatch(rows: list[dict], backend: str, binding: NativeJournalBinding,
                      session_id: str | None) -> int | None:
    """The first primary user input must be this owner's captured dispatch.

    Check raw history, not only the retained branch: another owner's receipt in
    an abandoned/compacted turn still makes the file unsafe. Tool results, quoted
    markers and arbitrary metadata are not dispatch receipts (§5.3.2).
    """
    found = False
    genesis = None
    observed = set()
    # Native compression may retain messages only inside replacement histories.
    # Keep their order and reject a foreign receipt there as well (§5.3.2).
    def history():
        for index, row in enumerate(rows):
            yield index, backend == "claude_code" and bool(row.get("isCompactSummary")), row
            if backend == "codex" and row.get("type") == "compacted":
                payload = row.get("payload")
                nested = payload.get("replacement_history") if isinstance(payload, dict) else None
                if isinstance(nested, list):
                    for message in nested:
                        yield index, True, {"type": "response_item", "payload": message}
            elif backend == "qwen" and row.get("subtype") == "chat_compression":
                payload = row.get("systemPayload")
                nested = payload.get("compressedHistory") if isinstance(payload, dict) else None
                if isinstance(nested, list):
                    for message in nested:
                        yield index, True, {"message": message}

    for index, nested, row in history():
        if backend != "codex" and row.get("isSidechain"):
            continue
        if backend == "codex":
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if row.get("type") == "session_meta" and "id" in payload and payload["id"] != session_id:
                return None
            if found and (row.get("type") == "session_meta" or payload.get("type") == "additional_tools"):
                return None  # A new startup envelope has no owner receipt yet.
            message = payload if row.get("type") == "response_item" and payload.get("type") == "message" else None
        else:
            if "sessionId" in row and row["sessionId"] != session_id:
                return None
            if found and isinstance(row.get("attachment"), dict):
                return None  # Do not adopt another launch's instructions/attachments.
            if backend == "claude_code":
                if (row.get("type") not in {None, "assistant", "user", "system"}
                        or row.get("isVisibleInTranscriptOnly") or row.get("isApiErrorMessage")):
                    continue  # Exactly the envelopes ignored by claude_context.
            elif not nested:
                if row.get("subtype") == "memory_catalog_snapshot":
                    message = row.get("message")
                    blocks = message.get("parts") if isinstance(message, dict) else None
                    block = blocks[0] if isinstance(blocks, list) and blocks else None
                    if found and isinstance(block, dict) and _qwen_catalog(block):
                        # qwen_context reads this even for type=system/no role.
                        # Resume catalogs carry no captured owner receipt (§5.3.2).
                        return None
                if row.get("type") == "system" or row.get("subtype") in {
                    "realtime_message", "code_mode_tool_result", "chat_compression",
                }:
                    continue  # qwen_context never reads these as primary inputs.
                if isinstance(row.get("message"), dict) and (
                    row.get("type") not in {None, "user", "assistant"} or row.get("subtype") is not None
                ):
                    return None  # Unknown/rendered Qwen message envelopes are not receipts.
            message = row.get("message")
        if not isinstance(message, dict):
            continue
        if backend == "qwen" and found and isinstance(blocks := message.get("parts"), list):
            if any(isinstance(block, dict) and _qwen_catalog(block)
                   and not isinstance(block.get("functionCall"), dict)
                   and not isinstance(block.get("functionResponse"), dict) for block in blocks):
                # The reader also exposes inline catalogs in ordinary messages
                # and compressedHistory, regardless of role. Neither is proof
                # that the instructions belong to this owner (§5.3.2).
                return None
        if message.get("role") not in {"user", "assistant", "model", "developer", "system", "tool"}:
            return None
        if message.get("role") != "user":
            if not found and message.get("role") not in {"developer", "system"}:
                return None  # Unknown conversation before genesis cannot be adopted.
            if found and message.get("role") in {"developer", "system"}:
                return None  # Unknown instruction update between dispatches.
            continue
        if backend != "codex" and row.get("type") not in {None, "user"}:
            return None
        content = message.get("parts" if backend == "qwen" else "content")
        if backend == "qwen" and not isinstance(content, list):
            return None
        if backend == "codex":
            # Use the reader's exact extraction, including future block types.
            # Ignored-for-auth but rendered text would bypass the digest.
            texts = [text for text, _ in _text_fields(content, "")]
        elif isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [item["text"] for item in content if isinstance(item, dict)
                     and isinstance(item.get("text"), str)
                     and (backend == "qwen" or item.get("type") == "text")]
            if texts and any(isinstance(item, dict) and (
                (backend == "qwen" and ("functionCall" in item or "functionResponse" in item))
                or (backend == "claude_code" and item.get("type") not in {"text", "image"})
            ) for item in content):
                return None  # A Studio text dispatch is not a mixed tool-result message.
        else:
            texts = []
        if not texts:
            if not found:
                return None
            continue
        value = "".join(texts)
        if not found:
            if (nested or not value.startswith(dispatch_marker(binding.token))
                    or dispatch_digest(value) != binding.dispatches[0]):
                return None
            found = True
            genesis = index
        if not nested or value.startswith(DISPATCH_PREFIX):
            digest = dispatch_digest(value)
            if not value.startswith(dispatch_marker(binding.token)) or digest not in binding.dispatches:
                return None
            observed.add(digest)
    if not found or not set(binding.dispatches) <= observed:
        return None
    if backend != "codex" and not any(index == genesis for index, _ in _branch(rows)):
        return None  # The reader's active ancestry must reach the proved genesis.
    return genesis


def _matches_workdir(rows: list[dict], backend: str, expected: Path) -> bool:
    """Require explicit, consistent native launch metadata, not directory-name guesses.

    Only known metadata envelopes count: never inspect arbitrary message text or
    tool arguments, whose cwd can legitimately differ from the runtime's launch.
    Unknown/old formats fail closed to Studio's retained dispatch (§5.3.2).
    """
    found = False
    for row in rows:
        if backend == "codex":
            if row.get("type") not in {"session_meta", "turn_context"}:
                continue
            metadata = row.get("payload")
            if not isinstance(metadata, dict):
                continue
            fields = ("cwd",)
        else:
            metadata = row
            fields = ("cwd", "workdir", "workingDirectory", "working_directory")
        for field in fields:
            if field not in metadata:
                continue
            value = metadata[field]
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                return False
            if canonical_root(value) != expected:
                return False
            found = True
    return found


def read_session(backend: str, session_id: str | None, *, studio_system: str = "",
                 workdir: Path | None = None, binding: NativeJournalBinding | None = None) -> JournalContext | None:
    """Read a native session; API callers supply a captured dispatch receipt and cwd.

    Unbound lookup remains only for direct low-level compatibility callers.
    Cwd alone never authorizes a bound lookup, even with only one candidate.
    Unmarked legacy journals remain readable by explicit low-level callers only;
    Context Viewer falls back to its own Studio snapshot/transcript.
    """
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    if binding is not None:
        if (not isinstance(binding, NativeJournalBinding)
                or not re.fullmatch(r"[0-9a-f]{32}", binding.token)
                or not binding.dispatches
                or any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in binding.dispatches)):
            return None
        if workdir is not None and canonical_root(workdir) != canonical_root(binding.workdir):
            return None
        workdir = binding.workdir
    if workdir is not None and (binding is None or not Path(workdir).is_absolute()):
        return None
    if backend == "codex":
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        patterns = [f"{folder}/**/rollout-*-{session_id}.jsonl" for folder in ("sessions", "archived_sessions")]
    elif backend == "claude_code":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        patterns = [f"projects/*/{session_id}.jsonl"]
    elif backend == "qwen":
        root = Path(os.environ.get("QWEN_RUNTIME_DIR") or os.environ.get("QWEN_HOME") or Path.home() / ".qwen").expanduser()
        patterns = [f"projects/*/chats/{session_id}.jsonl", f"projects/*/chats/archive/{session_id}.jsonl"]
    else:
        return None
    try:
        expected = str(canonical_root(workdir)) if workdir is not None else None
        match = None
        seen = set()
        for pattern in patterns:
            for path in root.glob(pattern):
                resolved = path.resolve()
                if not resolved.is_relative_to(root.resolve()):
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                stat = resolved.stat()
                result = _read(str(resolved), backend, stat.st_mtime_ns, stat.st_size,
                               studio_system, expected, binding, session_id)
                if result is not None:
                    if expected is None and (result.sources or result.compacted):
                        return result
                    if expected is not None:
                        if match is not None:
                            return None
                        match = result
        return match if match is not None and (match.sources or match.compacted) else None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        # Missing/rotated logs and incompatible runtime versions are not API failures.
        pass
    return None
