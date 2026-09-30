"""Phase 6, item 3: the overlay renders routed alerts, not only board state.

The Phase 5 alert router (observability item F4) dedupes per ``(kind, card)``,
applies a severity floor and records every delivery attempt. Phase 6's job is to
put the *delivered* alerts on the surface an operator is actually looking at
while working the board, instead of only inside the observability page.

The design decision these tests pin: the shell does **not** re-derive alerts.
It asks the router what it delivered. A second implementation of dedupe and
severity-floor policy is exactly the drift this repository keeps designing
against, so the only transformation here is the severity *vocabulary* mapping.
"""
from __future__ import annotations

from hermes_shell.overlay import OverlayClient, _route_severity, collect, compose, health_row


class TestSeverityMapping:
    def test_critical_and_high_colour_the_overlay_red(self):
        assert _route_severity("critical") == "alert"
        assert _route_severity("high") == "alert"

    def test_medium_is_amber(self):
        assert _route_severity("medium") == "warn"

    def test_low_and_info_are_visible_but_not_alarming(self):
        """The router already floored ``info``; what arrives is worth showing."""
        assert _route_severity("low") == "ok"
        assert _route_severity("info") == "ok"

    def test_an_unrecognised_severity_is_a_reason_to_look(self):
        """Fail closed: an unknown value must not read as 'all clear'."""
        assert _route_severity("banana") == "warn"
        assert _route_severity(None) == "warn"
        assert _route_severity("") == "warn"

    def test_matching_is_case_insensitive(self):
        assert _route_severity("HIGH") == "alert"


class TestComposeRendersRoutedAlerts:
    def _sources(self, notes):
        return {
            "totals": {"cards": 2, "running": 1, "blocked": 0, "pending_approvals": 0},
            "health": [health_row("kanban-core", "http://x", True)],
            "alert_notifications": notes,
        }

    def test_a_high_routed_alert_escalates_the_overlay(self):
        model = compose(self._sources([{"kind": "model_down", "severity": "high", "message": "model gone"}]))
        assert model["state"] == "alert"
        assert any(a["kind"] == "routed:model_down" for a in model["alerts"])
        assert any("model gone" in a["text"] for a in model["alerts"])

    def test_a_medium_alert_goes_amber_without_a_red_herring(self):
        model = compose(self._sources([{"kind": "card_blocked", "severity": "medium", "message": "x"}]))
        assert model["state"] == "warn"

    def test_an_info_alert_does_not_escalate(self):
        model = compose(self._sources([{"kind": "note", "severity": "info", "message": "fyi"}]))
        assert model["state"] == "ok"

    def test_routed_alerts_are_passed_through_and_counted(self):
        model = compose(self._sources([{"kind": "a", "severity": "high", "message": "m1"}]))
        assert model["routed_count"] == 1
        assert model["alert_notifications"][0]["kind"] == "a"

    def test_a_body_without_a_message_does_not_crash(self):
        model = compose(self._sources([{"kind": "bare", "severity": "high"}]))
        assert any(a["kind"] == "routed:bare" for a in model["alerts"])

    def test_absent_key_is_not_an_error(self):
        """A shell bound to the board alone must behave exactly as before."""
        model = compose({"totals": {"cards": 1}, "health": [health_row("k", "http://x", True)]})
        assert model["routed_count"] == 0
        assert model["alert_notifications"] == []


class _Board:
    """A board client with (or without) an observability endpoint."""

    def __init__(self, *, obs_url=None, notifications=()):
        self.obs_url = obs_url
        self._notifications = list(notifications)
        self.paths: list[str] = []
        self.services = [health_row("kanban-core", "http://x", True)]

    def overview(self):
        return {"totals": {"cards": 1}, "boards": [{}]}

    def pending_approvals(self):
        return []

    def recent_events(self, limit=1):
        return []

    def notifications(self, limit=20):
        return self._notifications


class TestCollectWiring:
    def test_without_an_obs_url_the_overlay_is_board_only(self):
        """No new endpoint is touched when the shell has no observability bound."""
        client = _Board(obs_url=None)
        sources = collect(client)
        assert "alert_notifications" not in sources

    def test_with_an_obs_url_routed_alerts_are_collected(self):
        client = _Board(obs_url="http://obs", notifications=[{"kind": "k", "severity": "high", "message": "m"}])
        sources = collect(client)
        assert sources["alert_notifications"][0]["kind"] == "k"
        model = compose(sources)
        assert model["state"] == "alert"

    def test_a_dead_router_does_not_double_count_the_outage(self):
        """Reachability is the probe's job; the router read must stay quiet."""
        class Dead(_Board):
            def notifications(self, limit=20):
                raise RuntimeError("connection refused")

        sources = collect(Dead(obs_url="http://obs"))
        assert sources["alert_notifications"] == []
        assert sources["errors"] == [], "an unreachable router must not be logged twice"


class TestOverlayClientConfig:
    def test_obs_url_is_normalised(self):
        assert OverlayClient("http://k/", obs_url="http://obs/").obs_url == "http://obs"

    def test_no_obs_url_stays_none(self):
        assert OverlayClient("http://k").obs_url is None

    def test_notifications_accepts_the_router_envelope(self, monkeypatch):
        client = OverlayClient("http://k", obs_url="http://obs")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"items": [{"kind": "k"}]}'

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse())
        assert client.notifications(limit=5) == [{"kind": "k"}]
