"""Standalone Remote REST boundary (studio-design §5.1/§6, dev spec §5.2.1).

Read services are mandatory and trusted; C6/C7/C9 commands are explicitly injected.
There is no home, credential
lookup, transport, cache, active-project selection or production composition here.
``ServerMetadataProvider`` is an explicit trusted injection seam, NOT a new
storage implementation. Only an unavailable verification result may be unknown
(studio-design §5.1; catalog-metadata-contract §4). Metadata/presence, pending
or reader failures still fail closed, never infer missing credentials or return
a successful empty/partial list. GET servers never runs Verify or a clock.
The separate VS-122 rows adapter is not composed here.

GET projects captures the current profile revision once and delegates to the
existing v2 guards. Cursor is only after's alias, not a cross-page snapshot or a
browser revision CAS. Verify's v1 success never authorizes catalog access.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

from ..core.application_config import RemoteServerProfile, validate_profile_name
from ..core.security import require_auth
from ..sync.catalog import CatalogError, CatalogPage, CatalogProject, CreateResult, _id, _label, _project
from ..sync.catalog_create_service import CatalogCreateService
from ..sync.catalog_service import CatalogService, ProfileProvider
from ..sync.catalog_verification import CatalogVerificationService, VerificationStatus
from ..sync.profile_lifecycle import ConsumerCensus
from ..sync.profiles import ProfileError
from ..sync.protocol import normalise_url


class _DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              revalidate_instances="always", hide_input_in_errors=True)


class VerifyInput(_DTO):
    expected_revision: Annotated[int, Field(ge=1)]


class CreateProjectInput(VerifyInput):
    id: str
    display_name: str
    operation_id: str

    @field_validator("id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        return _id(value)

    @field_validator("display_name")
    @classmethod
    def valid_label(cls, value: str) -> str:
        _label(value)
        # C8 owns exact caller input. Normalization belongs only to the wire;
        # even equivalent-but-changed labels must conflict on operation replay.
        return value

    @field_validator("operation_id")
    @classmethod
    def valid_operation(cls, value: str) -> str:
        return _operation_id(value)


class RenameProfileInput(VerifyInput):
    name: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return validate_profile_name(value)


class CreateProfileInput(_DTO):
    name: str
    url: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return validate_profile_name(value)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return normalise_url(value)


class ChangeProfileUrlInput(VerifyInput):
    url: str
    clear_credential: Literal[True]

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return normalise_url(value)

    @field_validator("clear_credential", mode="before")
    @classmethod
    def explicit_clear(cls, value: object) -> bool:
        # Literal[True] alone also accepts 1; confirmation is a strict bool.
        if value is not True:
            raise ValueError("Invalid confirmation")
        return True


class CredentialInput(VerifyInput):
    token: SecretStr = Field(repr=False)

    @model_validator(mode="after")
    def nonempty_token(self) -> CredentialInput:
        if not self.token.get_secret_value().strip():
            raise ValueError("Invalid credential")
        return self


class ConfirmConsumersInput(VerifyInput):
    """Exactly what the user was shown (dev spec §16.11); a stale view conflicts."""

    url: str
    bound_roots: Annotated[list[Annotated[str, Field(min_length=1)]], Field(max_length=10_000)]

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        # Never normalised here: the confirmation is for the exact URL shown.
        if normalise_url(value) != value:
            raise ValueError("Invalid server address")
        return value


class ConsumerCensusDTO(_DTO):
    profile_id: str
    url: str
    revision: Annotated[int, Field(ge=1)]
    state: Literal["known", "unknown"]
    confirmed_at: str | None
    bound_roots: list[str]
    active_roots: list[str]
    operations: list[Literal["verify", "read", "create"]]


class RecoveryConfirmation(_DTO):
    profile_id: str
    action: str

    @field_validator("profile_id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Invalid confirmation")
        return value


class RecoverProfileInput(VerifyInput):
    confirmation: RecoveryConfirmation


class _User(_DTO):
    id: str
    display_name: str
    color: str


class VerificationDTO(_DTO):
    state: Literal["valid", "unauthorised", "offline", "incompatible", "insecure_blocked", "error"]
    checked_at: str
    user: _User | None
    wire_protocol: int | None
    catalog_version: int | None
    error_code: str | None

    @model_validator(mode="after")
    def sanitized_status(self) -> VerificationDTO:
        if self.error_code is not None and self.error_code not in _ERROR_STATUS:
            raise ValueError("Invalid verification status")
        return self


class UncheckedVerification(_DTO):
    # No fabricated clock/cache entry for a profile that has not been checked.
    state: Literal["unchecked"]


class CheckingVerification(_DTO):
    # An injected metadata snapshot may report a check in progress. This DTO
    # neither starts a check nor invents its eventual checked_at/result.
    state: Literal["checking"]


class UnavailableVerification(_DTO):
    # No applicable result is known; this proves neither unchecked history nor
    # a performed check/error (catalog-metadata-contract §4, VS-123).
    state: Literal["unavailable"]


class ServerProfile(RemoteServerProfile):
    """Secret-free metadata snapshot, never permission to use the catalog."""

    model_config = _DTO.model_config
    revision: Annotated[int, Field(ge=1)]
    has_token: bool
    credential_source: Literal["stored", "environment", "none"]
    verification: UncheckedVerification | CheckingVerification | UnavailableVerification | VerificationDTO

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value: object) -> str:
        # This is a read DTO, not fresh profile input. Preserve historical labels
        # exactly as S2 reads do, including during FastAPI response validation.
        if not isinstance(value, str):
            raise ValueError("Invalid profile metadata")
        return value

    @model_validator(mode="after")
    def consistent_source(self) -> ServerProfile:
        if self.has_token != (self.credential_source != "none"):
            raise ValueError("Invalid credential metadata")
        return self


class ServerMetadataProvider(Protocol):
    """Complete secret-free, pending-safe snapshots; no reads of raw keys here.

    No default/production adapter exists. Correct metadata may explicitly carry
    unavailable verification, not fabricated unchecked/checked history. Unknown
    metadata/presence or pending/reader failures must raise, never become a
    successful empty/partial list, none source, or unavailable verification.
    """

    def list(self) -> tuple[ServerProfile, ...]: ...


class ProfileMutations(Protocol):
    """S2 publication plus an exact, pending-safe response snapshot (§5.1)."""

    def create(self, name: str, url: str) -> ServerProfile: ...

    def rename(self, profile_id: str, name: str, *, expected_revision: int) -> ServerProfile: ...

    def change_url(self, profile_id: str, url: str, *, expected_revision: int,
                   clear_credential: bool) -> ServerProfile: ...

    def set_credential(self, profile_id: str, token: SecretStr, *, expected_revision: int) -> ServerProfile: ...

    def clear_credential(self, profile_id: str, *, expected_revision: int) -> ServerProfile: ...

    def delete(self, profile_id: str, *, expected_revision: int) -> None: ...

    def recover(self, profile_id: str, *, expected_revision: int) -> ServerProfile | None: ...


class ConsumerConfirmation(Protocol):
    """Gate L owner (§16.11): census and the user's explicit URL confirmation."""

    def consumer_census(self, profile_id: str) -> ConsumerCensus: ...

    def confirm_consumers(self, profile_id: str, *, expected_revision: int, url: str,
                          bound_roots: tuple[str, ...]) -> ConsumerCensus: ...

    def withdraw_consumers(self, profile_id: str, *, expected_revision: int) -> ConsumerCensus: ...


