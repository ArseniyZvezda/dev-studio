"""Runtime state of pipeline execution (§8). Pipeline *definitions* live in YAML."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, SQLModel

from .base import EnumString, UtcDateTime, new_id, utcnow


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    """Held at a node whose ``pause_on_complete`` is set, waiting for the user (§8.2).

    Live state, not history: the job is still in the engine holding its place in the
    graph, so a run left in this state by a server restart is stale in exactly the way a
    ``running`` one is.
    """

    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    """Finished, and holding the run open. Its outputs are already stored — the node did
    its work — so this says which node the Continue button belongs to, and nothing more."""

    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    CACHED = "cached"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCESS, RunStatus.ERROR, RunStatus.CANCELLED}
)


class Run(SQLModel, table=True):
    """One execution of a pipeline."""

    __tablename__ = "run"

    id: str = Field(default_factory=lambda: new_id("run"), primary_key=True)
    pipeline_id: str = Field(index=True)
    pipeline_name: str = ""
    # Stored as its string value rather than SQLAlchemy's default enum-name mapping, so
    # the column stays readable to plain SQL and to anything reading app.db directly.
    status: RunStatus = Field(
        default=RunStatus.PENDING,
        sa_column=Column(EnumString(RunStatus), index=True, nullable=False),
    )
    trigger: str = "manual"
    created_by: str | None = None
    """Reserved for the multi-user case flagged in spec §14."""

    start_node_id: str | None = None
    """Set when the run was started from a single node rather than the whole graph."""

    use_cache: bool = True
    params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)
    started_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UtcDateTime)


class NodeRun(SQLModel, table=True):
    """Per-node state within a run: status, timing, resolved I/O, log location."""

    __tablename__ = "node_run"
    __table_args__ = (Index("ix_node_run_run_node", "run_id", "node_id", unique=True),)

    id: str = Field(default_factory=lambda: new_id("nr"), primary_key=True)
    run_id: str = Field(foreign_key="run.id", index=True)
    node_id: str
    node_type: str
    status: NodeStatus = Field(
        default=NodeStatus.PENDING,
        sa_column=Column(EnumString(NodeStatus), index=True, nullable=False),
    )
    cache_key: str | None = Field(default=None, index=True)
    from_cache: bool = False
    inputs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    outputs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))
    exit_code: int | None = None
    log_path: str | None = None
    started_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UtcDateTime)


class NodeCacheEntry(SQLModel, table=True):
    """Memoised node outputs keyed by (node type + version + inputs + input file hashes).

    Lets a re-run of one node reuse upstream results instead of recomputing the
    whole graph — the incremental-build behaviour called for in §6.3/§8.2.
    """

    __tablename__ = "node_cache"

    cache_key: str = Field(primary_key=True)
    node_type: str = Field(index=True)
    outputs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    run_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    last_used_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)
    hit_count: int = 0
