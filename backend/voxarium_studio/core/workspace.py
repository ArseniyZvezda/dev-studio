"""Opening another managed project without restarting the server (§5).

The Studio is pointed at one managed project at a time, and everything it knows about
that project hangs off a single :class:`~voxarium_studio.core.context.AppContext`. Switching
projects selects another context while keeping previous contexts running (§5.1). Returning
to a project reuses its context, including running sessions and their database connections.

This module also holds the recently-opened list, beside application settings (§5.2)
and the global plugin registry (§9.4). It is a list of paths and nothing else, so deleting
the file loses no project data.

That list is what a start with no path on the command line resolves against
(:func:`resolve_startup_project`), which is why it is a feature rather than a convenience:
losing it no longer only costs a menu, it sends the next start to the demo project.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from ..models.base import utcnow
from ..plugins import envs as plugin_envs
from ..sync.reservations import ReservationOwner
from ..ws.bus import Event, EventBus, Subscription
from ..ws.events import EventType, desktop_target, producer_needs_reply
from .context import AppContext
from .demo import ensure_demo_project
from .paths import ProjectPaths, has_project_data, user_config_dir
from .project_scope import ProjectDescriptor, canonical_root, resolve_project
from .task_counts import read_task_counts

log = logging.getLogger(__name__)

RECENT_FILENAME = "recent-projects.json"
MAX_RECENT = 12


class ProjectNotFound(ValueError):
    """The path does not exist, or is not a directory."""


class ProjectNotInitialised(ValueError):
    """The directory has no ``.voxarium_studio/`` and the caller asked not to create one."""


# ---------------------------------------------------------------- opening a project


def initialise_workspace(state: Any, *, browser_bus: EventBus | None = None) -> None:
    """Register the started bootstrap context and server-wide auth (§5.1, §5.3).

    Call after the initial context's startup succeeds. Repeated calls never rotate
    runtime ids, discard resident contexts or reset the legacy browser event bus.
    """
    # App lifespan supplies the owner BEFORE bootstrap startup (Studio design §4.3).
    # Embedded callers may initialise it here, but selection/reinitialisation must
    # never replace it or reopen admission, even after a failed shutdown.
    if getattr(state, "remote_reservations", None) is None:
        state.remote_reservations = ReservationOwner()
    state.ctx.remote_reservations = state.remote_reservations
    if getattr(state, "ai_access", None) is None:
        from ..ai_access.service import AiAccessService

        state.ai_access = AiAccessService(state)
    if not hasattr(state, "_failed_project_contexts"):
        state._failed_project_contexts = {}
    lifecycle = getattr(state, "remote_profile_lifecycle", None)
    state.ctx.remote_profile_lifecycle = lifecycle
    if lifecycle is not None:
        # Also cover embedded/test contexts whose startup does not register itself.
        lifecycle.register_context(state.ctx)
    if hasattr(state, "project_registry") and state.project_contexts:
        return
    if not hasattr(state, "project_lock"):
        state.project_lock = asyncio.Lock()
    if not hasattr(state, "auth"):
        state.auth = state.ctx.auth
    state.project_epoch = uuid4().hex
    state.project_registry = {}
    state.project_roots = {}
    state.project_contexts = {}
    state.desktop_events = DesktopEventSource(state)
    state.browser_bus = browser_bus if browser_bus is not None else EventBus()
    state.ctx.bus.forward_to = state.browser_bus
    state.ctx.work_contexts = lambda: list(state.project_contexts.values())
    _register_project(state, state.ctx)


def _register_project(state: Any, ctx: AppContext) -> ProjectDescriptor:
    """Publish atomically (no await) after successful startup, under project_lock."""
    root = canonical_root(ctx.paths.root)
    descriptor = ProjectDescriptor(uuid4().hex, state.project_epoch, root, ctx)
    state.project_registry[descriptor.project_id] = descriptor
    state.project_roots[root] = descriptor
    # Preserve VS-03's resident-context view for globals and restart guards (§5.2).
    state.project_contexts[root] = ctx
    state.desktop_events.attach(descriptor)
    return descriptor


async def shutdown_workspace(state: Any) -> None:
    """Only server shutdown cancels work in every resident project (§5.1)."""
    reservations = getattr(state, "remote_reservations", None)
    if reservations is not None:
        # Before the first await, including waiting for project_lock. Consumers
        # retain their leases until their own stop completes; never clear here.
        reservations.close_admission()
    # Failed startup contexts have no public descriptor, but remain owned and
    # retryable until shutdown actually joins their consumers
    # (runtime-integration-plan §3.1/I2).
    async with state.project_lock:
        ai_access = getattr(state, "ai_access", None)
        if ai_access is not None:
            await ai_access.close()
        desktop_events = getattr(state, "desktop_events", None)
        if desktop_events is not None:
            await desktop_events.close()
        residents = getattr(state, "project_contexts", {})
        failed = getattr(state, "_failed_project_contexts", {})
        contexts = list(residents.values())
        contexts.extend(ctx for ctx in failed.values() if ctx not in contexts)
        results = await asyncio.gather(*(ctx.shutdown() for ctx in contexts), return_exceptions=True)
        lifecycle = getattr(state, "remote_profile_lifecycle", None)
        for index, (ctx, result) in enumerate(zip(contexts, results)):
            if isinstance(result, BaseException):
                continue
            try:
                if lifecycle is not None:
                    # Embedded contexts may not implement AppContext's cleanup.
                    lifecycle.unregister_context(ctx)
            except Exception as exc:
                results[index] = exc
                continue
            # Never lose a failed stop by clearing the whole registry. Cancellation
            # before gather completes also leaves every context available to retry.
            for root, resident in list(residents.items()):
                if resident is ctx:
                    del residents[root]
                    descriptor = getattr(state, "project_roots", {}).pop(root, None)
                    if descriptor is not None:
                        getattr(state, "project_registry", {}).pop(descriptor.project_id, None)
            for root, pending in list(failed.items()):
                if pending is ctx:
                    del failed[root]
        # Shared by every project, so stopped here rather than by any one of them (§2.1).
        await plugin_envs.shutdown()
        for result in results:
            if isinstance(result, BaseException):
                raise result


async def _ensure_project_locked(
    state: Any, root: str | Path, *, create: bool,
) -> tuple[ProjectDescriptor, bool]:
    """Common ensure/open internals; caller holds the settings/open project_lock."""
    if not hasattr(state, "project_registry"):
        initialise_workspace(state)
    target = canonical_root(root)
    if not target.exists():
        raise ProjectNotFound(f"No such directory: {target}")
    if not target.is_dir():
        raise ProjectNotFound(f"Not a directory: {target}")

    existed = has_project_data(target)
    if not existed and not create:
        raise ProjectNotInitialised(str(target))
    resident = state.project_roots.get(target)
    if resident is not None:
        return resident, existed

    if target in state._failed_project_contexts:
        # A second consumer cannot take over a failed startup's still-live leases.
        raise RuntimeError("Project cleanup is incomplete")
    replacement = AppContext.create(str(target), state.auth)
    replacement.remote_reservations = state.remote_reservations
    replacement.work_contexts = lambda: list(state.project_contexts.values())
    lifecycle = getattr(state, "remote_profile_lifecycle", None)
    replacement.remote_profile_lifecycle = lifecycle
    try:
        # Track starting contexts before any await, not just the selected project.
        if lifecycle is not None:
            lifecycle.register_context(replacement)
        await replacement.startup()
    except BaseException:
        # Never publish a half-started context or mask the original startup failure.
        # Record BEFORE cleanup: failure/cancellation must retain the exact context.
        state._failed_project_contexts[target] = replacement
        try:
            await replacement.shutdown()
            if lifecycle is not None:
                lifecycle.unregister_context(replacement)
            del state._failed_project_contexts[target]
        except BaseException:
            log.exception("Could not clean up failed project startup at %s", target)
        raise
    return _register_project(state, replacement), existed


async def ensure_project(
    state: Any, root: str | Path, *, create: bool = False,
) -> ProjectDescriptor:
    """Start/reuse a project without changing legacy selection or forwarding (§5.3.2)."""
    async with state.project_lock:
        descriptor, _ = await _ensure_project_locked(state, root, create=create)
        return descriptor


async def open_project(state: Any, root: str | Path, *, create: bool = True) -> AppContext:
    """Point the running Studio at ``root``, initialising it if it is not a project yet.

    ``state`` is the ASGI app's state object — it carries the live context and the lock
    that serialises two clicks on "Open project" arriving at once.

    Two orderings matter here:

    * A new context is **built and started before** the selected one changes.
      A config that will not parse or a plugin that will not import must leave the Studio
      on the project it already had, not on nothing at all.
    * The browser bus stays alive across switches. Each context keeps a separate local
      bus for its plugins and automations, including while its tasks run in the background.
    """
    lock: asyncio.Lock = state.project_lock
    async with lock:
        current: AppContext = state.ctx
        descriptor, existed = await _ensure_project_locked(state, root, create=create)
        replacement = descriptor.ctx
        if replacement is current:
            record_recent(current.paths.root, project_label(current))
            return current
        current.bus.forward_to = None
        state.browser_bus.clear_history()
        replacement.bus.forward_to = state.browser_bus
        state.ctx = replacement
        log.info("Opened project %s (was %s)", replacement.paths.root, current.paths.root)
        record_recent(replacement.paths.root, project_label(replacement))
        await state.browser_bus.publish(
            EventType.PROJECT_OPENED,
            {
                "root": str(replacement.paths.root),
                "name": project_label(replacement),
                "data_dir": str(replacement.paths.data),
                "created": not existed,
            },
        )
    return replacement


def project_label(ctx: AppContext) -> str:
    return ctx.config.project_name or ctx.paths.root.name


@dataclass
class _DesktopWatch:
    subscription: Subscription
    task: asyncio.Task[None] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    attention: dict[str, Any] | None = None
    needs_seed: bool = True


class DesktopEventSource:
    """One app-level adapter over resident buses, not another project registry (§5.3.6).

    The workspace registry remains the only owner of identities/contexts. These watches
    own only subscriptions and derived Producer attention; no renderer, selection,
    backend launcher or job lifecycle participates. The small app bus supplies a single
    replay cursor; local source_seq remains diagnostic and may coincide in A and B.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        self.bus = EventBus()
        self._watches: dict[str, _DesktopWatch] = {}
        self._closed = False

    def attach(self, binding: ProjectDescriptor) -> None:
        if self._closed or binding.project_id in self._watches:
            return
        # Registration is synchronous: not even the first post-open event can fall
        # between publication of the resident and subscribing. Startup history is
        # retained too, without changing the context's selected-browser forwarding.
        watch = _DesktopWatch(binding.ctx.bus.subscribe(since_seq=0))
        self._watches[binding.project_id] = watch
        watch.task = asyncio.create_task(self._pump(binding, watch), name="desktop-project-events")

    async def ready(self) -> None:
        # New residents can be registered while a DB snapshot is read. Include them
        # before a socket takes its atomic subscription/hello watermark.
        while pending := [w.ready.wait() for w in self._watches.values() if not w.ready.is_set()]:
            await asyncio.gather(*pending)

    def snapshot(self) -> list[dict[str, Any]]:
        return [watch.attention for watch in self._watches.values() if watch.attention is not None]

    def unavailable(self) -> list[str]:
        """Missing/stale snapshots must be reported as a gap, never as known idle."""
        return [key for key, watch in self._watches.items() if watch.needs_seed]

    def _attention(
        self, binding: ProjectDescriptor, waiting: dict[str, str], source_seq: int,
    ) -> dict[str, Any]:
        return {
            "project": {
                "project_id": binding.project_id, "epoch": binding.epoch,
                "root": str(binding.root),
                "name": project_label(binding.ctx).strip() or binding.root.name or str(binding.root),
            },
            "source_seq": source_seq, "target": "/producer",
            "waiting": [{"chat_id": key, "target": value} for key, value in sorted(waiting.items())],
        }

    @staticmethod
    def _fold(waiting: dict[str, str], event: Event) -> None:
        data = event.data
        if event.type == EventType.TEAM_CHAT_UPDATED:
            chat_id = data.get("id")
            if isinstance(chat_id, str) and chat_id:
                if producer_needs_reply(data):
                    waiting[chat_id] = f"/producer?chat={quote(chat_id, safe='')}"
                else:
                    waiting.pop(chat_id, None)
        elif event.type == EventType.TEAM_CHAT_DELETED:
            chat_id = data.get("chat_id")
            if isinstance(chat_id, str):
                waiting.pop(chat_id, None)

    async def _seed(self, binding: ProjectDescriptor, waiting: dict[str, str]) -> int:
        bus = binding.ctx.bus
        # Reconcile the read with events published while SQLite was awaited. On a
        # retention gap start another read rather than pretending it was complete.
        for _ in range(3):
            before = bus.current_seq
            chats = await binding.ctx.chats.list_chats()
            if bus.replay_gap(before):
                continue
            waiting.clear()
            for chat in chats:
                data = binding.ctx.chats.to_json(chat)
                self._fold(waiting, Event(type=EventType.TEAM_CHAT_UPDATED, data=data))
            for event in bus.replay(before):
                self._fold(waiting, event)
            return bus.current_seq
        raise RuntimeError("Desktop attention snapshot could not catch up")

    async def _pump(self, binding: ProjectDescriptor, watch: _DesktopWatch) -> None:
        waiting: dict[str, str] = {}
        seeded_through = -1
        loop = asyncio.get_running_loop()
        retry_at = 0.0

        async def seed() -> None:
            nonlocal seeded_through
            seeded_through = await self._seed(binding, waiting)
            watch.attention = self._attention(binding, waiting, seeded_through)
            watch.needs_seed = False
            await self.bus.publish(EventType.DESKTOP_PRODUCER_ATTENTION, watch.attention)

        try:
            try:
                await seed()
            except Exception:
                log.exception("Could not seed desktop attention at %s", binding.root)
                retry_at = loop.time() + 1
                await self.bus.publish(EventType.DROPPED, {"reason": "attention_unavailable", "count": 0})
            finally:
                watch.ready.set()
            while True:
                if watch.needs_seed and loop.time() >= retry_at:
                    try:
                        await seed()
                    except Exception:
                        log.debug("Desktop attention retry failed at %s", binding.root, exc_info=True)
                        retry_at = loop.time() + 1
                try:
                    # Retry even on an idle bus, or under continuous unrelated
                    # events. The timeout belongs to this watch and is cancelled
                    # with it; no polling task or resident job is started.
                    if watch.needs_seed:
                        event = await asyncio.wait_for(
                            watch.subscription.get(), max(0.001, retry_at - loop.time()),
                        )
                    else:
                        event = await watch.subscription.get()
                except TimeoutError:
                    continue
                count = watch.subscription.take_dropped()
                if count:
                    try:
                        await seed()
                    except Exception:
                        watch.needs_seed = True
                        retry_at = loop.time() + 1
                        log.exception("Could not recover desktop attention at %s", binding.root)
                    await self.bus.publish(EventType.DROPPED, {"reason": "source_overflow", "count": count})
                if event.type == EventType.AUTOMATION_NOTIFICATION:
                    route = event.data.get("route")
                    target = desktop_target(route)
                    if route is not None and target is None:
                        continue  # A malformed/foreign destination is not made clickable.
                    source = self._attention(binding, waiting, event.seq)
                    await self.bus.publish(EventType.DESKTOP_NOTIFICATION, {
                        "project": source["project"], "source_seq": event.seq, "target": target,
                        "notification": {key: value for key, value in event.data.items() if key in {
                            "automation_id", "automation_name", "title", "message", "trigger", "values",
                        }},
                    })
                elif event.seq > seeded_through and event.type in {
                    EventType.TEAM_CHAT_UPDATED, EventType.TEAM_CHAT_DELETED,
                }:
                    before = waiting.copy()
                    self._fold(waiting, event)
                    if waiting != before:
                        watch.attention = self._attention(binding, waiting, event.seq)
                        await self.bus.publish(EventType.DESKTOP_PRODUCER_ATTENTION, watch.attention)
        finally:
            watch.ready.set()
            watch.subscription.close()

    async def close(self) -> None:
        """Release event resources only. Closing a receiver never stops resident jobs."""
        if self._closed:
            return
        self._closed = True
        tasks = []
        for watch in self._watches.values():
            watch.subscription.close()
            if watch.task is not None:
                watch.task.cancel()
                tasks.append(watch.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._watches.clear()


# ---------------------------------------------------------------- desktop exit guard


def _activity_status(counts: dict[str, int], errors: list[str]) -> str:
    if any(counts.values()):
        return "active"
    return "unknown" if errors else "idle"


def _project_activity(descriptor: ProjectDescriptor) -> dict[str, Any]:
    ctx = descriptor.ctx
    errors: list[str] = []
    observed: dict[str, set[str]] = {
        kind: set() for kind in (
            "pipeline_runs", "tasks", "chats", "subagents", "other_jobs", "commit_drafts",
        )
    }
    kinds = {
        "pipeline_run": "pipeline_runs", "agent_task": "tasks",
        "agent_chat": "chats", "subagent": "subagents",
    }
    try:
        # The engine covers queued/slot-waiting, paused pipelines and finishing
        # task/chat turns too. Do NOT use their UI is_running predicates: those
        # intentionally hide _ending while cleanup/child collection is still live.
        # Retain handles captured before unregister, even if they finish meanwhile.
        for job in tuple(ctx.jobs.jobs()):
            observed[kinds.get(job.kind, "other_jobs")].add(job.id)
    except Exception:
        errors.append("jobs")
        log.warning("Could not snapshot project jobs at %s", descriptor.root, exc_info=True)
    try:
        # Workers also have a parent-run registry. Union by id rather than counting
        # them twice; it remains useful while the parent's slot is suspended.
        for run in tuple(ctx.teams.subagents.runs.values()):
            for child_id, task in tuple(run.jobs.items()):
                if not task.done():
                    observed["subagents"].add(child_id)
    except Exception:
        errors.append("subagents")
        log.warning("Could not snapshot project subagents at %s", descriptor.root, exc_info=True)
    try:
        # These model calls deliberately live outside JobEngine (not agent runs).
        # Usage-limit timers and dormant Producer watches are NOT running work.
        for task_id, task in tuple(ctx.teams._commit_jobs.items()):
            if not task.done():
                observed["commit_drafts"].add(task_id)
    except Exception:
        errors.append("commit_drafts")
        log.warning("Could not snapshot project commit drafts at %s", descriptor.root, exc_info=True)
    try:
        name = project_label(ctx)
    except Exception:
        name = descriptor.root.name
        errors.append("name")
    counts = {kind: len(ids) for kind, ids in observed.items()}
    return {
        "project_id": descriptor.project_id, "root": str(descriptor.root), "name": name,
        "status": _activity_status(counts, errors), "counts": counts, "errors": errors,
    }


def workspace_activity(state: Any) -> dict[str, Any]:
    """Read-only, conservative exit/updater summary of resident work (§5.3.1 D3).

    Call on the application's event loop: there are no awaits, DB/file reads, context
    startups or selection changes. Copy the published registry, not recent paths or
    tabs. Async completions/open/shutdown cannot interleave with this snapshot. It is
    advisory, NOT a shutdown lock: new work may start after the response. Consumers
    must use requires_confirmation, including when status is unknown; counts are
    observed lower bounds when errors is nonempty. An unavailable/empty registry is
    never proof of idle (e.g. before startup or after shutdown).
    """
    errors: list[str] = []
    projects: list[dict[str, Any]] = []
    try:
        # Startup may already have autostart work before publishing its context;
        # shutdown/settings also hold this lock. Never await it or claim global
        # idle during a transition. This does not change VS-03's lifecycle.
        lock = getattr(state, "project_lock", None)
        if lock is not None and lock.locked():
            errors.append("project_transition")
    except Exception:
        errors.append("project_transition")
    try:
        descriptors = tuple(state.project_registry.values())
        if not descriptors:
            errors.append("project_registry")
    except Exception:
        errors.append("project_registry")
        log.warning("Could not snapshot resident projects", exc_info=True)
        descriptors = ()
    for descriptor in descriptors:
        try:
            projects.append(_project_activity(descriptor))
        except Exception:
            # One broken context must not hide the rest of the resident registry.
            projects.append({
                "project_id": descriptor.project_id, "root": str(descriptor.root),
                "name": descriptor.root.name, "status": "unknown", "counts": {},
                "errors": ["context"],
            })
            log.warning("Could not snapshot project at %s", descriptor.root, exc_info=True)
    status = "idle"
    if errors or any(project["errors"] for project in projects):
        status = "unknown"
    if any(project["status"] == "active" for project in projects):
        status = "active"
    return {
        "epoch": getattr(state, "project_epoch", None), "status": status,
        "requires_confirmation": status != "idle", "projects": projects, "errors": errors,
    }


# ---------------------------------------------------------------- recently opened


@dataclass(frozen=True, slots=True)
class RecentProject:
    root: Path
    name: str
    opened_at: datetime

    def to_json(self) -> dict[str, Any]:
        # `exists` and `initialised` are resolved on read, not stored: a project the user
        # has since moved or deleted should come back as a dead entry they can clear,
        # never as a path the UI happily offers to open.
        return {
            "root": str(self.root),
            "name": self.name,
            "opened_at": self.opened_at.isoformat(),
            "exists": _is_dir(self.root),
            "initialised": has_project_data(self.root),
            "task_counts": read_task_counts(self.root),
        }


def recent_file() -> Path:
    return user_config_dir() / RECENT_FILENAME


def load_recent() -> list[RecentProject]:
    """Most recently opened first. A malformed file is an empty list, never an error —
    this is a convenience list, and losing it must not stop the Studio from starting."""
    path = recent_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        log.warning("Ignoring unreadable recent-projects file at %s", path, exc_info=True)
        return []

    entries: list[RecentProject] = []
    for item in raw.get("projects", []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict) or not item.get("root"):
            continue
        try:
            opened_at = datetime.fromisoformat(str(item.get("opened_at")))
        except ValueError:
            opened_at = utcnow()
        root = Path(str(item["root"]))
        entries.append(
            RecentProject(root=root, name=str(item.get("name") or root.name), opened_at=opened_at)
        )
    return entries[:MAX_RECENT]


def record_recent(root: Path, name: str) -> list[RecentProject]:
    """Move ``root`` to the front of the list, de-duplicating what is already there."""
    entry = RecentProject(root=Path(root), name=name, opened_at=utcnow())
    kept = [item for item in load_recent() if not _same_path(item.root, entry.root)]
    return _write_recent([entry, *kept][:MAX_RECENT])


def forget_recent(root: Path | str) -> list[RecentProject]:
    target = Path(root)
    return _write_recent([item for item in load_recent() if not _same_path(item.root, target)])


def _write_recent(entries: list[RecentProject]) -> list[RecentProject]:
    path = recent_file()
    payload = {
        "projects": [
            {"root": str(e.root), "name": e.name, "opened_at": e.opened_at.isoformat()}
            for e in entries
        ]
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written through a temporary file so an interrupted write cannot leave a
        # truncated list behind — `load_recent` would then silently start from scratch.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            temp_name = handle.name
        os.replace(temp_name, path)
    except OSError:
        log.warning("Could not write the recent-projects list to %s", path, exc_info=True)
    return entries


def _same_path(left: Path, right: Path) -> bool:
    """Windows paths differ only in case; comparing the raw strings would list one
    project twice the first time the user typed the drive letter in lower case."""
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        # A disconnected network share raises rather than answering False.
        return False


# ---------------------------------------------------------------- which project starts


@dataclass(frozen=True, slots=True)
class StartupProject:
    """Where the Studio starts, and how that was decided."""

    root: Path
    source: str
    """``"argument"``, ``"recent"`` or ``"demo"`` — what the CLI reports it did."""


def resolve_startup_project(explicit: str | Path | None = None) -> StartupProject:
    """Which managed project a starting Studio opens (§5.1).

    A path on the command line wins, and is returned unchecked: whether it exists is the
    caller's error to report, not something to quietly paper over with another project.

    With no path, the Studio reopens what it had open last. That is the top of the recent
    list, and entries are skipped until one is still on disk — a project moved or deleted
    since must not hold the Studio hostage on every later start. Nothing left to reopen
    means a first run, and that opens the built-in demo project instead of the working
    directory: a shell that happens to be somewhere is not a statement about which project
    to initialise.
    """
    if explicit is not None:
        return StartupProject(ProjectPaths.for_project(explicit).root, "argument")

    for entry in load_recent():
        if _is_dir(entry.root):
            return StartupProject(ProjectPaths.for_project(entry.root).root, "recent")
        log.info("Skipping recently opened %s: it is not there any more", entry.root)

    return StartupProject(ensure_demo_project().root, "demo")
