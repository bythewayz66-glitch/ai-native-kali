"""Shell server routes for the Phase 7 surfaces (items 1, 2, 6).

These test the *route* contract, not the modules underneath - the modules have
their own suites. What matters here is the wiring, because that is where the
interesting failures live:

* an unclassified drop must be refused with a 4xx rather than minting a card;
* an unknown launcher id must 404 rather than silently opening nothing;
* a refused launch must be a 403, not a 500;
* an unknown window *action* must 400 while an unknown window *id* must 404 -
  a stale click is normal, a typo in the JS is not, and collapsing the two makes
  a shell bug look like a race.
"""
from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    from hermes_shell.server import build_app

    return TestClient(build_app())


class TestHealthAndConfig:
    def test_health_lists_the_new_surfaces(self, client):
        surfaces = client.get("/health").json()["surfaces"]
        assert {"app_launcher", "target_drop", "setup_wizard"} <= set(surfaces)

    def test_the_older_surfaces_are_still_advertised(self, client):
        """Item 1-2 must not drop Phase 4-6 surfaces from the manifest."""
        surfaces = client.get("/health").json()["surfaces"]
        assert {"kanban_board", "window_manager", "overlay_widget"} <= set(surfaces)


class TestLauncherRoute:
    def test_the_registry_is_listed(self, client):
        payload = client.get("/api/launcher").json()
        # counts is keyed by *kind*, not a synthetic "total" - so sum it.
        assert sum(payload["counts"].values()) > 0
        assert payload["entries"]

    def test_the_hermes_session_is_present(self, client):
        ids = {e["id"] for e in client.get("/api/launcher").json()["entries"]}
        assert "org.kali.hermes.session" in ids

    def test_a_query_filters(self, client):
        """The search is *ranked*, so every hit must match somewhere: name, id,
        keywords or categories. Matching on keywords matters - a user typing
        "scan" should find the tool whose keywords say so even if its name never
        contains the word."""
        payload = client.get("/api/launcher", params={"q": "terminal"}).json()
        assert payload["query"] == "terminal"
        for entry in payload["entries"]:
            haystack = " ".join(
                [entry["name"], entry["id"], *entry.get("keywords", []), *entry.get("categories", [])]
            ).lower()
            assert "terminal" in haystack

    def test_a_kind_filter_narrows(self, client):
        payload = client.get("/api/launcher", params={"kind": "tool"}).json()
        assert all(e["kind"] == "tool" for e in payload["entries"])

    def test_launching_opens_a_managed_window(self, client):
        before = client.get("/api/windows").json()["count"]
        response = client.post("/api/launcher/launch", json={"id": "org.kali.hermes.session"})
        assert response.status_code == 200
        body = response.json()
        assert body["window_id"]
        assert body["reused"] is False
        after = client.get("/api/windows").json()
        assert after["count"] == before + 1
        # Managed, not merely created: focused and on the taskbar.
        assert after["focused"] == body["window_id"]
        assert any(w["id"] == body["window_id"] for w in after["taskbar"])

    def test_launching_an_open_app_focuses_rather_than_duplicates(self, client):
        """Single-instance behaviour: two taskbar buttons for one app is the bug
        users report as "it opened twice"."""
        first = client.post("/api/launcher/launch", json={"id": "org.kali.hermes.session"}).json()
        second = client.post("/api/launcher/launch", json={"id": "org.kali.hermes.session"}).json()
        assert second["reused"] is True
        assert second["window_id"] == first["window_id"]

    def test_an_unknown_entry_is_a_404(self, client):
        response = client.post("/api/launcher/launch", json={"id": "does.not.exist"})
        assert response.status_code == 404

    def test_a_missing_id_is_a_400(self, client):
        assert client.post("/api/launcher/launch", json={}).status_code == 400


