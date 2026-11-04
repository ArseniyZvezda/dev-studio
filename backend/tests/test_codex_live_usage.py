"""Live Codex accounting from the selected rollout, without a network request (§10.6)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from voxarium_studio.models.teams import TeamMember, TeamTask
from voxarium_studio.teams.backends import codex
from voxarium_studio.teams.backends.base import AgentRunRequest


def token_count(
    input_tokens: int, output_tokens: int, *, cached: int = 0, context: int | None = None
) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": output_tokens // 2,
                    "total_tokens": input_tokens + output_tokens,
                },
                "last_token_usage": {"input_tokens": context if context is not None else input_tokens},
                "model_context_window": 258400,
            },
        },
    }


def rollout(home: Path, session: str = "thread-1") -> Path:
    path = home / "sessions" / "2026" / "11" / "04" / f"rollout-2026-11-04T10-00-00-{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def task_started() -> dict:
    return {"type": "event_msg", "payload": {"type": "task_started", "model_context_window": 258400}}


def test_rollout_resume_subtracts_history_and_uses_latest_context(tmp_path: Path) -> None:
    path = rollout(tmp_path)
    append(path, token_count(10000, 1000, cached=4000))
    reader = codex._RolloutUsage(tmp_path)
    reader.resume("thread-1")
    append(path, task_started())
    append(path, token_count(14000, 1200, cached=7000, context=4000))
    usage, context, window = reader.poll("thread-1", force=True)
    assert usage == {"input_tokens": 1000, "cache_read_input_tokens": 3000, "output_tokens": 200}
    assert context == 4000
    assert window == 258400
    # A repeated token_count often reports changed rate limits, not new tokens.
    append(path, token_count(14000, 1200, cached=7000, context=4000))
    assert reader.poll("thread-1", force=True) == (usage, context, window)
    # Compaction can shrink the context while lifetime accounting keeps growing.
    append(path, token_count(14500, 1250, cached=7000, context=500))
    usage, context, _ = reader.poll("thread-1", force=True)
    assert sum(usage.values()) == 4750
    assert context == 500


def test_rollout_handles_partial_oversized_invalid_and_other_thread_records(tmp_path: Path) -> None:
    path = rollout(tmp_path)
    append(rollout(tmp_path, "other-thread"), token_count(999999, 999999))
    record = json.dumps(token_count(120, 20)).encode()
    path.write_bytes(b"invalid\n" + b"x" * (codex._ROLLOUT_LINE_BYTES + 50) + b"\n" + record[:30])
    reader = codex._RolloutUsage(tmp_path)
    assert reader.poll("thread-1", force=True) == ({}, None, None)
    with path.open("ab") as stream:
        stream.write(record[30:] + b"\n")
    usage, context, window = reader.poll("thread-1", force=True)
    assert sum(usage.values()) == 140
    assert context == 120
    assert window == 258400


def test_rollout_missing_is_optional_and_window_can_arrive_before_tokens(tmp_path: Path) -> None:
    reader = codex._RolloutUsage(tmp_path)
    assert reader.poll("thread-1", force=True) is None
    assert reader.poll("../../other", force=True) is None
    path = rollout(tmp_path)
    append(path, {"type": "event_msg", "payload": {"type": "task_started", "model_context_window": 258400}})
    assert reader.poll("thread-1", force=True) == ({}, None, 258400)


def test_usage_snapshot_is_deduplicated_and_never_added_to_final_usage() -> None:
    state = codex._RunState(model="gpt-6-luna")
    state.set_live_usage({"input_tokens": 100, "cache_read_input_tokens": 200, "output_tokens": 50}, 300, 258400)
    event = state.usage_event()
    assert event is not None and event.text == "usage_update"
    assert event.data == {"subtype": "usage_update", "total_tokens": 350, "context_tokens": 300, "context_window": 258400}
    assert "usage" not in event.data  # transcript output-token counters must not double-count it
    assert state.usage_event() is None
    state.events({"type": "turn.completed", "usage": {"input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 50}})
    assert state.usage_event() is None
    assert state.result_data(exit_code=0, is_error=False)["total_tokens"] == 350
    # stdout can supply accounting missed while the rollout was unavailable.
    state.events({"type": "turn.completed", "usage": {"input_tokens": 400, "cached_input_tokens": 200, "output_tokens": 60}})
    assert state.usage_event().data["total_tokens"] == 460
    assert state.result_data(exit_code=0, is_error=False)["total_tokens"] == 460


def test_stdout_fallback_reports_spend_without_claiming_it_is_context() -> None:
    state = codex._RunState()
    state.events({"type": "turn.completed", "usage": {"input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 50}})
    assert state.usage_event().data == {"subtype": "usage_update", "total_tokens": 350}
    state.set_live_usage({"input_tokens": 100, "cache_read_input_tokens": 200, "output_tokens": 50}, 200, 258400)
    assert state.usage_event().data["context_tokens"] == 200
    # Context can shrink independently of cumulative spend after compaction.
    state.set_live_usage({"input_tokens": 100, "cache_read_input_tokens": 200, "output_tokens": 50}, 50, 258400)
    update = state.usage_event()
    assert update.data["context_tokens"] == 50
    assert update.data["total_tokens"] == 350


@pytest.mark.parametrize("resume", [False, True])
async def test_backend_emits_usage_while_subprocess_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    home = tmp_path / "codex-home"
    path = rollout(home)
    before_input, before_output, before_cache = (5000, 1000, 3000) if resume else (0, 0, 0)
    if resume:
        append(path, token_count(before_input, before_output, cached=before_cache))
    # A previous writer finishes after our pre-spawn read, before our own task_started.
    previous_writer = token_count(before_input + 1000, before_output + 100, cached=before_cache + 500)
    if resume:
        before_input += 1000
        before_output += 100
        before_cache += 500
    first = token_count(before_input + 300, before_output + 50, cached=before_cache + 200, context=300)
    second = token_count(before_input + 700, before_output + 80, cached=before_cache + 450, context=400)
    done = tmp_path / "finished"
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "sys.stdin.read()\n"
        f"path = Path({str(path)!r})\n"
        f"first, second = {first!r}, {second!r}\n"
        "def write(record):\n"
        "    with path.open('a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(record) + '\\n')\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-1'}), flush=True)\n"
        "print(json.dumps({'type': 'turn.started'}), flush=True)\n"
        + (f"write({previous_writer!r})\n" if resume else "")
        + f"write({task_started()!r})\n"
        "write(first)\n"
        "time.sleep(0.7)\n"
        "write(first)\n"
        "write(second)\n"
        "time.sleep(0.7)\n"
        f"Path({str(done)!r}).touch()\n"
        f"print(json.dumps({{'type': 'turn.completed', 'usage': {{'input_tokens': {before_input + 700}, 'cached_input_tokens': {before_cache + 450}, 'output_tokens': {before_output + 80}}}}}), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(codex, "codex_home", lambda: home)
    monkeypatch.setattr(codex, "_launcher", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(codex, "_has_credentials", lambda: True)
    request = AgentRunRequest(
        member=TeamMember(team_id="team", name="Dev", role_title="Developer", agent_backend="codex"),
        task=TeamTask(team_id="team", title="t", brief="b"),
        workdir=tmp_path,
        prompt="test",
        system_prompt="test",
        model="gpt-6-luna",
        resume_session_id="thread-1" if resume else None,
    )
    updates = []
    events = []
    async for event in codex.CodexBackend().run(request):
        events.append(event)
        if event.data.get("subtype") == "usage_update" and event.data["total_tokens"]:
            assert not done.exists(), "accounting must arrive before the process completes"
            assert event.session_id == "thread-1"
            updates.append(event.data)
    assert [event["total_tokens"] for event in updates] == [350, 780]
    assert [event["context_tokens"] for event in updates] == [300, 400]
    assert all(event["context_window"] == 258400 for event in updates)
    assert events[-1].kind == "result"
    assert events[-1].data["total_tokens"] == 780
    assert events[-1].data["usage"] == {"input_tokens": 250, "cache_read_input_tokens": 450, "output_tokens": 80}


def test_resume_without_baseline_does_not_charge_stdout_history() -> None:
    state = codex._RunState(resumed=True)
    state.events({"type": "turn.completed", "usage": {"input_tokens": 99999, "output_tokens": 9999}})
    assert state.usage_event() is None
    result = state.result_data(exit_code=0, is_error=False)
    assert result["total_tokens"] == 0
    assert result["cost_unavailable"] is True


def test_resume_waits_for_current_rollout_task_boundary(tmp_path: Path) -> None:
    path = rollout(tmp_path)
    append(path, token_count(1000, 100))
    reader = codex._RolloutUsage(tmp_path)
    reader.resume("thread-1")
    # A busy writer is still finishing; this is not the resumed process's usage.
    append(path, token_count(2000, 200))
    assert reader.poll("thread-1", force=True) is None
    assert reader.accounting_baseline is None
    append(path, task_started())
    append(path, token_count(2300, 250, context=300))
    usage, context, _ = reader.poll("thread-1", force=True)
    assert sum(usage.values()) == 350
    assert context == 300
    assert reader.accounting_baseline == {"input_tokens": 2000, "cached_input_tokens": 0, "output_tokens": 200}


def test_resume_with_late_discovery_never_treats_historical_start_as_its_own(tmp_path: Path) -> None:
    reader = codex._RolloutUsage(tmp_path)
    reader.resume("thread-1")
    path = rollout(tmp_path)
    append(path, task_started())
    append(path, token_count(99999, 9999))
    append(path, task_started())
    append(path, token_count(100299, 10049))
    assert reader.poll("thread-1", force=True) is None
    assert reader.accounting_baseline is None


async def test_failed_busy_resume_does_not_account_for_external_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = rollout(tmp_path / "codex-home")
    append(path, token_count(1000, 100))
    script = tmp_path / "busy_codex.py"
    script.write_text(
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        f"with open({str(path)!r}, 'a', encoding='utf-8') as stream:\n"
        f"    stream.write(json.dumps({token_count(5000, 500)!r}) + '\\n')\n"
        "time.sleep(0.6)\n"
        "print(json.dumps({'type': 'error', 'message': 'thread thread-1 already has an active writer'}), flush=True)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(codex, "codex_home", lambda: tmp_path / "codex-home")
    monkeypatch.setattr(codex, "_launcher", lambda: [sys.executable, str(script)])
    request = AgentRunRequest(
        member=TeamMember(team_id="team", name="Dev", role_title="Developer", agent_backend="codex"),
        task=TeamTask(team_id="team", title="t", brief="b"),
        workdir=tmp_path,
        prompt="test",
        system_prompt="test",
        resume_session_id="thread-1",
    )
    events = [event async for event in codex.CodexBackend()._run_once(request)]
    assert not any(event.data.get("subtype") == "usage_update" for event in events)
    assert events[-1].kind == "result"
    assert events[-1].data["total_tokens"] == 0
