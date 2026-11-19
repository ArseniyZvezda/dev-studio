"""Local browser API for ACP; never a general HTTP proxy or project selector."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import Field, ValidationError

from ..core.security import LOOPBACK_HOSTS, _origin
from ..ai_access import protocol as p
from ..ai_access.identity import AiAccessError
from ..ai_access.transport import RegistrationExists


async def _available_service(request: Request) -> Any:
    if request.app.state.auth.is_loopback:
        origin = _origin(f"{request.scope['scheme']}://{request.headers.get('host', '')}")
        if origin is None or origin[1] not in LOOPBACK_HOSTS:
            raise HTTPException(403, detail='invalid_host')
    service = getattr(request.app.state, 'ai_access', None)
    if service is None:
        raise HTTPException(503, detail='offline')
    return service


async def _service(request: Request) -> Any:
    if request.scope['query_string']:
        raise HTTPException(400, detail='invalid_request')
    return await _available_service(request)


router = APIRouter(prefix='/ai-access')


def _id(value: str) -> str:
    try:
        return p.check_uuid(value)
    except p.ProtocolError:
        raise HTTPException(422, detail='invalid_id') from None


async def _body(request: Request, model: type[p.Strict]) -> Any:
    if request.headers.get('content-type', '').split(';')[0].lower().strip() != 'application/json':
        raise HTTPException(415, detail='invalid_request')
    raw = bytearray()
    async for data in request.stream():
        if len(raw) + len(data) > p.MAX_FRAME_BYTES:
            raise HTTPException(413, detail='capacity_exceeded')
        raw.extend(data)
    try:
        return model.from_wire(p.parse_json(bytes(raw)))
    except (p.ProtocolError, ValidationError):
        raise HTTPException(422, detail='invalid_request') from None


_SAFE_ERRORS = {str(code) for code in p.ErrorCode} | {
    'auth_failure', 'registration_exists', 'offline', 'timeout', 'disabled', 'profile_not_found',
    'recover_explicitly', 'ai_access_conflict', 'invalid_response', 'redirect_refused', 'remote_error',
    'invalid_remote_url', 'network', 'tls', 'ai_access_store_unavailable', 'server_instance_mismatch',
    'not_mounted', 'incompatible_version', 'publication_disabled', 'catalog_unavailable',
}


async def _call(call, *args, **kwargs):
    try:
        return await call(*args, **kwargs)
    except RegistrationExists as exc:
        raise HTTPException(409, detail={'code': 'registration_exists', 'registration_id': exc.registration_id},
                            headers={'Cache-Control': 'no-store'}) from None
    except (AiAccessError, p.ProtocolError) as exc:
        code = str(exc.code) if str(exc.code) in _SAFE_ERRORS else 'invalid_request'
    except (ValueError, TypeError):
        code = 'invalid_request'
    status = {'invalid_request': 422, 'not_available': 404, 'profile_not_found': 404,
              'auth_failure': 401, 'denied': 403, 'busy': 409, 'offline': 503,
              'outcome_unknown': 503, 'catalog_unavailable': 503, 'disabled': 409, 'stale_target': 409,
              'revision_conflict': 409, 'rate_limited': 429}.get(code, 409)
    raise HTTPException(status, detail=code, headers={'Cache-Control': 'no-store'})


def _local(call, *args):
    try:
        return call(*args)
    except (AiAccessError, p.ProtocolError) as exc:
        code = str(exc.code) if str(exc.code) in _SAFE_ERRORS else 'invalid_request'
        raise HTTPException(404 if code == 'not_available' else 409, detail=code) from None


class _Register(p.Strict):
    profile_id: p.Uuid
    server_instance_id: p.Uuid
    display_name: p.Title


class _CleanupRegistration(p.Strict):
    server_instance_id: p.Uuid


class _Revision(p.Strict):
    expected_revision: p.Revision


class _Switch(_Revision):
    enabled: bool


class _Export(p.Strict):
    registration_id: p.Uuid
    profile_id: p.Uuid
    server_instance_id: p.Uuid
    project_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    project_epoch: str = Field(pattern=r'^[0-9a-f]{32}$')
    persistent_id: p.PersistentId
    storage_epoch: p.StorageEpoch
    identity_revision: p.Revision
    display_name: p.Title


class _GrantOptions(p.Strict):
    expected_export_revision: p.Revision
    operations: list[p.Permission] = Field(min_length=1, max_length=len(p.Permission))
    team_ids: list[p.LocalId] = Field(min_length=1, max_length=64)
    member_ids: list[p.LocalId] = Field(min_length=1, max_length=64)
    resource_scope: p.ResourceScope = p.ResourceScope.CREATED_BY_THIS_GRANTEE
    resource_ids: list[p.LocalId] = Field(default_factory=list, max_length=p.MAX_INDEX_ROWS_PER_EXPORT)
    execution_policy_digest: p.Sha256
    expires_at: p.Timestamp | None = None


class _GrantCreate(_GrantOptions):
    grantee_user_id: p.Uuid
    grantee_registration_id: p.Uuid


class _GrantUpdate(_GrantOptions):
    expected_revision: p.Revision


class _Request(p.Strict):
    registration_id: p.Uuid
    request: p.RequestBody


class _Recovery(p.Strict):
    executor_epoch: p.Uuid
    resend: bool = False


@router.get('/profiles/{profile_id}/capabilities')
async def capabilities(profile_id: str, response: Response, service=Depends(_service)):
    response.headers['Cache-Control'] = 'no-store'
    return await _call(service.capabilities, _id(profile_id))


@router.delete('/profiles/{profile_id}/orphan-registrations/{registration_id}', status_code=204)
async def cleanup_registration(profile_id: str, registration_id: str, request: Request, service=Depends(_service)):
    body = await _body(request, _CleanupRegistration)
    await _call(service.cleanup_registration, _id(profile_id), _id(registration_id), body.server_instance_id)


@router.get('/registrations')
async def registrations(service=Depends(_service)):
    return {'registrations': _local(service.registrations)}


@router.post('/registrations', status_code=201)
async def register(request: Request, service=Depends(_service)):
    body = await _body(request, _Register)
    return await _call(service.register, body.profile_id, body.display_name, server_instance_id=body.server_instance_id)


@router.delete('/registrations/{registration_id}', status_code=204)
async def unregister(registration_id: str, request: Request, service=Depends(_service)):
    body = await _body(request, _Revision)
    await _call(service.unregister, _id(registration_id), body.expected_revision)


@router.put('/registrations/{registration_id}/publication')
async def publication(registration_id: str, request: Request, service=Depends(_service)):
    body = await _body(request, _Switch)
    return await _call(service.set_publication, _id(registration_id), body.enabled, body.expected_revision)


@router.get('/registrations/{registration_id}/directory')
async def directory(registration_id: str, service=Depends(_service)):
    return {'registrations': await _call(service.directory, _id(registration_id))}


@router.get('/registrations/{registration_id}/targets')
async def targets(registration_id: str, request: Request, service=Depends(_available_service)):
    after = None
    raw = request.scope['query_string']
    if raw:
        items = request.query_params.multi_items()
        if b'&' in raw or len(items) != 1 or items[0][0] != 'after':
            raise HTTPException(400, detail='invalid_request')
        try:
            after = p.check_uuid(items[0][1])
        except p.ProtocolError:
            raise HTTPException(400, detail='invalid_request') from None
    return await _call(service.targets, _id(registration_id), after=after)


@router.get('/exports')
async def exports(service=Depends(_service)):
    return {'exports': _local(service.exports)}


@router.post('/exports', status_code=201)
async def create_export(request: Request, response: Response, service=Depends(_service)):
    response.headers['Cache-Control'] = 'no-store'
    body = await _body(request, _Export)
    return await _call(service.create_export, **body.model_dump())


@router.put('/exports/{export_id}/publication')
async def export_publication(export_id: str, request: Request, service=Depends(_service)):
    body = await _body(request, _Switch)
    return await _call(service.publish if body.enabled else service.unpublish, _id(export_id), body.expected_revision)


@router.get('/exports/{export_id}/teams/{team_id}/members/{member_id}/execution-policy')
async def execution_policy(export_id: str, team_id: str, member_id: str, response: Response,
                           service=Depends(_service)):
    response.headers['Cache-Control'] = 'no-store'
    return await _call(service.preview_policy, _id(export_id), team_id, member_id)


@router.get('/exports/{export_id}/grants')
async def grants(export_id: str, service=Depends(_service)):
    return {'grants': _local(service.list_grants, _id(export_id))}


@router.post('/exports/{export_id}/grants', status_code=201)
async def create_grant(export_id: str, request: Request, service=Depends(_service)):
    body = await _body(request, _GrantCreate)
    return await _call(service.put_grant, _id(export_id), body.model_dump(
        mode='json', exclude_none=True, exclude={'expected_export_revision'}),
        expected_export_revision=body.expected_export_revision)


@router.put('/exports/{export_id}/grants/{grant_id}')
async def update_grant(export_id: str, grant_id: str, request: Request, service=Depends(_service)):
    body = await _body(request, _GrantUpdate)
    return await _call(service.put_grant, _id(export_id), body.model_dump(
        mode='json', exclude_none=True, exclude={'expected_revision', 'expected_export_revision'}),
        expected_export_revision=body.expected_export_revision,
        grant_id=_id(grant_id), expected_revision=body.expected_revision)


@router.delete('/grants/{grant_id}')
async def revoke_grant(grant_id: str, request: Request, service=Depends(_service)):
    body = await _body(request, _Revision)
    return await _call(service.revoke_grant, _id(grant_id), body.expected_revision)


@router.post('/requests')
async def send(request: Request, response: Response, service=Depends(_service)):
    response.headers['Cache-Control'] = 'no-store'
    body = await _body(request, _Request)
    return await _call(service.request, body.registration_id, body.request)


@router.get('/receipts')
async def receipts(response: Response, service=Depends(_service)):
    response.headers['Cache-Control'] = 'no-store'
    return {'receipts': _local(service.receipts)}


@router.get('/receipts/{command_id}')
async def receipt(command_id: str, response: Response, service=Depends(_service)):
    response.headers['Cache-Control'] = 'no-store'
    return _local(service.receipt, _id(command_id))


@router.post('/receipts/{command_id}/recover')
async def recover(command_id: str, request: Request, service=Depends(_service)):
    body = await _body(request, _Recovery)
    return await _call(service.recover_request, _id(command_id), body.executor_epoch, resend=body.resend)


@router.delete('/receipts/{command_id}', status_code=204)
async def discard(command_id: str, service=Depends(_service)):
    _local(service.discard, _id(command_id))


@router.get('/events')
async def events(service=Depends(_service)):
    async def stream():
        try:
            async for event in service.events():
                kind = event['event']
                if kind == 'heartbeat':
                    yield ': heartbeat\n\n'
                elif kind in {'remote', 'resync_required'}:
                    yield f"event: {kind}\ndata: {json.dumps(event['data'], ensure_ascii=False, separators=(',', ':'))}\n\n"
        except AiAccessError:
            yield 'event: resync_required\ndata: {}\n\n'
    return StreamingResponse(stream(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no',
    })
