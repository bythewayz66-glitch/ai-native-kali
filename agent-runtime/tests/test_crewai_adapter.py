"""Tests for the CrewAI adapter: both branches, and the model path through it.

The adapter has two backends and the whole point is that a host without
``crewai`` behaves exactly as before. So this suite tests both branches:

* **absent** - the real state of this sandbox. ``crewai`` is not installed, so
  the absent branch is exercised for real: the adapter reports ``local``, runs
  the deterministic pipeline, and ``run_plan`` refuses rather than pretending.
  A subprocess test proves the module has no hard dependency on the package.
* **present** - ``crewai`` cannot be installed here, so a **fake** ``crewai``
  module is injected into ``sys.modules``. The fake is deliberately thin: it
  provides ``Agent``/``Task``/``Crew``/``Process``/``tool`` and a ``kickoff``
  that actually invokes the bound tools. That means the adapter's *real* code
  path runs - ``_make_tool`` -> the guardrailed executor -> ``_record_call`` ->
  ``_result_from_calls`` - and the assertions are about the adapter, not about
  the fake. **No real crewai run happened here and none is claimed.**

The model path is tested the same way: with the fake present, a validated plan
step must execute through ``run_plan``; with it absent, through the
deterministic executor; and a crewai failure must fall back rather than lose the
card.
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Optional

import pytest

from agent_runtime.crewai_adapter import CrewAIAdapter, crewai_available, crewai_version
from agent_runtime.crews import get_crew
from agent_runtime.llm_crew import LLMCrewRunner
from agent_runtime.model_client import LocalModelClient, ModelConfig
from agent_runtime.roles import get_role

REPO_ROOT = Path(__file__).resolve().parents[2]

SCOPE = {"targets": ["scanme.nmap.org", "example.com"], "authorization_ref": "TICKET-1"}


# --------------------------------------------------------------- fake crewai
class FakeTool:
    """Stands in for a crewai ``Tool``: named, and callable via ``run``."""

    def __init__(self, name: str, fn: Any) -> None:
        self.name = name
        self._fn = fn

    def run(self, **kwargs: Any) -> Any:
        return self._fn(**kwargs)

    def __call__(self, **kwargs: Any) -> Any:
        return self._fn(**kwargs)


def _fake_tool(name: Any = None) -> Any:
    """``@tool("name")`` and ``@tool`` both work, as in the real package."""

    def deco(fn: Any) -> FakeTool:
        return FakeTool(name or fn.__name__, fn)

    if callable(name):  # used bare: @tool
        fn = name
        return FakeTool(fn.__name__, fn)
    return deco


class FakeAgent:
    def __init__(self, role: str, goal: str, backstory: str = "", tools: Any = None,
                 llm: Any = None, verbose: bool = False) -> None:
        self.role = role
        self.goal = goal
        self.backstory = backstory
        self.tools = list(tools or [])
        self.llm = llm
        self.verbose = verbose


class FakeTask:
    def __init__(self, description: str, expected_output: str = "", agent: Any = None) -> None:
        self.description = description
        self.expected_output = expected_output
        self.agent = agent


class FakeProcess:
    sequential = "sequential"


class FakeCrew:
    """A crew whose ``kickoff`` deterministically runs each agent's tools.

    Real crewai would have an LLM choose; the fake just runs what it was given,
    which is exactly what makes the adapter's recording path observable.
    """

    def __init__(self, agents: Any, tasks: Any, process: Any = None, verbose: bool = False) -> None:
        self.agents = list(agents)
        self.tasks = list(tasks)
        self.process = process
        self.verbose = verbose
        self.kickoff_calls = 0

    def kickoff(self) -> str:
        self.kickoff_calls += 1
        for task in self.tasks:
            for bound in task.agent.tools:
                bound.run()
        return "fake crewai output"


def _make_fake_crewai(*, kickoff_raises: bool = False) -> types.ModuleType:
    module = types.ModuleType("crewai")
    module.__version__ = "0.60.0"
    module.Agent = FakeAgent
    module.Task = FakeTask
    module.Process = FakeProcess
    module.tool = _fake_tool

    class _Crew(FakeCrew):
        def kickoff(self) -> str:
            if kickoff_raises:
                raise RuntimeError("crewai kickoff exploded")
            return super().kickoff()

    module.Crew = _Crew
    return module


@pytest.fixture
def fake_crewai(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``crewai`` for the duration of one test."""
    module = _make_fake_crewai()
    monkeypatch.setitem(sys.modules, "crewai", module)
    return module


