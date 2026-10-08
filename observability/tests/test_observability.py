"""Observability tests: metrics projection, alerts, replay, HTTP panels."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from observability.collector import Collector
from observability.metrics import Metrics, summarise_alerts
from observability.server import build_app


# ---------------------------------------------------------------- metrics
class TestMetrics:
    def test_empty_metrics_report_zeros_not_fiction(self):
        snap = Metrics().snapshot()
        assert snap["traces_total"] == 0
        assert snap["latency"]["p95_ms"] == 0
        assert snap["token_cost"]["tokens_total"] == 0

    def test_traces_aggregate_by_tool_and_agent(self):
        m = Metrics()
        m.observe_trace({"tool": "nmap_scan", "agent": "recon-specialist", "tier": 1, "duration_ms": 100, "status": "ok"})
        m.observe_trace({"tool": "nmap_scan", "agent": "recon-specialist", "tier": 1, "duration_ms": 300, "status": "ok"})
        snap = m.snapshot()
        assert snap["by_tool"]["nmap_scan"]["calls"] == 2
        assert snap["by_tool"]["nmap_scan"]["duration_ms"] == 400
        assert snap["by_agent"]["recon-specialist"]["tool_calls"] == 2
        assert snap["latency"]["avg_ms"] == 200

    def test_latency_percentiles(self):
        m = Metrics()
        for value in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            m.observe_trace({"tool": "t", "duration_ms": value, "status": "ok"})
        lat = m.latency()
        # nearest-rank: index = round(pct/100 * (n-1)) over the sorted values
        assert lat["p50_ms"] == 50   # index 4 of [10..100]
        assert lat["p95_ms"] == 100  # index 9
        assert lat["max_ms"] == 100
        assert lat["avg_ms"] == 55

    def test_tokens_and_cost_accumulate(self):
        m = Metrics()
        m.observe_trace({"tool": "t", "tokens": 100, "cost_usd": 0.002, "status": "ok"})
        m.observe_trace({"tool": "t", "tokens": 50, "cost_usd": 0.001, "status": "ok"})
        tc = m.token_cost()
        assert tc["tokens_total"] == 150
        assert tc["cost_usd_total"] == pytest.approx(0.003)
        assert tc["tokens_per_trace"] == 75

    def test_tier_breakdown_counts_statuses(self):
        m = Metrics()
        m.observe_trace({"tool": "a", "tier": 2, "status": "denied"})
        m.observe_trace({"tool": "b", "tier": 2, "status": "ok"})
        assert m.snapshot()["by_tier"]["2"]["denied"] == 1
        assert m.snapshot()["by_tier"]["2"]["ok"] == 1

    def test_denied_and_error_classified_separately(self):
        m = Metrics()
        m.observe_trace({"tool": "x", "status": "denied"})
        m.observe_trace({"tool": "y", "status": "error"})
        snap = m.snapshot()
        assert snap["by_tool"]["x"]["denied"] == 1
        assert snap["by_tool"]["y"]["errors"] == 1

    def test_dry_run_vs_live_counted(self):
        m = Metrics()
        m.observe_trace({"tool": "t", "status": "ok", "dry_run": True})
        m.observe_trace({"tool": "t", "status": "ok", "dry_run": False})
        assert m.snapshot()["by_tool"]["t"]["dry_runs"] == 1
        assert m.snapshot()["by_tool"]["t"]["live"] == 1

    def test_model_health_error_rate(self):
        m = Metrics()
        m.observe_model({"model": "llama3.1", "requests": 10, "errors": 2, "latency_ms": 1000, "tokens": 500})
        health = m.model_health()["llama3.1"]
        assert health["error_rate"] == 0.2
        assert health["status"] == "degraded"
        assert health["avg_latency_ms"] == 100

    def test_model_healthy_below_threshold(self):
        m = Metrics()
        m.observe_model({"model": "m", "requests": 100, "errors": 1, "latency_ms": 100})
        assert m.model_health()["m"]["status"] == "healthy"

    def test_card_events_counted(self):
        m = Metrics()
        m.observe_event({"type": "card.moved", "actor": "bridge"})
        m.observe_event({"type": "card.created", "actor": "operator"})
        snap = m.snapshot()
        assert snap["events_total"] == 2
        assert snap["transitions_total"] == 1
        assert snap["by_agent"]["bridge"]["events"] == 1


# ----------------------------------------------------------------- alerts
class TestAlerts:
    def test_blocked_card_raises_high_severity(self):
        alerts = summarise_alerts(
            [{"type": "card.blocked", "card_id": "c1", "reason": "out of scope", "ts": "t"}], []
        )
        assert alerts[0]["severity"] == "high"
        assert alerts[0]["kind"] == "card_blocked"
        assert "out of scope" in alerts[0]["message"]

    def test_denied_trace_raises_medium(self):
        alerts = summarise_alerts([], [{"tool": "nikto_scan", "status": "denied", "agent": "web-specialist"}])
        assert alerts[0]["kind"] == "guardrail_denied"
        assert alerts[0]["severity"] == "medium"

    def test_slow_call_raises_low(self):
        alerts = summarise_alerts([], [{"tool": "nmap_scan", "status": "ok", "duration_ms": 9000}], slow_ms=5000)
        assert alerts[0]["kind"] == "slow_tool_call"
        assert alerts[0]["severity"] == "low"

    def test_healthy_run_raises_nothing(self):
        assert summarise_alerts([{"type": "card.moved"}], [{"tool": "dns_lookup", "status": "ok", "duration_ms": 10}]) == []


# -------------------------------------------------------------- collector
class TestCollector:
    def test_replay_merges_three_streams_in_order(self):
        col = Collector()
        col.events.append({"event_id": "e1", "ts": "2026-01-01T00:00:02", "type": "card.moved", "card_id": "c1"})
        col.traces.append({"trace_id": "t1", "ts": "2026-01-01T00:00:01", "tool": "dns_lookup", "card_id": "c1", "status": "ok"})
        col.audit.append({"seq": 1, "ts": "2026-01-01T00:00:03", "tool": "dns_lookup", "card_id": "c1"})
        replay = col.card_replay("c1")
        assert replay["count"] == 3
        assert [s["kind"] for s in replay["steps"]] == ["trace", "event", "audit"]

    def test_replay_ignores_other_cards(self):
        col = Collector()
        col.events.append({"event_id": "e1", "ts": "t", "card_id": "c1", "type": "x"})
        col.events.append({"event_id": "e2", "ts": "t", "card_id": "c2", "type": "x"})
        assert col.card_replay("c1")["count"] == 1

    def test_timeline_filters_by_agent_and_card(self):
        col = Collector()
        col.events.append({"event_id": "1", "ts": "a", "actor": "bridge", "card_id": "c1", "type": "x"})
        col.events.append({"event_id": "2", "ts": "b", "actor": "operator", "card_id": "c1", "type": "x"})
        assert len(col.timeline(agent="bridge")) == 1
        assert len(col.timeline(card_id="c1")) == 2

    def test_timeline_is_newest_first(self):
        col = Collector()
        col.events.append({"event_id": "1", "ts": "a", "type": "x"})
        col.events.append({"event_id": "2", "ts": "b", "type": "x"})
        assert col.timeline()[0]["ts"] == "b"

    def test_trace_filters(self):
        col = Collector()
        col.traces.append({"trace_id": "1", "tool": "a", "status": "ok", "ts": "t"})
        col.traces.append({"trace_id": "2", "tool": "b", "status": "denied", "ts": "t"})
        assert len(col.tool_call_traces(tool="a")) == 1
        assert len(col.tool_call_traces(status="denied")) == 1

    def test_ingest_is_idempotent(self):
        col = Collector()
        col._fetch = lambda url, params=None: {
            "events": [{"event_id": "e1", "ts": "t", "type": "card.moved", "actor": "bridge"}]
        }
        first = col.ingest_kanban_events()
        second = col.ingest_kanban_events()
        assert first == 1 and second == 0
        assert col.metrics.snapshot()["events_total"] == 1

    def test_ingest_error_is_recorded_not_raised(self):
        col = Collector()

        def boom(url, params=None):
            raise RuntimeError("connection refused")

        col._fetch = boom
        assert col.ingest_kanban_events() == 0
        assert col.errors and "connection refused" in col.errors[0]

    def test_audit_cursor_advances(self, tmp_path):
        # The audit cursor is persisted to disk (Phase 16: restart continuity),
        # so a Collector with the default path resumes the previous run's cursor
        # and the assertion here would depend on execution order. Isolating the
        # cursor file is what makes this a test of the *ingest* logic rather than
        # a test of whatever the last run left behind.
        col = Collector(cursor_path=str(tmp_path / "cursors.json"))
        col._fetch = lambda url, params=None: {
            "entries": [
                {"seq": 1, "ts": "t", "tool": "a"},
                {"seq": 7, "ts": "t", "tool": "b"},
            ]
        }
        added, cursor = col.ingest_tool_audit()
        assert added == 2 and cursor == 7


# ----------------------------------------------------------------- server
class TestObservabilityServer:
    @pytest.fixture()
    def client(self):
        col = Collector()
        col.events.append(
            {"event_id": "e1", "ts": "2026-01-01T00:00:01", "type": "card.moved", "actor": "bridge", "card_id": "c1", "from_column": "Assigned", "to_column": "Running"}
        )
        col.events.append(
            {"event_id": "e2", "ts": "2026-01-01T00:00:03", "type": "card.blocked", "actor": "bridge", "card_id": "c2", "reason": "scope"}
        )
        col.traces.append(
            {"trace_id": "t1", "ts": "2026-01-01T00:00:02", "tool": "nmap_scan", "agent": "recon-specialist", "tier": 1, "status": "ok", "duration_ms": 120, "dry_run": True, "card_id": "c1"}
        )
        col.audit.append({"seq": 1, "ts": "2026-01-01T00:00:02", "tool": "nmap_scan", "card_id": "c1", "audit_hash": "abc"})
        col.metrics.observe_event(col.events[0])
        col.metrics.observe_event(col.events[1])
        col.metrics.observe_trace(col.traces[0])
        app = build_app(collector=col, autostart=False)
        with TestClient(app) as c:
            yield c

    def test_health(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["service"] == "observability"
        assert body["source"]["buffered"]["events"] == 2

    def test_timeline_panel(self, client):
        body = client.get("/panels/timeline").json()
        assert body["panel"] == "agent_activity_timeline"
        assert body["count"] == 2
        assert body["items"][0]["ts"] > body["items"][1]["ts"]  # newest first

    def test_tokens_panel(self, client):
        body = client.get("/panels/tokens").json()
        assert body["panel"] == "token_cost_latency"
        assert body["latency"]["count"] == 1
        assert body["by_agent"]["recon-specialist"]["tool_calls"] == 1

    def test_traces_panel_and_filter(self, client):
        assert client.get("/panels/traces").json()["count"] == 1
        assert client.get("/panels/traces", params={"tool": "nmap_scan"}).json()["count"] == 1
        assert client.get("/panels/traces", params={"tool": "other"}).json()["count"] == 0

    def test_audit_panel(self, client):
        body = client.get("/panels/audit").json()
        assert body["count"] == 1
        assert body["items"][0]["audit_hash"] == "abc"

    def test_alerts_panel_surfaces_blocked_card(self, client):
        body = client.get("/panels/alerts").json()
        assert body["count"] == 1
        assert body["items"][0]["kind"] == "card_blocked"

    def test_models_panel(self, client):
        assert client.get("/panels/models").json()["models"] == {}

    def test_replay_endpoint(self, client):
        body = client.get("/cards/c1/replay").json()
        assert body["count"] == 3
        assert body["traces"] == 1 and body["audit_rows"] == 1 and body["events"] == 1

    def test_snapshot_bundles_all_panels(self, client):
        body = client.get("/snapshot").json()
        assert set(body) >= {"status", "timeline", "traces", "audit", "alerts", "tokens"}

    def test_dashboard_page_is_served(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "AI-NATIVE KALI" in response.text.upper()
    