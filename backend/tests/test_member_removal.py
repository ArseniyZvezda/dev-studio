"""Protected roles and confirmed removal retain work and stop sessions (§10.2)."""

import asyncio
from contextlib import asynccontextmanager

import pytest
import yaml
from fastapi import HTTPException
from sqlmodel import select

from voxarium_studio.api.teams import delete_member
from voxarium_studio.models.teams import AgentChat, AgentMessage, TaskStatus, TeamMember, TeamTask
from voxarium_studio.teams.backends.base import AgentEvent, register_backend
from voxarium_studio.teams.chat import UserContext

TEAM = "team_default"
DIRECTOR = f"{TEAM}__director"
PRODUCER = f"{TEAM}__studio-producer"
DEV = f"{TEAM}__dev"
DESIGNER = f"{TEAM}__designer"


@pytest.mark.parametrize("member_id,kind,builtin", [
    (DIRECTOR, "agent", "director"), (PRODUCER, "human", "producer"),
])
def test_reserved_roles_cannot_change_kind_or_be_deleted(client, member_id, kind, builtin):
    url = f"/api/teams/{TEAM}/members/{member_id}"
    renamed = client.patch(url, json={"name": "Renamed role", "role_title": "Custom title"})
    assert renamed.status_code == 200
    assert renamed.json()["builtin"] == builtin
    assert client.patch(url, json={"kind": kind}).status_code == 400
    for query in ("", "?reassign_to_director=true"):
        assert client.delete(url + query).status_code == 409


def test_delete_http_requires_explicit_transfer_confirmation(client):
    created = client.post(f"/api/teams/{TEAM}/tasks", json={
        "assignee_id": DEV, "title": "Keep this task", "brief": "Existing work",
    })
    assert created.status_code == 201
    url = f"/api/teams/{TEAM}/members/{DEV}"
    assert client.delete(url).status_code == 409
    removed = client.delete(url + "?reassign_to_director=true")
    assert removed.status_code == 200 and removed.json()["reassigned_tasks"] == 1
    task = client.get(f"/api/teams/{TEAM}/tasks/{created.json()['id']}").json()
    assert task["assignee_id"] == DIRECTOR and task["title"] == "Keep this task"


@pytest.mark.parametrize("dependency", ["task", "chat", "report"])
async def test_unconfirmed_removal_rechecks_dependencies_created_during_preflight(ctx, monkeypatch, dependency):
    original_session = ctx.db.session
    deleting_task = asyncio.current_task()
    inject = True
    created = None

    @asynccontextmanager
    async def racing_session():
        nonlocal inject, created
        async with original_session() as session:
            yield session
        if inject and asyncio.current_task() is deleting_task:
            inject = False
            # Simulate an assignment that finishes after preflight's reads, before the
            # removal gate closes. It must not silently gain transfer authorization.
            if dependency == "task":
                created = await ctx.teams.create_task(
                    team_id=TEAM, assignee_id=DEV, title="Arrived during preflight", brief="Keep",
                )
            elif dependency == "chat":
                created = await ctx.chats.create_chat(DEV, title="Arrived during preflight")
            else:
                async with original_session() as session:
                    created = await session.get(TeamMember, DESIGNER)
                    created.manager_id = DEV
                    session.add(created)

    monkeypatch.setattr(ctx.db, "session", racing_session)
    with pytest.raises(HTTPException) as refusal:
        await delete_member(TEAM, DEV, ctx)
    assert refusal.value.status_code == 409
    assert not ctx.teams.removing_members
    async with original_session() as session:
        assert await session.get(TeamMember, DEV) is not None
        if dependency == "task":
            assert (await session.get(TeamTask, created.id)).assignee_id == DEV
        elif dependency == "chat":
            assert (await session.get(AgentChat, created.id)).member_id == DEV
        else:
            assert (await session.get(TeamMember, DESIGNER)).manager_id == DEV


async def test_director_is_normalized_and_restored_without_losing_custom_fields(ctx):
    path = ctx.teams.store.file_for(TEAM)
    roster = yaml.safe_load(path.read_text("utf-8"))
    director = next(member for member in roster["members"] if member["id"] == "director")
    director.update(kind="agent", name="Project owner", responsibility="Make the final call")
    path.write_text(yaml.safe_dump(roster), encoding="utf-8")
    await ctx.teams.sync_definitions()
    async with ctx.db.session() as session:
        saved = await session.get(TeamMember, DIRECTOR)
        assert saved.kind == "human" and saved.builtin == "director"
        assert saved.name == "Project owner"
    roster["members"] = [member for member in roster["members"] if member["id"] != "director"]
    path.write_text(yaml.safe_dump(roster), encoding="utf-8")
    await ctx.teams.sync_definitions()
    restored = next(member for member in yaml.safe_load(path.read_text("utf-8"))["members"]
                    if member["id"] == "director")
    assert restored["kind"] == "human" and restored["name"] == "Project owner"
    assert restored["responsibility"] == "Make the final call"
    second = ctx.teams.store.directory / "empty.yaml"
    second.write_text("id: empty\nname: Empty\nmembers: []\n", encoding="utf-8")
    await ctx.teams.sync_definitions()
    async with ctx.db.session() as session:
        assert (await session.get(TeamMember, "empty__director")).kind == "human"


