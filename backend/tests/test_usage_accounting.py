"""Provider response identity, cache classification and cumulative Claude billing."""

import json
from types import SimpleNamespace

import pytest
from sqlmodel import select

from voxarium_studio.models.teams import AgentMessage, TeamMember, TeamTask
from voxarium_studio.teams.backends import claude_code
from voxarium_studio.teams.backends.base import AgentEvent, AgentRunRequest
from voxarium_studio.teams.usage import RunUsage


def response(response_id, output, **kwargs):
    return AgentEvent(kind="text", data={"provider_response_id": response_id, "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 1000, "output_tokens": output,
    }, **kwargs})


def test_response_frames_credit_each_token_once_without_losing_output_deltas():
    usage = RunUsage()
    for output in (5, 20, 20, 8, 30):
        usage.observe(response("msg-one", output))
    assert (usage.total, usage.uncached, usage.cached) == (1060, 60, 1000)
    usage.observe(response("msg-two", 10))
    assert (usage.total, usage.uncached, usage.cached) == (2100, 100, 2000)
    usage.finish({"total_tokens": 2100, "usage": {"input_tokens": 20,
        "cache_creation_input_tokens": 40, "cache_read_input_tokens": 2000, "output_tokens": 40}})
    assert usage.total == 2100
    usage.next_segment()
    usage.observe(response("msg-one", 30))
    assert usage.total == 0, "a replay across a continued segment is already credited"
    usage.observe(response("msg-three", 10))
    assert usage.total == 1040


def test_unknown_legacy_total_is_not_misreported_as_uncached_usage():
    usage = RunUsage()
    usage.finish({"total_tokens": 5000})
    assert usage.counters() == {"total_tokens": 5000, "total_uncached_tokens": 0,
                               "total_cached_tokens": 0, "accounting_version": 2}


def test_claude_adapter_preserves_api_response_identity_and_classification():
    block = type("TextBlock", (), {"text": "a response"})()
    event = claude_code._assistant_events(SimpleNamespace(
        content=[block], message_id="msg-original", usage={"input_tokens": 10,
        "cache_creation_input_tokens": 20, "cache_read_input_tokens": 1000, "output_tokens": 30},
    ), "session")[0]
    assert event.data["provider_response_id"] == "msg-original"
    result = claude_code._result_data(SimpleNamespace(usage=event.data["usage"]))
    assert (result["total_tokens"], result["total_uncached_tokens"], result["total_cached_tokens"]) == (1060, 60, 1000)


def test_claude_cost_is_delta_while_tokens_remain_per_invocation():
    counter = claude_code._SessionCost("session", 10)
    first = counter.apply({"total_cost_usd": 12, "total_tokens": 100}, "session")
    assert counter.apply({"total_cost_usd": 0}, "session")["total_cost_usd"] == 0
    second = counter.apply({"total_cost_usd": 15, "total_tokens": 200}, "session")
    assert [first["total_cost_usd"], second["total_cost_usd"]] == [2, 3]
    assert [first["total_tokens"], second["total_tokens"]] == [100, 200]
    assert first["session_total_cost_usd"] == 12
    assert first["session_cost_baseline_usd"] == 10
    assert counter.apply({"total_cost_usd": 4}, "new-session")["total_cost_usd"] == 4


def test_missing_resume_cost_baseline_does_not_charge_old_session_again():
    counter = claude_code._SessionCost("session", None)
    unknown = counter.apply({"total_cost_usd": 100}, "session")
    assert unknown["total_cost_usd"] == 0 and unknown["cost_unavailable"]
    assert counter.apply({"total_cost_usd": 105}, "session")["total_cost_usd"] == 5


def test_claude_baseline_reads_latest_cost_state_without_loading_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    path = tmp_path / "projects" / "test-project" / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join([
        json.dumps({"type": "cost-state", "totalCostUSD": 10}),
        json.dumps({"type": "assistant", "text": "x" * 160000}),
        json.dumps({"type": "cost-state", "totalCostUSD": 12.5}),
        json.dumps({"type": "assistant", "text": "я" * 100000}),
    ]), encoding="utf-8")
    assert claude_code._session_cost_baseline("session") == 12.5
    assert claude_code._session_cost_baseline("missing") is None
    assert claude_code._session_cost_baseline(None) == 0


async def test_live_task_dedup_split_and_result_reconciliation_survive_database_reload(ctx):
    task = await ctx.teams.create_task(team_id="team_default", assignee_id="team_default__dev",
                                       title="Token accounting", brief="Work", auto_start=False)
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, task.assignee_id)
    request = AgentRunRequest(member=member, task=task, workdir=ctx.paths.root,
                              prompt="Work", system_prompt="")

    class Backend:
        async def run(self, _request):
            yield response("msg-one", 5)
            yield response("msg-one", 20)
            yield response("msg-one", 20)
            yield AgentEvent(kind="result", data={"total_tokens": 1060, "usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 1000, "output_tokens": 30}, "continued": True})
            yield response("msg-two", 10)
            yield AgentEvent(kind="result", data={"total_tokens": 1040, "usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 1000, "output_tokens": 10}})

    await ctx.teams._drain_events(Backend(), request, task.id, task.assignee_id)
    async with ctx.db.session() as session:
        saved = await session.get(TeamTask, task.id)
        assert (saved.total_tokens, saved.total_uncached_tokens, saved.total_cached_tokens) == (2100, 100, 2000)
        results = (await session.exec(select(AgentMessage).where(
            AgentMessage.task_id == task.id, AgentMessage.kind == "result",
        ).order_by(AgentMessage.created_at))).all()
        assert [(row.data["total_tokens"], row.data["total_uncached_tokens"], row.data["total_cached_tokens"])
                for row in results] == [(1060, 60, 1000), (1040, 40, 1000)]
