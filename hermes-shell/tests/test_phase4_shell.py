"""Hermes shell Phase 4 tests: file-manager drop, overlay widget, window manager.

These exercise the three new surfaces the Phase 4 kickoff asks for. The pure
logic - what a drop attaches, what the overlay concludes, how focus moves - is
tested directly, because that is where the behaviour that matters lives; the HTTP
routes are tested for their contract (status codes, forwarding, error mapping).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hermes_shell.attach import artifact_payload, entry_from_drop, kind_for
from hermes_shell.overlay import compose, health_row
from hermes_shell.server import build_app
from hermes_shell.wm import WindowManager


@pytest.fixture()
def client():
    with TestClient(build_app()) as c:
        yield c


# ============================================== file-manager drag and drop
class TestAttachKindDetection:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("scan.xml", "scan-output"),
            ("hosts.gnmap", "scan-output"),
            ("results.csv", "scan-output"),
            ("capture.pcap", "pcap"),
            ("handshake.pcapng", "pcap"),
            ("screen.png", "screenshot"),
            ("proof.jpg", "screenshot"),
            ("notes.log", "log"),
            ("scan-errors.txt", "log"),
            ("findings.md", "report"),
            ("report.pdf", "report"),
            ("data.json", "json"),
            ("payload.bin", "other"),  # unknown must NOT be guessed
            ("Makefile", "other"),
            ("", "other"),
        ],
    )
    def test_extension_maps_to_artifact_kind(self, filename, expected):
        assert kind_for(filename) == expected

    def test_unknown_extension_is_other_not_a_report(self):
        """A file mislabelled as a report reads as reviewed evidence. It is not."""
        assert kind_for("targets.xyz") == "other"


class TestAttachPayload:
    def test_payload_carries_a_reference_and_no_invented_url(self):
        payload = artifact_payload(filename="scan.xml", path="/home/op/scan.xml", size=2048)
        assert payload["name"] == "scan.xml"
        assert payload["kind"] == "scan-output"
        assert payload["path"] == "/home/op/scan.xml"
        assert payload["bytes"] == 2048
        # no blob store exists, so no url may be claimed
        assert "url" not in payload
        assert "sha256" not in payload

    def test_hash_is_recorded_only_when_the_bytes_were_actually_supplied(self):
        payload = artifact_payload(filename="a.log", path="/a.log", content=b"hello")
        assert payload["bytes"] == 5
        assert payload["sha256"] == (
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )

    def test_empty_filename_is_rejected(self):
        with pytest.raises(ValueError):
            artifact_payload(filename="   ")

    def test_negative_size_is_rejected(self):
        with pytest.raises(ValueError):
            artifact_payload(filename="a.log", size=-1)

    def test_produced_by_and_summary_are_recorded(self):
        payload = artifact_payload(filename="a.log", produced_by="hermes-shell", summary="custom")
        assert payload["produced_by"] == "hermes-shell"
        assert payload["summary"] == "custom"

    def test_summary_defaults_to_the_file_manager_provenance(self):
        payload = artifact_payload(filename="a.log")
        assert "file manager" in payload["summary"]


class TestDropNormalisation:
    def test_accepts_the_panel_payload(self):
        entry = entry_from_drop({"name": "scan.xml", "path": "/tmp/scan.xml", "size": 10})
        assert entry == {"name": "scan.xml", "path": "/tmp/scan.xml", "size": 10}

    def test_accepts_an_external_file_manager_payload(self):
        """A real file manager will not know our payload shape."""
        entry = entry_from_drop({"filename": "/tmp/report.pdf", "size": "512"})
        assert entry["name"] == "report.pdf"
        assert entry["size"] == 512

    def test_basename_is_taken_so_a_path_cannot_escape_into_the_name(self):
        entry = entry_from_drop({"name": "../../etc/passwd"})
        assert entry["name"] == "passwd"

    def test_payload_without_a_filename_is_rejected(self):
        with pytest.raises(ValueError):
            entry_from_drop({})

    def test_bad_size_degrades_to_unknown(self):
        assert entry_from_drop({"name": "a.log", "size": "not-a-number"})["size"] is None


class TestAttachRoute:
    def test_attach_forwards_to_kanban_core(self, client, monkeypatch):
        """The shell must not write artifacts itself - attachment is a board op."""
        import hermes_shell.server as server

        captured: dict = {}

        def fake_forward(method, path, payload=None, **kw):
            captured.update(method=method, path=path, payload=payload)
            from fastapi.responses import JSONResponse

            return JSONResponse({"card_id": "crd_1", "artifacts": [payload]}, status_code=201)

        monkeypatch.setattr(server, "_forward", fake_forward)
        r = client.post("/api/attach/crd_1", json={"name": "scan.xml", "path": "/tmp/scan.xml", "size": 42})
        assert r.status_code == 201
        assert captured["method"] == "POST"
        assert captured["path"] == "/api/cards/crd_1/artifacts"
        assert captured["payload"]["kind"] == "scan-output"
        assert captured["payload"]["produced_by"] == "hermes-shell"

    def test_a_path_only_drop_is_accepted_and_named_from_the_path(self, client, monkeypatch):
        """A real file manager may hand us only a path - that is a valid drop."""
        import hermes_shell.server as server
        from fastapi.responses import JSONResponse

        captured: dict = {}

        def fake_forward(method, path, payload=None, **kw):
            captured["payload"] = payload
            return JSONResponse({"card_id": "crd_1"}, status_code=201)

        monkeypatch.setattr(server, "_forward", fake_forward)
        r = client.post("/api/attach/crd_1", json={"path": "/tmp/whatever"})
        assert r.status_code == 201
        assert captured["payload"]["name"] == "whatever"

    def test_attach_rejects_a_payload_with_no_filename_at_all(self, client, monkeypatch):
        import hermes_shell.server as server

        monkeypatch.setattr(
            server, "_forward", lambda *a, **k: pytest.fail("must not forward an invalid drop")
        )
        # no name, no filename, no path -> nothing to attach
        r = client.post("/api/attach/crd_1", json={})
        assert r.status_code == 400
        assert "filename" in r.json()["detail"]

    def test_board_refusal_is_passed_through_not_flattened(self, client, monkeypatch):
        """A 404 for an unknown card must stay a 404."""
        import hermes_shell.server as server
        from fastapi.responses import JSONResponse

        monkeypatch.setattr(
            server,
            "_forward",
            lambda *a, **k: JSONResponse({"detail": "card not found"}, status_code=404),
        )
        r = client.post("/api/attach/nope", json={"name": "a.log"})
        assert r.status_code == 404


# ================================================== overlay widget
class TestOverlayCompose:
    def test_all_healthy_and_idle_is_ok(self):
        model = compose(
            {
                "totals": {"cards": 3, "running": 0, "blocked": 0, "pending_approvals": 0},
                "health": [health_row("kanban-core", "http://x", True)],
            }
        )
        assert model["state"] == "ok"
        assert model["alerts"] == [] or model["alerts"][0]["severity"] == "ok"
        assert model["counters"]["cards"] == 3

    def test_a_blocked_card_escalates_the_whole_overlay(self):
        """An overlay reporting 'ok' while a card is blocked is actively misleading."""
        model = compose(
            {
                "totals": {"cards": 5, "running": 2, "blocked": 1},
                "health": [health_row("kanban-core", "http://x", True)],
            }
        )
        assert model["state"] == "alert"
        assert any(a["kind"] == "blocked" for a in model["alerts"])
        assert "blocked" in model["headline"]

    def test_one_down_service_colours_the_overlay_even_if_the_board_is_busy(self):
        """The failure mode this forbids: averaging a dead service away."""
        model = compose(
            {
                "totals": {"cards": 9, "running": 3, "blocked": 0},
                "health": [
                    health_row("kanban-core", "http://x", True),
                    health_row("memory-store", "http://y", False, "ConnectionRefusedError"),
                ],
            }
        )
        assert model["state"] == "alert"
        assert "memory-store" in model["headline"]

    def test_pending_gates_are_warn_level(self):
        model = compose(
            {
                "totals": {"cards": 2, "running": 1, "pending_approvals": 2},
                "health": [health_row("kanban-core", "http://x", True)],
            }
        )
        assert model["state"] == "warn"
        assert any(a["kind"] == "gate" for a in model["alerts"])

    def test_severity_is_the_maximum_not_the_mean(self):
        model = compose(
            {
                "totals": {"cards": 1, "pending_approvals": 1, "blocked": 1},
                "health": [health_row("k", "http://x", True)],
            }
        )
        assert model["state"] == "alert"  # not "warn", despite both being present

    def test_collector_errors_are_surfaced_not_hidden(self):
        model = compose(
            {
                "totals": {},
                "health": [],
                "errors": ["overview: connection refused"],
            }
        )
        assert model["state"] == "warn"
        assert any("overview" in a["text"] for a in model["alerts"])

    def test_empty_sources_do_not_crash_the_overlay(self):
        model = compose({})
        assert model["state"] == "ok"
        assert model["counters"]["cards"] == 0

    def test_recent_event_is_passed_through(self):
        model = compose(
            {
                "totals": {"cards": 1},
                "recent_event": {"type": "card.moved", "card_id": "crd_1"},
            }
        )
        assert model["recent_event"]["type"] == "card.moved"


class TestOverlayCollection:
    def test_a_broken_board_degrades_to_errors_not_an_exception(self):
        from hermes_shell.overlay import collect

        class DeadClient:
            services = [health_row("kanban-core", "http://x", False)]

            def overview(self):
                raise RuntimeError("connection refused")

            def pending_approvals(self):
                raise RuntimeError("connection refused")

            def recent_events(self, limit=1):
                raise RuntimeError("connection refused")

        sources = collect(DeadClient())
        assert len(sources["errors"]) == 3
        model = compose(sources)
        assert model["state"] == "alert"

    def test_collection_uses_the_board_endpoints(self):
        from hermes_shell.overlay import OverlayClient

        class FakeClient(OverlayClient):
            def __init__(self):
                super().__init__("http://unused")
                self.paths: list[str] = []

            def _get(self, path, **params):
                self.paths.append(path)
                if path == "/api/overview":
                    return {"totals": {"cards": 4}, "boards": [{}]}
                if path == "/api/approvals/pending":
                    return {"approvals": [{"id": "apr_1"}]}
                return {"events": [{"type": "card.created"}]}

        from hermes_shell.overlay import collect

        client = FakeClient()
        sources = collect(client)
        assert sources["totals"] == {"cards": 4}
        assert sources["pending"] == [{"id": "apr_1"}]
        assert client.paths == ["/api/overview", "/api/approvals/pending", "/api/events"]


class TestOverlayRoute:
    def test_overlay_route_returns_the_render_model(self, client, monkeypatch):
        import hermes_shell.server as server
        from hermes_shell.overlay import OverlayClient

        class FakeClient(OverlayClient):
            def __init__(self, *a, **k):
                super().__init__("http://unused", services=[])

            def overview(self):
                return {"totals": {"cards": 7, "running": 1, "blocked": 1}, "boards": []}

            def pending_approvals(self):
                return []

            def recent_events(self, limit=1):
                return []

        monkeypatch.setattr(server, "OverlayClient", FakeClient)
        monkeypatch.setattr(server, "probe_services", lambda services, **k: [])
        body = client.get("/api/overlay").json()
        assert body["state"] == "alert"
        assert body["counters"]["cards"] == 7

    def test_overlay_route_never_500s_when_services_are_down(self, client):
        """No services are running in the test process; the overlay must still answer."""
        r = client.get("/api/overlay")
        assert r.status_code == 200
        body = r.json()
        assert body["state"] in {"ok", "warn", "alert"}
        assert "counters" in body


# ================================================== window manager (4a)
class TestWindowManager:
    def test_open_focuses_and_stacks_the_new_window_on_top(self):
        wm = WindowManager()
        wm.open("board", "Kanban")
        state = wm.open("terminal", "Terminal")
        assert state["count"] == 2
        assert state["stack"][-1] == state["focused"]
        assert [w["app"] for w in state["windows"]] == ["board", "terminal"]

    def test_click_to_focus_raises(self):
        wm = WindowManager()
        first = wm.open("board")["windows"][0]["id"]
        wm.open("terminal")
        state = wm.focus(first)
        assert state["focused"] == first
        assert state["stack"][-1] == first

    def test_focus_is_always_the_top_window(self):
        """If these disagree, the keyboard goes to an invisible window."""
        wm = WindowManager()
        ids = [wm.open(f"app{i}")["windows"][-1]["id"] for i in range(4)]
        for wid in (ids[0], ids[2], ids[1]):
            state = wm.focus(wid)
            assert state["stack"][-1] == state["focused"] == wid

    def test_closing_the_focused_window_moves_focus_to_the_next_topmost(self):
        """Focus that vanishes is how a desktop stops responding, unexplained."""
        wm = WindowManager()
        a = wm.open("a")["windows"][-1]["id"]
        b = wm.open("b")["windows"][-1]["id"]
        state = wm.close(b)
        assert state["focused"] == a
        assert state["count"] == 1

    def test_closing_the_last_window_leaves_no_focus_but_does_not_crash(self):
        wm = WindowManager()
        a = wm.open("a")["windows"][-1]["id"]
        state = wm.close(a)
        assert state["focused"] is None
        assert state["count"] == 0

    def test_minimising_the_focused_window_moves_focus_away(self):
        wm = WindowManager()
        a = wm.open("a")["windows"][-1]["id"]
        b = wm.open("b")["windows"][-1]["id"]
        state = wm.minimize(b)
        assert state["focused"] == a
        # minimised windows are skipped, not focused invisibly
        assert next(w for w in state["windows"] if w["id"] == b)["state"] == "minimized"

    def test_focus_skips_minimised_windows(self):
        wm = WindowManager()
        a = wm.open("a")["windows"][-1]["id"]
        b = wm.open("b")["windows"][-1]["id"]
        c = wm.open("c")["windows"][-1]["id"]
        wm.minimize(c)
        wm.minimize(b)
        state = wm.focus(c)  # focus un-minimises, so minimise c again after
        wm.minimize(c)
        assert wm.snapshot()["focused"] == a
        assert state is not None

    def test_taskbar_lists_every_window_including_minimised_ones(self):
        """A minimised window must stay reachable from the taskbar."""
        wm = WindowManager()
        wm.open("a")
        b = wm.open("b")["windows"][-1]["id"]
        wm.open("c")
        wm.minimize(b)
        bar = wm.taskbar_windows()
        assert len(bar) == 3
        assert [w["app"] for w in bar] == ["a", "b", "c"]  # opening order, stable
        assert next(w for w in bar if w["id"] == b)["state"] == "minimized"

    def test_taskbar_order_does_not_shuffle_when_focus_changes(self):
        """Buttons that reorder under the cursor are how a taskbar feels broken."""
        wm = WindowManager()
        ids = [wm.open(f"app{i}")["windows"][-1]["id"] for i in range(3)]
        before = [w["id"] for w in wm.taskbar_windows()]
        wm.focus(ids[0])
        after = [w["id"] for w in wm.taskbar_windows()]
        assert before == after

    def test_taskbar_flags_the_focused_window(self):
        wm = WindowManager()
        a = wm.open("a")["windows"][-1]["id"]
        wm.open("b")
        wm.focus(a)
        assert [w["id"] for w in wm.taskbar_windows() if w["focused"]] == [a]

    def test_maximize_remembers_and_restore_returns_the_users_geometry(self):
        """Restoring must not send the window to a default size the user never chose."""
        wm = WindowManager(width=1280, height=800)
        wid = wm.open("a")["windows"][-1]["id"]
        wm.move(wid, x=200, y=150)
        wm.resize(wid, w=600, h=400)
        original = wm.get(wid).bounds()

        maxed = wm.maximize(wid)
        assert maxed["windows"][-1]["bounds"] == {"x": 0, "y": 0, "w": 1280, "h": 800}
        assert wm.get(wid).state == "maximized"

        restored = wm.restore(wid)
        assert restored["windows"][-1]["bounds"] == original
        assert wm.get(wid).state == "normal"

    def test_toggle_maximize_is_a_round_trip(self):
        wm = WindowManager()
        wid = wm.open("a")["windows"][-1]["id"]
        wm.move(wid, x=120, y=90)
        original = wm.get(wid).bounds()
        wm.toggle_maximize(wid)
        assert wm.get(wid).state == "maximized"
        wm.toggle_maximize(wid)
        assert wm.get(wid).state == "normal"
        assert wm.get(wid).bounds() == original

    def test_a_maximised_window_does_not_move_or_resize(self):
        wm = WindowManager(width=1280, height=800)
        wid = wm.open("a")["windows"][-1]["id"]
        wm.maximize(wid)
        wm.move(wid, x=10, y=10)
        wm.resize(wid, w=100, h=100)
        assert wm.get(wid).bounds() == {"x": 0, "y": 0, "w": 1280, "h": 800}

    def test_move_is_clamped_to_the_desktop(self):
        """An off-screen title bar cannot be dragged back."""
        wm = WindowManager(width=1280, height=800)
        wid = wm.open("a")["windows"][-1]["id"]
        wm.move(wid, x=9999, y=-500)
        bounds = wm.get(wid).bounds()
        assert bounds["x"] <= 1280 - bounds["w"]
        assert bounds["y"] == 0

    def test_resize_has_a_sane_floor(self):
        wm = WindowManager()
        wid = wm.open("a")["windows"][-1]["id"]
        wm.resize(wid, w=1, h=1)
        bounds = wm.get(wid).bounds()
        assert bounds["w"] >= 200 and bounds["h"] >= 120

    def test_new_windows_cascade_rather_than_stack_exactly(self):
        wm = WindowManager()
        a = wm.open("a")["windows"][-1]["bounds"]
        b = wm.open("b")["windows"][-1]["bounds"]
        assert (a["x"], a["y"]) != (b["x"], b["y"])

    def test_unknown_window_id_returns_none_rather_than_raising(self):
        """A stale click on a closed window is normal, not exceptional."""
        wm = WindowManager()
        assert wm.focus("nope") is None
        assert wm.close("nope") is None
        assert wm.minimize("nope") is None
        assert wm.maximize("nope") is None
        assert wm.restore("nope") is None
        assert wm.move("nope", x=1) is None

    def test_apply_is_the_only_entry_point_and_ignores_unknown_actions(self):
        wm = WindowManager()
        assert wm.apply("explode", {}) is None
        assert wm.apply("", {}) is None
        assert wm.apply("open", {"app": "terminal", "title": "Terminal"})["count"] == 1


class TestWindowRoutes:
    def test_desktop_starts_empty(self, client):
        body = client.get("/api/windows").json()
        # Phase 16 added the virtual-desktop fields, so this compares the parts
        # that mean "empty" rather than pinning the whole payload shape.
        assert body["windows"] == []
        assert body["stack"] == []
        assert body["focused"] is None
        assert body["taskbar"] == []
        assert body["count"] == 0
        assert body["workspace"] == 0
        assert body["workspaces"] >= 1

    def test_open_focus_and_close_over_http(self, client):
        a = client.post("/api/windows", json={"action": "open", "app": "kanban", "title": "Kanban"}).json()
        wid = a["windows"][-1]["id"]
        assert a["focused"] == wid

        b = client.post("/api/windows", json={"action": "open", "app": "terminal"}).json()
        assert b["count"] == 2

        focused = client.post("/api/windows", json={"action": "focus", "id": wid}).json()
        assert focused["focused"] == wid
        assert focused["stack"][-1] == wid

        closed = client.post("/api/windows", json={"action": "close", "id": wid}).json()
        assert closed["count"] == 1
        assert closed["focused"] != wid

    def test_maximize_over_http(self, client):
        wid = client.post("/api/windows", json={"action": "open", "app": "x"}).json()["windows"][-1]["id"]
        state = client.post("/api/windows", json={"action": "maximize", "id": wid}).json()
        assert next(w for w in state["windows"] if w["id"] == wid)["state"] == "maximized"

    def test_missing_action_is_a_400(self, client):
        assert client.post("/api/windows", json={}).status_code == 400

    def test_unknown_action_is_a_400_not_a_silent_no_op(self, client):
        r = client.post("/api/windows", json={"action": "levitate", "id": "win_1"})
        assert r.status_code == 400
        assert "levitate" in r.json()["detail"]

    def test_unknown_window_is_a_404(self, client):
        r = client.post("/api/windows", json={"action": "focus", "id": "win_9999"})
        assert r.status_code == 404

    def test_window_state_persists_across_requests(self, client):
        """Windows live in the shell, not the browser, so a reload keeps them."""
        client.post("/api/windows", json={"action": "open", "app": "kanban"})
        client.post("/api/windows", json={"action": "open", "app": "terminal"})
        assert client.get("/api/windows").json()["count"] == 2

    def test_health_advertises_the_new_surfaces(self, client):
        surfaces = client.get("/health").json()["surfaces"]
        for surface in ("file_manager_drop", "overlay_widget", "window_manager"):
            assert surface in surfaces

    def test_config_exposes_the_memory_service(self, client):
        assert "memory_url" in client.get("/config").json()


# ============================================ the panel is wired to them
class TestPanelWiring:
    def test_panel_mentions_the_new_surfaces(self, client):
        html = client.get("/panel").text
        for marker in ("/api/overlay", "/api/windows", "/api/attach/", "data-drop-card"):
            assert marker in html, marker

    def test_panel_has_an_overlay_element(self, client):
        html = client.get("/panel").text
        assert 'id="overlay"' in html

    def test_panel_is_still_a_thin_client(self, client):
        html = client.get("/panel").text
        assert "function esc(" in html
        assert "innerHTML = d.cards" not in html
