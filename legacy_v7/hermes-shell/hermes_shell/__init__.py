"""Hermes shell panel package.

``build_app`` is re-exported for convenience, but **lazily**, and that matters:

    from hermes_shell import build_app      # still works, exactly as before
    from hermes_shell.gtk.client import ... # no longer drags in FastAPI

The desktop client is a GTK application whose only dependencies are GTK and
WebKit. Importing the package used to pull in ``server.py`` and therefore FastAPI,
which meant the client could not be imported - or verified - on an image that has
PyGObject installed for the panel but no Python web stack. A thin client that
cannot start without the server package it talks to over HTTP is a real coupling
defect, discovered when the client was first exercised on a live Wayland session.
"""
from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

__all__ = ["build_app"]


def build_app(*args: Any, **kwargs: Any) -> Any:
    from .server import build_app as _build_app

    return _build_app(*args, **kwargs)
