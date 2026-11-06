"""Evaluate a proposed Producer task before allocating its board row (§10.11)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import MAX_MESSAGE_BYTES_DEFAULT
from ..models.teams import TeamMember
from .backends.base import AgentRunRequest, get_backend
from .effort_solver import (
    DEFAULT_SYSTEM_PROMPT,
    EFFORT_LEVELS,
    EffortScores,
    calculate_effort,
    parse_scores,
)
from .usage import RunUsage

SOLVER_TIMEOUT = 180.0


@dataclass(frozen=True, slots=True)
class EffortSolution:
    scores: EffortScores
    effort: int
    effort_level: str
    usage: dict[str, Any] = field(default_factory=dict)


async def solve_effort(
    member: TeamMember, title: str, brief: str,
    *, max_message_bytes: int = MAX_MESSAGE_BYTES_DEFAULT,
) -> EffortSolution:
    """One isolated evaluation using the Solver's model and its configured weights.

    Only the proposed title and brief enter the prompt. This call is awaited inside
    the Producer's current slot; acquiring another slot would deadlock at capacity.
    Cancellation/timeout closes the backend stream, including its child process.
    """
    backend = get_backend(member.agent_backend)
    available, reason = backend.is_available()
    if not available:
        raise RuntimeError(reason or f"Effort Solver backend {member.agent_backend} is unavailable")

    async def evaluate(workdir: Path) -> EffortSolution:
        request = AgentRunRequest(
            member=member.model_copy(update={"allowed_tools": [], "skills": [], "agent": None}),
            task=None, workdir=workdir,
            prompt=json.dumps({"title": title, "description": brief}, ensure_ascii=False),
            system_prompt=member.responsibility or DEFAULT_SYSTEM_PROMPT,
            model=member.model, effort=member.effort,
            permission_mode="plan", max_turns=1, text_only=True,
            max_message_bytes=max_message_bytes,
            session_key=f"effort-solver-{uuid.uuid4().hex}",
        )
        parts: list[str] = []
        final = ""
        usage = RunUsage()
        result_data: dict[str, Any] = {}
        stream = backend.run(request)
        try:
            async for event in stream:
                if event.kind == "error" or event.data.get("is_error"):
                    raise RuntimeError(event.text or "Effort Solver returned an error")
                usage.observe(event)
                if event.kind == "text" and event.text:
                    parts.append(event.text)
                elif event.kind == "result":
                    final = event.text
                    result_data = event.data
                    usage.finish(result_data)
        finally:
            await stream.aclose()
        scores = parse_scores((final or "\n".join(parts)).strip())
        effort = calculate_effort(scores, member.effort_solver_weights)
        return EffortSolution(
            scores=scores, effort=effort, effort_level=EFFORT_LEVELS[effort - 1],
            usage={**usage.counters(), "total_cost_usd": result_data.get("total_cost_usd", 0),
                   "num_turns": result_data.get("num_turns", 0)},
        )

    # No managed-project files, instructions, chat history or Studio tools are supplied.
    with tempfile.TemporaryDirectory(prefix="voxarium_studio-effort-") as directory:
        try:
            return await asyncio.wait_for(evaluate(Path(directory)), timeout=SOLVER_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"Effort Solver did not answer within {SOLVER_TIMEOUT:.0f}s") from exc
