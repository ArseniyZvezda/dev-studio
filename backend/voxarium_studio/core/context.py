"""Application context — one object holding every long-lived service.

Built once during FastAPI's lifespan and reachable from routes via
``request.app.state.ctx``, which keeps the wiring explicit and avoids module globals.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..automations.engine import AutomationEngine
from ..automations.store import AutomationStore
from ..engine.jobs import JobEngine
from ..engine.runner import PipelineRunner
from ..engine.store import PipelineStore
from ..explorer.service import ExplorerService
from ..explorer.watcher import FileWatcher
from ..plugins import envs as plugin_envs
from ..plugins import loader as plugin_loader
from ..plugins.api import PluginHost
from ..plugins.registry import PluginHook, Registry
from ..sync import RemoteSync, credentials
from ..sync.protocol import check_space_name, normalise_url
from ..sync.service import ActivatedRemoteTarget, RemoteStartupBinding, preflight_remote_binding
from ..sync.transitions import BindingTransitions
from ..teams import skill as studio_skill
from ..teams.authoring import ProjectAuthoring
from ..teams.chat import ChatService
from ..teams.docs import DocStore
from ..teams.memory import MemoryStore
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

if TYPE_CHECKING:
    from ..sync.profile_lifecycle import ProfileLifecycle
    from ..sync.reservations import ReservationLease, ReservationOwner

log = logging.getLogger(__name__)


class _ProfileLease(Protocol):
    def release(self) -> None: ...


def _completed_enable(paths: ProjectPaths) -> tuple[dict, str] | None:
    """The completed I12 Enable record that published the CURRENT config, if any.

    Returns ``(record, config_fingerprint)``; ``None`` means no journal exists at
    all. Raises when journals exist but do not prove an activation: unresolved
    (recovery_required), corrupt/lost, or the current (ID, R) was produced by a
    disabled I6 change or by nothing this root recorded (activation_unavailable).
    """
    from ..sync.bindings import BindingError
    from ..sync.transitions import (ENABLE_JOURNAL_VERSION, BindingChangeError, completed_activation,
                                    journal_unresolved, read_binding_change_journals)
    from .config import _project_config_document

    records = read_binding_change_journals(paths.root)
    if not records:
        return None
    if any(journal_unresolved(row) for row in records):
        # Before reading config: a half-published Enable is never reinterpreted.
        raise BindingChangeError("recovery_required")
    try:
        _, config, fingerprint = _project_config_document(paths)
        identity = config.project_identity
        if fingerprint is None or identity is None:
            raise ValueError
    except Exception:
        raise BindingError("activation_unavailable") from None
    record = completed_activation(records, persistent_id=identity.id, identity_revision=identity.revision)
    if record is None or record["version"] != ENABLE_JOURNAL_VERSION:
        raise BindingError("activation_unavailable")
    return record, fingerprint


def _read_remote_sync_activation(paths: ProjectPaths) -> ActivatedRemoteTarget | None:
    """Classify the owning I6/I12 journals without repairing or enabling anything.

    Completed evidence belongs to the exact canonical root, not to a private
    binding row or today's portable config (runtime-integration-plan §2.3/3.5;
    studio-design §7). In particular edit_id/unbind remove that row, and a VCS
    checkout can restore sync.space: neither permits a legacy runner again.
    Pending, corrupt and known-lost journals refuse through the same I2 seam;
    an unresolved I6/I12 record refuses as ``recovery_required``.

    I12: ONLY a completed Enable record whose committed (ID, R) is the current
    config's yields a target; a disabled I6 commit never does. The target's
    config fingerprint is the current bytes' (unrelated settings saves keep
    the target); preflight still checks every identity/URL/enabled/private/
    checkpoint component against it before any credential or socket.
    A canonical config without legacy sync.space also refuses if the journal
    was lost. Identity metadata WITH an explicit legacy space (including the
    fresh empty default) remains genuinely unactivated legacy.
    """
    from ..sync.bindings import BindingError
    from .config import _project_config_document

    enabled = _completed_enable(paths)
    if enabled is not None:
        record, fingerprint = enabled
        commit = record["commit"]
        return ActivatedRemoteTarget(
            canonical_root=record["canonical_root"], config_fingerprint=fingerprint,
            profile_id=commit["binding"]["profile_id"], url=commit["url"],
            persistent_id=commit["persistent_id"], identity_revision=commit["identity_revision"],
            space_instance_id=commit["binding"]["space_instance_id"],
        )
    try:
        # Read exact disk bytes with the existing duplicate/merge-strict owner;
        # load_config's defaults and the effective model cannot prove legacy.
        raw, _, fingerprint = _project_config_document(paths)
        if fingerprint is None or "sync" not in raw:
            return None
        settings = raw["sync"]
        if (not isinstance(settings, dict)
                or ("enabled" in settings and type(settings["enabled"]) is not bool)
                or ("url" in settings and type(settings["url"]) is not str)):
            raise BindingError("activation_unavailable")
        if settings.get("url"):
            normalise_url(settings["url"])
        if "space" in settings:
            if type(settings["space"]) is not str:
                raise BindingError("activation_unavailable")
            return None
        # Canonical identity + sync/url/enabled without space is format evidence,
        # not permission to connect. Partial target-bearing forms fail closed as
        # well: missing identity/fields must not be repaired into legacy defaults.
        if (raw.get("project_identity") is not None
                or settings.get("url") or settings.get("enabled")):
            raise BindingError("activation_unavailable")
    except BindingError:
        raise
    except Exception:
        raise BindingError("activation_unavailable") from None
    return None


def _read_remote_verified_account(paths: ProjectPaths, profile_id: str, url: str) -> str | None:
    """I12: the account Enable verified through ``/api/v2/me`` for this target.

    Enable records ``me.user.id`` in its completed journal before the runner may
    start, so the very first hello (and every reconnect, also after restart) is
    checked against it instead of pinning whatever the first hello said (§6).
    ``None`` only when no Enable proves an activation for exactly this profile
    and URL; unreadable/unresolved evidence raises (the runner then blocks).
    """
    enabled = _completed_enable(paths)
    if enabled is None:
        return None
    record, _ = enabled
    binding = record["commit"]["binding"]
    if binding["profile_id"] != profile_id or binding["url"] != url:
        return None
    return record["expected"]["account_id"]


def _activation_refusal(error: BaseException) -> str | None:
    """Typed blocked reason for an unreadable activation, else the generic one."""
    code = getattr(error, "code", None)
    return code if code == "recovery_required" else None


def _same_binding_target(current: RemoteStartupBinding, captured: RemoteStartupBinding) -> bool:
    """Same transport target, ignoring the runner's own checkpoint progress (I10).

    The whole-store private fingerprint is not compared: another root's row
    changing is not drift here, while this root's row is re-checked exactly by
    preflight's ``require_confirmed``. Profile labels/revisions stay excluded.
    I12: neither are the config bytes themselves — preflight re-reads the disk
    identity/URL/enabled against the target on every observation, and an
    unrelated settings save must not end a connected target.
    """
    return (replace(current.target, config_fingerprint="") == replace(captured.target, config_fingerprint="")
            and current.namespace == captured.namespace
            and current.enabled == captured.enabled and current.checkpoint is not None
            and current.checkpoint.space_instance_id == captured.target.space_instance_id)


class _ProfileRemoteSync(RemoteSync):
    """Fence attach/reconnect without holding a thread lock across awaits (§5.2.1).

    A failed start/stop retains its logical lease: only a successful runner stop
    proves it cannot reconnect. RemoteSync's internal retries share that lease.
    """

    def __init__(
        self,
        paths: ProjectPaths,
        config: Callable[[], AppConfig],
        docs: DocStore,
        bus: EventBus,
        *,
        lifecycle: Callable[[], ProfileLifecycle | None],
        reservations: Callable[[], ReservationOwner | None] = lambda: None,
        activation: Callable[[], ActivatedRemoteTarget | None] | None = None,
        profile_reader: Callable[[str], Any] | None = None,
        profile_token: Callable[[str, str], str | None] | None = None,
        verified_account: Callable[[str, str], str | None] | None = None,
    ) -> None:
        super().__init__(paths, config, docs, bus)
        # I10: the v2 runner's key is the S2 profile credential for the binding's exact
        # profile + URL — never the legacy per-URL token. Looked up at call time.
        self._profile_token = profile_token or (
            lambda profile_id, url: credentials.load_profile_token(profile_id, url))
        # The account ID a catalog verification proved for that profile, or None when
        # no such observation is available (then the runner pins the first hello's).
        self._verified_account = verified_account or (lambda _profile_id, _url: None)
        self._profile_lifecycle = lifecycle
        self._profile_lease: _ProfileLease | None = None
        self._profile_transition = asyncio.Lock()
        self._stopping_service = False
        self._cleanup_failed = False
        self._reservations = reservations
        self._activation = (activation if activation is not None
                            else lambda: _read_remote_sync_activation(paths))
        self._activated_model_seen = False
        self._reservation_owner: ReservationOwner | None = None
        self._reservation_lease: ReservationLease | None = None
        self._startup_binding: RemoteStartupBinding | None = None
        from .paths import user_config_dir
        from ..sync.profiles import RemoteProfiles
        self._remote_home = user_config_dir()
        self._profile_reader = profile_reader or RemoteProfiles().get

    async def start(self) -> None:
        async with self._profile_transition:
            if self._cleanup_failed:
                raise RuntimeError("sync_cleanup_required")
            await super().start()

    async def stop(self) -> None:
        async with self._profile_transition:
            self._stopping_service = True
            try:
                await super().stop()
            except BaseException:
                self._cleanup_failed = True
                raise
            else:
                self._release_ownership()
                self._cleanup_failed = False
            finally:
                self._stopping_service = False

    async def reconfigure(self) -> dict[str, Any]:
        # Serialize the entire stop/start pair, including watcher cleanup in stop.
        # Private runner overrides do not reacquire this asyncio-only lock.
        async with self._profile_transition:
            if self._cleanup_failed:
                raise RuntimeError("sync_cleanup_required")
            if (self._startup_binding is not None and self._runner is not None
                    and not self._runner.done()):
                try:
                    unchanged = _same_binding_target(self._preflight(self._activation()),
                                                     self._startup_binding)
                except Exception:
                    unchanged = False
                if unchanged:
                    # Profile names/revisions are deliberately not transport
                    # identity: a metadata refresh must not reconnect (§3.1).
                    return self.status()
            return await super().reconfigure()

    async def _start_runner(self) -> None:
        refusal = None
        try:
            target = self._activation()
        except Exception as error:
            self._activated_model_seen = True
            target = None  # Unreadable activation never becomes legacy fallback.
            refusal = _activation_refusal(error)
        if target is not None or self._activated_model_seen:
            self._activated_model_seen = True
            await self._start_activated(target, refusal=refusal)
            return
        # A repeated start must not overwrite a runner/lease that can still reconnect.
        # Reconfigure already stops first, so ordinarily this branch does nothing.
        if self._profile_lease is not None or self._runner is not None:
            await self._stop_runner()
        lifecycle = self._profile_lifecycle()
        settings = self._config().sync
        if lifecycle is not None and settings.enabled and settings.url:
            try:
                normalise_url(settings.url)
                check_space_name(settings.space)
            except ValueError:
                # Let the base service report its existing unconfigured diagnostic.
                # Invalid settings cannot start a runner; no key/network read here.
                pass
            else:
                # Outside the validation catch: actual admission refusal must propagate.
                self._profile_lease = lifecycle.attach(root=str(self.paths.root), url=settings.url)
        await super()._start_runner()

    def _may_inspect_token(self) -> bool:
        # status()/status events must not bypass preflight and read a legacy key.
        if self._activated_model_seen:
            return False
        try:
            legacy = self._activation() is None
        except Exception:
            legacy = False
        if not legacy:
            self._activated_model_seen = True
        return legacy

    def _preflight(self, target: ActivatedRemoteTarget | None) -> RemoteStartupBinding:
        return preflight_remote_binding(self.paths, self._config(), target,
                                        home=self._remote_home, profile_reader=self._profile_reader)

    def _activated_runner(self, binding: RemoteStartupBinding) -> Coroutine[Any, Any, None] | None:
        """The I10 v2 runner for THIS captured binding; synchronous publication only.

        Never the v1 runner or the legacy token: the key provider and verified
        account are bound to ``binding.target.profile_id``/``url``, and drift is a
        re-observation compared with the captured target, never a new target.
        Nothing is read here; the runner reads the key per connect attempt.
        """
        target = binding.target
        profile_id, url = target.profile_id, target.url
        return self._run_activated(
            binding,
            credential=lambda: self._profile_token(profile_id, url),
            account=lambda: self._verified_account(profile_id, url),
            drift=lambda: self._binding_drift(binding),
        )

    def _binding_drift(self, binding: RemoteStartupBinding) -> str | None:
        """A stop code when disk/private/profile no longer describe *binding* (I10)."""
        from ..sync.bindings import BindingError
        from ..sync.state import SyncStateError

        try:
            current = self._preflight(self._activation())
        except (BindingError, SyncStateError) as exc:
            return exc.code
        except Exception as exc:
            return _activation_refusal(exc) or "activation_unavailable"
        if current.checkpoint is None:
            return "preview_required"
        if not _same_binding_target(current, binding):
            return "binding_changed"
        if not current.enabled:
            return "disabled"
        return None

    async def _start_activated(self, target: ActivatedRemoteTarget | None, *,
                               refusal: str | None = None) -> None:
        from ..sync.bindings import BindingError
        from ..sync.reservations import ReservationError
        from ..sync.state import SyncStateError

        # Idempotent start reuses this consumer. Reconfigure explicitly stops it. A
        # runner that already ended (terminal close, drift) is replaced, not reused.
        if self._startup_binding is not None and self._runner is not None and not self._runner.done():
            return
        if self._profile_lease is not None or self._runner is not None:
            await self._stop_runner()
        if target is None and refusal is not None:
            # I12: an unresolved Enable/commit journal. No automatic retry, repair
            # or fresh reset; only an explicit recovery can change this state.
            await self._set_status("blocked", refusal)
            return
        try:
            binding = self._preflight(target)
        except (BindingError, SyncStateError) as exc:
            await self._set_status("blocked", exc.code)
            return
        if binding.checkpoint is None:
            await self._set_status("blocked", "preview_required")
            return
        if not binding.enabled:
            await self._set_status("disabled")
            return
        lifecycle, owner = self._profile_lifecycle(), self._reservations()
        if lifecycle is None or owner is None:
            await self._set_status("blocked", "consumer_lifecycle_unavailable")
            return
        lease = lifecycle.attach(root=binding.target.canonical_root, url=binding.target.url,
                                 profile_id=binding.target.profile_id)
        try:
            reservation = owner.reserve(root=binding.target.canonical_root, url=binding.target.url,
                                        persistent_id=binding.target.persistent_id, consumer=self)
        except ReservationError as exc:
            lease.release()  # Only THIS attempted attach, never the winner's capability.
            await self._set_status("blocked", exc.code)
            return
        except BaseException:
            lease.release()
            raise
        self._profile_lease = lease
        self._reservation_owner, self._reservation_lease = owner, reservation
        # No await between observation, admission/recheck and task publication.
        # Pure profile label/revision changes intentionally do not affect this CAS.
        rejected = None
        try:
            if self._preflight(target) != binding:
                rejected = "binding_required"
        except (BindingError, SyncStateError) as exc:
            rejected = exc.code
        if rejected is not None:
            await self._stop_runner()
            await self._set_status("blocked", rejected)
            return
        self._startup_binding = binding
        work = self._activated_runner(binding)
        if work is not None:
            self._runner = asyncio.create_task(work, name="sync-remote")
        else:
            await self._set_status("blocked", "protocol_unavailable")
            await self._stop_runner()

    async def _stop_runner(self) -> None:
        await super()._stop_runner()
        if not self._stopping_service:
            self._release_ownership()

    def complete_disabled_binding_change(self, change_id: str) -> None:
        """Retire THIS drained I5 boundary after durable I6 completion (§7).

        Called synchronously after transitions.complete_commit under the owning
        final commit section. The new generation is already published there, so
        authenticate the receipt against its captured OLD generation, not the
        new one. No stop/reconfigure, journal repair, credential access or base
        migration occurs here; the caller publishes the disabled status event.
        """
        from ..sync.service import BindingDrainProof
        from ..sync.transitions import BindingChangeError

        proof = self._binding_proof
        status = self.docs.transitions.change_status(change_id)
        if (status["state"] != "committed" or not status["fenced"]
                or type(proof) is not BindingDrainProof or proof._owner is not self
                or proof.change_id != change_id
                or proof.generation != self._binding_generation
                or proof.generation != status["expected_generation"]
                or proof.target != self._binding_target
                or not proof.reconciled or not proof.runner_stopped or not proof.fenced
                or proof.reason is not None or proof.corpus_fingerprint is None
                or not self._binding_quiescing or not self._binding_fenced
                or self._binding_draining or self._binding_failure or self._cleanup_failed
                or self._stopping_service or self._config().sync.enabled is not False
                or self._runner is not None or self._ws is not None or self._synced
                or self._tasks or self._pending or self._pending_paths or self._skip
                or self._live_writes or self._sessions or self._retained_evidence
                or self._profile_lease is not None or self._reservation_lease is not None):
            raise BindingChangeError("recovery_required")
        # Old-target evidence is already durable. Shutdown must not rewrite the
        # completed journal as a draining record. Preserve _state/_url/_space:
        # the old checkpoint is not a base for the newly committed target.
        self._activated_model_seen = True
        self._binding_evidence_sink = None
        self._binding_proof = None
        self._binding_quiescing = False
        self._binding_fenced = False
        self._binding_target = None
        self._binding_generation = None
        self._binding_resumed.set()

    def _release_ownership(self) -> None:
        # Full stop retains ownership through watcher join too. Reconfigure has
        # only runner cleanup and releases after that successful join.
        if self._reservation_lease is not None:
            assert self._reservation_owner is not None
            self._reservation_owner.release(self._reservation_lease)
            self._reservation_lease = None
            self._reservation_owner = None
        self._startup_binding = None
        if self._profile_lease is not None:
            self._profile_lease.release()
            self._profile_lease = None


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
        self.remote_profile_lifecycle: ProfileLifecycle | None = None
        """The app-owned consumer guard, supplied before startup (§5.2.1)."""
        self.remote_reservations: ReservationOwner | None = None
        # I6 reads the existing private transition journal, never portable metadata
        # or private selection alone. I10 may supply an actual activation target.
        self.remote_sync_activation: Callable[[], ActivatedRemoteTarget | None] = (
            lambda: _read_remote_sync_activation(self.paths)
        )
        # I10 hook, owned by I12: the account ID Enable's /api/v2/me verification
        # proved for (profile_id, url), read from the completed Enable journal so it
        # also holds after a restart. None = no Enable for that target; the v2
        # runner then pins the first accepted hello's account instead.
        self.remote_verified_account: Callable[[str, str], str | None] = (
            lambda profile_id, url: _read_remote_verified_account(self.paths, profile_id, url)
        )

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
        # I3: one admission owner per resident Ctx, shared by every bound writer
        # (remote-identity/runtime-integration-plan §§3.2–3.3).
        self.remote_transitions = BindingTransitions()
        self.docs = DocStore(paths, self.bus, transitions=self.remote_transitions)
        self.sync = _ProfileRemoteSync(
            paths, lambda: self.config, self.docs, self.bus,
            lifecycle=lambda: self.remote_profile_lifecycle,
            reservations=lambda: self.remote_reservations,
            activation=lambda: self.remote_sync_activation(),
            verified_account=lambda profile_id, url: self.remote_verified_account(profile_id, url),
        )
        """The connection to a Voxarium Studio Remote, when the project has one (§16)."""
        self.skills = SkillStore(paths, builtins=self._builtin_skills)
        """``.voxarium_studio/skills/`` — the Skills page's folder, and what members are given (§10.10)."""
        self.memory = MemoryStore(paths, self.bus)
        """``.voxarium_studio/memory/`` — what every agent is told the project has learned (§10.12)."""
        self.teams = TeamOrchestrator(
            paths=paths,
            db=self.db,
            bus=self.bus,
            jobs=self.jobs,
            config=config,
            docs=self.docs,
            skills=self.skills,
            memory=self.memory,
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
        self._env_watch: Callable[[], None] | None = None
        """Unsubscribes this project from plugin environment builds (§2.1)."""

    def _builtin_skills(self) -> dict[str, tuple[Path, str]]:
        """Package sources stay live across plugin reloads; nothing is copied into projects (§10.10)."""
        result = {
            studio_skill.SKILL_NAME: (
                studio_skill.BUNDLE / "skills" / studio_skill.SKILL_DIRECTORY, "voxarium_studio",
            ),
            studio_skill.PRODUCER_SKILL_NAME: (
                studio_skill.BUNDLE / "skills" / studio_skill.PRODUCER_SKILL_DIRECTORY,
                "voxarium_studio",
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
        if self.remote_profile_lifecycle is not None:
            self.remote_profile_lifecycle.register_context(self)
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
        # After the watcher: sync learns about edits made behind the Studio's back from it.
        await self.sync.start()
        await self.updates.start()
        await self.automations.start()
        # Last, once everything a hook might want is up: the hooks run in the background,
        # so the project is ready before any of them has done anything (§9.7).
        self._start_plugin_hooks()
        self._start_plugin_envs()
        log.info("Voxarium Studio ready for project %s", self.paths.root)

    async def shutdown(self) -> None:
        self.chats.closing = True
        await self.chats.producer.shutdown()
        if self._teams_ready:
            await self.chats.producer.restore_paused()
        await self._stop_plugin_hooks()
        if self._env_watch is not None:
            # Builds are the process's, not this project's: another project may be waiting
            # on the same one, so only server shutdown stops them (`shutdown_workspace`).
            self._env_watch()
            self._env_watch = None
        await self.automations.stop()
        await self.updates.stop()
        await self.sync.stop()
        await self.watcher.stop()
        self.teams.cancel_limit_waits()
        await self.teams.cancel_commit_jobs()
        await self.jobs.shutdown()
        await self.db.close()
        if self.remote_profile_lifecycle is not None:
            # Failed cleanup stays in the app inventory until the WHOLE context
            # is joined, not merely until its runner requested cancellation (I2).
            self.remote_profile_lifecycle.unregister_context(self)

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

    # ---- plugin environments (§2.1, §9.4) ----

    def _start_plugin_envs(self) -> None:
        """Build every enabled plugin's isolated environment that is missing or out of date.

        In the background, never awaited: a plugin pinning torch takes minutes and the
        project has to be usable meanwhile. A node that runs in one waits for it instead
        (``PipelineRunner``). Called again after every reload, so enabling a plugin builds
        its environment and a changed ``requirements.txt`` is picked up by the next save
        or enable; a build already running is joined, not repeated.
        """
        if self._env_watch is None:
            self._env_watch = plugin_envs.watch(self._publish_plugin_env)
        for plugin in self.registry.plugins():
            if plugin.enabled and plugin.status == "loaded":
                plugin_envs.schedule(plugin.manifest)

    async def _publish_plugin_env(self, venv: Path) -> None:
        for plugin in self.registry.plugins():
            if plugin.manifest.venv_path == venv:
                await self.bus.publish(EventType.PLUGIN_ENV, {"plugin": plugin.to_json()})

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
            self._start_plugin_envs()
        await self.bus.publish(
            EventType.PLUGIN_CHANGED,
            {
                "plugins": [p.to_json() for p in self.registry.plugins()],
                "node_type_count": len(self.registry.node_types()),
            },
        )
        return self.registry
