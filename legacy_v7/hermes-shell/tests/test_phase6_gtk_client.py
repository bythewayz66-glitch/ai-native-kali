"""Phase 6, item 5: the GTK4/Wayland client - the parts that can be tested here.

The GTK half of the client cannot run on this host: there is no Wayland session,
no compositor and no PyGObject. That is precisely why everything it *decides* was
put somewhere importable - :mod:`hermes_shell.gtk.session` for the session and
geometry model, and the pure :func:`panel_view` / :func:`fetch_overlay_state`
helpers for the strip's content. Those are tested here; the widget code around
them only places the strings these functions return.

The failures worth pinning are the ones an operator would otherwise meet in the
field, and three of them were **actually found by running the client against a
real Wayland session**, not by review:

* WebKitGTK 4.1 is GTK-3-based, so it cannot be embedded in a GTK 4 window at
  all. Offering it as a fallback produced a client that reported "WebKit
  available" and then died during import. Only ``WebKit-6.0`` is listed now.
* The typelib *namespace* differs between the two (``WebKit`` vs ``WebKit2``),
  and it is part of the import path - getting the version right and the
  namespace wrong fails with "cannot import name WebKit".
* A GTK application rejects argv it does not recognise, so the diagnostics flags
  have to be consumed before ``run()`` - otherwise the client cannot be
  snapshotted at all.
"""
from __future__ import annotations

import pytest

import hermes_shell.gtk  # noqa: F401 - importing the package must not require gi
from hermes_shell.gtk.client import (
    DEFAULT_RENDER,
    DEFAULT_SHELL_URL,
    WEBKIT_TYPELIBS,
    fetch_overlay_state,
    panel_view,
    require_webkit,
    self_check,
)
from hermes_shell.gtk.session import (
    DEFAULT_THICKNESS,
    anchors_for,
    detect_posture,
    exclusive_zone,
    panel_geometry,
)


def _finder(*available):
    return lambda name: f"/usr/lib/lib{name}.so" if name in available else None


