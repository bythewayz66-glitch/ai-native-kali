"""Event-driven card claiming over kanban-core's WebSocket event stream.

Blueprint ref: section 03.2 - "column transitions as event bus events" - plus the
Phase 2 item that replaces the bridge's 2-second poll with a real subscription.

Why this exists
---------------
Polling works, but it makes the board's own event bus redundant: the service
layer already *knows* the instant a card enters ``Assigned`` and publishes that
fact on the bus. Claiming a card 0-2 seconds late is a visible latency on a
desktop shell where a human just pressed "assign".

The watcher below subscribes to ``/ws/events`` and hands each event to the bridge
the moment it arrives. Three properties matter and are all tested:

1. **Reconnect with exponential backoff.** A dropped socket must not silently
   stop the system taking work. The watcher reconnects forever, with a capped
   exponential delay, and counts every attempt so ``/health`` can show it.

2. **Polling fallback.** If the socket is down (or ``websockets`` is not
   installed at all), the bridge still makes progress: the supervisor loop in
   :meth:`Bridge.run_forever` polls while the stream is unhealthy. The fallback
   is a *degraded* mode, and it is reported as such rather than hidden.

3. **Events never run on the socket thread.** ``process_event`` executes a whole
   crew: guardrails, tool calls, card writes. Running that inside the asyncio
   reader would stall the socket and miss subsequent events, so the watcher
   hands work to a worker thread through a queue. The socket thread only parses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("agent_runtime.ws_events")

try:  # websockets >= 13 moved the client implementation under .asyncio
    from websockets.asyncio.client import connect as _ws_connect  # type: ignore
except Exception:  # pragma: no cover - older websockets
    try:
        from websockets.client import connect as _ws_connect  # type: ignore
    except Exception:  # pragma: no cover - not installed
        _ws_connect = None


def websockets_available() -> bool:
    """Is a real WebSocket client importable in this environment?"""
    return _ws_connect is not None


def backoff_delay(
    attempt: int,
    *,
    base: float = 0.5,
    factor: float = 2.0,
    max_delay: float = 30.0,
    jitter: float = 0.0,
    rng: Any = None,
) -> float:
    """Exponential backoff for reconnect attempt *attempt* (0-based).

    ``jitter`` is a fraction of the computed delay (0.0 = fully deterministic,
    which is what the tests assert).
    """
    attempt = max(0, int(attempt))
    delay = min(max_delay, base * (factor**attempt))
    if jitter:
        source = rng or random
        delay += source.uniform(0.0, jitter * delay)
    return round(delay, 4)


#: Event types that can make a card claimable.
CLAIM_EVENT_TYPES = (
    "card.moved",
    "card.created",
    "card.assigned",
    # A human decision on a gate the bridge itself opened. The run was parked in
    # ``Assigned`` all along, so without this the approved work sits idle until
    # the next fallback poll - or forever, on a socket-only deployment.
    "card.approval.decided",
)


def is_claimable(event: Any) -> bool:
    """Should this event make the bridge try to claim a card?

    The bridge owns the ``Assigned`` column, so a transition *into* it is work to
    pick up. One other event matters because the bridge creates the situation it
    resolves:

    * ``card.approval.decided`` - the human answered a gate. Only an **approval**
      re-claims: a rejection is a human saying no, and re-claiming it would just
      re-open the same gate.

    Everything else on the bus (traces, artifacts, board events) is interesting
    to observability but is not work to pick up.
    """
    if not isinstance(event, dict):
        return False
    if event.get("card_id") in (None, ""):
        return False
    event_type = event.get("type")
    if event_type not in CLAIM_EVENT_TYPES:
        return False
    # A move declares its destination explicitly.
    if event.get("to_column") is not None:
        return event.get("to_column") == "Assigned"
    if event_type == "card.approval.decided":
        return (event.get("payload") or {}).get("status") == "approved"
    # card.created / card.assigned carry the column in the payload instead.
    payload = event.get("payload") or {}
    if payload.get("column") is not None:
        return payload.get("column") == "Assigned"
    # card.assigned with no column info still means "an agent now owns this".
    return event_type == "card.assigned"


@dataclass
class StreamState:
    """Observable state of the event stream (surfaced on ``/health``)."""

    url: str = ""
    enabled: bool = False
    available: bool = field(default_factory=websockets_available)
    connected: bool = False
    attempts: int = 0
    reconnects: int = 0
    connects: int = 0
    disconnects: int = 0
    snapshots: int = 0
    events: int = 0
    heartbeats: int = 0
    ignored: int = 0
    claimed: int = 0
    dispatched: int = 0
    last_event_at: Optional[float] = None
    last_message_at: Optional[float] = None
    last_connected_at: Optional[float] = None
    last_disconnected_at: Optional[float] = None
    last_error: Optional[str] = None
    #: True once the supervisor has had to fall back to polling.
    degraded_to_polling: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "enabled": self.enabled,
            "available": self.available,
            "connected": self.connected,
            "attempts": self.attempts,
            "reconnects": self.reconnects,
            "connects": self.connects,
            "disconnects": self.disconnects,
            "snapshots": self.snapshots,
            "events": self.events,
            "heartbeats": self.heartbeats,
            "ignored": self.ignored,
            "claimed": self.claimed,
            "dispatched": self.dispatched,
            "last_event_at": self.last_event_at,
            "last_message_at": self.last_message_at,
            "last_connected_at": self.last_connected_at,
            "last_disconnected_at": self.last_disconnected_at,
            "last_error": self.last_error,
            "degraded_to_polling": self.degraded_to_polling,
        }


class EventStreamWatcher:
    """Subscribes to ``/ws/events`` and dispatches claimable events.

    ``on_event(event) -> Any`` is called on a worker thread for every message of
    kind ``event``. ``on_connect`` / ``on_disconnect`` are called on the socket
    thread and must be non-blocking (they only flip counters).
    """

    def __init__(
        self,
        url: str,
        on_event: Callable[[dict[str, Any]], Any],
        *,
        on_connect: Optional[Callable[[], Any]] = None,
        on_disconnect: Optional[Callable[[], Any]] = None,
        base_delay: float = 0.5,
        factor: float = 2.0,
        max_delay: float = 30.0,
        jitter: float = 0.0,
        heartbeat_timeout: float = 45.0,
        open_timeout: float = 10.0,
        queue_size: int = 2000,
        connect: Any = None,
    ) -> None:
        self.url = url
        self.on_event = on_event
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self.heartbeat_timeout = heartbeat_timeout
        self.open_timeout = open_timeout

        self.state = StreamState(url=url, enabled=True)
        self._connect = connect or _ws_connect
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._queue: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue(maxsize=queue_size)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "EventStreamWatcher":
        """Start the socket thread and the dispatch worker. Idempotent."""
        if self._thread and self._thread.is_alive():
            return self
        if not self.state.available:
            self.state.enabled = False
            self.state.last_error = "websockets client library is not installed"
            log.warning("event stream disabled: %s", self.state.last_error)
            return self
        self._stop.clear()
        self._worker = threading.Thread(target=self._drain, name="ws-worker", daemon=True)
        self._worker.start()
        self._thread = threading.Thread(target=self._run_loop, name="ws-events", daemon=True)
        self._thread.start()
        return self

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the socket loop and the worker."""
        self._stop.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:  # pragma: no cover - thread timing
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass
        try:
            self._queue.put_nowait(None)  # unblock the worker
        except Exception:
            pass
        for thread in (self._thread, self._worker):
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
        self.state.connected = False

    @property
    def connected(self) -> bool:
        return self.state.connected

    def status(self) -> dict[str, Any]:
        return self.state.as_dict()

    # ------------------------------------------------------------- socket side
    def _run_loop(self) -> None:  # pragma: no cover - exercised via real sockets
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._supervise())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None

    async def _supervise(self) -> None:
        """Connect forever, backing off between failures."""
        attempt = 0
        while not self._stop.is_set():
            self.state.attempts += 1
            try:
                await self._session()
                attempt = 0  # a clean session resets the backoff
            except asyncio.CancelledError:  # pragma: no cover - stop() path
                raise
            except Exception as exc:
                self.state.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                if self.state.connected:
                    self._mark_disconnected()
            if self._stop.is_set():
                break
            if self.state.attempts > 1:
                self.state.reconnects += 1
                self.state.degraded_to_polling = True
            delay = backoff_delay(
                attempt,
                base=self.base_delay,
                factor=self.factor,
                max_delay=self.max_delay,
                jitter=self.jitter,
            )
            attempt += 1
            # Wait out the backoff, but wake immediately when asked to stop.
            await asyncio.to_thread(self._stop.wait, delay)

    async def _session(self) -> None:  # pragma: no cover - needs a live socket
        connect = self._connect
        if connect is None:
            raise RuntimeError("no websocket client available")
        async with connect(
            self.url,
            open_timeout=self.open_timeout,
            close_timeout=5,
            max_size=2**22,
            # The server sends its own application heartbeat every 15s; client
            # pings would only add noise.
            ping_interval=None,
            ping_timeout=None,
        ) as socket:
            self._mark_connected()
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=self.heartbeat_timeout)
                except asyncio.TimeoutError as exc:
                    # No message, not even a heartbeat: treat the stream as stale
                    # and reconnect rather than waiting on a half-open socket.
                    raise TimeoutError(
                        f"no message from {self.url} within {self.heartbeat_timeout}s"
                    ) from exc
                self._handle_raw(raw)

    def _mark_connected(self) -> None:
        self.state.connected = True
        self.state.connects += 1
        self.state.last_connected_at = time.time()
        self.state.degraded_to_polling = False
        # Clear the previous failure. Without this the field is *sticky*: the
        # bridge usually starts a moment before kanban-core has bound its port, so
        # the first connect is refused, the retry succeeds - and every later
        # ``/health`` read still reported
        # ``ConnectionRefusedError: [Errno 111] Connect call failed`` while the
        # stream was in fact connected and claiming cards.
        #
        # A status field that only ever reports failures is worse than none: an
        # operator watching for connection errors learns to ignore the one field
        # that would tell them the truth, and a monitor that keys on it never
        # clears. The last error is now what it says it is - the most recent error
        # *from the current attempt*, dropped the moment an attempt succeeds.
        self.state.last_error = None
        if self.on_connect:
            try:
                self.on_connect()
            except Exception:  # pragma: no cover - defensive
                pass

    def _mark_disconnected(self) -> None:
        self.state.connected = False
        self.state.disconnects += 1
        self.state.last_disconnected_at = time.time()
        if self.on_disconnect:
            try:
                self.on_disconnect()
            except Exception:  # pragma: no cover - defensive
                pass

    # -------------------------------------------------------------- parsing
    def _handle_raw(self, raw: Any) -> Optional[dict[str, Any]]:
        """Parse one frame and enqueue claimable events.

        Returns the parsed message (or ``None`` when it was unparseable), which
        makes this directly testable without a socket.
        """
        try:
            message = json.loads(raw if isinstance(raw, (str, bytes, bytearray)) else str(raw))
        except (ValueError, TypeError):
            log.debug("dropping unparseable frame from %s", self.url)
            return None
        if not isinstance(message, dict):
            return None

        self.state.last_message_at = time.time()
        kind = message.get("kind")

        if kind == "heartbeat":
            self.state.heartbeats += 1
            return message

        if kind == "snapshot":
            # The backlog replay on connect. These are history, not new work:
            # the supervisor polls once on connect anyway, so dispatching them
            # would double-claim. Counted so /health can show the handshake.
            self.state.snapshots += 1
            return message

        if kind != "event":
            return message

        event = message.get("event")
        if not isinstance(event, dict):
            return message

        if is_claimable(event):
            self.state.events += 1
            self.state.last_event_at = time.time()
            try:
                self._queue.put_nowait(event)
            except queue.Full:  # pragma: no cover - only under extreme backlog
                self.state.last_error = "event queue full; dropped a claimable event"
        else:
            self.state.ignored += 1
        return message

    # ------------------------------------------------------------- worker side
    def _drain(self) -> None:  # pragma: no cover - thread body
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:  # stop sentinel
                return
            try:
                self.on_event(event)
                self.state.dispatched += 1
            except Exception as exc:
                self.state.last_error = f"dispatch failed: {type(exc).__name__}: {exc}"

    # ---------------------------------------------------------------- testing
    def inject(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Feed a synthetic frame, exactly as the socket would.

        Used by tests and by the ``/stream/inject`` debug endpoint, so the
        claim path can be exercised without standing up a socket.
        """
        return self._handle_raw(json.dumps(message))
