"""Offline ACP discovery, relay wire and intent persistence fixtures."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from sqlmodel import select

from voxarium_studio.ai_access import grants, identity, projection, protocol as p
from voxarium_studio.ai_access.executor import execution_policy_digest, now_ms
from voxarium_studio.ai_access.service import AiAccessService, Unavailable, _forward, _wire_request
from voxarium_studio.ai_access.transport import AiTransport, DeviceCredentials, TransportError
from voxarium_studio.api import api_router
from voxarium_studio.api.ai_access import router as ai_access_router
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.models.teams import MemberKind, TeamMember


def uid():
    return str(uuid4())


@pytest.mark.parametrize('status,raw,kind,code', [
    (404, b'', 'unsupported', 'not_mounted'),
    (401, b'', 'auth_failure', 'auth_failure'),
    (302, b'', 'error', 'redirect_refused'),
    (200, b'{"acp":1,"acp":1}', 'error', 'invalid_response'),
    (200, b'{"acp":true}', 'error', 'invalid_response'),
])
async def test_capability_outcomes(status, raw, kind, code):
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(status, content=raw, headers={'Location': 'https://other.invalid'})

    transport = AiTransport(http_transport=httpx.MockTransport(server))
    result = await transport.capabilities('https://relay.invalid/base', 'fake-token')
    assert (result.kind, result.code) == (kind, code)
    assert len(requests) == 1
    req = requests[0]
    assert str(req.url) == 'https://relay.invalid/base/api/ai/v1/capabilities'
    assert req.headers['Authorization'] == 'Bearer fake-token'
    assert req.headers['Cache-Control'] == 'no-store'
    assert 'x-vxs-client-id' not in req.headers and 'x-vxs-device-token' not in req.headers


@pytest.mark.parametrize('token', [
    'vxd_' + 'A' * 43,
    'vxd_' + 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLM-_01',
])
async def test_register_accepts_and_preserves_exact_remote_wire_token(token):
    response = {'server_instance_id': uid(), 'registration_id': uid(), 'client_id': uid(),
                'user_id': uuid4().hex, 'device_token': token}
    transport = AiTransport(http_transport=httpx.MockTransport(lambda _: httpx.Response(201, json=response)))
    result = await transport.register('https://relay.invalid', 'fake-key', {})
    assert result == response
    assert result['device_token'] == token


@pytest.mark.parametrize('token', [
    'A' * 43,  # missing prefix
    'Vxd_' + 'A' * 43,
    'vxd_' + 'A' * 42,
    'vxd_' + 'A' * 44,
    'vxd_' + 'A' * 42 + '=',
    'vxd_' + 'A' * 42 + '\u00e9',
    'vxd_' + 'A' * 43 + '\n',
    'prefix_vxd_' + 'A' * 43,
    None,
    123,
])
async def test_register_rejects_malformed_wire_token(token):
    response = {'server_instance_id': uid(), 'registration_id': uid(), 'client_id': uid(),
                'user_id': uuid4().hex, 'device_token': token}
    transport = AiTransport(http_transport=httpx.MockTransport(lambda _: httpx.Response(201, json=response)))
    with pytest.raises(TransportError, match='invalid_response'):
        await transport.register('https://relay.invalid', 'fake-key', {})


async def test_browser_registration_stores_full_wire_token_without_returning_it():
    profile_id, server, registration, user = uid(), uid(), uid(), uuid4()
    token = 'vxd_' + 'aB0-_' * 8 + 'xyz'
    profile = SimpleNamespace(url='https://relay.invalid/base')
    requests = []

    def relay(request):
        requests.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json={'acp': 1, 'server_instance_id': server,
                                              'ai_control': {'versions': [1]}})
        body = p.parse_json(request.content)
        return httpx.Response(201, json={'server_instance_id': server, 'registration_id': registration,
                                         'client_id': body['client_id'], 'user_id': user.hex,
                                         'device_token': token})

    service = AiAccessService(None, transport=AiTransport(http_transport=httpx.MockTransport(relay)))
    service._key = lambda *_: (profile, 'fake-key')
    app = FastAPI()
    app.state.ai_access = service
    app.state.auth = SimpleNamespace(is_loopback=False)
    app.include_router(ai_access_router, prefix='/api')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/ai-access/registrations', json={
                'profile_id': profile_id, 'server_instance_id': server, 'display_name': 'Fixture'})
        assert response.status_code == 201
        assert response.json()['has_device_token'] is True
        assert 'device_token' not in response.json()
        assert token not in response.text
        assert identity.load_device_secret(profile.url, server, str(user), registration) == token
        assert token not in identity.REGISTRY.path.read_text(encoding='utf-8')
        assert [request.method for request in requests] == ['GET', 'POST']
    finally:
        await service.close()


async def test_negative_discovery_does_not_register():
    class Relay:
        async def capabilities(self, *_):
            return p.DiscoveryOutcome(p.DiscoveryKind.UNSUPPORTED)

    service = AiAccessService(None, transport=Relay())
    service._key = lambda *_: (SimpleNamespace(url='https://relay.invalid'), 'token')
    with pytest.raises(Unavailable, match='unsupported'):
        await service.register(uid(), 'Fixture', server_instance_id=uid())
    assert identity.client_id() is None
    assert service.connections == {}
    await service.close()


async def test_duplex_fake_relay_presaves_intent_and_pins_wire():
    server, registration, requester = uid(), uid(), uid()
    target = p.Target(server_instance_id=server, executor_registration_id=uid(), export_id=uid(),
        persistent_id='demo', storage_epoch='0' * 32, export_revision=1)
    request = p.RequestBody(target=target, grant_id=uid(), grant_revision=1, expected_executor_epoch=uid(),
        op=p.Operation.TASK_CREATE, command_id=uid(), issued_at=now_ms(),
        payload={'team_id': 'team', 'assignee_id': 'member', 'title': 'Draft', 'brief': 'private prompt', 'locale': 'en'})
    seen = []

    class Relay:
        @asynccontextmanager
        async def connect(self, url, key, device):
            assert url == 'https://relay.invalid/base' and device.client_id == requester
            yield self

        async def recv(self):
            return await incoming.get()

        async def send(self, raw):
            frame = p.Frame.from_wire(p.parse_json(raw))
            seen.append(frame.type)
            assert frame.type == p.FrameType.REQUEST
            assert service.receipt(request.command_id)['state'] == 'outcome_unknown'
            assert frame.body == _wire_request(request)
            assert 'assignee_id' not in frame.body['payload'] and frame.body['payload']['member_id'] == 'member'
            await incoming.put(p.canonical_json({'acp': 1, 'type': 'response', 'request_id': frame.request_id,
                'body': {'target': frame.body['target'], 'command_id': request.command_id,
                         'state': 'completed', 'result': {'resource_id': 'task_test'}}}).decode())

    incoming = asyncio.Queue()
    await incoming.put(p.canonical_json({'acp': 1, 'type': 'hello', 'request_id': uid(), 'body': {
        'acp': 1, 'server_instance_id': server, 'connection_id': uid(), 'lease_seconds': 15,
        'heartbeat_seconds': 5}}).decode())
    service = AiAccessService(None, transport=Relay())
    reg = SimpleNamespace(url='https://relay.invalid/base', registration_id=registration, server_instance_id=server)
    service.credentials = lambda _: (reg, 'fake-key', DeviceCredentials(requester, 'fake-secret'))
    try:
        assert service.connections == {} and not seen
        assert (await service.request(registration, request))['state'] == 'completed'
        assert 'private prompt' not in str(service.receipt(request.command_id))
        assert seen == ['request']
    finally:
        await service.close()


@pytest.mark.parametrize('relay_reply, status, receipt_state', [
    pytest.param({'state': 'rejected', 'code': 'offline'}, 200, 'rejected', id='offline-before-forward'),
    pytest.param({'state': 'rejected', 'code': 'not_available'}, 200, 'rejected', id='foreign-grant'),
    pytest.param({'state': 'outcome_unknown', 'code': 'outcome_unknown'}, 200, 'outcome_unknown', id='relay-ack-timeout'),
    pytest.param(None, 503, 'outcome_unknown', id='connection-lost-after-send'),
    pytest.param({'state': 'rejected', 'code': 'unexpected_code'}, 422, 'outcome_unknown', id='unknown-code'),
])
async def test_browser_preserves_relay_command_outcome(monkeypatch, relay_reply, status, receipt_state):
    server, registration, requester = uid(), uid(), uid()
    remote_url = 'https://relay.invalid/base'
    device_token = 'vxd_' + 'X' * 43
    target = {'server_instance_id': server, 'executor_registration_id': uid(), 'export_id': uid(),
              'persistent_id': 'fixture', 'storage_epoch': 'a' * 32, 'export_revision': 1}
    command = {'target': target, 'command_id': uid()}
    request = command | {'grant_id': uid(), 'grant_revision': 1, 'expected_executor_epoch': uid(),
                         'op': 'task.start', 'issued_at': now_ms(), 'expected_resource_revision': 1,
                         'payload': {'resource_id': 'task_fixture'}}
    connections, frames = [], []
    incoming = asyncio.Queue()

    # Script only the relay carrier. In particular do not construct a Studio ResponseBody
    # here: Remote emits a text envelope with target + command_id for these responses.
    def push(kind, request_id, body):
        incoming.put_nowait(p.canonical_json({'acp': 1, 'type': kind, 'request_id': request_id, 'body': body}).decode())

    class Socket:
        async def recv(self):
            frame = await incoming.get()
            if frame is None:
                raise ConnectionResetError('fixture: connection lost before receipt')
            return frame

        async def send(self, raw):
            frame = p.parse_json(raw)
            frames.append(frame)
            if relay_reply is None:
                incoming.put_nowait(None)
            else:
                push('response', frame['request_id'], command | relay_reply)

    @asynccontextmanager
    async def connect(url, *, additional_headers, **_options):
        connections.append((url, additional_headers))
        push('hello', uid(), {'acp': 1, 'server_instance_id': server, 'connection_id': uid(),
                              'lease_seconds': 15, 'heartbeat_seconds': 5})
        yield Socket()

    def unexpected_http(_request):
        pytest.fail('command attempted an HTTP fallback')

    service = AiAccessService(None, transport=AiTransport(
        ws_connect=connect, http_transport=httpx.MockTransport(unexpected_http)))
    reg = SimpleNamespace(url=remote_url, registration_id=registration, server_instance_id=server)
    monkeypatch.setattr(service, 'credentials', lambda _: (reg, 'fake-key', DeviceCredentials(requester, device_token)))
    app = FastAPI()
    app.state.ai_access = service
    app.state.auth = AuthSettings(token='local-fixture-token')
    app.include_router(api_router)
    origin, path = 'http://127.0.0.1', '/api/ai-access/requests'
    credentials = {'Authorization': 'Bearer local-fixture-token'}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                     headers={'Origin': origin}) as client:
            assert (await client.post(path, json={'registration_id': registration, 'request': request})).status_code == 401
            assert not connections
            async with asyncio.timeout(3):
                reply = await client.post(path, headers=credentials,
                                          json={'registration_id': registration, 'request': request})
            persisted = await client.get(f"/api/ai-access/receipts/{command['command_id']}", headers=credentials)

        assert reply.status_code == status
        expected = command | relay_reply if status == 200 else None
        assert reply.json() == (expected if expected is not None else {
            'detail': 'outcome_unknown' if relay_reply is None else 'invalid_request',
        })
        assert reply.headers['Cache-Control'] == 'no-store'
        assert persisted.status_code == 200
        assert persisted.json()['state'] == receipt_state
        assert persisted.json()['response'] == expected
        assert len(connections) == 1
        url, headers = connections[0]
        assert url == 'wss://relay.invalid/base/api/ai/v1/control'
        assert (headers['Authorization'], headers['X-VXS-Client-ID'], headers['X-VXS-Device-Token']) == (
            'Bearer fake-key', requester, device_token)
        # Only the pinned mutation was sent: no replay, command lookup, or alternate target.
        assert len(frames) == 1
        assert frames[0] == {'acp': 1, 'type': 'request', 'request_id': frames[0]['request_id'],
                             'body': request | {'payload': {}, 'resource_id': 'task_fixture'}}
    finally:
        await service.close()


def test_forward_uses_authenticated_remote_principal():
    server, epoch, user = uid(), uid(), uuid4()
    target = p.Target(server_instance_id=server, executor_registration_id=uid(), export_id=uid(),
        persistent_id='demo', storage_epoch='0' * 32, export_revision=1)
    request = p.RequestBody(target=target, grant_id=uid(), grant_revision=1, expected_executor_epoch=epoch,
        op=p.Operation.CHAT_SEND, command_id=uid(), issued_at=now_ms(), expected_resource_revision=1,
        payload={'resource_id': 'chat_test', 'text': 'hello'})
    forward = _forward({'source': {'user_id': user.hex, 'registration_id': uid(), 'connection_id': uid()},
        'grant': {'grant_id': request.grant_id, 'revision': 1, 'expires_at': now_ms() + 2000},
        'deadline': now_ms() + 1000, 'request': _wire_request(request)}, epoch)
    assert forward.actor().user_id == str(user)
    assert forward.request == request


class ExecutorRelay:
    """An in-memory relay peer speaking the real ACP/1 socket envelopes."""

    def __init__(self, registration):
        self.registration = registration
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.requester_connection = uid()
        self.executor_epoch = None
        self.index = None
        self.index_commits = []
        self.corrupt_index_ack = False
        self.event_blocked = asyncio.Event()
        self.event_release = asyncio.Event()
        self.event_release.set()

    @asynccontextmanager
    async def connect(self, url, _key, device):
        assert (url, device.client_id) == (self.registration.url, self.registration.client_id)
        self.push(p.FrameType.HELLO, uid(), {'acp': 1,
            'server_instance_id': self.registration.server_instance_id, 'connection_id': uid(),
            'lease_seconds': 15, 'heartbeat_seconds': 5})
        yield self

    def push(self, kind, rid, body):
        self.incoming.put_nowait(p.canonical_json({'acp': 1, 'type': kind, 'request_id': rid, 'body': body}))

    async def recv(self):
        return await self.incoming.get()

    async def send(self, raw):
        frame = p.Frame.from_wire(p.parse_json(raw))
        body = frame.body
        if frame.type == p.FrameType.EXECUTOR_ATTACH:
            self.executor_epoch = body['executor_epoch']
            self.push(p.FrameType.RESPONSE, frame.request_id, {'state': 'completed', 'result': {
                'executor_epoch': body['executor_epoch'], 'lease_seconds': 15,
                'mirrors': [entry | {'state': 'published', 'mirror_status': 'matched'} for entry in body['manifest']],
            }})
        elif frame.type == p.FrameType.HEARTBEAT_PING:
            self.push(p.FrameType.HEARTBEAT_PONG, frame.request_id, body | {'lease_seconds': 15})
        elif frame.type in {p.FrameType.GRANT_REVOKE, p.FrameType.GRANT_PUT,
                           p.FrameType.EXPORT_PUBLISH, p.FrameType.EXPORT_UNPUBLISH}:
            result = {k: v for k, v in body.items()
                      if k in ('grant_id', 'export_id', 'export_revision', 'revision', 'hash')}
            result['state'] = {p.FrameType.GRANT_REVOKE: 'revoked', p.FrameType.GRANT_PUT: 'active',
                               p.FrameType.EXPORT_PUBLISH: 'published', p.FrameType.EXPORT_UNPUBLISH: 'unpublished'}[frame.type]
            self.push(p.FrameType.RESPONSE, frame.request_id, {'state': 'completed', 'result': result})
        elif frame.type in {p.FrameType.RESOURCE_INDEX_BEGIN, p.FrameType.RESOURCE_INDEX_PAGE,
                           p.FrameType.RESOURCE_INDEX_COMMIT}:
            revision = body['index_revision']
            result = {'index_revision': revision, 'state': 'staging'}
            if frame.type == p.FrameType.RESOURCE_INDEX_BEGIN:
                self.index = body
            else:
                assert self.index is not None and revision == self.index['index_revision']
                if frame.type == p.FrameType.RESOURCE_INDEX_PAGE:
                    result['page_no'] = body['page_no']
                else:
                    assert body['hash'] == self.index['hash']
                    self.index_commits.append(self.index)
                    result = self.index | {'state': 'committed'}
                    if self.corrupt_index_ack:
                        result['export_revision'] -= 1
                    self.index = None
            self.push(p.FrameType.RESPONSE, frame.request_id, {'state': 'completed', 'result': result})
        else:
            if frame.type == p.FrameType.EVENT and not self.event_release.is_set():
                self.event_blocked.set()
                await self.event_release.wait()
            self.outgoing.put_nowait(frame)

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), timeout=3)

    async def request(self, grant, op, payload):
        request = p.RequestBody(target=grant.target, grant_id=grant.grant_id,
                                grant_revision=grant.revision, expected_executor_epoch=self.executor_epoch,
                                op=op, payload=payload)
        wire = _wire_request(request)
        del wire['expected_executor_epoch']  # ACP reads need not specify the executor's current epoch.
        rid = uid()
        self.push(p.FrameType.FORWARD, rid, {'source': {
            'user_id': grant.grantee_user_id.replace('-', ''), 'registration_id': grant.grantee_registration_id,
            'connection_id': self.requester_connection},
            'grant': {'grant_id': grant.grant_id, 'revision': grant.revision, 'expires_at': grant.expires_at},
            'deadline': now_ms() + 3000, 'request': wire})
        frame = await self.next()
        assert (frame.type, frame.request_id) == (p.FrameType.RESPONSE, rid), frame
        return p.ResponseBody.from_wire(frame.body)

    async def event(self):
        frame = await self.next()
        assert frame.type == p.FrameType.EVENT, frame
        return p.EventBody.from_wire(frame.body)

    async def quiet(self, *, subscription_id=None):
        # Assert absence on the wire, not just that a requester-side filter hides data.
        # An optional content-free terminal signal is allowed for a server-side fence.
        try:
            async with asyncio.timeout(0.25):
                frame = await self.outgoing.get()
                if subscription_id is not None:
                    assert frame.type == p.FrameType.EVENT
                    body = frame.body
                    assert body['subscription_id'] == subscription_id
                    assert (body.get('payload') == {'code': 'resync_required'} or
                            (set(body) == {'subscription_id', 'target', 'resource_id', 'code'} and
                             body['code'] in ('denied', 'resync_required'))), body
                    frame = await self.outgoing.get()
                pytest.fail(f'unexpected frame on executor socket: {frame!r}')
        except TimeoutError:
            pass


@pytest.fixture
async def stream_peer(ctx, monkeypatch):
    async with ctx.db.session() as session:
        member = (await session.exec(select(TeamMember).where(TeamMember.kind == MemberKind.AGENT))).first()
    # These are inert local cards; no jobs, providers or network connections are involved.
    async def task(title):
        return await ctx.teams.create_task(team_id=member.team_id, assignee_id=member.id,
                                           title=title, brief='Fixture brief', auto_start=False)

    selected, foreign = await task('Selected fixture'), await task('Foreign fixture')
    identity.ensure_client_id()
    reg = identity.record_registration(profile_id=uid(), url='https://relay.invalid/stream', server_instance_id=uid(),
        user_id=uid(), registration_id=uid(), display_name='Fixture executor')
    reg = identity.set_publication_enabled(reg.registration_id, True, expected_revision=reg.revision)
    digest = execution_policy_digest(ctx, member)
    export = identity.create_export(root=ctx.paths.root, registration_id=reg.registration_id,
        persistent_id='fixture', storage_epoch='a' * 32, identity_revision=1,
        display_name='Fixture', policy_digest=digest)
    published = identity.publish_export(export.export_id, expected_revision=export.export_revision)
    identity.apply_export_ack(p.MirrorAck(export_id=export.export_id, revision=published.revision,
        hash=published.hash, state=p.MirrorState.EFFECTIVE))

    def grant(*permissions):
        record = grants.create_grant(export.export_id, grantee_user_id=uid(), grantee_registration_id=uid(),
            operations=permissions, team_ids=[member.team_id], member_ids=[member.id],
            resource_scope=p.ResourceScope.SELECTED_RESOURCES, resource_ids=[selected.id],
            execution_policy_digest=digest, now_ms=now_ms())
        grants.apply_grant_ack(p.MirrorAck(export_id=export.export_id, grant_id=record.grant.grant_id,
            revision=record.grant.revision, hash=record.mirror_hash, state=p.MirrorState.EFFECTIVE))
        return record.grant

    relay = ExecutorRelay(reg)
    service = AiAccessService(None, transport=relay)
    # Substitute only local profile/binding discovery; resolution and grant checks remain real.
    monkeypatch.setattr(service, '_resident', lambda pinned: ctx if pinned.export_id == export.export_id else None)
    monkeypatch.setattr(service, 'credentials', lambda _: (reg, 'fixture-key', DeviceCredentials(reg.client_id, 'fixture-secret')))
    try:
        transcript = grant(p.Permission.VIEW_STATUS, p.Permission.VIEW_TRANSCRIPT)
        status = grant(p.Permission.VIEW_STATUS)
        control = await service._connection(reg.registration_id, executor=True)
        assert control.live(transcript.target)
        await service.sync_index(control, export.export_id)
        yield SimpleNamespace(relay=relay, service=service, control=control,
                              transcript=transcript, status=status, selected=selected.id, foreign=foreign.id)
    finally:
        relay.event_release.set()
        await service.close()


def cursor(snapshot):
    assert snapshot.state == 'completed', snapshot
    return {'stream_epoch': p.check_uuid(snapshot.result['stream_epoch']), 'after_seq': snapshot.result['seq']}


async def subscribe(peer, *, grant=None, resource=None, topic='transcript', **after):
    return await peer.relay.request(grant or peer.transcript, p.Operation.STREAM_SUBSCRIBE,
                                   {'resource_id': resource or peer.selected, 'topic': topic} | after)


async def page(peer):
    return await peer.relay.request(peer.transcript, p.Operation.TRANSCRIPT_PAGE, {'resource_id': peer.selected})


def subscription(response):
    assert response.state == 'completed', response
    p.check_uuid(response.result['stream_epoch'])
    assert type(response.result['seq']) is int
    return p.check_uuid(response.result['subscription_id'])


async def test_republish_requires_fresh_index_ack_without_reconnect(stream_peer):
    peer = stream_peer
    relay, control, previous = peer.relay, peer.control, peer.status
    target = previous.target
    payload = {'resource_id': peer.selected}
    assert (await relay.request(previous, p.Operation.RESOURCE_GET, payload)).state == 'completed'
    initial, = relay.index_commits
    assert initial['row_count'] == 1

    withdrawn = await peer.service.unpublish(target.export_id, target.export_revision)
    published = await peer.service.publish(target.export_id, withdrawn['target']['export_revision'])
    revision = published['target']['export_revision']
    assert revision > target.export_revision
    assert target.export_id not in control.acknowledged_indexes
    assert (await relay.request(previous, p.Operation.RESOURCE_GET, payload)).code == 'not_available'

    # Peer receives the commit but returns a stale ACK; a valid mirror grant alone
    # cannot authorize opening the resource until a subsequent index is confirmed.
    relay.corrupt_index_ack = True
    with pytest.raises(p.ProtocolError):
        await peer.service.put_grant(target.export_id, {
            'grantee_user_id': previous.grantee_user_id, 'grantee_registration_id': previous.grantee_registration_id,
            'operations': ['view_status'], 'team_ids': previous.team_ids, 'member_ids': previous.member_ids,
            'execution_policy_digest': previous.execution_policy_digest, 'resource_scope': previous.resource_scope,
            'resource_ids': previous.resource_ids,
        }, expected_export_revision=revision)
    old, new = sorted(grants.list_grants(target.export_id), key=lambda record: record.grant.target.export_revision)[-2:]
    assert old.grant.target.export_revision == target.export_revision and new.acked_revision == new.grant.revision
    assert target.export_id not in control.acknowledged_indexes
    assert (await relay.request(new.grant, p.Operation.RESOURCE_GET, payload)).code == 'not_available'
    invalid = relay.index_commits[-1]
    assert (invalid['export_revision'], invalid['hash']) == (revision, initial['hash'])

    relay.corrupt_index_ack = False
    await peer.service.sync_index(control, target.export_id)
    retried = relay.index_commits[-1]
    assert (retried['export_revision'], retried['hash']) == (revision, initial['hash'])
    assert retried['index_revision'] > invalid['index_revision']
    await peer.service.sync_index(control, target.export_id)
    assert relay.index_commits == [initial, invalid, retried]
    assert peer.service.connections[target.executor_registration_id] is control
    assert (await relay.request(new.grant, p.Operation.RESOURCE_GET, payload)).state == 'completed'
    assert (await relay.request(previous, p.Operation.RESOURCE_GET, payload)).code == 'not_available'


async def test_executor_snapshot_watermark_then_live_and_reconnect(ctx, stream_peer):
    peer = stream_peer
    status = await peer.relay.request(peer.status, p.Operation.RESOURCE_GET, {'resource_id': peer.selected})
    cursor(status)
    opened = cursor(await page(peer))
    # A committed message between snapshot and subscribe must be replayed on the same socket.
    first = await ctx.teams.add_note(peer.selected, 'between snapshot and subscribe')
    sid = subscription(await subscribe(peer, **opened))
    event = await peer.relay.event()
    assert (event.subscription_id, event.target, event.resource_id, event.stream_epoch, event.seq) == (
        sid, peer.transcript.target, peer.selected, opened['stream_epoch'], opened['after_seq'] + 1)
    assert first.id in str(event.payload)

    foreign = await ctx.teams.add_note(peer.foreign, 'out-of-scope transcript canary')
    handoff = 'https://claude.ai/connect/private-canary'
    second = await ctx.teams.add_note(peer.selected, f'live fixture {handoff}')
    event = await peer.relay.event()
    assert event.subscription_id == sid and event.seq == opened['after_seq'] + 2
    assert second.id in str(event.payload) and 'live fixture' in str(event.payload)
    assert handoff not in str(event.payload) and foreign.id not in str(event.payload)
    assert 'out-of-scope transcript canary' not in str(event.payload)

    # Remote closes the old requester socket and signals the executor using this terminal envelope.
    peer.relay.push(p.FrameType.EVENT, uid(), {'subscription_id': sid, 'target': event.target.model_dump(mode='json'),
                    'resource_id': peer.selected, 'code': 'resync_required'})
    peer.relay.requester_connection = uid()
    third = await ctx.teams.add_note(peer.selected, 'written during reconnect')
    resumed = subscription(await subscribe(peer, stream_epoch=event.stream_epoch, after_seq=event.seq))
    replay = await peer.relay.event()
    assert resumed != sid and replay.subscription_id == resumed
    assert (replay.stream_epoch, replay.seq) == (event.stream_epoch, event.seq + 1)
    assert third.id in str(replay.payload) and second.id not in str(replay.payload)
    # Knowing a subscription UUID is not authority to cancel it on a different socket.
    owner = peer.relay.requester_connection
    peer.relay.requester_connection = uid()
    foreign_end = await peer.relay.request(peer.transcript, p.Operation.STREAM_UNSUBSCRIBE, {'subscription_id': resumed})
    assert foreign_end.code == 'not_available'
    peer.relay.requester_connection = owner
    ended = await peer.relay.request(peer.transcript, p.Operation.STREAM_UNSUBSCRIBE, {'subscription_id': resumed})
    assert ended.state == 'completed', ended
    await ctx.teams.add_note(peer.selected, 'after unsubscribe')
    await peer.relay.quiet()


async def test_executor_rejects_scope_and_stops_on_revoke_or_lease_loss(ctx, stream_peer):
    peer = stream_peer
    for response in (await subscribe(peer, resource=peer.foreign),
                     await subscribe(peer, grant=peer.status)):
        assert response.code in ('denied', 'not_available') and response.result is None
    revoked = subscription(await subscribe(peer))
    active = subscription(await subscribe(peer, grant=peer.status, topic='status'))
    await peer.service.revoke_grant(peer.transcript.grant_id, peer.transcript.revision)
    await ctx.teams.add_note(peer.selected, 'after revoke canary')
    # Stay online with a different grant: only its status event may be emitted.
    await ctx.teams.update_task(peer.selected, {'title': 'public status only'})
    frame = await peer.relay.next()
    if frame.body['subscription_id'] == revoked:
        assert frame.body['payload'] == {'code': 'resync_required'}
        frame = await peer.relay.next()
    assert frame.type == p.FrameType.EVENT and frame.body['subscription_id'] == active
    assert 'after revoke canary' not in str(frame.body)
    peer.control.lease = 0
    await ctx.teams.update_task(peer.selected, {'title': 'after lease canary'})
    await peer.relay.quiet(subscription_id=active)


@pytest.mark.parametrize('reason', ['unknown epoch', 'future sequence', 'history evicted', 'history expired'])
async def test_executor_reports_replay_gap(ctx, stream_peer, monkeypatch, reason):
    peer = stream_peer
    original = projection.ProjectionStream.__init__
    clock = 100.0

    def bounded(self, *args, **kwargs):
        original(self, *args, **(kwargs | {'replay_frames': 2, 'clock': lambda: clock}))

    monkeypatch.setattr(projection.ProjectionStream, '__init__', bounded)
    opened = cursor(await page(peer))
    if reason == 'unknown epoch':
        opened['stream_epoch'] = uid()
    elif reason == 'future sequence':
        opened['after_seq'] += 1000
    else:
        sid = subscription(await subscribe(peer, **opened))
        for i in range(3 if reason == 'history evicted' else 1):
            note = await ctx.teams.add_note(peer.selected, f'eviction fixture {i}')
            event = await peer.relay.event()
            assert event.subscription_id == sid and note.id in str(event.payload)
        ended = await peer.relay.request(peer.transcript, p.Operation.STREAM_UNSUBSCRIBE, {'subscription_id': sid})
        assert ended.state == 'completed', ended
        if reason == 'history expired':
            clock += 60.001
    failed = await subscribe(peer, **opened)
    assert failed.code == 'resync_required' and failed.result is None


async def test_executor_terminates_overflowed_subscription(ctx, stream_peer, monkeypatch):
    peer = stream_peer
    original_init = projection.Subscription.__init__
    original_push = projection.Subscription._push
    overflow = asyncio.Event()

    def bounded(self, *args, **kwargs):
        original_init(self, *args, **(kwargs | {'max_frames': 2}))

    def observe_overflow(self, frame):
        original_push(self, frame)
        if self.resync_required:
            overflow.set()

    monkeypatch.setattr(projection.Subscription, '__init__', bounded)
    monkeypatch.setattr(projection.Subscription, '_push', observe_overflow)
    sid = subscription(await subscribe(peer))
    peer.relay.event_release.clear()
    try:
        first = await ctx.teams.add_note(peer.selected, 'already in-flight')
        await asyncio.wait_for(peer.relay.event_blocked.wait(), timeout=3)
        async with asyncio.timeout(3):
            for i in range(3):
                await ctx.teams.add_note(peer.selected, f'overflow canary {i}')
            await overflow.wait()
    finally:
        peer.relay.event_release.set()
    event = await peer.relay.event()
    if first.id in str(event.payload):  # one write had already entered socket.send()
        event = await peer.relay.event()
    assert event.subscription_id == sid and event.payload == {'code': 'resync_required'}
    await ctx.teams.add_note(peer.selected, 'after overflow')
    await peer.relay.quiet()

async def test_requester_accepts_immediate_replay_and_purges_on_relay_revoke():
    server, registration, client_id, epoch, sid = (uid() for _ in range(5))
    target = p.Target(server_instance_id=server, executor_registration_id=uid(), export_id=uid(),
                      persistent_id='fixture', storage_epoch='a' * 32, export_revision=1)
    target_wire = target.model_dump(mode='json')
    resource = 'task_public'
    incoming = asyncio.Queue()

    def receive(kind, body, rid=None):
        incoming.put_nowait(p.canonical_json({'acp': 1, 'type': kind, 'request_id': rid or uid(), 'body': body}))

    def event(seq, text, *, resource_id=resource):
        payload = projection.TranscriptMessageDTO(resource_id=resource_id, message_id=f'msg_{seq}',
            role='user', kind='note', text=text, created_at=now_ms(), origin='local', delivery='delivered',
            total_bytes=len(text.encode()), offset=0, content_omitted=False)
        receive(p.FrameType.EVENT, {'subscription_id': sid, 'target': target_wire, 'resource_id': resource_id,
                                    'stream_epoch': epoch, 'seq': seq, 'resource_revision': 1,
                                    'payload': payload.model_dump(mode='json')})

    class Relay:
        @asynccontextmanager
        async def connect(self, _url, _key, _device):
            receive(p.FrameType.HELLO, {'acp': 1, 'server_instance_id': server, 'connection_id': uid(),
                                       'lease_seconds': 15, 'heartbeat_seconds': 5})
            yield self

        async def recv(self):
            return await incoming.get()

        async def send(self, raw):
            frame = p.Frame.from_wire(p.parse_json(raw))
            assert frame.type == p.FrameType.REQUEST
            if frame.body['op'] == 'stream.subscribe':
                assert frame.body['resource_id'] == resource
                assert frame.body['payload'] == {'topic': 'transcript', 'cursor': f'{epoch}:0'}
                receive(p.FrameType.RESPONSE, {'state': 'completed', 'target': target_wire, 'result': {
                    'subscription_id': sid, 'stream_epoch': epoch, 'seq': 0}}, frame.request_id)
                # No scheduling gap between the subscribe response and the first event.
                event(1, 'foreign canary', resource_id='task_private')
                event(1, 'valid replay')
            else:
                assert frame.body['op'] == 'resource.get'
                receive(p.FrameType.RESPONSE, {'state': 'completed', 'target': target_wire, 'result': {
                    'stream_epoch': epoch, 'seq': 2}}, frame.request_id)

    service = AiAccessService(None, transport=Relay())
    reg = SimpleNamespace(url='https://relay.invalid', registration_id=registration, server_instance_id=server)
    service.credentials = lambda _: (reg, 'fixture-key', DeviceCredentials(client_id, 'fixture-secret'))
    base = dict(target=target, grant_id=uid(), grant_revision=1, expected_executor_epoch=uid())
    browser = service.events()
    first = asyncio.create_task(anext(browser))
    try:
        result = await service.request(registration, p.RequestBody(**base, op=p.Operation.STREAM_SUBSCRIBE,
            payload={'resource_id': resource, 'topic': 'transcript', 'stream_epoch': epoch, 'after_seq': 0}))
        assert result['result']['subscription_id'] == sid
        delivered = await asyncio.wait_for(first, 3)
        assert delivered['event'] == 'remote' and delivered['data']['seq'] == 1
        assert delivered['data']['payload']['text'] == 'valid replay'

        event(2, 'queued-before-revoke canary')
        receive(p.FrameType.EVENT, {'subscription_id': sid, 'target': target_wire, 'resource_id': resource,
                                    'code': 'denied'})
        event(3, 'after-revoke canary')
        # Barrier: the reader has processed all earlier socket frames before returning this response.
        await service.request(registration, p.RequestBody(**base, op=p.Operation.RESOURCE_GET,
                              payload={'resource_id': resource}))
        assert await asyncio.wait_for(anext(browser), 3) == {
            'event': 'resync_required', 'data': {'subscription_id': sid}}
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(browser), 0.25)
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await browser.aclose()
        await service.close()
