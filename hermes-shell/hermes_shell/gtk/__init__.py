"""GTK4 / Wayland client for the Hermes desktop shell.

The shell has shipped as a browser-hosted panel since Phase 1: real system state,
but rendered inside a tab. This package is the compositor client that replaces
that tab with an actual desktop surface - a GTK4 window using
``gtk4-layer-shell`` to anchor itself as a persistent panel on a Wayland session.

Split deliberately in two:

* :mod:`hermes_shell.gtk.session` - pure logic. Which session are we on, is
  layer-shell available, and what geometry does the panel occupy. Importable and
  testable anywhere, including a CI container with no display server.
* :mod:`hermes_shell.gtk.client` - the GTK/WebKit half. Imports ``gi`` lazily, so
  importing this package never requires PyGObject to be installed.
"""
