"""Recent account quota telemetry shared by all resident projects (§6.1).

This is not token accounting or a quota probe: adapters feed the CLI frames they
already consume and, for Claude Code, the CLI's own ``get_usage`` report asked of a
session that is already running. The Studio never calls a provider itself, and reading
the status starts nothing. Nothing is persisted, and no raw frames, account identifiers,
credentials or transcript content are retained. Unknown/stale usage is not zero.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any


_MAX_AGE_SECONDS = 15 * 60
# The SDK's RateLimitType vocabulary bounds both storage and exposed window names.
_CLAUDE_WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage")
_lock = Lock()


@dataclass(frozen=True, slots=True)
class _Sample:
    window: str
    used_percent: float
    resets_at: float | None
    observed_at: float
    received_at: float

    def fresh(self, now: float, monotonic: float) -> bool:
        return (
            now - self.observed_at < _MAX_AGE_SECONDS
            and monotonic - self.received_at < _MAX_AGE_SECONDS
            and (self.resets_at is None or self.resets_at > now)
        )

    def public(self) -> dict[str, Any]:
        return {"window": self.window, "used_percent": self.used_percent, "resets_at": self.resets_at}


# Five Claude windows and two Codex windows at most, independent of project/session count.
_claude: dict[str, _Sample] = {}
_codex: tuple[_Sample, ...] = ()
_codex_observed_at = 0.0


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _reset(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _observation(value: Any, now: float) -> float | None:
    """Use the rollout's timestamp, not the time old history happened to be read."""
    if value is None:
        return now
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            value = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    stamp = _number(value)
    # Small writer clock skew is harmless, but must not poison the ordering watermark.
    if stamp is None or stamp > now + 60 or now - stamp >= _MAX_AGE_SECONDS:
        return None
    return min(stamp, now)


def record_claude_usage(info: Any) -> None:
    """Merge one SDK rate_limit window, whose utilization is a fraction, not percent."""
    if not isinstance(info, dict):
        return
    window = info.get("rate_limit_type")
    if not isinstance(window, str) or window not in _CLAUDE_WINDOWS:
        return
    used = _number(info.get("utilization"))
    if used is None or not 0 <= used <= 1:
        return
    sample = _Sample(window, used * 100, _reset(info.get("resets_at")), time.time(), time.monotonic())
    with _lock:
        _claude[window] = sample


def _reset_instant(value: Any) -> float | None:
    """``get_usage`` dates a reset as ISO 8601; the stream frames as epoch seconds."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return _reset(parsed.timestamp())
        except (OverflowError, OSError):
            return None
    return _reset(value)


def record_claude_usage_report(rate_limits: Any) -> None:
    """Merge the CLI's ``get_usage`` report, whose utilization is already a percentage.

    These are the figures ``/usage`` shows. The stream's ``rate_limit`` frames carry a
    utilization only once a window crosses a warning threshold, so without this report an
    ordinary session never has a percentage, and one that once crossed it keeps that
    figure until it ages out (§6.1). A window the report names as ``null`` does not apply
    to this account and is dropped; one it does not mention is left as it was.
    """
    if not isinstance(rate_limits, dict):
        return
    now, monotonic = time.time(), time.monotonic()
    samples: dict[str, _Sample] = {}
    absent: list[str] = []
    for window in _CLAUDE_WINDOWS:
        if window not in rate_limits:
            continue
        info = rate_limits[window]
        if info is None:
            absent.append(window)
            continue
        if not isinstance(info, dict):
            continue
        used = _number(info.get("utilization"))
        if used is None or not 0 <= used <= 100:
            continue
        samples[window] = _Sample(window, used, _reset_instant(info.get("resets_at")), now, monotonic)
    with _lock:
        for window in absent:
            _claude.pop(window, None)
        _claude.update(samples)


def record_codex_usage(rate_limits: Any, *, observed_at: Any = None) -> None:
    """Replace Codex's primary/secondary snapshot, ignoring older rollout observations.

    ``observed_at`` is the optional event_msg timestamp (ISO or epoch seconds).
    Standard durations use the same window ids as Claude; unknown durations retain
    the provider's primary/secondary id rather than guessing a quota window.
    """
    global _codex, _codex_observed_at
    if not isinstance(rate_limits, dict):
        return
    now, monotonic = time.time(), time.monotonic()
    stamp = _observation(observed_at, now)
    if stamp is None:
        return
    samples: list[_Sample] = []
    for key in ("primary", "secondary"):
        info = rate_limits.get(key)
        if not isinstance(info, dict):
            continue
        used = _number(info.get("used_percent"))
        if used is None or not 0 <= used <= 100:
            continue
        minutes = _number(info.get("window_minutes"))
        window = {300: "five_hour", 10080: "seven_day"}.get(minutes, key)
        samples.append(_Sample(window, used, _reset(info.get("resets_at")), stamp, monotonic))
    # Missing/malformed telemetry says nothing; a valid partial snapshot does replace
    # the previous one, so absent secondary windows cannot linger after a plan change.
    if not samples:
        return
    with _lock:
        if stamp < _codex_observed_at:
            return
        _codex = tuple(samples)
        _codex_observed_at = stamp


def get_usage_limits(backend: str) -> list[dict[str, Any]]:
    """Return only fresh genuine percentages; expired resets disappear, never become 0%."""
    global _codex
    now, monotonic = time.time(), time.monotonic()
    with _lock:
        if backend == "claude_code":
            for window, sample in list(_claude.items()):
                if not sample.fresh(now, monotonic):
                    del _claude[window]
            return [_claude[window].public() for window in _CLAUDE_WINDOWS if window in _claude]
        if backend == "codex":
            _codex = tuple(sample for sample in _codex if sample.fresh(now, monotonic))
            return [sample.public() for sample in _codex]
    return []
