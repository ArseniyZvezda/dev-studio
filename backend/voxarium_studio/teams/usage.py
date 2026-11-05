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


def usage_components(data: dict[str, Any]) -> tuple[int, int]:
    """Known new/cache-read tokens; an unclassified legacy total is neither (§10.4)."""
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    uncached = token_count(data.get("total_uncached_tokens"))
    cached = token_count(data.get("total_cached_tokens"))
    return (
        uncached if uncached is not None else sum(token_count(usage.get(key)) or 0
            for key in ("input_tokens", "cache_creation_input_tokens", "output_tokens")),
        cached if cached is not None else token_count(usage.get("cache_read_input_tokens")) or 0,
    )


class RunUsage:
    """Credit each response once, then reconcile the final result without adding it twice.

    Ordinary assistant usage is incremental and Claude-shaped (cache excluded from input).
    A backend may instead send system/usage_update with a cumulative total since the last
    result. Context is a separate last-request measurement, never the cumulative input.
    """

    def __init__(self) -> None:
        self.total = 0
        self.uncached = 0
        self.cached = 0
        self.context: dict[str, int] = {}
        self.responses: dict[str, dict[str, int]] = {}

    def observe(self, event: AgentEvent) -> bool:
        data = event.data
        if event.kind == "system" and data.get("subtype") == "usage_update":
            total = token_count(data.get("total_tokens"))
            if total is not None:
                self.total = max(self.total, total)
            uncached, cached = usage_components(data)
            self.uncached = max(self.uncached, uncached)
            self.cached = max(self.cached, cached)
            self.total = max(self.total, self.uncached + self.cached)
            for key in ("context_tokens", "context_window"):
                value = token_count(data.get(key))
                if value is not None:
                    self.context[key] = value
            return total is not None or bool(uncached or cached or self.context)
        if event.kind not in {"text", "thinking", "tool_use"}:
            return False
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return False
        counted = {key: value for key in TOKEN_FIELDS
                   if (value := token_count(usage.get(key))) is not None}
        if not counted:
            return False
        # Claude emits several content frames for one API response, each repeating
        # its input usage and advancing output. Only per-field increments are new.
        response_id = data.get("provider_response_id")
        delta = counted
        if isinstance(response_id, str) and response_id:
            previous = self.responses.setdefault(response_id, {})
            delta = {key: max(0, count - previous.get(key, 0)) for key, count in counted.items()}
            for key, count in counted.items():
                previous[key] = max(previous.get(key, 0), count)
            counted = previous
        self.total += sum(delta.values())
        self.cached += delta.get("cache_read_input_tokens", 0)
        self.uncached += sum(delta.get(key, 0) for key in TOKEN_FIELDS if key != "cache_read_input_tokens")
        if not data.get("parent_tool_use_id") and any(key in counted for key in TOKEN_FIELDS[:3]):
            self.context = {"context_tokens": sum(counted.get(key, 0) for key in TOKEN_FIELDS[:3])}
        return True

    def counters(self) -> dict[str, int]:
        return {"total_tokens": self.total, "total_uncached_tokens": self.uncached,
                "total_cached_tokens": self.cached, "accounting_version": 2}

    def snapshot(self, task_total: int, task_uncached: int = 0, task_cached: int = 0) -> AgentEvent:
        return AgentEvent(kind="system", text="usage_update", data={
            "subtype": "usage_update", **self.counters(),
            "task_total_tokens": task_total, "task_total_uncached_tokens": task_uncached,
            "task_total_cached_tokens": task_cached, **self.context,
        })

    def finish(self, data: dict[str, Any]) -> None:
        self.total = max(self.total, token_count(data.get("total_tokens")) or 0)
        uncached, cached = usage_components(data)
        self.uncached = max(self.uncached, uncached)
        self.cached = max(self.cached, cached)
        self.total = max(self.total, self.uncached + self.cached)

    def next_segment(self) -> None:
        self.total = 0
        self.uncached = self.cached = 0
