"""Allowlisted read projection of tasks and chats for remote viewers (§16.12).

Implements ai-access-design §5 (``resource.list/get``, ``transcript.page``) and §8
(redaction, DTOs, limits, cursors, streams). Pure and synchronous: it takes rows that the
caller already loaded and scoped, and never touches the database, the event bus or the
network. Nothing is mounted yet (design §11 S-A4).

Rules this module owns:

* **Allowlist, not model_dump.** A DTO is built field by field from a row or a mapping;
  raw ``data``, tool arguments/results, system prompts, provider session ids, paths,
  attachment URLs, roster configuration and error *text* never enter one.
* **Redaction before chunking.** Every text field — title, brief, summary, user,
  assistant, historical text — goes through :class:`Redactor` before it is measured or
  split, so a provider Remote Control URL copied into a user message never leaves.
  The filter knows provider handoff URLs, environment identifiers and well-known
  credential shapes plus the caller's own secrets; it cannot find an arbitrary secret a
  model wrote out (design §8 — the UI warns before granting ``view_transcript``).
* **Limits.** A page holds ≤50 items and ≤32 KiB of canonical JSON; a text above
  16 KiB arrives in chunks (``message_id`` + byte ``offset``/``total_bytes``) with no
  silent truncation; a record above 1 MiB is ``content_omitted``.
* **Cursors** are opaque and bound to (target, resource, projection revision); one from
  another binding is ``snapshot_changed``.
* **Streams** carry ``stream_epoch`` + ``seq`` with a bounded replay (500 frames / 60 s);
  a gap, an unknown epoch, a cursor ahead or a slow subscriber's overflow is
  ``resync_required``. Appending never blocks and never waits for a subscriber.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from .protocol import (
    MAX_CHUNK_BYTES,
    MAX_PAGE_ITEMS,
    MAX_PAGE_PAYLOAD_BYTES,
    MAX_VISIBLE_RECORD_BYTES,
    REPLAY_FRAMES,
    REPLAY_SECONDS,
    ErrorCode,
    ProtocolError,
    ResourceKind,
    Strict,
    Target,
    canonical_hash,
    canonical_json,
)

REDACTED = "[redacted]"

MAX_SUBSCRIPTION_FRAMES = 128
MAX_SUBSCRIPTION_BYTES = 1024 * 1024
"""Per-subscription buffer (design §8): beyond either bound the subscriber must resync."""

# --------------------------------------------------------------------------- redaction

_URL_TAIL = r"[^\s<>\"'`)\]}]*"
_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in (
    # Private key blocks first: they span lines.
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    # Provider Remote Control / session / connect handoff URLs (§10.8), with or without
    # a scheme, on claude.ai or any subdomain of it.
    r"(?i)\b(?:https?://)?(?:[a-z0-9-]+\.)*claude\.ai/(?:code|connect|session|remote)" + _URL_TAIL,
    # Any URL that names an environment or a session in its query.
    r"(?i)\bhttps?://[^\s<>\"'`]*?[?&](?:environment|environment_id|env|session|session_id)="
    + _URL_TAIL,
    # Environment / provider session identifiers on their own.
    r"(?i)\benvironment[_ ]?id\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{6,}[\"']?",
    r"\benv_[A-Za-z0-9]{12,}\b",
    r"\bsession_[A-Za-z0-9]{16,}\b",
    # Credentials of well-known shapes.
    r"\bsk-ant-[A-Za-z0-9_\-]{8,}",
    r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}",
    r"\b[Bb]earer\s+(?=[A-Za-z0-9._~+/=\-]*\d)[A-Za-z0-9._~+/=\-]{8,}",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_\-]{35}\b",
    r"\bglpat-[A-Za-z0-9_\-]{20,}\b",
))

_MIN_KNOWN_SECRET = 8
"""Shorter literals are not treated as secrets: redacting ``"test"`` would maim text."""


class Redactor:
    """Replaces known secrets and provider handoff identifiers with ``[redacted]``.

    ``known_secrets`` are literals the executor knows — the local Studio token, a
    configured key, test canaries — matched exactly, longest first.
    """

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        literals = sorted(
            {s for s in known_secrets if isinstance(s, str) and len(s) >= _MIN_KNOWN_SECRET},
            key=len, reverse=True,
        )
        self._literals = (
            re.compile("|".join(re.escape(s) for s in literals)) if literals else None
        )

    def __call__(self, text: str | None) -> str:
        if not text:
            return ""
        if self._literals is not None:
            text = self._literals.sub(REDACTED, text)
        for pattern in _PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text


def redact(text: str | None, known_secrets: Iterable[str] = ()) -> str:
    """One-off :class:`Redactor` call; build a ``Redactor`` once for many texts."""
    return Redactor(known_secrets)(text)


# --------------------------------------------------------------------------- helpers


def _get(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def to_ms(value: datetime | int | None) -> int | None:
    """A ``datetime`` (naive = UTC, as SQLite returns it) or ms int → int64 ms."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProtocolError("invalid_timestamp")
    if isinstance(value, int):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _safe_label(value: str | None, redactor: Redactor, limit: int = 120) -> str:
    """A short display label — redacted first, then cut to ``limit`` UTF-8 bytes at a
    character boundary (a label, not content: content is never truncated)."""
    raw = redactor(value or "").encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore")


