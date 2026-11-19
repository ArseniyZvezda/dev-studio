"""Private AI-access identity: client id, registrations, device secrets, exports (§16.12).

Contract: ``.voxarium_studio/docs/remote-identity/ai-access-design.md`` §3, §6 and §9.
Everything here lives in the user's Studio home (``user_config_dir()``), never in the
managed project, so a project save, a copied folder or a git checkout cannot carry an
enabled export, a grant or a secret (ai-access-design §3).

Storage — two owner-only files in one home, separate namespaces:

* ``ai-access.json`` — the private registry: the installation ``client_id``,
  registration metadata, export mappings and (see :mod:`.grants`) local consent records.
  **Known divergence:** the design names ``ai-access.db``; ACP/1 stores the same records
  as one strict, versioned JSON document instead of SQLite, written with the discipline of
  the existing personal credential provider (``sync/credentials.py``).
* ``ai-access-secrets.json`` — device secrets only, keyed by the exact
  ``(url, server_instance_id, user_id, registration_id)``. It is NOT ``remote-keys.json``:
  that store treats unknown top-level keys as corruption, and a device secret is a
  different credential with a different lifecycle (ai-access-design §3, §9).

Both files reuse the credential provider's primitives — URL normalisation, profile id
checks and the owner-only DACL/chmod of the fresh temp file — and its write discipline:
every mutation takes a thread lock plus a cross-process lock sidecar, re-reads and fully
validates the current file, compares revisions, then writes a fsynced temp file and
atomically replaces. A malformed, duplicate-key, unknown-field or unknown-version file is
never overwritten: the mutation fails with ``ai_access_store_corrupt`` and leaves the bytes
alone. A missing file means defaults, and defaults mean everything is disabled (§4).
Reads never create a file. Errors carry fixed codes only — never a path, URL or secret.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Annotated, Any, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, Field, ValidationError, model_validator

from ..core.paths import user_config_dir
from ..core.project_scope import canonical_root
from ..sync.credentials import CredentialsError, _profile_id, _restrict_secret_file, _url
from . import protocol
from .protocol import (
    Grant,
    MirrorAck,
    MirrorState,
    PersistentId,
    ProtocolError,
    Revision,
    Sha256,
    StorageEpoch,
    Strict,
    Target,
    Title,
    Uuid,
)

log = logging.getLogger(__name__)

REGISTRY_FILE = "ai-access.json"
SECRETS_FILE = "ai-access-secrets.json"
REGISTRY_VERSION = 1
SECRETS_VERSION = 1
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_DEVICE_SECRET_CHARS = 512
_REPLACE_ATTEMPTS = 10
_REPLACE_DELAY_SECONDS = 0.03

T = TypeVar("T")


# --------------------------------------------------------------------------- errors


class AiAccessError(ValueError):
    """Base of every AI-access failure: a fixed code, never input, path or secret."""

    def __init__(self, code: str = "ai_access_store_unavailable") -> None:
        self.code = str(code)
        super().__init__(self.code)


class AiAccessStoreError(AiAccessError):
    """The private store is unreadable, corrupt or could not be written."""


class AiAccessConflict(AiAccessError):
    """A compare-and-swap lost: the caller's ``expected_revision`` is stale."""

    def __init__(self, code: str = "ai_access_conflict") -> None:
        super().__init__(code)


# --------------------------------------------------------------------------- JSON store


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise AiAccessStoreError("ai_access_store_corrupt")
        result[key] = value
    return result


def _reject_number(_: str) -> Any:
    raise AiAccessStoreError("ai_access_store_corrupt")


def strict_loads(data: bytes) -> Any:
    """Parse a private store: UTF-8 without BOM, no duplicate keys, no floats/NaN."""
    if len(data) > MAX_STORE_BYTES or data.startswith(b"\xef\xbb\xbf"):
        raise AiAccessStoreError("ai_access_store_corrupt")
    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=_strict_pairs,
            parse_float=_reject_number, parse_constant=_reject_number,
        )
    except AiAccessStoreError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise AiAccessStoreError("ai_access_store_corrupt") from None


