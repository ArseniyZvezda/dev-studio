"""Application context — one object holding every long-lived service.

Built once during FastAPI's lifespan and reachable from routes via
``request.app.state.ctx``, which keeps the wiring explicit and avoids module globals.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from ..automations.engine import AutomationEngine
from ..automations.store import AutomationStore
from ..engine.jobs import JobEngine
from ..engine.runner import PipelineRunner
from ..engine.store import PipelineStore
from ..explorer.service import ExplorerService
from ..explorer.watcher import FileWatcher
from ..plugins import loader as plugin_loader
from ..plugins.api import PluginHost
from ..plugins.registry import PluginHook, Registry
from ..teams import skill as studio_skill
from ..teams.authoring import ProjectAuthoring
from ..teams.chat import ChatService
from ..teams.docs import DocStore
from ..teams.orchestrator import TeamOrchestrator
from ..teams.project_skills import SkillStore, check_new_name
from ..updates import UpdateService, running_work
from ..vcs import GitBackend, VcsBackend, VcsError, detect_backend
from ..ws.bus import Event, EventBus, Subscription
from ..ws.events import EventType
from .config import AppConfig, effective_launch_root, load_config, save_config
from .data_migration import finish_data_migration
from .db import Database
from .paths import ProjectPaths
from .project import ensure_project, is_git_repo
from .security import AuthSettings

log = logging.getLogger(__name__)


class AppContext:
    def __init__(
        self,
        paths: ProjectPaths,
        config: AppConfig,
        auth: AuthSettings,
        *,
        bus: EventBus | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.auth = auth

        self.db = Database(paths.db_file)
        self.bus = bus or EventBus()
        """Project-local events; workspace forwards the selected project's events to the
        persistent browser bus (§5.1). Plugins and automations never cross projects."""
        self.work_contexts: Callable[[], list[AppContext]] = lambda: [self]
        """Workspace supplies all resident contexts for the restart guard (§5.1, §12.1)."""
        self.jobs = JobEngine(config.concurrency.as_dict())
        self.registry: Registry = plugin_loader.load_all(paths, config)
        self.pipelines = PipelineStore(paths)
        self.explorer = ExplorerService(paths, config)
        self.watcher = FileWatcher(paths, config, self.bus)
        self.runner = PipelineRunner(
            paths=paths,
            db=self.db,
            bus=self.bus,
            jobs=self.jobs,
            registry=self.registry,
            config=config,
        )
        self.docs = DocStore(paths, self.bus)
        self.skills = SkillStore(paths, builtins=self._builtin_skills)
        """``.voxarium_studio/skills/`` — the Skills page's folder, and what members are given (§10.10)."""
        self.teams = TeamOrchestrator(
            paths=paths,
            db=self.db,
            bus=self.bus,
            jobs=self.jobs,
            config=config,
            docs=self.docs,
            skills=self.skills,
        )
        self.chats = ChatService(self.teams)
        self._teams_ready = False
        """Chats with a member outside the board (§10.9) — the orchestrator's session
        machinery with the user at the other end, so it is built over it."""
        self.updates = UpdateService(
            bus=self.bus,
            config=config.updates,
            # Restart affects background projects too (§5.1, §12.1). Resolve their jobs
            # when somebody presses the button, including contexts opened since startup.
            busy=lambda: running_work([
                job.kind for ctx in self.work_contexts() for job in ctx.jobs.jobs()
            ]),
        )
        self.automation_store = AutomationStore(paths)
        self.teams.authoring = ProjectAuthoring(
            self.pipelines, self.automation_store, self.skills, lambda: self.registry, self.bus,
        )
        self.automations = AutomationEngine(self)
        """``.voxarium_studio/automations.yaml`` and what runs it off the bus (§6.5)."""
        self.is_git_repo = is_git_repo(paths.root)
        self.vcs: VcsBackend | None = None
        """The version control system the project is under — resolved in :meth:`startup`,
        because detection runs a command (§9.6, §10.5)."""

        self._plugin_state: dict[str, dict[str, Any]] = {}
        """``PluginHost.state`` per plugin — what a plugin keeps across a reload of its own
        code, for as long as this project is open (§9.7)."""
        self._plugin_tasks: set[asyncio.Task[Any]] = set()
        """Every hook run and everything a plugin ``spawn``ed, so closing the project or
        stopping the server cancels them rather than leaving them on the loop."""
        self._plugin_pump: asyncio.Task[None] | None = None
        self._plugin_events: Subscription | None = None
        self._opened_for: set[str] = set()
        """Plugins whose *project opened* hooks have run in this context. A reload after a
        settings save must not run them again; enabling a plugin must."""

    def _builtin_skills(self) -> dict[str, tuple[Path, str]]:
        """Package sources stay live across plugin reloads; nothing is copied into projects (§10.10)."""
        result = {
            studio_skill.SKILL_NAME: (
                studio_skill.BUNDLE / "skills" / studio_skill.SKILL_DIRECTORY, "voxarium_studio",
            ),
        }
        for plugin in self.registry.plugins():
            root = plugin.manifest.root
            if plugin.status != "loaded" or root is None:
                continue
            root = root.resolve()
            for relative in plugin.manifest.skills:
                directory = (root / relative).resolve()
                try:
                    check_new_name(directory.name)
                    if not directory.is_relative_to(root) or not (directory / "SKILL.md").is_file():
                        raise ValueError("skill must be a folder with SKILL.md inside the plugin")
                    if directory.name in result:
                        raise ValueError("skill name is already provided by another package")
                except ValueError as exc:
                    log.warning("Plugin %s skill %r: %s", plugin.id, relative, exc)
                    continue
                result[directory.name] = (directory, plugin.id)
        return result

    @classmethod
    def create(
        cls, project_root: str, auth: AuthSettings, *, bus: EventBus | None = None
    ) -> "AppContext":
        paths, config = ensure_project(project_root)
        return cls(paths, config, auth, bus=bus)

    async def startup(self) -> None:
        await asyncio.to_thread(self.docs.migrate_folder_styles)
        await self.db.create_all()
        await finish_data_migration(self)
        # The schema step is in two halves with the task-number backfill between them, and
        # the order is the whole point: a project opened by an older Studio has every task
        # at number 0 (§10.2), and the unique index that makes the number an identity
        # cannot exist until they differ.
        await self.teams.ensure_task_numbers()
        await self.db.create_missing_indexes()
        await self.detect_vcs()
        await self.teams.sync_definitions()
        await self.teams.subagents.recover()
        self._teams_ready = True
        await self.chats.producer.restore_paused()
        await self.chats.producer.start()
        # A usage-limit wait outlives the process it was armed in — the window it waits for
        # is hours long — so the timers are rebuilt from the rows that still carry one
        # (§10.3). One already due fires as soon as the loop gets to it.
        await self.teams.resume_limit_waits()
        await self.teams.resume_autostart_tasks()
        self.watcher.start()
        await self.updates.start()
        await self.automations.start()
        # Last, once everything a hook might want is up: the hooks run in the background,
        # so the project is ready before any of them has done anything (§9.7).
        self._start_plugin_hooks()
        log.info("Voxarium Studio ready for project %s", self.paths.root)

    async def shutdown(self) -> None:
        self.chats.closing = True
        await self.chats.producer.shutdown()
        if self._teams_ready:
            await self.chats.producer.restore_paused()
        await self._stop_plugin_hooks()
        await self.automations.stop()
        await self.updates.stop()
        await self.watcher.stop()
        self.teams.cancel_limit_waits()
        await self.teams.cancel_commit_jobs()
        await self.jobs.shutdown()
        await self.db.close()

    # ---- plugin hooks (§9.7) ----

    @property
    def launch_root(self) -> Path:
        """Where an agent session would start today (``teams.launch_root``, §10.5)."""
        return effective_launch_root(self.paths.root, self.config.teams.launch_root)

    def plugin_host(self, plugin_id: str) -> PluginHost:
        """The object a plugin's hook, action or search provider is handed — built fresh
        per call so it always reads the current config, over state that persists."""
        return PluginHost(
            plugin_id=plugin_id,
            paths=self.paths,
            config=lambda: self.config,
            bus=self.bus,
            state=self._plugin_state.setdefault(plugin_id, {}),
            spawn=self._track,
        )

    def _track(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(self._supervised(coro, name), name=name)
        self._plugin_tasks.add(task)
        task.add_done_callback(self._plugin_tasks.discard)
        return task

    @staticmethod
    async def _supervised(coro: Coroutine[Any, Any, Any], name: str) -> Any:
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            # Logged here rather than surfacing as "Task exception was never retrieved" at
            # interpreter exit: a plugin's background failure is the plugin's, and the
            # Studio carries on.
            log.exception("Plugin task %s failed", name)
            return None

    def _start_plugin_hooks(self) -> None:
        """Subscribe the hook pump to the bus and run the *project opened* hooks.

        Idempotent on the pump; the opened hooks run once per plugin per context (see
        :attr:`_opened_for`), which is what lets :meth:`reload_plugins` call this again
        after an enable without re-running every plugin that was already there.
        """
        if self._plugin_pump is None:
            self._plugin_events = self.bus.subscribe()
            self._plugin_pump = asyncio.create_task(self._pump_plugin_events(), name="plugin-hooks")
        loaded = {plugin.id for plugin in self.registry.plugins() if plugin.status == "loaded"}
        # A plugin disabled since is forgotten, so enabling it again is an opening.
        self._opened_for &= loaded
        for hook in self.registry.project_opened_hooks():
            if hook.plugin_id not in self._opened_for:
                self._run_hook(hook, None)
        self._opened_for |= loaded

    async def _pump_plugin_events(self) -> None:
        subscription = self._plugin_events
        assert subscription is not None
        try:
            async for event in subscription.stream():
                # The *current* registry's hooks, so a reload takes effect on the next
                # event and a disabled plugin's hook is simply not there any more.
                for hook in self.registry.event_hooks(event.type):
                    self._run_hook(hook, event)
        finally:
            subscription.close()

    def _run_hook(self, hook: PluginHook, event: Event | None) -> None:
        async def run() -> None:
            host = self.plugin_host(hook.plugin_id)
            if event is None:
                await hook.handler(host)
            else:
                await hook.handler(host, event)

        self._track(run(), f"plugin-hook:{hook.plugin_id}:{hook.id}")

    async def _stop_plugin_hooks(self) -> None:
        tasks = [task for task in (self._plugin_pump, *self._plugin_tasks) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._plugin_pump = None
        self._plugin_events = None
        self._plugin_tasks.clear()

    def plugin_tasks(self) -> list[asyncio.Task[Any]]:
        """What plugins have running in the background right now — for tests and diagnostics."""
        return [task for task in self._plugin_tasks if not task.done()]

    # ---- version control ----

    async def detect_vcs(self) -> VcsBackend | None:
        """Resolve which system the project is under and hand it to the orchestrator.

        VCS entities are independent of the plugin registry (§9.6).
        """
        if self.config.vcs.disabled:
            self.vcs = None
        else:
            self.vcs = await detect_backend(
                self.paths.root,
                configured=self.config.vcs.selected(),
                settings_for=self.vcs_settings,
            )
        self.teams.vcs = self.vcs
        if isinstance(self.vcs, GitBackend) and self.paths.worktrees.is_dir():
            try:
                await self.vcs.repair_moved_worktrees(self.paths.worktrees)
            except VcsError as exc:
                log.warning("Could not repair moved Git worktrees in %s: %s", self.paths.worktrees, exc)
        if self.vcs is None:
            log.info("Project %s is not under recognised version control", self.paths.root)
        else:
            log.info("Project %s is under %s", self.paths.root, self.vcs.name)
        return self.vcs

    def vcs_settings(self, backend_id: str) -> dict[str, Any]:
        """Settings owned by one independent Version Control card (§9.6)."""
        return dict(self.config.vcs.settings.get(backend_id, {}))

    # ---- task ids ----

    @property
    def task_code(self) -> str:
        """The code every task id is displayed with — ``VS`` in ``VS-01`` (§10.2).

        Chosen in Settings; derived from the project's name until somebody does.
        """
        return self.config.task_code(self.paths.root.name)

    # ---- config ----

    def save_config(self) -> None:
        save_config(self.paths, self.config)

    def reload_config(self) -> AppConfig:
        self.config = load_config(self.paths, migrate_application=True)
        self.jobs.update_limits(self.config.concurrency.as_dict())
        self.explorer.config = self.config
        self.watcher.config = self.config
        self.runner.config = self.config
        self.teams.config = self.config
        # `updates` is deliberately not assigned here: it has a poller to start or stop, so
        # it is told through `configure()` by the caller, which can await (§12.1).
        return self.config

    async def reload_plugins(self) -> Registry:
        """Rebuild the registry after an install/enable/disable and tell clients."""
        self.registry = plugin_loader.load_all(self.paths, self.config)
        self.runner.registry = self.registry
        await self.detect_vcs()
        # A plugin enabled just now has its *project opened* hooks to run; one that was
        # merely re-registered after a settings save does not (§9.7). Only once the context
        # is started — a reload before that is a test building a registry, not a project.
        if self._plugin_pump is not None:
            self._start_plugin_hooks()
        await self.bus.publish(
            EventType.PLUGIN_CHANGED,
            {
                "plugins": [p.to_json() for p in self.registry.plugins()],
                "node_type_count": len(self.registry.node_types()),
            },
        )
        return self.registry
