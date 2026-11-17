"""Personal plaintext credentials, never application/project settings (§5.2.1).

The v2 primitives are deliberately not wired to the legacy sync runtime. Profile lifecycle
and consumer draining belong to ``profiles``, not to this storage layer. Deletion retains a
revision-only tombstone (``token: null``) to prevent stale writers succeeding after an ABA.
Legacy URL keys survive explicit upgrades under ``legacy``; reads never migrate anything.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from threading import RLock
from uuid import UUID

from ..core.paths import user_config_dir
from .protocol import normalise_url

log = logging.getLogger(__name__)
TOKEN_ENV_VAR = "VOXARIUM_STUDIO_REMOTE_TOKEN"
URL_ENV_VAR = "VOXARIUM_STUDIO_REMOTE_URL"
_file_lock = RLock()


class CredentialsError(ValueError):
    """A fixed, secret-free storage error safe for an outer API to translate."""

    def __init__(self, code: str = "credential_store_unavailable") -> None:
        self.code = code
        super().__init__(code)


class CredentialConflict(CredentialsError):
    def __init__(self) -> None:
        super().__init__("credential_conflict")


def keys_file() -> Path:
    return user_config_dir() / "remote-keys.json"


def _profile_id(value: str) -> str:
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise CredentialsError("invalid_profile_id")
    return value


def _url(value: str) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        return normalise_url(value)
    except (ValueError, TypeError, AttributeError):
        # normalise_url's legacy errors can contain user input; never chain them.
        raise CredentialsError("invalid_remote_url") from None


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CredentialsError("credential_store_corrupt")
        result[key] = value
    return result


def _legacy(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(url, str) and isinstance(token, str) for url, token in value.items()
    )


def _read_store() -> dict:
    try:
        raw = json.loads(keys_file().read_text(encoding="utf-8"), object_pairs_hook=_object)
    except FileNotFoundError:
        return {}
    except CredentialsError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CredentialsError("credential_store_corrupt") from None
    if not isinstance(raw, dict):
        raise CredentialsError("credential_store_corrupt")
    if "version" not in raw:
        if _legacy(raw):
            return raw
        raise CredentialsError("credential_store_corrupt")
    if (type(raw.get("version")) is not int or raw["version"] != 2
            or set(raw) - {"version", "profiles", "legacy"}
            or not isinstance(raw.get("profiles"), dict)
            or not _legacy(raw.get("legacy", {}))):
        raise CredentialsError("credential_store_corrupt")
    for profile_id, entry in raw["profiles"].items():
        try:
            _profile_id(profile_id)
            valid = (
                isinstance(entry, dict) and set(entry) == {"url", "token", "revision"}
                and _url(entry["url"]) == entry["url"]
                and (entry["token"] is None or
                     isinstance(entry["token"], str) and bool(entry["token"].strip()))
                and type(entry["revision"]) is int and entry["revision"] >= 1
            )
        except CredentialsError:
            valid = False
        if not valid:
            raise CredentialsError("credential_store_corrupt")
    return raw


def _upgraded(raw: dict) -> dict:
    if raw.get("version") == 2:
        return raw
    return {"version": 2, "profiles": {}, "legacy": raw}


def _windows_acl_api():
    """Declare pointer-sized Win32 signatures; do not depend on pywin32 or a shell."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    pointer_out = ctypes.POINTER(pointer)
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        pointer_out, pointer_out, pointer_out, pointer_out, pointer_out,
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [pointer, pointer_out]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, pointer_out, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [
        pointer, ctypes.POINTER(wintypes.BOOL), pointer_out, ctypes.POINTER(wintypes.BOOL),
    ]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, pointer, pointer, pointer, pointer,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    return advapi, kernel


