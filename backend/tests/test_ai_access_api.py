"""The local browser boundary for AI access; no real Remote or provider."""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from voxarium_studio.ai_access import identity, projection, protocol as p, service as ai_service
from voxarium_studio.ai_access.transport import AiTransport
from voxarium_studio.core.application_config import RemoteServerProfile
from voxarium_studio.core.config import commit_disabled_identity, read_project_config_snapshot
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project_identity import ProjectIdentity
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.main import create_app
from voxarium_studio.models.teams import MemberKind, TeamMember
from voxarium_studio.sync import catalog_runtime, credentials
from voxarium_studio.sync.bindings import BindingRecord, RemoteBindings, bindings_file
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.profiles import RemoteProfiles


def test_dormant_singleton_and_local_auth(token_client):
    uri = '/api/ai-access/registrations'
    auth = {'Authorization': 'Bearer t0ken'}
    assert token_client.get(uri).status_code == 401
    assert token_client.get(uri, headers={**auth, 'Origin': 'https://untrusted.invalid'}).status_code == 403
    assert token_client.get(uri, headers={**auth, 'Host': 'untrusted.invalid'}).status_code == 403
    assert token_client.get(uri, headers=auth).json() == {'registrations': []}
    assert token_client.app.state.ai_access.connections == {}
    assert token_client.get('/api/ai-access/events?token=t0ken', headers=auth).status_code == 400


def test_authenticated_targets_pagination_keeps_query_scoped(token_client, monkeypatch):
    registration, *cursors = sorted(str(uuid4()) for _ in range(10))
    entries = [{'grant': {'grant_id': cursor}} for cursor in cursors]
    after = cursors[5]
    first = {'targets': entries[:6], 'next_after': after}
    second = {'targets': entries[6:], 'next_after': None}
    empty = {'targets': [], 'next_after': None}
    calls = []

    class Relay:
        async def control(self, kind, body):
            assert kind == p.FrameType.DIRECTORY_LIST
            calls.append(body)
            return {None: first, after: second, cursors[-1]: empty}[body.get('after')]

    async def connect(registration_id):
        assert registration_id == registration
        return Relay()

    monkeypatch.setattr(token_client.app.state.ai_access, '_connection', connect)
    route = f'/api/ai-access/registrations/{registration}/targets'
    auth = {'Authorization': 'Bearer t0ken'}
    assert token_client.get(route, params={'after': after}).status_code == 401
    assert token_client.get(route, headers={**auth, 'Origin': 'https://other.invalid'}).status_code == 403
    for invalid in ('after=', 'after', 'after=not-a-uuid', 'after=null', 'target=ignored',
                    f'after={after}&after={after}', f'after={after}&other=x', f'after={after}&'):
        response = token_client.get(route + '?' + invalid, headers=auth)
        assert (response.status_code, response.json()) == (400, {'detail': 'invalid_request'})
    assert token_client.get('/api/ai-access/registrations', params={'after': after}, headers=auth).status_code == 400
    assert token_client.get(route.replace('/targets', '/directory'), params={'after': after}, headers=auth).status_code == 400
    assert not calls
    assert token_client.get(route, headers=auth).json() == first
    assert token_client.get(route, params={'after': after}, headers=auth).json() == second
    assert token_client.get(route, params={'after': cursors[-1]}, headers=auth).json() == empty
    assert calls == [{}, {'after': after}, {'after': cursors[-1]}]


def test_request_body_is_strict(client):
    uri = '/api/ai-access/requests'
    assert client.post(uri, json={'url': 'https://untrusted.invalid', 'method': 'POST'}).status_code == 422
    uid = uuid4()
    assert client.post(uri, content=f'{{"registration_id":"{uid}","registration_id":"{uid}","request":{{}}}}',
                       headers={'Content-Type': 'application/json'}).status_code == 422
    assert client.app.state.ai_access.connections == {}