@pytest.fixture
def fake_crewai_broken(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = _make_fake_crewai(kickoff_raises=True)
    monkeypatch.setitem(sys.modules, "crewai", module)
    return module


# ------------------------------------------------------------------ executor
class RecordingExecutor:
    def __init__(self, status: str = "dry_run") -> None:
        self.status = status
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def __call__(self, tool: str, args: dict[str, Any], *, scope: Any = None,
                 approved: bool = False, card_id: Any = None) -> dict[str, Any]:
        self.calls.append((tool, dict(args), {"approved": approved, "card_id": card_id, "scope": scope}))
        return {
            "tool": tool,
            "status": self.status,
            "dry_run": self.status == "dry_run",
            "duration_ms": 7,
            "command": f"{tool} {args.get('target', '')}".strip(),
            "audit_hash": f"hash-{tool}",
            "reasons": [] if self.status != "denied" else ["refused by guardrails"],
            "stdout": f"[dry-run] would execute: {tool}",
            "stderr": "",
        }


def _card(**overrides: Any) -> dict[str, Any]:
    card = {
        "card_id": "crd_1",
        "title": "Recon scanme.nmap.org",
        "description": "Passive then active recon on the authorised host.",
        "scope": SCOPE,
        "crew": "recon",
        "tools": [
            {"name": "whois_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
            {"name": "dns_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
        ],
    }
    card.update(overrides)
    return card


# ============================================================ absent branch
class TestCrewAIAbsent:
    """The real state of this sandbox: crewai is not installed."""

    @pytest.mark.skipif(crewai_available(), reason="crewai is installed; the absent branch is not the default here")
    def test_absent_branch_is_the_real_default(self) -> None:
        assert crewai_available() is False
        assert crewai_version() is None

    def test_adapter_reports_local_without_crewai(self) -> None:
        adapter = CrewAIAdapter(tool_executor=RecordingExecutor())
        assert adapter.backend == "local"
        assert adapter.crewai_available is False

    def test_local_run_still_executes_the_card(self) -> None:
        executor = RecordingExecutor()
        adapter = CrewAIAdapter(tool_executor=executor)
        result = adapter.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert result.backend == "local"
        assert result.status == "ok"
        assert [c[0] for c in executor.calls] == ["whois_lookup", "dns_lookup"]

    def test_run_plan_refuses_rather_than_pretending(self) -> None:
        adapter = CrewAIAdapter(tool_executor=RecordingExecutor())
        with pytest.raises(RuntimeError, match="not importable"):
            adapter.run_plan(
                get_crew("recon"),
                _card(),
                [{"role": "recon-specialist", "tool": "whois_lookup", "args": {"target": "x"}}],
                role_lookup=get_role,
            )

    def test_the_module_has_no_hard_dependency_on_crewai(self) -> None:
        """Importing and constructing the adapter must not need crewai.

        Run in a subprocess so the assertion is about a clean interpreter, not
        about whatever this test session has already imported.
        """
        code = (
            "import sys; sys.path.insert(0, 'agent-runtime');"
            "from agent_runtime.crewai_adapter import CrewAIAdapter, crewai_available;"
            "a = CrewAIAdapter(tool_executor=lambda *a, **k: {});"
            "print(a.backend, crewai_available())"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(REPO_ROOT), capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "local False", proc.stdout


# =========================================================== present branch
class TestCrewAIPresent:
    """The crewai branch, exercised through a fake module (no real run)."""

    def test_backend_is_crewai_when_importable(self, fake_crewai: types.ModuleType) -> None:
        adapter = CrewAIAdapter(tool_executor=RecordingExecutor())
        assert adapter.backend == "crewai"
        assert adapter.crewai_available is True

    def test_version_is_reported(self, fake_crewai: types.ModuleType) -> None:
        assert crewai_version() == "0.60.0"

    def test_run_executes_the_card_bound_tools(self, fake_crewai: types.ModuleType) -> None:
        executor = RecordingExecutor()
        adapter = CrewAIAdapter(tool_executor=executor)
        result = adapter.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert result.backend == "crewai"
        assert result.status == "ok"
        assert [c[0] for c in executor.calls] == ["whois_lookup", "dns_lookup"]
        # The card's own arguments are what the tool layer sees.
        assert executor.calls[0][1] == {"target": "scanme.nmap.org"}
        # The card id is threaded through, so the audit log can be joined back.
        assert executor.calls[0][2]["card_id"] == "crd_1"
        assert executor.calls[0][2]["scope"] == SCOPE

    def test_run_records_audit_hashes_in_the_steps(self, fake_crewai: types.ModuleType) -> None:
        """A crewai run must carry the same per-call detail as the local one."""
        adapter = CrewAIAdapter(tool_executor=RecordingExecutor())
        result = adapter.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        calls = [c for step in result.steps for c in step.tool_calls]
        assert {c["tool"] for c in calls} == {"whois_lookup", "dns_lookup"}
        assert all(c["audit_hash"] for c in calls)
        assert all(c["status"] == "dry_run" for c in calls)

    def test_a_tool_the_card_does_not_bind_is_never_offered(self, fake_crewai: types.ModuleType) -> None:
        """The crew lists nmap_scan; the card does not bind it. It must not run."""
        executor = RecordingExecutor()
        adapter = CrewAIAdapter(tool_executor=executor)
        adapter.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert "nmap_scan" not in [c[0] for c in executor.calls]

    def test_guardrail_denial_reports_blocked_not_ok(self, fake_crewai: types.ModuleType) -> None:
        """A crewai run whose tools were all refused must not report success."""
        adapter = CrewAIAdapter(tool_executor=RecordingExecutor(status="denied"))
        result = adapter.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert result.status == "blocked"
        assert any("GUARDRAIL" in f for f in result.findings)
        assert result.errors

    def test_kickoff_failure_falls_back_to_local(self, fake_crewai_broken: types.ModuleType) -> None:
        executor = RecordingExecutor()
        adapter = CrewAIAdapter(tool_executor=executor)
        result = adapter.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        assert result.backend == "local (fallback)"
        assert any("fell back to local" in e for e in result.errors)
        assert executor.calls, "the fallback must still run the card"

    def test_run_plan_executes_the_validated_tool(self, fake_crewai: types.ModuleType) -> None:
        executor = RecordingExecutor()
        adapter = CrewAIAdapter(tool_executor=executor)
        result = adapter.run_plan(
            get_crew("recon"),
            _card(),
            [{"role": "recon-specialist", "tool": "whois_lookup",
              "args": {"target": "scanme.nmap.org"}, "approved": True}],
            role_lookup=get_role,
            scope=SCOPE,
        )
        assert result.backend == "crewai"
        assert [c[0] for c in executor.calls] == ["whois_lookup"]
        assert executor.calls[0][2]["approved"] is True

    def test_run_plan_does_not_widen_the_plan(self, fake_crewai: types.ModuleType) -> None:
        """Only the tool named in the plan is called - nothing else is added."""
        executor = RecordingExecutor()
        adapter = CrewAIAdapter(tool_executor=executor)
        adapter.run_plan(
            get_crew("recon"),
            _card(),
            [{"role": "recon-specialist", "tool": "dns_lookup", "args": {"target": "scanme.nmap.org"}}],
            role_lookup=get_role,
            scope=SCOPE,
        )
        assert [c[0] for c in executor.calls] == ["dns_lookup"]

    def test_run_plan_with_no_steps_is_blocked_not_ok(self, fake_crewai: types.ModuleType) -> None:
        adapter = CrewAIAdapter(tool_executor=RecordingExecutor())
        result = adapter.run_plan(get_crew("recon"), _card(), [], role_lookup=get_role)
        assert result.status == "blocked"
        assert "no executable plan steps" in result.summary


# ================================================== the model path through it
class FakeTransport:
    """A scripted model endpoint: tags + one chat reply."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply

    def __call__(self, method: str, url: str, payload: Any, headers: Any, timeout: Any) -> Any:
        if url.endswith("/api/tags"):
            return 200, {"models": [{"name": "llama3.1"}]}
        import json

        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return 200, {
            "model": "llama3.1",
            "message": {"content": text},
            "prompt_eval_count": 100,
            "eval_count": 20,
        }


def _runner(executor: RecordingExecutor, reply: Any) -> LLMCrewRunner:
    model = LocalModelClient(
        ModelConfig(enabled=True, base_url="http://fake", model="llama3.1"),
        transport=FakeTransport(reply),
    )
    adapter = CrewAIAdapter(tool_executor=executor)
    return LLMCrewRunner(adapter, model=model)


class TestModelPathThroughCrewAI:
    def test_validated_step_executes_through_crewai(self, fake_crewai: types.ModuleType) -> None:
        reply = {"steps": [{"role": "recon-specialist", "tool": "whois_lookup",
                            "args": {"target": "scanme.nmap.org"}}]}
        executor = RecordingExecutor()
        runner = _runner(executor, reply)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert result.backend == "local-model"
        assert result.status == "ok"
        assert runner.crewai_steps == 1
        assert runner.crewai_fallbacks == 0
        assert [c[0] for c in executor.calls] == ["whois_lookup"]

    def test_crewai_failure_falls_back_to_the_deterministic_executor(
        self, fake_crewai_broken: types.ModuleType
    ) -> None:
        reply = {"steps": [{"role": "recon-specialist", "tool": "whois_lookup",
                            "args": {"target": "scanme.nmap.org"}}]}
        executor = RecordingExecutor()
        runner = _runner(executor, reply)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert runner.crewai_fallbacks == 1
        assert runner.crewai_steps == 0
        assert [c[0] for c in executor.calls] == ["whois_lookup"], "the card must still move"
        assert result.status == "ok"

    def test_without_crewai_the_deterministic_executor_runs(self) -> None:
        reply = {"steps": [{"role": "recon-specialist", "tool": "whois_lookup",
                            "args": {"target": "scanme.nmap.org"}}]}
        executor = RecordingExecutor()
        runner = _runner(executor, reply)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert runner.crewai_steps == 0
        assert runner.crewai_fallbacks == 0
        assert [c[0] for c in executor.calls] == ["whois_lookup"]
        assert result.status == "ok"

    def test_validation_still_refuses_before_any_backend_runs(
        self, fake_crewai: types.ModuleType
    ) -> None:
        """A refused step must not reach crewai either."""
        reply = {"steps": [{"role": "recon-specialist", "tool": "nikto_scan",
                            "args": {"target": "scanme.nmap.org"}}]}
        executor = RecordingExecutor()
        runner = _runner(executor, reply)
        result = runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)

        assert executor.calls == []
        assert runner.crewai_steps == 0
        assert runner.refused_steps == 1
        assert any("not in the permitted set" in f for f in result.findings)

    def test_status_reports_the_crewai_counters(self, fake_crewai: types.ModuleType) -> None:
        reply = {"steps": [{"role": "recon-specialist", "tool": "whois_lookup",
                            "args": {"target": "scanme.nmap.org"}}]}
        runner = _runner(RecordingExecutor(), reply)
        runner.run(get_crew("recon"), _card(), role_lookup=get_role, scope=SCOPE)
        status = runner.status()
        assert status["crewai_steps"] == 1
        assert "crewai_fallbacks" in status
