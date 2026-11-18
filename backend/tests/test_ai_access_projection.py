"""AI access read projection: redaction, allowlisted DTOs, limits, cursors, streams.

ai-access-design §5, §8 and §10 rows "Scope/operations" and "Streams/limits" (§16.12).
Pure: rows are unsaved model instances or plain mappings, the clock is injected.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from voxarium_studio.ai_access.projection import (
    REDACTED,
    CursorBinding,
    ProjectedMessage,
    ProjectionStream,
    Redactor,
    chat_status,
    chunk_end,
    decode_cursor,
    encode_cursor,
    message_chunk,
    message_chunks,
    paginate,
    project_message,
    redact,
    task_documents,
    task_status,
    transcript_page,
)
from voxarium_studio.ai_access.protocol import (
    MAX_CHUNK_BYTES,
    MAX_PAGE_ITEMS,
    MAX_PAGE_PAYLOAD_BYTES,
    MAX_VISIBLE_RECORD_BYTES,
    REPLAY_FRAMES,
    REPLAY_SECONDS,
    ErrorCode,
    ProtocolError,
    Target,
    canonical_json,
)
from voxarium_studio.models import AgentChat, AgentChatMessage, AgentMessage, TeamTask

LOCAL_TOKEN = "studio-local-token-5d2c9a17e0"
PROVIDER_URL = "https://claude.ai/code/session_01AbCdEfGhIjKlMnOpQrStUv?environment=env_01XyZ1234567890"
CONNECT_URL = "claude.ai/code/session_01ZZZZZZZZZZZZZZZZZZZZ"
API_KEY = "sk-ant-api03-Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0"
ENV_ID = "env_01QwErTyUiOpAsDf"
TOOL_CANARY = "TOOL-ARGS-CANARY-c0ffee"
CANARIES = (
    LOCAL_TOKEN, "claude.ai/code", "session_01", API_KEY, "sk-ant", ENV_ID, "env_01",
    TOOL_CANARY, "environment=",
)

TARGET = Target(
    server_instance_id=str(uuid.uuid4()),
    executor_registration_id=str(uuid.uuid4()),
    export_id=str(uuid.uuid4()),
    persistent_id="voxel-game",
    storage_epoch="a" * 32,
    export_revision=3,
)
WHEN = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def redactor() -> Redactor:
    return Redactor([LOCAL_TOKEN])


def binding(resource: str = "task_1", revision: int = 1, target: Target = TARGET) -> CursorBinding:
    return CursorBinding(target=target, resource_id=resource, projection_revision=revision)


def assert_clean(value: Any) -> None:
    dumped = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    for canary in CANARIES:
        assert canary not in dumped, canary


def msg(i: int, text: str, *, role: str = "assistant", kind: str = "text",
        data: dict | None = None) -> AgentMessage:
    return AgentMessage(
        id=f"msg_{i:04d}", task_id="task_1", member_id="mbr_1", role=role, kind=kind,
        text=text, data=data or {}, created_at=WHEN,
    )


# --------------------------------------------------------------------------- redaction


def test_redaction_covers_handoff_urls_environment_ids_and_credentials(redactor: Redactor) -> None:
    text = (
        f"open {PROVIDER_URL} or {CONNECT_URL}; environment_id: {ENV_ID[4:]}, id {ENV_ID}\n"
        f"key {API_KEY} and sk-proj-abcdefghijklmnopqrstuvwxyz012345, "
        "Authorization: Bearer abcDEF123456789xyz, ghp_abcdefghijklmnopqrstuvwxyz0123, "
        "xoxb-1234567890-abcdefghij, AKIAABCDEFGHIJKLMNOP, "
        f"https://example.test/x?environment=prod-7 token={LOCAL_TOKEN}\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1\n-----END OPENSSH PRIVATE KEY-----"
    )
    out = redactor(text)
    assert_clean(out)
    for leaked in ("Bearer abc", "ghp_", "xoxb-", "AKIA", "sk-proj", "PRIVATE KEY", "prod-7"):
        assert leaked not in out
    assert out.count(REDACTED) >= 10
    # Plain prose survives untouched.
    prose = "The bearer of news asked: is the task done? See https://example.test/docs."
    assert redactor(prose) == prose
    # Too-short "secrets" are not literal-matched, so they cannot maim ordinary words.
    assert redact("the test passed", known_secrets=["test"]) == "the test passed"


# --------------------------------------------------------------------------- status DTO


def test_task_status_is_allowlisted_and_redacted(redactor: Redactor) -> None:
    task = TeamTask(
        id="task_1", number=5, team_id="team_1", assignee_id="mbr_1",
        title=f"Fix {PROVIDER_URL}", brief=f"secret brief {API_KEY}",
        result_summary=f"summary {LOCAL_TOKEN}", error=f"Traceback /home/me {API_KEY}",
        status="needs_review", result_type="done", agent_session_id="sess-provider-123",
        remote_control_url=PROVIDER_URL, worktree_path="D:/abs/path", total_cost_usd=1.234567,
        created_at=WHEN, started_at=WHEN, finished_at=WHEN,
    )
    dto = task_status(task, redactor=redactor, assignee_label="Developer", is_running=False,
                      run_id=str(uuid.uuid4()))
    data = dto.model_dump(mode="json")
    assert set(data) == {
        "resource_id", "kind", "team_id", "title", "assignee_label", "status", "is_running",
        "waiting", "error_code", "run_id", "created_at", "updated_at", "started_at",
        "finished_at", "result_type", "usage",
    }
    assert data["error_code"] == "agent_error" and data["status"] == "needs_review"
    assert data["usage"] == {"cost_estimated": True, "cost_micro_usd": 1234567, "unavailable": False}
    assert data["created_at"] == int(WHEN.timestamp() * 1000)
    blob = canonical_json(data).decode()
    assert_clean(blob)
    for absent in ("secret brief", "summary", "Traceback", "D:/abs", "sess-provider"):
        assert absent not in blob

    with pytest.raises(ProtocolError):
        task_status(task, redactor=redactor, assignee_label="x", is_running=False,
                    error_code="free text error")


def test_chat_status_refuses_producer_chats(redactor: Redactor) -> None:
    chat = AgentChat(id="chat_1", member_id="mbr_1", title=f"chat {LOCAL_TOKEN}",
                     created_at=WHEN, last_message_at=WHEN, total_cost_usd=0.0)
    dto = chat_status(chat, redactor=redactor, team_id="team_1", assignee_label="Dev",
                      status="running", usage_unavailable=True)
    assert dto.is_running and dto.usage.unavailable and dto.usage.cost_micro_usd is None
    assert_clean(dto.model_dump(mode="json"))
    producer = AgentChat(id="chat_2", member_id="mbr_1", producer_mode=True)
    with pytest.raises(ProtocolError) as caught:
        chat_status(producer, redactor=redactor, team_id="t", assignee_label="P", status="idle")
    assert caught.value.code == ErrorCode.NOT_AVAILABLE


# --------------------------------------------------------------------------- transcript projection


def test_transcript_drops_handoff_raw_data_and_tool_payloads(redactor: Redactor) -> None:
    rows = [
        msg(1, f"please open {PROVIDER_URL}", role="user"),  # URL copied by a user
        msg(2, f"here is the key {API_KEY} and token {LOCAL_TOKEN}"),
        msg(3, f"Remote Control on — {PROVIDER_URL}", role="system", kind="remote_control",
            data={"session_url": PROVIDER_URL, "environment_id": ENV_ID}),
        msg(4, f"handoff {CONNECT_URL}", role="system", kind="status",
            data={"connect_url": CONNECT_URL}),
        msg(5, "", kind="tool_use", data={"input": {"command": TOOL_CANARY}}),
        msg(6, TOOL_CANARY, role="tool", kind="tool_result", data={"out": TOOL_CANARY}),
        msg(7, "pondering secrets", kind="thinking"),
        msg(8, "system prompt text", role="system", kind="prompt_snapshot"),
        msg(9, "Final answer", role="system", kind="result"),
    ]
    projected = [project_message(r, resource_id="task_1", redactor=redactor) for r in rows]
    kept = {p.message_id: p for p in projected if p is not None}
    assert set(kept) == {"msg_0001", "msg_0002", "msg_0005", "msg_0006", "msg_0009"}
    assert kept["msg_0001"].text == f"please open {REDACTED}"
    assert (kept["msg_0005"].kind, kept["msg_0005"].text) == ("tool", "")
    assert (kept["msg_0006"].role, kept["msg_0006"].text) == ("system", "")
    assert (kept["msg_0009"].role, kept["msg_0009"].text) == ("result", "Final answer")

    chat_row = AgentChatMessage(id="cmsg_1", chat_id="chat_1", member_id="m", role="user",
                                text=f"historic {LOCAL_TOKEN}", created_at=WHEN)
    chat_msg = project_message(chat_row, resource_id="chat_1", redactor=redactor, origin="remote")
    assert chat_msg is not None and chat_msg.origin == "remote"

    page = transcript_page([*kept.values(), chat_msg], binding())
    blob = canonical_json(page).decode()
    assert_clean(blob)
    for item in page.items:
        assert set(item) == {
            "resource_id", "message_id", "role", "kind", "text", "created_at", "origin",
            "delivery", "total_bytes", "offset", "content_omitted",
        }


def test_brief_and_summary_are_transcript_content_and_redacted(redactor: Redactor) -> None:
    task = TeamTask(id="task_1", number=1, team_id="t", assignee_id="m", title="T",
                    brief=f"do it {API_KEY}", result_summary=f"done, see {PROVIDER_URL}",
                    created_at=WHEN, finished_at=WHEN)
    docs = task_documents(task, redactor=redactor)
    assert [(d.message_id, d.kind) for d in docs] == [("task_1.brief", "brief"),
                                                      ("task_1.summary", "summary")]
    assert_clean([d.text for d in docs])


def test_redaction_happens_before_chunking(redactor: Redactor) -> None:
    # A provider URL straddling the 16 KiB chunk boundary must not leak in halves.
    text = "x" * (MAX_CHUNK_BYTES - 20) + f" {PROVIDER_URL} " + "y" * 100
    projected = project_message(msg(1, text, role="user"), resource_id="task_1", redactor=redactor)
    assert projected is not None
    chunks = message_chunks(projected)
    assert len(chunks) == 2
    assert_clean([c.text for c in chunks])
    assert "".join(c.text for c in chunks) == redactor(text)


# --------------------------------------------------------------------------- limits


def test_pages_hold_at_most_50_items_and_cursor_walks_everything() -> None:
    messages = [
        ProjectedMessage(resource_id="task_1", message_id=f"m{i}", role="assistant", kind="text",
                         text=f"line {i}", created_at=i, origin="agent")
        for i in range(137)
    ]
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = transcript_page(messages, binding(), cursor=cursor)
        pages += 1
        assert len(page.items) <= MAX_PAGE_ITEMS
        assert len(canonical_json(page)) <= MAX_PAGE_PAYLOAD_BYTES
        seen.extend(item["message_id"] for item in page.items)
        if page.next_cursor is None:
            break
        assert len(page.next_cursor) <= 512
        cursor = page.next_cursor
    assert seen == [m.message_id for m in messages] and pages == 3
    assert len(transcript_page(messages, binding(), limit=7).items) == 7


def test_pages_stay_under_32_kib_even_with_escape_heavy_text() -> None:
    texts = ['"\\' * 1500, "\x01" * 3000, "я" * 4000, "a" * 9000]
    messages = [
        ProjectedMessage(resource_id="task_1", message_id=f"m{i}", role="user", kind="text",
                         text=texts[i % len(texts)], created_at=i, origin="local")
        for i in range(40)
    ]
    cursor, total = None, 0
    while True:
        page = transcript_page(messages, binding(), cursor=cursor)
        assert 1 <= len(page.items) < MAX_PAGE_ITEMS
        assert len(canonical_json(page)) <= MAX_PAGE_PAYLOAD_BYTES
        total += len(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert total == 40


@pytest.mark.parametrize("unit", ["я😀a", '"\\\n', "a", "\x02é"])
def test_long_text_chunks_reassemble_exactly_on_character_boundaries(unit: str) -> None:
    text = unit * (40_000 // len(unit.encode("utf-8")) + 7)
    projected = ProjectedMessage(resource_id="task_1", message_id="m1", role="assistant",
                                 kind="text", text=text, created_at=1, origin="agent")
    chunks = message_chunks(projected)
    assert len(chunks) >= 3
    assert "".join(c.text for c in chunks) == text
    offset = 0
    for chunk in chunks:
        raw = chunk.text.encode("utf-8")
        assert chunk.offset == offset and chunk.total_bytes == len(text.encode("utf-8"))
        assert 0 < len(raw) <= MAX_CHUNK_BYTES
        assert len(json.dumps(chunk.text, ensure_ascii=False)) - 2 <= MAX_CHUNK_BYTES
        assert not chunk.content_omitted
        offset += len(raw)
    # A chunk request inside a multi-byte character, or past the end, is refused.
    if "я" in unit:
        with pytest.raises(ProtocolError):
            message_chunk(projected, 1)
    with pytest.raises(ProtocolError):
        message_chunk(projected, offset + 1)


def test_chunk_end_never_splits_a_character() -> None:
    raw = ("😀" * 10_000).encode("utf-8")
    end = chunk_end(raw, 0)
    assert end % 4 == 0 and end <= MAX_CHUNK_BYTES


def test_records_over_one_mib_are_content_omitted() -> None:
    def make(size: int) -> ProjectedMessage:
        return ProjectedMessage(resource_id="task_1", message_id="big", role="assistant",
                                kind="text", text="z" * size, created_at=1, origin="agent")

    at_limit = message_chunk(make(MAX_VISIBLE_RECORD_BYTES))
    assert not at_limit.content_omitted and len(at_limit.text) == MAX_CHUNK_BYTES
    over = message_chunk(make(MAX_VISIBLE_RECORD_BYTES + 1))
    assert over.content_omitted and over.text == ""
    assert over.total_bytes == MAX_VISIBLE_RECORD_BYTES + 1
    assert message_chunks(make(MAX_VISIBLE_RECORD_BYTES + 1)) == [over]


# --------------------------------------------------------------------------- cursors


def test_cursor_is_bound_to_target_resource_and_projection_revision() -> None:
    items = [{"n": i} for i in range(120)]
    cursor = paginate(items, binding()).next_cursor
    assert cursor is not None and decode_cursor(cursor, binding()) == 50
    other_target = TARGET.model_copy(update={"export_revision": 4})
    for wrong in (binding("task_2"), binding(revision=2), binding(target=other_target)):
        with pytest.raises(ProtocolError) as caught:
            paginate(items, wrong, cursor=cursor)
        assert caught.value.code == ErrorCode.SNAPSHOT_CHANGED
    for garbage in ("", "!!!", "e30", encode_cursor(binding(), 1)[:-3]):
        with pytest.raises(ProtocolError):
            decode_cursor(garbage, binding())
    with pytest.raises(ProtocolError) as caught:  # the visible set shrank under the cursor
        paginate(items[:10], binding(), cursor=cursor)
    assert caught.value.code == ErrorCode.SNAPSHOT_CHANGED


# --------------------------------------------------------------------------- streams


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def stream(resource: str = "task_1", clock: FakeClock | None = None) -> ProjectionStream:
    return ProjectionStream(target=TARGET, resource_id=resource, clock=clock or FakeClock())


def seqs(frames: list) -> list[int]:
    return [f.seq for f in frames]


def test_stream_seq_and_resync_on_unknown_epoch_ahead_and_reset() -> None:
    s = stream()
    frames = [s.append({"status": "running", "i": i}, resource_revision=2) for i in range(5)]
    assert seqs(frames) == [1, 2, 3, 4, 5] and {f.stream_epoch for f in frames} == {s.stream_epoch}
    for epoch, after in ((str(uuid.uuid4()), 2), (s.stream_epoch, 6)):
        with pytest.raises(ProtocolError) as caught:
            s.subscribe(epoch, after)
        assert caught.value.code == ErrorCode.RESYNC_REQUIRED
    live = s.subscribe(s.stream_epoch, 5)
    old_epoch = s.stream_epoch
    s.reset()  # compaction / projection reset → new generation
    assert s.stream_epoch != old_epoch and s.seq == 0
    with pytest.raises(ProtocolError):
        live.drain()
    with pytest.raises(ProtocolError):
        s.subscribe(old_epoch, 5)


def test_replay_is_bounded_by_frames_and_seconds() -> None:
    clock = FakeClock()
    s = stream(clock=clock)
    for i in range(REPLAY_FRAMES + 100):
        s.append({"i": i}, resource_revision=1)
    assert s.replay_length == REPLAY_FRAMES
    with pytest.raises(ProtocolError) as caught:  # seq 100 already evicted → gap
        s.subscribe(s.stream_epoch, 99)
    assert caught.value.code == ErrorCode.RESYNC_REQUIRED
    replayed = s.subscribe(s.stream_epoch, 100).drain()
    assert seqs(replayed) == list(range(101, REPLAY_FRAMES + 101))

    clock.now += REPLAY_SECONDS - 1
    s.append({"late": True}, resource_revision=1)
    clock.now += 2  # every earlier frame is now older than 60 s
    with pytest.raises(ProtocolError):
        s.subscribe(s.stream_epoch, REPLAY_FRAMES + 99)
    late = REPLAY_FRAMES + 101
    assert seqs(s.subscribe(s.stream_epoch, late - 1).drain()) == [late]
    assert s.replay_length == 1


def test_snapshot_watermark_race_has_no_duplicates_or_mixed_targets() -> None:
    s = stream("task_1")
    other = stream("task_2")
    for i in range(3):
        s.append({"i": i}, resource_revision=1)
        other.append({"other": i}, resource_revision=1)
    epoch, seq = s.watermark()  # taken together with the paginated snapshot
    for i in range(3, 6):  # events land while the snapshot travels
        s.append({"i": i}, resource_revision=2)
        other.append({"other": i}, resource_revision=1)
    sub = s.subscribe(epoch, seq)
    s.append({"i": 6}, resource_revision=3)
    got = sub.drain()
    assert seqs(got) == [4, 5, 6, 7]
    assert all(f.resource_id == "task_1" and "other" not in f.payload for f in got)
    s.append({"i": 7}, resource_revision=3)
    assert seqs(sub.drain()) == [8]
    with pytest.raises(ProtocolError):  # another resource's cursor is an unknown epoch here
        s.subscribe(other.stream_epoch, other.seq)
    sub.close()
    assert s.subscriber_count == 0


def test_slow_subscriber_overflows_into_resync_without_blocking_the_producer() -> None:
    s = stream()
    slow = s.subscribe()
    fast = s.subscribe()
    received: list[int] = []
    started = time.perf_counter()
    for i in range(10_000):
        s.append({"i": i}, resource_revision=1)
        if i % 10 == 9:
            received.extend(seqs(fast.drain()))
    elapsed = time.perf_counter() - started
    assert elapsed < 5  # O(1) append; nothing waits on the slow reader
    assert received == list(range(1, 10_001))
    assert slow.resync_required and slow.buffered == 0
    with pytest.raises(ProtocolError) as caught:
        slow.drain()
    assert caught.value.code == ErrorCode.RESYNC_REQUIRED
    assert s.replay_length <= REPLAY_FRAMES

    by_bytes = s.subscribe()
    for _ in range(11):
        s.append({"blob": "b" * 100_000}, resource_revision=1)  # > 1 MiB in 11 frames
    assert by_bytes.resync_required and by_bytes.buffered == 0
