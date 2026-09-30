"""Phase 8, item 1: the sub-card scope model.

The invariant under test: **a child's scope must be a subset of its parent's,
never wider.** The tests are grouped by the way that invariant can be violated,
because the interesting cases are the ones a naive set-subset check waves through.
"""
from __future__ import annotations

import pytest

from kanban_core.models import Column, Scope
from kanban_core.scope_model import (
    SubCardCodes,
    ScopeNarrowingError,
    inherit_scope,
    is_subset,
    narrowing_verdict,
    parent_tier_ceiling,
    validate_child,
    widening_reasons,
)
from kanban_core.service import KanbanService
from kanban_core.store import Store


@pytest.fixture()
def svc() -> KanbanService:
    s = Store(":memory:")
    service = KanbanService(s)
    service.create_board("eng", board_id="brd_eng")
    return service


def _parent(svc: KanbanService, **kw) -> object:
    defaults = dict(
        scope={"targets": ["10.0.0.5"], "cidrs": ["10.0.0.0/24"]},
        tools=[{"name": "nmap", "tier": 2}],
    )
    defaults.update(kw)
    return svc.create_card("parent", "brd_eng", **defaults)


# ---------------------------------------------------------------------------
# the subset predicate itself
# ---------------------------------------------------------------------------
class TestSubsetPredicate:
    def test_bounded_child_inside_bounded_parent(self):
        parent = Scope(targets=["10.0.0.5"], cidrs=["10.0.0.0/24"])
        child = Scope(targets=["10.0.0.5"])
        assert is_subset(child, parent)

    def test_child_target_outside_parent_is_not_subset(self):
        parent = Scope(targets=["10.0.0.5"])
        child = Scope(targets=["8.8.8.8"])
        assert not is_subset(child, parent)

    def test_absent_child_scope_is_a_widening_not_a_subset(self):
        """The case a naive `set(child) <= set(parent)` check gets wrong.

        An absent scope is the *widest* scope, not the empty one, so a child with
        no scope under a scoped parent is unbounded - a widening.
        """
        parent = Scope(targets=["10.0.0.5"])
        assert not is_subset(None, parent)
        assert not is_subset(Scope(), parent)

    def test_absent_parent_scope_contains_anything(self):
        assert is_subset(Scope(targets=["8.8.8.8"]), None)
        assert is_subset(None, None)

    def test_cidr_containment_uses_real_subnets_not_prefixes(self):
        """`10.0.0.0/8` and `10.0.0.0/24` share a prefix; the first is wider.

        A string-prefix comparison gets this backwards for exactly the case that
        matters, so containment is checked with ipaddress.
        """
        parent = Scope(cidrs=["10.0.0.0/24"])
        assert is_subset(Scope(cidrs=["10.0.0.0/25"]), parent)
        assert not is_subset(Scope(cidrs=["10.0.0.0/8"]), parent)

    def test_single_host_network_covered_by_parent_target(self):
        parent = Scope(targets=["10.0.0.5"])
        assert is_subset(Scope(cidrs=["10.0.0.5/32"]), parent)

    def test_widening_reasons_name_the_escaping_target(self):
        parent = Scope(targets=["10.0.0.5"])
        reasons = widening_reasons(Scope(targets=["8.8.8.8"]), parent)
        assert len(reasons) == 1
        assert "8.8.8.8" in reasons[0]
        assert SubCardCodes.SCOPE_WIDENED in reasons[0]


# ---------------------------------------------------------------------------
# inheritance
# ---------------------------------------------------------------------------
class TestInheritance:
    def test_omitted_scope_inherits_the_parents(self, svc):
        parent = _parent(svc)
        child = svc.create_subcard(parent.card_id, "child")
        assert child.scope is not None
        assert sorted(child.scope.targets) == ["10.0.0.5"]
        assert sorted(child.scope.cidrs) == ["10.0.0.0/24"]

    def test_inherited_scope_is_a_copy_not_the_same_object(self, svc):
        """A child that later narrows must not mutate the parent's scope."""
        parent = _parent(svc)
        child = svc.create_subcard(parent.card_id, "child")
        child.scope.targets.append("10.0.0.9")
        assert "10.0.0.9" not in svc.get_card(parent.card_id).scope.targets

    def test_inherit_scope_returns_none_for_unscoped_parent(self, svc):
        parent = svc.create_card("unscoped", "brd_eng")
        assert inherit_scope(parent) is None

    def test_child_of_unscoped_parent_may_be_scoped(self, svc):
        """Narrowing from unbounded is allowed - it is the safe direction."""
        parent = svc.create_card("unscoped", "brd_eng")
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        assert child.scope.targets == ["10.0.0.5"]