class TestTargetDropRoute:
    @pytest.mark.parametrize(
        "target,kind",
        [
            ("10.10.0.5", "ipv4"),
            ("10.10.0.0/24", "cidr"),
            ("shop.example.net", "hostname"),
            ("https://shop.example.net/login", "url"),
            ("aa:bb:cc:dd:ee:ff", "mac"),
        ],
    )
    def test_each_address_shape_is_accepted_and_scoped(self, client, target, kind):
        response = client.post("/api/target-drop", json={"target": target, "board_id": "brd_agent"})
        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == kind
        # A single-host target lands in `targets`; a CIDR lands in `cidrs`. Either
        # is a real scope - an empty pair would be the failure.
        assert body["scope"]["targets"] or body["scope"]["cidrs"]

    def test_a_non_target_payload_is_refused(self, client):
        """The gate that matters most: an unclassified string must not mint a
        scoped card, because a scope built from nothing passes everything."""
        response = client.post("/api/target-drop", json={"target": "just some words"})
        assert response.status_code == 422
        assert "card" not in response.json()

    def test_a_file_path_is_refused(self, client):
        response = client.post("/api/target-drop", json={"target": "/etc/passwd"})
        assert response.status_code == 422

    def test_an_empty_payload_is_refused(self, client):
        assert client.post("/api/target-drop", json={}).status_code == 422

    def test_the_drop_is_recorded_in_the_audit_chain(self, client, tmp_path):
        client.post("/api/target-drop", json={"target": "10.10.0.5", "board_id": "brd_agent"})
        log = tmp_path / "audit.jsonl"
        assert log.exists() and log.stat().st_size > 0

    def test_a_refused_drop_is_also_recorded(self, client, tmp_path):
        """An audit that records only successes cannot explain the refusal a user
        will ask about - so the refusal is written too.

        Read back through the log's own API rather than the file bytes: the log is
        a chained SQLite store, so 'does the file contain the string' is not a
        question it can answer.
        """
        client.post("/api/target-drop", json={"target": "nope"})
        from tool_frontends.audit import ToolAuditLog

        rows = ToolAuditLog(tmp_path / "audit.jsonl").list(tool="target_drop")
        assert len(rows) == 1 and rows[0]["status"] == "denied"

    def test_the_response_carries_the_scope_for_the_caller(self, client):
        body = client.post(
            "/api/target-drop", json={"target": "10.10.0.0/24", "board_id": "brd_agent"}
        ).json()
        assert body["scope"]["cidrs"] == ["10.10.0.0/24"]


class TestSetupRoute:
    def test_the_step_model_is_served(self, client):
        payload = client.get("/api/setup").json()
        assert payload["steps"]
        assert payload["steps"][0]["key"] == "engagement"

    def test_steps_carry_what_the_shell_needs_to_render(self, client):
        for step in client.get("/api/setup").json()["steps"]:
            assert {"key", "title", "prompt", "requires", "help"} <= set(step)

    def test_a_fresh_wizard_offers_only_the_engagement(self, client):
        assert client.get("/api/setup").json()["state"]["pending"] == ["engagement"]

    def test_readiness_is_reported(self, client):
        readiness = client.get("/api/setup").json()["state"]["readiness"]
        assert "model_reachable" in readiness and isinstance(readiness["notes"], list)


class TestWindowRouteStillBehaves:
    def test_an_unknown_action_is_a_400(self, client):
        assert client.post("/api/windows", json={"action": "teleport"}).status_code == 400

    def test_an_unknown_window_id_is_a_404(self, client):
        response = client.post("/api/windows", json={"action": "focus", "id": "win_9999"})
        assert response.status_code == 404

    def test_a_missing_action_is_a_400(self, client):
        assert client.post("/api/windows", json={}).status_code == 400

    def test_the_depth_actions_are_reachable(self, client):
        """Item 6's cycle/snap/show-desktop must be dispatchable, not just
        implemented - an action the route rejects is an action the shell cannot
        send."""
        ids = [client.post("/api/windows", json={"action": "open", "app": "files", "title": "A"}).json()]
        first = ids[0]["windows"][-1]["id"]
        client.post("/api/windows", json={"action": "open", "app": "terminal", "title": "B"})
        assert client.post("/api/windows", json={"action": "cycle"}).status_code == 200
        assert client.post("/api/windows", json={"action": "snap", "id": first, "edge": "left"}).status_code == 200
        assert client.post("/api/windows", json={"action": "minimize_all"}).status_code == 200
        assert client.post("/api/windows", json={"action": "restore_all"}).status_code == 200