# --------------------------------------------------------------------------- DTOs


class UsageDTO(Strict):
    """Usage without floats on the wire: micro-USD, with estimated/unavailable explicit."""

    cost_estimated: bool
    cost_micro_usd: int | None
    unavailable: bool


class ResourceStatusDTO(Strict):
    """``resource.list/get`` item (design §5): status only, no brief/summary/prompt text."""

    resource_id: str
    kind: ResourceKind
    team_id: str
    title: str
    assignee_label: str
    status: str
    is_running: bool
    waiting: bool
    error_code: str | None
    run_id: str | None
    created_at: int
    updated_at: int | None
    started_at: int | None
    finished_at: int | None
    result_type: str | None
    usage: UsageDTO


class TranscriptMessageDTO(Strict):
    """``transcript.page`` item (design §8). ``text`` is one chunk of the redacted text."""

    resource_id: str
    message_id: str
    role: str
    kind: str
    text: str
    created_at: int
    origin: str
    delivery: str
    total_bytes: int
    offset: int
    content_omitted: bool


class PageDTO(Strict):
    items: list[dict[str, Any]]
    next_cursor: str | None


TASK_STATUSES = frozenset({
    "todo", "pending", "in_progress", "needs_review", "blocked", "done", "cancelled",
})
CHAT_STATUSES = frozenset({"idle", "running", "cancelling", "error"})
RESULT_TYPES = frozenset({"done", "question", "error"})

ERROR_CODES = frozenset({"agent_error", "usage_limit", "cancelled", "interrupted"})
"""The only error *codes* a status DTO carries; never the error text itself."""


def _usage(row: Any, *, cost_estimated: bool, unavailable: bool) -> UsageDTO:
    cost = _get(row, "total_cost_usd")
    if unavailable or cost is None:
        return UsageDTO(cost_estimated=cost_estimated, cost_micro_usd=None, unavailable=True)
    return UsageDTO(
        cost_estimated=cost_estimated,
        cost_micro_usd=int(round(float(cost) * 1_000_000)),
        unavailable=False,
    )


def task_status(
    task: Any,
    *,
    redactor: Redactor,
    assignee_label: str,
    is_running: bool,
    waiting: bool = False,
    run_id: str | None = None,
    error_code: str | None = None,
    cost_estimated: bool = True,
    usage_unavailable: bool = False,
) -> ResourceStatusDTO:
    """Status DTO of a ``TeamTask`` row or mapping.

    ``error_code`` is chosen by the caller from :data:`ERROR_CODES`; without one, a task
    with error text reports the generic ``agent_error`` — the text is never copied.
    """
    status = str(_get(task, "status") or "")
    if status not in TASK_STATUSES:
        raise ProtocolError("unsupported_status")
    if error_code is None and _get(task, "error"):
        error_code = "agent_error"
    if error_code is not None and error_code not in ERROR_CODES:
        raise ProtocolError("unsupported_error_code")
    result_type = _get(task, "result_type")
    return ResourceStatusDTO(
        resource_id=str(_get(task, "id")),
        kind=ResourceKind.TASK,
        team_id=str(_get(task, "team_id")),
        title=_safe_label(_get(task, "title"), redactor, 480),
        assignee_label=_safe_label(assignee_label, redactor),
        status=status,
        is_running=bool(is_running),
        waiting=bool(waiting),
        error_code=error_code,
        run_id=run_id,
        created_at=to_ms(_get(task, "created_at")) or 0,
        updated_at=to_ms(_get(task, "updated_at")),
        started_at=to_ms(_get(task, "started_at")),
        finished_at=to_ms(_get(task, "finished_at")),
        result_type=result_type if result_type in RESULT_TYPES else None,
        usage=_usage(task, cost_estimated=cost_estimated, unavailable=usage_unavailable),
    )


