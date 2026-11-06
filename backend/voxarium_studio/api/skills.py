"""Skills for the project's agents: ``voxarium_studio/skills/`` and who has been given what (§10.10, §11.1).

The listing is read from disk on every call and every write answers with what it changed as it
now is: a skill arrives by a copy in the file manager or a checkout as often as by the page, so
a client patching its own state from a narrower answer would drift from the folder.

Which members use a skill is answered here too, from the roster, because that is the one thing
the folder cannot say — and it is what the page has to show before a delete takes a skill away
from somebody.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from ..core.context import AppContext
from ..models.teams import TeamMember
from ..teams.docs import FolderInUse
from ..teams.project_skills import MAX_COUNTED_FILES, Skill, SkillChangedOnDisk
from .deps import Ctx

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillCreate(BaseModel):
    name: str
    """The folder's name — lower-case words joined by hyphens, as the Agent Skills format asks."""

    description: str = ""
    """What the skill is for and when to use it; the one line a session reads before deciding to
    open the skill. Written into the new ``SKILL.md``'s frontmatter."""


class SkillWrite(BaseModel):
    content: str
    """The whole ``SKILL.md``, frontmatter included, with ``\\n`` line endings."""

    expected_updated_at: str | None = None
    """The ``updated_at`` the editor read. Sent, a file changed since is refused with 409."""


async def _users(ctx: AppContext) -> dict[str, list[dict[str, Any]]]:
    """Every skill name the roster mentions, with the members who have it, in roster order."""
    async with ctx.db.session() as session:
        members = list((await session.exec(select(TeamMember))).all())
    members.sort(key=lambda member: (member.team_id, member.sort_order, member.name))
    users: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        required = [*(ctx.config.teams.required_skills if member.kind == "agent" else []),
                    *(member.required_skills or [])]
        for name in dict.fromkeys([*(member.skills or []), *required]):
            users.setdefault(name, []).append(
                {
                    "id": member.id,
                    "team_id": member.team_id,
                    "name": member.name,
                    "kind": member.kind,
                    "avatar_color": member.avatar_color,
                    "avatar_icon": member.avatar_icon,
                }
            )
    return users


def _skill_json(ctx: AppContext, skill: Skill, users: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": skill.name,
        "path": ctx.skills.relative(skill.directory),
        "description": skill.description,
        "warning": skill.warning,
        "files": list(skill.files),
        "file_count": skill.file_count,
        "file_count_capped": skill.file_count >= MAX_COUNTED_FILES,
        "updated_at": skill.updated_at,
        "used_by": users,
        "source": skill.source,
        "display_name": skill.display_name or skill.name,
        "scope": skill.scope,
        "backends": list(skill.backends),
        "readonly": skill.readonly,
        "automatic": skill.automatic,
    }


async def _overview(ctx: AppContext) -> dict[str, Any]:
    skills, invalid = ctx.skills.scan()
    users = await _users(ctx)
    on_disk = {skill.name for skill in skills} | {entry.name for entry in invalid}
    return {
        "path": ctx.skills.relative(ctx.skills.directory),
        "absolute_path": str(ctx.skills.directory),
        "skills": [_skill_json(ctx, skill, users.get(skill.name, [])) for skill in skills],
        "invalid": [
            {"name": entry.name, "error": entry.error, "used_by": users.get(entry.name, [])}
            for entry in invalid
        ],
        # Given to somebody and not in the folder — renamed or deleted outside the Studio. Named
        # rather than dropped: the member's list still holds it, and nothing else would say so.
        "missing": [
            {"name": name, "used_by": holders}
            for name, holders in sorted(users.items())
            if name not in on_disk
        ],
    }


@router.get("")
async def list_skills(ctx: Ctx) -> dict[str, Any]:
    return await _overview(ctx)


@router.post("", status_code=201)
async def create_skill(payload: SkillCreate, ctx: Ctx) -> dict[str, Any]:
    """A new skill folder with a ``SKILL.md`` in it. Given to nobody — that is the Teams page's."""
    try:
        ctx.skills.create(payload.name.strip(), payload.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _overview(ctx)


@router.get("/{name}")
async def read_skill(name: str, ctx: Ctx) -> dict[str, Any]:
    """One skill with its ``SKILL.md``, for the page's editor."""
    try:
        skill, text, crlf = ctx.skills.read_text(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    users = (await _users(ctx)).get(skill.name, [])
    return {**_skill_json(ctx, skill, users), "content": text, "crlf": crlf}


@router.put("/{name}")
async def write_skill(name: str, payload: SkillWrite, ctx: Ctx) -> dict[str, Any]:
    """Replace a skill's ``SKILL.md`` and answer as :func:`read_skill` does."""
    try:
        ctx.skills.write(name, payload.content, expected_updated_at=payload.expected_updated_at)
    except SkillChangedOnDisk as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await read_skill(name, ctx)


@router.delete("/{name}")
async def delete_skill(name: str, ctx: Ctx) -> dict[str, Any]:
    """Delete a skill's folder with everything in it, and take it off every member who had it.

    Also what takes a *missing* skill — renamed or deleted outside the Studio — off the members
    still holding it: there is no folder then, only the names. 404 only when neither is there.
    The folder goes first, so a folder the system will not let go of (409) leaves every member
    exactly as they were.
    """
    # §10.10: deleting availability must not silently remove a startup requirement.
    # The settings/editor can explicitly remove the requirement, even after its file
    # disappeared outside Studio; until then it is visible and startup fails clearly.
    async with ctx.db.session() as session:
        required = name in ctx.config.teams.required_skills or any(
            name in (member.required_skills or [])
            for member in (await session.exec(select(TeamMember))).all()
        )
    if required:
        raise HTTPException(status_code=409, detail=(
            f"Skill {name!r} is required at agent startup. Remove it from required skills "
            "in Team Settings and member settings before deleting it."
        ))
    if ctx.skills.is_builtin(name):
        raise HTTPException(status_code=403, detail=f"Built-in skill {name!r} is maintained by its package")
    if name.startswith("installed:"):
        try:
            installed = ctx.skills.get(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if installed is not None:
            raise HTTPException(status_code=403, detail=f"Installed skill {name!r} is maintained outside Studio")
    folder_there = name in ctx.skills.folder_names()
    holders = (await _users(ctx)).get(name, [])
    if not folder_there and not holders:
        raise HTTPException(
            status_code=404,
            detail=f"No skill {name!r} in {ctx.skills.relative(ctx.skills.directory)}",
        )
    if folder_there:
        try:
            await ctx.skills.delete(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FolderInUse as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    if holders:
        await _take_off_members(ctx, name)
    return await _overview(ctx)


async def _take_off_members(ctx: AppContext, name: str) -> None:
    """Remove *name* from every member's skills and write each team touched back to its YAML."""
    touched: set[str] = set()
    async with ctx.db.session() as session:
        for member in (await session.exec(select(TeamMember))).all():
            if name in (member.skills or []):
                member.skills = [entry for entry in member.skills if entry != name]
                session.add(member)
                touched.add(member.team_id)
    async with ctx.db.session() as session:
        for team_id in sorted(touched):
            await ctx.teams.store.save_from_db(session, team_id)
