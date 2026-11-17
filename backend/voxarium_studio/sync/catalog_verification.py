"""Unmounted HTTP-only Verify diagnostic (studio-design §5.1/§6, S3 slice D).

No default providers, clock, transport, runtime, routes or persistence. Providers
have the same trusted read-only/pending-safe contract as CatalogService; this is
not a new S2 storage composition or a production transport. A valid v1 credential
is reported separately from its unavailable catalog. No result grants consent
for catalog access, Create, Select, Enable or WS.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from .catalog import (
    CatalogClient, CatalogError, CatalogResponse, CatalogTransport, CatalogUser,
    _url,
)
from .catalog_service import (
    CatalogService, CredentialProvider, ProfileProvider, _ReadTransport,
    _contains_secret,
)


@dataclass(frozen=True)
class VerificationStatus:
    """Secret-free §5.1 status; valid never implies catalog availability.

    For a valid old server, catalog_version is None and error_code is the fixed
    catalog_unavailable diagnostic. The user is only authenticated identity, not
    an authorization/cache for other operations. Nothing is persisted here.
    """

    state: Literal["valid", "unauthorised", "offline", "incompatible",
                   "insecure_blocked", "error"]
    checked_at: str
    user: CatalogUser | None = None
    wire_protocol: int | None = None
    catalog_version: int | None = None
    error_code: str | None = None


class _DiscoveredTransport:
    """Replay one already guarded health observation to the strict v2 verifier.

    No second discovery request and, importantly, no relaxation/substitution of
    the v2 health envelope. All subsequent I/O still crosses the profile guards.
    """

    def __init__(self, transport: _ReadTransport, url: str, health: CatalogResponse):
        self._transport = transport
        self._health_url = url + "/healthz"
        self._health = health

    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      params: Mapping[str, str | int], json: Mapping[str, str] | None,
                      follow_redirects: bool) -> CatalogResponse:
        self._transport.check()
        if self._health is not None:
            if (method != "GET" or url != self._health_url or params or json is not None
                    or follow_redirects is not False or "Authorization" in headers):
                raise CatalogError("invalid_request")
            response, self._health = self._health, None
            return response
        return await self._transport.request(
            method, url, headers=headers, params=params, json=json,
            follow_redirects=follow_redirects,
        )


class CatalogVerificationService:
    """Only verify(profile_id, expected_revision); never accepts a URL or key.

    Explicit protocol 1 + the actual legacy health envelope permits exactly
    /api/v1/me. Version 2 is delegated unchanged to CatalogClient. Unknown or
    malformed discovery never sends Authorization and never tries guessed routes.
    The injected clock is a trusted synchronous UTC-aware datetime provider.
    """

    def __init__(self, *, profiles: ProfileProvider, credentials: CredentialProvider,
                 transport: CatalogTransport, clock: Callable[[], datetime]):
        # Reuse the existing profile/URL/revision/key guards, not a second store.
        self._guards = CatalogService(
            profiles=profiles, credentials=credentials, transport=transport,
        )
        self._clock = clock

    async def _request(self, transport: _ReadTransport, url: str,
                       token: str | None = None) -> CatalogResponse:
        headers = {"Accept": "application/json", "Cache-Control": "no-store"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        response = None
        code = None
        cancelled = False
        try:
            response = await transport.request(
                "GET", url, headers=headers, params={}, json=None,
                follow_redirects=False,
            )
        except asyncio.CancelledError:
            cancelled = True
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "transport_unavailable"
        # Raise outside handlers: neither raw errors nor cancellation messages
        # survive as exception context. Check even an unsuccessful await.
        transport.check()
        if cancelled:
            raise asyncio.CancelledError() from None
        if code is not None:
            raise CatalogError(code)
        if (not isinstance(response, CatalogResponse) or type(response.status) is not int
                or not 100 <= response.status <= 599):
            raise CatalogError("invalid_response")
        if 300 <= response.status <= 399:
            raise CatalogError("redirect_refused")
        return response

    @staticmethod
    def _legacy_user(response: CatalogResponse) -> CatalogUser:
        if response.status == 401:
            raise CatalogError("remote_unauthorised")
        if response.status != 200:
            raise CatalogError("remote_error")
        body = response.body
        if not isinstance(body, dict) or body.keys() != {"protocol", "user"}:
            raise CatalogError("invalid_response")
        if type(body["protocol"]) is not int or body["protocol"] != 1:
            raise CatalogError("version_mismatch")
        user = body["user"]
        # Current v1 User.to_json uses name, not the v2 display_name spelling.
        if (not isinstance(user, dict) or user.keys() != {"id", "name", "color"}
                or not isinstance(user["id"], str) or not user["id"]
                or not isinstance(user["name"], str)
                or not isinstance(user["color"], str)
                or re.fullmatch(r"#[0-9a-fA-F]{6}", user["color"]) is None):
            raise CatalogError("invalid_response")
        return CatalogUser(user["id"], user["name"], user["color"])

    async def verify(self, profile_id: str, *, expected_revision: int) -> VerificationStatus:
        snapshot = self._guards._snapshot(profile_id, expected_revision)
        guarded = _ReadTransport(self._guards, snapshot, frozenset({
            "/healthz", "/api/v1/me", "/api/v2/me",
        }))
        user = None
        wire = catalog = None
        code = None
        cancelled = False
        try:
            url = _url(snapshot.url)  # Reuse strict prefix + insecure guards.
            health = await self._request(guarded, url + "/healthz")
            if health.status != 200:
                raise CatalogError("remote_error")
            body = health.body
            if (not isinstance(body, dict) or "protocol" not in body
                    or type(body["protocol"]) is not int):
                raise CatalogError("invalid_response")
            if body["protocol"] == 1:
                if (body.keys() != {"status", "service", "version", "protocol"}
                        or body["status"] != "ok"
                        or body["service"] != "voxarium_studio_remote"
                        or not isinstance(body["version"], str)):
                    raise CatalogError("invalid_response")
                wire = 1
                response = await self._request(guarded, url + "/api/v1/me", snapshot.token)
                user = self._legacy_user(response)
                code = "catalog_unavailable"
            elif body["protocol"] == 2:
                client = CatalogClient(
                    _DiscoveredTransport(guarded, url, health), url=url, token=snapshot.token,
                )
                verification = await client.verify()
                user, wire, catalog = verification.user, 2, 2
            else:
                raise CatalogError("version_mismatch")
        except asyncio.CancelledError:
            cancelled = True
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "verification_failed"
        # Local fences are errors, not a misleading status of the latest key.
        self._guards._check(snapshot)
        if guarded.failure is not None:
            raise CatalogError(guarded.failure)
        if cancelled:
            raise asyncio.CancelledError() from None
        if code == "unauthorised":  # strict v2 error -> §5.1 local spelling
            code = "remote_unauthorised"
        if code in {None, "catalog_unavailable"}:
            state = "valid"
        elif code == "remote_unauthorised":
            state = "unauthorised"
        elif code == "transport_unavailable":
            state = "offline"
        elif code in {"version_mismatch", "invalid_response", "capabilities_missing"}:
            state = "incompatible"
        elif code == "insecure_blocked":
            state = "insecure_blocked"
        else:
            state = "error"
        checked_at = None
        try:
            now = self._clock()
            if isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None:
                checked_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            pass
        self._guards._check(snapshot)
        if checked_at is None:
            raise CatalogError("invalid_clock")
        result = VerificationStatus(
            state, checked_at, user if state == "valid" else None, wire, catalog, code,
        )
        if _contains_secret(result, snapshot.token):
            raise CatalogError("invalid_response")
        return result
