"""Gate L owner: the user's explicit consumer confirmation per server URL (dev spec §16.11).

The Studio cannot prove that no unopened (legacy) project on this computer uses a
server address: recents are bounded, roots move, and the disk is never walked. So
legacy completeness is not inferred at all. The user owns it: after being shown the
projects the Studio does know (private bindings, resident contexts, leases), they
explicitly confirm one exact normalised URL. Only that durable record makes the
profile lifecycle's inventory ``known`` (runtime-integration-plan §2.1, I14).

The record lives in the personal home next to the other Remote stores, never in a
managed project. Missing, unreadable, corrupt or foreign-version content is simply
*no confirmation* (``unknown``) for every URL: nothing is repaired on read, and a
confirmation is never derived from recents, an empty private store or a Verify.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core.application_config import _fsync_directory, application_config_transaction
from ..core.paths import user_config_dir
from .protocol import normalise_url

FILE_NAME = "remote-consumer-confirmations.json"
VERSION = 1


class ConfirmationError(ValueError):
    """A stable code only, never raw JSON, roots or underlying I/O errors."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def confirmations_file() -> Path:
    return user_config_dir() / FILE_NAME


@dataclass(frozen=True)
class Confirmation:
    url: str
    confirmed_at: str
    bound_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfirmationSnapshot:
    """A validated observation. ``fingerprint`` None means the store is absent."""

    fingerprint: str | None
    entries: dict[str, Confirmation] = field(default_factory=dict)

    def get(self, url: str) -> Confirmation | None:
        return self.entries.get(url)


def _canonical(url: object) -> str:
    if not isinstance(url, str):
        raise ValueError
    if normalise_url(url) != url:
        raise ValueError
    return url


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _valid_time(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


def _roots(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(r, str) or not r for r in value):
        raise ValueError
    if value != sorted(set(value)):
        raise ValueError
    return tuple(value)


class ConsumerConfirmations:
    """Read/confirm/withdraw. Writers take the shared personal transaction once.

    Callers that also hold the profile lifecycle lock must acquire it first
    (lifecycle → config), exactly like the C7 credential mutations.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def path(self) -> Path:
        return self._path if self._path is not None else confirmations_file()

    def read(self) -> ConfirmationSnapshot:
        """Strict read; raises for anything but an absent or valid regular file."""
        path = self.path()
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return ConfirmationSnapshot(fingerprint=None)
        except OSError:
            raise ConfirmationError("consumer_confirmation_unavailable") from None
        import stat

        snapshot = None
        try:
            if not stat.S_ISREG(mode):
                raise ValueError
            content = path.read_bytes()
            raw = json.loads(content.decode("utf-8"), object_pairs_hook=_unique)
            if (not isinstance(raw, dict) or raw.keys() != {"version", "urls"}
                    or type(raw["version"]) is not int or raw["version"] != VERSION
                    or not isinstance(raw["urls"], dict)):
                raise ValueError
            entries = {}
            for url, value in raw["urls"].items():
                _canonical(url)
                if not isinstance(value, dict) or value.keys() != {"confirmed_at", "bound_roots"}:
                    raise ValueError
                entries[url] = Confirmation(url, _valid_time(value["confirmed_at"]),
                                            _roots(value["bound_roots"]))
            snapshot = ConfirmationSnapshot(hashlib.sha256(content).hexdigest(), entries)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            pass
        if snapshot is None:
            raise ConfirmationError("consumer_confirmation_unavailable")
        return snapshot

    def confirmed(self, urls: tuple[str, ...]) -> bool:
        """True only when EVERY URL has a valid confirmation; any doubt is False."""
        try:
            snapshot = self.read()
            return bool(urls) and all(snapshot.get(url) is not None for url in urls)
        except Exception:
            return False

    def confirm(self, url: str, bound_roots: tuple[str, ...]) -> Confirmation:
        url = _canonical_or_error(url)
        roots = tuple(sorted(set(bound_roots)))
        if any(not isinstance(r, str) or not r for r in roots):
            raise ConfirmationError("consumer_confirmation_unavailable")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = Confirmation(url, stamp, roots)

        def change(entries: dict[str, Confirmation]) -> dict[str, Confirmation]:
            return {**entries, url: entry}

        self._update(change)
        return entry

    def withdraw(self, url: str) -> None:
        url = _canonical_or_error(url)

        def change(entries: dict[str, Confirmation]) -> dict[str, Confirmation] | None:
            if url not in entries:
                return None  # Nothing to withdraw: no file is created or rewritten.
            return {k: v for k, v in entries.items() if k != url}

        self._update(change)

    def _update(self, change) -> None:
        failed = False
        try:
            with application_config_transaction():
                path = self.path()
                try:
                    entries = dict(self.read().entries)
                except ConfirmationError:
                    # A corrupt regular file only loses OTHER confirmations: they
                    # become unknown, the fail-closed direction. A link/directory
                    # is never replaced.
                    import stat
                    try:
                        if not stat.S_ISREG(path.lstat().st_mode):
                            raise ValueError
                    except FileNotFoundError:
                        pass
                    entries = {}
                updated = change(entries)
                if updated is not None:
                    self._publish(path, updated)
        except Exception:
            failed = True
        if failed:
            raise ConfirmationError("consumer_confirmation_unavailable")

    @staticmethod
    def _publish(path: Path, entries: dict[str, Confirmation]) -> None:
        document = {"version": VERSION, "urls": {
            url: {"confirmed_at": e.confirmed_at, "bound_roots": list(e.bound_roots)}
            for url, e in sorted(entries.items())
        }}
        content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".remote-consumers-",
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows: AV/indexer handles can transiently refuse a replace.
            for attempt in range(5):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            temporary = None
            _fsync_directory(path.parent)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _canonical_or_error(url: object) -> str:
    try:
        return _canonical(url)
    except (TypeError, ValueError):
        raise ConfirmationError("consumer_confirmation_unavailable") from None
