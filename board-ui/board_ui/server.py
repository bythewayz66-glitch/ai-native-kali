"""Hermes Kanban board UI: a standalone, interactive board service.

Blueprint ref: section 03.3 - "Kanban surfaces in the shell" - and the Phase 2
requirement for a drag-and-drop board as a **separate web app** talking to
kanban-core's REST + WebSocket API.

This service is deliberately thin: it serves one static page and tells it where
kanban-core lives. It holds no board state of its own, so there is exactly one
source of truth (kanban-core) and no way for the UI to drift from the engine.

The drag-and-drop *decisions* live in ``static/board_logic.mjs`` as pure
functions, which means they can be unit-tested under plain Node (see
``tests/test_board_logic.mjs``). The UI checks a drop locally for instant
feedback and then asks the server ``/can-move`` - but the authoritative answer is
always kanban-core's, and a rejected move is surfaced with the server's own guard
reasons rather than a generic error.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: The six lifecycle columns, mirroring kanban_core.state_machine.
COLUMNS = ["Backlog", "Assigned", "Running", "Review", "Done", "Blocked"]


def build_app(
    *,
    kanban_url: Optional[str] = None,
    ws_url: Optional[str] = None,
    static_dir: Optional[Path] = None,
) -> FastAPI:
    kanban_url = (kanban_url or os.environ.get("KANBAN_URL", "http://127.0.0.1:8081")).rstrip("/")
    if ws_url is None:
        ws_url = os.environ.get("KANBAN_WS_URL") or (
            kanban_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/events"
        )
    directory = Path(static_dir) if static_dir else STATIC_DIR

    app = FastAPI(
        title="AI-native Kali - Hermes Kanban Board",
        version="0.2.0",
        description="Interactive drag-and-drop board over kanban-core's REST + WebSocket API.",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        index = directory / "index.html"
        return {
            "status": "ok" if index.is_file() else "degraded",
            "component": "board-ui",
            "static_dir": str(directory),
            "index_present": index.is_file(),
            "logic_present": (directory / "board_logic.mjs").is_file(),
            "columns": COLUMNS,
            "kanban_url": kanban_url,
            "ws_url": ws_url,
        }

    @app.get("/config")
    def config() -> dict[str, Any]:
        """Where the board should connect. The page reads this at load."""
        return {
            "kanban_url": kanban_url,
            "ws_url": ws_url,
            "columns": COLUMNS,
            "poll_interval_ms": int(os.environ.get("BOARD_POLL_INTERVAL_MS", "4000") or 4000),
            "reconnect_base_ms": int(os.environ.get("BOARD_RECONNECT_BASE_MS", "500") or 500),
        }

    @app.get("/")
    def index() -> Any:
        target = directory / "index.html"
        if not target.is_file():
            return JSONResponse(
                {"error": "board-ui static assets are missing", "expected": str(target)},
                status_code=500,
            )
        return FileResponse(target)

    app.mount("/static", StaticFiles(directory=str(directory)), name="static")

    return app


app = build_app()
