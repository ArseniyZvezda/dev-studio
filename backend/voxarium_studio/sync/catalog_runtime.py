"""Application-owned Remote composition (dev spec §5.2.1).

Implements C1/C4 of remote-identity/catalog-api-activation-plan §§2–4: explicitly
start one owner per application lifespan, then pass its ready dependencies to
the existing router. This module neither mounts routes nor chooses a project.
The S2 readers still use the existing user-config scope; live locators fence
that scope against the home pinned at start, rather than redirecting globals.

C4 permits explicit HTTP-only Verify, with one owned pool and fresh request
bindings; no verification history or persistent activation flag is maintained.
S2 fences are bounded observations, not a multiprocess lease or ABA proof.
C6 adds only S2 create/rename with exact-row, secret-free response observation.
C5/I1 adds app-owned consumer/admission fencing and private binding reads. Gate L
(positive legacy completeness) comes only from the user's durable confirmation of
the exact URL (dev spec §16.11); without it sensitive mutations stay closed.
C7 exposes explicit S2 credential/URL/delete/recovery commands. Recovery alone
may inspect a regular pending journal; ordinary reads/admission stay fenced.
C8 owns lazy durable Create routing/services; neither startup nor metadata GET
opens the intent journal.

I9 (runtime-integration-plan) releases the former R3 gate in this same owner:
strict-v2 catalog reads (list/exact get/snapshot/legacy resolve) and Create use
the accepted S3 services over the one owned pool. Every read is an owned task
holding a C5 ``read`` lease through each await/page/cleanup, with a fresh
revision/URL-bound provider; close cancels and joins it. A v1 Remote still
verifies only as a diagnostic: catalog services never send v1 requests.
"""
from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar

from pydantic import SecretStr

from ..core import application_config, paths
from ..core.application_config import RemoteServerProfile
from . import bindings, credentials
from .catalog import CatalogError, CatalogResponse
from .catalog_create_runtime import CatalogCreateRuntime
from .catalog_http import CatalogHttpTransport
from .catalog_metadata import CatalogMetadataObserver
from .catalog_providers import PendingCatalogProviders, S2CatalogReaders
from .catalog_read_service import CatalogReadService
from .catalog_service import CatalogService
from .catalog_verification import CatalogVerificationService
from .profiles import ProfileConsumers, ProfileError, RemoteProfiles
from .consumer_confirmations import FILE_NAME as CONFIRMATIONS_FILE, ConsumerConfirmations
from .profile_lifecycle import ConsumerCensus, ProfileLifecycle
from . import profiles as profile_store

if TYPE_CHECKING:
    from ..api.remote import ServerProfile
    from ..api.remote_metadata import CatalogMetadataAdapter
    from .catalog import CatalogPage, CatalogProject
    from .catalog_reads import LegacyResolution, ProjectSnapshot
    from .catalog_verification import VerificationStatus


T = TypeVar("T")
# Select reads one complete, consistent snapshot only for its exact identity
# (server-design §3.3–3.4). Wide pages; still bounded by the reader's bytes.
SELECT_SNAPSHOT_LIMIT = 100
SELECT_SNAPSHOT_MAX_PAGES = 1000
# Fixed local vocabulary for owner-side ProfileError refusals; anything else
# is a store failure, never a raw message (studio-design §5.1).
_PROFILE_CODES = frozenset({
    "profile_not_found", "profile_revision_conflict", "profile_recovery_required",
    "invalid_catalog_readers", "consumer_lifecycle_unavailable",
    "application_store_unavailable", "invalid_profile_metadata",
})


def _profile_code(error: ProfileError) -> str:
    if (type(error) is ProfileError and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in _PROFILE_CODES):
        return error.args[0]
    return "profile_store_unavailable"


@dataclass(frozen=True, repr=False)
class _MetadataProvider:
    owner: RemoteCatalogRuntime
    adapter: CatalogMetadataAdapter

    def list(self) -> tuple[ServerProfile, ...]:
        self.owner._admit()
        result = self.adapter.list()
        self.owner._admit()
        return result


@dataclass(frozen=True, repr=False)
class _ProfileProvider:
    owner: RemoteCatalogRuntime

    def get(self, profile_id: str) -> RemoteServerProfile:
        return self.owner._observe_profile(profile_id)


