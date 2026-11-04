"""Usage limits, and waiting one out instead of failing the task (§10.3).

An account that runs out of usage stops a session mid-work, and nothing about the task is
wrong: the same run would carry on an hour later. `TeamTask.resume_on_limit` is the opt-in
that says so — the task waits for the window to refill and starts again — and this module
is the half that reads *when* it refills.

The runtime states it outright when it can. The Agent SDK parses the CLI's `rate_limit`
frame into ``RateLimitInfo(status, resets_at, rate_limit_type, …)``, where ``resets_at`` is
unix epoch seconds and ``status`` is ``rejected`` once the window is actually spent —
checked against claude-agent-sdk 0.2.128, whose ``RateLimitStatus``/``RateLimitType``
literals are the vocabulary below. Everything else here is a fallback for a run that says
less: a 429 on the result frame (``api_error_status``, emitted by the CLI since 2.1.110),
or the phrase in the text of a failure. Those say *that* the limit was hit and not when it
lifts, which is the difference between arming a wait and blocking the task with the reason
written on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

#: Where the reset instant arrives. ``resets_at`` is the SDK's field, ``resetsAt`` the
#: CLI's raw spelling of the same number (``RateLimitInfo.raw`` keeps both), and the
#: overage pair is that fact for an account paying past its plan.
_RESET_KEYS = ("resets_at", "resetsAt", "overage_resets_at", "overageResetsAt")

#: Anything before this is not a reset instant. It is the guard that keeps a duration —
#: a "retry after 900" — from being read as an epoch somewhere in 1970.
_EPOCH_FLOOR = 1_577_836_800  # 2020-01-01Z

#: The account ran out, in the words the CLI and the API use for it.
_LIMIT = re.compile(
    r"usage limit|rate[ _-]?limit|out of (?:usage|credits)|quota (?:exceeded|reached)",
    re.I,
)

#: Other things called a limit, none of which a wait would fix. The turn cap and the
#: message-size ceiling are the Studio's own settings (§10.3), the depth cap is the
#: delegation ceiling (§10.4), and a context limit needs a shorter session rather than a
#: later one. Only consulted when nothing said 429 — a real rate limit hit during a run
#: that also spent its turns is still a rate limit.
_OTHER_LIMIT = re.compile(
    r"delegation depth|subagent|buffer size|context limit|max_turns|turn limit",
    re.I,
)

#: An epoch quoted in prose — "usage limit reached|1750604400", "resets at 1750604400".
#: Bounded lookahead so it is the number *belonging to* the phrase, not the next one in
#: the message.
_EPOCH_IN_TEXT = re.compile(r"(?:reset\w*|limit reached)\D{0,24}(\d{10,13})", re.I)


@dataclass(frozen=True, slots=True)
class UsageLimit:
    """A run stopped because the account is out of usage — and when that lifts, if known."""

    detail: str
    """Why the run stopped, for the transcript entry beside the wait."""

    reset_at: datetime | None = None
    """When the window refills, in UTC. ``None`` when the runtime did not say."""

    def resume_at(self, *, grace: float, fallback: float) -> datetime | None:
        """When the task may be started again, or ``None`` when it must not be.

        The grace is not politeness. The reset is *their* clock and this is ours, and a
        session opened on the exact second comes straight back refused — which would spend
        the whole wait and leave the task blocked anyway. ``teams.limit_resume_grace``
        (two minutes) is the slack.

        A reset already in the past is a limit that lifted while nobody was watching — a
        run stopped before a restart, say — so the wait is measured from now rather than
        from an instant that has been and gone.

        Without a reset instant there is nothing to wait *for*: ``fallback`` is
        ``teams.limit_fallback_wait``, off by default, because retrying blind is a guess
        the user should make deliberately rather than one the Studio makes for them.
        """
        now = datetime.now(timezone.utc)
        if self.reset_at is None:
            return now + timedelta(seconds=fallback) if fallback > 0 else None
        return max(self.reset_at, now) + timedelta(seconds=grace)


def from_rate_limit(info: dict[str, Any]) -> UsageLimit | None:
    """The CLI's own frame — the one source that states the reset rather than implying it.

    ``allowed`` and ``allowed_warning`` are the window filling up, not the window spent;
    only ``rejected`` stops a run, and only it arms anything.
    """
    if str(info.get("status") or "").strip() != "rejected":
        return None
    window = str(info.get("rate_limit_type") or "").replace("_", "-").strip()
    return UsageLimit(
        detail=f"the {window} usage limit is spent" if window else "the usage limit is spent",
        reset_at=reset_from(info),
    )


def from_failure(text: str, data: dict[str, Any]) -> UsageLimit | None:
    """Did a turn fail because of the account's usage, rather than because of its work?

    Read off whatever the failure carries: the HTTP status the CLI now reports, the
    ``errors`` list on the result frame, and the message itself — in that order, because
    only the first is unambiguous.
    """
    reported = data.get("api_error_status")
    said = "\n".join(
        [text or "", *(str(entry) for entry in (data.get("errors") or []) if entry)]
    )
    if _as_int(reported) == 429:
        return UsageLimit(
            detail="the API answered 429 — the account is rate limited",
            reset_at=reset_from(data) or _reset_in_text(said),
        )
    if not _LIMIT.search(said) or _OTHER_LIMIT.search(said):
        return None
    return UsageLimit(
        detail="the run reported a usage limit",
        reset_at=reset_from(data) or _reset_in_text(said),
    )


def stopped_by(
    text: str, data: dict[str, Any], reported: UsageLimit | None = None
) -> UsageLimit | None:
    """What stopped this turn, read against the last ``rate_limit`` frame the run saw.

    The two sources answer different halves and are combined here rather than at the call
    site. A failure says *that* the account ran out, in the words of whatever failed; the
    frame says the window is spent and **when it refills**, which is the half a wait needs.
    A run that saw ``rejected`` and then failed is taken to have failed on it — the retry
    that follows either succeeds or reports the limit again, so a wrong guess costs one
    session start rather than a wrong state.
    """
    found = from_failure(text, data)
    if found is None:
        return reported
    if found.reset_at is None and reported is not None:
        return UsageLimit(detail=found.detail, reset_at=reported.reset_at)
    return found


def reset_from(data: dict[str, Any]) -> datetime | None:
    """The reset instant under any of the names it travels as, or ``None``."""
    for key in _RESET_KEYS:
        found = _instant(data.get(key))
        if found is not None:
            return found
    return None


def _reset_in_text(text: str) -> datetime | None:
    match = _EPOCH_IN_TEXT.search(text)
    return _instant(match.group(1)) if match else None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _instant(value: Any) -> datetime | None:
    """An epoch (seconds or milliseconds) or an ISO timestamp, as an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    number = _as_int(value)
    if number is None:
        return _iso(value)
    # Milliseconds are what a JSON producer reaches for as often as seconds, and the two
    # are three orders of magnitude apart — far enough that the floor tells them apart.
    if number > _EPOCH_FLOOR * 1000:
        number //= 1000
    if number < _EPOCH_FLOOR:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
