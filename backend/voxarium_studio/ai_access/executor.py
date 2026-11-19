"""Pinned Studio-side ACP admission. No legacy REST or local-actor fallback."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import select

from ..models.ai_access import AiCommandLedger, AiMessageProvenance, AiResourceProvenance
from ..models.teams import AgentChat, AgentChatMessage, AgentMessage, MemberKind, TeamMember, TeamTask
from ..ws.events import EventType
from . import grants, identity, ledger, projection, protocol as p


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def recover_project(db: Any) -> None:
    """No worker survives process restart, including ones past launch acknowledgement."""
    stamp = now_ms()
    await ledger.recover(db, now_ms=stamp)
    async with db.session() as session:
        rows = (await session.exec(select(AiCommandLedger).where(
            AiCommandLedger.state == str(p.CommandState.RUNNING),
            AiCommandLedger.op.in_([str(op) for op in p.DISPATCHING]),
        ))).all()
        keys = [ledger.LedgerKey(row.server_instance_id, row.executor_registration_id,
                                 row.export_id, row.source_registration_id, row.command_id) for row in rows]
    for key in keys:
        await ledger.mark_terminal(db, key, p.CommandState.OUTCOME_UNKNOWN, now_ms=stamp,
                                   error_code=p.ErrorCode.OUTCOME_UNKNOWN)


@dataclass(frozen=True)
class Resource:
    kind: p.ResourceKind
    row: TeamTask | AgentChat
    member: TeamMember
    provenance: AiResourceProvenance | None

    @property
    def revision(self) -> int:
        return self.provenance.revision if self.provenance else 1

    def owned(self, actor: p.RemoteActor, export_id: str) -> bool:
        origin = self.provenance
        return bool(origin and (origin.origin_user_id, origin.origin_registration_id,
                                origin.origin_export_id) == (actor.user_id, actor.registration_id, export_id))


async def _resource(session: Any, resource_id: str, kind: p.ResourceKind | None = None) -> Resource:
    row = await session.get(TeamTask, resource_id) if kind != p.ResourceKind.CHAT else None
    if row is not None:
        kind = p.ResourceKind.TASK
        member = await session.get(TeamMember, row.assignee_id)
        if member is None or member.team_id != row.team_id:
            raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
    elif kind != p.ResourceKind.TASK:
        row = await session.get(AgentChat, resource_id)
        kind = p.ResourceKind.CHAT
        if row is None or row.producer_mode or row.producer_workspace:
            raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
        member = await session.get(TeamMember, row.member_id)
    else:
        raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
    if member is None or member.kind != MemberKind.AGENT:
        raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
    origin = await session.get(AiResourceProvenance, (str(kind), resource_id))
    return Resource(kind, row, member, origin)


def _execution_policy(ctx: Any, member: TeamMember, row: TeamTask | AgentChat | None = None) -> tuple[str, str, dict, int]:
    from ..teams.orchestrator import member_run_settings, project_mcp_servers

    backend = (row.agent_backend if row else None) or member.agent_backend
    model = (row.model if row else None) or (member.model if backend == member.agent_backend else "")
    settings = member_run_settings(member, ctx.config.teams)
    mcp_servers = project_mcp_servers(ctx.paths, ctx.config)
    effort = (row.effort if isinstance(row, TeamTask) else None) or member.effort
    # Include the settings source as well as the effective permission and model. Files
    # such as MCP config are represented by their digest, not returned to the caller.
    digest = grants.policy_digest(
        backend=backend, model=model, permission_mode=str(settings.get("permission_mode", "")),
        max_turns=settings.get("max_turns"),
        tools_policy={"member": member.model_dump(mode="json", include={
            "id", "team_id", "kind", "manager_id", "skills", "agent", "isolate_worktree",
            "override_team_settings",
        }), "effort": effort, "run_settings": settings, "settings": ctx.config.teams.model_dump_json(),
            "concurrency": ctx.config.concurrency.model_dump(mode="json")},
        mcp_policy={"strict": ctx.config.mcp.strict, "servers": mcp_servers},
    )
    return digest, backend, settings, len(mcp_servers)


def execution_policy_digest(ctx: Any, member: TeamMember, row: TeamTask | AgentChat | None = None) -> str:
    """Hash current server-side launch settings, never a digest supplied by a requester."""
    return _execution_policy(ctx, member, row)[0]


def execution_policy_preview(ctx: Any, member: TeamMember) -> dict:
    """Allowlisted local display of the member's launch defaults, not a backend sandbox promise."""
    from ..core.config import permission_modes
    from ..teams.backends.base import available_backends

    digest, backend, settings, mcp_count = _execution_policy(ctx, member)
    mode = settings['permission_mode']
    if backend not in available_backends() or mode not in permission_modes():
        raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
    return {
        'execution_policy_digest': digest,
        'backend': backend,
        'permission_mode': mode,
        'tools': {'backend_native_tools_may_include_shell': True},
        'mcp': {'strict': ctx.config.mcp.strict, 'project_server_count': mcp_count},
        'limits': {'max_turns': settings['max_turns'],
                   'max_run_continuations': ctx.config.teams.max_run_continuations,
                   'project_agent_slots': ctx.config.concurrency.agents,
                   'remote_runs_per_export': 1, 'remote_runs_per_grant': 1},
    }


