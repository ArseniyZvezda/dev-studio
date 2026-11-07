"""Pipeline execution: DAG traversal, caching, live status/log streaming (§8.2)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import select

from ..core.config import AppConfig
from ..core.db import Database
from ..core.paths import ProjectPaths, current_data_path
from ..models.base import utcnow
from ..models.pipeline import NodeRun, NodeStatus, Run, RunStatus
from ..plugins.registry import Registry
from ..schemas.nodes import NodeTypeSchema
from ..schemas.pipeline import NodeInstance, PipelineDefinition
from ..ws.bus import EventBus
from ..ws.events import EventType
from . import cache as node_cache
from . import dag
from .executors import (
    FILE_MARKER,
    ExecutionRequest,
    ExecutionResult,
    NodeExecutionError,
    execute,
)
from .jobs import JobEngine, JobHandle

log = logging.getLogger(__name__)

MIN_PROGRESS_INTERVAL = 0.1
"""Floor between two published frames for one node. A node reporting sensibly — the
Hunyuan3D poll is every two seconds — never meets it; a runaway one cannot flood the bus
into evicting other events.

The frame it drops can be a node's **last**, since nothing flushes on the way out. That is
survivable only because progress is not a result: the node's terminal status follows within
milliseconds and the client drops the bar on it, so what is lost is the final tick of a bar
about to disappear. Nothing may be published this way that has to arrive."""

MAX_PROGRESS_TEXT = 120
"""Cap on `label` and `detail`. They are drawn on a node roughly 240px wide."""


def _progress_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()[:MAX_PROGRESS_TEXT]
    return text or None


def _progress_number(value: Any, *, low: float, high: float, integer: bool) -> Any | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    clamped = max(low, min(high, float(value)))
    return int(clamped) if integer else round(clamped, 2)


def _normalise_progress(frame: dict[str, Any]) -> dict[str, Any] | None:
    """A node's progress frame reduced to the six fields clients are promised.

    Everything else a node sends is dropped rather than forwarded: the frame is broadcast
    to every connected client over a channel with no back pressure, so its size has to be
    the host's decision and not the node's.

    Node output is plugin code, so nothing here trusts the shape either: an out-of-range
    percent is clamped rather than refused, and a frame carrying no usable field at all is
    dropped — publishing an empty one would blank whatever the node last reported.
    """
    normalised = {
        "percent": _progress_number(frame.get("percent"), low=0, high=100, integer=False),
        "label": _progress_text(frame.get("label")),
        "detail": _progress_text(frame.get("detail")),
        "eta_s": _progress_number(frame.get("eta_s"), low=0, high=86400, integer=False),
        "step": _progress_number(frame.get("step"), low=0, high=10**9, integer=True),
        "steps": _progress_number(frame.get("steps"), low=0, high=10**9, integer=True),
    }
    if all(value is None for value in normalised.values()):
        return None
    return normalised


def _mark_files(outputs: dict[str, Any], schema: NodeTypeSchema) -> dict[str, Any]:
    """Tag file-typed outputs so the cache can later detect deleted artifacts."""
    marked = dict(outputs)
    for port in schema.outputs:
        if port.type not in ("file", "directory"):
            continue
        value = marked.get(port.id)
        if isinstance(value, str) and value and not value.startswith(FILE_MARKER):
            marked[port.id] = f"{FILE_MARKER}{value}"
    return marked


def _unmark(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(FILE_MARKER):
        return value.removeprefix(FILE_MARKER)
    return value


def _unmark_all(outputs: dict[str, Any]) -> dict[str, Any]:
    return {key: _unmark(value) for key, value in outputs.items()}


@dataclass(slots=True)
class _Hold:
    """One run's pause: the nodes holding it, and the event that lets them all go.

    One event for the whole run rather than one per node, because Continue is a statement
    about the run — two nodes finishing together in the same level would otherwise need two
    clicks to say the one thing. Kept only while somebody is waiting: the last waiter to
    leave drops it, so a node that pauses later builds a fresh one and holds the run again.

    In memory only. A paused run is a job still sitting in the engine, so it is exactly as
    live as a running one and exactly as stale after a restart.
    """

    event: asyncio.Event = field(default_factory=asyncio.Event)
    nodes: set[str] = field(default_factory=set)


class PipelineRunner:
    """Owns the lifecycle of pipeline runs for one managed project."""

    def __init__(
        self,
        *,
        paths: ProjectPaths,
        db: Database,
        bus: EventBus,
        jobs: JobEngine,
        registry: Registry,
        config: AppConfig,
    ) -> None:
        self.paths = paths
        self.db = db
        self.bus = bus
        self.jobs = jobs
        self.registry = registry
        self.config = config
        self._holds: dict[str, _Hold] = {}

    # ------------------------------------------------------------------ public API

    async def start(
        self,
        pipeline: PipelineDefinition,
        *,
        start_node_id: str | None = None,
        use_cache: bool = True,
        params: dict[str, Any] | None = None,
        created_by: str | None = None,
        trigger: str = "manual",
    ) -> Run:
        """Validate, persist a Run, and schedule it on the Job Engine.

        ``trigger`` says what started it — ``manual`` for a click, ``automation`` for a
        rule on the Automations page (§6.5), which also names itself in ``created_by``.
        """
        issues = dag.validate(pipeline, self.registry)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise ValueError(
                "Pipeline is not runnable:\n"
                + "\n".join(f"  - {i.message}" for i in errors)
            )

        selection: set[str] | None = None
        if start_node_id is not None:
            selection = dag.selection_for_start_node(pipeline, start_node_id)

        run = Run(
            pipeline_id=pipeline.id,
            pipeline_name=pipeline.name,
            status=RunStatus.PENDING,
            trigger=trigger,
            start_node_id=start_node_id,
            use_cache=use_cache,
            params=params or {},
            created_by=created_by,
        )
        node_ids = [
            node.id
            for node in pipeline.nodes
            if selection is None or node.id in selection
        ]

        async with self.db.session() as session:
            session.add(run)
            # Flush before the child rows: without a declared relationship SQLAlchemy
            # cannot infer the insert order from the foreign key alone.
            await session.flush()
            for node in pipeline.nodes:
                status = (
                    NodeStatus.PENDING
                    if node.id in node_ids and not node.disabled
                    else NodeStatus.SKIPPED
                )
                session.add(
                    NodeRun(
                        run_id=run.id,
                        node_id=node.id,
                        node_type=node.type,
                        status=status,
                    )
                )

        self.paths.run_dir(run.id).mkdir(parents=True, exist_ok=True)
        self.jobs.submit(
            run.id,
            "pipeline_run",
            lambda handle: self._execute_run(handle, run, pipeline, selection),
            pipeline_id=pipeline.id,
        )
        return run

    async def cancel(self, run_id: str) -> bool:
        """Cancel an in-flight run. Node subprocesses are killed as the task unwinds."""
        cancelled = await self.jobs.cancel(run_id)
        if not cancelled:
            # Not in flight — make sure a stale row is not left looking "running".
            async with self.db.session() as session:
                run = await session.get(Run, run_id)
                if run is not None and run.status in (
                    RunStatus.PENDING,
                    RunStatus.RUNNING,
                    RunStatus.PAUSED,
                ):
                    run.status = RunStatus.CANCELLED
                    run.finished_at = utcnow()
                    session.add(run)
                    cancelled = True
        return cancelled

    def is_running(self, run_id: str) -> bool:
        return self.jobs.is_running(run_id)

    def paused_nodes(self, run_id: str) -> list[str]:
        """Which nodes are holding this run open right now — empty when none are.

        Derived rather than stored: this is the same kind of live state as "is the job in
        the engine", so `GET /runs/{id}` asks the runner for it instead of reading a column
        that a crashed process would have left lying.
        """
        hold = self._holds.get(run_id)
        return [] if hold is None or hold.event.is_set() else sorted(hold.nodes)

    async def resume(self, run_id: str) -> bool:
        """Let a paused run carry on. False when nothing was waiting."""
        hold = self._holds.get(run_id)
        if hold is None or hold.event.is_set():
            return False
        # Read the members before releasing: the waiters remove themselves the moment the
        # event lets them run, and the event tells clients which nodes it just freed.
        released = sorted(hold.nodes)
        await self._set_run_status(run_id, RunStatus.RUNNING)
        hold.event.set()
        await self.bus.publish(
            EventType.RUN_RESUMED, {"run_id": run_id, "node_ids": released}
        )
        return True

    # ------------------------------------------------------------------ execution

    async def _execute_run(
        self,
        handle: JobHandle,
        run: Run,
        pipeline: PipelineDefinition,
        selection: set[str] | None,
    ) -> None:
        await self._set_run_status(run.id, RunStatus.RUNNING, started=True)
        await self.bus.publish(
            EventType.RUN_STARTED,
            {
                "run_id": run.id,
                "pipeline_id": pipeline.id,
                "pipeline_name": pipeline.name,
                "start_node_id": run.start_node_id,
                "node_ids": sorted(selection) if selection else [n.id for n in pipeline.nodes],
            },
        )

        outputs_by_node: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        error_message: str | None = None

        try:
            scope = {
                node.id
                for node in pipeline.nodes
                if self._should_run(pipeline, node.id, selection)
            }
            error_message = await self._run_subgraph(
                run, pipeline, scope, outputs_by_node, failed, handle
            )

        except asyncio.CancelledError:
            await self._finish_run(run.id, RunStatus.CANCELLED, None)
            await self._mark_unfinished_nodes(run.id, NodeStatus.CANCELLED)
            await self.bus.publish(
                EventType.RUN_CANCELLED, {"run_id": run.id, "pipeline_id": pipeline.id}
            )
            raise
        except Exception as exc:
            log.exception("Run %s crashed", run.id)
            await self._finish_run(run.id, RunStatus.ERROR, str(exc))
            await self.bus.publish(
                EventType.RUN_FAILED,
                {"run_id": run.id, "pipeline_id": pipeline.id, "error": str(exc)},
            )
            return

        status = RunStatus.ERROR if failed else RunStatus.SUCCESS
        await self._finish_run(run.id, status, error_message)
        await self._write_run_summary(run.id, pipeline, outputs_by_node, failed)
        await self.bus.publish(
            EventType.RUN_FAILED if failed else EventType.RUN_COMPLETED,
            {
                "run_id": run.id,
                "pipeline_id": pipeline.id,
                "status": str(status),
                "failed_nodes": failed,
                "error": error_message,
            },
        )

    async def _run_subgraph(
        self,
        run: Run,
        pipeline: PipelineDefinition,
        scope: set[str],
        outputs_by_node: dict[str, dict[str, Any]],
        failed: list[str],
        handle: JobHandle,
    ) -> str | None:
        """Run every node in ``scope`` in topological order; answer the first error.

        The whole run is one call of this, and so is one turn of a loop — which is what
        makes iteration cost the engine no second traversal. A loop's body is lifted out of
        the walk here and handed to the iterating node instead: it owns those nodes for as
        long as it is running them, so nothing else may schedule them (§8.2).
        """
        bodies = dag.loop_bodies(pipeline, self.registry, scope)
        # A nested iterator is inside its outer body and is excluded with it — the outer
        # loop hands it down, and the recursion picks it up with the smaller scope.
        owned: set[str] = set().union(*bodies.values()) if bodies else set()
        walkable = scope - owned
        error_message: str | None = None

        for level in dag.topological_levels(pipeline):
            runnable = [node_id for node_id in level if node_id in walkable]
            if not runnable:
                continue

            # A node whose upstream failed can never run; mark it and move on.
            blocked = [
                node_id
                for node_id in runnable
                if self._has_failed_ancestor(pipeline, node_id, failed)
            ]
            for node_id in blocked:
                await self._set_node_status(run.id, node_id, NodeStatus.SKIPPED)
                failed.append(node_id)

            pending = [n for n in runnable if n not in blocked]
            if not pending:
                continue

            results = await asyncio.gather(
                *(
                    self._run_node(
                        run,
                        pipeline,
                        pipeline.node(node_id),
                        outputs_by_node,
                        failed,
                        handle,
                        body=bodies.get(node_id),
                    )
                    for node_id in pending
                ),
                return_exceptions=True,
            )
            for node_id, result in zip(pending, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    failed.append(node_id)
                    if error_message is None:
                        error_message = f"{node_id}: {result}"
                else:
                    outputs_by_node[node_id] = result

        return error_message

    def _should_run(
        self, pipeline: PipelineDefinition, node_id: str, selection: set[str] | None
    ) -> bool:
        node = pipeline.node(node_id)
        if node is None or node.disabled:
            return False
        return selection is None or node_id in selection

    def _has_failed_ancestor(
        self, pipeline: PipelineDefinition, node_id: str, failed: list[str]
    ) -> bool:
        if not failed:
            return False
        return bool(dag.ancestors(pipeline, node_id) & set(failed))

    async def _run_node(
        self,
        run: Run,
        pipeline: PipelineDefinition,
        node: NodeInstance | None,
        outputs_by_node: dict[str, dict[str, Any]],
        failed: list[str],
        handle: JobHandle,
        *,
        body: set[str] | None = None,
    ) -> dict[str, Any]:
        assert node is not None
        schema = self.registry.node_type(node.type)
        if schema is None:
            raise NodeExecutionError(f"Unknown node type {node.type!r}")

        inputs = self._resolve_inputs(pipeline, node, schema, outputs_by_node)
        params = self._resolve_params(node, schema)
        log_path = self.paths.run_dir(run.id) / f"{node.id}.log"

        async def log_fn(line: str, stream: str) -> None:
            await self._append_log(log_path, line, stream)
            await self.bus.publish(
                EventType.NODE_LOG,
                {
                    "run_id": run.id,
                    "node_id": node.id,
                    "stream": stream,
                    "line": line,
                },
            )

        last_progress = 0.0

        async def progress_fn(frame: dict[str, Any]) -> None:
            nonlocal last_progress
            normalised = _normalise_progress(frame)
            if normalised is None:
                return
            now = time.monotonic()
            if now - last_progress < MIN_PROGRESS_INTERVAL:
                return
            last_progress = now
            await self.bus.publish(
                EventType.NODE_PROGRESS,
                {"run_id": run.id, "node_id": node.id, "progress": normalised},
            )

        cache_key: str | None = None
        cache_enabled = (
            self.config.node_cache_enabled
            and run.use_cache
            and schema.execution.cacheable
            # The explicitly-started node always re-executes: the user asked for it.
            and node.id != run.start_node_id
        )
        if cache_enabled:
            cache_key = node_cache.compute_cache_key(
                schema, inputs, params, project_root=self.paths.root
            )
            async with self.db.session() as session:
                cached = await node_cache.lookup(session, cache_key)
            if cached is not None:
                await log_fn(f"reusing cached result ({cache_key[:12]})", "stdout")
                await self._finish_node(
                    run,
                    node,
                    NodeStatus.CACHED,
                    log_fn,
                    outputs=_unmark_all(cached),
                    inputs=inputs,
                    cache_key=cache_key,
                    from_cache=True,
                )
                return _unmark_all(cached)

        await self._set_node_status(
            run.id, node.id, NodeStatus.RUNNING, inputs=inputs, started=True
        )

        request = ExecutionRequest(
            node_id=node.id,
            run_id=run.id,
            schema=schema,
            inputs=inputs,
            params=params,
            project_root=self.paths.root,
            assets_dir=self.paths.assets,
            work_dir=self.paths.run_dir(run.id),
            log=log_fn,
            progress=progress_fn,
            manifest=self.registry.manifest_for_node_type(node.type),
            node_class=self.registry.node_class(node.type),
            settings=self._plugin_settings(schema.plugin_id),
        )

        try:
            async with self.jobs.slot(schema.execution.resource_class):
                result: ExecutionResult = await execute(request)
        except asyncio.CancelledError:
            await self._set_node_status(
                run.id, node.id, NodeStatus.CANCELLED, finished=True
            )
            raise
        except Exception as exc:
            exit_code = getattr(exc, "exit_code", None)
            await log_fn(str(exc), "stderr")
            await self._set_node_status(
                run.id,
                node.id,
                NodeStatus.ERROR,
                error=str(exc),
                exit_code=exit_code,
                finished=True,
            )
            raise

        marked = _mark_files(result.outputs, schema)
        if cache_enabled and cache_key is not None:
            async with self.db.session() as session:
                await node_cache.store(
                    session, cache_key, schema.id, marked, run_id=run.id
                )

        plain = _unmark_all(marked)

        if body:
            # The loop is this node's own work, so it happens before the node is finished:
            # the canvas keeps it `running` with a progress bar over the items instead of
            # showing it done while everything downstream of it is still going. It is also
            # after the resource slot was released — the body takes slots of its own.
            try:
                await self._iterate(
                    run,
                    pipeline,
                    node,
                    schema,
                    plain,
                    body,
                    outputs_by_node,
                    failed,
                    handle,
                    log_fn,
                    progress_fn,
                )
            except asyncio.CancelledError:
                await self._set_node_status(
                    run.id, node.id, NodeStatus.CANCELLED, finished=True
                )
                raise
            except Exception as exc:
                await log_fn(str(exc), "stderr")
                await self._set_node_status(
                    run.id, node.id, NodeStatus.ERROR, error=str(exc), finished=True
                )
                raise

        await self._finish_node(
            run,
            node,
            NodeStatus.SUCCESS,
            log_fn,
            outputs=plain,
            inputs=inputs,
            cache_key=cache_key,
            exit_code=result.exit_code,
            log_path=str(log_path),
        )
        return plain

    # ------------------------------------------------------------------ loops

    async def _iterate(
        self,
        run: Run,
        pipeline: PipelineDefinition,
        node: NodeInstance,
        schema: NodeTypeSchema,
        outputs: dict[str, Any],
        body: set[str],
        outputs_by_node: dict[str, dict[str, Any]],
        failed: list[str],
        handle: JobHandle,
        log_fn: Callable[[str, str], Awaitable[None]],
        progress_fn: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Run ``body`` once per item on the node's iterating port (§8.2).

        The body sees the item alone: what the node published as a list is replaced, for
        the length of one turn, by the element the turn is about. Everything else the node
        answered is left as it is — those outputs do not repeat.

        A failing turn ends the loop rather than carrying on through the rest of the
        items. It is the same rule the engine already applies to a failed node's
        descendants, and the alternative — a run that reports success with three of two
        hundred files quietly unwritten — is the worse of the two.
        """
        port = schema.iterates or ""
        items = outputs.get(port)
        if not isinstance(items, list):
            raise NodeExecutionError(
                f"Node type {schema.id!r} iterates over {port!r} and must answer with a "
                f"list there, got {type(items).__name__}"
            )

        if not items:
            # Nothing to run the body with. Left `pending`, those nodes would sit in the
            # canvas's "still to come" colour for a run that has ended.
            await log_fn("nothing to iterate — everything downstream is skipped", "stdout")
            for node_id in sorted(body):
                await self._set_node_status(run.id, node_id, NodeStatus.SKIPPED)
            return

        total = len(items)
        constant = {key: value for key, value in outputs.items() if key != port}
        try:
            for index, item in enumerate(items, start=1):
                await progress_fn(
                    {
                        "label": str(item),
                        "percent": (index - 1) * 100 / total,
                        "step": index,
                        "steps": total,
                    }
                )
                await log_fn(f"[{index}/{total}] {item}", "stdout")
                outputs_by_node[node.id] = {**constant, port: item}
                error = await self._run_subgraph(
                    run, pipeline, body, outputs_by_node, failed, handle
                )
                if error is not None:
                    raise NodeExecutionError(f"item {index}/{total} ({item}): {error}")
        finally:
            # Back to what the node itself produced. The run summary and anything reading
            # the map afterwards should see the list, not whichever item was last.
            outputs_by_node[node.id] = outputs

    # ------------------------------------------------------------------ pausing

    async def _finish_node(
        self,
        run: Run,
        node: NodeInstance,
        status: NodeStatus,
        log_fn: Callable[[str, str], Awaitable[None]],
        *,
        from_cache: bool = False,
        **fields: Any,
    ) -> None:
        """Record a node's terminal status — and hold the run there if it was asked to.

        The pause travels *as* the node's status rather than beside it: the canvas already
        paints one status per node, so "which node is the Continue button for" needs no
        second channel. The outputs go with it, so a node whose result is something to look
        at — a preview — is showing it while it is the thing holding everything up.

        What is stored at the end is the ordinary terminal status either way. ``paused`` is
        live state, and a finished run that still claimed one would be describing a wait
        nobody can end.
        """
        if not node.pause_on_complete:
            await self._set_node_status(
                run.id, node.id, status, from_cache=from_cache, finished=True, **fields
            )
            return

        await self._set_node_status(
            run.id, node.id, NodeStatus.PAUSED, from_cache=from_cache, finished=True, **fields
        )
        try:
            await self._hold(run.id, node.id, log_fn)
        finally:
            # However the wait ended — the user continuing, or a cancel unwinding the run —
            # this node did its work and succeeded. Leaving it `paused` would outlive the
            # run that was paused.
            await self._set_node_status(run.id, node.id, status, from_cache=from_cache)

    async def _hold(
        self, run_id: str, node_id: str, log_fn: Callable[[str, str], Awaitable[None]]
    ) -> None:
        """Block this node until :meth:`resume` is called, or the run is cancelled.

        Deliberately *after* the resource slot is released, so a paused GPU node is not
        still holding the GPU while it waits for someone to come back from lunch.
        """
        hold = self._holds.get(run_id)
        if hold is None or hold.event.is_set():
            # A *spent* record is as good as none: a node that finishes after the user has
            # already continued the run is a second stop, not the same one. Reusing the
            # released event would let it wait on something that has already happened and
            # walk straight past the pause it was configured for.
            hold = _Hold()
            self._holds[run_id] = hold
        hold.nodes.add(node_id)
        try:
            await self._set_run_status(run_id, RunStatus.PAUSED)
            await log_fn("paused on complete — the run continues when you say so", "stdout")
            await self.bus.publish(
                EventType.RUN_PAUSED,
                {"run_id": run_id, "node_id": node_id, "node_ids": sorted(hold.nodes)},
            )
            await hold.event.wait()
        finally:
            # The waiter owns the record's lifetime — nothing else removes it, and a cancel
            # arriving anywhere above still runs this.
            hold.nodes.discard(node_id)
            # `is hold`, not just presence: this waiter may be unwinding *after* a later
            # node replaced the record, and it must not remove somebody else's pause.
            if not hold.nodes and self._holds.get(run_id) is hold:
                self._holds.pop(run_id, None)

    def _resolve_inputs(
        self,
        pipeline: PipelineDefinition,
        node: NodeInstance,
        schema: NodeTypeSchema,
        outputs_by_node: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Edge-supplied values for a node's input ports."""
        inputs: dict[str, Any] = {}
        for edge in pipeline.incoming(node.id):
            upstream = outputs_by_node.get(edge.from_.node)
            if upstream is None:
                continue
            if edge.from_.port in upstream:
                inputs[edge.to.port] = _unmark(upstream[edge.from_.port])
            elif len(upstream) == 1 and schema.input(edge.to.port) is not None:
                # Convenience: a single-output node can feed any input by position.
                inputs[edge.to.port] = _unmark(next(iter(upstream.values())))
        return inputs

    def _resolve_params(self, node: NodeInstance, schema: NodeTypeSchema) -> dict[str, Any]:
        """Literal config values, with schema defaults filled in."""
        params: dict[str, Any] = {}
        for port in schema.inputs:
            if port.id in node.params:
                params[port.id] = node.params[port.id]
            elif port.default is not None:
                params[port.id] = port.default
            value = params.get(port.id)
            if port.type in ("file", "directory", "path") and isinstance(value, str):
                params[port.id] = current_data_path(value, self.paths.root)
        # Keep extra params the schema does not declare — plugins may read them.
        for key, value in node.params.items():
            params.setdefault(key, value)
        return params

    def _plugin_settings(self, plugin_id: str) -> dict[str, Any]:
        if plugin_id.startswith("vcs:"):
            return dict(self.config.vcs.settings.get(plugin_id.removeprefix("vcs:"), {}))
        state = self.config.plugins.get(plugin_id)
        return dict(state.settings) if state else {}

    # ------------------------------------------------------------------ persistence

    async def _append_log(self, path: Path, line: str, stream: str) -> None:
        prefix = "! " if stream == "stderr" else ""
        try:
            await asyncio.to_thread(_append_line, path, f"{prefix}{line}\n")
        except OSError as exc:  # pragma: no cover - disk full etc.
            log.warning("Could not write node log %s: %s", path, exc)

    async def _set_run_status(
        self, run_id: str, status: RunStatus, *, started: bool = False
    ) -> None:
        async with self.db.session() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return
            run.status = status
            if started:
                run.started_at = utcnow()
            session.add(run)

    async def _finish_run(self, run_id: str, status: RunStatus, error: str | None) -> None:
        async with self.db.session() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return
            run.status = status
            run.error = error
            run.finished_at = utcnow()
            session.add(run)

    async def _mark_unfinished_nodes(self, run_id: str, status: NodeStatus) -> None:
        async with self.db.session() as session:
            result = await session.exec(
                select(NodeRun).where(
                    NodeRun.run_id == run_id,
                    NodeRun.status.in_([NodeStatus.PENDING, NodeStatus.RUNNING]),  # type: ignore[attr-defined]
                )
            )
            for node_run in result.all():
                node_run.status = status
                node_run.finished_at = utcnow()
                session.add(node_run)

    async def _set_node_status(
        self,
        run_id: str,
        node_id: str,
        status: NodeStatus,
        *,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
        exit_code: int | None = None,
        cache_key: str | None = None,
        from_cache: bool = False,
        log_path: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        async with self.db.session() as session:
            result = await session.exec(
                select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
            )
            node_run = result.first()
            if node_run is None:
                return
            node_run.status = status
            if inputs is not None:
                node_run.inputs = _json_safe(inputs)
            if outputs is not None:
                node_run.outputs = _json_safe(outputs)
            if error is not None:
                node_run.error = error
            if exit_code is not None:
                node_run.exit_code = exit_code
            if cache_key is not None:
                node_run.cache_key = cache_key
            if log_path is not None:
                node_run.log_path = log_path
            node_run.from_cache = from_cache
            if started:
                node_run.started_at = utcnow()
            if finished:
                node_run.finished_at = utcnow()
            session.add(node_run)

        await self.bus.publish(
            EventType.NODE_STATUS_CHANGED,
            {
                "run_id": run_id,
                "node_id": node_id,
                "status": str(status),
                "from_cache": from_cache,
                "error": error,
                "outputs": _json_safe(outputs) if outputs else None,
            },
        )

    async def _write_run_summary(
        self,
        run_id: str,
        pipeline: PipelineDefinition,
        outputs: dict[str, dict[str, Any]],
        failed: list[str],
    ) -> None:
        summary = {
            "run_id": run_id,
            "pipeline_id": pipeline.id,
            "pipeline_name": pipeline.name,
            "finished_at": utcnow().isoformat(),
            "failed_nodes": failed,
            "outputs": _json_safe(outputs),
        }
        path = self.paths.run_dir(run_id) / "run.json"
        with contextlib.suppress(OSError):
            await asyncio.to_thread(
                path.write_text,
                json.dumps(summary, indent=2, default=str),
                "utf-8",
            )


def _append_line(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _json_safe(value: Any) -> Any:
    """Outputs come from user code, so coerce anything unserialisable to a string."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))
