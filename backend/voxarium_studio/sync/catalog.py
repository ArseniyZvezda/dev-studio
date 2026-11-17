"""Unwired v2 catalog contract, with an explicitly injected transport only.

Implements remote-identity/studio-design §5.1/§6 (S3) and server-design §3.
This module provides no HTTP implementation, routes, credential lookup, selection,
sync activation or durable operation journal. Production v1 constants are untouched.
An eventual caller must bind profile/credential revisions and implement Select's
barrier separately; neither verification nor a successful Create grants consent.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import unquote, urlsplit

from .protocol import is_insecure, normalise_url


# Private expectations for this opt-in contract, NOT the live sync protocol.
_REQUIRED_CAPABILITIES = frozenset({
    "project_catalog", "explicit_project_create", "strict_connect",
})
_ERRORS = {
    400: {"invalid_id", "invalid_display_name", "invalid_request"},
    401: {"unauthorised"},
    404: {"project_not_found"},
    409: {"project_exists", "metadata_conflict", "snapshot_changed"},
    413: {"payload_too_large"},
    503: {"temporarily_unavailable"},
}


class CatalogError(ValueError):
    """Fixed local code only; never raw server messages or transport exceptions."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CatalogResponse:
    status: int
    body: object = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


class CatalogTransport(Protocol):
    """Trusted injected boundary; must not log headers or follow redirects.

    Returns decoded JSON without retaining/printing a raw HTTP response. There is
    intentionally no default implementation. Any future real transport needs its
    own security review (TLS, timeouts, byte limits, no-store and debug logging).
    """

    async def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        params: Mapping[str, str | int], json: Mapping[str, str] | None,
        follow_redirects: bool,
    ) -> CatalogResponse: ...


@dataclass(frozen=True)
class CatalogUser:
    id: str
    display_name: str
    color: str


@dataclass(frozen=True)
class Verification:
    user: CatalogUser
    capabilities: frozenset[str]


@dataclass(frozen=True)
class CatalogProject:
    id: str
    display_name: str
    storage_epoch: str
    metadata_revision: int
    created_at: str

    @property
    def space_instance_id(self) -> str:
        """Local alias only; never reinterpret the project's ID as incarnation."""
        return self.storage_epoch


@dataclass(frozen=True)
class CatalogPage:
    projects: tuple[CatalogProject, ...]
    next_after: str | None


@dataclass(frozen=True)
class CreateResult:
    state: Literal["created", "conflict", "unknown", "rejected", "found", "absent"]
    project: CatalogProject | None = None
    error_code: str | None = None


@dataclass
class _CreateOperation:
    id: str
    display_name: str
    result: CreateResult


def _id(value: object, code: str = "invalid_id") -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value) is None:
        raise CatalogError(code)
    return value


def _label(value: object) -> str:
    if not isinstance(value, str):
        raise CatalogError("invalid_display_name")
    # Reject controls before trimming, rather than silently removing newlines.
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or ord(c) in {0x2028, 0x2029}
           or 0x202A <= ord(c) <= 0x202E
           or 0x2066 <= ord(c) <= 0x2069 for c in value):
        raise CatalogError("invalid_display_name")
    label = unicodedata.normalize("NFC", value.strip())
    try:
        valid = 1 <= len(label) <= 120 and len(label.encode("utf-8")) <= 480
    except UnicodeError:
        valid = False
    if not valid:
        raise CatalogError("invalid_display_name")
    return label


