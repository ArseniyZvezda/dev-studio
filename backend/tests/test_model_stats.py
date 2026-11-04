from datetime import datetime, timedelta, timezone

import pytest

from voxarium_studio.models.teams import AgentMessage, TeamTask
from voxarium_studio.teams.stats import cost_stats


def test_model_spend_uses_history_and_preserves_task_totals():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    task = TeamTask(id="a", team_id="team", assignee_id="member", title="a",
                    model="new-model", total_cost_usd=10, archived=True)
    inconsistent = TeamTask(id="b", team_id="team", assignee_id="member", title="b",
                            total_cost_usd=2)
    def entry(task_id, kind, data, minute, text=""):
        return AgentMessage(task_id=task_id, member_id="member", role="system", kind=kind,
                            data=data, text=text, created_at=start + timedelta(minutes=minute))
    messages = [
        entry("a", "system", {"model": "old-model"}, 0, "init"),
        entry("a", "result", {"total_cost_usd": 3}, 1),
        entry("a", "model", {"model": "second-model"}, 2),
        entry("a", "result", {"total_cost_usd": 2}, 3),
        entry("a", "result", {"model": "reported-model", "total_cost_usd": 4}, 4),
        entry("b", "result", {"model": "untrusted", "total_cost_usd": 99}, 5),
        entry("other-team", "result", {"model": "ignored", "total_cost_usd": 100}, 6),
    ]
    stats = cost_stats([task, inconsistent], list(reversed(messages)), [])
    assert stats["by_model"] == [
        {"model": "reported-model", "spent_usd": 4},
        {"model": None, "spent_usd": 3},
        {"model": "old-model", "spent_usd": 3},
        {"model": "second-model", "spent_usd": 2},
    ]
    assert sum(row["spent_usd"] for row in stats["by_model"]) == pytest.approx(stats["totals"]["spent_usd"])


def test_empty_model_spend():
    assert cost_stats([], [], [])["by_model"] == []
