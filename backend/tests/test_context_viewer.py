"""Context Viewer must retain dispatched sources and never invent historical input."""
from datetime import timedelta

from voxarium_studio.models.base import utcnow
from voxarium_studio.models.task_context import TaskContext
from voxarium_studio.models.teams import AgentMessage, TeamMember
from voxarium_studio.teams.backends.base import AgentEvent, AgentRunRequest
from voxarium_studio.teams.context_viewer import capture_request, inventory, prompt_sources, source

TEAM = "team_default"
DEV = f"{TEAM}__dev"


def message(kind="text", text="hello", **kwargs):
    return AgentMessage(task_id="task", member_id=DEV, role="assistant", kind=kind, text=text, **kwargs)


def test_split_materials_preserves_full_text_once():
    text = ("Activation instructions\n--- BEGIN REQUIRED SKILL: design ---\nSKILL BODY\n"
            "--- END REQUIRED SKILL: design ---\nDocument rules\n"
            "--- BEGIN REQUIRED DOCUMENT: a.md ---\nДокумент\n"
            "--- END REQUIRED DOCUMENT: a.md ---\nTask brief")
    parts = prompt_sources(text)
    assert "".join(part["text"] for part in parts) == text
    assert [p["label"] for p in parts if p["category"] == "skills"] == ["design"]
    assert [p["label"] for p in parts if p["category"] == "documents"] == ["a.md"]


def test_legacy_inventory_unknown_remainder_and_no_cumulative_spending():
    messages = [message(), message("result", "duplicate result", data={"total_tokens": 99999}),
                message("system", "usage_update", data={"context_tokens": 1000, "context_window": 2000})]
    result = inventory(None, messages)
    assert result["partial"]
    assert result["reported_tokens"] == 1000
    assert result["estimated_tokens"] == 2
    assert sum(s["tokens"] for s in result["sources"]) == 1000
    assert result["sources"][-1]["category"] == "unattributed"
    assert not any("duplicate" in s["text"] for s in result["sources"])


def test_compaction_invalidates_old_snapshot_and_usage():
    now = utcnow()
    snapshot = TaskContext(task_id="task", started_at=now, captured_at=now, sources=[source("system", "old")])
    result = inventory(snapshot, [message("system", "usage_update", created_at=now, data={"context_tokens": 1000}),
                                  message("compressed summary", "new summary", created_at=now + timedelta(seconds=1))])
    assert result["partial"]
    assert result["reported_tokens"] is None
    assert [s["text"] for s in result["sources"]] == ["new summary"]
    assert result["sources"][0]["source_kind"] == "compaction"


def test_worker_messages_and_pre_session_history_are_excluded():
    now = utcnow()
    snapshot = TaskContext(task_id="task", started_at=now, captured_at=now, sources=[source("task", "brief")])
    result = inventory(snapshot, [message(text="old", created_at=now-timedelta(seconds=1)),
                                  message(text="child", data={"parent_tool_use_id": "worker"}), message(text="parent")])
    assert [s["text"] for s in result["sources"]] == ["brief", "parent"]


async def test_capture_resume_reset_and_delete(ctx):
    task = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV, title="Context", brief="", auto_start=False)
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
    request = AgentRunRequest(member=member, task=task, workdir=ctx.paths.root,
                              prompt="original brief", system_prompt="original system")
    await capture_request(ctx.db, task.id, request)
    request.resume_session_id = "session"
    request.system_prompt = "edited system"
    request.prompt = "continuation"
    await capture_request(ctx.db, task.id, request)
    async with ctx.db.session() as session:
        snapshot = await session.get(TaskContext, task.id)
        assert [s["text"] for s in snapshot.sources] == ["edited system", "original brief", "continuation"]
        assert not snapshot.partial
    await ctx.teams.reset_task(task.id)
    async with ctx.db.session() as session:
        assert await session.get(TaskContext, task.id) is None
    request.resume_session_id = None
    await capture_request(ctx.db, task.id, request)
    request.prompt = "fresh session"
    await capture_request(ctx.db, task.id, request)
    async with ctx.db.session() as session:
        snapshot = await session.get(TaskContext, task.id)
        assert [s["text"] for s in snapshot.sources] == ["edited system", "fresh session"]
    await ctx.teams.delete_task(task.id)
    async with ctx.db.session() as session:
        assert await session.get(TaskContext, task.id) is None


def test_context_endpoint_checks_team_and_handles_empty_task(client):
    task = client.post(f"/api/teams/{TEAM}/tasks", json={"assignee_id": DEV, "title": "Viewer", "auto_start": False}).json()
    url = f"/api/teams/{TEAM}/tasks/{task['id']}/context"
    result = client.get(url)
    assert result.status_code == 200
    assert result.json()["sources"] == []
    assert result.json()["reported_tokens"] is None
    assert client.get(url.replace(TEAM, "wrong-team")).status_code == 404


