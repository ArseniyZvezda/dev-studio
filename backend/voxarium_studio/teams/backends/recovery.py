"""Board/chat context to recover a missing runtime session (§10.6)."""

from __future__ import annotations

from typing import Any

from ...models.teams import TeamTask


def recovery_prompt(prompt: str, messages: list[Any], task: TeamTask | None = None) -> str:
    parts = [
        "The previous runtime session is unavailable. Continue the existing work using "
        "the saved context below. Check the current board and files before repeating "
        "completed actions or creating tasks. This is a recovery, not a new assignment."
    ]
    if task is not None:
        parts.append(f"Task: {task.title}\n\nBrief:\n{task.brief}")
        if task.result_summary:
            parts.append(f"Previous summary:\n{task.result_summary}")
        if task.review_comment:
            parts.append(f"Review feedback:\n{task.review_comment}")
    # A compressed handoff is the durable context, even after many later messages (§10.4).
    for message in messages:
        if message.kind in {"compressed summary", "context_summary"}:
            parts.append("Compressed task history:\n" + message.text)
    history = []
    remaining = 32_000
    for message in reversed(messages):
        if message.kind not in {"text", "note", "review", "delegate", "doc", "result"}:
            continue
        if not message.text:
            continue
        entry = f"[{message.role}/{message.kind}] {message.text}"
        history.append(entry[-remaining:])
        remaining -= len(entry)
        if remaining <= 0:
            break
    if history:
        parts.append("Recent saved transcript (may be incomplete):\n" + "\n\n".join(reversed(history)))
    parts.append(f"Current request:\n{prompt}")
    return "\n\n".join(parts)
