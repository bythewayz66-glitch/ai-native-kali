"""State machine tests: declared edges + scope/approval guards."""
from __future__ import annotations

import pytest

from kanban_core.models import Card, Column, Scope, ToolBinding
from kanban_core.state_machine import (
    TransitionError,
    can_move,
    extract_targets,
    next_columns,
    validate_transition,
)


# --------------------------------------------------------------- helpers
def make_card(**kwargs) -> Card:
    base = dict(title="t", board_id="b", assignee="recon-specialist")
    base.update(kwargs)
    return Card(**base)


# ------------------------------------------------------------------ scope
class TestScope:
    def test_exact_target(self):
        scope = Scope(targets=["example.com"])
        assert scope.covers("example.com")
        assert scope.covers("EXAMPLE.COM")

    def test_url_is_reduced_to_host(self):
        scope = Scope(targets=["example.com"])
        assert scope.covers("https://example.com/admin?x=1")

    def test_host_port_is_reduced_to_host(self):
        scope = Scope(targets=["example.com"])
        assert scope.covers("example.com:8443")

    def test_subdomain_of_wildcard(self):
        scope = Scope(targets=["*.example.com"])
        assert scope.covers("api.example.com")
        assert scope.covers("example.com")

    def test_outside_scope(self):
        scope = Scope(targets=["example.com"])
        assert not scope.covers("evil.example.net")
        assert not scope.covers("notexample.com")  # suffix, not a real subdomain

    def test_cidr(self):
        scope = Scope(cidrs=["192.0.2.0/24"])
        assert scope.covers("192.0.2.55")
        assert not scope.covers("198.51.100.9")

    def test_ipv6_bare_and_bracketed(self):
        scope = Scope(targets=["2001:db8::1"])
        assert scope.covers("2001:db8::1")
        assert scope.covers("[2001:db8::1]:443")

    def test_expiry(self):
        expired = Scope(targets=["a.com"], expires_at="2020-01-01T00:00:00+00:00")
        assert expired.is_expired()
        live = Scope(targets=["a.com"], expires_at="2999-01-01T00:00:00+00:00")
        assert not live.is_expired()
        assert not Scope(targets=["a.com"]).is_expired()

    def test_summary(self):
        assert "empty" in Scope().summary()
        assert "a.com" in Scope(targets=["a.com"]).summary()


# ------------------------------------------------------------ transitions
class TestTransitionLegality:
    def test_happy_path_edges(self):
        card = make_card()
        for frm, to in [
            (Column.BACKLOG, Column.ASSIGNED),
            (Column.ASSIGNED, Column.RUNNING),
            (Column.RUNNING, Column.REVIEW),
            (Column.REVIEW, Column.DONE),
        ]:
            card.column = frm
            validate_transition(card, to)  # must not raise

    def test_backlog_to_running_is_illegal(self):
        card = make_card(column=Column.BACKLOG)
        with pytest.raises(TransitionError) as exc:
            validate_transition(card, Column.RUNNING)
        assert any("illegal_edge" in r for r in exc.value.reasons)

    def test_done_is_terminal(self):
        card = make_card(column=Column.DONE)
        with pytest.raises(TransitionError):
            validate_transition(card, Column.BACKLOG)

    def test_self_transition_rejected(self):
        card = make_card(column=Column.BACKLOG)
        with pytest.raises(TransitionError) as exc:
            validate_transition(card, Column.BACKLOG)
        assert any("self_transition" in r for r in exc.value.reasons)

    def test_blocked_returns_to_backlog_or_assigned_only(self):
        card = make_card(column=Column.BLOCKED)
        validate_transition(card, Column.BACKLOG)
        validate_transition(card, Column.ASSIGNED)
        with pytest.raises(TransitionError):
            validate_transition(card, Column.RUNNING)

    def test_review_can_bounce_back_to_running(self):
        card = make_card(column=Column.REVIEW)
        validate_transition(card, Column.RUNNING)

    def test_review_to_done_allowed(self):
        card = make_card(column=Column.REVIEW)
        validate_transition(card, Column.DONE)


