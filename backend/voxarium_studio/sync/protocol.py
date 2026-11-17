"""The Studio's half of the remote's wire protocol (§16.3).

The remote is a package of its own (``remote/``) that shares no code with the Studio, so the
handful of names both sides must agree on are restated here. :data:`PROTOCOL_VERSION` is what
keeps the two copies honest: the server announces its own in ``hello`` and a mismatch stops
the client instead of letting it guess.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

# Staged delivery (§16.7–16.9, VS-180): these v2 declarations are shared with Remote,
# NOT permission to switch the legacy runner. service.py still imports PROTOCOL_VERSION
# and socket_url(), deliberately pinned to v1 until I9–I16 and L are accepted.
WIRE_V2_VERSION = 2
CATALOG_API_VERSION = 2
LEGACY_RUNNER_PROTOCOL_VERSION = 1
PROTOCOL_VERSION = LEGACY_RUNNER_PROTOCOL_VERSION
V2_CAPABILITIES = (
    "project_catalog", "explicit_project_create", "strict_connect",
    "project_snapshot", "legacy_resolution",
)

# Application close codes the remote uses (``voxarium_studio_remote/protocol.py``).
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORISED = 4401
CLOSE_PROJECT_NOT_FOUND = 4404
CLOSE_OVERLOADED = 4408

# What the local backend closes a browser's collaboration socket with (§16.4).
CLOSE_COLLAB_UNAVAILABLE = 4000
"""No room for this document right now — the editor carries on alone and asks again later."""
CLOSE_COLLAB_RESET = 4001
"""The room ended because the file was replaced or deleted — reopen the document."""

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

MAX_SPACE_NAME = 64


def check_project_id(raw: object) -> str:
    """Exact v2 identity, never a normalized legacy selector (§16.7)."""
    if not isinstance(raw, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", raw, re.ASCII) is None:
        raise ValueError("Invalid project ID")
    return raw


def check_display_name(raw: object) -> str:
    """New/edited labels only; migration preserves historical text literally."""
    if not isinstance(raw, str):
        raise ValueError("Invalid display name")
    # Validate before stripping: a trailing newline/control is not whitespace repair.
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or
           ord(c) in {0x2028, 0x2029, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e,
                      0x2066, 0x2067, 0x2068, 0x2069} for c in raw):
        raise ValueError("Invalid display name")
    name = unicodedata.normalize("NFC", raw).strip()
    if not 1 <= len(name) <= 120 or len(name.encode("utf-8")) > 480:
        raise ValueError("Invalid display name")
    return name


def check_color(raw: object) -> str:
    if not isinstance(raw, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", raw) is None:
        raise ValueError("Invalid color")
    return raw.lower()


def content_hash(content: str) -> str:
    """How both sides name a version of a document's text."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalise_url(raw: str) -> str:
    """The server's address as it is stored: scheme, host, optional port and path prefix.

    People paste what they have — ``studio.example.com``, an ``https://`` link, a ``wss://``
    one — so every spelling of one server is folded to one string, which is also the key its
    token is kept under. A bare host means HTTPS: the address carries a key.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("A server address is required")
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme.lower(), parts.scheme.lower())
    if scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"Not a server address: {raw!r}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("The server address must not carry credentials, a query or a fragment")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


def socket_url(base: str, space: str) -> str:
    """Where the sync socket is, for a normalised *base*."""
    parts = urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, f"{parts.path}/api/v1/sync", f"space={space}", ""))


def is_insecure(base: str) -> bool:
    """Whether the key would cross a network in the clear."""
    parts = urlsplit(base)
    return parts.scheme == "http" and (parts.hostname or "") not in LOOPBACK_HOSTS


def check_space_name(raw: str) -> str:
    """The remote's own rule for a space name, applied before a connection is spent on it."""
    name = (raw or "").strip().lower()
    if not name or len(name) > MAX_SPACE_NAME or not name.isascii():
        raise ValueError("A space name is 1–64 ASCII characters")
    if not name[0].isalnum() or any(not (char.isalnum() or char in "._-") for char in name):
        raise ValueError(
            "A space name is letters, digits, '.', '_' and '-', starting with a letter or digit"
        )
    return name
