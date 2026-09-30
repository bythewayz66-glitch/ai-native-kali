"""Phase 8, item 2: the remediation crew and its finding -> sub-card spawn path.

The spawn path runs against a REAL kanban-core (in-process TestClient), so the
narrowing rule under test is the one the board actually enforces - not a copy of
it. That is the point: the bridge proposes a scope and the board decides, and
these tests exercise both halves of that.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_runtime.bridge import Bridge
from agent_runtime.client import KanbanError
from agent_runtime.crews import CREWS, get_crew
from agent_runtime.remediation import (
    derive_scope,
    finding_targets,
    remediation_title,
    spawn_remediation,
)
from agent_runtime.roles import ROLE_REGISTRY
from kanban_core.api import build_app as build_kanban_app
from kanban_core.bus import EventBus
from kanban_core.store import Store


# ------------------------------------------------------------- fixtures
@pytest.fixture()
def kanban(tmp_path):
    import os

    os.environ["KANBAN_SEED"] = "1"
    app = build_kanban_app(Store(tmp_path / "kanban.db"), EventBus())
    with TestClient(app) as client:
        yield client


class _Shim:
    """Minimal KanbanClient-shaped adapter over an in-process TestClient."""

    def __init__(self, test_client: TestClient) -> None:
        self._c = test_client
        self.base_url = "in-process"

    def get(self, path, **params):
        r = self._c.get(path, params=params or None)
        if r.status_code >= 400:
            raise KanbanError(f"GET {path} -> {r.status_code}", r.status_code, r.json())
        return r.json()

    def post(self, path, payload=None):
        r = self._c.post(path, json=payload or {})
        if r.status_code >= 400:
            raise KanbanError(
                f"POST {path} -> {r.status_code}", r.status_code, r.json().get("detail")
            )
        return r.json()

    def create_subcard(self, parent_id, **payload):
        return self.post(f"/api/cards/{parent_id}/subcards", payload)

    def card(self, card_id):
        return self.get(f"/api/cards/{card_id}")


@pytest.fixture()
def shim(kanban):
    return _Shim(kanban)


def _parent(kanban, **kw):
    """A scoped parent card on the seeded engagement board."""
    board = kanban.get("/api/boards").json()["boards"][0]
    payload = {
        "title": "parent engagement",
        "board_id": board["board_id"],
        "scope": {"targets": ["10.0.0.5"], "cidrs": ["10.0.0.0/24"]},
    }
    payload.update(kw)
    r = kanban.post("/api/cards", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------- crew selection
class TestCrewSelection:
    def test_remediation_crew_is_registered(self):
        assert "remediation" in CREWS
        crew = get_crew("remediation")
        assert crew is not None
        assert crew.roles == ["remediation-specialist"]

    def test_remediation_crew_is_tier_1(self):
        """Remediation verifies a fix; it does not re-exploit."""
        assert CREWS["remediation"].max_tier == 1

    def test_remediation_role_is_registered_and_back_references_the_crew(self):
        role = ROLE_REGISTRY["remediation-specialist"]
        assert role.crew == "remediation"
        assert role.max_tier == 1

    def test_every_crew_tool_is_bound_to_its_role(self):
        crew = CREWS["remediation"]
        role = ROLE_REGISTRY["remediation-specialist"]
        for tool in crew.tools():
            assert tool in role.tools

    def test_a_card_naming_the_remediation_crew_resolves_to_it(self, shim, kanban):
        """The bridge resolves a card's crew by name before any fallback."""
        parent = _parent(kanban)
        bridge = Bridge(client=shim)
        card = {"crew": "remediation", "board_id": parent["board_id"]}
        assert bridge._resolve_crew(card).name == "remediation"

    def test_role_resolution_picks_the_remediation_specialist(self, shim, kanban):
        parent = _parent(kanban)
        bridge = Bridge(client=shim)
        crew = CREWS["remediation"]
        card = {"assignee": "remediation-specialist", "board_id": parent["board_id"]}
        assert bridge._resolve_role(card, crew) == "remediation-specialist"


