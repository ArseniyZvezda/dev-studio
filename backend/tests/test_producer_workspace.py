"""Producer workspace chat lifecycle (§10.11), without launching real agents."""
from __future__ import annotations

import asyncio

import pytest

from voxarium_studio.teams.chat import JOB_KIND
from voxarium_studio.teams.tools import SERVER_NAME

TEAM = "team_default"
PRODUCER = f"{TEAM}__studio-producer"
DEV = f"{TEAM}__dev"


async def test_workspace_creation_enables_and_persists_idle_producer(ctx):
    chat = await ctx.chats.create_chat(PRODUCER, producer_mode=True, locale="ru")
    stored = await ctx.chats.get_chat(chat.id)
    assert stored.producer_mode and stored.producer_workspace
    assert not stored.producer_started and not stored.producer_paused
    assert stored.producer_locale == "ru" and stored.producer_state == "discussing"
    assert not ctx.chats.is_running(chat.id)
    assert await ctx.chats.messages(chat.id) == []
    assert ctx.chats.to_json(stored)["producer_workspace"] is True


async def test_empty_workspace_stays_unstarted_on_project_reopen(ctx):
    chat = await ctx.chats.create_chat(PRODUCER, producer_mode=True)
    await ctx.chats.producer.restore_paused()
    assert chat.id not in ctx.teams.paused_producer_chats
    stored = await ctx.chats.get_chat(chat.id)
    assert not stored.producer_started and not stored.producer_paused


async def test_workspace_adopts_an_old_off_chat_without_starting_a_turn(ctx):
    old = await ctx.chats.create_chat(PRODUCER, title="Existing conversation")
    converted = await ctx.chats.use_producer_workspace(old.id, locale="ru")
    assert converted.producer_workspace and converted.producer_mode
    assert converted.producer_state == "discussing" and converted.producer_locale == "ru"
    assert converted.title == "Existing conversation"
    assert not ctx.chats.is_running(old.id)


async def test_workspace_adoption_preserves_the_active_plan_and_settings(ctx):
    old = await ctx.chats.create_chat(PRODUCER)
    await ctx.chats.producer._save(old.id, producer_mode=True, producer_started=True, producer_state="awaiting_approval",
                                    producer_plan=[{"id": "task_1"}], producer_plan_summary="Keep this",
                                    producer_plan_version=4, producer_policy="independent",
                                    producer_locale="ru", agent_session_id="existing-session")
    converted = await ctx.chats.use_producer_workspace(old.id, locale="en")
    assert converted.producer_workspace and converted.producer_mode
    assert converted.producer_started
    assert converted.producer_state == "awaiting_approval"
    assert converted.producer_plan == [{"id": "task_1"}]
    assert converted.producer_plan_summary == "Keep this" and converted.producer_plan_version == 4
    assert converted.producer_policy == "independent" and converted.producer_locale == "ru"
    assert converted.agent_session_id == "existing-session"


async def test_workspace_adoption_rejects_a_normal_turn_in_flight(ctx):
    from voxarium_studio.teams.chat import ChatBusy
    old = await ctx.chats.create_chat(PRODUCER)
    started = asyncio.Event()

    async def running(_handle):
        started.set()
        await asyncio.Event().wait()

    ctx.jobs.submit(old.id, JOB_KIND, running, member_id=PRODUCER, chat_id=old.id)
    await asyncio.wait_for(started.wait(), timeout=2)
    with pytest.raises(ChatBusy):
        await ctx.chats.use_producer_workspace(old.id)
    assert not (await ctx.chats.get_chat(old.id)).producer_mode
    await ctx.chats.delete_chat(old.id)


async def test_workspace_creation_rejects_non_producer_before_saving(ctx):
    with pytest.raises(ValueError, match="built-in"):
        await ctx.chats.create_chat(DEV, producer_mode=True)
    assert await ctx.chats.list_chats() == []
    ordinary = await ctx.chats.create_chat(DEV)
    assert not ordinary.producer_workspace and not ordinary.producer_mode


async def test_workspace_creation_rejects_missing_mcp_before_saving(ctx):
    ctx.config.mcp.disabled.append(SERVER_NAME)
    with pytest.raises(ValueError, match="MCP"):
        await ctx.chats.create_chat(PRODUCER, producer_mode=True)
    assert await ctx.chats.list_chats() == []


async def test_workspace_stays_enabled_until_deleted_and_preserves_board_tasks(ctx):
    chat = await ctx.chats.create_chat(PRODUCER, producer_mode=True)
    task = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV, created_by=PRODUCER,
                                      title="Keep this task", brief="Work", producer_chat_id=chat.id)
    with pytest.raises(ValueError, match="until deleted"):
        await ctx.chats.producer.disable(chat.id)
    assert (await ctx.chats.get_chat(chat.id)).producer_mode
    assert await ctx.chats.delete_chat(chat.id)
    assert await ctx.chats.get_chat(chat.id) is None
    assert await ctx.teams.find_task(task.id) is not None


async def test_workspace_deletion_waits_for_running_turn_cleanup(ctx):
    chat = await ctx.chats.create_chat(PRODUCER, producer_mode=True)
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def running(_handle):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    ctx.jobs.submit(chat.id, JOB_KIND, running, member_id=PRODUCER, chat_id=chat.id)
    await asyncio.wait_for(started.wait(), timeout=2)
    assert await ctx.chats.delete_chat(chat.id)
    assert cleaned.is_set()
    assert not ctx.chats.is_running(chat.id)
    assert await ctx.chats.get_chat(chat.id) is None
    assert await ctx.chats.messages(chat.id) == []


def test_workspace_api_enables_mode_and_only_deletion_can_stop_it(client):
    response = client.post("/api/chats", json={"member_id": PRODUCER, "producer_mode": True, "locale": "ru"})
    assert response.status_code == 201
    chat = response.json()
    assert chat["producer_mode"] and chat["producer_workspace"] and not chat["is_running"]
    assert client.post(f"/api/chats/{chat['id']}/producer/stop").status_code == 400
    assert client.get(f"/api/chats/{chat['id']}").json()["producer_mode"]
    assert client.delete(f"/api/chats/{chat['id']}").status_code == 200
    assert client.get(f"/api/chats/{chat['id']}").status_code == 404


def test_workspace_api_adopts_existing_chat_without_a_start_turn(client):
    old = client.post("/api/chats", json={"member_id": PRODUCER}).json()
    response = client.post(f"/api/chats/{old['id']}/producer/workspace", json={"locale": "ru"})
    assert response.status_code == 200
    converted = response.json()
    assert converted["producer_workspace"] and converted["producer_mode"]
    assert converted["producer_locale"] == "ru" and not converted["is_running"]


def test_workspace_api_prestart_policy_changes_never_activate_and_controls_reject(client):
    chat = client.post("/api/chats", json={"member_id": PRODUCER, "producer_mode": True}).json()
    path = f"/api/chats/{chat['id']}"
    for policy in ("independent", "careful", "independent"):
        response = client.patch(f"{path}/producer/policy", json={"policy": policy})
        assert response.status_code == 200
        assert response.json()["producer_policy"] == policy
        assert not response.json()["producer_started"] and not response.json()["is_running"]
    for action in ("pause", "resume"):
        assert client.post(f"{path}/producer/{action}").status_code == 409
    assert client.get(path).json()["messages"] == []
