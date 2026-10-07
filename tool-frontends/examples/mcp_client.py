#!/usr/bin/env python3
"""Example MCP client: drives the AI-native Kali tool layer over stdio.

This is a real, runnable client - not pseudocode. It spawns the MCP server as a
subprocess and walks a full session: handshake, tool discovery, a dry-run call, a
guardrail pre-flight, a deliberately refused live call, and an audit verification.

Run it from the repo root:

    PYTHONPATH=tool-frontends:kanban-core python3 tool-frontends/examples/mcp_client.py

    # with live execution unlocked for one tool:
    TOOLS_LIVE=1 TOOLS_UNLOCK=whois_lookup \\
      PYTHONPATH=tool-frontends:kanban-core python3 tool-frontends/examples/mcp_client.py

The same three calls are what any MCP-speaking host (an IDE assistant, a desktop
AI, CrewAI) would make, so this file doubles as the integration reference.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]


class McpStdioClient:
    """Minimal MCP client: newline-delimited JSON-RPC 2.0 over a subprocess pipe."""

    def __init__(self, env: Optional[dict[str, str]] = None, audit_db: Optional[str] = None) -> None:
        child_env = dict(os.environ)
        child_env.setdefault("PYTHONPATH", f"{REPO_ROOT / 'tool-frontends'}:{REPO_ROOT / 'kanban-core'}")
        if env:
            child_env.update(env)
        argv = [sys.executable, "-m", "tool_frontends.mcp_stdio"]
        if audit_db:
            argv += ["--audit-db", audit_db]
        self.proc = subprocess.Popen(
            argv,
            cwd=str(REPO_ROOT),
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._next_id = 0

    # ------------------------------------------------------------ protocol
    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        assert self.proc.stdin is not None
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Send a request and block for its matching response."""
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._next_id += 1
        msg_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

        while True:
            line = self.proc.stdout.readline()
            if not line:
                stderr = (self.proc.stderr.read() if self.proc.stderr else "") or ""
                raise RuntimeError(f"server closed the pipe. stderr:\n{stderr}")
            try:
                message = json.loads(line)
            except ValueError:
                continue  # ignore anything that is not JSON
            if message.get("id") == msg_id:
                if "error" in message:
                    raise RuntimeError(f"JSON-RPC error: {message['error']}")
                return message.get("result") or {}

    # ------------------------------------------------------------ lifecycle
    def initialize(self) -> dict[str, Any]:
        result = self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-client-example", "version": "1.0.0"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def close(self) -> None:
        try:
            self.call("shutdown")
        except Exception:
            pass
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def main() -> int:
    audit_db = os.environ.get("MCP_AUDIT_DB", ":memory:")
    client = McpStdioClient(audit_db=audit_db)
    try:
        print("=" * 72)
        print("1. initialize")
        info = client.initialize()
        server = info.get("serverInfo", {})
        print(f"   server   : {server.get('name')} v{server.get('version')}")
        print(f"   protocol : {info.get('protocolVersion')}")
        print(f"   tools    : {'yes' if 'tools' in info.get('capabilities', {}) else 'no'}")

        print("=" * 72)
        print("2. tools/list")
        listing = client.call("tools/list")
        tools = listing.get("tools", [])
        print(f"   {len(tools)} tools exposed")
        for tool in tools:
            ann = tool.get("annotations") or {}
            schema = tool.get("inputSchema") or {}
            print(
                f"     - {tool['name']:<16} T{ann.get('tier')}  {ann.get('category','')}"
                f"  params={list(schema.get('properties', {}))}"
            )

        print("=" * 72)
        print("3. kali/status")
        status = client.call("kali/status")
        policy = status.get("policy", {})
        print(f"   live_enabled : {policy.get('live_enabled')}")
        print(f"   max_tier     : {policy.get('max_tier')}")
        print(f"   unlocked     : {policy.get('unlocked') or '(none)'}")
        print(f"   audit chain  : {status.get('audit')}")

        print("=" * 72)
        print("4. tools/call - safe dry-run (nmap)")
        call = client.call(
            "tools/call",
            {
                "name": "nmap_scan",
                "arguments": {
                    "target": "scanme.nmap.org",
                    # ``nmap_scan`` declares ``ports`` - passing ``profile`` used
                    # to make this call come back *denied* for an unknown
                    # parameter rather than showing the dry-run command, which
                    # read like a guardrail refusal but was a client bug.
                    "ports": "top100",
                    "card_id": "crd_example",
                    "caller": "example-client",
                },
            },
        )
        structured = call.get("structuredContent", {})
        print(f"   isError  : {call.get('isError')}")
        print(f"   status   : {structured.get('status')}")
        print(f"   dry_run  : {structured.get('dry_run')}")
        print(f"   command  : {structured.get('command')}")
        print(f"   audit    : seq={structured.get('audit_seq')} hash={str(structured.get('audit_hash'))[:16]}...")
        print("   text block:")
        for line in (call.get("content") or [{}])[0].get("text", "").splitlines():
            print(f"     | {line}")

        print("=" * 72)
        print("5. kali/tools/check - explain a refusal without executing")
        check = client.call(
            "kali/tools/check",
            {
                "name": "nikto_scan",
                "arguments": {"target": "example.com", "live": True},
            },
        )
        print(f"   would_run_live : {check.get('would_run_live')}")
        print(f"   decision       : {check.get('decision', {}).get('status')}")
        for reason in check.get("decision", {}).get("reasons", []):
            print(f"     - {reason}")

        print("=" * 72)
        print("6. tools/call - request live execution (expect a guardrail refusal)")
        refused = client.call(
            "tools/call",
            {"name": "nikto_scan", "arguments": {"target": "example.com", "live": True}},
        )
        body = refused.get("structuredContent", {})
        print(f"   isError : {refused.get('isError')}")
        print(f"   status  : {body.get('status')}")
        for reason in body.get("reasons", []):
            print(f"     - {reason}")

        print("=" * 72)
        print("7. kali/audit/verify - the hash chain must be intact")
        verify = client.call("kali/audit/verify")
        print(f"   ok      : {verify.get('ok')}")
        print(f"   checked : {verify.get('checked')} entries")
        print(f"   head    : {str(verify.get('head_hash'))[:24]}...")

        audit = client.call("kali/audit/list", {"limit": 10})
        print(f"   audit rows recorded this session: {audit.get('count')}")

        print("=" * 72)
        print("OK - full MCP session completed")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
