"""Window-manager depth (Phase 7, item 6).

``wm.py`` already had focus, stacking, minimise and maximise under test. Item 6
adds the four behaviours a shell is judged by once it is in front of a person:

* **Alt-Tab cycling** (``cycle``) - and, importantly, *which* order it walks.
* **Edge snapping** (``snap``).
* **Show-desktop and its inverse** (``minimize_all`` / ``restore_all``).

The ordering claim is the one worth stating: cycling must walk
**most-recently-used**, not the z-stack and not the taskbar's opening order. A
z-stack walk oscillates between two windows because raising reorders the stack as
a side effect; an opening-order walk ignores what the user was just doing. Both
would pass a naive "does Alt-Tab change the window" test, so the tests below
assert the sequence, not just the change.
"""
from __future__ import annotations

import pytest

from hermes_shell.wm import WindowManager


@pytest.fixture()
def wm():
    return WindowManager(width=1280, height=800)


@pytest.fixture()
def three(wm):
    state = wm.open("files", "Files")
    a = state["windows"][-1]["id"]
    state = wm.open("terminal", "Terminal")
    b = state["windows"][-1]["id"]
    state = wm.open("nmap-frontend", "Nmap")
    c = state["windows"][-1]["id"]
    return wm, a, b, c


class TestCycle:
    def test_cycle_moves_focus_off_the_current_window(self, wm, three):
        wm_, _a, _b, c = three
        after = wm_.cycle()
        assert after["focused"] != c

    def test_cycle_walks_most_recently_used_order(self, wm, three):
        """The load-bearing assertion: the *sequence*, not just "it changed".

        Recency after opening A, B, C is C, B, A. Cycling from C must step to B
        and then to A. A z-stack implementation would return B then C, bouncing
        between two windows, and would pass a naive change-only test.
        """
        wm_, a, b, c = three
        assert wm_.snapshot()["focused"] == c
        assert wm_.cycle()["focused"] == b
        assert wm_.cycle()["focused"] == a

    def test_cycle_walks_every_window_before_repeating(self, wm, three):
        wm_, a, b, c = three
        assert [wm_.cycle()["focused"] for _ in range(3)] == [b, a, c]

    def test_backwards_cycle_reverses_the_order(self, wm, three):
        wm_, a, b, _c = three
        # Recency is C, B, A; backwards from C is A, then B.
        assert wm_.cycle(backwards=True)["focused"] == a
        assert wm_.cycle(backwards=True)["focused"] == b

    def test_focusing_a_window_makes_it_the_recency_head(self, wm, three):
        """Alt-Tab back to the window you just came from must be one press.

        After clicking A, recency is A, C, B - so one cycle lands on C.
        """
        wm_, _a, _b, c = three
        wm_.focus(_a)
        assert wm_.cycle()["focused"] == c

    def test_cycle_skips_minimised_windows(self, wm, three):
        wm_, a, b, c = three
        wm_.minimize(b)
        # Recency C, B, A with B hidden; cycling from C must reach A, never B.
        assert wm_.snapshot()["focused"] == c
        assert wm_.cycle()["focused"] == a

    def test_cycle_with_one_window_is_a_no_op(self, wm):
        wm.open("files", "Files")
        assert wm.cycle() is None

    def test_cycle_still_raises_the_target(self, wm, three):
        """Focusing a window must also bring it forward, or the keyboard goes to
        a window the user cannot see."""
        wm_, _a, _b, c = three
        target = wm_.cycle()["focused"]
        assert wm_.snapshot()["stack"][-1] == target
        assert target != c

    def test_dispatch_reaches_cycle(self, wm, three):
        assert wm.apply("cycle", {})["focused"] is not None
        assert wm.apply("cycle", {"backwards": True})["focused"] is not None


