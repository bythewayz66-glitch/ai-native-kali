"""Session detection and panel geometry for the GTK client - pure logic.

Phase 6, item 5: replace the browser-hosted panel with a real Wayland/GTK client.

Everything here is deliberately free of ``gi``. The GTK half of the client cannot
be imported - let alone run - on a build host with no Wayland session and no
PyGObject, which is every CI container this repository runs in. If the *decisions*
lived inside the GTK code they would be permanently untested, so they live here
instead and the GTK code is a thin, boring shell around them.

Three decisions are worth getting right, and each is a way a compositor client
fails unhelpfully in the field:

1. **What kind of session are we in?** A Wayland session, an X11 session (where
   ``wl_surface`` does not exist and the client simply cannot run), or no display
   at all. The client must say which, rather than dying with an XCB error.
2. **Is ``gtk4-layer-shell`` actually present?** It is a separate shared library
   from GTK. Its absence is the normal case on an ordinary desktop, and falling
   back to a plain toplevel window is correct - failing is not.
3. **Where does the panel go?** Anchoring and the exclusive zone are computed
   once, here, so the geometry cannot disagree with the reserved strut.
"""
from __future__ import annotations

import ctypes.util
from dataclasses import dataclass
from typing import Mapping, Optional

#: Edges a panel may be anchored to. Layer-shell accepts any combination; the
#: shell only ever wants one full-length edge, so the model is that closed set.
EDGES = ("top", "bottom", "left", "right")

#: Default panel thickness in logical pixels.
DEFAULT_THICKNESS = 40


@dataclass(frozen=True)
class CompositorPosture:
    """What the client may assume about the session it was started in."""

    session: str  # "wayland" | "x11" | "headless"
    layer_shell: bool
    reason: str

    @property
    def can_anchor(self) -> bool:
        """May the panel reserve an exclusive zone on the compositor?"""
        return self.session == "wayland" and self.layer_shell

    @property
    def surface_role(self) -> str:
        """The surface role the client should ask for."""
        return "panel" if self.can_anchor else "toplevel"

    @property
    def runnable(self) -> bool:
        """A Wayland session is the one hard requirement."""
        return self.session == "wayland"

    def describe(self) -> dict[str, object]:
        return {
            "session": self.session,
            "layer_shell": self.layer_shell,
            "surface_role": self.surface_role,
            "can_anchor": self.can_anchor,
            "runnable": self.runnable,
            "reason": self.reason,
        }


def _find_library(name: str) -> Optional[str]:
    try:
        return ctypes.util.find_library(name)
    except Exception:  # noqa: BLE001 - a broken loader is "not present", not fatal
        return None


def detect_posture(
    env: Optional[Mapping[str, str]] = None,
    *,
    library_finder=_find_library,
) -> CompositorPosture:
    """Decide the session kind and whether layer-shell is usable.

    ``library_finder`` is injectable so the decision is tested on all three
    sessions without any of them having to exist on the test host.
    """
    source = env if env is not None else {}
    if "WAYLAND_DISPLAY" in source and source.get("WAYLAND_DISPLAY"):
        return CompositorPosture(
            session="wayland",
            layer_shell=library_finder("gtk4-layer-shell") is not None,
            reason=(
                "wayland session; gtk4-layer-shell present"
                if library_finder("gtk4-layer-shell") is not None
                else "wayland session, but gtk4-layer-shell is not installed: "
                "falling back to an ordinary toplevel window"
            ),
        )
    if source.get("DISPLAY"):
        return CompositorPosture(
            session="x11",
            layer_shell=False,
            reason="X11 session: gtk4-layer-shell needs a Wayland compositor",
        )
    return CompositorPosture(
        session="headless",
        layer_shell=False,
        reason="no WAYLAND_DISPLAY and no DISPLAY: there is no session to attach to",
    )


def panel_geometry(
    screen_w: int,
    screen_h: int,
    *,
    edge: str = "top",
    thickness: int = DEFAULT_THICKNESS,
) -> dict[str, int]:
    """The panel's surface geometry on a ``screen_w`` x ``screen_h`` output.

    A full-length bar on one edge. Raising ``ValueError`` rather than clamping a
    bad edge is deliberate: an unknown edge is a typo in the session config, and
    silently rendering the panel at the top when ``edge: buttom`` was written
    would hide it indefinitely.
    """
    if edge not in EDGES:
        raise ValueError(f"unknown edge {edge!r} (known: {', '.join(EDGES)})")
    thickness = max(1, int(thickness))
    if edge in ("top", "bottom"):
        # A thickness taller than the screen would reserve the whole output.
        thickness = min(thickness, max(1, screen_h))
        y = 0 if edge == "top" else max(0, screen_h - thickness)
        return {"x": 0, "y": y, "w": int(screen_w), "h": thickness}
    thickness = min(thickness, max(1, screen_w))
    x = 0 if edge == "left" else max(0, screen_w - thickness)
    return {"x": x, "y": 0, "w": thickness, "h": int(screen_h)}


def exclusive_zone(edge: str, thickness: int) -> int:
    """How much of the output the compositor must reserve for the panel.

    This is the number that stops a maximised window from being drawn *under* the
    panel, which is the difference between a panel and an overlay that hides the
    top 40 pixels of every window.
    """
    if edge not in EDGES:
        raise ValueError(f"unknown edge {edge!r} (known: {', '.join(EDGES)})")
    return max(1, int(thickness))


def anchors_for(edge: str) -> tuple[str, ...]:
    """Which layer-shell anchors to set for an edge-anchored bar."""
    if edge not in EDGES:
        raise ValueError(f"unknown edge {edge!r} (known: {', '.join(EDGES)})")
    if edge in ("top", "bottom"):
        return (edge, "left", "right")
    return (edge, "top", "bottom")
