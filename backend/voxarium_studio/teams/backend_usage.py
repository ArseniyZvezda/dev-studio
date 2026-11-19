"""Recent backend telemetry for the status bar (§6.1).

Account quotas are shared by all resident projects. Output speeds describe actual
work within a single project and backend connection, and expire independently.

Adapters reuse usage counts and timings from their normal runs. For Claude Code quotas,
the CLI can also supply a ``get_usage`` report from an already running session. Reading
status starts no process or provider request. Nothing is persisted, and no raw frames,
account identifiers, credentials or transcript content are retained. Unknown or stale
telemetry is not zero.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from ..core.project_scope import canonical_root

if TYPE_CHECKING:
    from .backends.base import RuntimeSessionKey


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


# Keep a sample long enough to be seen through the status bar's 15-second poll,
# but do not imply that a backend is still producing tokens after work goes quiet.
_SPEED_LIFETIME_SECONDS = 30
_MAX_SPEED_SAMPLES = 256


@dataclass(frozen=True, slots=True)
class _SpeedSample:
    rate: float
    expires_monotonic: float


_speeds: dict[tuple[Path, str], _SpeedSample] = {}


def record_token_speed(
    runtime_key: RuntimeSessionKey | None, backend: str, output_tokens: Any, duration_seconds: Any,
) -> None:
    """Keep the latest measured output rate, never inferring tokens from text or input usage."""
    if runtime_key is None or not isinstance(output_tokens, int) or isinstance(output_tokens, bool):
        return
    tokens, duration = _number(output_tokens), _number(duration_seconds)
    if tokens is None or tokens <= 0 or duration is None or duration <= 0:
        return
    rate = tokens / duration
    if not math.isfinite(rate) or rate <= 0:
        return
    key = (runtime_key.owner.project_root, backend)
    with _lock:
        _speeds.pop(key, None)
        _speeds[key] = _SpeedSample(rate, time.monotonic() + _SPEED_LIFETIME_SECONDS)
        if len(_speeds) > _MAX_SPEED_SAMPLES:
            del _speeds[next(iter(_speeds))]


def record_result_speed(runtime_key: RuntimeSessionKey | None, backend: str, data: dict[str, Any]) -> None:
    """CLI results pair a whole turn/run's usage with its wall duration in milliseconds."""
    usage = data.get("usage")
    duration_ms = _number(data.get("duration_ms"))
    if not data.get("is_error") and isinstance(usage, dict) and duration_ms is not None:
        record_token_speed(runtime_key, backend, usage.get("output_tokens"), duration_ms / 1000)


def get_token_speeds(project_root: Path) -> dict[str, dict[str, float]]:
    root = canonical_root(project_root)
    result = {}
    with _lock:
        now, monotonic = time.time(), time.monotonic()
        for key, sample in list(_speeds.items()):
            remaining = sample.expires_monotonic - monotonic
            if remaining <= 0:
                del _speeds[key]
            elif key[0] == root:
                result[key[1]] = {"tokens_per_second": sample.rate, "expires_at": now + remaining}
    return result
