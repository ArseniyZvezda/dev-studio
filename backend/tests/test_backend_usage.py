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
from voxarium_studio.teams.backends.base import AgentRunRequest, RuntimeSessionKey, runtime_session_owner


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    clock = SimpleNamespace(wall=1_800_000_000.0, elapsed=1000.0)
    monkeypatch.setattr(usage, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.elapsed))
    monkeypatch.setattr(usage, "_claude", {})
    monkeypatch.setattr(usage, "_codex", ())
    monkeypatch.setattr(usage, "_codex_observed_at", 0.0)
    monkeypatch.setattr(usage, "_speeds", {})
    return clock


def test_token_speed_isolated_by_project_and_connection(tmp_path: Path, clock: SimpleNamespace) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    one = RuntimeSessionKey(runtime_session_owner(first, "chat", "same-id"))
    two = RuntimeSessionKey(runtime_session_owner(second, "chat", "same-id"))
    usage.record_result_speed(one, "codex", {"duration_ms": 2000, "usage": {
        "input_tokens": 1000, "cache_read_input_tokens": 400, "output_tokens": 75,
    }})
    usage.record_token_speed(two, "codex", 40, 2)
    usage.record_token_speed(one, "openai_api:a", 100, 2)
    usage.record_token_speed(one, "openai_api:b", 100, 4)
    expiry = clock.wall + usage._SPEED_LIFETIME_SECONDS
    assert usage.get_token_speeds(first) == {
        "codex": {"tokens_per_second": 37.5, "expires_at": expiry},
        "openai_api:a": {"tokens_per_second": 50, "expires_at": expiry},
        "openai_api:b": {"tokens_per_second": 25, "expires_at": expiry},
    }
    assert usage.get_token_speeds(second) == {"codex": {"tokens_per_second": 20, "expires_at": expiry}}

    clock.elapsed += usage._SPEED_LIFETIME_SECONDS - 1
    clock.wall += usage._SPEED_LIFETIME_SECONDS - 1
    usage.record_token_speed(one, "codex", 30, 2)
    clock.elapsed += 1
    # Expiry follows monotonic time even if the system clock was adjusted backwards.
    clock.wall -= 60
    assert usage.get_token_speeds(second) == {}
    assert usage.get_token_speeds(first) == {"codex": {
        "tokens_per_second": 15, "expires_at": clock.wall + usage._SPEED_LIFETIME_SECONDS - 1,
    }}


@pytest.mark.parametrize("value", [None, False, True, "1", 0, -1, float("nan"), float("inf"), 10**500])
def test_token_speed_ignores_invalid_values(tmp_path: Path, value: object) -> None:
    key = RuntimeSessionKey(runtime_session_owner(tmp_path, "task", "test"))
    usage.record_token_speed(key, "codex", value, 1)
    usage.record_token_speed(key, "codex", 10, value)
    assert usage.get_token_speeds(tmp_path) == {}


def test_status_reports_only_fresh_token_speed(client, project: Path, clock: SimpleNamespace) -> None:
    key = RuntimeSessionKey(runtime_session_owner(project, "task", "test"))
    usage.record_token_speed(key, "codex", 90, 3)
    response = client.get("/api/status")
    assert response.status_code == 200
    backends = response.json()["agent_backends"]
    assert backends["codex"]["token_speed"] == {
        "tokens_per_second": 30, "expires_at": clock.wall + usage._SPEED_LIFETIME_SECONDS,
    }
    assert backends["qwen"]["token_speed"] is None

    clock.elapsed += usage._SPEED_LIFETIME_SECONDS
    assert client.get("/api/status").json()["agent_backends"]["codex"]["token_speed"] is None


def test_token_speed_ignores_failed_or_unscoped_results(tmp_path: Path) -> None:
    key = RuntimeSessionKey(runtime_session_owner(tmp_path, "task", "test"))
    usage.record_result_speed(key, "qwen", {"duration_ms": 2000, "usage": {"input_tokens": 75}})
    usage.record_result_speed(key, "qwen", {"duration_ms": 1000, "usage": {"output_tokens": 75}, "is_error": True})
    usage.record_token_speed(None, "qwen", 75, 1)
    assert usage.get_token_speeds(tmp_path) == {}


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


def test_claude_usage_report_is_percent_with_iso_resets(clock: SimpleNamespace) -> None:
    """`get_usage` is what `/usage` shows: percentages, not the stream's fractions (§6.1)."""
    usage.record_claude_usage(claude(0.9, "seven_day_opus"))
    usage.record_claude_usage_report({
        "five_hour": {"utilization": 17, "resets_at": "2027-01-16T08:00:00+00:00"},
        "seven_day": {"utilization": 2.5, "resets_at": None},
        "seven_day_opus": None,  # not this account's window: the stale warning goes
        "seven_day_sonnet": {"utilization": 140},
        "extra_usage": {"utilization": 50},
        "limits": [{"percent": 99}],
    })
    expected = [
        {"window": "five_hour", "used_percent": 17.0, "resets_at": clock.wall + 86_400},
        {"window": "seven_day", "used_percent": 2.5, "resets_at": None},
    ]
    assert usage.get_usage_limits("claude_code") == expected
    assert usage._reset_instant("2027-01-16T08:00:00Z") == clock.wall + 86_400
    assert usage._reset_instant("not a date") is None
    for malformed in (None, [], "17%", {"five_hour": "17"}, {"five_hour": {"utilization": "17"}}):
        usage.record_claude_usage_report(malformed)
    assert usage.get_usage_limits("claude_code") == expected


