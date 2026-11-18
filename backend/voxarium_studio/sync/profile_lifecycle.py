"""Application-owned admission seam (dev spec §5.2.1, §16.11; Studio design S4/S5).

I1 reads private bindings, but is NOT a legacy disk census or drain implementation.
Without explicit legacy completeness (gate L), sensitive S2 mutations fail closed,
even with an empty private store or no open projects. Gate L's owner is the user:
only their durable, explicit confirmation of the exact URL (``consumer_confirmations``)
makes the inventory ``known``. Active consumers are never stopped or drained here;
they refuse the mutation until the user disables them.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Lock
from typing import Any

from ..core.application_config import RemoteServerProfile
from ..core.project_scope import canonical_root
from .bindings import BindingSnapshot, RemoteBindings
from .consumer_confirmations import ConsumerConfirmations
from .profiles import ProfileConsumers, ProfileError
from .protocol import normalise_url


@dataclass(frozen=True)
class BindingInventory:
    """Explicit coverage/drain evidence, separate from private store contents.

    ``known`` is an explicit completeness assertion, NOT inferred from empty
    tuples. ``drained`` concerns active consumers only; it never clears bindings.
    The reader must run synchronously, without network, drain or lock inversion.
    Production has no positive legacy completeness owner (gate L); the optional
    callback remains a test/future-owner seam, never inferred from recents or
    even a valid nonempty private store (runtime-integration-plan §2.1).
    """

    state: str = "unknown"  # known / unknown / recovering
    bound_roots: tuple[str, ...] = ()
    active_roots: tuple[str, ...] = ()
    drained: bool = False


def _display_root(root: str) -> str:
    if root.startswith("catalog:"):
        return root
    return str(canonical_root(root))


@dataclass(frozen=True)
class ConsumerCensus:
    """What the Studio itself knows about one profile's consumers (§16.11).

    ``state`` is ``known`` only with the user's confirmation of exactly ``url``.
    ``bound_roots`` are private bindings and resident contexts for the profile or
    URL; ``active_roots`` are attached runners/enabled legacy contexts;
    ``operations`` are in-flight catalog Verify/read/Create leases.
    """

    profile_id: str
    url: str
    revision: int
    state: str
    confirmed_at: str | None
    bound_roots: tuple[str, ...]
    active_roots: tuple[str, ...]
    operations: tuple[str, ...]


@dataclass(frozen=True)
class _Consumer:
    root: str
    url: str
    profile_id: str | None


class ConsumerLease:
    """Logical in-flight admission, not a lock held over an await.

    Release is queued, so ACK/cancellation cleanup never waits for a thread
    doing synchronous S2 publication. The next critical section reaps releases.
    """

    def __init__(self, owner: ProfileLifecycle, key: object) -> None:
        self._owner = owner
        self._key = key

    def release(self) -> None:
        self._owner._released.put(self._key)
        self._owner._reap_available()

    def __enter__(self) -> ConsumerLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class ProfileLifecycle:
    """One owner for ALL resident contexts and outbound operations in an app.

    Short sections use nonblocking admission: contention refuses rather than
    blocking the asyncio/ACK loop. The S2 guard holds this lock through journal,
    config and credential publication, in lifecycle → config → credentials order.
    Attach/read/Create callers retain only logical leases across awaits. No
    method stops a context, sync runner, editor, job or another project's work.
    """

    def __init__(
        self, *, profile_reader: Callable[[str], RemoteServerProfile],
        inventory: Callable[[str], BindingInventory] | None = None,
        binding_reader: Callable[[], BindingSnapshot] | None = None,
        confirmations: ConsumerConfirmations | None = None,
    ) -> None:
        self._profile_reader = profile_reader
        self._inventory = inventory
        # Production coverage owner (gate L). An explicit ``inventory`` callback
        # (tests/future owners) takes precedence; with neither, coverage is unknown.
        self._confirmations = confirmations
        self._binding_reader = binding_reader or RemoteBindings().read
        self._lock = Lock()
        self._closed = False
        self._contexts: dict[int, Any] = {}
        self._leases: dict[object, _Consumer] = {}
        self._released: SimpleQueue[object] = SimpleQueue()
        self._retired: SimpleQueue[tuple[int, Any]] = SimpleQueue()

    def _reap(self) -> None:
        while True:
            try:
                self._leases.pop(self._released.get_nowait(), None)
            except Empty:
                break

        while True:
            try:
                key, ctx = self._retired.get_nowait()
                if self._contexts.get(key) is ctx:
                    self._contexts.pop(key)
            except Empty:
                break

    def _reap_available(self) -> None:
        if self._lock.acquire(blocking=False):
            try:
                self._reap()
            finally:
                self._lock.release()

    @contextmanager
    def _critical(self) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise ProfileError("consumer_lifecycle_unavailable")
        try:
            self._reap()
            if self._closed:
                raise ProfileError("consumer_lifecycle_unavailable")
            yield
        finally:
            self._reap()
            self._lock.release()

    def close_admission(self) -> None:
        # Immediate fence; no thread wait, network wait or cancellation here.
        # A previously admitted synchronous publication may finish normally.
        self._closed = True

    def register_context(self, ctx: Any) -> None:
        """Before startup's first await; includes hidden and starting contexts."""
        with self._critical():
            self._contexts[id(ctx)] = ctx

    def unregister_context(self, ctx: Any) -> None:
        """After successful sync stop; cleanup must never wait OR be refused.

        Publication may be in another thread. Queue retirement just like lease
        release, rather than aborting watcher/job/database cleanup on contention.
        """
        self._retired.put((id(ctx), ctx))
        self._reap_available()

    @staticmethod
    def _url(value: str) -> str:
        try:
            return normalise_url(value)
        except (TypeError, ValueError):
            pass
        raise ProfileError("consumer_lifecycle_unavailable")

    def _profile(self, profile_id: str) -> RemoteServerProfile:
        # Read under lifecycle, before S2 takes its application transaction.
        # Errors/uncertain metadata are never mistaken for an unbound profile.
        code = "consumer_lifecycle_unavailable"
        try:
            profile = self._profile_reader(profile_id)
            if not isinstance(profile, RemoteServerProfile) or profile.profile_id != profile_id:
                raise ValueError
            return profile
        except ProfileError as error:
            if error.args == ("profile_not_found",) or error.args == ("profile_recovery_required",):
                code = error.args[0]
        except Exception:
            pass
        raise ProfileError(code)

    def attach(self, *, root: str, url: str, profile_id: str | None = None) -> ConsumerLease:
        """Fence initial attach AND reconnect before a runner can be published."""
        with self._critical():
            url = self._url(url)
            if profile_id is not None and self._profile(profile_id).url != url:
                raise ProfileError("profile_revision_conflict")
            return self._lease(_Consumer(str(Path(root).resolve()), url, profile_id))

    def operation(self, profile_id: str, *, kind: str) -> ConsumerLease:
        """Admission for Verify/read/Create, BEFORE capturing binding or token.

        Future catalog/Create composition must use this same seam for its whole
        operation (including cleanup), not just at the HTTP send boundary.
        """
        if kind not in {"verify", "read", "create"}:
            raise ProfileError("consumer_lifecycle_unavailable")
        with self._critical():
            profile = self._profile(profile_id)
            return self._lease(_Consumer("catalog:" + kind, profile.url, profile_id))

    def _lease(self, consumer: _Consumer) -> ConsumerLease:
        key = object()
        self._leases[key] = consumer
        return ConsumerLease(self, key)

    @contextmanager
    def consumer_guard(self, profile_id: str) -> Iterator[ProfileConsumers]:
        """S2's synchronous guard, frozen through its final publication.

        No private inventory is fabricated from the selected project, recently
        opened list or the (possibly empty) resident registry.
        """
        with self._consumer_guard(profile_id) as snapshot:
            yield snapshot

    @contextmanager
    def recovery_guard(self, profile_id: str, *, urls: tuple[str, ...]) -> Iterator[ProfileConsumers]:
        """Explicit S2 recovery only (studio-design §5.1), never read admission.

        The runtime supplies validated journal/current metadata URLs, including
        both sides of an interrupted URL change. Normal admission still refuses
        pending/missing metadata. The same lock, inventory and all resident/lease
        evidence fence the discard operation; no consumer is stopped or drained.
        """
        if (not isinstance(urls, tuple) or not urls
                or any(not isinstance(url, str) or self._url(url) != url for url in urls)):
            raise ProfileError("consumer_lifecycle_unavailable")
        with self._consumer_guard(profile_id, recovery_urls=urls) as snapshot:
            yield snapshot

    @contextmanager
    def _consumer_guard(self, profile_id: str, *,
                        recovery_urls: tuple[str, ...] | None = None) -> Iterator[ProfileConsumers]:
        with self._critical():
            urls = recovery_urls if recovery_urls is not None else (self._profile(profile_id).url,)
            observed = self._observe_inventory(profile_id, urls=urls,
                                               check_profile=recovery_urls is None)

            def recheck() -> None:
                # Called only by S2 under its ALREADY acquired config transaction.
                # Read never locks/writes. RemoteBindings.save's shared OS lock
                # now excludes publication until S2 finishes; do not nest it here.
                if self._observe_inventory(profile_id, urls=urls,
                                           check_profile=recovery_urls is None) != observed:
                    raise ProfileError("consumer_lifecycle_unavailable")

            snapshot = observed[2]
            yield ProfileConsumers(snapshot.active_roots, snapshot.bound_roots,
                                   snapshot.drained, recheck=recheck)

    def _coverage(self, profile_id: str, urls: tuple[str, ...]) -> BindingInventory:
        if self._inventory is not None:
            return self._inventory(profile_id)
        if self._confirmations is not None and self._confirmations.confirmed(urls):
            # The user confirmed these exact URLs: no other consumers exist beyond
            # what this process observes below. Local evidence still decides drain.
            return BindingInventory(state="known", drained=True)
        return BindingInventory()

    def _collect(self, profile_id: str, urls: tuple[str, ...], private: BindingSnapshot
                 ) -> tuple[set[str], set[str]]:
        """Private rows + resident contexts -> bound; enabled contexts + leases -> active."""
        bound: set[str] = set()
        # Both selected and confirmed are bindings; no root is opened or
        # stat'ed to decide whether its saved record may be forgotten.
        for record in private.records:
            if record.profile_id == profile_id or record.url in urls:
                bound.add(record.canonical_root)
        local_active: set[str] = set()
        for ctx in tuple(self._contexts.values()):
            # Starting/failed-cleanup contexts stay registered until stop.
            # Disabled legacy URLs remain bound. Observation errors fail closed.
            settings = ctx.config.sync
            if (getattr(settings, "profile_id", None) == profile_id
                    or settings.url and self._url(settings.url) in urls):
                root = str(Path(ctx.paths.root).resolve())
                bound.add(root)
                if settings.enabled:
                    local_active.add(root)
        for consumer in self._leases.values():
            if consumer.profile_id == profile_id or consumer.url in urls:
                local_active.add(consumer.root)
        return bound, local_active

    def _observe_inventory(self, profile_id: str, *, urls: tuple[str, ...], check_profile: bool
                           ) -> tuple[BindingInventory, str | None, ProfileConsumers]:
        """One C5 observation: coverage + private rows + residents/leases (I1).

        The URL union is supplied by the guard, including all recovery targets;
        the old profile-only coverage callback cannot hide private rows from it.
        A store fingerprint is whole-file CAS evidence, never completeness.
        """
        failed = False
        try:
            private = self._binding_reader()
            inventory = self._coverage(profile_id, urls)
            if (not isinstance(private, BindingSnapshot)
                    or not isinstance(inventory, BindingInventory) or inventory.state != "known"
                    or type(inventory.drained) is not bool
                    or not isinstance(inventory.bound_roots, tuple)
                    or not isinstance(inventory.active_roots, tuple)
                    or any(not isinstance(root, str) or not root
                           for root in (*inventory.bound_roots, *inventory.active_roots))):
                raise ValueError
            bound, local_active = self._collect(profile_id, urls, private)
            bound.update(inventory.bound_roots)
            active = set(inventory.active_roots)
            if check_profile and self._profile(profile_id).url != urls[0]:
                raise ValueError
            active.update(local_active)
            snapshot = ProfileConsumers(
                active_roots=tuple(sorted(active)), bound_roots=tuple(sorted(bound)),
                drained=inventory.drained and not local_active,
            )
        except Exception:
            failed = True
        if failed:
            raise ProfileError("consumer_lifecycle_unavailable")
        return inventory, private.fingerprint, snapshot

    # --- Gate L: the user's explicit confirmation (dev spec §16.11) -------------

    def _census(self, profile: RemoteServerProfile) -> ConsumerCensus:
        failed = False
        try:
            private = self._binding_reader()
            if not isinstance(private, BindingSnapshot):
                raise ValueError
            bound, active = self._collect(profile.profile_id, (profile.url,), private)
            # One spelling per working copy for the person reading the list: a
            # private row (canonical) and its resident context (resolved) coincide.
            bound = {_display_root(root) for root in bound}
            active = {_display_root(root) for root in active}
        except Exception:
            failed = True
        if failed:
            raise ProfileError("consumer_lifecycle_unavailable")
        confirmed_at = None
        if self._inventory is None and self._confirmations is not None:
            try:
                entry = self._confirmations.read().get(profile.url)
            except Exception:
                entry = None  # Missing or corrupt store: simply unknown.
            confirmed_at = entry.confirmed_at if entry is not None else None
        try:
            known = self._coverage(profile.profile_id, (profile.url,)).state == "known"
        except Exception:
            known = False
        operations = sorted({root.removeprefix("catalog:") for root in active
                             if root.startswith("catalog:")})
        return ConsumerCensus(
            profile_id=profile.profile_id, url=profile.url, revision=profile.revision,
            state="known" if known else "unknown",
            confirmed_at=confirmed_at, bound_roots=tuple(sorted(bound)),
            active_roots=tuple(sorted(r for r in active if not r.startswith("catalog:"))),
            operations=tuple(operations),
        )

    def census(self, profile_id: str) -> ConsumerCensus:
        """Read-only: what is shown to the user before they confirm."""
        with self._critical():
            return self._census(self._profile(profile_id))

    def _confirmation_owner(self) -> ConsumerConfirmations:
        if self._confirmations is None or self._inventory is not None:
            raise ProfileError("consumer_confirmation_unavailable")
        return self._confirmations

    def confirm(self, profile_id: str, *, expected_revision: int, url: str,
                bound_roots: tuple[str, ...]) -> ConsumerCensus:
        """Record the user's explicit confirmation of exactly what they were shown.

        A changed URL or bound set refuses (``consumer_inventory_changed``): the
        user must look at the new list. Nothing is stopped, unbound or connected.
        """
        owner = self._confirmation_owner()
        with self._critical():
            profile = self._profile(profile_id)
            if type(expected_revision) is not int or profile.revision != expected_revision:
                raise ProfileError("profile_revision_conflict")
            current = self._census(profile)
            if (not isinstance(url, str) or url != profile.url
                    or not isinstance(bound_roots, tuple)
                    or len(bound_roots) != len(current.bound_roots)
                    or tuple(sorted(bound_roots)) != current.bound_roots):
                raise ProfileError("consumer_inventory_changed")
            failed = False
            try:
                owner.confirm(profile.url, current.bound_roots)
            except Exception:
                failed = True
            if failed:
                raise ProfileError("consumer_confirmation_unavailable")
            return self._census(profile)

    def withdraw(self, profile_id: str, *, expected_revision: int) -> ConsumerCensus:
        """Forget the confirmation: later C7 mutations of this URL fail closed again."""
        owner = self._confirmation_owner()
        with self._critical():
            profile = self._profile(profile_id)
            if type(expected_revision) is not int or profile.revision != expected_revision:
                raise ProfileError("profile_revision_conflict")
            failed = False
            try:
                owner.withdraw(profile.url)
            except Exception:
                failed = True
            if failed:
                raise ProfileError("consumer_confirmation_unavailable")
            return self._census(profile)
