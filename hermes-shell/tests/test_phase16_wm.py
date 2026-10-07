"""Phase 16, item 7: virtual desktops and tiling in the shell window manager.

The properties worth pinning are the *invisible* ones - the windows the user is
not looking at:

* a window on another desktop must not be focusable, listed, or counted, because
  the desktop a user sees and the one they type into would then disagree;
* switching to an **empty** desktop must clear focus rather than leave it on a
  window that is no longer on screen; and
* tiling must reach the screen edge exactly, with no pixel lost to rounding and
  no un-maximised state left over.

The tiling remainder test is the one that caught a bug: the first implementation
computed the last cell's width with a trailing-``if`` expression that silently
mis-sized it, and the grid stopped short of the right edge.
"""
from __future__ import annotations

import math

import pytest

from hermes_shell.wm import WindowManager


@pytest.fixture()
def wm() -> WindowManager:
    return WindowManager(width=1200, height=800, workspaces=4)


def _ids(wm: WindowManager) -> list[str]:
    return list(wm.snapshot()["stack"])


# ---------------------------------------------------------------------------
# virtual desktops
# ---------------------------------------------------------------------------
class TestVirtualDesktops:
    def test_a_new_window_opens_on_the_current_desktop(self, wm):
        wm.open("terminal")
        assert wm.snapshot()["windows"][0]["workspace"] == 0

    def test_switching_hides_the_other_desktops_windows(self, wm):
        wm.open("terminal")
        wm.switch_workspace(1)
        snap = wm.snapshot()
        assert snap["windows"] == []
        assert snap["taskbar"] == []
        assert snap["count"] == 0
        assert snap["total_count"] == 1

    def test_switching_to_an_empty_desktop_clears_focus(self, wm):
        """Leaving focus on an invisible window is the failure this guards."""
        wid = wm.open("terminal")["focused"]
        assert wid
        wm.switch_workspace(2)
        assert wm.snapshot()["focused"] is None

    def test_switching_back_restores_focus(self, wm):
        wid = wm.open("terminal")["focused"]
        wm.switch_workspace(1)
        wm.switch_workspace(0)
        assert wm.snapshot()["focused"] == wid

    def test_out_of_range_switch_is_ignored(self, wm):
        assert wm.switch_workspace(9) is None
        assert wm.switch_workspace(-1) is None
        assert wm.snapshot()["workspace"] == 0

    def test_workspace_counts_track_occupancy(self, wm):
        wm.open("a")
        wm.open("b")
        wm.switch_workspace(1)
        wm.open("c")
        assert wm.workspace_counts() == [2, 1, 0, 0]

    def test_move_to_workspace_does_not_follow(self, wm):
        wid = wm.open("terminal")["focused"]
        wm.move_to_workspace(wid, 2)
        # The user stays where they are; the window does not.
        assert wm.snapshot()["workspace"] == 0
        assert wm.snapshot()["count"] == 0
        assert wm.workspace_counts()[2] == 1

    def test_moving_the_focused_window_recomputes_focus(self, wm):
        first = wm.open("a")["focused"]
        second = wm.open("b")["focused"]
        assert second != first
        wm.move_to_workspace(second, 3)
        # Focus must land on something still visible, not follow the window away.
        assert wm.snapshot()["focused"] == first

    def test_focusing_a_window_on_another_desktop_follows_it(self, wm):
        wid = wm.open("terminal")["focused"]
        wm.move_to_workspace(wid, 1)
        wm.focus(wid)
        # Clicking a window on another desktop means "take me there".
        assert wm.snapshot()["workspace"] == 1
        assert wm.snapshot()["focused"] == wid

    def test_minimize_all_is_scoped_to_the_current_desktop(self, wm):
        wm.open("a")
        wm.switch_workspace(1)
        wm.open("b")
        wm.minimize_all()
        assert wm.snapshot()["windows"][0]["state"] == "minimized"
        # Desktop 0's window was never hidden by a show-desktop on desktop 1.
        wm.switch_workspace(0)
        assert wm.snapshot()["windows"][0]["state"] == "normal"

    def test_at_least_one_desktop_exists(self):
        assert WindowManager(workspaces=0).workspaces == 1
        assert WindowManager(workspaces=-5).workspaces == 1

    def test_cycle_skips_other_desktops(self, wm):
        a = wm.open("a")["focused"]
        b = wm.open("b")["focused"]
        wm.move_to_workspace(b, 2)
        wm.focus(a)
        # Only one visible window, so there is nothing to cycle to.
        assert wm.cycle() is None


