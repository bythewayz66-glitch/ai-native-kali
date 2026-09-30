"""Tests for the Phase 3 crew: local model, guardrail validation, human gate.

The point of these tests is that a *model* in an offensive-security loop is
treated as an untrusted planner. So the suite attacks it from that angle:

* the model is faked, and its plan is asserted to be validated - a plan that
  escalates a T1 role to a T2 tool, or aims at a host outside the scope, must be
  refused with the tool never called;
* the human gate is asserted to block, and to treat a timeout as a *rejection*
  (never an implicit approval);
* with no model reachable, the deterministic executor must still run, so the
  card still moves and CI needs no GPU.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from agent_runtime.crewai_adapter import CrewAIAdapter, CrewRunResult
from agent_runtime.crews import get_crew
from agent_runtime.gate import GATE_APPROVED, GATE_REJECTED, GATE_TIMEOUT, HumanFeedbackGate
from agent_runtime.llm_crew import LLMCrewRunner
from agent_runtime.model_client import LocalModelClient, ModelConfig
from agent_runtime.roles import get_role

# ------------------------------------------------------------------ helpers
SCOPE = {"targets": ["scanme.nmap.org", "example.com"], "authorization_ref": "TICKET-1"}


class FakeTransport:
    """Stands in for the model endpoint: scripted tags + chat replies."""

    def __init__(self, reply: Any = None, *, model: str = "llama3.1", tags_ok: bool = True,
                 tags_models: Optional[list[str]] = None, chat_status: int = 200):
        self.reply = reply
        self.model = model
        self.tags_ok = tags_ok
        self.tags_models = tags_models if tags_models is not None else [model]
        self.chat_status = chat_status
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, payload, headers, timeout):
        self.calls.append((method, url))
        if url.endswith("/api/tags"):
            if not self.tags_ok:
                raise RuntimeError("connection refused")
            return 200, {"models": [{"name": n} for n in self.tags_models]}
        text = self.reply if isinstance(self.reply, str) else __import__("json").dumps(self.reply)
        return self.chat_status, {
            "model": self.model,
            "message": {"content": text},
            "prompt_eval_count": 120,
            "eval_count": 45,
        }


def _client(transport: FakeTransport) -> LocalModelClient:
    return LocalModelClient(
        ModelConfig(enabled=True, base_url="http://fake", model="llama3.1", flavor="ollama"),
        transport=transport,
    )


class RecordingExecutor:
    """A tool executor that records calls and answers with a chosen status."""

    def __init__(self, status: str = "dry_run") -> None:
        self.status = status
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def __call__(self, tool, args, *, scope=None, approved=False, card_id=None):
        self.calls.append((tool, dict(args), {"approved": approved, "card_id": card_id, "scope": scope}))
        return {
            "tool": tool,
            "status": self.status,
            "dry_run": self.status == "dry_run",
            "duration_ms": 12,
            "command": f"{tool} {args.get('target', '')}".strip(),
            "audit_hash": f"hash-{tool}",
            "reasons": [] if self.status != "denied" else ["refused by guardrails"],
            "stdout": f"[dry-run] would execute: {tool}",
            "stderr": "",
        }


class FakeKanbanClient:
    """A board client that holds approvals in memory and can decide them."""

    def __init__(self, card_id: str = "crd_1") -> None:
        self.card_id = card_id
        self.approvals: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def request_approval(self, card_id, *, reason, requested_by, tool=None, tier=None):
        self.requests.append({"reason": reason, "requested_by": requested_by, "tool": tool, "tier": tier})
        approval = {
            "id": f"apr_{len(self.approvals) + 1}",
            "status": "pending",
            "reason": reason,
            "requested_by": requested_by,
            "tool": tool,
            "tier": tier,
            "decided_by": None,
            "note": "",
        }
        self.approvals.append(approval)
        return {"card_id": card_id, "approvals": list(self.approvals), "pending_approval": approval}

    def card(self, card_id):
        pending = next((a for a in self.approvals if a["status"] == "pending"), None)
        return {"card_id": card_id, "approvals": list(self.approvals), "pending_approval": pending}

    def decide(self, approval_id: str, *, approved: bool, decided_by: str = "operator", note: str = ""):
        for approval in self.approvals:
            if approval["id"] == approval_id:
                approval["status"] = "approved" if approved else "rejected"
                approval["decided_by"] = decided_by
                approval["note"] = note
        return {"ok": True}

    def post(self, path, payload=None):
        return {"ok": True}


def _make_runner(
    *,
    reply: Any = None,
    transport: Optional[FakeTransport] = None,
    executor: Optional[RecordingExecutor] = None,
    gate: Optional[HumanFeedbackGate] = None,
    model_enabled: bool = True,
    **client_kwargs: Any,
) -> tuple[LLMCrewRunner, RecordingExecutor, FakeTransport]:
    transport = transport or FakeTransport(reply)
    if not model_enabled:
        transport.tags_ok = False
    model = LocalModelClient(
        ModelConfig(enabled=model_enabled, base_url="http://fake", model="llama3.1"),
        transport=transport,
        **client_kwargs,
    )
    executor = executor or RecordingExecutor()
    adapter = CrewAIAdapter(tool_executor=executor, prefer_real=False)
    runner = LLMCrewRunner(adapter, model=model, gate=gate)
    return runner, executor, transport


def _card(**overrides: Any) -> dict[str, Any]:
    card = {
        "card_id": "crd_1",
        "title": "Recon scanme.nmap.org",
        "description": "Passive then active recon on the authorised host.",
        "column": "Running",
        "scope": SCOPE,
        "crew": "recon",
        "tools": [
            {"name": "whois_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
            {"name": "dns_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
            {"name": "nmap_scan", "tier": 1, "args": {"target": "scanme.nmap.org"}},
        ],
    }
    card.update(overrides)
    return card


# ------------------------------------------------------------ model posture
class TestModelClient:
    def test_probe_reports_availability_and_models(self):
        client = _client(FakeTransport())
        probe = client.probe()
        assert probe["available"] is True
        assert probe["models"] == ["llama3.1"]
        assert probe["error"] is None

    def test_probe_is_cached_within_the_ttl(self):
        transport = FakeTransport()
        clock_value = [0.0]
        client = LocalModelClient(
            ModelConfig(enabled=True, base_url="http://fake"),
            transport=transport,
            probe_ttl_s=10.0,
            clock=lambda: clock_value[0],
        )
        client.probe()
        client.probe()
        assert len([c for c in transport.calls if c[1].endswith("/api/tags")]) == 1
        clock_value[0] = 60.0
        client.probe()
        assert len([c for c in transport.calls if c[1].endswith("/api/tags")]) == 2

    def test_unreachable_endpoint_is_reported_not_raised(self):
        client = _client(FakeTransport(tags_ok=False))
        probe = client.probe()
        assert probe["available"] is False
        assert probe["error"], "an unreachable endpoint must explain itself"
        assert "refused" in probe["error"]

    def test_disabled_model_never_calls_out(self):
        transport = FakeTransport()
        client = LocalModelClient(ModelConfig(enabled=False), transport=transport)
        probe = client.probe()
        assert probe["available"] is False
        assert probe["error"] == "MODEL_ENABLED=0"
        assert transport.calls == []

    def test_completion_records_tokens_and_latency(self):
        client = _client(FakeTransport({"steps": [], "summary": "nothing to do"}))
        response = client.complete("hi", json_mode=True)
        assert response.ok is True
        assert response.total_tokens == 165
        assert response.prompt_tokens == 120
        assert response.completion_tokens == 45
        assert response.parsed == {"steps": [], "summary": "nothing to do"}

    def test_json_wrapped_in_prose_or_fences_is_still_parsed(self):
        client = _client(FakeTransport('Sure! Here you go:\n```json\n{"steps": []}\n```\nHope that helps.'))
        response = client.complete("hi", json_mode=True)
        assert response.parsed == {"steps": []}

    def test_http_error_comes_back_as_ok_false(self):
        client = _client(FakeTransport({}, chat_status=500))
        response = client.complete("hi")
        assert response.ok is False
        assert "500" in (response.error or "")


# ----------------------------------------------------- guardrail validation
class TestPlanValidation:
    def test_a_valid_plan_runs_its_tools(self):
        reply = {
            "steps": [
                {"role": "recon-specialist", "tool": "whois_lookup", "args": {"target": "scanme.nmap.org"}},
                {"role": "recon-specialist", "tool": "nmap_scan", "args": {"target": "scanme.nmap.org"}},
            ],
            "summary": "Passive lookup then a low-impact scan.",
        }
        runner, executor, _ = _make_runner(reply=reply)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert result.backend == "local-model"
        assert result.status == "ok"
        assert [c[0] for c in executor.calls] == ["whois_lookup", "nmap_scan"]
        assert result.tokens == 165
        assert runner.model_runs == 1
        assert runner.fallback_runs == 0

    def test_a_tool_outside_the_permitted_set_is_refused(self):
        """The model cannot conjure a tool the card never bound."""
        reply = {"steps": [{"role": "recon-specialist", "tool": "nikto_scan", "args": {"target": "scanme.nmap.org"}}]}
        runner, executor, _ = _make_runner(reply=reply)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert executor.calls == [], "a non-permitted tool must never be invoked"
        assert runner.refused_steps == 1
        assert any("not in the permitted set" in f for f in result.findings)
        assert result.status == "blocked"

    def test_role_escalation_is_refused_even_when_the_tool_is_bound(self):
        """A role may not exceed its ceiling, even when the card binds the tool.

        The shipped crews are internally consistent (no role is bound a tool above
        its own ceiling), so the escalation is constructed deliberately here: the
        T2 ``web-specialist`` is asked to run the T3 ``sqlmap_test``. The ceiling -
        not the crew definition - is what must refuse it.
        """
        import copy

        base = get_crew("vuln-assessment")
        crew = copy.deepcopy(base)
        assert crew.steps[0].role == "web-specialist"
        crew.steps[0].tools = [*crew.steps[0].tools, "sqlmap_test"]

        reply = {"steps": [{"role": "web-specialist", "tool": "sqlmap_test",
                            "args": {"target": "example.com"}}]}
        card = _card(
            crew="vuln-assessment",
            tools=[{"name": "sqlmap_test", "tier": 3, "args": {"target": "example.com"}}],
        )
        runner, executor, _ = _make_runner(reply=reply)
        result = runner.run(crew, card, role_lookup=get_role, scope=SCOPE)

        assert executor.calls == [], "a T3 tool must never run through a T2 role"
        assert runner.refused_steps == 1
        assert any("may not use" in f for f in result.findings), result.findings

    def test_a_withheld_tool_is_never_even_shown_to_the_model(self):
        card = _card(tools=[
            {"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}},
        ])
        runner, _executor, _transport = _make_runner(reply={"steps": []})
        allowed, withheld = runner._candidates(
            get_crew("recon"),
            {t["name"]: t for t in card["tools"]},
            get_role,
            lambda _n: None,
        )
        assert allowed == []
        assert any("withheld" in w for w in withheld)

    def test_out_of_scope_target_is_refused(self):
        reply = {"steps": [{"role": "recon-specialist", "tool": "nmap_scan",
                            "args": {"target": "attacker.example.net"}}]}
        runner, executor, _ = _make_runner(reply=reply)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert executor.calls == []
        assert runner.refused_steps == 1
        assert any("outside the authorized scope" in f for f in result.findings)

    def test_a_card_with_no_scope_refuses_active_work(self):
        reply = {"steps": [{"role": "recon-specialist", "tool": "nmap_scan",
                            "args": {"target": "scanme.nmap.org"}}]}
        runner, executor, _ = _make_runner(reply=reply)
        result = runner.run(get_crew("recon"), _card(scope=None), role_lookup=get_role, scope=None)

        assert executor.calls == []
        assert any("no authorization scope" in f for f in result.findings)

    def test_a_role_not_in_the_crew_is_refused(self):
        reply = {"steps": [{"role": "report-writer", "tool": "whois_lookup", "args": {"target": "example.com"}}]}
        runner, executor, _ = _make_runner(reply=reply)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert executor.calls == []
        assert any("not part of the" in f for f in result.findings)

    def test_a_non_object_step_is_refused(self):
        runner, executor, _ = _make_runner(reply={"steps": ["whois_lookup"]})
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert executor.calls == []
        assert result.status == "blocked"

    def test_a_guardrail_denial_from_the_tool_layer_is_recorded(self):
        reply = {"steps": [{"role": "recon-specialist", "tool": "nmap_scan", "args": {"target": "scanme.nmap.org"}}]}
        runner, executor, _ = _make_runner(reply=reply, executor=RecordingExecutor(status="denied"))
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert len(executor.calls) == 1
        assert any("refused by guardrails" in e for e in result.errors)
        assert any("GUARDRAIL" in f for f in result.findings)

    def test_an_argument_failure_is_reported_not_swallowed(self):
        transport = FakeTransport(chat_status=503)
        runner, executor, _ = _make_runner(transport=transport)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert executor.calls, "the deterministic fallback should still have run"
        assert any("model unavailable" in e for e in result.errors)


# ------------------------------------------------------------- human gate
class TestHumanFeedbackGate:
    def _gate(self, client: FakeKanbanClient, **kwargs: Any) -> HumanFeedbackGate:
        kwargs.setdefault("poll_interval_s", 0.001)
        kwargs.setdefault("timeout_s", 5.0)
        return HumanFeedbackGate(client, **kwargs)

    def test_open_creates_a_pending_approval_on_the_card(self):
        client = FakeKanbanClient()
        gate = self._gate(client)
        approval = gate.open("crd_1", reason="T2 work", requested_by="web-specialist",
                            tool="nikto_scan", tier=2)
        assert approval["status"] == "pending"
        assert client.requests[0]["tier"] == 2
        assert gate.opened == 1

    def test_wait_returns_approved_when_a_human_approves(self):
        client = FakeKanbanClient()
        gate = self._gate(client)
        approval = gate.open("crd_1", reason="T2", requested_by="agent")
        client.decide(approval["id"], approved=True, decided_by="bythewayz66", note="go ahead")

        decision = gate.wait("crd_1", approval_id=approval["id"])
        assert decision.outcome == GATE_APPROVED
        assert decision.approved is True
        assert decision.decided_by == "bythewayz66"
        assert gate.approved == 1

    def test_wait_returns_rejected_when_a_human_rejects(self):
        client = FakeKanbanClient()
        gate = self._gate(client)
        approval = gate.open("crd_1", reason="T2", requested_by="agent")
        client.decide(approval["id"], approved=False, decided_by="operator", note="wrong subnet")

        decision = gate.wait("crd_1", approval_id=approval["id"])
        assert decision.outcome == GATE_REJECTED
        assert decision.approved is False
        assert decision.note == "wrong subnet"
        assert gate.rejected == 1

    def test_a_timeout_is_a_rejection_never_an_approval(self):
        """The safety property: silence must not authorise intrusive work."""
        client = FakeKanbanClient()
        gate = HumanFeedbackGate(client, poll_interval_s=0.001, timeout_s=0.02)
        approval = gate.open("crd_1", reason="T2", requested_by="agent")

        decision = gate.wait("crd_1", approval_id=approval["id"])
        assert decision.outcome == GATE_TIMEOUT
        assert decision.approved is False
        assert decision.polls >= 1
        assert gate.timed_out == 1

    def test_dry_run_work_waives_the_gate(self):
        client = FakeKanbanClient()
        gate = self._gate(client)
        decision = gate.request("crd_1", reason="T2", requested_by="agent", dry_run=True)
        assert decision.approved is True
        assert decision.decided_by == "policy:dry-run"
        assert client.requests == [], "a waived gate must not spam the human"

    def test_dry_run_waiver_can_be_disabled(self):
        client = FakeKanbanClient()
        gate = self._gate(client, auto_approve_in_dry_run=False)
        decision = gate.request("crd_1", reason="T2", requested_by="agent",
                               dry_run=True, timeout_s=0.02)
        assert decision.approved is False
        assert decision.outcome == GATE_TIMEOUT

    def test_an_unreadable_card_reports_error_not_approval(self):
        class Broken(FakeKanbanClient):
            def card(self, card_id):
                raise RuntimeError("board unreachable")

        gate = self._gate(Broken())
        decision = gate.wait("crd_1")
        assert decision.outcome == "error"
        assert decision.approved is False
        assert gate.errors == 1

    def test_human_feedback_decorator_blocks_until_approved(self):
        client = FakeKanbanClient()
        gate = self._gate(client)

        @gate.human_feedback(reason="needs a human", timeout_s=0.02)
        def intrusive(*, card_id=None, gate_decision=None):
            return "ran"

        with pytest.raises(Exception) as excinfo:
            intrusive(card_id="crd_1")
        assert "timeout" in str(excinfo.value)
        assert gate.timed_out == 1

    def test_human_feedback_decorator_requires_a_card(self):
        gate = self._gate(FakeKanbanClient())

        @gate.human_feedback()
        def step(card_id=None, gate_decision=None):
            return "ran"

        with pytest.raises(ValueError):
            step()


class TestGateInsideTheCrew:
    def _gated_card(self) -> dict[str, Any]:
        return _card(tools=[
            {"name": "whois_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
            {"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}},
        ])

    def _runner_with_gate(self, *, approve: bool, reply: Any):
        client = FakeKanbanClient()
        gate = HumanFeedbackGate(client, poll_interval_s=0.001, timeout_s=0.5)
        runner, executor, _ = _make_runner(reply=reply, gate=gate)
        # Approve the gate as soon as the crew opens it.
        original_open = gate.open

        def open_and_decide(card_id, **kwargs):
            approval = original_open(card_id, **kwargs)
            client.decide(approval["id"], approved=approve, decided_by="operator")
            return approval

        gate.open = open_and_decide  # type: ignore[assignment]
        return runner, executor, gate

    def test_t2_step_runs_only_after_the_human_approves(self):
        reply = {"steps": [{"role": "web-specialist", "tool": "nikto_scan",
                            "args": {"target": "example.com"}}]}
        card = _card(
            crew="vuln-assessment",
            tools=[{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
        )
        runner, executor, gate = self._runner_with_gate(approve=True, reply=reply)

        result = runner.run(get_crew("vuln-assessment"), card, role_lookup=get_role, scope=SCOPE)

        assert len(executor.calls) == 1
        assert executor.calls[0][0] == "nikto_scan"
        assert executor.calls[0][2]["approved"] is True
        assert runner.gated_steps == 1
        assert runner.gate_denials == 0
        assert "gate[nikto_scan] -> approved" in result.summary

    def test_t2_step_is_skipped_when_the_human_rejects(self):
        reply = {"steps": [{"role": "web-specialist", "tool": "nikto_scan",
                            "args": {"target": "example.com"}}]}
        card = _card(
            crew="vuln-assessment",
            tools=[{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
        )
        runner, executor, _gate = self._runner_with_gate(approve=False, reply=reply)

        result = runner.run(get_crew("vuln-assessment"), card, role_lookup=get_role, scope=SCOPE)

        assert executor.calls == [], "rejected intrusive work must not execute"
        assert runner.gated_steps == 1
        assert runner.gate_denials == 1
        assert any("blocked by the human gate" in f for f in result.findings)
        assert any("rejected" in f for f in result.findings)

    def test_low_tier_work_does_not_open_a_gate(self):
        reply = {"steps": [{"role": "recon-specialist", "tool": "nmap_scan",
                            "args": {"target": "scanme.nmap.org"}}]}
        client = FakeKanbanClient()
        gate = HumanFeedbackGate(client, poll_interval_s=0.001, timeout_s=0.1)
        runner, executor, _ = _make_runner(reply=reply, gate=gate)

        runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert len(executor.calls) == 1
        assert client.requests == [], "T1 work must not ask a human for permission"
        assert runner.gated_steps == 0


# --------------------------------------------------------------- fallback
class TestDeterministicFallback:
    def test_no_model_endpoint_falls_back_and_still_runs(self):
        runner, executor, _ = _make_runner(model_enabled=False)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert result.backend == "local (no model)"
        assert runner.fallback_runs == 1
        assert runner.model_runs == 0
        assert executor.calls, "the fallback must still execute the card's bound tools"
        assert "deterministic fallback" in result.summary
        assert any("model unavailable" in e for e in result.errors)

    def test_fallback_records_why_it_degraded(self):
        runner, _executor, _ = _make_runner(transport=FakeTransport(tags_ok=False))
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert "refused" in result.summary or "refused" in " ".join(result.errors)

    def test_a_model_returning_no_steps_falls_back_rather_than_failing_the_card(self):
        runner, executor, _ = _make_runner(reply={"note": "I cannot help"})
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert runner.fallback_runs == 1
        assert executor.calls
        assert "no 'steps' array" in result.summary or "no 'steps' array" in " ".join(result.errors)

    def test_without_a_gate_t2_work_still_refuses_without_approval(self):
        """No gate configured is not a licence to skip authorisation."""
        reply = {"steps": [{"role": "web-specialist", "tool": "nikto_scan",
                            "args": {"target": "example.com"}}]}
        card = _card(
            crew="vuln-assessment",
            tools=[{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
        )
        runner, executor, _ = _make_runner(reply=reply)  # no gate
        result = runner.run(
            get_crew("vuln-assessment"), card,
            role_lookup=get_role, scope=SCOPE, approved=False,
        )
        # The tool layer is the backstop: unapproved T2 work comes back denied.
        assert len(executor.calls) == 1
        assert executor.calls[0][2]["approved"] is False
        assert runner.gated_steps == 0
        _ = result

    def test_runner_status_is_serialisable(self):
        runner, _executor, _ = _make_runner(reply={"steps": []})
        runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        status = runner.status()
        assert status["runs"] == 1
        assert "tokens" in status
        assert "gate_denials" in status
