"""CrewAI YAML <-> Python registry equivalence (Phase 2, item 6).

`docs/crews/*.yaml` is what a real CrewAI run would load; `roles.py` / `crews.py`
is what the bridge actually executes. Two sources of truth for the same thing is
a drift hazard: someone edits the YAML, the bridge keeps running the old
contract, and nothing complains.

These tests are the complaint. They parse the YAML and assert it describes the
Python registry exactly - roles, tools, tiers, board kinds and crew order. If the
two diverge, this fails.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.crews import CREWS
from agent_runtime.roles import ROLE_REGISTRY

yaml = pytest.importorskip("yaml", reason="PyYAML is required to parse the crew specs")

CREW_DIR = Path(__file__).resolve().parents[2] / "docs" / "crews"

#: (manifest, agents file, tasks file, crew name in the Python registry)
SPECS = [
    ("recon.yaml", "recon_agents.yaml", "recon_tasks.yaml", "recon"),
    (
        "vuln_assessment.yaml",
        "vuln_assessment_agents.yaml",
        "vuln_assessment_tasks.yaml",
        "vuln-assessment",
    ),
    # Phase 7, item 6: the remaining two crews. Before this, only two of the four
    # registered crews had a manifest, which made the spec set an accident of who
    # had written one.
    ("reporting.yaml", "reporting_agents.yaml", "reporting_tasks.yaml", "reporting"),
    ("system.yaml", "system_agents.yaml", "system_tasks.yaml", "system"),
    # Phase 8, item 2: the remediation crew, which spawns scoped sub-cards.
    (
        "remediation.yaml",
        "remediation_agents.yaml",
        "remediation_tasks.yaml",
        "remediation",
    ),
]


def test_every_registered_crew_has_a_spec():
    """Nothing keeps a spec set honest like requiring all of it.

    Without this, adding a crew to the registry and forgetting its manifest would
    leave a crew that runs but has no spec - and the parametrised tests above
    would happily keep covering only the crews that happened to be listed.
    """
    specced = {spec[3] for spec in SPECS}
    assert specced == set(CREWS), f"crews without a spec: {set(CREWS) - specced}"


def test_every_registered_role_is_covered_by_a_spec():
    """The agents files together must describe every registered role that belongs
    to a crew.

    Board-level roles (``crew == ""``) are excluded by construction: the
    orchestrator runs the board and is not a step of any pipeline, so it must not
    be forced into a manifest it does not belong to.
    """
    covered: set[str] = set()
    for _manifest, agents_name, _tasks_name, _crew in SPECS:
        covered |= set(load(agents_name))
    expected = {name for name, role in ROLE_REGISTRY.items() if role.crew}
    assert covered == expected, f"roles without an agent spec: {expected - covered}"
    # And nothing in a manifest is a board-level role sneaking back in.
    assert not {name for name in covered if not ROLE_REGISTRY[name].crew}


def load(name: str) -> dict:
    path = CREW_DIR / name
    assert path.is_file(), f"missing crew spec: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(params=SPECS, ids=[spec[3] for spec in SPECS])
def spec(request):
    manifest_name, agents_name, tasks_name, crew_name = request.param
    return {
        "manifest_name": manifest_name,
        "agents_name": agents_name,
        "tasks_name": tasks_name,
        "crew_name": crew_name,
        "manifest": load(manifest_name),
        "agents": load(agents_name),
        "tasks": load(tasks_name),
        "crew": CREWS[crew_name],
    }


class TestManifest:
    def test_manifest_declares_the_crew_under_its_own_name(self, spec):
        key = spec["crew_name"]
        assert key in spec["manifest"], f"{spec['manifest_name']} must key on '{key}'"

    def test_goal_matches(self, spec):
        assert spec["manifest"][spec["crew_name"]]["goal"].strip() == spec["crew"].goal.strip()

    def test_display_name_matches(self, spec):
        declared = spec["manifest"][spec["crew_name"]]["display_name"].strip()
        assert declared == spec["crew"].display_name

    def test_boards_match(self, spec):
        declared = list(spec["manifest"][spec["crew_name"]]["boards"])
        assert declared == list(spec["crew"].boards)

    def test_max_tier_matches(self, spec):
        declared = int(spec["manifest"][spec["crew_name"]]["max_tier"])
        assert declared == spec["crew"].max_tier

    def test_agent_order_matches_the_execution_order(self, spec):
        """Order is significant - it is the sequence the bridge runs."""
        declared = list(spec["manifest"][spec["crew_name"]]["agents"])
        assert declared == spec["crew"].roles

    def test_every_declared_task_exists(self, spec):
        for task in spec["manifest"][spec["crew_name"]]["tasks"]:
            assert task in spec["tasks"], f"task '{task}' has no definition"


class TestAgents:
    def test_agent_set_matches_the_crew(self, spec):
        declared = set(spec["agents"])
        assert declared == set(spec["crew"].roles)

    def test_each_agent_exists_in_the_registry(self, spec):
        for name in spec["agents"]:
            assert name in ROLE_REGISTRY, f"'{name}' is not a registered role"

    def test_tool_bindings_match(self, spec):
        for name, body in spec["agents"].items():
            role = ROLE_REGISTRY[name]
            assert list(body["tools"]) == list(role.tools), (
                f"{name}: YAML tools {body['tools']} != registry {role.tools}"
            )

    def test_tier_ceiling_matches(self, spec):
        for name, body in spec["agents"].items():
            role = ROLE_REGISTRY[name]
            assert int(body["guardrail"]["max_tier"]) == role.max_tier

    def test_declared_boards_match(self, spec):
        for name, body in spec["agents"].items():
            role = ROLE_REGISTRY[name]
            assert list(body["boards"]) == list(role.boards)

    def test_crew_back_reference_matches(self, spec):
        for name, body in spec["agents"].items():
            assert body["crew"] == ROLE_REGISTRY[name].crew


class TestTasks:
    def test_task_set_covers_the_manifest(self, spec):
        declared = set(spec["manifest"][spec["crew_name"]]["tasks"])
        assert set(spec["tasks"]) == declared

    def test_every_task_runs_as_a_role_in_the_crew(self, spec):
        for name, body in spec["tasks"].items():
            assert body["agent"] in spec["crew"].roles, (
                f"task '{name}' runs as '{body['agent']}', not a role in this crew"
            )

    def test_a_task_never_exceeds_its_role_ceiling(self, spec):
        """The ceiling is the contract: no task may bind above its role's tier."""
        for name, body in spec["tasks"].items():
            role = ROLE_REGISTRY[body["agent"]]
            assert int(body["guardrail_tier"]) <= role.max_tier, (
                f"task '{name}' is T{body['guardrail_tier']} but "
                f"'{body['agent']}' is capped at T{role.max_tier}"
            )

    def test_a_task_never_uses_a_tool_its_role_cannot_bind(self, spec):
        for name, body in spec["tasks"].items():
            role = ROLE_REGISTRY[body["agent"]]
            for tool in body.get("tools") or []:
                assert tool in role.tools, (
                    f"task '{name}' uses '{tool}', which '{body['agent']}' is not bound to"
                )

    def test_task_tools_stay_within_the_crew_binding(self, spec):
        crew_tools = set(spec["crew"].tools())
        for name, body in spec["tasks"].items():
            for tool in body.get("tools") or []:
                assert tool in crew_tools, f"task '{name}' uses '{tool}' outside the crew binding"

    def test_every_tier_2_task_requires_approval(self, spec):
        """The rule that makes intrusive work safe: T2+ is gated, always."""
        gated = [
            name
            for name, body in spec["tasks"].items()
            if int(body["guardrail_tier"]) >= 2
        ]
        for name in gated:
            assert spec["tasks"][name].get("requires_approval") is True, (
                f"task '{name}' is T2+ but does not require approval"
            )

    def test_no_task_below_tier_2_claims_to_require_approval(self, spec):
        """A stale gate flag would train reviewers to approve reflexively."""
        for name, body in spec["tasks"].items():
            if int(body["guardrail_tier"]) < 2:
                assert not body.get("requires_approval"), (
                    f"task '{name}' is T{body['guardrail_tier']} but asks for approval"
                )


