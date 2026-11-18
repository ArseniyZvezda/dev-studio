"""Responses wire translation over the shared local API history (§10.6).

Use store=false and replay complete output items, including encrypted reasoning,
instead of depending on remotely stored response IDs. No tool executes in this layer.
"""
from __future__ import annotations


def input_items(messages):
    items = []
    for message in messages:
        role = message["role"]
        if role == "assistant" and message.get("responses_output"):
            items.extend(message["responses_output"])
            continue
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message["content"]})
            continue
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if part["type"] == "text":
                    parts.append({"type": "input_text", "text": part["text"]})
                elif part["type"] == "image_url":
                    parts.append({"type": "input_image", "image_url": part["image_url"]["url"],
                                  "detail": part["image_url"].get("detail", "auto")})
            content = parts
        if content:
            items.append({"role": role, "content": content})
        for call in message.get("tool_calls", []):
            items.append({"type": "function_call", "call_id": call["id"],
                          "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
    return items


def request_body(body):
    result = {"model": body["model"], "input": input_items(body["messages"]),
              "stream": body["stream"], "store": False, "include": ["reasoning.encrypted_content"],
              "max_output_tokens": body.get("max_completion_tokens", body.get("max_tokens", 4096))}
    if body.get("reasoning_effort"):
        result["reasoning"] = {"effort": body["reasoning_effort"]}
    if body.get("tools"):
        # Responses defaults function schemas to strict; Studio/MCP schemas allow
        # optional properties, so explicitly retain their non-strict contract.
        result["tools"] = [{"type": "function", **tool["function"], "strict": False} for tool in body["tools"]]
    return result


def usage_stats(response):
    usage = response.get("usage") or {}
    if not usage:
        return None
    return {"prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "prompt_tokens_details": {"cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)}}


def summary_result(response):
    text = "".join(part.get("text", "") for item in response.get("output", [])
                   if item.get("type") == "message" for part in item.get("content", [])
                   if part.get("type") == "output_text")
    return {"choices": [{"message": {"content": text},
                         "finish_reason": "stop" if response.get("status") == "completed" else "incomplete"}],
            "usage": usage_stats(response)}


class ResponseStream:
    """Normalize semantic SSE events to the existing tool/event loop's chunks."""

    def __init__(self):
        self.text = ""
        self.calls = set()

    def _call(self, item, index):
        call_id = item["call_id"]
        if call_id in self.calls:
            return None
        self.calls.add(call_id)
        return {"choices": [{"delta": {"tool_calls": [{"index": index, "id": call_id,
                "function": {"name": item["name"], "arguments": item["arguments"]}}]}}]}

    def chunks(self, event):
        kind = event.get("type")
        if kind == "response.output_text.delta":
            self.text += event["delta"]
            yield {"choices": [{"delta": {"content": event["delta"]}}]}
        elif kind == "response.refusal.delta":
            self.text += event["delta"]
            yield {"choices": [{"delta": {"refusal": event["delta"]}}]}
        elif kind == "response.reasoning_summary_text.delta":
            yield {"choices": [{"delta": {"reasoning_content": event["delta"]}}]}
        elif kind == "response.output_item.done" and event["item"].get("type") == "function_call":
            if chunk := self._call(event["item"], event["output_index"]):
                yield chunk
        elif kind in {"response.completed", "response.incomplete", "response.failed"}:
            response = event["response"]
            if kind == "response.failed":
                yield {"error": response.get("error") or {"code": "response_failed"}}
                return
            output = response.get("output", [])
            if kind == "response.completed":
                for index, item in enumerate(output):
                    if item.get("type") == "function_call":
                        if chunk := self._call(item, index):
                            yield chunk
                # Some compatible servers send only the terminal response.
                if not self.text:
                    text = summary_result(response)["choices"][0]["message"]["content"]
                    if text:
                        yield {"choices": [{"delta": {"content": text}}]}
            yield {"choices": [{"delta": {}, "finish_reason":
                    ("tool_calls" if self.calls else "stop") if kind == "response.completed" else "incomplete"}],
                   "usage": usage_stats(response), "responses_output": output}
        elif kind == "error":
            yield {"error": event}
