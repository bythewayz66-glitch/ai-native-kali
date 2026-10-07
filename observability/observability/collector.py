"""Collector: pulls Kanban events and tool audit rows into one observable stream.

Blueprint ref: sections 04.1/04.3/04.6. The collector is a *pull* loop with an
idempotent cursor per source, which means it can be restarted at any time without
duplicating or losing alerts.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import httpx

from .alerting import AlertRouter
from .metrics import Metrics, summarise_alerts
from .model_calls import ModelCallStore
from .model_health import ModelHealthProbe
from .retention import RetentionPolicy, RetentionSweeper, Store


class Collector:
    """Aggregates the platform's event streams into panel-ready state."""

    def __init__(
        self,
        *,
        kanban_url: str = "http://127.0.0.1:8081",
        tools_url: str = "http://127.0.0.1:8083",
        max_events: int = 5000,
        max_traces: int = 5000,
        slow_ms: int = 5000,
        timeout: float = 20.0,
        alert_router: Optional[AlertRouter] = None,
        model_calls: Optional[ModelCallStore] = None,
        retention: Optional[RetentionSweeper] = None,
        cursor_path: Optional[str] = None,
    ) -> None:
        self.kanban_url = kanban_url.rstrip("/")
        self.tools_url = tools_url.rstrip("/")
        self.max_events = max_events
        self.max_traces = max_traces
        self.slow_ms = slow_ms
        self.timeout = timeout

        self.metrics = Metrics()
        self.events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self.traces: deque[dict[str, Any]] = deque(maxlen=max_traces)
        self.audit: deque[dict[str, Any]] = deque(maxlen=max_events)
        self.ingested_events = 0
        self.ingested_traces = 0
        self.ingested_audit = 0
        self.errors: list[str] = []
        self.last_poll_at: Optional[float] = None
        # -- Phase 16: restart continuity ---------------------------------------
        # The audit cursor used to live in the *server's* loop state, so a restart
        # re-pulled the chain from seq 0. That is not just wasted work: a
        # long-running chain re-ingested from the beginning fills the bounded
        # buffer with rows that were already delivered, and any alert derived from
        # "what arrived since the last poll" fires again for old activity. Keeping
        # the cursor on disk makes a restart resume rather than replay.
        self.cursor_path = Path(cursor_path or os.environ.get("OBS_CURSOR_FILE", "var/obs_cursors.json"))
        self.cursors: dict[str, int] = self._load_cursors()
        #: Seen ids are capped for the same reason latencies are: an unbounded set
        #: on a long-running collector is a slow leak. When it is trimmed the
        #: *oldest* ids go, which is correct - a source would have to re-deliver a
        #: very old event to need them, and the audit cursor already prevents that.
        self._seen_events: deque[str] = deque(maxlen=max_events * 4)
        self._lock = threading.RLock()

        # -- Phase 5 subsystems -------------------------------------------------
        # Owned by the collector rather than by the server so that anything
        # holding a collector (the dashboard, a test, the shell) sees the same
        # captured calls, heartbeats and deliveries - one instance, one truth.
        self.model_calls = model_calls or ModelCallStore()
        self.model_probe = ModelHealthProbe()
        self.alerts_router = alert_router or AlertRouter()
        # Retention covers only the *diagnostic* buffers. The two hash-chained
        # stores are declared here as well, so the sweep reports them as refused
        # (with a reason) instead of silently omitting them - see retention.py.
        self.retention = retention or RetentionSweeper()
        self.retention.add_buffer("obs_events", self.events)
        self.retention.add_buffer("obs_traces", self.traces)
        self.retention.add_store(Store.for_capture("model_calls", self.model_calls))
        self.retention.policy = self.retention.policy or RetentionPolicy()
        self.alerts_routed = 0
        self.alerts_suppressed = 0
        self.last_route_at: Optional[float] = None


    # -- cursors (Phase 16) -------------------------------------------------
    def _load_cursors(self) -> dict[str, int]:
        """Read persisted cursors. A corrupt file degrades to empty, never raises.

        A collector that refuses to start because its cursor file is truncated is
        worse than one that re-pulls: the first is an outage, the second is a
        duplicate alert. Bad data here is not worth failing over.
        """
        try:
            raw = json.loads(self.cursor_path.read_text())
            return {str(k): int(v) for k, v in raw.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_cursors(self) -> None:
        """Persist cursors atomically, so a crash cannot leave a half-written file."""
        try:
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cursor_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cursors, sort_keys=True))
            tmp.replace(self.cursor_path)
        except OSError as exc:  # pragma: no cover - filesystem failure
            self._record_error(f"cursor persist: {exc}")

    def cursor(self, source: str) -> int:
        """The last position consumed from *source* (0 if never read)."""
        return int(self.cursors.get(source, 0) or 0)

    # -- sources ---------------------------------------------------------
    def _fetch(self, url: str, params: Optional[dict[str, Any]] = None) -> Any:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    def ingest_kanban_events(self) -> int:
        """Pull new card events (the board *is* the activity timeline)."""
        try:
            payload = self._fetch(f"{self.kanban_url}/api/events", {"limit": self.max_events})
        except Exception as exc:
            self._record_error(f"kanban events: {exc}")
            return 0
        added = 0
        with self._lock:
            for event in payload.get("events", []):
                key = event.get("event_id")
                if key and key in self._seen_events:
                    continue
                if key:
                    self._seen_events.append(key)
                self.events.append(event)
                self.ingested_events += 1
                self.metrics.observe_event(event)
                added += 1
        return added

    def ingest_kanban_traces(self, limit: int = 500) -> int:
        """Pull card traces: every tool call the agents made, with its card."""
        try:
            payload = self._fetch(f"{self.kanban_url}/api/cards", {"limit": limit})
        except Exception as exc:
            self._record_error(f"kanban cards: {exc}")
            return 0
        added = 0
        with self._lock:
            for card in payload.get("cards", []):
                for trace in card.get("traces") or []:
                    key = trace.get("trace_id")
                    if key and key in self._seen_events:
                        continue
                    if key:
                        self._seen_events.append(key)
                    enriched = {
                        **trace,
                        "card_id": card.get("card_id"),
                        "card_title": card.get("title"),
                        "board_id": card.get("board_id"),
                        "column": card.get("column"),
                    }
                    self.traces.append(enriched)
                    self.ingested_traces += 1
                    self.metrics.observe_trace(enriched)
                    added += 1
        return added

    def ingest_tool_audit(
        self, since_seq: Optional[int] = None, limit: int = 2000
    ) -> tuple[int, int]:
        """Pull the tool audit chain (blueprint 04.6).

        Returns ``(added, new_cursor)``. ``since_seq`` omitted means "resume from
        where the last run stopped", which is what makes a restart continuous
        instead of a replay.
        """
        start = self.cursor("audit") if since_seq is None else since_seq
        try:
            payload = self._fetch(
                f"{self.tools_url}/audit", {"limit": limit, "since_seq": start}
            )
        except Exception as exc:
            self._record_error(f"tool audit: {exc}")
            return 0, start
        cursor = start
        added = 0
        with self._lock:
            for row in payload.get("entries", []):
                self.audit.append(row)
                self.ingested_audit += 1
                added += 1
                cursor = max(cursor, int(row.get("seq", 0)))
            if cursor != start:
                self.cursors["audit"] = cursor
                self._save_cursors()
        return added, cursor

    def ingest_model_health(self, records: list[dict[str, Any]]) -> None:
        with self._lock:
            for record in records:
                self.metrics.observe_model(record)

    # -- Phase 5: model heartbeats, alert routing, retention ----------------
    def beat_model(self, *, probe: Optional[Any] = None) -> dict[str, Any]:
        """Take one model heartbeat and fold it into the model panel.

        Called from the poll loop. The probe is injectable so a test can drive
        up->down->up without a model endpoint, and so a deployment can point this
        at a different health signal than the runtime's own.
        """
        if probe is not None:
            self.model_probe._probe = probe  # noqa: SLF001 - deliberate injection point
        beat = self.model_probe.beat()
        # The *same* heartbeat feeds the model panel and the alert router, so the
        # panel and the alert can never disagree about whether the model was up.
        self.ingest_model_health(self.model_probe.model_records())
        return beat.as_dict()

    def route_alerts(self, *, limit: int = 100) -> dict[str, Any]:
        """Derive alerts from observed data, then route them.

        Two steps that were one: ``alerts()`` derives, ``route_alerts`` delivers.
        Keeping them separate means the dashboard can read the derived list
        without re-routing it on every poll, and the poll loop can route without
        re-rendering anything.
        """
        derived = self.alerts(limit=limit)
        derived.extend(self.model_probe.alerts())
        outcome = self.alerts_router.route_many(derived)
        with self._lock:
            self.alerts_routed += outcome["delivered"]
            self.alerts_suppressed += outcome["suppressed"]
            self.last_route_at = time.time()
        return outcome

    def sweep_retention(self, *, now: Optional[float] = None) -> dict[str, Any]:
        """Run the retention policy over the diagnostic stores."""
        return self.retention.sweep(now=now)

    def route_and_sweep(self, *, alert_limit: int = 100) -> dict[str, Any]:
        """The poll loop's post-ingest step: heartbeat, route, prune."""
        return {
            "model": self.beat_model(),
            "alerts": self.route_alerts(limit=alert_limit),
            "retention": self.sweep_retention(),
        }

    def _record_error(self, message: str) -> None:
        self.errors.append(message)
        del self.errors[:-20]

    # -- panels ----------------------------------------------------------
    def timeline(self, *, limit: int = 100, agent: Optional[str] = None, card_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Panel 04.1 - agent activity timeline."""
        out = []
        for event in reversed(self.events):
            if agent and event.get("actor") != agent:
                continue
            if card_id and event.get("card_id") != card_id:
                continue
            out.append(
                {
                    "ts": event.get("ts"),
                    "kind": "event",
                    "type": event.get("type"),
                    "actor": event.get("actor"),
                    "card_id": event.get("card_id"),
                    "from": event.get("from_column"),
                    "to": event.get("to_column"),
                    "note": event.get("note"),
                }
            )
            if len(out) >= limit:
                break
        return out

    def tool_call_traces(
        self, *, limit: int = 100, tool: Optional[str] = None, card_id: Optional[str] = None, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Panel 04.3 - tool-call traces."""
        out = []
        for trace in reversed(self.traces):
            if tool and trace.get("tool") != tool:
                continue
            if card_id and trace.get("card_id") != card_id:
                continue
            if status and trace.get("status") != status:
                continue
            out.append(trace)
            if len(out) >= limit:
                break
        return out

    def audit_log(self, *, limit: int = 200, tool: Optional[str] = None, card_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Panel 04.6 - the tool audit log, newest first."""
        out = []
        for row in reversed(self.audit):
            if tool and row.get("tool") != tool:
                continue
            if card_id and row.get("card_id") != card_id:
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        """Panel 04.7 - alerts derived from observed data."""
        found = summarise_alerts(list(self.events), list(self.traces), slow_ms=self.slow_ms)
        found.sort(key=lambda a: a.get("ts") or "", reverse=True)
        return found[:limit]

    def card_replay(self, card_id: str) -> dict[str, Any]:
        """BluePrint 03.4 - replay a card's full execution history.

        Merges all three streams for one card into a single ordered log, which is
        what the shell's card drawer renders.
        """
        merged: list[dict[str, Any]] = []
        for event in self.events:
            if event.get("card_id") == card_id:
                merged.append({"kind": "event", **event})
        for trace in self.traces:
            if trace.get("card_id") == card_id:
                merged.append({"kind": "trace", **trace})
        for row in self.audit:
            if row.get("card_id") == card_id:
                merged.append({"kind": "audit", **row})
        merged.sort(key=lambda item: item.get("ts") or "")
        return {
            "card_id": card_id,
            "steps": merged,
            "count": len(merged),
            "events": sum(1 for m in merged if m["kind"] == "event"),
            "traces": sum(1 for m in merged if m["kind"] == "trace"),
            "audit_rows": sum(1 for m in merged if m["kind"] == "audit"),
        }

    def tokens_panel(self) -> dict[str, Any]:
        """Panel 04.2 - token / cost / latency."""
        return {
            "token_cost": self.metrics.token_cost(),
            "latency": self.metrics.latency(),
            "by_agent": self.metrics.snapshot()["by_agent"],
            "by_tier": self.metrics.snapshot()["by_tier"],
        }

    def status(self) -> dict[str, Any]:
        return {
            "kanban_url": self.kanban_url,
            "tools_url": self.tools_url,
            "ingested": {
                "events": self.ingested_events,
                "traces": self.ingested_traces,
                "audit": self.ingested_audit,
            },
            "buffered": {
                "events": len(self.events),
                "traces": len(self.traces),
                "audit": len(self.audit),
            },
            "last_poll_at": self.last_poll_at,
            "errors": self.errors[-5:],
            "metrics": self.metrics.snapshot(),
            "model_calls": self.model_calls.counts(),
            "model_health": self.model_probe.status(),
            "alert_routing": {
                **self.alerts_router.status(),
                "routed": self.alerts_routed,
                "suppressed_by_router": self.alerts_suppressed,
                "last_route_at": self.last_route_at,
            },
            "retention": self.retention.status(),
            "cursors": {"audit": self.cursor("audit"), "path": str(self.cursor_path)},
        }