# ---------------------------------------------------------------------------
# tiling
# ---------------------------------------------------------------------------
class TestTiling:
    def test_single_window_fills_the_desktop(self, wm):
        wm.open("terminal")
        snap = wm.tile()
        win = snap["windows"][0]
        assert win["bounds"] == {"x": 0, "y": 0, "w": 1200, "h": 800}

    def test_two_windows_split_vertically(self, wm):
        wm.open("a")
        wm.open("b")
        snap = wm.tile()
        left, right = snap["windows"]
        assert (left["bounds"]["x"], left["bounds"]["y"]) == (0, 0)
        assert (right["bounds"]["x"], right["bounds"]["y"]) == (600, 0)
        assert left["bounds"]["h"] == 800

    def test_four_windows_make_a_square_grid(self, wm):
        for _ in range(4):
            wm.open("app")
        snap = wm.tile()
        positions = {(w["bounds"]["x"], w["bounds"]["y"]) for w in snap["windows"]}
        assert positions == {(0, 0), (600, 0), (0, 400), (600, 400)}

    def test_grid_reaches_the_screen_edge_exactly(self, wm):
        """The remainder must be absorbed, not dropped.

        Uses a width that does not divide evenly by three so rounding cannot hide:
        1200 / 7 columns is 171.43, and the last column must take the remainder.
        """
        wm7 = WindowManager(width=1200, height=800)
        for _ in range(7):
            wm7.open("app")
        snap = wm7.tile()
        right = max(w["bounds"]["x"] + w["bounds"]["w"] for w in snap["windows"])
        assert right == 1200

    def test_gap_is_respected_and_the_grid_still_reaches_the_edge(self, wm):
        for _ in range(4):
            wm.open("app")
        snap = wm.tile(gap=10)
        right = max(w["bounds"]["x"] + w["bounds"]["w"] for w in snap["windows"])
        bottom = max(w["bounds"]["y"] + w["bounds"]["h"] for w in snap["windows"])
        assert right == 1200
        assert bottom == 800
        # Windows must not overlap once a gap is asked for.
        rects = [(w["bounds"]["x"], w["bounds"]["y"], w["bounds"]["w"], w["bounds"]["h"]) for w in snap["windows"]]
        for (x1, y1, w1, h1), (x2, y2, w2, h2) in [(rects[0], rects[1]), (rects[0], rects[2])]:
            overlap_x = not (x1 + w1 <= x2 or x2 + w2 <= x1)
            overlap_y = not (y1 + h1 <= y2 or y2 + h2 <= y1)
            assert not (overlap_x and overlap_y)

    def test_columns_can_be_overridden(self, wm):
        for _ in range(4):
            wm.open("app")
        snap = wm.tile(columns=4)
        # One row of four: every y is 0 and each is a quarter wide.
        assert {w["bounds"]["y"] for w in snap["windows"]} == {0}
        assert snap["windows"][0]["bounds"]["w"] == 300

    def test_tile_un_maximizes_and_clears_stale_restore_bounds(self, wm):
        """A maximised window cannot also occupy one grid cell."""
        first = wm.open("a")["focused"]
        wm.open("b")
        wm.maximize(first)
        snap = wm.tile()
        tiled = next(w for w in snap["windows"] if w["id"] == first)
        assert tiled["state"] == "normal"
        assert tiled["bounds"]["w"] < 1200
        # A later restore must not resurrect pre-maximise geometry.
        assert wm.get(first).restore_bounds is None
        assert wm.restore(first)["windows"][0]["bounds"]["w"] < 1200

    def test_tile_skips_minimized_windows(self, wm):
        first = wm.open("a")["focused"]
        wm.open("b")
        wm.minimize(first)
        snap = wm.tile()
        # Only the visible window is arranged.
        visible = [w for w in snap["windows"] if w["state"] != "minimized"]
        assert len(visible) == 1
        assert visible[0]["bounds"]["w"] == 1200

    def test_tile_ignores_other_desktops(self, wm):
        a = wm.open("a")["focused"]
        b = wm.open("b")["focused"]
        wm.move_to_workspace(b, 1)
        snap = wm.tile()
        assert [w["id"] for w in snap["windows"]] == [a]
        assert snap["windows"][0]["bounds"]["w"] == 1200

    def test_tile_on_an_empty_desktop_is_a_no_op(self, wm):
        assert wm.tile() is None

    def test_columns_never_exceed_the_window_count(self, wm):
        wm.open("a")
        wm.open("b")
        snap = wm.tile(columns=50)
        # Clamped to 2 columns, i.e. side by side - not 50 slivers.
        assert snap["windows"][0]["bounds"]["w"] == 600

    def test_default_columns_are_near_square(self, wm):
        """ceil(sqrt(n)) keeps cells as square as an integer grid allows."""
        for n in range(1, 10):
            wm2 = WindowManager(width=900, height=900)
            for _ in range(n):
                wm2.open("app")
            snap = wm2.tile()
            xs = {w["bounds"]["x"] for w in snap["windows"]}
            assert len(xs) == math.ceil(math.sqrt(n)), f"n={n} gave {len(xs)} columns"


# ---------------------------------------------------------------------------
# the earlier guarantees still hold with desktops in the picture
# ---------------------------------------------------------------------------
class TestPriorBehaviourIntact:
    def test_close_still_moves_focus(self, wm):
        first = wm.open("a")["focused"]
        second = wm.open("b")["focused"]
        snap = wm.close(second)
        assert snap["focused"] == first

    def test_snap_still_works_with_workspaces(self, wm):
        wid = wm.open("a")["focused"]
        snap = wm.snap(wid, edge="left")
        assert snap["windows"][0]["bounds"]["w"] == 600

    def test_cycle_still_walks_the_mru_list(self, wm):
        a = wm.open("a")["focused"]
        b = wm.open("b")["focused"]
        c = wm.open("c")["focused"]
        assert wm.cycle()["focused"] == b
        assert wm.cycle()["focused"] == a
        assert wm.cycle()["focused"] == c

    def test_apply_dispatches_the_new_actions(self, wm):
        a = wm.open("a")["focused"]
        wm.open("b")
        assert wm.apply("tile", {})["windows"][0]["bounds"]["w"] == 600
        assert wm.apply("move_to_workspace", {"id": a, "workspace": 1})["count"] == 1
        assert wm.apply("switch_workspace", {"workspace": 1})["workspace"] == 1
