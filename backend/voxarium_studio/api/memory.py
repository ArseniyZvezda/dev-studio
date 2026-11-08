"""Project memory — the Memory page's routes (§10.12).

The switch and the index cap are not here: they are ``teams.memory_enabled`` and
``teams.memory_index_chars``, saved through ``PUT /api/settings`` like every other team setting.
The overview reports them so the page can show what agents are actually sent.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from ..core.context import AppContext
from ..models.teams import TeamMember
from ..teams.memory import MemoryChangedOnDisk, MemoryEntry, render_index
from .deps import Ctx

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryWrite(BaseModel):
    description: str
    content: str = ""
    create: bool = False
    """Refuse with 409 when the name is taken — what New entry sends."""
    expected_updated_at: str | None = None
    """The ``updated_at`` the editor read. Sent, an entry changed since is refused with 409."""


async def _authors(ctx: AppContext, entries: list[MemoryEntry]) -> dict[str, dict[str, Any]]:
    """The members behind the entries, so the page names people rather than ids."""
    wanted = {entry.author for entry in entries if entry.author}
    if not wanted:
        return {}
    async with ctx.db.session() as session:
        rows = (await session.exec(select(TeamMember).where(TeamMember.id.in_(wanted)))).all()
    return {
        row.id: {
            "id": row.id,
            "team_id": row.team_id,
            "name": row.name,
            "kind": row.kind,
            "avatar_color": row.avatar_color,
            "avatar_icon": row.avatar_icon,
        }
        for row in rows
    }


def _entry_json(entry: MemoryEntry, authors: dict[str, dict[str, Any]], *, content: bool = False) -> dict[str, Any]:
    return {**entry.to_json(content=content), "author_member": authors.get(entry.author or "")}


async def _overview(ctx: AppContext) -> dict[str, Any]:
    entries, invalid = ctx.memory.scan()
    authors = await _authors(ctx, entries)
    limit = ctx.config.teams.memory_index_chars
    return {
        "path": ctx.memory.relative_directory,
        "enabled": ctx.config.teams.memory_enabled,
        "index_chars": limit,
        "index": render_index(entries, limit).to_json(),
        "entries": [_entry_json(entry, authors) for entry in entries],
        "invalid": [item.to_json() for item in invalid],
    }


@router.get("")
async def memory_overview(ctx: Ctx) -> dict[str, Any]:
    try:
        return await _overview(ctx)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{name}")
async def read_entry(name: str, ctx: Ctx) -> dict[str, Any]:
    try:
        entry = ctx.memory.get(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No memory entry named {name!r}")
    return _entry_json(entry, await _authors(ctx, [entry]), content=True)


@router.put("/{name}")
async def write_entry(name: str, payload: MemoryWrite, ctx: Ctx) -> dict[str, Any]:
    try:
        entry = await ctx.memory.write(
            name,
            description=payload.description,
            content=payload.content,
            author=None,
            create=payload.create,
            expected_updated_at=payload.expected_updated_at,
        )
    except (FileExistsError, MemoryChangedOnDisk) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _entry_json(entry, {}, content=True)


@router.delete("/{name}")
async def delete_entry(name: str, ctx: Ctx) -> dict[str, Any]:
    try:
        await ctx.memory.delete(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return await _overview(ctx)