# ------------------------------------------------------- scope derivation
class TestScopeDerivation:
    def test_finding_target_is_used(self):
        scope = derive_scope({"target": "10.0.0.5"}, {"targets": ["10.0.0.0/24"]})
        assert scope == {"targets": ["10.0.0.5"]}

    def test_finding_targets_list_is_used(self):
        scope = derive_scope({"targets": ["10.0.0.5", "10.0.0.6"]}, None)
        assert scope == {"targets": ["10.0.0.5", "10.0.0.6"]}

    def test_finding_without_a_target_inherits_the_parent_scope(self):
        """Never 'no scope' - an absent scope is the widest one, not the narrowest."""
        parent = {"targets": ["10.0.0.5"], "cidrs": ["10.0.0.0/24"]}
        assert derive_scope({"statement": "x"}, parent) == parent

    def test_finding_without_a_target_falls_back_to_the_card_target(self):
        scope = derive_scope({"statement": "x"}, None, card_target="10.0.0.9")
        assert scope == {"targets": ["10.0.0.9"]}

    def test_finding_with_no_target_anywhere_and_no_parent_scope(self):
        assert derive_scope({"statement": "x"}, None) is None

    def test_finding_targets_reads_every_supported_key(self):
        """Order is by specificity: an explicit target beats a bare host."""
        assert finding_targets({"host": "a", "asset": "b", "target": "c"}) == ["c", "a", "b"]

    def test_finding_targets_de_duplicates(self):
        assert finding_targets({"target": "a", "host": "a"}) == ["a"]

    def test_title_is_derived_from_the_statement(self):
        assert remediation_title({"statement": "Apache is unpatched"}).startswith("Remediate:")

    def test_title_is_truncated(self):
        title = remediation_title({"statement": "x" * 200})
        assert len(title) <= 80

    def test_title_falls_back_when_there_is_no_statement(self):
        assert remediation_title({}, index=2) == "Remediate finding 3"


# ------------------------------------------------------- the spawn path
class TestSpawnPath:
    def test_spawn_creates_a_child_with_a_narrowed_scope(self, shim, kanban):
        parent = _parent(kanban)
        results = spawn_remediation(
            shim,
            {"id": parent["card_id"], "scope": parent["scope"]},
            [{"statement": "unpatched apache", "target": "10.0.0.5", "confidence": 0.9}],
        )
        assert results[0]["status"] == "created"
        child = kanban.get(f"/api/cards/{results[0]['child_id']}").json()
        assert child["parent_id"] == parent["card_id"]
        assert child["scope"]["targets"] == ["10.0.0.5"]
        assert child["crew"] == "remediation"

    def test_spawned_child_scope_is_a_strict_narrowing(self, shim, kanban):
        """The child's scope is inside the parent's - checked with the shared helper."""
        from kanban_core.models import Scope
        from kanban_core.scope_model import is_subset

        parent = _parent(kanban)
        results = spawn_remediation(
            shim,
            {"id": parent["card_id"], "scope": parent["scope"]},
            [{"statement": "finding", "target": "10.0.0.5"}],
        )
        child = kanban.get(f"/api/cards/{results[0]['child_id']}").json()
        assert is_subset(Scope.model_validate(child["scope"]), Scope.model_validate(parent["scope"]))

    def test_spawn_is_refused_when_the_finding_names_an_out_of_scope_target(self, shim, kanban):
        """The board refuses the widening; the bridge reports it, does not crash."""
        parent = _parent(kanban)
        results = spawn_remediation(
            shim,
            {"id": parent["card_id"], "scope": parent["scope"]},
            [{"statement": "finding", "target": "8.8.8.8"}],
        )
        assert results[0]["status"] == "refused"
        assert results[0]["reasons"]

    def test_refusal_is_counted_on_the_stats(self, shim, kanban):
        from agent_runtime.bridge import BridgeStats

        parent = _parent(kanban)
        stats = BridgeStats()
        spawn_remediation(
            shim,
            {"id": parent["card_id"], "scope": parent["scope"]},
            [{"statement": "finding", "target": "8.8.8.8"}],
            stats=stats,
        )
        assert stats.remediation_refused == 1
        assert stats.remediation_spawned == 0

    def test_spawn_counts_successes(self, shim, kanban):
        from agent_runtime.bridge import BridgeStats

        parent = _parent(kanban)
        stats = BridgeStats()
        spawn_remediation(
            shim,
            {"id": parent["card_id"], "scope": parent["scope"]},
            [{"statement": "finding", "target": "10.0.0.5"}],
            stats=stats,
        )
        assert stats.remediation_spawned == 1

    def test_finding_without_a_statement_is_skipped(self, shim, kanban):
        parent = _parent(kanban)
        results = spawn_remediation(
            shim, {"id": parent["card_id"], "scope": parent["scope"]}, [{"target": "10.0.0.5"}]
        )
        assert results[0]["status"] == "skipped"

    def test_spawn_is_bounded(self, shim, kanban):
        parent = _parent(kanban)
        findings = [{"statement": f"f{i}", "target": "10.0.0.5"} for i in range(20)]
        results = spawn_remediation(
            shim, {"id": parent["card_id"], "scope": parent["scope"]}, findings, max_children=3
        )
        assert len(results) == 3

    def test_spawn_records_the_audit_trail(self, shim, kanban):
        parent = _parent(kanban)
        results = spawn_remediation(
            shim,
            {"id": parent["card_id"], "scope": parent["scope"]},
            [{"statement": "finding", "target": "10.0.0.5"}],
        )
        child_id = results[0]["child_id"]
        events = kanban.get("/api/audit", params={"card_id": child_id}).json()["events"]
        assert any(e["type"] == "card.subcard.created" for e in events)

    def test_chain_still_verifies_after_a_spawn(self, shim, kanban):
        parent = _parent(kanban)
        spawn_remediation(
            shim,
            {"id": parent["card_id"], "scope": parent["scope"]},
            [{"statement": "finding", "target": "10.0.0.5"}],
        )
        assert kanban.get("/api/audit/verify").json()["ok"] is True


