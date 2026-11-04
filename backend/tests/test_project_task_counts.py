"""Project summaries never activate a recently opened project (§5.1)."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.task_counts import read_task_counts
from voxarium_studio.core.workspace import record_recent
from voxarium_studio.models.teams import TaskStatus, TeamMember, TeamTask


def seed_counts(root: Path) -> Path:
    path = ProjectPaths.for_project(root).db_file
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE team_task (team_id TEXT, status TEXT)")
        db.executemany(
            "INSERT INTO team_task VALUES (?, ?)",
            [("one", "in_progress"), ("two", "in_progress"), ("two", "needs_review")]
            + [("one", status) for status in ("todo", "pending", "blocked", "done", "cancelled")],
        )
    return path


def test_counts_include_all_teams_and_only_requested_statuses(tmp_path: Path) -> None:
    path = seed_counts(tmp_path)
    before = path.read_bytes()
    assert read_task_counts(tmp_path) == {"in_progress": 2, "needs_review": 1}
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_task SET status = 'done' WHERE status = 'in_progress'")
    assert read_task_counts(tmp_path) == {"in_progress": 0, "needs_review": 1}


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    assert read_task_counts(tmp_path) == {"in_progress": 0, "needs_review": 0}
    assert list(tmp_path.iterdir()) == []
    assert read_task_counts(tmp_path / "gone") is None


def test_unreadable_or_old_database_is_not_reported_as_empty(tmp_path: Path) -> None:
    path = ProjectPaths.for_project(tmp_path).db_file
    path.parent.mkdir()
    path.write_bytes(b"not a SQLite database")
    assert read_task_counts(tmp_path) is None
    path.write_bytes(b"")
    assert read_task_counts(tmp_path) is None
    assert path.read_bytes() == b""


def test_recent_and_footer_endpoints_keep_current_project(
    client: TestClient, project: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other # игра"
    path = seed_counts(other)
    before = path.read_bytes()
    record_recent(other, "Other")

    entries = client.get("/api/project/recent").json()
    assert entries[0]["task_counts"] == {"in_progress": 2, "needs_review": 1}
    assert client.get("/api/project/task-counts").json() == {"in_progress": 0, "needs_review": 0}
    assert Path(client.get("/api/project").json()["root"]) == project.resolve()
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]

    remaining = client.delete("/api/project/recent", params={"path": str(project)}).json()
    assert remaining[0]["task_counts"] == entries[0]["task_counts"]


def test_footer_counts_follow_committed_task_statuses(client: TestClient, project: Path) -> None:
    engine = create_engine(f"sqlite:///{ProjectPaths.for_project(project).db_file.as_posix()}")
    try:
        with Session(engine) as db:
            member = db.exec(select(TeamMember)).first()
            assert member is not None
            task = TeamTask(
                title="Counting only, no agent run",
                team_id=member.team_id,
                assignee_id=member.id,
                status=TaskStatus.IN_PROGRESS,
                auto_start=False,
            )
            db.add(task)
            db.commit()
            assert client.get("/api/project/task-counts").json() == {
                "in_progress": 1, "needs_review": 0,
            }
            task.status = TaskStatus.NEEDS_REVIEW
            db.add(task)
            db.commit()
            expected = {"in_progress": 0, "needs_review": 1}
            assert client.get("/api/project/task-counts").json() == expected
            entries = client.get("/api/project/recent").json()
            current = next(entry for entry in entries if Path(entry["root"]) == project.resolve())
            assert current["task_counts"] == expected
    finally:
        engine.dispose()
