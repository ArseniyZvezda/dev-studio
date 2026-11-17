"""Opt-in personal Remote storage foundation (dev spec §5.2.1).

Not mounted by an API or consumed by RemoteSync. Existing-profile mutations need
an authoritative consumer guard that holds the lifecycle lock through publication.
Until S5 provides that guard they fail closed. No network operations live here.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ContextManager
from uuid import uuid4

from ..core.application_config import (
    ApplicationConfig, RemoteServerProfile, _load_application_config_strict,
    _write_application_profiles as _write_application_config, _fsync_directory, application_config_file,
    application_config_transaction,
    validate_profile_name,
)
from .protocol import normalise_url

if TYPE_CHECKING:
    from .bindings import BindingSnapshot


class ProfileError(ValueError):
    """A stable code only: never retain input, tokens or underlying exceptions."""


@dataclass(frozen=True)
class ProfileConsumers:
    """Lifecycle evidence plus an I1 final check under S2's existing transaction.

    The callback is read-only: it must not acquire another config transaction.
    No callback means the caller supplied an already-stable test/owner snapshot.
    """
    active_roots: tuple[str, ...] = ()
    bound_roots: tuple[str, ...] = ()
    drained: bool = False
    recheck: Callable[[], None] | None = field(default=None, repr=False, compare=False)


ConsumerGuard = Callable[[str], ContextManager[ProfileConsumers]]


def _url(value: str) -> str:
    try:
        return normalise_url(value)
    except ValueError:
        raise ProfileError("invalid_server_url") from None


def _name(value: object) -> str:
    try:
        return validate_profile_name(value)
    except ValueError:
        raise ProfileError("invalid_profile_metadata") from None


class RemoteProfiles:
    """Metadata repository; constructing or reading it never imports legacy keys."""

    def __init__(self, *, consumer_guard: ConsumerGuard | None = None):
        self._consumer_guard = consumer_guard

    def _read(self) -> ApplicationConfig:
        try:
            return _load_application_config_strict()
        except ValueError:
            raise ProfileError("application_store_unavailable") from None

    def list(self) -> tuple[RemoteServerProfile, ...]:
        # Atomic metadata publication permits truly read-only diagnostic snapshots.
        return tuple(self._read().remote_servers)

    def get(self, profile_id: str) -> RemoteServerProfile:
        return self._find(self._read(), profile_id)

    @staticmethod
    def _find(config: ApplicationConfig, profile_id: str) -> RemoteServerProfile:
        for profile in config.remote_servers:
            if profile.profile_id == profile_id:
                return profile
        raise ProfileError("profile_not_found")

    @staticmethod
    def _cas(profile: RemoteServerProfile, expected_revision: int) -> None:
        if type(expected_revision) is not int or profile.revision != expected_revision:
            raise ProfileError("profile_revision_conflict")

    @staticmethod
    def _publish(config: ApplicationConfig, profile: RemoteServerProfile) -> None:
        config.remote_servers = [
            profile if old.profile_id == profile.profile_id else old
            for old in config.remote_servers
        ]
        _write_application_config(config)

    def create(self, name: str, url: str) -> RemoteServerProfile:
        name = _name(name)  # Reject before creating even a transaction lock file.
        url = _url(url)
        with application_config_transaction():
            self._check_pending()
            config = self._read()
            if any(p.url == url for p in config.remote_servers):
                raise ProfileError("profile_url_collision")
            try:
                profile = RemoteServerProfile(profile_id=str(uuid4()), name=name, url=url)
            except ValueError:
                raise ProfileError("invalid_profile_metadata") from None
            config.remote_servers.append(profile)
            _write_application_config(config)
            return profile

    def rename(self, profile_id: str, name: str, *, expected_revision: int) -> RemoteServerProfile:
        """A label change has no consumer/reconnect side effect (§5.1)."""
        name = _name(name)
        with application_config_transaction():
            self._check_pending()
            config = self._read()
            old = self._find(config, profile_id)
            self._cas(old, expected_revision)
            try:
                new = RemoteServerProfile(
                    profile_id=old.profile_id, url=old.url, name=name, revision=old.revision + 1,
                )
            except ValueError:
                raise ProfileError("invalid_profile_metadata") from None
            self._publish(config, new)
            return new

    @contextlib.contextmanager
    def _consumers(self, profile_id: str, *, unbound: bool = False) -> Iterator[ProfileConsumers]:
        if self._consumer_guard is None:
            raise ProfileError("consumer_lifecycle_unavailable")
        with self._consumer_guard(profile_id) as state:
            if not isinstance(state, ProfileConsumers):
                raise ProfileError("consumer_lifecycle_unavailable")
            if unbound and (state.bound_roots or state.active_roots):
                raise ProfileError("profile_in_use")
            if state.active_roots and not state.drained:
                raise ProfileError("profile_consumers_not_drained")
            yield state

    @staticmethod
    def _recheck_consumers(state: ProfileConsumers) -> None:
        # runtime-integration-plan §3.2/I1: the private writer uses this SAME
        # cross-process transaction. Recheck before journal AND key observation;
        # an observation made before its acquisition cannot fence publication.
        if state.recheck is not None:
            state.recheck()

    @staticmethod
    def _consent_snapshot(profile_id: str, urls: tuple[str, ...]) -> BindingSnapshot:
        """I14 preflight under the caller's existing lifecycle/config transaction.

        Exact profile AND URL are required. A row matching only one is ambiguous:
        refuse rather than repair another binding or leave possibly affected consent.
        Recovery supplies its validated old/target/current URL union. This reads no
        managed root and proves neither legacy completeness L nor positive drain.
        """
        from .bindings import RemoteBindings

        snapshot = None
        try:
            snapshot = RemoteBindings().read()
            if any((row.profile_id == profile_id) != (row.url in urls)
                   for row in snapshot.records):
                snapshot = None
        except Exception:
            snapshot = None
        if snapshot is None:
            raise ProfileError("consumer_lifecycle_unavailable")
        return snapshot

    @staticmethod
    def _invalidate_consent(snapshot: BindingSnapshot, profile_id: str, urls: tuple[str, ...]) -> None:
        """Demote only confirmed rows AFTER durable fence, BEFORE key publication.

        The caller already owns application_config_transaction; public save would
        acquire a second OS descriptor. Each successful CAS feeds the next one.
        Partial failure retains the journal and all earlier demotions; recovery
        repeats only remaining confirmed rows, never restoring consent
        (studio-design §7, runtime-integration-plan I14).
        """
        from .bindings import RemoteBindings

        failed = False
        try:
            store = RemoteBindings()
            affected = False
            for row in snapshot.records:
                if row.profile_id == profile_id and row.url in urls:
                    affected = True
                    if row.selection_state == "confirmed":
                        snapshot = store._save_in_transaction(
                            row.model_copy(update={"selection_state": "selected"}),
                            expected_fingerprint=snapshot.fingerprint,
                        )
            if affected:
                # Same pinned personal-home directory as S2's journal/config.
                # Persist private replaces before any credential can be published;
                # retry also flushes selected rows left by an interrupted attempt.
                _fsync_directory(application_config_file().parent)
        except Exception:
            failed = True
        if failed:
            # Outside the handler: do not retain raw store errors in __context__.
            raise ProfileError("consumer_lifecycle_unavailable")

    @staticmethod
    def _journal() -> Path:
        return application_config_file().with_name("remote-profile-change.json")

    def _check_pending(self) -> None:
        if self._journal().exists():
            raise ProfileError("profile_recovery_required")

    def _begin(self, profile: RemoteServerProfile, *, target_url: str | None = None) -> None:
        """Nonsecret recovery fence, durable before either store changes (§9.2)."""
        path = self._journal()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".remote-change-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({
                    "version": 1, "profile_id": profile.profile_id,
                    "expected_revision": profile.revision, "url": profile.url,
                    "target_url": target_url or profile.url,
                }, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            _fsync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _end(self) -> None:
        self._journal().unlink()
        _fsync_directory(self._journal().parent)

    def load_token(self, profile_id: str) -> str | None:
        """Private backend lookup, not a DTO. Pending cross-file writes are unusable."""
        from .credentials import load_profile_token

        self.get(profile_id)  # A missing profile must not create a home/lock file.
        with application_config_transaction():
            self._check_pending()
            profile = self._find(self._read(), profile_id)
            return load_profile_token(profile.profile_id, profile.url)

    def set_credential(self, profile_id: str, token: str, *, expected_revision: int) -> RemoteServerProfile:
        if not isinstance(token, str) or not token.strip():
            raise ProfileError("credential_required")
        return self._credential(profile_id, token, expected_revision=expected_revision)

    def clear_credential(self, profile_id: str, *, expected_revision: int) -> RemoteServerProfile:
        return self._credential(profile_id, None, expected_revision=expected_revision)

    def _credential(self, profile_id: str, token: str | None, *, expected_revision: int) -> RemoteServerProfile:
        from . import credentials

        # Lifecycle lock precedes the personal-store lock; providers must use this order.
        with self._consumers(profile_id) as consumers, application_config_transaction():
            self._check_pending()
            config = self._read()
            old = self._find(config, profile_id)
            self._cas(old, expected_revision)
            self._recheck_consumers(consumers)
            consent = self._consent_snapshot(profile_id, (old.url,))
            revision = credentials.profile_token_revision(profile_id, old.url)
            self._begin(old)
            self._invalidate_consent(consent, profile_id, (old.url,))
            credentials.save_profile_token(profile_id, old.url, token, expected_revision=revision)
            new = old.model_copy(update={"revision": old.revision + 1})
            self._publish(config, new)
            self._end()
            return new

    def change_url(self, profile_id: str, url: str, *, expected_revision: int,
                   clear_credential: bool = False) -> RemoteServerProfile:
        from . import credentials

        url = _url(url)
        with self._consumers(profile_id, unbound=True) as consumers, application_config_transaction():
            self._check_pending()
            config = self._read()
            old = self._find(config, profile_id)
            self._cas(old, expected_revision)
            if not clear_credential:
                raise ProfileError("credential_clear_confirmation_required")
            if any(p.url == url and p.profile_id != profile_id for p in config.remote_servers):
                raise ProfileError("profile_url_collision")
            self._recheck_consumers(consumers)
            revision = credentials.profile_token_revision(profile_id, old.url)
            self._begin(old, target_url=url)
            revision = credentials.save_profile_token(profile_id, old.url, None, expected_revision=revision)
            credentials.save_profile_token(profile_id, url, None, expected_revision=revision)
            new = old.model_copy(update={"url": url, "revision": old.revision + 1})
            self._publish(config, new)
            self._end()
            return new

    def delete(self, profile_id: str, *, expected_revision: int) -> None:
        from . import credentials

        with self._consumers(profile_id, unbound=True) as consumers, application_config_transaction():
            self._check_pending()
            config = self._read()
            old = self._find(config, profile_id)
            self._cas(old, expected_revision)
            self._recheck_consumers(consumers)
            revision = credentials.profile_token_revision(profile_id, old.url)
            self._begin(old)
            credentials.save_profile_token(profile_id, old.url, None, expected_revision=revision)
            config.remote_servers = [p for p in config.remote_servers if p.profile_id != profile_id]
            _write_application_config(config)
            self._end()

    def import_legacy_credential(self, profile_id: str, *, expected_revision: int) -> RemoteServerProfile:
        """Explicit personal import only; no project migration, ENV or connection."""
        from . import credentials

        with self._consumers(profile_id) as consumers, application_config_transaction():
            self._check_pending()
            config = self._read()
            old = self._find(config, profile_id)
            self._cas(old, expected_revision)
            self._recheck_consumers(consumers)
            revision = credentials.profile_token_revision(profile_id, old.url)
            predicted = credentials.preflight_legacy_import(
                profile_id, old.url, expected_revision=revision,
            )
            if predicted == revision:
                return old
            consent = self._consent_snapshot(profile_id, (old.url,))
            self._begin(old)
            self._invalidate_consent(consent, profile_id, (old.url,))
            try:
                imported = credentials.import_legacy_token(profile_id, old.url, expected_revision=revision)
            except credentials.CredentialConflict:
                # The importer raises this only before writing: preserve the existing
                # usable key rather than turning a known conflict into crash recovery.
                self._end()
                raise
            new = old if imported == revision else old.model_copy(update={"revision": old.revision + 1})
            if new is not old:
                self._publish(config, new)
            self._end()
            return new

    def recover(self, profile_id: str, *, expected_revision: int) -> RemoteServerProfile | None:
        """Explicit conservative recovery: discard uncertain credential, never replay it.

        Requires the same drain guard. The user must re-enter/import the key afterwards.
        The journal has no token/backup and a malformed journal is never discarded.
        """
        from . import credentials

        with self._consumers(profile_id) as consumers, application_config_transaction():
            try:
                pending = json.loads(self._journal().read_text(encoding="utf-8"))
                if (not isinstance(pending, dict) or type(pending.get("version")) is not int
                        or pending["version"] != 1
                        or pending.get("profile_id") != profile_id
                        or type(pending.get("expected_revision")) is not int
                        or pending["expected_revision"] < 1
                        or _url(pending["url"]) != pending["url"]
                        or _url(pending["target_url"]) != pending["target_url"]):
                    raise ValueError()
            except (OSError, UnicodeError, ValueError, KeyError, TypeError):
                raise ProfileError("profile_recovery_unavailable") from None
            config = self._read()
            current = next((p for p in config.remote_servers if p.profile_id == profile_id), None)
            if current is not None:
                self._cas(current, expected_revision)
            elif type(expected_revision) is not int or expected_revision != pending["expected_revision"]:
                raise ProfileError("profile_revision_conflict")
            self._recheck_consumers(consumers)
            old_url = pending["url"]
            target_url = current.url if current else old_url
            urls = tuple(dict.fromkeys((old_url, target_url, pending["target_url"])))
            consent = self._consent_snapshot(profile_id, urls)
            revision = max(credentials.profile_token_revision(profile_id, old_url),
                           credentials.profile_token_revision(profile_id, target_url),
                           credentials.profile_token_revision(profile_id, pending["target_url"]))
            self._invalidate_consent(consent, profile_id, urls)
            revision = credentials.save_profile_token(profile_id, old_url, None, expected_revision=revision)
            if target_url != old_url:
                credentials.save_profile_token(profile_id, target_url, None, expected_revision=revision)
            if current is not None:
                current = current.model_copy(update={"revision": current.revision + 1})
                self._publish(config, current)
            self._end()
            return current