class JsonStore:
    """One owner-only JSON file in the Studio home with locked, atomic, fsynced writes.

    The same discipline as ``sync/credentials.py`` (§5.2.1): a stable ``.lock`` sidecar
    (never unlinked — see the credential provider for why), re-read under the lock, an
    owner-only temp file in the same directory, ``fsync`` and ``os.replace``. Windows
    may transiently refuse the replace (AV/indexer handles), so it is retried briefly.
    """

    _thread_locks: dict[str, RLock] = {}
    _guard = RLock()

    def __init__(self, filename: str) -> None:
        self.filename = filename

    @property
    def path(self) -> Path:
        return user_config_dir() / self.filename

    def _thread_lock(self) -> RLock:
        with JsonStore._guard:
            return JsonStore._thread_locks.setdefault(self.filename, RLock())

    def read(self) -> Any | None:
        """The parsed document, or ``None`` when the file does not exist. Never writes."""
        with self._thread_lock():
            try:
                data = self.path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError:
                raise AiAccessStoreError("ai_access_store_unavailable") from None
        return strict_loads(data)

    @contextlib.contextmanager
    def mutation(self) -> Iterator[None]:
        """Serialise read → validate → CAS → replace across threads and processes."""
        with self._thread_lock():
            lock_path = self.path.with_suffix(".lock")
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                stream = lock_path.open("a+b")
            except OSError:
                raise AiAccessStoreError("ai_access_store_unavailable") from None
            with stream:
                with contextlib.suppress(OSError):
                    lock_path.chmod(0o600)
                try:
                    if os.name == "nt":
                        import msvcrt

                        if stream.seek(0, os.SEEK_END) == 0:
                            stream.write(b"\0")
                            stream.flush()
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream, fcntl.LOCK_EX)
                except OSError:
                    raise AiAccessStoreError("ai_access_store_unavailable") from None
                try:
                    yield
                finally:
                    with contextlib.suppress(OSError):
                        if os.name == "nt":
                            stream.seek(0)
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(stream, fcntl.LOCK_UN)

    def write(self, value: Any) -> None:
        """Atomically replace the file. Call only inside :meth:`mutation`."""
        path = self.path
        temporary: Path | None = None
        try:
            data = json.dumps(
                value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                _restrict_secret_file(temporary)  # still empty: owner-only before data
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _replace(temporary, path)
            temporary = None
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except (OSError, ValueError, TypeError, UnicodeError):
            raise AiAccessStoreError("ai_access_store_unavailable") from None
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)


def _replace(source: Path, target: Path) -> None:
    """``os.replace`` with a short bounded retry for transient Windows sharing errors."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_SECONDS)


REGISTRY = JsonStore(REGISTRY_FILE)
SECRETS = JsonStore(SECRETS_FILE)


def registry_file() -> Path:
    return REGISTRY.path


def secrets_file() -> Path:
    return SECRETS.path


# --------------------------------------------------------------------------- field types


def _checked_url(value: str) -> str:
    if _url(value) != value:  # stored URLs are already exactly normalised
        raise ValueError("invalid_remote_url")
    return value


def _checked_root(value: str) -> str:
    if not value or len(value) > 4096 or not Path(value).is_absolute():
        raise ValueError("invalid_root")
    return value


def _checked_profile(value: str) -> str:
    return _profile_id(value)


ExactUrl = Annotated[str, AfterValidator(_checked_url)]
RootPath = Annotated[str, AfterValidator(_checked_root)]
ProfileId = Annotated[str, AfterValidator(_checked_profile)]


def normalise_remote_url(value: object) -> str:
    """The credential provider's exact normalisation; fixed error code, no echo."""
    try:
        return _url(value)  # type: ignore[arg-type]
    except CredentialsError:
        raise AiAccessError("invalid_remote_url") from None


def _uuid(value: object, code: str = "invalid_id") -> str:
    try:
        return protocol.check_uuid(value)
    except ProtocolError:
        raise AiAccessError(code) from None


# --------------------------------------------------------------------------- records


class Registration(Strict):
    """This installation's registration on one Remote control registry (ai-access-design §3).

    Keyed by the exact ``(url, server_instance_id, user_id)``. ``publication_enabled`` is a
    separate explicit switch and defaults to off (§4). Carries no secret: see
    :func:`load_device_secret` and :func:`registration_view`.
    """

    profile_id: ProfileId
    url: ExactUrl
    server_instance_id: Uuid
    user_id: Uuid
    registration_id: Uuid
    client_id: Uuid
    display_name: Title
    kind: Literal["studio"] = "studio"
    publication_enabled: bool = False
    revision: Revision


ExportFrameKind = Literal["publish", "unpublish"]


class ExportConsent(Strict):
    """AI-only binding evidence; private, never part of a mirror frame."""

    policy_version: Revision
    binding_fingerprint: Sha256


