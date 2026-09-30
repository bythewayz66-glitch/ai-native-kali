"""MCP-shaped tool server: list tools, call tools.

Blueprint ref: section 05 - 'CLI wrapper + MCP/tool schema'. This is the surface
an agent (or any MCP client) talks to. It is deliberately the same shape as MCP
(``tools/list``, ``tools/call``) so the CrewAI tool bindings and an external MCP
client use one contract.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from kanban_core.models import Scope

from .audit import ToolAuditLog
from .guardrails import evaluate
from .policy import LivePolicy
from .registry import get_registry
from .runner import SandboxLimits, run_tool
from .spec import TOOL_TIERS

AUDIT_DB = os.environ.get("TOOLS_AUDIT_DB", "var/db/tool-audit.db")
#: Global default: dry-run unless an operator unlocks live execution.
#:
#: The decision itself lives in :mod:`tool_frontends.policy` so this HTTP server
#: and the MCP stdio transport answer the safety question from one place. These
#: module constants are kept for introspection and for the existing tests.
policy = LivePolicy.from_env()
LIVE_ENABLED = policy.live_enabled
UNLOCKED = set(policy.unlocked)
MAX_TIER = policy.max_tier


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    card_id: Optional[str] = None
    caller: str = "agent"
    scope: Optional[dict[str, Any]] = None
    approved: bool = False
    live: bool = False


class GuardCheck(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    scope: Optional[dict[str, Any]] = None
    approved: bool = False
    live: bool = False


def build_app(audit: Optional[ToolAuditLog] = None) -> FastAPI:
    registry = get_registry()
    if audit is None:
        audit = ToolAuditLog(AUDIT_DB)

    app = FastAPI(
        title="AI-native Kali - Tool Frontends",
        version="0.1.0",
        description="MCP-shaped tool layer with T0-T3 guardrails, scope validation and hash-chained audit.",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )
    app.state.audit = audit
    app.state.registry = registry

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "tool-frontends",
            "version": "0.1.0",
            "tools": registry.summary(),
            "live_enabled": LIVE_ENABLED,
            "max_tier": MAX_TIER,
            "unlocked": sorted(UNLOCKED),
            "policy": policy.describe(),
            "audit": audit.verify_chain(),
            "audit_counts": audit.counts(),
        }

    @app.get("/tools")
    def list_tools(category: Optional[str] = None, tier: Optional[int] = None) -> dict[str, Any]:
        tools = registry.all()
        if category:
            tools = [t for t in tools if t.category == category]
        if tier is not None:
            tools = [t for t in tools if t.tier == tier]
        return {
            "tools": [t.model_dump() for t in tools],
            "count": len(tools),
            "tiers": TOOL_TIERS,
        }

    @app.get("/mcp/tools/list")
    def mcp_list() -> dict[str, Any]:
        """MCP `tools/list` shape."""
        return {"tools": registry.mcp_listing()}

    @app.get("/tools/{name}")
    def get_tool(name: str) -> dict[str, Any]:
        spec = registry.get(name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown tool '{name}'")
        return spec.model_dump()

    @app.post("/tools/check")
    def check(body: GuardCheck) -> dict[str, Any]:
        """Dry guardrail evaluation: explains what *would* happen. Never executes."""
        spec = registry.get(body.name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown tool '{name}'")
        scope = Scope.model_validate(body.scope) if body.scope else None
        decision = evaluate(
            spec,
            body.arguments,
            scope=scope,
            approved=body.approved,
            live=body.live,
            live_unlocked=_live_allowed(spec),
        )
        problems = spec.validate_args(body.arguments)
        return {
            "tool": spec.name,
            "tier": spec.tier,
            "decision": decision.as_dict(),
            "argument_problems": problems,
            "would_run_live": bool(decision.allowed and body.live),
        }

    def _live_allowed(spec) -> bool:
        """Whether the operator has unlocked live execution for this tool."""
        return policy.allows(spec)

    @app.post("/tools/call")
    def call_tool(body: ToolCall) -> dict[str, Any]:
        """MCP `tools/call` shape. Dry-run unless live is requested *and* unlocked."""
        spec = registry.get(body.name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown tool '{body.name}'")
        scope = Scope.model_validate(body.scope) if body.scope else None
        want_live = bool(body.live)
        allowed_live = _live_allowed(spec)
        result = run_tool(
            spec,
            body.arguments,
            scope=scope,
            approved=body.approved,
            live=want_live,
            live_unlocked=allowed_live,
            caller=body.caller,
            card_id=body.card_id,
            audit=audit,
            limits=SandboxLimits(timeout_s=spec.timeout_s),
        )
        payload = result.as_dict()
        payload["requested_live"] = want_live
        payload["live_allowed"] = allowed_live
        payload["explain"] = spec.explain
        payload["next_steps"] = spec.next_steps
        return payload

    @app.post("/mcp/tools/call")
    def mcp_call(body: ToolCall) -> dict[str, Any]:
        """MCP-shaped wrapper returning content blocks."""
        result = call_tool(body)
        return {
            "content": [
                {"type": "text", "text": result.get("stdout") or result.get("status", "")},
            ],
            "isError": result.get("status") not in ("ok", "dry_run"),
            "structuredContent": result,
        }

    @app.get("/audit")
    def audit_list(limit: int = 200, tool: Optional[str] = None, card_id: Optional[str] = None) -> dict[str, Any]:
        rows = audit.list(limit=limit, tool=tool, card_id=card_id)
        return {"entries": rows, "count": len(rows), "counts": audit.counts()}

    @app.get("/audit/verify")
    def audit_verify() -> dict[str, Any]:
        return audit.verify_chain()

    @app.post("/intent")
    def resolve(body: dict[str, Any]) -> dict[str, Any]:
        """Lexical intent router: 'scan this subnet' -> candidate tool specs."""
        text = body.get("text", "")
        matches = registry.resolve_intent(text)
        return {
            "text": text,
            "matches": [
                {"name": s.name, "tier": s.tier, "category": s.category, "description": s.description}
                for s in matches[:5]
            ],
        }

    return app


app = build_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "tool_frontends.server:app",
        host=os.environ.get("TOOLS_HOST", "127.0.0.1"),
        port=int(os.environ.get("TOOLS_PORT", "8083")),
        reload=False,
    )
