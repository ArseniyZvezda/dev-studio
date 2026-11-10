"""Where a person's remote keys are kept (§16.5).

In the user's config directory, beside the application settings (§5.2) — never in the managed
project. A project's ``config.yaml`` is committed and says *which* server and space it syncs
with; the key is what says *who is asking*, and it must not travel with the project.

A file of its own rather than a field of the application ``config.yaml``: that config is
returned whole to the browser by ``GET /api/settings``, and a key has no business there.
Nothing in the API ever returns one — only whether one is stored.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from threading import RLock

from ..core.paths import user_config_dir

log = logging.getLogger(__name__)

TOKEN_ENV_VAR = "VOXARIUM_STUDIO_REMOTE_TOKEN"
"""Overrides the stored key for every server — for a headless Studio that has no settings UI."""

_file_lock = RLock()


def keys_file() -> Path:
    return user_config_dir() / "remote-keys.json"


def _read() -> dict[str, str]:
    path = keys_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Could not read remote keys %s (%s)", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {url: token for url, token in raw.items() if isinstance(url, str) and isinstance(token, str)}


def load_token(url: str) -> str | None:
    """The key for the server at *url* (normalised), or ``None``."""
    override = os.environ.get(TOKEN_ENV_VAR)
    if override:
        return override
    with _file_lock:
        return _read().get(url) or None


def save_token(url: str, token: str | None) -> None:
    """Store the key for *url*, or forget it when *token* is empty."""
    with _file_lock:
        keys = _read()
        if token:
            keys[url] = token
        else:
            keys.pop(url, None)
        path = keys_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=".remote-keys-",
                suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(keys, stream, indent=2)
            # Owner-only where the platform has the notion; on Windows the user's profile
            # directory is already theirs alone.
            with contextlib.suppress(OSError):
                temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