def test_fragment_loads_past_preview_budget_and_rejects_changed_text(client):
    task = client.post(f"/api/teams/{TEAM}/tasks", json={
        "assignee_id": DEV, "title": "Fragments", "auto_start": False,
    }).json()
    url = f"/api/teams/{TEAM}/tasks/{task['id']}/context"
    body = "полный текст\n" * 3000

    async def store_fragment(text):
        async with client.app.state.ctx.db.session() as session:
            row = await session.get(TaskContext, task["id"]) or TaskContext(task_id=task["id"])
            row.sources = [source("tools", "x" * 32_000)] * 16 + [source("documents", text)]
            session.add(row)

    client.portal.call(store_fragment, body)
    preview = client.get(url).json()["sources"][-1]
    assert preview["text"] == "" and preview["truncated"]
    read_url = f"{url}/fragments/{preview['id']}?key={preview['text_key']}"
    assert client.get(read_url).json() == {"text": body}
    assert client.get(read_url.replace(TEAM, "wrong-team")).status_code == 404
    client.portal.call(store_fragment, "replacement at the same index")
    assert client.get(read_url).status_code == 409


def test_partial_snapshot_keeps_available_legacy_history():
    now = utcnow()
    snapshot = TaskContext(task_id="task", started_at=now, captured_at=now, partial=True,
                           sources=[source("task", "new request")])
    result = inventory(snapshot, [message(text="legacy response", created_at=now-timedelta(seconds=1))])
    assert result["partial"]
    assert [s["text"] for s in result["sources"]] == ["new request", "legacy response"]
    captured, transcript = result["sources"]
    assert captured["source_kind"] == "snapshot"
    assert (transcript["source_kind"], transcript["source_detail"]) == ("assistant_message", DEV)


def test_studio_identifies_required_materials_in_old_snapshots_and_tools_in_the_transcript():
    snapshot = TaskContext(task_id="task", sources=[
        source("skills", "body", "map-design"), source("documents", "body", "docs/terrain.md"),
        source("skills", "legacy field without a name"),
    ])
    view = inventory(snapshot, [message("tool_use", "Read", data={"input": {"file_path": "terrain.md"}}),
                                message("tool_result", "contents", data={"tool_use_id": "call-45"})])
    assert [(s["source_kind"], s["source_detail"]) for s in view["sources"]] == [
        ("required_skill", "map-design"), ("required_document", "docs/terrain.md"),
        ("snapshot", "skills"), ("tool_call", "Read"), ("tool_result", "call-45"),
    ]


async def test_backend_recovery_replaces_snapshot_with_recovery_input(ctx):
    task = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV, title="Recover", brief="", auto_start=False)
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, DEV)
    request = AgentRunRequest(member=member, task=task, workdir=ctx.paths.root,
                              prompt="original input", system_prompt="system")
    await capture_request(ctx.db, task.id, request)
    request.resume_session_id = "missing-session"
    request.prompt = "continue"
    request.recovery_prompt = "restored history"

    class Backend:
        async def run(self, request):
            yield AgentEvent(kind="system", text="Recovered", data={"subtype": "session_recovered"})

    await ctx.teams._drain_events(Backend(), request, task.id, DEV)
    async with ctx.db.session() as session:
        snapshot = await session.get(TaskContext, task.id)
        assert [s["text"] for s in snapshot.sources] == ["system", "restored history"]
        assert not snapshot.partial


def test_chat_viewer_reads_only_this_conversation_and_its_captured_input(client):
    ctx = client.app.state.ctx
    first, second = [client.post("/api/chats", json={"member_id": DEV}).json()["id"] for _ in range(2)]
    url = f"/api/chats/{first}/context"
    other_url = f"/api/chats/{second}/context"
    long_prompt = "selected passage\n" * 2300

    async def run_turns():
        async with ctx.db.session() as session:
            member = await session.get(TeamMember, DEV)

        class Backend:
            async def run(self, request):
                yield AgentEvent(kind="system", text="usage_update", data={
                    "subtype": "usage_update", "context_tokens": 830, "context_window": 2000,
                    "total_tokens": 1200,
                })
                yield AgentEvent(kind="text", text="My reply")

        request = AgentRunRequest(member=member, task=None, workdir=ctx.paths.root,
                                  session_key=first, prompt=long_prompt, system_prompt="system")
        await ctx.chats._consume(Backend(), request, first, DEV)
        request.resume_session_id = "resume"
        request.prompt = "second question"
        await ctx.chats._consume(Backend(), request, first, DEV)

    client.portal.call(run_turns)
    view = client.get(url).json()
    assert view["reported_tokens"] == 830 and view["context_window"] == 2000
    assert not view["partial"] and not view["can_inspect"]
    inputs = [part for part in view["sources"] if part["category"] == "task"]
    first_input, second_input = inputs
    assert first_input["source_kind"] == second_input["source_kind"] == "request_input"
    assert next(part["source_kind"] for part in view["sources"] if part["category"] == "system") == "request_system"
    assert first_input["truncated"] and second_input["text"] == "second question"
    fragment = f"/fragments/{first_input['id']}?key={first_input['text_key']}"
    assert client.get(url + fragment).json() == {"text": long_prompt}
    assert client.get(other_url).json()["sources"] == []
    assert client.get(other_url + fragment).status_code == 409
    assert client.post(url + "/inspect").status_code == 409
    assert client.delete(f"/api/chats/{first}").status_code == 200
    assert client.get(url).status_code == 404