class ExportRecord(Strict):
    """Private mapping exact canonical root → registration + catalog identity (§3).

    ``export_revision`` is the fence: a change of root, persistent ID, storage epoch,
    identity revision, display name or policy digest disables the export and — once the
    current revision has been sent to Remote — bumps it, so every grant and command bound
    to the old target becomes stale. URL, account and registration are part of an
    export's identity: moving to another one is a new export, never a rebind.

    ``mirror_*`` describe the last lifecycle frame built for Remote; ``acked_revision``
    is the last revision Remote acknowledged with a matching hash.
    """

    export_id: Uuid
    root: RootPath
    url: ExactUrl
    server_instance_id: Uuid
    user_id: Uuid
    registration_id: Uuid
    persistent_id: PersistentId
    storage_epoch: StorageEpoch
    identity_revision: Revision
    display_name: Title
    policy_digest: Sha256
    consent: ExportConsent | None = None  # Legacy rows do not imply AI consent from a selected binding.
    enabled: bool = False
    deleted: bool = False
    export_revision: Revision
    mirror: MirrorState = MirrorState.UNPUBLISHED
    mirror_kind: ExportFrameKind | None = None
    mirror_revision: int = Field(default=0, ge=0)
    mirror_hash: Sha256 | None = None
    acked_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _mirror(self) -> ExportRecord:
        if (self.mirror_kind is None) != (self.mirror_revision == 0) or (
            (self.mirror_kind is None) != (self.mirror_hash is None)
        ):
            raise ValueError("invalid_export_mirror")
        if self.acked_revision > self.mirror_revision:
            raise ValueError("invalid_export_mirror")
        if not self.mirror_revision <= self.export_revision <= self.mirror_revision + 1:
            raise ValueError("invalid_export_revision")
        return self


class GrantRecord(Strict):
    """A local consent record and its mirror state; logic lives in :mod:`.grants` (§4).

    ``grant`` is the latest local revision (possibly pending or a revoke tombstone).
    ``active`` is what this PC may honour: the last acknowledged grant while an
    expansion is pending, the narrowed grant after a narrowing, ``None`` after a revoke.
    It is honoured only once ``acked_revision`` reaches its revision.
    """

    grant: Grant
    active: Grant | None = None
    mirror: MirrorState = MirrorState.PENDING
    mirror_hash: Sha256
    acked_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> GrantRecord:
        if self.active is not None and (
            self.active.grant_id != self.grant.grant_id
            or self.active.revision > self.grant.revision
            or self.active.target != self.grant.target
        ):
            raise ValueError("invalid_grant_record")
        if self.grant.revoked_at is not None and self.active is not None:
            raise ValueError("invalid_grant_record")
        if self.acked_revision > self.grant.revision:
            raise ValueError("invalid_grant_record")
        return self


class RegistryStore(Strict):
    """The whole private registry document, validated on every read and write."""

    version: Annotated[int, Field(ge=REGISTRY_VERSION, le=REGISTRY_VERSION)]
    revision: int = Field(ge=0)
    client_id: Uuid | None = None
    registrations: dict[str, Registration] = Field(default_factory=dict)
    exports: dict[str, ExportRecord] = Field(default_factory=dict)
    grants: dict[str, GrantRecord] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys(self) -> RegistryStore:
        triples = set()
        for key, registration in self.registrations.items():
            triple = (registration.url, registration.server_instance_id, registration.user_id)
            if (key != registration.registration_id or triple in triples
                    or registration.client_id != self.client_id):
                raise ValueError("invalid_registry")
            triples.add(triple)
        if any(key != export.export_id for key, export in self.exports.items()):
            raise ValueError("invalid_registry")
        for key, record in self.grants.items():
            if key != record.grant.grant_id or record.grant.target.export_id not in self.exports:
                raise ValueError("invalid_registry")
        return self


def _empty_registry() -> RegistryStore:
    return RegistryStore(version=REGISTRY_VERSION, revision=0)


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


M = TypeVar("M", bound=Strict)


def replace_model(model: M, **changes: Any) -> M:
    """A validated copy (``model_copy`` would skip validation of frozen strict models)."""
    data = model.model_dump(mode="json")
    data.update({key: _json(value) for key, value in changes.items()})
    try:
        return type(model).from_wire(data)
    except (ValidationError, ProtocolError):
        raise AiAccessError("invalid_record") from None


def _validate_store(model: type[M], value: Any) -> M:
    try:
        return model.from_wire(value)
    except (ValidationError, ProtocolError, ValueError):
        raise AiAccessStoreError("ai_access_store_corrupt") from None


def load_registry() -> RegistryStore:
    """Read-only view of the registry; a missing file is the all-disabled default."""
    raw = REGISTRY.read()
    return _empty_registry() if raw is None else _validate_store(RegistryStore, raw)


def mutate_registry(change: Callable[[RegistryStore], tuple[RegistryStore | None, T]]) -> T:
    """Run *change* on the freshly validated registry under the cross-process lock.

    *change* returns ``(new_store_or_None, result)``; ``None`` means nothing to write.
    A corrupt file raises before *change* runs and is never overwritten.
    """
    with REGISTRY.mutation():
        current = load_registry()
        updated, result = change(current)
        if updated is not None:
            updated = replace_model(updated, revision=current.revision + 1)
            REGISTRY.write(updated.model_dump(mode="json"))
        return result


# --------------------------------------------------------------------------- client id


def client_id() -> str | None:
    """The installation id, or ``None`` before an explicit :func:`ensure_client_id`."""
    return load_registry().client_id


