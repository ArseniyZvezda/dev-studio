"""OpenAI Codex CLI backend (§10.6).

The second ``AgentBackend``, and the one that proves the seam: a team member whose
``agent_backend`` is ``codex`` runs each task or chat turn as one ``codex exec --json``
subprocess, read as JSONL. Nothing of the board, the hierarchy or the UI knows the
difference — which is what §10.6 promised and what this module exists to check.

What the request carries that Codex can take, and what it cannot:

- **The prompt and the system prompt.** The prompt goes in on stdin (``-`` as the prompt
  argument), so a long brief never meets the command-line ceiling. The system prompt is the
  Studio's *append* — the role, the task id, the working directory — and travels as
  ``developer_instructions``, Codex's own key for text layered over its stock instructions,
  which is the same arrangement the Claude backend has with ``append``. When the command
  line would not fit (Windows stops at 32 K characters), the append is prepended to the
  stdin prompt instead, under a heading that says what it is.
- **The model and the effort.** ``-m`` verbatim, unless the member still carries a Claude
  model name (``sonnet`` is the roster default), in which case Codex's own default from
  ``~/.codex/config.toml`` runs and the transcript says so. Effort levels share a vocabulary
  (``low`` … ``max``), so ``model_reasoning_effort`` takes the Studio's value as it is.
- **The permission mode**, mapped onto Codex's sandbox: ``plan`` is ``read-only``,
  ``bypassPermissions`` bypasses the sandbox outright, and everything else is
  ``workspace-write``. Approval is ``never`` always — ``codex exec`` is non-interactive and
  there is nobody to answer a prompt, so a command the sandbox refuses fails rather than
  waits (the same rule ``dontAsk`` has on the Claude side).
- **Writable roots** are passed explicitly for ``workspace-write``, including when a turn
  resumes. The project (or the isolated checkout) may be below or outside ``workdir``;
  Windows ACL inheritance is not guaranteed to carry the working directory's grant to it.
- **MCP servers** (§10.7) become ``mcp_servers.<name>.*`` config overrides. The Studio's
  SDK server is exposed through a per-run authenticated stdio/loopback bridge; its handlers
  keep running in the Studio with the same identity, routing and delegation limits.
  ``plugin_dirs``, ``agent_name`` and the tool allow-list are Claude Code's and ignored here.
- **Pictures** ride ``-i`` on both the first run and a resume.

What it never does: continue a stalled session (Codex has no turn cap), hold a run open for
delegated work (none), report a usage window (``exec --json`` has no such frame), or offer
Remote Control. A failed turn surfaces as an ``error`` event with Codex's own wording, so a
usage limit it names is still recognised downstream (``teams/limits.py``).

The event vocabulary was read off the installed CLI (0.164.0) rather than guessed:
``thread.started`` / ``turn.started`` / ``turn.completed`` / ``turn.failed`` / ``error`` and
``item.started`` / ``item.updated`` / ``item.completed`` carrying ``agent_message``,
``reasoning``, ``command_execution``, ``file_change``, ``mcp_tool_call``, ``web_search`` and
``todo_list`` items.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import tomllib
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from .base import AgentBackend, AgentEvent, AgentRunRequest, register_backend
from .cli_stream import _Oversized, _drain, _frames
from .codex_costs import estimate_cost
from .mcp_bridge import bridge_servers
from .recovery import recovery_prompt
from ...core.processes import kill_process_tree

log = logging.getLogger(__name__)

#: A model name that belongs to the Claude backend. The roster default is ``sonnet``, so a
#: member switched to Codex without a new model would otherwise send ``-m sonnet`` and fail
#: on the first turn; leaving the flag out runs Codex's configured default instead.
_CLAUDE_MODEL = re.compile(r"^(claude-|opus|sonnet|haiku|fable|opusplan)", re.I)

#: Where the whole command line stops fitting. Windows' ``CreateProcess`` ceiling is 32 767
#: characters; the margin covers the launcher and the quoting Python adds around arguments.
_MAX_COMMAND_LINE = 30_000

# §10.6: replace Codex's effort-dependent delegation rule for Studio task and chat turns.
_SUBAGENT_HINT = (
    "At any reasoning effort, use sub-agents on your own initiative when independent work "
    "would benefit from delegation. For a Studio board task, use the Studio spawn_subagent "
    "tool and respect the task's delegation limits. Follow user and role instructions."
)

#: How much of stderr is kept for the failure message — the tail, where the reason is.
_STDERR_TAIL = 4_000

#: Give a previous writer time to release the session, without retrying forever (§10.6).
_WRITER_RETRY_DELAYS = (1.0, 3.0, 6.0)

# exec's stdout only reports accounting at turn.completed. Its session rollout carries
# token_count after each model response, including the current context window (§10.6).
_USAGE_POLL_SECONDS = 0.25
_ROLLOUT_LINE_BYTES = 1024 * 1024

#: Which sandbox each of the Claude SDK's permission modes becomes. ``plan`` reads only;
#: ``bypassPermissions`` is the one that drops the sandbox, which the flag spells out.
_SANDBOX_FOR_MODE = {
    "plan": "read-only",
    "default": "workspace-write",
    "acceptEdits": "workspace-write",
    "dontAsk": "workspace-write",
    "auto": "workspace-write",
    "bypassPermissions": "danger-full-access",
}

#: The Studio's transcript names a file edit by the Claude tools it knows
#: (``WRITING_TOOLS`` in ``teams/changes.py``); a Codex ``file_change`` is translated to the
#: nearest one so the changes column and the Compact fold treat both backends alike.
_FILE_CHANGE_TOOL = {"add": "Write", "update": "Edit", "delete": "Delete"}

#: ``turn.completed`` usage as the transcript's own key names, which are the Claude SDK's:
#: the task header sums what the frontend knows how to read, and a second spelling of a
#: cache read would be a second code path for one number.
_USAGE_KEYS = {
    "input_tokens": "input_tokens",
    "cached_input_tokens": "cache_read_input_tokens",
    "output_tokens": "output_tokens",
}


def codex_home() -> Path:
    """Codex's own state directory — ``$CODEX_HOME``, else ``~/.codex``."""
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def _launcher() -> list[str] | None:
    """The command that runs Codex, or ``None`` when it is not installed.

    PATH takes precedence. A Windows desktop process can retain the PATH from before the
    native PowerShell installation, so also check its per-user install directory (§10.6).

    For npm installs, ``shutil.which`` can answer with the shim (``codex.cmd``), a batch file that
    starts ``node`` to start the real binary. The native executable shipped beside it is
    looked for first: a batch file through ``CreateProcess`` re-parses every argument through
    ``cmd.exe``, and the TOML overrides this backend passes are full of the quotes that
    mangles. The shim is the fallback, not the choice.
    """
    found = shutil.which("codex")
    if found is None and sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            native = Path(local_app_data) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
            if native.is_file():
                found = str(native)
    if found is None:
        return None
    path = Path(found)
    if path.suffix.lower() in (".cmd", ".bat"):
        native = _native_beside_shim(path)
        if native is not None:
            return [str(native)]
    return [str(path)]


