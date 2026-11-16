"""Final integration seam: copied entity IDs + delayed scoped mutation (§5.3.2).

Reuse VS-25's in-process A/B fixture, including colliding plugin IDs and paths.
This is not another transport matrix: inspect committed SQLite rows, files and
local/legacy event histories after the request crosses a legacy selection change.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from test_project_resources import resources  # noqa: F401 -- existing pytest fixture

from voxarium_studio.models.teams import TeamTask
from voxarium_studio.ws.events import EventType


async def test_delayed_copied_task_patch_commits_and_emits_only_in_its_project(resources):
    app, http, (a, b) = resources
    contexts = [app.state.project_registry[s["project_id"]].ctx for s in (a, b)]
    task_id = "task_copied"
    suffix = f"teams/team_default/tasks/{task_id}"
    for ctx, title in zip(contexts, ("Alpha before", "Bravo before"), strict=True):
        async with ctx.db.session() as session:
            session.add(TeamTask(
                id=task_id, number=999, team_id="team_default",
                assignee_id="team_default__dev", title=title, auto_start=False,
            ))
    sentinels = {ctx.paths.root / "same.txt": (ctx.paths.root / "same.txt").read_bytes()
                 for ctx in contexts}
    cursors = [ctx.bus.current_seq for ctx in contexts]
    started, release = asyncio.Event(), asyncio.Event()
    body = json.dumps({"title": "Alpha after"}).encode()

    async def chunks():
        yield body[:10]
        started.set()
        await release.wait()
        yield body[10:]

    pending = asyncio.create_task(http.patch(
        f"/api/projects/{a['project_id']}/{suffix}", content=chunks(),
        headers={"Content-Type": "application/json"},
    ))
    try:
        await asyncio.wait_for(started.wait(), 10)
        selected = await http.post("/api/project/open", json={"path": b["root"], "create": False})
        assert selected.status_code == 200, selected.text
        legacy_cursor = app.state.browser_bus.current_seq
        # Mutate the same ID through legacy while A's body is still suspended.
        changed_b = await http.patch(f"/api/{suffix}", json={"title": "Bravo after"})
        assert changed_b.status_code == 200, changed_b.text
        assert not pending.done()
        release.set()
        changed_a = await asyncio.wait_for(pending, 10)
        assert changed_a.status_code == 200, changed_a.text
        assert app.state.ctx is contexts[1]

        for ctx, cursor, expected, foreign in zip(
            contexts, cursors, ("Alpha after", "Bravo after"),
            ("Bravo after", "Alpha after"), strict=True,
        ):
            # A separate disk connection proves commits, not an ORM identity-map/HTTP echo.
            with sqlite3.connect(f"{ctx.paths.db_file.as_uri()}?mode=ro", uri=True) as db:
                rows = db.execute("SELECT title, status FROM team_task WHERE id = ?", (task_id,)).fetchall()
                assert rows == [(expected, "todo")]
                assert db.execute("SELECT count(*) FROM team_task WHERE title = ?", (foreign,)).fetchone() == (0,)
                audit = db.execute("SELECT text, data FROM agent_message WHERE task_id = ?", (task_id,)).fetchall()
                assert expected in json.dumps(audit)
                assert foreign not in json.dumps(audit)
            events = [event.data["title"] for event in ctx.bus.replay(cursor)
                      if event.type == EventType.TEAM_TASK_UPDATED and event.data["task_id"] == task_id]
            assert events == [expected]
        legacy_events = [event.data["title"] for event in app.state.browser_bus.replay(legacy_cursor)
                         if event.type == EventType.TEAM_TASK_UPDATED and event.data["task_id"] == task_id]
        assert legacy_events == ["Bravo after"]
        assert all(path.read_bytes() == content for path, content in sentinels.items())
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
