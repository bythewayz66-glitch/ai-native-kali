"""The GTK4 half of the desktop client.

Phase 6, item 5: replace the browser-hosted panel with a real Wayland/GTK client.

What this actually is, stated plainly so it is not oversold: a GTK4 application
that anchors itself to the compositor as a persistent panel through
``gtk4-layer-shell`` - a ``wl_surface`` holding a reserved strip of the screen
rather than a tab in someone's browser. The panel's content is rendered with
**native GTK4 widgets**, so the client needs neither a browser nor a WebKit
runtime to show anything.

Why native, and not the live panel in a WebView
-----------------------------------------------
The first attempt hosted the existing panel HTML in a WebKit view, on the theory
that reusing a working surface beats rewriting it. **Running it against a real
Wayland session showed that cannot work on this image.** WebKitGTK 4.1 - the
only WebKitGTK packaged on the Debian bookworm base Kaii ships on - is built
against **GTK 3**. Embedding it in a GTK 4 window fails at version-pin time with
``Requiring namespace 'Gtk' version '3.0', but '4.0' is already loaded``. Only
WebKitGTK 6.0 is GTK-4-based, and it is not on bookworm.

So the WebView route would have left the client unable to start on the very
image it is built for. The panel is a status strip - state, services, pending
approvals, routed alerts - which is native-widget territory, not a web page. The
WebView mode is kept for images that *do* have WebKitGTK 6.0 (``SHELL_RENDER=web``)
and is refused, with a reason, anywhere else.

Honesty about the environment
-----------------------------
``gi`` is imported inside :func:`main`, never at module scope, and every decision
this client makes lives in :mod:`hermes_shell.gtk.session` or in the pure
:func:`panel_view` / :func:`fetch_overlay_state` helpers below - all testable on
a host with no Wayland session and no PyGObject. :func:`self_check` reports what
is missing instead of raising an import error nobody can act on.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from html import escape
from typing import Any, Mapping, Optional, Sequence

from .session import (
    DEFAULT_THICKNESS,
    anchors_for,
    detect_posture,
    exclusive_zone,
)
from .session import panel_geometry as _panel_geometry

log = logging.getLogger("hermes_shell.gtk")

APPLICATION_ID = "org.kali.ai.hermes.shell"
#: Where the board/overlay API is served.
DEFAULT_SHELL_URL = "http://127.0.0.1:8085"
#: Where routed alerts are read from.
DEFAULT_OBS_URL = "http://127.0.0.1:8084"

#: WebKitGTK typelibs a **GTK 4** client may embed, newest first.
#:
#: Only ``WebKit-6.0`` qualifies. ``WebKit2-4.1`` is deliberately absent: it is
#: GTK-3-based and pinning it inside a GTK 4 process fails outright. Listing it
#: as a fallback would produce a client that reports "WebKit available" and then
#: dies during import.
WEBKIT_TYPELIBS: tuple[tuple[str, str], ...] = (("WebKit", "6.0"),)

#: Human-readable form of the candidates, for the error and the report.
WEBKIT_CANDIDATES = ", ".join(f"{ns}-{ver}" for ns, ver in WEBKIT_TYPELIBS)

#: Default renderer. ``native`` needs only GTK4; ``web`` needs WebKitGTK 6.0.
DEFAULT_RENDER = "native"


def require_webkit(gi_module: Any) -> tuple[str, str]:
    """Pin the newest available GTK4-compatible WebKitGTK typelib.

    Returns ``(namespace, version)``. The namespace is part of the import path,
    so it is returned rather than assumed.
    """
    last: Optional[Exception] = None
    for namespace, version in WEBKIT_TYPELIBS:
        try:
            gi_module.require_version(namespace, version)
            return namespace, version
        except Exception as exc:  # noqa: BLE001 - keep trying older typelibs
            last = exc
    raise ImportError(f"no GTK4-compatible WebKitGTK typelib (tried {WEBKIT_CANDIDATES}): {last}")


def load_webkit(gi_module: Any) -> tuple[Any, str]:
    """Require and import the WebKitGTK module. Returns ``(module, label)``."""
    import importlib

    namespace, version = require_webkit(gi_module)
    module = importlib.import_module(f"gi.repository.{namespace}")
    return module, f"{namespace}-{version}"


def _gi_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("gi") is not None


def _webkit_label() -> Optional[str]:
    """The GTK4-compatible WebKitGTK typelib that would be used, or ``None``."""
    try:  # pragma: no cover - only when PyGObject really is installed
        import gi

        namespace, version = require_webkit(gi)
        return f"{namespace}-{version}"
    except Exception:  # noqa: BLE001
        return None


def self_check(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Report whether this client could start here, and what it is missing.

    Returns a dict rather than raising, because "no Wayland session" is an
    ordinary state on a build host - not an error condition.
    """
    source = dict(os.environ) if env is None else dict(env)
    posture = detect_posture(source)
    render = source.get("SHELL_RENDER", DEFAULT_RENDER).lower()
    webkit = _webkit_label()

    missing: list[str] = []
    if posture.session != "wayland":
        missing.append(posture.reason)
    elif not posture.layer_shell:
        missing.append(posture.reason)
    if not _gi_available():
        missing.append("PyGObject (python3-gi) is not importable")
    if render == "web" and webkit is None:
        missing.append(
            f"SHELL_RENDER=web but no GTK4-compatible WebKitGTK typelib (tried {WEBKIT_CANDIDATES})"
        )

    # The native panel needs GTK4 and nothing else, which is the point of it.
    can_start = (
        posture.runnable
        and _gi_available()
        and (render != "web" or webkit is not None)
    )
    return {
        **posture.describe(),
        "application_id": APPLICATION_ID,
        "shell_url": source.get("SHELL_URL", DEFAULT_SHELL_URL),
        "obs_url": source.get("OBS_URL", DEFAULT_OBS_URL),
        "render": render,
        "webkit": webkit,
        "can_start": can_start,
        "missing": missing,
    }


