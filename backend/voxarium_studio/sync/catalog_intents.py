"""Unmounted local Create reservations (studio-design §5.1/§6, S3).

This is NOT server replay, a credential store, or CatalogCreateService wiring.
Only a newly committed reservation reports first-POST eligibility, exactly once.
Every existing row, even ``reserved`` after a crash before sending anything, is
GET-only recovery *metadata*. A future caller must independently revalidate the
original profile/URL/revision AND live credential binding before any GET; this
module cannot authorize GET, Select, Preview or Enable. Even ``absent`` never
releases an operation ID: another Create requires a new explicit user command.

SQLite's rollback journal and synchronous=EXTRA provide atomic durable commits
and inter-process serialization on a local filesystem supporting SQLite locks
and fsync. No filesystem/database failure is repaired or overwritten here.
An interrupted initialization may leave an unusable file: fail closed, not retry
as an empty journal. Callers must preserve this file and its SQLite sidecars;
deleting/restoring a journal cannot preserve its at-most-once history.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
from typing import TypeVar
from uuid import UUID

from .catalog import CatalogError, _id, _label, _url


_VERSION = 1
_APPLICATION_ID = 0x5643494A  # VCIJ; local file identity, not a wire constant.
_STATES = frozenset({"reserved", "created", "conflict", "unknown", "rejected",
                     "found", "absent"})
_SCHEMA = """CREATE TABLE catalog_intents (
operation_id TEXT PRIMARY KEY NOT NULL,
profile_id TEXT NOT NULL,
url TEXT NOT NULL,
revision INTEGER NOT NULL,
project_id TEXT NOT NULL,
display_name TEXT NOT NULL,
state TEXT NOT NULL
) STRICT, WITHOUT ROWID"""
T = TypeVar("T")


@dataclass(frozen=True)
class CatalogIntent:
    """Exact original metadata only. Never pass keys, digests or headers here.

    ``url`` must already be normalized; ``display_name`` is deliberately NOT
    trimmed/NFC-normalized, so changed caller input cannot masquerade as replay.
    Credential changes must be fenced by the future owner, not by storing secrets
    (or their hashes) in this journal. Metadata is not proof of live binding.
    """

    operation_id: str
    profile_id: str
    url: str
    revision: int
    project_id: str
    display_name: str


@dataclass(frozen=True)
class IntentRecord:
    intent: CatalogIntent
    state: str


@dataclass(frozen=True)
class IntentReservation:
    record: IntentRecord
    # A one-time decision returned only by the inserting call, NOT a reusable
    # token/capability. The future POST owner still needs all profile/key guards.
    post_permitted: bool


def _operation_id(value: object) -> None:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is None:
        raise CatalogError("invalid_operation_id")


def _validate_intent(intent: object) -> None:
    valid = False
    try:
        if type(intent) is CatalogIntent:
            _operation_id(intent.operation_id)
            valid = (type(intent.profile_id) is str
                     and str(UUID(intent.profile_id)) == intent.profile_id
                     and type(intent.url) is str and bool(intent.url)
                     and _url(intent.url) == intent.url
                     and type(intent.revision) is int
                     and 1 <= intent.revision <= 2**63 - 1
                     and type(intent.project_id) is str
                     and _id(intent.project_id) == intent.project_id
                     and type(intent.display_name) is str
                     and bool(_label(intent.display_name)))
    except Exception:
        pass
    if not valid:
        raise CatalogError("invalid_intent")


def _intent_id(intent: object) -> str:
    # Capture the local ID before comparing an existing record. Changed input
    # conflicts even when its new body would fail validation (including bool=1).
    if (type(intent) is not CatalogIntent or type(intent.operation_id) is not str
            or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", intent.operation_id) is None):
        raise CatalogError("invalid_intent")
    return intent.operation_id


def _same_intent(original: CatalogIntent, candidate: CatalogIntent) -> bool:
    return all(type(getattr(original, name)) is type(getattr(candidate, name))
               and getattr(original, name) == getattr(candidate, name)
               for name in CatalogIntent.__dataclass_fields__)


class CatalogIntentJournal:
    """Mandatory injected absolute LOCAL file; no home/default/provider/transport.

    Construction creates only a missing file, never repairs an existing one.
    The injected local parent directory is trusted and must already exist. Each
    call re-reads and validates the entire database under a write lock;
    independently constructed instances/processes cannot both insert one ID.
    Format/I/O failures permanently poison this instance, including ambiguous
    commit failures. Reopening a valid committed row still cannot grant POST.
    This component accepts validated metadata only; it does NOT yet replace
    VS-113's in-memory reservation of invalid commands. Future integration must
    preserve that boundary explicitly. There is intentionally no delete, reset,
    migration or authorization API.
    """

    def __init__(self, path: Path):
        self._failure: str | None = None
        if (not isinstance(path, Path) or not path.is_absolute()
                or str(path).startswith(("\\\\", "//"))):
            raise CatalogError("invalid_intent_path")
        self._path = path
        created = False
        code = None
        try:
            # Claim initialization before sqlite can silently create a database.
            # An existing zero-byte file is an incomplete journal, not permission.
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                created = True
            if path.is_symlink() or not path.is_file():
                code = "intent_store_invalid"
            else:
                stat = path.stat()
                self._identity = (stat.st_dev, stat.st_ino)
        except Exception:
            code = "intent_store_unavailable"
        if code is not None:
            raise CatalogError(code)
        self._transaction(lambda _db, _records: None, initializing=created)

    def _check_file(self) -> None:
        stat = self._path.stat()
        if self._path.is_symlink() or (stat.st_dev, stat.st_ino) != self._identity:
            raise CatalogError("intent_store_invalid")

    def _connect(self) -> sqlite3.Connection:
        self._check_file()
        # mode=rw prevents accidental recreation after a missing/deleted file.
        return sqlite3.connect(self._path.as_uri() + "?mode=rw", uri=True,
                               isolation_level=None, timeout=5.0)

    @staticmethod
    def _records(db: sqlite3.Connection) -> dict[str, IntentRecord]:
        if (db.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,)
                or db.execute("PRAGMA user_version").fetchone() != (_VERSION,)
                or db.execute("PRAGMA journal_mode").fetchone() != ("delete",)
                or db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or db.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema").fetchall()
                != [("table", "catalog_intents", "catalog_intents", _SCHEMA)]):
            raise CatalogError("intent_store_invalid")
        records = {}
        for row in db.execute("SELECT * FROM catalog_intents"):
            intent, state = CatalogIntent(*row[:6]), row[6]
            valid = True
            try:
                _validate_intent(intent)
            except CatalogError:
                valid = False
            if not valid or type(state) is not str or state not in _STATES:
                raise CatalogError("intent_store_invalid")
            records[intent.operation_id] = IntentRecord(intent, state)
        return records

    def _transaction(self, action: Callable[[sqlite3.Connection, dict[str, IntentRecord]], T],
                     *, initializing: bool = False) -> T:
        if self._failure is not None:
            raise CatalogError(self._failure)
        db = None
        code = None
        try:
            db = self._connect()
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA synchronous=EXTRA")
            if db.execute("PRAGMA synchronous").fetchone() != (3,):
                raise CatalogError("intent_store_unavailable")
            db.execute("BEGIN IMMEDIATE")
            if initializing:
                # Only the O_EXCL winner may initialize, never an empty found file.
                if db.execute("SELECT name FROM sqlite_schema").fetchall():
                    raise CatalogError("intent_store_invalid")
                db.execute(_SCHEMA)
                db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={_VERSION}")
            records = self._records(db)
            # Keep disk exceptions inside this frame. A generator contextmanager
            # would retain the caller's raw exception through __context__.
            result = action(db, records)
            db.commit()
            self._check_file()
        except CatalogError as error:
            code = error.code
        except sqlite3.DatabaseError as error:
            code = ("intent_store_invalid" if getattr(error, "sqlite_errorcode", None)
                    in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
                    else "intent_store_unavailable")
        except Exception:
            code = "intent_store_unavailable"
        finally:
            if db is not None:
                try:
                    try:
                        if db.in_transaction:
                            db.rollback()
                    finally:
                        # Rollback itself can fail (disk error): still release
                        # the connection/lock, without ever granting eligibility.
                        db.close()
                except Exception:
                    code = "intent_store_unavailable"
        # Raise outside handlers: no raw disk exception/path is exposed as context.
        if code is not None:
            if code.startswith("intent_store_"):
                self._failure = code
            raise CatalogError(code)
        return result

    def reserve(self, intent: CatalogIntent) -> IntentReservation:
        """Commit BEFORE returning eligibility. Repeats never grant it again."""
        operation_id = _intent_id(intent)

        def action(db: sqlite3.Connection, records: dict[str, IntentRecord]) -> IntentReservation:
            existing = records.get(operation_id)
            if existing is not None:
                if not _same_intent(existing.intent, intent):
                    raise CatalogError("operation_conflict")
                return IntentReservation(existing, False)
            _validate_intent(intent)
            record = IntentRecord(intent, "reserved")
            db.execute("INSERT INTO catalog_intents VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (intent.operation_id, intent.profile_id, intent.url,
                        intent.revision, intent.project_id, intent.display_name,
                        record.state))
            return IntentReservation(record, True)
        return self._transaction(action)

    def lookup(self, operation_id: str) -> IntentRecord:
        """Return original recovery metadata, NEVER authorization for a request."""
        _operation_id(operation_id)

        def action(_db: sqlite3.Connection, records: dict[str, IntentRecord]) -> IntentRecord:
            record = records.get(operation_id)
            if record is None:
                raise CatalogError("operation_not_found")
            return record
        return self._transaction(action)

    def record_state(self, intent: CatalogIntent, *, state: str) -> IntentRecord:
        """Record an observation only. No outcome, including 404, releases POST.

        Transition table: reserved -> any observation; any observation -> any
        observation (including itself). Returning to reserved is forbidden.
        These are not authoritative lifecycle states: repeated GET may observe
        found/absent in either order, without proving ownership or consent.
        """
        operation_id = _intent_id(intent)
        if type(state) is not str or state not in _STATES - {"reserved"}:
            raise CatalogError("invalid_intent_state")

        def action(db: sqlite3.Connection, records: dict[str, IntentRecord]) -> IntentRecord:
            existing = records.get(operation_id)
            if existing is None:
                _validate_intent(intent)
                raise CatalogError("operation_not_found")
            if not _same_intent(existing.intent, intent):
                raise CatalogError("operation_conflict")
            db.execute("UPDATE catalog_intents SET state=? WHERE operation_id=?",
                       (state, intent.operation_id))
            return IntentRecord(intent, state)
        return self._transaction(action)
