"""MCP stdio transport: expose the tool layer to any MCP client.

Blueprint ref: section 05 - "integration approach (CLI wrapper + MCP/tool
schema)". The HTTP service already speaks MCP-*shaped* JSON; this module makes
the tool layer an actual **MCP server over stdio**, so an external client (a
desktop AI, an IDE assistant, CrewAI, another agent) can spawn it and drive it
with the standard JSON-RPC 2.0 handshake.

Everything that makes the layer safe is reused unchanged - the same registry, the
same :func:`tool_frontends.guardrails.evaluate`, the same
:class:`tool_frontends.runner.run_tool` and the same hash-chained audit log. There
is no second code path where a guardrail could be forgotten; the only new code
here is protocol framing.

Three protocol details that are easy to get wrong and are therefore explicit:

1. **stdout is the wire.** Anything printed to stdout corrupts the JSON-RPC
   stream, so logs go to stderr and nothing else writes to stdout.
2. **A guardrail denial is not a protocol error.** ``tools/call`` on a refused
   tool returns a normal result with ``isError: true`` and the reasons in the
   content. Using a JSON-RPC error instead would make a *successful* safety
   decision look like a broken server.
3. **Malformed input gets a parse error, not a crash.** One bad line must not
   take the connection down.

Control parameters (``live``, ``approved``, ``scope``, ``card_id``, ``caller``)
travel inside ``arguments`` because MCP has no side channel for them. They are
stripped before argument validation, and ``__``-prefixed aliases are accepted.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Callable, Iterable, Optional, TextIO

from kanban_core.models import Scope

from .audit import ToolAuditLog
from .guardrails import evaluate
from .policy import LivePolicy
from .registry import ToolRegistry, get_registry
from .runner import SandboxLimits, run_tool
from .spec import TOOL_TIERS

#: Protocol revision this server implements. Echoed back, or the client's value
#: when it asks for one we also understand.
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2024-10-07", "2024-06-18")

SERVER_NAME = "ai-native-kali-tool-frontends"
SERVER_VERSION = "0.2.0"

#: Keys in ``arguments`` that configure the *call*, not the tool.
CONTROL_KEYS = frozenset(
    {"live", "approved", "scope", "card_id", "caller", "force_dry_run"}
)

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

log = logging.getLogger("tool_frontends.mcp_stdio")


def _is_notification(message: dict[str, Any]) -> bool:
    """JSON-RPC: no ``id`` means a notification - never answer it."""
    return "id" not in message or message.get("id") is None


class McpToolServer:
    """MCP server logic, transport-agnostic and therefore directly testable.

    :meth:`handle` takes one decoded JSON-RPC message and returns the response
    dict (or ``None`` for notifications). :meth:`serve` wires that to streams.
    """

    def __init__(
        self,
        *,
        registry: Optional[ToolRegistry] = None,
        audit: Optional[ToolAuditLog] = None,
        policy: Optional[LivePolicy] = None,
        limits_factory: Optional[Callable[[Any], SandboxLimits]] = None,
    ) -> None:
        self.registry = registry or get_registry()
        self.audit = audit if audit is not None else ToolAuditLog(":memory:")
        self.policy = policy or LivePolicy.from_env()
        self._limits_factory = limits_factory or (lambda spec: SandboxLimits(timeout_s=spec.timeout_s))
        self.initialized = False
        self.state: dict[str, Any] = {
            "requests": 0,
            "tool_calls": 0,
            "list_calls": 0,
            "errors": 0,
            "client_info": None,
            "protocol_version": DEFAULT_PROTOCOL_VERSION,
        }

    # ------------------------------------------------------------- helpers
    def _live_allowed(self, spec: Any) -> bool:
        return self.policy.allows(spec)

    @staticmethod
    def _split_arguments(arguments: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Separate tool arguments from call-control parameters."""
        raw = dict(arguments) if isinstance(arguments, dict) else {}
        control: dict[str, Any] = {}
        for key in list(raw):
            bare = key[2:] if key.startswith("__") else key
            if bare in CONTROL_KEYS:
                control[bare] = raw.pop(key)
        return raw, control

    def _scope_from(self, control: dict[str, Any]) -> Optional[Scope]:
        raw = control.get("scope")
        if not raw:
            return None
        if isinstance(raw, Scope):
            return raw
        return Scope.model_validate(raw)

    # -------------------------------------------------------------- methods
    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        self.state["protocol_version"] = version
        self.state["client_info"] = params.get("clientInfo")
        self.initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {
                # We expose tools only - honestly, rather than advertising
                # resources/prompts we do not implement.
                "tools": {"listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "AI-native Kali tool frontends. Every tool runs in dry-run mode unless "
                "the operator has enabled live execution for it (TOOLS_LIVE + TOOLS_UNLOCK). "
                "T2+ tools additionally require an authorization scope and a human approval; "
                "refusals are returned as tool errors with reasons."
            ),
        }

    def _tools_list(self) -> dict[str, Any]:
        self.state["list_calls"] += 1
        return {"tools": self.registry.mcp_listing()}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _RpcError(INVALID_PARAMS, "tools/call requires a 'name' string")
        spec = self.registry.get(name)
        if spec is None:
            # An unknown tool is a caller mistake, so it is a protocol error -
            # unlike a *refusal*, which is a successful call with a denial body.
            raise _RpcError(INVALID_PARAMS, f"unknown tool '{name}'")

        arguments, control = self._split_arguments(params.get("arguments"))
        scope = self._scope_from(control)
        want_live = bool(control.get("live", False))
        approved = bool(control.get("approved", False))
        caller = str(control.get("caller") or "mcp-client")
        card_id = control.get("card_id")
        allowed_live = self._live_allowed(spec)

        self.state["tool_calls"] += 1
        result = run_tool(
            spec,
            arguments,
            scope=scope,
            approved=approved,
            live=want_live,
            live_unlocked=allowed_live,
            caller=caller,
            card_id=card_id if isinstance(card_id, str) else None,
            audit=self.audit,
            limits=self._limits_factory(spec),
        )
        payload = result.as_dict()
        payload["requested_live"] = want_live
        payload["live_allowed"] = allowed_live
        payload["explain"] = spec.explain
        payload["next_steps"] = spec.next_steps
        payload["mcp"] = {"server": SERVER_NAME, "version": SERVER_VERSION}

        text = self._render_text(payload)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": payload["status"] not in ("ok", "dry_run"),
            "structuredContent": payload,
        }

    @staticmethod
    def _render_text(payload: dict[str, Any]) -> str:
        """Human/LLM-readable summary of a tool result."""
        status = payload.get("status")
        lines = [
            f"{payload.get('tool')} (T{payload.get('tier')}) -> {status}"
            + (" [dry-run]" if payload.get("dry_run") else " [live]"),
        ]
        if payload.get("command"):
            lines.append(f"command: {payload['command']}")
        if status == "denied":
            lines.append("refused by guardrails:")
            lines.extend(f"  - {r}" for r in (payload.get("reasons") or []))
        else:
            out = (payload.get("stdout") or "").strip()
            if out:
                lines.append("output:")
                lines.append(out[:4000])
            err = (payload.get("stderr") or "").strip()
            if err:
                lines.append("stderr:")
                lines.append(err[:1000])
        if payload.get("audit_hash"):
            lines.append(f"audit: seq {payload.get('audit_seq')} hash {str(payload['audit_hash'])[:16]}...")
        if payload.get("next_steps"):
            lines.append("suggested next steps:")
            lines.extend(f"  - {s}" for s in payload["next_steps"])
        return "\n".join(lines)

    def _tools_check(self, params: dict[str, Any]) -> dict[str, Any]:
        """Guardrail pre-flight: explain what *would* happen. Never executes."""
        name = params.get("name")
        spec = self.registry.get(name) if isinstance(name, str) else None
        if spec is None:
            raise _RpcError(INVALID_PARAMS, f"unknown tool '{name}'")
        arguments, control = self._split_arguments(params.get("arguments"))
        decision = evaluate(
            spec,
            arguments,
            scope=self._scope_from(control),
            approved=bool(control.get("approved", False)),
            live=bool(control.get("live", False)),
            live_unlocked=self._live_allowed(spec),
            caller=str(control.get("caller") or "mcp-client"),
        )
        return {
            "tool": spec.name,
            "tier": spec.tier,
            "decision": decision.as_dict(),
            "argument_problems": spec.validate_args(arguments),
            "would_run_live": bool(decision.allowed and control.get("live")),
            "live_allowed": self._live_allowed(spec),
        }

    def _server_status(self) -> dict[str, Any]:
        return {
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "protocolVersion": self.state["protocol_version"],
            "initialized": self.initialized,
            "tools": self.registry.summary(),
            "policy": self.policy.describe(),
            "tiers": TOOL_TIERS,
            "audit": self.audit.verify_chain(),
            "audit_counts": self.audit.counts(),
            "state": {
                "requests": self.state["requests"],
                "tool_calls": self.state["tool_calls"],
                "list_calls": self.state["list_calls"],
                "errors": self.state["errors"],
                "client_info": self.state["client_info"],
            },
        }

    # -------------------------------------------------------------- dispatch
    def handle(self, message: Any) -> Optional[dict[str, Any]]:
        """Handle one decoded JSON-RPC message; return the response or None."""
        if not isinstance(message, dict):
            return _error_response(None, INVALID_REQUEST, "request must be a JSON object")

        msg_id = message.get("id")
        is_notification = _is_notification(message)
        method = message.get("method")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        if message.get("jsonrpc") not in (None, "2.0"):
            if is_notification:
                return None
            return _error_response(msg_id, INVALID_REQUEST, "jsonrpc must be '2.0'")

        if not isinstance(method, str) or not method:
            if is_notification:
                return None
            return _error_response(msg_id, INVALID_REQUEST, "missing 'method'")

        if not is_notification:
            self.state["requests"] += 1

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method in ("notifications/initialized", "initialized"):
                self.initialized = True
                return None
            elif method in ("notifications/cancelled", "notifications/progress"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._tools_list()
            elif method == "tools/call":
                result = self._tools_call(params)
            elif method == "kali/tools/check":
                result = self._tools_check(params)
            elif method == "kali/audit/list":
                limit = params.get("limit", 200)
                rows = self.audit.list(
                    limit=int(limit) if isinstance(limit, int) else 200,
                    tool=params.get("tool"),
                    card_id=params.get("card_id"),
                )
                result = {"entries": rows, "count": len(rows), "counts": self.audit.counts()}
            elif method == "kali/audit/verify":
                result = self.audit.verify_chain()
            elif method == "kali/status":
                result = self._server_status()
            elif method == "shutdown":
                result = None
                if not is_notification:
                    return {"jsonrpc": "2.0", "id": msg_id, "result": None}
            else:
                if is_notification:
                    return None
                return _error_response(msg_id, METHOD_NOT_FOUND, f"method '{method}' is not supported")
        except _RpcError as exc:
            self.state["errors"] += 1
            if is_notification:
                return None
            return _error_response(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - defensive
            self.state["errors"] += 1
            log.exception("internal error handling %s", method)
            if is_notification:
                return None
            return _error_response(msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def handle_line(self, line: str) -> Optional[dict[str, Any]]:
        """Decode one wire line and handle it. Parse failures are answered, not raised."""
        text = (line or "").strip()
        if not text:
            return None
        try:
            message = json.loads(text)
        except ValueError as exc:
            self.state["errors"] += 1
            return _error_response(None, PARSE_ERROR, f"invalid JSON: {exc}")
        return self.handle(message)

    # --------------------------------------------------------------- serving
    def serve(
        self,
        stdin: Optional[Iterable[str]] = None,
        stdout: Optional[TextIO] = None,
        stderr: Optional[TextIO] = None,
    ) -> int:
        """Read newline-delimited JSON-RPC from *stdin*, write responses to *stdout*.

        Returns a process exit code. Only JSON-RPC goes to stdout; diagnostics go
        to stderr, because a stray ``print`` here would corrupt the protocol.
        """
        stream_in = stdin if stdin is not None else sys.stdin
        stream_out = stdout if stdout is not None else sys.stdout
        stream_err = stderr if stderr is not None else sys.stderr

        for line in stream_in:
            if self._shutdown_requested(line):
                pass  # handled below; kept explicit for readability
            response = self.handle_line(line)
            if response is None:
                # A `shutdown` request returns result:null and ends the session;
                # notifications return None but must NOT end it.
                if self._is_shutdown_line(line):
                    break
                continue
            _write(stream_out, response)
            if self._is_shutdown_line(line):
                break
        return 0

    @staticmethod
    def _is_shutdown_line(line: str) -> bool:
        try:
            message = json.loads((line or "").strip() or "{}")
        except ValueError:
            return False
        return isinstance(message, dict) and message.get("method") == "shutdown"

    def _shutdown_requested(self, line: str) -> bool:  # pragma: no cover - readability hook
        return False


def _write(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, default=str) + "\n")
    stream.flush()


def _error_response(
    msg_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


class _RpcError(Exception):
    """A JSON-RPC-level failure (as opposed to a tool-level refusal)."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - process entry
    """Entrypoint for ``python -m tool_frontends.mcp_stdio``."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    audit_db = None
    args = list(argv if argv is not None else sys.argv[1:])
    if "--audit-db" in args:
        idx = args.index("--audit-db")
        if idx + 1 < len(args):
            audit_db = args[idx + 1]
    audit = ToolAuditLog(audit_db or ":memory:")
    server = McpToolServer(audit=audit)
    log.warning("%s %s ready on stdio (live_enabled=%s)", SERVER_NAME, SERVER_VERSION, server.policy.live_enabled)
    try:
        return server.serve()
    finally:
        audit.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
