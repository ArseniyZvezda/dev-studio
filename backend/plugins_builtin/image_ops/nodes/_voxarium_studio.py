"""Dependency-free helper implementing the node-script protocol.

A node script runs in the plugin's own venv, where ``voxarium_studio`` itself is not installed,
so the contract is intentionally plain: JSON on stdin, logs on stdout, results written
to ``$VOXARIUM_STUDIO_OUTPUT_FILE``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

OUTPUT_SENTINEL = "::voxarium_studio:output::"
PROGRESS_SENTINEL = "::voxarium_studio:progress::"


def read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {"inputs": {}, "params": {}, "resolved": {}, "context": {}}
    return json.loads(raw)


def resolved(payload: dict[str, Any]) -> dict[str, Any]:
    """Inputs merged over params — a connected edge beats a literal value."""
    values = dict(payload.get("params") or {})
    values.update({k: v for k, v in (payload.get("inputs") or {}).items() if v is not None})
    if payload.get("resolved"):
        values.update({k: v for k, v in payload["resolved"].items() if v is not None})
    return values


def settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Plugin settings for this project, as edited on the plugin's own page (§9.4)."""
    return dict(payload.get("settings") or {})


def log(message: str) -> None:
    print(message, flush=True)


def progress(
    *,
    percent: float | None = None,
    label: str | None = None,
    detail: str | None = None,
    eta_s: float | None = None,
    step: int | None = None,
    steps: int | None = None,
) -> None:
    """Say how far along this node is. The canvas draws it on the node.

    A frame is advisory and nothing keeps it: it is how a node that takes minutes stops
    looking hung, not a result. Send whatever is actually known — ``label`` alone is a
    stage name with an indeterminate bar under it, and ``percent`` is the overall 0-100.

    Written on stdout like a log line, but under its own prefix, so the host takes it out
    of the stream instead of printing it. An old host that does not know the prefix prints
    the frame and carries on, which is why this is never worth guarding.
    """
    frame = {
        "percent": percent,
        "label": label,
        "detail": detail,
        "eta_s": eta_s,
        "step": step,
        "steps": steps,
    }
    print(
        PROGRESS_SENTINEL + json.dumps({k: v for k, v in frame.items() if v is not None}),
        flush=True,
    )


def project_root(payload: dict[str, Any]) -> Path:
    context = payload.get("context") or {}
    return Path(context.get("project_root") or os.environ.get("VOXARIUM_STUDIO_PROJECT_ROOT", "."))


def assets_dir(payload: dict[str, Any]) -> Path:
    context = payload.get("context") or {}
    path = Path(context.get("assets_dir") or os.environ.get("VOXARIUM_STUDIO_ASSETS_DIR", "."))
    path.mkdir(parents=True, exist_ok=True)
    return path


def emit(outputs: dict[str, Any]) -> None:
    """Publish node results. Falls back to the stdout sentinel if no file is configured."""
    target = os.environ.get("VOXARIUM_STUDIO_OUTPUT_FILE")
    encoded = json.dumps(outputs, default=str)
    if target:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(encoded, encoding="utf-8")
    else:
        print(f"{OUTPUT_SENTINEL}{encoded}", flush=True)


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(code)


def require_file(raw: Any, label: str) -> Path:
    if not raw:
        fail(f"{label} is required but was not provided")
    path = Path(str(raw))
    if not path.exists():
        fail(f"{label} not found: {path}")
    return path