def _object(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or value.keys() != fields:
        raise CatalogError("invalid_response")
    return value


def _version(value: object) -> None:
    if type(value) is not int or value != 2:
        raise CatalogError("version_mismatch")


def _project(value: object, expected_id: str | None = None) -> CatalogProject:
    raw = _object(value, {"id", "display_name", "storage_epoch", "metadata_revision", "created_at"})
    project_id = _id(raw["id"], "invalid_response")
    if expected_id is not None and project_id != expected_id:
        raise CatalogError("identity_mismatch")
    if (not isinstance(raw["storage_epoch"], str)
            or re.fullmatch(r"[0-9a-f]{32}", raw["storage_epoch"]) is None
            or type(raw["metadata_revision"]) is not int or raw["metadata_revision"] < 1
            or not isinstance(raw["created_at"], str) or not raw["created_at"]
            or not isinstance(raw["display_name"], str)):
        raise CatalogError("invalid_response")
    # Historical migrated labels are preserved literally (server-design §2.2).
    # New Create input, unlike catalog reads, applies the new label rules.
    return CatalogProject(**raw)


def _project_envelope(body: object, expected_id: str) -> CatalogProject:
    raw = _object(body, {"protocol", "project"})
    _version(raw["protocol"])
    return _project(raw["project"], expected_id)


def _error(response: CatalogResponse) -> str:
    # Even a server's request_id/message may echo the key: retain neither.
    body = response.body
    if isinstance(body, dict) and body.keys() == {"error"}:
        error = body["error"]
        if (isinstance(error, dict) and error.keys() == {"code", "message", "request_id"}
                and isinstance(error["code"], str)
                and isinstance(error["message"], str) and isinstance(error["request_id"], str)
                and error["code"] in _ERRORS.get(response.status, set())):
            return error["code"]
    return "remote_error"


def _url(raw: object) -> str:
    result = None
    if isinstance(raw, str):
        try:
            result = normalise_url(raw)
        except (ValueError, UnicodeError):
            pass
    # Raise outside the handler: not even __context__ retains raw input errors.
    if result is None:
        raise CatalogError("invalid_server_url")
    path = urlsplit(result).path
    # Endpoint prefix cannot change path meaning after another decode. Preserve
    # normalise_url's existing namespace; never merge URL aliases or redirects.
    decoded = unquote(path)
    if ("\\" in path or "%" in decoded or decoded != path
            or any(segment in {".", ".."} for segment in path.split("/"))
            or any(ord(c) <= 32 or ord(c) == 127 for c in result)):
        raise CatalogError("invalid_server_url")
    if is_insecure(result):
        raise CatalogError("insecure_blocked")
    return result


class CatalogClient:
    """One immutable endpoint/key, ephemeral verification and Create journal.

    Journal lifetime is this instance only, NOT crash recovery or server replay.
    A caller must preserve its own operation record across restarts and must never
    infer selection from a CreateResult. No project configuration is accessible.
    """

    def __init__(self, transport: CatalogTransport, *, url: str, token: str):
        self._url = _url(url)
        if (not isinstance(token, str) or not token or not token.isascii()
                or any(ord(c) <= 32 or ord(c) == 127 for c in token)):
            raise CatalogError("invalid_credential")
        self._transport = transport
        self._token = token
        self._verification: Verification | None = None
        self._verification_generation = 0
        self._operations: dict[str, _CreateOperation] = {}
        self._create_lock = asyncio.Lock()

    async def _request(
        self, method: str, path: str, *, authenticated: bool = True,
        params: Mapping[str, str | int] | None = None,
        body: Mapping[str, str] | None = None,
    ) -> CatalogResponse:
        headers = {"Accept": "application/json", "Cache-Control": "no-store"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        response = None
        cancelled = False
        try:
            response = await self._transport.request(
                method, self._url + path, headers=headers, params=params or {},
                json=body, follow_redirects=False,
            )
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # Never stringify, chain, log or retain the transport exception.
            pass
        if cancelled:
            # Preserve cancellation semantics, not a transport-supplied message.
            raise asyncio.CancelledError() from None
        if response is None:
            raise CatalogError("transport_unavailable")
        if (not isinstance(response, CatalogResponse) or type(response.status) is not int
                or not 100 <= response.status <= 599):
            raise CatalogError("invalid_response")
        if 300 <= response.status <= 399:
            raise CatalogError("redirect_refused")
        if (200 <= response.status <= 299 and isinstance(response.body, dict)
                and "protocol" in response.body):
            protocol = response.body.get("protocol")
            if type(protocol) is not int or protocol != 2:
                self._verification = None
                self._verification_generation += 1
                raise CatalogError("version_mismatch")
        if authenticated and response.status == 401:
            self._verification = None
            self._verification_generation += 1
        return response

    def _verified(self) -> Verification:
        if self._verification is None:
            raise CatalogError("verification_required")
        return self._verification

    async def verify(self) -> Verification:
        """Read-only discovery + authenticated identity; no WS or fallback v1."""
        self._verification = None
        self._verification_generation += 1
        generation = self._verification_generation
        response = await self._request("GET", "/healthz", authenticated=False)
        if response.status != 200:
            raise CatalogError(_error(response))
        health = response.body
        required = {"service", "version", "protocol", "supported_protocols", "catalog_api"}
        if (not isinstance(health, dict) or not required <= health.keys()
                or health.keys() - required - {"status"}
                or health["service"] != "voxarium_studio_remote"
                or not isinstance(health["version"], str)):
            raise CatalogError("invalid_response")
        _version(health["protocol"])
        _version(health["catalog_api"])
        supported = health["supported_protocols"]
        if (not isinstance(supported, list) or not supported
                or any(type(v) is not int for v in supported) or 2 not in supported):
            raise CatalogError("version_mismatch")
        response = await self._request("GET", "/api/v2/me")
        if response.status != 200:
            raise CatalogError(_error(response))
        me = _object(response.body, {"protocol", "catalog_api", "capabilities", "user"})
        _version(me["protocol"])
        _version(me["catalog_api"])
        capabilities = me["capabilities"]
        if (not isinstance(capabilities, list)
                or any(not isinstance(c, str) for c in capabilities)):
            raise CatalogError("invalid_response")
        if not _REQUIRED_CAPABILITIES <= set(capabilities):
            raise CatalogError("capabilities_missing")
        user = _object(me["user"], {"id", "display_name", "color"})
        if (not isinstance(user["id"], str) or not user["id"]
                or not isinstance(user["display_name"], str)
                or not isinstance(user["color"], str)
                or re.fullmatch(r"#[0-9a-fA-F]{6}", user["color"]) is None):
            raise CatalogError("invalid_response")
        # An older successful probe must not supersede a newer failed one.
        if generation != self._verification_generation:
            raise CatalogError("verification_stale")
        self._verification = Verification(CatalogUser(**user), frozenset(capabilities))
        return self._verification

    async def get_project(self, id: str) -> CatalogProject:
        self._verified()
        project_id = _id(id)
        response = await self._request("GET", f"/api/v2/projects/{project_id}")
        if response.status != 200:
            raise CatalogError(_error(response))
        return _project_envelope(response.body, project_id)

    async def list_projects(self, *, limit: int = 100, after: str | None = None) -> CatalogPage:
        self._verified()
        if type(limit) is not int or not 1 <= limit <= 200:
            raise CatalogError("invalid_request")
        params: dict[str, str | int] = {"limit": limit}
        if after is not None:
            params["after"] = _id(after)
        response = await self._request("GET", "/api/v2/projects", params=params)
        if response.status != 200:
            raise CatalogError(_error(response))
        raw = _object(response.body, {"protocol", "projects", "next_after"})
        _version(raw["protocol"])
        if not isinstance(raw["projects"], list) or len(raw["projects"]) > limit:
            raise CatalogError("invalid_response")
        projects = tuple(_project(p) for p in raw["projects"])
        ids = [p.id for p in projects]
        # Tolerate a repeated boundary row for defensive dedup, never an earlier
        # page masquerading as continuation (including a terminal page).
        if ids != sorted(set(ids)) or (after is not None and any(id < after for id in ids)):
            raise CatalogError("invalid_response")
        cursor = raw["next_after"]
        if cursor is not None:
            cursor = _id(cursor, "invalid_response")
            if not ids or cursor != ids[-1] or (after is not None and cursor <= after):
                raise CatalogError("invalid_response")
        return CatalogPage(projects, cursor)

    async def list_all(self, *, limit: int = 100, max_pages: int = 100) -> tuple[CatalogProject, ...]:
        """Bounded best-effort dedup by ID, not a snapshot or label-based merge.

        Pages may race with metadata edits; callers refresh before a later Select.
        A repeated ID keeps its last observed metadata. Never return partial data
        on an invalid cursor, failed page or exceeded page budget.
        """
        if type(max_pages) is not int or max_pages < 1:
            raise CatalogError("invalid_request")
        projects: dict[str, CatalogProject] = {}
        after = None
        for _ in range(max_pages):
            page = await self.list_projects(limit=limit, after=after)
            projects.update((p.id, p) for p in page.projects)
            if page.next_after is None:
                return tuple(projects[key] for key in sorted(projects))
            after = page.next_after
        raise CatalogError("pagination_limit")

    async def create_project(self, *, id: str, display_name: str, operation_id: str) -> CreateResult:
        """Explicit user command. Never select, re-POST or send operation_id.

        Unknown outcomes (including cancellation) are reserved before awaiting;
        subsequent calls with the same local operation can only read its result.
        After a process restart the *caller* must recover by GET, not resubmit.
        """
        self._verified()
        project_id, label = _id(id), _label(display_name)
        if (not isinstance(operation_id, str)
                or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", operation_id) is None):
            raise CatalogError("invalid_operation_id")
        async with self._create_lock:
            # Verification can fail while this command waits behind another
            # Create. Check at the mutation boundary, not only before the await.
            self._verified()
            existing = self._operations.get(operation_id)
            if existing is not None:
                if (existing.id, existing.display_name) != (project_id, label):
                    raise CatalogError("operation_conflict")
                return existing.result
            operation = _CreateOperation(project_id, label, CreateResult("unknown", error_code="create_unknown"))
            self._operations[operation_id] = operation
            response = None
            code = None
            try:
                response = await self._request(
                    "POST", "/api/v2/projects", body={"id": project_id, "display_name": label},
                )
            except CatalogError as error:
                code = error.code
            if response is None:
                operation.result = CreateResult("unknown", error_code=code)
            elif response.status == 201:
                project = None
                try:
                    project = _project_envelope(response.body, project_id)
                except CatalogError as error:
                    code = error.code
                operation.result = (CreateResult("created", project) if project is not None
                                    else CreateResult("unknown", error_code=code))
            else:
                code = _error(response)
                if response.status == 409 and code == "project_exists":
                    operation.result = CreateResult("conflict", error_code=code)
                elif response.status in {400, 401, 404, 409, 413}:
                    operation.result = CreateResult("rejected", error_code=code)
                else:
                    # A server may commit before failing to construct its response.
                    operation.result = CreateResult("unknown", error_code=code)
            return operation.result

    async def recover_create(self, operation_id: str) -> CreateResult:
        """GET the originally requested ID; found is NOT ownership or selection.

        Conflict/unknown can be reread repeatedly. Even an absent result permits
        no automatic replay: a new explicit user command needs a new operation.
        """
        self._verified()
        if not isinstance(operation_id, str) or operation_id not in self._operations:
            raise CatalogError("operation_not_found")
        async with self._create_lock:
            operation = self._operations[operation_id]
            if operation.result.state in {"created", "rejected"}:
                return operation.result
            project = None
            code = None
            try:
                project = await self.get_project(operation.id)
            except CatalogError as error:
                code = error.code
            if project is not None:
                operation.result = CreateResult("found", project)
            elif code == "project_not_found":
                operation.result = CreateResult("absent", error_code=code)
            else:
                operation.result = CreateResult("unknown", error_code=code)
            return operation.result