def chat_status(
    chat: Any,
    *,
    redactor: Redactor,
    team_id: str,
    assignee_label: str,
    status: str,
    run_id: str | None = None,
    error_code: str | None = None,
    cost_estimated: bool = True,
    usage_unavailable: bool = False,
) -> ResourceStatusDTO:
    """Status DTO of an ordinary ``AgentChat``; Producer chats are outside ACP/1 (§5)."""
    if _get(chat, "producer_mode") or _get(chat, "producer_workspace"):
        raise ProtocolError(ErrorCode.NOT_AVAILABLE)
    if status not in CHAT_STATUSES:
        raise ProtocolError("unsupported_status")
    if error_code is None and _get(chat, "error"):
        error_code = "agent_error"
    if error_code is not None and error_code not in ERROR_CODES:
        raise ProtocolError("unsupported_error_code")
    return ResourceStatusDTO(
        resource_id=str(_get(chat, "id")),
        kind=ResourceKind.CHAT,
        team_id=team_id,
        title=_safe_label(_get(chat, "title"), redactor, 480),
        assignee_label=_safe_label(assignee_label, redactor),
        status=status,
        is_running=status in ("running", "cancelling"),
        waiting=False,
        error_code=error_code,
        run_id=run_id,
        created_at=to_ms(_get(chat, "created_at")) or 0,
        updated_at=to_ms(_get(chat, "last_message_at")),
        started_at=None,
        finished_at=None,
        result_type=None,
        usage=_usage(chat, cost_estimated=cost_estimated, unavailable=usage_unavailable),
    )


# --------------------------------------------------------------------------- transcript

CONTENT_ROLES = frozenset({"user", "human", "assistant", "result"})
CONTENT_KINDS = frozenset({
    "text", "note", "summary", "user_message", "human", "user", "brief", "result",
})
"""``thinking`` is deliberately absent: model reasoning is not transcript content."""

STATUS_KINDS: dict[str, str] = {
    "queued": "queued",
    "cancelled": "cancelled",
    "continued": "continued",
    "compaction": "compacted",
    "error": "error",
    "result_missing": "result_missing",
    "status": "status",
    "tool_use": "tool",
    "tool_result": "tool",
    "tool": "tool",
    "mcp": "tool",
}
"""System/tool events that survive projection — as a fixed status word, no payload."""

EXCLUDED_KINDS = frozenset({"remote_control"})
_HANDOFF_KEYS = frozenset({"session_url", "connect_url", "environment_id"})


@dataclass(frozen=True, slots=True)
class ProjectedMessage:
    """One message after allowlisting and redaction, before chunking."""

    resource_id: str
    message_id: str
    role: str
    kind: str
    text: str
    created_at: int
    origin: str
    delivery: str = "delivered"

    @property
    def total_bytes(self) -> int:
        return _utf8_len(self.text)


def project_message(
    message: Any,
    *,
    resource_id: str,
    redactor: Redactor,
    origin: str = "local",
) -> ProjectedMessage | None:
    """Allowlist one ``AgentMessage``/``AgentChatMessage`` row (or mapping); ``None`` = hidden.

    ``kind=remote_control`` and any message carrying a provider handoff in ``data`` are
    excluded entirely; system/tool events become a fixed status word with empty text;
    unknown kinds and roles are hidden rather than guessed at.
    """
    kind = str(_get(message, "kind") or "text")
    role = str(_get(message, "role") or "")
    data = _get(message, "data") or {}
    if kind in EXCLUDED_KINDS:
        return None
    if isinstance(data, Mapping) and _HANDOFF_KEYS & set(data):
        return None
    if origin not in ("local", "remote", "agent"):
        origin = "local"
    common = {
        "resource_id": resource_id,
        "message_id": str(_get(message, "id")),
        "created_at": to_ms(_get(message, "created_at")) or 0,
        "origin": origin,
    }
    if kind in STATUS_KINDS:
        return ProjectedMessage(role="system", kind=STATUS_KINDS[kind], text="", **common)
    if kind == "result":
        role = "result"
    if role in CONTENT_ROLES and kind in CONTENT_KINDS:
        return ProjectedMessage(
            role=role, kind=kind, text=redactor(_get(message, "text") or ""), **common,
        )
    return None


