"""Process-owned ACP service. Selection of a project never selects a remote target."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

from ..core.application_config import application_config_transaction, load_application_config
from ..core.project_scope import canonical_root
from ..models.teams import MemberKind, TeamMember
from ..sync.bindings import BindingRecord, RemoteBindings
from ..sync.catalog import CatalogClient, CatalogError
from ..sync.catalog_http import CatalogHttpTransport
from ..sync.credentials import load_profile_token
from . import grants, identity, projection, protocol as p
from .executor import (
    Executor, StatusDTO, StreamSubscription, execution_policy_digest, execution_policy_preview, now_ms, recover_project,
)
from .intents import IntentStore
from .transport import AiTransport, DeviceCredentials, RegistrationExists


class Unavailable(identity.AiAccessError):
    pass


_EXPORT_POLICY_VERSION = 1


def export_policy_digest() -> str:
    """ACP publication policy, not an agent policy or a mirror frame digest."""
    return p.canonical_hash({
        'kind': 'studio.ai_access.export_policy',
        'version': _EXPORT_POLICY_VERSION,
        'acp': p.AICP_VERSION,
        'authority': 'exact_target_and_acknowledged_explicit_device_grant',
        'operations': {str(op): str(perm) if perm is not None else None
                       for op, perm in p.OPERATION_PERMISSION.items()},
        'grant_scopes': sorted(str(scope) for scope in p.ResourceScope),
        'default_grant_scope': str(p.ResourceScope.CREATED_BY_THIS_GRANTEE),
        'max_grant_age_ms': p.GRANT_MAX_MS,
        'index': 'grant_scoped_task_and_agent_chat_metadata',
    })


def _binding_fingerprint(binding: BindingRecord) -> str:
    return p.canonical_hash({'kind': 'studio.ai_access.binding', 'binding': binding.model_dump(mode='json')})


def _remote_user(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r'[a-f0-9]{32}', value) is None:
        raise p.ProtocolError()
    return str(UUID(hex=value))


def _wire_request(request: p.RequestBody) -> dict:
    data = request.model_dump(mode='json', exclude_none=True)
    body = request.parsed_payload().model_dump(mode='json', exclude_none=True)
    if request.op == p.Operation.TASK_CREATE:
        body['member_id'] = body.pop('assignee_id')
    if 'resource_id' in body:
        data['resource_id'] = body.pop('resource_id')
    if request.op == p.Operation.TRANSCRIPT_PAGE and body.get('message_id') is not None:
        body['cursor'] = 'chunk:' + p.canonical_json([body.pop('message_id'), body.pop('offset')]).hex()
    if request.op == p.Operation.STREAM_SUBSCRIBE and body.get('stream_epoch') is not None:
        body['cursor'] = f"{body.pop('stream_epoch')}:{body.pop('after_seq')}"
    data['payload'] = body
    return data


def _forward(data: dict, epoch: str) -> p.ForwardBody:
    if set(data) != {'source', 'grant', 'deadline', 'request'}:
        raise p.ProtocolError()
    source, grant, request = data['source'], data['grant'], dict(data['request'])
    if (set(source) != {'user_id', 'registration_id', 'connection_id'}
            or set(grant) != {'grant_id', 'revision', 'expires_at'}
            or request.get('grant_id') != grant['grant_id']):
        raise p.ProtocolError()
    p.check_timestamp(grant['expires_at'])
    body = dict(request['payload'])
    if 'resource_id' in request:
        if 'resource_id' in body:
            raise p.ProtocolError()
        body['resource_id'] = request.pop('resource_id')
    op = request['op']
    if op == 'task.create':
        if 'assignee_id' in body:
            raise p.ProtocolError()
        body['assignee_id'] = body.pop('member_id', None)
    if op == 'transcript.page' and str(body.get('cursor', '')).startswith('chunk:'):
        body['message_id'], body['offset'] = p.parse_json(bytes.fromhex(body.pop('cursor')[6:]))
    if op == 'stream.subscribe' and 'cursor' in body:
        body['stream_epoch'], seq = body.pop('cursor').split(':', 1)
        body['after_seq'] = int(seq)
    if 'expected_executor_epoch' not in request and op not in p.MUTATIONS:
        request['expected_executor_epoch'] = epoch
    request['payload'] = body
    return p.ForwardBody.from_wire({'request': request, 'source_user_id': _remote_user(source['user_id']),
        'source_registration_id': source['registration_id'], 'connection_id': source['connection_id'],
        'current_grant_revision': grant['revision'], 'deadline_ms': data['deadline']})


def _wire_mirror(kind: p.FrameType, body: p.Strict) -> dict:
    data = body.model_dump(mode='json', exclude_none=False)
    semantics = {key: value for key, value in data.items() if key not in {'expected_revision', 'hash'}}
    if kind in (p.FrameType.EXPORT_PUBLISH, p.FrameType.EXPORT_UNPUBLISH):
        semantics.update(kind='export', state='published' if kind == p.FrameType.EXPORT_PUBLISH else 'unpublished')
    elif kind == p.FrameType.GRANT_PUT:
        data['grant']['grantee_user_id'] = UUID(data['grant']['grantee_user_id']).hex
        semantics = {key: data[key] for key in ('export_id', 'export_revision', 'grant')}
        semantics.update(kind='grant', state='active')
    else:
        semantics.update(kind='grant', state='revoked')
    data['hash'] = p.canonical_hash(semantics)
    return data


@dataclass
class _RequestedStream:
    control: _Control
    request: p.RequestBody
    epoch: str
    seq: int

    @property
    def resource_id(self) -> str:
        return self.request.payload['resource_id']


class _Control:
    def __init__(self, service: AiAccessService, registration: identity.Registration) -> None:
        self.service = service
        self.registration = registration
        self.epoch = str(uuid4())
        self.lease = 0.0
        self.matched: set[str] = set()
        self.acknowledged_indexes: dict[str, tuple[p.Target, str]] = {}
        self.index_lock = asyncio.Lock()
        self.conn_id = ''
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()
        self.pending: dict[str, asyncio.Future] = {}
        self.incoming: set[asyncio.Task] = set()
        self._stream_tasks: set[asyncio.Task] = set()
        self._response_hooks: dict[str, Callable[[p.FrameType, dict], None]] = {}
        self.served: dict[str, tuple[StreamSubscription, asyncio.Task | None]] = {}
        self._send_lock = asyncio.Lock()
        self._socket: Any = None

    def alive(self) -> bool:
        return (not self.service._closed and self.ready.is_set() and self._socket is not None
                and self.task is not None and not self.task.done())

    def live(self, target: p.Target) -> bool:
        return self.alive() and time.monotonic() < self.lease and target.export_id in self.matched

    def fence(self) -> None:
        self.lease = 0
        self.matched.clear()
        self.acknowledged_indexes.clear()
        self._socket = None
        for sid in tuple(self.served):
            self.end_stream(sid)
        self.service.disconnected(self)

    async def send(self, kind: p.FrameType, rid: str, body: p.Strict | dict, *,
                   guard: Callable[[], Awaitable[bool]] | None = None) -> bool:
        data = body.model_dump(mode='json', exclude_none=True) if isinstance(body, p.Strict) else body
        envelope = p.canonical_json({'acp': 1, 'type': str(kind), 'request_id': rid, 'body': data})
        p.parse_json(envelope)
        async with self._send_lock:
            if guard is not None and not await guard():
                return False
            if self._socket is None:
                raise Unavailable('offline')
            await self._socket.send(envelope.decode('utf-8'))
            return True

    async def exchange(self, kind: p.FrameType, body: p.Strict | dict, *,
                       on_response: Callable[[p.FrameType, dict], None] | None = None,
                       ) -> tuple[p.FrameType, dict]:
        if len(self.pending) >= 16:
            raise Unavailable('busy')
        rid = str(uuid4())
        waiter = asyncio.get_running_loop().create_future()
        self.pending[rid] = waiter
        if on_response is not None:
            self._response_hooks[rid] = on_response
        try:
            async with asyncio.timeout(10):
                await self.send(kind, rid, body)
                return await waiter
        except (TimeoutError, OSError):
            raise Unavailable('outcome_unknown') from None
        finally:
            self.pending.pop(rid, None)
            self._response_hooks.pop(rid, None)

    async def control(self, kind: p.FrameType, body: p.Strict | dict) -> dict:
        typ, reply = await self.exchange(kind, body)
        if typ != p.FrameType.RESPONSE or set(reply) - {'state', 'result', 'code', 'retry_after'}:
            raise p.ProtocolError()
        if reply.get('state') != 'completed':
            try:
                code = p.ErrorCode(reply.get('code'))
            except ValueError:
                code = p.ErrorCode.DENIED
            raise Unavailable(code)
        result = reply.get('result')
        if not isinstance(result, dict):
            raise p.ProtocolError()
        return result

    async def read(self) -> None:
        while True:
            data = await self._socket.recv()
            frame = p.Frame.from_wire(p.parse_json(data))
            if frame.type in (p.FrameType.RESPONSE, p.FrameType.HEARTBEAT_PONG, p.FrameType.ACK):
                waiter = self.pending.get(frame.request_id)
                if waiter is not None and not waiter.done():
                    hook = self._response_hooks.get(frame.request_id)
                    if hook is not None:
                        try:
                            hook(frame.type, frame.body)
                        except (p.ProtocolError, identity.AiAccessError) as error:
                            waiter.set_exception(error)
                            continue
                    waiter.set_result((frame.type, frame.body))
            elif frame.type == p.FrameType.FORWARD:
                forward = _forward(frame.body, self.epoch)
                if len(self.incoming) >= 16:
                    await self.send(p.FrameType.RESPONSE, frame.request_id, p.ResponseBody(
                        target=forward.request.target, command_id=forward.request.command_id,
                        state=p.CommandState.REJECTED, code=p.ErrorCode.BUSY))
                    continue
                worker = asyncio.create_task(self.forward(frame.request_id, forward))
                self._track(worker)
            elif frame.type == p.FrameType.EVENT:
                self.service.receive_event(self, frame.body)
            else:
                raise p.ProtocolError()

    def worker_done(self, task: asyncio.Task) -> None:
        self.incoming.discard(task)
        self._stream_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # A failed worker must not leave a stale live lease or log private payloads.
            self.lease = 0
            if self.task is not None:
                self.task.cancel()

    async def forward(self, rid: str, fwd: p.ForwardBody) -> None:
        target = fwd.request.target
        if target.executor_registration_id != self.registration.registration_id:
            raise p.ProtocolError()
        # The relay may have installed an index whose commit ACK we did not receive.
        # Do not open a resource based on that uncertain result or a prior publication.
        addressed = ('resource_id' in fwd.request.payload
                     and fwd.request.expected_executor_epoch == self.epoch)

        def indexed() -> bool:
            ack = self.acknowledged_indexes.get(target.export_id)
            return not addressed or (ack is not None and ack[0] == target)

        if not indexed():
            await self.send(p.FrameType.RESPONSE, rid, p.ResponseBody(
                target=target, command_id=fwd.request.command_id,
                state=p.CommandState.REJECTED, code=p.ErrorCode.NOT_AVAILABLE))
            return
        subscribed = None

        def subscribe(sub: StreamSubscription) -> None:
            nonlocal subscribed
            # A Relay disconnect/revoke terminal notification releases the old
            # subscription. Impose independent executor-side capacity limits too.
            records = [entry[0] for control in self.service.connections.values() for entry in control.served.values()]
            if (sum(s.actor.registration_id == sub.actor.registration_id for s in records)
                    >= p.MAX_SUBSCRIPTIONS_PER_SOURCE or
                    sum(s.request.target == target for s in records) >= p.MAX_SUBSCRIPTIONS_PER_EXPORT or
                    len(records) >= 128):
                raise p.ProtocolError(p.ErrorCode.CAPACITY_EXCEEDED)
            self.served[sub.id] = sub, None
            subscribed = sub

        result = await self.service.executor.handle(fwd, executor_epoch=self.epoch,
                                                    live=lambda: self.live(target) and indexed(),
                                                    subscribe=subscribe, unsubscribe=self.unsubscribe)
        if subscribed is not None:
            # Response first: the relay creates its subscription mapping from this
            # receipt; events written before it would be silently dropped.
            try:
                allowed = result.code is None and await self.send(p.FrameType.RESPONSE, rid, result,
                    guard=lambda: self.service.executor.allow_stream_frame(subscribed, terminal=True))
                if not allowed:
                    self.end_stream(subscribed.id)
                    await self.send(p.FrameType.RESPONSE, rid, p.ResponseBody(target=target,
                        state=p.CommandState.REJECTED, code=p.ErrorCode.DENIED))
                elif subscribed.id in self.served:
                    task = asyncio.create_task(self.stream(subscribed), name='ai-access-stream')
                    self.served[subscribed.id] = subscribed, task
                    self._track(task, stream=True)
            except BaseException:
                self.end_stream(subscribed.id)
                raise
            return
        if result.code is None and fwd.request.op in p.MUTATIONS:
            try:
                await self.service.sync_index(self, target.export_id)
            except (p.ProtocolError, identity.AiAccessError):
                # Durable receipt remains authoritative; a failed index ACK must not
                # advertise an unindexed new resource as available for control.
                result = p.ResponseBody(target=target, command_id=fwd.request.command_id,
                                        state=p.CommandState.OUTCOME_UNKNOWN, code=p.ErrorCode.OUTCOME_UNKNOWN)
        await self.send(p.FrameType.RESPONSE, rid, result)

    def _track(self, task: asyncio.Task, *, stream: bool = False) -> None:
        (self._stream_tasks if stream else self.incoming).add(task)
        task.add_done_callback(self.worker_done)

    def end_stream(self, sid: str) -> None:
        entry = self.served.pop(sid, None)
        if entry is not None:
            sub, task = entry
            sub.close()
            if task is not None and task is not asyncio.current_task():
                task.cancel()

    def unsubscribe(self, sid: str, target: p.Target, actor: p.RemoteActor, connection_id: str) -> bool:
        entry = self.served.get(sid)
        if entry is None:
            raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
        sub, _ = entry
        if (sub.request.target, sub.actor, sub.connection_id) != (target, actor, connection_id):
            raise p.ProtocolError(p.ErrorCode.NOT_AVAILABLE)
        self.end_stream(sid)
        return True

    async def stream(self, sub: StreamSubscription) -> None:
        feed = sub.feed
        seq = sub.initial_seq
        revision = 1

        async def emit(payload: dict, *, terminal: bool = False) -> bool:
            frame = p.EventBody(subscription_id=sub.id, target=sub.request.target,
                resource_id=feed.stream.resource_id, stream_epoch=sub.initial_epoch,
                seq=seq + 1, resource_revision=revision, payload=payload)
            async with asyncio.timeout(2):
                return await self.send(p.FrameType.EVENT, str(uuid4()), frame,
                    guard=lambda: self.service.executor.allow_stream_frame(sub, terminal=terminal))

        try:
            while not sub.buffer.closed and sub.live():
                if sub.buffer.resync_required:
                    await emit({'code': 'resync_required'}, terminal=True)
                    break
                feed.signal.clear()
                batch = sub.buffer.drain()
                if not batch:
                    try:
                        async with asyncio.timeout(1):
                            await feed.signal.wait()
                    except TimeoutError:
                        # Also release idle subscriptions whose grant or lease expired.
                        if not await self.service.executor.allow_stream_frame(sub, terminal=True):
                            break
                    continue
                for frame in batch:
                    if sub.buffer.resync_required:
                        break
                    if frame.seq != seq + 1 or frame.stream_epoch != sub.initial_epoch:
                        await emit({'code': 'resync_required'}, terminal=True)
                        return
                    revision = frame.resource_revision
                    if not await emit(frame.payload):
                        if sub.buffer.resync_required:
                            break
                        return
                    seq = frame.seq
                    # Below the relay's frame and byte rate limits, including a
                    # burst of max-sized (~16 KiB) replay chunks.
                    await asyncio.sleep(0.085)
        except (TimeoutError, OSError, identity.AiAccessError):
            # No retry: the relay/browser must take a fresh snapshot on a broken
            # delivery; fence the socket so its subscriptions are retired too.
            self.lease = 0
            if self.task is not None:
                self.task.cancel()
        finally:
            self.end_stream(sub.id)

    async def ping(self) -> None:
        nonce = str(uuid4())
        sent = time.monotonic()
        typ, data = await self.exchange(p.FrameType.HEARTBEAT_PING, p.HeartbeatPing(
            nonce=nonce, executor_epoch=self.epoch))
        pong = p.HeartbeatPong.from_wire(data)
        if typ != p.FrameType.HEARTBEAT_PONG or pong.nonce != nonce or pong.executor_epoch != self.epoch:
            raise p.ProtocolError()
        self.lease = sent + pong.lease_seconds

    async def maintain(self) -> None:
        while True:
            await asyncio.sleep(p.HEARTBEAT_SECONDS)
            if self.service.credentials(self.registration.registration_id)[1:] != self._credentials:
                raise Unavailable('auth_failure')
            await self.ping()
            for export_id in tuple(self.matched):
                try:
                    await self.service.sync_index(self, export_id)
                except (p.ProtocolError, identity.AiAccessError):
                    # Missing/invalid ACKs are not cached; retry on the next pass.
                    continue

    async def run(self, *, executor: bool) -> None:
        reader = maintenance = None
        try:
            reg, key, device = self.service.credentials(self.registration.registration_id)
            self._credentials = key, device
            async with self.service.transport.connect(reg.url, key, device) as socket:
                try:
                    self._socket = socket
                    async with asyncio.timeout(10):
                        frame, hello = p.parse_frame(await socket.recv())
                    if frame.type != p.FrameType.HELLO or hello.server_instance_id != reg.server_instance_id:
                        raise p.ProtocolError()
                    self.conn_id = hello.connection_id
                    reader = asyncio.create_task(self.read())
                    if executor:
                        manifest = self.service.manifest(reg.registration_id)
                        reply = await self.control(p.FrameType.EXECUTOR_ATTACH,
                                                   p.ExecutorAttach(executor_epoch=self.epoch, manifest=manifest))
                        if (set(reply) != {'executor_epoch', 'lease_seconds', 'mirrors'}
                                or reply['executor_epoch'] != self.epoch
                                or type(reply['lease_seconds']) is not int or reply['lease_seconds'] != p.LEASE_SECONDS
                                or not isinstance(reply['mirrors'], list) or len(reply['mirrors']) != len(manifest)):
                            raise p.ProtocolError()
                        wanted = {entry.export_id: entry for entry in manifest}
                        for item in reply['mirrors']:
                            entry = wanted.pop(item['export_id'], None)
                            if not entry:
                                raise p.ProtocolError()
                            if (item.get('mirror_status') == 'matched' and item.get('revision') == entry.revision
                                    and item.get('hash') == entry.hash and item.get('state') == 'published'):
                                self.matched.add(entry.export_id)
                        await self.ping()
                        maintenance = asyncio.create_task(self.maintain())
                    self.ready.set()
                    if maintenance is None:
                        await reader
                    else:
                        done, _ = await asyncio.wait((reader, maintenance), return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                finally:
                    # Drop admission before the socket close handshake can await.
                    self.fence()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not log socket exceptions: library messages may include credentials.
            pass
        finally:
            self.fence()
            self.ready.set()  # wake initial enable on failure, too
            for waiter in self.pending.values():
                if not waiter.done():
                    waiter.set_exception(Unavailable('outcome_unknown'))
            workers = [task for task in (reader, maintenance, *self.incoming, *self._stream_tasks)
                       if task is not None]
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


class AiAccessService:
    def __init__(self, workspace: Any, *, transport: Any = None) -> None:
        if transport is None:
            transport = AiTransport()
        self.workspace = workspace
        self.transport = transport
        self.executor = Executor(self.resolve)
        self.intents = IntentStore()
        self.connections: dict[str, _Control] = {}
        self._connection_lock = asyncio.Lock()
        self._events: set[asyncio.Queue] = set()
        self._gap_events: set[asyncio.Queue] = set()
        self._subscriptions: dict[str, _RequestedStream] = {}
        self._recovered: set[Any] = set()
        self._orphan_candidates: dict[str, tuple[str, str, str, bytes]] = {}
        self._closed = False
        self._index_revision = 0

    @staticmethod
    def _profile(profile_id: str) -> Any:
        profile = next((item for item in load_application_config().remote_servers
                        if item.profile_id == profile_id), None)
        if profile is None or identity.normalise_remote_url(profile.url) != profile.url:
            raise Unavailable('profile_not_found')
        return profile

    def _key(self, profile_id: str, url: str | None = None) -> tuple[Any, str]:
        profile = self._profile(profile_id)
        if url is not None and url != profile.url:
            raise Unavailable('stale_target')
        key = load_profile_token(profile.profile_id, profile.url)
        if not key:
            raise Unavailable('auth_failure')
        return profile, key

    def credentials(self, registration_id: str) -> tuple[identity.Registration, str, Any]:
        reg = identity.get_registration(registration_id)
        if reg is None:
            raise Unavailable('not_available')
        _, key = self._key(reg.profile_id, reg.url)
        secret = identity.load_device_secret(reg.url, reg.server_instance_id, reg.user_id, registration_id)
        if not secret:
            raise Unavailable('auth_failure')
        return reg, key, DeviceCredentials(reg.client_id, secret)

    def _resident(self, export: identity.ExportRecord) -> Any:
        descriptor = self.workspace.project_roots.get(canonical_root(export.root))
        if descriptor is None:
            return None
        ctx = descriptor.ctx
        reg = identity.get_registration(export.registration_id)
        if reg is None or (reg.url, reg.server_instance_id, reg.user_id) != (
                export.url, export.server_instance_id, export.user_id):
            return None
        try:
            if self._profile(reg.profile_id).url != reg.url:
                return None
            binding = self._export_binding(ctx, reg, export.persistent_id, export.storage_epoch,
                                           export.identity_revision)
            if export.consent is None:
                # Old rows do not become AI consent for a selected binding on upgrade.
                if binding.selection_state != 'confirmed':
                    return None
            elif (ctx.config.sync.url != binding.url
                    or export.consent.policy_version != _EXPORT_POLICY_VERSION
                    or export.policy_digest != export_policy_digest()
                    or export.consent.binding_fingerprint != _binding_fingerprint(binding)):
                return None
        except (ValueError, OSError):
            return None
        return ctx

    @staticmethod
    def _export_binding(ctx: Any, reg: identity.Registration, persistent_id: str,
                        storage_epoch: str, identity_revision: int) -> BindingRecord:
        try:
            project = ctx.config.project_identity
            binding = RemoteBindings().read().get(ctx.paths.root)
        except (ValueError, OSError):
            raise Unavailable('stale_target') from None
        if (project is None or binding is None
                or (project.id, project.revision) != (persistent_id, identity_revision)
                or (binding.profile_id, binding.url, binding.persistent_id, binding.identity_revision,
                    binding.space_instance_id) != (reg.profile_id, reg.url, persistent_id, identity_revision,
                                                    storage_epoch)):
            raise Unavailable('stale_target')
        return binding

    def resolve(self, target: p.Target) -> Any:
        if self._closed:
            return None
        store = identity.load_registry()
        export = store.exports.get(target.export_id)
        if export is None or identity.export_target(export) != target or not identity.export_published(store, export):
            return None
        return self._resident(export)

    def _open(self) -> None:
        if self._closed:
            raise Unavailable('offline')

    async def capabilities(self, profile_id: str) -> dict:
        self._open()
        profile, key = self._key(profile_id)
        outcome = await self.transport.capabilities(profile.url, key)
        self._open()
        if self._key(profile_id, profile.url) != (profile, key):
            raise Unavailable('stale_target')
        return {'kind': str(outcome.kind), 'code': outcome.code, 'server_instance_id': outcome.server_instance_id}

    async def _discover_for_registration(self, profile_id: str, server_instance_id: str) -> tuple[Any, str]:
        self._open()
        profile, key = self._key(profile_id)
        outcome = await self.transport.capabilities(profile.url, key)
        self._open()
        if outcome.kind != p.DiscoveryKind.SUPPORTED:
            raise Unavailable(outcome.code or str(outcome.kind))
        if outcome.server_instance_id != server_instance_id:
            raise Unavailable('server_instance_mismatch')
        if self._key(profile_id, profile.url) != (profile, key):
            raise Unavailable('stale_target')
        return profile, key

    async def register(self, profile_id: str, display_name: str, *, server_instance_id: str) -> dict:
        profile, key = await self._discover_for_registration(profile_id, server_instance_id)
        if any(reg.url == profile.url and reg.profile_id == profile_id and reg.server_instance_id != server_instance_id
               for reg in identity.list_registrations()):
            raise Unavailable('server_instance_mismatch')
        client_id = identity.ensure_client_id()
        try:
            registration = await self.transport.register(profile.url, key, {
                'kind': 'studio', 'client_id': client_id, 'display_name': display_name})
        except RegistrationExists as conflict:
            self._open()
            if self._key(profile_id, profile.url) != (profile, key):
                raise Unavailable('stale_target') from None
            if identity.get_registration(conflict.registration_id) is None:
                # Only the exact id returned for this installation by its explicit Register
                # can be cleaned up. Nothing is persisted or retried automatically.
                if profile_id not in self._orphan_candidates and len(self._orphan_candidates) >= 32:
                    self._orphan_candidates.pop(next(iter(self._orphan_candidates)))
                self._orphan_candidates[profile_id] = (
                    profile.url, server_instance_id, conflict.registration_id, hashlib.sha256(key.encode()).digest())
            raise
        self._open()
        if isinstance(registration, p.Strict):
            registration = registration.model_dump(mode='json')
        if (registration['server_instance_id'] != server_instance_id or registration['client_id'] != client_id
                or self._key(profile_id, profile.url) != (profile, key)):
            raise Unavailable('stale_target')
        reg = identity.record_registration(profile_id=profile_id, url=profile.url,
            server_instance_id=registration['server_instance_id'], user_id=_remote_user(registration['user_id']) if len(registration['user_id']) == 32
            else p.check_uuid(registration['user_id']),
            registration_id=registration['registration_id'], display_name=display_name)
        identity.save_device_secret(reg.url, reg.server_instance_id, reg.user_id, reg.registration_id,
                                    registration['device_token'], expected_revision=0)
        self._orphan_candidates.pop(profile_id, None)
        return asdict(identity.registration_view(reg))

    async def cleanup_registration(self, profile_id: str, registration_id: str, server_instance_id: str) -> None:
        self._open()
        candidate = self._orphan_candidates.get(profile_id)
        if candidate is None or candidate[1:3] != (server_instance_id, registration_id):
            raise Unavailable('not_available')
        profile, key = await self._discover_for_registration(profile_id, server_instance_id)
        if (self._orphan_candidates.get(profile_id) != candidate
                or (profile.url, hashlib.sha256(key.encode()).digest()) != (candidate[0], candidate[3])
                or identity.get_registration(registration_id) is not None):
            raise Unavailable('stale_target')
        await self.transport.unregister(profile.url, key, registration_id)
        if self._orphan_candidates.get(profile_id) == candidate:
            self._orphan_candidates.pop(profile_id)

    async def unregister(self, registration_id: str, expected_revision: int) -> None:
        # Local removal first; loss of network never reinstates local authority.
        async with self.executor.admission:
            self._open()
            reg = identity.get_registration(registration_id)
            if reg is None:
                raise Unavailable('not_available')
            identity.remove_registration(registration_id, expected_revision=expected_revision)
            control = self.connections.get(registration_id)
        if control is not None:
            await self._stop(control)
        self._open()
        _, key = self._key(reg.profile_id, reg.url)
        await self.transport.unregister(reg.url, key, registration_id)

    def registrations(self) -> list[dict]:
        return [asdict(identity.registration_view(reg)) for reg in identity.list_registrations()]

    async def directory(self, registration_id: str) -> Any:
        reg, key, device = self.credentials(registration_id)
        return await self.transport.directory(reg.url, key, device)

    def manifest(self, registration_id: str) -> list[p.ManifestEntry]:
        return [p.ManifestEntry(export_id=export.export_id, revision=export.export_revision, hash=_wire_mirror(p.FrameType.EXPORT_PUBLISH, identity.publish_frame(export))['hash'])
                for export in identity.list_exports() if export.registration_id == registration_id
                and export.enabled and export.mirror_hash and self._resident(export) is not None]

    async def _connection(self, registration_id: str, *, executor: bool = False) -> _Control:
        async with self._connection_lock:
            if self._closed:
                raise Unavailable('offline')
            reg, _, _ = self.credentials(registration_id)
            current = self.connections.get(registration_id)
            if current is not None and current.alive() and (not executor or current.lease > time.monotonic()):
                return current
            if current is not None:
                await self._stop(current)
            if executor and not reg.publication_enabled:
                raise Unavailable('disabled')
            control = _Control(self, reg)
            self.connections[registration_id] = control
            control.task = asyncio.create_task(control.run(executor=executor))
            try:
                async with asyncio.timeout(12):
                    await control.ready.wait()
                if control._socket is None:
                    raise Unavailable('offline')
            except BaseException:
                await self._stop(control)
                raise
            return control

    async def _stop(self, control: _Control) -> None:
        control.lease = 0
        if control.task is not None:
            control.task.cancel()
            await asyncio.gather(control.task, return_exceptions=True)
        if self.connections.get(control.registration.registration_id) is control:
            self.connections.pop(control.registration.registration_id)

    async def set_publication(self, registration_id: str, enabled: bool, expected_revision: int) -> dict:
        async with self.executor.admission:
            reg = identity.set_publication_enabled(registration_id, enabled, expected_revision=expected_revision)
            if enabled:
                for export in identity.list_exports():
                    ctx = self._resident(export) if export.registration_id == registration_id else None
                    if ctx is not None and ctx.db not in self._recovered:
                        await recover_project(ctx.db)
                        self._recovered.add(ctx.db)
            else:
                control = self.connections.get(registration_id)
                if control is not None:
                    await self._stop(control)
        if enabled:
            await self._connection(registration_id, executor=True)
        return asdict(identity.registration_view(reg))

    async def targets(self, registration_id: str, *, after: str | None = None) -> dict:
        body = {} if after is None else {'after': p.check_uuid(after)}
        control = await self._connection(registration_id)
        return await control.control(p.FrameType.DIRECTORY_LIST, body)

    def exports(self) -> list[dict]:
        return [self._export_view(export) for export in identity.list_exports()]

    @staticmethod
    def _export_view(export: identity.ExportRecord) -> dict:
        return {'target': identity.export_target(export).model_dump(mode='json'), 'enabled': export.enabled,
                'display_name': export.display_name, 'mirror_state': str(export.mirror)}

    async def _catalog_epoch(self, reg: identity.Registration, key: str, persistent_id: str) -> str:
        """Fresh, GET-only catalog evidence; no Documents activation or writes."""
        discovery = await self.transport.capabilities(reg.url, key)
        if discovery.kind != p.DiscoveryKind.SUPPORTED:
            raise Unavailable(discovery.code or str(discovery.kind))
        if discovery.server_instance_id != reg.server_instance_id:
            raise Unavailable('stale_target')
        try:
            async with CatalogHttpTransport() as transport:
                catalog = CatalogClient(transport, url=reg.url, token=key)
                verified = await catalog.verify()
                user_id = verified.user.id
                if (_remote_user(user_id) if len(user_id) == 32 else p.check_uuid(user_id)) != reg.user_id:
                    raise Unavailable('stale_target')
                return (await catalog.get_project(persistent_id)).storage_epoch
        except CatalogError as exc:
            code = {'project_not_found': 'stale_target', 'identity_mismatch': 'stale_target',
                    'unauthorised': 'auth_failure', 'invalid_response': 'invalid_response'}.get(
                        exc.code, 'catalog_unavailable')
            raise Unavailable(code) from None
        except p.ProtocolError:
            raise Unavailable('invalid_response') from None

    async def create_export(self, registration_id: str, project_id: str, display_name: str,
                            *, profile_id: str, server_instance_id: str, project_epoch: str,
                            persistent_id: str, storage_epoch: str, identity_revision: int) -> dict:
        self._open()
        descriptor = self.workspace.project_registry.get(project_id)
        if descriptor is None or descriptor.epoch != project_epoch:
            raise Unavailable('stale_target')
        reg = identity.get_registration(registration_id)
        if reg is None or (reg.profile_id, reg.server_instance_id) != (profile_id, server_instance_id):
            raise Unavailable('stale_target')
        profile, key = self._key(profile_id, reg.url)
        ctx = descriptor.ctx
        binding = self._export_binding(ctx, reg, persistent_id, storage_epoch, identity_revision)
        if ctx.config.sync.url != binding.url:
            raise Unavailable('stale_target')
        if await self._catalog_epoch(reg, key, persistent_id) != storage_epoch:
            raise Unavailable('stale_target')
        async with self.executor.admission:
            # No awaits between rereading local evidence and storing the disabled
            # draft. Binding/profile writers share the personal metadata lock.
            with application_config_transaction():
                self._open()
                if (self.workspace.project_registry.get(project_id) is not descriptor
                        or ctx.config.sync.url != binding.url
                        or self._key(profile_id, reg.url) != (profile, key)
                        or self._export_binding(ctx, reg, persistent_id, storage_epoch, identity_revision) != binding):
                    raise Unavailable('stale_target')
                return self._export_view(identity.create_export(root=ctx.paths.root, registration_id=registration_id,
                    persistent_id=persistent_id, storage_epoch=storage_epoch, identity_revision=identity_revision,
                    display_name=display_name, policy_digest=export_policy_digest(), expected_registration=reg,
                    consent=identity.ExportConsent(policy_version=_EXPORT_POLICY_VERSION,
                                                   binding_fingerprint=_binding_fingerprint(binding))))

    async def publish(self, export_id: str, expected_revision: int) -> dict:
        self._open()
        export = identity.get_export(export_id)
        ctx = self._resident(export) if export and not export.deleted else None
        if ctx is None:
            raise Unavailable('stale_target')
        if export.export_revision != expected_revision:
            raise identity.AiAccessConflict()
        reg = identity.get_registration(export.registration_id)
        if reg is None or not reg.publication_enabled:
            raise Unavailable('publication_disabled')
        credentials = None
        if export.consent is not None:
            credentials = self._key(reg.profile_id, reg.url)
            if await self._catalog_epoch(reg, credentials[1], export.persistent_id) != export.storage_epoch:
                raise Unavailable('stale_target')
        async with self.executor.admission:
            if self._resident(export) is not ctx or identity.get_export(export_id) != export:
                raise Unavailable('stale_target')
            if ctx.db not in self._recovered:
                await recover_project(ctx.db)
                self._recovered.add(ctx.db)
            with application_config_transaction():
                self._open()
                if (self._resident(export) is not ctx or identity.get_registration(export.registration_id) != reg
                        or (credentials is not None and self._key(reg.profile_id, reg.url) != credentials)):
                    raise Unavailable('stale_target')
                body = identity.publish_export(export_id, expected_revision=expected_revision,
                                               expected_export=export, expected_registration=reg)
        control = await self._connection(export.registration_id, executor=True)
        await self._mirror(control, p.FrameType.EXPORT_PUBLISH, body)
        control.matched.add(export_id)
        return self._export_view(identity.get_export(export_id))

    async def unpublish(self, export_id: str, expected_revision: int) -> dict:
        async with self.executor.admission:
            export = identity.get_export(export_id)
            if export is None:
                raise Unavailable('not_available')
            if export.export_revision != expected_revision:
                raise Unavailable('revision_conflict')
            body = identity.unpublish_export(export_id)
            control = self.connections.get(export.registration_id)
            if control is not None:
                control.matched.discard(export_id)
                control.acknowledged_indexes.pop(export_id, None)
                for sid, (sub, _) in tuple(control.served.items()):
                    if sub.request.target.export_id == export_id:
                        control.end_stream(sid)
        if body is not None and control is not None and control.alive():
            await self._mirror(control, p.FrameType.EXPORT_UNPUBLISH, body)
        return self._export_view(identity.get_export(export_id))

    async def _mirror(self, control: _Control, typ: p.FrameType, body: Any) -> None:
        wire = _wire_mirror(typ, body)
        data = await control.control(typ, wire)
        if getattr(body, 'grant_id', None):
            if data.pop('export_revision', None) != body.export_revision:
                raise p.ProtocolError()
        expected_state = {p.FrameType.EXPORT_PUBLISH: 'published', p.FrameType.EXPORT_UNPUBLISH: 'unpublished',
                          p.FrameType.GRANT_PUT: 'active', p.FrameType.GRANT_REVOKE: 'revoked'}[typ]
        if ((data.get('export_id'), data.get('grant_id'), data.get('revision'), data.get('hash'), data.get('state'))
                != (body.export_id, getattr(body, 'grant_id', None), body.revision, wire['hash'], expected_state)):
            raise p.ProtocolError()
        ack = p.MirrorAck.from_wire({**data, 'hash': body.hash,
            'state': 'effective' if expected_state in {'published', 'active'} else expected_state})
        async with self.executor.admission:
            outcome = grants.apply_grant_ack(ack) if ack.grant_id else identity.apply_export_ack(ack)
            if outcome not in {identity.AckOutcome.APPLIED, identity.AckOutcome.DUPLICATE}:
                raise Unavailable('stale_target')

    def list_grants(self, export_id: str) -> list[dict]:
        return [self._grant_view(record) for record in grants.list_grants(export_id)]

    @staticmethod
    def _grant_view(record: identity.GrantRecord) -> dict:
        return {'grant': record.grant.model_dump(mode='json'), 'acked_revision': record.acked_revision}

    @staticmethod
    async def _policy_members(ctx: Any, team_ids: set[str], member_ids: list[str]) -> list[TeamMember]:
        async with ctx.db.session() as session:
            members = [await session.get(TeamMember, member_id) for member_id in member_ids]
        if (any(member is None or member.kind != MemberKind.AGENT
                or member.id in ctx.teams.removing_members for member in members)
                or {member.team_id for member in members} != team_ids):
            raise Unavailable('not_available')
        return members

    async def preview_policy(self, export_id: str, team_id: str, member_id: str) -> dict:
        p.check_local_id(team_id)
        p.check_local_id(member_id)
        async with self.executor.admission:
            self._open()
            export = identity.get_export(export_id)
            ctx = self._resident(export) if export else None
            if ctx is None:
                raise Unavailable('not_available')
            member, = await self._policy_members(ctx, {team_id}, [member_id])
            if identity.get_export(export_id) != export or self._resident(export) is not ctx:
                raise Unavailable('stale_target')
            return {'target': identity.export_target(export).model_dump(mode='json'),
                    'team_id': team_id, 'member_id': member_id, **execution_policy_preview(ctx, member)}

    async def put_grant(self, export_id: str, values: dict, *, expected_export_revision: int,
                        grant_id: str | None = None, expected_revision: int | None = None) -> dict:
        async with self.executor.admission:
            self._open()
            export = identity.get_export(export_id)
            if export is None or export.export_revision != expected_export_revision:
                raise Unavailable('stale_target')
            target = identity.export_target(export)
            ctx = self._resident(export)
            if ctx is None:
                raise Unavailable('stale_target')
            members = await self._policy_members(ctx, set(values['team_ids']), values['member_ids'])
            if identity.get_export(export_id) != export or self._resident(export) is not ctx:
                raise Unavailable('stale_target')
            digests = {execution_policy_digest(ctx, member) for member in members}
            consent = values['execution_policy_digest']
            # ACP/1 has one digest. For a dispatch grant it must cover every member;
            # for a non-dispatch scope a preview of any selected member suffices.
            dispatches = bool(set(values['operations']) & {'run_task', 'run_chat'})
            if consent not in digests or (dispatches and digests != {consent}):
                raise Unavailable('stale_target')
            # Recheck this exact target under the registry write lock, not only the
            # process-local admission lock: another instance can fence the export.
            if grant_id is not None:
                existing = grants.get_grant(grant_id)
                if existing is None or existing.grant.target.export_id != export_id:
                    raise Unavailable('not_available')
                record = grants.update_grant(grant_id, expected_revision=expected_revision,
                                             expected_target=target, now_ms=now_ms(), **values)
            else:
                record = grants.create_grant(export_id, expected_target=target, now_ms=now_ms(), **values)
        control = await self._connection(export.registration_id, executor=True)
        await self._mirror(control, p.FrameType.GRANT_PUT, grants.mirror_frame(record))
        await self.sync_index(control, export_id)
        return self._grant_view(grants.get_grant(record.grant.grant_id))

    async def revoke_grant(self, grant_id: str, expected_revision: int) -> dict:
        async with self.executor.admission:
            record = grants.get_grant(grant_id)
            if record is None:
                raise Unavailable('not_available')
            if record.grant.revision != expected_revision:
                raise Unavailable('revision_conflict')
            body = grants.revoke_grant(grant_id, now_ms=now_ms())
            control = self.connections.get(record.grant.target.executor_registration_id)
            if control is not None:
                for sid, (sub, _) in tuple(control.served.items()):
                    if sub.actor.grant_id == grant_id:
                        control.end_stream(sid)
        if control is not None and control.alive():
            await self._mirror(control, p.FrameType.GRANT_REVOKE, body)
        return self._grant_view(grants.get_grant(grant_id))

    async def sync_index(self, control: _Control, export_id: str) -> None:
        async with control.index_lock:
            try:
                await self._sync_index(control, export_id)
            except BaseException:
                # A cancelled/failed exchange may have committed at the peer. Only an
                # exact successful ACK permits the local cache to suppress retransmission.
                control.acknowledged_indexes.pop(export_id, None)
                raise

    async def _sync_index(self, control: _Control, export_id: str) -> None:
        export = identity.get_export(export_id)
        target = identity.export_target(export) if export else None
        ctx = self.resolve(target) if target else None
        if (ctx is None or export.registration_id != control.registration.registration_id
                or export.server_instance_id != control.registration.server_instance_id):
            raise Unavailable('stale_target')

        def check_current() -> None:
            if (export_id not in control.matched or time.monotonic() >= control.lease
                    or self.resolve(target) is not ctx):
                raise Unavailable('stale_target')

        async def exchange(kind: p.FrameType, body: p.Strict | dict, expected: dict) -> None:
            check_current()
            result = await control.control(kind, body)
            # Canonical comparison rejects extras, absent values and bool-as-int ACKs.
            if p.canonical_json(result) != p.canonical_json(expected):
                raise p.ProtocolError()

        check_current()
        rows = await self.executor.index(ctx, export)
        check_current()
        digest = p.canonical_hash({'kind': 'resource.index', 'rows': rows})
        if control.acknowledged_indexes.get(export_id) == (target, digest):
            return
        control.acknowledged_indexes.pop(export_id, None)
        self._index_revision = max(self._index_revision + 1, now_ms())
        rev = self._index_revision
        staging = {'index_revision': rev, 'state': 'staging'}
        await exchange(p.FrameType.RESOURCE_INDEX_BEGIN, p.IndexBegin(
            export_id=export_id, export_revision=target.export_revision, index_revision=rev,
            row_count=len(rows), hash=digest), staging)
        for start in range(0, len(rows), 16):
            page_no = start // 16
            await exchange(p.FrameType.RESOURCE_INDEX_PAGE,
                {'index_revision': rev, 'page_no': page_no, 'rows': rows[start:start + 16]},
                staging | {'page_no': page_no})
        await exchange(p.FrameType.RESOURCE_INDEX_COMMIT, p.IndexCommit(index_revision=rev, hash=digest),
            {'export_id': export_id, 'export_revision': target.export_revision, 'index_revision': rev,
             'hash': digest, 'row_count': len(rows), 'state': 'committed'})
        check_current()
        control.acknowledged_indexes[export_id] = target, digest

    async def request(self, registration_id: str, request: p.RequestBody) -> dict:
        request.parsed_payload()
        if request.op in p.MUTATIONS:
            self.intents.stage(registration_id, request, now_ms())
        return await self._send_request(registration_id, request)

    async def _send_request(self, registration_id: str, request: p.RequestBody) -> dict:
        control = await self._connection(registration_id)
        if request.target.server_instance_id != control.registration.server_instance_id:
            raise Unavailable('stale_target')
        def register_stream(typ: p.FrameType, data: dict) -> None:
            if typ != p.FrameType.RESPONSE:
                raise p.ProtocolError()
            response = p.ResponseBody.from_wire({'target': request.target.model_dump(mode='json')} | data)
            if response.target != request.target or response.command_id is not None:
                raise p.ProtocolError()
            if response.code is not None:
                return
            result = response.result
            if response.state != p.CommandState.COMPLETED or not result or set(result) != {'subscription_id', 'stream_epoch', 'seq'}:
                raise p.ProtocolError()
            sid = p.check_uuid(result['subscription_id'])
            epoch = p.check_uuid(result['stream_epoch'])
            seq = result['seq']
            if type(seq) is not int or seq < 0 or sid in self._subscriptions:
                raise p.ProtocolError()
            cursor = request.parsed_payload()
            if cursor.stream_epoch is not None and (epoch, seq) != (cursor.stream_epoch, cursor.after_seq):
                raise p.ProtocolError()
            self._subscriptions[sid] = _RequestedStream(control, request, epoch, seq)

        if request.op == p.Operation.STREAM_SUBSCRIBE:
            if len(self._subscriptions) >= 128:
                raise Unavailable('capacity_exceeded')
            # Register in the read loop before it consumes any replay/event frame.
            typ, data = await control.exchange(p.FrameType.REQUEST, _wire_request(request), on_response=register_stream)
        else:
            if request.op == p.Operation.STREAM_UNSUBSCRIBE:
                sid = request.payload['subscription_id']
                existing = self._subscriptions.get(sid)
                if existing is not None and (existing.control, existing.request.target) == (control, request.target):
                    self._end_subscription(sid, notify=False)
            typ, data = await control.exchange(p.FrameType.REQUEST, _wire_request(request))
        if typ != p.FrameType.RESPONSE:
            raise p.ProtocolError()
        # Relay rejections before forwarding may omit target and command.
        data.setdefault('target', request.target.model_dump(mode='json'))
        if request.command_id:
            data.setdefault('command_id', request.command_id)
        response = p.ResponseBody.from_wire(data)
        if response.target != request.target or response.command_id != request.command_id:
            raise p.ProtocolError()
        if request.command_id:
            self.intents.record(request.command_id, response, now_ms())
        return response.model_dump(mode='json', exclude_none=True)

    def receipts(self) -> list[dict]:
        return self.intents.list(now_ms())

    def receipt(self, command_id: str) -> dict:
        return self.intents.get(command_id, now_ms()).view()

    def discard(self, command_id: str) -> None:
        self.intents.discard(command_id, now_ms())

    async def recover_request(self, command_id: str, executor_epoch: str, *, resend: bool = False) -> dict:
        entry = self.intents.get(command_id, now_ms())
        current = entry.request.model_dump(mode='json', exclude_none=True)
        lookup = p.RequestBody.from_wire({key: current[key] for key in ('target', 'grant_id', 'grant_revision')} | {
            'expected_executor_epoch': executor_epoch, 'op': 'command.get', 'payload': {'command_id': command_id}})
        result = await self._send_request(entry.registration_id, lookup)
        if resend and result.get('code') == 'not_available':
            return await self._send_request(entry.registration_id, entry.request)
        if not result.get('code') and result.get('result'):
            receipt = p.ResponseBody.from_wire(result | {'command_id': command_id, 'state': result['result']['state']})
            self.intents.record(command_id, receipt, now_ms())
        return result

    def receive_event(self, control: _Control, body: dict) -> None:
        if set(body) == {'subscription_id', 'target', 'resource_id', 'code'}:
            # The relay sends this on either side of a gap, disconnect or revoke.
            sid = p.check_uuid(body['subscription_id'])
            target = p.Target.from_wire(body['target'])
            resource = p.check_local_id(body['resource_id'])
            if body['code'] not in ('denied', 'resync_required'):
                raise p.ProtocolError()
            served = control.served.get(sid)
            if served is not None and (target, resource) == (served[0].request.target, served[0].feed.stream.resource_id):
                control.end_stream(sid)
            sub = self._subscriptions.get(sid)
            if sub is not None and (control, target, resource) == (sub.control, sub.request.target, sub.resource_id):
                self._end_subscription(sid)
            return
        event = p.EventBody.from_wire(body)
        sub = self._subscriptions.get(event.subscription_id)
        if sub is None or (control, event.target, event.resource_id) != (sub.control, sub.request.target, sub.resource_id):
            return
        if (event.payload == {'code': 'resync_required'} or event.stream_epoch != sub.epoch
                or event.seq != sub.seq + 1):
            self._end_subscription(event.subscription_id)
            # Terminal executor frames are ordinary valid EventBody on ACP/1.
            # Release the relay's capacity on this same requester socket.
            control._track(asyncio.create_task(self._release_remote_stream(sub, event.subscription_id)), stream=True)
            return
        # Never forward unknown payload fields or a transcript on a status topic.
        dto = StatusDTO if sub.request.payload['topic'] == 'status' else projection.TranscriptMessageDTO
        projected = dto.from_wire(event.payload)
        if projected.resource_id != sub.resource_id:
            raise p.ProtocolError()
        sub.seq = event.seq
        self._emit({'event': 'remote', 'data': event.model_dump(mode='json')})

    async def _release_remote_stream(self, sub: _RequestedStream, sid: str) -> None:
        request = sub.request.model_copy(update={'op': p.Operation.STREAM_UNSUBSCRIBE,
                                                 'payload': {'subscription_id': sid}})
        try:
            if sub.control.alive():
                await sub.control.exchange(p.FrameType.REQUEST, _wire_request(request))
        except (OSError, identity.AiAccessError, p.ProtocolError):
            pass  # the local cursor is already invalid and never used again

    def _end_subscription(self, subscription_id: str, *, notify: bool = True) -> None:
        if self._subscriptions.pop(subscription_id, None) is None:
            return
        for queue in self._events:
            buffered = [queue.get_nowait() for _ in range(queue.qsize())]
            for event in buffered:
                if event['event'] != 'remote' or event['data']['subscription_id'] != subscription_id:
                    queue.put_nowait(event)
        if notify:
            self._emit({'event': 'resync_required', 'data': {'subscription_id': subscription_id}})

    def _emit(self, event: dict) -> None:
        for queue in self._events:
            if queue in self._gap_events:
                continue
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({'event': 'resync_required', 'data': {}})
                # The consumer closes this stream after delivering the gap marker.
                self._gap_events.add(queue)
            else:
                queue.put_nowait(event)

    async def events(self):
        if len(self._events) >= 16:
            raise Unavailable('busy')
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._events.add(queue)
        try:
            while not self._closed:
                try:
                    async with asyncio.timeout(15):
                        event = await queue.get()
                except TimeoutError:
                    event = {'event': 'heartbeat', 'data': {}}
                if event['event'] == 'remote' and event['data']['subscription_id'] not in self._subscriptions:
                    continue
                yield event
                if queue in self._gap_events:
                    return
        finally:
            self._events.discard(queue)
            self._gap_events.discard(queue)

    def disconnected(self, control: _Control) -> None:
        for key, sub in tuple(self._subscriptions.items()):
            if sub.control is control:
                self._end_subscription(key)

    def close_admission(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._orphan_candidates.clear()
        for control in self.connections.values():
            control.fence()
        self._emit({'event': 'resync_required', 'data': {}})

    async def close(self) -> None:
        self.close_admission()
        async with self._connection_lock:
            for connection in tuple(self.connections.values()):
                await self._stop(connection)
        await self.executor.close_streams()
