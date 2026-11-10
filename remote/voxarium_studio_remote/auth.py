"""Who is connecting (dev spec §16.2).

Today a user is whoever holds a key: a unique token an administrator issued with
``voxarium-studio-remote user add``. Other ways in are expected later, so the server asks an
:class:`Authenticator` rather than a token table — a second scheme is a second class here and
a line in :func:`build_authenticator`, and nothing in the hub changes.

The key itself is never stored. The database holds its SHA-256, which is enough to recognise
it and useless to anyone who reads the file; that is also why a lost key is rotated rather
than recovered.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Mapping, Protocol

from .store import Store, UserRow

TOKEN_PREFIX = "vxs_"


@dataclass(frozen=True, slots=True)
class Credentials:
    """What a connection presented, before anyone has judged it."""

    scheme: str
    """Lower-cased ``Authorization`` scheme — ``bearer`` for a key."""
    value: str


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    color: str

    @classmethod
    def from_row(cls, row: UserRow) -> "User":
        return cls(id=row.id, name=row.name, color=row.color)

    def to_json(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "color": self.color}


class Authenticator(Protocol):
    def authenticate(self, credentials: Credentials) -> User | None:
        """The user these credentials belong to, or ``None`` — never an exception."""


def new_token() -> str:
    """A fresh key. Shown once, to whoever ran the command, and hashed from then on."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenAuthenticator:
    """``Authorization: Bearer <key>`` against the keys in the store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def authenticate(self, credentials: Credentials) -> User | None:
        if credentials.scheme != "bearer" or not credentials.value:
            return None
        # Looked up by hash, so there is no secret-dependent comparison to time.
        row = self._store.user_by_token_hash(hash_token(credentials.value))
        return User.from_row(row) if row else None


class ChainAuthenticator:
    """The first scheme that recognises the credentials wins — where the next ones plug in."""

    def __init__(self, *authenticators: Authenticator) -> None:
        self._authenticators = authenticators

    def authenticate(self, credentials: Credentials) -> User | None:
        for authenticator in self._authenticators:
            user = authenticator.authenticate(credentials)
            if user is not None:
                return user
        return None


def build_authenticator(store: Store) -> Authenticator:
    return ChainAuthenticator(TokenAuthenticator(store))


def credentials_from_headers(headers: Mapping[str, str]) -> Credentials | None:
    """Read the ``Authorization`` header and nothing else.

    No ``?token=`` fallback, unlike the Studio's own browser socket: the only client is a
    local backend, which can set headers, and a key in a URL ends up in every proxy log
    between it and this server.
    """
    header = headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if not scheme or not value.strip():
        return None
    return Credentials(scheme=scheme.lower(), value=value.strip())