def task_documents(task: Any, *, redactor: Redactor) -> list[ProjectedMessage]:
    """Brief and summary as transcript content — the only way they leave (design §5)."""
    out: list[ProjectedMessage] = []
    task_id = str(_get(task, "id"))
    created = to_ms(_get(task, "created_at")) or 0
    brief = _get(task, "brief")
    if brief:
        out.append(ProjectedMessage(
            resource_id=task_id, message_id=f"{task_id}.brief", role="user", kind="brief",
            text=redactor(brief), created_at=created, origin="local",
        ))
    summary = _get(task, "result_summary")
    if summary:
        out.append(ProjectedMessage(
            resource_id=task_id, message_id=f"{task_id}.summary", role="result",
            kind="summary", text=redactor(summary),
            created_at=to_ms(_get(task, "finished_at")) or created, origin="agent",
        ))
    return out


def _escaped_len(char: str) -> int:
    """Bytes ``char`` takes inside a canonical JSON string (``ensure_ascii=False``)."""
    if char in ('"', "\\"):
        return 2
    code = ord(char)
    if code < 0x20:
        return 2 if char in "\b\f\n\r\t" else 6
    if 0xD800 <= code <= 0xDFFF:  # lone surrogate — refused by canonical_json anyway
        return 6
    return len(char.encode("utf-8"))


_NEEDS_ESCAPE = re.compile(rb'["\\\x00-\x1f]')


def _width(lead: int) -> int:
    return 1 if lead < 0x80 else 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4


def chunk_end(text_bytes: bytes, offset: int, limit: int = MAX_CHUNK_BYTES) -> int:
    """End byte of the chunk starting at ``offset``: ≤``limit`` raw bytes *and* ≤``limit``
    bytes once JSON-escaped, never inside a UTF-8 character, always non-empty."""
    total = len(text_bytes)
    if offset >= total:
        return total
    # Fast path: nothing in the window grows when escaped — cut at the last boundary.
    end = min(total, offset + limit)
    while end < total and end > offset and (text_bytes[end] & 0xC0) == 0x80:
        end -= 1
    if end > offset and _NEEDS_ESCAPE.search(text_bytes, offset, end) is None:
        return end
    end = offset
    escaped = 0
    while end < total:
        width = _width(text_bytes[end])
        char = text_bytes[end:end + width].decode("utf-8")
        cost = _escaped_len(char)
        if end + width - offset > limit or escaped + cost > limit:
            break
        end += width
        escaped += cost
    return end if end > offset else min(total, offset + _width(text_bytes[offset]))


def _is_boundary(text_bytes: bytes, offset: int) -> bool:
    return offset == len(text_bytes) or (
        0 <= offset < len(text_bytes) and (text_bytes[offset] & 0xC0) != 0x80
    )


def message_chunk(message: ProjectedMessage, offset: int = 0) -> TranscriptMessageDTO:
    """The chunk of a projected message that starts at byte ``offset``.

    A record above 1 MiB is ``content_omitted`` with empty text (design §8: look at it
    locally). An offset not on a character boundary, or past the end, is refused.
    """
    raw = message.text.encode("utf-8")
    total = len(raw)
    omitted = total > MAX_VISIBLE_RECORD_BYTES
    if omitted:
        if offset != 0:
            raise ProtocolError("invalid_offset")
        text = ""
    else:
        if not _is_boundary(raw, offset) or (offset >= total and offset != 0):
            raise ProtocolError("invalid_offset")
        text = raw[offset:chunk_end(raw, offset)].decode("utf-8")
    return TranscriptMessageDTO(
        resource_id=message.resource_id,
        message_id=message.message_id,
        role=message.role,
        kind=message.kind,
        text=text,
        created_at=message.created_at,
        origin=message.origin,
        delivery=message.delivery,
        total_bytes=total,
        offset=offset,
        content_omitted=omitted,
    )


def message_chunks(message: ProjectedMessage) -> list[TranscriptMessageDTO]:
    """Every chunk of one message, in order — what a requester pages through."""
    first = message_chunk(message, 0)
    chunks = [first]
    if first.content_omitted:
        return chunks
    offset = _utf8_len(first.text)
    while offset < first.total_bytes:
        chunk = message_chunk(message, offset)
        chunks.append(chunk)
        offset += _utf8_len(chunk.text)
    return chunks


# --------------------------------------------------------------------------- cursors


