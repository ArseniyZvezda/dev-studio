"""Online sync settings and status (§16.5).

Kept apart from ``PUT /api/settings`` because one of its fields is not a setting of the
project at all: the key is the user's and is stored in their own config directory. This
endpoint takes it, stores it there, and never gives it back — a status says only whether one
is stored.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ..core.config import save_config
from ..sync import credentials
from ..sync.protocol import check_space_name, normalise_url
from .deps import Ctx

router = APIRouter(prefix="/sync", tags=["sync"])


class SyncUpdate(BaseModel):
    """A patch: a field left out is left alone."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    url: str | None = None
    space: str | None = None
    token: str | None = None
    """The user's key for the server at ``url``. An empty string forgets the stored one."""


@router.get("")
async def get_sync(ctx: Ctx) -> dict[str, Any]:
    return ctx.sync.status()


@router.put("")
async def update_sync(payload: SyncUpdate, request: Request, ctx: Ctx) -> dict[str, Any]:
    """Save the settings and reconnect with them.

    Validated whole before anything is stored: an address that does not parse must not leave
    a key filed under it. The address and the space go to the project's ``config.yaml``; the
    key goes to the user's config directory, under the address it is for.
    """
    sent = payload.model_fields_set
    # The lock every other settings write holds (§5.2), so two saves cannot interleave.
    async with request.app.state.project_lock:
        draft = ctx.config.sync.model_copy()
        if "url" in sent and payload.url is not None:
            draft.url = payload.url.strip()
        if "space" in sent and payload.space is not None:
            draft.space = payload.space.strip()
        if "enabled" in sent and payload.enabled is not None:
            draft.enabled = payload.enabled

        url: str | None = None
        try:
            if draft.url:
                url = normalise_url(draft.url)
            if draft.space:
                draft.space = check_space_name(draft.space)
            if draft.enabled and (url is None or not draft.space):
                raise ValueError("Online sync needs a server address and a space")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if "token" in sent and payload.token is not None:
            if url is None:
                raise HTTPException(status_code=400, detail="A key is stored for a server address")
            credentials.save_token(url, payload.token.strip() or None)

        if draft != ctx.config.sync:
            ctx.config.sync = draft
            save_config(ctx.paths, ctx.config)
        return await ctx.sync.reconfigure()
