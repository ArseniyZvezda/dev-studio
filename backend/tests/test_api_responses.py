"""Responses wire round trips, stateless tool reasoning and failure boundaries (§10.6)."""
import json
from uuid import uuid4

import httpx
import pytest

from voxarium_studio.core.api_backends import ApiBackendConfig
from voxarium_studio.teams.backends.api_context import compact_history
from voxarium_studio.teams.backends.openai_api import OpenAIApiBackend
from test_openai_api_backend import connection, request, fake_http


def events(*items):
    return "".join("event: " + item["type"] + "\ndata: " + json.dumps(item) + "\n\n" for item in items)


def message(text):
    return {"type": "message", "id": "msg_one", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def completed(output, *, status="completed", stats=None):
    return {"type": "response." + status, "response": {"id": "resp_one", "status": status, "output": output,
            "usage": stats or {"input_tokens": 20, "input_tokens_details": {"cached_tokens": 5}, "output_tokens": 7}}}


def test_api_mode_auto_and_overrides():
    c = connection(api_mode="auto")
    assert c.effective_api_mode == "responses"
    c.base_url = "http://localhost:8080/v1"
    assert c.effective_api_mode == "chat_completions"
    c.api_mode = "responses"
    assert c.effective_api_mode == "responses"
    c.base_url = "https://api.openai.com.example.test/v1"
    c.api_mode = "auto"
    assert c.effective_api_mode == "chat_completions"


def test_model_catalog_persistence_and_member_options(client):
    id = str(uuid4())
    payload = {"name": "API", "model": "default", "models": [" other ", "default", "other", "third"],
               "api_mode": "responses", "api_key": "secret-canary"}
    result = client.put(f"/api/settings/api-backends/{id}", json=payload)
    assert result.status_code == 200
    assert result.json()["models"] == ["other", "default", "third"]
    # A stale settings client must not erase the new fields or the key.
    result = client.put(f"/api/settings/api-backends/{id}", json={"name": "Renamed", "model": "default"})
    assert result.json()["api_mode"] == "responses" and result.json()["has_api_key"]
    assert result.json()["models"] == ["other", "default", "third"]
    options = client.get("/api/teams/member-options").json()
    models = [m for m in options["suggested_models"] if m["backend"] == f"openai_api:{id}"]
    assert [m["id"] for m in models] == ["default", "other", "third"]
    assert all(m["group"] == "Renamed" for m in models)
    assert "secret-canary" not in str(options) + result.text
    assert "api_backends" not in client.get("/api/settings").json()
    assert client.put(f"/api/settings/api-backends/{id}", json={"name": "API", "model": "default", "models": []}).json()["models"] == []


@pytest.mark.parametrize("field,value", [("models", [""]), ("models", [" " ]), ("models", ["x" * 201]),
                                         ("models", "one\ntwo"), ("api_mode", "unknown")])
def test_invalid_catalog_or_protocol(client, field, value):
    payload = {"name": "API", "model": "default", field: value}
    assert client.put(f"/api/settings/api-backends/{uuid4()}", json=payload).status_code == 400


async def test_stream_tools_reasoning_usage_and_resume(monkeypatch, tmp_path):
    (tmp_path / "evidence.txt").write_text("read evidence", encoding="utf-8")
    reasoning = {"id": "rs_one", "type": "reasoning", "summary": [], "encrypted_content": "opaque-state"}
    call = {"id": "fc_one", "type": "function_call", "status": "completed", "call_id": "call_one",
            "name": "Read", "arguments": '{"path":"evidence.txt"}'}
    sent = fake_http(monkeypatch, [
        (200, events({"type": "response.reasoning_summary_text.delta", "delta": "Inspecting evidence"},
                     {"type": "response.output_item.added", "output_index": 1, "item": {**call, "arguments": ""}},
                     {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"path":'},
                     {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '"evidence.txt"}'},
                     {"type": "response.output_item.done", "output_index": 1, "item": call}, completed([reasoning, call]))),
        (200, events({"type": "response.output_text.delta", "delta": "Read "},
                     {"type": "response.output_text.delta", "delta": "successfully"}, completed([message("Read successfully")]))),
        (200, events(completed([message("Resumed")])))])
    c = connection(api_mode="responses")
    runtime = OpenAIApiBackend(c)
    result = [e async for e in runtime.run(request(tmp_path, effort="low"))]
    assert result[-1].text == "Read successfully" and not result[-1].data["is_error"]
    assert len([e for e in result if e.kind == "tool_use"]) == 1
    assert any(e.kind == "tool_result" and e.text == "read evidence" for e in result)
    assert any(e.kind == "thinking" and e.text == "Inspecting evidence" for e in result)
    assert result[-1].data["usage"] == {"input_tokens": 30, "output_tokens": 14, "cache_read_input_tokens": 10}
    assert all(req.url.path == "/v1/responses" for req, _ in sent)
    body = sent[0][1]
    assert body["store"] is False and body["reasoning"] == {"effort": "low"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert "messages" not in body and "stream_options" not in body
    assert body["tools"][0]["name"] == "Read" and body["tools"][0]["strict"] is False
    assert sent[1][1]["input"][-3:] == [reasoning, call, {"type": "function_call_output", "call_id": "call_one", "output": "read evidence"}]
    resumed = [e async for e in OpenAIApiBackend(c).run(request(tmp_path, resume_session_id=result[-1].session_id))]
    assert resumed[-1].text == "Resumed"
    assert reasoning in sent[2][1]["input"]
    assert sent[2][1]["input"][-2:] == [message("Read successfully"), {"role": "user", "content": "Hello"}]


@pytest.mark.parametrize("terminal", ["incomplete", "failed", "interrupted"])
async def test_incomplete_stream_never_executes_tools(monkeypatch, tmp_path, terminal):
    call = {"id": "fc_one", "type": "function_call", "call_id": "call_one", "name": "Write",
            "arguments": '{"path":"unexpected.txt","content":"must not run"}'}
    wire = events({"type": "response.output_item.done", "output_index": 0, "item": call})
    if terminal != "interrupted":
        wire += events(completed([call], status=terminal))
    fake_http(monkeypatch, [(200, wire)])
    req = request(tmp_path)
    req.member.allowed_tools = ["Write"]
    result = [e async for e in OpenAIApiBackend(connection(api_mode="responses")).run(req)]
    assert result[-1].data["is_error"]
    assert not (tmp_path / "unexpected.txt").exists()
    assert not any(e.kind == "tool_use" for e in result)


async def test_images_and_legacy_chat_history(monkeypatch, tmp_path):
    image = tmp_path / "test.png"
    image.write_bytes(b"image-bytes")
    sent = fake_http(monkeypatch, [(200, events(completed([message("Seen")])))])
    c = connection(api_mode="responses")
    runtime = OpenAIApiBackend(c)
    req = request(tmp_path, resume_session_id=uuid4().hex, images=[{"path": str(image), "media_type": "image/png"}])
    runtime._save(runtime._history_path(req, req.resume_session_id), [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "old", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]}])
    result = [e async for e in runtime.run(req)]
    assert not result[-1].data["is_error"]
    items = sent[0][1]["input"]
    assert items[1]["type"] == "function_call"
    assert items[2]["type"] == "function_call_output" and "Interrupted" in items[2]["output"]
    assert items[-1]["content"][1]["type"] == "input_image"
    assert items[-1]["content"][1]["image_url"].startswith("data:image/png;base64,")


async def test_compaction_uses_responses_and_removes_opaque_state():
    sent = []
    def handler(req):
        sent.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json=completed([message("Goal: continue safely")])["response"])
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    history = [{"role": "assistant", "content": "Earlier result", "responses_output": [
        {"type": "reasoning", "encrypted_content": "opaque-secret-state"}]}, {"role": "user", "content": "Continue"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        compacted = await compact_history(client, "https://api.openai.com/v1/chat/completions", {}, "test",
                                          history, 4096, 100000, usage, api_mode="responses")
    assert compacted[-1] == history[-1] and "Goal: continue safely" in compacted[0]["content"]
    assert all(path == "/v1/responses" and body["stream"] is False for path, body in sent)
    assert "opaque-secret-state" not in str(sent)
    assert usage == {"input_tokens": 15, "output_tokens": 7, "cache_read_input_tokens": 5}
