"""Hermes shell server tests: endpoints and the panel contract."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_shell.server import build_app


@pytest.fixture()
def client():
    with TestClient(build_app()) as c:
        yield c


class TestShellServer:
    def test_health_lists_surfaces(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["service"] == "hermes-shell"
        assert "kanban_board" in body["surfaces"]
        assert "taskbar_widget" in body["surfaces"]
        assert "notification_feed" in body["surfaces"]

    def test_panel_served_at_root_and_panel(self, client):
        for path in ("/", "/panel"):
            r = client.get(path)
            assert r.status_code == 200
            assert "Hermes Shell" in r.text

    def test_panel_html_has_the_shell_affordances(self, client):
        html = client.get("/panel").text
        # the four integration surfaces the blueprint asks for
        assert 'id="board"' in html
        assert 'id="tabs"' in html
        assert 'class="taskbar"' in html
        assert 'id="feed"' in html
        assert 'id="drawer"' in html
        # taskbar counters
        for marker in ('s-cards', 's-running', 's-blocked', 's-gates', 's-traces'):
            assert marker in html

    def test_panel_columns_match_the_lifecycle(self, client):
        html = client.get("/panel").text
        assert "const COLS" in html
        for col in ("Backlog", "Assigned", "Running", "Review", "Blocked", "Done"):
            assert f"'{col}'" in html

    def test_panel_is_a_thin_client(self, client):
        """The shell must not embed board data - everything comes over HTTP."""
        html = client.get("/panel").text
        assert "/api/overview" in html
        assert "/api/cards" in html
        assert "/api/events" in html
        assert "/api/approvals/pending" in html
        assert "/replay" in html

    def test_panel_supports_gate_decisions(self, client):
        html = client.get("/panel").text
        assert "approvals/" in html and "/decide" in html
        assert "data-approve" in html and "data-reject" in html

    def test_config_exposes_service_endpoints(self, client):
        body = client.get("/config").json()
        assert set(body) >= {"kanban_url", "runtime_url", "tools_url", "observability_url", "poll_ms"}
        assert isinstance(body["poll_ms"], int)

    def test_desktop_entry_is_readable(self, client):
        body = client.get("/desktop-entry").json()
        # present once packaging lands; must never 500 either way
        assert "found" in body
        if body["found"]:
            assert "[Desktop Entry]" in body["body"]

    def test_panel_escapes_untrusted_values(self, client):
        """Card titles come from users and agents, so the shell must escape them."""
        html = client.get("/panel").text
        assert "function esc(" in html
        assert "innerHTML = d.cards" not in html  # no raw injection of card data
