"""Event bus, audit chain and service-layer lifecycle tests."""
from __future__ import annotations

import asyncio

import pytest

from kanban_core.bus import EventBus, drain
from kanban_core.models import Card, Column, Event, EventType, Scope, ToolBinding, Trace
from kanban_core.service import KanbanService
from kanban_core.state_machine import TransitionError
from kanban_core.store import Store, chain_hash, GENESIS


# ------------------------------------------------------------------- chain
class TestAuditChain:
    def test_genesis_is_zeros(self):
        assert GENESIS == "0" * 64

    def test_first_event_chains_from_genesis(self, tmp_path):
        store = Store(tmp_path / "a.db")
        ev = store.append_event(Event(type="test.one"))
        assert ev.seq == 1
        assert ev.audit_hash
        assert store.verify_chain()["ok"]

    def test_chain_links_prev_hash(self, tmp_path):
        store = Store(tmp_path / "b.db")
        first = store.append_event(Event(type="t1"))
        second = store.append_event(Event(type="t2"))
        rows = store.list_events()
        assert rows[1].audit_hash == second.audit_hash
        assert first.audit_hash != second.audit_hash
        verdict = store.verify_chain()
        assert verdict["ok"] and verdict["checked"] == 2 and verdict["chain_tip"] == 2

    def test_tampering_is_detected(self, tmp_path):
        store = Store(tmp_path / "c.db")
        for i in range(4):
            store.append_event(Event(type=f"t{i}", payload={"i": i}))
        assert store.verify_chain()["ok"]
        store._conn.execute("UPDATE events SET payload='{\"i\": 999}' WHERE seq=2")
        store._conn.commit()
        verdict = store.verify_chain()
        assert verdict["ok"] is False
        assert verdict["broken_at_seq"] == 2

    def test_deleting_a_middle_row_breaks_the_chain(self, tmp_path):
        store = Store(tmp_path / "d.db")
        for i in range(3):
            store.append_event(Event(type=f"t{i}"))
        store._conn.execute("DELETE FROM events WHERE seq=2")
        store._conn.commit()
        assert store.verify_chain()["ok"] is False

    def test_chain_hash_is_deterministic(self):
        record = {"a": 1, "b": [1, 2, 3]}
        assert chain_hash(GENESIS, record) == chain_hash(GENESIS, record)
        assert chain_hash(GENESIS, record) != chain_hash("1" * 64, record)


# --------------------------------------------------------------------- bus
class TestEventBus:
    def test_fanout_to_two_subscribers(self):
        bus = EventBus()
        q1, q2 = bus.subscribe(), bus.subscribe()
        bus.publish(Event(type="x"))
        assert len(drain(q1)) == 1
        assert len(drain(q2)) == 1

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish(Event(type="x"))
        assert drain(q) == []

    def test_recent_buffer_is_capped(self):
        bus = EventBus(replay=3)
        for i in range(10):
            bus.publish(Event(type=f"t{i}"))
        assert len(bus.recent(limit=99)) == 3
        assert bus.stats()["published"] == 10

    def test_slow_consumer_does_not_block_or_raise(self):
        bus = EventBus(queue_size=2)
        bus.subscribe()  # never drained
        for i in range(20):
            bus.publish(Event(type=f"t{i}"))  # must not raise
        assert bus.stats()["published"] == 20

    def test_publish_never_raises_without_subscribers(self):
        bus = EventBus()
        assert bus.publish(Event(type="lonely")).type == "lonely"

    @pytest.mark.asyncio
    async def test_async_consumer_receives_in_order(self):
        bus = EventBus()
        q = bus.subscribe()
        for i in range(5):
            bus.publish(Event(type=f"t{i}"))
        got = []
        for _ in range(5):
            got.append((await asyncio.wait_for(q.get(), timeout=1)).type)
        assert got == ["t0", "t1", "t2", "t3", "t4"]


# ----------------------------------------------------------------- service
@pytest.fixture()
def svc(tmp_path):
    store = Store(tmp_path / "svc.db")
    return KanbanService(store, EventBus())


