"""What the project's automations did (§6.5). The automations themselves live in YAML."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel

from .base import UtcDateTime, new_id, utcnow


class AutomationRun(SQLModel, table=True):
    """One firing of an automation: what set it off and how its action went.

    A log rather than state — nothing reads it back to decide anything — so it is pruned to
    the newest rows (``automations.engine.MAX_LOG_ROWS``) instead of growing with the project.
    """

    __tablename__ = "automation_run"

    id: str = Field(default_factory=lambda: new_id("arun"), primary_key=True)
    automation_id: str = Field(index=True)
    automation_name: str = ""
    """As it was called when it fired: the log outlives a rename and a delete."""

    status: str = "ok"
    """``running`` while the action's work goes on — a pipeline run, a graph update — then
    ``ok`` | ``error`` | ``cancelled``; ``skipped`` is a guard refusing (a loop, a flood)."""

    cause: str = ""
    """What set it off, in words: ``DP-04 → needs_review``, ``3 files under models/``."""

    detail: str = Field(default="", sa_column=Column(Text))
    """What the action answered, or why it failed."""

    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    """The trigger's values and the action's references (``run_id``), for the page's links."""

    created_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)
