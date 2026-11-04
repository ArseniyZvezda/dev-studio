"""Chats with a team member — the floating chat popup's API (§10.9, §11.1).

A chat is a member's session with the user at the other end instead of a brief: the same
tools, the same MCP servers, no card on the board. ``teams/chat.py`` owns the rules; this
is the HTTP shape of them.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.context import AppContext
from ..teams.chat import ChatBusy, UserContext
from .deps import Ctx

router = APIRouter(prefix="/chats", tags=["chats"])


class ChatCreate(BaseModel):
    member_id: str
    title: str = ""
    producer_mode: bool = False
    locale: Literal["en", "ru"] = "en"


class ChatContextIn(BaseModel):
    """Where the user is in the Studio at the moment they send (``UserContext``)."""

    page: str = ""
    route: str = ""
    title: str = ""
    document: str | None = None
    file: str | None = None
    selection: str | None = None

    def to_context(self) -> UserContext:
        return UserContext(
            page=self.page,
            route=self.route,
            title=self.title,
            document=self.document or None,
            file=self.file or None,
            selection=self.selection or None,
        )


class ChatMessageIn(BaseModel):
    text: str = ""
    attachments: list[str] = Field(default_factory=list)
    context: ChatContextIn = Field(default_factory=ChatContextIn)


class ProducerStart(BaseModel):
    locale: Literal["en", "ru"] = "en"
    policy: Literal["careful", "independent"] | None = None
    estimate_task_cost_usd: float = Field(default=150.0, gt=0, allow_inf_nan=False)


class ProducerWorkspace(BaseModel):
    locale: Literal["en", "ru"] = "en"


class ProducerPolicy(BaseModel):
    policy: Literal["careful", "independent"]


class ProducerSettings(BaseModel):
    estimate_task_cost_usd: float = Field(gt=0, allow_inf_nan=False)


class ProducerApproval(BaseModel):
    version: int


async def _chat_or_404(ctx: AppContext, chat_id: str) -> dict[str, Any]:
    chat = await ctx.chats.get_chat(chat_id)
    if chat is None:
        raise HTTPException(404, f"No chat {chat_id!r}")
    return ctx.chats.to_json(chat)


@router.get("")
async def list_chats(ctx: Ctx) -> list[dict[str, Any]]:
    """Every chat, newest activity first — the popup's list."""
    return [ctx.chats.to_json(chat) for chat in await ctx.chats.list_chats()]


@router.post("", status_code=201)
async def create_chat(body: ChatCreate, ctx: Ctx) -> dict[str, Any]:
    try:
        chat = await ctx.chats.create_chat(
            body.member_id, title=body.title, producer_mode=body.producer_mode, locale=body.locale,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ctx.chats.to_json(chat)


@router.get("/{chat_id}")
async def get_chat(chat_id: str, ctx: Ctx) -> dict[str, Any]:
    """One chat with its whole transcript."""
    chat = await _chat_or_404(ctx, chat_id)
    messages = await ctx.chats.messages(chat_id)
    return {**chat, "messages": [message.model_dump(mode="json") for message in messages]}


@router.delete("/{chat_id}")
async def delete_chat(chat_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        deleted = await ctx.chats.delete_chat(chat_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, f"No chat {chat_id!r}")
    return {"chat_id": chat_id, "deleted": True}


@router.post("/{chat_id}/messages", status_code=202)
async def send_message(chat_id: str, body: ChatMessageIn, ctx: Ctx) -> dict[str, Any]:
    """Say something to the member. Answers as soon as the turn has started; the reply
    streams as ``team.chat.message`` events and lands in the transcript."""
    try:
        message = await ctx.chats.send(chat_id, body.text, body.context.to_context(), attachments=body.attachments)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChatBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    chat = await _chat_or_404(ctx, chat_id)
    return {"chat": chat, "message": message.model_dump(mode="json")}


@router.post("/{chat_id}/cancel")
async def cancel_turn(chat_id: str, ctx: Ctx) -> dict[str, Any]:
    await _chat_or_404(ctx, chat_id)
    cancelled = await ctx.chats.cancel(chat_id)
    return {"chat_id": chat_id, "cancelled": cancelled}


@router.post("/{chat_id}/producer/start", status_code=202)
async def start_producer(chat_id: str, body: ProducerStart, ctx: Ctx) -> dict[str, Any]:
    try:
        chat = await ctx.chats.producer.enable(
            chat_id, body.locale, body.policy,
            estimate_task_cost_usd=(body.estimate_task_cost_usd
                                    if "estimate_task_cost_usd" in body.model_fields_set else None),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ChatBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ctx.chats.to_json(chat)


@router.post("/{chat_id}/producer/workspace")
async def use_producer_workspace(chat_id: str, body: ProducerWorkspace, ctx: Ctx) -> dict[str, Any]:
    try:
        chat = await ctx.chats.use_producer_workspace(chat_id, locale=body.locale)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ChatBusy, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return ctx.chats.to_json(chat)


@router.patch("/{chat_id}/producer/policy")
async def set_producer_policy(chat_id: str, body: ProducerPolicy, ctx: Ctx) -> dict[str, Any]:
    try:
        chat = await ctx.chats.producer.set_policy(chat_id, body.policy)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, ChatBusy) as exc:
        raise HTTPException(409, str(exc)) from exc
    return ctx.chats.to_json(chat)


@router.patch("/{chat_id}/producer/settings")
async def set_producer_settings(chat_id: str, body: ProducerSettings, ctx: Ctx) -> dict[str, Any]:
    try:
        chat = await ctx.chats.producer.set_estimate_task_cost(chat_id, body.estimate_task_cost_usd)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, ChatBusy) as exc:
        raise HTTPException(409, str(exc)) from exc
    return ctx.chats.to_json(chat)


@router.post("/{chat_id}/producer/approve", status_code=202)
async def approve_producer_plan(chat_id: str, body: ProducerApproval, ctx: Ctx) -> dict[str, Any]:
    try:
        chat = await ctx.chats.producer.approve_plan(chat_id, body.version)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ChatBusy, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return ctx.chats.to_json(chat)


@router.post("/{chat_id}/producer/stop")
async def stop_producer(chat_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        async with ctx.chats.lock(chat_id):
            await ctx.chats.producer.disable(chat_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return await _chat_or_404(ctx, chat_id)


async def control_producer(chat_id: str, action: str, ctx: Ctx) -> dict[str, Any]:
    try:
        chat = await getattr(ctx.chats.producer, action)(chat_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, ChatBusy) as exc:
        raise HTTPException(409, str(exc)) from exc
    return ctx.chats.to_json(chat)


@router.post("/{chat_id}/producer/pause")
async def pause_producer(chat_id: str, ctx: Ctx) -> dict[str, Any]:
    return await control_producer(chat_id, "pause", ctx)


@router.post("/{chat_id}/producer/resume")
async def resume_producer(chat_id: str, ctx: Ctx) -> dict[str, Any]:
    return await control_producer(chat_id, "resume", ctx)
