"""The wire protocol between a local Studio and the remote (dev spec §16.3).

One WebSocket per local backend, JSON text frames, each an object with a ``t`` naming the
message. Binary payloads — Yjs updates and awareness — travel base64-encoded inside those
frames: they are small, and one framing for everything keeps the relay in the local backend
a matter of adding and removing a session id.

The local backend holds a copy of these names (``voxarium_studio/sync/protocol.py``). The two
are kept in step by :data:`PROTOCOL_VERSION`: the server announces it in ``hello`` and a
client that speaks another one stops instead of guessing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import PurePosixPath

PROTOCOL_VERSION = 1

# The same bounds the Studio's own DocStore enforces (``teams/docs.py``): the server never
# stores a document a local Studio would refuse to write to disk.
ALLOWED_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
MAX_DOC_BYTES = 1_000_000
MAX_PATH_CHARS = 512
MAX_UPDATE_BYTES = 8 * 1024 * 1024
MAX_SPACE_NAME = 64

# Close codes in the application range (4000–4999), so a client can tell "your token is
# wrong, stop retrying" from "the network dropped, try again".
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORISED = 4401
CLOSE_OVERLOADED = 4408


class ProtocolError(ValueError):
    """A frame the server will not act on. Reported to the sender; the connection stays."""


def content_hash(content: str) -> str:
    """How both sides name a version of a document's text."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def check_path(raw: object) -> str:
    """A document path as the server stores it: relative, posix, inside the docs tree.

    The path is written to disk by every other Studio in the space, so this is the same
    trust boundary ``DocStore.resolve`` is — traversal and unexpected suffixes are refused
    rather than normalised.
    """
    if not isinstance(raw, str):
        raise ProtocolError("A document path is required")
    path = raw.strip()
    if not path or len(path) > MAX_PATH_CHARS:
        raise ProtocolError(f"Illegal document path: {raw!r}")
    if "\\" in path or path.startswith("/") or any(ord(char) < 32 for char in path):
        raise ProtocolError(f"Illegal document path: {raw!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ProtocolError(f"Illegal document path: {raw!r}")
    if PurePosixPath(path).suffix.lower() not in ALLOWED_SUFFIXES:
        raise ProtocolError(f"Not a document: {raw!r}")
    return path


def check_content(raw: object) -> str:
    if not isinstance(raw, str):
        raise ProtocolError("Document content must be text")
    if len(raw.encode("utf-8")) > MAX_DOC_BYTES:
        raise ProtocolError(f"Document exceeds {MAX_DOC_BYTES} bytes")
    return raw


def check_space_name(raw: object) -> str:
    """A space's name: lowercase, URL-safe, and short enough to type into a settings field."""
    name = raw.strip().lower() if isinstance(raw, str) else ""
    if not name or len(name) > MAX_SPACE_NAME:
        raise ProtocolError("A space name is required")
    if not name[0].isalnum() or any(not (char.isalnum() or char in "._-") for char in name):
        raise ProtocolError(
            "A space name is letters, digits, '.', '_' and '-', starting with a letter or digit"
        )
    if not name.isascii():
        raise ProtocolError("A space name must be ASCII")
    return name


def decode_update(raw: object) -> bytes:
    if not isinstance(raw, str):
        raise ProtocolError("An update is required")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("An update must be base64") from exc
    if len(data) > MAX_UPDATE_BYTES:
        raise ProtocolError("Update is too large")
    return data


def encode_update(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