async def test_confirmed_removal_transfers_all_tasks_reports_and_chats_with_history(ctx):
    tasks = []
    for status in TaskStatus:
        task = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV, title=status, brief="keep me")
        async with ctx.db.session() as session:
            row = await session.get(TeamTask, task.id)
            row.status = status
            row.archived = status == TaskStatus.DONE
            row.total_cost_usd = 3.25
            row.agent_session_id = "old-session"
            row.worktree_path = "/kept-checkout"
            session.add(row)
        tasks.append(task)
    historical = await ctx.teams.record_message(tasks[0].id, DEV, role="assistant", kind="text", text="Existing work")
    chat = await ctx.chats.create_chat(DEV, title="Existing discussion")
    chat_message = await ctx.chats.record(chat.id, DEV, role="assistant", kind="text", text="Keep this answer")
    async with ctx.db.session() as session:
        report = await session.get(TeamMember, DESIGNER)
        report.manager_id = DEV
        session.add(report)
        director = await session.get(TeamMember, DIRECTOR)
        director.manager_id = DESIGNER  # Removing an ancestor must not introduce a cycle.
        session.add(director)
    with pytest.raises(HTTPException) as refusal:
        await delete_member(TEAM, DEV, ctx)
    assert refusal.value.status_code == 409

    result = await delete_member(TEAM, DEV, ctx, reassign_to_director=True)

    assert result["reassigned_tasks"] == len(tasks)
    async with ctx.db.session() as session:
        assert await session.get(TeamMember, DEV) is None
        assert (await session.get(TeamMember, DIRECTOR)).manager_id is None
        assert (await session.get(TeamMember, DESIGNER)).manager_id == DIRECTOR
        for old in tasks:
            task = await session.get(TeamTask, old.id)
            assert task.assignee_id == DIRECTOR and not task.auto_start
            assert task.agent_session_id is None
            assert task.total_cost_usd == 3.25 and task.worktree_path == "/kept-checkout"
            assert task.status == (TaskStatus.PENDING if old.title == TaskStatus.IN_PROGRESS else old.title)
            assert task.archived == (old.title == TaskStatus.DONE)
        assert (await session.get(AgentMessage, historical.id)).member_id == DEV
        saved_chat = await session.get(AgentChat, chat.id)
        assert saved_chat.member_id == DIRECTOR and saved_chat.agent_session_id is None
    assert any(message.id == chat_message.id and message.member_id == DEV
               for message in await ctx.chats.messages(chat.id))
    with pytest.raises(ValueError, match="read-only"):
        await ctx.chats.send(chat.id, "Must not launch", UserContext())
    roster = yaml.safe_load(ctx.teams.store.file_for(TEAM).read_text("utf-8"))
    assert not any(member["id"] == "dev" for member in roster["members"])
    await ctx.teams.sync_definitions()
    async with ctx.db.session() as session:
        assert await session.get(TeamMember, DEV) is None


async def test_removal_stops_active_task_and_chat_without_dispatching_queued_work(ctx):
    started = asyncio.Event()
    requests = []

    class HeldBackend:
        name = "removal_held"
        named_agents = False

        def is_available(self):
            return True, "stub"

        async def run(self, request):
            requests.append(request)
            if len(requests) == 2:
                started.set()
            yield AgentEvent(kind="text", text="Working", session_id="held-session")
            await asyncio.Event().wait()

    register_backend(HeldBackend.name, HeldBackend)
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.agent_backend = HeldBackend.name
        member.isolate_worktree = False
        session.add(member)
    first = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV, title="Running", brief="work", auto_start=True)
    second = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV, title="Queued", brief="work", auto_start=True)
    await ctx.teams.update_task(first.id, {"status": TaskStatus.PENDING})
    await ctx.teams.update_task(second.id, {"status": TaskStatus.PENDING})
    chat = await ctx.chats.create_chat(DEV)
    await ctx.chats.send(chat.id, "Discuss", UserContext())
    await asyncio.wait_for(started.wait(), timeout=10)

    await delete_member(TEAM, DEV, ctx, reassign_to_director=True)

    assert len(requests) == 2
    assert not any(ctx.jobs.is_running(identifier) for identifier in (first.id, second.id, chat.id))
    assert not ctx.teams.removing_members
    async with ctx.db.session() as session:
        for identifier in (first.id, second.id):
            task = await session.get(TeamTask, identifier)
            assert task.assignee_id == DIRECTOR and task.status == TaskStatus.PENDING
            assert task.agent_session_id is None and not task.auto_start
        messages = list((await session.exec(select(AgentMessage).where(AgentMessage.task_id == first.id))).all())
        assert any(message.kind == "cancelled" for message in messages)
