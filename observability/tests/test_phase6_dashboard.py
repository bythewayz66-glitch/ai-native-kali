"""Phase 6, item 2: the dashboard must actually *wire* the Phase 5 endpoints.

Phase 5 shipped the collector, model-call inspector, model health, alert router
and retention modules with a working HTTP surface - but the dashboard page only
rendered the pre-Phase-5 panels. An operator could not see any of it without
reading the API by hand.

The failure this guards against is a panel that *looks* present and is wired to
nothing: markup without a fetch, or a fetch of an endpoint that does not exist.
So every assertion here is either "the page references this route" or "this
route answers", which together mean the panel can only be broken at runtime.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from observability.server import build_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(build_app(autostart=False))


#: Routes the inspector / routing / retention panels actually call.
#:
#: ``/alerting/deliveries`` is deliberately absent: the panel reads the delivery
#: *counters* from ``/alerting/status`` and the shell-feed rows from
#: ``/alerting/notifications``. The full delivery log exists and is asserted to
#: answer, but a dashboard that pulled it on every poll would ship the whole log
#: to the browser for two numbers it already has.
PHASE5_ROUTES = [
    "/model/calls",
    "/model/health/history",
    "/alerting/status",
    "/alerting/notifications",
    "/retention",
    "/snapshot",
]


class TestDashboardServes:
    def test_dashboard_route_answers(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


class TestDashboardWiresEveryPhase5Endpoint:
    """The page must *call* each endpoint its panels claim to show."""

    @pytest.mark.parametrize("route", PHASE5_ROUTES)
    def test_page_references_the_route(self, client, route):
        html = client.get("/dashboard").text
        assert route in html, f"the dashboard never calls {route}"

    def test_every_referenced_route_exists(self, client):
        """No panel may poll a route the server does not serve."""
        html = client.get("/dashboard").text
        referenced = {
            "/model/calls",
            "/model/health/history",
            "/alerting/status",
            "/alerting/notifications",
            "/retention",
        }
        for route in referenced:
            assert route in html
            assert client.get(route).status_code == 200, route


class TestInspectorPanel:
    def test_capture_then_list_then_open(self, client):
        """The panel's own read path: capture is separate from display."""
        posted = client.post(
            "/model/calls",
            json={
                "prompt": "scan 10.0.0.5 with aws_secret_key=AKIAIOSFODNN7EXAMPLE",
                "response": "ok",
                "model": "llama3.1",
                "backend": "ollama",
                "card_id": "crd_1",
                "crew": "recon",
                "role": "recon-specialist",
            },
        )
        assert posted.status_code == 200
        call_id = posted.json()["id"]

        listed = client.get("/model/calls", params={"limit": 10}).json()
        assert listed["counts"]["captured"] >= 1
        # A list row must not carry the bodies - that is the whole point of the
        # two-route design, and the panel relies on it to poll cheaply.
        row = next(r for r in listed["items"] if r["id"] == call_id)
        assert "prompt" not in row
        assert row["prompt_chars"] > 0
        assert row["redactions"], "the fake AWS key must be redacted before storage"

        detail = client.get(f"/model/calls/{call_id}").json()
        assert "prompt" in detail and "response" in detail
        assert "AKIAIOSFODNN7EXAMPLE" not in detail["prompt"], "redaction must survive to the detail view"

    def test_redacted_only_filter_is_honoured(self, client):
        client.post("/model/calls", json={"prompt": "nothing secret here", "response": "ok"})
        client.post("/model/calls", json={"prompt": "token=ghp_012345678901234567890123456789012345", "response": "ok"})
        body = client.get("/model/calls", params={"redacted_only": True}).json()
        assert body["items"], "a redacted call must be findable by the filter"
        assert all(r["redactions"] for r in body["items"])

    def test_counts_expose_redaction_kinds_for_the_header(self, client):
        body = client.get("/model/calls").json()
        assert "redaction_kinds" in body["counts"]
        assert "truncated_calls" in body["counts"]


class TestRoutingAndRetentionPanels:
    def test_alerting_status_has_the_counters_the_panel_renders(self, client):
        body = client.get("/alerting/status").json()
        for key in ("received", "delivered", "suppressed", "failed"):
            assert key in body, f"the routing panel renders '{key}'"

    def test_notifications_envelope_carries_items(self, client):
        body = client.get("/alerting/notifications").json()
        assert "items" in body and "count" in body

    def test_retention_status_carries_the_panel_fields(self, client):
        body = client.get("/retention").json()
        assert "status" in body and "plan" in body