# ---------------------------------------------------------------------------
# narrowing
# ---------------------------------------------------------------------------
class TestNarrowing:
    def test_child_may_drop_targets(self, svc):
        parent = _parent(svc, scope={"targets": ["10.0.0.5", "10.0.0.6"]})
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        assert child.scope.targets == ["10.0.0.5"]

    def test_child_may_narrow_a_cidr(self, svc):
        parent = _parent(svc, scope={"cidrs": ["10.0.0.0/16"]})
        child = svc.create_subcard(parent.card_id, "child", scope={"cidrs": ["10.0.0.0/24"]})
        assert child.scope.cidrs == ["10.0.0.0/24"]

    def test_child_may_narrow_a_cidr_to_a_single_host(self, svc):
        parent = _parent(svc, scope={"cidrs": ["10.0.0.0/24"]})
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.7"]})
        assert child.scope.targets == ["10.0.0.7"]

    def test_child_may_add_authorization_provenance(self, svc):
        """Narrowing in time and provenance is still narrowing."""
        parent = _parent(svc)
        child = svc.create_subcard(
            parent.card_id,
            "child",
            scope={"targets": ["10.0.0.5"], "authorization_ref": "TICKET-9"},
        )
        assert child.scope.authorization_ref == "TICKET-9"

    def test_verdict_is_inherited_when_scope_is_unchanged(self, svc):
        parent = _parent(svc)
        child = svc.create_subcard(parent.card_id, "child")
        assert narrowing_verdict(parent, child.scope) == "inherited"

    def test_verdict_is_narrowed_when_scope_shrinks(self, svc):
        parent = _parent(svc, scope={"targets": ["10.0.0.5", "10.0.0.6"]})
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        assert narrowing_verdict(parent, child.scope) == "narrowed"


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------
class TestRefusals:
    def test_child_target_outside_parent_is_refused(self, svc):
        parent = _parent(svc)
        with pytest.raises(ScopeNarrowingError) as exc:
            svc.create_subcard(parent.card_id, "bad", scope={"targets": ["8.8.8.8"]})
        assert any(SubCardCodes.SCOPE_WIDENED in r for r in exc.value.reasons)

    def test_explicitly_empty_scope_is_refused_not_upgraded(self, svc):
        """An explicit empty scope is a widening, not a request to inherit.

        Silently upgrading it to the parent's would hide the caller's mistake.
        """
        parent = _parent(svc)
        with pytest.raises(ScopeNarrowingError) as exc:
            svc.create_subcard(parent.card_id, "bad", scope={})
        assert any(SubCardCodes.SCOPE_DROPPED in r for r in exc.value.reasons)

    def test_widened_cidr_is_refused(self, svc):
        parent = _parent(svc, scope={"cidrs": ["10.0.0.0/24"]})
        with pytest.raises(ScopeNarrowingError):
            svc.create_subcard(parent.card_id, "bad", scope={"cidrs": ["10.0.0.0/8"]})

    def test_tier_escalation_is_refused(self, svc):
        parent = _parent(svc)  # ceiling T2
        with pytest.raises(ScopeNarrowingError) as exc:
            svc.create_subcard(
                parent.card_id,
                "bad",
                scope={"targets": ["10.0.0.5"]},
                tools=[{"name": "exploit", "tier": 3}],
            )
        assert any(SubCardCodes.TIER_ESCALATED in r for r in exc.value.reasons)

    def test_child_at_the_parent_ceiling_is_allowed(self, svc):
        parent = _parent(svc)  # ceiling T2
        child = svc.create_subcard(
            parent.card_id,
            "ok",
            scope={"targets": ["10.0.0.5"]},
            tools=[{"name": "nmap", "tier": 2}],
        )
        assert child.max_tier == 2

    def test_container_card_may_declare_a_higher_ceiling(self, svc):
        """A container that wants to spawn T2 work must say so in a readable field."""
        parent = svc.create_card("container", "brd_eng", meta={"tier_ceiling": 2})
        assert parent_tier_ceiling(parent) == 2
        child = svc.create_subcard(
            parent.card_id, "ok", tools=[{"name": "nmap", "tier": 2}]
        )
        assert child.max_tier == 2

    def test_container_ceiling_is_clamped_to_the_tier_range(self, svc):
        parent = svc.create_card("container", "brd_eng", meta={"tier_ceiling": 99})
        assert parent_tier_ceiling(parent) == 3

    def test_child_of_killed_parent_is_refused(self, svc):
        parent = _parent(svc)
        svc.kill_card(parent.card_id, reason="stop")
        with pytest.raises(ScopeNarrowingError) as exc:
            svc.create_subcard(parent.card_id, "bad", scope={"targets": ["10.0.0.5"]})
        assert any(SubCardCodes.PARENT_KILLED in r for r in exc.value.reasons)

    def test_validate_child_reports_every_reason_at_once(self, svc):
        """A caller that stops at the first problem makes the operator fix them
        one round-trip at a time."""
        parent = _parent(svc)
        reasons = validate_child(
            parent, Scope(targets=["8.8.8.8"]), child_tier=3
        )
        assert len(reasons) == 2


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------
class TestAuditTrail:
    def test_child_creation_is_recorded_with_both_scopes(self, svc):
        parent = _parent(svc)
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        events = svc.store.list_events(card_id=child.card_id)
        created = [e for e in events if e.type == "card.subcard.created"]
        assert len(created) == 1
        payload = created[0].payload
        assert payload["parent_id"] == parent.card_id
        assert payload["child_id"] == child.card_id
        # Both summaries, so the *relationship* is auditable, not just the child.
        assert payload["parent_scope"] is not None
        assert payload["child_scope"] is not None
        assert payload["narrowing"] in ("inherited", "narrowed")

    def test_refused_spawn_is_recorded(self, svc):
        """A refusal an operator cannot see is one they will re-attempt."""
        parent = _parent(svc)
        with pytest.raises(ScopeNarrowingError):
            svc.create_subcard(parent.card_id, "bad", scope={"targets": ["8.8.8.8"]})
        events = svc.store.list_events(card_id=parent.card_id)
        refused = [e for e in events if e.type == "card.subcard.refused"]
        assert len(refused) == 1
        assert refused[0].payload["reasons"]

    def test_chain_still_verifies_after_subcard_activity(self, svc):
        parent = _parent(svc)
        svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        with pytest.raises(ScopeNarrowingError):
            svc.create_subcard(parent.card_id, "bad", scope={"targets": ["8.8.8.8"]})
        assert svc.store.verify_chain()["ok"] is True


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
class TestLifecycle:
    def _advance(self, svc, card_id, *columns):
        for col in columns:
            svc.move_card(card_id, col)

    def test_parent_cannot_finish_with_an_open_child(self, svc):
        parent = _parent(svc, tools=[])
        svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        svc.assign_card(parent.card_id, "recon")
        self._advance(svc, parent.card_id, "Running", "Review")
        with pytest.raises(Exception) as exc:
            svc.move_card(parent.card_id, "Done")
        assert any(SubCardCodes.OPEN_CHILDREN in r for r in exc.value.reasons)

    def test_parent_may_finish_once_children_are_done(self, svc):
        parent = _parent(svc, tools=[])
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        svc.assign_card(child.card_id, "recon")
        self._advance(svc, child.card_id, "Running", "Review", "Done")
        svc.assign_card(parent.card_id, "recon")
        self._advance(svc, parent.card_id, "Running", "Review", "Done")
        assert svc.get_card(parent.card_id).column is Column.DONE

    def test_child_of_killed_parent_cannot_run(self, svc):
        parent = _parent(svc, tools=[])
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        svc.kill_card(parent.card_id, reason="stop")
        svc.assign_card(child.card_id, "recon")
        with pytest.raises(Exception) as exc:
            svc.move_card(child.card_id, "Running")
        assert any(SubCardCodes.PARENT_KILLED in r for r in exc.value.reasons)

    def test_child_may_be_killed_without_affecting_the_parent(self, svc):
        parent = _parent(svc, tools=[])
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        svc.kill_card(child.card_id, reason="narrow the blast radius")
        assert svc.get_card(child.card_id).killed is True
        assert svc.get_card(parent.card_id).killed is False

    def test_card_view_exposes_family_counts(self, svc):
        parent = _parent(svc, tools=[])
        svc.create_subcard(parent.card_id, "c1", scope={"targets": ["10.0.0.5"]})
        svc.create_subcard(parent.card_id, "c2", scope={"targets": ["10.0.0.5"]})
        view = svc.card_view(svc.get_card(parent.card_id))
        assert view["child_count"] == 2
        assert view["open_child_count"] == 2

    def test_child_view_reports_a_killed_parent(self, svc):
        parent = _parent(svc, tools=[])
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        svc.kill_card(parent.card_id, reason="stop")
        assert svc.card_view(svc.get_card(child.card_id))["parent_killed"] is True
