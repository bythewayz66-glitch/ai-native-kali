"""Shared pytest fixtures for the AI-native Kali monorepo."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Each component is a standalone package directory; put them all on the path.
for component in (
    "kanban-core",
    "agent-runtime",
    "tool-frontends",
    "observability",
    "hermes-shell",
    "board-ui",
    "memory-store",
):
    path = ROOT / component
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Keep test runs from touching a developer's real database.
os.environ.setdefault("KANBAN_DB", ":memory:")
os.environ.setdefault("KANBAN_SEED", "0")
os.environ.setdefault("TOOLS_DRY_RUN", "1")
os.environ.setdefault("TOOLS_AUDIT_DB", ":memory:")
os.environ.setdefault("OBS_DB", ":memory:")
# Never let a test reach out to a real model endpoint.
os.environ.setdefault("MODEL_ENABLED", "0")
os.environ.setdefault("BRIDGE_AUTOSTART", "0")
# L6 memory is off unless a test opts in, so the suite stays hermetic.
os.environ.setdefault("MEMORY_ENABLED", "0")
os.environ.setdefault("MEMORY_DB", ":memory:")
