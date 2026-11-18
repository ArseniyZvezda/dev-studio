"""One API response block in durable history and live WS, for tasks and chats (§10.6)."""
import pytest
from sqlmodel import select

from voxarium_studio.models.teams import AgentMessage, AgentChatMessage


@pytest.mark.parametrize("owner", ["chat", "task"])
async def test_stream_snapshots_upsert_both_persistence_and_websocket(ctx, monkeypatch, owner):
    member = "team_default__dev"
    if owner == "chat":
        chat = await ctx.chats.create_chat(member)
        owner_id = chat.id
        model, field = AgentChatMessage, AgentChatMessage.chat_id

        async def record(kind, text, data):
            return await ctx.chats.record(owner_id, member, role="assistant", kind=kind, text=text, data=data)
    else:
        task = await ctx.teams.create_task(team_id="team_default", assignee_id=member, title="Stream", brief="Test")
        owner_id = task.id
        model, field = AgentMessage, AgentMessage.task_id

        async def record(kind, text, data):
            return await ctx.teams.record_message(owner_id, member, role="assistant", kind=kind, text=text, data=data)

    published = []

    async def publish(event, data, **kwargs):
        published.append(data)

    monkeypatch.setattr(ctx.bus, "publish", publish)
    initial_data = {"stream_id": "response-one", "stream_text": "Hello"}
    first = await record("text", "Hello", initial_data)
    second = await record("text", " world", {"stream_id": "response-one", "stream_text": "Hello world"})
    thought = await record("thinking", "Reasoning", {"stream_id": "response-one", "stream_text": "Reasoning"})
    next_response = await record("text", "Next", {"stream_id": "response-two", "stream_text": "Next"})
    assert first.id == second.id
    assert len({second.id, thought.id, next_response.id}) == 3
    assert published[0]["id"] == published[1]["id"]
    assert published[1]["text"] == "Hello world"
    assert initial_data["stream_text"] == "Hello"  # Event metadata belongs to its caller.
    async with ctx.db.session() as session:
        rows = (await session.exec(select(model).where(field == owner_id, model.role == "assistant"))).all()
    assert len(rows) == 3
    assert next(row for row in rows if row.id == first.id).text == "Hello world"
    assert all("stream_text" not in row.data for row in rows)
