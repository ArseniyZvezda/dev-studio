"""Backend-neutral live task accounting (spec §10.4, §10.6)."""

from __future__ import annotations

from typing import Any

from .backends.base import AgentEvent

TOKEN_FIELDS = (
    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
)


def token_count(value: Any) -> int | None:
    """Unknown is not zero; malformed provider telemetry must not break a run."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class RunUsage:
    """Credit each response once, then reconcile the final result without adding it twice.

    Ordinary assistant usage is incremental and Claude-shaped (cache excluded from input).
    A backend may instead send system/usage_update with a cumulative total since the last
    result. Context is a separate last-request measurement, never the cumulative input.
    """

    def __init__(self) -> None:
        self.total = 0
        self.context: dict[str, int] = {}

    def observe(self, event: AgentEvent) -> bool:
        data = event.data
        if event.kind == "system" and data.get("subtype") == "usage_update":
            total = token_count(data.get("total_tokens"))
            if total is not None:
                self.total = max(self.total, total)
            for key in ("context_tokens", "context_window"):
                value = token_count(data.get(key))
                if value is not None:
                    self.context[key] = value
            return total is not None or bool(self.context)
        if event.kind not in {"text", "thinking", "tool_use"}:
            return False
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return False
        counted = {key: value for key in TOKEN_FIELDS
                   if (value := token_count(usage.get(key))) is not None}
        if not counted:
            return False
        self.total += sum(counted.values())
        if not data.get("parent_tool_use_id") and any(key in counted for key in TOKEN_FIELDS[:3]):
            self.context = {"context_tokens": sum(counted.get(key, 0) for key in TOKEN_FIELDS[:3])}
        return True

    def snapshot(self, task_total: int) -> AgentEvent:
        return AgentEvent(kind="system", text="usage_update", data={
            "subtype": "usage_update", "total_tokens": self.total,
            "task_total_tokens": task_total, **self.context,
        })

    def finish(self, data: dict[str, Any]) -> None:
        self.total = max(self.total, token_count(data.get("total_tokens")) or 0)

    def next_segment(self) -> None:
        self.total = 0
