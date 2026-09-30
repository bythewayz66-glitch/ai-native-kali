"""Agent runtime service: exposes the bridge over HTTP and on a timer.

Blueprint ref: section 03.2. The runtime is the thing that makes cards *move on
their own*. It runs the bridge supervisor in a background thread so `make dev`
needs no separate worker process, and exposes the loop for tests and the smoke
test.

Since Phase 2 the supervisor is **event-driven**: it subscribes to
kanban-core's ``/ws/events`` stream and claims a card the instant it enters
``Assigned``. Polling is retained as the fallback, not as the primary path.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .bridge import Bridge
from .client import KanbanClient, KanbanError
from .crewai_adapter import crewai_available, crewai_version
from .crews import CREWS, get_crew
from .gate import gate_status
from .model_client import model_status
from .roles import ROLE_REGISTRY, get_role
from .ws_events import EventStreamWatcher, websockets_available


class ProcessRequest(BaseModel):
    card_id: Optional[str] = None
    board_id: Optional[str] = None
    limit: int = 20


class InjectRequest(BaseModel):
    """Feed a synthetic bus event into the claim path (debug/testing)."""

    type: str = "card.moved"
    card_id: str
    to_column: str = "Assigned"
    from_column: Optional[str] = None
    board_id: Optional[str] = None
    actor: str = "injected"
    event_id: Optional[str] = None


def _default_tool_executor(tools_url: str) -> Any:
    """Call the tool-frontends service over HTTP (the real path).

    The card's scope and gate state are forwarded with every call: the tool
    layer is the authoritative guardrail, so if they were dropped here the whole
    authorization chain would silently collapse in the deployed topology.
    """
    import httpx

    def _execute(
        tool_name: str,
        args: dict[str, Any],
        *,
        scope: Optional[dict[str, Any]] = None,
        approved: bool = False,
        card_id: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": tool_name, "arguments": args, "approved": approved}
        if scope:
            payload["scope"] = scope
        if card_id:
            # Without this the audit row cannot be joined back to the card.
            payload["card_id"] = card_id
        with httpx.Client(timeout=180) as client:
            response = client.post(f"{tools_url.rstrip('/')}/tools/call", json=payload)
        if response.status_code >= 400:
            return {"tool": tool_name, "status": "error", "stderr": response.text}
        return response.json()

    return _execute


def build_app(
    *,
    kanban_url: Optional[str] = None,
    tools_url: Optional[str] = None,
    tool_executor: Optional[Any] = None,
    autostart: Optional[bool] = None,
    interval: Optional[float] = None,
    ws_url: Optional[str] = None,
    use_stream: Optional[bool] = None,
    watcher: Optional[Any] = None,
) -> FastAPI:
    kanban_url = kanban_url or os.environ.get("KANBAN_URL", "http://127.0.0.1:8081")
    tools_url = tools_url or os.environ.get("TOOLS_URL", "http://127.0.0.1:8083")
    if ws_url is None:
        ws_url = os.environ.get("KANBAN_WS_URL") or kanban_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        ) + "/ws/events"
    if interval is None:
        interval = float(os.environ.get("BRIDGE_INTERVAL", "2.0"))
    if autostart is None:
        autostart = os.environ.get("BRIDGE_AUTOSTART", "1") == "1"
    if use_stream is None:
        use_stream = os.environ.get("BRIDGE_USE_STREAM", "1") == "1"

    executor = tool_executor or _default_tool_executor(tools_url)
    client = KanbanClient(kanban_url)
    bridge = Bridge(client=client, tool_executor=executor)

    app = FastAPI(
        title="AI-native Kali - Agent Runtime",
        version="0.1.0",
        description="CrewAI bridge: cards trigger crews, crews write results back to cards.",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )
    app.state.bridge = bridge
    app.state.client = client

    stop_event = threading.Event()
    state: dict[str, Any] = {
        "running": False,
        "thread": None,
        "watcher": None,
        "last_outcomes": [],
        "mode": "polling",
    }

    def _make_watcher() -> Optional[EventStreamWatcher]:
        """Subscribe to the board's event stream, if we can."""
        if not use_stream:
            return None
        if watcher is not None:
            return watcher
        if not websockets_available():
            return None
        return EventStreamWatcher(ws_url, bridge.process_event)

    def _supervise() -> None:
        """Run the event stream and the polling fallback side by side."""
        state["running"] = True
        stream = state["watcher"]
        if stream is not None:
            stream.start()
            state["mode"] = "event-stream"
        else:
            state["mode"] = "polling"
        try:
            bridge.run_forever(stop_event, watcher=stream, interval=interval)
        except Exception as exc:  # pragma: no cover - defensive
            bridge.stats.errors += 1
            bridge.stats.last_error = str(exc)
        finally:
            if stream is not None:
                stream.stop()
            state["running"] = False

    @app.on_event("startup")
    def _start() -> None:  # pragma: no cover - exercised by the running service
        if autostart and not state["running"]:
            state["watcher"] = _make_watcher()
            thread = threading.Thread(target=_supervise, name="bridge-supervisor", daemon=True)
            state["thread"] = thread
            thread.start()

    @app.on_event("shutdown")
    def _stop() -> None:  # pragma: no cover
        stop_event.set()
        stream = state.get("watcher")
        if stream is not None:
            stream.stop(timeout=2.0)

    @app.get("/health")
    def health() -> dict[str, Any]:
        kanban_ok = True
        audit: dict[str, Any] = {}
        try:
            h = client.health()
            audit = h.get("audit", {})
        except KanbanError:
            kanban_ok = False
        return {
            "status": "ok" if kanban_ok else "degraded",
            "service": "agent-runtime",
            "version": "0.1.0",
            "backend": bridge.adapter.backend,
            "crewai_installed": crewai_available(),
            "crewai_version": crewai_version(),
            "kanban_url": kanban_url,
            "kanban_reachable": kanban_ok,
            "tools_url": tools_url,
            "interval_s": interval,
            "loop_running": state["running"],
            "claim_mode": state["mode"],
            "stream": (
                state["watcher"].status()
                if state.get("watcher") is not None
                else {"enabled": False, "available": websockets_available(), "url": ws_url,
                       "reason": "event stream disabled (BRIDGE_USE_STREAM=0)" if not use_stream else "no websocket client"}
            ),
            "stats": bridge.stats.as_dict(),
            "kanban_audit": audit,
            "model": model_status(),
            "gate": gate_status(),
            # Which engagement this bridge reads memory for. Exposed rather than
            # implied, because a mismatch between the engagement the bridge reads
            # and the one the operator seeded looks exactly like "recall is
            # broken" - and the first thing to check is whether they agree.
            "memory": {
                "enabled": getattr(bridge, "memory", None) is not None,
                "engagement": getattr(bridge, "memory_engagement", None),
            },
        }

    @app.get("/stream")
    def stream_status() -> dict[str, Any]:
        """State of the event stream: the socket is the primary claim path."""
        live = state.get("watcher")
        return {
            "mode": state["mode"],
            "ws_url": ws_url,
            "use_stream": use_stream,
            "websockets_available": websockets_available(),
            "stream": live.status() if live is not None else None,
            "claim_sources": {
                "socket": bridge.stats.socket_claims,
                "poll": bridge.stats.poll_claims,
                "last": bridge.stats.last_claim_source,
            },
        }

    @app.post("/stream/inject")
    def stream_inject(body: InjectRequest) -> dict[str, Any]:
        """Inject a synthetic bus event straight into the claim path.

        Exists so the event-driven path can be exercised end to end without
        depending on socket timing: the smoke test and the tests both use it.
        """
        event: dict[str, Any] = {
            "type": body.type,
            "card_id": body.card_id,
            "to_column": body.to_column,
            "from_column": body.from_column,
            "board_id": body.board_id,
            "actor": body.actor,
        }
        if body.event_id:
            event["event_id"] = body.event_id
        live = state.get("watcher")
        if live is not None:
            live.inject({"kind": "event", "event": event})
            return {"injected": True, "via": "stream", "event": event, "stream": live.status()}
        # No stream configured: dispatch directly through the same handler.
        outcome = bridge.process_event(event)
        return {
            "injected": True,
            "via": "direct",
            "event": event,
            "outcome": outcome.as_dict() if outcome else None,
            "stats": bridge.stats.as_dict(),
        }

    @app.get("/roles")
    def roles() -> dict[str, Any]:
        return {
            "roles": [r.model_dump() for r in ROLE_REGISTRY.values()],
            "count": len(ROLE_REGISTRY),
        }

    @app.get("/roles/{name}")
    def role(name: str) -> dict[str, Any]:
        found = get_role(name)
        if found is None:
            raise HTTPException(status_code=404, detail=f"unknown role '{name}'")
        return found.model_dump()

    @app.get("/crews")
    def crews() -> dict[str, Any]:
        return {"crews": [c.model_dump() for c in CREWS.values()], "count": len(CREWS)}

    @app.get("/crews/{name}")
    def crew_detail(name: str) -> dict[str, Any]:
        found = get_crew(name)
        if found is None:
            raise HTTPException(status_code=404, detail=f"unknown crew '{name}'")
        payload = found.model_dump()
        payload["tools"] = found.tools()
        return payload

    @app.post("/process")
    def process(body: ProcessRequest) -> dict[str, Any]:
        """Run one bridge pass - the entrypoint the smoke test drives."""
        if body.card_id:
            outcome = bridge.process_card(body.card_id)
            return {"outcomes": [outcome.as_dict()], "count": 1, "stats": bridge.stats.as_dict()}
        outcomes = bridge.poll_once(board_id=body.board_id, limit=body.limit)
        state["last_outcomes"] = [o.as_dict() for o in outcomes]
        return {
            "outcomes": [o.as_dict() for o in outcomes],
            "count": len(outcomes),
            "stats": bridge.stats.as_dict(),
        }

    @app.post("/run/{card_id}")
    def run_card(card_id: str) -> dict[str, Any]:
        outcome = bridge.process_card(card_id)
        return outcome.as_dict()

    @app.get("/queue")
    def queue() -> dict[str, Any]:
        try:
            cards = client.agents_queue()
        except KanbanError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return {"queue": cards, "count": len(cards)}

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        return {"stats": bridge.stats.as_dict(), "last_outcomes": state["last_outcomes"]}

    return app


app = build_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "agent_runtime.server:app",
        host=os.environ.get("RUNTIME_HOST", "127.0.0.1"),
        port=int(os.environ.get("RUNTIME_PORT", "8082")),
        reload=False,
    )