def _publish_for_consent(export_id, expected_revision):
    frame = identity.publish_export(export_id, expected_revision=expected_revision)
    assert identity.apply_export_ack(p.MirrorAck(
        export_id=export_id, revision=frame.revision, hash=frame.hash,
        state=p.MirrorState.EFFECTIVE,
    )) == identity.AckOutcome.APPLIED
    return frame.revision


@pytest.fixture
def grant_consent(token_client, monkeypatch):
    state = token_client.app.state
    service = state.ai_access
    ctx = state.ctx

    async def member_ids():
        async with ctx.db.session() as session:
            member = (await session.exec(select(TeamMember).where(TeamMember.kind == MemberKind.AGENT))).first()
            assert member is not None
            return member.team_id, member.id

    team_id, member_id = token_client.portal.call(member_ids)
    profile = RemoteServerProfile(profile_id=str(uuid4()), url='https://relay.invalid/prefix', name='Fixture')
    project = ProjectIdentity(id='fixture', revision=1)
    ctx.config.project_identity = project
    RemoteBindings().save(BindingRecord.for_root(ctx.paths.root, profile=profile, identity=project,
        selection_state='confirmed', space_instance_id='a' * 32), expected_fingerprint=None)
    identity.ensure_client_id()
    reg = identity.record_registration(profile_id=profile.profile_id, url=profile.url,
        server_instance_id=str(uuid4()), user_id=str(uuid4()), registration_id=str(uuid4()), display_name='Executor')
    identity.set_publication_enabled(reg.registration_id, True, expected_revision=reg.revision)
    export = identity.create_export(root=ctx.paths.root, registration_id=reg.registration_id,
        persistent_id=project.id, storage_epoch='a' * 32, identity_revision=project.revision,
        display_name='Fixture', policy_digest='a' * 64)
    _publish_for_consent(export.export_id, export.export_revision)
    monkeypatch.setattr(service, '_profile', lambda _: profile)

    effects = []

    class Relay:
        async def control(self, kind, body):
            assert kind == p.FrameType.GRANT_PUT
            effects.append(('mirror', body['grant_id'], body['revision'], body['export_revision']))
            return {key: body[key] for key in ('export_id', 'export_revision', 'grant_id', 'revision', 'hash')} | {
                'state': 'active'}

    relay = Relay()

    async def connect(registration_id, *, executor):
        assert registration_id == reg.registration_id and executor
        effects.append(('connection', registration_id))
        return relay

    async def index(control, export_id):
        assert control is relay and export_id == export.export_id
        effects.append(('index', export_id))

    monkeypatch.setattr(service, '_connection', connect)
    monkeypatch.setattr(service, 'sync_index', index)
    path = f'/api/ai-access/exports/{export.export_id}'
    auth = {'Authorization': 'Bearer t0ken'}

    def preview():
        response = token_client.get(f'{path}/teams/{team_id}/members/{member_id}/execution-policy', headers=auth)
        assert response.status_code == 200, response.text
        shown = response.json()
        assert shown['target']['export_id'] == export.export_id
        assert (shown['team_id'], shown['member_id']) == (team_id, member_id)
        return {'operations': ['view_status', 'run_task'], 'team_ids': [shown['team_id']],
                'member_ids': [shown['member_id']], 'execution_policy_digest': shown['execution_policy_digest'],
                'expected_export_revision': shown['target']['export_revision']}

    grantee = {'grantee_user_id': str(uuid4()), 'grantee_registration_id': str(uuid4())}

    def mutate(options, *, grant_id=None):
        if grant_id is not None:
            return token_client.put(f'{path}/grants/{grant_id}', headers=auth, json=options)
        return token_client.post(f'{path}/grants', headers=auth, json=options | grantee)

    return export.export_id, preview, mutate, effects


