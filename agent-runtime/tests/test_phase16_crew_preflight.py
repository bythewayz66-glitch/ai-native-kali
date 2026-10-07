"""Phase 16, item 8: crew pre-flight - the crew/bridge boundary, enforced.

Phase 15's audit found two declaration-level defects that a live run would only
surface as a confusing mid-run failure: a crew step bound a tool that was **not in
the registry at all**, and a tool's scope parameter was declared as a *file path*
so the scope check compared a path against a host. Both are answerable without
executing anything.

Pre-flight answers three declaration questions up front:

1. does every tool a step names exist in the registry;
2. does any step's tool exceed the crew's own ``max_tier`` ceiling; and
3. does each step's role exist.

It deliberately does **not** decide whether a tool suits a role, or whether a
target is authorised. The first is a judgement the registry cannot make; the second
is the guardrail engine's job at call time, with the scope in hand. A pre-flight
that guessed at either would be a second, weaker authorisation path.

The bridge runs this before the approval gate, so an operator is never asked to
approve a run that cannot happen.
"""
from __future__ import annotations

import pytest

from agent_runtime.crew_preflight import (
    PreflightCodes,
    known_tool_names,
    preflight_all,
    preflight_crew,
)
from agent_runtime.crews import CREWS, CrewDef, CrewStep
from agent_runtime.roles import ROLE_REGISTRY


class _Spec:
    """Minimal stand-in for a ToolSpec: the pre-flight only reads tier."""

    def __init__(self, name: str, tier: int) -> None:
        self.name = name
        self.tier = tier


class _Registry:
    def __init__(self, specs: list[_Spec]) -> None:
        self._specs = specs

    def all(self) -> list[_Spec]:
        return list(self._specs)


def _crew(steps: list[CrewStep], *, max_tier: int = 1, name: str = "test") -> CrewDef:
    return CrewDef(name=name, display_name=name, goal="test", steps=steps, max_tier=max_tier)


# ---------------------------------------------------------------------------
# item 8a: the two Phase 15 defects, now caught up front
# ---------------------------------------------------------------------------
class TestThePhase15DefectsAreCaught:
    def test_a_step_binding_an_unregistered_tool_is_refused(self):
        """This is exactly the Phase 15 finding: a role's authority was a
        declaration nothing enforced."""
        reg = _Registry([_Spec("nmap_scan", 1)])
        crew = _crew([CrewStep(role="recon-specialist", tools=["nmap_scan", "ghost_tool"])])
        report = preflight_crew(crew, registry=reg)
        assert report.ok is False
        assert "ghost_tool" in report.unknown_tools
        assert any(r.startswith(PreflightCodes.UNKNOWN_TOOL) for r in report.problems)

    def test_a_tool_above_the_crew_ceiling_is_refused(self):
        """A T2 tool inside a max_tier=1 crew is an over-ceiling binding."""
        reg = _Registry([_Spec("nmap_scan", 1), _Spec("sqlmap_scan", 2)])
        crew = _crew([CrewStep(role="recon-specialist", tools=["sqlmap_scan"])], max_tier=1)
        report = preflight_crew(crew, registry=reg)
        assert report.ok is False
        assert report.tier_exceeded == ["sqlmap_scan(T2)"]
        assert any(r.startswith(PreflightCodes.TIER_EXCEEDED) for r in report.problems)

    def test_at_the_ceiling_is_allowed(self):
        """Boundary: tier == max_tier is inside the ceiling, not above it."""
        reg = _Registry([_Spec("nmap_scan", 1)])
        crew = _crew([CrewStep(role="recon-specialist", tools=["nmap_scan"])], max_tier=1)
        assert preflight_crew(crew, registry=reg).ok is True

    def test_an_unknown_role_is_refused(self):
        reg = _Registry([_Spec("nmap_scan", 1)])
        crew = _crew([CrewStep(role="ghost-role", tools=["nmap_scan"])])
        report = preflight_crew(crew, registry=reg)
        assert report.ok is False
        assert report.unknown_roles == ["ghost-role"]


class TestPreflightShape:
    def test_a_clean_crew_passes(self):
        reg = _Registry([_Spec("nmap_scan", 1)])
        crew = _crew([CrewStep(role="recon-specialist", tools=["nmap_scan"])])
        report = preflight_crew(crew, registry=reg)
        assert report.ok is True
        assert report.problems == []
        assert report.tools_checked == 1

    def test_an_empty_crew_is_refused(self):
        report = preflight_crew(_crew([]), registry=_Registry([]))
        assert report.ok is False
        assert any(r.startswith(PreflightCodes.EMPTY_CREW) for r in report.problems)

    def test_missing_registry_warns_rather_than_failing(self):
        """The tool layer being absent must not crash the card claim path."""
        crew = _crew([CrewStep(role="recon-specialist", tools=["nmap_scan"])])
        report = preflight_crew(crew, registry=object())  # not a registry
        # No tools verified, but a warning not a hard failure.
        assert report.warnings or report.ok

    def test_report_serialises(self):
        reg = _Registry([_Spec("nmap_scan", 1)])
        crew = _crew([CrewStep(role="recon-specialist", tools=["ghost"])])
        body = preflight_crew(crew, registry=reg).as_dict()
        assert body["crew"] == "test"
        assert body["ok"] is False
        assert set(body) >= {"problems", "tools_checked", "unknown_tools"}


# ---------------------------------------------------------------------------
# item 8b: the shipped crews
# ---------------------------------------------------------------------------
class TestShippedCrews:
    def test_every_shipped_crew_passes_preflight(self):
        """The regression that matters: no crew in the tree may be undispatchable."""
        reports = preflight_all()
        bad = {name: r.problems for name, r in reports.items() if not r.ok}
        assert bad == {}, bad

    def test_every_shipped_role_is_in_the_registry(self):
        for name, crew in CREWS.items():
            for step in crew.steps:
                assert step.role in ROLE_REGISTRY, f"{name} names unknown role {step.role}"

    def test_preflight_uses_the_real_tool_layer(self):
        """Not a mock: the names come from the actual registry."""
        names = known_tool_names()
        assert names, "the tool registry must be importable for this check to mean anything"
        for crew in CREWS.values():
            for step in crew.steps:
                for tool in step.tools:
                    assert tool in names, f"{crew.name}/{step.role} binds unknown tool {tool}"

    def test_registry_is_shared_when_injected(self):
        calls: list[int] = []

        class Counting:
            def all(self):
                calls.append(1)
                return [_Spec("nmap_scan", 1)]

        crew = _crew([CrewStep(role="recon-specialist", tools=["nmap_scan"])])
        preflight_crew(crew, registry=Counting())
        assert calls == [1]


class TestBridgeIntegration:
    def test_bridge_preflights_by_default(self):
        from agent_runtime.bridge import Bridge

        bridge = Bridge(client=None, tool_executor=lambda *a, **k: {})
        assert bridge.preflight_crews is True

    def test_bridge_preflight_can_be_disabled_for_a_diagnostic_run(self):
        from agent_runtime.bridge import Bridge

        bridge = Bridge(client=None, tool_executor=lambda *a, **k: {}, preflight_crews=False)
        assert bridge.preflight_crews is False

    def test_bridge_accepts_an_injected_registry(self):
        from agent_runtime.bridge import Bridge

        reg = _Registry([_Spec("nmap_scan", 1)])
        bridge = Bridge(client=None, tool_executor=lambda *a, **k: {}, tool_registry=reg)
        assert bridge._tool_registry is reg