class TestSnap:
    def test_snap_left_takes_the_left_half(self, wm):
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        after = wm.snap(wid, edge="left")
        window = [w for w in after["windows"] if w["id"] == wid][0]
        assert window["bounds"] == {"x": 0, "y": 0, "w": 640, "h": 800}

    def test_snap_right_takes_the_right_half(self, wm):
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        after = wm.snap(wid, edge="right")
        window = [w for w in after["windows"] if w["id"] == wid][0]
        assert window["bounds"] == {"x": 640, "y": 0, "w": 640, "h": 800}

    def test_snap_top_is_full_width(self, wm):
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        after = wm.snap(wid, edge="top")
        window = [w for w in after["windows"] if w["id"] == wid][0]
        assert window["bounds"]["x"] == 0
        assert window["bounds"]["w"] == 1280

    def test_a_snapped_window_is_no_longer_maximised(self, wm):
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        wm.maximize(wid)
        after = wm.snap(wid, edge="left")
        window = [w for w in after["windows"] if w["id"] == wid][0]
        assert window["state"] == "normal"

    def test_snap_after_maximise_does_not_restore_the_old_geometry(self, wm):
        """The interaction that would otherwise be a real bug.

        Maximise stores the pre-maximise bounds for restore. If snapping left
        those bounds in place, a later restore would return the window to
        geometry from two gestures ago - the user would see their snap silently
        undone by an unrelated click.
        """
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        wm.snap(wid, edge="left")
        snapped = {"x": 0, "y": 0, "w": 640, "h": 800}
        wm.maximize(wid)
        after = wm.restore(wid)
        window = [w for w in after["windows"] if w["id"] == wid][0]
        assert window["bounds"] == snapped

    def test_snap_clamps_the_fraction(self, wm):
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        after = wm.snap(wid, edge="left", fraction=0.0)
        window = [w for w in after["windows"] if w["id"] == wid][0]
        assert window["bounds"]["w"] >= 200

    def test_an_unknown_edge_is_refused(self, wm):
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        with pytest.raises(ValueError, match="unknown snap edge"):
            wm.snap(wid, edge="diagonal")

    def test_snapping_focuses_the_window(self, wm, three):
        wm_, a, _b, _c = three
        assert wm_.snap(a, edge="left")["focused"] == a

    def test_dispatch_reaches_snap(self, wm):
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        after = wm.apply("snap", {"id": wid, "edge": "right"})
        window = [w for w in after["windows"] if w["id"] == wid][0]
        assert window["bounds"]["x"] == 640


class TestShowDesktop:
    def test_minimize_all_hides_everything(self, wm, three):
        wm_, _a, _b, _c = three
        after = wm_.minimize_all()
        assert all(w["state"] == "minimized" for w in after["windows"])

    def test_minimize_all_leaves_nothing_focused(self, wm, three):
        """Reporting a focused window while nothing is visible is the bug
        ``_focus_next`` exists to prevent - and it applies here too."""
        wm_, _a, _b, _c = three
        assert wm_.minimize_all()["focused"] is None

    def test_the_taskbar_still_lists_the_hidden_windows(self, wm, three):
        """A minimised window must stay reachable from the taskbar, or it is
        gone with no way back."""
        wm_, _a, _b, _c = three
        after = wm_.minimize_all()
        assert len(after["taskbar"]) == 3

    def test_restore_all_brings_them_back(self, wm, three):
        wm_, _a, _b, _c = three
        wm_.minimize_all()
        after = wm_.restore_all()
        assert all(w["state"] == "normal" for w in after["windows"])
        assert after["focused"] is not None

    def test_restore_all_is_a_no_op_when_nothing_is_hidden(self, wm, three):
        """A window that was never hidden must not be marked restored - and on an
        empty desktop there is nothing to restore at all."""
        wm_, _a, _b, _c = three
        assert wm_.restore_all() is None

    def test_show_desktop_round_trips(self, wm, three):
        wm_, _a, _b, c = three
        wm_.minimize_all()
        after = wm_.restore_all()
        # The most recently opened window ends up focused, not some arbitrary one.
        assert after["focused"] == c

    def test_dispatch_reaches_both(self, wm, three):
        assert wm.apply("minimize_all", {})["focused"] is None
        assert wm.apply("restore_all", {})["focused"] is not None


class TestExistingContractUnchanged:
    """Item 6 must not regress the Phase 4 state machine."""

    def test_open_focus_close_still_behaves(self, wm, three):
        wm_, a, _b, c = three
        wm_.focus(a)
        assert wm_.snapshot()["focused"] == a
        after = wm_.close(a)
        assert after["focused"] is not None and after["focused"] != a
        assert len(after["windows"]) == 2
        assert c in after["stack"]

    def test_closing_the_last_window_leaves_nothing_focused(self, wm):
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        assert wm.close(wid)["focused"] is None

    def test_maximise_restore_still_remembers_the_user_geometry(self, wm):
        state = wm.open("files", "Files")
        wid = state["windows"][-1]["id"]
        wm.move(wid, x=100, y=80)
        before = [w for w in wm.snapshot()["windows"] if w["id"] == wid][0]["bounds"]
        wm.maximize(wid)
        after = wm.restore(wid)
        assert [w for w in after["windows"] if w["id"] == wid][0]["bounds"] == before

    def test_unknown_actions_are_still_ignored(self, wm):
        assert wm.apply("teleport", {"id": "win_0001"}) is None