class TestServiceLifecycle:
    def test_full_lifecycle_emits_ordered_events(self, svc):
        board = svc.create_board("Test", "agent")
        card = svc.create_card("scan example.com", board.board_id, assignee="recon-specialist")
        svc.assign_card(card.card_id, "recon-specialist", crew="recon")
        svc.move_card(card.card_id, Column.RUNNING, actor="recon-specialist", actor_is_agent=True)
        svc.move_card(card.card_id, Column.REVIEW, actor="recon-specialist", actor_is_agent=True)
        svc.move_card(card.card_id, Column.DONE, actor="operator")

        types = [e.type for e in svc.store.list_events()]
        assert types[0] == EventType.BOARD_CREATED
        assert EventType.CARD_CREATED in types
        assert EventType.CARD_ASSIGNED in types
        # assign_card performs Backlog->Assigned itself, then Running, Review, Done.
        assert types.count(EventType.CARD_MOVED) == 4
        assert svc.get_card(card.card_id).column is Column.DONE
        assert svc.chain_verdict()["ok"]

    def test_move_emits_from_and_to_columns(self, svc):
        board = svc.create_board("B", "agent")
        card = svc.create_card("c", board.board_id, assignee="a")
        svc.move_card(card.card_id, Column.ASSIGNED, actor="orch")
        moved = [e for e in svc.store.list_events() if e.type == EventType.CARD_MOVED][-1]
        assert moved.from_column == "Backlog" and moved.to_column == "Assigned"
        assert moved.card_id == card.card_id and moved.actor == "orch"

    def test_illegal_move_raises_and_does_not_mutate(self, svc):
        board = svc.create_board("B", "agent")
        card = svc.create_card("c", board.board_id, assignee="a")
        with pytest.raises(TransitionError):
            svc.move_card(card.card_id, Column.DONE)
        assert svc.get_card(card.card_id).column is Column.BACKLOG

    def test_events_published_on_bus(self, svc):
        board = svc.create_board("B", "agent")
        q = svc.bus.subscribe()
        svc.create_card("c", board.board_id)
        types = [e.type for e in drain(q)]
        assert EventType.CARD_CREATED in types

    def test_assign_moves_backlog_card_to_assigned(self, svc):
        board = svc.create_board("B", "agent")
        card = svc.create_card("c", board.board_id)
        svc.assign_card(card.card_id, "recon-specialist", crew="recon")
        assert svc.get_card(card.card_id).column is Column.ASSIGNED

    def test_block_and_unblock(self, svc):
        board = svc.create_board("B", "agent")
        card = svc.create_card("c", board.board_id, assignee="a", column=Column.ASSIGNED)
        svc.block_card(card.card_id, "waiting on credentials")
        blocked = svc.get_card(card.card_id)
        assert blocked.column is Column.BLOCKED
        assert blocked.blocked_reason == "waiting on credentials"
        svc.move_card(card.card_id, Column.ASSIGNED, actor="operator")
        assert svc.get_card(card.card_id).blocked_reason is None

    def test_kill_freezes_the_card(self, svc):
        board = svc.create_board("B", "agent")
        card = svc.create_card("c", board.board_id, assignee="a", column=Column.RUNNING)
        killed = svc.kill_card(card.card_id, actor="operator", reason="stop")
        assert killed.killed and killed.column is Column.RUNNING
        with pytest.raises(TransitionError):
            svc.move_card(card.card_id, Column.REVIEW)

    def test_child_cards_link_to_parent(self, svc):
        board = svc.create_board("B", "engagement")
        parent = svc.create_card("scan subnet", board.board_id)
        child = svc.create_card("nmap run", board.board_id, parent_id=parent.card_id)
        kids = svc.list_cards(parent_id=parent.card_id)
        assert [c.card_id for c in kids] == [child.card_id]

    def test_trace_and_artifact_attach_to_card(self, svc):
        board = svc.create_board("B", "engagement")
        card = svc.create_card("c", board.board_id)
        svc.add_trace(
            card.card_id,
            Trace(tool="nmap_scan", tier=1, status="ok", duration_ms=412, dry_run=True, audit_hash="deadbeef"),
        )
        svc.add_artifact(
            card.card_id,
            {"name": "scan.json", "kind": "scan-output", "sha256": "abc", "bytes": 128},
        )
        reloaded = svc.get_card(card.card_id)
        assert len(reloaded.traces) == 1 and reloaded.traces[0].tool == "nmap_scan"
        assert len(reloaded.artifacts) == 1 and reloaded.artifacts[0].bytes == 128
        types = [e.type for e in svc.store.list_events()]
        assert EventType.CARD_TRACE_ADDED in types and EventType.CARD_ARTIFACT_ADDED in types

    def test_board_view_groups_by_column(self, svc):
        board = svc.create_board("B", "agent")
        svc.create_card("a", board.board_id)
        svc.create_card("b", board.board_id, assignee="x", column=Column.ASSIGNED)
        view = svc.board_view(board.board_id)
        assert view["total"] == 2
        assert len(view["columns"]["Backlog"]) == 1
        assert len(view["columns"]["Assigned"]) == 1
        assert view["counts"]["Done"] == 0

    def test_unknown_board_and_card_raise(self, svc):
        from kanban_core.service import NotFound

        with pytest.raises(NotFound):
            svc.get_board("nope")
        with pytest.raises(NotFound):
            svc.get_card("nope")
        with pytest.raises(NotFound):
            svc.create_card("c", "nope")