class TestSessionDetection:
    def test_wayland_with_layer_shell_can_anchor(self):
        posture = detect_posture({"WAYLAND_DISPLAY": "wayland-0"}, library_finder=_finder("gtk4-layer-shell"))
        assert posture.session == "wayland"
        assert posture.layer_shell is True
        assert posture.can_anchor is True
        assert posture.surface_role == "panel"
        assert posture.runnable is True

    def test_wayland_without_layer_shell_falls_back_to_a_toplevel(self):
        """The normal case on an ordinary desktop: usable, just not anchored."""
        posture = detect_posture({"WAYLAND_DISPLAY": "wayland-0"}, library_finder=_finder())
        assert posture.session == "wayland"
        assert posture.layer_shell is False
        assert posture.can_anchor is False
        assert posture.surface_role == "toplevel"
        assert posture.runnable is True, "a missing layer-shell library must not block startup"
        assert "not installed" in posture.reason

    def test_x11_is_reported_as_unrunnable(self):
        posture = detect_posture({"DISPLAY": ":0"}, library_finder=_finder("gtk4-layer-shell"))
        assert posture.session == "x11"
        assert posture.runnable is False
        assert "X11" in posture.reason

    def test_headless_is_reported_as_unrunnable(self):
        posture = detect_posture({}, library_finder=_finder())
        assert posture.session == "headless"
        assert posture.runnable is False
        assert posture.can_anchor is False

    def test_wayland_takes_precedence_over_display(self):
        """A Wayland session usually also exports DISPLAY, via XWayland."""
        posture = detect_posture(
            {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
            library_finder=_finder("gtk4-layer-shell"),
        )
        assert posture.session == "wayland"

    def test_an_empty_wayland_display_is_not_a_session(self):
        posture = detect_posture({"WAYLAND_DISPLAY": "", "DISPLAY": ":0"}, library_finder=_finder())
        assert posture.session == "x11"

    def test_describe_exposes_the_render_model(self):
        described = detect_posture({"WAYLAND_DISPLAY": "w"}, library_finder=_finder("gtk4-layer-shell")).describe()
        for key in ("session", "layer_shell", "surface_role", "can_anchor", "runnable", "reason"):
            assert key in described


class TestPanelGeometry:
    def test_top_panel_spans_the_width(self):
        assert panel_geometry(1920, 1080, edge="top", thickness=40) == {
            "x": 0, "y": 0, "w": 1920, "h": 40,
        }

    def test_bottom_panel_sits_at_the_bottom(self):
        assert panel_geometry(1920, 1080, edge="bottom", thickness=40) == {
            "x": 0, "y": 1040, "w": 1920, "h": 40,
        }

    def test_left_and_right_panels_span_the_height(self):
        assert panel_geometry(1920, 1080, edge="left", thickness=48) == {"x": 0, "y": 0, "w": 48, "h": 1080}
        assert panel_geometry(1920, 1080, edge="right", thickness=48) == {"x": 1872, "y": 0, "w": 48, "h": 1080}

    def test_an_unknown_edge_is_a_loud_error(self):
        """A typo must not silently render the panel somewhere else."""
        with pytest.raises(ValueError):
            panel_geometry(1920, 1080, edge="buttom")

    def test_a_thickness_taller_than_the_screen_is_clamped(self):
        assert panel_geometry(100, 20, edge="top", thickness=500)["h"] == 20

    def test_default_thickness_is_used_by_the_client(self):
        assert DEFAULT_THICKNESS == 40


class TestExclusiveZone:
    def test_the_zone_matches_the_thickness(self):
        assert exclusive_zone("top", 40) == 40

    def test_a_zero_thickness_still_reserves_a_pixel(self):
        assert exclusive_zone("top", 0) == 1

    def test_an_unknown_edge_is_a_loud_error(self):
        with pytest.raises(ValueError):
            exclusive_zone("middle", 40)


class TestAnchors:
    def test_a_top_bar_anchors_top_left_right(self):
        assert set(anchors_for("top")) == {"top", "left", "right"}

    def test_a_left_bar_anchors_left_top_bottom(self):
        assert set(anchors_for("left")) == {"left", "top", "bottom"}

    def test_an_unknown_edge_is_a_loud_error(self):
        with pytest.raises(ValueError):
            anchors_for("nowhere")


class TestWebkitNegotiation:
    def test_only_gtk4_compatible_typelibs_are_offered(self):
        """WebKitGTK 4.1 is GTK3-based; listing it produced a client that could
        not import inside a GTK4 process."""
        assert ("WebKit2", "4.1") not in WEBKIT_TYPELIBS
        assert ("WebKit", "6.0") in WEBKIT_TYPELIBS

    def test_6_0_is_tried_before_4_1(self):
        assert WEBKIT_TYPELIBS[0] == ("WebKit", "6.0")

    def test_the_newer_typelib_wins_when_both_exist(self):
        class FakeGi:
            def __init__(self):
                self.required = []

            def require_version(self, namespace, version):
                self.required.append((namespace, version))

        fake = FakeGi()
        assert require_webkit(fake) == ("WebKit", "6.0")
        assert fake.required == [("WebKit", "6.0")]

    def test_a_gtk3_only_webkit_is_refused_not_used_as_a_fallback(self):
        """The finding, pinned: WebKitGTK 4.1 is GTK3-based.

        Offering it as a fallback produced a client that reported WebKit as
        available and then died inside a GTK4 process with
        "Requiring namespace 'Gtk' version '3.0', but '4.0' is already loaded".
        A host that has only 4.1 must be told the truth, not handed a client that
        cannot start.
        """
        class FakeGi:
            def require_version(self, namespace, version):
                raise ValueError("Gtk 3.0 already loaded" if version == "6.0" else "not installed")

        with pytest.raises(ImportError, match="GTK4-compatible"):
            require_webkit(FakeGi())

    def test_no_typelib_at_all_is_a_clear_error(self):
        class FakeGi:
            def require_version(self, namespace, version):
                raise ValueError("nope")

        with pytest.raises(ImportError, match="GTK4-compatible"):
            require_webkit(FakeGi())

    def test_the_native_renderer_needs_no_webkit(self):
        assert DEFAULT_RENDER == "native"


class TestSelfCheck:
    def test_headless_self_check_says_not_runnable_and_what_is_missing(self):
        check = self_check({})
        assert check["runnable"] is False
        assert check["can_start"] is False
        assert isinstance(check["missing"], list) and check["missing"]

    def test_self_check_reports_the_shell_url(self):
        check = self_check({"SHELL_URL": "http://127.0.0.1:8085"})
        assert check["shell_url"] == "http://127.0.0.1:8085"

    def test_self_check_defaults_the_urls(self):
        check = self_check({})
        assert check["shell_url"] == DEFAULT_SHELL_URL
        assert check["obs_url"].startswith("http")

    def test_the_native_renderer_is_the_default(self):
        assert self_check({})["render"] == "native"

    def test_the_web_renderer_without_webkit_is_refused_with_a_reason(self):
        check = self_check({"SHELL_RENDER": "web"})
        assert check["can_start"] is False
        assert any("WebKitGTK" in m for m in check["missing"])

    def test_self_check_never_raises_on_a_hostile_environment(self):
        check = self_check({"WAYLAND_DISPLAY": "w", "SHELL_THICKNESS": "not-a-number"})
        assert "session" in check


# ------------------------------------------------------- the panel's content
def _state(**over):
    state = {
        "state": "warn",
        "services": [
            {"name": "kanban-core", "ok": True},
            {"name": "agent-runtime", "ok": False},
        ],
        "totals": {"pending_approvals": 2},
        "alerts": [{"severity": "warn", "kind": "card_blocked", "text": "crd_1 blocked"}],
        "alert_notifications": [],
    }
    state.update(over)
    return state


class TestPanelView:
    def test_it_reports_the_service_ratio(self):
        assert panel_view(_state())["services"] == "services 1/2"

    def test_it_reports_pending_approvals(self):
        assert panel_view(_state())["pending"] == "pending 2"

    def test_the_most_severe_alert_wins_the_strip(self):
        """A panel has one line; the worst thing is what belongs on it."""
        view = panel_view(
            _state(
                alerts=[
                    {"severity": "warn", "text": "minor"},
                    {"severity": "alert", "text": "the database is on fire"},
                ]
            )
        )
        assert "the database is on fire" in view["alert"]
        assert view["state"] == "warn"

    def test_the_tooltip_carries_every_alert(self):
        view = panel_view(_state(alerts=[{"severity": "warn", "text": "a"}, {"severity": "alert", "text": "b"}]))
        assert "a" in view["tooltip"] and "b" in view["tooltip"]

    def test_a_routed_alert_is_shown_when_there_is_no_board_alert(self):
        view = panel_view(_state(alerts=[], alert_notifications=[{"kind": "model_down", "message": "model gone"}]))
        assert "model gone" in view["alert"]

    def test_the_all_clear_case_says_so(self):
        view = panel_view(_state(alerts=[], alert_notifications=[]))
        assert view["alert"] == "no alerts"

    def test_an_unreachable_shell_degrades_instead_of_crashing(self):
        view = panel_view({"error": "overlay unreachable: refused"})
        assert view["state"] == "unknown"
        assert "unreachable" in view["alert"]

    def test_no_services_is_not_zero_out_of_zero(self):
        assert panel_view(_state(services=[]))["services"] == "services \u2013"

    def test_it_never_returns_none_fields(self):
        for key, value in panel_view(_state()).items():
            assert isinstance(value, str), key


class TestFetchOverlayState:
    def test_it_reads_the_overlay(self):
        calls = []

        def opener(url):
            calls.append(url)
            return {"state": "ok"}

        assert fetch_overlay_state("http://shell", opener=opener)["state"] == "ok"
        assert calls == ["http://shell/api/overlay"]

    def test_it_merges_the_routed_feed_when_an_obs_url_is_given(self):
        def opener(url):
            if "/api/overlay" in url:
                return {"state": "ok"}
            return {"items": [{"kind": "model_down"}]}

        state = fetch_overlay_state("http://shell", "http://obs", opener=opener)
        assert state["alert_notifications"] == [{"kind": "model_down"}]

    def test_an_unreachable_overlay_becomes_a_reported_error_not_an_exception(self):
        def opener(url):
            raise ConnectionRefusedError("refused")

        state = fetch_overlay_state("http://shell", opener=opener)
        assert "unreachable" in state["error"]
        assert panel_view(state)["state"] == "unknown"

    def test_a_dead_router_does_not_lose_the_overlay(self):
        def opener(url):
            if "/api/overlay" in url:
                return {"state": "ok", "services": []}
            raise ConnectionRefusedError("refused")

        state = fetch_overlay_state("http://shell", "http://obs", opener=opener)
        assert state["state"] == "ok"
