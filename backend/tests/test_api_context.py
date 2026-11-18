"""90% window policy, bounded compaction requests and safe resumable replacement."""
import json
import asyncio
from uuid import uuid4

import httpx
import pytest

from voxarium_studio.core.api_backends import ApiBackendConfig
from voxarium_studio.teams.backends.api_context import ContextUsage, estimate_tokens
from voxarium_studio.teams.backends.openai_api import OpenAIApiBackend
from test_openai_api_backend import request, stream, text_stream


def backend(window=65536):
    return OpenAIApiBackend(ApiBackendConfig(id=str(uuid4()), name="Local", model="local-model", context_window=window))


def fake_provider(monkeypatch, *, completion=None, summary_status=200, summary_text="Goal: finish the task. Completed tool actions must not be repeated. Continue from their results."):
    factory = httpx.AsyncClient
    sent = []
    normal = 0

    def handle(req):
        nonlocal normal
        body = json.loads(req.content)
        sent.append(body)
        if body["stream"]:
            normal += 1
            value = completion(normal) if completion else text_stream()
            return httpx.Response(200, text=value, headers={"content-type": "text/event-stream"})
        return httpx.Response(summary_status, json={"choices": [{"message": {"content": summary_text}, "finish_reason": "stop"}],
                                                  "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    monkeypatch.setattr("voxarium_studio.teams.backends.openai_api.httpx.AsyncClient",
                        lambda **kw: factory(transport=httpx.MockTransport(handle), **kw))
    return sent


@pytest.mark.parametrize("value", [0, -1, 1023, 1.5, True, "65536"])
def test_window_validation_and_global_persistence(client, value):
    id = str(uuid4())
    payload = {"name": "API", "model": "local", "context_window": 65536}
    response = client.put(f"/api/settings/api-backends/{id}", json=payload)
    assert response.status_code == 200 and response.json()["context_window"] == 65536
    # Older clients may omit the new field; that must not disable configured compaction.
    assert client.put(f"/api/settings/api-backends/{id}", json={"name": "Renamed", "model": "local"}).json()["context_window"] == 65536
    payload["context_window"] = value
    assert client.put(f"/api/settings/api-backends/{id}", json=payload).status_code == 400
    assert "context_window" not in client.get("/api/settings").json()


@pytest.mark.parametrize("window", [4096, 65536])
@pytest.mark.parametrize("crossed", [False, True])
async def test_exact_90_percent_threshold_and_bounded_summary_requests(monkeypatch, tmp_path, window, crossed):
    sent = fake_provider(monkeypatch)
    runtime = backend(window)
    req = request(tmp_path)
    history = [{"role": "assistant", "content": "Earlier work. " * (window // 4)},
               {"role": "user", "content": "Keep this exact latest instruction."}]
    current_estimate = estimate_tokens(req.system_prompt, history, [])
    # Choose a multiple of ten to test the boundary without rounding ambiguity.
    runtime.connection.context_window = window // 10 * 10
    threshold = runtime.connection.context_window * 9 // 10
    context = ContextUsage(threshold if crossed else threshold - 1, current_estimate)
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    original = list(history)
    path = runtime._history_path(req, uuid4().hex)
    runtime._save(path, history, context)
    async with httpx.AsyncClient() as client:
        events = [e async for e in runtime._compact_if_needed(client, req, req.model or "local-model", history, [], usage, context, path, "same-session")]
    if not crossed:
        assert not sent and not events and history == original
        return
    assert len(sent) > 1  # Even a single large message must be split.
    for body in sent:
        assert body["stream"] is False and "tools" not in body
        input_bound = sum(len(m["content"].encode("utf-8")) + 16 for m in body["messages"])
        assert input_bound + body["max_completion_tokens"] < window
    assert history[-1] == original[-1]
    assert len(history) == 2 and "compressed" in history[0]["content"]
    assert any(e.data.get("subtype") == "context_compacted" for e in events)
    assert usage["input_tokens"] == len(sent) * 100
    assert usage["output_tokens"] == len(sent) * 20
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["messages"] == history
    assert saved["context_usage"] == context.dump()


async def test_large_file_read_is_bounded_inside_active_run(monkeypatch, tmp_path):
    (tmp_path / "huge.txt").write_text("start-marker " + "tool evidence " * 40000 + " end-marker", encoding="utf-8")

    def completion(n):
        if n == 1:
            return stream({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1", "function": {
                "name": "Read", "arguments": '{"path":"huge.txt"}'}}]}, "finish_reason": "tool_calls"}]},
                {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
        return text_stream("Finished after tool result compaction")

    sent = fake_provider(monkeypatch, completion=completion)
    events = [e async for e in backend().run(request(tmp_path, permission_mode="plan", max_turns=2))]
    assert not events[-1].data["is_error"], events[-1].text
    normal = [body for body in sent if body["stream"]]
    summaries = [body for body in sent if not body["stream"]]
    assert len(normal) == 2 and not summaries
    output = normal[1]["messages"][-1]["content"]
    assert "start-marker" in output and "end-marker" not in output
    assert "Read truncated" in output and "offset=" in output
    assert len(output.encode("utf-8")) <= 8192
    assert events[-1].data["total_tokens"] == 123


async def test_legacy_history_resume_and_compaction_failure_preserve_data(monkeypatch, tmp_path):
    runtime = backend()
    session_id = uuid4().hex
    req = request(tmp_path, resume_session_id=session_id)
    history = [{"role": "user", "content": "Original goal"},
               {"role": "assistant", "content": "Important history " * 20000}]
    path = runtime._history_path(req, session_id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(history), encoding="utf-8")
    fake_provider(monkeypatch, summary_status=500)
    events = [e async for e in runtime.run(req)]
    assert not events[-1].data["is_error"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "local recovery" in saved["messages"][0]["content"]
    archives = list(path.parent.glob("*.before-recovery-*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["messages"] == [*history, {"role": "user", "content": req.prompt}]


async def test_reported_usage_triggers_after_response_and_survives_resume(monkeypatch, tmp_path):
    runtime = backend()
    session_id = uuid4().hex
    req = request(tmp_path, resume_session_id=session_id)
    path = runtime._history_path(req, session_id)
    # Initial estimate below 90%; the provider's actual usage crosses the threshold.
    history = [{"role": "user", "content": "Prior goal"},
               {"role": "assistant", "content": "history " * 26000}]
    runtime._save(path, history)

    def completion(n):
        if n == 1:
            return stream({"choices": [{"delta": {"content": "Completed"}, "finish_reason": "stop"}]},
                          {"choices": [], "usage": {"prompt_tokens": 60000, "completion_tokens": 10}})
        return text_stream("Next answer")

    sent = fake_provider(monkeypatch, completion=completion)
    events = [e async for e in runtime.run(req)]
    assert not events[-1].data["is_error"], events[-1].text
    assert sent[0]["stream"] is True
    assert any(not body["stream"] for body in sent)
    assert any(e.data.get("context_window") == 65536 for e in events)
    compressed = json.loads(path.read_text(encoding="utf-8"))
    assert "compressed" in compressed["messages"][0]["content"]
    before = len(sent)
    again = [e async for e in OpenAIApiBackend(runtime.connection).run(request(tmp_path, resume_session_id=session_id))]
    assert not again[-1].data["is_error"]
    assert len(sent) == before + 1
    assert "compressed" in sent[-1]["messages"][1]["content"]


async def test_64k_summaries_are_reduced_again_when_first_pass_is_too_large(monkeypatch, tmp_path):
    runtime = backend()
    id = uuid4().hex
    req = request(tmp_path, resume_session_id=id)
    path = runtime._history_path(req, id)
    runtime._save(path, [{"role": "user", "content": "Goal"},
                         {"role": "assistant", "content": "evidence " * 48000}])
    sent = fake_provider(monkeypatch, summary_text="Useful summary. " * 250)
    events = [e async for e in runtime.run(req)]
    assert not events[-1].data["is_error"], events[-1].text
    summaries = [body for body in sent if not body["stream"]]
    assert any("Useful summary." in body["messages"][1]["content"] for body in summaries)
    for body in summaries:
        assert sum(len(m["content"].encode("utf-8")) + 16 for m in body["messages"]) + body["max_completion_tokens"] < 65536


@pytest.mark.parametrize("ending", ["empty", "cancel"])
async def test_incomplete_compaction_preserves_exact_history_and_cancellation(monkeypatch, tmp_path, ending):
    runtime = backend()
    id = uuid4().hex
    req = request(tmp_path, resume_session_id=id)
    path = runtime._history_path(req, id)
    original = [{"role": "user", "content": "Exact old instruction"},
                {"role": "assistant", "content": "evidence " * 48000}]
    runtime._save(path, original)
    if ending == "empty":
        fake_provider(monkeypatch, summary_text="")
        events = [e async for e in runtime.run(req)]
        assert not events[-1].data["is_error"]
    else:
        factory = httpx.AsyncClient

        async def cancel(req):
            raise asyncio.CancelledError()

        monkeypatch.setattr("voxarium_studio.teams.backends.openai_api.httpx.AsyncClient",
                            lambda **kw: factory(transport=httpx.MockTransport(cancel), **kw))
        with pytest.raises(asyncio.CancelledError):
            [e async for e in runtime.run(req)]
    saved = json.loads(path.read_text(encoding="utf-8"))
    if ending == "empty":
        archive = next(path.parent.glob("*.before-recovery-*.json"))
        saved = json.loads(archive.read_text(encoding="utf-8"))
    assert saved["messages"] == [*original, {"role": "user", "content": req.prompt}]


async def test_no_space_for_fixed_context_fails_without_dropping_latest_user(monkeypatch, tmp_path):
    runtime = backend(4096)
    id = uuid4().hex
    req = request(tmp_path, resume_session_id=id)
    req.prompt = "Latest instruction " * 4000
    path = runtime._history_path(req, id)
    original = [{"role": "assistant", "content": "Previous work"}]
    runtime._save(path, original)
    sent = fake_provider(monkeypatch)
    events = [e async for e in runtime.run(req)]
    assert events[-1].data["is_error"] and not sent
    assert json.loads(path.read_text(encoding="utf-8"))["messages"][-1]["content"] == req.prompt


async def test_local_provider_gets_legacy_output_limit(monkeypatch, tmp_path):
    runtime = backend()
    runtime.connection.base_url = "http://localhost:11434/v1"
    id = uuid4().hex
    req = request(tmp_path, resume_session_id=id)
    runtime._save(runtime._history_path(req, id), [{"role": "assistant", "content": "evidence " * 40000}])
    sent = fake_provider(monkeypatch)
    events = [e async for e in runtime.run(req)]
    assert not events[-1].data["is_error"], events[-1].text
    summaries = [body for body in sent if not body["stream"]]
    assert summaries and all("max_tokens" in body and "max_completion_tokens" not in body for body in summaries)


@pytest.mark.parametrize("window", [1024, 64000, None])
async def test_multimegabyte_single_line_read_pages_without_loading_whole_file(tmp_path, monkeypatch, window):
    runtime = backend(window)
    req = request(tmp_path)
    text = "Начало🙂" + "я" * 1_100_000 + "END"
    target = tmp_path / "minified.html"
    target.write_text(text, encoding="utf-8")
    # Read must stream an excerpt even if the file exceeds the transport limit.
    req.max_message_bytes = 4096
    monkeypatch.setattr(type(target), "read_text", lambda *a, **kw: pytest.fail("whole file loaded"))
    output = await runtime._native("Read", {"path": target.name, "limit": 99_999_999}, req)
    assert len(output.encode("utf-8")) <= runtime._tool_budget(req)
    excerpt, marker = output.split("\n[Read truncated", 1)
    assert text.startswith(excerpt)
    assert f"offset={len(excerpt)}" in marker
    second = await runtime._native("Read", {"path": target.name, "offset": len(excerpt), "limit": 12}, req)
    assert second.startswith(text[len(excerpt):len(excerpt) + 12])
    end = await runtime._native("Read", {"path": target.name, "offset": len(text) - 3}, req)
    assert end == "END"


async def test_all_tool_outputs_are_bounded_and_full_mcp_output_is_archived(monkeypatch, tmp_path):
    from types import SimpleNamespace

    runtime = backend(4096)
    req = request(tmp_path, max_turns=2)
    full = json.dumps({"content": [{"type": "text", "text": "я" * 100000}]}, ensure_ascii=False)

    class Session:
        async def call_tool(self, name, args):
            return SimpleNamespace(isError=False, model_dump_json=lambda **kw: full)

    async def tools(request, stack):
        return [{"type": "function", "function": {"name": "mcp_test", "description": "test", "parameters": {"type": "object"}}}], {"mcp_test": (Session(), "test", "mcp__test")}

    monkeypatch.setattr(runtime, "_tools", tools)
    def completion(n):
        if n == 1:
            return stream({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "../call1", "function": {
                "name": "mcp_test", "arguments": '{}'}}]}, "finish_reason": "tool_calls"}]})
        return text_stream()
    sent = fake_provider(monkeypatch, completion=completion)
    events = [e async for e in runtime.run(req)]
    assert not events[-1].data["is_error"], events[-1].text
    output = next(e.text for e in events if e.kind == "tool_result")
    assert len(output.encode("utf-8")) <= runtime._tool_budget(req)
    assert "truncated" in output
    artifact = next(tmp_path.glob(".voxarium_studio/api-sessions/*/tool-results/*/*.txt"))
    assert artifact.read_text(encoding="utf-8") == full
    assert len(json.dumps(sent[-1], ensure_ascii=False).encode("utf-8")) < 4096


