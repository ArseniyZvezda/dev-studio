"""Unmounted observer -> list adapter (catalog-metadata-contract §6, VS-125).

The already-bound observer is mandatory: this module chooses no home, readers,
runtime or production composition. Its one bounded observation owns the S2
fences; this adapter only validates and projects the complete immutable result.
Presence is not credential continuity or permission. No verification history is
invented, read or cached: every successful row explicitly reports unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..sync.catalog_metadata import CatalogMetadataObserver, CatalogMetadataRow
from ..sync.profiles import ProfileError
from .remote import ServerProfile, UnavailableVerification


# Preserve the observer's existing wire-safe failures, never arbitrary exception
# text. invalid_catalog_readers has no REST code and stays metadata_unavailable.
_OBSERVER_ERRORS = frozenset({
    "application_store_unavailable", "credential_store_unavailable",
    "profile_recovery_required", "profile_revision_conflict",
    "invalid_profile_metadata", "profile_not_found", "metadata_unavailable",
})


def _profiles(rows: tuple[CatalogMetadataRow, ...]) -> tuple[ServerProfile, ...]:
    if type(rows) is not tuple:
        raise ProfileError("metadata_unavailable")
    items = []
    for row in rows:
        # Do not serialize/deepcopy arbitrary objects or trust dataclass type
        # hints: constructed/tampered rows must fail before DTO publication.
        if type(row) is not CatalogMetadataRow:
            raise ProfileError("metadata_unavailable")
        raw = {name: getattr(row, name) for name in (
            "profile_id", "name", "url", "revision", "has_token", "credential_source",
        )}
        if (any(type(raw[name]) is not str for name in (
                "profile_id", "name", "url", "credential_source"))
                or type(raw["revision"]) is not int
                or type(raw["has_token"]) is not bool
                or type(row.verification) is not str
                or row.verification != "unavailable"):
            raise ProfileError("metadata_unavailable")
        source, present = raw["credential_source"], raw["has_token"]
        if not ((source in {"stored", "environment"} and present)
                or (source == "none" and not present)):
            # Includes ENV-unbound: unusable ENV is never known missing, nor a
            # reason to skip a row or fall back to a stored credential.
            raise ProfileError("metadata_unavailable")
        profile = ServerProfile.model_validate(
            {**raw, "verification": UnavailableVerification(state="unavailable")},
            strict=True, context={"stored_profile_metadata": True},
        )
        # Validation must not silently normalize/rebind an exact observer row.
        # Historical names are preserved by the read DTO, not fresh-name rules.
        if any(getattr(profile, name) != value for name, value in raw.items()):
            raise ProfileError("metadata_unavailable")
        items.append(profile)
    if (len({item.profile_id for item in items}) != len(items)
            or len({item.url for item in items}) != len(items)):
        raise ProfileError("metadata_unavailable")
    return tuple(items)


@dataclass(frozen=True, repr=False, kw_only=True)
class CatalogMetadataAdapter:
    """ServerMetadataProvider.list backed only by an injected ready observer."""

    observer: CatalogMetadataObserver = field(repr=False)

    def list(self) -> tuple[ServerProfile, ...]:
        code = None
        items = ()
        try:
            rows = self.observer.observe()  # Exactly once; no retry or rebinding.
        except Exception as error:
            code = "metadata_unavailable"
            if (type(error) is ProfileError and len(error.args) == 1
                    and type(error.args[0]) is str and error.args[0] in _OBSERVER_ERRORS):
                code = error.args[0]
        else:
            try:
                items = _profiles(rows)
            except Exception:
                # Even DTO errors may contain untrusted row data or chains.
                code = "metadata_unavailable"
        # Outside both handlers: neither __context__ nor __cause__ retains the
        # reader's exception, including sanitized-looking forged typed errors.
        if code:
            raise ProfileError(code)
        return items
