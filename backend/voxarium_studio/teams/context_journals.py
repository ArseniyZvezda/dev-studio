"""Read the selected backend session, rather than Studio's shortened timeline (§10.4).

These are local estimates of retained text. Journal formats are not provider request
dumps: encrypted reasoning, images and undisclosed tool schemas remain unaccounted for.
Never start or resume an agent merely to inspect its context.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from .context_codex import codex_context
from .context_provider_journals import claude_context, qwen_context
from .context_sources import JournalContext

MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024


@lru_cache(maxsize=8)
def _read(path: str, backend: str, mtime: int, size: int, studio_system: str = "") -> JournalContext | None:
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
    if backend == "claude_code":
        return claude_context(rows)
    reader = codex_context if backend == "codex" else qwen_context
    return reader(rows, studio_system=studio_system)


def read_session(backend: str, session_id: str | None, *, studio_system: str = "") -> JournalContext | None:
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
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
        for pattern in patterns:
            for path in root.glob(pattern):
                if not path.resolve().is_relative_to(root.resolve()):
                    continue
                stat = path.stat()
                result = _read(str(path), backend, stat.st_mtime_ns, stat.st_size, studio_system)
                if result and (result.sources or result.compacted):
                    return result
    except (OSError, ValueError, TypeError, KeyError):
        # Missing/rotated logs and incompatible runtime versions are not API failures.
        pass
    return None
