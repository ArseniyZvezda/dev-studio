"""Application-owned durable Create (dev spec §5.2.1; activation plan C8).

No routes, project binding, replay authorization or production POST activation.
The default transport is absent until R3. Only an explicit trusted composition
(offline tests today) can supply it. Journal metadata never proves a cold key.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from . import credentials
from .catalog import CatalogError, CatalogTransport, CreateResult
from .catalog_create_service import CatalogCreateService
from .catalog_intents import CatalogIntentJournal
from .catalog_providers import PendingCatalogProviders
from .profile_lifecycle import ProfileLifecycle
from .profiles import ProfileError


MAX_LIVE_OPERATIONS = 1024
_SIDECARS = ("-journal", "-wal", "-shm")


def _owner_only(path: Path) -> None:
    """Strict policy, unlike the credential writer's best-effort wrapper.

    Empty new files are protected BEFORE SQLite can write metadata. Never
    change directory ACLs, follow links, or silently continue on an ACL error.
    """
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise CatalogError("intent_store_invalid")
    if os.name != "nt" and info.st_uid != os.getuid():
        raise CatalogError("intent_store_unavailable")
    path.chmod(0o600)
    credentials._windows_owner_only(path)
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise CatalogError("intent_store_unavailable")


class _OwnerOnlyJournal(CatalogIntentJournal):
    """Reuse the durable owner, including its poison/identity/schema checks.

    SQLite DELETE mode creates a rollback sidecar in the home, whose Windows
    ACL does NOT inherit from the DB. Preclaim that empty file and restrict it
    before EVERY transaction, including initialization/recovery, not after a
    commit. Existing hot sidecars are protected before SQLite can open them.
    No umask changes (process-global), home ACL edits, reset, removal or GC.
    """

    def _transaction(self, action, *, initializing: bool = False):
        if self._failure is not None:
            raise CatalogError(self._failure)
        result = None
        code = None
        try:
            # The trusted parent must remain the original, real local home.
            parent = self._path.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode)
                    or getattr(parent, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise CatalogError("intent_store_invalid")
            _owner_only(self._path)
            # Opening a WAL-mode DB can itself create a populated -shm file,
            # before the reused journal checks PRAGMA journal_mode. Reject its
            # persistent header BEFORE sqlite3.connect, not after that write.
            # Only our O_EXCL initialization winner may have an empty header.
            with self._path.open("rb") as stream:
                header = stream.read(20)
            if ((initializing and header)
                    or (not initializing and (header[:16] != b"SQLite format 3\0"
                                               or header[18:20] != b"\x01\x01"))):
                raise CatalogError("intent_store_invalid")
            for suffix in _SIDECARS:
                sidecar = Path(str(self._path) + suffix)
                try:
                    sidecar.lstat()
                except FileNotFoundError:
                    if suffix != "-journal":
                        continue
                    # SQLite can safely open a zero-length rollback journal.
                    # Only ENOENT permits creation; a dangling link is refused.
                    fd = os.open(sidecar, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                    os.close(fd)
                _owner_only(sidecar)
            result = super()._transaction(action, initializing=initializing)
            for suffix in ("", *_SIDECARS):
                candidate = Path(str(self._path) + suffix)
                try:
                    candidate.lstat()
                except FileNotFoundError:
                    if not suffix:
                        raise CatalogError("intent_store_invalid")
                else:
                    _owner_only(candidate)
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "intent_store_unavailable"
        if code is not None:
            if code.startswith("intent_store_"):
                self._failure = code
            raise CatalogError(code)
        return result


@dataclass(repr=False)
class _Route:
    inputs: tuple
    url: str | None = None
    cold: bool = False
    service: CatalogCreateService | None = None
    provider: PendingCatalogProviders | None = None
    error: str | None = None


async def _join(task: asyncio.Task):
    """Keep ownership through repeated cancellation, including request cleanup."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError() from None
    return task.result()


