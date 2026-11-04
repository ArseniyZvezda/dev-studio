"""Transcript retention shared by task and chat capture (spec §10.4)."""

from __future__ import annotations

import hashlib

# Mirrors frontend/src/lib/transcript.ts: shows(message, 'advanced').
ALWAYS_KINDS = frozenset({"status", "edited", "commit", "tool_use", "continued"})


def verbose_only(role: str, kind: str) -> bool:
    return role == "system" and kind not in ALWAYS_KINDS


def compact_state(message, owner_id: str) -> bool:
    """Replace noisy telemetry with one current row, also used as the WS upsert id."""
    if message.kind != "system" or message.text not in {"thinking_tokens", "commands_changed"}:
        return False
    key = f"{owner_id}:{message.member_id}:{message.text}"
    message.id = "state_" + hashlib.sha256(key.encode()).hexdigest()[:32]
    if message.text == "thinking_tokens":
        value = message.data.get("estimated_tokens", 0)
        message.data = {"estimated_tokens": value if isinstance(value, int) and value >= 0 else 0}
    else:
        commands = message.data.get("commands", [])
        message.data = {"command_count": len(commands) if isinstance(commands, list) else 0}
    return True
