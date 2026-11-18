"""API connections, real HTTP wire translation, tools and project-bound resumption."""
import json
from contextlib import AsyncExitStack
from uuid import uuid4

import httpx
import pytest

from voxarium_studio.core.api_backends import ApiBackendConfig, connections
from voxarium_studio.core.application_config import application_config_file, load_application_config, save_application_config
from voxarium_studio.models.teams import TeamMember
from voxarium_studio.teams.backends.base import AgentRunRequest, available_backends, get_backend, RuntimeSessionKey, runtime_session_owner
from voxarium_studio.teams.backends.openai_api import OpenAIApiBackend


def connection(**kwargs):
    return ApiBackendConfig(id=str(uuid4()), name="My API", model="test-model", api_key="secret-canary", **{"api_mode": "chat_completions", **kwargs})


def request(tmp_path, **kwargs):
    return AgentRunRequest(member=TeamMember(team_id="test", name="API", model="", allowed_tools=["Read"]),
                           task=None, workdir=tmp_path, prompt="Hello", system_prompt="Role",
                           session_key="chat-test", **kwargs)


def stream(*chunks):
    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"


def text_stream(text="Answer"):
    return stream({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3,
                                           "prompt_tokens_details": {"cached_tokens": 4}}})


def fake_http(monkeypatch, responses):
    sent = []
    factory = httpx.AsyncClient

    def handle(req):
        sent.append((req, json.loads(req.content)))
        response = responses.pop(0)
        return httpx.Response(response[0], text=response[1], headers={"content-type": "text/event-stream"})

    monkeypatch.setattr("voxarium_studio.teams.backends.openai_api.httpx.AsyncClient",
                        lambda **kw: factory(transport=httpx.MockTransport(handle), **kw))
    return sent


def test_multiple_connections_global_only_secret_preserved(client, project):
    first, second = connection(), connection()
    for c in (first, second):
        body = {**c.public(), "api_key": c.api_key.get_secret_value()}
        body.pop("has_api_key")
        response = client.put(f"/api/settings/api-backends/{c.id}", json=body)
        assert response.status_code == 200, response.text
        assert "secret-canary" not in response.text
    assert len(connections()) == 2
    assert first.backend_id in available_backends()
    assert isinstance(get_backend(first.backend_id), OpenAIApiBackend)
    assert "secret-canary" not in client.get("/api/settings/application").text
    assert "secret-canary" not in client.get("/api/settings/api-backends").text
    assert "secret-canary" not in client.get("/api/teams/member-options").text
    application = load_application_config()
    application.respond_language = "Russian"
    save_application_config(application)
    assert connections()[0].api_key.get_secret_value() == "secret-canary"
    assert "api_backends" not in client.get("/api/settings").json()
    body = first.public()
    body.pop("has_api_key")
    body["name"] = "Renamed"
    assert client.put(f"/api/settings/api-backends/{first.id}", json=body).status_code == 200
    assert next(c for c in connections() if c.id == first.id).api_key.get_secret_value() == "secret-canary"
    body["api_key"] = ""
    assert client.put(f"/api/settings/api-backends/{first.id}", json=body).json()["has_api_key"] is False
    assert client.delete(f"/api/settings/api-backends/{first.id}").status_code == 200
    assert first.backend_id not in available_backends()
    assert second.backend_id in available_backends()
    assert "secret-canary" in application_config_file().read_text(encoding="utf-8")


def test_auto_strength_falls_back_to_connection_model(client):
    from voxarium_studio.core.config import AppConfig
    from voxarium_studio.teams.effort_solver import resolve_model

    c = connection()
    assert client.put(f"/api/settings/api-backends/{c.id}", json={"name": c.name, "model": c.model}).status_code == 200
    effective = AppConfig()
    assert [resolve_model(c.backend_id, n, effective) for n in (1, 2, 3)] == [c.model] * 3
    effective.orchestrator_models[c.backend_id] = {"1": "small", "2": "medium", "3": "large"}
    assert resolve_model(c.backend_id, 2, effective) == "medium"