class TestWhichCrewsHaveSpecs:
    def test_all_four_crews_ship_a_spec(self):
        """Phase 7, item 6 closed the gap where only two of four crews had YAML.

        Asserted as equality rather than a subset so a new crew cannot be added to
        the registry without a manifest, and a manifest cannot be deleted without
        the suite noticing.
        """
        specced = {spec[3] for spec in SPECS}
        assert specced == set(CREWS)

    def test_a_role_with_a_crew_name_is_a_step_of_that_crew(self):
        """The field that surfaced the orchestrator drift.

        ``AgentRole.crew`` says which pipeline a role runs in. If it names a crew,
        that crew must actually list the role - otherwise the field is a stale
        claim, which is exactly what it had become.
        """
        for name, role in ROLE_REGISTRY.items():
            if not role.crew:
                continue
            assert role.crew in CREWS, f"{name}: crew '{role.crew}' is not registered"
            assert name in CREWS[role.crew].roles, (
                f"{name} claims crew '{role.crew}', which does not list it as a step"
            )

    def test_every_crew_step_role_back_references_its_crew(self):
        for crew_name, crew in CREWS.items():
            for step in crew.steps:
                role = ROLE_REGISTRY[step.role]
                assert role.crew == crew_name, (
                    f"{crew_name}: step '{step.role}' claims crew '{role.crew}'"
                )

    def test_unspecced_crews_are_still_internally_consistent(self):
        """No YAML is not an excuse for a crew that breaks its own ceilings."""
        for name, crew in CREWS.items():
            for step in crew.steps:
                role = ROLE_REGISTRY[step.role]
                assert role.crew == name, f"{name}: step '{step.role}' belongs to '{role.crew}'"
                for tool in step.tools:
                    assert tool in role.tools, (
                        f"{name}: step '{step.role}' names '{tool}', not in its bindings"
                    )
