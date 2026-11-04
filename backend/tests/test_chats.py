"""Chats with a team member outside the board (§10.9), against a stub backend.

Nothing here reaches the real Claude Agent SDK: the stub records the request it was handed
— which is how the tests read what the member would have been told — and answers with a
short transcript.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlmodel import select

from voxarium_studio.core.context import AppContext
from voxarium_studio.core.prompts import default as default_prompt
from voxarium_studio.models.teams import AgentChat, AgentChatMessage, TeamMember
from voxarium_studio.teams import skill as studio_skill
from voxarium_studio.teams.backends.base import AgentEvent, AgentRunRequest, register_backend
from voxarium_studio.teams.chat import (
    MAX_SELECTION_CHARS,
    ChatBusy,
    UserContext,
    describe_context,
    title_from,
)
from voxarium_studio.ws.events import EventType

TEAM = "team_default"
DIRECTOR = f"{TEAM}__director"  # human
DEV = f"{TEAM}__dev"  # agent


class RecordingBackend:
    """Answers at once, keeping every request so a test can read what was sent."""

    name = "chat_stub"
    requests: list[AgentRunRequest] = []

    def is_available(self):
        return True, "stub"

    async def run(self, request):
        RecordingBackend.requests.append(request)
        yield AgentEvent(kind="text", text=f"You said: {request.prompt[-20:]}", session_id="chat_sess")
        yield AgentEvent(
            kind="result",
            text="Done.",
            session_id="chat_sess",
            data={"is_error": False, "num_turns": 1, "total_cost_usd": 0.01, "total_tokens": 300},
            is_final=True,
        )


class GatedBackend:
    """Holds every turn open until the test lets it go, to catch the chat mid-answer."""

    name = "chat_gate"
    gate = asyncio.Event()

    def is_available(self):
        return True, "stub"

    async def run(self, request):
        yield AgentEvent(kind="text", text="thinking…", session_id="gated_sess")
        await GatedBackend.gate.wait()
        yield AgentEvent(kind="result", text="Done.", session_id="gated_sess", is_final=True)


register_backend("chat_stub", RecordingBackend)
register_backend("chat_gate", GatedBackend)


@pytest.fixture
async def chats(ctx: AppContext) -> AppContext:
    RecordingBackend.requests = []
    GatedBackend.gate = asyncio.Event()
    async with ctx.db.session() as session:
        for member in (await session.exec(select(TeamMember))).all():
            member.agent_backend = "chat_stub"
            session.add(member)
    return ctx


CONTEXT = UserContext(
    page="Documents",
    route="/documents?path=features/dash.md",
    title="Dash ability",
    document="features/dash.md",
    selection="The dash lasts 0.3 s.",
)


async def turn_done(ctx: AppContext, chat_id: str) -> None:
    handle = ctx.jobs.get(chat_id)
    if handle is not None and handle.task is not None:
        await asyncio.wait([handle.task], timeout=30)


# ------------------------------------------------------------------ describing the context


def test_the_context_names_the_page_the_document_and_the_selection(tmp_path) -> None:
    text = describe_context(CONTEXT, docs_dir=tmp_path / "docs", project_root=tmp_path)
    assert text.startswith("- Page: Documents — Dash ability (/documents?path=features/dash.md)")
    assert "- Open document: features/dash.md (read_doc path; on disk at " in text
    assert str(tmp_path / "docs" / "features/dash.md") in text
    assert "- Selected text:\n```\nThe dash lasts 0.3 s.\n```" in text


def test_an_empty_context_still_says_where_the_user_is(tmp_path) -> None:
    text = describe_context(UserContext(), docs_dir=tmp_path, project_root=tmp_path)
    assert text == "- Page: the Studio"


def test_a_long_selection_is_cut_and_the_cut_is_named(tmp_path) -> None:
    long = UserContext(selection="x" * (MAX_SELECTION_CHARS + 500))
    text = describe_context(long, docs_dir=tmp_path, project_root=tmp_path)
    assert f"(first {MAX_SELECTION_CHARS} of {MAX_SELECTION_CHARS + 500} characters)" in text
    body = text.split("```")[1].strip()
    assert body == "x" * MAX_SELECTION_CHARS


def test_a_chat_is_titled_by_the_first_line_of_the_first_message() -> None:
    assert title_from("\n\nWhy is the dash slow?\nSecond line") == "Why is the dash slow?"
    long = title_from("a" * 100)
    assert len(long) == 60 and long.endswith("…")


# ------------------------------------------------------------------ lifecycle


async def test_a_chat_is_opened_with_an_agent_and_never_with_a_person(chats: AppContext) -> None:
    chat = await chats.chats.create_chat(DEV)
    assert chat.member_id == DEV and chat.title == "" and chat.agent_session_id is None
    with pytest.raises(ValueError):
        await chats.chats.create_chat(DIRECTOR)
    with pytest.raises(KeyError):
        await chats.chats.create_chat("nobody")


async def test_a_message_reaches_the_member_with_the_context_in_front(chats: AppContext) -> None:
    chat = await chats.chats.create_chat(DEV)
    sent = await chats.chats.send(chat.id, "  Why is the dash slow?  ", CONTEXT)
    assert sent.role == "user" and sent.text == "Why is the dash slow?"
    assert sent.data["context"]["document"] == "features/dash.md"
    await turn_done(chats, chat.id)

    request = RecordingBackend.requests[-1]
    assert request.task is None and request.session_key == chat.id
    assert request.resume_session_id is None
    assert request.prompt.endswith("\n\nWhy is the dash slow?")
    assert request.prompt.startswith("Where the user is in the Studio right now:\n- Page: Documents")
    assert "- Selected text:\n```\nThe dash lasts 0.3 s.\n```" in request.prompt
    # The launch root, never an isolated checkout.
    assert request.workdir == chats.paths.root
    assert request.writable_roots == [chats.paths.root]

    stored = await chats.chats.get_chat(chat.id)
    assert stored is not None
    assert stored.title == "Why is the dash slow?"
    assert stored.agent_session_id == "chat_sess"
    assert stored.num_turns == 1 and stored.total_tokens == 300
    assert stored.total_cost_usd == pytest.approx(0.01)
    assert stored.error is None
    assert not chats.chats.is_running(chat.id)

    kinds = [(m.role, m.kind) for m in await chats.chats.messages(chat.id)]
    assert kinds == [("user", "text"), ("assistant", "text"), ("system", "result")]


async def test_a_chat_above_the_project_keeps_the_project_as_a_write_root(chats: AppContext) -> None:
    chats.config.teams.launch_root = ".."
    chat = await chats.chats.create_chat(DEV)
    await chats.chats.send(chat.id, "Check the project", UserContext())
    await turn_done(chats, chat.id)

    request = RecordingBackend.requests[-1]
    assert request.workdir == chats.paths.root.parent
    assert request.writable_roots == [chats.paths.root]


async def test_the_system_prompt_is_the_chat_one(chats: AppContext) -> None:
    chat = await chats.chats.create_chat(DEV)
    await chats.chats.send(chat.id, "hello", UserContext(page="Tasks"))
    await turn_done(chats, chat.id)
    request = RecordingBackend.requests[-1]
    prompt = request.system_prompt
    assert default_prompt("chat_mode") in prompt
    assert "You are working on task" not in prompt
    assert default_prompt("finish") not in prompt
    # The docs location and the Studio skill travel exactly as they do on a task run.
    assert str(chats.docs.directory) in prompt
    assert request.plugin_dirs == [studio_skill.BUNDLE]
    assert set(studio_skill.allowed_tools()) <= set(request.extra_allowed_tools)


async def test_a_chat_is_told_the_members_skills_as_a_task_run_is(chats: AppContext) -> None:
    """The same folders, the same fragment and the same Read rules a task run gets (§10.10),
    read fresh each turn — a skill given between two messages reaches the second."""
    chat = await chats.chats.create_chat(DEV)
    await chats.chats.send(chat.id, "first", UserContext(page="Tasks"))
    await turn_done(chats, chat.id)
    first_prompt = RecordingBackend.requests[-1].system_prompt
    assert str(chats.paths.skills) not in first_prompt, "no project skill was assigned"
    assert str(studio_skill.BUNDLE / "skills" / "studio" / "SKILL.md") in first_prompt
    assert "- graphify:" in first_prompt, "enabled plugin skills reach chats without assignment"

    folder = chats.paths.skills / "tester"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\ndescription: Writes test plans.\n---\n", encoding="utf-8")
    async with chats.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        dev.skills = ["tester"]
        session.add(dev)

    await chats.chats.send(chat.id, "second", UserContext(page="Tasks"))
    await turn_done(chats, chat.id)
    request = RecordingBackend.requests[-1]
    assert f"- tester: Writes test plans. (instructions: {folder / 'SKILL.md'})" in (
        request.system_prompt
    )
    assert request.system_prompt.index("SKILL.md") < request.system_prompt.index(
        str(chats.docs.directory)
    ), "with the other things the member has, before where the documents are"
    rule = studio_skill.rule_path(folder)
    if rule is not None:
        assert f"Read({rule}/**)" in request.extra_allowed_tools


async def test_the_second_message_resumes_the_same_session(chats: AppContext) -> None:
    chat = await chats.chats.create_chat(DEV)
    await chats.chats.send(chat.id, "first", UserContext(page="Tasks"))
    await turn_done(chats, chat.id)
    await chats.chats.send(chat.id, "second", UserContext(page="Explorer", file="Assets/a.png"))
    await turn_done(chats, chat.id)
    first, second = RecordingBackend.requests[-2:]
    assert first.resume_session_id is None
    assert second.resume_session_id == "chat_sess"
    assert "- Open file: Assets/a.png" in second.prompt
    stored = await chats.chats.get_chat(chat.id)
    assert stored is not None and stored.num_turns == 2
    assert stored.title == "first", "the title is the first message's and stays"


async def test_a_second_message_while_the_member_answers_is_refused(chats: AppContext) -> None:
    async with chats.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.agent_backend = "chat_gate"
        session.add(member)
    chat = await chats.chats.create_chat(DEV)
    await chats.chats.send(chat.id, "go", UserContext())
    await asyncio.sleep(0.05)
    assert chats.chats.is_running(chat.id)
    with pytest.raises(ChatBusy):
        await chats.chats.send(chat.id, "again", UserContext())
    GatedBackend.gate.set()
    await turn_done(chats, chat.id)
    assert not chats.chats.is_running(chat.id)


async def test_stopping_a_turn_leaves_a_line_in_the_transcript(chats: AppContext) -> None:
    async with chats.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.agent_backend = "chat_gate"
        session.add(member)
    chat = await chats.chats.create_chat(DEV)
    await chats.chats.send(chat.id, "go", UserContext())
    await asyncio.sleep(0.05)
    assert await chats.chats.cancel(chat.id)
    await turn_done(chats, chat.id)
    kinds = [m.kind for m in await chats.chats.messages(chat.id)]
    assert kinds[-1] == "cancelled"
    assert not chats.chats.is_running(chat.id)


async def test_deleting_a_chat_takes_its_transcript(chats: AppContext) -> None:
    chat = await chats.chats.create_chat(DEV)
    await chats.chats.send(chat.id, "hello", UserContext())
    await turn_done(chats, chat.id)
    assert await chats.chats.delete_chat(chat.id)
    assert not await chats.chats.delete_chat(chat.id)
    async with chats.db.session() as session:
        assert await session.get(AgentChat, chat.id) is None
        left = (
            await session.exec(select(AgentChatMessage).where(AgentChatMessage.chat_id == chat.id))
        ).all()
    assert left == []


async def test_the_list_is_newest_activity_first(chats: AppContext) -> None:
    older = await chats.chats.create_chat(DEV, title="older")
    newer = await chats.chats.create_chat(DEV, title="newer")
    await chats.chats.send(older.id, "wake up", UserContext())
    await turn_done(chats, older.id)
    listed = [chat.id for chat in await chats.chats.list_chats()]
    assert listed == [older.id, newer.id]


async def test_a_turn_publishes_its_messages_and_its_state(chats: AppContext) -> None:
    subscription = chats.bus.subscribe()
    try:
        chat = await chats.chats.create_chat(DEV)
        await chats.chats.send(chat.id, "hello", UserContext(page="Tasks"))
        await turn_done(chats, chat.id)
        await asyncio.sleep(0)
        seen: list[Any] = []
        while True:
            try:
                seen.append(subscription._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        subscription.close()
    types = [event.type for event in seen]
    assert types[0] == EventType.TEAM_CHAT_UPDATED, "creation announces the chat"
    messages = [event for event in seen if event.type == EventType.TEAM_CHAT_MESSAGE]
    assert [m.data["kind"] for m in messages] == ["text", "text", "result"]
    assert messages[0].data["chat_id"] == chat.id and messages[0].data["role"] == "user"
    updates = [event for event in seen if event.type == EventType.TEAM_CHAT_UPDATED]
    assert updates[1].data["is_running"] is True, "sending says the member is answering"
    assert updates[-1].data["is_running"] is False, "the end of the turn says it is not"
    assert updates[-1].data["num_turns"] == 1


async def test_a_failing_turn_lands_on_the_chat_not_the_server(chats: AppContext) -> None:
    class Failing:
        name = "chat_fail"

        def is_available(self):
            return True, "stub"

        async def run(self, request):
            yield AgentEvent(kind="error", text="boom", is_final=True)

    register_backend("chat_fail", Failing)
    async with chats.db.session() as session:
        member = await session.get(TeamMember, DEV)
        member.agent_backend = "chat_fail"
        session.add(member)
    chat = await chats.chats.create_chat(DEV)
    await chats.chats.send(chat.id, "hello", UserContext())
    await turn_done(chats, chat.id)
    stored = await chats.chats.get_chat(chat.id)
    assert stored is not None and stored.error == "boom"
    # The next message clears it before the member is asked again.
    await chats.chats.send(chat.id, "retry", UserContext())
    async with chats.db.session() as session:
        assert (await session.get(AgentChat, chat.id)).error is None
    await turn_done(chats, chat.id)


# ------------------------------------------------------------------ the tools without a task


async def test_the_toolset_works_without_a_task(chats: AppContext) -> None:
    """A chat builds the same tools as a run; a task created from one is a delegation root."""
    from voxarium_studio.teams.tools import build_toolset

    async with chats.db.session() as session:
        member = await session.get(TeamMember, DIRECTOR)
        # The director may assign to anyone below; make them an agent for the tool's sake.
        member.kind = "agent"
        session.add(member)
    toolset = build_toolset(chats.teams, member=member, task=None)
    assert toolset is not None
    by_name = {definition.name: definition for definition in toolset.definitions}
    answer = await by_name["create_task"].handler(
        {"assignee_id": DEV, "title": "From a chat", "brief": "b"}
    )
    assert "Created" in answer["content"][0]["text"]
    created = await chats.teams.find_task("1", team_id=TEAM)
    assert created is not None and created.delegation_depth == 0
    assert created.created_by == DIRECTOR


# ------------------------------------------------------------------ over HTTP


def test_the_chat_endpoints(client) -> None:
    ctx: AppContext = client.app.state.ctx
    # Point the member at the stub so a send has somewhere to go.
    patched = client.patch(
        f"/api/teams/{TEAM}/members/{DEV}", json={"agent_backend": "chat_stub"}
    )
    assert patched.status_code == 200, patched.text

    assert client.post("/api/chats", json={"member_id": DIRECTOR}).status_code == 400
    assert client.post("/api/chats", json={"member_id": "nobody"}).status_code == 404

    created = client.post("/api/chats", json={"member_id": DEV})
    assert created.status_code == 201, created.text
    chat = created.json()
    assert chat["is_running"] is False and chat["title"] == ""

    listed = client.get("/api/chats").json()
    assert [entry["id"] for entry in listed] == [chat["id"]]

    sent = client.post(
        f"/api/chats/{chat['id']}/messages",
        json={"text": "hi", "context": {"page": "Tasks", "route": "/tasks"}},
    )
    assert sent.status_code == 202, sent.text
    assert sent.json()["message"]["data"]["context"]["page"] == "Tasks"
    assert sent.json()["chat"]["title"] == "hi"

    for _ in range(100):
        detail = client.get(f"/api/chats/{chat['id']}").json()
        if not detail["is_running"] and len(detail["messages"]) == 3:
            break
        import time

        time.sleep(0.05)
    assert [m["kind"] for m in detail["messages"]] == ["text", "text", "result"]
    assert detail["agent_session_id"] == "chat_sess"

    empty = client.post(f"/api/chats/{chat['id']}/messages", json={"text": "   "})
    assert empty.status_code == 400
    assert client.get("/api/chats/nope").status_code == 404
    assert client.post("/api/chats/nope/cancel").status_code == 404
    assert client.post(f"/api/chats/{chat['id']}/cancel").json()["cancelled"] is False

    assert client.delete(f"/api/chats/{chat['id']}").json()["deleted"] is True
    assert client.delete(f"/api/chats/{chat['id']}").status_code == 404
    assert ctx.jobs.get(chat["id"]) is None


async def test_chat_counter_is_replaced_not_appended(chats: AppContext):
    chat = await chats.chats.create_chat(DEV)
    for count in range(3):
        await chats.chats.record(chat.id, DEV, role="system", kind="system",
            text="thinking_tokens", data={"estimated_tokens": count})
    rows = await chats.chats.messages(chat.id)
    assert len(rows) == 1
    assert rows[0].data == {"estimated_tokens": 2}
