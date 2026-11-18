"""Project-side storage of the future AI Control Protocol (§16.12, ai-access-design §3, §4, §7).

Four tables in the managed project's ``app.db``, every one prefixed ``ai_``:

* :class:`AiCommandLedger` — the authoritative idempotency ledger of remote mutations;
* :class:`AiResourceProvenance` — immutable ``remote_origin`` of a task/chat plus its
  mutable ``last_actor`` and the durable integer ``revision`` sidecar used for CAS;
* :class:`AiMessageProvenance` — which transcript message a remote command wrote;
* :class:`AiAuditReceipt` — append-only audit receipts (no prompts, no transcript).

**No credential lives here** (design §3): no device secret, key, token, provider session
id or URL. Only non-secret identifiers, hashes, states and timestamps. Copying ``app.db``
therefore never activates an export: the ledger key includes the executor registration
and the ``export_id``, both of which resolve only through the private per-user registry.
Text payloads (prompts, notes, briefs) are never stored in the ledger either — only their
SHA-256 inside ``payload_hash`` (design §7).

All times are signed int64 Unix UTC milliseconds, like the wire (design §7), not
``datetime`` columns. Nothing here is used by the running application yet: the tables are
created by ``create_all`` and stay empty until the executor stages (design §11 S-A3/S-A4).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Column, DDL, Index, event
from sqlmodel import Field, SQLModel

from .base import new_id

LEDGER_KEY_INDEX = "ux_ai_command_ledger_key"
"""The unique ledger key of design §7 — a named index so a migration can find it."""


class AiCommandLedger(SQLModel, table=True):
    """One remote mutation command, keyed exactly as design §7 requires.

    A compacted row (``compacted=True``) is a dedupe *tombstone*: ``safe_result`` is
    cleared after the 30-day retention but the key, ``payload_hash``, IDs and state stay
    for the whole life of the export, so a late duplicate is still recognised.
    """

    __tablename__ = "ai_command_ledger"
    __table_args__ = (
        Index(
            LEDGER_KEY_INDEX,
            "server_instance_id",
            "executor_registration_id",
            "export_id",
            "source_registration_id",
            "command_id",
            unique=True,
        ),
        Index("ix_ai_command_ledger_state", "state"),
        Index("ix_ai_command_ledger_resource", "resource_kind", "resource_id"),
    )

    id: str = Field(default_factory=lambda: new_id("aicmd"), primary_key=True)

    # -- the ledger key (§7)
    server_instance_id: str
    executor_registration_id: str
    export_id: str
    source_registration_id: str
    command_id: str

    # -- principal and consent the command was admitted under (§4)
    user_id: str
    grant_id: str
    grant_revision: int = Field(sa_column=Column(BigInteger, nullable=False))

    # -- what was asked: op, hash of the canonical semantic payload, pinned target (§3, §7)
    op: str
    payload_hash: str
    persistent_id: str
    storage_epoch: str
    export_revision: int = Field(sa_column=Column(BigInteger, nullable=False))
    executor_epoch: str
    issued_at: int = Field(sa_column=Column(BigInteger, nullable=False))

    # -- what it touched
    resource_kind: str | None = None
    resource_id: str | None = None
    message_id: str | None = None
    run_id: str | None = None

    # -- outcome
    state: str
    """A :class:`~voxarium_studio.ai_access.protocol.CommandState` value."""
    error_code: str | None = None
    """A fixed :class:`~voxarium_studio.ai_access.protocol.ErrorCode`, never error text."""
    safe_result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    """Allowlisted IDs/states only — never a text payload. Cleared by compaction."""

    # -- dispatch fence for task.start / chat.send (§7.3, §7.5)
    dispatch_token: str | None = None
    dispatch_claimed: bool = False
    dispatching_at: int | None = Field(default=None, sa_column=Column(BigInteger))

    compacted: bool = False
    created_at: int = Field(sa_column=Column(BigInteger, nullable=False))
    updated_at: int = Field(sa_column=Column(BigInteger, nullable=False))


class AiResourceProvenance(SQLModel, table=True):
    """Origin, last actor and durable revision of one task or chat (design §4, §7.1).

    ``origin_*`` is written once, when a remote command creates the object, and never
    changed afterwards; a locally created object has a row only once its revision is
    first bumped, with every ``origin_*`` null. ``revision`` starts at 1 and increases on
    every local and remote edit/note/send/start/cancel/status change — the CAS value of
    ``expected_resource_revision``.
    """

    __tablename__ = "ai_resource_provenance"

    resource_kind: str = Field(primary_key=True)
    resource_id: str = Field(primary_key=True)
    revision: int = Field(default=1, sa_column=Column(BigInteger, nullable=False))

    # -- immutable remote_origin
    origin_user_id: str | None = None
    origin_registration_id: str | None = None
    origin_grant_id: str | None = None
    origin_grant_revision: int | None = Field(default=None, sa_column=Column(BigInteger))
    origin_command_id: str | None = None
    origin_export_id: str | None = None
    origin_created_at: int | None = Field(default=None, sa_column=Column(BigInteger))

    # -- mutable last_actor
    last_actor_kind: str = "local"
    """``local`` or ``remote``. A local actor carries no IDs here."""
    last_user_id: str | None = None
    last_registration_id: str | None = None
    last_grant_id: str | None = None
    last_grant_revision: int | None = Field(default=None, sa_column=Column(BigInteger))
    last_command_id: str | None = None
    last_op: str | None = None
    last_at: int = Field(sa_column=Column(BigInteger, nullable=False))


class AiMessageProvenance(SQLModel, table=True):
    """The remote origin of one transcript message a command wrote (design §4)."""

    __tablename__ = "ai_message_provenance"

    message_id: str = Field(primary_key=True)
    resource_kind: str
    resource_id: str = Field(index=True)
    user_id: str
    registration_id: str
    grant_id: str
    grant_revision: int = Field(sa_column=Column(BigInteger, nullable=False))
    command_id: str
    export_id: str
    created_at: int = Field(sa_column=Column(BigInteger, nullable=False))


class AiAuditReceipt(SQLModel, table=True):
    """Append-only audit receipt: who, which device and grant, what, outcome, when.

    No prompt, note or transcript text (design §9). SQLite triggers created together
    with the table refuse UPDATE and DELETE, so the log cannot be rewritten through the
    Studio's own connections.
    """

    __tablename__ = "ai_audit_receipt"

    id: int | None = Field(default=None, primary_key=True)
    at: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    user_id: str
    registration_id: str
    grant_id: str
    grant_revision: int = Field(sa_column=Column(BigInteger, nullable=False))
    export_id: str
    command_id: str
    op: str
    outcome: str
    error_code: str | None = None
    resource_kind: str | None = None
    resource_id: str | None = None
    run_id: str | None = None


# Append-only enforcement, created with the table by ``create_all`` — both for a new
# database and for an existing one that predates the table.
for _verb in ("UPDATE", "DELETE"):
    event.listen(
        AiAuditReceipt.__table__,  # type: ignore[attr-defined]
        "after_create",
        DDL(
            f"CREATE TRIGGER IF NOT EXISTS trg_ai_audit_receipt_no_{_verb.lower()} "
            f"BEFORE {_verb} ON ai_audit_receipt "
            "BEGIN SELECT RAISE(ABORT, 'ai_audit_receipt is append-only'); END"
        ),
    )
