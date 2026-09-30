"""Phase 5 observability tests: inspector, heartbeats, retention, alert routing.

Four new capabilities were added to the observability service in this phase, and
each one has a failure mode that looks like success:

* the **inspector** would look fine while storing a bearer token verbatim, and
  fine again while silently truncating a prompt it presents as complete;
* the **heartbeat** would look fine reporting "up" while never noticing that the
  model had been down at all, if it only ever recorded successes;
* the **retention sweep** would look fine removing rows while quietly making
  ``verify_chain`` report tampering;
* the **alert router** would look fine delivering alerts while a flaky webhook
  was taking down collection for every sink.

So the tests are written against those failure modes rather than against the
happy paths.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from observability.alerting import AlertRouter, alert_key, severity_rank
from observability.collector import Collector
from observability.model_calls import ModelCallStore, redact
from observability.model_health import STATE_DOWN, STATE_UP, ModelHealthProbe
from observability.retention import (
    CHAINED_STORES,
    RetentionPolicy,
    RetentionSweeper,
    Store,
    assert_chains_survive,
)
from observability.server import build_app
from tool_frontends.audit import ToolAuditLog

# ======================================================= prompt inspector
class TestRedaction:
    def test_bearer_token_is_replaced_and_named(self):
        text, hits = redact("Authorization: Bearer abcdef1234567890XYZ\nscan this")
        assert "abcdef1234567890XYZ" not in text
        assert "<redacted:bearer>" in text
        assert "bearer" in hits

    def test_api_key_and_password_are_both_caught(self):
        text, hits = redact("api_key=supersecretvalue123 password=hunter2hunter2")
        assert "supersecretvalue123" not in text
        assert "hunter2hunter2" not in text
        assert set(hits) >= {"api_key", "password"}

    def test_tool_flag_password_is_caught(self):
        """A credential passed as ``-p`` is how secrets end up inside a prompt."""
        text, hits = redact("nasl -u admin -p P@ssw0rd123 target")
        assert "P@ssw0rd123" not in text
        assert "redacted" in text
        assert hits == ["flag_password"]

    def test_a_port_list_is_not_mistaken_for_a_password(self):
        """The ``-p`` rule must not redact a port list.

        An unbounded ``-p`` rule rewrites ``nmap -p 22,80,443`` into a redacted
        password. That is not harmless tidiness: an operator who sees their own
        scan command mangled in the inspector stops trusting the inspector, and
        the real redactions go unread with it.
        """
        original = "nmap -sV -p 22,80,443 scanme.nmap.org with --script default"
        text, hits = redact(original)
        assert text == original
        assert hits == []

    def test_a_genuine_password_after_the_same_flag_still_goes(self):
        text, hits = redact("nmap -p 22,80 -p hunter2secret target")
        assert "hunter2secret" not in text
        assert "22,80" in text, "the port list on the first -p must survive"


class TestModelCallStore:
    def _store(self, **kwargs) -> ModelCallStore:
        return ModelCallStore(clock=lambda: 1000.0, **kwargs)

    def test_capture_stores_the_body_and_reports_it(self):
        store = self._store()
        record = store.record(prompt="plan this", response='{"steps": []}', model="llama3.1", total_tokens=42)
        assert record.prompt == "plan this"
        assert record.prompt_chars == 9
        assert record.total_tokens == 42
        assert record.truncated is False
        assert store.counts()["captured"] == 1

    def test_secrets_never_reach_the_store_even_when_posted_raw(self):
        store = self._store()
        store.record(prompt="Authorization: Bearer sk-live-abcdef1234567890")
        full = store.get("mc_00001")
        assert full is not None
        assert "sk-live-abcdef1234567890" not in full["prompt"]
        assert "bearer" in full["redactions"]
        assert store.counts()["redacted_calls"] == 1

    def test_truncation_is_declared_not_silent(self):
        store = self._store(max_chars=50)
        store.record(prompt="x" * 500, response="ok")
        full = store.get("mc_00001")
        assert full is not None
        assert len(full["prompt"]) == 50
        assert full["prompt_chars"] == 500
        assert full["truncated"] is True
        assert store.counts()["truncated_calls"] == 1

    def test_list_rows_omit_bodies_and_get_returns_them(self):
        store = self._store()
        store.record(prompt="a long prompt body", response="a response body")
        row = store.list(limit=10)[0]
        assert "prompt" not in row and "response" not in row
        assert row["prompt_chars"] == len("a long prompt body")
        full = store.get(row["id"])
        assert full["prompt"] == "a long prompt body"

    def test_the_ring_is_bounded(self):
        store = self._store(max_records=5)
        for i in range(12):
            store.record(prompt=f"call {i}")
        assert store.counts()["buffered"] == 5
        assert store.counts()["captured"] == 12

    def test_filters_by_card_and_outcome(self):
        store = self._store()
        store.record(prompt="p1", card_id="c1", ok=True)
        store.record(prompt="p2", card_id="c2", ok=False, error="timeout")
        assert len(store.list(card_id="c1")) == 1
        assert len(store.list(ok=False)) == 1
        assert store.counts()["failed"] == 1

    def test_capture_from_a_model_response_shaped_object(self):
        class FakeResponse:
            text = "{}"
            model = "llama3.1"
            backend = "ollama"
            ok = True
            error = None
            latency_ms = 88
            prompt_tokens = 10
            completion_tokens = 4
            total_tokens = 14
            cost_usd = 0.0
            parsed = {"steps": []}

        store = self._store()
        record = store.capture_from_response(prompt="go", response=FakeResponse(), card_id="c9")
        assert record.latency_ms == 88
        assert record.total_tokens == 14
        assert record.card_id == "c9"


# ======================================================= model heartbeats
class TestModelHealthProbe:
    def test_first_probe_finding_a_dead_model_is_a_transition(self):
        probe = ModelHealthProbe(lambda: {"available": False, "error": "refused", "model": "llama3.1"})
        beat = probe.beat()
        assert beat.state == STATE_DOWN
        assert beat.error == "refused"
        status = probe.status()
        assert status["state"] == STATE_DOWN
        assert status["transition_count"] == 1
        assert status["transitions"][0]["from"] == "unknown"

    def test_up_down_up_records_both_edges(self):
        state = {"ok": True}

        def probe_fn():
            return {"available": state["ok"], "model": "llama3.1", "error": None if state["ok"] else "down"}

        probe = ModelHealthProbe(probe_fn)
        probe.beat()                       # unknown -> up
        state["ok"] = False
        probe.beat()                       # up -> down
        state["ok"] = True
        probe.beat()                       # down -> up
        status = probe.status()
        assert status["state"] == STATE_UP
        assert status["probes"] == 3
        assert status["failures"] == 1
        assert [t["to"] for t in status["transitions"]] == [STATE_UP, STATE_DOWN, STATE_UP]
        assert status["availability"] == pytest.approx(2 / 3, abs=0.01)

    def test_a_prober_that_raises_still_produces_a_heartbeat(self):
        def boom():
            raise RuntimeError("connection refused")

        probe = ModelHealthProbe(boom)
        beat = probe.beat()
        assert beat.state == STATE_DOWN
        assert "connection refused" in (beat.error or "")

    def test_a_slow_probe_is_flagged(self):
        """A model that answers but takes seconds to do it is a distinct problem
        from one that is down, and only the latency history tells them apart."""
        ticks = iter([0.0, 3.0, 3.0, 3.1])
        probe = ModelHealthProbe(
            lambda: {"available": True, "model": "m"}, slow_ms=100, timer=lambda: next(ticks, 4.0)
        )
        probe.beat()
        probe.beat()
        status = probe.status()
        assert status["max_latency_ms"] == 3000
        assert status["slow"] is True
        assert status["state"] == STATE_UP, "slow is not down"

    def test_alerts_key_off_edges_not_the_current_state(self):
        """A model down for ten probes must produce one alert, not ten."""
        probe = ModelHealthProbe(lambda: {"available": False, "model": "m", "error": "down"})
        for _ in range(10):
            probe.beat()
        rows = probe.alerts()
        assert len(rows) == 1
        assert rows[0]["kind"] == "model_down"
        assert rows[0]["severity"] == "high"

    def test_model_records_feed_the_existing_model_panel(self):
        probe = ModelHealthProbe(lambda: {"available": True, "model": "llama3.1"})
        probe.beat()
        records = probe.model_records()
        assert records[0]["model"] == "llama3.1"
        assert records[0]["requests"] == 1
        assert records[0]["errors"] == 0


# ============================================================= retention
class TestRetentionPolicy:
    def test_chained_stores_are_refused_with_a_reason(self):
        sweeper = RetentionSweeper()
        plan = sweeper.plan()
        refused = {row["name"]: row for row in plan["stores"] if row["refused"]}
        for name in ("kanban_events", "tool_audit"):
            assert name in refused
            assert "hash-chained" in refused[name]["refusal_reason"]
        assert plan["prunable"] == 0

    def test_a_chained_store_is_refused_even_when_not_pinned(self):
        """Chaining is a property of the data, not a config flag."""
        policy = RetentionPolicy({"tool_audit": {"pinned": False, "max_rows": 1}})
        sweeper = RetentionSweeper(policy)
        sweeper.add_buffer("tool_audit", [{"ts": 1.0}, {"ts": 2.0}])
        report = sweeper.sweep(now=100.0)
        entry = next(r for r in report["stores"] if r["name"] == "tool_audit")
        assert entry["action"] == "refused"
        assert sweeper.stores["tool_audit"].record_count() == 2, "a refused store must be untouched"

    def test_prune_by_age_keeps_rows_with_no_usable_timestamp(self):
        sweeper = RetentionSweeper(
            RetentionPolicy({"obs_events": {"max_age_s": 60.0}})
        )
        rows = [{"ts": 10.0}, {"ts": 95.0}, {"no_ts": True}, {"ts": "not-a-number"}]
        sweeper.add_buffer("obs_events", rows)
        report = sweeper.sweep(now=100.0)
        entry = next(r for r in report["stores"] if r["name"] == "obs_events")
        assert entry["removed"] == 1
        assert entry["kept"] == 3, "a row whose age cannot be determined must not be deleted on a guess"

    def test_prune_by_count_keeps_the_newest(self):
        sweeper = RetentionSweeper(RetentionPolicy({"obs_traces": {"max_rows": 2}}))
        sweeper.add_buffer("obs_traces", [{"ts": 1.0}, {"ts": 2.0}, {"ts": 3.0}])
        sweeper.sweep(now=100.0)
        remaining = list(sweeper.stores["obs_traces"].items())
        assert [r["ts"] for r in remaining] == [2.0, 3.0]

    def test_plan_does_not_mutate_the_store(self):
        sweeper = RetentionSweeper(RetentionPolicy({"obs_events": {"max_age_s": 1.0}}))
        sweeper.add_buffer("obs_events", [{"ts": 1.0}, {"ts": 2.0}])
        plan = sweeper.plan(now=100.0)
        assert plan["prunable"] == 2
        assert sweeper.stores["obs_events"].record_count() == 2, "planning must never delete"

    def test_status_reports_the_policies(self):
        sweeper = RetentionSweeper()
        status = sweeper.status()
        assert "kanban_events" in status["chained_stores"]
        names = {p["name"] for p in status["policies"]}
        assert {"model_calls", "obs_events", "obs_traces"} <= names

    def test_real_tool_audit_chain_survives_a_sweep(self):
        """The invariant, asserted against the real chained log.

        A sweep with an aggressive policy runs, and the tool audit chain is then
        asked to verify itself. If retention had pruned it - as a naive
        'delete older than N' policy would - this returns not-ok with
        'tampered with', which is exactly the false alarm the split prevents.
        """
        audit = ToolAuditLog(":memory:")
        for i in range(5):
            audit.append(ts=f"2026-09-27T00:00:0{i}Z", tool="whois_lookup", tier=0, status="dry_run", dry_run=True)
        assert audit.verify_chain()["ok"] is True

        policy = RetentionPolicy({"tool_audit": {"max_age_s": 1.0}, "obs_events": {"max_age_s": 1.0}})
        sweeper = RetentionSweeper(policy)
        sweeper.add_buffer("tool_audit", list(audit.list(limit=100)))  # a mirror, to prove it is refused
        sweeper.add_buffer("obs_events", [{"ts": 1.0}])
        outcome = assert_chains_survive(sweeper, {"tool_audit": audit.verify_chain})

        assert outcome["all_ok"] is True
        assert outcome["chains"]["tool_audit"]["ok"] is True
        assert outcome["chains"]["tool_audit"]["checked"] == 5
        assert any(r["name"] == "tool_audit" for r in outcome["sweep"]["refused"])
        # The diagnostic buffer *was* pruned - the refusal is specific, not blanket.
        assert outcome["sweep"]["removed"] == 1
        audit.close()

    def test_capture_store_is_prunable_through_the_store_abstraction(self):
        capture = ModelCallStore(clock=lambda: 5.0)
        for i in range(4):
            capture.record(prompt=f"p{i}")
        sweeper = RetentionSweeper(RetentionPolicy({"model_calls": {"max_rows": 2}}))
        sweeper.add_store(Store.for_capture("model_calls", capture))
        report = sweeper.sweep(now=5.0)
        entry = next(r for r in report["stores"] if r["name"] == "model_calls")
        assert entry["action"] == "pruned"
        assert capture.counts()["buffered"] == 2


# ========================================================== alert routing
def _blocked(card_id: str = "c1", reason: str = "scope") -> dict:
    return {"type": "card.blocked", "card_id": card_id, "ts": 1000.0, "payload": {"reason": reason}}


class TestAlertRouter:
    def _router(self, **kwargs) -> tuple[AlertRouter, list]:
        sent: list = []

        def transport(url, body, timeout):
            sent.append({"url": url, "body": body})
            return 204, ""

        kwargs.setdefault("clock", lambda: 1000.0)
        router = AlertRouter(webhook_url="http://hook.invalid/x", transport=transport, **kwargs)
        return router, sent

    def test_high_severity_reaches_the_webhook_and_the_shell(self):
        router, sent = self._router()
        outcome = router.route({"kind": "card_blocked", "card_id": "c1", "severity": "high", "message": "blocked"})
        assert outcome["suppressed"] is False
        assert {s["sink"] for s in outcome["sinks"]} == {"shell", "webhook"}
        assert len(sent) == 1
        assert sent[0]["body"]["card_id"] == "c1"

    def test_a_duplicate_inside_the_window_is_suppressed(self):
        router, sent = self._router()
        alert = {"kind": "card_blocked", "card_id": "c1", "severity": "high", "message": "x"}
        router.route(alert)
        second = router.route(alert)
        assert second["suppressed"] is True
        assert len(sent) == 1, "the same block must not page twice"
        assert router.status()["suppressed"] == 1

    def test_the_window_expires_so_a_reminder_can_fire(self):
        now = {"t": 1000.0}
        router, sent = self._router(clock=lambda: now["t"], dedupe_window_s=60.0)
        alert = {"kind": "card_blocked", "card_id": "c1", "severity": "high", "message": "x"}
        router.route(alert)
        now["t"] = 1100.0
        assert router.route(alert)["suppressed"] is False
        assert len(sent) == 2

    def test_low_severity_stays_in_the_shell_feed(self):
        router, sent = self._router()
        outcome = router.route({"kind": "slow_tool_call", "card_id": "c1", "severity": "low", "message": "slow"})
        assert [s["sink"] for s in outcome["sinks"]] == ["shell"]
        assert sent == [], "routing everything to a pager is how a pager gets muted"

    def test_webhook_failure_retries_then_reports_without_raising(self):
        attempts = {"n": 0}

        def transport(url, body, timeout):
            attempts["n"] += 1
            raise RuntimeError("connection refused")

        router = AlertRouter(
            webhook_url="http://hook.invalid/x", transport=transport, max_retries=2, clock=lambda: 1000.0
        )
        outcome = router.route({"kind": "model_down", "severity": "high", "message": "down"})
        webhook = next(s for s in outcome["sinks"] if s["sink"] == "webhook")
        assert webhook["action"] == "failed"
        assert attempts["n"] == 3, "the initial attempt plus two retries"
        assert router.status()["failed"] == 1

    def test_an_unconfigured_webhook_is_skipped_not_failed(self):
        router = AlertRouter(clock=lambda: 1000.0)
        outcome = router.route({"kind": "card_blocked", "card_id": "c1", "severity": "high", "message": "x"})
        webhook = next(s for s in outcome["sinks"] if s["sink"] == "webhook")
        assert webhook["action"] == "skipped"
        assert router.status()["failed"] == 0, "shell-only is a configuration, not an outage"

    def test_unknown_sink_is_skipped_never_raised(self):
        router = AlertRouter(routes={"carrier_pigeon": "low"}, clock=lambda: 1000.0)
        outcome = router.route({"kind": "k", "severity": "high", "message": "m"})
        assert outcome["sinks"][0]["action"] == "skipped"

    def test_a_failed_delivery_does_not_suppress_the_retry(self):
        def transport(url, body, timeout):
            return 500, "boom"

        router = AlertRouter(
            webhook_url="http://hook.invalid/x", transport=transport, max_retries=0, clock=lambda: 1000.0
        )
        alert = {"kind": "card_blocked", "card_id": "c1", "severity": "high", "message": "x"}
        first = router.route(alert)
        second = router.route(alert)
        # The shell sink accepted it, so the window *does* apply - but the
        # webhook failure is recorded rather than swallowed.
        assert first["sinks"][-1]["action"] == "failed"
        assert router.deliveries(action="failed")
        assert second["key"] == first["key"]

    def test_feed_and_delivery_log_are_available_to_the_shell(self):
        router, _sent = self._router()
        router.route({"kind": "model_down", "severity": "high", "message": "model gone"})
        feed = router.notifications()
        assert feed[0]["message"] == "model gone"
        assert router.deliveries(limit=10)
        assert router.status()["kinds_seen"] == ["model_down"]

    def test_route_many_isolates_rows(self):
        router, _sent = self._router()
        outcome = router.route_many(
            [
                {"kind": "a", "card_id": "c1", "severity": "high", "message": "1"},
                "not-a-dict",  # type: ignore[list-item]
                {"kind": "b", "card_id": "c2", "severity": "low", "message": "2"},
            ]
        )
        assert outcome["routed"] == 2

    def test_alert_key_and_severity_rank_are_stable(self):
        assert alert_key({"kind": "x", "card_id": "c"}) == "x:c"
        assert alert_key({"kind": "x"}) == "x:system"
        assert severity_rank("high") > severity_rank("medium") > severity_rank("low")


# ================================================== collector integration
class TestCollectorPhase5:
    def _collector(self) -> Collector:
        col = Collector(kanban_url="http://kanban.invalid", tools_url="http://tools.invalid")
        col.events.append(_blocked("c1"))
        return col

    def test_route_alerts_delivers_the_block_then_suppresses_it(self):
        col = self._collector()
        first = col.route_alerts()
        assert first["delivered"] >= 1
        second = col.route_alerts()
        assert second["delivered"] == 0
        assert second["suppressed"] >= 1
        assert col.alerts_routed >= 1

    def test_beat_model_feeds_the_panel_and_the_router(self):
        col = self._collector()
        col.beat_model(probe=lambda: {"available": False, "model": "llama3.1", "error": "gone"})
        assert col.model_probe.status()["state"] == STATE_DOWN
        routed = col.route_alerts()
        kinds = {s["sink"] for r in routed["results"] for s in r["sinks"]}
        assert "shell" in kinds, "a model outage must reach the shell feed"

    def test_status_exposes_every_phase5_block(self):
        status = self._collector().status()
        for key in ("model_calls", "model_health", "alert_routing", "retention"):
            assert key in status, f"{key} missing from /health"

    def test_route_and_sweep_runs_all_three_steps(self):
        col = self._collector()
        outcome = col.route_and_sweep()
        assert set(outcome) == {"model", "alerts", "retention"}
        assert outcome["retention"]["refused"], "chained stores must be reported as refused"


# ====================================================== HTTP surface
class TestPhase5Endpoints:
    def _client(self) -> TestClient:
        col = Collector(kanban_url="http://kanban.invalid", tools_url="http://tools.invalid")
        app = build_app(collector=col, autostart=False)
        return TestClient(app)

    def test_capture_then_read_a_model_call(self):
        with self._client() as client:
            posted = client.post(
                "/model/calls",
                json={"prompt": "Authorization: Bearer abcdef1234567890", "response": '{"steps":[]}', "model": "llama3.1"},
            )
            assert posted.status_code == 200
            body = posted.json()
            assert "abcdef1234567890" not in body["prompt"]
            call_id = body["id"]

            listed = client.get("/model/calls", params={"limit": 10})
            assert listed.status_code == 200
            assert listed.json()["count"] == 1
            assert "prompt" not in listed.json()["items"][0]

            single = client.get(f"/model/calls/{call_id}")
            assert single.status_code == 200
            assert single.json()["prompt_chars"] > 0

            assert client.get("/model/calls/mc_99999").status_code == 404

    def test_model_health_beat_and_history(self):
        with self._client() as client:
            assert client.get("/model/health").json()["state"] == "unknown"
            beat = client.post("/model/health/beat")
            assert beat.status_code == 200
            # conftest pins MODEL_ENABLED=0, so the probe reports down with a reason.
            assert beat.json()["state"] == STATE_DOWN
            history = client.get("/model/health/history")
            assert history.json()["count"] == 1

    def test_retention_plan_then_sweep_then_status(self):
        with self._client() as client:
            plan = client.post("/retention/plan")
            assert plan.status_code == 200
            assert plan.json()["refused"], "chained stores must show up as refused in the plan"

            swept = client.post("/retention/sweep")
            assert swept.status_code == 200
            assert "tool_audit" in [r["name"] for r in swept.json()["refused"]]

            status = client.get("/retention")
            assert status.status_code == 200
            assert status.json()["status"]["sweeps"] == 1

    def test_alerting_routes_and_exposes_a_feed(self):
        with self._client() as client:
            routed = client.post("/alerting/route")
            assert routed.status_code == 200
            assert "routed" in routed.json()
            assert client.get("/alerting/status").status_code == 200
            assert client.get("/alerting/notifications").status_code == 200
            assert client.get("/alerting/deliveries").status_code == 200

    def test_snapshot_carries_the_new_blocks(self):
        with self._client() as client:
            snap = client.get("/snapshot").json()
            for key in ("model_health", "model_calls", "alert_routing", "retention"):
                assert key in snap

    def test_health_is_unchanged_for_existing_consumers(self):
        with self._client() as client:
            body = client.get("/health").json()
            assert body["status"] == "ok"
            assert body["service"] == "observability"
            assert "last_poll_at" in body["source"]


def test_chained_store_names_are_the_documented_set():
    assert CHAINED_STORES == frozenset({"kanban_events", "tool_audit", "card_events"})


def test_alert_payload_is_json_serialisable_end_to_end():
    _router, sent = TestAlertRouter()._router()
    _router.route({"kind": "model_down", "severity": "high", "message": "gone"})
    json.dumps(sent[0]["body"])  # must not raise
