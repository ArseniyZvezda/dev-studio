"""Unmounted offline Create boundary (studio-design §5.1/§6, S3).

Only injected read-only S2 providers and a trusted injected transport are used.
Instance mode keeps only a local registry. Explicit durable mode additionally
requires an injected journal; it never chooses a path or falls back to memory.
Journal metadata cannot prove an original credential after restart: recovery
without the original live binding fails closed, never captures a replacement key.
Nothing here selects a project, enables sync or writes config.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from .catalog import (
    CatalogClient, CatalogError, CatalogResponse, CatalogTransport, CreateResult,
    Verification, _id, _label,
)
from .catalog_service import (
    CatalogService, CredentialProvider, ProfileProvider, _contains_secret,
    _ReadTransport, _Snapshot,
)
from .catalog_intents import CatalogIntent, CatalogIntentJournal, IntentRecord


T = TypeVar("T")


class _CreateTransport:
    """Reuse profile guards; permit at most one exact POST for this intent.

    Every await, including failed/cancelled requests, is fenced before control
    returns to the wire owner. Detected binding failures are permanently latched.
    """

    def __init__(self, guards: CatalogService, snapshot: _Snapshot,
                 project_id: str | None = None, label: str | None = None,
                 fence: Callable[[], None] | None = None):
        paths = {"/healthz", "/api/v2/me"}
        if project_id is not None:
            paths.add(f"/api/v2/projects/{project_id}")
        self._guard = _ReadTransport(guards, snapshot, frozenset(paths))
        self._guards = guards
        self._snapshot = snapshot
        self._gets = frozenset(snapshot.url + path for path in paths)
        self._body = {"id": project_id, "display_name": label}
        self.allow_post = project_id is not None
        self.post_sent = False
        self._fence = fence

    def check(self) -> None:
        if self._guard.failure is not None:
            raise CatalogError(self._guard.failure)
        self._guard.check()
        if self._fence is not None:
            self._fence()

    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      params: Mapping[str, str | int], json: Mapping[str, str] | None,
                      follow_redirects: bool) -> CatalogResponse:
        self.check()
        read = method == "GET" and url in self._gets and json is None
        create = (method == "POST" and self.allow_post and not self.post_sent
                  and url == self._snapshot.url + "/api/v2/projects"
                  and json == self._body)
        if not (read or create) or params or follow_redirects is not False:
            self._guard.failure = "invalid_request"
            raise CatalogError("invalid_request")
        if create:
            # Reserve BEFORE the await; timeout/cancellation can follow commit.
            self.post_sent = True
        response = None
        cancelled = failed = False
        try:
            response = await self._guards._transport.request(
                method, url, headers=headers, params=params, json=json,
                follow_redirects=False,
            )
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            failed = True
        self.check()
        if cancelled:
            raise asyncio.CancelledError() from None
        if failed:
            raise CatalogError("transport_unavailable")
        if (isinstance(response, CatalogResponse) and type(response.status) is int
                and 200 <= response.status <= 299
                and _contains_secret(response.body, self._snapshot.token)):
            self._guard.failure = "invalid_response"
            raise CatalogError("invalid_response")
        return response


@dataclass(repr=False)
class _Operation:
    # Exact caller parameters, not the normalized wire label. Even canonically
    # equivalent but changed input under the same local operation is a conflict.
    inputs: tuple[str, int, str, str]
    transport: _CreateTransport | None = None
    snapshot: _Snapshot | None = None
    result: CreateResult = field(default_factory=lambda: CreateResult(
        "unknown", error_code="create_unknown"))
    error: str | None = None
    busy: bool = False
    intent: CatalogIntent | None = None


class CatalogCreateService:
    """Explicit Create plus separate GET-only recovery; no runtime integration.

    Providers obey CatalogService's trusted, synchronous, read-only contract,
    including fencing pending S2 recovery (not RemoteProfiles.load_token).
    operation_id is local and is never sent over the wire. Durable mode reserves
    valid metadata synchronously before the first await. Invalid commands retain
    VS-113's instance-local reservation (the journal accepts valid metadata only).
    Registry entries, including failures, are not evicted: reusing an operation
    can never retry POST or migrate its captured profile/URL/revision/key.
    """

    def __init__(self, *, profiles: ProfileProvider, credentials: CredentialProvider,
                 transport: CatalogTransport,
                 mode: Literal["instance", "durable"] = "instance",
                 journal: CatalogIntentJournal | None = None):
        if (type(mode) is not str or mode not in {"instance", "durable"}
                or (mode == "durable" and not isinstance(journal, CatalogIntentJournal))
                or (mode == "instance" and journal is not None)):
            raise CatalogError("invalid_intent_mode")
        self._guards = CatalogService(profiles=profiles, credentials=credentials,
                                      transport=transport)
        self._journal = journal
        self._operations: dict[str, _Operation] = {}

    @staticmethod
    def _same_inputs(original: tuple, candidate: tuple) -> bool:
        return all(type(a) is type(b) and a == b for a, b in zip(original, candidate))

    @staticmethod
    def _journal_call(action: Callable[[], T]) -> T:
        # Also sanitize injected disk fault probes; never retain paths/secrets in
        # exception context. No journal error permits an in-memory fallback.
        code = None
        result = None
        try:
            result = action()
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "intent_store_unavailable"
        if code is not None:
            raise CatalogError(code)
        return result

    def _lookup(self, operation_id: str) -> IntentRecord | None:
        code = None
        record = None
        try:
            record = self._journal_call(lambda: self._journal.lookup(operation_id))
        except CatalogError as error:
            code = error.code
        if code is not None and code != "operation_not_found":
            raise CatalogError(code)
        return record

    def _profile_matches(self, intent: CatalogIntent) -> None:
        # Compare the recorded locator BEFORE any credential lookup. Revision
        # equality alone cannot prove an unchanged ENV credential after restart.
        profile = self._guards._profile(intent.profile_id, intent.revision)
        if profile.url != intent.url:
            raise CatalogError("profile_revision_conflict")

    def _check_record(self, operation: _Operation) -> None:
        if operation.error is not None and operation.error.startswith("intent_store_"):
            raise CatalogError(operation.error)
        if self._journal is None or operation.intent is None:
            return
        code = None
        try:
            record = self._lookup(operation.intent.operation_id)
            if record is None or record.intent != operation.intent:
                raise CatalogError("intent_store_invalid")
            self._profile_matches(record.intent)
        except CatalogError as error:
            code = error.code
        if code is not None:
            operation.error = code
            if operation.transport is not None:
                operation.transport._guard.failure = code
            raise CatalogError(code)

    def _record_result(self, operation: _Operation, state: str) -> None:
        if self._journal is None:
            return
        code = None
        try:
            self._journal_call(lambda: self._journal.record_state(operation.intent, state=state))
        except CatalogError as error:
            code = error.code
        if code is not None:
            operation.error = code
            raise CatalogError(code)

    @staticmethod
    def _operation_id(operation_id: str) -> None:
        if (not isinstance(operation_id, str)
                or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", operation_id) is None):
            raise CatalogError("invalid_operation_id")

    async def _run(self, snapshot: _Snapshot, transport: _CreateTransport,
                   action: Callable[[CatalogClient], Awaitable[T]] | None = None
                   ) -> T | Verification:
        result = None
        code = None
        cancelled = False
        try:
            transport.check()
            client = CatalogClient(transport, url=snapshot.url, token=snapshot.token)
            verification = await client.verify()
            transport.check()
            if _contains_secret(verification, snapshot.token):
                raise CatalogError("invalid_response")
            result = verification if action is None else await action(client)
        except asyncio.CancelledError:
            cancelled = True
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "catalog_unavailable"
        # Outside handlers: no raw exception/message is retained as context.
        transport.check()
        if cancelled:
            raise asyncio.CancelledError() from None
        if code is not None:
            raise CatalogError(code)
        if _contains_secret(result, snapshot.token):
            raise CatalogError("invalid_response")
        return result

    async def verify(self, profile_id: str, *, expected_revision: int) -> Verification:
        snapshot = self._guards._snapshot(profile_id, expected_revision)
        return await self._run(snapshot, _CreateTransport(self._guards, snapshot))

    @staticmethod
    def _bound(operation: _Operation) -> tuple[_Snapshot, _CreateTransport]:
        if operation.snapshot is None or operation.transport is None:
            raise CatalogError(operation.error or "catalog_unavailable")
        operation.transport.check()
        return operation.snapshot, operation.transport

    async def create_project(self, profile_id: str, *, expected_revision: int,
                             id: str, display_name: str,
                             operation_id: str) -> CreateResult:
        self._operation_id(operation_id)
        inputs = (profile_id, expected_revision, id, display_name)
        existing = self._operations.get(operation_id)
        if existing is not None:
            if not self._same_inputs(existing.inputs, inputs):
                raise CatalogError("operation_conflict")
            self._check_record(existing)
            self._bound(existing)
            if existing.error is not None:
                raise CatalogError(existing.error)
            # A concurrent repeat observes unknown, never starts another POST.
            return existing.result
        operation = _Operation(inputs)
        self._operations[operation_id] = operation
        # Reserve valid local operation IDs even when the intent is invalid.
        # Repairing its parameters is a NEW explicit command, not a retry/rebind.
        # No await between reservation, validation and binding scalars.
        code = None
        try:
            if self._journal is not None:
                record = self._lookup(operation_id)
                if record is not None:
                    original = record.intent
                    if not self._same_inputs(
                            (original.profile_id, original.revision,
                             original.project_id, original.display_name), inputs):
                        raise CatalogError("operation_conflict")
                    operation.intent = original
                    self._profile_matches(original)
                    # Do not infer the original key from current metadata or
                    # journal state, even 'created'. No provider contract here
                    # proves unchanged credentials across service lifetimes.
                    raise CatalogError("credential_binding_unproven")
            if type(expected_revision) is not int:
                raise CatalogError("profile_revision_conflict")
            project_id, label = _id(id), _label(display_name)
            operation.snapshot = self._guards._snapshot(profile_id, expected_revision)
            if self._journal is not None:
                snapshot = operation.snapshot
                intent = CatalogIntent(operation_id, profile_id, snapshot.url,
                                       expected_revision, id, display_name)
                if _contains_secret(intent, snapshot.token):
                    raise CatalogError("invalid_intent")
                reservation = self._journal_call(lambda: self._journal.reserve(intent))
                operation.intent = intent
                if not reservation.post_permitted:
                    operation.snapshot = None
                    raise CatalogError("credential_binding_unproven")
        except CatalogError as error:
            code = error.code
        if code is not None:
            operation.error = code
            raise CatalogError(code)
        snapshot = operation.snapshot
        transport = _CreateTransport(self._guards, snapshot, project_id, label,
                                     lambda: self._check_record(operation))
        operation.transport = transport
        operation.busy = True
        cancelled = False
        result = None
        try:
            result = await self._run(snapshot, transport, lambda client: client.create_project(
                id=project_id, display_name=display_name, operation_id=operation_id))
        except asyncio.CancelledError:
            cancelled = True
        except CatalogError as error:
            code = error.code
        finally:
            transport.allow_post = False
            operation.busy = False
        # Observation persistence is synchronous too. A failed/ambiguous write
        # latches the operation closed; the committed reservation is NEVER freed.
        self._record_result(operation, result.state if result is not None else "unknown")
        transport.check()
        if code is not None:
            operation.error = code
            raise CatalogError(code)
        if cancelled:
            raise asyncio.CancelledError() from None
        operation.result = result
        return result

    async def recover_create(self, profile_id: str, operation_id: str, *,
                             expected_revision: int) -> CreateResult:
        self._operation_id(operation_id)
        operation = self._operations.get(operation_id)
        if operation is None and self._journal is not None:
            record = self._lookup(operation_id)
            if record is not None:
                intent = record.intent
                operation = _Operation((intent.profile_id, intent.revision,
                                        intent.project_id, intent.display_name),
                                       intent=intent, error="credential_binding_unproven")
                self._operations[operation_id] = operation
        if operation is None:
            raise CatalogError("operation_not_found")
        if not self._same_inputs(operation.inputs[:2], (profile_id, expected_revision)):
            raise CatalogError("operation_conflict")
        self._check_record(operation)
        snapshot, transport = self._bound(operation)
        if operation.busy:
            raise CatalogError("operation_in_progress")
        operation.busy = True
        transport.allow_post = False
        code = None
        cancelled = False
        result = None

        async def read_original(client: CatalogClient) -> CreateResult:
            # Only the exact project's 404 means absent. A failed health/me
            # verification (even a 404) must remain an error, not an observation.
            project = None
            read_code = None
            try:
                project = await client.get_project(operation.inputs[2])
            except CatalogError as error:
                read_code = error.code
            if read_code is not None and read_code != "project_not_found":
                raise CatalogError(read_code)
            return (CreateResult("found", project) if project is not None
                    else CreateResult("absent", error_code="project_not_found"))

        try:
            result = await self._run(snapshot, transport, read_original)
        except asyncio.CancelledError:
            cancelled = True
        except CatalogError as error:
            code = error.code
        finally:
            operation.busy = False
        self._record_result(operation, result.state if result is not None else "unknown")
        transport.check()
        if cancelled:
            raise asyncio.CancelledError() from None
        if code is not None:
            raise CatalogError(code)
        operation.result = result
        operation.error = None
        # Observations grant no selection/ownership, even after a successful POST.
        return operation.result
