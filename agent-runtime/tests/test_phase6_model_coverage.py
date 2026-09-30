"""Phase 6, item 4: every role plans with the model, and every crew keeps it.

Phase 2 put a real model behind the *recon* and *vuln-assessment* crews - the two
that happened to have YAML manifests - and left ``reporting`` and ``system`` on
the deterministic runner. That made model coverage a side effect of who wrote a
manifest rather than a property of the system.

"Six crews" in the phase plan means the six **roles** in ``roles.py``
(``recon-specialist``, ``web-specialist``, ``vuln-analyst``, ``report-writer``,
``system-agent``, ``orchestrator``); the Python ``CREWS`` registry holds four.
These tests pin both readings, because the two ways to get this wrong are to
under-cover a role ("reporting never got a model path") or to over-claim coverage
in the report.

The fallback tests matter as much as the coverage ones. A build host with no GPU
and no API key is the *normal* CI case here, so a role that cannot fall back
would make the whole suite unrunnable - the model path must always be additive.
"""
from __future__ import annotations

import pytest

from agent_runtime.crews import (
    ALL_ROLES,
    CREWS,
    crew_roles,
    model_roles,
    role_uses_model,
)
from agent_runtime.llm_crew import LLMCrewRunner
from agent_runtime.model_client import ModelResponse, _memory_section
from agent_runtime.roles import ROLE_REGISTRY


# --------------------------------------------------------------- fakes
class FakeSpec:
    """A resolved tool spec, as ``spec_lookup`` would return it."""

    def __init__(self, tier: int = 0, requires_scope: bool = False) -> None:
        self.tier = tier
        self.requires_scope = requires_scope
        self.binary = "fakebin"
        self.description = "a fake tool"


class FakeModelConfig:
    enabled = True


class FakeModel:
    """A model client that always answers with one valid step."""

    def __init__(self, *, available: bool = True, enabled: bool = True) -> None:
        self.config = FakeModelConfig()
        self.config.enabled = enabled
        self._available = available
        self.plans: list[dict] = []

    def probe(self, *, force: bool = False) -> dict:
        return {"available": self._available, "error": None if self._available else "not reachable"}

    def plan(self, *, crew: str, card: dict, candidates: list[dict]) -> ModelResponse:
        self.plans.append({"crew": crew, "candidates": [c["name"] for c in candidates]})
        steps = []
        if candidates:
            first = candidates[0]
            steps = [{"role": first["role"], "tool": first["name"], "args": {}, "rationale": "fake"}]
        return ModelResponse(
            ok=True,
            text="{}",
            model="fake-model",
            total_tokens=7,
            backend="ollama",
            parsed={"steps": steps, "summary": "fake plan"},
        )


class FakeAdapter:
    """The deterministic executor the runner delegates to."""

    def __init__(self) -> None:
        self.fallbacks = 0
        self.executed: list[str] = []

    def run(self, crew, card, *, role_lookup=None, scope=None, approved=False):
        from agent_runtime.crewai_adapter import CrewRunResult

        self.fallbacks += 1
        return CrewRunResult(
            crew=crew.name,
            backend="deterministic",
            status="ok",
            summary="deterministic run",
            steps=[],
        )

    def tool_executor(self, tool, args, *, scope=None, approved=False, card_id=None):
        self.executed.append(tool)
        return {"status": "dry_run", "dry_run": True, "duration_ms": 1, "command": tool}


def _card(**over) -> dict:
    card = {
        "card_id": "crd_test",
        "title": "test card",
        "description": "d",
        "tools": [],
        "scope": {"targets": ["10.10.0.5"], "cidrs": []},
    }
    card.update(over)
    return card


def _runner(model) -> tuple[LLMCrewRunner, FakeAdapter]:
    adapter = FakeAdapter()
    runner = LLMCrewRunner(adapter, model=model)
    return runner, adapter


# ------------------------------------------------------- the coverage claim
class TestModelCoverage:
    def test_the_registry_holds_every_role(self):
        """Pin the registry size so a role cannot be dropped silently.

        Phase 6 pinned this at six; Phase 8 added ``remediation-specialist``, so
        the number moved to seven. The assertion that matters is the second one -
        the tuple and the registry agree - which is what keeps the count honest.
        """
        assert len(ALL_ROLES) == 7
        assert set(ALL_ROLES) == set(ROLE_REGISTRY)

    def test_every_role_is_on_the_model_path(self):
        assert model_roles() == frozenset(ROLE_REGISTRY)

    def test_coverage_did_not_shrink_back_to_the_two_specced_crews(self):
        """The Phase 2 limit was two *crews*; anything at 2 would be a regression."""
        assert len(model_roles()) > 2

    @pytest.mark.parametrize("role", sorted(ROLE_REGISTRY))
    def test_role_uses_model_for_every_registered_role(self, role):
        assert role_uses_model(role) is True

    def test_an_unregistered_role_has_no_model_path(self):
        """No tool ceiling means nothing safe to plan with."""
        assert role_uses_model("not-a-role") is False

    @pytest.mark.parametrize("crew_name", sorted(CREWS))
    def test_every_role_of_every_crew_is_covered(self, crew_name):
        """No crew may be left behind just because it has no YAML manifest."""
        for role in crew_roles(crew_name):
            assert role in model_roles(), f"{crew_name}: '{role}' has no model path"

    def test_the_original_crews_still_exist(self):
        """The four Phase 6 crews, plus the Phase 8 remediation crew.

        Asserted as a superset of the original four so a crew cannot be dropped,
        and as equality so a new crew cannot appear without this test noticing.
        """
        assert {"recon", "vuln-assessment", "reporting", "system"} <= set(CREWS)
        assert set(CREWS) == {"recon", "vuln-assessment", "reporting", "system", "remediation"}


