"""In-process fan-out event bus backing the WebSocket channel.

Design note: a slow browser must never stall pipeline execution, so each subscriber
gets a bounded queue. On overflow the *oldest* events are dropped and the subscriber
is told to resync over REST, rather than applying backpressure to the producer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..models.base import utcnow
from .events import EventType

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 1000
DEFAULT_HISTORY_SIZE = 500


@dataclass(slots=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: datetime = field(default_factory=utcnow)
    channel: str | None = None
    """Optional finer-grained topic, e.g. ``team.mbr_123.message``."""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "seq": self.seq,
            "ts": self.ts.isoformat(),
            "data": self.data,
        }
        if self.channel:
            payload["channel"] = self.channel
        return payload


class Subscription:
    """A single consumer's view of the bus."""

    def __init__(self, bus: "EventBus", maxsize: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._dropped = 0

    def _offer(self, event: Event) -> None:
        """Non-blocking enqueue; evicts the oldest event when full."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(event)

    async def get(self) -> Event:
        return await self._queue.get()

    def take_dropped(self) -> int:
        """Number of events evicted since the last call, then reset.

        The transport asks for this and tells its own client to resync — a broadcast
        would be wrong, since only the subscriber that fell behind lost anything.
        """
        dropped, self._dropped = self._dropped, 0
        if dropped:
            log.warning("Subscriber fell behind; dropped %d event(s)", dropped)
        return dropped

    @property
    def dropped(self) -> int:
        return self._dropped

    async def stream(self) -> AsyncIterator[Event]:
        while True:
            yield await self.get()

    def close(self) -> None:
        self._bus._unsubscribe(self)


class EventBus:
    """Fan-out to every connected client, with a short replay buffer for reconnects."""

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        history_size: int = DEFAULT_HISTORY_SIZE,
    ) -> None:
        self._subscribers: set[Subscription] = set()
        self._queue_size = queue_size
        self._history: deque[Event] = deque(maxlen=history_size)
        self._seq = 0
        self._lock = asyncio.Lock()
        self.forward_to: EventBus | None = None
        """The persistent browser bus, only while this project is selected (§5.1).

        Local subscribers (plugins and automations) always receive their own events.
        Background projects must never send them to another project's browser.
        """

    async def publish(
        self,
        event_type: EventType | str,
        data: dict[str, Any] | None = None,
        *,
        channel: str | None = None,
    ) -> Event:
        async with self._lock:
            self._seq += 1
            event = Event(
                type=str(event_type), data=data or {}, seq=self._seq, channel=channel
            )
            self._history.append(event)
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber._offer(event)
        if self.forward_to is not None:
            await self.forward_to.publish(event_type, data, channel=channel)
        return event

    def clear_history(self) -> None:
        """Discard the previous project's replay buffer without resetting its sequence."""
        self._history.clear()

    def publish_nowait(
        self,
        event_type: EventType | str,
        data: dict[str, Any] | None = None,
        *,
        channel: str | None = None,
    ) -> None:
        """Fire-and-forget publish for sync call sites (e.g. filesystem watcher callbacks)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("publish_nowait outside an event loop; dropping %s", event_type)
            return
        loop.create_task(self.publish(event_type, data, channel=channel))

    def subscribe(self, *, since_seq: int | None = None) -> Subscription:
        subscription = Subscription(self, self._queue_size)
        self._subscribers.add(subscription)
        if since_seq is not None:
            for event in self.replay(since_seq):
                subscription._offer(event)
        return subscription

    def replay(self, since_seq: int) -> Iterable[Event]:
        return [event for event in self._history if event.seq > since_seq]

    def _unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription)

    @property
    def current_seq(self) -> int:
        return self._seq

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