@pytest.mark.parametrize("failure", ["http", "stream"])
async def test_context_rejection_recovers_same_turn_and_never_repeats_tool_actions(monkeypatch, tmp_path, failure):
    runtime = backend(64000)
    req = request(tmp_path, permission_mode="bypassPermissions", max_turns=2)
    factory = httpx.AsyncClient
    normal, tool_runs, sent = 0, [], []
    native = runtime._native

    async def count(name, args, request, **kwargs):
        tool_runs.append(name)
        return await native(name, args, request, **kwargs)

    monkeypatch.setattr(runtime, "_native", count)
    def handle(http_req):
        nonlocal normal
        body = json.loads(http_req.content)
        sent.append(body)
        if not body["stream"]:
            return httpx.Response(500, text="summary unavailable secret-canary")
        normal += 1
        if normal == 1:
            text = stream({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "write-once", "function": {
                "name": "Write", "arguments": '{"path":"result.txt","content":"done"}'}}]}, "finish_reason": "tool_calls"}]})
        elif normal == 2:
            if failure == "http":
                return httpx.Response(400, json={"error": {"code": "context_length_exceeded", "message": "secret-canary"}})
            text = stream({"error": {"message": "maximum context length exceeded secret-canary"}})
        else:
            text = text_stream("Recovered")
        return httpx.Response(200, text=text)

    monkeypatch.setattr("voxarium_studio.teams.backends.openai_api.httpx.AsyncClient",
                        lambda **kw: factory(transport=httpx.MockTransport(handle), **kw))
    events = [e async for e in runtime.run(req)]
    assert not events[-1].data["is_error"], events[-1].text
    assert events[-1].text == "Recovered" and normal == 3
    assert tool_runs == ["Write"]
    assert "secret-canary" not in str(events)
    assert any(e.data.get("local_recovery") for e in events)
    assert not any(m.get("tool_calls") or m["role"] == "tool" for m in sent[-1]["messages"])
    saved = json.loads(runtime._history_path(req, events[-1].session_id).read_text(encoding="utf-8"))
    assert saved["context_usage"]["window_limit"] == 32000


