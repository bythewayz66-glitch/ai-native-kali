"""Agent runtime tests: crews, roles, adapter and the bridge loop.

The bridge tests run against a REAL kanban-core (in-process TestClient) and the
real tool registry, so they exercise the actual loop rather than a mock of it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_runtime.bridge import Bridge
from agent_runtime.client import KanbanClient, KanbanError
from agent_runtime.crewai_adapter import CrewAIAdapter, crewai_available
from agent_runtime.crews import CREWS, crew_for_board, get_crew
from agent_runtime.roles import ROLE_REGISTRY, get_role
from kanban_core.api import build_app as build_kanban_app
from kanban_core.bus import EventBus
from kanban_core.store import Store
from tool_frontends.audit import ToolAuditLog
from tool_frontends.registry import ToolRegistry
from tool_frontends.runner import run_tool
from tool_frontends.wrappers import register_builtin as register_tools


# ------------------------------------------------------------- fixtures
@pytest.fixture()
def kanban(tmp_path):
    """A real kanban-core app with seeded boards."""
    import os

    os.environ["KANBAN_SEED"] = "1"
    app = build_kanban_app(Store(tmp_path / "kanban.db"), EventBus())
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def tools(tmp_path):
    """A real tool registry with its own audit log."""
    registry = ToolRegistry()
    register_tools(registry)
    audit = ToolAuditLog(tmp_path / "tools.db")
    return registry, audit


def in_process_executor(registry, audit):
    """Tool executor that runs the real guardrailed tool layer in-process."""

    def _execute(tool_name: str, args: dict, *, scope: dict | None = None, approved: bool = False, card_id: str | None = None) -> dict:
        spec = registry.get(tool_name)
        if spec is None:
            return {"tool": tool_name, "status": "error", "reason": f"unknown tool {tool_name}"}
        result = run_tool(spec, args, scope=scope, approved=approved, card_id=card_id, audit=audit)
        return result.as_dict()

    return _execute


class _ClientShim:
    """Adapts a FastAPI TestClient to the KanbanClient interface.

    Lets the bridge run against an in-process kanban-core with no sockets, while
    still going through the real HTTP routes and therefore the real guards.
    """

    def __init__(self, test_client: TestClient) -> None:
        self._c = test_client
        self.base_url = "in-process"

    def get(self, path: str, **params):
        response = self._c.get(path, params=params or None)
        if response.status_code >= 400:
            raise KanbanError(f"GET {path} -> {response.status_code}", response.status_code, response.json())
        return response.json()

    def post(self, path: str, payload=None):
        response = self._c.post(path, json=payload or {})
        if response.status_code >= 400:
            detail = response.json().get("detail")
            raise KanbanError(f"POST {path} -> {response.status_code}", response.status_code, detail)
        return response.json()

    def card(self, card_id: str) -> dict:
        return self.get(f"/api/cards/{card_id}")

    def agents_queue(self) -> list[dict]:
        return self.get("/api/cards", column="Assigned")["cards"]

    def move(self, card_id, to_column, *, actor="bridge", actor_is_agent=True, force=False, note=""):
        return self.post(
            f"/api/cards/{card_id}/move",
            {"to_column": to_column, "actor": actor, "actor_is_agent": actor_is_agent, "force": force, "note": note},
        )

    def add_trace(self, card_id, trace):
        return self.post(f"/api/cards/{card_id}/traces", trace)

    def add_artifact(self, card_id, artifact):
        return self.post(f"/api/cards/{card_id}/artifacts", artifact)

    def set_result(self, card_id, result, *, actor="bridge"):
        return self.post(f"/api/cards/{card_id}/result", {"result": result, "actor": actor})

    def request_approval(self, card_id, *, reason, requested_by, tool=None, tier=None):
        payload = {"reason": reason, "requested_by": requested_by}
        if tool:
            payload["tool"] = tool
        if tier is not None:
            payload["tier"] = tier
        return self.post(f"/api/cards/{card_id}/approvals", payload)

    def block(self, card_id, reason, *, actor="bridge"):
        return self.post(f"/api/cards/{card_id}/block", {"reason": reason, "actor": actor})

    def create_subcard(self, parent_id, **payload):
        return self.post(f"/api/cards/{parent_id}/subcards", payload)

    def health(self):
        return self.get("/health")


@pytest.fixture()
def bridge(kanban, tools):
    registry, audit = tools
    return Bridge(
        client=_ClientShim(kanban),
        tool_executor=in_process_executor(registry, audit),
    )


# ---------------------------------------------------------------- roles
class TestRoles:
    def test_registry_not_empty(self):
        assert len(ROLE_REGISTRY) >= 6
        assert "recon-specialist" in ROLE_REGISTRY

    def test_role_tier_ceiling_enforced(self):
        recon = get_role("recon-specialist")
        assert recon.may_use("nmap_scan", 1)
        assert not recon.may_use("nikto_scan", 2)  # T2 beyond a T1 role
        assert not recon.may_use("sqlmap_test", 3)

    def test_web_specialist_may_use_t2(self):
        web = get_role("web-specialist")
        assert web.may_use("nikto_scan", 2)
        assert not web.may_use("sqlmap_test", 3)

    def test_unknown_role_is_none(self):
        assert get_role("nobody") is None


# ---------------------------------------------------------------- crews
class TestCrews:
    def test_recon_crew_shape(self):
        crew = get_crew("recon")
        assert crew.display_name == "Recon Crew"
        assert crew.roles == ["recon-specialist"]
        assert "nmap_scan" in crew.tools()
        assert crew.max_tier == 1

    def test_vuln_crew_has_two_steps(self):
        crew = get_crew("vuln-assessment")
        assert crew.roles == ["web-specialist", "vuln-analyst"]
        assert crew.max_tier == 2

    def test_crew_tools_are_deduplicated(self):
        crew = get_crew("vuln-assessment")
        assert len(crew.tools()) == len(set(crew.tools()))

    def test_crew_for_board_kind(self):
        assert crew_for_board("engagement").name == "recon"
        assert crew_for_board("system").name == "system"

    def test_unknown_crew_is_none(self):
        assert get_crew("nope") is None


# -------------------------------------------------------------- adapter
class TestAdapter:
    def test_backend_is_local_without_crewai(self, tools):
        registry, audit = tools
        adapter = CrewAIAdapter(tool_executor=in_process_executor(registry, audit))
        expected = "crewai" if crewai_available() else "local"
        assert adapter.backend == expected

    def test_local_run_executes_bound_tools(self, tools):
        registry, audit = tools
        adapter = CrewAIAdapter(tool_executor=in_process_executor(registry, audit), prefer_real=False)
        card = {
            "title": "Recon: scanme.nmap.org",
            "description": "recon the host",
            "tools": [
                {"name": "whois_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
                {"name": "dns_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
            ],
        }
        run = adapter.run(get_crew("recon"), card, role_lookup=get_role)
        assert run.backend == "local"
        assert run.status == "ok"
        assert len(run.steps) == 1
        assert len(run.steps[0].tool_calls) == 2
        assert all(c["status"] == "dry_run" for c in run.steps[0].tool_calls)
        assert "Recon Crew" in run.summary

    def test_local_run_reports_denied_tools_as_findings(self, tools):
        registry, audit = tools
        adapter = CrewAIAdapter(tool_executor=in_process_executor(registry, audit), prefer_real=False)
        card = {
            "title": "web scan",
            "description": "scan",
            "tools": [{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
        }
        run = adapter.run(get_crew("vuln-assessment"), card, role_lookup=get_role)
        # No scope, no approval -> the tool layer refuses, and that is surfaced.
        denied = [c for s in run.steps for c in s.tool_calls if c["status"] == "denied"]
        assert denied
        assert any("GUARDRAIL" in f for f in run.findings)
        assert run.status in ("partial", "blocked")

    def test_adapter_never_loses_the_card_on_executor_crash(self, tools):
        registry, _ = tools

        def exploding(tool_name, args, *, scope=None, approved=False, card_id=None):
            raise RuntimeError("boom")

        adapter = CrewAIAdapter(tool_executor=exploding, prefer_real=False)
        card = {"title": "t", "tools": [{"name": "whois_lookup", "tier": 0, "args": {"target": "a.com"}}]}
        run = adapter.run(get_crew("recon"), card, role_lookup=get_role)
        assert run.status in ("blocked", "partial")
        assert run.errors and "boom" in run.errors[0]


# --------------------------------------------------------------- bridge
class TestBridgeHappyPath:
    """The full loop: Assigned -> Running -> tools run -> traces -> Review."""

    def test_recon_card_lands_in_review_with_traces(self, bridge, kanban):
        card = kanban.post(
            "/api/cards",
            json={
                "title": "Recon: scanme.nmap.org",
                "board_id": "brd_engagement",
                "assignee": "recon-specialist",
                "crew": "recon",
                "scope": {"targets": ["scanme.nmap.org"], "authorization_ref": "X"},
                "tools": [
                    {"name": "whois_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
                    {"name": "dns_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
                ],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})

        outcome = bridge.process_card(cid)
        assert outcome.status == "ok", outcome.reasons
        assert outcome.column == "Review"
        assert outcome.traces == 2
        assert outcome.artifacts == 1

        final = kanban.get(f"/api/cards/{cid}").json()
        assert final["column"] == "Review"
        assert len(final["traces"]) == 2
        assert len(final["artifacts"]) == 1
        assert final["result"]
        assert final["artifacts"][0]["sha256"]

    def test_traces_carry_audit_hash_and_agent(self, bridge, kanban, tools):
        registry, audit = tools
        card = kanban.post(
            "/api/cards",
            json={
                "title": "dns",
                "board_id": "brd_agent",
                "assignee": "recon-specialist",
                "crew": "recon",
                "tools": [{"name": "dns_lookup", "tier": 0, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        bridge.process_card(cid)

        traces = kanban.get(f"/api/cards/{cid}").json()["traces"]
        assert traces[0]["tool"] == "dns_lookup"
        assert traces[0]["dry_run"] is True
        assert traces[0]["agent"]
        assert traces[0]["audit_hash"]
        # the same hash must exist in the tool audit chain
        assert any(row["audit_hash"] == traces[0]["audit_hash"] for row in audit.list())

    def test_poll_once_processes_the_queue(self, bridge, kanban):
        for i in range(2):
            card = kanban.post(
                "/api/cards",
                json={
                    "title": f"dns {i}",
                    "board_id": "brd_agent",
                    "assignee": "recon-specialist",
                    "crew": "recon",
                    "tools": [{"name": "dns_lookup", "tier": 0, "args": {"target": "example.com"}}],
                },
            ).json()
            kanban.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Assigned"})
        outcomes = bridge.poll_once()
        assert len(outcomes) == 2
        assert all(o.column == "Review" for o in outcomes)
        assert bridge.stats.claimed == 2 and bridge.stats.released == 2
        assert kanban.get("/api/cards", params={"column": "Assigned"}).json()["count"] == 0

    def test_bridge_emits_card_moved_events(self, bridge, kanban):
        card = kanban.post(
            "/api/cards",
            json={
                "title": "dns",
                "board_id": "brd_agent",
                "assignee": "recon-specialist",
                "crew": "recon",
                "tools": [{"name": "dns_lookup", "tier": 0, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        bridge.process_card(cid)
        events = kanban.get("/api/events", params={"card_id": cid, "limit": 100}).json()["events"]
        moves = [(e["from_column"], e["to_column"]) for e in events if e["type"] == "card.moved"]
        assert ("Assigned", "Running") in moves
        assert ("Running", "Review") in moves


class TestBridgeGates:
    def test_gated_card_opens_approval_and_does_not_run(self, bridge, kanban, tools):
        registry, audit = tools
        card = kanban.post(
            "/api/cards",
            json={
                "title": "nikto the target",
                "board_id": "brd_engagement",
                "assignee": "web-specialist",
                "crew": "vuln-assessment",
                "scope": {"targets": ["example.com"], "authorization_ref": "ACME-1"},
                "tools": [{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})

        outcome = bridge.process_card(cid)
        assert outcome.status == "gated"
        assert bridge.stats.gated == 1

        after = kanban.get(f"/api/cards/{cid}").json()
        assert after["column"] == "Assigned"  # never entered Running
        assert after["has_pending_approval"] is True
        assert len(audit.list()) == 0  # no tool was touched

    def test_approved_card_then_runs(self, bridge, kanban):
        card = kanban.post(
            "/api/cards",
            json={
                "title": "nikto the target",
                "board_id": "brd_engagement",
                "assignee": "web-specialist",
                "crew": "vuln-assessment",
                "scope": {"targets": ["example.com"], "authorization_ref": "ACME-1"},
                "tools": [{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        bridge.process_card(cid)  # opens the gate

        pending = kanban.get("/api/approvals/pending").json()["pending"]
        assert pending and pending[0]["card_id"] == cid
        approval_id = pending[0]["approval"]["id"]
        kanban.post(
            f"/api/cards/{cid}/approvals/{approval_id}/decide",
            json={"approved": True, "decided_by": "operator", "note": "authorized"},
        )

        outcome = bridge.process_card(cid)
        assert outcome.status in ("ok", "partial")
        assert outcome.column == "Review"
        traces = kanban.get(f"/api/cards/{cid}").json()["traces"]
        assert any(t["tool"] == "nikto_scan" for t in traces)

    def test_rejected_card_is_blocked_and_never_runs(self, bridge, kanban):
        card = kanban.post(
            "/api/cards",
            json={
                "title": "x",
                "board_id": "brd_engagement",
                "assignee": "web-specialist",
                "crew": "vuln-assessment",
                "scope": {"targets": ["example.com"], "authorization_ref": "ACME-1"},
                "tools": [{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        bridge.process_card(cid)  # opens the gate
        approval_id = kanban.get("/api/approvals/pending").json()["pending"][0]["approval"]["id"]
        kanban.post(
            f"/api/cards/{cid}/approvals/{approval_id}/decide", json={"approved": False, "decided_by": "operator"}
        )
        outcome = bridge.process_card(cid)
        assert outcome.status == "blocked"
        assert outcome.column == "Blocked"
        assert any("rejected" in r for r in outcome.reasons)
        # A rejected gate must never result in a tool run.
        assert not kanban.get(f"/api/cards/{cid}").json()["traces"]

    def test_pending_gate_is_not_re_requested(self, bridge, kanban):
        card = kanban.post(
            "/api/cards",
            json={
                "title": "x",
                "board_id": "brd_engagement",
                "assignee": "web-specialist",
                "crew": "vuln-assessment",
                "scope": {"targets": ["example.com"], "authorization_ref": "ACME-1"},
                "tools": [{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        bridge.process_card(cid)
        first = len(kanban.get("/api/approvals/pending").json()["pending"])
        outcome = bridge.process_card(cid)  # second pass, gate already open
        assert outcome.status == "gated"
        assert "pending" in outcome.reasons[0]
        assert len(kanban.get("/api/approvals/pending").json()["pending"]) == first


class TestBridgeSafety:
    def test_bridge_never_writes_done(self, bridge, kanban):
        card = kanban.post(
            "/api/cards",
            json={
                "title": "dns",
                "board_id": "brd_agent",
                "assignee": "recon-specialist",
                "crew": "recon",
                "tools": [{"name": "dns_lookup", "tier": 0, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        bridge.process_card(cid)
        assert kanban.get(f"/api/cards/{cid}").json()["column"] == "Review"

    def test_out_of_scope_card_is_blocked_not_run(self, bridge, kanban, tools):
        registry, audit = tools
        card = kanban.post(
            "/api/cards",
            json={
                "title": "scan evil",
                "board_id": "brd_engagement",
                "assignee": "recon-specialist",
                "crew": "recon",
                "scope": {"targets": ["example.com"], "authorization_ref": "X"},
                "tools": [{"name": "nmap_scan", "tier": 1, "args": {"target": "evil.net"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})

        outcome = bridge.process_card(cid)
        assert outcome.status == "blocked"
        assert any("outside the authorized scope" in r for r in outcome.reasons)
        assert kanban.get(f"/api/cards/{cid}").json()["column"] == "Blocked"
        # out-of-scope work must never reach the Running column at all
        events = kanban.get("/api/events", params={"card_id": cid, "limit": 100}).json()["events"]
        assert not any(e["to_column"] == "Running" for e in events if e["type"] == "card.moved")

    def test_scope_less_card_with_t1_tool_is_blocked(self, bridge, kanban):
        card = kanban.post(
            "/api/cards",
            json={
                "title": "scan with no scope",
                "board_id": "brd_engagement",
                "assignee": "recon-specialist",
                "crew": "recon",
                "tools": [{"name": "nmap_scan", "tier": 1, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        outcome = bridge.process_card(cid)
        assert outcome.status == "blocked"
        assert any("none" in r for r in outcome.reasons)

    def test_killed_card_is_skipped(self, bridge, kanban):
        card = kanban.post(
            "/api/cards",
            json={
                "title": "dns",
                "board_id": "brd_agent",
                "assignee": "recon-specialist",
                "crew": "recon",
                "tools": [{"name": "dns_lookup", "tier": 0, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        kanban.post(f"/api/cards/{cid}/kill", json={"reason": "operator"})
        outcome = bridge.process_card(cid)
        assert outcome.status == "skipped"

    def test_card_not_in_assigned_is_skipped(self, bridge, kanban):
        card = kanban.post("/api/cards", json={"title": "x", "board_id": "brd_agent"}).json()
        outcome = bridge.process_card(card["card_id"])
        assert outcome.status == "skipped"
        assert "Backlog" in outcome.reasons[0]

    def test_unknown_card_fails_cleanly(self, bridge):
        outcome = bridge.process_card("nope")
        assert outcome.status == "failed"


# --------------------------------------------------------- runtime server
class TestRuntimeServer:
    def test_health_reports_backend_and_stats(self, tmp_path, monkeypatch, tools):
        registry, audit = tools
        from agent_runtime.server import build_app as build_runtime

        app = build_runtime(tool_executor=in_process_executor(registry, audit), autostart=False)
        with TestClient(app) as client:
            body = client.get("/health").json()
            assert body["service"] == "agent-runtime"
            assert body["backend"] in ("local", "crewai")
            assert "kanban_reachable" in body
            assert body["stats"]["polls"] == 0

    def test_roles_and_crews_endpoints(self, tools):
        registry, audit = tools
        from agent_runtime.server import build_app as build_runtime

        app = build_runtime(tool_executor=in_process_executor(registry, audit), autostart=False)
        with TestClient(app) as client:
            assert client.get("/roles").json()["count"] >= 6
            assert client.get("/crews").json()["count"] >= 4
            assert client.get("/crews/recon").json()["display_name"] == "Recon Crew"
            assert "nmap_scan" in client.get("/crews/recon").json()["tools"]
            assert client.get("/roles/nobody").status_code == 404
            assert client.get("/crews/nobody").status_code == 404