def ensure_client_id() -> str:
    """Create (once) the random UUIDv4 installation id — only on explicit Register (§3).

    Never derived from hostname, IP, profile, task or project; stable across reloads.
    """

    def change(store: RegistryStore) -> tuple[RegistryStore | None, str]:
        if store.client_id is not None:
            return None, store.client_id
        created = str(uuid.uuid4())
        return replace_model(store, client_id=created), created

    return mutate_registry(change)


# --------------------------------------------------------------------------- registrations


def _registration_tuple(url: object, server_instance_id: object, user_id: object
                        ) -> tuple[str, str, str]:
    return (normalise_remote_url(url), _uuid(server_instance_id), _uuid(user_id))


def record_registration(
    *, profile_id: str, url: str, server_instance_id: str, user_id: str,
    registration_id: str, display_name: str,
) -> Registration:
    """Explicitly record a Remote ``201`` registration for this installation (§6).

    A different URL, server instance or user is a different record. The same profile and
    URL on a different ``server_instance_id`` is refused (``server_instance_mismatch``):
    a reset registry is never silently rebound. Publication stays disabled.
    """
    try:
        profile = _profile_id(profile_id)
    except CredentialsError:
        raise AiAccessError("invalid_profile_id") from None
    url, server_instance_id, user_id = _registration_tuple(url, server_instance_id, user_id)
    registration_id = _uuid(registration_id)
    try:
        name = protocol.check_title(display_name)
    except ProtocolError:
        raise AiAccessError("invalid_display_name") from None

    def change(store: RegistryStore) -> tuple[RegistryStore | None, Registration]:
        if store.client_id is None:
            raise AiAccessError("client_id_missing")
        for existing in store.registrations.values():
            if existing.profile_id == profile and existing.url == url and (
                existing.server_instance_id != server_instance_id
            ):
                raise AiAccessError("server_instance_mismatch")
            if (existing.url, existing.server_instance_id, existing.user_id) == (
                url, server_instance_id, user_id,
            ) or existing.registration_id == registration_id:
                raise AiAccessConflict("registration_exists")
        if any(export.registration_id == registration_id for export in store.exports.values()):
            raise AiAccessConflict("registration_exists")
        record = Registration(
            profile_id=profile, url=url, server_instance_id=server_instance_id,
            user_id=user_id, registration_id=registration_id, client_id=store.client_id,
            display_name=name, revision=1,
        )
        registrations = {**store.registrations, registration_id: record}
        return replace_model(store, registrations=registrations), record

    return mutate_registry(change)


def get_registration(registration_id: str) -> Registration | None:
    return load_registry().registrations.get(_uuid(registration_id))


def find_registration(url: str, server_instance_id: str, user_id: str) -> Registration | None:
    """Exact ``(normalised url, server_instance_id, user_id)`` lookup."""
    key = _registration_tuple(url, server_instance_id, user_id)
    for registration in load_registry().registrations.values():
        if (registration.url, registration.server_instance_id, registration.user_id) == key:
            return registration
    return None


def list_registrations() -> list[Registration]:
    return sorted(load_registry().registrations.values(), key=lambda r: r.registration_id)


def set_publication_enabled(
    registration_id: str, enabled: bool, *, expected_revision: int,
) -> Registration:
    """The separate, explicit global publication switch (§4). CAS on the record revision."""
    registration_id = _uuid(registration_id)
    if not isinstance(enabled, bool):
        raise AiAccessError("invalid_flag")

    def change(store: RegistryStore) -> tuple[RegistryStore | None, Registration]:
        current = store.registrations.get(registration_id)
        if current is None:
            raise AiAccessError("registration_not_found")
        if current.revision != expected_revision:
            raise AiAccessConflict()
        updated = replace_model(current, publication_enabled=enabled,
                                revision=current.revision + 1)
        registrations = {**store.registrations, registration_id: updated}
        return replace_model(store, registrations=registrations), updated

    return mutate_registry(change)


def remove_registration(registration_id: str, *, expected_revision: int) -> None:
    """Explicitly forget a registration and its device secret; fence its exports (§3, §9).

    The device secret is tombstoned first, so a failure half way leaves a registration
    without a secret (fail closed), never a secret without its registration record.
    """
    registration_id = _uuid(registration_id)
    current = get_registration(registration_id)
    if current is None:
        raise AiAccessError("registration_not_found")
    if current.revision != expected_revision:
        raise AiAccessConflict()
    _tombstone_device_secret(current)

    def change(store: RegistryStore) -> tuple[RegistryStore | None, None]:
        latest = store.registrations.get(registration_id)
        if latest is None:
            raise AiAccessError("registration_not_found")
        if latest.revision != expected_revision:
            raise AiAccessConflict()
        registrations = dict(store.registrations)
        del registrations[registration_id]
        exports = {
            key: fence_export(export) if export.registration_id == registration_id else export
            for key, export in store.exports.items()
        }
        return replace_model(store, registrations=registrations, exports=exports), None

    mutate_registry(change)