async def test_vs185_oversized_legacy_session_recovers_without_summary_request(monkeypatch, tmp_path):
    runtime = backend(64000)
    id = uuid4().hex
    req = request(tmp_path, resume_session_id=id)
    original = [{"role": "user", "content": "VS-185: show chat-launcher everywhere"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "read1", "type": "function", "function": {"name": "Read", "arguments": '{}'}}]},
                {"role": "tool", "tool_call_id": "read1", "content": "<html>" + "x" * 2_163_405 + "</html>"}]
    path = runtime._history_path(req, id)
    runtime._save(path, original)
    sent = fake_provider(monkeypatch)
    events = [e async for e in runtime.run(req)]
    assert not events[-1].data["is_error"], events[-1].text
    assert len(sent) == 1 and sent[0]["stream"]
    assert "VS-185" in sent[0]["messages"][1]["content"]
    assert len(json.dumps(sent[0]).encode()) < 64000
    archive = next(path.parent.glob("*.before-recovery-*.json"))
    assert json.loads(archive.read_text(encoding="utf-8"))["messages"][:-1] == original


@pytest.mark.parametrize("failure", ["group", "timeout"])
async def test_summary_failure_recovers_without_failing_agent(monkeypatch, tmp_path, failure):
    runtime = backend(4096)
    id = uuid4().hex
    req = request(tmp_path, resume_session_id=id)
    runtime._save(runtime._history_path(req, id), [{"role": "user", "content": "Original goal"},
                                               {"role": "assistant", "content": "evidence " * 2000}])
    async def fail(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError("secret-canary")
        raise ExceptionGroup("transport failed", [RuntimeError("secret-canary")])
    monkeypatch.setattr("voxarium_studio.teams.backends.openai_api.compact_history", fail)
    fake_provider(monkeypatch)
    events = [e async for e in runtime.run(req)]
    assert not events[-1].data["is_error"], events[-1].text
    assert any(e.data.get("local_recovery") for e in events)
    assert "secret-canary" not in str(events)


async def test_permanent_provider_context_rejection_has_bounded_retries(monkeypatch, tmp_path):
    runtime = backend(65536)
    req = request(tmp_path, max_turns=1, text_only=True)
    factory = httpx.AsyncClient
    sent = []
    def handle(http_req):
        body = json.loads(http_req.content)
        sent.append(body)
        if not body["stream"]:
            return httpx.Response(500)
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded", "message": "secret-canary"}})
    monkeypatch.setattr("voxarium_studio.teams.backends.openai_api.httpx.AsyncClient",
                        lambda **kw: factory(transport=httpx.MockTransport(handle), **kw))
    events = [e async for e in runtime.run(req)]
    assert events[-1].data["is_error"]
    assert "Check the configured context window" in events[-1].text
    assert 2 <= sum(body["stream"] for body in sent) <= 7
    assert "secret-canary" not in str(events)
    saved = json.loads(runtime._history_path(req, events[-1].session_id).read_text(encoding="utf-8"))
    assert saved["context_usage"]["window_limit"] == 1024


async def test_fixed_context_diagnostic_survives_mcp_exception_group(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    runtime = backend(4096)
    req = request(tmp_path)
    req.prompt = "oversized instruction " * 5000
    @asynccontextmanager
    async def mcp_stack():
        try:
            yield
        except Exception as exc:
            raise ExceptionGroup("MCP transport task group", [exc])
    async def tools(request, stack):
        await stack.enter_async_context(mcp_stack())
        return [], {}
    monkeypatch.setattr(runtime, "_tools", tools)
    sent = fake_provider(monkeypatch)
    events = [e async for e in runtime.run(req)]
    assert events[-1].data["is_error"] and not sent
    assert "leave no room" in events[-1].text
    assert "ExceptionGroup" not in events[-1].text


async def test_lower_provider_budget_is_reused_on_resume(monkeypatch, tmp_path):
    runtime = backend(64000)
    id = uuid4().hex
    req = request(tmp_path, resume_session_id=id)
    runtime._save(runtime._history_path(req, id), [{"role": "user", "content": "Goal"}], ContextUsage(window_limit=8000))
    sent = fake_provider(monkeypatch)
    events = [e async for e in runtime.run(req)]
    assert not events[-1].data["is_error"]
    assert sent[0]["max_completion_tokens"] == 800


async def test_truncated_output_can_be_paged_from_isolated_checkout(tmp_path):
    from voxarium_studio.teams.backends.base import RuntimeSessionKey, runtime_session_owner
    runtime = backend(1024)
    checkout = tmp_path / "isolated"
    checkout.mkdir()
    req = request(checkout, runtime_key=RuntimeSessionKey(runtime_session_owner(tmp_path, "task", "one")))
    id = uuid4().hex
    path = runtime._history_path(req, id)
    output = await runtime._bound_output("evidence " * 10000, req, path, "call1")
    assert len(output.encode()) <= runtime._tool_budget(req)
    reference = output.split("Full output: ")[1].split(". Read with")[0]
    excerpt = await runtime._native("Read", {"path": reference, "limit": 8}, req)
    assert excerpt.startswith("evidence")
    with pytest.raises(ValueError):
        await runtime._native("Write", {"path": reference, "content": "oops"}, req)
