"""Window-manager behaviour for the Hermes desktop shell.

Item 4 of the Phase 4 kickoff, first half: "Windows-like shell chrome (window
manager behaviour, taskbar, window controls, focus/stacking)".

Why this is a module and not a hundred lines of DOM
---------------------------------------------------
The rules that make a window manager feel *correct* are not visual - they are a
small state machine that is easy to get subtly wrong:

* clicking a window raises it and focuses it;
* the **focused** window is always the top one, or the two disagree and the
  keyboard goes somewhere invisible;
* closing the focused window moves focus to the next-highest window, **not** to
  nothing - focus that vanishes is how a desktop stops responding to the
  keyboard with no explanation;
* minimising the focused window does the same;
* maximising remembers the previous geometry, so restore returns the window to
  the size the user actually chose rather than to a default.

None of that is testable inside an event handler, so the state machine lives here
and the browser only sends it intentions (``open``, ``focus``, ``close``, ...).
The shell renders whatever this returns.

Taskbar semantics, stated because they are a decision rather than a detail
---------------------------------------------------------------------
The taskbar lists **every** window, including minimised ones, in the order the
windows were opened. Listing only visible windows would make a minimised window
unreachable from the taskbar, which is the one place a user looks for it; and
ordering by z would make the buttons shuffle under the cursor every time focus
changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

#: Cascade offset for new windows. Windows are placed in a cascade rather than
#: exactly on top of each other so a second window is obviously a second window.
CASCADE_STEP = 32
DEFAULT_SIZE = (760, 520)


@dataclass
class Window:
    """One shell window."""

    id: str
    app: str
    title: str
    x: int = 0
    y: int = 0
    w: int = DEFAULT_SIZE[0]
    h: int = DEFAULT_SIZE[1]
    state: str = "normal"  # normal | minimized | maximized
    #: Geometry before maximise, so restore returns to the user's real size.
    restore_bounds: Optional[dict[str, int]] = None

    def bounds(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "app": self.app,
            "title": self.title,
            "state": self.state,
            "bounds": self.bounds(),
        }


class WindowManager:
    """Focus, stacking and lifecycle for shell windows.

    Every mutating method returns the current view (``snapshot()``) or ``None``
    when the target window does not exist. Returning ``None`` rather than raising
    is deliberate for the UI path: a stale click on a window that a crew just
    closed is normal, not exceptional, and must not 500 the shell.
    """

    def __init__(self, *, width: int = 1280, height: int = 800) -> None:
        self.width = width
        self.height = height
        self._windows: dict[str, Window] = {}
        #: Bottom-to-top. The last element is the topmost window.
        self._stack: list[str] = []
        self._focused: Optional[str] = None
        self._created: list[str] = []  # opening order, for taskbar stability
        #: Most-recently-focused first. The Alt-Tab order, maintained explicitly
        #: rather than inferred from the z-stack (which changes as a side effect of
        #: raising) or from `_created` (opening order, which ignores what the user
        #: was just doing).
        self._recent: list[str] = []
        self._seq = 0

    def _touch(self, window_id: str) -> None:
        """Record that a window was just focused, making it the MRU head."""
        if window_id in self._recent:
            self._recent.remove(window_id)
        self._recent.insert(0, window_id)

    # ------------------------------------------------------------- views
    def snapshot(self) -> dict[str, Any]:
        """The full render model: z-order, focus, and the taskbar list."""
        return {
            "windows": [self._windows[wid].as_dict() for wid in self._stack],
            "stack": list(self._stack),  # bottom -> top
            "focused": self._focused,
            "taskbar": [
                {
                    **self._windows[wid].as_dict(),
                    "focused": wid == self._focused,
                }
                for wid in self._created
            ],
            "count": len(self._windows),
        }

    def get(self, window_id: str) -> Optional[Window]:
        return self._windows.get(window_id)

    # ----------------------------------------------------------- lifecycle
    def open(self, app: str, title: str = "", *, w: Optional[int] = None, h: Optional[int] = None) -> dict[str, Any]:
        """Open a window, focus it, and place it in a cascade."""
        self._seq += 1
        wid = f"win_{self._seq:04d}"
        offset = (len(self._created) % 8) * CASCADE_STEP
        width = min(w or DEFAULT_SIZE[0], self.width)
        height = min(h or DEFAULT_SIZE[1], self.height)
        window = Window(
            id=wid,
            app=app,
            title=title or app,
            x=min(offset + 24, max(0, self.width - width)),
            y=min(offset + 24, max(0, self.height - height)),
            w=width,
            h=height,
        )
        self._windows[wid] = window
        self._stack.append(wid)  # a new window opens on top
        self._created.append(wid)
        self._focused = wid
        self._touch(wid)
        return self.snapshot()

    def _raise(self, window_id: str) -> None:
        """Move a window to the top of the stack. Internal: focus owns this."""
        if window_id in self._stack:
            self._stack.remove(window_id)
        self._stack.append(window_id)

    def _focus_next(self, excluding: str) -> None:
        """Move focus to the topmost remaining non-minimised window.

        Minimised windows are skipped. Focusing a window the user cannot see
        would report a focused window while nothing on screen had focus.
        """
        for wid in reversed(self._stack):
            if wid == excluding:
                continue
            if self._windows[wid].state == "minimized":
                continue
            self._focused = wid
            return
        self._focused = None

    def focus(self, window_id: str) -> Optional[dict[str, Any]]:
        """Click-to-focus. Also un-minimises, because clicking a taskbar entry for
        a minimised window means 'show me that window'."""
        window = self._windows.get(window_id)
        if window is None:
            return None
        window.state = "normal" if window.state == "minimized" else window.state
        self._raise(window_id)
        self._focused = window_id
        self._touch(window_id)
        return self.snapshot()

    def close(self, window_id: str) -> Optional[dict[str, Any]]:
        window = self._windows.pop(window_id, None)
        if window is None:
            return None
        if window_id in self._stack:
            self._stack.remove(window_id)
        if window_id in self._created:
            self._created.remove(window_id)
        if window_id in self._recent:
            self._recent.remove(window_id)
        if self._focused == window_id:
            # Focus must land somewhere real, or the keyboard goes nowhere.
            self._focus_next(window_id)
        return self.snapshot()

    def minimize(self, window_id: str) -> Optional[dict[str, Any]]:
        window = self._windows.get(window_id)
        if window is None:
            return None
        window.state = "minimized"
        if self._focused == window_id:
            self._focus_next(window_id)
        return self.snapshot()

    def maximize(self, window_id: str) -> Optional[dict[str, Any]]:
        window = self._windows.get(window_id)
        if window is None:
            return None
        if window.state != "maximized":
            window.restore_bounds = window.bounds()
            window.state = "maximized"
            window.x = window.y = 0
            window.w = self.width
            window.h = self.height
        self._raise(window_id)
        self._focused = window_id
        self._touch(window_id)
        return self.snapshot()

    def restore(self, window_id: str) -> Optional[dict[str, Any]]:
        """Un-maximise back to the remembered geometry (and focus the window)."""
        window = self._windows.get(window_id)
        if window is None:
            return None
        if window.state == "maximized":
            bounds = window.restore_bounds or {"x": 24, "y": 24, **dict(zip(("w", "h"), DEFAULT_SIZE))}
            window.x, window.y = int(bounds.get("x", 24)), int(bounds.get("y", 24))
            window.w, window.h = int(bounds.get("w", DEFAULT_SIZE[0])), int(bounds.get("h", DEFAULT_SIZE[1]))
            window.restore_bounds = None
        window.state = "normal"
        self._raise(window_id)
        self._focused = window_id
        self._touch(window_id)
        return self.snapshot()

    def toggle_maximize(self, window_id: str) -> Optional[dict[str, Any]]:
        window = self._windows.get(window_id)
        if window is None:
            return None
        return self.restore(window_id) if window.state == "maximized" else self.maximize(window_id)

    def move(self, window_id: str, *, x: Optional[int] = None, y: Optional[int] = None) -> Optional[dict[str, Any]]:
        """Move a window, clamped to the desktop.

        Clamping is not cosmetic: a window whose title bar is off-screen cannot be
        dragged back, so a bad coordinate would permanently strand it.
        """
        window = self._windows.get(window_id)
        if window is None:
            return None
        if window.state == "maximized":
            return self.snapshot()  # a maximised window does not move
        if x is not None:
            window.x = max(0, min(int(x), max(0, self.width - window.w)))
        if y is not None:
            window.y = max(0, min(int(y), max(0, self.height - window.h)))
        return self.snapshot()

    def resize(self, window_id: str, *, w: int, h: int) -> Optional[dict[str, Any]]:
        window = self._windows.get(window_id)
        if window is None:
            return None
        if window.state == "maximized":
            return self.snapshot()
        window.w = max(200, min(int(w), self.width))
        window.h = max(120, min(int(h), self.height))
        # Re-clamp: growing a window can push it past the edge.
        return self.move(window_id, x=window.x, y=window.y)

    # ------------------------------------------------------------ dispatch
    def apply(self, action: str, payload: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        """Apply an intention from the shell. Unknown actions are ignored.

        One entry point means the browser cannot invent a transition the state
        machine does not implement - the same reasoning as the Kanban board's own
        transition guards.
        """
        payload = payload or {}
        wid = payload.get("id") or payload.get("window_id")
        if action == "open":
            return self.open(
                str(payload.get("app") or "app"),
                str(payload.get("title") or ""),
                w=payload.get("w"),
                h=payload.get("h"),
            )
        if action == "focus":
            return self.focus(str(wid))
        if action == "close":
            return self.close(str(wid))
        if action == "minimize":
            return self.minimize(str(wid))
        if action == "maximize":
            return self.maximize(str(wid))
        if action == "restore":
            return self.restore(str(wid))
        if action == "toggle_maximize":
            return self.toggle_maximize(str(wid))
        if action == "move":
            return self.move(str(wid), x=payload.get("x"), y=payload.get("y"))
        if action == "resize":
            return self.resize(str(wid), w=int(payload.get("w") or 600), h=int(payload.get("h") or 400))
        if action == "cycle":
            return self.cycle(backwards=bool(payload.get("backwards")))
        if action == "snap":
            return self.snap(
                str(wid),
                edge=str(payload.get("edge") or "left"),
                fraction=float(payload.get("fraction") or 0.5),
            )
        if action == "minimize_all":
            return self.minimize_all()
        if action == "restore_all":
            return self.restore_all()
        return None

    def cycle(self, *, backwards: bool = False) -> Optional[dict[str, Any]]:
        """Alt-Tab: focus the next window in **recency** order.

        The order is MRU (most-recently-used), not the z-stack and not the
        taskbar's opening order, and the distinction matters:

        * the z-stack changes as a side effect of raising, so cycling by z would
          make repeated Alt-Tabs oscillate between two windows instead of walking
          through them;
        * the taskbar order is opening order, so cycling by it would ignore what
          the user was just doing - the whole point of Alt-Tab is "the one I had
          open a second ago".

        Minimised windows are skipped: focusing one the user cannot see would
        report a focused window while nothing on screen had focus.

        Cycling moves a cursor along the MRU list; it does **not** re-rank it.
        Re-ranking on every press is the bug that makes Alt-Tab oscillate - see
        the comment on the body below.
        """
        candidates = [wid for wid in self._recent if wid in self._windows and self._windows[wid].state != "minimized"]
        if len(candidates) < 2:
            return None
        # `candidates[0]` is the current window; step to the next MRU entry.
        index = candidates.index(self._focused) if self._focused in candidates else 0
        step = -1 if backwards else 1
        target = candidates[(index + step) % len(candidates)]
        self._raise(target)
        self._focused = target
        # Deliberately NO `_touch(target)` here. Recording the target as the MRU
        # head would make the *next* cycle step straight back to the window we
        # just left, so Alt-Tab would oscillate between two windows instead of
        # walking the list - the exact failure the z-stack walk has, arrived at
        # from the other direction. The MRU list is reordered only by a direct
        # focus (`focus`/`open`), which is what "most recently used" means;
        # cycling moves the cursor without re-ranking the entries.
        return self.snapshot()

    def snap(
        self,
        window_id: str,
        *,
        edge: str,
        fraction: float = 0.5,
    ) -> Optional[dict[str, Any]]:
        """Snap a window to a screen edge: ``left`` | ``right`` | ``top``.

        Half-width for left/right, full desktop for top (the conventional
        "maximise but not really" gesture). Snapping **stacks with maximise
        correctly** because it clears ``restore_bounds`` when it un-maximises: a
        snapped window is a normal window with new geometry, so a later restore
        must read *that* geometry and not a stale pre-maximise one.

        ``fraction`` is clamped to (0, 1] - a zero-width window is unclickable.
        """
        window = self._windows.get(window_id)
        if window is None:
            return None
        if edge not in ("left", "right", "top"):
            raise ValueError(f"unknown snap edge {edge!r} (use left, right or top)")
        fraction = max(0.1, min(float(fraction), 1.0))
        window.state = "normal"
        # Snapping replaces the geometry, so any remember-me-before-maximise
        # bounds are now stale and must go.
        window.restore_bounds = None
        if edge == "left":
            window.x, window.y = 0, 0
            window.w = max(200, int(self.width * fraction))
            window.h = self.height
        elif edge == "right":
            window.w = max(200, int(self.width * fraction))
            window.x = max(0, self.width - window.w)
            window.y = 0
            window.h = self.height
        else:  # top
            window.x, window.y = 0, 0
            window.w = self.width
            window.h = max(120, int(self.height * fraction))
        self._raise(window_id)
        self._focused = window_id
        self._touch(window_id)
        return self.snapshot()

    def minimize_all(self) -> Optional[dict[str, Any]]:
        """Show-desktop. Focus lands on ``None`` because nothing is visible.

        ``None`` is the honest report here, and the alternative - leaving focus on
        a window the user just hid - is the bug ``_focus_next`` exists to prevent.
        """
        if not self._windows:
            return None
        for window in self._windows.values():
            window.state = "minimized"
        self._focused = None
        return self.snapshot()

    def restore_all(self) -> Optional[dict[str, Any]]:
        """Un-minimise everything, focusing the most recently opened window.

        The inverse of :meth:`minimize_all`. Without it, show-desktop is a
        one-way door: every window is still listed in the taskbar, but the user
        has to click each one back individually.
        """
        hidden = [wid for wid, w in self._windows.items() if w.state == "minimized"]
        if not hidden:
            return None
        for wid in hidden:
            self._windows[wid].state = "normal"
            self._raise(wid)
        self._focused = hidden[-1]
        self._touch(self._focused)
        return self.snapshot()

    def taskbar_windows(self) -> list[dict[str, Any]]:
        """The taskbar list: every window, opening order, focus flagged."""
        return [w for w in self.snapshot()["taskbar"]]

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot()