@pytest.mark.parametrize("url", ["file:///secret", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret"])
def test_reject_invalid_connection_without_echoing_secret(client, url):
    c = connection()
    response = client.put(f"/api/settings/api-backends/{c.id}", json={"name": "API", "model": "test", "base_url": url, "api_key": "canary"})
    assert response.status_code == 400
    assert "canary" not in response.text and "secret" not in response.text


async def test_stream_and_resume_fresh_instance(monkeypatch, tmp_path):
    sent = fake_http(monkeypatch, [(200, text_stream()), (200, text_stream("Follow-up"))])
    c = connection()
    events = [e async for e in OpenAIApiBackend(c).run(request(tmp_path, permission_mode="plan"))]
    result = events[-1]
    assert result.kind == "result" and result.is_final and not result.data["is_error"]
    assert result.text == "Answer"
    assert result.data["total_tokens"] == 13
    assert result.data["usage"]["cache_read_input_tokens"] == 4
    assert sent[0][1]["model"] == "test-model"
    assert sent[0][0].headers["Authorization"] == "Bearer secret-canary"
    assert sent[0][0].url.path == "/v1/chat/completions"
    assert {t["function"]["name"] for t in sent[0][1]["tools"]} == {"Read"}
    follow = request(tmp_path, resume_session_id=result.session_id, permission_mode="plan")
    follow.prompt = "Continue"
    again = [e async for e in OpenAIApiBackend(c).run(follow)]
    assert again[-1].text == "Follow-up"
    assert [m["role"] for m in sent[1][1]["messages"]] == ["system", "user", "assistant", "user"]


async def test_tool_round_fragmented_arguments(monkeypatch, tmp_path):
    (tmp_path / "hello.txt").write_text("Tool text", encoding="utf-8")
    tool = stream({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1", "function": {"name": "Read", "arguments": '{"path":'}}]}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"hello.txt"}'}}]}, "finish_reason": "tool_calls"}]})
    sent = fake_http(monkeypatch, [(200, tool), (200, text_stream())])
    events = [e async for e in OpenAIApiBackend(connection()).run(request(tmp_path, permission_mode="plan"))]
    assert events[-1].data["is_error"] is False
    assert next(e for e in events if e.kind == "tool_result").text == "Tool text"
    assert sent[1][1]["messages"][-1] == {"role": "tool", "tool_call_id": "call1", "content": "Tool text"}


async def test_text_deltas_share_block_and_keep_plain_collectors_correct(monkeypatch, tmp_path):
    fake_http(monkeypatch, [(200, stream(
        {"choices": [{"delta": {"reasoning_content": "Think"}}]},
        {"choices": [{"delta": {"reasoning_content": " more"}}]},
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]},
    ))])
    events = [e async for e in OpenAIApiBackend(connection()).run(request(tmp_path))]
    text = [e for e in events if e.kind == "text"]
    thinking = [e for e in events if e.kind == "thinking"]
    assert len({e.data["stream_id"] for e in [*text, *thinking]}) == 1
    assert [e.data["stream_text"] for e in text] == ["Hello", "Hello world"]
    assert [e.data["stream_text"] for e in thinking] == ["Think", "Think more"]
    assert "".join(e.text for e in text) == events[-1].text == "Hello world"


@pytest.mark.parametrize("response", [(401, "secret-canary"), (429, "secret-canary"), (200, stream({"choices": [{"delta": {"content": "partial"}}]}))])
async def test_api_failures_are_final_and_do_not_leak_key(monkeypatch, tmp_path, response):
    fake_http(monkeypatch, [response])
    events = [e async for e in OpenAIApiBackend(connection()).run(request(tmp_path))]
    assert events[-1].is_final and events[-1].data["is_error"]
    assert "secret-canary" not in str(events)


@pytest.mark.parametrize("status,body,diagnostic", [
    (401, "secret-canary", "HTTP 401"),
    (429, "secret-canary", "HTTP 429"),
    (400, "Function tools with reasoning_effort are not supported; use /v1/responses secret-canary",
     "requires the Responses API"),
])
async def test_provider_diagnostic_survives_nested_mcp_groups(monkeypatch, tmp_path, status, body, diagnostic):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def transport():
        try:
            yield
        except Exception as exc:
            raise ExceptionGroup("secret-canary", [ExceptionGroup("transport", [exc])])

    runtime = OpenAIApiBackend(connection())

    async def tools(req, stack):
        await stack.enter_async_context(transport())
        return [], {}

    monkeypatch.setattr(runtime, "_tools", tools)
    fake_http(monkeypatch, [(status, body)])
    events = [e async for e in runtime.run(request(tmp_path))]
    assert events[-1].is_final and events[-1].data["is_error"]
    assert diagnostic in events[-1].text
    assert "ExceptionGroup" not in events[-1].text
    assert "secret-canary" not in str(events)


async def test_plan_tool_policy_and_text_only(tmp_path):
    c = OpenAIApiBackend(connection())
    req = request(tmp_path, permission_mode="plan")
    req.member.allowed_tools = ["Read", "Write", "Bash"]
    async with AsyncExitStack() as stack:
        tools, _ = await c._tools(req, stack)
        assert {t["function"]["name"] for t in tools} == {"Read"}
        req.text_only = True
        assert await c._tools(req, stack) == ([], {})
    with pytest.raises(ValueError, match="inside"):
        await c._native("Read", {"path": "../secret"}, req)


async def test_history_is_bound_to_managed_root_and_connection(monkeypatch, tmp_path):
    sent = fake_http(monkeypatch, [(200, text_stream()), (200, text_stream()), (200, text_stream())])
    c = connection()
    isolated = tmp_path / "checkout"
    isolated.mkdir()
    req = request(isolated, text_only=True,
                  runtime_key=RuntimeSessionKey(runtime_session_owner(tmp_path, "chat", "one")))
    events = [e async for e in OpenAIApiBackend(c).run(req)]
    assert list((tmp_path / ".voxarium_studio" / "api-sessions" / c.id).glob("*.json"))
    req2 = request(tmp_path / "other-project", resume_session_id=events[-1].session_id, text_only=True)
    [e async for e in OpenAIApiBackend(c).run(req2)]
    assert len(sent[1][1]["messages"]) == 2
    [e async for e in OpenAIApiBackend(connection()).run(request(tmp_path, resume_session_id=events[-1].session_id, text_only=True))]
    assert len(sent[2][1]["messages"]) == 2


@pytest.mark.parametrize("mode,kind", [("chat_completions", "task"), ("responses", "chat")])
async def test_context_viewer_reads_retained_api_history(monkeypatch, tmp_path, mode, kind):
    from voxarium_studio.api.session_context import fragment_response, read_context
    from voxarium_studio.models.task_context import ChatSessionContext, TaskContext
    from voxarium_studio.teams.context_viewer import fragment_key

    c = connection(api_mode=mode, context_window=16000)
    args = '{ "path": "hello.txt" }'
    (tmp_path / "hello.txt").write_text("Exact tool result", encoding="utf-8")
    if mode == "responses":
        def completed(*output):
            return stream({"type": "response.completed", "response": {
                "status": "completed", "output": output, "usage": {"input_tokens": 10, "output_tokens": 3},
            }})

        first = completed(
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Reasoning summary"}],
             "encrypted_content": "encrypted-canary"},
            {"type": "function_call", "call_id": "call1", "name": "Read", "arguments": args},
        )
        last = completed({"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": "Answer"}]})
    else:
        first = stream({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1",
                       "function": {"name": "Read", "arguments": args}}]}, "finish_reason": "tool_calls"}]})
        last = text_stream()
    sent = fake_http(monkeypatch, [(200, first), (200, last)])
    owner = runtime_session_owner(tmp_path, kind, "owner")
    req = request(tmp_path, permission_mode="plan", runtime_key=RuntimeSessionKey(owner))
    runtime = OpenAIApiBackend(c)
    events = [e async for e in runtime.run(req)]
    assert events[-1].data["is_error"] is False
    session_id = events[-1].session_id
    snapshot_class = TaskContext if kind == "task" else ChatSessionContext
    snapshot = snapshot_class(**{f"{kind}_id": owner.owner_id}, backend=c.backend_id,
                              sources=[{"category": "task", "text": "Studio fallback"}])
    parts, metrics = await read_context(snapshot, [], None, session_id, project_root=tmp_path)
    assert metrics["origin"] == "openai_api_journal"
    assert metrics["window"] == 16000
    assert metrics["reported"] == 10
    assert metrics["has_studio_view"]
    assert [part["text"] for part in parts if part["category"] == "tools"] == [args, "Exact tool result"]
    assert [part["text"] for part in parts if part["category"] == "thinking"] == (
        ["Reasoning summary"] if mode == "responses" else []
    )
    assert [part["text"] for part in parts if part["category"] == "history"] == ["Answer"]
    assert "encrypted-canary" not in str(parts)
    assert len(sent) == 2
    schema = next(part for part in parts if part["category"] == "tool_definitions")
    assert json.loads(schema["text"])["name"] == "Read"
    assert fragment_response(parts, parts.index(schema), fragment_key(schema)) == {"text": schema["text"]}
    studio, _ = await read_context(snapshot, [], None, session_id, "studio", project_root=tmp_path)
    assert [part["text"] for part in studio] == ["Studio fallback"]

    other = snapshot_class(**{f"{kind}_id": "other"}, backend=c.backend_id)
    assert (await read_context(other, [], None, session_id, project_root=tmp_path))[1]["origin"] == "studio"


async def test_real_studio_mcp_bridge(tmp_path):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("echo", "Echo a string", {"text": str})
    async def echo(args):
        return {"content": [{"type": "text", "text": args["text"]}]}

    server = create_sdk_mcp_server(name="test", tools=[echo])
    req = request(tmp_path, permission_mode="bypassPermissions", mcp_servers={"studio": server})
    async with AsyncExitStack() as stack:
        schemas, handlers = await OpenAIApiBackend(connection())._tools(req, stack)
        assert handlers
        session, name, display = next(iter(handlers.values()))
        assert display == "mcp__studio__echo"
        result = await session.call_tool(name, {"text": "real bridge"})
        assert result.content[0].text == "real bridge"
