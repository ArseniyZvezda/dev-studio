"""Known token classes reconcile across every statistics projection (§10.4)."""

from datetime import datetime, timezone

from voxarium_studio.models.teams import AgentMessage, TeamTask, TaskStatus
from voxarium_studio.teams.stats import cost_stats


def moment(day):
    return datetime(2026, 11, day, 12, tzinfo=timezone.utc)


def make_task(task_id, total, uncached, cached, **kwargs):
    return TeamTask(id=task_id, team_id="team", assignee_id="member", title=task_id,
                    total_tokens=total, total_uncached_tokens=uncached,
                    total_cached_tokens=cached, created_at=moment(1), **kwargs)


def result(task_id, day, total, uncached, cached, model):
    return AgentMessage(task_id=task_id, member_id="member", role="system", kind="result",
                        created_at=moment(day), data={"total_tokens": total,
                            "total_uncached_tokens": uncached, "total_cached_tokens": cached,
                            "model": model})


def assert_partition(row, prefix):
    assert row[f"{prefix}_tokens"] == sum(row[f"{prefix}_{kind}_tokens"]
                                         for kind in ("uncached", "cached", "unclassified"))


def test_split_is_consistent_in_totals_timeline_models_members_activity_and_averages():
    tasks = [
        make_task("known", 1000, 100, 900, status=TaskStatus.DONE, archived=True, finished_at=moment(2)),
        make_task("legacy", 400, 0, 0, status=TaskStatus.DONE, finished_at=moment(2)),
        make_task("cancelled", 60, 20, 40, status=TaskStatus.CANCELLED, finished_at=moment(3)),
    ]
    messages = [result("known", 1, 300, 30, 270, "first"),
                result("known", 2, 700, 70, 630, "second")]
    stats = cost_stats(tasks, messages, [])
    totals = stats["totals"]
    assert (totals["spent_uncached_tokens"], totals["spent_cached_tokens"],
            totals["spent_unclassified_tokens"]) == (120, 940, 400)
    for prefix in ("spent", "done", "in_flight", "cancelled"):
        assert_partition(totals, prefix)
    for rows in (stats["token_series"], stats["by_model_tokens"], stats["by_member"], stats["activity"]):
        for row in rows:
            assert_partition(row, "spent")
        for kind in ("tokens", "uncached_tokens", "cached_tokens", "unclassified_tokens"):
            assert sum(row[f"spent_{kind}"] for row in rows) == totals[f"spent_{kind}"]
    assert stats["token_series"][-1]["cumulative_cached_tokens"] == 940
    average = stats["average_task_series"][0]
    assert (average["tasks"], average["average_tokens"], average["average_uncached_tokens"],
            average["average_cached_tokens"], average["average_unclassified_tokens"]) == (2, 700, 50, 450, 200)


def test_legacy_unknown_balance_is_not_presented_as_uncached():
    stats = cost_stats([make_task("old", 9000000, 0, 0, finished_at=moment(2))], [], [])
    assert stats["totals"]["spent_uncached_tokens"] == 0
    assert stats["totals"]["spent_cached_tokens"] == 0
    assert stats["totals"]["spent_unclassified_tokens"] == 9000000


def test_repaired_task_parts_override_stale_unsplit_results_without_negative_buckets():
    task = make_task("fixed", 100, 20, 80, finished_at=moment(2))
    old = result("fixed", 1, 100, 0, 0, "stale")
    stats = cost_stats([task], [old], [])
    assert len(stats["token_series"]) == 1
    row = stats["token_series"][0]
    assert row["day"] == "2026-11-02"
    assert (row["spent_uncached_tokens"], row["spent_cached_tokens"], row["spent_unclassified_tokens"]) == (20, 80, 0)
    assert stats["by_model_tokens"][0]["model"] is None


def test_live_residual_splits_add_only_uncredited_balances():
    task = make_task("live", 130, 30, 100, started_at=moment(1))
    messages = [result("live", 1, 100, 20, 80, "model"),
                AgentMessage(task_id="live", member_id="member", role="system", kind="system",
                    created_at=moment(3), data={"subtype": "usage_update", "task_total_tokens": 130})]
    stats = cost_stats([task], messages, [])
    assert [(row["spent_uncached_tokens"], row["spent_cached_tokens"]) for row in stats["token_series"]] == [(20, 80), (10, 20)]
    assert stats["token_series"][-1]["day"] == "2026-11-03"