@pytest.mark.parametrize('update', [False, True], ids=['create', 'update'])
def test_grant_requires_a_strict_positive_export_revision(grant_consent, update):
    _, preview, mutate, effects = grant_consent
    options = preview()
    grant_id = None
    if update:
        created = mutate(options)
        assert created.status_code == 201, created.text
        grant = created.json()['grant']
        grant_id = grant['grant_id']
        options['expected_revision'] = grant['revision']

    before = identity.load_registry()
    prior_effects = effects.copy()
    revision = options.pop('expected_export_revision')
    invalid = [{}] + [{'expected_export_revision': value}
                      for value in (None, True, 0, -1, float(revision), str(revision))]
    for override in invalid:
        response = mutate(options | override, grant_id=grant_id)
        assert (response.status_code, response.json()) == (422, {'detail': 'invalid_request'})
        assert identity.load_registry() == before
        assert effects == prior_effects


def test_republished_export_requires_fresh_consent_even_when_the_policy_is_unchanged(grant_consent):
    export_id, preview, mutate, effects = grant_consent
    old = preview()
    assert not effects
    created = mutate(old)
    assert created.status_code == 201, created.text
    old_grant = created.json()['grant']

    withdrawn = identity.unpublish_export(export_id)
    assert withdrawn is not None
    assert identity.apply_export_ack(p.MirrorAck(
        export_id=export_id, revision=withdrawn.revision, hash=withdrawn.hash,
        state=p.MirrorState.UNPUBLISHED,
    )) == identity.AckOutcome.APPLIED
    republished_revision = _publish_for_consent(export_id, withdrawn.revision)
    assert republished_revision > old['expected_export_revision']

    def reject(options, code, *, grant_id=None):
        before = identity.load_registry()
        prior_effects = effects.copy()
        response = mutate(options, grant_id=grant_id)
        assert (response.status_code, response.json()) == (409, {'detail': code})
        # Compare the durable grant and mirror records as well as the connection/relay/index calls.
        assert identity.load_registry() == before
        assert effects == prior_effects

    reject(old, 'stale_target')
    reject(old | {'expected_revision': old_grant['revision']}, 'stale_target', grant_id=old_grant['grant_id'])

    fresh = preview()
    assert fresh == old | {'expected_export_revision': republished_revision}
    # Fresh consent cannot retarget an old grant; it permits a new grant on this publication.
    reject(fresh | {'expected_revision': old_grant['revision']}, 'stale_target', grant_id=old_grant['grant_id'])
    created = mutate(fresh)
    assert created.status_code == 201, created.text
    assert created.json()['acked_revision'] == 1
    grant = created.json()['grant']
    assert grant['target']['export_revision'] == republished_revision
    grant_id = grant['grant_id']
    change = {'operations': ['view_status'], 'expected_revision': grant['revision']}

    # A grant on the current target still needs the caller's current target consent.
    # Testing only the historical grant could pass via the grant's own target fence.
    reject(old | change, 'stale_target', grant_id=grant_id)
    updated = mutate(fresh | change, grant_id=grant_id)
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body['acked_revision'] == body['grant']['revision'] == grant['revision'] + 1
    assert body['grant']['operations'] == ['view_status']
    assert body['grant']['target'] == grant['target']
    reject(fresh | change, 'ai_access_conflict', grant_id=grant_id)


