"""Per-Ctx document admission and bounded binding-change coordination.

I3 of remote-identity/runtime-integration-plan §§3.2–3.3: the bound DocStore
owns one of these for browser, in-process MCP and remote-apply mutations.
I4 adds server-owned editor leases and HTTP fencing (studio-design §§5.2–5.3).
I5 coordinates prepare/drain/fence; I6 retains durable disabled-commit evidence.
I12 adds the separate Enable journal (publication order only; the API owner
performs the publications). No transport, automatic recovery or job gate here.
All admission methods run on the owning context's event loop. Thread locks
and external filesystem writers are not covered by this boundary.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from ..teams.docs import DocStore


class MutationGateError(RuntimeError):
    """Typed local refusal; not a disk conflict or permission error."""

    code: str
    retryable = False


class StaleBindingGeneration(MutationGateError):
    code = "stale_binding_generation"

    def __init__(self, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(self.code)


class MutationBusy(MutationGateError):
    """A short final snapshot excludes new writers; callers may retry explicitly."""

    code = "mutation_busy"
    retryable = True

    def __init__(self) -> None:
        super().__init__(self.code)


class EditorLeaseError(MutationGateError):
    """Fixed, secret-free I4 refusals, suitable for a local HTTP 409."""

    def __init__(self, code: str) -> None:
        if code not in {
            "reload_required", "stale_backend_epoch", "unknown_editor", "editor_mismatch",
            "editor_lease_expired", "unknown_sid", "pending_edits", "stale_editor_version",
            "invalid_editor", "use_remote_settings",
        }:
            raise ValueError("unknown_editor_error_code")
        self.code = code
        super().__init__(code)


class BindingChangeError(MutationGateError):
    """Secret-free local coordinator refusal; evidence validation belongs to API owners."""

    def __init__(self, code: str) -> None:
        if code not in {
            "unknown_change", "change_in_progress", "change_expired", "invalid_change",
            "change_not_quiescing", "invalid_editor_ack", "binding_paused",
            "binding_journal_unavailable", "change_not_ready", "already_committed",
            "stale_binding", "confirmation_required", "recovery_required",
            "binding_generation_exhausted",
        }:
            raise ValueError("unknown_binding_change_error")
        self.code = code
        super().__init__(code)


# I6/I7: G is a nonsecret ordering/CAS fence within backend_epoch, not an
# authorization capability. JSON clients must compare it losslessly. Restart
# creates a new backend_epoch; durable journals never seed the runtime counter.
MAX_BINDING_GENERATION = (1 << 53) - 1


# Windows refuses MoveFileEx(REPLACE_EXISTING) while another process (an AV
# scanner or indexer) briefly holds the just-written target without
# FILE_SHARE_DELETE: ERROR_ACCESS_DENIED (5) / ERROR_SHARING_VIOLATION (32).
# Observed for a journal overwritten milliseconds after its previous phase;
# it surfaced as I6/I12 binding_journal_unavailable -> recovery_required.
_TRANSIENT_REPLACE_WINERRORS = frozenset({5, 32})
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF_SECONDS = 0.01
_replace_sleep = time.sleep


def _os_replace(source: Path, target: Path) -> None:
    # Path.replace (os.replace): the same seam the I6 fault tests inject into.
    Path(source).replace(target)


def _replace_journal(temporary: Path, path: Path) -> None:
    """Atomic journal publication, bounded-retried ONLY on a foreign Windows handle.

    os.replace either happened or did not, so repeating the identical rename is
    safe. Any other error, or the last attempt's, propagates unchanged (≤0.28 s).
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            _os_replace(temporary, path)
            return
        except PermissionError as error:
            if (getattr(error, "winerror", None) not in _TRANSIENT_REPLACE_WINERRORS
                    or attempt == _REPLACE_ATTEMPTS - 1):
                raise
        _replace_sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))


def next_binding_generation(generation: int, steps: int = 1) -> int:
    """Checked increments; exhaust BEFORE drain/publication rather than wrap G."""
    if (type(generation) is not int or not 0 <= generation <= MAX_BINDING_GENERATION
            or type(steps) is not int or steps < 1
            or generation > MAX_BINDING_GENERATION - steps):
        raise BindingChangeError("binding_generation_exhausted")
    return generation + steps


