"""Running the project's automations off the event bus (§6.5).

The engine is a bus subscriber like the plugin hook pump beside it (§9.7): it invents no
signals of its own, so an automation can only react to what the Studio already says
happened, and everything it does goes through the service a click would have used —
``PipelineRunner.start``, a plugin's registered handler, the bus.

Four things here are guards rather than features, because a firing may cost GPU time or a
model call and nobody is watching when it happens:

- **A status trigger fires on a change, not on the event.** ``team.task.status_changed`` is
  re-published when a summary lands on a task whose status did not move; the payload's
  ``previous`` is what tells the two apart.
- **A path trigger waits for the folder to go quiet** (:data:`PATH_SETTLE_SECONDS`), so an
  export writing two hundred files is one firing that names them, not two hundred.
- **A path automation does not hear the run it started.** A pipeline set off by
  ``./models/`` that writes into ``./models/`` would otherwise start itself for ever; changes
  arriving while its run is live, and for a moment after, are dropped.
- **Chains and floods end.** A run started by an automation carries its depth, and a
  ``run.finished`` trigger refuses past :data:`MAX_CHAIN_DEPTH`; any automation firing more
  than :data:`MAX_FIRES_PER_WINDOW` times a minute is skipped and says so in its log.

**A firing lasts as long as its work.** An action is done when what it set going is done — a
pipeline run ended, a graph update finished — not when it was asked for, so the log never
says "done" about work still going. One that is still at it after :data:`RUNNING_AFTER` is
written *in progress* (``automation.fired``) and written again when it ends
(``automation.run_updated``); a quick one is written once. A row the Studio stopped under is
closed as interrupted — by the firing on its way out, or on the next start if the process
died without one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete
from sqlmodel import select

from ..core.prompts import fill
from ..engine.store import PipelineNotFound
from ..models.automation import AutomationRun
from ..models.pipeline import Run
from ..models.teams import TeamMember, TeamTask
from ..plugins.api import AutomationCancelled
from ..ws.bus import Event, Subscription
from ..ws.events import EventType
from .schema import (
    Automation,
    NotifyAction,
    PathTrigger,
    PluginAction,
    RunFinishedTrigger,
    RunPipelineAction,
    TaskCreatedTrigger,
    TaskStatusTrigger,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import AppContext

log = logging.getLogger(__name__)

PATH_SETTLE_SECONDS = 2.0
"""How long a watched folder must stay quiet before its automation fires."""

RUN_ECHO_SECONDS = 1.5
"""How long after its own run ends a path automation stays deaf. The watcher debounces, so
the run's last writes are reported a few hundred milliseconds after ``run.completed``."""

RUNNING_AFTER = 0.3
"""How long an action may take before its firing is written *in progress* rather than waited
for. A notification is long done by then, and is written once; a pipeline run or a graph
update is not."""

MAX_CHAIN_DEPTH = 3
MAX_FIRES_PER_WINDOW = 10
FIRE_WINDOW_SECONDS = 60.0
MAX_LOG_ROWS = 500
MAX_REPORTED_PATHS = 20
MAX_DETAIL = 2000

INTERRUPTED = "The Studio stopped before this finished"

RUN_ENDINGS = {
    str(EventType.RUN_COMPLETED): "success",
    str(EventType.RUN_FAILED): "failed",
    str(EventType.RUN_CANCELLED): "cancelled",
}

WATCHED = frozenset(
    {
        str(EventType.TEAM_TASK_STATUS_CHANGED),
        str(EventType.TEAM_TASK_CREATED),
        str(EventType.EXPLORER_FILE_CHANGED),
        *RUN_ENDINGS,
    }
)


@dataclass(slots=True)
class Firing:
    """What set an automation off, in the three forms its consumers want."""

    trigger: str
    """The trigger's kind, or ``manual`` for the page's Run now."""

    cause: str
    """One line for the log — ``DP-04 → needs_review``."""

    values: dict[str, str] = field(default_factory=dict)
    """Placeholders for a notification's texts, and what a plugin's handler is told."""

    route: str | None = None
    """Where in the Studio a notification about this should lead."""

    depth: int = 0
    """How many automation-started runs led here (see :data:`MAX_CHAIN_DEPTH`)."""