@pytest.fixture
def selected_ai(project, monkeypatch):
    paths = ProjectPaths(project)
    snapshot = read_project_config_snapshot(paths)
    revision = snapshot.config.project_identity.revision if snapshot.config.project_identity else 0
    commit_disabled_identity(paths, identity=ProjectIdentity(id='fixture', revision=revision + 1),
                             url='', expected_fingerprint=snapshot.fingerprint)
    profile = RemoteProfiles().create('Catalog fixture', 'https://relay.invalid/prefix')
    credentials.save_profile_token(profile.profile_id, profile.url, 'fixture-key', expected_revision=0)
    server, user, registration = (str(uuid4()) for _ in range(3))
    remote = SimpleNamespace(storage_epoch='a' * 32, server=server, user=user, on_detail=None, requests=[])

    def catalog(request):
        suffix = request.url.path.removeprefix('/prefix')
        assert request.method == 'GET' and str(request.url).startswith(profile.url + '/')
        remote.requests.append(suffix)
        if suffix == '/healthz':
            assert 'authorization' not in request.headers
            return httpx.Response(200, json={'service': 'voxarium_studio_remote', 'version': 'fixture',
                'protocol': 2, 'catalog_api': 2, 'supported_protocols': [1, 2]})
        assert request.headers['authorization'] == 'Bearer fixture-key'
        if suffix == '/api/v2/me':
            return httpx.Response(200, json={'protocol': 2, 'catalog_api': 2,
                'capabilities': ['project_catalog', 'explicit_project_create', 'strict_connect', 'project_snapshot'],
                'user': {'id': remote.user.replace('-', ''), 'display_name': 'Owner', 'color': '#123456'}})
        if suffix == '/api/v2/projects/fixture/snapshot':
            return httpx.Response(200, json={'protocol': 2, 'snapshot': {'id': 'fixture',
                'storage_epoch': remote.storage_epoch, 'seq': 0}, 'documents': [], 'next_after': None})
        assert suffix == '/api/v2/projects/fixture'
        if remote.on_detail:
            remote.on_detail()
        return httpx.Response(200, json={'protocol': 2, 'project': {'id': 'fixture', 'display_name': 'Fixture',
            'storage_epoch': remote.storage_epoch, 'metadata_revision': 1, 'created_at': 'fixture'}})

    async def capabilities(_transport, url, key):
        assert (url, key) == (profile.url, 'fixture-key')
        remote.requests.append('/ai/capabilities')
        return p.DiscoveryOutcome(p.DiscoveryKind.SUPPORTED, server_instance_id=remote.server)

    async def register(_transport, url, key, body):
        assert (url, key) == (profile.url, 'fixture-key')
        return {'server_instance_id': server, 'user_id': user, 'client_id': body['client_id'],
                'registration_id': registration, 'device_token': 'vxd_' + 'a' * 43}

    def transport():
        return CatalogHttpTransport(transport=httpx.MockTransport(catalog))

    monkeypatch.setattr(ai_service, 'CatalogHttpTransport', transport)
    monkeypatch.setattr(catalog_runtime, 'CatalogHttpTransport', transport)
    monkeypatch.setattr(AiTransport, 'capabilities', capabilities)
    monkeypatch.setattr(AiTransport, 'register', register)
    with TestClient(create_app(str(project), auth=AuthSettings(token='owner')), base_url='http://127.0.0.1') as client:
        client.headers['Authorization'] = 'Bearer owner'
        route = '/api/ai-access/exports'
        result = client.post('/api/ai-access/registrations', json={
            'profile_id': profile.profile_id, 'server_instance_id': server, 'display_name': 'Local fixture'})
        assert result.status_code == 201, result.text
        registration_view = result.json()
        descriptor, = client.app.state.project_registry.values()
        remote_path = f'/api/projects/{descriptor.project_id}/remote'
        shown = client.get(remote_path).json()
        preconditions = {'expected_revision': shown['identity_revision'],
                         'config_fingerprint': shown['config_fingerprint']}
        result = client.post(remote_path + '/prepare', json=preconditions | {'operation': 'select',
            'profile_id': profile.profile_id, 'url': profile.url, 'persistent_id': 'fixture',
            'catalog_id': 'fixture', 'catalog_instance_id': remote.storage_epoch})
        assert result.status_code == 200, result.text
        prepared = result.json()
        change = remote_path + '/changes/' + prepared['change_id']
        result = client.post(change + '/quiesce', json={'expected_generation': shown['binding_generation']})
        assert result.status_code == 200 and result.json()['state'] == 'ready', result.text
        result = client.post(change + '/commit', json=preconditions | {
            'expected_generation': shown['binding_generation'] + 1,
            'confirmation': {k: prepared['to'][k] for k in ('url', 'persistent_id')}})
        assert result.status_code == 200, result.text
        shown = client.get(remote_path).json()
        assert not shown['enabled'] and RemoteBindings().read().get(project).selection_state == 'selected'
        assert client.get(route).json() == {'exports': []}
        remote.requests.clear()

        def documents():
            return paths.config_file.read_bytes(), bindings_file().read_bytes()

        yield SimpleNamespace(client=client, path=route, remote=remote, project=project, documents=documents,
            service=client.app.state.ai_access, registration=registration_view,
            body={'registration_id': registration, 'profile_id': shown['profile_id'], 'server_instance_id': server,
                  'project_id': shown['project_id'], 'project_epoch': shown['epoch'],
                  'persistent_id': shown['persistent_id'], 'storage_epoch': shown['space_instance_id'],
                  'identity_revision': shown['identity_revision'], 'display_name': 'Local fixture'})


