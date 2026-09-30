"""Observability HTTP service: the panels the shell and dashboard read.

Blueprint ref: section 04. One endpoint per panel plus a combined ``/snapshot``
for the live dashboard (and for the shell, which needs all panels at once).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .collector import Collector

STATIC_DIR = Path(__file__).parent / "static"


def build_app(
    *,
    kanban_url: Optional[str] = None,
    tools_url: Optional[str] = None,
    autostart: Optional[bool] = None,
    interval: Optional[float] = None,
    collector: Optional[Collector] = None,
) -> FastAPI:
    kanban_url = kanban_url or os.environ.get("KANBAN_URL", "http://127.0.0.1:8081")
    tools_url = tools_url or os.environ.get("TOOLS_URL", "http://127.0.0.1:8083")
    if interval is None:
        interval = float(os.environ.get("OBS_INTERVAL", "2.0"))
    if autostart is None:
        autostart = os.environ.get("OBS_AUTOSTART", "1") == "1"

    col = collector or Collector(kanban_url=kanban_url, tools_url=tools_url)

    app = FastAPI(
        title="AI-native Kali - Observability",
        version="0.1.0",
        description="Agent timeline, token/cost/latency, tool-call traces, model health and a hash-chained audit log.",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )
    app.state.collector = col

    stop_event = threading.Event()
    state: dict[str, Any] = {"running": False, "audit_cursor": 0}

    def _poll_once() -> dict[str, int]:
        events = col.ingest_kanban_events()
        traces = col.ingest_kanban_traces()
        audit, new_cursor = col.ingest_tool_audit(since_seq=state["audit_cursor"])
        state["audit_cursor"] = new_cursor
        col.last_poll_at = __import__("time").time()
        return {"events": events, "traces": traces, "audit": audit}

    def _loop() -> None:
        state["running"] = True
        while not stop_event.is_set():
            try:
                _poll_once()
            except Exception as exc:  # pragma: no cover - defensive
                col._record_error(f"poll loop: {exc}")
            stop_event.wait(interval)
        state["running"] = False

    @app.on_event("startup")
    def _start() -> None:  # pragma: no cover - exercised by the running service
        if autostart and not state["running"]:
            threading.Thread(target=_loop, name="obs-loop", daemon=True).start()

    @app.on_event("shutdown")
    def _stop() -> None:  # pragma: no cover
        stop_event.set()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "observability",
            "version": "0.1.0",
            "loop_running": state["running"],
            "interval_s": interval,
            "source": col.status(),
        }

    @app.post("/ingest")
    def ingest() -> dict[str, Any]:
        """Force one pull right now - what the smoke test and the UI use."""
        counts = _poll_once()
        return {"ingested": counts, "status": col.status()}

    @app.get("/panels/timeline")
    def timeline(limit: int = 100, agent: Optional[str] = None, card_id: Optional[str] = None) -> dict[str, Any]:
        rows = col.timeline(limit=limit, agent=agent, card_id=card_id)
        return {"panel": "agent_activity_timeline", "items": rows, "count": len(rows)}

    @app.get("/panels/tokens")
    def tokens() -> dict[str, Any]:
        return {"panel": "token_cost_latency", **col.tokens_panel()}

    @app.get("/panels/traces")
    def traces(
        limit: int = 100,
        tool: Optional[str] = None,
        card_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        rows = col.tool_call_traces(limit=limit, tool=tool, card_id=card_id, status=status)
        return {"panel": "tool_call_traces", "items": rows, "count": len(rows)}

    @app.get("/panels/audit")
    def audit(limit: int = 200, tool: Optional[str] = None, card_id: Optional[str] = None) -> dict[str, Any]:
        rows = col.audit_log(limit=limit, tool=tool, card_id=card_id)
        return {"panel": "audit_log", "items": rows, "count": len(rows)}

    @app.get("/panels/alerts")
    def alerts(limit: int = 100) -> dict[str, Any]:
        rows = col.alerts(limit=limit)
        return {"panel": "alerts", "items": rows, "count": len(rows)}

    @app.get("/panels/models")
    def models() -> dict[str, Any]:
        return {"panel": "model_health", "models": col.metrics.model_health()}

    @app.get("/cards/{card_id}/replay")
    def replay(card_id: str) -> dict[str, Any]:
        return col.card_replay(card_id)

    # ------------------------------------------- Phase 5: model inspector
    @app.get("/model/calls")
    def model_calls(
        limit: int = 50,
        card_id: Optional[str] = None,
        ok: Optional[bool] = None,
        redacted_only: bool = False,
    ) -> dict[str, Any]:
        """List captured prompts/responses, newest first - bodies omitted.

        Bodies are only served by the single-call route: a list that carried
        every prompt would push the whole inspector buffer through the dashboard
        on each poll.
        """
        rows = col.model_calls.list(limit=limit, card_id=card_id, ok=ok, redacted_only=redacted_only)
        return {"panel": "model_call_inspector", "items": rows, "count": len(rows), "counts": col.model_calls.counts()}

    @app.get("/model/calls/{call_id}")
    def model_call(call_id: str) -> dict[str, Any]:
        record = col.model_calls.get(call_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no captured call {call_id!r}")
        return record

    @app.post("/model/calls")
    def capture_model_call(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Record one model interaction.

        The runtime posts here (or a test does). Redaction and truncation happen
        inside the store, so a caller cannot bypass them by posting a raw prompt.
        """
        record = col.model_calls.record(
            prompt=str(payload.get("prompt") or ""),
            response=str(payload.get("response") or ""),
            model=str(payload.get("model") or ""),
            backend=str(payload.get("backend") or ""),
            card_id=payload.get("card_id"),
            crew=payload.get("crew"),
            role=payload.get("role"),
            ok=bool(payload.get("ok", True)),
            error=payload.get("error"),
            latency_ms=int(payload.get("latency_ms") or 0),
            prompt_tokens=int(payload.get("prompt_tokens") or 0),
            completion_tokens=int(payload.get("completion_tokens") or 0),
            total_tokens=int(payload.get("total_tokens") or 0),
            cost_usd=float(payload.get("cost_usd") or 0.0),
        )
        return record.as_dict(full=True)

    @app.delete("/model/calls")
    def clear_model_calls() -> dict[str, Any]:
        return {"cleared": col.model_calls.clear()}

    # ------------------------------------------ Phase 5: model heartbeats
    @app.get("/model/health")
    def model_health() -> dict[str, Any]:
        """Current model posture plus the history needed to read it correctly."""
        return {"panel": "model_health_heartbeat", **col.model_probe.status()}

    @app.post("/model/health/beat")
    def model_health_beat() -> dict[str, Any]:
        """Take one heartbeat now (the poll loop does this on its own cadence)."""
        return col.beat_model()

    @app.get("/model/health/history")
    def model_health_history(limit: int = 50) -> dict[str, Any]:
        rows = col.model_probe.history_rows(limit=limit)
        return {"panel": "model_heartbeats", "items": rows, "count": len(rows)}

    # --------------------------------------------- Phase 5: alert routing
    @app.get("/alerting/status")
    def alerting_status() -> dict[str, Any]:
        return {"panel": "alert_routing", **col.alerts_router.status()}

    @app.get("/alerting/notifications")
    def alerting_notifications(limit: int = 50) -> dict[str, Any]:
        """What the shell's notification feed renders."""
        rows = col.alerts_router.notifications(limit=limit)
        return {"panel": "alert_feed", "items": rows, "count": len(rows)}

    @app.get("/alerting/deliveries")
    def alerting_deliveries(limit: int = 50, action: Optional[str] = None) -> dict[str, Any]:
        """Every routing attempt, including the suppressed and the failed ones."""
        rows = col.alerts_router.deliveries(limit=limit, action=action)
        return {"panel": "alert_deliveries", "items": rows, "count": len(rows)}

    @app.post("/alerting/route")
    def alerting_route(limit: int = 100) -> dict[str, Any]:
        """Derive alerts from what has been observed and route them now."""
        return col.route_alerts(limit=limit)

    # ------------------------------------------------ Phase 5: retention
    @app.get("/retention")
    def retention_status() -> dict[str, Any]:
        return {"panel": "retention", "status": col.retention.status(), "plan": col.retention.plan()}

    @app.post("/retention/plan")
    def retention_plan() -> dict[str, Any]:
        """What a sweep *would* remove. Read-only by construction."""
        return col.retention.plan()

    @app.post("/retention/sweep")
    def retention_sweep() -> dict[str, Any]:
        """Apply the policy. Chained stores are refused with a reason."""
        return col.retention.sweep()

    @app.get("/snapshot")
    def snapshot(limit: int = 50) -> dict[str, Any]:
        """Everything the dashboard needs, in one round trip."""
        return {
            "status": col.status(),
            "timeline": col.timeline(limit=limit),
            "traces": col.tool_call_traces(limit=limit),
            "audit": col.audit_log(limit=limit),
            "alerts": col.alerts(limit=limit),
            "tokens": col.tokens_panel(),
            "model_health": col.model_probe.status(),
            "model_calls": col.model_calls.counts(),
            "alert_routing": col.alerts_router.status(),
            "retention": col.retention.status(),
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> Any:
        page = STATIC_DIR / "dashboard.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="dashboard.html is missing from the build")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    return app


app = build_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "observability.server:app",
        host=os.environ.get("OBS_HOST", "127.0.0.1"),
        port=int(os.environ.get("OBS_PORT", "8084")),
        reload=False,
    )
