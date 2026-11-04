"""Keeping the Studio up to date with its own repository (§12.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..updates import RelaunchTarget, UpdateRefused
from .deps import Ctx

router = APIRouter(prefix="/updates", tags=["updates"])


class ApplyRequest(BaseModel):
    force: bool = False
    """Update despite a refusal the Studio raised itself — uncommitted changes in its
    checkout, or work still running that the restart will kill. Never overrides git: a
    pull that cannot fast-forward still fails, with git's own message."""


@router.get("")
async def get_updates(ctx: Ctx) -> dict[str, Any]:
    """What the last check found. Does not touch the network — see ``/updates/check``."""
    return ctx.updates.to_json()


@router.post("/check")
async def check_for_updates(ctx: Ctx) -> dict[str, Any]:
    """Fetch now, rather than waiting for the next scheduled check."""
    return await ctx.updates.check()


@router.post("/apply")
async def apply_update(payload: ApplyRequest, ctx: Ctx, request: Request) -> dict[str, Any]:
    """Pull, rebuild and restart. Returns as soon as it starts; progress arrives by WebSocket.

    Where the Studio has to come back up is read from the live server rather than from
    configuration — the socket uvicorn actually bound, and the project actually open — so
    a restart cannot quietly move it to another port or another project.
    """
    server = request.scope.get("server") or (None, None)
    target = RelaunchTarget(
        project=Path(ctx.paths.root),
        host=server[0] or "127.0.0.1",
        port=int(server[1] or 8765),
        dev=bool(getattr(request.app.state, "dev_origins", None)),
        token=ctx.auth.token,
    )
    try:
        return await ctx.updates.apply(target, force=payload.force)
    except UpdateRefused as exc:
        # 409, not 400: nothing about the request is wrong — the Studio is in a state where
        # this cannot be done yet, and `force` is how the client says it knows.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