def test_selected_is_not_consent_until_public_ai_draft(selected_ai, monkeypatch):
    rig = selected_ai
    before = rig.documents()
    assert rig.client.post(rig.path, headers={'Authorization': 'Bearer wrong'}, json=rig.body).status_code == 401
    bad_digest = rig.client.post(rig.path, json=rig.body | {'policy_digest': 'b' * 64})
    assert (bad_digest.status_code, bad_digest.json()) == (422, {'detail': 'invalid_request'})
    for override in ({'profile_id': str(uuid4())}, {'server_instance_id': str(uuid4())},
                     {'identity_revision': rig.body['identity_revision'] + 1}, {'persistent_id': 'other'},
                     {'project_epoch': 'b' * 32}, {'storage_epoch': 'b' * 32}):
        response = rig.client.post(rig.path, json=rig.body | override)
        assert (response.status_code, response.json()) == (409, {'detail': 'stale_target'})
    assert rig.remote.requests == []
    assert not identity.list_exports()
    legacy = identity.ExportRecord.from_wire({
        'export_id': str(uuid4()), 'root': str(rig.project), 'url': rig.registration['url'],
        'server_instance_id': rig.registration['server_instance_id'], 'user_id': rig.registration['user_id'],
        'registration_id': rig.registration['registration_id'], 'persistent_id': rig.body['persistent_id'],
        'storage_epoch': rig.body['storage_epoch'], 'identity_revision': rig.body['identity_revision'],
        'display_name': 'No implicit consent', 'export_revision': 1, 'policy_digest': 'a' * 64,
    })
    assert legacy.consent is None and rig.service._resident(legacy) is None

    result = rig.client.post(rig.path, json=rig.body)
    assert result.status_code == 201 and result.headers['Cache-Control'] == 'no-store', result.text
    draft = result.json()
    assert draft['enabled'] is False and draft['mirror_state'] == 'unpublished'
    stored, = identity.list_exports()
    assert (stored.export_id, stored.policy_digest) == (draft['target']['export_id'], ai_service.export_policy_digest())
    assert stored.consent.policy_version == 1 and stored.acked_revision == stored.mirror_revision == 0
    assert not identity.load_registry().grants and not rig.service.connections
    assert not rig.service.manifest(rig.body['registration_id'])
    assert rig.documents() == before
    assert rig.remote.requests == ['/ai/capabilities', '/healthz', '/api/v2/me', '/api/v2/projects/fixture']

    # A move in the server catalog cannot be silently accepted at Publish either.
    identity.set_publication_enabled(rig.body['registration_id'], True,
                                     expected_revision=rig.registration['revision'])
    rig.remote.storage_epoch = 'b' * 32
    result = rig.client.put(rig.path + '/' + stored.export_id + '/publication',
                            json={'enabled': True, 'expected_revision': stored.export_revision})
    assert (result.status_code, result.json()) == (409, {'detail': 'stale_target'})
    assert identity.get_export(stored.export_id) == stored and not rig.service.connections
    assert rig.documents() == before

    class Relay:
        matched = set()
        published = []

        async def control(self, kind, body):
            assert kind == p.FrameType.EXPORT_PUBLISH and body['policy_digest'] == stored.policy_digest
            self.published.append(body)
            return {k: body[k] for k in ('export_id', 'revision', 'hash')} | {'state': 'published'}

    async def connect(registration, *, executor):
        assert registration == rig.body['registration_id'] and executor
        return relay

    relay = Relay()
    monkeypatch.setattr(rig.service, '_connection', connect)
    rig.remote.storage_epoch = stored.storage_epoch
    result = rig.client.put(rig.path + '/' + stored.export_id + '/publication',
                            json={'enabled': True, 'expected_revision': stored.export_revision})
    assert result.status_code == 200 and result.json()['mirror_state'] == 'effective', result.text
    target = p.Target.from_wire(result.json()['target'])
    assert rig.service.resolve(target) is not None and rig.documents() == before
    assert len(relay.published) == 1 and not identity.load_registry().grants

    # An observed local rebind fences even an acknowledged AI export.
    rows = RemoteBindings().read()
    binding = rows.get(rig.project)
    RemoteBindings().save(binding.model_copy(update={'space_instance_id': 'b' * 32}),
                          expected_fingerprint=rows.fingerprint)
    assert rig.service.resolve(target) is None and not rig.service.manifest(rig.body['registration_id'])