def _windows_owner_only(path: Path) -> None:
    """Replace inherited permissions with one protected, owner-only full-access ACE.

    A relocated Studio home may inherit permissive ACLs. chmod(0600) alone only sets
    the DOS read-only bit on Windows, not a DACL. Apply this to the still-empty fresh
    tempfile, never to the home directory or another user's existing settings (§5.2.1).
    """
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    advapi, kernel = _windows_acl_api()
    owner, original, sid_text, restricted, dacl = (ctypes.c_void_p() for _ in range(5))
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    try:
        # SE_FILE_OBJECT=1, OWNER_SECURITY_INFORMATION=1. The returned owner SID
        # belongs to the separately allocated original descriptor until LocalFree.
        if advapi.GetNamedSecurityInfoW(
            str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(original),
        ) != 0 or not owner.value:
            raise OSError("windows_acl_unavailable")
        if not advapi.ConvertSidToStringSidW(owner, ctypes.byref(sid_text)):
            raise OSError("windows_acl_unavailable")
        sddl = f"D:P(A;;FA;;;{ctypes.wstring_at(sid_text)})"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(restricted), None,
        ):
            raise OSError("windows_acl_unavailable")
        if not advapi.GetSecurityDescriptorDacl(
            restricted, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted),
        ) or not present.value or not dacl.value:
            raise OSError("windows_acl_unavailable")
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        # No owner/group/SACL changes and no inherited ACEs from the relocated home.
        if advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None) != 0:
            raise OSError("windows_acl_unavailable")
    finally:
        for allocation in (restricted, sid_text, original):
            if allocation.value:
                kernel.LocalFree(allocation)


def _restrict_secret_file(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    try:
        _windows_owner_only(path)
    except (OSError, AttributeError):
        # Best effort on unsupported filesystems/Windows API failures. Never log paths,
        # exception text or the serialized data; no raw WinError escapes this layer.
        log.warning("Could not restrict remote credential file permissions")


@contextlib.contextmanager
def _mutation() -> Iterator[None]:
    """Serialise read/CAS/replace across threads AND Studio processes sharing a home.

    The stable sidecar is not removed: unlinking a lock file allows a third writer to
    lock a different inode while another writer is still waiting on the original one.
    """
    with _file_lock:
        try:
            path = keys_file().with_suffix(".lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+b") as stream:
                with contextlib.suppress(OSError):
                    path.chmod(0o600)
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
                try:
                    yield
                finally:
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream, fcntl.LOCK_UN)
        except OSError:
            raise CredentialsError() from None


def _write_store(raw: dict) -> None:
    path = keys_file()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".remote-keys-",
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            _restrict_secret_file(temporary)
            json.dump(raw, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        if os.name != "nt":
            # POSIX directory durability; Windows does not support opening directories
            # with os.open. Replacement there is atomic, with the data already fsynced.
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, ValueError, UnicodeError):
        raise CredentialsError() from None
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def _read() -> dict[str, str]:
    """Legacy runtime view; malformed stores fail closed without secret-bearing logs."""
    try:
        raw = _read_store()
        return raw.get("legacy", {}) if raw.get("version") == 2 else raw
    except CredentialsError:
        log.warning("Could not read remote credentials")
        return {}


def load_token(url: str) -> str | None:
    """Unchanged legacy lookup, including the old globally scoped ENV override."""
    override = os.environ.get(TOKEN_ENV_VAR)
    if override:
        return override
    with _file_lock:
        return _read().get(url) or None


def save_token(url: str, token: str | None) -> None:
    """Legacy mutation touches only legacy data, never a v2 profile key."""
    with _mutation():
        raw = _read_store()  # Never overwrite an unreadable store with defaults.
        legacy = raw.setdefault("legacy", {}) if raw.get("version") == 2 else raw
        if token:
            legacy[url] = token
        else:
            legacy.pop(url, None)
        _write_store(raw)


def _entry(raw: dict, profile_id: str, url: str) -> dict | None:
    entry = raw.get("profiles", {}).get(profile_id) if raw.get("version") == 2 else None
    return entry if entry and entry["url"] == url else None


def _environment(url: str) -> tuple[bool, str | None]:
    if TOKEN_ENV_VAR not in os.environ:
        return False, None
    try:
        matches = _url(os.environ.get(URL_ENV_VAR, "")) == url
    except CredentialsError:
        matches = False
    return True, (os.environ[TOKEN_ENV_VAR] or None) if matches else None


def load_profile_token(profile_id: str, url: str) -> str | None:
    """Exact profile + normalized URL lookup; ENV must be explicitly URL-bound."""
    profile_id, url = _profile_id(profile_id), _url(url)
    supplied, token = _environment(url)
    if supplied:
        return token  # An unbound ENV token must NOT fall back to a stored key.
    with _file_lock:
        entry = _entry(_read_store(), profile_id, url)
        return entry["token"] if entry else None