class ProjectCreation(Protocol):
    """Application-owned C8 commands; never a request-local service (§5.1/C9)."""

    async def create_project(self, profile_id: str, *, expected_revision: int,
                             id: str, display_name: str, operation_id: str) -> CreateResult: ...

    async def recover_create(self, profile_id: str, operation_id: str, *,
                             expected_revision: int) -> CreateResult: ...


class _RecoveredProfile(_DTO):
    profile: ServerProfile | None


class _Servers(_DTO):
    items: list[ServerProfile]


class _Project(_DTO):
    id: str
    display_name: str
    storage_epoch: str
    metadata_revision: Annotated[int, Field(ge=1)]
    created_at: str


class _Projects(_DTO):
    items: list[_Project]
    next_cursor: str | None


class _CreateResult(_DTO):
    state: Literal["created", "conflict", "unknown", "rejected", "found", "absent"]
    project: _Project | None
    error_code: str | None


class RemoteError(_DTO):
    code: str
    details: dict[str, str]


# Fixed local vocabulary only; even a forged typed exception cannot echo input.
_ERROR_STATUS = {
    "invalid_request": 422, "invalid_id": 422, "invalid_display_name": 422,
    "invalid_operation_id": 422, "invalid_intent": 422,
    "profile_not_found": 404, "operation_not_found": 404,
    "profile_revision_conflict": 409, "credential_binding_conflict": 409,
    "operation_conflict": 409, "operation_in_progress": 409,
    "credential_binding_unproven": 409,
    "profile_url_collision": 409,
    "profile_in_use": 409, "profile_consumers_not_drained": 409,
    "consumer_inventory_changed": 409, "consumer_confirmation_unavailable": 503,
    "credential_clear_confirmation_required": 409,
    "profile_recovery_confirmation_conflict": 409,
    "profile_recovery_required": 409, "credential_required": 409,
    "invalid_credential": 409,
    "profile_store_unavailable": 503, "application_store_unavailable": 503,
    "credential_store_unavailable": 503, "metadata_unavailable": 503,
    "consumer_lifecycle_unavailable": 503, "profile_recovery_unavailable": 503,
    # Publication succeeded; response observation failed. Never invite an
    # automatic retry or imply that the mutation was rolled back (§5.2.1/C6).
    "profile_committed_metadata_unavailable": 503,
    "intent_store_unavailable": 503, "intent_store_invalid": 503,
    "invalid_intent_path": 503, "invalid_intent_mode": 503,
    "invalid_catalog_readers": 503,
    "invalid_profile_metadata": 502, "invalid_response": 502,
    "invalid_server_url": 502,
    "remote_unauthorised": 502, "insecure_blocked": 502,
    "transport_unavailable": 502, "catalog_unavailable": 502,
    "version_mismatch": 502, "capabilities_missing": 502,
    "redirect_refused": 502, "remote_error": 502,
    "temporarily_unavailable": 502, "verification_failed": 502,
    "invalid_clock": 502, "service_unavailable": 502,
    "identity_mismatch": 502, "project_not_found": 502,
    "metadata_conflict": 502, "snapshot_changed": 502,
    "payload_too_large": 502,
}


