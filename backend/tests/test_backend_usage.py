"""Account usage snapshots come only from existing CLI telemetry (§6.1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from voxarium_studio.models.teams import TeamMember
from voxarium_studio.teams import backend_usage as usage
from voxarium_studio.teams.backends import claude_code, codex
from voxarium_studio.teams.backends.base import AgentRunRequest


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    clock = SimpleNamespace(wall=1_800_000_000.0, elapsed=1000.0)
    monkeypatch.setattr(usage, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.elapsed))
    monkeypatch.setattr(usage, "_claude", {})
    monkeypatch.setattr(usage, "_codex", ())
    monkeypatch.setattr(usage, "_codex_observed_at", 0.0)
    return clock


def claude(fraction: object = 0.5, window: str = "five_hour", reset: object = None) -> dict:
    return {"rate_limit_type": window, "utilization": fraction, "resets_at": reset}


def codex_limits(percent: object = 25, *, reset: object = None, minutes: object = 300) -> dict:
    return {"primary": {"used_percent": percent, "resets_at": reset, "window_minutes": minutes}}


def test_unknown_is_not_zero() -> None:
    for backend in ("claude_code", "codex", "qwen_code", "unknown"):
        assert usage.get_usage_limits(backend) == []


def test_claude_merges_windows_with_real_zero_and_no_raw_data(clock: SimpleNamespace) -> None:
    usage.record_claude_usage({**claude(0), "raw": {"credential": "secret", "text": "private"}})
    usage.record_claude_usage(claude(0.325, "seven_day", clock.wall + 100))
    usage.record_claude_usage(claude(1, "seven_day_opus"))
    assert usage.get_usage_limits("claude_code") == [
        {"window": "five_hour", "used_percent": 0.0, "resets_at": None},
        {"window": "seven_day", "used_percent": 32.5, "resets_at": clock.wall + 100},
        {"window": "seven_day_opus", "used_percent": 100.0, "resets_at": None},
    ]
    usage.record_claude_usage(claude(0.1))
    result = usage.get_usage_limits("claude_code")
    assert [entry["used_percent"] for entry in result] == [10.0, 32.5, 100.0]
    result[0]["used_percent"] = 99
    assert usage.get_usage_limits("claude_code")[0]["used_percent"] == 10.0
    assert "secret" not in repr(usage._claude)


def test_claude_storage_is_bounded_to_provider_windows() -> None:
    for index in range(100):
        usage.record_claude_usage(claude(0.5, f"unexpected_{index}"))
    for window in usage._CLAUDE_WINDOWS:
        usage.record_claude_usage(claude(0.5, window))
    assert len(usage._claude) == len(usage._CLAUDE_WINDOWS) == 5


@pytest.mark.parametrize("value", [None, True, False, "0.5", {}, [], -0.01, 1.01, float("nan"), float("inf"), -float("inf"), 10**500])
def test_claude_invalid_fraction_is_ignored(value: object) -> None:
    usage.record_claude_usage(claude(0.25))
    usage.record_claude_usage(claude(value))
    assert usage.get_usage_limits("claude_code")[0]["used_percent"] == 25


@pytest.mark.parametrize("value", [None, True, False, "25", {}, [], -0.01, 100.01, float("nan"), float("inf"), -float("inf"), 10**500])
def test_codex_invalid_percentage_is_ignored(value: object) -> None:
    usage.record_codex_usage(codex_limits(25))
    usage.record_codex_usage(codex_limits(value))
    assert usage.get_usage_limits("codex")[0]["used_percent"] == 25


def test_malformed_envelopes_and_windows_are_ignored() -> None:
    for value in (None, True, "private", [], 1):
        usage.record_claude_usage(value)
        usage.record_codex_usage(value)
    for window in (None, {}, [], "", "unexpected"):
        usage.record_claude_usage({"rate_limit_type": window, "utilization": 0.5})
    usage.record_codex_usage({"primary": [], "secondary": True, "raw": {"secret": "private"}})
    assert usage.get_usage_limits("claude_code") == usage.get_usage_limits("codex") == []


def test_codex_replaces_snapshot_and_normalizes_known_durations(clock: SimpleNamespace) -> None:
    usage.record_codex_usage({
        **codex_limits(0, reset=clock.wall + 200),
        "secondary": {"used_percent": 100, "window_minutes": 10080},
        "credits": {"balance": "private"},
    })
    assert usage.get_usage_limits("codex") == [
        {"window": "five_hour", "used_percent": 0.0, "resets_at": clock.wall + 200},
        {"window": "seven_day", "used_percent": 100.0, "resets_at": None},
    ]
    usage.record_codex_usage(codex_limits(42, minutes=60))
    assert usage.get_usage_limits("codex") == [{"window": "primary", "used_percent": 42.0, "resets_at": None}]
    usage.record_codex_usage({"secondary": {"used_percent": 3}})
    assert usage.get_usage_limits("codex") == [{"window": "secondary", "used_percent": 3.0, "resets_at": None}]
    assert "private" not in repr(usage._codex)


@pytest.mark.parametrize("reset", [True, False, "1800000300", {}, -1, float("nan"), float("inf")])
def test_invalid_reset_is_unknown_not_an_epoch(reset: object) -> None:
    usage.record_claude_usage(claude(reset=reset))
    usage.record_codex_usage(codex_limits(reset=reset))
    assert usage.get_usage_limits("claude_code")[0]["resets_at"] is None
    assert usage.get_usage_limits("codex")[0]["resets_at"] is None


def test_reset_expiry_hides_window_not_fabricated_zero(clock: SimpleNamespace) -> None:
    usage.record_claude_usage(claude(0.7, reset=clock.wall + 10))
    usage.record_claude_usage(claude(0.1, "seven_day"))
    usage.record_codex_usage(codex_limits(75, reset=clock.wall + 10))
    clock.wall += 10
    assert usage.get_usage_limits("claude_code") == [{"window": "seven_day", "used_percent": 10.0, "resets_at": None}]
    assert usage.get_usage_limits("codex") == []
    usage.record_codex_usage(codex_limits(0, reset=clock.wall + 100))
    assert usage.get_usage_limits("codex")[0]["used_percent"] == 0


def test_both_backends_expire_no_later_than_fifteen_minutes(clock: SimpleNamespace) -> None:
    usage.record_claude_usage(claude())
    usage.record_codex_usage(codex_limits())
    clock.wall += 899
    clock.elapsed += 899
    assert usage.get_usage_limits("claude_code") and usage.get_usage_limits("codex")
    clock.wall += 1
    clock.elapsed += 1
    assert usage.get_usage_limits("claude_code") == usage.get_usage_limits("codex") == []


def test_freshness_uses_monotonic_clock_when_wall_clock_moves_back(clock: SimpleNamespace) -> None:
    usage.record_claude_usage(claude())
    usage.record_codex_usage(codex_limits())
    clock.wall -= 3600
    clock.elapsed += 900
    assert usage.get_usage_limits("claude_code") == usage.get_usage_limits("codex") == []


def test_per_window_updates_do_not_refresh_other_claude_windows(clock: SimpleNamespace) -> None:
    usage.record_claude_usage(claude())
    clock.wall += 899
    clock.elapsed += 899
    usage.record_claude_usage(claude(0, "seven_day"))
    clock.wall += 1
    clock.elapsed += 1
    assert usage.get_usage_limits("claude_code") == [{"window": "seven_day", "used_percent": 0.0, "resets_at": None}]


def test_rollout_timestamps_prevent_stale_or_out_of_order_history(clock: SimpleNamespace) -> None:
    usage.record_codex_usage(codex_limits(10), observed_at=clock.wall - 50)
    usage.record_codex_usage(codex_limits(90), observed_at=clock.wall - 100)
    assert usage.get_usage_limits("codex")[0]["used_percent"] == 10
    # ISO timestamps are what genuine rollout event_msg records carry.
    usage.record_codex_usage(codex_limits(20), observed_at="2027-01-15T08:00:00Z")
    assert usage.get_usage_limits("codex")[0]["used_percent"] == 20
    clock.wall += 900
    clock.elapsed += 900
    usage.record_codex_usage(codex_limits(20), observed_at=clock.wall - 900)
    assert usage.get_usage_limits("codex") == []


@pytest.mark.parametrize("stamp", [True, "bad timestamp", float("nan"), float("inf"), 0, 1_900_000_000])
def test_invalid_stale_or_future_timestamp_does_not_replace_snapshot(stamp: object) -> None:
    usage.record_codex_usage(codex_limits(25))
    usage.record_codex_usage(codex_limits(80), observed_at=stamp)
    assert usage.get_usage_limits("codex")[0]["used_percent"] == 25


def append(path: Path, payload: dict, stamp: object = None) -> None:
    record = {"type": "event_msg", "payload": payload}
    if stamp is not None:
        record["timestamp"] = stamp
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def rollout(home: Path) -> Path:
    path = home / "sessions" / "rollout-test-thread-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_codex_poll_hook_records_quota_without_token_info(tmp_path: Path, clock: SimpleNamespace) -> None:
    path = rollout(tmp_path)
    append(path, {"type": "token_count", "info": None, "rate_limits": codex_limits(30)}, clock.wall)
    reader = codex._RolloutUsage(tmp_path)
    reader.poll("thread-1", force=True)
    assert usage.get_usage_limits("codex")[0]["used_percent"] == 30
    # Another resident project's observation is newer than this reader's history.
    usage.record_codex_usage(codex_limits(40), observed_at=clock.wall)
    append(path, {"type": "token_count", "rate_limits": codex_limits(90)}, clock.wall - 50)
    reader.poll("thread-1", force=True)
    assert usage.get_usage_limits("codex")[0]["used_percent"] == 40


def test_codex_resume_without_timestamp_skips_history_but_reads_live_boundary(tmp_path: Path) -> None:
    path = rollout(tmp_path)
    append(path, {"type": "token_count", "rate_limits": codex_limits(90)})
    reader = codex._RolloutUsage(tmp_path)
    reader.resume("thread-1")
    assert usage.get_usage_limits("codex") == []
    append(path, {"type": "token_count", "rate_limits": codex_limits(95)})
    reader.poll("thread-1", force=True)
    assert usage.get_usage_limits("codex") == []
    append(path, {"type": "task_started"})
    append(path, {"type": "token_count", "rate_limits": codex_limits(35)})
    reader.poll("thread-1", force=True)
    assert usage.get_usage_limits("codex")[0]["used_percent"] == 35


def test_codex_late_resume_discovery_cannot_refresh_undated_history(tmp_path: Path) -> None:
    reader = codex._RolloutUsage(tmp_path)
    reader.resume("thread-1")
    path = rollout(tmp_path)
    append(path, {"type": "task_started"})
    append(path, {"type": "token_count", "rate_limits": codex_limits(90)})
    reader.poll("thread-1", force=True)
    assert usage.get_usage_limits("codex") == []


async def test_claude_adapter_records_real_rate_limit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A protocol fake, never an SDK subprocess or network request.
    sdk = ModuleType("claude_agent_sdk")
    for name in ("AssistantMessage", "ResultMessage", "SystemMessage", "UserMessage"):
        setattr(sdk, name, type(name, (), {}))
    sdk.ClaudeAgentOptions = lambda **kwargs: SimpleNamespace(**kwargs)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, prompt):
            pass

        async def receive_response(self):
            yield SimpleNamespace(rate_limit_info=SimpleNamespace(**claude(0)), session_id="thread-1")
            yield SimpleNamespace(rate_limit_info=SimpleNamespace(**claude(0.7, "seven_day")))

    sdk.ClaudeSDKClient = Client
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setattr(claude_code, "_option_fields", lambda: frozenset())
    monkeypatch.setattr(claude_code.ClaudeCodeBackend, "is_available", lambda self: (True, "Ready"))
    request = AgentRunRequest(
        member=TeamMember(team_id="team", name="Dev", role_title="Developer"),
        task=None, session_key="usage-test", workdir=tmp_path, prompt="test", system_prompt="test",
    )
    events = [event async for event in claude_code.ClaudeCodeBackend().run(request)]
    assert [event.kind for event in events] == ["rate_limit", "rate_limit"]
    assert events[0].data["utilization"] == 0
    assert [item["used_percent"] for item in usage.get_usage_limits("claude_code")] == [0, 70]
