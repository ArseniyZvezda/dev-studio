"""Native ACP endpoints: exact HTTP(S) profile URL, bounded bodies and no redirects."""
from __future__ import annotations

import asyncio
import logging
import re
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from . import protocol as p
from .identity import AiAccessError, normalise_remote_url


class TransportError(AiAccessError):
    pass


class RegistrationExists(TransportError):
    def __init__(self, registration_id: str) -> None:
        super().__init__('registration_exists')
        self.registration_id = registration_id


# websockets' debug tracing includes the opening headers and complete text frames.
# Use a private logger, not a global logging setting that affects Documents sync.
_SOCKET_LOG = logging.Logger(__name__, level=logging.CRITICAL + 1)
_SOCKET_LOG.disabled = True


@dataclass(frozen=True, slots=True)
class DeviceCredentials:
    client_id: str
    token: str = field(repr=False)


class _NoRedirect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        return exc


def _url(base: str, suffix: str) -> str:
    if normalise_remote_url(base) != base:
        raise TransportError('invalid_remote_url')
    return base + '/api/ai/v1/' + suffix


def _ascii_secret(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or any(not 33 <= ord(c) <= 126 for c in value):
        raise TransportError('auth_failure')
    return value


def _headers(key: str, device: DeviceCredentials | None = None) -> dict[str, str]:
    headers = {**p.DISCOVERY_REQUEST_HEADERS, 'Authorization': 'Bearer ' + _ascii_secret(key)}
    if device is not None:
        headers.update({'X-VXS-Client-ID': p.check_uuid(device.client_id),
                        'X-VXS-Device-Token': _ascii_secret(device.token)})
    return headers


class _Socket:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def send(self, frame: str) -> None:
        if not isinstance(frame, str) or len(frame.encode('utf-8')) > p.MAX_FRAME_BYTES:
            raise p.ProtocolError()
        await self._connection.send(frame)

    async def recv(self) -> str:
        frame = await self._connection.recv()
        if not isinstance(frame, str) or len(frame.encode('utf-8')) > p.MAX_FRAME_BYTES:
            raise p.ProtocolError()
        return frame


class AiTransport:
    def __init__(self, *, http_transport: Any = None, ws_connect: Any = _NoRedirect) -> None:
        self._http = http_transport
        self._ws_connect = ws_connect

    async def _http_call(self, method: str, url: str, headers: dict, *, value: dict | None = None,
                         read_status: int | tuple[int, ...], limit: int = p.MAX_FRAME_BYTES) -> tuple[int, bytes]:
        try:
            async with asyncio.timeout(10), httpx.AsyncClient(
                transport=self._http, follow_redirects=False, trust_env=False, timeout=10,
            ) as client:
                kwargs = {'content': p.canonical_json(value)} if value is not None else {}
                # Do not decompress an untrusted body before enforcing our byte limit.
                headers = {**headers, 'Accept-Encoding': 'identity'}
                if value is not None:
                    headers['Content-Type'] = 'application/json'
                async with client.stream(method, url, headers=headers, **kwargs) as response:
                    result = bytearray()
                    allowed = (read_status,) if isinstance(read_status, int) else read_status
                    if response.status_code in allowed:
                        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                            raise TransportError('invalid_response')
                        async for data in response.aiter_bytes(chunk_size=4096):
                            result.extend(data)
                            if len(result) > limit:
                                raise TransportError('invalid_response')
                    return response.status_code, bytes(result)
        except (TimeoutError, httpx.TimeoutException):
            raise TransportError('timeout') from None
        except (httpx.TransportError, OSError) as exc:
            cause = exc
            while cause is not None:
                if isinstance(cause, ssl.SSLError):
                    raise TransportError('tls') from None
                cause = cause.__cause__
            raise TransportError('network') from None

    async def capabilities(self, url: str, key: str) -> p.DiscoveryOutcome:
        try:
            status, body = await self._http_call('GET', _url(url, 'capabilities'), _headers(key),
                                                 read_status=200, limit=4096)
        except AiAccessError as exc:
            if exc.code in {'timeout', 'tls', 'network'}:
                return p.classify_transport_failure(exc.code)
            return p.DiscoveryOutcome(p.DiscoveryKind.ERROR, exc.code)
        return p.classify_capabilities(status, body)

    @staticmethod
    def _expect(status: int, success: int) -> None:
        if status == success:
            return
        raise TransportError({401: 'auth_failure', 403: 'denied', 404: 'not_available'}.get(status,
                'redirect_refused' if 300 <= status < 400 else 'remote_error'))

    async def register(self, url: str, key: str, request: dict) -> dict:
        status, body = await self._http_call('POST', _url(url, 'registrations'), _headers(key),
                                            value=request, read_status=(201, 409), limit=4096)
        if status == 409:
            data = p.parse_json(body)
            error = data.get('error') if isinstance(data, dict) else None
            if not isinstance(error, dict) or set(error) != {'code', 'message', 'request_id'}:
                raise TransportError('invalid_response')
            if error['code'] == 'registration_exists' and set(data) == {'error', 'registration_id'}:
                raise RegistrationExists(p.check_uuid(data['registration_id']))
            if error['code'] == 'capacity_exceeded' and set(data) == {'error'}:
                raise TransportError('capacity_exceeded')
            raise TransportError('invalid_response')
        if status == 404:
            raise TransportError('unsupported')  # capability was withdrawn after discovery
        self._expect(status, 201)
        data = p.parse_json(body)
        if not isinstance(data, dict) or set(data) != {
            'server_instance_id', 'registration_id', 'client_id', 'user_id', 'device_token',
        }:
            raise TransportError('invalid_response')
        for name in ('server_instance_id', 'registration_id', 'client_id'):
            p.check_uuid(data[name])
        if not isinstance(data['user_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', data['user_id']):
            raise TransportError('invalid_response')
        if not isinstance(data['device_token'], str) or not re.fullmatch(r'vxd_[A-Za-z0-9_-]{43}', data['device_token']):
            raise TransportError('invalid_response')
        return data

    async def directory(self, url: str, key: str, device: DeviceCredentials) -> list[dict]:
        status, body = await self._http_call('GET', _url(url, 'registrations'), _headers(key, device), read_status=200)
        self._expect(status, 200)
        # The HTTP directory is byte-bounded; unlike control frames it can have >128 fields.
        from .identity import strict_loads
        data = strict_loads(body)
        if (not isinstance(data, dict) or set(data) != {'acp', 'registrations'}
                or type(data['acp']) is not int or data['acp'] != 1 or not isinstance(data['registrations'], list)):
            raise TransportError('invalid_response')
        entries = []
        from uuid import UUID
        for row in data['registrations']:
            if not isinstance(row, dict) or set(row) != {'user_id', 'registration_id', 'client_id', 'display_name', 'kind'}:
                raise TransportError('invalid_response')
            if not isinstance(row['user_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', row['user_id']):
                raise TransportError('invalid_response')
            entries.append({'user_id': str(UUID(hex=row['user_id'])), 'registration_id': p.check_uuid(row['registration_id']),
                            'client_id': p.check_uuid(row['client_id']), 'display_name': p.check_title(row['display_name']),
                            'kind': str(p.ClientKind(row['kind']))})
        return entries

    async def unregister(self, url: str, key: str, registration_id: str) -> None:
        status, _ = await self._http_call('DELETE', _url(url, 'registrations/' + p.check_uuid(registration_id)),
                                          _headers(key), read_status=204)
        self._expect(status, 204)

    @asynccontextmanager
    async def connect(self, url: str, key: str, device: DeviceCredentials) -> AsyncIterator[_Socket]:
        headers = _headers(key, device)
        endpoint = _url(url, 'control')
        secure = endpoint.startswith('https://')
        socket_url = ('wss://' + endpoint[8:]) if secure else ('ws://' + endpoint[7:])
        try:
            async with self._ws_connect(socket_url, additional_headers=headers,
                proxy=None, ssl=ssl.create_default_context() if secure else None, max_size=p.MAX_FRAME_BYTES, max_queue=16,
                open_timeout=10, close_timeout=2, compression=None, logger=_SOCKET_LOG,
            ) as socket:
                yield _Socket(socket)
        except (WebSocketException, OSError, TimeoutError):
            raise TransportError('offline') from None
