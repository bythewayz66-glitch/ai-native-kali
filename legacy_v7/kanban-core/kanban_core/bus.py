"""In-process event bus for column transitions.

Blueprint ref: section 03.2 'Column transitions as event bus events'.

The bus is deliberately simple: a fan-out of asyncio queues. Producers (the
service layer) call :meth:`EventBus.publish` synchronously, which is safe from
the event-loop thread and never blocks; consumers (the WebSocket endpoint, the
CrewAI bridge, the observability collector) each own a queue and drain it.

Publishing also persists the event through the store's hash-chained audit log,
so the bus and the audit trail can never disagree.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Optional

from .models import Event


class EventBus:
    """Fan-out bus with a small replay buffer."""

    def __init__(self, *, replay: int = 500, queue_size: int = 2000) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._lock = threading.Lock()
        self._recent: deque[Event] = deque(maxlen=replay)
        self._queue_size = queue_size
        self._published = 0
        self._dropped = 0

    # -- producer side ---------------------------------------------------
    def publish(self, event: Event) -> Event:
        """Fan out to every subscriber. Never blocks, never raises."""
        with self._lock:
            self._recent.append(event)
            self._published += 1
            subs = list(self._subscribers)
        for queue in subs:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow consumer must never stall the orchestration backbone.
                self._dropped += 1
                try:
                    queue.get_nowait()  # drop oldest, keep the stream live
                    queue.put_nowait(event)
                except Exception:  # pragma: no cover - defensive
                    pass
            except Exception:  # pragma: no cover - defensive
                pass
        return event

    # -- consumer side ---------------------------------------------------
    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    @contextmanager
    def subscription(self) -> Iterator[asyncio.Queue[Event]]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    def recent(self, limit: int = 50) -> list[Event]:
        with self._lock:
            items = list(self._recent)
        return items[-limit:]

    # -- introspection ---------------------------------------------------
    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "subscribers": len(self._subscribers),
                "published": self._published,
                "dropped": self._dropped,
                "buffered": len(self._recent),
            }

    def reset(self) -> None:
        with self._lock:
            self._recent.clear()
            self._published = 0
            self._dropped = 0


def drain(queue: asyncio.Queue[Event], limit: int = 100) -> list[Event]:
    """Non-blocking drain helper used by tests and polling consumers."""
    out: list[Event] = []
    while len(out) < limit:
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


#: Process-wide default bus (the API, the bridge and the collector share it).
def get_default_bus() -> EventBus:
    global _DEFAULT_BUS
    try:
        return _DEFAULT_BUS
    except NameError:
        _DEFAULT_BUS = EventBus()
        return _DEFAULT_BUS


_DEFAULT_BUS: Optional[EventBus] = None
