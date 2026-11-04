"""Producer session Plan: durable, short, and refreshed as work changes (§10.11)."""
from __future__ import annotations

import json

import pytest

from test_producer import DEV, PRODUCER, draft, finished, opened, producer, tools_for
from voxarium_studio.core.db import Database
from voxarium_studio.models.teams import AgentChat
from voxarium_studio.teams.chat import UserContext
from voxarium_studio.teams.producer import ProducerService


@pytest.mark.parametrize("items", [
    None, [], [{"text": "Step", "done": False}] * 21,
    [{"text": "x" * 51, "done": False}],
    [{"text": "", "done": False}], [{"text": "   ", "done": False}],
    [{"text": "Step\nMore prose", "done": False}],
    [{"text": "Step\tMore prose", "done": False}],
    [{"text": "Step\u2028More prose", "done": False}],
    [{"text": "Step\u2029More prose", "done": False}],
    [{"text": "- [ ] Embedded list", "done": False}],
    [{"text": "# Heading", "done": False}],
    [{"text": "[link](https://example.com)", "done": False}],
    [{"text": "<b>Markup</b>", "done": False}],
    [{"text": "Step", "done": "false"}], [{"text": "Step", "done": 1}],
    [{"text": "Step", "done": False, "summary": "Extra prose"}],
])
def test_checklist_rejects_content_outside_its_contract(items):
    with pytest.raises(ValueError):
        ProducerService.checklist_items(items)


def test_checklist_counts_unicode_characters_and_keeps_completion():
    items = [{"text": "🛠" * 50, "done": True}, {"text": "  Проверить результат  ", "done": False}]
    assert ProducerService.checklist_items(items) == [
        items[0], {"text": "Проверить результат", "done": False}]


async def workspace_chat(ctx):
    chat = await opened(ctx)
    async with ctx.db.session() as session:
        stored = await session.get(AgentChat, chat.id)
        stored.producer_workspace = True
        session.add(stored)
    return await ctx.chats.get_chat(chat.id)


async def test_plan_is_saved_in_chat_json_context_and_survives_approval(producer):
    chat = await workspace_chat(producer)
    task = await draft(producer, chat)
    items = [{"text": "Implement movement", "done": False}]
    service = producer.chats.producer
    await service.update_checklist(chat.id, items)
    await service.propose(chat.id, [task.id], "Implement movement")
    pending = await producer.chats.get_chat(chat.id)
    await service._approve_plan(chat.id, pending.producer_plan_version, automatic=False)
    saved = await producer.chats.get_chat(chat.id)
    assert not saved.producer_plan
    assert saved.producer_checklist == items
    assert producer.chats.to_json(saved)["producer_checklist"] == items
    context = json.loads(await service.context(chat.id))
    assert context["session_plan"] == items
    assert "50 characters" in context["session_plan_instructions"]
    assert "Never call stop_producer_mode" in context["mode_instructions"]


async def test_task_tools_require_plan_then_require_refresh_before_yield(producer):
    chat = await workspace_chat(producer)
    tools = await tools_for(producer, chat)
    args = {"assignee_id": DEV, "title": "Movement", "brief": "Move with arrow keys"}
    denied = await tools["create_task"](args)
    assert denied["is_error"] and "update_producer_checklist" in denied["content"][0]["text"]
    assert not await producer.chats.producer.tasks(chat.id)
    saved = await tools["update_producer_checklist"]({
        "items": [{"text": "Implement movement", "done": False}]})
    assert not saved.get("is_error")
    created = await tools["create_task"](args)
    assert not created.get("is_error")
    assert "Update the session Plan" in created["content"][-1]["text"]
    task = (await producer.chats.producer.tasks(chat.id))[0]
    for tool, args in [
        ("propose_producer_plan", {"tasks": [task.id], "summary": "Movement"}),
        ("wait_producer_tasks", {}),
        ("ask_producer_user", {"question": "Approve movement?"}),
    ]:
        denied = await tools[tool](args)
        assert denied["is_error"] and "Tasks changed" in denied["content"][0]["text"]
    await tools["update_producer_checklist"]({
        "items": [{"text": "Implement movement", "done": False}]})
    approved = await tools["propose_producer_plan"]({"tasks": [task.id], "summary": "Movement"})
    assert not approved.get("is_error")


