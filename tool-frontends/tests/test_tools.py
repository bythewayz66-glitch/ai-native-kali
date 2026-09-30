"""Tool frontend tests: guardrails, tiers, scope, sandbox, audit chain."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kanban_core.models import Scope
from tool_frontends.audit import GENESIS, ToolAuditLog
from tool_frontends.guardrails import evaluate
from tool_frontends.registry import ToolRegistry, get_registry
from tool_frontends.runner import run_tool
from tool_frontends.server import build_app
from tool_frontends.spec import ToolSpec
from tool_frontends.wrappers import BUILTIN_TOOLS, register_builtin


@pytest.fixture()
def audit(tmp_path):
    return ToolAuditLog(tmp_path / "tool-audit.db")


@pytest.fixture()
def reg():
    registry = ToolRegistry()
    register_builtin(registry)
    return registry


@pytest.fixture()
def client(tmp_path):
    with TestClient(build_app(ToolAuditLog(tmp_path / "api-audit.db"))) as c:
        yield c


SCOPE = Scope(
    targets=["scanme.nmap.org", "example.com"],
    cidrs=["192.0.2.0/24"],
    authorization_ref="ACME-SOW-2026-0912",
)


# ------------------------------------------------------------ registry
class TestRegistry:
    def test_builtins_registered(self, reg):
        names = {s.name for s in reg.all()}
        assert {"whois_lookup", "dns_lookup", "nmap_scan", "httpx_probe", "nikto_scan", "sqlmap_test"} <= names

    def test_tier_spread_covers_all_four(self, reg):
        for tier in range(4):
            assert reg.by_tier(tier), f"no tool at tier {tier}"

    def test_duplicate_registration_rejected(self, reg):
        with pytest.raises(ValueError):
            reg.register(BUILTIN_TOOLS[0])

    def test_t2_requires_scope_flag(self):
        registry = ToolRegistry()
        with pytest.raises(ValueError, match="requires_scope"):
            registry.register(
                ToolSpec(
                    name="bad",
                    binary="x",
                    category="exploitation",
                    tier=2,
                    dry_run_template="x",
                    live_template="x",
                )
            )

    def test_t3_requires_sandbox_flag(self):
        registry = ToolRegistry()
        with pytest.raises(ValueError, match="requires_sandbox"):
            registry.register(
                ToolSpec(
                    name="bad3",
                    binary="x",
                    category="exploitation",
                    tier=3,
                    dry_run_template="x",
                    live_template="x",
                    requires_scope=True,
                )
            )

    def test_every_tool_has_a_dry_run_template(self, reg):
        assert all(s.dry_run_template for s in reg.all())

    def test_intent_router(self, reg):
        assert reg.resolve_intent("please scan the subnet for open ports")[0].name == "nmap_scan"
        assert reg.resolve_intent("whois lookup for the domain")[0].name == "whois_lookup"
        assert reg.resolve_intent("resolve the domain please")[0].name == "dns_lookup"
        assert reg.resolve_intent("test this parameter for sql injection")[0].name == "sqlmap_test"
        assert reg.resolve_intent("unrelated chatter") == []

    def test_mcp_listing_shape(self, reg):
        listing = reg.mcp_listing()
        assert len(listing) == len(reg.all())
        one = next(t for t in listing if t["name"] == "nmap_scan")
        assert one["inputSchema"]["type"] == "object"
        assert "target" in one["inputSchema"]["properties"]
        assert "target" in one["inputSchema"]["required"]
        assert one["annotations"]["tier"] == 1


# ----------------------------------------------------------- arg validation
class TestArgumentValidation:
    def test_unknown_param_rejected(self, reg):
        assert any("unknown parameter" in p for p in reg.require("whois_lookup").validate_args({"bogus": 1}))

    def test_missing_required_rejected(self, reg):
        assert any("missing required" in p for p in reg.require("whois_lookup").validate_args({}))

    def test_type_mismatch_rejected(self, reg):
        assert any("integer" in p for p in reg.require("sqlmap_test").validate_args({"target": "http://x", "level": "deep"}))

    def test_enum_choice_enforced(self, reg):
        problems = reg.require("dns_lookup").validate_args({"target": "a.com", "record_type": "PTR"})
        assert any("must be one of" in p for p in problems)

    def test_valid_args_pass(self, reg):
        assert reg.require("dns_lookup").validate_args({"target": "a.com", "record_type": "MX"}) == []

    def test_defaults_applied(self, reg):
        assert reg.require("dns_lookup").defaults()["record_type"] == "A"

    def test_sanitizer_strips_shell_metacharacters(self, reg):
        spec = reg.require("whois_lookup")
        rendered = spec.render(spec.dry_run_template, {"target": "evil.com; rm -rf /"})
        assert ";" not in rendered
        assert " " not in rendered.split("whois ", 1)[1]  # no extra argv token
        assert rendered == "whois evil.comrm-rf/"

    def test_sanitizer_blocks_command_substitution(self, reg):
        spec = reg.require("whois_lookup")
        rendered = spec.render(spec.dry_run_template, {"target": "$(whoami)"})
        assert "$" not in rendered and "(" not in rendered and ")" not in rendered

    def test_missing_required_breaks_render(self, reg):
        spec = reg.require("whois_lookup")
        from tool_frontends.spec import ParamSpec

        strict = ToolSpec(
            name="strict",
            binary="x",
            category="system",
            dry_run_template="x {must}",
            params=[ParamSpec(name="must", type="string")],
        )
        with pytest.raises(ValueError):
            strict.render(strict.dry_run_template, {})


# --------------------------------------------------------------- guardrails
class TestGuardrails:
    def test_t0_allowed_without_scope(self, reg):
        d = evaluate(reg.require("whois_lookup"), {"target": "example.com"})
        assert d.allowed and d.status == "allowed"

    def test_t1_requires_a_target(self, reg):
        d = evaluate(reg.require("nmap_scan"), {})
        assert not d.allowed
        assert any("no target" in r for r in d.reasons)

    def test_t1_allowed_in_scope_without_approval(self, reg):
        d = evaluate(reg.require("nmap_scan"), {"target": "scanme.nmap.org"}, scope=SCOPE)
        assert d.allowed, d.reasons

    def test_t2_denied_without_scope(self, reg):
        d = evaluate(reg.require("nikto_scan"), {"target": "example.com"})
        assert not d.allowed
        assert any("authorization scope" in r for r in d.reasons)

    def test_t2_denied_out_of_scope(self, reg):
        d = evaluate(reg.require("nikto_scan"), {"target": "evil.net"}, scope=SCOPE, approved=True)
        assert not d.allowed
        assert any("outside the authorized scope" in r for r in d.reasons)

    def test_t2_denied_expired_scope(self, reg):
        expired = Scope(targets=["example.com"], authorization_ref="X", expires_at="2020-01-01T00:00:00+00:00")
        d = evaluate(reg.require("nikto_scan"), {"target": "example.com"}, scope=expired, approved=True)
        assert not d.allowed
        assert any("expired" in r for r in d.reasons)

    def test_t2_denied_without_authorization_ref(self, reg):
        no_ref = Scope(targets=["example.com"])
        d = evaluate(reg.require("nikto_scan"), {"target": "example.com"}, scope=no_ref, approved=True)
        assert not d.allowed
        assert any("authorization_ref" in r for r in d.reasons)

    def test_t2_denied_without_approval(self, reg):
        d = evaluate(reg.require("nikto_scan"), {"target": "example.com"}, scope=SCOPE)
        assert not d.allowed
        assert d.status == "needs_approval"

    def test_t2_allowed_with_scope_and_approval(self, reg):
        d = evaluate(reg.require("nikto_scan"), {"target": "example.com"}, scope=SCOPE, approved=True)
        assert d.allowed, d.reasons

    def test_t3_needs_scope_approval_and_sandbox(self, reg):
        spec = reg.require("sqlmap_test")
        assert spec.requires_sandbox
        d = evaluate(spec, {"target": "http://example.com/?id=1"}, scope=SCOPE, approved=False)
        assert not d.allowed
        d2 = evaluate(spec, {"target": "http://example.com/?id=1"}, scope=SCOPE, approved=True)
        assert d2.allowed, d2.reasons

    def test_t3_scope_applies_to_embedded_host(self, reg):
        d = evaluate(
            reg.require("sqlmap_test"),
            {"target": "http://evil.net/?id=1"},
            scope=SCOPE,
            approved=True,
        )
        assert not d.allowed
        assert any("outside the authorized scope" in r for r in d.reasons)

    def test_live_requires_unlock(self, reg):
        d = evaluate(
            reg.require("whois_lookup"), {"target": "example.com"}, live=True, live_unlocked=False
        )
        assert not d.allowed
        assert any("not unlocked" in r for r in d.reasons)

    def test_live_allowed_when_unlocked(self, reg):
        d = evaluate(
            reg.require("whois_lookup"), {"target": "example.com"}, live=True, live_unlocked=True
        )
        assert d.allowed, d.reasons

    def test_unknown_tier_denied(self, reg):
        spec = ToolSpec(name="weird", binary="x", category="system", tier=3, requires_sandbox=True,
                        dry_run_template="x", live_template="x")
        spec.tier = 9
        d = evaluate(spec, {})
        assert not d.allowed


# ------------------------------------------------------------------- runner
class TestRunner:
    def test_dry_run_is_the_default_and_executes_nothing(self, reg, audit):
        result = run_tool(reg.require("nmap_scan"), {"target": "scanme.nmap.org"}, scope=SCOPE, audit=audit)
        assert result.status == "dry_run"
        assert result.dry_run is True
        assert result.allowed is True
        assert result.exit_code is None
        assert "nmap" in result.command
        assert "[dry-run]" in result.stdout

    def test_dry_run_command_is_recorded_in_audit(self, reg, audit):
        run_tool(reg.require("dns_lookup"), {"target": "example.com"}, audit=audit)
        rows = audit.list()
        assert len(rows) == 1
        assert rows[0]["tool"] == "dns_lookup"
        assert rows[0]["status"] == "dry_run"
        assert rows[0]["dry_run"] is True
        assert rows[0]["command"].startswith("dig")
        assert rows[0]["audit_hash"]

    def test_denied_run_is_audited_and_not_executed(self, reg, audit):
        result = run_tool(reg.require("sqlmap_test"), {"target": "http://evil.net/?id=1"}, scope=SCOPE, audit=audit)
        assert result.status == "denied"
        assert not result.allowed
        rows = audit.list()
        assert rows[0]["status"] == "denied"
        assert rows[0]["reasons"]

    def test_live_request_without_unlock_is_refused_not_downgraded(self, reg, audit):
        """A refused live run must be an explicit denial, never a silent dry run.

        If it were downgraded, every downstream consumer (card trace, audit log,
        shell notification) would report a scan that never happened.
        """
        result = run_tool(
            reg.require("dns_lookup"), {"target": "example.com"}, live=True, live_unlocked=False, audit=audit
        )
        assert result.status == "denied"
        assert result.allowed is False
        assert result.stdout == ""  # nothing executed
        assert any("not unlocked" in r for r in result.reasons)
        assert audit.list()[0]["status"] == "denied"

    def test_missing_binary_is_reported_not_faked(self, reg, audit):
        spec = ToolSpec(
            name="nonexistent_tool",
            binary="definitely-not-installed-xyz",
            category="system",
            tier=0,
            dry_run_template="definitely-not-installed-xyz --version",
            live_template="definitely-not-installed-xyz --version",
        )
        result = run_tool(spec, {}, live=True, live_unlocked=True, audit=audit)
        assert result.available is False
        assert result.status == "blocked"
        assert "not installed" in result.stderr

    def test_live_run_of_a_real_binary(self, reg, audit):
        spec = ToolSpec(
            name="true_check",
            binary="true",
            category="system",
            tier=0,
            dry_run_template="true",
            live_template="true",
        )
        result = run_tool(spec, {}, live=True, live_unlocked=True, audit=audit)
        assert result.status == "ok"
        assert result.exit_code == 0
        assert result.dry_run is False

    def test_live_run_captures_nonzero_exit(self, reg, audit):
        spec = ToolSpec(
            name="false_check",
            binary="false",
            category="system",
            tier=0,
            dry_run_template="false",
            live_template="false",
        )
        result = run_tool(spec, {}, live=True, live_unlocked=True, audit=audit)
        assert result.status == "error"
        assert result.exit_code == 1

    def test_live_run_never_uses_a_shell(self, reg, audit):
        """A metacharacter must never reach a shell or spawn a second command."""
        spec = ToolSpec(
            name="echo_check",
            binary="echo",
            category="system",
            tier=0,
            params=[{"name": "msg", "type": "string", "default": "hi"}],
            dry_run_template="echo {msg}",
            live_template="echo {msg}",
        )
        result = run_tool(spec, {"msg": "a;whoami"}, live=True, live_unlocked=True, audit=audit)
        assert result.status == "ok"
        # The semicolon is stripped by the sanitiser AND the runner never uses a
        # shell, so `whoami` never becomes a second command.
        assert "root" not in result.stdout
        assert result.stdout.strip() == "awhoami"

    def test_value_cannot_smuggle_extra_arguments(self, reg, audit):
        """A value containing spaces must not expand into additional argv tokens."""
        spec = ToolSpec(
            name="args_check",
            binary="echo",
            category="system",
            tier=0,
            params=[{"name": "target", "type": "string", "default": "x"}],
            dry_run_template="echo {target}",
            live_template="echo {target}",
        )
        result = run_tool(
            spec, {"target": "example.com -oN /etc/passwd"}, live=True, live_unlocked=True, audit=audit
        )
        assert result.status == "ok"
        assert result.stdout.split() == ["example.com-oN/etc/passwd"]

    def test_trace_shape_for_kanban(self, reg, audit):
        result = run_tool(reg.require("nmap_scan"), {"target": "scanme.nmap.org"}, scope=SCOPE, audit=audit)
        trace = result.to_trace(agent="recon-specialist")
        assert trace["tool"] == "nmap_scan"
        assert trace["tier"] == 1
        assert trace["status"] == "ok"
        assert trace["dry_run"] is True
        assert trace["agent"] == "recon-specialist"
        assert trace["audit_hash"]

    def test_argument_problems_block_the_run(self, reg, audit):
        result = run_tool(reg.require("whois_lookup"), {"bogus": "x"}, audit=audit)
        assert result.status == "denied"
        assert any("unknown parameter" in r for r in result.reasons)


# ---------------------------------------------------------------- audit log
class TestAuditLog:
    def test_chain_starts_at_genesis(self, audit):
        audit.append(ts="t", tool="x", tier=0, status="ok", dry_run=True)
        rows = audit.list()
        assert rows[0]["prev_hash"] == GENESIS
        assert audit.verify_chain()["ok"]

    def test_chain_verifies_over_many_rows(self, audit, reg):
        for tool in ("whois_lookup", "dns_lookup"):
            run_tool(reg.require(tool), {"target": "example.com"}, audit=audit)
        verdict = audit.verify_chain()
        assert verdict["ok"] and verdict["checked"] == 2

    def test_tampering_detected(self, audit, reg):
        run_tool(reg.require("whois_lookup"), {"target": "example.com"}, audit=audit)
        run_tool(reg.require("dns_lookup"), {"target": "example.com"}, audit=audit)
        assert audit.verify_chain()["ok"]
        audit._conn.execute("UPDATE tool_audit SET tool='tampered' WHERE seq=1")
        audit._conn.commit()
        verdict = audit.verify_chain()
        assert verdict["ok"] is False and verdict["broken_at_seq"] == 1

    def test_counts_by_status(self, audit, reg):
        run_tool(reg.require("whois_lookup"), {"target": "example.com"}, audit=audit)  # dry_run
        run_tool(reg.require("sqlmap_test"), {"target": "http://evil.net"}, audit=audit)  # denied
        counts = audit.counts()
        assert counts.get("dry_run") == 1
        assert counts.get("denied") == 1
        assert counts["total"] == 2

    def test_filter_by_tool_and_card(self, audit):
        audit.append(ts="t", tool="a", tier=0, status="ok", dry_run=True, card_id="c1")
        audit.append(ts="t", tool="b", tier=0, status="ok", dry_run=True, card_id="c2")
        assert len(audit.list(tool="a")) == 1
        assert len(audit.list(card_id="c2")) == 1


# --------------------------------------------------------------- HTTP server
class TestToolServer:
    def test_health_reports_tools_and_chain(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["tools"]["count"] >= 7
        assert body["tools"]["by_tier"]["3"] >= 1
        assert body["audit"]["ok"] is True
        assert body["live_enabled"] is False

    def test_list_tools_and_filter(self, client):
        assert client.get("/tools").json()["count"] >= 7
        assert client.get("/tools", params={"tier": 2}).json()["count"] >= 1
        webs = client.get("/tools", params={"category": "web-application"}).json()["tools"]
        assert {t["name"] for t in webs} >= {"httpx_probe", "nikto_scan"}

    def test_get_single_tool_and_404(self, client):
        assert client.get("/tools/nmap_scan").json()["tier"] == 1
        assert client.get("/tools/nope").status_code == 404

    def test_mcp_tools_list_shape(self, client):
        body = client.get("/mcp/tools/list").json()
        assert body["tools"]
        assert "inputSchema" in body["tools"][0]

    def test_call_in_dry_run_mode(self, client):
        body = client.post("/tools/call", json={"name": "dns_lookup", "arguments": {"target": "example.com"}}).json()
        assert body["status"] == "dry_run"
        assert body["dry_run"] is True
        assert body["requested_live"] is False
        assert body["audit_hash"]
        assert body["next_steps"]

    def test_call_live_request_is_refused_while_locked(self, client):
        body = client.post(
            "/tools/call", json={"name": "dns_lookup", "arguments": {"target": "example.com"}, "live": True}
        ).json()
        assert body["status"] == "denied"
        assert body["requested_live"] is True
        assert body["live_allowed"] is False

    def test_call_t3_denied_without_approval(self, client):
        body = client.post(
            "/tools/call",
            json={
                "name": "sqlmap_test",
                "arguments": {"target": "http://example.com/?id=1"},
                "scope": {"targets": ["example.com"], "authorization_ref": "X"},
            },
        ).json()
        assert body["status"] == "denied"
        assert any("approval" in r for r in body["reasons"])

    def test_call_t3_allowed_with_scope_and_approval(self, client):
        body = client.post(
            "/tools/call",
            json={
                "name": "sqlmap_test",
                "arguments": {"target": "http://example.com/?id=1"},
                "scope": {"targets": ["example.com"], "authorization_ref": "X"},
                "approved": True,
            },
        ).json()
        assert body["status"] == "dry_run"
        assert body["allowed"] is True

    def test_check_endpoint_explains_without_executing(self, client):
        before = client.get("/audit").json()["count"]
        body = client.post(
            "/tools/check",
            json={"name": "nikto_scan", "arguments": {"target": "evil.net"}, "scope": {"targets": ["example.com"], "authorization_ref": "X"}},
        ).json()
        assert body["decision"]["allowed"] is False
        assert any("outside the authorized scope" in r for r in body["decision"]["reasons"])
        assert client.get("/audit").json()["count"] == before  # nothing written

    def test_audit_endpoint_and_verify(self, client):
        client.post("/tools/call", json={"name": "whois_lookup", "arguments": {"target": "example.com"}})
        listed = client.get("/audit").json()
        assert listed["count"] >= 1
        assert listed["counts"]["total"] >= 1
        assert client.get("/audit/verify").json()["ok"] is True

    def test_mcp_call_returns_content_blocks(self, client):
        body = client.post(
            "/mcp/tools/call", json={"name": "dns_lookup", "arguments": {"target": "example.com"}}
        ).json()
        assert body["isError"] is False
        assert body["content"][0]["type"] == "text"
        assert body["structuredContent"]["status"] == "dry_run"

    def test_intent_endpoint(self, client):
        body = client.post("/intent", json={"text": "scan the host for open ports"}).json()
        assert body["matches"][0]["name"] == "nmap_scan"

    def test_unknown_tool_call_404(self, client):
        assert client.post("/tools/call", json={"name": "nope"}).status_code == 404
