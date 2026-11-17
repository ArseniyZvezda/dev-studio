"""Opt-in read-only S2 -> S3 composition (studio-design §5.1/§6, slice A).

All readers AND their live location observers are required, explicitly bound to
one supplied home. No production path, runtime wiring, lock or writer is chosen
here. Callers supply trusted read-only S2 ``RemoteProfiles.get`` and
``credentials.load_profile_token`` readers, never ``RemoteProfiles.load_token``.
Location observers must describe those very readers, not unrelated paths.

Create a fresh immutable provider for the observed profile/URL/revision, then pass
it as ``profiles`` and its ``load_token`` as ``credentials`` to CatalogService or
CatalogReadService. The explicit revision binding is necessary because the S3
credential callback has no revision parameter. Existing S3 guards fence both
sides of awaits/pages and compare the live key to their ephemeral snapshot.

These are discrete observations, NOT continuous/multiprocess isolation. A
journal may appear after the last check and before transport dispatch; no new
S2 locking API or send-boundary atomicity is claimed. Cold Create recovery still
has no historical credential proof and remains credential_binding_unproven.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import stat

from ..core.application_config import RemoteServerProfile
from .catalog_service import CredentialProvider
from .profiles import ProfileError, RemoteProfiles
from .credentials import _profile_id, _url


@dataclass(frozen=True, repr=False)
class S2CatalogReaders:
    """Trusted readers plus their actual live path getters; nothing is defaulted.

    In offline tests all five callbacks are explicitly attached to synthetic
    storage. This declaration is a dependency contract, not a sandbox capable
    of inspecting arbitrary Python closures or preventing dishonest callbacks.
    """

    home: Path
    profile_reader: Callable[[str], RemoteServerProfile]
    credential_reader: CredentialProvider
    application_file: Callable[[], Path]
    credential_file: Callable[[], Path]
    journal_file: Callable[[], Path]

    def __post_init__(self) -> None:
        if (not isinstance(self.home, Path) or not self.home.is_absolute()
                or ".." in self.home.parts
                or not all(callable(reader) for reader in (
                    self.profile_reader, self.credential_reader,
                    self.application_file, self.credential_file, self.journal_file,
                ))):
            raise ProfileError("invalid_catalog_readers")

    def check(self) -> None:
        """No journal entry of *any* kind may authorize a credential lookup.

        lstat observes dangling links too. Only ENOENT means absence; unreadable
        paths, non-directories and any other observation error fail closed. A
        missing home is allowed and never created; the S2 metadata reader then
        reports a missing profile. A home symlink is not an absent directory.
        """
        failed = False
        try:
            locations = (self.application_file(), self.credential_file(), self.journal_file())
            expected = tuple(self.home / name for name in (
                "config.yaml", "remote-keys.json", "remote-profile-change.json",
            ))
            if locations != expected:
                failed = True
            else:
                try:
                    mode = self.home.lstat().st_mode
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISDIR(mode):
                        failed = True
                try:
                    expected[2].lstat()
                except FileNotFoundError:
                    pass
                else:
                    failed = True
        except Exception:
            failed = True
        # Raise outside the handler: raw observer exceptions may contain keys.
        if failed:
            raise ProfileError("profile_recovery_required")


@dataclass(frozen=True)
class PendingCatalogProviders:
    """One profile/URL/revision observation, reusable by existing S3 guards.

    No shared 'last get' cache: overlapping requests/profiles cannot replace a
    credential callback's expected binding. A new URL/revision requires a new
    explicit provider, not silent rebinding of an in-flight operation. The URL
    must be captured explicitly too: S3 may perform more than one metadata read
    between its original-URL comparison and its credential callback.
    """

    readers: S2CatalogReaders = field(repr=False)
    profile_id: str = field(kw_only=True)
    expected_revision: int = field(kw_only=True)
    expected_url: str = field(repr=False, kw_only=True)

    def __post_init__(self) -> None:
        invalid = False
        try:
            _profile_id(self.profile_id)
            invalid = (not isinstance(self.readers, S2CatalogReaders)
                       or type(self.expected_revision) is not int
                       or self.expected_revision < 1
                       or _url(self.expected_url) != self.expected_url)
        except Exception:
            invalid = True
        if invalid:
            raise ProfileError("invalid_catalog_readers")

    def get(self, profile_id: str) -> RemoteServerProfile:
        self.readers.check()
        if profile_id != self.profile_id:
            raise ProfileError("profile_not_found")
        profile = None
        code = "profile_store_unavailable"
        try:
            profile = self.readers.profile_reader(profile_id)
        except ProfileError as error:
            if error.args == ("profile_not_found",):
                code = "profile_not_found"
        except Exception:
            pass
        self.readers.check()  # Also fence failed metadata reads.
        if profile is None:
            raise ProfileError(code)
        if not isinstance(profile, RemoteServerProfile) or profile.profile_id != profile_id:
            raise ProfileError("invalid_profile_metadata")
        RemoteProfiles._cas(profile, self.expected_revision)
        if profile.url != self.expected_url:
            raise ProfileError("profile_revision_conflict")
        return profile

    def load_token(self, profile_id: str, url: str) -> str | None:
        # Re-read metadata before ANY credential lookup, including scoped ENV.
        # Compare the caller's original locator, never lookup at a new one.
        profile = self.get(profile_id)
        if profile.url != url:
            raise ProfileError("profile_revision_conflict")
        self.readers.check()
        token = None
        failed = False
        try:
            token = self.readers.credential_reader(profile_id, url)
        except Exception:
            failed = True
        self.readers.check()  # Includes the ENV early-return branch and failures.
        latest = self.get(profile_id)
        if latest.url != url:
            raise ProfileError("profile_revision_conflict")
        if failed:
            raise ProfileError("credential_store_unavailable")
        return token
