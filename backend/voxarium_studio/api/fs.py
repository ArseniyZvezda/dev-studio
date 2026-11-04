"""Choosing a folder on the machine the backend runs on.

Two ways to do the same thing, because neither works everywhere: the native dialog is
what a desktop user expects and cannot be offered remotely, and the in-app browser works
anywhere but is a web reimplementation of Explorer. The Open Project dialog offers the
first and falls back to the second — see :mod:`voxarium_studio.core.picker` and
:mod:`voxarium_studio.core.browse` for what each one is and is not allowed to do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..core import browse, picker
from .deps import Ctx

router = APIRouter(prefix="/fs", tags=["filesystem"])


class PickRequest(BaseModel):
    start_path: str | None = None
    """Where the dialog opens. Defaults to the current project's parent, which is where
    a sibling game project is most likely to be."""


# Neither of the next two takes a `Ctx`: they are about the machine, not the open
# project. Auth still applies — it is declared once on the parent `/api` router.


@router.get("/browse")
async def browse_directory(
    path: str = Query("", description="Directory to list. Empty lists the drives/roots."),
) -> dict[str, Any]:
    try:
        return browse.list_directory(path)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/inspect")
async def inspect_path(path: str = Query(..., min_length=1)) -> dict[str, Any]:
    return browse.inspect(path)


@router.get("/picker")
async def picker_status(ctx: Ctx) -> dict[str, Any]:
    """Whether the UI should offer the native dialog at all, and why not if it should not."""
    supported, reason = picker.dialog_supported()
    remote = not ctx.auth.is_loopback
    return {
        "available": supported and not remote,
        "reason": _unavailable_reason(supported, reason, remote),
        "home": str(browse.home_directory()),
        "default_start": str(ctx.paths.root.parent),
    }


@router.post("/pick")
async def pick_directory(payload: PickRequest, ctx: Ctx, request: Request) -> dict[str, Any]:
    """Open the machine's folder dialog and block until the user chooses or cancels.

    Deliberately long-running: the response *is* the user's answer. The client should
    have no timeout of its own on this call, and the dialog has one of its own so a
    forgotten window cannot hold the connection open forever.

    `request.is_disconnected` is what closes the window when the answer stops mattering.
    uvicorn runs this handler to completion whether or not the browser is still there, so
    a reloaded tab would otherwise leave a modal dialog on the desktop until that timeout.
    """
    if not ctx.auth.is_loopback:
        # The dialog would appear on the server's desktop, not the caller's.
        raise HTTPException(
            status_code=409,
            detail=(
                "This Studio is not running on your machine, so it cannot open a folder "
                "dialog for you. Browse to the project folder instead."
            ),
        )

    start = Path(payload.start_path) if payload.start_path else ctx.paths.root.parent
    result = await picker.pick_directory(
        start=start if start.is_dir() else None,
        abandoned=request.is_disconnected,
    )
    payload_json = result.to_json()
    if result.path is not None:
        payload_json["inspection"] = browse.inspect(result.path)
    return payload_json


def _unavailable_reason(supported: bool, reason: str, remote: bool) -> str:
    if remote:
        return "The Studio is not running on this machine."
    return "" if supported else reason