# ------------------------------------------------------- bridge integration
class TestBridgeIntegration:
    def test_bridge_spawns_from_a_run_with_findings(self, shim, kanban):
        """The bridge's own hook turns a run's findings into sub-cards."""
        parent = _parent(kanban)
        bridge = Bridge(client=shim)

        class _Run:
            findings = [{"statement": "unpatched apache", "target": "10.0.0.5"}]

        results = bridge._spawn_remediation(
            {"id": parent["card_id"], "scope": parent["scope"]}, _Run()
        )
        assert results[0]["status"] == "created"
        assert bridge.stats.remediation_spawned == 1

    def test_bridge_spawn_is_a_no_op_without_findings(self, shim, kanban):
        parent = _parent(kanban)
        bridge = Bridge(client=shim)

        class _Run:
            findings = []

        assert bridge._spawn_remediation({"id": parent["card_id"]}, _Run()) == []
        assert bridge.stats.remediation_spawned == 0

    def test_bridge_spawn_never_raises_on_a_refusal(self, shim, kanban):
        parent = _parent(kanban)
        bridge = Bridge(client=shim)

        class _Run:
            findings = [{"statement": "finding", "target": "8.8.8.8"}]

        results = bridge._spawn_remediation(
            {"id": parent["card_id"], "scope": parent["scope"]}, _Run()
        )
        assert results[0]["status"] == "refused"
        assert bridge.stats.remediation_refused == 1

    def test_stats_are_surfaced_on_health(self, shim, kanban):
        bridge = Bridge(client=shim)
        payload = bridge.stats.as_dict()
        assert "remediation_spawned" in payload
        assert "remediation_refused" in payload


# ------------------------------------------------------- no-model fallback
class TestNoModelFallback:
    def test_bridge_without_a_model_uses_the_deterministic_adapter(self, shim):
        """No model configured is the normal CI case, not an error."""
        bridge = Bridge(client=shim)
        assert bridge.llm_runner is None
        assert bridge.adapter is not None

    def test_spawn_path_needs_no_model(self, shim, kanban):
        """The spawn is deterministic, so it works in exactly the conditions
        where the crew's run falls back to the deterministic adapter."""
        parent = _parent(kanban)
        bridge = Bridge(client=shim)
        assert bridge.model is None

        class _Run:
            findings = [{"statement": "finding", "target": "10.0.0.5"}]

        results = bridge._spawn_remediation(
            {"id": parent["card_id"], "scope": parent["scope"]}, _Run()
        )
        assert results[0]["status"] == "created"

    def test_remediation_crew_runs_on_the_deterministic_adapter(self, shim, kanban):
        """The crew itself is runnable with no model attached."""
        from agent_runtime.crewai_adapter import CrewAIAdapter

        parent = _parent(kanban)
        # The executor must return a real outcome dict: the adapter counts a step
        # as "ran" only when a call reports status ok/dry_run, so a bare {} would
        # (correctly) leave the crew with nothing run and status "blocked".
        bridge = Bridge(
            client=shim,
            adapter=CrewAIAdapter(
                tool_executor=lambda *a, **k: {"status": "dry_run", "dry_run": True, "duration_ms": 1}
            ),
        )
        crew = CREWS["remediation"]
        card = {
            "id": parent["card_id"],
            "title": "remediate",
            "scope": parent["scope"],
            "tools": [],
        }
        run = bridge._run_crew(crew, card, "remediation-specialist", "none")
        assert run is not None
        assert run.status in ("ok", "partial", "error")