_UNKNOWN_CODES = frozenset({
    "create_unknown", "transport_unavailable", "invalid_response", "version_mismatch",
    "redirect_refused", "temporarily_unavailable", "remote_error",
})
_REJECTED_CODES = frozenset({
    "invalid_id", "invalid_display_name", "invalid_request", "unauthorised",
    "project_not_found", "metadata_conflict", "snapshot_changed", "payload_too_large",
    "remote_error",
})


def _create_result(value: CreateResult, *, expected_id: str | None = None) -> _CreateResult:
    """Map a returned result only, never a thrown boundary failure (C0/C9)."""
    if (type(value) is not CreateResult or type(value.state) is not str
            or value.state not in {"created", "conflict", "unknown", "rejected", "found", "absent"}):
        raise CatalogError("invalid_response")
    project = None
    code = value.error_code
    if value.state in {"created", "found"}:
        if type(value.project) is not CatalogProject or code is not None:
            raise CatalogError("invalid_response")
        # Validate all metadata, not just the DTO's primitive field types.
        validated = _project(asdict(value.project), expected_id)
        project = _Project.model_validate(asdict(validated))
    else:
        if value.project is not None:
            raise CatalogError("invalid_response")
        if value.state in {"conflict", "absent"}:
            required = "project_exists" if value.state == "conflict" else "project_not_found"
            if type(code) is not str or code != required:
                raise CatalogError("invalid_response")
        elif value.state == "unknown":
            # Invalid 201 identity is an invalid response, not a new public code.
            if type(code) is str and code == "identity_mismatch":
                code = "invalid_response"
            elif type(code) is not str or code not in _UNKNOWN_CODES:
                code = "remote_error"
        elif type(code) is not str or code not in _REJECTED_CODES:
            code = "remote_error"
    return _CreateResult(state=value.state, project=project, error_code=code)