@dataclass(frozen=True, slots=True)
class CursorBinding:
    """What a cursor is valid for (design §8): target + resource + projection revision.

    ``projection_revision`` changes on compaction, reset or any change of the visible
    set other than appending — the caller's counter, not the resource revision.
    """

    target: Target
    resource_id: str
    projection_revision: int

    def digest(self) -> str:
        return canonical_hash({
            "target": self.target.model_dump(mode="json"),
            "resource_id": self.resource_id,
            "projection_revision": self.projection_revision,
        })


def encode_cursor(binding: CursorBinding, position: int) -> str:
    raw = canonical_json({"v": 1, "b": binding.digest(), "p": position})
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, binding: CursorBinding) -> int:
    """Position of a cursor issued for ``binding``; another binding → ``snapshot_changed``."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error):
        raise ProtocolError("invalid_cursor") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"v", "b", "p"}
        or value["v"] != 1
        or type(value["p"]) is not int
        or value["p"] < 0
        or not isinstance(value["b"], str)
    ):
        raise ProtocolError("invalid_cursor")
    if value["b"] != binding.digest():
        raise ProtocolError(ErrorCode.SNAPSHOT_CHANGED)
    return value["p"]


def _dump(item: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, BaseModel):
        return item.model_dump(mode="json")
    return dict(item)


def paginate(
    items: Sequence[BaseModel | Mapping[str, Any]],
    binding: CursorBinding,
    *,
    cursor: str | None = None,
    limit: int = MAX_PAGE_ITEMS,
) -> PageDTO:
    """One page: ≤``limit`` (≤50) items and ≤32 KiB of canonical JSON (design §8).

    Items are never cut to fit; a page that would overflow ends early and the cursor
    resumes at the first item left out. A single item larger than a page is refused —
    transcript items are chunked beforehand so that never happens.
    """
    limit = max(1, min(limit, MAX_PAGE_ITEMS))
    start = 0 if cursor is None else decode_cursor(cursor, binding)
    if start > len(items):
        raise ProtocolError(ErrorCode.SNAPSHOT_CHANGED)
    # Reserve room for a cursor of the largest position this page could end at.
    reserve = len(canonical_json({"next_cursor": encode_cursor(binding, len(items))}))
    budget = MAX_PAGE_PAYLOAD_BYTES - reserve - len(canonical_json({"items": []}))
    page: list[dict[str, Any]] = []
    used = 0
    position = start
    while position < len(items) and len(page) < limit:
        dumped = _dump(items[position])
        size = len(canonical_json(dumped)) + (1 if page else 0)
        if used + size > budget:
            if not page:
                raise ProtocolError(ErrorCode.CAPACITY_EXCEEDED)
            break
        page.append(dumped)
        used += size
        position += 1
    next_cursor = encode_cursor(binding, position) if position < len(items) else None
    result = PageDTO(items=page, next_cursor=next_cursor)
    if len(canonical_json(result)) > MAX_PAGE_PAYLOAD_BYTES:  # pragma: no cover - budgeted
        raise ProtocolError(ErrorCode.CAPACITY_EXCEEDED)
    return result


def transcript_page(
    messages: Sequence[ProjectedMessage],
    binding: CursorBinding,
    *,
    cursor: str | None = None,
    limit: int = MAX_PAGE_ITEMS,
) -> PageDTO:
    """A page of first chunks; longer texts continue through :func:`message_chunk`."""
    return paginate([message_chunk(m, 0) for m in messages], binding, cursor=cursor, limit=limit)


# --------------------------------------------------------------------------- streams


@dataclass(frozen=True, slots=True)
class Frame:
    stream_epoch: str
    seq: int
    resource_id: str
    resource_revision: int
    payload: dict[str, Any]
    size: int
    at: float


class Subscription:
    """A bounded per-subscriber buffer. Overflow drops the backlog and demands a resync."""

    def __init__(
        self,
        stream: ProjectionStream,
        *,
        max_frames: int = MAX_SUBSCRIPTION_FRAMES,
        max_bytes: int = MAX_SUBSCRIPTION_BYTES,
    ) -> None:
        self.id = str(uuid.uuid4())
        self._stream = stream
        self._buffer: deque[Frame] = deque()
        self._replay: list[Frame] = []
        """Frames replayed at subscribe — already bounded by the stream's replay window."""
        self._bytes = 0
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self.resync_required = False
        self.closed = False

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def _push(self, frame: Frame) -> None:
        if self.resync_required or self.closed:
            return
        if len(self._buffer) + 1 > self._max_frames or self._bytes + frame.size > self._max_bytes:
            self._invalidate()
            return
        self._buffer.append(frame)
        self._bytes += frame.size

    def _invalidate(self) -> None:
        self.resync_required = True
        self._buffer.clear()
        self._replay = []
        self._bytes = 0

    def drain(self) -> list[Frame]:
        """Replayed then live frames since the last drain; after a gap → ``resync_required``."""
        if self.resync_required:
            raise ProtocolError(ErrorCode.RESYNC_REQUIRED)
        frames = [*self._replay, *self._buffer]
        self._replay = []
        self._buffer.clear()
        self._bytes = 0
        return frames

    def close(self) -> None:
        self.closed = True
        self._buffer.clear()
        self._replay = []
        self._bytes = 0
        self._stream._subscribers.discard(self)


