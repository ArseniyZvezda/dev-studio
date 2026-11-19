"""ACP/1 bounded relay (ai-access design §6–§9; R-A2).

The Remote authenticates, checks grants and forwards — nothing more. It runs no AI, keeps
no offline command queue, stores no prompts/transcripts/results and logs no bodies. A
requester's ``request`` reaches exactly one live executor connection (the target's
registration, inside its heartbeat lease), and the executor's ``response``/``event``
reaches the requester only after the requester's credentials and grant are checked
*again* at delivery and once more when the frame leaves the queue.

Like :class:`~.hub.Hub`, **no handler awaits**: frames are handled start to finish
synchronously; outgoing frames go into bounded per-connection queues drained by the route.
The clock is injected so lease, expiry and rate limits are deterministic in tests.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import ai_protocol as p
from .ai_auth import Actor, AiAuth
from .ai_store import NotFound

log = logging.getLogger(__name__)

MAX_CONNECTION_QUEUE_BYTES = 4 * 1024 * 1024
MAX_CONNECTION_QUEUE_FRAMES = 1024
DIRECTORY_PAGE = 50


class CloseConnection(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


class TokenBucket:
    def __init__(self, rate: float, burst: float, now: float) -> None:
        self.rate, self.burst, self.tokens, self.last = rate, burst, burst, now

    def _refill(self, now: float) -> None:
        self.tokens = min(self.burst, self.tokens + max(0.0, now - self.last) * self.rate)
        self.last = now

    def take(self, now: float, amount: float = 1.0) -> bool:
        self._refill(now)
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def retry_after(self, now: float, amount: float = 1.0) -> int:
        self._refill(now)
        return max(1, math.ceil((amount - self.tokens) / self.rate))


@dataclass(slots=True)
class Outbound:
    text: str
    size: int
    subscription: "Subscription | None" = None
    check: Callable[[], bool] | None = None


class ControlConnection:
    def __init__(self, actor: Actor) -> None:
        self.id = str(uuid.uuid4())
        self.actor = actor
        self.queue: asyncio.Queue[Outbound | None] = asyncio.Queue()
        self.queued_bytes = 0
        self.closed = False
        self.close_code = 1000
        self.close_reason = ""
        self.executor_epoch: str | None = None
        self.lease_deadline = 0.0
        self.staging: dict[int, dict] = {}

    def enqueue(self, item: Outbound) -> bool:
        if self.closed:
            return False
        if (self.queued_bytes + item.size > MAX_CONNECTION_QUEUE_BYTES
                or self.queue.qsize() >= MAX_CONNECTION_QUEUE_FRAMES):
            self.close(p.CLOSE_OVERLOADED, "overloaded")
            return False
        self.queued_bytes += item.size
        if item.subscription is not None:
            item.subscription.queued_frames += 1
            item.subscription.queued_bytes += item.size
        self.queue.put_nowait(item)
        return True

    def dequeued(self, item: Outbound) -> None:
        self.queued_bytes -= item.size
        if item.subscription is not None:
            item.subscription.queued_frames -= 1
            item.subscription.queued_bytes -= item.size

    def close(self, code: int, reason: str) -> None:
        """Revocation discards buffers: nothing queued is released afterwards (§9)."""
        if self.closed:
            return
        self.closed, self.close_code, self.close_reason = True, code, reason
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if item is not None:
                self.dequeued(item)
        self.queue.put_nowait(None)


@dataclass(slots=True)
class Pending:
    id: str
    requester: ControlConnection
    request_id: str
    body: dict
    executor: ControlConnection
    deadline: float
    mutation: bool


@dataclass(slots=True)
class Subscription:
    id: str
    requester: ControlConnection
    executor: ControlConnection
    body: dict
    frames: TokenBucket
    bytes: TokenBucket
    queued_frames: int = 0
    queued_bytes: int = 0

    @property
    def export_id(self) -> str:
        return self.body["target"]["export_id"]


@dataclass(slots=True)
class _Limits:
    buckets: dict[tuple, TokenBucket] = field(default_factory=dict)

    def take(self, key: tuple, rate: float, burst: float, now: float) -> int | None:
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = self.buckets[key] = TokenBucket(rate, burst, now)
        return None if bucket.take(now) else bucket.retry_after(now)


class Relay:
    def __init__(self, auth: AiAuth, *, monotonic: Callable[[], float] = time.monotonic,
                 wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000) -> None:
        self.auth = auth
        self.monotonic, self.wall_ms = monotonic, wall_ms
        self.connections: dict[str, ControlConnection] = {}
        self.executors: dict[str, ControlConnection] = {}
        self.pending: dict[str, Pending] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self._limits = _Limits()
        self._handlers: dict[str, Callable[[ControlConnection, str, dict], None]] = {
            "executor.attach": self._on_attach,
            "directory.list": self._on_directory,
            "request": self._on_request,
            "response": self._on_response,
            "event": self._on_event,
            "export.publish": self._on_publish,
            "export.unpublish": self._on_unpublish,
            "grant.put": self._on_grant_put,
            "grant.revoke": self._on_grant_revoke,
            "mirror.get": self._on_mirror_get,
            "resource.index.begin": self._on_index_begin,
            "resource.index.page": self._on_index_page,
            "resource.index.commit": self._on_index_commit,
            "heartbeat.ping": self._on_ping,
        }

    @property
    def server_instance_id(self) -> str:
        return self.auth.ai.server_instance_id

    # ------------------------------------------------------------- connections
    def connect(self, actor: Actor) -> ControlConnection:
        connection = ControlConnection(actor)
        self.connections[connection.id] = connection
        self._send(connection, "hello", str(uuid.uuid4()), {
            "acp": p.ACP_VERSION, "server_instance_id": self.server_instance_id,
            "connection_id": connection.id, "lease_seconds": p.LEASE_SECONDS,
            "heartbeat_seconds": p.HEARTBEAT_SECONDS})
        return connection

    def disconnect(self, connection: ControlConnection, code: int = 1000, reason: str = "closed") -> None:
        connection.close(code, reason)
        if self.connections.pop(connection.id, None) is None:
            return
        if self.executors.get(connection.actor.registration_id) is connection:
            del self.executors[connection.actor.registration_id]
        for pending in list(self.pending.values()):
            if pending.requester is connection:
                del self.pending[pending.id]  # a late response is simply dropped
            elif pending.executor is connection:
                del self.pending[pending.id]
                self._fail_pending(pending)
        for subscription in list(self.subscriptions.values()):
            if connection in (subscription.requester, subscription.executor):
                self._end_subscription(subscription, "resync_required")

    def _deny_reason(self, actor: Actor) -> tuple[int, str]:
        if not self.auth.store.credential_valid(actor.user_id, actor.credential_version):
            return p.CLOSE_UNAUTHORISED, "unauthorised"
        return p.CLOSE_PERMISSION_REVOKED, "permission_revoked"

    def close_user(self, user_id: str, credential_version: int | None = None) -> int:
        """Close every socket of *user_id* whose credentials are no longer current (R-A0)."""
        closed = 0
        for connection in list(self.connections.values()):
            if connection.actor.user_id == user_id and not self.auth.current(connection.actor):
                self.disconnect(connection, *self._deny_reason(connection.actor))
                closed += 1
        return closed

    def close_registration(self, registration_id: str) -> None:
        for connection in list(self.connections.values()):
            if connection.actor.registration_id == registration_id:
                self.disconnect(connection, p.CLOSE_PERMISSION_REVOKED, "permission_revoked")
        self._drop_where(lambda body: body["target"]["executor_registration_id"] == registration_id)

    # ------------------------------------------------------------- sending
    def _send(self, connection: ControlConnection, kind: str, request_id: str, body: dict, *,
              subscription: Subscription | None = None, check: Callable[[], bool] | None = None) -> bool:
        text = p.frame(kind, request_id, body)
        size = len(text.encode("utf-8"))
        if size > p.MAX_FRAME_BYTES:
            raise p.RequestRejected("capacity_exceeded")
        return connection.enqueue(Outbound(text, size, subscription, check))

    def _reply(self, connection: ControlConnection, request_id: str, *, state: str = "completed",
               code: str | None = None, result: Any = None, target: dict | None = None,
               command_id: str | None = None, retry_after: int | None = None) -> None:
        body: dict[str, Any] = {"state": state}
        if target is not None:
            body["target"] = target
        if command_id is not None:
            body["command_id"] = command_id
        if code is not None:
            body["code"] = code
        if retry_after is not None:
            body["retry_after"] = retry_after
        if result is not None:
            body["result"] = result
        self._send(connection, "response", request_id, body)

    def deliverable(self, connection: ControlConnection, item: Outbound) -> bool:
        """Called by the writer immediately before the network send."""
        if connection.closed:
            return False
        if not self.auth.current(connection.actor):
            self.disconnect(connection, *self._deny_reason(connection.actor))
            return False
        return item.check is None or item.check()

    # ------------------------------------------------------------- frames
    def handle(self, connection: ControlConnection, raw: str | bytes) -> None:
        """Act on one inbound frame; :class:`CloseConnection` ends the socket."""
        if connection.closed:
            raise CloseConnection(connection.close_code, connection.close_reason)
        # Per-frame key + device check, independent of the Documents watcher.
        if not self.auth.current(connection.actor):
            raise CloseConnection(*self._deny_reason(connection.actor))
        if not isinstance(raw, str):
            raise CloseConnection(p.CLOSE_PROTOCOL_ERROR, "protocol_error")
        try:
            kind, request_id, body = p.parse_frame(raw)
            if kind in p.EXECUTOR_TYPES and connection.executor_epoch is None:
                raise p.AcpError("executor frame before attach")
            if kind not in ("executor.attach", "heartbeat.ping") and connection.executor_epoch is not None:
                self._check_lease(connection)
            try:
                self._handlers[kind](connection, request_id, body)
            except NotFound:
                self._reply(connection, request_id, state="rejected", code="not_available")
            except p.RequestRejected as exc:
                self._reply(connection, request_id, state="rejected", code=exc.code,
                            retry_after=exc.retry_after, result=exc.result,
                            target=body.get("target") if kind == "request" else None,
                            command_id=body.get("command_id") if kind == "request" else None)
        except p.AcpError:
            raise CloseConnection(p.CLOSE_PROTOCOL_ERROR, "protocol_error") from None

    def _check_lease(self, connection: ControlConnection) -> None:
        if self.monotonic() >= connection.lease_deadline:
            raise CloseConnection(p.CLOSE_PROTOCOL_ERROR, "lease_expired")

    def _executor(self, connection: ControlConnection) -> str:
        return connection.actor.registration_id

    # --- executor lifecycle -----------------------------------------------------
    def _on_attach(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.fields(body, {"executor_epoch", "manifest"})
        if connection.actor.kind != "studio" or connection.executor_epoch is not None:
            raise p.AcpError("only a studio registration attaches once per socket")
        epoch = p.check_uuid4(body["executor_epoch"])
        manifest = p.check_list(body["manifest"], _manifest_entry, max_items=p.MAX_MANIFEST, unique=False)
        if len({entry["export_id"] for entry in manifest}) != len(manifest):
            raise p.AcpError("duplicate manifest export")
        now = self.monotonic()
        current = self.executors.get(connection.actor.registration_id)
        if current is not None and current is not connection:
            if not current.closed and now < current.lease_deadline:
                # One live executor per registration; the second never displaces the first.
                raise CloseConnection(p.CLOSE_EXECUTOR_CONFLICT, "executor_conflict")
            self.disconnect(current, p.CLOSE_EXECUTOR_CONFLICT, "lease_expired")
        connection.executor_epoch = epoch
        connection.lease_deadline = now + p.LEASE_SECONDS
        self.executors[connection.actor.registration_id] = connection
        mirrors = []
        for entry in manifest:
            export = self.auth.ai.export(entry["export_id"])
            if export is None or export.executor_registration_id != connection.actor.registration_id:
                mirrors.append({"export_id": entry["export_id"], "mirror_status": "missing"})
            elif (export.state == "published" and export.revision == entry["revision"]
                  and export.hash == entry["hash"]):
                mirrors.append({**export.ack(), "mirror_status": "matched"})
            else:
                mirrors.append({**export.ack(), "mirror_status": "mismatched"})
        self._reply(connection, request_id, result={"executor_epoch": epoch, "lease_seconds": p.LEASE_SECONDS,
                                                    "mirrors": mirrors})

    def _on_ping(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.fields(body, {"nonce", "executor_epoch"})
        p.check_uuid(body["nonce"])
        if body["executor_epoch"] != connection.executor_epoch:
            raise p.AcpError("heartbeat for another executor epoch")
        self._check_lease(connection)
        # Lease = monotonic time of the last *valid* ping + 15 s (§6); auth was checked above.
        connection.lease_deadline = self.monotonic() + p.LEASE_SECONDS
        self._send(connection, "heartbeat.pong", request_id, {
            "nonce": body["nonce"], "executor_epoch": connection.executor_epoch,
            "lease_seconds": p.LEASE_SECONDS})

    def _on_publish(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        before = self.auth.ai.export(body.get("export_id")) if isinstance(body.get("export_id"), str) else None
        export = self.auth.ai.publish(self._executor(connection), body, self.wall_ms())
        if before is not None and before.revision != export.revision:
            self._drop_where(lambda b: b["target"]["export_id"] == export.export_id)
        self._reply(connection, request_id, result=export.ack())

    def _on_unpublish(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        export = self.auth.ai.unpublish(self._executor(connection), body, self.wall_ms())
        self._drop_where(lambda b: b["target"]["export_id"] == export.export_id)
        self._reply(connection, request_id, result=export.ack())

    def _on_grant_put(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        grant = body.get("grant")
        grantee = None
        if isinstance(grant, dict) and isinstance(grant.get("grantee_registration_id"), str):
            candidate = self.auth.ai.registration(grant["grantee_registration_id"])
            grantee = candidate if self.auth.registration_live(candidate) else None
        before = self.auth.ai.grant(body["grant_id"]) if isinstance(body.get("grant_id"), str) else None
        stored = self.auth.ai.put_grant(self._executor(connection), body, self.wall_ms(), grantee)
        if before is not None and before.revision != stored.revision:
            # Narrowing applies at once: re-check every live use of this grant.
            self._drop_where(lambda b: b["grant_id"] == stored.grant_id)
        self._reply(connection, request_id, result=stored.ack())

    def _on_grant_revoke(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        stored = self.auth.ai.revoke_grant(self._executor(connection), body, self.wall_ms())
        self._drop_where(lambda b: b["grant_id"] == stored.grant_id)
        self._reply(connection, request_id, result=stored.ack())

    def _on_mirror_get(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.fields(body, {"export_id"}, {"grant_id"})
        p.check_uuid(body["export_id"])
        export = self.auth.ai.export(body["export_id"])
        if export is None or export.executor_registration_id != self._executor(connection):
            raise NotFound()
        result: dict[str, Any] = {"export": export.ack()}
        if "grant_id" in body:
            p.check_uuid(body["grant_id"])
            grant = self.auth.ai.grant(body["grant_id"])
            if grant is None or grant.export_id != export.export_id:
                raise NotFound()
            result["grant"] = grant.ack()
        self._reply(connection, request_id, result=result)

    def _on_index_begin(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.fields(body, {"export_id", "export_revision", "index_revision", "row_count", "hash"})
        p.check_uuid(body["export_id"])
        p.check_int(body["export_revision"], 1)
        index_revision = p.check_int(body["index_revision"], 1)
        row_count = p.check_int(body["row_count"], 0, p.MAX_INDEX_ROWS)
        p.check_hash(body["hash"])
        export = self.auth.ai.export(body["export_id"])
        if export is None or export.executor_registration_id != self._executor(connection):
            raise NotFound()
        if export.state != "published" or export.revision != body["export_revision"]:
            raise p.RequestRejected("stale_target", result=export.ack())
        # One staging index per export: a new begin replaces an unfinished one.
        for key, staged in list(connection.staging.items()):
            if staged["export_id"] == body["export_id"]:
                del connection.staging[key]
        if index_revision in connection.staging:
            raise p.RequestRejected("busy")
        connection.staging[index_revision] = {**body, "rows": [], "bytes": 0, "next_page": 0}
        self._reply(connection, request_id, result={"index_revision": index_revision, "state": "staging"})

    def _on_index_page(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.fields(body, {"index_revision", "page_no", "rows"})
        staged = connection.staging.get(p.check_int(body["index_revision"], 1))
        if staged is None:
            raise p.RequestRejected("resync_required")
        if p.check_int(body["page_no"], 0) != staged["next_page"]:
            del connection.staging[body["index_revision"]]
            raise p.RequestRejected("resync_required")
        rows = p.check_list(body["rows"], p.check_index_row, max_items=p.MAX_PAGE_ITEMS, unique=False)
        size = sum(len(p.canonical(row)) for row in rows)
        if (len(staged["rows"]) + len(rows) > staged["row_count"]
                or staged["bytes"] + size > p.MAX_STAGING_BYTES):
            del connection.staging[body["index_revision"]]
            raise p.RequestRejected("capacity_exceeded")
        staged["rows"].extend(rows)
        staged["bytes"] += size
        staged["next_page"] += 1
        self._reply(connection, request_id, result={"index_revision": body["index_revision"],
                                                    "page_no": body["page_no"], "state": "staging"})

    def _on_index_commit(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.fields(body, {"index_revision", "hash"})
        staged = connection.staging.pop(p.check_int(body["index_revision"], 1), None)
        p.check_hash(body["hash"])
        if staged is None:
            raise p.RequestRejected("resync_required")
        if len(staged["rows"]) != staged["row_count"] or body["hash"] != staged["hash"]:
            raise p.RequestRejected("snapshot_changed")
        ack = self.auth.ai.replace_index(self._executor(connection), staged["export_id"],
                                         staged["export_revision"], staged["index_revision"],
                                         staged["rows"], staged["hash"])
        self._recheck_subscriptions(lambda s: s.export_id == staged["export_id"])
        self._reply(connection, request_id, result={**ack, "state": "committed"})

    # --- requester ---------------------------------------------------------------
    def _on_directory(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.fields(body, set(), {"after"})
        after = p.check_uuid(body["after"]) if "after" in body else None
        now = self.monotonic()
        visible = [(g, e) for g, e in self.auth.visible_grants(connection.actor, self.wall_ms())
                   if after is None or g.grant_id > after]
        targets: list[dict] = []
        next_after = None
        for grant, export in visible:
            executor = self.executors.get(export.executor_registration_id)
            online = executor is not None and not executor.closed and now < executor.lease_deadline
            record = grant.record or {}
            entry = {"target": self.auth.target_of(export), "display_name": export.display_name,
                     "state": "online" if online else "offline",
                     "executor_epoch": executor.executor_epoch if online else None,
                     "grant": {key: record[key] for key in (
                         "grant_id", "revision", "operations", "team_ids", "member_ids",
                         "resource_scope", "expires_at")}}
            candidate = {"acp": 1, "type": "response", "request_id": request_id, "body": {
                "state": "completed", "result": {"targets": targets + [entry], "next_after": None}}}
            # A page must decode under the peer's own frame bounds: bytes *and* the
            # §8 total-field budget (≈6 targets per page), never only a byte cap.
            if (len(targets) == DIRECTORY_PAGE or p.field_count(candidate) > p.MAX_FIELDS
                    or len(p.encode(candidate).encode("utf-8")) > p.MAX_FRAME_BYTES):
                next_after = targets[-1]["grant"]["grant_id"] if targets else None
                break
            targets.append(entry)
        self._reply(connection, request_id, result={"targets": targets, "next_after": next_after})

    def _on_request(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.check_request(body)
        actor, now = connection.actor, self.monotonic()
        op = body["op"]
        _, mutation, _ = p.OPERATIONS[op]
        if sum(1 for item in self.pending.values() if item.requester is connection) >= p.MAX_INFLIGHT:
            raise p.RequestRejected("rate_limited", retry_after=1)
        export_id = body["target"]["export_id"]
        for key, rate, burst in ((("principal", actor.user_id), 20, 40),
                                 (("mutation" if mutation else "read", actor.registration_id, export_id),
                                  1 if mutation else 4, 4 if mutation else 8)):
            wait = self._limits.take(key, rate, burst, now)
            if wait is not None:
                raise p.RequestRejected("rate_limited", retry_after=wait)
        grant = self.auth.authorize(actor, body, self.wall_ms()).grant
        if op == "stream.subscribe":
            source = sum(1 for s in self.subscriptions.values()
                         if s.requester.actor.registration_id == actor.registration_id)
            per_export = sum(1 for s in self.subscriptions.values() if s.export_id == export_id)
            if source >= p.MAX_SUBSCRIPTIONS_PER_SOURCE or per_export >= p.MAX_SUBSCRIPTIONS_PER_EXPORT:
                raise p.RequestRejected("capacity_exceeded")
        if op == "stream.unsubscribe":
            subscription = self.subscriptions.get(body["payload"]["subscription_id"])
            if subscription is None or subscription.requester is not connection:
                raise p.RequestRejected("not_available")
            del self.subscriptions[subscription.id]
        executor = self.executors.get(body["target"]["executor_registration_id"])
        # Only reveal liveness after authorize() has verified the exact target, grant and scope.
        now = self.monotonic()
        if executor is None or executor.closed or now >= executor.lease_deadline:
            raise p.RequestRejected("offline")
        pending = Pending(str(uuid.uuid4()), connection, request_id, body, executor,
                          now + p.RECEIPT_TIMEOUT_SECONDS, mutation)
        sent = self._send(executor, "forward", pending.id, {
            "source": {**actor.source(), "connection_id": connection.id},
            "grant": {"grant_id": grant.grant_id, "revision": grant.revision, "expires_at": grant.expires_at},
            "deadline": self.wall_ms() + int(p.RECEIPT_TIMEOUT_SECONDS * 1000),
            "request": body})
        if not sent:
            raise p.RequestRejected("not_available")
        self.pending[pending.id] = pending
        if mutation:
            self._audit(actor, body, "forwarded")

    def _audit(self, actor: Actor, body: dict, outcome: str) -> None:
        self.auth.ai.audit(at=self.wall_ms(), op=body["op"], outcome=outcome, user_id=actor.user_id,
                           registration_id=actor.registration_id, export_id=body["target"]["export_id"],
                           command_id=body.get("command_id"), command_hash=p.semantic_hash(body))

    def _still_allowed(self, requester: ControlConnection, body: dict) -> bool:
        if requester.closed or not self.auth.current(requester.actor):
            return False
        try:
            self.auth.authorize(requester.actor, body, self.wall_ms())
        except p.RequestRejected:
            return False
        return True

    def _on_response(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.check_response(body)
        pending = self.pending.get(request_id)
        if pending is None:
            return  # timed out or requester gone: late responses are never released
        if pending.executor is not connection:
            raise p.AcpError("response from a connection that was not asked")
        if body["target"] != pending.body["target"] or body.get("command_id") != pending.body.get("command_id"):
            raise p.AcpError("response does not match its request")
        subscription_id = None
        if pending.body["op"] == "stream.subscribe" and body["state"] == "completed":
            # Validate before releasing the pending entry: a bad receipt closes the
            # executor, and disconnect() then still answers the waiting requester.
            result = body.get("result")
            subscription_id = result.get("subscription_id") if isinstance(result, dict) else None
            p.check_uuid(subscription_id)
            if subscription_id in self.subscriptions:
                raise p.AcpError("subscription id reused")
        del self.pending[request_id]
        requester, request = pending.requester, pending.body
        if not self._still_allowed(requester, request):
            # Revoked/expired between forward and response: do not reveal the result.
            if not requester.closed:
                self._reply(requester, pending.request_id,
                            state="outcome_unknown" if pending.mutation else "rejected",
                            code="denied", target=request["target"], command_id=request.get("command_id"))
            if pending.mutation:
                self._audit(requester.actor, request, "denied_after_forward")
            return
        if subscription_id is not None:
            now = self.monotonic()
            self.subscriptions[subscription_id] = Subscription(
                subscription_id, requester, connection, request,
                TokenBucket(p.STREAM_FRAMES_PER_SECOND, p.STREAM_FRAMES_PER_SECOND, now),
                TokenBucket(p.STREAM_BYTES_PER_SECOND, p.STREAM_BYTES_PER_SECOND, now))
        try:
            self._send(requester, "response", pending.request_id, body,
                       check=lambda: self._still_allowed(requester, request))
        except p.RequestRejected:
            self._reply(requester, pending.request_id, state="rejected", code="capacity_exceeded",
                        target=request["target"], command_id=request.get("command_id"))
        if pending.mutation:
            self._audit(requester.actor, request, body["state"])

    def _on_event(self, connection: ControlConnection, request_id: str, body: dict) -> None:
        p.check_event(body)
        subscription = self.subscriptions.get(body["subscription_id"])
        if subscription is None:
            return  # ended (unsubscribe/revoke/overflow): nothing is buffered for later
        if subscription.executor is not connection:
            raise p.AcpError("event for a subscription this connection does not serve")
        request = subscription.body
        if body["target"] != request["target"] or body["resource_id"] != request["resource_id"]:
            raise p.AcpError("event outside its subscription")
        requester = subscription.requester
        if not self._still_allowed(requester, request):
            self._end_subscription(subscription, "denied")
            return
        text = p.frame("event", str(uuid.uuid4()), body)
        size = len(text.encode("utf-8"))
        now = self.monotonic()
        if (size > p.MAX_FRAME_BYTES or not subscription.frames.take(now)
                or not subscription.bytes.take(now, size)
                or subscription.queued_frames + 1 > p.STREAM_BUFFER_FRAMES
                or subscription.queued_bytes + size > p.STREAM_BUFFER_BYTES):
            # Slow requester or bursty stream: gap → resync, never unbounded memory.
            self._end_subscription(subscription, "resync_required")
            return
        requester.enqueue(Outbound(text, size, subscription,
                                   lambda: subscription.id in self.subscriptions
                                   and self._still_allowed(requester, request)))

    def _end_subscription(self, subscription: Subscription, code: str) -> None:
        if self.subscriptions.pop(subscription.id, None) is None:
            return
        body = {"subscription_id": subscription.id, "target": subscription.body["target"],
                "resource_id": subscription.body["resource_id"], "code": code}
        for connection in (subscription.requester, subscription.executor):
            if not connection.closed:
                self._send(connection, "event", str(uuid.uuid4()), body)

    def _recheck_subscriptions(self, predicate: Callable[[Subscription], bool]) -> None:
        for subscription in list(self.subscriptions.values()):
            if predicate(subscription) and not self._still_allowed(subscription.requester, subscription.body):
                self._end_subscription(subscription, "denied")

    def _drop_where(self, predicate: Callable[[dict], bool]) -> None:
        for subscription in list(self.subscriptions.values()):
            if predicate(subscription.body):
                self._end_subscription(subscription, "denied")

    def _fail_pending(self, pending: Pending) -> None:
        requester = pending.requester
        if requester.closed:
            return
        self._reply(requester, pending.request_id,
                    state="outcome_unknown" if pending.mutation else "rejected",
                    code="outcome_unknown" if pending.mutation else "not_available",
                    target=pending.body["target"], command_id=pending.body.get("command_id"))
        if pending.mutation:
            self._audit(requester.actor, pending.body, "outcome_unknown")

    # ------------------------------------------------------------- periodic sweep
    def tick(self) -> None:
        """Every ≤5 s: close revoked/expired sockets, time out receipts, re-check streams."""
        now = self.monotonic()
        for connection in list(self.connections.values()):
            if not self.auth.current(connection.actor):
                self.disconnect(connection, *self._deny_reason(connection.actor))
            elif connection.executor_epoch is not None and now >= connection.lease_deadline:
                self.disconnect(connection, p.CLOSE_PROTOCOL_ERROR, "lease_expired")
        for pending in list(self.pending.values()):
            if now >= pending.deadline:
                del self.pending[pending.id]
                self._fail_pending(pending)
        self._recheck_subscriptions(lambda _s: True)
        # Bounded limiter memory: a refilled, idle bucket carries no state worth keeping.
        for key, bucket in list(self._limits.buckets.items()):
            if now - bucket.last > 60:
                del self._limits.buckets[key]


def _manifest_entry(value: object) -> dict:
    p.fields(value, {"export_id", "revision", "hash"})
    p.check_uuid(value["export_id"])
    p.check_int(value["revision"], 1)
    p.check_hash(value["hash"])
    return value  # type: ignore[return-value]
