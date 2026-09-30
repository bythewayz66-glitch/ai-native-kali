"""Model-serving heartbeats (blueprint 04.4's missing half).

Phase 4 added token/cost/latency *aggregates* for models, and the collector
already has an ``observe_model`` hook - but nothing ever *produced* a heartbeat,
so the model panel could only ever show numbers for models that happened to have
served a request. A model that has gone away entirely is the case that matters
most, and it was the one case the panel could not express.

What this adds
--------------
A probe that runs on a timer and records **every** attempt, whether it succeeded
or not:

* ``state`` - ``up`` / ``down`` / ``unknown``, derived from the probe result;
* ``latency_ms`` - how long the probe took, which is the earliest signal of a
  model that is technically answering but thrashing;
* ``transitions`` - the count of up->down and down->up edges.

Why transitions are the point, and not the current state
-------------------------------------------------------
A heartbeat that only exposes "up right now" cannot distinguish a model that has
been down for an hour from one that flapped down for one probe. Those need
different responses - page someone versus carry on - and the difference lives
only in the history. So the *first* failure and the *recovery* are both recorded
as discrete events, and the alert rule keys off the transition rather than the
state.

A probe that never raises
-------------------------
An unreachable endpoint is the answer, not an error. Every failure mode - a
raised exception, an HTTP error, a malformed body - is turned into a ``down``
heartbeat with the reason attached, because a health prober that raises on
unhealthy input is a prober that stops reporting exactly when it is needed.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

STATE_UP = "up"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"


@dataclass
class Heartbeat:
    """One probe attempt."""

    ts: float
    state: str
    model: str = ""
    backend: str = ""
    latency_ms: int = 0
    error: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "state": self.state,
            "model": self.model,
            "backend": self.backend,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "detail": self.detail,
        }


class ModelHealthProbe:
    """Probes a model endpoint and keeps its heartbeat history.

    ``probe`` is a callable returning a dict shaped like
    ``agent_runtime.model_client.LocalModelClient.probe``: ``available``,
    ``model``, ``backend``, ``error``. Injectable so the whole thing is testable
    without a model, which is the same rule the rest of the stack follows.
    """

    def __init__(
        self,
        probe: Optional[Callable[[], dict[str, Any]]] = None,
        *,
        max_history: int = 200,
        clock: Callable[[], float] = time.time,
        slow_ms: int = 2000,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._clock = clock
        #: Monotonic timer used to *measure* a probe. Separate from ``clock`` (which
        #: stamps the record) so a test can assert latency without sleeping, and so
        #: the measurement is immune to a wall-clock adjustment mid-probe.
        self._timer = timer
        self.slow_ms = slow_ms
        self.history: deque[Heartbeat] = deque(maxlen=max_history)
        self._lock = threading.RLock()
        self.probes = 0
        self.failures = 0
        self.transitions: list[dict[str, Any]] = []
        self.last_state = STATE_UNKNOWN
        self.last_probe_at: Optional[float] = None

    # -------------------------------------------------------------- probe
    def beat(self) -> Heartbeat:
        """Run one probe. Never raises."""
        started = self._timer()
        detail: dict[str, Any] = {}
        error: Optional[str] = None
        model = ""
        backend = ""

        try:
            result = dict(self._probe() if self._probe else {})
            detail = result
            model = str(result.get("model") or "")
            backend = str(result.get("backend") or "")
            available = bool(result.get("available"))
            if not available:
                error = str(result.get("error") or "model unavailable")
        except Exception as exc:  # noqa: BLE001 - an unreachable endpoint is the answer
            available = False
            error = f"{type(exc).__name__}: {exc}"

        state = STATE_UP if available else STATE_DOWN
        latency_ms = int((self._timer() - started) * 1000)
        beat = Heartbeat(
            ts=self._clock(),
            state=state,
            model=model,
            backend=backend,
            latency_ms=latency_ms,
            error=error,
            detail={k: v for k, v in detail.items() if k != "models"} | (
                {"models_offered": detail.get("models")} if detail.get("models") else {}
            ),
        )

        with self._lock:
            self.probes += 1
            if state == STATE_DOWN:
                self.failures += 1
            previous = self.last_state
            self.history.append(beat)
            self.last_state = state
            self.last_probe_at = beat.ts
            # Record the *edge*, not the level. ``unknown -> down`` counts: the
            # very first probe discovering a dead model is a transition worth
            # reporting, not a baseline to be silently adopted.
            if previous != state:
                self.transitions.append(
                    {
                        "ts": beat.ts,
                        "from": previous,
                        "to": state,
                        "error": error,
                        "model": model,
                    }
                )
                del self.transitions[:-100]
        return beat

    # ------------------------------------------------------------- reports
    def status(self) -> dict[str, Any]:
        """The current posture, plus the history needed to read it correctly."""
        with self._lock:
            up = sum(1 for b in self.history if b.state == STATE_UP)
            down = sum(1 for b in self.history if b.state == STATE_DOWN)
            latencies = [b.latency_ms for b in self.history]
            last = self.history[-1].as_dict() if self.history else None
            recent_transitions = self.transitions[-5:]
            return {
                "state": self.last_state,
                "model": (self.history[-1].model if self.history else ""),
                "backend": (self.history[-1].backend if self.history else ""),
                "probes": self.probes,
                "failures": self.failures,
                "up": up,
                "down": down,
                "availability": round(up / len(self.history), 4) if self.history else None,
                "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
                "max_latency_ms": max(latencies) if latencies else 0,
                "slow": bool(latencies and max(latencies) >= self.slow_ms),
                "last_probe_at": self.last_probe_at,
                "last": last,
                "transitions": recent_transitions,
                "transition_count": len(self.transitions),
            }

    def history_rows(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [b.as_dict() for b in list(self.history)[-limit:]][::-1]

    def model_records(self) -> list[dict[str, Any]]:
        """Heartbeats in the shape ``Metrics.observe_model`` already accepts.

        The collector's model panel reads ``requests``/``errors``/``latency_ms``
        per model, so the probe feeds those fields rather than growing a second
        model-reporting path the dashboard would have to know about.
        """
        with self._lock:
            by_model: dict[str, dict[str, Any]] = {}
            for beat in self.history:
                name = beat.model or "(unknown model)"
                entry = by_model.setdefault(
                    name, {"model": name, "requests": 0, "errors": 0, "tokens": 0, "latency_ms": 0}
                )
                entry["requests"] += 1
                entry["latency_ms"] += beat.latency_ms
                if beat.state == STATE_DOWN:
                    entry["errors"] += 1
            return list(by_model.values())

    def alerts(self) -> list[dict[str, Any]]:
        """Alert rows for the routing layer.

        Keyed off the latest *transition* rather than the current state: an alert
        per probe would fire every few seconds for as long as the model stayed
        down, and an alert stream that repeats is one an operator learns to
        ignore.
        """
        rows: list[dict[str, Any]] = []
        with self._lock:
            if not self.transitions:
                return rows
            for transition in self.transitions[-5:]:
                down = transition["to"] == STATE_DOWN
                rows.append(
                    {
                        "severity": "high" if down else "medium",
                        "kind": "model_down" if down else "model_recovered",
                        "card_id": None,
                        "message": (
                            f"model {transition.get('model') or '(unknown)'} went down: "
                            f"{transition.get('error') or 'no reason given'}"
                            if down
                            else f"model {transition.get('model') or '(unknown)'} recovered"
                        ),
                        "ts": transition["ts"],
                    }
                )
        return rows