@pytest.mark.parametrize('changed', ['server', 'user', 'storage_epoch', 'binding'])
def test_ai_draft_rechecks_exact_catalog_and_binding(selected_ai, changed):
    rig = selected_ai
    before = rig.documents()
    previous_registry = identity.load_registry()
    if changed == 'binding':
        def move_binding():
            rows = RemoteBindings().read()
            current = rows.get(rig.project)
            RemoteBindings().save(current.model_copy(update={'space_instance_id': 'b' * 32}),
                                  expected_fingerprint=rows.fingerprint)

        rig.remote.on_detail = move_binding
    else:
        setattr(rig.remote, changed, 'b' * 32 if changed == 'storage_epoch' else str(uuid4()))
    result = rig.client.post(rig.path, json=rig.body)
    assert (result.status_code, result.json()) == (409, {'detail': 'stale_target'})
    assert identity.load_registry() == previous_registry and not rig.service.connections
    assert rig.documents()[0] == before[0]
    if changed != 'binding':
        assert rig.documents() == before


def test_events_use_fetch_sse(client, monkeypatch):
    server, executor, export, subscription, epoch = (str(uuid4()) for _ in range(5))
    target = p.Target(server_instance_id=server, executor_registration_id=executor, export_id=export,
                      persistent_id='fixture', storage_epoch='a' * 32, export_revision=1)
    payload = projection.TranscriptMessageDTO(resource_id='task_fixture', message_id='msg_fixture',
        role='user', kind='note', text='Safe fixture', created_at=0, origin='local', delivery='delivered',
        total_bytes=12, offset=0, content_omitted=False)
    projected = p.EventBody(subscription_id=subscription, target=target, resource_id=payload.resource_id,
        stream_epoch=epoch, seq=1, resource_revision=2, payload=payload.model_dump(mode='json'))
    content = projected.model_dump(mode='json')

    async def events():
        yield {'event': 'heartbeat', 'data': {}}
        yield {'event': 'remote', 'data': content}
        yield {'event': 'resync_required', 'data': {'subscription_id': subscription}}

    monkeypatch.setattr(client.app.state.ai_access, 'events', events)
    with client.stream('GET', '/api/ai-access/events') as response:
        assert response.status_code == 200
        assert response.headers['Content-Type'].startswith('text/event-stream')
        assert response.headers['Cache-Control'] == 'no-store'
        lines = list(response.iter_lines())
        assert lines[:3] == [': heartbeat', '', 'event: remote']
        assert json.loads(lines[3].removeprefix('data: ')) == content
        assert lines[4:6] == ['', 'event: resync_required']
        assert json.loads(lines[6].removeprefix('data: ')) == {'subscription_id': subscription}
        assert lines[7:] == ['']