@dataclass(slots=True)
class ProjectionStream:
    """The projection event stream of one resource of one pinned target (design §8).

    ``stream_epoch`` is a fresh UUID per process start and per :meth:`reset` (compaction,
    projection reset); ``seq`` grows by one per frame from 1. The replay buffer keeps at
    most ``replay_frames`` frames and none older than ``replay_seconds`` by the injected
    monotonic ``clock``. :meth:`append` is synchronous and O(1) amortised: a slow
    subscriber overflows its own buffer, the producer never waits.
    """

    target: Target
    resource_id: str
    clock: Callable[[], float] = time.monotonic
    replay_frames: int = REPLAY_FRAMES
    replay_seconds: float = REPLAY_SECONDS
    stream_epoch: str = field(default_factory=lambda: str(uuid.uuid4()))
    seq: int = 0
    _replay: deque[Frame] = field(default_factory=deque)
    _subscribers: set[Subscription] = field(default_factory=set)
    _evicted_through: int = 0
    """Highest seq no longer in the replay buffer — a cursor below it is a gap."""

    def _prune(self) -> None:
        horizon = self.clock() - self.replay_seconds
        while self._replay and (
            len(self._replay) > self.replay_frames or self._replay[0].at < horizon
        ):
            self._evicted_through = self._replay.popleft().seq

    def append(self, payload: BaseModel | Mapping[str, Any], resource_revision: int) -> Frame:
        """Publish one projected payload (already allowlisted and redacted)."""
        body = _dump(payload)
        frame = Frame(
            stream_epoch=self.stream_epoch,
            seq=self.seq + 1,
            resource_id=self.resource_id,
            resource_revision=resource_revision,
            payload=body,
            size=len(canonical_json(body)),
            at=self.clock(),
        )
        self.seq = frame.seq
        self._replay.append(frame)
        self._prune()
        for subscriber in tuple(self._subscribers):
            subscriber._push(frame)
        return frame

    def watermark(self) -> tuple[str, int]:
        """``(stream_epoch, seq)`` to hand out with a snapshot taken at the same moment."""
        return self.stream_epoch, self.seq

    def reset(self) -> str:
        """New generation: every cursor and subscription of the old one must resync."""
        self.stream_epoch = str(uuid.uuid4())
        self.seq = 0
        self._evicted_through = 0
        self._replay.clear()
        for subscriber in tuple(self._subscribers):
            subscriber._invalidate()
        return self.stream_epoch

    def subscribe(
        self,
        stream_epoch: str | None = None,
        after_seq: int | None = None,
        *,
        max_frames: int = MAX_SUBSCRIPTION_FRAMES,
        max_bytes: int = MAX_SUBSCRIPTION_BYTES,
    ) -> Subscription:
        """Subscribe live, or from a cursor with replay of everything after it.

        Atomic relative to :meth:`append` (no await in between): frames after the
        cursor are queued first, later ones follow, none is duplicated. An unknown
        epoch, a cursor ahead of ``seq`` or one older than the replay window →
        ``resync_required`` — take a new snapshot and its watermark.
        """
        if (stream_epoch is None) != (after_seq is None):
            raise ProtocolError("invalid_cursor")
        subscription = Subscription(self, max_frames=max_frames, max_bytes=max_bytes)
        if stream_epoch is not None:
            assert after_seq is not None
            self._prune()
            if stream_epoch != self.stream_epoch or after_seq > self.seq or after_seq < 0:
                raise ProtocolError(ErrorCode.RESYNC_REQUIRED)
            if after_seq < self._evicted_through:
                raise ProtocolError(ErrorCode.RESYNC_REQUIRED)
            subscription._replay = [frame for frame in self._replay if frame.seq > after_seq]
        self._subscribers.add(subscription)
        return subscription

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def replay_length(self) -> int:
        return len(self._replay)
