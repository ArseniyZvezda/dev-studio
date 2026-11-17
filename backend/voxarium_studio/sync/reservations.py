"""One local sync consumer per destination (Studio design §4.3/§8/S4).

The workspace owns this in-memory registry for one backend lifetime. It is not
an interprocess lease, binding inventory, consent check, or sync runner. Nothing
here reads credentials, persists state, drains work, or starts a connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from ..core.project_identity import ProjectIdentity
from ..core.project_scope import canonical_root
from .protocol import normalise_url


class ReservationError(ValueError):
    """Stable nonsecret code; only a collision exposes the canonical owner root."""

    def __init__(self, code: str, *, owner_root: Path | None = None) -> None:
        self.code = code
        self.owner_root = owner_root
        super().__init__(code)


class ReservationLease:
    """Opaque identity capability, valid only with the owner that issued it.

    It contains no destination or credential data. Constructing/copying a lease
    cannot confer ownership: release checks the exact issued object.
    """

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class _Reservation:
    root: Path
    lease: ReservationLease
    consumer: object | None = None


class ReservationOwner:
    """Atomic reserve/release with no await in any critical section (§8).

    Repeated reserve for a root alias and the same consumer returns its existing
    lease, not another consumer slot. Callers must reuse that consumer, and
    retain its lease until that consumer has actually stopped. Neither closing
    admission nor changing workspace selection revokes an outstanding lease.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._closed = False
        self._reservations: dict[tuple[str, str], _Reservation] = {}
        self._leases: dict[ReservationLease, tuple[str, str]] = {}

    def reserve(
        self, *, root: str | Path, url: str, persistent_id: str,
        consumer: object | None = None,
    ) -> ReservationLease:
        """Admit an exact destination, or report its current canonical owner.

        Canonicalisation happens before taking the registry lock; the lock only
        protects short in-memory operations. Invalid input is never reflected in
        an error, including URL userinfo and underlying validation exceptions.
        """
        with self._lock:
            if self._closed:
                raise ReservationError("reservation_admission_closed")
        target = None
        try:
            if (not isinstance(root, (str, Path)) or not str(root) or "\0" in str(root)
                    or not isinstance(url, str) or not isinstance(persistent_id, str)):
                raise ValueError
            target = (normalise_url(url), ProjectIdentity.canonical_id(persistent_id))
            owner_root = canonical_root(root)
        except (OSError, ValueError, TypeError, RuntimeError):
            target = None
        with self._lock:
            # Closing admission also fences a reserve still canonicalising its
            # root. Never publish after shutdown, even if validation began first.
            if self._closed:
                raise ReservationError("reservation_admission_closed")
            if target is None:
                raise ReservationError("invalid_reservation")
            incumbent = self._reservations.get(target)
            if incumbent is not None:
                # Runtime callers identify the actual consumer as well as its root.
                # A second AppContext for an alias must not borrow/release the first
                # runner's capability (runtime-integration-plan §3.1/I2).
                if incumbent.root == owner_root and incumbent.consumer is consumer:
                    return incumbent.lease
                raise ReservationError("duplicate_local_binding", owner_root=incumbent.root)
            lease = ReservationLease()
            self._reservations[target] = _Reservation(owner_root, lease, consumer)
            self._leases[lease] = target
            return lease

    def release(self, lease: object) -> bool:
        """Release only this owner's exact live lease, even after admission closes.

        Stale, forged, cross-owner and double releases are harmless. No root or
        destination based release exists: old cleanup cannot revoke a successor.
        """
        if type(lease) is not ReservationLease:
            return False
        with self._lock:
            target = self._leases.get(lease)
            if target is None:
                return False
            incumbent = self._reservations.get(target)
            if incumbent is None or incumbent.lease is not lease:
                return False
            del self._reservations[target]
            del self._leases[lease]
            return True

    def close_admission(self) -> None:
        """Fence new consumers, without releasing or stopping current consumers."""
        with self._lock:
            self._closed = True
