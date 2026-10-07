"""Phase 16, item 1: card dependencies ("this card waits for that card").

The two things worth testing are the ones the *column* cannot express:

* a card waiting on unfinished work must not reach ``Running``, and
* a self-edge or a cycle must be refused when the edge is created, because a
  deadlock is only discoverable at move time if it is allowed to exist first.

The negative cases matter most - most of these tests assert that something is
**not** allowed, because the failure mode is a card that runs past work it was
supposed to wait for.
"""
from __future__ import annotations

import pytest

from kanban_core.dependencies import (
    DependencyCodes,
    DependencyError,
    build_tree,
    dependency_ids,
    edge_problems,
    reachable,
)
from kanban_core.models import Column
from kanban_core.service import KanbanService
from kanban_core.state_machine import TransitionError
from kanban_core.store import Store


@pytest.fixture()
def svc() -> KanbanService:
    s = Store(":memory:")
    service = KanbanService(s)
    service.create_board("eng", board_id="brd_eng")
    return service


def _card(svc: KanbanService, title: str, **kw):
    # An assignee is required before a card can enter Running (NO_ASSIGNEE guard),
    # so every card here is assigned - otherwise the dependency guard under test
    # would be masked by an unrelated one.
    kw.setdefault("assignee", "agent-1")
    kw.setdefault("crew", "recon")
    return svc.create_card(title, "brd_eng", **kw)


def _running(svc: KanbanService, card):
    """Walk a card all the way to Running, so it can be a satisfied dependency."""
    svc.move_card(card.card_id, Column.ASSIGNED)
    svc.move_card(card.card_id, Column.RUNNING)
    svc.move_card(card.card_id, Column.REVIEW)
    svc.move_card(card.card_id, Column.DONE)
    return svc.get_card(card.card_id)


# ---------------------------------------------------------------------------
# edges
# ---------------------------------------------------------------------------
class TestEdges:
    def test_dependency_round_trips_through_meta(self, svc):
        a = _card(svc, "prereq")
        b = _card(svc, "dependant")
        svc.add_dependency(b.card_id, a.card_id)
        assert dependency_ids(svc.get_card(b.card_id)) == [a.card_id]

    def test_self_dependency_is_refused(self, svc):
        a = _card(svc, "solo")
        with pytest.raises(DependencyError) as exc:
            svc.add_dependency(a.card_id, a.card_id)
        assert any(r.startswith(DependencyCodes.SELF) for r in exc.value.reasons)

    def test_two_card_cycle_is_refused(self, svc):
        a = _card(svc, "a")
        b = _card(svc, "b")
        svc.add_dependency(a.card_id, b.card_id)
        with pytest.raises(DependencyError) as exc:
            svc.add_dependency(b.card_id, a.card_id)
        assert any(r.startswith(DependencyCodes.CYCLE) for r in exc.value.reasons)

    def test_cycle_refusal_names_the_loop(self, svc):
        a = _card(svc, "a")
        b = _card(svc, "b")
        svc.add_dependency(a.card_id, b.card_id)
        with pytest.raises(DependencyError) as exc:
            svc.add_dependency(b.card_id, a.card_id)
        # An operator acts on a concrete loop, not on the word "cycle".
        assert "->" in " ".join(exc.value.reasons)

    def test_longer_cycle_is_refused(self, svc):
        a, b, c = (_card(svc, n) for n in "abc")
        svc.add_dependency(a.card_id, b.card_id)
        svc.add_dependency(b.card_id, c.card_id)
        with pytest.raises(DependencyError):
            svc.add_dependency(c.card_id, a.card_id)

    def test_dependency_on_unknown_card_is_not_found(self, svc):
        a = _card(svc, "a")
        from kanban_core.service import NotFound

        with pytest.raises(NotFound):
            svc.add_dependency(a.card_id, "card_does_not_exist")

    def test_duplicate_edge_is_idempotent(self, svc):
        a = _card(svc, "a")
        b = _card(svc, "b")
        svc.add_dependency(b.card_id, a.card_id)
        svc.add_dependency(b.card_id, a.card_id)
        assert dependency_ids(svc.get_card(b.card_id)) == [a.card_id]

    def test_remove_is_idempotent(self, svc):
        a = _card(svc, "a")
        b = _card(svc, "b")
        svc.add_dependency(b.card_id, a.card_id)
        svc.remove_dependency(b.card_id, a.card_id)
        svc.remove_dependency(b.card_id, a.card_id)
        assert dependency_ids(svc.get_card(b.card_id)) == []