import asyncio  # noqa: E402


def _fake_claude_sdk(monkeypatch: pytest.MonkeyPatch, turns: list[list[object]], control: list[dict]) -> None:
    """A protocol fake of the SDK surface the adapter reads — never a subprocess."""
    sdk = ModuleType("claude_agent_sdk")
    for name in ("AssistantMessage", "ResultMessage", "SystemMessage", "UserMessage"):
        setattr(sdk, name, type(name, (), {"__init__": lambda self, **kw: self.__dict__.update(kw)}))
    sdk.ClaudeAgentOptions = lambda **kwargs: SimpleNamespace(**kwargs)

    class Query:
        async def _send_control_request(self, request, timeout=60.0):
            control.append(request)
            if control[0] == "silent":
                await asyncio.Event().wait()  # the CLI exits before it answers
            return {"rate_limits": {"five_hour": {"utilization": 42, "resets_at": None}}}

    class Client:
        def __init__(self, **kwargs):
            self._query = Query()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, prompt):
            pass

        async def receive_response(self):
            for message in turns.pop(0):
                await asyncio.sleep(0)  # a real session reads a pipe between frames
                yield message

    sdk.ClaudeSDKClient = Client
    sdk.TextBlock = type("TextBlock", (), {"__init__": lambda self, text: setattr(self, "text", text)})
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setattr(claude_code, "_option_fields", lambda: frozenset())
    monkeypatch.setattr(claude_code.ClaudeCodeBackend, "is_available", lambda self: (True, "Ready"))
    monkeypatch.setattr(claude_code, "_CONTEXT_WINDOWS", {})
    monkeypatch.setattr(claude_code, "_usage_refreshed_at", -float("inf"))
    monkeypatch.setattr(claude_code, "_usage_refresh", None)
    monkeypatch.setattr(claude_code, "_usage_refresh_client", None)


async def test_claude_adapter_reports_session_window_and_refreshes_account_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk_turn = lambda sdk: [  # noqa: E731
        sdk.SystemMessage(subtype="init", data={"model": "claude-opus-5[1m]"}, session_id="s1"),
        sdk.AssistantMessage(content=[sdk.TextBlock("ok")], model="claude-opus-5", message_id="r1",
                             usage={"input_tokens": 2, "cache_creation_input_tokens": 38000,
                                    "output_tokens": 4}),
        sdk.ResultMessage(subtype="success", result="ok", num_turns=1, session_id="s1", usage={},
                          model_usage={"claude-opus-5[1m]": {"contextWindow": 1_000_000},
                                       "claude-haiku-5-5": {"contextWindow": 200_000}}),
    ]
    control: list[dict] = []
    turns: list[list[object]] = []
    _fake_claude_sdk(monkeypatch, turns, control)
    sdk = sys.modules["claude_agent_sdk"]
    turns.extend([sdk_turn(sdk), sdk_turn(sdk)])
    request = AgentRunRequest(
        member=TeamMember(team_id="team", name="Dev", role_title="Developer"),
        task=None, session_key="usage-test", workdir=tmp_path, prompt="test", system_prompt="test",
    )
    first = [event async for event in claude_code.ClaudeCodeBackend().run(request)]
    await asyncio.sleep(0)
    text = next(event for event in first if event.kind == "text")
    assert "context_window" not in text.data  # nothing has reported it yet
    assert next(event for event in first if event.kind == "result").data["context_window"] == 1_000_000
    # The CLI's own `/usage` report, asked once and throttled across sessions.
    assert control == [{"subtype": "get_usage"}]
    assert usage.get_usage_limits("claude_code") == [
        {"window": "five_hour", "used_percent": 42.0, "resets_at": None}]

    second = [event async for event in claude_code.ClaudeCodeBackend().run(request)]
    await asyncio.sleep(0)
    assert next(event for event in second if event.kind == "text").data["context_window"] == 1_000_000
    assert len(control) == 1


async def test_claude_usage_request_dies_with_its_session_and_is_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short run closes before a Studio session answers `get_usage` (§6.1)."""
    control: list = ["silent"]
    turns: list[list[object]] = []
    _fake_claude_sdk(monkeypatch, turns, control)
    sdk = sys.modules["claude_agent_sdk"]
    result = lambda: [sdk.ResultMessage(subtype="success", result="ok", session_id="s1")]  # noqa: E731
    turns.extend([result(), result()])
    request = AgentRunRequest(
        member=TeamMember(team_id="team", name="Dev", role_title="Developer"),
        task=None, session_key="usage-test", workdir=tmp_path, prompt="test", system_prompt="test",
    )
    [event async for event in claude_code.ClaudeCodeBackend().run(request)]
    await asyncio.sleep(0)
    assert claude_code._usage_refresh is None  # cancelled with its session, not left to time out
    assert usage.get_usage_limits("claude_code") == []
    control[0] = "answering"
    [event async for event in claude_code.ClaudeCodeBackend().run(request)]
    await asyncio.sleep(0)
    assert control.count({"subtype": "get_usage"}) >= 2
    assert [item["used_percent"] for item in usage.get_usage_limits("claude_code")] == [42.0]