def _native_beside_shim(shim: Path) -> Path | None:
    package = shim.parent / "node_modules" / "@openai" / "codex"
    if not package.is_dir():
        return None
    for platform_dir in sorted((package / "node_modules" / "@openai").glob("codex-*")):
        for candidate in sorted(platform_dir.glob("vendor/*/bin/codex.exe")):
            return candidate
    return None


def _has_credentials() -> bool:
    """A login (``auth.json`` under the Codex home) or an API key in the environment."""
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
        return True
    return (codex_home() / "auth.json").is_file()


def suggested_models() -> list[dict[str, str]]:
    """Models the installed Codex lists, for the member editor's picker.

    Read from the catalogue the CLI caches on this machine rather than hard-coded: the
    names move with OpenAI's releases, and a stale list would offer models that no longer
    exist beside the ones that do. Only what Codex itself would list (``visibility: list``).
    An absent or unreadable cache gives an empty list, and the editor's "Something else…"
    row still takes any id typed in.
    """
    path = codex_home() / "models_cache.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, list):
        return []
    listed = [
        entry
        for entry in models
        if isinstance(entry, dict)
        and entry.get("visibility") == "list"
        and isinstance(entry.get("slug"), str)
    ]
    listed.sort(key=lambda entry: (entry.get("priority") or 0, entry["slug"]))
    return [
        {
            "id": entry["slug"],
            "label": str(entry.get("display_name") or entry["slug"]),
            "group": "Codex",
            "note": str(entry.get("description") or ""),
            "backend": "codex",
        }
        for entry in listed
    ]