def _plain_change_evidence(value: object) -> object:
    """Bounded JSON copy of nonsecret coordinator evidence, never caller storage."""
    if isinstance(value, Mapping):
        if any(str(key).lower() in {"token", "key", "password", "authorization",
                                   "key_hash", "token_hash", "content", "markdown", "yjs"}
               for key in value):
            raise BindingChangeError("invalid_change")
        return {key: _plain_change_evidence(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_change_evidence(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    raise BindingChangeError("invalid_change")


def binding_change_journal_path(root: str | Path, change_id: str) -> Path:
    from ..core.paths import user_config_dir
    from .bindings import root_key

    if (not isinstance(change_id, str) or len(change_id) != 32
            or any(c not in "0123456789abcdef" for c in change_id)):
        raise BindingChangeError("invalid_change")
    return user_config_dir() / "remote-changes" / root_key(root) / (change_id + ".json")


def read_binding_change_journals(root: str | Path) -> tuple[dict, ...]:
    """Strict, read-only recovery classification input (plan §2.3/3.5).

    An existing empty/unreadable directory is known-lost evidence, not legacy.
    Completed records are deliberately never expired or garbage-collected in I6.
    """
    import json
    import stat
    from ..core.project_scope import canonical_root

    directory = binding_change_journal_path(root, "0" * 32).parent
    try:
        if (directory.parent.is_symlink()
                or directory.parent.exists() and not directory.parent.is_dir()):
            raise ValueError
        try:
            directory.lstat()
        except FileNotFoundError:
            return ()
        if (directory.is_symlink() or directory.parent.is_symlink()
                or not directory.is_dir()):
            raise ValueError
        paths = list(directory.iterdir())
        records = []

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        for path in paths:
            # A failed pre-replace temp is not a published journal. It cannot
            # make a directory without published records count as pristine.
            if path.name.startswith(".binding-change-"):
                continue
            info = path.lstat()
            if (path.suffix != ".json" or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1):
                raise ValueError
            binding_change_journal_path(root, path.stem)
            content = path.read_bytes()
            if len(content) > 4 * 1024 * 1024:
                raise ValueError
            row = json.loads(content.decode("utf-8"), object_pairs_hook=unique)
            if (not isinstance(row, dict) or type(row.get("version")) is not int
                    or row["version"] not in {1, 2, ENABLE_JOURNAL_VERSION}
                    or row.get("canonical_root") != str(canonical_root(root))
                    or row.get("change_id") != path.stem
                    or not isinstance(row.get("expected"), dict)
                    or not isinstance(row.get("evidence"), list)):
                raise ValueError
            fields = {"version", "phase", "canonical_root", "change_id", "expected", "evidence"}
            if row["version"] == 1:
                if set(row) != fields or row["phase"] != "draining":
                    raise ValueError
            elif row["version"] == ENABLE_JOURNAL_VERSION:
                _validate_enable_journal(row)
            else:
                if (set(row) != fields | {"commit"}
                        or row["phase"] not in {"committing", "config_committed", "completed"}
                        or not isinstance(row["commit"], dict)):
                    raise ValueError
                commit = row["commit"]
                if (set(commit) != {"identity_revision", "persistent_id", "url", "enabled",
                                    "config_fingerprint", "private_fingerprint", "binding",
                                    "binding_generation"}
                        or type(commit["identity_revision"]) is not int
                        or commit["identity_revision"] < 1 or commit["enabled"] is not False
                        or type(commit["binding_generation"]) is not int):
                    raise ValueError
                from ..core.project_identity import ProjectIdentity
                from .protocol import normalise_url
                from .bindings import BindingRecord
                ProjectIdentity.canonical_id(commit["persistent_id"])
                if commit["url"] and normalise_url(commit["url"]) != commit["url"]:
                    raise ValueError
                for fingerprint in (commit["config_fingerprint"], commit["private_fingerprint"]):
                    if fingerprint is not None and (not isinstance(fingerprint, str)
                            or len(fingerprint) != 64
                            or any(c not in "0123456789abcdef" for c in fingerprint)):
                        raise ValueError
                if commit["config_fingerprint"] is None:
                    raise ValueError
                expected = row["expected"]
                if (set(expected) not in (
                        {"observation", "from", "to", "operation", "binding_generation", "backend_epoch"},
                        {"observation", "from", "to", "operation", "binding_generation", "backend_epoch", "selection"})
                        or not isinstance(expected["observation"], dict)
                        or expected["observation"].get("canonical_root") != row["canonical_root"]
                        or type(expected["observation"].get("identity_revision")) is not int
                        or commit["identity_revision"] != expected["observation"]["identity_revision"] + 1
                        or expected["to"] != {"persistent_id": commit["persistent_id"],
                                               "url": commit["url"], "enabled": False}
                        or expected["operation"] not in {"edit_id", "select", "disable", "unbind"}
                        or type(expected["binding_generation"]) is not int
                        or not isinstance(expected["backend_epoch"], str)):
                    raise ValueError
                if commit["binding"] is not None:
                    binding = BindingRecord.model_validate(commit["binding"])
                    if (binding.canonical_root != row["canonical_root"]
                            or binding.identity_revision != commit["identity_revision"]
                            or binding.url != commit["url"]
                            or binding.persistent_id != commit["persistent_id"]
                            or binding.selection_state != "selected"):
                        raise ValueError
            _plain_change_evidence(row)
            records.append(row)
        if not records:
            raise ValueError
        return tuple(records)
    except Exception:
        raise BindingChangeError("binding_journal_unavailable") from None


def publish_binding_change_commit(
    root: str | Path, change_id: str, expected: Mapping[str, object],
    commit: Mapping[str, object], phase: str,
) -> dict:
    """Durable nonsecret journal under the caller's lifecycle/config ownership.

    No rollback, auto-repair, expiry or credential store is hidden here. The
    project config replace, not this multi-file record, remains the commit point.
    """
    import json
    import os
    import tempfile
    from ..core.application_config import _fsync_directory
    from ..core.project_scope import canonical_root

    path = binding_change_journal_path(root, change_id)
    temporary = None
    try:
        previous = next((row for row in read_binding_change_journals(root)
                         if row["change_id"] == change_id), None)
        captured = _plain_change_evidence(expected)
        value = _plain_change_evidence(commit)
        if phase not in {"committing", "config_committed", "completed"}:
            raise ValueError
        if previous is not None:
            if previous["expected"] != captured:
                raise ValueError
            if previous["version"] == 2:
                # Completion can add only the resulting private fingerprint.
                old = dict(previous["commit"])
                old["private_fingerprint"] = value["private_fingerprint"]
                if old != value:
                    raise ValueError
        row = {"version": 2, "phase": phase, "canonical_root": str(canonical_root(root)),
               "change_id": change_id, "expected": captured, "commit": value,
               "evidence": previous["evidence"] if previous else []}
        encoded = json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=".binding-change-", delete=False) as stream:
            temporary = Path(stream.name)
            if stream.write(encoded) != len(encoded):
                raise OSError
            stream.flush()
            os.fsync(stream.fileno())
        _replace_journal(temporary, path)
        _fsync_directory(path.parent)
        return row
    except Exception:
        raise BindingChangeError("binding_journal_unavailable") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


# --- I12: the separate Enable/catch-up journal (studio-design §7.7) ---------
#
# A version-3 record under the same per-root remote-changes directory. Phases
# advance strictly: intent ("enabling") is durable BEFORE the project config
# is published; "config_committed" and "binding_committed" follow each store;
# "completed" is written only after the v2 checkpoint exists, and only a
# completed record is activation evidence. "aborted" is terminal and is used
# ONLY when the project config is proven unpublished. Anything else is
# unresolved: blocked recovery_required, never an automatic retry or rollback.

ENABLE_JOURNAL_VERSION = 3
ENABLE_JOURNAL_PHASES = ("enabling", "config_committed", "binding_committed", "completed", "aborted")
TERMINAL_JOURNAL_PHASES = frozenset({"completed", "aborted"})
_ENABLE_EXPECTED = frozenset({"operation", "observation", "to", "snapshot", "binding_generation",
                              "backend_epoch", "account_id"})
_ENABLE_COMMIT = frozenset({"identity_revision", "persistent_id", "url", "enabled", "config_fingerprint",
                            "private_fingerprint", "binding", "binding_generation"})


def journal_unresolved(row: Mapping[str, object]) -> bool:
    """A started change (I5 drain, I6 commit or I12 Enable) that never settled."""
    return row.get("phase") not in TERMINAL_JOURNAL_PHASES


def _hex64(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _validate_enable_journal(row: Mapping[str, object]) -> None:
    """Strict schema of one I12 record; raises ValueError on any deviation."""
    from ..core.project_identity import ProjectIdentity
    from .bindings import BindingRecord
    from .protocol import normalise_url

    if (set(row) != {"version", "phase", "canonical_root", "change_id", "expected", "evidence", "commit"}
            or row["phase"] not in ENABLE_JOURNAL_PHASES
            or not isinstance(row["commit"], dict) or not isinstance(row["expected"], dict)):
        raise ValueError
    expected, commit = row["expected"], row["commit"]
    observation = expected.get("observation")
    if (set(expected) != _ENABLE_EXPECTED or expected["operation"] != "enable"
            or not isinstance(observation, dict)
            or observation.get("canonical_root") != row["canonical_root"]
            or type(observation.get("identity_revision")) is not int
            or type(expected["binding_generation"]) is not int
            or not isinstance(expected["backend_epoch"], str) or not expected["backend_epoch"]
            or not isinstance(expected["account_id"], str) or not expected["account_id"]
            or len(expected["account_id"]) > 256):
        raise ValueError
    if (set(commit) != _ENABLE_COMMIT or commit["enabled"] is not True
            or type(commit["identity_revision"]) is not int
            or commit["identity_revision"] != observation["identity_revision"] + 1
            or type(commit["binding_generation"]) is not int
            or commit["binding_generation"] != expected["binding_generation"] + 1
            or not _hex64(commit["config_fingerprint"]) or not _hex64(commit["private_fingerprint"])
            or not isinstance(commit["url"], str) or not commit["url"]
            or normalise_url(commit["url"]) != commit["url"]
            or ProjectIdentity.canonical_id(commit["persistent_id"]) != commit["persistent_id"]
            or expected["to"] != {"persistent_id": commit["persistent_id"], "url": commit["url"],
                                  "enabled": True}):
        raise ValueError
    snapshot = expected["snapshot"]
    binding = BindingRecord.model_validate(commit["binding"])
    if (not isinstance(snapshot, dict) or set(snapshot) != {"id", "storage_epoch", "seq"}
            or snapshot["id"] != commit["persistent_id"]
            or type(snapshot["seq"]) is not int or snapshot["seq"] < 0
            or binding.canonical_root != row["canonical_root"]
            or binding.selection_state != "confirmed"
            or binding.identity_revision != commit["identity_revision"]
            or binding.url != commit["url"] or binding.persistent_id != commit["persistent_id"]
            or binding.space_instance_id != snapshot["storage_epoch"]):
        raise ValueError


def publish_enable_journal(
    root: str | Path, change_id: str, expected: Mapping[str, object],
    commit: Mapping[str, object], phase: str,
) -> dict:
    """Durable nonsecret I12 record under the caller's lifecycle/config ownership.

    The first write must be the intent; later phases only advance, and the
    captured expected/commit values can never change. Like the I6 journal it
    is never garbage-collected and never repairs config/private state.
    """
    import json
    import os
    import tempfile
    from ..core.application_config import _fsync_directory
    from ..core.project_scope import canonical_root

    path = binding_change_journal_path(root, change_id)
    temporary = None
    try:
        previous = next((row for row in read_binding_change_journals(root)
                         if row["change_id"] == change_id), None)
        captured = _plain_change_evidence(expected)
        value = _plain_change_evidence(commit)
        order = ENABLE_JOURNAL_PHASES.index(phase)
        if previous is None:
            if phase != "enabling":
                raise ValueError
        elif (previous["version"] != ENABLE_JOURNAL_VERSION or previous["expected"] != captured
                or previous["commit"] != value or not journal_unresolved(previous)
                # Aborted only while nothing can have been published.
                or phase == "aborted" and previous["phase"] != "enabling"
                or phase != "aborted" and order <= ENABLE_JOURNAL_PHASES.index(previous["phase"])):
            raise ValueError
        row = {"version": ENABLE_JOURNAL_VERSION, "phase": phase,
               "canonical_root": str(canonical_root(root)), "change_id": change_id,
               "expected": captured, "commit": value, "evidence": []}
        _validate_enable_journal(row)
        encoded = json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=".binding-change-", delete=False) as stream:
            temporary = Path(stream.name)
            if stream.write(encoded) != len(encoded):
                raise OSError
            stream.flush()
            os.fsync(stream.fileno())
        _replace_journal(temporary, path)
        _fsync_directory(path.parent)
        return row
    except Exception:
        raise BindingChangeError("binding_journal_unavailable") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def completed_activation(records: tuple[Mapping, ...], *, persistent_id: str,
                         identity_revision: int) -> Mapping | None:
    """The settled change that produced THIS project identity revision, if any.

    The project config is the commit point and every committed change advances
    its revision, so the record whose commit equals the current (ID, R) is the
    one that published it; journal order and mtimes are never consulted. An
    unresolved record refuses (recovery_required) before anything else; two
    completed records claiming the same (ID, R) are ambiguous and refuse.
    Aborted Enable records published nothing and are ignored.
    """
    if any(journal_unresolved(row) for row in records):
        raise BindingChangeError("recovery_required")
    matching = [row for row in records if row["phase"] == "completed"
                and row["commit"]["persistent_id"] == persistent_id
                and row["commit"]["identity_revision"] == identity_revision]
    if len(matching) > 1:
        raise BindingChangeError("binding_journal_unavailable")
    return matching[0] if matching else None


def binding_change_evidence_writer(
    root: str | Path, change_id: str, expected: Mapping[str, object],
) -> Callable[[dict[str, object]], None]:
    """Private nonsecret I5 recovery evidence (integration plan §3.5).

    Constructing this seam is read-only. The service calls it before destructive
    cleanup and for ambiguous old-target sends. I6 will classify these versioned
    records at startup; they are never consent, queue replay or activation proof.
    Earlier evidence is retained even when a later snapshot has an empty queue.
    """
    import json
    import os
    import tempfile
    from pathlib import Path

    from ..core.application_config import _fsync_directory
    from ..core.paths import user_config_dir
    from ..core.project_scope import canonical_root
    from .bindings import root_key

    if (not isinstance(change_id, str) or len(change_id) != 32
            or any(c not in "0123456789abcdef" for c in change_id)):
        raise BindingChangeError("invalid_change")
    canonical = str(canonical_root(root))
    path = user_config_dir() / "remote-changes" / root_key(canonical) / (change_id + ".json")

    def plain(value: object) -> object:
        if isinstance(value, Mapping):
            # Credential revision/source are metadata; actual credentials and
            # key hashes are explicitly outside this journal's schema.
            if any(str(k).lower() in {
                "token", "password", "authorization", "key", "key_hash", "token_hash",
                "content", "markdown", "yjs",
            } for k in value):
                raise ValueError("nonsecret_evidence_required")
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [plain(v) for v in value]
        if value is None or type(value) in (str, int, bool):
            return value
        raise ValueError("invalid_evidence")

    try:
        captured = plain(expected)
        if len(json.dumps(captured)) > 65536:
            raise ValueError("invalid_evidence")
    except (TypeError, ValueError, RecursionError):
        raise BindingChangeError("invalid_change") from None

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_journal_field")
            result[key] = value
        return result

    def write(snapshot: dict[str, object]) -> None:
        temporary: Path | None = None
        try:
            evidence = plain(snapshot)
            required = {"target", "generation", "cursor", "space_instance_id", "pending",
                        "live", "rooms", "retained", "reason"}
            if (not isinstance(evidence, dict) or not required <= set(evidence)
                    or not set(evidence) <= required | {"quiescing", "fenced"}
                    or any(type(evidence[key]) is not bool
                           for key in ("quiescing", "fenced") if key in evidence)):
                raise ValueError("invalid_evidence")
            journal = {
                "version": 1, "phase": "draining", "canonical_root": canonical,
                "change_id": change_id, "expected": captured, "evidence": [],
            }
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                if path.is_symlink():
                    raise ValueError("unavailable_journal")
            else:
                if len(content) > 4 * 1024 * 1024:
                    raise ValueError("invalid_journal")
                previous = json.loads(content.decode("utf-8"), object_pairs_hook=unique)
                if isinstance(previous, dict) and previous.get("version") == 2:
                    # The I6 coordinator has already published a recovery fence.
                    # Shutdown may retain further old-target evidence but may
                    # never downgrade its phase or remove activation history.
                    validated = next(row for row in read_binding_change_journals(root)
                                     if row["change_id"] == change_id)
                    if validated["expected"] != captured:
                        raise ValueError("invalid_journal")
                    journal = validated
                    if evidence not in journal["evidence"]:
                        journal["evidence"].append(evidence)
                    encoded = json.dumps(journal, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    if len(encoded) > 4 * 1024 * 1024:
                        raise ValueError("journal_capacity")
                    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                            prefix=".binding-change-", delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    _replace_journal(temporary, path)
                    _fsync_directory(path.parent)
                    return
                if (not isinstance(previous, dict) or previous.keys() != journal.keys()
                        or type(previous["version"]) is not int or previous["version"] != 1
                        or previous["phase"] != "draining"
                        or previous["canonical_root"] != canonical or previous["change_id"] != change_id
                        or previous["expected"] != captured or not isinstance(previous["evidence"], list)):
                    raise ValueError("invalid_journal")
                journal = previous
            if evidence not in journal["evidence"]:
                journal["evidence"].append(evidence)
            encoded = json.dumps(journal, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > 4 * 1024 * 1024 or len(journal["evidence"]) > 1024:
                raise ValueError("journal_capacity")
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                             prefix=".binding-change-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_journal(temporary, path)
            _fsync_directory(path.parent)
        except Exception:
            raise BindingChangeError("binding_journal_unavailable") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    return write


@dataclass
class _BindingChange:
    change_id: str
    expected: Mapping[str, object]
    generation: int
    expires_at: float
    deadline: float
    phase: str = "prepared"
    reason: str | None = None
    fenced: bool = False
    # Keep lease objects even after a clean editor close: close is not an ACK.
    editors: dict[str, _EditorLease] = field(default_factory=dict)
    acks: dict[str, tuple[int, int]] = field(default_factory=dict)
    running: bool = False
    cancel_pending: bool = False
    waiter: asyncio.Task[object] | None = None
    expiry: asyncio.TimerHandle | None = None
    result: dict[str, object] | None = None


@dataclass(frozen=True)
class EditorSave:
    """In-process capability captured BEFORE awaiting a DocStore write.

    Never accept this from JSON. A successful write's continuation consumes it;
    failures/cancellation consume it through ``fail_editor_save`` instead.
    """

    editor_id: str
    generation: int
    version: int
    revision: int
    sequence: int
    _owner: object = field(repr=False)


@dataclass
class _EditorLease:
    editor_id: str
    document_path: str
    client_instance_id: str
    backend_epoch: str
    generation: int
    expires_at: float
    deadline: float
    sid: str | None = None
    version: int = 0
    minimum_save_version: int = 0
    revision: int = 0
    dirty: bool = True
    confirmed: bool = False
    last_saved_version: int | None = None
    saved_client_version: int | None = None
    saved_revision: int | None = None
    saves: dict[int, EditorSave] = field(default_factory=dict)
    overlapping: set[int] = field(default_factory=set)
    materialized_check: Callable[[], bool] | None = field(default=None, repr=False)
    initial_materialization: bool = True


class MutationAdmission:
    """Logical ownership through a mutation's *whole* continuation.

    Unlike the short critical lock this can be handed to the folder-mending
    task. Publishing events must happen outside ``critical``. Release is
    synchronous, so cancellation cannot strand an admission in cleanup.
    """

    def __init__(
        self, owner: BindingTransitions, generation: int,
        http_check: Callable[[], None] | None = None,
    ) -> None:
        self._owner = owner
        self.generation = generation
        self._released = False
        self._critical = False
        self._http_check = http_check

    @asynccontextmanager
    async def critical(self) -> AsyncIterator[None]:
        if self._released:
            raise RuntimeError("mutation_admission_released")
        async with self._owner._mutation_lock:
            if self._released:
                raise RuntimeError("mutation_admission_released")
            self._owner._require_generation(self.generation)
            if self._http_check is not None:
                # The lock may have waited across heartbeat expiry, room leave
                # or a newer editor version. HTTP preflight alone is not enough.
                token = self._owner._validating_admission.set(True)
                try:
                    self._http_check()
                finally:
                    self._owner._validating_admission.reset(token)
                # Once the FS phase starts, folder link-mending remains one
                # admitted continuation (VS-168), even if the lease later expires.
                self._http_check = None
            self._critical = True
            try:
                yield
            finally:
                self._critical = False

    def release(self) -> None:
        if self._released:
            return
        if self._critical:
            raise RuntimeError("mutation_admission_in_critical_section")
        self._released = True
        self._owner._active -= 1
        if not self._owner._active:
            self._owner._idle.set()

    @contextmanager
    def publishing(self) -> Iterator[None]:
        """Callbacks may mutate, but cannot wait for their own continuation to finish."""
        token = self._owner._publishing.set(self)
        try:
            yield
        finally:
            self._owner._publishing.reset(token)


class SnapshotFence:
    """A generation may change only with no admitted mutation remaining.

    DocStore layers the Markdown fingerprint recheck on top of this fence.
    It is a transient local capability, never activation/consent evidence.
    """

    def __init__(self, owner: BindingTransitions) -> None:
        self._owner = owner
        self.generation = owner.generation
        self._open = True
        self._advanced = False

    def advance_generation(self) -> int:
        if not self._open or self._advanced:
            raise RuntimeError("snapshot_fence_closed")
        self._owner._require_generation(self.generation)
        self._owner._generation = next_binding_generation(self.generation)
        self._advanced = True
        return self._owner.generation


class BindingTransitions:
    """Minimal state for one resident Ctx, independent of paths/other roots.

    No automatic generation adoption for explicit callers: their captured G
    is checked at admission and again inside the filesystem critical section.
    ``None`` remains the in-process/legacy DocStore contract. HTTP callers use
    ``require_http`` first; they must not infer in-process privileges from it.
    """

    def __init__(
        self, *, backend_epoch: str | None = None, lease_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("invalid_editor_lease_seconds")
        self.backend_epoch = backend_epoch if backend_epoch is not None else uuid4().hex
        if not isinstance(self.backend_epoch, str) or not self.backend_epoch:
            raise ValueError("invalid_backend_epoch")
        # Only a future trusted coordinator may set this. GET, editor registration,
        # HTTP/WS capability announcements and ordinary generation bumps NEVER do.
        self.activated = False
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._monotonic = monotonic
        self._editors: dict[str, _EditorLease] = {}
        self._sessions: dict[str, tuple[str, int, str | None]] = {}
        self._clients: dict[str, bool] = {}
        self._client_inventory: Callable[[], bool | int] | None = None
        self._save_sequence = 0
        self._validating_admission: ContextVar[bool] = ContextVar(
            "doc_admitted_http_validation", default=False,
        )
        self._pending_editor_save: ContextVar[EditorSave | None] = ContextVar(
            "doc_http_editor_save", default=None,
        )
        self._pending_reconciliation: ContextVar[Callable[[], None] | None] = ContextVar(
            "doc_exact_pending_reconciliation", default=None,
        )
        self._change: _BindingChange | None = None
        self._change_signal = asyncio.Event()
        self._begin_quiesce: Callable[[], None] | None = None
        self._drain_change: Callable[[str, float], Awaitable[object]] | None = None
        self._cancel_quiesce: Callable[..., None] | None = None
        self._service_fenced: Callable[[], bool] | None = None
        self._change_event: Callable[[dict[str, object]], Awaitable[None]] | None = None
        self._pending_http_check: ContextVar[Callable[[], None] | None] = ContextVar(
            "doc_http_mutation_check", default=None,
        )
        self.identity_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._generation = 0
        self._snapshot_pending = False
        self._snapshot_scope: ContextVar[SnapshotFence | None] = ContextVar(
            "doc_final_snapshot", default=None
        )
        self._publishing: ContextVar[MutationAdmission | None] = ContextVar(
            "doc_mutation_publishing", default=None
        )
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        # I11: Ctx-owned, memory-only reconcile previews (never durable/shared).
        self.reconcile_previews = ReconcilePreviews(clock=clock, monotonic=monotonic)

    @staticmethod
    def _document_path(path: str) -> str:
        # Lexical validation only: registration never opens/writes a document or
        # follows a root supplied by a client. The bound DocStore checks disk paths.
        if not isinstance(path, str) or not path or "\\" in path or ":" in path:
            raise EditorLeaseError("invalid_editor")
        parts = path.split("/")
        if any(part in {"", ".", ".."} or any(ord(c) < 32 for c in part) for part in parts):
            raise EditorLeaseError("invalid_editor")
        parsed = PurePosixPath(path)
        if not parsed.suffix:
            return path + ".md"
        if parsed.suffix.lower() not in {".md", ".markdown", ".txt"}:
            raise EditorLeaseError("invalid_editor")
        return path

    def _require_epoch(self, backend_epoch: str | None) -> None:
        if backend_epoch is None:
            raise EditorLeaseError("reload_required")
        if backend_epoch != self.backend_epoch:
            raise EditorLeaseError("stale_backend_epoch")

    def _require_captured(self, backend_epoch: str | None, generation: int | None) -> None:
        self._require_epoch(backend_epoch)
        if generation is None:
            raise EditorLeaseError("reload_required")
        self._require_generation(generation)

    def _require_session(
        self, sid: str, document_path: str, generation: int, editor_id: str | None = None,
    ) -> None:
        session = self._sessions.get(sid)
        if session is None or session[:2] != (document_path, generation):
            raise EditorLeaseError("unknown_sid")
        if session[2] is not None and session[2] != editor_id:
            raise EditorLeaseError("unknown_sid")

    def _editor(
        self, editor_id: str, backend_epoch: str | None, expected_binding_generation: int | None,
        *, document_path: str | None = None, client_instance_id: str | None = None,
        sid: str | None = None,
    ) -> _EditorLease:
        self._require_captured(backend_epoch, expected_binding_generation)
        lease = self._editors.get(editor_id)
        if lease is None:
            raise EditorLeaseError("unknown_editor")
        if lease.backend_epoch != backend_epoch or lease.generation != expected_binding_generation:
            raise EditorLeaseError("editor_mismatch")
        if document_path is not None and self._document_path(document_path) != lease.document_path:
            raise EditorLeaseError("editor_mismatch")
        if client_instance_id is not None and client_instance_id != lease.client_instance_id:
            raise EditorLeaseError("editor_mismatch")
        if self._monotonic() >= lease.deadline:
            raise EditorLeaseError("editor_lease_expired")
        if lease.sid != sid:
            raise EditorLeaseError("unknown_sid")
        if sid is not None:
            self._require_session(sid, lease.document_path, lease.generation, lease.editor_id)
        return lease

    def _editor_dto(self, lease: _EditorLease) -> dict[str, object]:
        return {
            "editor_id": lease.editor_id, "backend_epoch": lease.backend_epoch,
            "binding_generation": lease.generation, "document_path": lease.document_path,
            "client_instance_id": lease.client_instance_id, "lease_expires_at": lease.expires_at,
            "last_saved_version": lease.last_saved_version, "version": lease.version,
            "dirty": lease.dirty, "confirmed": lease.confirmed,
            "revision": lease.revision,
        }

    @staticmethod
    def _materialized_valid(lease: _EditorLease) -> bool:
        try:
            return lease.materialized_check is None or lease.materialized_check() is True
        except Exception:
            return False

    def materialization_lease(
        self, editor_id: str, backend_epoch: str, expected_binding_generation: int, *,
        document_path: str, client_instance_id: str, sid: str, version: int, revision: int,
    ) -> _EditorLease:
        """Exact read-only CAS for the I8 bound service; never adopt a newer v/G."""
        lease = self._editor(editor_id, backend_epoch, expected_binding_generation,
                             document_path=document_path, client_instance_id=client_instance_id, sid=sid)
        if self._snapshot_pending or self.active_mutations:
            raise MutationBusy()
        if self.binding_quiescing and not self._final_editor_allowed(editor_id):
            raise MutationBusy()
        if (type(version) is not int or type(revision) is not int
                or version != lease.version or revision != lease.revision
                or version < lease.minimum_save_version):
            raise EditorLeaseError("stale_editor_version")
        if lease.saves or lease.overlapping:
            raise EditorLeaseError("pending_edits")
        return lease

    def confirm_materialized(
        self, editor_id: str, backend_epoch: str, expected_binding_generation: int, *,
        check: Callable[[], bool], **captured: object,
    ) -> dict[str, object]:
        """Service-only proof of this lease's snapshot, not a peer's save receipt.

        The service supplies a fresh content+room/save validator, retained for
        ACK/close/sealing checks (runtime integration plan §3.3–3.4/I8).
        """
        lease = self.materialization_lease(editor_id, backend_epoch, expected_binding_generation, **captured)
        if check() is not True:
            raise EditorLeaseError("pending_edits")
        # A custom reader may invalidate a lease; repeat the CAS after reading.
        self.materialization_lease(editor_id, backend_epoch, expected_binding_generation, **captured)
        self._save_sequence += 1
        lease.last_saved_version = self._save_sequence
        lease.saved_client_version, lease.saved_revision = lease.version, lease.revision
        lease.dirty, lease.confirmed = False, True
        lease.materialized_check = check
        self._change_signal.set()
        return self._editor_dto(lease)

    def invalidate_room_materialization(self, document_path: str) -> None:
        """Trusted relay hook: room updates invalidate every local peer, not just scribe."""
        for lease in self._editors.values():
            if lease.sid is not None and lease.document_path == document_path:
                lease.revision += 1
                lease.dirty, lease.confirmed = True, False
                lease.materialized_check = None
                lease.initial_materialization = False
        self._change_signal.set()

    def confirm_loaded_server_version(
        self, editor_id: str, backend_epoch: str, expected_binding_generation: int, *,
        docs: DocStore, document_path: str | None = None,
        client_instance_id: str | None = None, sid: str | None = None,
    ) -> dict[str, object]:
        """Confirm an untouched ordinary editor from a fresh bound server read.

        Call immediately after registration, before returning the initial document
        to the editor. The returned ``loaded_document`` is that exact server read;
        serve it rather than accepting client content/mtime/``saved`` booleans as
        evidence. Reading and issuing its receipt never writes Markdown (§16.4).
        A later dirty announcement/save makes this initial-only seam unusable.
        A collab room's current text is NOT proved by reading its Markdown.
        """
        lease = self._editor(
            editor_id, backend_epoch, expected_binding_generation, document_path=document_path,
            client_instance_id=client_instance_id, sid=sid,
        )
        if self._snapshot_pending:
            raise MutationBusy()
        if (
            docs.transitions is not self or lease.sid is not None or lease.revision != 0
            or lease.version != 0 or lease.last_saved_version is not None or lease.saves
        ):
            raise EditorLeaseError("pending_edits")
        loaded = docs.read(lease.document_path)
        if (
            loaded.get("path") != lease.document_path
            or not isinstance(loaded.get("content"), str)
            or not isinstance(loaded.get("updated_at"), str)
        ):
            raise EditorLeaseError("editor_mismatch")
        # DocStore.read is synchronous, but recheck expiry and local ownership
        # before issuing evidence; a custom bound reader may do other work.
        if (
            self._monotonic() >= lease.deadline or lease.generation != self._generation
            or lease.revision != 0 or lease.saves
        ):
            raise EditorLeaseError("pending_edits")
        self._save_sequence += 1
        lease.last_saved_version = self._save_sequence
        lease.saved_client_version = lease.version
        lease.saved_revision = lease.revision
        lease.dirty, lease.confirmed = False, True
        return {**self._editor_dto(lease), "loaded_document": loaded}

    def register_editor(
        self, document_path: str, client_instance_id: str, backend_epoch: str,
        expected_binding_generation: int, sid: str | None = None,
    ) -> dict[str, object]:
        """Register an unconfirmed lease, not activation or evidence of a save."""
        self._require_captured(backend_epoch, expected_binding_generation)
        if self._snapshot_pending or self.binding_quiescing:
            raise MutationBusy()
        if not isinstance(client_instance_id, str) or not client_instance_id or len(client_instance_id) > 256:
            raise EditorLeaseError("invalid_editor")
        path = self._document_path(document_path)
        if sid is not None:
            self._require_session(sid, path, expected_binding_generation)
        lease = _EditorLease(
            uuid4().hex, path, client_instance_id, backend_epoch, expected_binding_generation,
            self._clock() + self._lease_seconds, self._monotonic() + self._lease_seconds, sid,
        )
        self._editors[lease.editor_id] = lease
        if sid is not None:
            self._sessions[sid] = (path, expected_binding_generation, lease.editor_id)
        return self._editor_dto(lease)

    @staticmethod
    def _version(lease: _EditorLease, version: int | None) -> None:
        if version is None:
            return
        if type(version) is not int or version < lease.version:
            raise EditorLeaseError("stale_editor_version")
        if version > lease.version:
            lease.initial_materialization = False
            lease.version = version
            lease.revision += 1
            lease.dirty = True
            lease.confirmed = False

    def heartbeat_editor(
        self, editor_id: str, backend_epoch: str, expected_binding_generation: int, *,
        document_path: str | None = None, client_instance_id: str | None = None,
        sid: str | None = None, dirty: bool | None = None, version: int | None = None,
        last_saved_version: int | None = None,
    ) -> dict[str, object]:
        lease = self._editor(
            editor_id, backend_epoch, expected_binding_generation,
            document_path=document_path, client_instance_id=client_instance_id, sid=sid,
        )
        self._version(lease, version)
        if dirty is True:
            lease.initial_materialization = False
            # A new dirty announcement invalidates any save already in flight,
            # even if a buggy/old client reused its local version counter.
            lease.revision += 1
            if version is None or (
                lease.saved_client_version is not None and version <= lease.saved_client_version
            ):
                # An unversioned/reused-version dirty notification cannot let
                # an older callback re-label its content with the current v.
                lease.minimum_save_version = max(lease.minimum_save_version, lease.version + 1)
            lease.dirty = True
            lease.confirmed = False
        elif dirty is False and (
            type(last_saved_version) is int and last_saved_version == lease.last_saved_version
            and version is not None and version == lease.saved_client_version
            and lease.saved_revision == lease.revision and not lease.saves
            and self._materialized_valid(lease)
        ):
            lease.dirty = False
            lease.confirmed = True
        # Expiry cannot be revived, and client booleans cannot create evidence.
        lease.expires_at = self._clock() + self._lease_seconds
        lease.deadline = self._monotonic() + self._lease_seconds
        self._change_signal.set()
        return self._editor_dto(lease)

    def close_editor(
        self, editor_id: str, backend_epoch: str, expected_binding_generation: int, *,
        document_path: str | None = None, client_instance_id: str | None = None,
        sid: str | None = None, last_saved_version: int | None = None,
        version: int | None = None, dirty: bool | None = None,
    ) -> dict[str, object]:
        self.heartbeat_editor(
            editor_id, backend_epoch, expected_binding_generation, document_path=document_path,
            client_instance_id=client_instance_id, sid=sid, dirty=dirty, version=version,
            last_saved_version=last_saved_version,
        )
        lease = self._editors[editor_id]
        if (
            lease.dirty or not lease.confirmed or lease.saves
            or not self._materialized_valid(lease)
            or type(last_saved_version) is not int or last_saved_version != lease.last_saved_version
        ):
            raise EditorLeaseError("pending_edits")
        del self._editors[editor_id]
        self._change_signal.set()
        return {**self._editor_dto(lease), "closed": True}

    def require_http(
        self, expected_binding_generation: int | None = None, *, backend_epoch: str | None = None,
        editor_id: str | None = None, document_path: str | None = None,
        client_instance_id: str | None = None, sid: str | None = None,
        require_editor: bool = False,
    ) -> int | None:
        """Validate captured HTTP identity before entering the existing DocStore gate."""
        if self.activated or editor_id is not None or require_editor:
            self._require_captured(backend_epoch, expected_binding_generation)
        else:
            if backend_epoch is not None:
                self._require_epoch(backend_epoch)
            self._require_generation(expected_binding_generation)
        if self._snapshot_pending and not self._validating_admission.get():
            raise MutationBusy()
        if self.binding_quiescing and not self._validating_admission.get() and (
            editor_id is None or not self._final_editor_allowed(editor_id)
        ):
            raise MutationBusy()
        if editor_id is None:
            if require_editor or (sid is not None and self.activated):
                raise EditorLeaseError("unknown_editor")
            if sid is not None:
                if document_path is None:
                    raise EditorLeaseError("unknown_sid")
                self._require_session(sid, self._document_path(document_path), self._generation)
                # A real, server-registered legacy room is still usable before
                # activation, but never falls back to an unscoped plain save.
                return self._generation
        else:
            if document_path is None:
                raise EditorLeaseError("editor_mismatch")
            self._editor(
                editor_id, backend_epoch, expected_binding_generation, document_path=document_path,
                client_instance_id=client_instance_id, sid=sid,
            )
        return expected_binding_generation

    @contextmanager
    def http_mutation_guard(
        self, check: Callable[[], object], *, save: EditorSave | None = None,
        before_mutation: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        """Explicit one-admission HTTP scope, revalidated at the FS boundary.

        A read/join/preflight never leaves ambient HTTP privileges behind. Admit
        consumes the callback before event publication, so reentrant bound local
        writes do not inherit an editor's lease or deadlock behind its request.
        """
        def validate() -> None:
            check()
            if save is not None:
                self.validate_editor_save(save)
            if before_mutation is not None:
                before_mutation()

        token = self._pending_http_check.set(validate)
        save_token = self._pending_editor_save.set(save)
        try:
            yield
        finally:
            self._pending_editor_save.reset(save_token)
            self._pending_http_check.reset(token)

    @contextmanager
    def reconciliation_mutation_guard(self, check: Callable[[], None]) -> Iterator[None]:
        """One trusted old-target NACK mutation, never general remote admission.

        The service must validate its exact pending ID/target/G before requesting
        this capability and again at the filesystem boundary. Like HTTP saves,
        admission consumes it before event publication, so reentrant writers do
        not inherit privileges. No lock is held across reconciliation or ACKs.
        """
        token = self._pending_reconciliation.set(check)
        try:
            with self.http_mutation_guard(check):
                yield
        finally:
            self._pending_reconciliation.reset(token)

    def begin_editor_save(
        self, editor_id: str, backend_epoch: str, expected_binding_generation: int, *,
        document_path: str | None = None, client_instance_id: str | None = None,
        sid: str | None = None, version: int | None = None,
    ) -> EditorSave:
        lease = self._editor(
            editor_id, backend_epoch, expected_binding_generation, document_path=document_path,
            client_instance_id=client_instance_id, sid=sid,
        )
        if self._snapshot_pending:
            raise MutationBusy()
        if self.binding_quiescing and not self._final_editor_allowed(editor_id):
            raise MutationBusy()
        if version is None:
            # Never adopt the lease's latest version for a delayed callback.
            # Legacy REST has no editor and does not enter this method.
            raise EditorLeaseError("reload_required")
        if type(version) is not int or version < lease.minimum_save_version:
            raise EditorLeaseError("stale_editor_version")
        self._version(lease, version)
        lease.materialized_check = None
        lease.initial_materialization = False
        lease.revision += 1
        lease.dirty, lease.confirmed = True, False
        self._save_sequence += 1
        save = EditorSave(
            editor_id, lease.generation, lease.version, lease.revision, self._save_sequence, self,
        )
        if lease.saves:
            # DocStore publishes outside its lock. Completion ordering alone
            # cannot prove disk ordering for overlapping callbacks (§5.3).
            lease.overlapping.update(lease.saves)
            lease.overlapping.add(save.sequence)
        lease.saves[save.sequence] = save
        return save

    def validate_editor_save(self, save: EditorSave) -> None:
        if not isinstance(save, EditorSave) or save._owner is not self:
            raise EditorLeaseError("editor_mismatch")
        lease = self._editors.get(save.editor_id)
        if lease is None or lease.saves.get(save.sequence) is not save:
            raise EditorLeaseError("unknown_editor")
        self._editor(lease.editor_id, lease.backend_epoch, save.generation,
                     document_path=lease.document_path, client_instance_id=lease.client_instance_id,
                     sid=lease.sid)
        if lease.version != save.version or lease.revision != save.revision:
            raise EditorLeaseError("stale_editor_version")

    def _consume_save(self, save: EditorSave) -> tuple[_EditorLease, bool]:
        if not isinstance(save, EditorSave) or save._owner is not self:
            raise EditorLeaseError("editor_mismatch")
        lease = self._editors.get(save.editor_id)
        if lease is None or lease.saves.get(save.sequence) is not save:
            raise EditorLeaseError("unknown_editor")
        del lease.saves[save.sequence]
        overlapped = save.sequence in lease.overlapping
        lease.overlapping.discard(save.sequence)
        return lease, overlapped

    def record_saved(self, save: EditorSave) -> dict[str, object]:
        """Record only after actual successful DocStore.write; never client asserted."""
        lease, overlapped = self._consume_save(save)
        lease.last_saved_version = save.sequence
        lease.saved_client_version = save.version
        clean = (
            not overlapped and not lease.saves and lease.revision == save.revision
            and lease.version == save.version and lease.generation == self._generation
            and self._monotonic() < lease.deadline
        )
        # Do not let a later client heartbeat launder an ambiguous overlapping
        # write into a clean acknowledgement by echoing its receipt.
        lease.saved_revision = save.revision if clean else None
        lease.dirty, lease.confirmed = not clean, clean
        self._change_signal.set()
        return self._editor_dto(lease)

    def fail_editor_save(self, save: EditorSave) -> None:
        lease, _ = self._consume_save(save)
        lease.dirty, lease.confirmed = True, False
        # Invalidate all preceding evidence, including a success whose publish
        # continuation may have completed before this failed/cancelled callback.
        lease.revision += 1
        self._change_signal.set()

    def register_collab_session(
        self, sid: str, document_path: str, expected_binding_generation: int, *,
        editor_id: str | None = None,
    ) -> None:
        """Trusted bound WS/service hook, never a client-provided inventory row."""
        self._require_generation(expected_binding_generation)
        if self._snapshot_pending or self.binding_quiescing:
            raise MutationBusy()
        if not isinstance(sid, str) or not sid or sid in self._sessions:
            raise EditorLeaseError("unknown_sid")
        path = self._document_path(document_path)
        if editor_id is not None:
            lease = self._editors.get(editor_id)
            if lease is None or lease.document_path != path or lease.generation != expected_binding_generation:
                raise EditorLeaseError("editor_mismatch")
            if self._monotonic() >= lease.deadline or lease.sid is not None:
                raise EditorLeaseError("editor_lease_expired" if self._monotonic() >= lease.deadline else "unknown_sid")
            lease.sid = sid
            # Server Markdown at ordinary open is not the room's RAM/Yjs text.
            lease.revision += 1
            lease.dirty, lease.confirmed = True, False
        self._sessions[sid] = (path, expected_binding_generation, editor_id)

    def close_collab_session(self, sid: str) -> None:
        session = self._sessions.pop(sid, None)
        if session is not None:
            lease = self._editors.get(session[2])
            if lease is not None and not (
                self._change is not None and self._change.phase == "sealing"
                and self._ack_matches(self._change, lease)
            ):
                lease.revision += 1
                lease.dirty, lease.confirmed = True, False
        # Keep the lease/sid and invalidate clean evidence: disconnect/reset is
        # NOT proof that every peer/scribe's RAM text was materialised to disk.

    def register_client(self, client_id: str, *, capable: bool = False) -> None:
        """Inventory hook. Call for every affected Ctx on legacy selection changes."""
        if not isinstance(client_id, str) or not client_id:
            raise EditorLeaseError("invalid_editor")
        if self._clients.get(client_id) is not (capable is True):
            self._clients[client_id] = capable is True
            self._change_signal.set()

    def close_client(self, client_id: str, *, confirmed_clean: bool = False) -> None:
        if confirmed_clean:
            self._clients.pop(client_id, None)
        # Silence/close without positive evidence retains an unknown tombstone.

    def set_client_inventory(self, inventory: Callable[[], bool | int]) -> None:
        """Install a trusted synchronous live-WS inventory, not a client flag.

        True/a positive count means unknown live clients affect this Ctx;
        False/integer zero is the only negative result accepted. The callback
        should close over the bound Ctx
        and shared browser-socket registry so new contexts/selection changes
        cannot miss existing legacy browser sockets. An error/invalid result
        fails closed. It must never open roots or perform network operations.
        """
        if not callable(inventory):
            raise ValueError("invalid_client_inventory")
        self._client_inventory = inventory

    def set_change_callbacks(
        self, *, begin_quiesce: Callable[[], None],
        drain: Callable[[str, float], Awaitable[object]],
        cancel_quiesce: Callable[..., None],
        event: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        fence_status: Callable[[], bool] | None = None,
    ) -> None:
        """Bind the *same Ctx* service/event owners, never a browser selection.

        ``begin_quiesce()`` and ``cancel_quiesce(fenced=bool)`` are synchronous
        admission hooks. ``await drain(change_id, timeout)`` must return a mapping
        with ``drained is True`` only after exact pending ACK/reconciliation,
        leave, socket close and runner join. Empty pending/settled is NOT proof.
        ``fence_status()`` observes the service's destructive-fence latch, including
        cancellation between leave and runner join; production adapters must bind it.
        Event is an async Ctx-only publisher taking the DTO. No callback is called
        with the identity, DocStore mutation or transport locks held.
        """
        if self.binding_quiescing:
            raise BindingChangeError("change_in_progress")
        if not all(callable(callback) for callback in (begin_quiesce, drain, cancel_quiesce)):
            raise ValueError("invalid_change_callbacks")
        if event is not None and not callable(event):
            raise ValueError("invalid_change_callbacks")
        self._begin_quiesce, self._drain_change = begin_quiesce, drain
        if fence_status is not None and not callable(fence_status):
            raise ValueError("invalid_change_callbacks")
        self._cancel_quiesce, self._change_event = cancel_quiesce, event
        self._service_fenced = fence_status

    @staticmethod
    def _freeze_evidence(value: object, depth: int = 0) -> object:
        # Bound both allocation and nesting; never retain caller-owned containers.
        if depth > 16:
            raise BindingChangeError("invalid_change")
        if value is None or type(value) in (str, bool, int):
            if isinstance(value, str) and len(value) > 8192:
                raise BindingChangeError("invalid_change")
            return value
        if isinstance(value, Mapping) and len(value) <= 128:
            if not all(isinstance(key, str) and len(key) <= 256 for key in value):
                raise BindingChangeError("invalid_change")
            return MappingProxyType({
                key: BindingTransitions._freeze_evidence(item, depth + 1)
                for key, item in value.items()
            })
        if isinstance(value, (list, tuple)) and len(value) <= 128:
            return tuple(BindingTransitions._freeze_evidence(item, depth + 1) for item in value)
        raise BindingChangeError("invalid_change")

    def _expire_change(self) -> None:
        change = self._change
        if change is not None and change.phase not in {"cancelled", "expired", "paused", "committed"}:
            if self._monotonic() >= change.deadline:
                self.cancel_change(change.change_id, _expired=True)

    @property
    def binding_quiescing(self) -> bool:
        """Logical admission fence, including blocked and post-fence paused states."""
        self._expire_change()
        return self._change is not None and self._change.phase in {
            "draining", "sealing", "blocked", "ready", "paused",
        }

    def _final_editor_allowed(self, editor_id: str) -> bool:
        change = self._change
        return bool(
            change is not None and change.phase in {"draining", "blocked"}
            and editor_id in change.editors and editor_id not in change.acks
            and change.generation == self._generation
        )

    def _get_change(self, change_id: str) -> _BindingChange:
        self._expire_change()
        if self._change is None or self._change.change_id != change_id:
            raise BindingChangeError("unknown_change")
        return self._change

    def expected_change_evidence(self, change_id: str) -> Mapping[str, object]:
        """Immutable trusted-owner evidence, NOT a commit or durable recovery journal."""
        return self._get_change(change_id).expected

    def prepare_change(self, expected_evidence: Mapping[str, object]) -> dict[str, object]:
        """Read-only I5 prepare, after the API owner validates revision/fingerprints.

        Evidence contains nonsecret expected values (R/config bytes/private/profile/
        catalog/root scope). This class owns only epoch/G CAS; it never reads config,
        reserves a target, connects, writes a journal, changes identity or Enables.
        One live change is retained per Ctx; TTL is exactly five monotonic minutes.
        """
        self._expire_change()
        if self._change is not None and (
            self._change.running or self._change.phase not in {"cancelled", "expired", "committed"}
        ):
            raise BindingChangeError("change_in_progress")
        if not isinstance(expected_evidence, Mapping):
            raise BindingChangeError("invalid_change")
        # A total encoded-size bound prevents exponentially wide nested input.
        import json
        try:
            if len(json.dumps(expected_evidence, allow_nan=False)) > 65536:
                raise BindingChangeError("invalid_change")
        except (TypeError, ValueError, RecursionError):
            raise BindingChangeError("invalid_change") from None
        evidence = self._freeze_evidence(expected_evidence)
        assert isinstance(evidence, Mapping)
        generation = evidence.get("binding_generation", evidence.get("expected_generation"))
        if generation is not None:
            self._require_generation(generation)  # type: ignore[arg-type]
        epoch = evidence.get("backend_epoch")
        if epoch is not None:
            self._require_epoch(epoch)  # type: ignore[arg-type]
        change = _BindingChange(
            uuid4().hex, evidence, self._generation,
            self._clock() + 300.0, self._monotonic() + 300.0,
            editors=dict(self._editors),
        )
        self._change = change
        try:
            change.expiry = asyncio.get_running_loop().call_later(300.0, self._expire_change)
        except RuntimeError:
            # Synchronous unit owners still enforce expiry at every admission/read.
            pass
        return self._change_dto(change)

    def require_ready_commit(
        self, change_id: str, *, expected_revision: int, expected_generation: int,
        config_fingerprint: str, confirmation: Mapping[str, object],
    ) -> _BindingChange:
        """Check captured request evidence without silently adopting current R/G."""
        change = self._get_change(change_id)
        expected = change.expected
        observation = expected.get("observation", {})
        destination = expected.get("to", {})
        if (type(expected_revision) is not int
                or observation.get("identity_revision") != expected_revision
                or observation.get("config_fingerprint") != config_fingerprint
                or dict(confirmation) != {"url": destination.get("url"),
                                          "persistent_id": destination.get("persistent_id")}):
            raise BindingChangeError("confirmation_required")
        if change.phase == "committed":
            if expected_generation != change.generation + 1 or type(expected_generation) is not int:
                raise BindingChangeError("stale_binding")
            return change
        if (change.phase not in {"ready", "blocked"} or not change.fenced or change.running
                or change.phase == "blocked" and change.reason != "recovery_required"):
            raise BindingChangeError("change_not_ready")
        self._require_generation(expected_generation)
        self._require_epoch(expected.get("backend_epoch"))
        return change

    def complete_commit(self, change_id: str, generation: int | None = None) -> dict[str, object]:
        """Publish only after both stores and durable completion; no I/O/await."""
        change = self._change
        if change is None or change.change_id != change_id:
            raise BindingChangeError("unknown_change")
        if change.phase not in {"ready", "blocked"} or not change.fenced:
            raise BindingChangeError("change_not_ready")
        # Drain consumes oldG+1; commit consumes oldG+2. Both invalidate old
        # callbacks, but neither is an auth secret. Never adopt an arbitrary or
        # historical journal G (including a previous backend epoch's receipt).
        self._require_epoch(change.expected.get("backend_epoch"))
        self._require_generation(change.generation + 1)
        committed_generation = next_binding_generation(self._generation)
        if generation is not None and (type(generation) is not int
                                       or generation != committed_generation):
            raise BindingChangeError("stale_binding")
        self._generation = committed_generation
        self.observe_activated_model()
        change.phase, change.reason = "committed", None
        if change.expiry is not None:
            change.expiry.cancel()
        return self._change_dto(change)

    @staticmethod
    def _ack_matches(change: _BindingChange, lease: _EditorLease) -> bool:
        return (
            not lease.dirty and lease.confirmed and not lease.saves
            and type(lease.last_saved_version) is int
            and lease.saved_revision == lease.revision
            and lease.saved_client_version == lease.version
            and change.acks.get(lease.editor_id) == (lease.last_saved_version, lease.revision)
            and BindingTransitions._materialized_valid(lease)
        )

    def _unknown_change_clients(self) -> bool:
        if self._client_inventory is not None:
            try:
                result = self._client_inventory()
                if not (result is False or (type(result) is int and result == 0)):
                    return True
            except Exception:
                return True
        change = self._change
        for client_id, capable in self._clients.items():
            if not capable:
                return True
            # Capability alone never establishes inventory completeness. Only an
            # exact backend lease association can account for this client; the
            # separate missing-ACK check still requires every associated receipt.
            # WS IDs without that association stay blocked until I8 integration.
            if change is None or not any(
                lease.client_instance_id == client_id and lease.generation == change.generation
                for lease in change.editors.values()
            ):
                return True
        return False

    def _missing_change_acks(self, change: _BindingChange) -> list[str]:
        return [editor_id for editor_id, lease in change.editors.items()
                if not self._ack_matches(change, lease)]

    def room_drain_ready(self, sid: str, path: str, editor_id: str | None, generation: int) -> bool:
        """Every old room needs its own acknowledged backend editor (§3.3–3.4).

        A clean disk snapshot says nothing about an unleased room's RAM/Yjs.
        The service owner also checks this for rooms absent from our registry.
        """
        change = self._change
        if change is None or generation != change.generation or editor_id is None:
            return False
        lease = change.editors.get(editor_id)
        return bool(
            self._sessions.get(sid) == (path, generation, editor_id)
            and lease is not None and lease.sid == sid and lease.document_path == path
            and lease.generation == generation and self._ack_matches(change, lease)
        )

    def _unleased_change_rooms(self, change: _BindingChange) -> bool:
        return any(
            editor_id is None or editor_id not in change.editors
            or change.editors[editor_id].sid != sid
            or change.editors[editor_id].document_path != path
            or generation != change.generation
            for sid, (path, generation, editor_id) in self._sessions.items()
        )

    def _change_dto(self, change: _BindingChange) -> dict[str, object]:
        phase = "draining" if change.phase == "sealing" else change.phase
        return {
            "change_id": change.change_id, "expires_at": change.expires_at,
            "state": phase, "binding_generation": self._generation,
            "expected_generation": change.generation, "fenced": change.fenced,
            "required_editor_acks": list(change.editors),
            "missing_acks": self._missing_change_acks(change),
            "reason": change.reason,
        }

    def change_status(self, change_id: str) -> dict[str, object]:
        return self._change_dto(self._get_change(change_id))

    def editor_ack(
        self, change_id: str, editor_id: str, old_generation: int,
        last_saved_version: int, *, backend_epoch: str | None = None,
        client_instance_id: str | None = None,
    ) -> dict[str, object]:
        """Accept only the exact backend receipt; there is deliberately no saved bool."""
        change = self._get_change(change_id)
        if change.phase not in {"draining", "blocked"}:
            raise BindingChangeError("change_not_quiescing")
        self._require_generation(old_generation)
        if old_generation != change.generation:
            raise BindingChangeError("invalid_editor_ack")
        if backend_epoch is not None:
            self._require_epoch(backend_epoch)
        lease = change.editors.get(editor_id)
        if (lease is None or lease.generation != old_generation
                or (client_instance_id is not None and client_instance_id != lease.client_instance_id)):
            raise BindingChangeError("invalid_editor_ack")
        # Even a formerly clean lease expiring before ACK is unreachable, not proof.
        if self._monotonic() >= lease.deadline:
            raise EditorLeaseError("editor_lease_expired")
        if (type(last_saved_version) is not int or last_saved_version != lease.last_saved_version
                or lease.dirty or not lease.confirmed or lease.saves
                or lease.saved_revision != lease.revision
                or lease.saved_client_version != lease.version):
            raise BindingChangeError("invalid_editor_ack")
        if not self._materialized_valid(lease):
            raise BindingChangeError("invalid_editor_ack")
        change.acks[editor_id] = (last_saved_version, lease.revision)
        self._change_signal.set()
        return self._change_dto(change)

    def cancel_change(self, change_id: str, *, _expired: bool = False) -> dict[str, object]:
        # Do not call _get_change here: its lazy expiry invokes this method.
        change = self._change
        if change is None or change.change_id != change_id:
            raise BindingChangeError("unknown_change")
        if change.phase == "committed":
            raise BindingChangeError("already_committed")
        if change.phase in {"cancelled", "expired", "paused"}:
            return self._change_dto(change)
        try:
            # A leave/close can already have started while the async drain is
            # still joining. Reflect that fence in this synchronous response,
            # not merely after its cancelled waiter gets another loop turn.
            self._observe_service_fence(change)
        except Exception:
            change.phase, change.reason = "paused", "resume_required"
            if change.waiter is not None and not change.waiter.done():
                change.waiter.cancel()
            change.cancel_pending = True
            return self._change_dto(change)
        was_quiescing = change.phase != "prepared"
        change.phase = "paused" if change.fenced else ("expired" if _expired else "cancelled")
        change.reason = "resume_required" if change.fenced else ("change_expired" if _expired else None)
        if change.expiry is not None:
            change.expiry.cancel()
        self._change_signal.set()
        if change.waiter is not None and not change.waiter.done():
            change.waiter.cancel()
        if was_quiescing:
            change.cancel_pending = True
            if not change.running:
                self._finish_cancel(change)
        return self._change_dto(change)

    def _observe_service_fence(self, change: _BindingChange) -> None:
        if change.fenced or self._service_fenced is None:
            return
        # Called after cancellation has joined the service waiter. The sealing
        # phase already excluded new saves and waited for admitted mutations.
        if self._service_fenced() is True:
            if self._active or self._mutation_lock.locked():
                raise MutationBusy()
            self._require_generation(change.generation)
            self._generation = next_binding_generation(self._generation)
            self._sessions.clear()
            self._editors.clear()
            change.fenced = True

    def _finish_cancel(self, change: _BindingChange) -> None:
        change.cancel_pending = False
        try:
            self._observe_service_fence(change)
            if change.fenced:
                change.phase, change.reason = "paused", "resume_required"
            if self._cancel_quiesce is not None:
                self._cancel_quiesce(fenced=change.fenced)
        except Exception:
            # A failed service resume never opens local admission optimistically.
            change.phase, change.reason = "paused", "resume_required"

    async def _publish_change(self, change: _BindingChange) -> None:
        if self._change_event is not None:
            await self._change_event(self._change_dto(change))

    async def quiesce_change(
        self, change_id: str, expected_generation: int, *, timeout: float = 10.0,
    ) -> dict[str, object]:
        """Drain editor/transport owners without any lock held across their waits.

        Timeout is capped at 30 seconds per request and by prepare TTL. Blocked
        requests retain text/receipts/queues and may retry with the same evidence.
        Only proof permits the short existing final_snapshot to advance G; I5
        deliberately stops at ready (disabled identity commit is a later layer).
        """
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
            raise BindingChangeError("invalid_change")
        change = self._get_change(change_id)
        if change.phase in {"cancelled", "expired", "paused"}:
            return self._change_dto(change)
        if expected_generation != change.generation or type(expected_generation) is not int:
            raise StaleBindingGeneration(expected_generation, self._generation)
        if change.phase == "ready" or change.running:
            return self._change_dto(change)
        self._require_generation(expected_generation)
        if self._publishing.get() is not None or self._snapshot_scope.get() is not None:
            raise MutationBusy()
        if self._snapshot_pending:
            raise MutationBusy()
        # Reserve headroom for drain AND disabled commit before any service
        # fence/leave. Exhaustion cannot strand a target halfway through commit.
        next_binding_generation(self._generation, 2)
        if change.phase == "prepared":
            # A lease registered after prepare is just as real as an initial one.
            change.editors.update(self._editors)
        change.phase, change.reason, change.running = "draining", None, True
        deadline = min(asyncio.get_running_loop().time() + min(float(timeout), 30.0),
                       asyncio.get_running_loop().time() + max(0.0, change.deadline - self._monotonic()))

        async def drain() -> None:
            if self._begin_quiesce is None or self._drain_change is None:
                change.phase, change.reason = "blocked", "drain_unavailable"
                return
            await self._publish_change(change)
            while True:
                self._change_signal.clear()
                if self._unknown_change_clients():
                    change.phase, change.reason = "blocked", "unknown_clients"
                    return
                if self._unleased_change_rooms(change):
                    change.phase, change.reason = "blocked", "pending_edits"
                    return
                missing = self._missing_change_acks(change)
                if not missing:
                    break
                if any(self._monotonic() >= change.editors[item].deadline for item in missing):
                    change.phase, change.reason = "blocked", "pending_edits"
                    return
                await self._change_signal.wait()
            # Close final-save admission, then wait for already admitted continuations.
            change.phase = "sealing"
            await self._idle.wait()
            if self._missing_change_acks(change):
                change.phase, change.reason = "blocked", "pending_edits"
                return
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            proof = await self._drain_change(change.change_id, remaining)
            if not isinstance(proof, Mapping):
                change.phase, change.reason = "blocked", "drain_unproven"
                return
            if proof.get("drained") is not True:
                if proof.get("fenced") is True:
                    async with self.final_snapshot() as fence:
                        self._require_generation(change.generation)
                        fence.advance_generation()
                        change.fenced = True
                        self._sessions.clear()
                        self._editors.clear()
                reason = proof.get("reason")
                allowed = {"drain_timeout", "offline_unresolved", "unresolved_old_target",
                           "drain_failed", "binding_journal_unavailable", "resume_required",
                           "stale_binding", "invalid_pending_ack", "invalid_pending_nack",
                             "reconciliation_failed", "remote_refused", "room_unresolved",
                             "pending_edits", "local_changed"}
                change.phase = "blocked"
                change.reason = reason if isinstance(reason, str) and reason in allowed else "drain_unproven"
                return
            if self._unknown_change_clients() or self._missing_change_acks(change):
                change.phase, change.reason = "blocked", "pending_edits"
                return
            # The service has joined the runner. No new mutation is admissible.
            # Existing final_snapshot owns locks only in this non-waiting section.
            async with self.final_snapshot() as fence:
                self._require_generation(change.generation)
                fence.advance_generation()
                change.fenced = True
                self._sessions.clear()
                self._editors.clear()
                change.phase, change.reason = "ready", None

        try:
            # Choose both logical and service admission fences in this same loop
            # turn. Scheduling the worker first would let join/reconnect race the
            # logical quiesce before the worker receives its initial timeslice.
            if self._begin_quiesce is not None:
                self._begin_quiesce()
            change.waiter = asyncio.create_task(drain(), name="binding-change-drain")
            await asyncio.wait_for(change.waiter, max(0.001, deadline - asyncio.get_running_loop().time()))
        except TimeoutError:
            change.phase, change.reason = "blocked", "drain_timeout"
        except asyncio.CancelledError:
            if change.phase not in {"cancelled", "expired", "paused"}:
                self.cancel_change(change.change_id)
                raise
        except Exception:
            # Do not leak transport/config exceptions or erase unresolved evidence.
            change.phase, change.reason = "blocked", "drain_failed"
        finally:
            change.running, change.waiter = False, None
            if change.cancel_pending:
                self._finish_cancel(change)
            elif change.phase == "blocked":
                try:
                    self._observe_service_fence(change)
                except Exception:
                    change.reason = "resume_required"
        await self._publish_change(change)
        return self._change_dto(change)

    def cutover_blockers(self) -> tuple[str, ...]:
        """Conservative inventory seam, NOT the future quiesce/flush-ACK protocol."""
        blockers: list[str] = []
        live_unknown = False
        if self._client_inventory is not None:
            try:
                result = self._client_inventory()
                live_unknown = not (result is False or (type(result) is int and result == 0))
            except Exception:
                live_unknown = True
        if live_unknown or any(not capable for capable in self._clients.values()):
            blockers.append("unknown_clients")
        if any(self._clients.values()):
            # Advertising flush capability is not itself a flush ACK. I4 has
            # deliberately not implemented that protocol or an auto-activation.
            blockers.append("unconfirmed_clients")
        if any(lease.dirty or not lease.confirmed or lease.saves for lease in self._editors.values()):
            blockers.append("pending_editors")
        if any(self._monotonic() >= lease.deadline for lease in self._editors.values()):
            blockers.append("expired_editors")
        if self._sessions:
            blockers.append("active_rooms")
        if self._active:
            blockers.append("active_mutations")
        if self._snapshot_pending:
            blockers.append("mutation_busy")
        return tuple(blockers)

    def activate(self) -> None:
        """Internal-only seam for a future coordinator, never invoked by GET/register."""
        if self.activated:
            return
        if self.cutover_blockers():
            raise EditorLeaseError("pending_edits")
        self.activated = True

    def observe_activated_model(self) -> None:
        """Latch existing trusted I2 activation evidence, not execute a cutover.

        A durable already-activated target (or fail-closed unreadable evidence)
        must not revive legacy HTTP simply because editors are now open. Only
        the owning backend evidence reader may call this; identity metadata,
        register requests and WS capabilities do not constitute such evidence.
        Future actual activation still uses the guarded ``activate`` seam.
        """
        self.activated = True

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def active_mutations(self) -> int:
        return self._active

    @property
    def snapshot_pending(self) -> bool:
        return self._snapshot_pending

    def _require_generation(self, expected: int | None) -> None:
        if expected is not None and (type(expected) is not int or expected != self._generation):
            raise StaleBindingGeneration(expected, self._generation)

    async def admit(self, expected_binding_generation: int | None = None) -> MutationAdmission:
        # No await between validation, checking the fence and taking ownership.
        self._require_generation(expected_binding_generation)
        if self.binding_quiescing:
            reconciliation = self._pending_reconciliation.get()
            if reconciliation is not None:
                if self._change is None or self._change.phase != "sealing" or self._change.fenced:
                    raise MutationBusy()
                reconciliation()
            else:
                save = self._pending_editor_save.get()
                if save is None or not self._final_editor_allowed(save.editor_id):
                    raise MutationBusy()
                self.validate_editor_save(save)
        if self._snapshot_pending:
            # Waiting here can deadlock a reentrant event callback belonging to
            # an already-admitted continuation the snapshot itself is waiting on.
            raise MutationBusy()
        http_check = self._pending_http_check.get()
        self._pending_http_check.set(None)
        self._pending_editor_save.set(None)
        self._pending_reconciliation.set(None)
        admission = MutationAdmission(self, self._generation, http_check)
        self._active += 1
        self._idle.clear()
        return admission

    @asynccontextmanager
    async def final_snapshot(self) -> AsyncIterator[SnapshotFence]:
        """Exclude new writes and wait for admitted continuations, without their lock.

        Lock order is Ctx identity ownership → DocStore mutation lock (§3.2).
        The identity lock is local to this short, non-network operation; a future
        quiesce coordinator must not keep it while awaiting editor/remote ACKs.
        Cancellation while waiting restores admission without changing G.
        """
        publishing = self._publishing.get()
        snapshot = self._snapshot_scope.get()
        if (
            (publishing is not None and not publishing._released)
            or (snapshot is not None and snapshot._open)
        ):
            # A coordinator invoked from an awaited continuation callback would
            # otherwise wait for that very callback to return. Do not shorten
            # the continuation's admission to conceal this self-deadlock.
            raise MutationBusy()
        async with self.identity_lock:
            fence = SnapshotFence(self)
            token = self._snapshot_scope.set(fence)
            self._snapshot_pending = True
            try:
                await self._idle.wait()
                async with self._mutation_lock:
                    yield fence
            finally:
                fence._open = False
                self._snapshot_pending = False
                self._snapshot_scope.reset(token)


# --- I11: read-only reconcile preview (studio-design §5.2/§6, server-design §3.3) ---

RECONCILE_PREVIEW_TTL = 300.0
MAX_RECONCILE_PREVIEWS = 8
RECONCILE_COUNT_KEYS = ("upload", "download", "delete_local", "delete_remote", "conflict", "unchanged")


class ReconcilePreviewError(MutationGateError):
    """Fixed local refusal about a temporary preview; never about drafts/bases."""

    def __init__(self, code: str) -> None:
        if code not in {"preview_not_found", "preview_expired", "preview_stale"}:
            raise ValueError("unknown_reconcile_preview_error")
        self.code = code
        super().__init__(code)


def classify_reconcile(
    local: Mapping[str, str],
    remote: Mapping[str, tuple[int, str | None]],
    bases: Mapping[str, tuple[int, str | None]],
) -> tuple[dict[str, int], tuple[str, ...]]:
    """Plan what the existing §16.3 engine would do, without doing any of it.

    ``local`` maps path -> wire content hash of the raw Markdown; ``remote``
    maps path -> (rev, hash or None for a tombstone); ``bases`` are the old v2
    checkpoint's agreed (rev, hash) and must be empty when they do not belong to
    this exact incarnation. Mirrors ``SyncService._reconcile`` + ``_push_local``:
    a remote revision newer than the base is pulled over an untouched file;
    deleted-there/edited-here and deleted-here/edited-there keep the edit; both
    sides moved is a conflict (copy + pull). A remote that is behind/missing an
    agreed base is never silently "unchanged": it is reported as a conflict.
    """
    counts = dict.fromkeys(RECONCILE_COUNT_KEYS, 0)
    conflicts: list[str] = []
    for path in sorted(set(local) | set(remote) | set(bases), key=lambda p: p.encode("utf-8")):
        mine = local.get(path)
        remote_rev, theirs = remote.get(path, (0, None))
        base = bases.get(path)
        if mine == theirs:
            if mine is not None:
                counts["unchanged"] += 1
            continue
        base_hash = base[1] if base is not None else None
        if remote_rev > 0 and (base is None or base[0] < remote_rev):
            if mine == base_hash:
                kind = "delete_local" if theirs is None else "download"
            elif theirs is None:
                kind = "upload"
            elif mine is None:
                kind = "download"
            else:
                kind = "conflict"
        elif mine == base_hash:
            kind = "conflict"  # Remote is behind or lost an agreed version.
        else:
            kind = "delete_remote" if mine is None else "upload"
        counts[kind] += 1
        if kind == "conflict":
            conflicts.append(path)
    return counts, tuple(conflicts)


def reconcile_preview_fingerprint(preview_id: str, expires_at: float,
                                  evidence: Mapping[str, object]) -> str:
    """Bind the preview id/TTL to every observed nonsecret component."""
    import hashlib
    import json
    payload = json.dumps({"preview_id": preview_id, "expires_at": expires_at, "evidence": evidence},
                         sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(b"voxarium-reconcile-preview-v1\0" + payload.encode("utf-8")).hexdigest()


def _plain_preview_evidence(evidence: Mapping[str, object]) -> dict:
    import json
    return json.loads(json.dumps(evidence, allow_nan=False))


@dataclass(frozen=True)
class ReconcilePreview:
    preview_id: str
    fingerprint: str
    expires_at: float
    deadline: float
    counts: Mapping[str, int]
    conflicts: tuple[str, ...]
    evidence: Mapping[str, object] = field(repr=False)

    def dto(self) -> dict[str, object]:
        return {"preview_id": self.preview_id, "fingerprint": self.fingerprint,
                "counts": dict(self.counts), "conflicts": list(self.conflicts),
                "expires_at": self.expires_at}


class ReconcilePreviews:
    """Bounded in-memory registry of read-only previews for one Ctx (I11).

    A preview is an observation, not a lock and not permission to overwrite.
    Expiry, refusal or a stale recheck discards only that temporary entry; no
    draft, base, checkpoint, queue or configuration is touched. Nothing here
    is persisted, so a backend restart simply forgets every preview.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 ttl: float = RECONCILE_PREVIEW_TTL, limit: int = MAX_RECONCILE_PREVIEWS) -> None:
        if not math.isfinite(ttl) or ttl <= 0 or type(limit) is not int or limit < 1:
            raise ValueError("invalid_reconcile_preview_bounds")
        self._clock, self._monotonic = clock, monotonic
        self.ttl, self.limit = ttl, limit
        self._previews: dict[str, ReconcilePreview] = {}

    def __len__(self) -> int:
        self._expire()
        return len(self._previews)

    def _expire(self) -> None:
        now = self._monotonic()
        for preview_id in [k for k, p in self._previews.items() if now >= p.deadline]:
            del self._previews[preview_id]

    def publish(self, evidence: Mapping[str, object], counts: Mapping[str, int],
                conflicts: tuple[str, ...]) -> ReconcilePreview:
        self._expire()
        plain = _plain_preview_evidence(evidence)  # Detached from the caller.
        preview_id = uuid4().hex
        expires_at = self._clock() + self.ttl
        preview = ReconcilePreview(
            preview_id, reconcile_preview_fingerprint(preview_id, expires_at, plain),
            expires_at, self._monotonic() + self.ttl,
            MappingProxyType({key: int(counts[key]) for key in RECONCILE_COUNT_KEYS}),
            tuple(conflicts), MappingProxyType(plain),
        )
        while len(self._previews) >= self.limit:
            # Oldest first (insertion order). Eviction only forgets an observation.
            del self._previews[next(iter(self._previews))]
        self._previews[preview_id] = preview
        return preview

    def get(self, preview_id: str) -> ReconcilePreview:
        preview = self._previews.get(preview_id) if isinstance(preview_id, str) else None
        if preview is None:
            raise ReconcilePreviewError("preview_not_found")
        if self._monotonic() >= preview.deadline:
            self._previews.pop(preview_id, None)
            raise ReconcilePreviewError("preview_expired")
        return preview

    def require(self, preview_id: str, fingerprint: str,
                evidence: Mapping[str, object]) -> ReconcilePreview:
        """I12 recheck: the caller's FRESH evidence must reproduce the fingerprint.

        Both the client-presented fingerprint and the recomputed one must equal
        the stored value. Any mismatch discards this preview (409 repreview);
        success does not consume it — Enable owns that decision.
        """
        preview = self.get(preview_id)
        fresh = None
        try:
            fresh = reconcile_preview_fingerprint(
                preview_id, preview.expires_at, _plain_preview_evidence(evidence))
        except (TypeError, ValueError, RecursionError):
            pass
        if fresh != preview.fingerprint or fingerprint != preview.fingerprint:
            self.discard(preview_id)
            raise ReconcilePreviewError("preview_stale")
        return preview

    def discard(self, preview_id: str) -> bool:
        return self._previews.pop(preview_id, None) is not None

    def clear(self) -> None:
        self._previews.clear()
