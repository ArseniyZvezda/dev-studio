"""A commit drafted for a task the moment it reaches review (§10.4).

The Commit section beside a task's transcript shows three things: the files the task
changed, a title, and a description. The first two are derived — the changes column already
knows the files, and the title is ``<display id> | <task title>`` so a commit log reads like
the board. The description is the one thing worth a model: a paragraph over work that is
already summarised (the task's own result, the diff) is exactly what a cheap model does well
and a reviewer would otherwise type by hand for every card.

What this module owns is the prompt and the call. **When** a draft is made is the
orchestrator's — on every arrival in ``needs_review``, because a task sent back and
resubmitted has new work to describe — and **where** it is committed is the
:class:`~voxarium_studio.vcs.base.VcsBackend`'s, so a Plastic project checks in through the same
button a git one commits through. Nothing here names git.

The describer is a function the orchestrator holds rather than a call it makes, so the
suite swaps in a fake and no test ever reaches the Agent SDK (``testing.md``).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

Describer = Callable[[str, str, Path], Awaitable[str]]
"""``(prompt, model, cwd) -> description``. Raises on anything but an answer."""

PATCH_LIMIT = 24_000
"""Characters of patch handed to the model. A task that rewrote a generated asset produces a
diff no description needs to read in full; what is cut is said in the prompt."""

DESCRIBE_TIMEOUT = 180.0
"""Seconds for the one-turn call. The CLI starts in a few seconds and a paragraph takes a
few more; past this the draft is recorded as failed rather than left generating forever."""

DESCRIBE_EFFORT = "low"
"""The paragraph is a summary of a summary, so no reasoning is worth waiting for. Sent with
thinking turned off: ``haiku`` is Haiku 4.5 on the bundled CLI 2.1.280, which has no effort
levels and is given a thinking budget by the CLI otherwise — measured on a 24K-character
diff, 2169 thinking tokens and 23 s against 0 and 3.5 s."""

SYSTEM_PROMPT = (
    "You write commit descriptions for a game project's version control. You are given one "
    "task from the team board — its title, brief, the agent's own summary of what it did, "
    "the files it changed and a diff — and you answer with the description only: plain "
    "text, no heading, no title line, no Markdown fences, no preamble. Two to six short "
    "sentences or a short bullet list saying what changed and why, in the language the task "
    "is written in. Never invent changes the diff does not show."
)


@dataclass(slots=True)
class CommitWork:
    """Everything the prompt is built from — gathered by the orchestrator, worded here."""

    display_id: str
    title: str
    brief: str = ""
    summary: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    """As the changes column reports them: ``path``, ``kind``, the counts where known."""

    patch: str = ""


def commit_title(display_id: str, title: str) -> str:
    """``VS-01 | Add the grapple hook`` — the board's own id first, so a log reads back to a
    card. One line: a newline in a task title would become a second subject line."""
    return f"{display_id} | {' '.join(title.split())}".strip()


def build_prompt(work: CommitWork) -> str:
    lines = [f"Task {work.display_id}: {work.title.strip()}"]
    if work.brief.strip():
        lines += ["", "Brief:", work.brief.strip()]
    if work.summary.strip():
        lines += ["", "What the agent reported when it finished:", work.summary.strip()]
    if work.files:
        lines += ["", "Files changed:"]
        for change in work.files:
            lines.append(f"- {_file_line(change)}")
    patch = work.patch
    if patch:
        clipped = len(patch) > PATCH_LIMIT
        if clipped:
            patch = patch[:PATCH_LIMIT]
        lines += ["", "Diff" + (" (cut short; the rest is more of the same):" if clipped else ":")]
        lines += ["```diff", patch.rstrip(), "```"]
    lines += ["", "Write the commit description."]
    return "\n".join(lines)


def _file_line(change: dict[str, Any]) -> str:
    kind = change.get("kind")
    counts = ""
    if change.get("insertions") is not None or change.get("deletions") is not None:
        counts = f" (+{change.get('insertions') or 0} −{change.get('deletions') or 0})"
    label = f" [{kind}]" if kind and kind != "unknown" else ""
    return f"{change.get('path', '')}{label}{counts}"


def describe_options(model: str, cwd: Path) -> Any:
    """The ``ClaudeAgentOptions`` for one description — apart from the call, so the suite
    can check what is sent without sending it.

    ``tools=[]`` and ``max_turns=1`` are the whole point: the model is handed everything it
    may read in the prompt, so a session that could open files would only spend turns
    re-reading what the diff already says. ``setting_sources=[]`` keeps the project's
    ``CLAUDE.md`` out of the context for the same reason — it is written for an agent
    doing the work, not for one describing it.

    **Never ``permission_mode="plan"``**, which looks like the natural "read only" mode and
    is not one here: in plan mode the CLI answers the turn on Sonnet whatever ``model``
    says — measured on CLI 2.1.280, ``haiku`` asked and ``claude-sonnet-5`` billed. With no
    tools there is nothing for a permission mode to guard, so the default is left alone.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    from voxarium_studio.teams.backends.claude_code import _option_fields

    # An environment built before the `teams` floor was raised may lack either field, and a
    # draft that runs slower beats one that fails on a TypeError.
    extra: dict[str, Any] = {}
    if "effort" in _option_fields():
        extra["effort"] = DESCRIBE_EFFORT
    if "thinking" in _option_fields():
        extra["thinking"] = {"type": "disabled"}

    return ClaudeAgentOptions(  # type: ignore[arg-type]
        model=model or None,
        max_turns=1,
        tools=[],
        allowed_tools=[],
        system_prompt=SYSTEM_PROMPT,
        setting_sources=[],
        cwd=str(cwd),
        **extra,
    )