# --------------------------------------------------------------------------- #
# Pure render helpers - the part of the native panel that is testable here
# --------------------------------------------------------------------------- #
def fetch_overlay_state(
    shell_url: str,
    obs_url: Optional[str] = None,
    *,
    timeout: float = 2.0,
    opener: Any = None,
) -> dict[str, Any]:
    """Read the overlay model and the routed-alert feed.

    Never raises: a panel that dies because a service blipped would be worse than
    one showing "unreachable", so a failure is returned as
    ``{"error": ...}`` and rendered as such.
    """
    get = opener or (lambda url: json.loads(urllib.request.urlopen(url, timeout=timeout).read().decode()))
    state: dict[str, Any] = {}
    try:
        state = get(f"{shell_url.rstrip('/')}/api/overlay") or {}
    except Exception as exc:  # noqa: BLE001 - a degraded panel beats no panel
        state = {"error": f"overlay unreachable: {exc}"}

    if obs_url:
        try:
            feed = get(f"{obs_url.rstrip('/')}/alerting/notifications?limit=10") or {}
            state.setdefault("alert_notifications", feed.get("items") or [])
        except Exception:  # noqa: BLE001 - advisory; the overlay already reports health
            pass
    return state


def panel_view(state: Mapping[str, Any]) -> dict[str, str]:
    """Turn the overlay model into the exact strings the panel shows.

    Pure, and therefore the part of the GTK client that is actually covered by
    tests: the widget code around it is a place to put these strings.
    """
    if state.get("error"):
        return {
            "state": "unknown",
            "services": "services \u2013",
            "pending": "pending \u2013",
            "alert": str(state["error"]),
            "tooltip": str(state["error"]),
        }

    services = list(state.get("services") or [])
    healthy = sum(1 for s in services if s.get("ok"))
    totals = state.get("totals") or {}
    alerts = list(state.get("alerts") or [])
    routed = list(state.get("alert_notifications") or [])

    # The most severe alert wins the strip; the rest live in the tooltip. A panel
    # has one line, and the first thing an operator needs is the worst thing.
    rank = {"ok": 0, "warn": 1, "alert": 2}
    worst = max(alerts, key=lambda a: rank.get(str(a.get("severity")), 1), default=None)
    if worst:
        headline = f"{worst.get('severity', 'warn')}: {worst.get('text', '')}"
    elif routed:
        headline = f"routed: {routed[0].get('message') or routed[0].get('kind') or 'alert'}"
    else:
        headline = "no alerts"

    tooltip_lines = [
        f"{a.get('severity', 'warn')}: {a.get('text', '')}" for a in alerts
    ] or ["no alerts"]
    return {
        "state": str(state.get("state") or "unknown"),
        "services": f"services {healthy}/{len(services)}" if services else "services \u2013",
        "pending": f"pending {totals.get('pending_approvals', 0)}",
        "alert": headline,
        "tooltip": "\n".join(tooltip_lines),
    }