class TestApprovalGate:
    def _gated_card(self, svc, board_id):
        return svc.create_card(
            "web assessment",
            board_id,
            assignee="web-specialist",
            scope=Scope(targets=["example.com"]),
            tools=[ToolBinding(name="nikto_scan", tier=2, args={"target": "example.com"})],
            requires_approval=True,
        )

    def test_gated_card_cannot_run_before_approval(self, svc):
        board = svc.create_board("B", "engagement")
        card = self._gated_card(svc, board.board_id)
        svc.move_card(card.card_id, Column.ASSIGNED)
        with pytest.raises(TransitionError) as exc:
            svc.move_card(card.card_id, Column.RUNNING, actor_is_agent=True)
        assert any("approval" in r for r in exc.value.reasons)

    def test_approval_request_then_approve_unlocks_running(self, svc):
        board = svc.create_board("B", "engagement")
        card = self._gated_card(svc, board.board_id)
        svc.move_card(card.card_id, Column.ASSIGNED)
        svc.request_approval(card.card_id, reason="T2 nikto needs a human gate", tool="nikto_scan", tier=2)
        pending = svc.get_card(card.card_id).pending_approval
        assert pending is not None
        svc.decide_approval(card.card_id, pending.id, approved=True, decided_by="operator")
        assert svc.get_card(card.card_id).approved
        svc.move_card(card.card_id, Column.RUNNING, actor_is_agent=True)  # now permitted
        assert svc.get_card(card.card_id).column is Column.RUNNING

    def test_rejection_keeps_card_gated(self, svc):
        board = svc.create_board("B", "engagement")
        card = self._gated_card(svc, board.board_id)
        svc.move_card(card.card_id, Column.ASSIGNED)
        svc.request_approval(card.card_id, reason="gate")
        pending = svc.get_card(card.card_id).pending_approval
        svc.decide_approval(card.card_id, pending.id, approved=False, decided_by="operator", note="out of scope")
        with pytest.raises(TransitionError) as exc:
            svc.move_card(card.card_id, Column.RUNNING, actor_is_agent=True)
        assert any("approval_rejected" in r for r in exc.value.reasons)

    def test_double_decide_is_rejected(self, svc):
        board = svc.create_board("B", "engagement")
        card = self._gated_card(svc, board.board_id)
        svc.move_card(card.card_id, Column.ASSIGNED)
        svc.request_approval(card.card_id, reason="gate")
        pid = svc.get_card(card.card_id).pending_approval.id
        svc.decide_approval(card.card_id, pid, approved=True)
        with pytest.raises(TransitionError):
            svc.decide_approval(card.card_id, pid, approved=False)

    def test_approval_events_emitted(self, svc):
        board = svc.create_board("B", "engagement")
        card = self._gated_card(svc, board.board_id)
        svc.request_approval(card.card_id, reason="gate")
        pid = svc.get_card(card.card_id).pending_approval.id
        svc.decide_approval(card.card_id, pid, approved=True)
        types = [e.type for e in svc.store.list_events()]
        assert EventType.CARD_APPROVAL_REQUESTED in types
        assert EventType.CARD_APPROVAL_DECIDED in types


class TestReplayAndMetrics:
    def test_replay_returns_full_history(self, svc):
        board = svc.create_board("B", "agent")
        card = svc.create_card("c", board.board_id, assignee="a")
        svc.move_card(card.card_id, Column.ASSIGNED)
        svc.add_trace(card.card_id, Trace(tool="nmap_scan", tier=1, duration_ms=100))
        replay = svc.replay_card(card.card_id)
        assert replay["card_id"] == card.card_id
        assert replay["current_column"] == "Assigned"
        assert len(replay["traces"]) == 1
        assert any(e["type"] == EventType.CARD_MOVED for e in replay["events"])
        assert all("audit_hash" in e for e in replay["events"])

    def test_board_metrics_aggregate(self, svc):
        board = svc.create_board("B", "agent")
        card = svc.create_card("c", board.board_id, assignee="a")
        svc.add_trace(card.card_id, Trace(tool="t", tier=0, duration_ms=200, args={"__tokens": 500, "__cost_usd": 0.01}))
        metrics = svc.board_metrics(board.board_id)
        assert metrics["cards"] == 1
        assert metrics["tool_runs"] == 1
        assert metrics["tokens"] == 500
        assert metrics["cost_usd"] == 0.01
        assert metrics["avg_latency_ms"] == 200

    def test_pending_approvals_surface(self, svc):
        board = svc.create_board("B", "engagement")
        card = svc.create_card("c", board.board_id, requires_approval=True)
        svc.request_approval(card.card_id, reason="gate")
        assert svc.get_card(card.card_id).pending_approval is not None
        assert any(c.pending_approval for c in svc.list_cards())
