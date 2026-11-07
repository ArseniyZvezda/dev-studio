"""Captured Studio inputs, loaded only by Context Viewer (§10.4)."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON
from sqlmodel import Field, SQLModel

from .base import UtcDateTime, utcnow


class SessionContext(SQLModel):
    started_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime, nullable=False)
    captured_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime, nullable=False)
    partial: bool = False
    backend: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSON)


class TaskContext(SessionContext, table=True):
    __tablename__ = "task_context"

    task_id: str = Field(primary_key=True, foreign_key="team_task.id", ondelete="CASCADE")


class ChatSessionContext(SessionContext, table=True):
    __tablename__ = "chat_session_context"

    chat_id: str = Field(primary_key=True, foreign_key="agent_chat.id", ondelete="CASCADE")
