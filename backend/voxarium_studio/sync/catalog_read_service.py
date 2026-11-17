"""Unmounted profile-bound snapshot/legacy reads (studio-design §5.1/§6, S3).

Only synchronous, read-only S2-compatible providers and an injected transport
are accepted. No default home, HTTP client, routes, writes or lifecycle wiring
exist here. Provider recovery fencing and a future real transport's byte limits
still require separate integration work; an observation grants no Select/Enable.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .catalog import CatalogError, CatalogTransport
from .catalog_reads import CatalogReads, LegacyResolution, ProjectSnapshot
from .catalog_service import (
    CatalogService, CredentialProvider, ProfileProvider, _contains_secret,
    _ReadTransport,
)


T = TypeVar("T")


class CatalogReadService:
    """Fresh verified GET-only observations, never a cached profile binding.

    Reuse the accepted profile/URL/revision/credential guards and the accepted
    wire reader rather than restating either contract. Providers have the same
    trusted read-only requirements as CatalogService (not RemoteProfiles.load_token).
    Callers supply only a profile ID, revision and exact project/legacy selector.
    """

    def __init__(self, *, profiles: ProfileProvider, credentials: CredentialProvider,
                 transport: CatalogTransport):
        self._guards = CatalogService(profiles=profiles, credentials=credentials,
                                      transport=transport)

    async def _read(self, profile_id: str, revision: int, path: str,
                    operation: Callable[[CatalogReads], Awaitable[T]]) -> T:
        snapshot = self._guards._snapshot(profile_id, revision)
        transport = _ReadTransport(
            self._guards, snapshot,
            frozenset({"/healthz", "/api/v2/me", path}),
        )
        reads = CatalogReads(transport, url=snapshot.url, token=snapshot.token)
        result = None
        code = None
        cancelled = False
        try:
            transport.check()
            verification = await reads.verify()
            transport.check()
            if _contains_secret(verification, snapshot.token):
                raise CatalogError("invalid_response")
            # The transport guard also fences BOTH sides of each page await,
            # before CatalogReads can parse/accumulate it or request another page.
            result = await operation(reads)
        except asyncio.CancelledError:
            cancelled = True
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "catalog_unavailable"
        # Run outside exception handlers: no provider/transport input is retained
        # as __context__. Also check failed and cancelled awaits, not just success.
        transport.check()
        if transport.failure is not None:
            raise CatalogError(transport.failure)
        if cancelled:
            raise asyncio.CancelledError() from None
        if code is not None:
            raise CatalogError(code)
        if _contains_secret(result, snapshot.token):
            raise CatalogError("invalid_response")
        return result

    async def snapshot(
        self, profile_id: str, id: str, *, expected_revision: int,
        expected_epoch: str | None = None, limit: int = 20,
        max_pages: int = 100, max_bytes: int = 64 * 1024 * 1024,
    ) -> ProjectSnapshot:
        # The wire owner validates the exact ID before any snapshot request.
        path = (f"/api/v2/projects/{id}/snapshot" if isinstance(id, str)
                else "/api/v2/projects//snapshot")
        return await self._read(
            profile_id, expected_revision, path,
            lambda reads: reads.snapshot(id, expected_epoch=expected_epoch,
                                         limit=limit, max_pages=max_pages,
                                         max_bytes=max_bytes),
        )

    async def resolve_legacy(self, profile_id: str, name: str, *,
                             expected_revision: int) -> LegacyResolution:
        # No trim, normalization, fuzzy lookup or fallback Create/connect.
        return await self._read(
            profile_id, expected_revision, "/api/v2/legacy-spaces/resolve",
            lambda reads: reads.resolve_legacy(name),
        )
