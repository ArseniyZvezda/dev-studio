"""Import-string ASGI entry point, used by ``--reload`` and by external servers.

Configuration comes from the environment because uvicorn's reloader re-imports this
module in a fresh process and cannot receive Python objects.
"""

from __future__ import annotations

import os

from .core.security import AuthSettings
from .core.workspace import resolve_startup_project
from .main import create_app

# `voxarium_studio run --reload` always sets the variable, so the fallback is for an external
# server started by hand — and it makes the same choice the CLI would (§5.1): the project
# that was open last, or the built-in demo project, never the directory uvicorn was run in.
_project = os.environ.get("VOXARIUM_STUDIO_PROJECT") or str(resolve_startup_project().root)
_dev = os.environ.get("VOXARIUM_STUDIO_DEV") == "1"

app = create_app(
    _project,
    auth=AuthSettings.resolve(
        token=os.environ.get("VOXARIUM_STUDIO_TOKEN") or None,
        host=os.environ.get("VOXARIUM_STUDIO_HOST", "127.0.0.1"),
        allow_insecure=os.environ.get("VOXARIUM_STUDIO_ALLOW_INSECURE") == "1",
    ),
    dev_origins=["http://localhost:5173", "http://127.0.0.1:5173"] if _dev else None,
)
