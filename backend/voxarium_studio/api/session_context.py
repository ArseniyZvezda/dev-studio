"""Shared Context Viewer responses for task and chat sessions (§10.4, §10.9)."""

import asyncio
from collections.abc import Sequence
from typing import Any, Literal

from fastapi import HTTPException

from ..models import AgentChatMessage, AgentMessage, TeamMember
from ..models.task_context import SessionContext
from ..teams.backends.base import live_session
from ..teams.context_journals import read_session
from ..teams.context_viewer import fragment_key, native_inventory, present, retained_context

ContextView = Literal["auto", "studio"]


async def read_context(
    snapshot: SessionContext | None, messages: Sequence[AgentMessage | AgentChatMessage],
    member: TeamMember | None, session_id: str | None, view: ContextView = "auto",
) -> tuple[list[dict], dict]:
    backend = snapshot.backend if snapshot and snapshot.backend else member.agent_backend if member else ""
    journal = None
    if view == "auto":
        notice = {"codex": "instructions_in_prompt", "qwen": "system_prompt_in_message"}.get(backend)
        hint = ""
        if snapshot and notice and any(m.kind == "system" and m.data.get("subtype") == notice
                                       and m.created_at >= snapshot.started_at for m in messages):
            hint = next((p["text"] for p in snapshot.sources if p["category"] == "system"), "")
        journal = await asyncio.to_thread(read_session, backend, session_id, studio_system=hint)
    return retained_context(snapshot, messages, journal, backend=backend)


def inventory_response(owner_id: str, parts: list[dict], metrics: dict) -> dict[str, Any]:
    return {**present(parts, **metrics),
            "can_inspect": callable(getattr(live_session(owner_id), "get_context_usage", None))}


def fragment_response(parts: list[dict], source_id: int, key: str) -> dict[str, str]:
    if 0 <= source_id < len(parts):
        part = parts[source_id]
        if fragment_key(part) == key:
            return {"text": part["text"]}
    raise HTTPException(status_code=409, detail="The context fragment changed. Refresh the map.")


async def inspect_session(owner_id: str) -> dict[str, Any]:
    """An explicit request to the existing runtime, never a new agent turn."""
    inspect = getattr(live_session(owner_id), "get_context_usage", None)
    if not callable(inspect):
        raise HTTPException(status_code=409, detail="Context inspection requires an active Claude Code session")
    try:
        report = await asyncio.wait_for(inspect(), timeout=45)
        if not isinstance(report, dict):
            raise ValueError("The runtime returned an invalid context breakdown")
        result = native_inventory(report)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Context inspection timed out") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {**result, "can_inspect": True}