@dataclass(frozen=True, repr=False)
class _ProfileMutations:
    owner: RemoteCatalogRuntime

    def create(self, name: str, url: str) -> ServerProfile:
        self.owner._check()
        assert self.owner._repo is not None
        committed = self.owner._repo.create(name, url)
        return self.owner._committed_profile(committed)

    def rename(self, profile_id: str, name: str, *, expected_revision: int) -> ServerProfile:
        self.owner._check()
        assert self.owner._repo is not None
        committed = self.owner._repo.rename(profile_id, name, expected_revision=expected_revision)
        return self.owner._committed_profile(committed)

    def set_credential(self, profile_id: str, token: SecretStr, *,
                       expected_revision: int) -> ServerProfile:
        try:
            result = self.owner._mutate_profile(
                "set_credential", profile_id, expected_revision=expected_revision, token=token,
            )
        finally:
            token = None
        assert result is not None
        return result

    def clear_credential(self, profile_id: str, *, expected_revision: int) -> ServerProfile:
        result = self.owner._mutate_profile(
            "clear_credential", profile_id, expected_revision=expected_revision,
        )
        assert result is not None
        return result

    def change_url(self, profile_id: str, url: str, *, expected_revision: int,
                   clear_credential: bool) -> ServerProfile:
        result = self.owner._mutate_profile(
            "change_url", profile_id, expected_revision=expected_revision,
            url=url, clear_credential=clear_credential,
        )
        assert result is not None
        return result

    def delete(self, profile_id: str, *, expected_revision: int) -> None:
        self.owner._mutate_profile("delete", profile_id, expected_revision=expected_revision)

    def recover(self, profile_id: str, *, expected_revision: int) -> ServerProfile | None:
        return self.owner._mutate_profile("recover", profile_id, expected_revision=expected_revision)


@dataclass(frozen=True, repr=False)
class _OwnedTransport:
    """The owner's single pool, resolved at each send; never a second client (I9).

    Create is composed before the pool opens (the pool stays the last fallible
    startup action), so it holds this handle instead of the pool itself.
    Admission is rechecked at the send boundary: once close fences the owner no
    new request leaves, while an admitted response keeps its own cleanup.
    """

    owner: RemoteCatalogRuntime

    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      params: Mapping[str, str | int], json: Mapping[str, str] | None,
                      follow_redirects: bool) -> CatalogResponse:
        self.owner._admit()
        transport = self.owner._transport
        if transport is None:
            raise CatalogError("service_unavailable")
        return await transport.request(method, url, headers=headers, params=params,
                                       json=json, follow_redirects=follow_redirects)


@dataclass(frozen=True, repr=False)
class _ReadServices:
    """Fresh accepted S3 services bound to ONE provider, for one owned read."""

    catalog: CatalogService
    reads: CatalogReadService


@dataclass(frozen=True, repr=False)
class _CatalogProvider:
    """I9 catalog facade: each call is one owned, leased, freshly bound read.

    Callers supply only a profile ID, its expected revision and an exact
    project/legacy selector. URL, key and transport are never inputs, and no
    result, verification or provider is cached between calls.
    """

    owner: RemoteCatalogRuntime

    async def list_projects(self, profile_id: str, *, expected_revision: int,
                            after: str | None = None, limit: int = 20) -> CatalogPage:
        return await self.owner._read(profile_id, expected_revision, lambda s: s.catalog.list_projects(
            profile_id, expected_revision=expected_revision, limit=limit, after=after,
        ))

    async def get_project(self, profile_id: str, id: str, *,
                          expected_revision: int) -> CatalogProject:
        return await self.owner._read(profile_id, expected_revision, lambda s: s.catalog.get_project(
            profile_id, id, expected_revision=expected_revision,
        ))

    async def snapshot(self, profile_id: str, id: str, *, expected_revision: int,
                       expected_epoch: str | None = None, limit: int = 20,
                       max_pages: int = 100, max_bytes: int = 64 * 1024 * 1024) -> ProjectSnapshot:
        return await self.owner._read(profile_id, expected_revision, lambda s: s.reads.snapshot(
            profile_id, id, expected_revision=expected_revision, expected_epoch=expected_epoch,
            limit=limit, max_pages=max_pages, max_bytes=max_bytes,
        ))

    async def resolve_legacy(self, profile_id: str, name: str, *,
                             expected_revision: int) -> LegacyResolution:
        return await self.owner._read(profile_id, expected_revision, lambda s: s.reads.resolve_legacy(
            profile_id, name, expected_revision=expected_revision,
        ))

    async def select_observation(self, profile_id: str, id: str, *,
                                 expected_revision: int) -> dict[str, object]:
        """Exact Select evidence: id, storage_epoch (= space_instance_id), seq.

        server-design §3.4: the incarnation is the wire storage_epoch 1:1, read
        with the seq of one complete consistent snapshot. Document contents are
        dropped inside the owned worker; this observation is not a lock.
        """
        async def observe(services: _ReadServices) -> dict[str, object]:
            snapshot = await services.reads.snapshot(
                profile_id, id, expected_revision=expected_revision,
                limit=SELECT_SNAPSHOT_LIMIT, max_pages=SELECT_SNAPSHOT_MAX_PAGES,
            )
            return {"persistent_id": snapshot.id, "space_instance_id": snapshot.space_instance_id,
                    "snapshot_seq": snapshot.seq}

        return await self.owner._read(profile_id, expected_revision, observe)


