"""Tests for the MCP stdio transport (Phase 2, item 2).

Two levels, because they catch different things:

* **Direct** tests drive :class:`McpToolServer.handle` in-process - fast, and they
  pin the protocol semantics (notifications are never answered, a refusal is a
  result rather than an error, malformed input is a parse error).
* **Subprocess** tests spawn the real ``python -m tool_frontends.mcp_stdio`` and
  talk to it over pipes, which is the only way to prove the wire framing, the
  stdout-discipline rule and the JSON-RPC handshake actually work end to end.

Both assert that the safety machinery is *reused*, not reimplemented: a live call
that is not unlocked must come back denied and be recorded in the audit chain.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from tool_frontends.audit import ToolAuditLog
from tool_frontends.mcp_stdio import (
    DEFAULT_PROTOCOL_VERSION,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_NAME,
    McpToolServer,
)
from tool_frontends.policy import LivePolicy
from tool_frontends.registry import get_registry

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------- fixtures
@pytest.fixture()
def server() -> McpToolServer:
    """A server in strict dry-run mode with an isolated in-memory audit log."""
    return McpToolServer(
        registry=get_registry(),
        audit=ToolAuditLog(":memory:"),
        policy=LivePolicy(live_enabled=False, max_tier=3, unlocked=frozenset()),
    )


def _request(method: str, params: Optional[dict[str, Any]] = None, msg_id: Any = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


# --------------------------------------------------------------- direct API
class TestProtocol:
    def test_initialize_reports_capabilities_and_picks_a_version(self, server):
        result = server.handle(
            _request("initialize", {"protocolVersion": "2024-11-05", "clientInfo": {"name": "t"}})
        )["result"]
        assert result["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == SERVER_NAME
        assert server.initialized is True
        assert server.state["client_info"] == {"name": "t"}

    def test_initialize_falls_back_to_our_version_when_the_client_asks_for_an_unknown_one(self, server):
        result = server.handle(_request("initialize", {"protocolVersion": "9999-01-01"}))["result"]
        assert result["protocolVersion"] == DEFAULT_PROTOCOL_VERSION

    def test_response_is_always_jsonrpc_2_0_with_the_same_id(self, server):
        response = server.handle(_request("ping", msg_id="abc-123"))
        assert response == {"jsonrpc": "2.0", "id": "abc-123", "result": {}}

    def test_notifications_are_never_answered(self, server):
        assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None
        # An unknown *notification* is still silent - answering it would violate JSON-RPC.
        assert server.handle({"jsonrpc": "2.0", "method": "no/such/notification"}) is None

    def test_unknown_method_is_method_not_found(self, server):
        error = server.handle(_request("does/not/exist", msg_id=7))["error"]
        assert error["code"] == METHOD_NOT_FOUND
        assert server.handle(_request("does/not/exist", msg_id=7))["id"] == 7

    def test_bad_json_is_a_parse_error_with_a_null_id(self, server):
        response = server.handle_line("{not json")
        assert response["error"]["code"] == PARSE_ERROR
        assert response["id"] is None

    def test_blank_line_is_ignored(self, server):
        assert server.handle_line("   ") is None

    def test_non_object_request_is_invalid(self, server):
        assert server.handle_line("[1,2,3]")["error"]["code"] == INVALID_REQUEST

    def test_wrong_jsonrpc_version_is_invalid(self, server):
        response = server.handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        assert response["error"]["code"] == INVALID_REQUEST


class TestToolsList:
    def test_lists_every_registered_tool_with_a_schema(self, server):
        tools = server.handle(_request("tools/list"))["result"]["tools"]
        assert len(tools) == len(get_registry().all())
        nmap = next(t for t in tools if t["name"] == "nmap_scan")
        assert nmap["inputSchema"]["type"] == "object"
        assert "target" in nmap["inputSchema"]["properties"]
        assert nmap["annotations"]["tier"] == 1
        assert server.state["list_calls"] == 1

    def test_every_tool_carries_its_tier_and_guardrail_annotations(self, server):
        tools = server.handle(_request("tools/list"))["result"]["tools"]
        for tool in tools:
            ann = tool["annotations"]
            assert ann["tier"] in (0, 1, 2, 3)
            assert isinstance(ann["requires_scope"], bool)
            assert tool["description"].startswith("[T")


class TestToolsCall:
    def test_dry_run_call_executes_nothing_and_reports_the_command(self, server):
        result = server.handle(
            _request(
                "tools/call",
                {"name": "nmap_scan", "arguments": {"target": "scanme.nmap.org", "card_id": "crd_t"}},
            )
        )["result"]
        structured = result["structuredContent"]
        assert structured["status"] == "dry_run"
        assert structured["dry_run"] is True
        assert "nmap" in structured["command"]
        assert result["isError"] is False
        assert structured["audit_hash"], "every call must be audited"
        assert "scanme.nmap.org" in result["content"][0]["text"]
        assert server.state["tool_calls"] == 1

    def test_live_request_without_an_unlock_is_denied_not_downgraded(self, server):
        """The critical safety property: refusal, never a silent dry-run."""
        result = server.handle(
            _request(
                "tools/call",
                {"name": "nmap_scan", "arguments": {"target": "scanme.nmap.org", "live": True}},
            )
        )["result"]
        structured = result["structuredContent"]
        assert structured["status"] == "denied"
        assert structured["allowed"] is False
        assert structured["requested_live"] is True
        assert structured["live_allowed"] is False
        assert any("not unlocked" in r for r in structured["reasons"])
        assert result["isError"] is True, "a refusal is surfaced as a tool error"

    def test_t2_tool_without_scope_is_denied(self, server):
        result = server.handle(
            _request(
                "tools/call",
                {"name": "nikto_scan", "arguments": {"target": "example.com"}},
            )
        )["result"]["structuredContent"]
        assert result["status"] == "denied"
        assert any("authorization scope" in r for r in result["reasons"])

    def test_t2_tool_with_scope_but_no_approval_is_denied(self, server):
        result = server.handle(
            _request(
                "tools/call",
                {
                    "name": "nikto_scan",
                    "arguments": {
                        "target": "example.com",
                        "scope": {
                            "targets": ["example.com"],
                            "authorization_ref": "TICKET-1",
                        },
                    },
                },
            )
        )["result"]["structuredContent"]
        assert result["status"] == "denied"
        assert any("approval" in r for r in result["reasons"])

    def test_unknown_tool_is_a_protocol_error(self, server):
        error = server.handle(_request("tools/call", {"name": "no_such_tool"}))["error"]
        assert error["code"] == INVALID_PARAMS
        assert "no_such_tool" in error["message"]

    def test_missing_name_is_a_protocol_error(self, server):
        error = server.handle(_request("tools/call", {"arguments": {}}))["error"]
        assert error["code"] == INVALID_PARAMS

    def test_control_arguments_are_stripped_before_validation(self, server):
        """`live`/`scope`/`card_id` are call controls, not tool arguments."""
        result = server.handle(
            _request(
                "tools/call",
                {
                    "name": "whois_lookup",
                    "arguments": {
                        "target": "example.com",
                        "__card_id": "crd_x",
                        "__caller": "unit-test",
                    },
                },
            )
        )["result"]["structuredContent"]
        assert result["status"] == "dry_run", f"control keys leaked into validation: {result['reasons']}"

    def test_live_call_succeeds_once_the_policy_unlocks_the_tool(self):
        server = McpToolServer(
            registry=get_registry(),
            audit=ToolAuditLog(":memory:"),
            policy=LivePolicy(live_enabled=True, max_tier=3, unlocked=frozenset({"whois_lookup"})),
        )
        result = server.handle(
            _request(
                "tools/call",
                {"name": "whois_lookup", "arguments": {"target": "example.com", "live": True}},
            )
        )["result"]["structuredContent"]
        # The tool may or may not be installed here; what matters is that the
        # request got past the guardrails into the runner.
        assert result["status"] in ("ok", "error", "blocked")
        assert result["live_allowed"] is True
        assert result["dry_run"] is False

    def test_a_refused_call_is_still_recorded_in_the_audit_chain(self, server):
        server.handle(
            _request(
                "tools/call",
                {"name": "nikto_scan", "arguments": {"target": "example.com"}},
            )
        )
        rows = server.handle(_request("kali/audit/list", {"limit": 10}))["result"]["entries"]
        assert len(rows) == 1
        assert rows[0]["tool"] == "nikto_scan"
        assert rows[0]["status"] == "denied"
        verify = server.handle(_request("kali/audit/verify"))["result"]
        assert verify["ok"] is True


class TestCheckAndStatus:
    def test_check_explains_a_refusal_without_executing(self, server):
        result = server.handle(
            _request(
                "kali/tools/check",
                {"name": "nikto_scan", "arguments": {"target": "example.com", "live": True}},
            )
        )["result"]
        assert result["would_run_live"] is False
        assert result["live_allowed"] is False
        assert result["decision"]["status"] == "denied"
        # A pre-flight must not write an audit row.
        assert server.handle(_request("kali/audit/list"))["result"]["count"] == 0

    def test_status_reports_policy_registry_and_audit(self, server):
        status = server.handle(_request("kali/status"))["result"]
        assert status["server"] == SERVER_NAME
        assert status["policy"]["live_enabled"] is False
        assert status["tools"]["count"] > 0
        assert status["audit"]["ok"] is True


# ------------------------------------------------- subprocess integration
def _spawn(audit_db: str = ":memory:", extra_env: Optional[dict[str, str]] = None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(REPO_ROOT / "tool-frontends"),
            str(REPO_ROOT / "kanban-core"),
            env.get("PYTHONPATH", ""),
        ]
    )
    env["TOOLS_LIVE"] = "0"
    env["TOOLS_UNLOCK"] = ""
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-m", "tool_frontends.mcp_stdio", "--audit-db", audit_db],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


class TestSubprocessTransport:
    def _rpc(self, proc, message: dict[str, Any]) -> dict[str, Any]:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, f"no response; stderr:\n{(proc.stderr.read() if proc.stderr else '')}"
        return json.loads(line)

    def test_full_stdio_session(self, tmp_path):
        audit_db = str(tmp_path / "mcp-audit.db")
        proc = _spawn(audit_db=audit_db)
        try:
            init = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
                },
            )
            assert init["result"]["serverInfo"]["name"] == SERVER_NAME
            # Notification: no id, no response expected.
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()

            listing = self._rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = [t["name"] for t in listing["result"]["tools"]]
            assert "nmap_scan" in names and "whois_lookup" in names

            called = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "dns_lookup", "arguments": {"target": "example.com"}},
                },
            )
            assert called["result"]["structuredContent"]["dry_run"] is True
            assert called["result"]["isError"] is False

            denied = self._rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "nmap_scan", "arguments": {"target": "example.com", "live": True}},
                },
            )
            assert denied["result"]["structuredContent"]["status"] == "denied"
            assert denied["result"]["isError"] is True

            verify = self._rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "kali/audit/verify"})
            assert verify["result"]["ok"] is True
            assert verify["result"]["checked"] == 2

            shutdown = self._rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "shutdown"})
            assert shutdown["result"] is None
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        # The audit chain persisted to a real database file and stayed valid.
        reopened = ToolAuditLog(audit_db)
        try:
            verdict = reopened.verify_chain()
            assert verdict["ok"] is True
            assert verdict["checked"] == 2
            assert reopened.counts()["denied"] == 1
        finally:
            reopened.close()

    def test_stdout_carries_only_jsonrpc(self):
        """A stray print() would corrupt the protocol; prove nothing else leaks."""
        proc = _spawn()
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
            proc.stdin.flush()
            for _ in range(2):
                line = proc.stdout.readline()
                json.loads(line)  # raises if anything non-JSON reached stdout
            # Diagnostics, if any, belong on stderr.
            assert proc.stderr is not None
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    def test_example_client_runs_a_complete_session(self):
        """The shipped example must work as documented - it is the integration reference."""
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(REPO_ROOT / "tool-frontends"),
                str(REPO_ROOT / "kanban-core"),
                env.get("PYTHONPATH", ""),
            ]
        )
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tool-frontends" / "examples" / "mcp_client.py")],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert completed.returncode == 0, f"client failed:\n{completed.stdout}\n{completed.stderr}"
        assert "full MCP session completed" in completed.stdout
        # It must have demonstrated a refusal rather than only the happy path.
        assert "denied" in completed.stdout
        assert "ok      : True" in completed.stdout
