"""REST and agent tools carry the explicit write set without inferring from prose (§10.5)."""

import pytest

from voxarium_studio.core.context import AppContext
from voxarium_studio.models.teams import TeamMember
from voxarium_studio.teams.tools import build_toolset

TEAM = "team_default"
DIRECTOR = f"{TEAM}__director"
DEV = f"{TEAM}__dev"
TASKS_URL = f"/api/teams/{TEAM}/tasks"


def test_api_write_paths_round_trip_replace_and_clear(client) -> None:
    paths = [f"src/file_{index:02}.py" for index in range(25)]
    response = client.post(TASKS_URL, json={
        "assignee_id": DEV,
        "title": "Change implementation",
        "brief": "Read docs/design.md for context.",
        "write_paths": paths,
    })
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["write_paths"] == paths, "a long explicit write set must not be truncated"
    task_url = f"{TASKS_URL}/{task['id']}"
    assert client.get(task_url).json()["write_paths"] == paths
    listed = client.get(TASKS_URL).json()
    assert next(row for row in listed if row["id"] == task["id"])["write_paths"] == paths

    unchanged = client.patch(task_url, json={"brief": "Read docs/new-design.md."})
    assert unchanged.status_code == 200
    assert unchanged.json()["write_paths"] == paths
    replacement = ["src/old.py", "src/new.py"]
    replaced = client.patch(task_url, json={"write_paths": replacement})
    assert replaced.status_code == 200
    assert replaced.json()["write_paths"] == replacement
    cleared = client.patch(task_url, json={"write_paths": []})
    assert cleared.status_code == 200
    assert cleared.json()["write_paths"] == []
    assert client.get(task_url).json()["write_paths"] == []


def test_api_omitted_write_paths_means_no_declared_writes(client) -> None:
    response = client.post(TASKS_URL, json={
        "assignee_id": DEV,
        "title": "Review src/shared.py",
        "brief": "Only read docs/design.md and src/shared.py.",
    })
    assert response.status_code == 201
    assert response.json()["write_paths"] == []


@pytest.mark.parametrize("invalid", [None, "src/a.py", [1], [""], ["src/*.py"], ["src/"]])
def test_api_invalid_write_paths_do_not_create_or_replace_a_write_set(client, invalid) -> None:
    rejected = client.post(TASKS_URL, json={
        "assignee_id": DEV, "title": "Invalid paths", "write_paths": invalid,
    })
    assert rejected.status_code in {400, 422}, rejected.text
    assert client.get(TASKS_URL).json() == []

    created = client.post(TASKS_URL, json={
        "assignee_id": DEV, "title": "Keep paths", "write_paths": ["src/keep.py"],
    })
    assert created.status_code == 201, created.text
    task_url = f"{TASKS_URL}/{created.json()['id']}"
    rejected = client.patch(task_url, json={"write_paths": invalid})
    assert rejected.status_code in {400, 422}, rejected.text
    assert client.get(task_url).json()["write_paths"] == ["src/keep.py"]


async def director_tools(ctx: AppContext):
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DIRECTOR)
    toolset = build_toolset(ctx.teams, member=member)
    if toolset is None:
        pytest.skip("claude-agent-sdk is not installed")
    return {definition.name: definition for definition in toolset.definitions}


def result_text(result: dict) -> str:
    return "\n".join(block.get("text", "") for block in result.get("content", []))


async def test_agent_tools_create_read_replace_and_clear_write_paths(ctx: AppContext) -> None:
    tools = await director_tools(ctx)
    paths = [f"src/file_{index:02}.py" for index in range(25)]
    created = await tools["create_task"].handler({
        "assignee_id": DEV,
        "title": "Agent write set",
        "brief": "Read docs/design.md before making the changes.",
        "write_paths": paths,
    })
    assert not created.get("is_error"), result_text(created)
    [task] = await ctx.teams.query_tasks(team_id=TEAM)
    assert task.write_paths == paths
    read = await tools["get_task"].handler({"task": task.id})
    assert "write paths: " + ", ".join(paths) in result_text(read)

    for fields, expected in [
        ({"brief": "Read docs/new-design.md."}, paths),
        ({"write_paths": ["src/old.py", "src/new.py"]}, ["src/old.py", "src/new.py"]),
        ({"write_paths": []}, []),
    ]:
        updated = await tools["update_task"].handler({"task": task.id, **fields})
        assert not updated.get("is_error"), result_text(updated)
        assert (await ctx.teams.find_task(task.id)).write_paths == expected
    read = await tools["get_task"].handler({"task": task.id})
    assert "write paths: (none; no path locks)" in result_text(read)


@pytest.mark.parametrize("invalid", [None, "src/a.py", [1], [""], ["src/*.py"], ["src/"]])
async def test_agent_tools_reject_invalid_write_paths(ctx: AppContext, invalid) -> None:
    tools = await director_tools(ctx)
    rejected = await tools["create_task"].handler({
        "assignee_id": DEV, "title": "Invalid paths", "brief": "", "write_paths": invalid,
    })
    assert rejected.get("is_error"), result_text(rejected)
    assert await ctx.teams.query_tasks(team_id=TEAM) == []

    task = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="Keep paths", brief="",
        write_paths=["src/keep.py"],
    )
    rejected = await tools["update_task"].handler({"task": task.id, "write_paths": invalid})
    assert rejected.get("is_error"), result_text(rejected)
    assert (await ctx.teams.find_task(task.id)).write_paths == ["src/keep.py"]