async def describe_with_claude(prompt: str, model: str, cwd: Path) -> str:
    """One turn of a cheap model with no tools, through the same SDK the sessions use —
    see :func:`describe_options` for what the turn is allowed."""
    try:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query
    except ImportError as exc:  # pragma: no cover - the `teams` extra is not installed
        raise RuntimeError(
            "claude-agent-sdk is not installed; install the Studio with the [teams] extra"
        ) from exc

    options = describe_options(model, cwd)

    async def run() -> str:
        parts: list[str] = []
        final: str | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        parts.append(block.text)
            elif isinstance(message, ResultMessage):
                if message.is_error:
                    raise RuntimeError(message.result or "The model returned an error")
                final = message.result
        text = (final or "\n".join(parts)).strip()
        if not text:
            raise RuntimeError("The model answered with no text")
        return text

    try:
        return await asyncio.wait_for(run(), DESCRIBE_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"No commit description within {DESCRIBE_TIMEOUT:.0f}s") from exc


async def describe_with_backend(prompt: str, model: str, cwd: Path, backend_name: str) -> str:
    """Use the project's description backend (§10.4), without a task session.

    Claude retains its single tool-free SDK turn. Other runtimes receive all context in
    the prompt, an empty temporary working directory, no Studio MCP servers and read-only
    permissions. Their normal stream handles process cleanup on timeout/cancellation.
    """
    if backend_name == "claude_code":
        return await describe_with_claude(prompt, model, cwd)

    from voxarium_studio.models.teams import TeamMember
    from voxarium_studio.teams.backends import AgentRunRequest, get_backend

    backend = get_backend(backend_name)
    available, reason = backend.is_available()
    if not available:
        raise RuntimeError(reason or f"Backend {backend_name} is unavailable")

    async def run(workdir: Path) -> str:
        member = TeamMember(
            team_id="commit-description", name="Commit description",
            agent_backend=backend_name, model=model, allowed_tools=[],
            permission_mode="plan", max_turns=1,
        )
        request = AgentRunRequest(
            member=member, task=None, workdir=workdir, prompt=prompt,
            system_prompt=SYSTEM_PROMPT, model=model, effort=DESCRIBE_EFFORT,
            session_key=f"commit-description-{uuid.uuid4().hex}",
        )
        parts: list[str] = []
        final = ""
        stream = backend.run(request)
        try:
            async for event in stream:
                if event.kind == "error" or event.data.get("is_error"):
                    raise RuntimeError(event.text or "The model returned an error")
                if event.kind == "text" and event.text:
                    parts.append(event.text)
                elif event.kind == "result":
                    final = event.text
        finally:
            await stream.aclose()
        text = (final or "\n".join(parts)).strip()
        if not text:
            raise RuntimeError("The model answered with no text")
        return text

    with tempfile.TemporaryDirectory(prefix="voxarium_studio-commit-") as directory:
        try:
            return await asyncio.wait_for(run(Path(directory)), DESCRIBE_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"No commit description within {DESCRIBE_TIMEOUT:.0f}s") from exc


__all__ = [
    "describe_with_backend",
    "DESCRIBE_EFFORT",
    "DESCRIBE_TIMEOUT",
    "PATCH_LIMIT",
    "CommitWork",
    "Describer",
    "build_prompt",
    "commit_title",
    "describe_options",
    "describe_with_claude",
]