class StatusDTO(projection.ResourceStatusDTO):
    revision: p.Revision


# Retain only projections that were explicitly read or subscribed to. Raw bus
# messages are wakeups, never event content. The smaller byte window also bounds
# memory when several pinned resources have very large transcript chunks.
_MAX_FEEDS = 64
_REPLAY_BYTES = 1024 * 1024
_TASK_SIGNALS = {str(e) for e in (EventType.TEAM_MEMBER_MESSAGE, EventType.TEAM_TASK_UPDATED,
                                EventType.TEAM_TASK_STATUS_CHANGED, EventType.TEAM_TASK_DELETED)}
_CHAT_SIGNALS = {str(e) for e in (EventType.TEAM_CHAT_MESSAGE, EventType.TEAM_CHAT_UPDATED,
                                EventType.TEAM_CHAT_DELETED)}


@dataclass(eq=False)
class _Feed:
    ctx: Any
    kind: p.ResourceKind
    topic: str
    stream: projection.ProjectionStream
    touched: float
    scope: tuple | None = None
    fingerprint: tuple[str, ...] | None = None
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    sizes: deque[tuple[int, int]] = field(default_factory=deque)
    bytes: int = 0

    def reset(self) -> None:
        self.stream.reset()
        self.fingerprint = None
        self.bytes = 0
        self.sizes.clear()
        self.signal.set()

    def append(self, data: Any, revision: int) -> None:
        # The accepted projection layer enforces 500 frames, 60s and the live
        # buffer bounds; also limit material retained in the ACP adapter for replay.
        frame = self.stream.append(data, revision)
        self.sizes.append((frame.seq, frame.size))
        self.bytes += frame.size
        while self.sizes and (len(self.sizes) > p.REPLAY_FRAMES or self.bytes > _REPLAY_BYTES):
            seq, size = self.sizes.popleft()
            self.bytes -= size
            while self.stream._replay and self.stream._replay[0].seq <= seq:
                self.stream._evicted_through = self.stream._replay.popleft().seq
        self.signal.set()


@dataclass(eq=False)
class StreamSubscription:
    feed: _Feed
    buffer: projection.Subscription
    request: p.RequestBody
    actor: p.RemoteActor
    connection_id: str
    live: Callable[[], bool]
    initial_epoch: str
    initial_seq: int

    @property
    def id(self) -> str:
        return self.buffer.id

    def close(self) -> None:
        self.buffer.close()
        self.feed.signal.set()


def _resource_scope(res: Resource) -> tuple:
    origin = res.provenance
    return (res.kind, res.member.team_id, res.member.id,
            origin.origin_export_id if origin else None,
            origin.origin_registration_id if origin else None,
            origin.origin_user_id if origin else None)