class CatalogCreateRuntime:
    """One lazy journal, bounded global routing and live services per app.

    Global routing precedes providers/keys, even for invalid commands. A new
    ID is refused at capacity (503), NEVER evicted to make room. Each admitted
    worker owns a C5 logical lease through service and cancellation cleanup;
    synchronous lifecycle/config/SQLite locks are never held across awaits.
    ``transport=None`` is the production R3 gate, before journal/key lookup.
    """

    def __init__(self, *, home: Path, check: Callable[[], None],
                 providers: Callable[..., PendingCatalogProviders],
                 lifecycle: ProfileLifecycle, transport: CatalogTransport | None = None):
        if (not isinstance(home, Path) or not home.is_absolute()
                or ".." in home.parts or str(home).startswith(("\\\\", "//"))):
            raise CatalogError("invalid_intent_path")
        self._path = home / "remote-catalog-intents.sqlite3"
        self._check = check
        self._providers = providers
        self._lifecycle = lifecycle
        self._transport = transport
        self._journal: CatalogIntentJournal | None = None
        self._failure: str | None = None
        self._routes: dict[str, _Route] = {}
        self._services: dict[tuple[str, str, int], tuple[PendingCatalogProviders, CatalogCreateService]] = {}
        self._requests: set[asyncio.Task] = set()
        self._cancelled_requests: set[asyncio.Task] = set()
        self._closed = False
        self._close_task: asyncio.Task | None = None

    def _admit(self) -> None:
        if self._closed or self._transport is None:
            raise CatalogError("service_unavailable")
        self._check()

    def _store(self) -> CatalogIntentJournal:
        if self._failure is not None:
            raise CatalogError(self._failure)
        code = None
        try:
            if self._journal is None:
                parent = self._path.parent.lstat()
                if (not stat.S_ISDIR(parent.st_mode)
                        or getattr(parent, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise CatalogError("intent_store_invalid")
                try:
                    self._path.lstat()
                except FileNotFoundError:
                    # A surviving sidecar is evidence of a lost DB, not a new
                    # installation. Never initialize over that recovery state.
                    for suffix in _SIDECARS:
                        try:
                            Path(str(self._path) + suffix).lstat()
                        except FileNotFoundError:
                            continue
                        raise CatalogError("intent_store_invalid")
                self._journal = _OwnerOnlyJournal(self._path)
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "intent_store_unavailable"
        if code is not None:
            self._failure = code
            raise CatalogError(code)
        return self._journal

    def _lookup(self, operation_id: str):
        record = None
        code = None
        try:
            record = self._store().lookup(operation_id)
        except CatalogError as error:
            if error.code != "operation_not_found":
                code = error.code
        if code is not None:
            self._failure = code
            raise CatalogError(code)
        return record

    def _route(self, operation_id: str, inputs: tuple, *, observe: bool) -> _Route:
        CatalogCreateService._operation_id(operation_id)
        route = self._routes.get(operation_id)
        if route is not None:
            original = route.inputs[:2] if observe else route.inputs
            if not CatalogCreateService._same_inputs(original, inputs):
                raise CatalogError("operation_conflict")
        else:
            if len(self._routes) >= MAX_LIVE_OPERATIONS:
                raise CatalogError("service_unavailable")
            # Keep even a failed journal-open command. A later changed body
            # must conflict, not escape the original in-memory reservation.
            if not observe:
                route = _Route(inputs)
                self._routes[operation_id] = route
            code = None
            record = None
            try:
                record = self._lookup(operation_id)
            except CatalogError as error:
                code = error.code
            if code is not None:
                if route is not None:
                    route.error = code
                raise CatalogError(code)
            if record is not None:
                intent = record.intent
                route = _Route((intent.profile_id, intent.revision, intent.project_id,
                                intent.display_name), url=intent.url, cold=True)
                self._routes[operation_id] = route
                original = route.inputs[:2] if observe else route.inputs
                if not CatalogCreateService._same_inputs(original, inputs):
                    raise CatalogError("operation_conflict")
            elif observe:
                raise CatalogError("operation_not_found")
        if route.cold:
            # Validate storage too: loss/corruption is never an empty success.
            self._lookup(operation_id)
            raise CatalogError("credential_binding_unproven")
        if route.error is not None:
            raise CatalogError(route.error)
        self._lookup(operation_id)  # Detect journal loss BEFORE any key lookup.
        return route

    def _bind(self, route: _Route) -> CatalogCreateService:
        code = None
        try:
            if route.service is None:
                profile_id, revision = route.inputs[:2]
                provider = self._providers(profile_id, expected_revision=revision)
                binding = (profile_id, provider.expected_url, revision)
                pair = self._services.get(binding)
                if pair is None:
                    service = CatalogCreateService(
                        profiles=provider, credentials=provider.load_token,
                        transport=self._transport, mode="durable", journal=self._store(),
                    )
                    pair = (provider, service)
                    self._services[binding] = pair
                route.provider, route.service = pair
                route.url = provider.expected_url
            route.provider.get(route.inputs[0])  # Original URL/revision, no key.
        except ProfileError as error:
            code = (error.args[0] if error.args in {
                ("profile_revision_conflict",), ("profile_not_found",),
                ("profile_recovery_required",), ("invalid_catalog_readers",),
            } else "profile_store_unavailable")
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "profile_store_unavailable"
        if code is not None:
            route.error = code
            raise CatalogError(code)
        return route.service

    async def _invoke(self, profile_id: str, operation_id: str, inputs: tuple, *,
                      observe: bool) -> CreateResult:
        self._admit()
        route = self._route(operation_id, inputs, observe=observe)
        # Admission precedes binding/token capture; only the logical lease is
        # retained across await and through the service's final journal write.
        lease = None
        code = None
        try:
            lease = self._lifecycle.operation(profile_id, kind="create")
        except ProfileError as error:
            code = (error.args[0] if error.args in {
                ("consumer_lifecycle_unavailable",), ("profile_not_found",),
                ("profile_recovery_required",), ("profile_revision_conflict",),
            } else "service_unavailable")
        except Exception:
            code = "service_unavailable"
        if code is not None:
            if route.service is None:
                route.error = code
            raise CatalogError(code)
        with lease:
            service = self._bind(route)
            if observe:
                return await service.recover_create(
                    profile_id, operation_id, expected_revision=inputs[1],
                )
            return await service.create_project(
                profile_id, expected_revision=inputs[1], id=inputs[2],
                display_name=inputs[3], operation_id=operation_id,
            )

    async def _request(self, profile_id: str, operation_id: str, inputs: tuple, *,
                       observe: bool) -> CreateResult:
        self._admit()
        task = asyncio.create_task(self._invoke(profile_id, operation_id, inputs, observe=observe))
        self._requests.add(task)
        task.add_done_callback(self._finished_request)
        cancelled = False
        result = None
        code = None
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except CatalogError as error:
            code = error.code
        except ProfileError as error:
            code = (error.args[0] if error.args in {
                ("consumer_lifecycle_unavailable",), ("profile_not_found",),
                ("profile_recovery_required",), ("profile_revision_conflict",),
            } else "service_unavailable")
        except Exception:
            code = "service_unavailable"
        if cancelled:
            self._cancel_request(task)
            try:
                await _join(task)
            except BaseException:
                pass
            raise asyncio.CancelledError() from None
        if code is not None:
            raise CatalogError(code)
        return result

    def _finished_request(self, task: asyncio.Task) -> None:
        self._requests.discard(task)
        self._cancelled_requests.discard(task)

    def _cancel_request(self, task: asyncio.Task) -> None:
        # Caller cancellation and app shutdown can overlap. A second cancel
        # would interrupt the transport's response cleanup and release its C5
        # lease early. One cancellation per owned worker; all callers join it.
        if not task.done() and task not in self._cancelled_requests:
            self._cancelled_requests.add(task)
            task.cancel()

    async def create_project(self, profile_id: str, *, expected_revision: int,
                             id: str, display_name: str, operation_id: str) -> CreateResult:
        return await self._request(profile_id, operation_id,
                                   (profile_id, expected_revision, id, display_name), observe=False)

    async def recover_create(self, profile_id: str, operation_id: str, *,
                             expected_revision: int) -> CreateResult:
        return await self._request(profile_id, operation_id,
                                   (profile_id, expected_revision), observe=True)

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True  # Synchronous admission fence before any await.
            requests = tuple(self._requests)
            for task in requests:
                self._cancel_request(task)
            self._close_task = asyncio.create_task(self._shutdown(requests))
        await _join(self._close_task)

    async def _shutdown(self, requests: tuple[asyncio.Task, ...]) -> None:
        await asyncio.gather(*requests, return_exceptions=True)
        self._requests.clear()
        self._services.clear()  # Release live keys only AFTER request cleanup.
        self._routes.clear()
        self._transport = None
