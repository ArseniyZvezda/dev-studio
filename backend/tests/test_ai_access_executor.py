"""Executor admission using the real project transaction and only a fake backend."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import select

from voxarium_studio.ai_access import grants, identity, ledger, protocol as p
from voxarium_studio.ai_access.executor import Executor, execution_policy_digest, now_ms
from voxarium_studio.ai_access.service import AiAccessService, Unavailable, _Control
from voxarium_studio.api import api_router
from voxarium_studio.core.application_config import RemoteServerProfile
from voxarium_studio.core.project_identity import ProjectIdentity
from voxarium_studio.core.project_scope import canonical_root
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.sync.bindings import BindingRecord, RemoteBindings
from voxarium_studio.models.teams import AgentChatMessage, MemberKind, Team, TeamMember, TeamTask
from voxarium_studio.teams.backends.base import AgentEvent, register_backend


def uid():
    return str(uuid4())


class Backend:
    supports_effort = False
    calls = 0
    prompts = []

    def is_available(self):
        return True, 'fixture'

    async def run(self, request):
        type(self).calls += 1
        type(self).prompts.append(request.prompt)
        yield AgentEvent(kind='result', text='ok', is_final=True, data={'is_error': False})


@pytest.fixture
async def acp(ctx):
    register_backend('acp_fixture', Backend)
    Backend.calls = 0
    Backend.prompts = []
    async with ctx.db.session() as session:
        team = (await session.exec(select(Team))).first()
        team.autostart_tasks = True
        session.add(team)
        member = (await session.exec(select(TeamMember).where(TeamMember.kind == MemberKind.AGENT))).first()
        member.agent_backend = 'acp_fixture'
        member.model = 'fixture-model'
        member.isolate_worktree = False
        session.add(member)
    identity.ensure_client_id()
    reg = identity.record_registration(profile_id=uid(), url='https://relay.invalid/prefix',
        server_instance_id=uid(), user_id=uid(), registration_id=uid(), display_name='Executor')
    identity.set_publication_enabled(reg.registration_id, True, expected_revision=reg.revision)
    digest = execution_policy_digest(ctx, member)
    export = identity.create_export(root=ctx.paths.root, registration_id=reg.registration_id,
        persistent_id='fixture', storage_epoch='a' * 32, identity_revision=1,
        display_name='Fixture', policy_digest=digest)
    frame = identity.publish_export(export.export_id, expected_revision=export.export_revision)
    identity.apply_export_ack(p.MirrorAck(export_id=export.export_id, revision=frame.revision,
        hash=frame.hash, state=p.MirrorState.EFFECTIVE))
    record = grants.create_grant(export.export_id, grantee_user_id=uid(), grantee_registration_id=uid(),
        operations=[p.Permission.VIEW_STATUS, p.Permission.CREATE_TASK, p.Permission.RUN_TASK,
                    p.Permission.CREATE_CHAT, p.Permission.RUN_CHAT, p.Permission.WRITE_TASK],
        team_ids=[team.id], member_ids=[member.id], execution_policy_digest=digest, now_ms=now_ms())
    grants.apply_grant_ack(p.MirrorAck(export_id=export.export_id, grant_id=record.grant.grant_id,
        revision=record.grant.revision, hash=record.mirror_hash, state=p.MirrorState.EFFECTIVE))
    executor = Executor(lambda target: ctx if target == record.grant.target else None)
    try:
        yield ctx, executor, record.grant, team, member, uid()
    finally:
        await executor.close_streams()


def forward(acp, op, payload, *, revision=None):
    _, _, grant, _, _, epoch = acp
    issued = now_ms()
    return p.ForwardBody(request=p.RequestBody(target=grant.target, grant_id=grant.grant_id,
        grant_revision=grant.revision, expected_executor_epoch=epoch, op=op, payload=payload,
        issued_at=issued, command_id=uid(), expected_resource_revision=revision),
        source_user_id=grant.grantee_user_id, source_registration_id=grant.grantee_registration_id,
        connection_id=uid(), current_grant_revision=grant.revision, deadline_ms=issued + 10_000)


async def handle(acp, message, *, live=lambda: True):
    return await acp[1].handle(message, executor_epoch=acp[-1], live=live)


async def new_chat(acp):
    _, _, _, team, member, _ = acp
    response = await handle(acp, forward(acp, p.Operation.CHAT_CREATE, {
        'team_id': team.id, 'member_id': member.id, 'title': 'Fixture chat', 'locale': 'en'}))
    assert response.state == p.CommandState.COMPLETED, response
    return response.result['resource_id']


async def test_local_policy_preview_and_fresh_consent(ctx, monkeypatch):
    register_backend('acp_fixture', Backend)
    Backend.calls = 0
    async with ctx.db.session() as session:
        member = (await session.exec(select(TeamMember).where(TeamMember.kind == MemberKind.AGENT))).first()
        member.agent_backend = 'acp_fixture'
        member.run_settings_override = True
        member.permission_mode = 'plan'
        member.max_turns = 7
    profile = RemoteServerProfile(profile_id=uid(), url='https://relay.invalid/prefix', name='Fixture')
    project = ProjectIdentity(id='fixture', revision=1)
    ctx.config.project_identity = project
    RemoteBindings().save(BindingRecord.for_root(ctx.paths.root, profile=profile, identity=project,
        selection_state='confirmed', space_instance_id='a' * 32), expected_fingerprint=None)
    ctx.paths.mcp_file.write_text(json.dumps({'mcpServers': {'fixture': {
        'command': 'fixture-command-canary', 'args': ['fixture-argv-canary'],
        'env': {'PRIVATE_VALUE': 'fixture-env-canary'},
    }}}), encoding='utf-8')
    identity.ensure_client_id()
    reg = identity.record_registration(profile_id=profile.profile_id, url=profile.url,
        server_instance_id=uid(), user_id=uid(), registration_id=uid(), display_name='Executor')
    identity.set_publication_enabled(reg.registration_id, True, expected_revision=reg.revision)
    export = identity.create_export(root=ctx.paths.root, registration_id=reg.registration_id,
        persistent_id=project.id, storage_epoch='a' * 32, identity_revision=project.revision,
        display_name='Fixture', policy_digest='a' * 64)
    frame = identity.publish_export(export.export_id, expected_revision=export.export_revision)
    identity.apply_export_ack(p.MirrorAck(export_id=export.export_id, revision=frame.revision,
        hash=frame.hash, state=p.MirrorState.EFFECTIVE))

    app = FastAPI()
    app.include_router(api_router)
    app.state.auth = AuthSettings(token='owner-fixture')
    root = canonical_root(ctx.paths.root)
    service = AiAccessService(SimpleNamespace(project_roots={root: SimpleNamespace(ctx=ctx)}))
    app.state.ai_access = service
    monkeypatch.setattr(service, '_profile', lambda _: profile)
    network = []

    class Relay:
        async def control(self, kind, body):
            assert kind == p.FrameType.GRANT_PUT
            network.append(kind)
            return {key: body[key] for key in ('export_id', 'export_revision', 'grant_id', 'revision', 'hash')} | {
                'state': 'active'}

    async def connect(registration_id, *, executor):
        assert registration_id == reg.registration_id and executor
        return Relay()

    async def index(_control, export_id):
        assert export_id == export.export_id

    monkeypatch.setattr(service, '_connection', connect)
    monkeypatch.setattr(service, 'sync_index', index)
    path = f'/api/ai-access/exports/{export.export_id}'
    member_path = f'/teams/{member.team_id}/members/{member.id}/execution-policy'
    auth = {'Authorization': 'Bearer owner-fixture'}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            preview = path + member_path
            assert (await client.get(preview)).status_code == 401
            assert (await client.get(preview, headers=auth | {'Origin': 'https://other.invalid'})).status_code == 403
            assert (await client.get(preview + '?member_id=x', headers=auth)).status_code == 400
            assert (await client.get(path + f'/teams/wrong/members/{member.id}/execution-policy', headers=auth)).status_code == 404
            response = await client.get(preview, headers=auth)
            assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
            data = response.json()
            digest = p.check_sha256(data.pop('execution_policy_digest'))
            assert set(data) == {'target', 'team_id', 'member_id', 'backend', 'permission_mode', 'tools', 'mcp', 'limits'}
            assert data['target']['executor_registration_id'] == reg.registration_id
            assert data['target']['export_id'] == export.export_id
            assert (data['backend'], data['permission_mode'], data['limits']['max_turns']) == ('acp_fixture', 'plan', 7)
            assert data['mcp'] == {'strict': True, 'project_server_count': 1}
            assert 'canary' not in response.text and 'PRIVATE_VALUE' not in response.text
            assert (await client.get(path + '/grants', headers=auth)).json() == {'grants': []}
            assert not network and Backend.calls == 0
            options = {'operations': ['view_status', 'run_task'], 'team_ids': [member.team_id],
                       'member_ids': [member.id], 'execution_policy_digest': digest,
                       'expected_export_revision': data['target']['export_revision']}
            grantee = {'grantee_user_id': uid(), 'grantee_registration_id': uid()}
            rejected = await client.post(path + '/grants', headers=auth,
                                         json=options | grantee | {'team_ids': ['wrong']})
            assert rejected.status_code == 404 and not network
            created = await client.post(path + '/grants', headers=auth, json=options | grantee)
            assert created.status_code == 201, created.text
            result = created.json()
            assert result['acked_revision'] == 1
            grant_path = path + '/grants/' + result['grant']['grant_id']
            async with ctx.db.session() as session:
                changed = await session.get(TeamMember, member.id)
                changed.max_turns = 13
            stale = await client.put(grant_path, headers=auth, json=options | {'expected_revision': 1})
            assert stale.status_code == 409 and stale.json() == {'detail': 'stale_target'}
            assert len(network) == 1
            current = (await client.get(preview, headers=auth)).json()
            assert current['limits']['max_turns'] == 13 and current['execution_policy_digest'] != digest
            updated = await client.put(grant_path, headers=auth, json=options | {
                'expected_revision': 1, 'execution_policy_digest': current['execution_policy_digest'],
                'expected_export_revision': current['target']['export_revision']})
            assert updated.status_code == 200, updated.text
            assert updated.json()['acked_revision'] == 2 and len(network) == 2 and Backend.calls == 0
    finally:
        await service.close()


@pytest.mark.parametrize('update', [False, True], ids=['create', 'update'])
async def test_export_consent_rechecked_inside_registry_write(acp, monkeypatch, update):
    ctx, _, grant, team, member, _ = acp
    export_id = grant.target.export_id
    service = AiAccessService(SimpleNamespace())
    monkeypatch.setattr(service, '_resident', lambda _: ctx)

    async def connect(*args, **kwargs):
        pytest.fail('a rejected grant must not connect to Remote')

    monkeypatch.setattr(service, '_connection', connect)
    mutate = grants.mutate_registry
    snapshots = []

    def race(change):
        # Another writer fences the export after local validation, just before the
        # grant mutation acquires the cross-process lock.
        frame = identity.unpublish_export(export_id)
        assert frame.revision > grant.target.export_revision
        snapshots.append(identity.load_registry())
        return mutate(change)

    monkeypatch.setattr(grants, 'mutate_registry', race)
    values = {'operations': ['view_status'], 'team_ids': [team.id], 'member_ids': [member.id],
              'execution_policy_digest': grant.execution_policy_digest}
    preconditions = {'expected_export_revision': grant.target.export_revision}
    if update:
        preconditions.update(grant_id=grant.grant_id, expected_revision=grant.revision)
    else:
        values.update(grantee_user_id=uid(), grantee_registration_id=uid())
    try:
        with pytest.raises(identity.AiAccessError) as err:
            await service.put_grant(export_id, values, **preconditions)
        assert err.value.code == 'stale_target'
        assert snapshots == [identity.load_registry()]
    finally:
        await service.close()


async def test_remote_draft_is_inert_and_duplicate_reuses_it(acp):
    ctx, _, _, team, member, _ = acp
    msg = forward(acp, p.Operation.TASK_CREATE, {'team_id': team.id, 'assignee_id': member.id,
        'title': 'Draft', 'brief': 'Local fixture text', 'locale': 'en', 'write_paths': []})
    accepted = await handle(acp, msg)
    replay = await handle(acp, msg)
    assert accepted.state == p.CommandState.COMPLETED, accepted
    assert replay.result['resource_id'] == accepted.result['resource_id']
    assert not await ctx.teams._release_tasks(team.id, automatic=True)
    async with ctx.db.session() as session:
        tasks = (await session.exec(select(TeamTask).where(TeamTask.title == 'Draft'))).all()
        assert len(tasks) == 1 and tasks[0].status == 'todo' and not tasks[0].auto_start
    assert Backend.calls == 0


async def test_busy_chat_leaves_no_message_or_inbox(acp):
    ctx = acp[0]
    chat = await new_chat(acp)
    busy = ctx.jobs.try_reserve(chat, 'fake')
    try:
        response = await handle(acp, forward(acp, p.Operation.CHAT_SEND, {'resource_id': chat, 'text': 'not queued'}, revision=1))
        assert response.code == p.ErrorCode.BUSY, response
        async with ctx.db.session() as session:
            assert not (await session.exec(select(AgentChatMessage).where(AgentChatMessage.chat_id == chat))).all()
        assert chat not in ctx.chats._inboxes
    finally:
        ctx.jobs.release_reservation(busy)


async def test_launch_once_then_duplicate_cannot_dispatch(acp):
    ctx = acp[0]
    chat = await new_chat(acp)
    msg = forward(acp, p.Operation.CHAT_SEND, {'resource_id': chat, 'text': 'fake backend only'}, revision=1)
    response = await handle(acp, msg)
    assert response.state == p.CommandState.ACCEPTED, response
    job = ctx.jobs.get(chat)
    await job.task
    duplicate = await handle(acp, msg)
    assert duplicate.state == p.CommandState.COMPLETED, duplicate
    assert Backend.calls == 1


async def test_revoked_during_preparation_does_not_launch(acp, monkeypatch):
    ctx, _, grant, *_ = acp
    chat = await new_chat(acp)
    original = ctx.chats.remote_dispatch_chat

    def dispatch(reservation, *, before_launch, after_run, deadline):
        async def fence():
            grants.revoke_grant(grant.grant_id, now_ms=now_ms())
            return await before_launch()
        return original(reservation, before_launch=fence, after_run=after_run, deadline=deadline)

    monkeypatch.setattr(ctx.chats, 'remote_dispatch_chat', dispatch)
    msg = forward(acp, p.Operation.CHAT_SEND, {'resource_id': chat, 'text': 'revoked'}, revision=1)
    assert (await handle(acp, msg)).state == p.CommandState.ACCEPTED
    await ctx.jobs.get(chat).task
    receipt = await ledger.lookup(ctx.db, ledger.LedgerKey.of(grant.target, grant.grantee_registration_id,
                                                              msg.request.command_id))
    assert receipt.state == p.CommandState.DENIED_BEFORE_DISPATCH
    assert Backend.calls == 0
    assert (await handle(acp, msg)).code == p.ErrorCode.DENIED


async def test_task_admission_revision_locks_and_notes(acp):
    ctx, _, grant, team, member, epoch = acp
    created = await handle(acp, forward(acp, p.Operation.TASK_CREATE, {
        'team_id': team.id, 'assignee_id': member.id, 'title': 'Draft',
        'brief': 'Fake task', 'locale': 'en', 'write_paths': ['src/a.txt'],
    }))
    task_id = created.result['resource_id']
    note = await handle(acp, forward(acp, p.Operation.TASK_NOTE, {
        'resource_id': task_id, 'text': 'Note before first start',
    }))
    assert note.state == p.CommandState.COMPLETED, note
    await ctx.teams.update_task(task_id, {'title': 'Edited locally'})
    issued = now_ms()
    read = p.ForwardBody(request=p.RequestBody(target=grant.target, grant_id=grant.grant_id,
        grant_revision=grant.revision, expected_executor_epoch=epoch, op=p.Operation.RESOURCE_GET,
        payload={'resource_id': task_id}), source_user_id=grant.grantee_user_id,
        source_registration_id=grant.grantee_registration_id, connection_id=uid(),
        current_grant_revision=grant.revision, deadline_ms=issued + 10_000)
    status = await handle(acp, read)
    assert status.result['revision'] == 3, status
    stale = await handle(acp, forward(acp, p.Operation.TASK_START, {'resource_id': task_id}, revision=2))
    assert stale.code == p.ErrorCode.REVISION_CONFLICT

    locks = await ctx.teams._path_locks.acquire(['src/a.txt'], root=ctx.paths.root, owner='fixture')
    try:
        busy = await handle(acp, forward(acp, p.Operation.TASK_START, {'resource_id': task_id}, revision=3))
        assert busy.code == p.ErrorCode.BUSY
        assert (await ctx.teams.find_task(task_id)).status == 'todo'
    finally:
        ctx.teams._path_locks.release(locks)
    start = forward(acp, p.Operation.TASK_START, {'resource_id': task_id}, revision=3)
    accepted = await handle(acp, start)
    assert accepted.state == p.CommandState.ACCEPTED, accepted
    await ctx.jobs.get(task_id).task
    assert (await handle(acp, start)).state == p.CommandState.COMPLETED
    assert Backend.calls == 1
    assert 'Note before first start' in Backend.prompts[0]


async def test_cancel_while_reservation_has_no_worker(acp, monkeypatch):
    ctx = acp[0]
    chat = await new_chat(acp)
    reserve_dispatch = ledger.reserve_dispatch

    async def stop_first(*args, **kwargs):
        assert ctx.jobs.get(chat).task is None
        assert await ctx.chats.cancel(chat)
        return await reserve_dispatch(*args, **kwargs)

    monkeypatch.setattr(ledger, 'reserve_dispatch', stop_first)
    send = forward(acp, p.Operation.CHAT_SEND, {'resource_id': chat, 'text': 'do not launch'}, revision=1)
    assert (await handle(acp, send)).state == p.CommandState.ACCEPTED
    await ctx.jobs.get(chat).task
    assert (await handle(acp, send)).state == p.CommandState.DENIED_BEFORE_DISPATCH
    assert Backend.calls == 0


async def test_timed_out_preparation_releases_reservation(acp, monkeypatch):
    ctx = acp[0]
    chat = await new_chat(acp)
    original = ctx.chats.remote_dispatch_chat

    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    def dispatch(*args, **kwargs):
        kwargs['deadline'] = asyncio.get_running_loop().time() + 0.05
        return original(*args, **kwargs)

    monkeypatch.setattr(ctx.chats, '_run_turn', stalled)
    monkeypatch.setattr(ctx.chats, 'remote_dispatch_chat', dispatch)
    send = forward(acp, p.Operation.CHAT_SEND, {'resource_id': chat, 'text': 'do not launch'}, revision=1)
    assert (await handle(acp, send)).state == p.CommandState.ACCEPTED
    await asyncio.wait_for(ctx.jobs.get(chat).task, 2)
    assert not ctx.jobs.is_running(chat)
    assert (await handle(acp, send)).state == p.CommandState.DENIED_BEFORE_DISPATCH
    assert Backend.calls == 0
