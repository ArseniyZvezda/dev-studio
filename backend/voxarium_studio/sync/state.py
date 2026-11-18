"""What this checkout last agreed with the remote about (§16.3).

For every document, the server revision this Studio last had and the hash of its text at that
revision. That pair is the *base* of a three-way comparison: a file whose hash still matches
it has not been touched here, so a newer server revision can simply be written over it; one
that differs was edited here, and is either pushed or — when the server moved too — a
conflict.

Kept in the user's config directory, keyed by the project's path, not in ``.voxarium_studio/``:
it describes one person's checkout, and committing it would hand everybody else a base that
is not theirs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..core.paths import user_config_dir

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass(slots=True)
class Base:
    rev: int
    hash: str | None
    """``None`` for a document the server holds a tombstone for."""


@dataclass(slots=True)
class SyncState:
    path: Path
    url: str = ""
    space_id: str = ""
    cursor: int = 0
    """The highest server revision this checkout has been told about."""
    docs: dict[str, Base] = field(default_factory=dict)

    @staticmethod
    def file_for(project_root: Path) -> Path:
        digest = hashlib.sha1(str(project_root).encode("utf-8")).hexdigest()[:16]
        return user_config_dir() / "sync" / f"{digest}.json"

    @classmethod
    def load(cls, project_root: Path, *, url: str, space_id: str) -> "SyncState":
        """The remembered state — or an empty one when it is about another server or space.

        A space's id is minted when the space is created, so a server whose database was
        replaced announces a different one and every remembered revision is rightly dropped:
        the next sync compares what is on disk with what the server has, from nothing.
        """
        path = cls.file_for(project_root)
        fresh = cls(path=path, url=url, space_id=space_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return fresh
        except (OSError, ValueError) as exc:
            log.warning("Could not read sync state %s (%s); starting over", path, exc)
            return fresh
        if (
            not isinstance(raw, dict)
            or raw.get("version") != STATE_VERSION
            or raw.get("url") != url
            or raw.get("space_id") != space_id
        ):
            return fresh
        docs: dict[str, Base] = {}
        for doc_path, entry in (raw.get("docs") or {}).items():
            if isinstance(entry, dict) and isinstance(entry.get("rev"), int):
                digest = entry.get("hash")
                docs[doc_path] = Base(entry["rev"], digest if isinstance(digest, str) else None)
        cursor = raw.get("cursor")
        return cls(
            path=path, url=url, space_id=space_id,
            cursor=cursor if isinstance(cursor, int) else 0, docs=docs,
        )

    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "url": self.url,
            "space_id": self.space_id,
            "cursor": self.cursor,
            "docs": {path: {"rev": base.rev, "hash": base.hash} for path, base in self.docs.items()},
        }
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix=".sync-",
                suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream)
            temporary.replace(self.path)
        except OSError as exc:
            # Losing the state costs a full comparison on the next connect, never a document.
            log.warning("Could not save sync state %s (%s)", self.path, exc)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


# Opt-in S4 storage (remote-identity/studio-design §4.3/§6/§8). Nothing in
# RemoteSync consumes this API yet; the v1 class, path and format above stay intact.
STATE_V2_VERSION = 2
_RENAME_ATTEMPTS = 8


class SyncStateError(ValueError):
    """Fail-closed v2 storage/identity failure, carrying only a stable local code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SyncNamespace:
    """One checkout and exact Remote destination, not a portable project identity."""

    canonical_root: str
    url: str
    persistent_id: str

    def __post_init__(self) -> None:
        from ..core.project_identity import ProjectIdentity
        from ..core.project_scope import canonical_root
        from .protocol import normalise_url

        try:
            valid = (
                isinstance(self.canonical_root, str)
                and self.canonical_root == str(canonical_root(self.canonical_root))
                and isinstance(self.url, str)
                and self.url == normalise_url(self.url)
                and self.persistent_id == ProjectIdentity(id=self.persistent_id, revision=1).id
            )
            # Reject unencodable input before either hashing or creating a directory.
            json.dumps(self.as_dict(), ensure_ascii=False).encode("utf-8")
        except (OSError, ValueError, TypeError, RuntimeError):
            valid = False
        if not valid:
            raise SyncStateError("invalid_namespace")

    @classmethod
    def for_project(
        cls, project_root: str | Path, *, url: str, persistent_id: str,
    ) -> "SyncNamespace":
        from ..core.project_scope import canonical_root
        from .protocol import normalise_url

        try:
            if not isinstance(project_root, (str, Path)) or not isinstance(url, str):
                raise ValueError
            root = str(canonical_root(project_root))
            normalized_url = normalise_url(url)
        except (OSError, ValueError, TypeError, RuntimeError):
            raise SyncStateError("invalid_namespace") from None
        return cls(root, normalized_url, persistent_id)

    def as_dict(self) -> dict[str, str]:
        return {
            "canonical_root": self.canonical_root,
            "url": self.url,
            "persistent_id": self.persistent_id,
        }

    @property
    def path(self) -> Path:
        # Length-delimited JSON tuple avoids ambiguous separator concatenation.
        encoded = json.dumps(
            [self.canonical_root, self.url, self.persistent_id],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        return user_config_dir() / "sync" / "v2" / digest / "state.json"


def _v2_object(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or value.keys() != fields:
        raise SyncStateError("invalid_state")
    return value


def _v2_unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SyncStateError("invalid_state")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class SyncStateV2:
    """Explicit v2 checkpoint. Missing is ``None``; damage is always an exception.

    ``fresh`` only constructs a candidate after the caller's explicit agreement;
    it never loads legacy data or grants permission to sync. ``validate_hello``
    checks the checkpoint's identity/cursor only, not wire protocol/account/consent
    (those are the future service boundary's responsibility, §6). Callers must
    serialize saves per namespace; this is atomic publication, not a CAS/lease.
    """

    namespace: SyncNamespace
    space_instance_id: str
    cursor: int = 0
    docs: dict[str, Base] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.namespace.path

    @classmethod
    def fresh(
        cls, project_root: str | Path, *, url: str, persistent_id: str,
        space_instance_id: str,
    ) -> "SyncStateV2":
        state = cls(
            SyncNamespace.for_project(project_root, url=url, persistent_id=persistent_id),
            space_instance_id,
        )
        state._payload()  # Validate even an explicit empty candidate without writing it.
        return state

    @classmethod
    def load(
        cls, project_root: str | Path, *, url: str, persistent_id: str,
    ) -> "SyncStateV2 | None":
        namespace = SyncNamespace.for_project(project_root, url=url, persistent_id=persistent_id)
        return cls._load(namespace)

    @classmethod
    def _load(cls, namespace: SyncNamespace) -> "SyncStateV2 | None":
        path = namespace.path
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # A dangling state symlink is not a genuinely absent checkpoint.
            try:
                path.lstat()
            except FileNotFoundError:
                return None
            except OSError:
                pass
            raise SyncStateError("state_unreadable") from None
        except UnicodeError:
            raise SyncStateError("invalid_state") from None
        except OSError:
            raise SyncStateError("state_unreadable") from None
        try:
            raw = json.loads(content, object_pairs_hook=_v2_unique_object)
        except (ValueError, RecursionError):
            raise SyncStateError("invalid_state") from None
        return cls._decode(namespace, raw)

    @classmethod
    def _decode(cls, namespace: SyncNamespace, raw: object) -> "SyncStateV2":
        from .catalog import CatalogError
        from .catalog_reads import _epoch, _path

        raw = _v2_object(raw, {"version", "namespace", "space_instance_id", "cursor", "docs"})
        if type(raw["version"]) is not int or raw["version"] != STATE_V2_VERSION:
            raise SyncStateError("unsupported_version")
        stored_namespace = _v2_object(raw["namespace"], {"canonical_root", "url", "persistent_id"})
        # The hash is a locator, never proof: do NOT normalize stored values.
        if stored_namespace != namespace.as_dict():
            raise SyncStateError("namespace_mismatch")
        if type(raw["cursor"]) is not int or raw["cursor"] < 0 or not isinstance(raw["docs"], dict):
            raise SyncStateError("invalid_state")
        docs = {}
        try:
            _epoch(raw["space_instance_id"])
            for doc_path, entry in raw["docs"].items():
                _path(doc_path)
                entry = _v2_object(entry, {"rev", "hash"})
                if type(entry["rev"]) is not int or not 1 <= entry["rev"] <= raw["cursor"]:
                    raise SyncStateError("invalid_state")
                digest = entry["hash"]
                if digest is not None and (
                    not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise SyncStateError("invalid_state")
                docs[doc_path] = Base(entry["rev"], digest)
        except CatalogError:
            raise SyncStateError("invalid_state") from None
        return cls(namespace, raw["space_instance_id"], raw["cursor"], docs)

    def _payload(self) -> dict:
        if not isinstance(self.namespace, SyncNamespace) or not isinstance(self.docs, dict):
            raise SyncStateError("invalid_state")
        if any(not isinstance(base, Base) for base in self.docs.values()):
            raise SyncStateError("invalid_state")
        payload = {
            "version": STATE_V2_VERSION,
            "namespace": self.namespace.as_dict(),
            "space_instance_id": self.space_instance_id,
            "cursor": self.cursor,
            "docs": {path: {"rev": base.rev, "hash": base.hash} for path, base in self.docs.items()},
        }
        self._decode(self.namespace, payload)
        return payload

    def validate_hello(self, *, persistent_id: str, space_instance_id: str, seq: int) -> None:
        """Block incarnation mismatch/rollback, preserving all saved bases (§4.3/§6)."""
        from ..core.project_identity import ProjectIdentity
        from .catalog import CatalogError
        from .catalog_reads import _epoch

        self._payload()
        try:
            ProjectIdentity(id=persistent_id, revision=1)
            _epoch(space_instance_id)
        except (ValueError, CatalogError):
            raise SyncStateError("invalid_hello") from None
        if type(seq) is not int or seq < 0:
            raise SyncStateError("invalid_hello")
        if persistent_id != self.namespace.persistent_id:
            raise SyncStateError("identity_mismatch")
        if space_instance_id != self.space_instance_id:
            raise SyncStateError("instance_mismatch")
        if seq < self.cursor:
            raise SyncStateError("cursor_rollback")

    @classmethod
    def archive(cls, expected: "SyncStateV2") -> str:
        """VS-188: move THIS namespace's checkpoint aside after explicit confirmation.

        Only a rename inside the same namespace directory: the old bytes are kept
        under ``state.archived-<UTC>-<random>.json`` and are never rewritten,
        deleted or migrated; Markdown is not touched. The current checkpoint must
        still equal ``expected`` (another incarnation's, or one whose cursor the
        Remote went behind). Afterwards ``load`` returns ``None`` and only a new
        Preview → Enable may create a fresh checkpoint (§4.3/§6). Returns the
        archive file name. Callers serialize this with Enable for the namespace.
        """
        import time
        from uuid import uuid4

        from ..core.application_config import _fsync_directory

        if not isinstance(expected, SyncStateV2):
            raise SyncStateError("invalid_state")
        namespace = expected.namespace
        expected._payload()
        current = cls._load(namespace)
        if current is None or current != expected:
            raise SyncStateError("state_changed")
        path = namespace.path
        content = path.read_bytes()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        target = path.with_name(f"state.archived-{stamp}-{uuid4().hex[:12]}.json")
        try:
            if target.exists() or target.is_symlink():
                raise OSError("archive name taken")
            for attempt in range(_RENAME_ATTEMPTS):
                try:
                    # rename, not replace: an existing archive is never overwritten.
                    os.rename(path, target)
                    break
                except PermissionError as error:
                    # A foreign Windows handle (AV/indexer) on the just-read file.
                    if (getattr(error, "winerror", None) not in {5, 32}
                            or attempt == _RENAME_ATTEMPTS - 1):
                        raise
                time.sleep(0.01 * (attempt + 1))
            _fsync_directory(path.parent)
        except OSError:
            raise SyncStateError("state_write_failed") from None
        try:
            moved = target.read_bytes() == content
        except OSError:
            moved = False
        if cls._load(namespace) is not None or not moved:
            raise SyncStateError("state_write_failed")
        return target.name

    def save(self) -> None:
        """Explicit atomic publication; failure never masquerades as a saved base.

        Validate before mkdir, and refuse to overwrite an unreadable/foreign or
        newer checkpoint. No automatic repair, reset, migration or legacy access.
        A same-directory, flushed/fsynced temporary file is the only replace source.
        Directory-entry crash durability is filesystem-dependent; no multi-file
        transaction or concurrent-writer protection is promised by this API.
        """
        payload = self._payload()
        previous = self._load(self.namespace)
        if previous is not None:
            previous.validate_hello(
                persistent_id=self.namespace.persistent_id,
                space_instance_id=self.space_instance_id, seq=self.cursor,
            )
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix=".sync-v2-",
                suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        except (OSError, ValueError):
            raise SyncStateError("state_write_failed") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # Cleanup must not hide the publication failure/success.
                    log.warning("Could not remove a v2 sync temporary file")
