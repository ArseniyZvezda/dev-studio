"""The three node execution kinds (§8.1/§8.2).

``python_script`` runs out of process on purpose: it isolates conflicting dependency
sets, lets stdout/stderr stream as live logs, makes cancellation a process kill, and
keeps heavy synchronous ML libraries off the FastAPI event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import string
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..plugins.api import BaseNode, NodeContext
from ..plugins.manifest import PluginManifest
from ..schemas.nodes import NodeTypeSchema

log = logging.getLogger(__name__)

LogFn = Callable[[str, str], Awaitable[None]]
ProgressFn = Callable[[dict[str, Any]], Awaitable[None]]
OUTPUT_SENTINEL = "::voxarium_studio:output::"
PROGRESS_SENTINEL = "::voxarium_studio:progress::"
"""A stdout line a node writes to say how far along it is. Advisory and unpersisted: it
paints the node on the canvas while the run streams and is forgotten when the node ends,
which is why it rides the event bus rather than the ``NodeRun`` row."""

FILE_MARKER = "__voxarium_studio_file__:"
STREAM_LIMIT = 1024 * 1024
"""Subprocess pipe buffer. The asyncio default is 64 KiB, which a node echoing a large
JSON result on stdout overruns."""

MAX_LINE_BYTES = 32 * 1024 * 1024
"""Cap on reassembling one oversized line, so a runaway node cannot exhaust memory."""
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class NodeExecutionError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class ExecutionRequest:
    node_id: str
    run_id: str
    schema: NodeTypeSchema
    inputs: dict[str, Any]
    params: dict[str, Any]
    project_root: Path
    assets_dir: Path
    work_dir: Path
    log: LogFn
    manifest: PluginManifest | None = None
    node_class: type[BaseNode] | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    on_process: Callable[[asyncio.subprocess.Process], None] | None = None
    progress: ProgressFn | None = None
    """Where a node's ``::voxarium_studio:progress::`` frames go. Optional because a caller that
    only wants the outputs — a test, a one-off execution — should not have to care."""

    def resolved(self) -> dict[str, Any]:
        """Params merged under edge-supplied inputs — a connection overrides a literal."""
        merged = dict(self.params)
        merged.update({k: v for k, v in self.inputs.items() if v is not None})
        return merged


@dataclass
class ExecutionResult:
    outputs: dict[str, Any]
    exit_code: int | None = None


def _expand_env(value: str) -> str:
    """Expand ``${VAR}`` from the environment so secrets stay out of committed YAML (§12)."""
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


async def execute(request: ExecutionRequest) -> ExecutionResult:
    kind = request.schema.execution.kind
    if kind == "python_function":
        return await _run_python_function(request)
    if kind == "python_script":
        return await _run_python_script(request)
    if kind == "http_api":
        return await _run_http_api(request)
    raise NodeExecutionError(f"Unsupported execution kind: {kind!r}")


# --------------------------------------------------------------------------- in-process


async def _run_python_function(request: ExecutionRequest) -> ExecutionResult:
    """Built-in and lightweight plugin nodes that are safe to run in the event loop."""
    node_class = request.node_class
    if node_class is None:
        raise NodeExecutionError(
            f"Node type {request.schema.id!r} declares kind='python_function' "
            "but no implementation class is registered"
        )
    context = NodeContext(
        inputs=request.inputs,
        params=request.params,
        project_root=request.project_root,
        assets_dir=request.assets_dir,
        work_dir=request.work_dir,
        settings=request.settings,
        log_fn=request.log,
        progress_fn=request.progress,
    )
    instance = node_class()
    timeout = request.schema.execution.timeout_seconds
    coro = instance.run(context)
    try:
        outputs = await (asyncio.wait_for(coro, timeout) if timeout else coro)
    except asyncio.TimeoutError as exc:
        raise NodeExecutionError(f"Node timed out after {timeout}s") from exc
    if outputs is None:
        outputs = {}
    if not isinstance(outputs, dict):
        raise NodeExecutionError(
            f"Node {request.schema.id!r} returned {type(outputs).__name__}, expected dict"
        )
    return ExecutionResult(outputs=outputs, exit_code=0)


# --------------------------------------------------------------------------- subprocess


def _parse_progress(line: str) -> dict[str, Any] | None:
    """One ``::voxarium_studio:progress::`` line as a dict, or None if it is not one.

    None means "log this instead": a node that meant to report progress and got the JSON
    wrong has a bug, and swallowing the line would hide it.
    """
    try:
        frame = json.loads(line.removeprefix(PROGRESS_SENTINEL).strip())
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


def _interpreter_for(request: ExecutionRequest) -> str:
    """Prefer the plugin's isolated venv; fall back to the Studio interpreter."""
    execution = request.schema.execution
    if execution.env and request.manifest is not None:
        python_path = request.manifest.venv_python()
        if python_path is not None:
            return str(python_path)
        log.warning(
            "Node %s requests env %r but plugin %s has no provisioned venv; "
            "falling back to the Studio interpreter",
            request.schema.id,
            execution.env,
            request.manifest.id,
        )
    return sys.executable


