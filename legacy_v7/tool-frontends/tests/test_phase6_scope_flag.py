"""Phase 6, item 1: the tier/flag split must not be re-conflated.

Phases 2 and 5 fixed three layers that gated scope coverage on ``tier >= 1``
instead of on the tool's declared ``requires_scope``. Those fixes were local.
This file pins the *rule* in one place - ``guardrails.scope_is_required`` /
``scope_is_mandatory`` - so a later boundary cannot quietly reintroduce the
proxy by writing ``tier >= 1`` again.

Two failure directions, and they are not symmetric:

* **Escape** - gating on the tier means a ``requires_scope`` tool below T2 runs
  unchecked. A security hole.
* **Over-correction** - gating on the *absence* of a flag means a public T0
  lookup is refused for a scope it never needed. A guardrail that fires on
  legitimate work is one operators learn to bypass, so this direction is tested
  too.
"""
from __future__ import annotations

from tool_frontends.guardrails import evaluate, scope_is_mandatory, scope_is_required
from tool_frontends.spec import ToolSpec


def _spec(**over: object) -> ToolSpec:
    base: dict[str, object] = {
        "name": "probe_stub",
        "binary": "probe",
        "category": "information-gathering",
        "tier": 1,
        "target_params": ["target"],
        "dry_run_template": "probe {target}",
        "live_template": "probe {target}",
    }
    base.update(over)
    return ToolSpec(**base)  # type: ignore[arg-type]


class TestScopeIsRequired:
    def test_the_flag_governs_at_every_tier(self):
        """A T0 tool that declares the flag is checked - the latent-gap case."""
        assert scope_is_required(0, True) is True
        assert scope_is_required(1, True) is True
        assert scope_is_required(2, True) is True

    def test_tier_is_a_fail_closed_floor_not_a_substitute(self):
        """An unresolvable spec at T1+ must not read as 'needs no scope'."""
        assert scope_is_required(1, False) is True
        assert scope_is_required(2, False) is True

    def test_t0_without_the_flag_is_not_over_corrected(self):
        assert scope_is_required(0, False) is False

    def test_mandatory_is_tier_driven_alone(self):
        assert scope_is_mandatory(2) is True
        assert scope_is_mandatory(3) is True
        assert scope_is_mandatory(1) is False
        assert scope_is_mandatory(0) is False


class TestSpecFlag:
    def test_scope_required_mirrors_the_declaration(self):
        assert _spec(requires_scope=True).scope_required is True
        assert _spec(requires_scope=False).scope_required is False


class TestEvaluateHonoursTheFlag:
    def test_an_unlocked_live_run_is_actually_reachable(self):
        """Guards the three tests below: the live-unlock path must be the *only*
        thing standing between a compliant invocation and a decision."""
        decision = evaluate(
            _spec(tier=1, requires_scope=True),
            {"target": "example.com"},
            scope={"targets": ["example.com"], "authorization_ref": "ENG-1"},
            live=True,
            live_unlocked=True,
        )
        assert decision.allowed is True, decision.reasons

    def test_t1_flag_live_without_scope_is_refused(self):
        decision = evaluate(
            _spec(tier=1, requires_scope=True),
            {"target": "example.com"},
            live=True,
            live_unlocked=True,
        )
        assert decision.allowed is False
        assert any("requires_scope" in r for r in decision.reasons)

    def test_t1_flag_dry_without_scope_is_still_visible(self):
        """The operator may always *see* what would run."""
        decision = evaluate(
            _spec(tier=1, requires_scope=True),
            {"target": "example.com"},
            live=False,
        )
        assert decision.allowed is True, decision.reasons

    def test_t1_without_the_flag_live_needs_no_scope(self):
        """The fix must not over-correct: a public lookup is not refused."""
        decision = evaluate(
            _spec(tier=1, requires_scope=False),
            {"target": "example.com"},
            live=True,
            live_unlocked=True,
        )
        assert decision.allowed is True, decision.reasons

    def test_t2_without_a_scope_is_denied_whatever_the_flag(self):
        decision = evaluate(
            _spec(tier=2, requires_scope=True, requires_approval=True),
            {"target": "example.com"},
            approved=True,
        )
        assert decision.allowed is False
        assert any("authorization scope" in r for r in decision.reasons)