@dataclass(frozen=True, slots=True)
class RegistrationView:
    """Browser/registry-facing view: metadata and ``has_device_token`` only (§6)."""

    profile_id: str
    url: str
    server_instance_id: str
    user_id: str
    registration_id: str
    client_id: str
    display_name: str
    kind: str
    publication_enabled: bool
    revision: int
    has_device_token: bool


def registration_view(registration: Registration) -> RegistrationView:
    return RegistrationView(
        profile_id=registration.profile_id, url=registration.url,
        server_instance_id=registration.server_instance_id, user_id=registration.user_id,
        registration_id=registration.registration_id, client_id=registration.client_id,
        display_name=registration.display_name, kind=registration.kind,
        publication_enabled=registration.publication_enabled,
        revision=registration.revision,
        has_device_token=device_secret_source(
            registration.url, registration.server_instance_id, registration.user_id,
            registration.registration_id,
        ) == "stored",
    )


# --------------------------------------------------------------------------- device secrets


class _SecretEntry(Strict):
    url: ExactUrl
    server_instance_id: Uuid
    user_id: Uuid
    registration_id: Uuid
    token: str | None = Field(repr=False)
    revision: Revision

    @model_validator(mode="after")
    def _token(self) -> _SecretEntry:
        if self.token is not None:
            _check_secret(self.token)
        return self


class _SecretStore(Strict):
    version: Annotated[int, Field(ge=SECRETS_VERSION, le=SECRETS_VERSION)]
    secrets: dict[str, _SecretEntry] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def _keys(self) -> _SecretStore:
        if any(key != entry.registration_id for key, entry in self.secrets.items()):
            raise ValueError("invalid_secret_store")
        return self


def _check_secret(token: object) -> str:
    if (not isinstance(token, str) or not 16 <= len(token) <= MAX_DEVICE_SECRET_CHARS
            or any(not 33 <= ord(c) <= 126 for c in token)):
        raise AiAccessError("invalid_device_secret")
    return token


def _load_secrets() -> _SecretStore:
    raw = SECRETS.read()
    if raw is None:
        return _SecretStore(version=SECRETS_VERSION)
    return _validate_store(_SecretStore, raw)


def _secret_key(url: object, server_instance_id: object, user_id: object,
                registration_id: object) -> tuple[str, str, str, str]:
    return (*_registration_tuple(url, server_instance_id, user_id), _uuid(registration_id))


def _exact_entry(store: _SecretStore, key: tuple[str, str, str, str]) -> _SecretEntry | None:
    entry = store.secrets.get(key[3])
    if entry is None:
        return None
    if (entry.url, entry.server_instance_id, entry.user_id, entry.registration_id) != key:
        return None
    return entry


def load_device_secret(url: str, server_instance_id: str, user_id: str,
                       registration_id: str) -> str | None:
    """The device secret for the exact 4-tuple, else ``None``. Native transport only."""
    key = _secret_key(url, server_instance_id, user_id, registration_id)
    entry = _exact_entry(_load_secrets(), key)
    return entry.token if entry else None


def device_secret_revision(url: str, server_instance_id: str, user_id: str,
                           registration_id: str) -> int:
    """Stored revision including deletion tombstones; ``0`` when never stored."""
    key = _secret_key(url, server_instance_id, user_id, registration_id)
    entry = _load_secrets().secrets.get(key[3])
    return entry.revision if entry else 0


def device_secret_source(url: str, server_instance_id: str, user_id: str,
                         registration_id: str) -> Literal["stored", "missing"]:
    """Secret-free status for registry and browser views."""
    try:
        key = _secret_key(url, server_instance_id, user_id, registration_id)
        entry = _exact_entry(_load_secrets(), key)
    except AiAccessStoreError:
        log.warning("Could not read AI access device secrets")
        return "missing"
    return "stored" if entry and entry.token else "missing"


