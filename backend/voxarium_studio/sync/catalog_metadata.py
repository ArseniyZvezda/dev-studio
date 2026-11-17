"""Unmounted presence observations (catalog-metadata-contract §3/§7, slice M).

All S2 readers and their *actual* live paths must be supplied for one home. This
is a trusted dependency boundary, not a sandbox for arbitrary Python callbacks.
No default home, token lookup, status cache, clock, transport or REST mapping is
provided. Verification ownership remains a separate blocker for full metadata.

These bounded observations detect races, not ABA or changes after the final
fence. In particular key A -> B with the same source is NOT credential continuity
evidence, and can never make verification available.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import errno
from pathlib import Path
import stat
from typing import Literal, TypeVar

from ..core.application_config import RemoteServerProfile
from .catalog_providers import PendingCatalogProviders, S2CatalogReaders
from .profiles import ProfileError


_CODES = frozenset({
    "invalid_catalog_readers", "application_store_unavailable",
    "credential_store_unavailable", "profile_recovery_required",
    "profile_revision_conflict", "invalid_profile_metadata", "profile_not_found",
    "metadata_unavailable",
})
T = TypeVar("T")


def _code(error: Exception, fallback: str) -> str:
    # Never retain even a sanitized reader's exception/context on our exception.
    if (type(error) is ProfileError and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in _CODES):
        return error.args[0]
    return fallback


@dataclass(frozen=True, slots=True)
class _Binding:
    profile_id: str
    name: str
    url: str
    revision: int


@dataclass(frozen=True, slots=True)
class CatalogMetadataRow:
    """Internal list/source row, deliberately NOT the REST ServerProfile.

    None/unbound is unusable ENV, not known absence (False/none). Neither bool
    nor source says anything about bearer validity, account or permissions.
    """

    profile_id: str
    name: str
    url: str
    revision: int
    has_token: bool | None
    credential_source: Literal["stored", "environment", "none", "environment_unbound"]
    verification: Literal["unavailable"] = field(default="unavailable", init=False)


@dataclass(frozen=True, repr=False, kw_only=True)
class CatalogMetadataObserver:
    home: Path
    list_reader: Callable[[], tuple[RemoteServerProfile, ...]]
    profile_reader: Callable[[str], RemoteServerProfile]
    source_reader: Callable[[str, str], str]
    application_file: Callable[[], Path]
    credential_file: Callable[[], Path]
    journal_file: Callable[[], Path]
    _readers: S2CatalogReaders = field(init=False, repr=False)

    def __post_init__(self) -> None:
        code = None
        readers = None
        try:
            if not callable(self.list_reader) or not callable(self.source_reader):
                raise ProfileError("invalid_catalog_readers")
            # Only check/get are reused: credential_reader is the secret-free
            # source callback, and PendingCatalogProviders.load_token is NEVER used.
            readers = S2CatalogReaders(
                self.home, self.profile_reader, self.source_reader,
                self.application_file, self.credential_file, self.journal_file,
            )
        except Exception:
            code = "invalid_catalog_readers"
        if code:
            raise ProfileError(code)
        object.__setattr__(self, "_readers", readers)

    def _check(self) -> None:
        self._readers.check()
        code = None
        # Fence validates live locations. Observe the two exact stores with
        # lstat, not exists: dangling links must never become known absence.
        for name, unavailable in (
            ("config.yaml", "application_store_unavailable"),
            ("remote-keys.json", "credential_store_unavailable"),
        ):
            try:
                mode = (self.home / name).lstat().st_mode
            except FileNotFoundError as error:
                if error.errno != errno.ENOENT:
                    code = code or unavailable
            except Exception:
                code = code or unavailable
            else:
                if not stat.S_ISREG(mode):
                    code = code or unavailable
        self._readers.check()
        if code:
            raise ProfileError(code)

    def _read(self, reader: Callable[[], T], fallback: str) -> T:
        self._check()
        code = None
        result = None
        try:
            result = reader()
        except Exception as error:
            code = _code(error, fallback)
        self._check()  # Includes failed readers, ENV early returns and empty list.
        if code:
            raise ProfileError(code)
        return result

    @staticmethod
    def _binding(profile: RemoteServerProfile) -> _Binding:
        if type(profile) is not RemoteServerProfile:
            raise ProfileError("invalid_profile_metadata")
        # Do not serialize an unvalidated model_construct/model_copy snapshot:
        # Pydantic serializer warnings can echo malformed, secret-bearing input.
        keys = ("profile_id", "name", "url", "revision")
        raw = {key: getattr(profile, key) for key in keys}
        if (profile.model_extra
                or any(type(raw[key]) is not str for key in ("profile_id", "name", "url"))
                or type(raw["revision"]) is not int):
            raise ProfileError("invalid_profile_metadata")
        # Revalidate model_construct/model_copy too, preserving historical names.
        checked = RemoteServerProfile.model_validate(
            raw, context={"stored_profile_metadata": True},
        )
        if {key: getattr(checked, key) for key in keys} != raw:
            raise ProfileError("invalid_profile_metadata")
        return _Binding(**raw)

    def _list(self) -> tuple[_Binding, ...]:
        values = self._read(self.list_reader, "application_store_unavailable")
        if type(values) is not tuple:
            raise ProfileError("invalid_profile_metadata")
        bindings = tuple(self._binding(value) for value in values)
        if (len({row.profile_id for row in bindings}) != len(bindings)
                or len({row.url for row in bindings}) != len(bindings)):
            raise ProfileError("invalid_profile_metadata")
        return bindings

    def _source(self, binding: _Binding) -> str:
        provider = PendingCatalogProviders(
            self._readers, profile_id=binding.profile_id,
            expected_url=binding.url, expected_revision=binding.revision,
        )

        def exact() -> None:
            profile = self._read(
                lambda: provider.get(binding.profile_id), "application_store_unavailable",
            )
            if self._binding(profile) != binding:
                raise ProfileError("profile_revision_conflict")

        exact()
        code = None
        source = None
        try:
            source = self._read(
                lambda: self.source_reader(binding.profile_id, binding.url),
                "credential_store_unavailable",
            )
        except Exception as error:
            code = _code(error, "credential_store_unavailable")
        exact()  # Also revalidate binding after a failed source read.
        if code:
            raise ProfileError(code)
        if type(source) is not str or source not in {
            "stored", "missing", "environment", "environment_unbound",
        }:
            raise ProfileError("credential_store_unavailable")
        return source

    def observe(self) -> tuple[CatalogMetadataRow, ...]:
        """Publish all rows only after L1, repeated sources and the final fence."""
        code = None
        rows = ()
        try:
            initial = self._list()
            sources = tuple(self._source(binding) for binding in initial)
            for binding, source in zip(initial, sources):
                if self._source(binding) != source:
                    raise ProfileError("metadata_unavailable")
            if self._list() != initial:
                raise ProfileError("metadata_unavailable")
            self._check()  # Required even for an empty registry/missing home.
            rows = tuple(CatalogMetadataRow(
                binding.profile_id, binding.name, binding.url, binding.revision,
                None if source == "environment_unbound" else source != "missing",
                "none" if source == "missing" else source,
            ) for binding, source in zip(initial, sources))
        except Exception as error:
            code = _code(error, "invalid_profile_metadata")
        # Outside every handler: no raw reader exception chain survives.
        if code:
            raise ProfileError(code)
        return rows