@dataclass(frozen=True, repr=False)
class _VerificationProvider:
    owner: RemoteCatalogRuntime

    async def verify(self, profile_id: str, *, expected_revision: int) -> VerificationStatus:
        return await self.owner._verify(profile_id, expected_revision=expected_revision)


async def _settle(task: asyncio.Task):
    """Join owned cleanup even under repeated cancellation, then propagate it.

    Shield alone leaves a detached cleanup task when its waiter is cancelled.
    This loop retains ownership until completion and consumes any task error.
    """
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


@dataclass(frozen=True, repr=False)
class RemoteCatalogReady:
    """Immutable dependencies for one ready owner; retained handles are gated."""

    metadata: _MetadataProvider = field(repr=False)
    profiles: _ProfileProvider = field(repr=False)
    verification: _VerificationProvider = field(repr=False)
    catalog: _CatalogProvider = field(repr=False)
    mutations: _ProfileMutations = field(repr=False)
    create: CatalogCreateRuntime = field(repr=False)


class _PinnedConfirmations(ConsumerConfirmations):
    """Gate L store at the home pinned on start; drift/links are unavailable, never empty."""

    def __init__(self, runtime: RemoteCatalogRuntime) -> None:
        super().__init__()
        self._runtime = runtime

    def path(self) -> Path:
        self._runtime._locating()
        home = self._runtime._home
        if home is None:
            raise ProfileError("profile_recovery_required")
        path = home / CONFIRMATIONS_FILE
        try:
            mode = home.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(mode):
                raise ProfileError("profile_recovery_required")
        return path