# --------------------------------------------------------------------------- #
# The GTK application
# --------------------------------------------------------------------------- #
_STATE_COLOURS = {"ok": "#3fb950", "warn": "#d29922", "alert": "#f85149", "unknown": "#8b949e"}


def _apply_layer_shell(layer_shell: Any, window: Any, edge: str, thickness: int) -> None:
    """Anchor the window as a reserved panel on the compositor."""
    layer_shell.init_for_window(window)
    edges = {
        "top": layer_shell.Edge.TOP,
        "bottom": layer_shell.Edge.BOTTOM,
        "left": layer_shell.Edge.LEFT,
        "right": layer_shell.Edge.RIGHT,
    }
    for name in anchors_for(edge):
        layer_shell.set_anchor(window, edges[name], True)
    layer_shell.set_exclusive_zone(window, exclusive_zone(edge, thickness))
    layer_shell.set_namespace(window, "hermes-shell")


def snapshot_widget(Gtk: Any, Gdk: Any, widget: Any, path: str) -> Optional[str]:
    """Render a widget to a PNG and return an error string, or ``None`` on success.

    This exists because the panel is otherwise only verifiable by looking at a
    compositor's output, and a headless backend's own screenshooter is not
    reliably available (``weston-screenshooter`` needs the debug protocol enabled
    on the *server*). Rendering the widget directly proves the same thing the
    screenshot would - that the widgets really painted the live state - and needs
    no compositor cooperation.

    Never raises: a diagnostics flag must not be able to take down the panel.
    """
    try:  # pragma: no cover - requires GTK rendering
        width, height = widget.get_width(), widget.get_height()
        if width <= 0 or height <= 0:
            return "the widget has not been allocated a size yet"
        paintable = Gtk.WidgetPaintable.new(widget)
        snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            return "the widget produced no render node"

        # The Cairo renderer, not ``render_texture``. The software renderer (the
        # one a headless compositor uses) cannot translate every node type
        # ``render_texture`` hands it - it fails on ``GskClipNode`` with "No means
        # to translate argument or return value", which is exactly what the
        # scrollable/clipped containers here produce. Rendering the node tree
        # straight into a cairo surface sidesteps that translation layer.
        import cairo

        from gi.repository import Gsk

        renderer = Gsk.CairoRenderer.new()
        renderer.realize(None)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        renderer.render(node, cairo.Context(surface))
        surface.write_to_png(path)
        return None
    except Exception as exc:  # noqa: BLE001 - diagnostics must never be fatal
        return str(exc)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start the client. Returns a process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    check = self_check()
    if not check["runnable"]:
        log.error("cannot start the Hermes GTK client: %s", check["reason"])
        log.error("the browser panel remains available at %s", check["shell_url"])
        return 2

    if not check["can_start"]:
        log.error("cannot start: %s", "; ".join(check["missing"]))
        log.error("set SHELL_RENDER=native (default) or install a GTK4 WebKitGTK")
        return 3

    try:  # pragma: no cover - requires PyGObject
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, GLib
    except Exception as exc:  # noqa: BLE001
        log.error("PyGObject/GTK4 unavailable (%s); install python3-gi gir1.2-gtk-4.0", exc)
        return 4

    edge = os.environ.get("SHELL_EDGE", "top")
    try:
        thickness = int(os.environ.get("SHELL_THICKNESS", str(DEFAULT_THICKNESS)))
    except ValueError:
        thickness = DEFAULT_THICKNESS
    shell_url = check["shell_url"]
    obs_url = check["obs_url"]
    render = check["render"]

    use_layer_shell = bool(check["can_anchor"])
    layer_shell = None
    if use_layer_shell:
        try:  # pragma: no cover - requires the layer-shell typelib
            gi.require_version("Gtk4LayerShell", "1.0")
            from gi.repository import Gtk4LayerShell as layer_shell
        except Exception as exc:  # noqa: BLE001
            log.warning("gtk4-layer-shell present as a library but not as a typelib (%s)", exc)
            use_layer_shell = False

    WebKit = None
    if render == "web":  # pragma: no cover - requires WebKitGTK 6.0
        try:
            WebKit, webkit_label = load_webkit(gi)
        except Exception as exc:  # noqa: BLE001
            log.error("SHELL_RENDER=web but WebKitGTK 6.0 is unavailable (%s)", exc)
            return 5
        log.info("rendering the live panel in WebKitGTK %s", webkit_label)

    # GTK parses the argv it is handed and rejects anything it does not
    # recognise, so the diagnostics flags are consumed here and never reach
    # `run()`. (Missing this is what "Unknown option --snapshot" was telling us.)
    raw = list(argv) if argv is not None else sys.argv
    snapshot_path: Optional[str] = None
    consume = {"--once", "--snapshot"}
    if "--snapshot" in raw:
        index = raw.index("--snapshot")
        if index + 1 < len(raw) and not raw[index + 1].startswith("--"):
            snapshot_path = raw[index + 1]
            consume.add(snapshot_path)
    gtk_argv = [raw[0] if raw else "hermes-shell"] + [a for a in raw[1:] if a not in consume]
    exit_after_paint = snapshot_path is not None or "--once" in raw

    class HermesPanel(Gtk.Application):  # pragma: no cover - needs a session
        def __init__(self) -> None:
            super().__init__(application_id=APPLICATION_ID)
            # `--snapshot PATH` renders the strip to a PNG and exits; `--once`
            # fetches, paints and exits without staying up. Both exist to verify
            # the panel on a headless compositor, where the compositor's own
            # screenshooter is not reliably available.
            self.snapshot_path = snapshot_path
            self.exit_after_paint = exit_after_paint

        def do_activate(self) -> None:  # noqa: N802 - GTK naming
            window = Gtk.ApplicationWindow(application=self, title="Hermes AI Desktop")
            if use_layer_shell:
                _apply_layer_shell(layer_shell, window, edge, thickness)
                geometry = _panel_geometry(1_000_000, 1, edge=edge, thickness=thickness)
                window.set_default_size(geometry["w"], geometry["h"])
            else:
                window.set_default_size(1100, 720)

            if WebKit is not None:
                view = WebKit.WebView()
                view.load_uri(f"{shell_url.rstrip('/')}/panel")
                window.set_child(view)
                window.present()
                log.info("hermes panel up: mode=web url=%s edge=%s", shell_url, edge)
                return

            window.set_child(self._build_strip())
            window.present()
            self._refresh()
            GLib.timeout_add_seconds(3, self._refresh)
            if self.exit_after_paint:
                # Give the compositor a beat to map and layout the surface before
                # the widget is rendered or the window is torn down.
                GLib.timeout_add(700, self._finish)
            log.info(
                "hermes panel up: mode=native url=%s role=%s edge=%s thickness=%d",
                shell_url,
                "panel" if use_layer_shell else "toplevel",
                edge,
                thickness,
            )

        def _build_strip(self) -> Any:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            box.set_margin_top(4)
            box.set_margin_bottom(4)
            box.set_margin_start(12)
            box.set_margin_end(12)

            self.badge = Gtk.Label(label="\u25cf boot")
            self.services = Gtk.Label(label="services \u2013")
            self.pending = Gtk.Label(label="pending \u2013")
            self.alerts = Gtk.Label(label="connecting\u2026")
            self.alerts.set_hexpand(True)
            self.alerts.set_xalign(0.0)
            try:
                from gi.repository import Pango

                self.alerts.set_ellipsize(Pango.EllipsizeMode.END)
            except Exception:  # noqa: BLE001 - ellipsizing is cosmetic
                pass
            self.clock = Gtk.Label(label="")

            for widget in (self.badge, self.services, self.pending, self.alerts, self.clock):
                box.append(widget)
            return box

        def _finish(self) -> bool:
            if self.snapshot_path:
                error = snapshot_widget(Gtk, None, self.badge.get_parent(), self.snapshot_path)
                if error:
                    log.warning("snapshot failed: %s", error)
                else:
                    log.info("panel snapshot written to %s", self.snapshot_path)
            self.quit()
            return False

        def _refresh(self) -> bool:
            view = panel_view(fetch_overlay_state(shell_url, obs_url))
            colour = _STATE_COLOURS.get(view["state"], _STATE_COLOURS["unknown"])
            self.badge.set_markup(
                f'<span foreground="{colour}">\u25cf</span> <b>{escape(view["state"])}</b>'
            )
            self.services.set_text(view["services"])
            self.pending.set_text(view["pending"])
            self.alerts.set_markup(f'<span foreground="#c9d1d9">{escape(view["alert"])}</span>')
            self.alerts.set_tooltip_text(view["tooltip"])
            import time

            self.clock.set_text(time.strftime("%H:%M"))
            return True

    return int(HermesPanel().run(gtk_argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())