def profile_token_revision(profile_id: str, url: str) -> int:
    """Stored revision (including deletion tombstones), never an ENV revision."""
    profile_id, url = _profile_id(profile_id), _url(url)
    with _file_lock:
        entry = _entry(_read_store(), profile_id, url)
        return entry["revision"] if entry else 0


def profile_token_source(profile_id: str, url: str) -> str:
    """Secret-free status: environment, environment_unbound, stored or missing."""
    profile_id, url = _profile_id(profile_id), _url(url)
    supplied, token = _environment(url)
    if supplied:
        return "environment" if token else "environment_unbound"
    with _file_lock:
        entry = _entry(_read_store(), profile_id, url)
        return "stored" if entry and entry["token"] else "missing"


def _revision(value: int) -> None:
    if type(value) is not int or value < 0:
        raise CredentialsError("invalid_credential_revision")


def save_profile_token(
    profile_id: str, url: str, token: str | None, *, expected_revision: int,
) -> int:
    """CAS an explicit replacement/deletion. Only ``None`` deletes, never empty input.

    A live key cannot be carried to another URL. A tombstone may be rebound using its
    last revision, so profile URL lifecycle changes cannot accidentally transfer keys.
    """
    profile_id, url = _profile_id(profile_id), _url(url)
    _revision(expected_revision)
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise CredentialsError("invalid_credential")
    with _mutation():
        raw = _upgraded(_read_store())
        previous = raw["profiles"].get(profile_id)
        revision = previous["revision"] if previous else 0
        if revision != expected_revision or (
            previous and previous["url"] != url and previous["token"] is not None
        ):
            raise CredentialConflict()
        revision += 1
        raw["profiles"][profile_id] = {"url": url, "token": token, "revision": revision}
        _write_store(raw)
        return revision


def _legacy_import_plan(raw: dict, profile_id: str, url: str,
                        expected_revision: int) -> tuple[str | None, int, bool]:
    legacy = raw.get("legacy", {}) if raw.get("version") == 2 else raw
    tokens = set()
    for legacy_url, token in legacy.items():
        try:
            matches = _url(legacy_url) == url
        except CredentialsError:
            continue  # Preserve unrelated old data, including unparseable addresses.
        if matches and token:
            tokens.add(token)
    if len(tokens) > 1:
        raise CredentialConflict()
    previous = _upgraded(raw)["profiles"].get(profile_id)
    revision = previous["revision"] if previous else 0
    if not tokens:
        return None, revision, False
    token = next(iter(tokens))
    if not token.strip():
        raise CredentialConflict()
    if previous and previous["token"] is not None:
        if previous["url"] == url and previous["token"] == token:
            return None, revision, False
        raise CredentialConflict()
    if revision != expected_revision:
        raise CredentialConflict()
    return token, revision + 1, True


def preflight_legacy_import(profile_id: str, url: str, *, expected_revision: int = 0) -> int:
    """Read-only predicted revision; reject known conflicts before a caller journals.

    Returns only a revision, never a token. Does not reserve it: the eventual import
    must still revalidate under its OS lock, and genuine races need the caller's fence.
    """
    profile_id, url = _profile_id(profile_id), _url(url)
    _revision(expected_revision)
    with _file_lock:
        _, revision, _ = _legacy_import_plan(_read_store(), profile_id, url, expected_revision)
        return revision


def import_legacy_token(profile_id: str, url: str, *, expected_revision: int = 0) -> int:
    """Explicit, idempotent import; preserve legacy keys and never overwrite a key.

    An existing identical profile key is a no-op even on replay with revision zero.
    Conflicting normalized URL spellings are rejected without exposing either key.
    An absent legacy key is a no-op (no upgrade/write), returning the current revision.
    """
    profile_id, url = _profile_id(profile_id), _url(url)
    _revision(expected_revision)
    with _mutation():
        raw = _read_store()
        token, revision, changed = _legacy_import_plan(raw, profile_id, url, expected_revision)
        if not changed:
            return revision
        upgraded = _upgraded(raw)
        upgraded["profiles"][profile_id] = {"url": url, "token": token, "revision": revision}
        _write_store(upgraded)
        return revision