class RemoteCatalogRuntime:
    """Explicit lifecycle holder, independent of app.state.ctx and project tabs.

    Construction/factory does no I/O. ``start`` pins the current absolute home
    and checks its real live locators/pending fence, without reading metadata or
    opening recovery journals. Metadata failures remain request-time failures.
    Start is idempotent while ready. Close (also before start) and failed start
    are terminal: a new lifespan needs a new factory, never a half-ready owner.
    No lock is held across an await. Close fences admission synchronously, then
    cancels/joins owned requests before closing the pool, even if cancelled.
    """

    def __init__(self) -> None:
        self._state = "new"
        self.lifecycle = ProfileLifecycle(profile_reader=self._get, binding_reader=self._read_bindings,
                                          confirmations=_PinnedConfirmations(self))
        self._home: Path | None = None
        self._repo: RemoteProfiles | None = None
        self._readers: S2CatalogReaders | None = None
        self._ready: RemoteCatalogReady | None = None
        self._transport: CatalogHttpTransport | None = None
        self._requests: set[asyncio.Task] = set()
        self._cancelled_requests: set[asyncio.Task] = set()
        self._close_task: asyncio.Task | None = None
        self._create: CatalogCreateRuntime | None = None
        self._outbound = _OwnedTransport(self)

    @property
    def ready(self) -> RemoteCatalogReady:
        self._admit()
        assert self._ready is not None
        return self._ready

    def _admit(self) -> None:
        if self._state != "ready":
            raise CatalogError("service_unavailable")

    def _locating(self) -> None:
        if self._state not in {"starting", "ready"}:
            raise CatalogError("service_unavailable")
        # Do not resolve relative/ambiguous homes, change cwd, create a home or
        # follow a new home when ENV changes. S2.check also rejects home links.
        if paths.user_config_dir() != self._home:
            raise ProfileError("profile_recovery_required")

    def _application_file(self) -> Path:
        self._locating()
        actual = application_config.application_config_file()
        # RemoteProfiles imports this locator too (not only the loader). Fence
        # both actual references so a drift cannot hide behind a pinned lambda.
        if profile_store.application_config_file() != actual:
            raise ProfileError("profile_recovery_required")
        return actual

    def _credential_file(self) -> Path:
        self._locating()
        return credentials.keys_file()

    def _journal_file(self) -> Path:
        self._locating()
        assert self._repo is not None
        return self._repo._journal()

    def _check(self) -> None:
        self._admit()
        assert self._readers is not None
        self._readers.check()

    def _read_bindings(self) -> bindings.BindingSnapshot:
        """Read-only I1 inventory at the pinned personal home (design §4.3).

        Missing is an observation, not legacy completeness. Like the S2 readers,
        reject locator drift/links, including a change during observation. This
        does not acquire a transaction or inspect credentials/managed roots.
        """
        failed = False
        result = None
        try:
            def check() -> None:
                self._locating()
                assert self._home is not None
                path = bindings.bindings_file()
                if path != self._home / "remote-bindings.json":
                    raise ValueError
                try:
                    mode = path.lstat().st_mode
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(mode):
                        raise ValueError

            check()
            result = bindings.RemoteBindings().read()
            check()
        except Exception:
            failed = True
        if failed:
            raise ProfileError("consumer_lifecycle_unavailable")
        assert result is not None
        return result

    def _check_mutation_locations(self, *, allow_pending: bool = False) -> None:
        """C7 mutation paths; pending is permitted ONLY for explicit recovery.

        Keep S2CatalogReaders.check's exact live-home/location and home-lstat
        safety. Stores must be absent or regular files: a dangling link is NOT
        proven absent metadata/credentials. Recovery's sole relaxation is a
        regular journal file: never a home/store/journal symlink or directory. This
        does not authorize token lookup, normal admission, startup or GET.
        """
        self._admit()
        failed = False
        try:
            self._locating()
            assert self._home is not None
            locations = (self._application_file(), self._credential_file(), self._journal_file())
            expected = tuple(self._home / name for name in (
                "config.yaml", "remote-keys.json", "remote-profile-change.json",
            ))
            if locations != expected:
                failed = True
            else:
                try:
                    mode = self._home.lstat().st_mode
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISDIR(mode):
                        failed = True
                for location in expected[:2]:
                    try:
                        mode = location.lstat().st_mode
                    except FileNotFoundError:
                        pass
                    else:
                        if not stat.S_ISREG(mode):
                            failed = True
                try:
                    mode = expected[2].lstat().st_mode
                except FileNotFoundError:
                    pass  # S2 recover will report the absent journal, not null.
                else:
                    if not allow_pending or not stat.S_ISREG(mode):
                        failed = True
        except Exception:
            failed = True
        if failed:
            raise ProfileError("profile_recovery_required")

    def _recovery_urls(self, profile_id: str) -> tuple[str, ...]:
        """Nonsecret evidence for the pending-tolerant lifecycle guard.

        S2 still owns recovery, CAS, writes and journal removal. Both journal
        URLs and the current metadata URL are consumers, even after a partial
        URL publication. A deleted profile needs no invented metadata reader.
        These are bounded observations, not a multiprocess recovery lease.
        """
        def strict_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("invalid_recovery_journal")
                result[key] = value
            return result

        self._check_mutation_locations(allow_pending=True)
        code = None
        pending = current = None
        urls = ()
        try:
            assert self._repo is not None
            credentials._profile_id(profile_id)
            pending = json.loads(
                self._journal_file().read_text(encoding="utf-8"), object_pairs_hook=strict_object,
            )
            if (not isinstance(pending, dict)
                    or set(pending) != {"version", "profile_id", "expected_revision", "url", "target_url"}
                    or type(pending["version"]) is not int or pending["version"] != 1
                    or pending["profile_id"] != profile_id
                    or type(pending["expected_revision"]) is not int or pending["expected_revision"] < 1
                    or not isinstance(pending["url"], str)
                    or not isinstance(pending["target_url"], str)
                    or credentials._url(pending["url"]) != pending["url"]
                    or credentials._url(pending["target_url"]) != pending["target_url"]):
                raise ValueError("invalid_recovery_journal")
            # Deliberately not _get: its normal pending fence is still required
            # by lifecycle.operation/attach, and must not be weakened globally.
            try:
                current = self._repo.get(profile_id)
            except ProfileError as error:
                if error.args != ("profile_not_found",):
                    raise
            urls = tuple(dict.fromkeys((pending["url"], pending["target_url"],
                                        *((current.url,) if current is not None else ()))))
        except ProfileError as error:
            code = ("application_store_unavailable" if error.args == ("application_store_unavailable",)
                    else "profile_recovery_unavailable")
        except Exception:
            code = "profile_recovery_unavailable"
        finally:
            # A corrupt journal/config may contain arbitrary private strings.
            # Do not retain its values in the sanitized exception traceback.
            pending = current = None
        self._check_mutation_locations(allow_pending=True)  # Fence failed observations too.
        if code is not None:
            raise ProfileError(code)
        return urls

    @contextmanager
    def _mutation_consumer_guard(self, profile_id: str) -> Iterator[ProfileConsumers]:
        with self.lifecycle.consumer_guard(profile_id) as state:
            # Inventory callbacks may observe/change local paths. Revalidate
            # after their observation, still under lifecycle and before S2's
            # config/credential transactions, just like explicit recovery.
            self._check_mutation_locations()

            def recheck() -> None:
                self._check_mutation_locations()
                if state.recheck is not None:
                    state.recheck()

            yield replace(state, recheck=recheck)

    @contextmanager
    def _recovery_consumer_guard(self, profile_id: str, *,
                                 urls: tuple[str, ...]) -> Iterator[ProfileConsumers]:
        with self.lifecycle.recovery_guard(profile_id, urls=urls) as state:
            # Inventory observation itself may reveal drift. Recheck under the
            # shared lifecycle lock, before S2 opens its application transaction;
            # never let newly observed journal URLs escape the frozen guard.
            if self._recovery_urls(profile_id) != urls:
                raise ProfileError("profile_revision_conflict")

            def recheck() -> None:
                # A pending journal/current URL may have changed while waiting
                # for S2's config transaction. Recheck the union there too, before
                # credential observation; do NOT open a nested transaction.
                if self._recovery_urls(profile_id) != urls:
                    raise ProfileError("profile_revision_conflict")
                if state.recheck is not None:
                    state.recheck()

            yield replace(state, recheck=recheck)

    def _mutate_profile(
        self, operation: Literal["set_credential", "clear_credential", "change_url", "delete", "recover"],
        profile_id: str, *, expected_revision: int, token: SecretStr | None = None,
        url: str | None = None, clear_credential: bool = True,
    ) -> ServerProfile | None:
        """Fixed, secret-free S2 boundary (studio-design §5.1, activation plan C7).

        The original repository keeps its ordinary authoritative consumer guard.
        Only this explicit recovery gets a separate repository with the same
        lifecycle owner/lock and validated old/target/current URL evidence.
        """
        committed = None
        code = None
        catalog_error = False
        try:
            if operation == "recover":
                urls = self._recovery_urls(profile_id)
                repo = RemoteProfiles(consumer_guard=lambda requested: self._recovery_consumer_guard(
                    requested, urls=urls,
                ))
                committed = repo.recover(profile_id, expected_revision=expected_revision)
            else:
                self._check_mutation_locations()
                self._check()
                assert self._repo is not None
                # A proven missing row is a local 404, not an unknown lifecycle
                # inventory. This observation grants no mutation permission:
                # S2 still repeats metadata/CAS inside its authoritative guard.
                self._repo.get(profile_id)
                if operation == "set_credential":
                    if not isinstance(token, SecretStr):
                        raise CatalogError("invalid_request")
                    # The only raw unwrap is the actual S2 call argument. No
                    # raw token lives in this frame, kwargs, lambda or DTO.
                    committed = self._repo.set_credential(
                        profile_id, token.get_secret_value(), expected_revision=expected_revision,
                    )
                elif operation == "clear_credential":
                    committed = self._repo.clear_credential(profile_id, expected_revision=expected_revision)
                elif operation == "change_url":
                    committed = self._repo.change_url(
                        profile_id, url, expected_revision=expected_revision,
                        clear_credential=clear_credential,
                    )
                elif operation == "delete":
                    self._repo.delete(profile_id, expected_revision=expected_revision)
                else:
                    raise CatalogError("invalid_request")
        except ProfileError as error:
            code = "profile_store_unavailable"
            if (type(error) is ProfileError and len(error.args) == 1
                    and type(error.args[0]) is str and error.args[0] in {
                        "profile_not_found", "profile_revision_conflict", "profile_url_collision",
                        "profile_in_use", "profile_consumers_not_drained", "consumer_lifecycle_unavailable",
                        "consumer_inventory_changed", "consumer_confirmation_unavailable",
                        "credential_clear_confirmation_required", "credential_required",
                        "invalid_profile_metadata", "invalid_server_url", "profile_recovery_required",
                        "profile_recovery_unavailable", "application_store_unavailable",
                    }):
                code = error.args[0]
        except credentials.CredentialsError as error:
            code = ("credential_binding_conflict" if type(error) is credentials.CredentialConflict
                    else "credential_store_unavailable")
        except CatalogError as error:
            catalog_error = True
            code = (error.code if type(error) is CatalogError and type(error.code) is str
                    and error.code in {"service_unavailable", "invalid_request"} else "service_unavailable")
        except Exception:
            code = "profile_store_unavailable"
        finally:
            token = None
        # Raising outside *all* handlers drops S2's raw credential/disk exception
        # chains (including errors which used `raise ... from None` internally).
        if code is not None:
            if catalog_error:
                raise CatalogError(code)
            raise ProfileError(code)
        if operation not in {"delete", "recover"} or committed is not None:
            return self._committed_profile(committed)
        failed = False
        try:
            self._check()
        except Exception:
            failed = True
        if failed:
            raise CatalogError("profile_committed_metadata_unavailable")
        # Only an actual successful S2 recover returning None proves absent
        # metadata. No failed read, missing journal or exception becomes null.
        return None

    # --- Gate L facade (dev spec §16.11) ------------------------------------------

    def consumer_census(self, profile_id: str) -> ConsumerCensus:
        """Read-only: known bindings for the exact profile URL; never grants consent."""
        self._admit()
        return self.lifecycle.census(profile_id)

    def confirm_consumers(self, profile_id: str, *, expected_revision: int, url: str,
                          bound_roots: tuple[str, ...]) -> ConsumerCensus:
        """The user's explicit confirmation of what they were shown; nothing else."""
        self._check_mutation_locations()
        return self.lifecycle.confirm(profile_id, expected_revision=expected_revision,
                                      url=url, bound_roots=bound_roots)

    def withdraw_consumers(self, profile_id: str, *, expected_revision: int) -> ConsumerCensus:
        self._check_mutation_locations()
        return self.lifecycle.withdraw(profile_id, expected_revision=expected_revision)

    def _list(self) -> tuple[RemoteServerProfile, ...]:
        self._check()
        assert self._repo is not None
        return self._repo.list()

    def _get(self, profile_id: str) -> RemoteServerProfile:
        self._check()
        assert self._repo is not None
        return self._repo.get(profile_id)

    def _source(self, profile_id: str, url: str) -> str:
        self._check()
        return credentials.profile_token_source(profile_id, url)

    def _token(self, profile_id: str, url: str) -> str | None:
        # Only explicit request-bound providers expose this read-only callback;
        # metadata and the catalog gate never call it. No S2 mutation lock.
        self._check()
        return credentials.load_profile_token(profile_id, url)

    def _observe_profile(self, profile_id: str) -> RemoteServerProfile:
        """Pending-safe initial binding for the existing GET projects router."""
        self._check()
        result = None
        code = None
        try:
            result = self._get(profile_id)
        except ProfileError as error:
            code = ("profile_not_found" if error.args == ("profile_not_found",)
                    else "application_store_unavailable")
        except Exception:
            code = "application_store_unavailable"
        self._check()  # Fence failed reads too; no raw exception chain survives.
        if code:
            raise ProfileError(code)
        assert result is not None
        return result

    def _committed_profile(self, committed: RemoteServerProfile) -> ServerProfile:
        """C6 response, NOT a second mutation or a list-after-write success gate.

        Reuse the metadata owner's exact-binding/pending/presence fences for
        only the row S2 returned after publication. Unrelated profiles cannot
        mask success. These remain bounded observations, not an atomic lease.
        A failed observation is explicitly *post-commit*, never a rollback or
        permission to repeat POST (dev spec §5.2.1, activation plan C6).
        """
        failed = False
        result = None
        try:
            self._admit()
            assert self._ready is not None
            observer = self._ready.metadata.adapter.observer
            binding = observer._binding(committed)
            source = observer._source(binding)
            if observer._source(binding) != source:
                raise ProfileError("metadata_unavailable")
            observer._check()
            from .catalog_metadata import CatalogMetadataRow
            from ..api.remote_metadata import _profiles

            row = CatalogMetadataRow(
                binding.profile_id, binding.name, binding.url, binding.revision,
                None if source == "environment_unbound" else source != "missing",
                "none" if source == "missing" else source,
            )
            result = _profiles((row,))[0]
            self._admit()
        except Exception:
            failed = True
        # No reader error or secret-bearing validation context escapes. The
        # typed code honestly distinguishes publication from response failure.
        if failed:
            raise CatalogError("profile_committed_metadata_unavailable")
        assert result is not None
        return result

    def providers_for(self, profile_id: str, *, expected_revision: int) -> PendingCatalogProviders:
        """Fresh immutable URL/revision binding; no mutable last-profile slot.

        Pass this as profiles and its load_token as credentials to the existing
        guarded S3 services, synchronously capturing it before awaits.
        Keeping a provider after close cannot authorize another read.
        """
        profile = self._observe_profile(profile_id)
        assert self._readers is not None
        provider = PendingCatalogProviders(
            self._readers, profile_id=profile_id,
            expected_url=profile.url, expected_revision=expected_revision,
        )
        provider.get(profile_id)  # CAS and URL fence before publishing the handle.
        self._admit()
        return provider

    async def _verify(self, profile_id: str, *, expected_revision: int) -> VerificationStatus:
        self._admit()
        # Logical lease only: S2 publication cannot race an admitted request,
        # but the event/ACK loop never waits on a thread lock across this await.
        with self.lifecycle.operation(profile_id, kind="verify"):
            return await self._verify_admitted(profile_id, expected_revision=expected_revision)

    async def _verify_admitted(self, profile_id: str, *, expected_revision: int) -> VerificationStatus:
        self._admit()
        # Capture exact profile/URL/revision before the first await. There is no
        # selected-project or shared last-profile binding, and no status cache.
        provider = self.providers_for(profile_id, expected_revision=expected_revision)
        assert self._transport is not None
        service = CatalogVerificationService(
            profiles=provider, credentials=provider.load_token,
            transport=self._transport, clock=lambda: datetime.now(timezone.utc),
        )
        operation = asyncio.create_task(service.verify(
            profile_id, expected_revision=expected_revision,
        ))
        self._requests.add(operation)
        operation.add_done_callback(self._finished_request)
        cancelled = False
        code = None
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancelled = True
        except CatalogError as error:
            code = error.code
        except Exception:
            code = "verification_failed"
        if cancelled:
            self._cancel_request(operation)
            try:
                await _settle(operation)
            except BaseException:
                pass  # Consume cleanup outcome, never its raw cancellation text.
            raise asyncio.CancelledError() from None
        # A shutdown-induced locator refusal is not damaged S2 storage (409).
        # Retained request handles must report lifecycle unavailability instead.
        self._admit()
        if code is not None:
            raise CatalogError(code)
        return result

    def _finished_request(self, task: asyncio.Task) -> None:
        self._requests.discard(task)
        self._cancelled_requests.discard(task)

    def _cancel_request(self, task: asyncio.Task) -> None:
        # Caller cancellation and close can overlap. A second cancel would
        # interrupt the transport's response cleanup (and, for reads, release
        # the C5 lease early). One cancellation per owned worker; all join it.
        if not task.done() and task not in self._cancelled_requests:
            self._cancelled_requests.add(task)
            task.cancel()

    async def _read(self, profile_id: str, expected_revision: int,
                    operation: Callable[[_ReadServices], Awaitable[T]]) -> T:
        """One owned catalog read (I9), shaped like C8's owned Create worker.

        The worker owns the C5 logical lease from admission through provider
        capture, every service await/page and its cancellation cleanup. Close
        cancels it once and joins it before closing the pool. A failure, close
        or cancellation never returns a partial result or a raw exception.
        """
        self._admit()
        task = asyncio.create_task(self._read_admitted(profile_id, expected_revision, operation))
        self._requests.add(task)
        task.add_done_callback(self._finished_request)
        cancelled = False
        result = None
        code = None
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                cancelled = True
            else:
                code = "service_unavailable"  # The owner cancelled its worker (close).
        except CatalogError as error:
            code = error.code if type(error) is CatalogError and type(error.code) is str else "catalog_unavailable"
        except Exception:
            code = "service_unavailable"
        if cancelled:
            self._cancel_request(task)
            try:
                await _settle(task)
            except BaseException:
                pass  # Consume cleanup outcome, never its raw cancellation text.
            raise asyncio.CancelledError() from None
        # After guard for retained handles: a closed owner publishes nothing.
        self._admit()
        if code is not None:
            raise CatalogError(code)
        return result

    async def _read_admitted(self, profile_id: str, expected_revision: int,
                             operation: Callable[[_ReadServices], Awaitable[T]]) -> T:
        self._admit()
        lease = None
        code = None
        try:
            # Admission BEFORE binding/token capture (profile_lifecycle.operation).
            lease = self.lifecycle.operation(profile_id, kind="read")
        except ProfileError as error:
            code = _profile_code(error)
        except Exception:
            code = "service_unavailable"
        if code is not None:
            raise CatalogError(code)
        with lease:
            provider = None
            try:
                # Before guard: exact profile/URL/revision CAS, no key lookup yet.
                provider = self.providers_for(profile_id, expected_revision=expected_revision)
            except ProfileError as error:
                code = _profile_code(error)
            except CatalogError as error:
                code = error.code if type(error.code) is str else "service_unavailable"
            except Exception:
                code = "profile_store_unavailable"
            if code is not None:
                raise CatalogError(code)
            services = _ReadServices(
                catalog=CatalogService(profiles=provider, credentials=provider.load_token,
                                       transport=self._outbound),
                reads=CatalogReadService(profiles=provider, credentials=provider.load_token,
                                         transport=self._outbound),
            )
            # The S3 guards fence both sides of every send/page and re-read the
            # live key after the operation (after guard), still inside the lease.
            result = await operation(services)
            self._admit()
            return result

    async def start(self) -> RemoteCatalogReady:
        if self._state == "ready":
            self._check()
            return self.ready
        if self._state != "new":
            raise CatalogError("service_unavailable")
        self._state = "starting"
        code = None
        try:
            home = paths.user_config_dir()
            if (not isinstance(home, Path) or not home.is_absolute()
                    or ".." in home.parts):
                raise ProfileError("invalid_catalog_readers")
            self._home = home
            self._repo = RemoteProfiles(consumer_guard=self._mutation_consumer_guard)
            readers = S2CatalogReaders(
                home, self._get, self._token, self._application_file,
                self._credential_file, self._journal_file,
            )
            readers.check()
            observer = CatalogMetadataObserver(
                home=home, list_reader=self._list, profile_reader=self._get,
                source_reader=self._source, application_file=self._application_file,
                credential_file=self._credential_file, journal_file=self._journal_file,
            )
            # Keep the API adapter import out of module/factory initialization.
            from ..api.remote_metadata import CatalogMetadataAdapter

            adapter = CatalogMetadataAdapter(observer=observer)
            self._readers = readers
            # One app-owned registry, not a request/project singleton. No
            # journal I/O here. I9: Create sends through the owner's pool handle
            # (admission-checked per send), never a pool of its own.
            creation = CatalogCreateRuntime(
                home=home, check=self._check, providers=self.providers_for,
                lifecycle=self.lifecycle, transport=self._outbound,
            )
            ready = RemoteCatalogReady(
                _MetadataProvider(self, adapter), _ProfileProvider(self),
                _VerificationProvider(self), _CatalogProvider(self), _ProfileMutations(self),
                creation,
            )
            readers.check()  # No usable owner until composition fully succeeds.
            # Last fallible startup action: no opened pool can be lost by a
            # later observer failure. Factory/constructor still perform no I/O.
            self._transport = CatalogHttpTransport()
        except BaseException as error:
            self.lifecycle.close_admission()
            self._state = "failed"
            self._ready = None
            self._readers = None
            self._repo = None
            self._home = None
            if not isinstance(error, Exception):
                raise
            code = "service_unavailable"
            if (type(error) is ProfileError and len(error.args) == 1
                    and type(error.args[0]) is str and error.args[0] in {
                        "invalid_catalog_readers", "profile_recovery_required",
                    }):
                code = error.args[0]
        if code:
            if code == "service_unavailable":
                raise CatalogError(code)
            raise ProfileError(code)
        self._ready = ready
        self._create = creation
        self._state = "ready"
        return self.ready

    async def close(self) -> None:
        if self._close_task is None:
            # No suspension until admission is closed and every worker captured.
            self.lifecycle.close_admission()
            self._state = "closing"
            self._ready = None
            requests = tuple(self._requests)
            for request in requests:
                self._cancel_request(request)
            self._close_task = asyncio.create_task(self._shutdown(requests))
        await _settle(self._close_task)

    async def _shutdown(self, requests: tuple[asyncio.Task, ...]) -> None:
        failed = False
        try:
            # Cancel Create concurrently with Verify/read cleanup, not after a
            # possibly slow response close. Admission was already fenced.
            create_close = (() if self._create is None else (self._create.close(),))
            await asyncio.gather(*requests, *create_close, return_exceptions=True)
            if self._transport is not None:
                await self._transport.aclose()
        except Exception:
            failed = True
        finally:
            self._transport = None
            self._create = None
            self._requests.clear()
            self._cancelled_requests.clear()
            self._readers = None
            self._repo = None
            self._home = None
            self._state = "closed"
        if failed:
            raise CatalogError("service_unavailable")


def create_remote_catalog_runtime() -> RemoteCatalogRuntime:
    """Create an unstarted application owner, with no implicit I/O or singleton."""
    return RemoteCatalogRuntime()
