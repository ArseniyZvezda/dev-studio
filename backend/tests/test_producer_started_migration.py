"""Preserve the distinction between unused Producer chats and existing work (§10.11)."""
from __future__ import annotations

from sqlalchemy import text

from voxarium_studio.core.db import Database
from voxarium_studio.models.teams import AgentChat, AgentChatMessage

PRODUCER = "team_default__studio-producer"


async def test_old_producer_chats_infer_start_from_persisted_work_but_not_settings(ctx):
    await ctx.chats.producer.shutdown()
    cases = {
        "empty": ({}, False),
        "configured": ({"title": "My next project", "producer_policy": "independent",
                        "estimate_task_cost_usd": 25}, False),
        "user-message": ({}, True),
        "session": ({"agent_session_id": "previous-runtime"}, True),
        "compacted": ({"producer_summary": "Keep the agreed goal."}, True),
        "failed-before-session": ({"producer_state": "error", "error": "Offline"}, True),
        "cancelled-before-session": ({"error": "Turn interrupted"}, True),
        "awaiting-user": ({"producer_state": "awaiting_user"}, True),
        "plan": ({"producer_plan": [{"id": "old-task"}]}, True),
        "approved-plan": ({"producer_plan_version": 2}, True),
        "checklist": ({"producer_checklist": [{"text": "Build it", "done": False}]}, True),
        "watch": ({"producer_watch": [{"id": "old-task"}]}, True),
        "question": ({"producer_questions": [{"question": "Which platform?"}]}, True),
        "release": ({"producer_release_pending": ["old-task"]}, True),
        "completed-turn": ({"num_turns": 1}, True),
        "disabled-after-compaction": ({"producer_mode": False, "producer_workspace": False,
                                       "producer_state": "off", "producer_summary": "Past work"}, True),
        "ordinary-history": ({"producer_mode": False, "producer_workspace": False,
                              "producer_state": "off"}, False),
    }
    async with ctx.db.session() as session:
        for chat_id, (overrides, _) in cases.items():
            values = {"producer_mode": True, "producer_workspace": True,
                      "producer_state": "discussing", "producer_paused": True, **overrides}
            session.add(AgentChat(id=chat_id, member_id=PRODUCER, **values))
        await session.flush()
        for chat_id in ("user-message", "ordinary-history"):
            session.add(AgentChatMessage(chat_id=chat_id, member_id=PRODUCER,
                                         role="user", text="Build a small game."))
    async with ctx.db._engine.begin() as conn:
        await conn.execute(text("ALTER TABLE agent_chat DROP COLUMN producer_started"))

    reopened = Database(ctx.db.path)
    try:
        await reopened.create_all()
        async with reopened.session() as session:
            for chat_id, (_, started) in cases.items():
                chat = await session.get(AgentChat, chat_id)
                assert chat.producer_started is started, chat_id
                assert chat.producer_paused is started, chat_id
            configured = await session.get(AgentChat, "configured")
            assert configured.producer_policy == "independent"
            assert configured.estimate_task_cost_usd == 25
    finally:
        await reopened.close()


async def test_started_migration_does_not_reinfer_current_rows_on_every_open(ctx):
    await ctx.chats.producer.shutdown()
    async with ctx.db.session() as session:
        session.add(AgentChat(id="durable-start", member_id=PRODUCER,
                              producer_mode=True, producer_started=True,
                              producer_state="discussing", producer_paused=True))
        session.add(AgentChat(id="current-marker", member_id=PRODUCER,
                              producer_mode=True, producer_started=False,
                              producer_state="discussing", agent_session_id="ordinary-session"))

    reopened = Database(ctx.db.path)
    try:
        await reopened.create_all()
        async with reopened.session() as session:
            started = await session.get(AgentChat, "durable-start")
            assert started.producer_started and started.producer_paused
            assert not (await session.get(AgentChat, "current-marker")).producer_started
    finally:
        await reopened.close()
