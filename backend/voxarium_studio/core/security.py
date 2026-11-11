"""Authentication for server mode (§2.5, §12).

The Studio executes arbitrary Python and drives agents with file and shell access, so an
open port on a shared host is a real risk. The policy is fail-closed: binding anywhere
other than loopback requires a token, and the server refuses to start without one.

Full accounts/RBAC are explicitly out of scope for v1 (§14) — this is a single shared
secret for a single-user or small-team tool.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket, status
from starlette.requests import HTTPConnection

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
TOKEN_ENV_VAR = "VOXARIUM_STUDIO_TOKEN"

# Project content is data, including SVGs and review attachments (§2.5). A file opened
# in its own tab must not acquire the Studio's origin and API/credential access.
UNTRUSTED_FILE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}


class InsecureBindError(RuntimeError):
    pass


@dataclass(slots=True)
class AuthSettings:
    token: str | None = None
    host: str = "127.0.0.1"

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @property
    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    @classmethod
    def resolve(cls, *, host: str, token: str | None, allow_insecure: bool = False) -> "AuthSettings":
        """Decide the auth posture for this run, refusing unsafe combinations."""
        settings = cls(token=token or None, host=host)
        if settings.is_loopback or settings.enabled:
            return settings
        if allow_insecure:
            log.warning(
                "Serving on %s with NO token because --allow-insecure was passed. "
                "Anyone who can reach this port can run code on this machine.",
                host,
            )
            return settings
        raise InsecureBindError(
            f"Refusing to bind {host} without authentication.\n"
            f"The Studio runs arbitrary code, so a non-loopback bind requires a token:\n"
            f"  voxarium_studio run <project> --host {host} --token <secret>\n"
            f"or set {TOKEN_ENV_VAR} in the environment. Use --allow-insecure only on a "
            f"trusted private network."
        )

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(32)


def _token_from_request(request: HTTPConnection) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    token = request.headers.get("x-voxarium_studio-token")
    if token:
        return token
    return request.query_params.get("token")


def verify_token(expected: str, provided: str | None) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def _origin(value: str) -> tuple[str, str, int] | None:
    """Parse an HTTP origin, refusing opaque/malformed values rather than guessing."""
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return None
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def browser_access_allowed(connection: HTTPConnection, settings: AuthSettings) -> bool:
    """Reject cross-site requests and DNS rebinding, including on loopback (§2.5).

    CORS alone only prevents reading HTTP responses; it does not stop a simple POST
    or a WebSocket connection. Non-browser clients may omit Origin, but unauthenticated
    loopback requests must still name a loopback Host. Never trust Forwarded headers.
    """
    hosts = connection.headers.getlist("host")
    scheme = "https" if connection.scope["scheme"] in {"https", "wss"} else "http"
    target = _origin(f"{scheme}://{hosts[0]}") if len(hosts) == 1 else None
    if target is None:
        return False
    if settings.is_loopback and not settings.enabled and target[1] not in LOOPBACK_HOSTS:
        return False
    origins = connection.headers.getlist("origin")
    if not origins:
        return True
    if len(origins) != 1:
        return False
    source = _origin(origins[0])
    allowed = getattr(connection.app.state, "dev_origins", ())
    return source is not None and (source == target or source in {_origin(value) for value in allowed})


async def require_auth(request: Request) -> None:
    """FastAPI dependency guarding the REST API."""
    settings: AuthSettings = request.app.state.ctx.auth
    if not browser_access_allowed(request, settings):
        raise HTTPException(status_code=403, detail="Untrusted request host or origin.")
    if not settings.enabled:
        return
    if not verify_token(settings.token or "", _token_from_request(request)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def authorise_websocket(websocket: WebSocket) -> bool:
    """WebSocket handshake auth. Browsers cannot set headers here, so a query
    parameter is accepted as well; the token never leaves the local machine by default."""
    settings: AuthSettings = websocket.app.state.ctx.auth
    if not browser_access_allowed(websocket, settings):
        return False
    if not settings.enabled:
        return True
    return verify_token(settings.token or "", _token_from_request(websocket))