class Executor:
    def __init__(self, resolve: Callable[[p.Target], Any], *, clock: Callable[[], int] = now_ms) -> None:
        self._resolve = resolve
        self._now = clock
        # Private consent writers and dispatch claims use the same admission fence.
        self.admission = asyncio.Lock()
        self._active: dict[str, tuple[str, str, str]] = {}
        self._feeds: dict[tuple, _Feed] = {}
        self._last_wall = 0
        self._clock_failed = False

    def _time(self) -> int:
        value = self._now()
        if self._clock_failed or value < self._last_wall:
            self._clock_failed = True
            raise p.ProtocolError(p.ErrorCode.DENIED)
        self._last_wall = value
        return value

    async def handle(self, fwd: p.ForwardBody, *, executor_epoch: str,
                     live: Callable[[], bool],
                     subscribe: Callable[[StreamSubscription], None] | None = None,
                     unsubscribe: Callable[[str, p.Target, p.RemoteActor, str], bool] | None = None,
                     ) -> p.ResponseBody:
        req = fwd.request
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(10, max(0, fwd.deadline_ms - self._now()) / 1000)

        def connected() -> None:
            if not live() or loop.time() >= deadline or self._time() >= fwd.deadline_ms:
                raise p.ProtocolError(p.ErrorCode.DENIED)
            if self._resolve(req.target) is not ctx:
                raise p.ProtocolError(p.ErrorCode.STALE_TARGET)

        try:
            if req.grant_revision != fwd.current_grant_revision:
                raise p.ProtocolError(p.ErrorCode.DENIED)
            ctx = self._resolve(req.target)
            if ctx is None:
                raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
            actor = fwd.actor()
            payload = req.parsed_payload()
            async with self.admission:
                connected()
                if req.op in p.MUTATIONS:
                    return await self._mutate(ctx, fwd, payload, actor, executor_epoch, connected, deadline)
                if req.expected_executor_epoch != executor_epoch:
                    raise p.ProtocolError(p.ErrorCode.STALE_TARGET)
                if req.op == p.Operation.STREAM_UNSUBSCRIBE:
                    if unsubscribe is None or not unsubscribe(payload.subscription_id, req.target,
                                                               actor, fwd.connection_id):
                        raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
                    result = {'subscription_id': payload.subscription_id}
                elif req.op == p.Operation.STREAM_SUBSCRIBE:
                    if subscribe is None:
                        raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
                    sub = await self._subscribe(ctx, req, payload, actor, fwd.connection_id, live, connected)
                    try:
                        subscribe(sub)
                    except BaseException:
                        sub.close()
                        raise
                    result = {'subscription_id': sub.id, 'stream_epoch': sub.initial_epoch,
                              'seq': sub.initial_seq}
                else:
                    result = await self._read(ctx, req, payload, actor, connected)
                connected()
                return p.ResponseBody(target=req.target, state=p.CommandState.COMPLETED, result=result)
        except (p.ProtocolError, identity.AiAccessError) as exc:
            try:
                code = p.ErrorCode(exc.code)
            except ValueError:
                code = p.ErrorCode.DENIED
        except (ValueError, TypeError):
            code = p.ErrorCode.DENIED
        return p.ResponseBody(target=req.target, command_id=req.command_id,
                              state=p.CommandState.REJECTED, code=code)

    def _grant(self, ctx: Any, req: p.RequestBody, actor: p.RemoteActor, *,
               res: Resource | None = None, member: TeamMember | None = None,
               op: p.Operation | None = None, topic: str | None = None) -> p.Grant:
        operation = op or req.op
        member = res.member if res else member
        return grants.effective_grant(
            actor.grant_id, now_ms=self._time(), target=req.target, actor=actor, op=operation,
            team_id=member.team_id if member else None, member_id=member.id if member else None,
            resource_id=res.row.id if res else None,
            created_by_grantee=res.owned(actor, req.target.export_id) if res else False,
            current_policy_digest=execution_policy_digest(ctx, member, res.row if res else None)
            if member and operation in p.DISPATCHING else None, topic=topic,
        )

    async def _mutate(self, ctx: Any, fwd: p.ForwardBody, body: Any, actor: p.RemoteActor,
                      epoch: str, connected: Callable[[], None], deadline: float) -> p.ResponseBody:
        req = fwd.request
        op = req.op
        kind = p.ResourceKind.TASK if str(op).startswith("task.") else p.ResourceKind.CHAT
        creating = op in p.CREATING
        resource_id = None if creating else body.resource_id
        command = ledger.Command.from_forward(fwd, resource_kind=kind, resource_id=resource_id)
        svc = ctx.teams if kind == p.ResourceKind.TASK else ctx.chats
        reservation = None
        handed_off = False
        message = None
        cancel_handle = None

        async def scope(session: Any) -> Resource | None:
            if creating:
                member = await session.get(TeamMember, body.assignee_id if kind == p.ResourceKind.TASK
                                           else body.member_id)
                if member is None or member.team_id != body.team_id or member.kind != MemberKind.AGENT:
                    raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
                connected()
                self._grant(ctx, req, actor, member=member)
                return None
            res = await _resource(session, resource_id, kind)
            connected()
            self._grant(ctx, req, actor, res=res)
            return res

        async def effect(session: Any) -> ledger.EffectResult:
            nonlocal resource_id, reservation, message, cancel_handle
            res = await scope(session)
            for path in getattr(body, "write_paths", None) or ():
                p.resolve_write_path(ctx.paths.root, path)
            if op == p.Operation.TASK_CREATE:
                row = await svc.remote_create_task(session, actor=actor, team_id=body.team_id,
                    assignee_id=body.assignee_id, title=body.title, brief=body.brief,
                    write_paths=body.write_paths, summary_locale=body.locale)
                resource_id = row.id
            elif op == p.Operation.CHAT_CREATE:
                row = await svc.remote_create_chat(session, actor=actor, team_id=body.team_id,
                    member_id=body.member_id, title=body.title, locale=body.locale)
                resource_id = row.id
            elif op == p.Operation.TASK_EDIT_DRAFT:
                await svc.remote_edit_task(session, resource_id, actor=actor, title=body.title,
                                           brief=body.brief, write_paths=body.write_paths)
            elif op == p.Operation.TASK_NOTE:
                message = await svc.remote_add_task_note(session, resource_id, actor=actor, text=body.text)
            elif op in p.DISPATCHING:
                assert res is not None
                if any(x[0] == req.target.export_id or x[1] == actor.grant_id for x in self._active.values()):
                    raise p.ProtocolError(p.ErrorCode.BUSY)
                uncertain = await session.exec(select(AiCommandLedger.id).where(
                    AiCommandLedger.resource_kind == str(kind), AiCommandLedger.resource_id == resource_id,
                    AiCommandLedger.op.in_([str(o) for o in p.DISPATCHING]),
                    AiCommandLedger.state == str(p.CommandState.OUTCOME_UNKNOWN),
                ).limit(1))
                if uncertain.first() is not None:
                    raise p.ProtocolError(p.ErrorCode.OUTCOME_UNKNOWN)
                run_id = str(uuid4())
                if op == p.Operation.TASK_START:
                    for path in res.row.write_paths:
                        p.resolve_write_path(ctx.paths.root, path)
                    reservation = await svc.remote_reserve_task_start(session, resource_id,
                                                                     actor=actor, run_id=run_id)
                else:
                    reservation = await svc.remote_reserve_chat_send(session, resource_id,
                                                              actor=actor, run_id=run_id, text=body.text)
                connected()
                self._grant(ctx, req, actor, res=res)
                return ledger.EffectResult(kind, resource_id, getattr(reservation, "message_id", None), run_id)
            elif op == p.Operation.TASK_CANCEL:
                cancel_handle = svc.remote_task_cancel_target(resource_id, req.expected_run_id, actor=actor)
            elif op == p.Operation.CHAT_CANCEL:
                cancel_handle = svc.remote_chat_cancel_target(resource_id, req.expected_run_id, actor=actor)
            else:
                raise p.ProtocolError(p.ErrorCode.UNSUPPORTED)
            connected()
            self._grant(ctx, req, actor, res=res, member=None if not creating else
                        await session.get(TeamMember, body.assignee_id if kind == p.ResourceKind.TASK else body.member_id))
            connected()
            return ledger.EffectResult(kind, resource_id, getattr(message, "id", None), req.expected_run_id)

        async def admit() -> ledger.Receipt:
            async with ctx.db.session() as session:
                await scope(session)
            return await ledger.admit_mutation(ctx.db, command, effect, current_executor_epoch=epoch,
                                               now_ms=self._time())

        try:
            if op == p.Operation.TASK_CREATE:
                async with svc.task_number_lock:
                    receipt = await admit()
            else:
                receipt = await admit()
            if receipt.duplicate:
                return self._response(req, receipt)
            if reservation is not None:
                token = await ledger.reserve_dispatch(ctx.db, command.key, receipt.run_id, now_ms=self._time())
                expected = (req.expected_resource_revision or 0) + 1

                async def before_launch() -> bool:
                    async with self.admission:
                        try:
                            connected()
                            if reservation.handle.cancel_requested:
                                raise p.ProtocolError(p.ErrorCode.DENIED)
                            if not await ledger.claim_dispatch(ctx.db, command.key, token, now_ms=self._time()):
                                return False
                            await ledger.mark_running(ctx.db, command.key, now_ms=self._time())
                            # Re-read after the durable writes; local state may have
                            # changed while the worker was preparing or claiming the token.
                            async with ctx.db.session() as session:
                                res = await scope(session)
                                if res is None or res.revision != expected:
                                    raise p.ProtocolError(p.ErrorCode.REVISION_CONFLICT)
                            if res.member.id in ctx.teams.removing_members:
                                raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
                            if kind == p.ResourceKind.TASK:
                                if str(res.row.status) != 'in_progress' or res.row.archived or res.row.producer_chat_id:
                                    raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
                                for path in res.row.write_paths:
                                    p.resolve_write_path(ctx.paths.root, path)
                            # No await between the final immutable-consent check and the
                            # caller handing this CLAIMED token to the backend iterator.
                            connected()
                            self._grant(ctx, req, actor, res=res)
                            if reservation.handle.cancel_requested:
                                raise p.ProtocolError(p.ErrorCode.DENIED)
                            return True
                        except (p.ProtocolError, identity.AiAccessError):
                            await ledger.mark_terminal(ctx.db, command.key, p.CommandState.DENIED_BEFORE_DISPATCH,
                                                       now_ms=self._now(), error_code=p.ErrorCode.DENIED)
                            return False

                async def after_run(entered: bool, outcome: str) -> None:
                    self._active.pop(receipt.run_id, None)
                    state = p.CommandState(outcome) if entered else p.CommandState.DENIED_BEFORE_DISPATCH
                    await ledger.mark_terminal(ctx.db, command.key, state, now_ms=self._now(),
                                               error_code=None if entered else p.ErrorCode.DENIED)

                self._active[receipt.run_id] = (req.target.export_id, actor.grant_id, resource_id)
                dispatch = svc.remote_dispatch_task if kind == p.ResourceKind.TASK else svc.remote_dispatch_chat
                try:
                    dispatch(reservation, before_launch=before_launch, after_run=after_run, deadline=deadline)
                except BaseException:
                    self._active.pop(receipt.run_id, None)
                    raise
                handed_off = True
            else:
                if cancel_handle is not None:
                    connected()
                    async with ctx.db.session() as session:
                        await scope(session)
                    cancel = svc.remote_cancel_task if kind == p.ResourceKind.TASK else svc.remote_cancel_chat
                    await cancel(cancel_handle, actor=actor)
                elif op == p.Operation.TASK_NOTE:
                    await svc.remote_publish_task_note(message)
                elif kind == p.ResourceKind.TASK:
                    await svc.remote_publish_task_changed(resource_id)
                elif op == p.Operation.CHAT_CREATE:
                    await svc.remote_publish_chat_changed(resource_id)
                receipt = await ledger.mark_terminal(ctx.db, command.key, p.CommandState.COMPLETED,
                                                      now_ms=self._now())
            return self._response(req, receipt)
        finally:
            if reservation is not None and not handed_off:
                reservation.release()

    @staticmethod
    def _response(req: p.RequestBody, receipt: ledger.Receipt) -> p.ResponseBody:
        return p.ResponseBody.from_wire({"target": req.target.model_dump(mode="json"),
                                        "command_id": receipt.command_id, "state": str(receipt.state),
                                        "code": receipt.error_code, "result": receipt.to_wire()})

    async def _resources(self, session: Any) -> list[Resource]:
        resources = []
        for model, kind in ((TeamTask, p.ResourceKind.TASK), (AgentChat, p.ResourceKind.CHAT)):
            ids = (await session.exec(select(model.id).order_by(model.id)
                                      .limit(p.MAX_INDEX_ROWS_PER_EXPORT + 1))).all()
            if len(ids) > p.MAX_INDEX_ROWS_PER_EXPORT:
                raise p.ProtocolError(p.ErrorCode.CAPACITY_EXCEEDED)
            for resource_id in ids:
                try:
                    resources.append(await _resource(session, resource_id, kind))
                except p.ProtocolError:
                    continue
        return resources

    async def index(self, ctx: Any, export: identity.ExportRecord) -> list[dict]:
        # Publish only metadata within at least one currently relevant local scope.
        candidates = [record.grant for record in grants.list_grants(export.export_id)
                      if record.grant.target == identity.export_target(export)
                      and record.grant.effective(self._time())]
        rows = []
        async with ctx.db.session() as session:
            for res in await self._resources(session):
                if not any(res.member.team_id in g.team_ids and res.member.id in g.member_ids and (
                    g.resource_scope == p.ResourceScope.ALL_RESOURCES_IN_SELECTED_TEAMS
                    or res.row.id in g.resource_ids
                    or (g.resource_scope == p.ResourceScope.CREATED_BY_THIS_GRANTEE and res.owned(
                        p.RemoteActor(g.grantee_user_id, g.grantee_registration_id, g.grant_id, g.revision),
                        export.export_id))
                ) for g in candidates):
                    continue
                origin = res.provenance
                remote_origin = None
                if origin and origin.origin_export_id == export.export_id and origin.origin_user_id:
                    remote_origin = {'user_id': UUID(origin.origin_user_id).hex,
                                     'registration_id': origin.origin_registration_id}
                rows.append({'resource_id': res.row.id, 'kind': str(res.kind), 'team_id': res.member.team_id,
                             'member_id': res.member.id, 'revision': res.revision, 'remote_origin': remote_origin})
        if len(rows) > p.MAX_INDEX_ROWS_PER_EXPORT:
            raise p.ProtocolError(p.ErrorCode.CAPACITY_EXCEEDED)
        return sorted(rows, key=lambda row: row['resource_id'])

    def _status(self, ctx: Any, res: Resource, redactor: projection.Redactor,
                *, target: p.Target) -> StatusDTO:
        row = res.row
        run_id = next((key for key, item in self._active.items() if item[0] == target.export_id and item[2] == row.id), None)
        if res.kind == p.ResourceKind.CHAT:
            status = 'running' if ctx.chats.is_running(row.id) else ('error' if row.error else 'idle')
            dto = projection.chat_status(row, redactor=redactor, team_id=res.member.team_id,
                assignee_label=res.member.name, status=status, run_id=run_id)
        else:
            dto = projection.task_status(row, redactor=redactor, assignee_label=res.member.name,
                is_running=bool(run_id) or str(row.status) == 'in_progress', run_id=run_id)
        return StatusDTO(**dto.model_dump(), revision=res.revision)

    def _feed(self, ctx: Any, target: p.Target, res: Resource, topic: str) -> _Feed:
        key = (ctx, target, res.kind, res.row.id, topic)
        existing = self._feeds.get(key)
        stamp = time.monotonic()
        if existing is not None and not existing.task.done():
            existing.touched = stamp
            return existing
        if len(self._feeds) >= _MAX_FEEDS:
            raise p.ProtocolError(p.ErrorCode.CAPACITY_EXCEEDED)
        stream = projection.ProjectionStream(target, res.row.id, replay_frames=p.REPLAY_FRAMES,
                                             replay_seconds=p.REPLAY_SECONDS)
        feed = _Feed(ctx, res.kind, topic, stream, stamp)
        # Subscribe before reading the DB. An update concurrent with the snapshot
        # is either already reflected in the sample or arrives as a later wakeup.
        bus = ctx.bus.subscribe()
        self._feeds[key] = feed
        feed.task = asyncio.create_task(self._watch(feed, bus), name='ai-access-projection')

        def retire(_):
            bus.close()  # also when cancelled before its coroutine first executes
            feed.reset()
            if self._feeds.get(key) is feed:
                del self._feeds[key]
            if not feed.task.cancelled():
                feed.task.exception()

        feed.task.add_done_callback(retire)
        return feed

    async def close_streams(self) -> None:
        tasks = [feed.task for feed in self._feeds.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch(self, feed: _Feed, bus: Any) -> None:
        signals = _TASK_SIGNALS if feed.kind == p.ResourceKind.TASK else _CHAT_SIGNALS
        resource_id = feed.stream.resource_id
        while True:
            if (not feed.stream.subscriber_count
                    and time.monotonic() - feed.touched >= p.REPLAY_SECONDS):
                return
            try:
                async with asyncio.timeout(1):
                    event = await bus.get()
            except TimeoutError:
                event = None
            if event is None:
                # Job registries settle just after the last runtime notification.
                # Reconcile status without rescanning idle transcripts on a timer.
                relevant = feed.topic == 'status' and bool(feed.stream.subscriber_count)
            elif event.type == str(EventType.TEAM_UPDATED):
                relevant = True  # member removal/reassignment can change scope
            elif feed.kind == p.ResourceKind.TASK:
                relevant = event.type in signals and event.data.get('task_id') == resource_id
            else:
                relevant = event.type in signals and event.data.get('chat_id', event.data.get('id')) == resource_id
            dropped = bus.take_dropped()
            if not (relevant or dropped):
                continue
            async with self.admission:
                if dropped:
                    feed.reset()
                try:
                    if self._resolve(feed.stream.target) is not feed.ctx:
                        return
                    async with feed.ctx.db.session() as session:
                        res = await _resource(session, resource_id, feed.kind)
                        await self._sample(feed, session, res, self._redactor(feed.ctx))
                except (p.ProtocolError, identity.AiAccessError):
                    feed.reset()

    @staticmethod
    def _redactor(ctx: Any) -> projection.Redactor:
        return projection.Redactor((getattr(getattr(ctx, 'auth', None), 'token', None),))

    async def _transcript(self, session: Any, res: Resource,
                          redactor: projection.Redactor) -> list[projection.ProjectedMessage]:
        model = AgentMessage if res.kind == p.ResourceKind.TASK else AgentChatMessage
        foreign_id = model.task_id if res.kind == p.ResourceKind.TASK else model.chat_id
        rows = (await session.exec(select(model, AiMessageProvenance.message_id).outerjoin(
            AiMessageProvenance, AiMessageProvenance.message_id == model.id).where(
            foreign_id == res.row.id).order_by(model.created_at, model.id).limit(4097))).all()
        if len(rows) > 4096:
            raise p.ProtocolError(p.ErrorCode.CAPACITY_EXCEEDED)
        projected = projection.task_documents(res.row, redactor=redactor) if res.kind == p.ResourceKind.TASK else []
        for message, origin in rows:
            item = projection.project_message(message, resource_id=res.row.id, redactor=redactor,
                                              origin='remote' if origin else 'local')
            if item is not None:
                projected.append(item)
        return projected

    async def _sample(self, feed: _Feed, session: Any, res: Resource,
                      redactor: projection.Redactor) -> Any:
        scope = _resource_scope(res)
        if feed.scope is not None and feed.scope != scope:
            feed.reset()
        feed.scope = scope
        previous = feed.fingerprint
        if feed.topic == 'status':
            status = self._status(feed.ctx, res, redactor, target=feed.stream.target)
            fingerprint = (p.canonical_hash(status.model_dump(mode='json')),)
            if previous is not None and previous != fingerprint:
                feed.append(status, res.revision)
            feed.fingerprint = fingerprint
            return status
        projected = await self._transcript(session, res, redactor)
        fingerprint = tuple(p.canonical_hash([item.message_id, item.role, item.kind, item.text,
                                             item.created_at, item.origin, item.delivery]) for item in projected)
        if previous is not None:
            if fingerprint[:len(previous)] != previous:
                # Rewritten or reordered transcript (including compaction/reset).
                # Never replay deltas belonging to a different timeline.
                feed.reset()
            else:
                for message in projected[len(previous):]:
                    for chunk in projection.message_chunks(message):
                        feed.append(chunk, res.revision)
        feed.fingerprint = fingerprint
        return projected

    async def _subscribe(self, ctx: Any, req: p.RequestBody, body: p.StreamSubscribePayload,
                         actor: p.RemoteActor, connection_id: str, live: Callable[[], bool],
                         connected: Callable[[], None]) -> StreamSubscription:
        async with ctx.db.session() as session:
            res = await _resource(session, body.resource_id)
            self._grant(ctx, req, actor, res=res, topic=body.topic)
            feed = self._feed(ctx, req.target, res, body.topic)
            await self._sample(feed, session, res, self._redactor(ctx))
        connected()
        self._grant(ctx, req, actor, res=res, topic=body.topic)
        buffer = feed.stream.subscribe(body.stream_epoch, body.after_seq)
        epoch, seq = feed.stream.watermark()
        # The first replay frame must be one after the sequence returned here.
        return StreamSubscription(feed, buffer, req, actor, connection_id, live, epoch,
                                  seq if body.after_seq is None else body.after_seq)

    async def allow_stream_frame(self, sub: StreamSubscription, *, terminal: bool = False) -> bool:
        """Called after acquiring the socket write lock, before any content is sent."""
        feed, req = sub.feed, sub.request
        try:
            async with self.admission:
                if sub.buffer.closed or not sub.live() or self._resolve(req.target) is not feed.ctx:
                    return False
                async with feed.ctx.db.session() as session:
                    res = await _resource(session, feed.stream.resource_id, feed.kind)
                if res.member.id in feed.ctx.teams.removing_members:
                    return False
                if _resource_scope(res) != feed.scope:
                    feed.reset()
                    feed.scope = _resource_scope(res)
                self._grant(feed.ctx, req, sub.actor, res=res, topic=feed.topic)
                return (not sub.buffer.closed and sub.live() and self._resolve(req.target) is feed.ctx
                        and (terminal or not sub.buffer.resync_required))
        except (p.ProtocolError, identity.AiAccessError):
            return False

    async def _read(self, ctx: Any, req: p.RequestBody, body: Any, actor: p.RemoteActor,
                    connected: Callable[[], None]) -> dict[str, Any]:
        redactor = self._redactor(ctx)
        async with ctx.db.session() as session:
            if req.op == p.Operation.COMMAND_GET:
                key = ledger.LedgerKey.of(req.target, actor.registration_id, body.command_id)
                row = await ledger._find(session, key)
                if not row or row.user_id != actor.user_id or row.grant_id != actor.grant_id:
                    raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
                res = await _resource(session, row.resource_id, p.ResourceKind(row.resource_kind))
                self._grant(ctx, req, actor, res=res, op=p.Operation.RESOURCE_GET)
                connected()
                return ledger._receipt(row).to_wire()
            if req.op == p.Operation.CATALOG_LIST:
                self._grant(ctx, req, actor)
                return {'target': req.target.model_dump(mode='json')}
            if req.op == p.Operation.RESOURCE_LIST:
                grant = self._grant(ctx, req, actor)
                items = []
                for res in await self._resources(session):
                    if body.team_id is not None and body.team_id != res.member.team_id:
                        continue
                    try:
                        self._grant(ctx, req, actor, res=res, op=p.Operation.RESOURCE_GET)
                    except identity.AiAccessError:
                        continue
                    items.append(self._status(ctx, res, redactor, target=req.target))
                rev = int(p.canonical_hash([item.model_dump(mode='json') for item in items])[:15], 16)
                binding = projection.CursorBinding(req.target, grant.grant_id, rev)
                return projection.paginate(items, binding, cursor=body.cursor, limit=min(3, body.limit)).model_dump(mode='json')
            res = await _resource(session, body.resource_id)
            self._grant(ctx, req, actor, res=res)
            if req.op == p.Operation.RESOURCE_GET:
                feed = self._feed(ctx, req.target, res, 'status')
                status = await self._sample(feed, session, res, redactor)
                epoch, seq = feed.stream.watermark()
                return status.model_dump(mode='json') | {'stream_epoch': epoch, 'seq': seq}
            if req.op != p.Operation.TRANSCRIPT_PAGE:
                raise p.ProtocolError(p.ErrorCode.UNSUPPORTED)
            feed = self._feed(ctx, req.target, res, 'transcript')
            projected = await self._sample(feed, session, res, redactor)
            self._grant(ctx, req, actor, res=res)
            epoch, seq = feed.stream.watermark()
            watermark = {'stream_epoch': epoch, 'seq': seq}
            if body.message_id:
                for item in projected:
                    if item.message_id == body.message_id:
                        return projection.message_chunk(item, body.offset).model_dump(mode='json') | watermark
                raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
            rev = int(p.canonical_hash([epoch, feed.fingerprint])[:15], 16)
            binding = projection.CursorBinding(req.target, res.row.id, rev)
            page = projection.transcript_page(projected, binding, cursor=body.cursor, limit=min(5, body.limit))
            return page.model_dump(mode='json') | watermark