async def test_external_task_changes_require_plan_refresh_but_cost_changes_do_not(producer):
    chat = await workspace_chat(producer)
    task = await draft(producer, chat)
    service = producer.chats.producer
    await service.update_checklist(chat.id, [{"text": "Implement movement", "done": False}])
    from voxarium_studio.models.teams import TeamTask
    async with producer.db.session() as session:
        stored = await session.get(TeamTask, task.id)
        stored.total_cost_usd = 0.15
        session.add(stored)
    await service.require_checklist(chat.id, current=True)
    await producer.teams.update_task(task.id, {"title": "Movement and jumping"}, actor_id=PRODUCER)
    with pytest.raises(ValueError, match="Tasks changed"):
        await service.require_checklist(chat.id, current=True)
    await service.update_checklist(chat.id, [{"text": "Implement movement and jumping", "done": False}])
    await service.require_checklist(chat.id, current=True)


async def test_invalid_update_keeps_previous_plan_and_goal_can_be_clarified(producer):
    chat = await workspace_chat(producer)
    service = producer.chats.producer
    await draft(producer, chat, "Existing board work")
    assert "Question shown" in await service.ask(chat.id, [], "What should we build?", purpose="goal")
    tools = await tools_for(producer, chat)
    items = [{"text": "Clarify the goal", "done": True}]
    await tools["update_producer_checklist"]({"items": items})
    denied = await tools["update_producer_checklist"]({"items": [{"text": "x" * 51, "done": False}]})
    assert denied["is_error"]
    assert (await producer.chats.get_chat(chat.id)).producer_checklist == items


async def test_workspace_mode_can_only_stop_during_chat_deletion(producer):
    chat = await workspace_chat(producer)
    with pytest.raises(ValueError, match="until deleted"):
        await producer.chats.producer.disable(chat.id)
    assert (await producer.chats.get_chat(chat.id)).producer_mode
    await producer.chats.producer.disable(chat.id, deleting=True)
    assert not (await producer.chats.get_chat(chat.id)).producer_mode


async def test_checklist_survives_compaction_and_database_reopen(producer, monkeypatch):
    chat = await workspace_chat(producer)
    items = [{"text": "Implement movement", "done": True}, {"text": "Verify movement", "done": False}]
    await producer.chats.producer.update_checklist(chat.id, items)
    monkeypatch.setattr("voxarium_studio.teams.producer.COMPACT_CHARS", 1)
    await producer.chats.send(chat.id, "Continue with verification", UserContext())
    await finished(producer, chat.id)
    reopened = Database(producer.db.path)
    try:
        await reopened.create_all()
        async with reopened.session() as session:
            saved = await session.get(AgentChat, chat.id)
            assert saved.producer_compacted_through
            assert saved.producer_checklist == items
            assert saved.producer_workspace and saved.producer_checklist_revision
    finally:
        await reopened.close()


async def test_existing_database_gains_empty_plan_without_losing_chat(producer):
    from sqlalchemy import text
    chat = await opened(producer)
    async with producer.db._engine.begin() as conn:
        for column in ("producer_workspace", "producer_checklist", "producer_checklist_revision"):
            await conn.execute(text(f'ALTER TABLE agent_chat DROP COLUMN {column}'))
    reopened = Database(producer.db.path)
    try:
        await reopened.create_all()
        async with reopened.session() as session:
            saved = await session.get(AgentChat, chat.id)
            assert saved.producer_mode and saved.producer_state == chat.producer_state
            assert saved.producer_checklist == []
            assert saved.producer_checklist_revision == ""
            assert saved.producer_workspace is False
    finally:
        await reopened.close()
