"""The Studio's half of the remote's wire protocol (§16.3).

The remote is a package of its own (``remote/``) that shares no code with the Studio, so the
handful of names both sides must agree on are restated here. :data:`PROTOCOL_VERSION` is what
keeps the two copies honest: the server announces its own in ``hello`` and a mismatch stops
the client instead of letting it guess.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

PROTOCOL_VERSION = 1

# Application close codes the remote uses (``voxarium_studio_remote/protocol.py``).
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORISED = 4401

# What the local backend closes a browser's collaboration socket with (§16.4).
CLOSE_COLLAB_UNAVAILABLE = 4000
"""No room for this document right now — the editor carries on alone and asks again later."""
CLOSE_COLLAB_RESET = 4001
"""The room ended because the file was replaced or deleted — reopen the document."""

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

MAX_SPACE_NAME = 64


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
