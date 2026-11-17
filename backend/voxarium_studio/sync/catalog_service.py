"""Unmounted, read-only profile adapter (studio-design §5.1/§6, S3).

No home, transport, network client or runtime is selected by this module. Providers
are trusted synchronous read-only boundaries: ``RemoteProfiles.get`` and the S2
``load_profile_token(profile_id, url)`` signature are compatible. An eventual
production provider must additionally fence pending S2 recovery transactions;
``RemoteProfiles.load_token`` is NOT a read-only provider (it takes a file lock).
Wiring, lifecycle locking and integration readiness are deliberately out of scope.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Protocol, TypeVar
from uuid import UUID

from ..core.application_config import RemoteServerProfile
from .catalog import (
    CatalogClient, CatalogError, CatalogPage, CatalogProject, CatalogResponse,
    CatalogTransport, Verification,
)
from .profiles import ProfileError
from .protocol import normalise_url


class ProfileProvider(Protocol):
    def get(self, profile_id: str) -> RemoteServerProfile: ...


CredentialProvider = Callable[[str, str], str | None]
T = TypeVar("T")


@dataclass(frozen=True)
class _Snapshot:
    profile_id: str
    url: str
    revision: int
    token: str = field(repr=False)


def _contains_secret(value: object, token: str) -> bool:
    """Do not let even a successful server response echo Authorization into a DTO."""
    if isinstance(value, str):
        return token in value
    if is_dataclass(value) and not isinstance(value, type):
        return any(_contains_secret(getattr(value, f.name), token) for f in fields(value))
    if isinstance(value, Mapping):
        return any(_contains_secret(k, token) or _contains_secret(v, token)
                   for k, v in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_contains_secret(v, token) for v in value)
    return False


class _ReadTransport:
    """Per-operation guard; never rebind a waiting request to the newest profile."""

    def __init__(self, service: CatalogService, snapshot: _Snapshot,
                 paths: frozenset[str]):
        self._service = service
        self._snapshot = snapshot
        self._urls = frozenset(snapshot.url + path for path in paths)
        self.failure: str | None = None

    def check(self) -> None:
        code = None
        try:
            self._service._check(self._snapshot)
        except CatalogError as error:
            code = error.code
        if code is not None:
            self.failure = code
            raise CatalogError(code)

    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      params: Mapping[str, str | int], json: Mapping[str, str] | None,
                      follow_redirects: bool) -> CatalogResponse:
        self.check()
        if (method != "GET" or url not in self._urls or json is not None
                or follow_redirects is not False):
            self.failure = "invalid_request"
            raise CatalogError(self.failure)
        response = None
        failed = cancelled = False
        try:
            response = await self._service._transport.request(
                method, url, headers=headers, params=params, json=None,
                follow_redirects=False,
            )
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            failed = True
        # Check even failed awaits, and do so before returning health to the
        # client: a stale health reply must never be followed by an auth request.
        if cancelled:
            raise asyncio.CancelledError() from None
        self.check()
        if failed:
            raise CatalogError("transport_unavailable")
        if (isinstance(response, CatalogResponse) and type(response.status) is int
                and 200 <= response.status <= 299
                and _contains_secret(response.body, self._snapshot.token)):
            self.failure = "invalid_response"
            raise CatalogError(self.failure)
        return response


class CatalogService:
    """verify/list/get only, using a fresh ephemeral client for every operation.

    There is no cached 'valid' profile state, Create, journal, Select, config write
    or WS. Both providers and transport are mandatory; callers supply no URL/key.
    The credential provider must use exact profile+URL and fail closed on corrupt
    storage, unbound environment credentials or pending recovery, without legacy
    or alternate-key fallback (S2 contract).
    """

    def __init__(self, *, profiles: ProfileProvider, credentials: CredentialProvider,
                 transport: CatalogTransport):
        self._profiles = profiles
        self._credentials = credentials
        self._transport = transport

    def _profile(self, profile_id: str, revision: int) -> RemoteServerProfile:
        profile = None
        code = "profile_store_unavailable"
        try:
            profile = self._profiles.get(profile_id)
        except ProfileError as error:
            if error.args == ("profile_not_found",):
                code = "profile_not_found"
            elif error.args == ("profile_recovery_required",):
                code = "profile_recovery_required"
        except Exception:
            pass
        if profile is None:
            raise CatalogError(code)
        valid = False
        url = None
        try:
            if isinstance(profile, RemoteServerProfile):
                url = normalise_url(profile.url)
            valid = (isinstance(profile, RemoteServerProfile)
                     and profile.profile_id == profile_id
                     and str(UUID(profile.profile_id)) == profile.profile_id
                     and isinstance(profile.url, str) and url is not None
                     and type(profile.revision) is int and profile.revision >= 1)
        except Exception:
            pass
        if not valid:
            raise CatalogError("invalid_profile_metadata")
        if type(revision) is not int or revision != profile.revision:
            raise CatalogError("profile_revision_conflict")
        return profile.model_copy(update={"url": url})

    def _snapshot(self, profile_id: str, revision: int) -> _Snapshot:
        profile = self._profile(profile_id, revision)
        token = None
        failed = False
        try:
            token = self._credentials(profile.profile_id, profile.url)
        except Exception:
            failed = True
        if failed:
            raise CatalogError("credential_store_unavailable")
        if token is None:
            raise CatalogError("credential_required")
        if (not isinstance(token, str) or not token or not token.isascii()
                or any(ord(c) <= 32 or ord(c) == 127 for c in token)):
            raise CatalogError("invalid_credential")
        # Copy scalars, never retain the provider's mutable record. The second
        # metadata read also fences synchronous provider side effects/races.
        latest = self._profile(profile_id, revision)
        if latest.url != profile.url:
            raise CatalogError("profile_revision_conflict")
        return _Snapshot(profile.profile_id, profile.url, revision, token)

    def _check(self, snapshot: _Snapshot) -> None:
        # Reject a new locator BEFORE asking for any credential at that locator.
        profile = self._profile(snapshot.profile_id, snapshot.revision)
        if profile.url != snapshot.url:
            raise CatalogError("profile_revision_conflict")
        current = self._snapshot(snapshot.profile_id, snapshot.revision)
        if current.url != snapshot.url:
            raise CatalogError("profile_revision_conflict")
        if current.token != snapshot.token:
            # Also fences scoped ENV rotation, which has no stored revision.
            raise CatalogError("credential_binding_conflict")

    async def _read(self, profile_id: str, revision: int, path: str | None,
                    operation: Callable[[CatalogClient], Awaitable[T]] | None = None
                    ) -> T | Verification:
        snapshot = self._snapshot(profile_id, revision)
        paths = {"/healthz", "/api/v2/me"}
        if path is not None:
            paths.add(path)
        transport = _ReadTransport(self, snapshot, frozenset(paths))
        client = CatalogClient(transport, url=snapshot.url, token=snapshot.token)
        result = None
        code = None
        cancelled = False
        try:
            verification = await client.verify()
            # Filter before catalog access, not just before returning the DTO.
            if _contains_secret(verification, snapshot.token):
                raise CatalogError("invalid_response")
            result = verification if operation is None else await operation(client)
        except asyncio.CancelledError:
            cancelled = True
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "catalog_unavailable"
        if cancelled:
            raise asyncio.CancelledError() from None
        # CatalogClient sanitizes transport errors. Preserve the guard's fixed
        # stale code rather than flattening it into transport_unavailable.
        if transport.failure is not None:
            raise CatalogError(transport.failure)
        self._check(snapshot)
        if code is not None:
            raise CatalogError(code)
        if _contains_secret(result, snapshot.token):
            raise CatalogError("invalid_response")
        return result

    async def verify(self, profile_id: str, *, expected_revision: int) -> Verification:
        return await self._read(profile_id, expected_revision, None)

    async def list_projects(self, profile_id: str, *, expected_revision: int,
                            limit: int = 100, after: str | None = None) -> CatalogPage:
        return await self._read(
            profile_id, expected_revision, "/api/v2/projects",
            lambda client: client.list_projects(limit=limit, after=after),
        )

    async def get_project(self, profile_id: str, id: str, *,
                          expected_revision: int) -> CatalogProject:
        # CatalogClient validates the exact ID before constructing/sending its
        # request; the guard permits only this operation's one literal endpoint.
        path = f"/api/v2/projects/{id}" if isinstance(id, str) else "/api/v2/projects/"
        return await self._read(
            profile_id, expected_revision, path, lambda client: client.get_project(id),
        )