class CodexBackend(AgentBackend):
    name = "codex"
    session_recovery = True

    #: Codex has no equivalent of ``claude --agent``, so the orchestrator must keep the
    #: member's identity in the system prompt rather than in ``.claude/agents/`` (§10.3).
    named_agents = False

    def is_available(self) -> tuple[bool, str]:
        if _launcher() is None:
            return False, (
                "The `codex` CLI was not found. Install Codex CLI with the native installer "
                "or `npm install -g @openai/codex`, then sign in once with `codex login`. "
                "If it is already installed in a custom location, add it to PATH and restart "
                "the Studio."
            )
        if not _has_credentials():
            return False, (
                "No Codex credentials found. Run `codex login` once, or set OPENAI_API_KEY "
                "in the Studio's environment. Never commit the key into .voxarium_studio/config.yaml."
            )
        return True, "Ready"

    async def run(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]:
        available, reason = self.is_available()
        if not available:
            yield AgentEvent(kind="error", text=reason, is_final=True)
            return
        started_at = time.monotonic()
        try:
            async with bridge_servers(request.mcp_servers, request.max_message_bytes) as servers:
                current = replace(request, mcp_servers=servers)
                writer_retries = 0
                recovered = False
                while True:
                    pending: list[AgentEvent] = []
                    retryable = bool(current.resume_session_id)
                    async for event in self._run_once(current):
                        # A failed resume can be recovered only before the turn does work.
                        # Hold its errors until the process exits so the orchestrator does
                        # not mark a successfully recovered task as blocked (§10.6).
                        if event.data.get("subtype") in {"turn.started", "turn.completed"} or event.kind in {
                            "text", "thinking", "tool_use", "tool_result"
                        }:
                            retryable = False
                            for previous in pending:
                                yield previous
                            pending.clear()
                        if retryable and event.kind in {"error", "result"}:
                            pending.append(event)
                        else:
                            if (writer_retries or recovered) and event.kind == "result":
                                event.data["duration_ms"] = round(
                                    (time.monotonic() - started_at) * 1000
                                )
                            yield event
                    missing = any(
                        "no rollout found for thread id " + str(current.resume_session_id)
                        in event.text
                        for event in pending
                        if event.kind == "error"
                    )
                    busy = retryable and any(
                        f"thread {current.resume_session_id} already has an active writer" in event.text
                        for event in pending
                        if event.kind == "error"
                    )
                    if busy and writer_retries < len(_WRITER_RETRY_DELAYS):
                        delay = _WRITER_RETRY_DELAYS[writer_retries]
                        writer_retries += 1
                        yield AgentEvent(
                            kind="system",
                            text="The Codex session is still in use. "
                            f"Retrying the same session in {delay:g} seconds "
                            f"({writer_retries}/{len(_WRITER_RETRY_DELAYS)}).",
                            data={
                                "subtype": "session_waiting",
                                "retry": writer_retries,
                                "delay_seconds": delay,
                            },
                            session_id=current.resume_session_id,
                        )
                        # _run_once has exited and reaped its process. Never fork the
                        # work or remove another writer's lock to bypass this conflict.
                        await asyncio.sleep(delay)
                        continue
                    if not recovered and retryable and missing and not busy:
                        recovered = True
                        yield AgentEvent(
                            kind="system",
                            text="The saved Codex session is unavailable. Starting a new session "
                            "with the saved task/chat context.",
                            data={
                                "subtype": "session_recovered",
                                "previous_session_id": current.resume_session_id,
                            },
                        )
                        current = replace(
                            current,
                            resume_session_id=None,
                            prompt=request.recovery_prompt
                            or recovery_prompt(request.prompt, [], request.task),
                        )
                        continue
                    if busy:
                        detail = (
                            f"Codex session {current.resume_session_id} is still in use by another "
                            f"process after {writer_retries} retries. Wait for that run to finish "
                            "or stop it in the application that started it, then retry here. "
                            "The saved session has been preserved; no new session was started."
                        )
                        for event in pending:
                            if event.kind == "error":
                                event.text = f"{detail}\n\n{event.text}"
                                event.data["subtype"] = "session_busy"
                            elif event.kind == "result":
                                # Chat stores the final result as its error too (§10.6).
                                event.text = detail
                    for event in pending:
                        if (writer_retries or recovered) and event.kind == "result":
                            event.data["duration_ms"] = round((time.monotonic() - started_at) * 1000)
                        yield event
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Codex setup failed for member %s", request.member.id)
            yield AgentEvent(kind="error", text=f"{type(exc).__name__}: {exc}", is_final=True)

    async def _run_once(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]:
        launcher = _launcher() or []

        session_id: str | None = request.resume_session_id
        notes, argv, stdin_text = _build_command(launcher, request)
        for note in notes:
            yield AgentEvent(kind="system", text=note.text, data=note.data, session_id=session_id)

        started_at = time.monotonic()
        process: asyncio.subprocess.Process | None = None
        stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)
        stderr_task: asyncio.Task[None] | None = None
        frame_task: asyncio.Task[Any] | None = None
        usage = _RolloutUsage(codex_home())
        try:
            if session_id:
                # Snapshot before the child can append: a resumed session's cumulative
                # total includes previous Studio runs and must not be billed again.
                await asyncio.to_thread(usage.resume, session_id)
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(request.workdir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=max(request.max_message_bytes, 64 * 1024),
                env=_child_environment(),
                start_new_session=os.name != "nt",
            )
            assert process.stdin and process.stdout and process.stderr
            stderr_task = asyncio.create_task(_drain(process.stderr, stderr_tail))
            process.stdin.write(stdin_text.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            state = _RunState(model=_cost_model(request), resumed=bool(request.resume_session_id))
            usage_active = False
            frames = _frames(process.stdout, request.max_message_bytes)
            frame_task = asyncio.create_task(anext(frames, None))
            while frame_task is not None:
                await asyncio.wait({frame_task}, timeout=_USAGE_POLL_SECONDS)
                if session_id and usage_active:
                    snapshot = await asyncio.to_thread(usage.poll, session_id)
                    state.set_usage_baseline(usage.accounting_baseline)
                    if snapshot is not None:
                        state.set_live_usage(*snapshot)
                    update = state.usage_event()
                    if update is not None:
                        update.session_id = session_id
                        yield update
                if not frame_task.done():
                    continue
                frame = frame_task.result()
                frame_task = None
                if frame is None:
                    break
                frame_task = asyncio.create_task(anext(frames, None))
                if isinstance(frame, _Oversized):
                    yield AgentEvent(
                        kind="system",
                        text=(
                            f"Skipped one {frame.size}-byte line from codex: larger than "
                            f"the {request.max_message_bytes} bytes allowed by "
                            "teams.max_message_bytes."
                        ),
                        data={"subtype": "oversized_line", "bytes": frame.size},
                        session_id=session_id,
                    )
                    continue
                if frame.get("type") in {
                    "turn.started", "turn.completed", "item.started", "item.updated", "item.completed"
                }:
                    # A failed resume may still have an external writer appending to
                    # this rollout. Do not charge its work while our child is rejected.
                    usage_active = True
                for event in state.events(frame):
                    if event.kind == "system" and event.data.get("thread_id"):
                        session_id = str(event.data["thread_id"])
                    event.session_id = session_id
                    yield event
                update = state.usage_event()
                if update is not None:
                    update.session_id = session_id
                    yield update

            exit_code = await process.wait()
            if session_id and usage_active:
                # The final accounting can be flushed after the last stdout frame.
                snapshot = await asyncio.to_thread(usage.poll, session_id, force=True)
                state.set_usage_baseline(usage.accounting_baseline)
                if snapshot is not None:
                    state.set_live_usage(*snapshot)
                update = state.usage_event()
                if update is not None:
                    update.session_id = session_id
                    yield update
            if stderr_task is not None:
                await stderr_task
            if state.reconnecting and not state.failed:
                # A retry notice is not a failure while the CLI is alive (§10.6),
                # but an interrupted stream without turn.completed is not success.
                state.failed = True
                state.error_text = f"Codex ended before reconnecting completed: {state.reconnecting}"
                yield AgentEvent(kind="error", text=state.error_text, session_id=session_id)
            failed = exit_code != 0 or state.failed
            if exit_code != 0 and not state.failed:
                # Codex said nothing structured about why: its stderr is the only account.
                tail = "".join(stderr_tail).strip()
                yield AgentEvent(
                    kind="error",
                    text=f"codex exited with code {exit_code}" + (f": {tail}" if tail else ""),
                    data={"exit_code": exit_code},
                    session_id=session_id,
                )
            yield AgentEvent(
                kind="result",
                text=state.error_text if failed else state.last_message,
                data={
                    **state.result_data(exit_code=exit_code, is_error=failed),
                    "duration_ms": round((time.monotonic() - started_at) * 1000),
                },
                session_id=session_id,
                is_final=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a CLI failure belongs on the task, never on the server
            log.exception("Codex run failed for member %s", request.member.id)
            yield AgentEvent(
                kind="error",
                text=f"{type(exc).__name__}: {exc}",
                session_id=session_id,
                is_final=True,
            )
        finally:
            if frame_task is not None:
                frame_task.cancel()
                await asyncio.gather(frame_task, return_exceptions=True)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            if process is not None and process.returncode is None:
                # Cancellation, or a failure before the stream ended: the subprocess must
                # not outlive the run that owns it.
                await kill_process_tree(process)
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)


# ------------------------------------------------------------------ the command


class _Note:
    """Something the transcript should say about how this run was set up."""

    __slots__ = ("data", "text")

    def __init__(self, text: str, **data: Any) -> None:
        self.text = text
        self.data = data


def _build_command(
    launcher: list[str], request: AgentRunRequest
) -> tuple[list[_Note], list[str], str]:
    """The argv and the stdin text for one run, with the notes the transcript gets.

    Shared settings for the first run and a resume, with each subcommand's own flags:
    ``exec resume`` accepts fewer options than ``exec`` (notably no ``--color``).
    """
    notes: list[_Note] = []
    argv = [*launcher, "exec"]
    if request.resume_session_id:
        argv += ["resume"]
    argv += ["--json", "--skip-git-repo-check"]
    if not request.resume_session_id:
        argv += ["--color", "never"]

    model = (request.model or "").strip()
    if model and _CLAUDE_MODEL.match(model):
        notes.append(
            _Note(
                f"The model {model} belongs to Claude Code; this run uses Codex's default "
                "model from its own config. Pick a Codex model on the member to change it.",
                subtype="model_fallback",
                model=model,
            )
        )
    elif model:
        argv += ["-m", model]

    effort = (request.effort or "").strip()
    if effort:
        argv += ["-c", f"model_reasoning_effort={_toml_string(effort)}"]

    if request.proactive_subagents:
        argv += ["-c", "features.multi_agent=true"]
        argv += ["-c", f"features.multi_agent_v2.multi_agent_mode_hint_text={_toml_string(_SUBAGENT_HINT)}"]

    argv += ["-c", 'approval_policy="never"']
    sandbox = _SANDBOX_FOR_MODE.get(request.permission_mode, "workspace-write")
    if sandbox == "danger-full-access":
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        argv += ["-c", f"sandbox_mode={_toml_string(sandbox)}"]
        if sandbox == "workspace-write" and request.writable_roots:
            # §10.6: cwd may be above the project. On Windows an explicit root also
            # lets Codex provision an ACL when the project does not inherit cwd's ACL.
            roots = [str(root.resolve()) for root in request.writable_roots]
            argv += ["-c", f"sandbox_workspace_write.writable_roots={_toml_array(roots)}"]

    skipped: list[str] = []
    for name, server in request.mcp_servers.items():
        overrides = _mcp_overrides(name, server)
        if overrides is None:
            skipped.append(name)
            continue
        for key, value in overrides:
            argv += ["-c", f"{key}={value}"]
        if sandbox == "danger-full-access":
            # §10.6: bypassing the filesystem sandbox does not approve MCP writes.
            # With approval_policy=never, an unapproved tool fails instead of asking.
            prefix = f"mcp_servers.{_toml_key(name)}"
            argv += ["-c", f'{prefix}.default_tools_approval_mode="approve"']
    if skipped:
        notes.append(
            _Note(
                f"Unsupported MCP server configuration: {', '.join(skipped)}.",
                subtype="studio_tools_unavailable",
                servers=skipped,
            )
        )

    for image in request.images:
        path = image.get("path") if isinstance(image, dict) else None
        if isinstance(path, str) and path:
            argv += ["-i", path]

    system_prompt = request.system_prompt.strip()
    stdin_text = request.prompt
    if system_prompt:
        with_instructions = [*argv, "-c", f"developer_instructions={_toml_string(system_prompt)}"]
        if _command_length(with_instructions) <= _MAX_COMMAND_LINE:
            argv = with_instructions
        else:
            # Too long for the command line, so it rides the prompt. Said in the transcript
            # because the model then sees it as part of the user's message, not as
            # instructions layered over its own.
            stdin_text = f"<instructions>\n{system_prompt}\n</instructions>\n\n{request.prompt}"
            notes.append(
                _Note(
                    "The system prompt was sent inside the message: it is too long for the "
                    "command line Codex is given its instructions on.",
                    subtype="instructions_in_prompt",
                    bytes=len(system_prompt),
                )
            )
    # Positionals last: the thread to resume, then `-` for "the prompt is on stdin".
    if request.resume_session_id:
        argv += [request.resume_session_id]
    argv += ["-"]
    return notes, argv, stdin_text


def _mcp_overrides(name: str, server: Any) -> list[tuple[str, str]] | None:
    """``mcp_servers.<name>.*`` overrides for one project server, or ``None`` to skip it.

    A project entry is a plain dict in the ``.mcp.json`` shape (``core/mcp.py``); the
    Studio's own toolset is an Agent SDK server object and has no process to start.
    """
    if not isinstance(server, dict):
        return None
    prefix = f"mcp_servers.{_toml_key(name)}"
    pairs: list[tuple[str, str]] = []
    if isinstance(server.get("command"), str) and server["command"]:
        pairs.append((f"{prefix}.command", _toml_string(server["command"])))
        args = server.get("args")
        if isinstance(args, list) and args:
            pairs.append((f"{prefix}.args", _toml_array([str(arg) for arg in args])))
        env = server.get("env")
        if isinstance(env, dict) and env:
            pairs.append((f"{prefix}.env", _toml_table({str(k): str(v) for k, v in env.items()})))
        return pairs
    if isinstance(server.get("url"), str) and server["url"]:
        pairs.append((f"{prefix}.url", _toml_string(server["url"])))
        headers = server.get("headers")
        if isinstance(headers, dict) and headers:
            pairs.append(
                (f"{prefix}.http_headers", _toml_table({str(k): str(v) for k, v in headers.items()}))
            )
        return pairs
    return None


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(name: str) -> str:
    return name if _BARE_KEY.match(name) else _toml_string(name)


def _toml_string(text: str) -> str:
    """A TOML basic string. JSON's escapes are a subset of TOML's, so ``json.dumps`` is it —
    with non-ASCII left raw, because TOML has no surrogate pairs and JSON would write them
    for anything outside the BMP."""
    return json.dumps(text, ensure_ascii=False)


def _toml_array(items: list[str]) -> str:
    return "[" + ", ".join(_toml_string(item) for item in items) + "]"


def _toml_table(items: dict[str, str]) -> str:
    return "{" + ", ".join(f"{_toml_key(k)} = {_toml_string(v)}" for k, v in items.items()) + "}"


def _command_length(argv: list[str]) -> int:
    # Every argument quoted, which is the worst case the launcher has to fit.
    return sum(len(arg) + 3 for arg in argv)


def _child_environment() -> dict[str, str]:
    """The Studio's environment, minus the marker a nested Claude Code session leaves.

    Harmless to Codex, but a Studio started from a Claude Code terminal passes it to every
    child, and the project's own MCP servers started by Codex may be Claude-aware tools
    that refuse to run under it (the graphify plugin met exactly that).
    """
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


# ------------------------------------------------------------------ the stream


def _cost_model(request: AgentRunRequest) -> str | None:
    """The explicit model, or the user-configured default when the Claude name is skipped.

    No guessed CLI default: if config cannot establish it, the estimate is unavailable.
    """
    model = (request.model or "").strip()
    if model and not _CLAUDE_MODEL.match(model):
        return model
    try:
        config = tomllib.loads((codex_home() / "config.toml").read_text(encoding="utf-8"))
        profiles = config.get("profiles", {})
        profile = profiles.get(config.get("profile"), {}) if isinstance(profiles, dict) else {}
        configured = profile.get("model", config.get("model"))
        return configured if isinstance(configured, str) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _normalise_usage(usage: dict[str, Any]) -> dict[str, int]:
    counted = {_USAGE_KEYS[key]: _count(usage[key]) for key in _USAGE_KEYS if key in usage}
    if "input_tokens" in counted:
        # Codex's input already includes its cache; output already includes reasoning.
        counted["input_tokens"] = max(
            0, counted["input_tokens"] - counted.get("cache_read_input_tokens", 0)
        )
    return counted


class _RolloutUsage:
    """A bounded, read-only tail of this run's Codex rollout (§10.6).

    stdout deliberately omits token_count. Read just the selected thread's rollout;
    never infer context from a turn's cumulative usage or from a model-name table.
    New threads can fall back to stdout. A resume without a readable baseline keeps
    accounting unknown rather than charging the old session's cumulative usage.
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.session_id: str | None = None
        self.path: Path | None = None
        self.offset = 0
        self.discarding = False
        self.next_lookup = 0.0
        self.next_poll = 0.0
        self.baseline: dict[str, int] | None = {}
        self.waiting_for_start = False
        self.unavailable_resume = False
        self.total: dict[str, int] = {}
        self.context_tokens: int | None = None
        self.context_window: int | None = None

    def resume(self, session_id: str) -> None:
        self.session_id = session_id
        self.baseline = None
        self.unavailable_resume = self.poll(session_id, force=True) is None
        self.waiting_for_start = True

    @property
    def accounting_baseline(self) -> dict[str, int] | None:
        return None if self.waiting_for_start or self.unavailable_resume else self.baseline

    def poll(
        self, session_id: str, *, force: bool = False
    ) -> tuple[dict[str, int], int | None, int | None] | None:
        if session_id != self.session_id:
            self.session_id = session_id
            self.path = None
            self.offset = 0
            self.discarding = False
            self.next_lookup = 0.0
            self.next_poll = 0.0
            self.baseline = {}
            self.waiting_for_start = False
            self.unavailable_resume = False
            self.total = {}
            self.context_tokens = self.context_window = None
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            return None
        if not force and time.monotonic() < self.next_poll:
            return None
        self.next_poll = time.monotonic() + _USAGE_POLL_SECONDS
        try:
            if self.path is None:
                if not force and time.monotonic() < self.next_lookup:
                    return None
                self.next_lookup = time.monotonic() + 1.0
                for folder in ("sessions", "archived_sessions"):
                    self.path = next(
                        (self.home / folder).glob(f"**/rollout-*-{session_id}.jsonl"), None
                    )
                    if self.path is not None:
                        break
                if self.path is None:
                    return None
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                while True:
                    start = stream.tell()
                    raw = stream.readline(_ROLLOUT_LINE_BYTES + 1)
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        if len(raw) <= _ROLLOUT_LINE_BYTES:
                            # Writers can append a JSON record in several writes.
                            self.offset = start
                            break
                        self.discarding = True
                        self.offset = stream.tell()
                        continue
                    self.offset = stream.tell()
                    if self.discarding or len(raw) > _ROLLOUT_LINE_BYTES:
                        self.discarding = False
                        continue
                    # Avoid decoding transcript content that cannot hold accounting.
                    if b'"token_count"' not in raw and b'"task_started"' not in raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(record, dict) or record.get("type") != "event_msg":
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") == "task_started":
                        if self.waiting_for_start and not self.unavailable_resume:
                            # An old writer can finish between our pre-spawn snapshot
                            # and the successful resume. Its last token_count belongs
                            # to the baseline, before our new task_started boundary.
                            self.baseline = dict(self.total)
                            self.waiting_for_start = False
                        window = _count(payload.get("model_context_window"))
                        if window:
                            self.context_window = window
                    if payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    total = info.get("total_token_usage")
                    if isinstance(total, dict) and "input_tokens" in total:
                        self.total = {
                            key: _count(total.get(key)) for key in _USAGE_KEYS
                        }
                    last = info.get("last_token_usage")
                    if isinstance(last, dict) and "input_tokens" in last:
                        self.context_tokens = _count(last["input_tokens"])
                    window = _count(info.get("model_context_window"))
                    if window:
                        self.context_window = window
        except OSError:
            return None
        if self.waiting_for_start or self.unavailable_resume:
            return None
        if self.baseline is None:
            # If a resumed rollout was initially unavailable, prefer an incomplete
            # live count to charging historical usage; stdout supplies the final total.
            self.baseline = dict(self.total)
        delta = {
            key: max(0, value - self.baseline.get(key, 0))
            for key, value in self.total.items()
        }
        return _normalise_usage(delta), self.context_tokens, self.context_window


class _RunState:
    """What one ``codex exec`` has said so far, folded into the Studio's events."""

    def __init__(self, *, model: str | None = None, resumed: bool = False) -> None:
        self.model = model
        self.last_message = ""
        self.failed = False
        self.turns = 0
        self.usage: dict[str, int] = {}
        self._usage_baseline: dict[str, int] | None = None if resumed else {}
        self._stdout_total: dict[str, int] = {}
        self.live_usage: dict[str, int] = {}
        self.context_tokens: int | None = None
        self.context_window: int | None = None
        self._last_usage_event: dict[str, Any] | None = None
        self.error_text = ""
        self.reconnecting = ""
        self._started: set[str] = set()

    def events(self, frame: dict[str, Any]) -> list[AgentEvent]:
        kind = str(frame.get("type") or "")
        if kind == "thread.started":
            thread_id = frame.get("thread_id")
            return [
                AgentEvent(
                    kind="system",
                    text="thread.started",
                    data={"subtype": "thread.started", "thread_id": thread_id},
                )
            ]
        if kind == "turn.started":
            return [AgentEvent(kind="system", text="turn.started", data={"subtype": kind})]
        if kind == "turn.completed":
            self.reconnecting = ""
            self.turns += 1
            usage = frame.get("usage") if isinstance(frame.get("usage"), dict) else {}
            previous = self.usage
            self._stdout_total = {key: _count(usage[key]) for key in _USAGE_KEYS if key in usage}
            self._update_stdout_usage()
            counted = {key: max(0, value - previous.get(key, 0)) for key, value in self.usage.items()}
            data: dict[str, Any] = {"subtype": kind}
            if counted:
                data["usage"] = counted
            return [AgentEvent(kind="system", text="turn.completed", data=data)]
        if kind == "turn.failed":
            self.failed = True
            message = _error_message(frame.get("error"))
            self.error_text = message
            return [AgentEvent(kind="error", text=message, data={"subtype": kind})]
        if kind == "error":
            message = _error_message(frame)
            # Codex emits StreamError retry progress as top-level `error` frames.
            # Keep the task/chat running; only turn.failed or process exit ends it
            # (§10.6). Other errors retain their existing terminal semantics.
            if re.match(r"^Reconnecting(?:\.{3}|…)\s+\d+/\d+\b", message):
                self.reconnecting = message
                return [AgentEvent(kind="system", text=message, data={"subtype": "reconnecting"})]
            self.failed = True
            self.error_text = message
            return [AgentEvent(kind="error", text=message, data={"subtype": kind})]
        if kind in ("item.started", "item.updated", "item.completed"):
            item = frame.get("item")
            if isinstance(item, dict):
                return self._item(kind.split(".", 1)[1], item)
            return []
        # A frame this build does not know: kept, hidden below Verbose like any system line.
        return [AgentEvent(kind="system", text=kind or "frame", data=_plain(frame))]

    def set_usage_baseline(self, baseline: dict[str, int] | None) -> None:
        self._usage_baseline = baseline
        self._update_stdout_usage()

    def _update_stdout_usage(self) -> None:
        # exec emits the thread's cumulative usage, even for `exec resume` (Codex
        # event_processor_with_jsonl_output::usage_from_last_total). Without a known
        # baseline a resumed stdout total cannot honestly be charged to this run.
        if self._usage_baseline is None or not self._stdout_total:
            return
        usage = _normalise_usage({
            key: max(0, value - self._usage_baseline.get(key, 0))
            for key, value in self._stdout_total.items()
        })
        if sum(usage.values()) >= sum(self.usage.values()):
            self.usage = usage

    def set_live_usage(
        self, usage: dict[str, int], context_tokens: int | None, context_window: int | None
    ) -> None:
        if sum(usage.values()) >= sum(self.live_usage.values()):
            self.live_usage = usage
        if context_tokens is not None:
            self.context_tokens = context_tokens
        if context_window is not None:
            self.context_window = context_window

    def effective_usage(self) -> dict[str, int]:
        # Both sources describe the same work. stdout is the fallback when rollout
        # accounting is unavailable or arrives only partway through a resumed run.
        return max((self.usage, self.live_usage), key=lambda usage: sum(usage.values()))

    def usage_event(self) -> AgentEvent | None:
        usage = self.effective_usage()
        if not usage and self.context_tokens is None and self.context_window is None:
            return None
        data: dict[str, Any] = {"subtype": "usage_update", "total_tokens": sum(usage.values()),
                                "total_cached_tokens": usage.get("cache_read_input_tokens", 0),
                                "total_input_tokens": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                                "total_output_tokens": usage.get("output_tokens", 0),
                                "total_uncached_tokens": sum(value for key, value in usage.items()
                                                             if key != "cache_read_input_tokens"),
                                "accounting_version": 2}
        if self.context_tokens is not None:
            data["context_tokens"] = self.context_tokens
        if self.context_window is not None:
            data["context_window"] = self.context_window
        if data == self._last_usage_event:
            return None
        self._last_usage_event = dict(data)
        return AgentEvent(kind="system", text="usage_update", data=data)

    def _item(self, phase: str, item: dict[str, Any]) -> list[AgentEvent]:
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        if item_type == "agent_message":
            if phase != "completed":
                return []
            text = str(item.get("text") or "")
            if text:
                self.last_message = text
                return [AgentEvent(kind="text", text=text)]
            return []
        if item_type == "reasoning":
            if phase != "completed":
                return []
            text = str(item.get("text") or "")
            return [AgentEvent(kind="thinking", text=text)] if text else []
        if item_type == "command_execution":
            events = self._call_once(item_id, "Bash", {"command": item.get("command")}, phase)
            if phase == "completed":
                exit_code = item.get("exit_code")
                failed = (isinstance(exit_code, int) and exit_code != 0) or item.get(
                    "status"
                ) == "failed"
                output = str(item.get("aggregated_output") or "")
                if exit_code is not None and not output:
                    output = f"(exit code {exit_code})"
                events.append(
                    AgentEvent(
                        kind="tool_result",
                        text=output[:2000],
                        data={"tool_use_id": item_id, "is_error": failed, "exit_code": exit_code},
                    )
                )
            return events
        if item_type == "mcp_tool_call":
            server = str(item.get("server") or "mcp")
            tool = str(item.get("tool") or "tool")
            name = f"mcp__{server}__{tool}"
            arguments = item.get("arguments")
            events = self._call_once(
                item_id, name, arguments if isinstance(arguments, dict) else {"arguments": arguments}, phase
            )
            if phase == "completed":
                error = item.get("error")
                failed = error is not None or item.get("status") == "failed"
                result = _error_message(error) if error is not None else _result_text(item.get("result"))
                events.append(
                    AgentEvent(
                        kind="tool_result",
                        text=result[:2000],
                        data={"tool_use_id": item_id, "is_error": failed},
                    )
                )
            return events
        if item_type == "web_search":
            return self._call_once(item_id, "WebSearch", {"query": item.get("query")}, phase)
        if item_type == "file_change":
            if phase != "completed":
                return []
            events: list[AgentEvent] = []
            changes = item.get("changes")
            for index, change in enumerate(changes if isinstance(changes, list) else []):
                if not isinstance(change, dict):
                    continue
                kind = str(change.get("kind") or "update")
                tool = _FILE_CHANGE_TOOL.get(kind, "Edit")
                data: dict[str, Any] = {
                    "id": f"{item_id}:{index}" if item_id else None,
                    "name": tool,
                    "input": {"file_path": str(change.get("path") or "")},
                }
                stat = _diff_stat(change.get("diff"))
                if stat is not None:
                    data["edit"] = stat
                events.append(AgentEvent(kind="tool_use", text=tool, data=data))
            return events
        if item_type == "todo_list":
            if phase == "started":
                return []
            return [
                AgentEvent(
                    kind="system",
                    text="todo_list",
                    data={"subtype": "todo_list", "items": _plain(item).get("items")},
                )
            ]
        return [
            AgentEvent(
                kind="system", text=item_type or "item", data={"subtype": item_type, **_plain(item)}
            )
        ]

    def _call_once(
        self, item_id: str, name: str, arguments: Any, phase: str
    ) -> list[AgentEvent]:
        """The ``tool_use`` for an item, on whichever frame shows it first.

        Codex reports a command on ``item.started`` and again on ``item.completed``; the
        transcript wants one call row, with the result under it.
        """
        if item_id and item_id in self._started:
            return []
        if item_id:
            self._started.add(item_id)
        return [
            AgentEvent(
                kind="tool_use",
                text=name,
                data={"id": item_id or None, "name": name, "input": _summarise(arguments)},
            )
        ]

    def result_data(self, *, exit_code: int, is_error: bool) -> dict[str, Any]:
        """The run's accounting in the shape the orchestrator stores (``_store_result_stats``).

        Codex reports tokens, not a bill. Keep the API-equivalent estimate explicitly
        labelled, including an unknown tariff, so the UI never calls missing data free.
        """
        usage = self.effective_usage()
        cost = estimate_cost(self.model, usage)
        return {
            "subtype": "error" if is_error else "success",
            "is_error": is_error,
            "num_turns": self.turns,
            "total_cost_usd": cost or 0.0,
            "cost_estimated": cost is not None,
            "cost_unavailable": cost is None,
            "model": self.model,
            "total_tokens": sum(usage.values()),
            "total_cached_tokens": usage.get("cache_read_input_tokens", 0),
            "total_uncached_tokens": sum(value for key, value in usage.items()
                                         if key != "cache_read_input_tokens"),
            "accounting_version": 2,
            "usage": dict(usage),
            "exit_code": exit_code,
            "stop_reason": None,
            "api_error_status": None,
            "errors": [self.error_text] if self.error_text else [],
        }


def _error_message(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("message", "error", "detail"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        return json.dumps(_plain(value))[:2000]
    return str(value or "codex reported an error without a message")


def _result_text(value: Any) -> str:
    """An MCP result as a line a person can read — the text of its content blocks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        return _result_text(value["content"])
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            else:
                parts.append(json.dumps(part)[:500])
        return "\n".join(parts)
    return json.dumps(value)[:2000]


def _diff_stat(diff: Any) -> dict[str, int] | None:
    """Added and removed lines of a unified diff, when the frame carried one."""
    if not isinstance(diff, str) or not diff:
        return None
    insertions = deletions = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            insertions += 1
        elif line.startswith("-"):
            deletions += 1
    return {"insertions": insertions, "deletions": deletions}


def _summarise(value: Any) -> dict[str, Any]:
    """Mirror of the Claude backend's ``_summarise_tool_input``: readable, bounded."""
    if not isinstance(value, dict):
        return {"value": str(value)[:500]}
    summary: dict[str, Any] = {}
    for key, item in value.items():
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        summary[str(key)] = text[:500] + ("…" if len(text) > 500 else "")
    return summary


def _plain(value: Any) -> dict[str, Any]:
    """A frame as JSON-safe data for the transcript, which stores ``data`` as JSON."""
    if not isinstance(value, dict):
        return {}
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return {str(k): str(v) for k, v in value.items()}


register_backend("codex", CodexBackend)
