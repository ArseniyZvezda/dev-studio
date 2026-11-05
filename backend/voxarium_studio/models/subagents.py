"""Durable workers belonging to one task, not separate board cards (§10.3)."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel

from .base import UtcDateTime, new_id, utcnow


class TaskSubagent(SQLModel, table=True):
    __tablename__ = "task_subagent"

    id: str = Field(default_factory=lambda: new_id("subagent"), primary_key=True)
    task_id: str = Field(foreign_key="team_task.id", index=True)
    member_id: str
    description: str
    backend: str
    model: str
    effort: str | None = None
    status: str = "queued"
    session_id: str | None = None
    summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    total_tokens: int = 0
    total_uncached_tokens: int = 0
    total_cached_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0
    tool_uses: int = 0
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UtcDateTime)


class SubagentMessage(SQLModel, table=True):
    __tablename__ = "subagent_message"

    id: str = Field(default_factory=lambda: new_id("submsg"), primary_key=True)
    subagent_id: str = Field(foreign_key="task_subagent.id", index=True)
    kind: str
    text: str = Field(default="", sa_column=Column(Text, nullable=False))
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