async def _pump(
    stream: asyncio.StreamReader,
    name: str,
    log_fn: LogFn,
    sink: list[str],
    progress: ProgressFn | None = None,
) -> None:
    """Forward one output stream line by line, keeping a tail for error reporting.

    Uses ``readuntil`` rather than ``readline``: a line longer than the reader's buffer
    makes ``readline`` **discard** the buffer and raise, which would silently truncate a
    node echoing a large result on stdout (the ``::voxarium_studio:output::`` fallback).
    ``readuntil`` leaves the bytes in place, so the long line can be reassembled.

    A well-formed ``::voxarium_studio:progress::`` frame is swallowed whether or not anyone is
    listening — a node should not have to know, and the alternative is the raw sentinel
    turning up in the log. A malformed one is logged instead of dropped, because that is
    a bug in the node and silence is the worst way to report it.
    """
    pending = bytearray()

    async def flush() -> None:
        if not pending:
            return
        line = pending.decode("utf-8", errors="replace").rstrip("\r\n")
        pending.clear()
        if line.startswith(OUTPUT_SENTINEL):
            sink.append(line.removeprefix(OUTPUT_SENTINEL).strip())
            return
        if line.startswith(PROGRESS_SENTINEL):
            frame = _parse_progress(line)
            if frame is not None:
                if progress is not None:
                    await progress(frame)
                return
        await log_fn(line, name)

    while True:
        try:
            pending += await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            pending += exc.partial
            break
        except asyncio.LimitOverrunError:
            # No newline within the buffer yet — drain what is there and keep looking.
            chunk = await stream.read(STREAM_LIMIT)
            if not chunk:
                break
            pending += chunk
            if len(pending) > MAX_LINE_BYTES:
                # A node producing an endless unbroken line must not exhaust memory.
                await flush()
            continue
        await flush()

    await flush()