class _Record:
    """One firing's row while it is being written.

    Shared by the action, which reports what it is doing (``report``), and the engine, which
    writes the row when it goes into the log and when it ends (``write``). Every write persists
    the row *as it stands now*, under one lock, so however the two interleave the last write is
    the latest state — a pipeline that took longer to start than :data:`RUNNING_AFTER` still
    gets its "Running …" and its run's link onto a row already in the log.
    """

    def __init__(self, engine: "AutomationEngine", row: AutomationRun) -> None:
        self.engine = engine
        self.row = row
        self.stored = False
        self.lock = asyncio.Lock()

    async def report(self, detail: str, **data: Any) -> None:
        """What the action is doing now: kept on the row, and written if the log has it yet."""
        self.row.detail = detail[:MAX_DETAIL]
        self.row.data.update(data)
        async with self.lock:
            if self.stored:
                await self.engine._update(self.row)

    async def write(self, written: asyncio.Future[AutomationRun] | None = None) -> None:
        async with self.lock:
            if self.stored:
                await self.engine._update(self.row)
                return
            async with self.engine.ctx.db.session() as session:
                session.add(self.row)
            # Set the moment the row exists, so a firing cancelled on the way out of this
            # method still knows it has a row to close.
            self.stored = True
            if written is not None and not written.done():
                written.set_result(self.row)
            await self.engine._prune()
            await self.engine.ctx.bus.publish(EventType.AUTOMATION_FIRED, run_json(self.row))


@dataclass(slots=True)
class _PathState:
    pending: dict[str, str] = field(default_factory=dict)
    """Changed path → what happened to it, since the last firing."""

    timer: asyncio.Task[None] | None = None
    run_id: str | None = None
    """The live run this automation started; its own writes must not set it off again."""

    deaf_until: float = 0.0