class TestEdgeValidation:
    """The pure predicate, tested without a store."""

    def test_diamond_is_not_a_cycle(self):
        # d waits on b and c, which both wait on a. Not a cycle.
        edges = {"b": ["a"], "c": ["a"], "d": ["b", "c"]}
        assert edge_problems("d", "b", edges=edges) == []

    def test_reachable_is_bounded_on_a_chain(self):
        edges = {f"n{i}": [f"n{i + 1}"] for i in range(100)}
        assert len(reachable("n0", edges)) <= 100

    def test_reachable_tolerates_a_loop(self):
        # reachable() must terminate even on data that already contains a loop.
        edges = {"a": ["b"], "b": ["a"]}
        assert reachable("a", edges) == {"b", "a"}


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------
class TestMoveGuard:
    def test_waiting_card_cannot_reach_running(self, svc):
        prereq = _card(svc, "prereq")
        dependant = _card(svc, "dependant")
        svc.add_dependency(dependant.card_id, prereq.card_id)
        svc.move_card(dependant.card_id, Column.ASSIGNED)
        with pytest.raises(TransitionError) as exc:
            svc.move_card(dependant.card_id, Column.RUNNING)
        assert any(r.startswith(DependencyCodes.UNMET) for r in exc.value.reasons)

    def test_card_runs_once_the_prerequisite_is_done(self, svc):
        prereq = _card(svc, "prereq")
        dependant = _card(svc, "dependant")
        svc.add_dependency(dependant.card_id, prereq.card_id)
        _running(svc, prereq)
        svc.move_card(dependant.card_id, Column.ASSIGNED)
        card = svc.move_card(dependant.card_id, Column.RUNNING)
        assert card.column is Column.RUNNING

    def test_blocked_prerequisite_does_not_release_the_dependant(self, svc):
        """Blocked is 'parked', not 'finished'.

        If a blocked prerequisite counted as satisfied, a card stuck on a human
        would silently release everything downstream of it - exactly the
        phantom-progress the guard exists to prevent.
        """
        prereq = _card(svc, "prereq")
        dependant = _card(svc, "dependant")
        svc.add_dependency(dependant.card_id, prereq.card_id)
        svc.move_card(prereq.card_id, Column.ASSIGNED)
        svc.move_card(prereq.card_id, Column.BLOCKED, force=True)
        svc.move_card(dependant.card_id, Column.ASSIGNED)
        with pytest.raises(TransitionError):
            svc.move_card(dependant.card_id, Column.RUNNING)

    def test_force_is_the_operator_override(self, svc):
        prereq = _card(svc, "prereq")
        dependant = _card(svc, "dependant")
        svc.add_dependency(dependant.card_id, prereq.card_id)
        svc.move_card(dependant.card_id, Column.ASSIGNED)
        card = svc.move_card(dependant.card_id, Column.RUNNING, force=True, actor="operator")
        assert card.column is Column.RUNNING

    def test_force_is_recorded_in_the_event(self, svc):
        prereq = _card(svc, "prereq")
        dependant = _card(svc, "dependant")
        svc.add_dependency(dependant.card_id, prereq.card_id)
        svc.move_card(dependant.card_id, Column.ASSIGNED)
        svc.move_card(dependant.card_id, Column.RUNNING, force=True, actor="operator")
        events = svc.store.list_events(card_id=dependant.card_id, limit=100)
        moved = [e for e in events if e.to_column == "Running"]
        assert moved and moved[-1].payload.get("forced") is True

    def test_dependency_only_gates_running(self, svc):
        """Backlog -> Assigned is not gated; a card must be able to be picked up."""
        prereq = _card(svc, "prereq")
        dependant = _card(svc, "dependant")
        svc.add_dependency(dependant.card_id, prereq.card_id)
        assert svc.move_card(dependant.card_id, Column.ASSIGNED).column is Column.ASSIGNED