async def _run_python_script(request: ExecutionRequest) -> ExecutionResult:
    """Run a node script in a subprocess.

    Contract (documented for plugin authors in docs/plugins.md):
      * the resolved ``{"inputs": ..., "params": ..., "context": ...}`` arrives on stdin
      * stdout/stderr stream to the UI as logs
      * results are written as JSON to ``$VOXARIUM_STUDIO_OUTPUT_FILE`` (or echoed on stdout
        prefixed with ``::voxarium_studio:output::``)
      * a stdout line prefixed ``::voxarium_studio:progress::`` carries a progress frame, which
        paints the node on the canvas instead of appearing in the log
    """
    execution = request.schema.execution
    plugin_root = request.manifest.root if request.manifest else request.project_root
    assert plugin_root is not None
    script_path = (plugin_root / execution.entry).resolve()
    if not script_path.exists():
        raise NodeExecutionError(f"Node script not found: {script_path}")

    request.work_dir.mkdir(parents=True, exist_ok=True)
    output_file = request.work_dir / f"{request.node_id}.output.json"
    with contextlib.suppress(OSError):
        output_file.unlink()

    env = dict(os.environ)
    env.update(
        {
            "VOXARIUM_STUDIO_OUTPUT_FILE": str(output_file),
            "VOXARIUM_STUDIO_PROJECT_ROOT": str(request.project_root),
            "VOXARIUM_STUDIO_ASSETS_DIR": str(request.assets_dir),
            "VOXARIUM_STUDIO_WORK_DIR": str(request.work_dir),
            "VOXARIUM_STUDIO_NODE_ID": request.node_id,
            "VOXARIUM_STUDIO_RUN_ID": request.run_id,
            "VOXARIUM_STUDIO_NODE_TYPE": request.schema.id,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )

    payload = json.dumps(
        {
            "inputs": request.inputs,
            "params": request.params,
            "resolved": request.resolved(),
            "settings": request.settings,
            "context": {
                "project_root": str(request.project_root),
                "assets_dir": str(request.assets_dir),
                "work_dir": str(request.work_dir),
                "output_file": str(output_file),
                "node_id": request.node_id,
                "run_id": request.run_id,
            },
        },
        default=str,
    )

    process = await asyncio.create_subprocess_exec(
        _interpreter_for(request),
        str(script_path),
        cwd=str(request.project_root),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,
    )
    if request.on_process is not None:
        request.on_process(process)

    assert process.stdin and process.stdout and process.stderr
    sentinel_lines: list[str] = []
    stderr_tail: list[str] = []

    async def stderr_log(line: str, stream: str) -> None:
        stderr_tail.append(line)
        del stderr_tail[:-40]
        await request.log(line, stream)

    # Reading starts before the payload is written. A script that fills its output pipe
    # before it reads stdin — a noisy import is enough — would otherwise wait for a reader
    # that is itself waiting for the script to take the payload.
    pumps = asyncio.gather(
        _pump(process.stdout, "stdout", request.log, sentinel_lines, request.progress),
        _pump(process.stderr, "stderr", stderr_log, sentinel_lines, request.progress),
    )

    async def feed_and_wait() -> int:
        stdin = process.stdin
        assert stdin is not None
        try:
            stdin.write(payload.encode("utf-8"))
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            log.debug("Node %s closed stdin before the payload was written", request.node_id)
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                stdin.close()
        return await process.wait()

    # One wait for both halves, so the timeout and a cancel cover the hand-over of the
    # payload as well: either one arriving mid-write still kills the process below.
    timeout = execution.timeout_seconds
    try:
        if timeout:
            exit_code = await asyncio.wait_for(feed_and_wait(), timeout)
        else:
            exit_code = await feed_and_wait()
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        pumps.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pumps
        raise NodeExecutionError(f"Node timed out after {timeout}s") from exc
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        pumps.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pumps
        raise
    finally:
        with contextlib.suppress(asyncio.CancelledError):
            await pumps

    if exit_code != 0:
        detail = "\n".join(stderr_tail[-15:]) or "(no stderr output)"
        raise NodeExecutionError(
            f"Node script exited with code {exit_code}:\n{detail}", exit_code=exit_code
        )

    outputs = _read_script_outputs(output_file, sentinel_lines, request)
    return ExecutionResult(outputs=outputs, exit_code=exit_code)


def _read_script_outputs(
    output_file: Path, sentinel_lines: list[str], request: ExecutionRequest
) -> dict[str, Any]:
    raw: str | None = None
    if output_file.exists():
        raw = output_file.read_text(encoding="utf-8").strip()
    elif sentinel_lines:
        raw = sentinel_lines[-1]

    if not raw:
        if request.schema.outputs:
            log.warning(
                "Node %s declares outputs but produced no result payload",
                request.schema.id,
            )
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NodeExecutionError(
            f"Node produced invalid JSON output: {exc}. Got: {raw[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise NodeExecutionError(
            f"Node output must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


# --------------------------------------------------------------------------- http


class _SafeFormatter(string.Formatter):
    """URL templating that leaves unknown placeholders untouched rather than raising.

    Records which keys it consumed so they are not *also* sent as a query parameter or a
    body field — a node whose URL is itself a parameter would otherwise echo it back to
    the endpoint it just called.
    """

    def __init__(self) -> None:
        super().__init__()
        self.used: set[str] = set()

    def get_value(self, key: Any, args: Any, kwargs: Any) -> Any:  # type: ignore[override]
        if isinstance(key, str):
            if key in kwargs:
                self.used.add(key)
                return kwargs[key]
            return "{" + key + "}"
        return super().get_value(key, args, kwargs)


async def _run_http_api(request: ExecutionRequest) -> ExecutionResult:
    """Call a hosted API — the common shape for text-to-3D services (§8.1)."""
    execution = request.schema.execution
    resolved = request.resolved()
    formatter = _SafeFormatter()
    url = formatter.vformat(_expand_env(execution.entry), (), resolved)
    payload = {k: v for k, v in resolved.items() if k not in formatter.used}
    headers = {k: _expand_env(v) for k, v in execution.headers.items()}
    method = execution.method.upper()
    timeout = execution.timeout_seconds or 300.0

    await request.log(f"{method} {url}", "stdout")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if method == "GET":
                response = await client.get(url, headers=headers, params=payload)
            else:
                response = await client.request(
                    method, url, headers=headers, json=payload
                )
    except httpx.TimeoutException as exc:
        raise NodeExecutionError(f"HTTP request timed out after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise NodeExecutionError(f"HTTP request failed: {exc}") from exc

    await request.log(f"-> {response.status_code} ({len(response.content)} bytes)", "stdout")
    if response.status_code >= 400:
        raise NodeExecutionError(
            f"HTTP {response.status_code}: {response.text[:500]}",
            exit_code=response.status_code,
        )

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        return ExecutionResult(
            outputs=payload if isinstance(payload, dict) else {"result": payload},
            exit_code=response.status_code,
        )

    # Binary responses are persisted as artifacts so downstream file ports can use them.
    request.assets_dir.mkdir(parents=True, exist_ok=True)
    suffix = _suffix_for(content_type)
    artifact = request.assets_dir / f"{request.run_id}_{request.node_id}{suffix}"
    artifact.write_bytes(response.content)
    await request.log(f"saved response to {artifact}", "stdout")
    return ExecutionResult(
        outputs={"result": f"{FILE_MARKER}{artifact}"}, exit_code=response.status_code
    )


def _suffix_for(content_type: str) -> str:
    mapping = {
        "model/stl": ".stl",
        "application/sla": ".stl",
        "model/obj": ".obj",
        "model/gltf-binary": ".glb",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "text/plain": ".txt",
    }
    base = content_type.split(";")[0].strip().lower()
    return mapping.get(base, ".bin")