# ------------------------------------------------- the model path is taken
class TestEveryCrewRunsOnTheModel:
    @pytest.mark.parametrize("crew_name", sorted(CREWS))
    def test_crew_runs_on_the_model_not_the_fallback(self, crew_name):
        model = FakeModel()
        runner, adapter = _runner(model)
        result = runner.run(
            CREWS[crew_name],
            _card(),
            spec_lookup=lambda _n: FakeSpec(tier=0),
        )
        assert result.backend == "local-model", result.errors
        assert runner.model_runs == 1
        assert adapter.fallbacks == 0
        assert model.plans and model.plans[0]["crew"] == crew_name

    def test_all_crews_run_on_the_model_in_one_runner(self):
        """The runner counts coverage across crews, not just per call."""
        model = FakeModel()
        runner, adapter = _runner(model)
        for crew_name in sorted(CREWS):
            runner.run(CREWS[crew_name], _card(), spec_lookup=lambda _n: FakeSpec(tier=0))
        assert runner.model_runs == len(CREWS)
        assert runner.fallback_runs == 0
        assert adapter.fallbacks == 0

    def test_the_planner_only_sees_permitted_tools(self):
        """Widening the path to all crews must not widen any role's tool set."""
        model = FakeModel()
        runner, _ = _runner(model)
        crew = CREWS["recon"]
        # Card bindings with matching tiers, so every crew tool is provably
        # permitted and the candidate list should be the crew's full tool set -
        # widening the model path to all crews must not widen any tool set.
        card = _card(tools=[{"name": t, "tier": 0} for t in crew.tools()])
        runner.run(crew, card, spec_lookup=lambda _n: FakeSpec(tier=0))
        offered = set(model.plans[0]["candidates"])
        assert offered == set(crew.tools())


# ------------------------------------------ the fallback survives everywhere
class TestFallbackSurvivesForEveryCrew:
    @pytest.mark.parametrize("crew_name", sorted(CREWS))
    def test_a_disabled_model_falls_back(self, crew_name):
        model = FakeModel(enabled=False)
        runner, adapter = _runner(model)
        result = runner.run(CREWS[crew_name], _card(), spec_lookup=lambda _n: FakeSpec())
        assert result.backend == "local (no model)"
        assert adapter.fallbacks == 1
        assert any("no model configured" in e for e in result.errors)

    @pytest.mark.parametrize("crew_name", sorted(CREWS))
    def test_an_unreachable_endpoint_falls_back(self, crew_name):
        model = FakeModel(available=False)
        runner, adapter = _runner(model)
        result = runner.run(CREWS[crew_name], _card(), spec_lookup=lambda _n: FakeSpec())
        assert result.backend == "local (no model)"
        assert adapter.fallbacks == 1
        assert any("not reachable" in e for e in result.errors)

    @pytest.mark.parametrize("crew_name", sorted(CREWS))
    def test_no_model_at_all_falls_back(self, crew_name):
        runner, adapter = _runner(None)
        result = runner.run(CREWS[crew_name], _card(), spec_lookup=lambda _n: FakeSpec())
        assert result.backend == "local (no model)"
        assert adapter.fallbacks == 1


# ------------------------------------------------------- the graph seed
class TestGraphSeedReachesThePrompt:
    """Phase 5 shipped ``memory_seed``; Phase 6 must actually spend it."""

    def test_the_seed_entity_and_neighbours_are_rendered(self):
        section = _memory_section(
            {
                "memory_seed": {
                    "seed": "10.10.0.5",
                    "entities": ["svc-db-01", "10.10.0.5"],
                    "relations": 3,
                }
            }
        )
        assert "10.10.0.5" in section
        assert "svc-db-01" in section
        assert "relations" in section

    def test_the_seed_is_labelled_untrusted_in_the_prompt_text(self):
        """The label has to travel with the text - the model is what must honour it."""
        section = _memory_section({"memory_seed": {"seed": "10.10.0.5", "entities": ["10.10.0.5"]}})
        assert "untrusted" in section.lower()
        assert "cannot grant tools" in section.lower()

    def test_a_card_with_neither_block_nor_seed_says_so(self):
        section = _memory_section({})
        assert "nothing relevant" in section.lower()

    def test_the_prose_block_is_still_rendered(self):
        section = _memory_section({"memory_context": "earlier: 22/tcp open ssh"})
        assert "22/tcp open ssh" in section

    def test_seed_and_block_coexist(self):
        section = _memory_section(
            {"memory_context": "earlier: 22/tcp open ssh", "memory_seed": {"seed": "h1", "entities": ["h1"]}}
        )
        assert "22/tcp open ssh" in section and "h1" in section

    def test_a_garbage_seed_does_not_crash_the_prompt(self):
        assert isinstance(_memory_section({"memory_seed": "not-a-dict"}), str)
        assert isinstance(_memory_section({"memory_seed": {"entities": None}}), str)
