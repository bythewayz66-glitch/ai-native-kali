"""Metrics aggregation over collected events.

Blueprint ref: sections 04.2 and 04.4. Everything is derived from what the
collector actually saw - there are no synthetic numbers here. Percentiles are
computed from the observed latency list, so an empty stack reports zeros rather
than a plausible-looking fiction.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Optional


class Metrics:
    """Rolling aggregates over ingested events."""

    def __init__(self) -> None:
        self.by_agent: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"tool_calls": 0, "duration_ms": 0, "tokens": 0, "cost_usd": 0.0, "errors": 0}
        )
        self.by_tool: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "duration_ms": 0, "denied": 0, "errors": 0, "dry_runs": 0, "live": 0}
        )
        self.by_tier: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.latencies: list[int] = []
        self.tokens_total = 0
        self.cost_total = 0.0
        self.events_total = 0
        self.traces_total = 0
        self.transitions_total = 0
        self.models: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"requests": 0, "errors": 0, "tokens": 0, "latency_ms": 0}
        )

    # -- ingestion -------------------------------------------------------
    def observe_event(self, event: dict[str, Any]) -> None:
        self.events_total += 1
        if event.get("type") == "card.moved":
            self.transitions_total += 1
        actor = event.get("actor") or "system"
        entry = self.by_agent[actor]
        entry["events"] = entry.get("events", 0) + 1
        if event.get("type", "").startswith("card."):
            entry["cards_touched"] = entry.get("cards_touched", 0) + 1

    def observe_trace(self, trace: dict[str, Any]) -> None:
        self.traces_total += 1
        tool = trace.get("tool", "unknown")
        agent = trace.get("agent") or "unassigned"
        tier = int(trace.get("tier", 0) or 0)
        duration = int(trace.get("duration_ms", 0) or 0)
        tokens = int(trace.get("tokens", 0) or 0)
        cost = float(trace.get("cost_usd", 0.0) or 0.0)
        status = trace.get("status", "ok")

        tool_entry = self.by_tool[tool]
        tool_entry["calls"] += 1
        tool_entry["duration_ms"] += duration
        if status in ("denied", "blocked"):
            tool_entry["denied"] += 1
        elif status == "error":
            tool_entry["errors"] += 1
        if trace.get("dry_run"):
            tool_entry["dry_runs"] += 1
        else:
            tool_entry["live"] += 1

        agent_entry = self.by_agent[agent]
        agent_entry["tool_calls"] += 1
        agent_entry["duration_ms"] += duration
        agent_entry["tokens"] += tokens
        agent_entry["cost_usd"] = round(agent_entry["cost_usd"] + cost, 6)
        if status not in ("ok",):
            agent_entry["errors"] += 1

        self.by_tier[str(tier)][status] += 1
        self.latencies.append(duration)
        self.tokens_total += tokens
        self.cost_total = round(self.cost_total + cost, 6)

    def observe_model(self, record: dict[str, Any]) -> None:
        """Model health heartbeat (04.4). ``record`` comes from the model probe."""
        model = record.get("model", "unknown")
        entry = self.models[model]
        entry["requests"] += int(record.get("requests", 0) or 0)
        entry["errors"] += int(record.get("errors", 0) or 0)
        entry["tokens"] += int(record.get("tokens", 0) or 0)
        entry["latency_ms"] += int(record.get("latency_ms", 0) or 0)

    # -- reporting -------------------------------------------------------
    @staticmethod
    def _percentile(values: list[int], pct: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
        return ordered[index]

    def latency(self) -> dict[str, Any]:
        return {
            "count": len(self.latencies),
            "avg_ms": int(sum(self.latencies) / len(self.latencies)) if self.latencies else 0,
            "p50_ms": self._percentile(self.latencies, 50),
            "p95_ms": self._percentile(self.latencies, 95),
            "max_ms": max(self.latencies) if self.latencies else 0,
        }

    def token_cost(self) -> dict[str, Any]:
        return {
            "tokens_total": self.tokens_total,
            "cost_usd_total": self.cost_total,
            "cost_per_trace_usd": (
                round(self.cost_total / self.traces_total, 6) if self.traces_total else 0.0
            ),
            "tokens_per_trace": (int(self.tokens_total / self.traces_total) if self.traces_total else 0),
        }

    def model_health(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for model, entry in self.models.items():
            requests = entry["requests"]
            out[model] = {
                **entry,
                "avg_latency_ms": int(entry["latency_ms"] / requests) if requests else 0,
                "error_rate": round(entry["errors"] / requests, 4) if requests else 0.0,
                "status": "degraded" if requests and entry["errors"] / requests > 0.1 else "healthy",
            }
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "events_total": self.events_total,
            "traces_total": self.traces_total,
            "transitions_total": self.transitions_total,
            "latency": self.latency(),
            "token_cost": self.token_cost(),
            "by_agent": {k: dict(v) for k, v in sorted(self.by_agent.items())},
            "by_tool": {k: dict(v) for k, v in sorted(self.by_tool.items())},
            "by_tier": {k: dict(v) for k, v in sorted(self.by_tier.items())},
            "models": self.model_health(),
        }


def summarise_alerts(
    events: Iterable[dict[str, Any]],
    traces: Iterable[dict[str, Any]],
    *,
    slow_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Derive alerts from observed data (blueprint 04.7).

    Rules are deliberately simple and deterministic so they can be asserted in
    tests: blocked cards, denials, errors, and slow tool calls.
    """
    alerts: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") == "card.blocked":
            # The reason lives in the event payload; older/simpler emitters may
            # put it at the root, so accept either.
            payload = event.get("payload") or {}
            reason = payload.get("reason") or event.get("reason") or payload.get("note") or event.get("note")
            alerts.append(
                {
                    "severity": "high",
                    "kind": "card_blocked",
                    "card_id": event.get("card_id"),
                    "message": f"Card {event.get('card_id')} is blocked: {reason or 'no reason recorded'}",
                    "ts": event.get("ts"),
                }
            )
    for trace in traces:
        status = trace.get("status")
        if status == "denied":
            alerts.append(
                {
                    "severity": "medium",
                    "kind": "guardrail_denied",
                    "card_id": trace.get("card_id"),
                    "message": f"{trace.get('tool')} was refused by guardrails for {trace.get('agent')}",
                    "ts": trace.get("ts"),
                }
            )
        elif status in ("error", "blocked"):
            alerts.append(
                {
                    "severity": "medium" if status == "error" else "low",
                    "kind": f"tool_{status}",
                    "card_id": trace.get("card_id"),
                    "message": f"{trace.get('tool')} finished with status {status}",
                    "ts": trace.get("ts"),
                }
            )
        duration = int(trace.get("duration_ms", 0) or 0)
        if duration >= slow_ms:
            alerts.append(
                {
                    "severity": "low",
                    "kind": "slow_tool_call",
                    "card_id": trace.get("card_id"),
                    "message": f"{trace.get('tool')} took {duration}ms (threshold {slow_ms}ms)",
                    "ts": trace.get("ts"),
                }
            )
    return alerts
