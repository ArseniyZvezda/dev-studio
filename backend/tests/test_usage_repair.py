"""Offline journal recovery must prove coverage before touching historical tasks."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from voxarium_studio.teams.usage_repair import apply_audit, build_audit, parse_claude, parse_codex


def write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")


def frame(kind, payload, minute=0, second=0):
    return {"type": kind, "timestamp": f"2026-11-01T10:{minute:02d}:{second:02d}Z", "payload": payload}


def count(inputs, cached, outputs, minute):
    return frame("event_msg", {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": inputs, "cached_input_tokens": cached, "output_tokens": outputs,
        "reasoning_output_tokens": outputs // 2, "total_tokens": inputs + outputs,
    }}}, minute)


def codex_records(project, sid="session-a", label="TT-1"):
    return [
        frame("session_meta", {"id": sid, "cwd": str(project), "source": "exec"}),
        frame("response_item", {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": f"You are working on task {label}."}]}),
        frame("event_msg", {"type": "task_started"}),
        frame("turn_context", {"model": "gpt-6-astra"}),
        count(1000, 800, 20, 1), count(1000, 800, 20, 1),
        frame("event_msg", {"type": "task_complete"}, 1),
        frame("event_msg", {"type": "task_started"}, 1, 30),
        count(1500, 1200, 30, 2),
        frame("event_msg", {"type": "task_complete"}, 2),
    ]


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "project"
    data = project / "voxarium_studio"
    data.mkdir(parents=True)
    (data / "config.yaml").write_text("project_code: TT\n", encoding="utf-8")
    with sqlite3.connect(data / "app.db") as c:
        c.execute("""CREATE TABLE team_task (id TEXT PRIMARY KEY,number INTEGER,title TEXT,
            status TEXT,assignee_id TEXT,agent_session_id TEXT,created_at TEXT,finished_at TEXT,
            total_tokens INTEGER,total_cost_usd REAL,brief TEXT)""")
        c.execute("""CREATE TABLE agent_message (id TEXT PRIMARY KEY,task_id TEXT,member_id TEXT,
            role TEXT,kind TEXT,text TEXT,data TEXT,created_at TEXT)""")
        c.execute("INSERT INTO team_task VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "task-a", 1, "Task A", "done", "member", "session-a",
            "2026-11-01 09:59:00", "2026-11-01 10:03:00", 2550, .0095, "preserve this brief"))
        for index, (tokens, cost) in enumerate([(1020, .0038), (1530, .0057)], 1):
            c.execute("INSERT INTO agent_message VALUES (?,?,?,?,?,?,?,?)", (
                f"msg-{index}", "task-a", "member", "system", "result", "original transcript",
                json.dumps({"total_tokens": tokens, "total_cost_usd": cost, "model": "gpt-6-astra"}),
                f"2026-11-01 10:0{index}:05"))
    codex = tmp_path / "codex"
    path = codex / "sessions" / "rollout-date-session-a.jsonl"
    write_records(path, codex_records(project))
    return project, tmp_path / "claude", codex, path


def audit_fixture(project):
    root, claude, codex, _ = project
    return build_audit(root, claude_home=claude, codex_home=codex)


def test_codex_cumulative_resume_cache_and_repeated_snapshots(project):
    journal = parse_codex(project[3])
    assert journal["issues"] == []
    assert len(journal["events"]) == 2
    assert [e["data"]["total_tokens"] for e in journal["events"]] == [1020, 510]
    assert sum(e["data"]["total_cached_tokens"] for e in journal["events"]) == 1200
    assert sum(e["data"]["total_cost_usd"] for e in journal["events"]) == pytest.approx(.0057)
    audit = audit_fixture(project)
    row = audit["tasks"][0]
    assert row["coverage"] == "exact"
    assert row["after"] == {"total_tokens": 1530, "total_uncached_tokens": 330,
                            "total_cached_tokens": 1200, "total_cost_usd": .0057}


def test_claude_message_blocks_and_internal_cost_state_usage_are_not_counted_twice(tmp_path):
    def assistant(identity, usage, minute):
        return {"type": "assistant", "sessionId": "claude-a", "timestamp": f"2026-11-01T10:0{minute}:00Z",
                "message": {"id": identity, "model": "claude-test", "usage": usage}}
    one = {"input_tokens": 20, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 50, "output_tokens": 10}
    state = {"type": "cost-state", "sessionId": "claude-a", "totalCostUSD": .05,
             "modelUsage": {"claude-test": {"inputTokens": 22, "cacheCreationInputTokens": 30,
                                            "cacheReadInputTokens": 60, "outputTokens": 16, "costUSD": .05}}}
    path = tmp_path / "claude-a.jsonl"
    write_records(path, [assistant("msg-one", one, 1), assistant("msg-one", one, 1),
                         assistant("msg-one", {**one, "output_tokens": 15}, 1), state, state,
                         assistant("msg-two", {"input_tokens": 3, "cache_read_input_tokens": 100, "output_tokens": 7}, 2),
                         {**state, "totalCostUSD": .08, "modelUsage": {"claude-test": {
                             "inputTokens": 25, "cacheCreationInputTokens": 30,
                             "cacheReadInputTokens": 160, "outputTokens": 23, "costUSD": .08}}}])
    journal = parse_claude(path)
    assert not journal["issues"]
    assert sum(e["data"]["total_tokens"] for e in journal["events"]) == 238
    assert sum(e["data"]["total_cached_tokens"] for e in journal["events"]) == 160
    assert sum(e["data"]["total_cost_usd"] for e in journal["events"]) == pytest.approx(.08)


def test_missing_older_session_prevents_replacing_lifetime_with_latest(project):
    root = project[0]
    with sqlite3.connect(root / "voxarium_studio/app.db") as c:
        c.execute("INSERT INTO agent_message VALUES (?,?,?,?,?,?,?,?)", (
            "missing-init", "task-a", "member", "system", "system", "thread.started",
            json.dumps({"thread_id": "older-missing-session"}), "2026-11-01 10:00:01"))
    row = audit_fixture(project)["tasks"][0]
    assert row["coverage"] == "partial"
    assert row["missing_sessions"] == ["older-missing-session"]
    assert "after" not in row


def test_duplicate_codex_archive_copy_does_not_add_the_same_session_again(project):
    duplicate = project[2] / "archived_sessions" / project[3].name
    duplicate.parent.mkdir()
    shutil.copyfile(project[3], duplicate)
    row = audit_fixture(project)["tasks"][0]
    assert row["coverage"] == "exact"
    assert row["after"]["total_tokens"] == 1530
    assert len(row["source_paths"]) == 1


def test_conflicting_codex_archive_copy_blocks_apply(project):
    duplicate = project[2] / "archived_sessions" / project[3].name
    write_records(duplicate, codex_records(project[0])[:-2])
    row = audit_fixture(project)["tasks"][0]
    assert row["coverage"] == "partial"
    assert "conflicting_journal_versions" in row["reasons"]


def test_legacy_token_proof_can_repair_tokens_while_preserving_unverified_cost(project):
    with sqlite3.connect(project[0] / "voxarium_studio/app.db") as c:
        c.execute("DELETE FROM agent_message")
        c.execute("UPDATE team_task SET total_cost_usd=.0105")
    row = audit_fixture(project)["tasks"][0]
    assert row["coverage"] == "exact"
    assert row["token_status"] == "exact" and row["cost_status"] == "unverified"
    assert row["after"]["total_tokens"] == 1530
    assert row["after"]["total_cost_usd"] == .0105
    assert sum(e["data"]["total_cost_usd"] for e in row["evidence"]) == pytest.approx(.0105)
    assert row["evidence"][-1]["data"]["repair_source"] == "preserved_unverified_cost"


def test_apply_only_changes_counters_and_adds_provenance_with_backup(project, tmp_path):
    report = audit_fixture(project)
    source = project[0] / "voxarium_studio/app.db"
    copy = tmp_path / "rehearsal.db"
    shutil.copyfile(source, copy)
    result = apply_audit(report, database_path=copy)
    assert result["applied"] == 1
    with sqlite3.connect(copy) as c:
        assert c.execute("SELECT total_tokens,total_uncached_tokens,total_cached_tokens,total_cost_usd,brief FROM team_task").fetchone() == (
            1530, 330, 1200, .0057, "preserve this brief")
        assert c.execute("SELECT COUNT(*) FROM agent_message WHERE text='original transcript'").fetchone()[0] == 2
        data = json.loads(c.execute("SELECT data FROM agent_message WHERE kind='accounting'").fetchone()[0])
        assert data["repair"]["audit_sha256"] == report["audit_sha256"]
        assert data["repair"]["cutoff_at"] == "2026-11-01T10:02:05+00:00"
    for original in [source, Path(result["backup"])]:
        with sqlite3.connect(original) as c:
            assert c.execute("SELECT total_tokens,total_cost_usd FROM team_task").fetchone() == (2550, .0095)


def test_changed_task_aborts_entire_apply(project):
    report = audit_fixture(project)
    db = project[0] / "voxarium_studio/app.db"
    with sqlite3.connect(db) as c:
        c.execute("UPDATE team_task SET status='in_progress'")
    with pytest.raises(ValueError, match="Task changed"):
        apply_audit(report)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT total_tokens FROM team_task").fetchone()[0] == 2550
        assert c.execute("SELECT COUNT(*) FROM agent_message WHERE kind='accounting'").fetchone()[0] == 0


def test_changed_journal_aborts_before_backup_or_mutation(project):
    report = audit_fixture(project)
    with project[3].open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(frame("event_msg", {"type": "task_started"}, 3)) + "\n")
    with pytest.raises(ValueError, match="journal changed"):
        apply_audit(report)


def test_unrelated_task_number_in_user_text_is_not_association(project):
    records = codex_records(project[0], sid="unrelated", label="TT-1")
    records[1]["payload"]["role"] = "user"
    write_records(project[2] / "sessions/rollout-date-unrelated.jsonl", records)
    row = audit_fixture(project)["tasks"][0]
    assert row["after"]["total_tokens"] == 1530
    assert len(row["source_paths"]) == 1


def test_summary_matches_unique_cost_model_and_completion_time(project):
    records = codex_records(project[0], sid="summary")[:7]
    records[1]["payload"]["content"][0]["text"] = "Summarize this task history for continuation."
    records[4] = records[5] = count(1000, 800, 28, 1)
    write_records(project[2] / "sessions/rollout-date-summary.jsonl", records)
    with sqlite3.connect(project[0] / "voxarium_studio/app.db") as c:
        c.execute("UPDATE team_task SET total_tokens=3578,total_cost_usd=.0137")
        c.execute("INSERT INTO agent_message VALUES (?,?,?,?,?,?,?,?)", (
            "summary-result", "task-a", "member", "system", "result", "",
            json.dumps({"total_tokens": 1028, "total_cost_usd": .0042, "model": "gpt-6-astra"}),
            "2026-11-01 10:01:10"))
    row = audit_fixture(project)["tasks"][0]
    assert any(s["association"] == "unique_summary_cost_model_completion_time" for s in row["sources"])
    assert row["coverage"] == "exact"
    assert row["after"]["total_tokens"] == 2558


def test_summary_cannot_reuse_result_already_explained_by_worker(project):
    records = codex_records(project[0], sid="summary")[:7]
    records[1]["payload"]["content"][0]["text"] = "Summarize this task history for continuation."
    write_records(project[2] / "sessions/rollout-date-summary.jsonl", records)
    row = audit_fixture(project)["tasks"][0]
    assert len(row["sources"]) == 1
    assert row["after"]["total_tokens"] == 1530


def test_unaccounted_token_only_balance_blocks_repair(project):
    with sqlite3.connect(project[0] / "voxarium_studio/app.db") as c:
        c.execute("UPDATE team_task SET total_tokens=5550")
    row = audit_fixture(project)["tasks"][0]
    assert row["coverage"] == "partial"
    assert "unexplained_task_token_balance" in row["reasons"]
    assert "after" not in row


def test_shared_parent_workdir_and_task_number_alone_do_not_prove_project(project):
    with sqlite3.connect(project[0] / "voxarium_studio/app.db") as c:
        c.execute("UPDATE team_task SET agent_session_id=NULL")
    records = codex_records(project[0].parent)
    write_records(project[3], records)
    row = audit_fixture(project)["tasks"][0]
    assert row["coverage"] == "unrecoverable"
    assert not row["sources"]
    records[1]["payload"]["content"][0]["text"] += f" The project you are working on is at {project[0]};"
    write_records(project[3], records)
    assert audit_fixture(project)["tasks"][0]["coverage"] == "exact"


def test_non_terminal_task_is_never_an_apply_candidate(project):
    with sqlite3.connect(project[0] / "voxarium_studio/app.db") as c:
        c.execute("UPDATE team_task SET status='running'")
    row = audit_fixture(project)["tasks"][0]
    assert row["coverage"] == "partial" and "after" not in row


def test_overlapping_missing_session_is_not_covered_by_another_journals_dates(project):
    with sqlite3.connect(project[0] / "voxarium_studio/app.db") as c:
        c.execute("INSERT INTO agent_message VALUES (?,?,?,?,?,?,?,?)", (
            "missing-result", "task-a", "member", "system", "result", "missing session",
            json.dumps({"total_tokens": 100, "total_cost_usd": .001}), "2026-11-01 10:01:30"))
        c.execute("UPDATE team_task SET total_tokens=2650,total_cost_usd=.0105")
    row = audit_fixture(project)["tasks"][0]
    assert row["coverage"] == "partial" and "after" not in row
    assert "original_results_without_matching_journal" in row["reasons"]


def test_reset_excludes_disowned_journals_with_the_same_task_identity(project):
    old_path = project[2] / "sessions/rollout-date-disowned.jsonl"
    write_records(old_path, codex_records(project[0], sid="disowned"))
    records = codex_records(project[0])
    for record in records:
        minute = int(record["timestamp"][14:16]) + 5
        record["timestamp"] = record["timestamp"][:14] + f"{minute:02d}" + record["timestamp"][16:]
    write_records(project[3], records)
    with sqlite3.connect(project[0] / "voxarium_studio/app.db") as c:
        c.execute("UPDATE team_task SET finished_at='2026-11-01 10:08:00'")
        c.execute("UPDATE agent_message SET created_at='2026-11-01 10:06:05' WHERE id='msg-1'")
        c.execute("UPDATE agent_message SET created_at='2026-11-01 10:07:05' WHERE id='msg-2'")
        c.execute("INSERT INTO agent_message VALUES (?,?,?,?,?,?,?,?)", (
            "reset", "task-a", "member", "system", "status", "reset",
            json.dumps({"reason": "reset"}), "2026-11-01 10:03:00"))
    row = audit_fixture(project)["tasks"][0]
    assert row["coverage"] == "exact"
    assert row["after"]["total_tokens"] == 1530
    assert [source["session_id"] for source in row["sources"]] == ["session-a"]
