"""Private, opt-in root consent storage (remote-identity/studio-design §4.3/8).

I1/I2/I6 integrate this existing owner with inventory, startup and disabled local
publication. A confirmed record is local consent for an exact
working copy/target/revision/incarnation, not proof of current server permission.
Reads never create directories, lock files, defaults on error, or recovery state.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.application_config import RemoteServerProfile, application_config_transaction
from ..core.paths import user_config_dir
from ..core.project_identity import ProjectIdentity
from ..core.project_scope import canonical_root
from .catalog_reads import _epoch
from .protocol import normalise_url


class BindingError(ValueError):
    """Stable local code only, never raw JSON, addresses or underlying I/O errors."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def bindings_file() -> Path:
    return user_config_dir() / "remote-bindings.json"


def root_key(root: str | Path) -> str:
    """Same canonicalisation as the resident root registry, never a runtime ID."""
    result = None
    try:
        if not isinstance(root, (str, Path)) or not str(root) or "\0" in str(root):
            raise ValueError
        result = hashlib.sha256(str(canonical_root(root)).encode("utf-8")).hexdigest()
    except (OSError, ValueError, RuntimeError):
        pass
    if result is None:
        raise BindingError("invalid_binding")
    return result


class BindingRecord(BaseModel):
    """Nonsecret persisted DTO; no credentials or runtime handles (§4.3)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    canonical_root: str
    profile_id: str
    url: str
    persistent_id: str
    identity_revision: int = Field(ge=1)
    selection_state: Literal["selected", "confirmed"]
    space_instance_id: str | None = None

    @field_validator("canonical_root")
    @classmethod
    def valid_root(cls, value: str) -> str:
        if (not value or "\0" in value or not Path(value).is_absolute()
                or str(canonical_root(value)) != value):
            raise ValueError("Invalid canonical root")
        value.encode("utf-8")
        return value

    @field_validator("profile_id")
    @classmethod
    def valid_profile(cls, value: str) -> str:
        return RemoteServerProfile.valid_id(value)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        valid = False
        try:
            valid = normalise_url(value) == value
        except ValueError:
            pass
        if not valid:
            raise ValueError("Invalid canonical server address")
        return value

    @field_validator("persistent_id")
    @classmethod
    def valid_identity(cls, value: str) -> str:
        return ProjectIdentity.canonical_id(value)

    @field_validator("space_instance_id")
    @classmethod
    def valid_incarnation(cls, value: str | None) -> str | None:
        return None if value is None else _epoch(value, "invalid_binding")

    @model_validator(mode="after")
    def confirmed_incarnation(self) -> BindingRecord:
        if self.selection_state == "confirmed" and self.space_instance_id is None:
            raise ValueError("Confirmed binding requires incarnation")
        return self

    @classmethod
    def for_root(
        cls, root: str | Path, *, profile: RemoteServerProfile, identity: ProjectIdentity,
        selection_state: Literal["selected", "confirmed"], space_instance_id: str | None = None,
    ) -> BindingRecord:
        """Build explicit storage input; does not select, enable, or inspect keys."""
        record = None
        try:
            # Revalidate even model_copy/model_construct inputs at this boundary.
            profile = RemoteServerProfile.model_validate(
                profile.model_dump(), context={"stored_profile_metadata": True},
            )
            identity = ProjectIdentity.model_validate(identity.model_dump())
            root_key(root)
            record = cls(
                canonical_root=str(canonical_root(root)), profile_id=profile.profile_id,
                url=profile.url, persistent_id=identity.id, identity_revision=identity.revision,
                selection_state=selection_state, space_instance_id=space_instance_id,
            )
        except (AttributeError, OSError, ValueError, TypeError, RuntimeError):
            pass
        if record is None:
            raise BindingError("invalid_binding")
        return record


@dataclass(frozen=True)
class BindingSnapshot:
    """Immutable validated observation; fingerprint is CAS, not consent."""

    fingerprint: str | None
    records: tuple[BindingRecord, ...] = ()

    def get(self, root: str | Path) -> BindingRecord | None:
        key = root_key(root)
        for record in self.records:
            # Compare the actual root as well, not just a digest (§4.3).
            if root_key(record.canonical_root) == key and record.canonical_root == str(canonical_root(root)):
                return record
        return None

    def require_confirmed(
        self, root: str | Path, *, profile: RemoteServerProfile, identity: ProjectIdentity,
        url: str, space_instance_id: str,
    ) -> BindingRecord:
        """Check EVERY component, including the current portable target URL.

        The caller supplies the expected/observed incarnation explicitly; it is
        never inferred from persistent_id. This method performs no server check.
        selected/absent/stale observations cannot grant confirmed consent (§8).
        """
        expected = BindingRecord.for_root(
            root, profile=profile, identity=identity, selection_state="confirmed",
            space_instance_id=space_instance_id,
        )
        target = None
        try:
            if isinstance(url, str):
                target = normalise_url(url)
        except ValueError:
            pass
        if target is None:
            raise BindingError("invalid_binding")
        record = self.get(root)
        if target != expected.url or record != expected:
            raise BindingError("binding_required")
        return record


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate binding field")
        result[key] = value
    return result


class RemoteBindings:
    """Atomic per-root writes with whole-document optimistic CAS.

    Writers share the existing cross-process personal metadata transaction. A
    stale writer must reread/reconsider, even when it edits a different root; it
    cannot replace someone else's update. Unrelated records are copied exactly
    as validated values. No public bulk-replace/default-repair API is offered.
    """

    def read(self) -> BindingSnapshot:
        content = None
        missing = False
        try:
            content = bindings_file().read_bytes()
        except FileNotFoundError:
            # A dangling file link is unavailable state, not an absent store
            # which a later writer may silently replace with a fresh document.
            try:
                bindings_file().lstat()
            except FileNotFoundError:
                missing = True
            except OSError:
                pass
        except OSError:
            pass
        if missing:
            return BindingSnapshot(fingerprint=None)
        if content is None:
            raise BindingError("binding_store_unavailable")

        snapshot = None
        code = "binding_store_unavailable"
        try:
            raw = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(raw, dict) or raw.keys() != {"version", "roots"}:
                raise ValueError
            if type(raw["version"]) is not int or raw["version"] != 1:
                code = "binding_version_mismatch"
                raise ValueError
            if not isinstance(raw["roots"], dict):
                raise ValueError
            records = []
            for key, value in raw["roots"].items():
                # Presence of all schema fields matters; no default incarnation
                # repairs a partially written/older record during diagnostic read.
                if not isinstance(value, dict) or value.keys() != BindingRecord.model_fields.keys():
                    raise ValueError
                record = BindingRecord.model_validate(value)
                if key != root_key(record.canonical_root):
                    raise ValueError
                records.append(record)
            snapshot = BindingSnapshot(hashlib.sha256(content).hexdigest(), tuple(records))
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
        if snapshot is None:
            raise BindingError(code)
        return snapshot

    @staticmethod
    def _validate_fingerprint(expected_fingerprint: str | None) -> None:
        if (expected_fingerprint is not None
                and (not isinstance(expected_fingerprint, str)
                     or len(expected_fingerprint) != 64
                     or any(c not in "0123456789abcdef" for c in expected_fingerprint))):
            raise BindingError("invalid_binding")

    @staticmethod
    def _validate_record(record: BindingRecord) -> BindingRecord:
        validated = None
        try:
            if not isinstance(record, BindingRecord):
                raise ValueError
            validated = BindingRecord.model_validate(record.model_dump())
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
        if validated is None:
            raise BindingError("invalid_binding")
        return validated

    def save(self, record: BindingRecord, *, expected_fingerprint: str | None) -> BindingSnapshot:
        """Explicit CAS publication, acquiring the personal transaction once."""
        record = self._validate_record(record)
        self._validate_fingerprint(expected_fingerprint)
        result = None
        code = "binding_store_unavailable"
        try:
            with application_config_transaction():
                result = self._save_in_transaction(record, expected_fingerprint=expected_fingerprint)
        except BindingError as error:
            code = error.code
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
        if result is None:
            raise BindingError(code)
        return result

    def remove(self, root: str | Path, *, expected_fingerprint: str | None) -> BindingSnapshot:
        """Remove just this root with whole-file CAS; never delete the store.

        An absent root is an exact-CAS no-op. In particular, removing from a
        missing store does not manufacture an empty document or consent.
        """
        root_key(root)
        self._validate_fingerprint(expected_fingerprint)
        result = None
        code = "binding_store_unavailable"
        try:
            with application_config_transaction():
                result = self._remove_in_transaction(root, expected_fingerprint=expected_fingerprint)
        except BindingError as error:
            code = error.code
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
        if result is None:
            raise BindingError(code)
        return result

    def _save_in_transaction(
        self, record: BindingRecord, *, expected_fingerprint: str | None,
    ) -> BindingSnapshot:
        """Internal I6/I14 seam; caller owns lifecycle → config transaction.

        No lock acquisition here: another OS descriptor is not reentrant. This
        shares the public writer's validation, CAS and atomic publication.
        """
        record = self._validate_record(record)
        self._validate_fingerprint(expected_fingerprint)
        current = self.read()
        if current.fingerprint != expected_fingerprint:
            raise BindingError("binding_revision_conflict")
        records = {root_key(old.canonical_root): old for old in current.records}
        records[root_key(record.canonical_root)] = record
        return self._publish_in_transaction(tuple(records.values()))

    def _remove_in_transaction(
        self, root: str | Path, *, expected_fingerprint: str | None,
    ) -> BindingSnapshot:
        """Internal I6 remove; caller already owns the shared config transaction."""
        key = root_key(root)
        self._validate_fingerprint(expected_fingerprint)
        current = self.read()
        if current.fingerprint != expected_fingerprint:
            raise BindingError("binding_revision_conflict")
        if current.get(root) is None:
            return current
        records = tuple(old for old in current.records if root_key(old.canonical_root) != key)
        return self._publish_in_transaction(records)

    def _publish_in_transaction(self, records: tuple[BindingRecord, ...]) -> BindingSnapshot:
        """Flush before atomic replace; no fallible write/rollback after commit."""
        temporary = None
        result = None
        try:
            document = {"version": 1, "roots": {
                root_key(old.canonical_root): old.model_dump() for old in records
            }}
            content = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
                       + "\n").encode("utf-8")
            candidate = BindingSnapshot(hashlib.sha256(content).hexdigest(), records)
            path = bindings_file()
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".remote-bindings-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                if stream.write(content) != len(content):
                    raise OSError("Incomplete binding write")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            result = candidate
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        if result is None:
            raise BindingError("binding_store_unavailable")
        return result