def _error(code: str, status: int | None = None) -> JSONResponse:
    reserved = {"studio_unauthorised", "studio_forbidden", "method_not_allowed"}
    if (type(code) is not str or (code not in _ERROR_STATUS
            and not (status is not None and code in reserved))):
        code = "service_unavailable"
    return JSONResponse(
        RemoteError(code=code, details={}).model_dump(mode="json"),
        status_code=status or _ERROR_STATUS[code],
        headers={"Cache-Control": "no-store"},
    )


class _RemoteRoute(APIRoute):
    """Cover auth and validation too, without global exception handlers/middleware."""

    @staticmethod
    def error_response(code: str, status: int | None = None) -> JSONResponse:
        return _error(code, status)

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()
        error_response = self.error_response

        async def handler(request: Request):
            try:
                response = await original(request)
            except RequestValidationError:
                response = error_response("invalid_request")
            except HTTPException as error:
                if error.status_code in {401, 403}:
                    code = "studio_unauthorised" if error.status_code == 401 else "studio_forbidden"
                    response = error_response(code, error.status_code)
                    if error.status_code == 401:
                        response.headers["WWW-Authenticate"] = "Bearer"
                else:
                    response = error_response("service_unavailable")
            except CatalogError as error:
                code = "remote_unauthorised" if error.code == "unauthorised" else error.code
                response = error_response(code)
            except ProfileError as error:
                code = error.args[0] if len(error.args) == 1 and type(error.args[0]) is str else "metadata_unavailable"
                response = error_response(code)
            except ValidationError:
                # Response/provider validation is not the caller's raw input.
                response = error_response("invalid_response")
            except Exception:
                response = error_response("service_unavailable")
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler

    async def handle(self, scope, receive, send) -> None:
        if self.methods and scope["method"] not in self.methods:
            response = _error("method_not_allowed", 405)
            response.headers["Allow"] = ", ".join(sorted(self.methods))
            await response(scope, receive, send)
            return
        await super().handle(scope, receive, send)


class _CreateRoute(_RemoteRoute):
    @staticmethod
    def error_response(code: str, status: int | None = None) -> JSONResponse:
        # C8's absent transport, closed admission and finite-capacity refusal
        # share this fixed code. C9 maps local owner unavailability to 503;
        # retain the pre-existing read/Verify boundary outside these routes.
        if status is None and (type(code) is not str or code not in _ERROR_STATUS
                               or code == "service_unavailable"):
            status = 503
        return _error(code, status)


def _query(request: Request, allowed: set[str]) -> None:
    keys = list(request.query_params)
    if (set(keys) - allowed or any(len(request.query_params.getlist(k)) != 1 for k in keys)):
        raise CatalogError("invalid_request")


def _profile_id(value: str) -> str:
    try:
        valid = str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise CatalogError("invalid_request")
    return value


def _operation_id(value: str) -> str:
    valid = True
    try:
        CatalogCreateService._operation_id(value)
    except CatalogError:
        valid = False
    if not valid:
        raise CatalogError("invalid_request")
    return value


def _profile(provider: ProfileProvider, profile_id: str) -> RemoteServerProfile:
    value = provider.get(profile_id)
    if not isinstance(value, RemoteServerProfile):
        raise CatalogError("invalid_profile_metadata")
    value = RemoteServerProfile.model_validate(value.model_dump(warnings=False), strict=True,
                                              context={"stored_profile_metadata": True})
    if value.profile_id != profile_id:
        raise CatalogError("invalid_profile_metadata")
    return value