def save_device_secret(
    url: str, server_instance_id: str, user_id: str, registration_id: str,
    token: str | None, *, expected_revision: int,
) -> int:
    """CAS-store or (``None``) tombstone a device secret under its exact 4-tuple (§3, §9).

    A live secret is never moved to another URL/server/user; a tombstone keeps its
    revision so a stale writer cannot succeed after delete+recreate (ABA).
    """
    key = _secret_key(url, server_instance_id, user_id, registration_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise AiAccessError("invalid_revision")
    if token is not None:
        _check_secret(token)
    with SECRETS.mutation():
        store = _load_secrets()
        previous = store.secrets.get(key[3])
        revision = previous.revision if previous else 0
        if revision != expected_revision:
            raise AiAccessConflict()
        if previous is not None and previous.token is not None and (
            (previous.url, previous.server_instance_id, previous.user_id) != key[:3]
        ):
            raise AiAccessConflict()
        entry = _SecretEntry(
            url=key[0], server_instance_id=key[1], user_id=key[2], registration_id=key[3],
            token=token, revision=revision + 1,
        )
        secrets = {**store.secrets, key[3]: entry}
        SECRETS.write({
            "version": SECRETS_VERSION,
            "secrets": {k: v.model_dump(mode="json") for k, v in secrets.items()},
        })
        return revision + 1


def _tombstone_device_secret(registration: Registration) -> None:
    key = (registration.url, registration.server_instance_id, registration.user_id,
           registration.registration_id)
    with SECRETS.mutation():
        store = _load_secrets()
        previous = store.secrets.get(key[3])
        if previous is None or previous.token is None:
            return
        entry = _SecretEntry(
            url=previous.url, server_instance_id=previous.server_instance_id,
            user_id=previous.user_id, registration_id=previous.registration_id,
            token=None, revision=previous.revision + 1,
        )
        secrets = {**store.secrets, key[3]: entry}
        SECRETS.write({
            "version": SECRETS_VERSION,
            "secrets": {k: v.model_dump(mode="json") for k, v in secrets.items()},
        })


# --------------------------------------------------------------------------- exports


def export_target(export: ExportRecord) -> Target:
    """The full network target of an export at its current revision (§3)."""
    return Target(
        server_instance_id=export.server_instance_id,
        executor_registration_id=export.registration_id,
        export_id=export.export_id,
        persistent_id=export.persistent_id,
        storage_epoch=export.storage_epoch,
        export_revision=export.export_revision,
    )


def export_published(store: RegistryStore, export: ExportRecord) -> bool:
    """Published, enabled and acknowledged at its current revision on a live registration
    whose publication switch is on. Anything else is not available (§4)."""
    registration = store.registrations.get(export.registration_id)
    return (
        registration is not None
        and registration.publication_enabled
        and (registration.url, registration.server_instance_id, registration.user_id)
        == (export.url, export.server_instance_id, export.user_id)
        and export.enabled
        and not export.deleted
        and export.mirror is MirrorState.EFFECTIVE
        and export.mirror_kind == "publish"
        and export.acked_revision == export.mirror_revision == export.export_revision
    )


def fence_export(export: ExportRecord) -> ExportRecord:
    """Disable an export and move its fence past anything Remote may have seen.

    The revision is bumped only when the current revision was already sent: an unsent
    revision has no remote record, grants or commands bound to it, and skipping it
    would leave Remote a revision gap its CAS cannot accept (§6).
    """
    revision = export.export_revision
    if export.mirror_revision == revision:
        revision += 1
    return replace_model(export, enabled=False, export_revision=revision)


def _canonical_root(root: str | Path) -> str:
    try:
        resolved = canonical_root(root)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise AiAccessError("invalid_root") from None
    if not resolved.is_dir():
        raise AiAccessError("invalid_root")
    return str(resolved)


def _check_unique_export(store: RegistryStore, candidate: ExportRecord) -> None:
    live = [
        export for export in store.exports.values()
        if not export.deleted and export.export_id != candidate.export_id
        and export.registration_id == candidate.registration_id
    ]
    if any(export.persistent_id == candidate.persistent_id or export.root == candidate.root
           for export in live):
        raise AiAccessError("duplicate_export")
    if len(live) + 1 > protocol.MAX_EXPORTS_PER_EXECUTOR:
        raise AiAccessError(protocol.ErrorCode.CAPACITY_EXCEEDED)


def create_export(
    *, root: str | Path, registration_id: str, persistent_id: str, storage_epoch: str,
    identity_revision: int, display_name: str, policy_digest: str,
    export_id: str | None = None, consent: ExportConsent | None = None,
    expected_registration: Registration | None = None,
) -> ExportRecord:
    """Record a private, disabled, unpublished mapping of one resident working copy (§3).

    One live export per (registration, persistent ID) and per (registration, root): a
    second root gets ``duplicate_export``, never a merge. Export ids are never reused.
    """
    canonical = _canonical_root(root)
    registration_id = _uuid(registration_id)
    chosen = str(uuid.uuid4()) if export_id is None else _uuid(export_id)

    def change(store: RegistryStore) -> tuple[RegistryStore | None, ExportRecord]:
        registration = store.registrations.get(registration_id)
        if expected_registration is not None and registration != expected_registration:
            raise AiAccessError(protocol.ErrorCode.STALE_TARGET)
        if registration is None:
            raise AiAccessError("registration_not_found")
        if chosen in store.exports:
            raise AiAccessError("export_id_retired")
        try:
            record = ExportRecord.from_wire({
                "export_id": chosen, "root": canonical, "url": registration.url,
                "server_instance_id": registration.server_instance_id,
                "user_id": registration.user_id, "registration_id": registration_id,
                "persistent_id": persistent_id, "storage_epoch": storage_epoch,
                "identity_revision": identity_revision, "display_name": display_name,
                "policy_digest": policy_digest, "export_revision": 1,
                "consent": consent.model_dump(mode="json") if consent is not None else None,
            })
        except (ValidationError, ProtocolError):
            raise AiAccessError("invalid_export") from None
        _check_unique_export(store, record)
        return replace_model(store, exports={**store.exports, chosen: record}), record

    return mutate_registry(change)


def get_export(export_id: str) -> ExportRecord | None:
    return load_registry().exports.get(_uuid(export_id))


def list_exports(*, include_deleted: bool = False) -> list[ExportRecord]:
    exports = load_registry().exports.values()
    return sorted((e for e in exports if include_deleted or not e.deleted),
                  key=lambda e: e.export_id)


def _live_export(store: RegistryStore, export_id: str) -> ExportRecord:
    export = store.exports.get(export_id)
    if export is None:
        raise AiAccessError("export_not_found")
    if export.deleted:
        raise AiAccessError("export_deleted")
    return export


def update_export(
    export_id: str, *, expected_revision: int, root: str | Path | None = None,
    persistent_id: str | None = None, storage_epoch: str | None = None,
    identity_revision: int | None = None, display_name: str | None = None,
    policy_digest: str | None = None,
) -> ExportRecord:
    """Change a mapping or export policy: any real change disables and fences (§3, §6).

    Old commands and grants are never re-routed; publishing again is a new explicit act.
    """
    export_id = _uuid(export_id)
    changes: dict[str, Any] = {}
    if root is not None:
        changes["root"] = _canonical_root(root)
    for name, value in (("persistent_id", persistent_id), ("storage_epoch", storage_epoch),
                        ("identity_revision", identity_revision),
                        ("display_name", display_name), ("policy_digest", policy_digest)):
        if value is not None:
            changes[name] = value

    def change(store: RegistryStore) -> tuple[RegistryStore | None, ExportRecord]:
        current = _live_export(store, export_id)
        if current.export_revision != expected_revision:
            raise AiAccessConflict()
        candidate = replace_model(current, **changes)
        if candidate == current:
            return None, current
        _check_unique_export(store, candidate)
        updated = fence_export(candidate)
        return replace_model(store, exports={**store.exports, export_id: updated}), updated

    return mutate_registry(change)


def _publish_semantics(export: ExportRecord) -> dict[str, Any]:
    return {
        "export_id": export.export_id, "persistent_id": export.persistent_id,
        "storage_epoch": export.storage_epoch, "revision": export.export_revision,
        "expected_revision": export.export_revision - 1,
        "policy_digest": export.policy_digest, "display_name": export.display_name,
    }


def publish_frame(export: ExportRecord) -> protocol.ExportPublish:
    """``export.publish`` for the export's current revision; hash over the semantic
    record without ``hash`` (§6). Pure: identical records give identical frames."""
    semantics = _publish_semantics(export)
    return protocol.ExportPublish.from_wire(
        {**semantics, "hash": protocol.record_hash(semantics)},
    )


def _unpublish_semantics(export: ExportRecord) -> dict[str, Any]:
    return {
        "export_id": export.export_id, "expected_revision": export.export_revision - 1,
        "revision": export.export_revision,
    }


def unpublish_frame(export: ExportRecord) -> protocol.ExportUnpublish:
    semantics = _unpublish_semantics(export)
    return protocol.ExportUnpublish.from_wire(
        {**semantics, "hash": protocol.record_hash(semantics)},
    )


def publish_export(
    export_id: str, *, expected_revision: int, expected_export: ExportRecord | None = None,
    expected_registration: Registration | None = None,
) -> protocol.ExportPublish:
    """Explicit Publish: enable locally, save the frame as pending, return it (§4, §6).

    The export is honoured only after :func:`apply_export_ack` with a matching ACK. A
    re-publish after an unpublish needs a greater revision, so it bumps the fence.
    """
    export_id = _uuid(export_id)

    def change(store: RegistryStore) -> tuple[RegistryStore | None, protocol.ExportPublish]:
        if expected_export is not None and store.exports.get(export_id) != expected_export:
            raise AiAccessError(protocol.ErrorCode.STALE_TARGET)
        current = _live_export(store, export_id)
        if current.export_revision != expected_revision:
            raise AiAccessConflict()
        registration = store.registrations.get(current.registration_id)
        if expected_registration is not None and registration != expected_registration:
            raise AiAccessError(protocol.ErrorCode.STALE_TARGET)
        if registration is None or not registration.publication_enabled:
            raise AiAccessError("publication_disabled")
        export = current
        if export.mirror_kind == "unpublish" and export.mirror_revision == export.export_revision:
            export = replace_model(export, export_revision=export.export_revision + 1)
        frame = publish_frame(export)
        same = (export.mirror_kind == "publish" and export.mirror_revision == frame.revision
                and export.mirror_hash == frame.hash)
        export = replace_model(
            export, enabled=True, mirror_kind="publish", mirror_revision=frame.revision,
            mirror_hash=frame.hash,
            mirror=export.mirror if same else MirrorState.PENDING,
        )
        if export == current:
            return None, frame
        return replace_model(store, exports={**store.exports, export_id: export}), frame

    return mutate_registry(change)


_Unpublish = protocol.ExportUnpublish | None


def unpublish_export(export_id: str) -> protocol.ExportUnpublish | None:
    """Local deny first: disable and fence now, then return the mirror frame (§6, §9).

    No CAS: narrowing can never lose an update that grants anything. ``None`` when Remote
    was never sent this export. Repeating returns the identical frame.
    """
    export_id = _uuid(export_id)

    def change(store: RegistryStore) -> tuple[RegistryStore | None, _Unpublish]:
        current = store.exports.get(export_id)
        if current is None:
            raise AiAccessError("export_not_found")
        return _unpublished(store, current)

    return mutate_registry(change)


def _unpublished(store: RegistryStore, current: ExportRecord
                 ) -> tuple[RegistryStore | None, protocol.ExportUnpublish | None]:
    if current.mirror_revision == 0:
        if not current.enabled:
            return None, None
        export = replace_model(current, enabled=False)
        return replace_model(store, exports={**store.exports, current.export_id: export}), None
    if current.mirror_kind == "unpublish" and current.mirror_revision == current.export_revision:
        frame = unpublish_frame(current)
        if not current.enabled:
            return None, frame
        export = replace_model(current, enabled=False)
        return replace_model(store, exports={**store.exports, current.export_id: export}), frame
    export = fence_export(current)
    frame = unpublish_frame(export)
    export = replace_model(export, mirror_kind="unpublish", mirror_revision=frame.revision,
                           mirror_hash=frame.hash, mirror=MirrorState.PENDING)
    return replace_model(store, exports={**store.exports, current.export_id: export}), frame


def delete_export(export_id: str) -> protocol.ExportUnpublish | None:
    """Delete a mapping: a terminal tombstone whose id is never reused (§6, §7).

    Its live grants are revoked locally (tombstones kept) and, if Remote knew it, the
    unpublish frame is returned for the mirror.
    """
    from .grants import revoked_record  # local import: grants depends on this module

    export_id = _uuid(export_id)

    def change(store: RegistryStore) -> tuple[RegistryStore | None, _Unpublish]:
        current = store.exports.get(export_id)
        if current is None:
            raise AiAccessError("export_not_found")
        updated, frame = _unpublished(store, current)
        updated = updated or store
        export = replace_model(updated.exports[export_id], deleted=True, enabled=False)
        grants = {
            key: revoked_record(record, now_ms=_now_ms())
            if record.grant.target.export_id == export_id and record.grant.revoked_at is None
            else record
            for key, record in updated.grants.items()
        }
        return replace_model(updated, exports={**updated.exports, export_id: export},
                             grants=grants), frame

    return mutate_registry(change)


class AckOutcome(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE = "stale"
    CONFLICT = "conflict"


def apply_export_ack(ack: MirrorAck) -> AckOutcome:
    """Mark an export effective/unpublished only on a matching ACK (§6).

    Same export id, revision and hash → applied (or duplicate). Equal revision with a
    different hash, an unexpected state or a revision we never sent → conflict, which
    never enables anything. A lower revision is stale and changes nothing.
    """
    if ack.grant_id is not None:
        raise AiAccessError("ack_mismatch")

    def change(store: RegistryStore) -> tuple[RegistryStore | None, AckOutcome]:
        current = store.exports.get(ack.export_id)
        if current is None:
            raise AiAccessError("export_not_found")
        if ack.revision < current.mirror_revision:
            return None, AckOutcome.STALE
        expected_state = (MirrorState.EFFECTIVE if current.mirror_kind == "publish"
                          else MirrorState.UNPUBLISHED)
        if (ack.revision > current.mirror_revision or ack.hash != current.mirror_hash
                or ack.state is not expected_state):
            if current.mirror is MirrorState.CONFLICT:
                return None, AckOutcome.CONFLICT
            export = replace_model(current, mirror=MirrorState.CONFLICT)
            return (replace_model(store, exports={**store.exports, current.export_id: export}),
                    AckOutcome.CONFLICT)
        if current.acked_revision == ack.revision and current.mirror is expected_state:
            return None, AckOutcome.DUPLICATE
        export = replace_model(current, mirror=expected_state, acked_revision=ack.revision)
        return (replace_model(store, exports={**store.exports, current.export_id: export}),
                AckOutcome.APPLIED)

    return mutate_registry(change)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