class AutomationEngine:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._subscription: Subscription | None = None
        self._pump: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._paths: dict[str, _PathState] = {}
        self._run_depth: dict[str, int] = {}
        """Runs automations started → how deep in a chain they are. Emptied as they end."""

        self._run_waiters: dict[str, asyncio.Future[tuple[str, str]]] = {}
        """Runs a firing is waiting out → how they ended (``success`` | ``failed`` |
        ``cancelled``, and the error), set from the run's own ending event."""

        self._fired: dict[str, deque[float]] = {}
        self._flooding: set[str] = set()
        """Automations whose flood has been logged, so the log gets one entry per flood."""

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._pump is not None:
            return
        await self._close_interrupted()
        self._subscription = self.ctx.bus.subscribe()
        self._pump = asyncio.create_task(self._run(), name="automations")

    async def stop(self) -> None:
        tasks = [task for task in (self._pump, *self._tasks) if task is not None]
        tasks += [state.timer for state in self._paths.values() if state.timer is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pump = None
        self._tasks.clear()
        self._paths.clear()
        self._run_waiters.clear()

    async def _close_interrupted(self) -> None:
        """Rows still *in progress* from a Studio that stopped without closing them — killed,
        or crashed. Their work died with that process, so they can never finish now."""
        async with self.ctx.db.session() as session:
            stale = (await session.exec(select(AutomationRun).where(AutomationRun.status == "running"))).all()
            for row in stale:
                row.status = "cancelled"
                row.detail = INTERRUPTED
                row.data = {**(row.data or {}), "interrupted": True}
                session.add(row)

    async def _run(self) -> None:
        subscription = self._subscription
        assert subscription is not None
        try:
            async for event in subscription.stream():
                if event.type not in WATCHED:
                    continue
                try:
                    await self._on_event(event)
                except Exception:  # an automation must never take the pump down with it
                    log.exception("Automations failed on %s", event.type)
        finally:
            subscription.close()

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._forget)
        return task

    def _forget(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        # A firing records its action's failures itself; what reaches here is the engine's
        # own — a database that refused the row — and is logged rather than lost with the loop.
        if not task.cancelled() and task.exception() is not None:
            log.error("Automation task %s failed", task.get_name(), exc_info=task.exception())

    # ------------------------------------------------------------------ matching

    def _enabled(self) -> list[Automation]:
        return [a for a in self.ctx.automation_store.load().automations if a.enabled]

    async def _on_event(self, event: Event) -> None:
        if event.type in RUN_ENDINGS:
            await self._on_run_ended(event)
            return
        automations = self._enabled()
        if not automations:
            return
        if event.type == EventType.TEAM_TASK_STATUS_CHANGED:
            await self._on_task_status(event, automations)
        elif event.type == EventType.TEAM_TASK_CREATED:
            await self._on_task_created(event, automations)
        elif event.type == EventType.EXPLORER_FILE_CHANGED:
            self._on_files_changed(event, automations)

    async def _task_values(self, task_id: str) -> dict[str, str] | None:
        """A task as a notification can name it — or ``None`` for a task this project does
        not have, which is an event from the project being closed during a switch (§5.1:
        the two contexts share one bus for a moment)."""
        async with self.ctx.db.session() as session:
            task = await session.get(TeamTask, task_id)
            if task is None:
                return None
            member = await session.get(TeamMember, task.assignee_id)
        return {
            "task": self.ctx.teams.display_id(task),
            "task_id": task.id,
            "title": task.title,
            "assignee": member.name if member is not None else "",
        }

    async def _on_task_status(self, event: Event, automations: list[Automation]) -> None:
        status = str(event.data.get("status") or "")
        previous = event.data.get("previous")
        if previous is not None and str(previous) == status:
            return
        matching = [
            a
            for a in automations
            if isinstance(a.trigger, TaskStatusTrigger)
            and (a.trigger.status is None or str(a.trigger.status) == status)
        ]
        if not matching:
            return
        values = await self._task_values(str(event.data.get("task_id") or ""))
        if values is None:
            return
        values |= {"status": status, "previous": str(previous or "")}
        firing = Firing(
            trigger="task.status_changed",
            cause=f"{values['task']} → {status}",
            values=values,
            route=f"/tasks?task={values['task_id']}",
        )
        for automation in matching:
            self._spawn(self.fire(automation, firing), f"automation:{automation.id}")

    async def _on_task_created(self, event: Event, automations: list[Automation]) -> None:
        matching = [a for a in automations if isinstance(a.trigger, TaskCreatedTrigger)]
        if not matching:
            return
        values = await self._task_values(str(event.data.get("task_id") or ""))
        if values is None:
            return
        firing = Firing(
            trigger="task.created",
            cause=f"{values['task']} created",
            values=values,
            route=f"/tasks?task={values['task_id']}",
        )
        for automation in matching:
            self._spawn(self.fire(automation, firing), f"automation:{automation.id}")

    def _on_files_changed(self, event: Event, automations: list[Automation]) -> None:
        # The watcher stamps its project on the event: during a project switch the outgoing
        # context's watcher is still publishing on the shared bus, and `models/` there is
        # not `models/` here.
        root = event.data.get("root")
        if root is not None and root != str(self.ctx.paths.root):
            return
        changes = event.data.get("changes") or []
        now = time.monotonic()
        for automation in automations:
            trigger = automation.trigger
            if not isinstance(trigger, PathTrigger):
                continue
            matched = {
                str(entry.get("path")): str(entry.get("change"))
                for entry in changes
                if not _is_echo(entry)
                and trigger.matches(str(entry.get("path") or ""), str(entry.get("change") or ""))
            }
            if not matched:
                continue
            state = self._paths.setdefault(automation.id, _PathState())
            if state.run_id is not None or now < state.deaf_until:
                continue
            state.pending.update(matched)
            if state.timer is not None:
                state.timer.cancel()
            state.timer = asyncio.create_task(
                self._settle(automation.id), name=f"automation-settle:{automation.id}"
            )

    async def _settle(self, automation_id: str) -> None:
        await asyncio.sleep(PATH_SETTLE_SECONDS)
        state = self._paths.get(automation_id)
        if state is None:
            return
        state.timer = None
        pending, state.pending = state.pending, {}
        # Read again rather than kept from two seconds ago: the automation may have been
        # disabled, deleted or pointed elsewhere while the folder settled.
        automation = next((a for a in self._enabled() if a.id == automation_id), None)
        if automation is None or not isinstance(automation.trigger, PathTrigger) or not pending:
            return
        paths = sorted(pending)
        shown = paths[:MAX_REPORTED_PATHS]
        where = f"./{automation.trigger.path}/" if automation.trigger.path else "./"
        first = paths[0]
        firing = Firing(
            trigger="path.changed",
            cause=first if len(paths) == 1 else f"{len(paths)} files under {where}",
            values={
                "path": first,
                "paths": "\n".join(shown),
                "count": str(len(paths)),
                "folder": where,
                "change": pending[first],
            },
            route=f"/explorer?path={first}" if pending[first] != "deleted" else "/explorer",
        )
        await self.fire(automation, firing)

    async def _on_run_ended(self, event: Event) -> None:
        run_id = str(event.data.get("run_id") or "")
        outcome = RUN_ENDINGS[event.type]
        waiter = self._run_waiters.pop(run_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result((outcome, _run_error(event)))
        depth = self._run_depth.pop(run_id, 0)
        deaf_until = time.monotonic() + RUN_ECHO_SECONDS
        for state in self._paths.values():
            if state.run_id == run_id:
                state.run_id = None
                state.deaf_until = deaf_until

        matching = [
            a
            for a in self._enabled()
            if isinstance(a.trigger, RunFinishedTrigger)
            and a.trigger.pipeline_id in (None, "", event.data.get("pipeline_id"))
            and a.trigger.outcome in ("any", outcome)
        ]
        if not matching:
            return
        async with self.ctx.db.session() as session:
            run = await session.get(Run, run_id)
        if run is None:  # another project's run, heard across a switch
            return
        firing = Firing(
            trigger="run.finished",
            cause=f"{run.pipeline_name or run.pipeline_id} {outcome}",
            values={
                "pipeline": run.pipeline_name or run.pipeline_id,
                "pipeline_id": run.pipeline_id,
                "run_id": run.id,
                "outcome": outcome,
                "error": str(event.data.get("error") or ""),
            },
            route=f"/pipelines/{run.pipeline_id}",
            depth=depth,
        )
        for automation in matching:
            self._spawn(self.fire(automation, firing), f"automation:{automation.id}")

    # ------------------------------------------------------------------ firing

    async def run_now(self, automation: Automation) -> AutomationRun:
        """The page's Run now: the action, with nothing having happened. Guards do not
        apply — a person asked — and placeholders nothing can fill stay as written.

        Answers as soon as the firing has a row: finished for a quick action, *in progress*
        for a long one, whose work carries on after the answer — a request must not be held
        open for the length of a graph update.
        """
        written: asyncio.Future[AutomationRun] = asyncio.get_running_loop().create_future()
        self._spawn(
            self.fire(
                automation,
                Firing(trigger="manual", cause="Run from the Automations page"),
                guarded=False,
                written=written,
            ),
            f"automation:{automation.id}",
        )
        return await written

    def _flooded(self, automation_id: str) -> bool:
        now = time.monotonic()
        fired = self._fired.setdefault(automation_id, deque())
        while fired and now - fired[0] > FIRE_WINDOW_SECONDS:
            fired.popleft()
        if len(fired) >= MAX_FIRES_PER_WINDOW:
            return True
        fired.append(now)
        self._flooding.discard(automation_id)
        return False

    async def fire(
        self,
        automation: Automation,
        firing: Firing,
        *,
        guarded: bool = True,
        written: asyncio.Future[AutomationRun] | None = None,
    ) -> AutomationRun:
        """Run the automation's action and keep its row: once, when the action is quick; in
        progress and then again, when it is not. ``written`` is resolved with the row the
        moment it first exists — that is all ``run_now`` waits for."""
        record = _Record(self, AutomationRun(
            automation_id=automation.id,
            automation_name=automation.name,
            status="running",
            cause=firing.cause,
            # `cause` and `detail` are English for whoever reads the database; the page words
            # a row from these instead, in its own language (§6.4), and falls back to them.
            data={
                "trigger": firing.trigger,
                "action": automation.action.kind,
                # What it was when it fired: the log outlives an edit and a delete, and an
                # automation nobody named has only its sentence to be recognised by.
                "definition": {
                    "trigger": automation.trigger.model_dump(mode="json"),
                    "action": automation.action.model_dump(mode="json"),
                },
                "values": firing.values,
            },
        ))
        row = record.row
        try:
            if guarded and self._flooded(automation.id):
                if automation.id in self._flooding:
                    # Said once per flood; the row is returned unsaved for the caller's sake.
                    row.status = "skipped"
                    return row
                self._flooding.add(automation.id)
                row.status, row.data["guard"] = "skipped", "flood"
                row.detail = (
                    f"Fired {MAX_FIRES_PER_WINDOW} times within {int(FIRE_WINDOW_SECONDS)} s — "
                    "further firings are skipped until it calms down."
                )
                await record.write(written)
                return row
            if guarded and firing.depth >= MAX_CHAIN_DEPTH:
                row.status, row.data["guard"] = "skipped", "chain"
                row.detail = f"A chain of {MAX_CHAIN_DEPTH} automation-started runs led here; not extended."
                await record.write(written)
                return row
            return await self._carry_out(automation, firing, record, written)
        finally:
            if written is not None and not written.done():
                written.set_result(row)

    async def _carry_out(
        self,
        automation: Automation,
        firing: Firing,
        record: _Record,
        written: asyncio.Future[AutomationRun] | None,
    ) -> AutomationRun:
        row = record.row
        work = asyncio.ensure_future(self._act(automation, firing, record))
        try:
            await asyncio.wait({work}, timeout=RUNNING_AFTER)
            if not work.done():
                await record.write(written)
            detail = await work
        except asyncio.CancelledError:
            # The Studio is stopping under this firing. Its work goes with it — say so on the
            # row rather than leave it in progress for ever.
            work.cancel()
            if record.stored:
                row.status, row.detail = "cancelled", INTERRUPTED
                row.data["interrupted"] = True
                await record.write()
            raise
        except AutomationCancelled as exc:
            row.status, row.detail = "cancelled", str(exc)
        except Exception as exc:
            log.warning("Automation %s failed: %s", automation.id, exc)
            row.status, row.detail = "error", str(exc) or type(exc).__name__
        else:
            row.status, row.detail = "ok", detail
        row.detail = row.detail[:MAX_DETAIL]
        await record.write(written)
        return row

    async def _prune(self) -> None:
        async with self.ctx.db.session() as session:
            newest = select(AutomationRun.id).order_by(AutomationRun.created_at.desc()).limit(MAX_LOG_ROWS)  # type: ignore[attr-defined]
            await session.exec(delete(AutomationRun).where(AutomationRun.id.not_in(newest)))  # type: ignore[attr-defined,call-overload]

    async def _update(self, row: AutomationRun) -> None:
        """Write the row as it stands now over the one in the log — a report, or where it
        ended up. Read fresh by id: the row in hand was written in another session, and it
        may be gone since — pruned, or the log cleared."""
        async with self.ctx.db.session() as session:
            stored = await session.get(AutomationRun, row.id)
            if stored is None:
                return
            stored.status = row.status
            stored.detail = row.detail
            stored.data = dict(row.data)
            session.add(stored)
        await self.ctx.bus.publish(EventType.AUTOMATION_RUN_UPDATED, run_json(row))

    async def _act(self, automation: Automation, firing: Firing, record: _Record) -> str:
        """Do what the automation says and answer with the log's line once it is done.
        A long action says what it is doing through ``record.report``; raising
        :class:`AutomationCancelled` records the firing as cancelled, anything else as failed."""
        action = automation.action
        values = {"automation": automation.name, **firing.values}
        if isinstance(action, NotifyAction):
            await self.ctx.bus.publish(
                EventType.AUTOMATION_NOTIFICATION,
                {
                    "automation_id": automation.id,
                    "automation_name": automation.name,
                    "title": fill(action.title, values),
                    "message": fill(action.message, values),
                    "trigger": firing.trigger,
                    "values": firing.values,
                    "route": firing.route,
                },
            )
            return "Notification sent"

        if isinstance(action, RunPipelineAction):
            try:
                pipeline = self.ctx.pipelines.get(action.pipeline_id)
            except PipelineNotFound as exc:
                raise ValueError(f"No pipeline {action.pipeline_id!r}") from exc
            name = pipeline.name or pipeline.id
            run = await self.ctx.runner.start(
                pipeline,
                use_cache=action.use_cache,
                trigger="automation",
                created_by=f"automation:{automation.id}",
            )
            # Registered before anything can await: the run's ending event is handled by the
            # pump, and a run that ended before its waiter existed would be waited on for ever.
            ended: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
            self._run_waiters[run.id] = ended
            self._run_depth[run.id] = firing.depth + 1
            if isinstance(automation.trigger, PathTrigger):
                self._paths.setdefault(automation.id, _PathState()).run_id = run.id
            try:
                await record.report(f"Running {name}", run_id=run.id, pipeline_id=pipeline.id)
                outcome, error = await ended
            finally:
                self._run_waiters.pop(run.id, None)
            if outcome == "success":
                return f"Finished {name}"
            if outcome == "cancelled":
                raise AutomationCancelled(f"{name} was cancelled")
            raise RuntimeError(error or f"{name} failed")

        assert isinstance(action, PluginAction)
        offered = self.ctx.registry.automation_action(action.plugin_id, action.action_id)
        if offered is None:
            raise ValueError(
                f"Plugin {action.plugin_id!r} offers no action {action.action_id!r} — "
                "is the plugin enabled?"
            )
        answer = await offered.handler(
            self.ctx.plugin_host(action.plugin_id),
            {
                "automation": {"id": automation.id, "name": automation.name},
                "trigger": firing.trigger,
                "values": dict(firing.values),
            },
        )
        if answer is None:
            return offered.label
        if isinstance(answer, str):
            return answer
        with contextlib.suppress(TypeError, ValueError):
            return json.dumps(answer, ensure_ascii=False, default=str)
        return str(answer)

    # ------------------------------------------------------------------ the log

    async def recent(self, limit: int = 50) -> list[AutomationRun]:
        async with self.ctx.db.session() as session:
            statement = select(AutomationRun).order_by(AutomationRun.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
            return list((await session.exec(statement)).all())

    async def clear_log(self) -> None:
        """Everything that has finished. A row still in progress is not history yet — its work
        is running, and clearing it would take away the one place that says so."""
        async with self.ctx.db.session() as session:
            await session.exec(delete(AutomationRun).where(AutomationRun.status != "running"))  # type: ignore[call-overload,arg-type]


def _is_echo(entry: dict[str, Any]) -> bool:
    """A directory reported as *modified*: its timestamp moving because a child was written.

    The child is in the same batch under its own name, so counting the folder too is how two
    saved files became "3 files under ./models/". A folder *added* or *deleted* is news of
    its own and is kept.
    """
    return bool(entry.get("is_dir")) and entry.get("change") == "modified"


def _run_error(event: Event) -> str:
    """Why a run ended badly, as its ending event says — the crash path carries only ``error``,
    the normal one the nodes that failed as well."""
    error = event.data.get("error")
    if error:
        return str(error)
    failed = event.data.get("failed_nodes") or []
    return f"Failed at {', '.join(str(node) for node in failed)}" if failed else ""


def run_json(row: AutomationRun) -> dict[str, Any]:
    return row.model_dump(mode="json")