def create_remote_router(*, metadata: ServerMetadataProvider, profiles: ProfileProvider,
                         verification: CatalogVerificationService,
                         catalog: CatalogService,
                         mutations: ProfileMutations | None = None,
                         creation: ProjectCreation | None = None,
                         consumers: ConsumerConfirmation | None = None) -> APIRouter:
    """Standalone factory only: never mounted by importing this module.

    ``profiles`` must observe the same pending-safe source as the injected
    services. Profile mutations are explicitly opt-in; read-only compositions retain
    their route surface. This factory does not choose a storage home.
    """
    router = APIRouter(prefix="/api/remote/servers", route_class=_RemoteRoute,
                       dependencies=[Depends(require_auth)])

    @router.get("", response_model=_Servers)
    async def servers(request: Request):
        _query(request, set())
        if await request.body():
            raise CatalogError("invalid_request")
        values = metadata.list()
        if not isinstance(values, tuple) or any(not isinstance(v, ServerProfile) for v in values):
            raise CatalogError("metadata_unavailable")
        # Revalidate even constructed/copied models; never dump arbitrary extras.
        items = [ServerProfile.model_validate(v.model_dump(warnings=False), context={"stored_profile_metadata": True})
                 for v in values]
        if len({v.profile_id for v in items}) != len(items):
            raise CatalogError("invalid_profile_metadata")
        return _Servers(items=items)

    if mutations is not None:
        @router.post("", response_model=ServerProfile, status_code=201)
        async def create_profile(request: Request, body: CreateProfileInput):
            _query(request, set())
            return mutations.create(body.name, body.url)

        @router.patch("/{profile_id}", response_model=ServerProfile)
        async def patch_profile(profile_id: str, request: Request,
                                body: RenameProfileInput | ChangeProfileUrlInput):
            _query(request, set())
            _profile_id(profile_id)
            # One explicit action, never split a single Apply into two commits.
            if isinstance(body, RenameProfileInput):
                return mutations.rename(profile_id, body.name, expected_revision=body.expected_revision)
            return mutations.change_url(profile_id, body.url, expected_revision=body.expected_revision,
                                        clear_credential=body.clear_credential)

        @router.put("/{profile_id}/credential", response_model=ServerProfile)
        async def set_credential(profile_id: str, request: Request, body: CredentialInput):
            _query(request, set())
            _profile_id(profile_id)
            return mutations.set_credential(profile_id, body.token, expected_revision=body.expected_revision)

        @router.delete("/{profile_id}/credential", response_model=ServerProfile)
        async def clear_credential(profile_id: str, request: Request, body: VerifyInput):
            _query(request, set())
            _profile_id(profile_id)
            return mutations.clear_credential(profile_id, expected_revision=body.expected_revision)

        @router.delete("/{profile_id}", status_code=204)
        async def delete_profile(profile_id: str, request: Request, body: VerifyInput):
            _query(request, set())
            _profile_id(profile_id)
            mutations.delete(profile_id, expected_revision=body.expected_revision)
            return Response(status_code=204)

        @router.post("/{profile_id}/recover", response_model=_RecoveredProfile)
        async def recover_profile(profile_id: str, request: Request, body: RecoverProfileInput):
            _query(request, set())
            _profile_id(profile_id)
            if (body.confirmation.profile_id != profile_id
                    or body.confirmation.action != "discard_uncertain_credential"):
                raise ProfileError("profile_recovery_confirmation_conflict")
            return _RecoveredProfile(profile=mutations.recover(
                profile_id, expected_revision=body.expected_revision))

    if consumers is not None:
        def census(profile_id: str, value: ConsumerCensus) -> ConsumerCensusDTO:
            if not isinstance(value, ConsumerCensus) or value.profile_id != profile_id:
                raise CatalogError("invalid_response")
            return ConsumerCensusDTO.model_validate({
                "profile_id": value.profile_id, "url": value.url, "revision": value.revision,
                "state": value.state, "confirmed_at": value.confirmed_at,
                "bound_roots": list(value.bound_roots), "active_roots": list(value.active_roots),
                "operations": list(value.operations),
            })

        @router.get("/{profile_id}/consumers", response_model=ConsumerCensusDTO)
        async def consumer_census(profile_id: str, request: Request):
            _query(request, set())
            if await request.body():
                raise CatalogError("invalid_request")
            _profile_id(profile_id)
            return census(profile_id, consumers.consumer_census(profile_id))

        @router.post("/{profile_id}/consumers/confirmation", response_model=ConsumerCensusDTO)
        async def confirm_consumers(profile_id: str, request: Request, body: ConfirmConsumersInput):
            _query(request, set())
            _profile_id(profile_id)
            return census(profile_id, consumers.confirm_consumers(
                profile_id, expected_revision=body.expected_revision, url=body.url,
                bound_roots=tuple(body.bound_roots)))

        @router.delete("/{profile_id}/consumers/confirmation", response_model=ConsumerCensusDTO)
        async def withdraw_consumers(profile_id: str, request: Request, body: VerifyInput):
            _query(request, set())
            _profile_id(profile_id)
            return census(profile_id, consumers.withdraw_consumers(
                profile_id, expected_revision=body.expected_revision))

    @router.post("/{profile_id}/verify", response_model=VerificationDTO)
    async def verify(profile_id: str, request: Request, body: VerifyInput):
        _query(request, set())
        _profile_id(profile_id)
        result = await verification.verify(profile_id, expected_revision=body.expected_revision)
        if not isinstance(result, VerificationStatus):
            raise CatalogError("invalid_response")
        dto = VerificationDTO.model_validate(asdict(result))
        return dto

    @router.get("/{profile_id}/projects", response_model=_Projects)
    async def projects(profile_id: str, request: Request):
        _query(request, {"cursor"})
        if await request.body():
            raise CatalogError("invalid_request")
        _profile_id(profile_id)
        cursor = request.query_params.get("cursor")
        if cursor is not None:
            # §6 defines the local cursor as the literal canonical after ID.
            from ..sync.catalog import _id
            _id(cursor, "invalid_request")
        observed = _profile(profiles, profile_id)
        result = await catalog.list_projects(profile_id, expected_revision=observed.revision,
                                             after=cursor)
        if not isinstance(result, CatalogPage):
            raise CatalogError("invalid_response")
        latest = _profile(profiles, profile_id)
        if latest.revision != observed.revision or latest.url != observed.url:
            raise CatalogError("profile_revision_conflict")
        return _Projects.model_validate({"items": [asdict(p) for p in result.projects],
                                         "next_cursor": result.next_after})

    if creation is not None:
        commands = APIRouter(route_class=_CreateRoute)

        @commands.post("/{profile_id}/projects", response_model=_CreateResult, status_code=201)
        async def create_project(profile_id: str, request: Request, body: CreateProjectInput,
                                 response: Response):
            _query(request, set())
            _profile_id(profile_id)
            result = await creation.create_project(
                profile_id, expected_revision=body.expected_revision, id=body.id,
                display_name=body.display_name, operation_id=body.operation_id,
            )
            dto = _create_result(result, expected_id=body.id)
            response.status_code = 201 if dto.state == "created" else 200
            return dto

        @commands.post("/{profile_id}/projects/operations/{operation_id}/observe",
                     response_model=_CreateResult)
        async def observe_create(profile_id: str, operation_id: str, request: Request,
                                 body: VerifyInput):
            _query(request, set())
            _profile_id(profile_id)
            _operation_id(operation_id)
            # Only C8's original reservation determines the ID. No arbitrary
            # GET, Create retry, selection, activation or operation release.
            return _create_result(await creation.recover_create(
                profile_id, operation_id, expected_revision=body.expected_revision,
            ))
        router.include_router(commands)

    return router