# ---------------------------------------------------------------------------
# the view + tree
# ---------------------------------------------------------------------------
class TestView:
    def test_card_view_exposes_blockers(self, svc):
        prereq = _card(svc, "prereq")
        dependant = _card(svc, "dependant")
        svc.add_dependency(dependant.card_id, prereq.card_id)
        view = svc.card_view(svc.get_card(dependant.card_id))
        assert view["waiting"] is True
        assert view["depends_on"] == [prereq.card_id]
        assert view["blockers"][0]["satisfied"] is False

    def test_waiting_clears_when_prerequisite_finishes(self, svc):
        prereq = _card(svc, "prereq")
        dependant = _card(svc, "dependant")
        svc.add_dependency(dependant.card_id, prereq.card_id)
        _running(svc, prereq)
        view = svc.card_view(svc.get_card(dependant.card_id))
        assert view["waiting"] is False
        assert view["blockers"][0]["satisfied"] is True

    def test_missing_prerequisite_is_reported_not_hidden(self, svc):
        """A hand-edited document can name a card that does not exist.

        The API refuses to *create* such a card (see TestEdges), so this writes
        the document directly - which is exactly the state a hand-edit or an
        older version can leave behind. Skipping the dangling edge silently would
        let the card run past a prerequisite nobody can see.
        """
        dependant = _card(svc, "dependant")
        dependant.meta = {"depends_on": ["card_ghost"]}
        svc.store.save_card(dependant)
        view = svc.card_view(svc.get_card(dependant.card_id))
        assert view["blockers"][0]["missing"] is True
        assert view["waiting"] is True

    def test_creating_a_card_on_an_unknown_prerequisite_is_refused(self, svc):
        """The write path rejects a dangling edge rather than storing a dead wait."""
        with pytest.raises(DependencyError):
            _card(svc, "dependant", meta={"depends_on": ["card_ghost"]})

    def test_tree_nests_children_and_blockers(self, svc):
        parent = _card(svc, "parent")
        child = svc.create_subcard(parent.card_id, "child", scope={"targets": ["10.0.0.5"]})
        prereq = _card(svc, "prereq")
        svc.add_dependency(parent.card_id, prereq.card_id)
        tree = svc.card_tree(parent.card_id)
        assert tree["tree"]["child_count"] == 1
        assert tree["tree"]["children"][0]["card_id"] == child.card_id
        assert tree["tree"]["blockers"][0]["card_id"] == prereq.card_id
        assert tree["counts"]["nodes"] == 2

    def test_tree_depth_is_bounded(self, svc):
        root = _card(svc, "root")
        node = root
        for i in range(12):
            node = svc.create_subcard(node.card_id, f"c{i}", scope={"targets": ["10.0.0.5"]})
        tree = build_tree(
            svc.get_card(root.card_id),
            children_by_parent=svc._children_by_parent("brd_eng"),
            cards_by_id={c.card_id: c for c in svc.store.list_cards(board_id="brd_eng")},
            max_depth=3,
        )
        # Depth 0,1,2,3 with the last marked truncated.
        depth = 0
        cursor = tree["tree"]
        while cursor["children"]:
            depth += 1
            cursor = cursor["children"][0]
        assert depth == 3
        assert cursor["truncated"] is True
