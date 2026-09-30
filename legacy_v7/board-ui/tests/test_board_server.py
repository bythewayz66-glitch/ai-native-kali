"""Contract tests for the board-ui service.

The page's *logic* is covered by `tests/test_board_logic.mjs` under Node; these
tests cover the Python service: it must serve the page and static logic, expose
the connection config the page boots from, and fail loudly rather than serving a
blank shell when its assets are missing.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from board_ui.server import COLUMNS, build_app

STATIC = Path(__file__).resolve().parents[1] / "board_ui" / "static"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(build_app(kanban_url="http://127.0.0.1:9999/"))


class TestHealth:
    def test_health_reports_ok_when_assets_are_present(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["component"] == "board-ui"
        assert body["index_present"] is True
        assert body["logic_present"] is True

    def test_health_exposes_the_lifecycle_columns(self, client):
        assert client.get("/health").json()["columns"] == COLUMNS

    def test_health_degrades_when_assets_are_missing(self, tmp_path):
        app = build_app(static_dir=tmp_path, kanban_url="http://x")
        body = TestClient(app).get("/health").json()
        assert body["status"] == "degraded"
        assert body["index_present"] is False


class TestConfig:
    def test_config_points_at_kanban_core(self, client):
        body = client.get("/config").json()
        assert body["kanban_url"] == "http://127.0.0.1:9999", "trailing slash must be stripped"
        assert body["ws_url"] == "ws://127.0.0.1:9999/ws/events"

    def test_config_derives_ws_from_https(self):
        app = build_app(kanban_url="https://board.example.net")
        body = TestClient(app).get("/config").json()
        assert body["ws_url"] == "wss://board.example.net/ws/events"

    def test_config_honours_an_explicit_ws_url(self):
        app = build_app(kanban_url="http://a", ws_url="ws://custom/events")
        assert TestClient(app).get("/config").json()["ws_url"] == "ws://custom/events"

    def test_config_exposes_timing_knobs(self, client):
        body = client.get("/config").json()
        assert body["poll_interval_ms"] > 0
        assert body["reconnect_base_ms"] > 0

    def test_missing_static_dir_returns_a_500_not_a_blank_page(self, tmp_path):
        app = build_app(static_dir=tmp_path)
        response = TestClient(app).get("/")
        assert response.status_code == 500
        assert "missing" in response.json()["error"]


class TestStaticServing:
    def test_index_is_served_and_references_the_logic_module(self, client):
        response = client.get("/")
        assert response.status_code == 200
        html = response.text
        assert "Hermes Kanban" in html
        assert "board_logic.mjs" in html, "the page must load its tested logic module"

    def test_logic_module_is_served_as_javascript(self, client):
        response = client.get("/static/board_logic.mjs")
        assert response.status_code == 200
        assert "export function canDrop" in response.text

    def test_the_page_calls_the_server_side_authority(self, client):
        """The UI must defer to kanban-core, never decide a move on its own."""
        html = client.get("/").text
        assert "/can-move" in html, "drops must be validated by the engine"
        assert "/move" in html

    def test_the_page_surfaces_guard_reasons(self, client):
        html = client.get("/").text
        assert "refusalReasons" in html
        assert "Move refused by the board" in html

    def test_index_has_no_unbalanced_critical_tags(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        # Use a tag-boundary regex, not a substring count: `<header>` starts with
        # `<head`, so a naive count reports the head as unbalanced (a false
        # positive that would otherwise send someone hunting for a real bug).
        for tag in ("html", "head", "body", "script", "style"):
            opens = len(re.findall(rf"<{tag}[\s>]", html))
            closes = len(re.findall(rf"</{tag}\s*>", html))
            assert opens == closes, f"<{tag}> is unbalanced: {opens} open vs {closes} close"
        # Template literals legitimately contain "<div ...>" fragments, so a raw
        # div count is meaningless here; assert the document shape instead.
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")


class TestNodeLogicSuite:
    def test_node_unit_tests_for_the_drag_logic_pass(self):
        """The pure logic must pass under plain Node - no browser, no build step."""
        node = subprocess.run(["node", "--version"], capture_output=True, text=True)
        if node.returncode != 0:
            pytest.skip("node is not available")

        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["node", "--test", "board-ui/tests/test_board_logic.mjs"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"node tests failed:\n{result.stdout}\n{result.stderr}"
        assert "# fail 0" in result.stdout, result.stdout


class TestNodeModuleShape:
    def test_the_logic_module_is_syntactically_valid_javascript(self):
        """`node --check` catches a syntax error that would blank the board."""
        module = STATIC / "board_logic.mjs"
        result = subprocess.run(
            ["node", "--check", str(module)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, f"invalid JS: {result.stderr}"

    def test_the_page_script_parses(self):
        """An unescaped quote would keep counts balanced yet break rendering."""
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        start = html.index('<script type="module">') + len('<script type="module">')
        end = html.rindex("</script>")
        script = html[start:end]
        tmp = STATIC.parent.parent / "tests" / "_page_script_check.mjs"
        tmp.write_text(script, encoding="utf-8")
        try:
            result = subprocess.run(
                ["node", "--check", str(tmp)], capture_output=True, text=True, timeout=60
            )
            assert result.returncode == 0, f"page script does not parse: {result.stderr}"
        finally:
            tmp.unlink(missing_ok=True)

    def test_the_page_embeds_no_credentials(self):
        """A shell panel must not carry secrets into the browser."""
        html = (STATIC / "index.html").read_text(encoding="utf-8").lower()
        for needle in ("api_key", "apikey", "password", "secret", "bearer "):
            assert needle not in html, f"the page must not embed '{needle}'"