class TestGuards:
    def test_requires_assignee_to_run(self):
        card = make_card(assignee=None, column=Column.ASSIGNED)
        ok, reasons = can_move(card, Column.RUNNING)
        assert not ok
        assert any("no_assignee" in r for r in reasons)

    def test_t2_without_scope_cannot_run(self):
        card = make_card(
            column=Column.ASSIGNED,
            tools=[ToolBinding(name="nikto_scan", tier=2, args={"target": "example.com"})],
        )
        ok, reasons = can_move(card, Column.RUNNING)
        assert not ok
        assert any("scope_missing" in r for r in reasons)

    def test_t2_out_of_scope_target_cannot_run(self):
        card = make_card(
            column=Column.ASSIGNED,
            scope=Scope(targets=["example.com"]),
            tools=[ToolBinding(name="nikto_scan", tier=2, args={"target": "evil.net"})],
        )
        ok, reasons = can_move(card, Column.RUNNING)
        assert not ok
        assert any("target_out_of_scope" in r for r in reasons)

    def test_t2_expired_scope_cannot_run(self):
        card = make_card(
            column=Column.ASSIGNED,
            scope=Scope(targets=["example.com"], expires_at="2020-01-01T00:00:00+00:00"),
            tools=[ToolBinding(name="nikto_scan", tier=2, args={"target": "example.com"})],
        )
        ok, reasons = can_move(card, Column.RUNNING)
        assert not ok
        assert any("scope_expired" in r for r in reasons)

    def test_t2_in_scope_but_gated_needs_approval(self):
        card = make_card(
            column=Column.ASSIGNED,
            scope=Scope(targets=["example.com"]),
            tools=[ToolBinding(name="nikto_scan", tier=2, args={"target": "example.com"})],
        )
        assert card.is_gated
        ok, reasons = can_move(card, Column.RUNNING)
        assert not ok
        assert any("tier_requires_approval" in r for r in reasons)

    def test_t1_in_scope_runs_without_gate(self):
        card = make_card(
            column=Column.ASSIGNED,
            scope=Scope(targets=["example.com"]),
            tools=[ToolBinding(name="nmap_scan", tier=1, args={"target": "example.com"})],
        )
        ok, reasons = can_move(card, Column.RUNNING)
        assert ok, reasons
        assert not card.is_gated

    def test_t0_runs_without_scope(self):
        card = make_card(
            column=Column.ASSIGNED,
            tools=[ToolBinding(name="whois_lookup", tier=0, args={"target": "example.com"})],
        )
        ok, reasons = can_move(card, Column.RUNNING)
        assert ok, reasons

    def test_killed_card_is_frozen(self):
        card = make_card(column=Column.RUNNING, killed=True)
        with pytest.raises(TransitionError) as exc:
            validate_transition(card, Column.REVIEW)
        assert any("card_killed" in r for r in exc.value.reasons)
        # even force cannot resurrect a killed card
        with pytest.raises(TransitionError):
            validate_transition(card, Column.BLOCKED, force=True)

    def test_force_overrides_edge_but_not_scope(self):
        card = make_card(
            column=Column.BACKLOG,
            scope=Scope(targets=["other.com"]),
            tools=[ToolBinding(name="nikto_scan", tier=2, args={"target": "example.com"})],
        )
        # force may bypass the illegal edge Backlog->Running...
        with pytest.raises(TransitionError) as exc:
            validate_transition(card, Column.RUNNING, force=True)
        # ...but the scope guard is a hard stop regardless.
        assert any("target_out_of_scope" in r for r in exc.value.reasons)

    def test_cannot_leave_running_with_pending_approval(self):
        from kanban_core.models import Approval

        card = make_card(column=Column.RUNNING)
        card.approvals.append(Approval(requested_by="agent", reason="gate"))
        ok, reasons = can_move(card, Column.REVIEW)
        assert not ok
        assert any("approval_pending" in r for r in reasons)

    def test_agent_cannot_write_done(self):
        card = make_card(column=Column.REVIEW)
        from kanban_core.state_machine import AGENT_WRITABLE

        validate_transition(card, Column.DONE)  # human path is fine
        with pytest.raises(TransitionError):
            validate_transition(card, Column.DONE, allowed=AGENT_WRITABLE)


class TestHelpers:
    def test_extract_targets_reads_common_keys(self):
        card = make_card(
            tools=[
                ToolBinding(name="a", tier=1, args={"target": "one.com"}),
                ToolBinding(name="b", tier=1, args={"hosts": ["two.com", "three.com"]}),
                ToolBinding(name="c", tier=1, args={"cidr": "192.0.2.0/24"}),
                ToolBinding(name="d", tier=1, args={"unrelated": "ignored"}),
            ]
        )
        assert set(extract_targets(card)) == {"one.com", "two.com", "three.com", "192.0.2.0/24"}

    def test_next_columns_reflects_guards(self):
        blocked_card = make_card(assignee=None, column=Column.ASSIGNED)
        assert "Running" not in next_columns(blocked_card)
        assert "Backlog" in next_columns(blocked_card)

    def test_killed_card_has_no_next_columns(self):
        card = make_card(column=Column.RUNNING, killed=True)
        assert next_columns(card) == []

    def test_max_tier(self):
        card = make_card(tools=[ToolBinding(name="a", tier=1), ToolBinding(name="b", tier=3)])
        assert card.max_tier == 3
        assert make_card().max_tier == 0
