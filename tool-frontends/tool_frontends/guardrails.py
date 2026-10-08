"""Guardrail engine: tier enforcement + scope validation.

Blueprint ref: section 08 - the five-stage authorization chain. This module is
stages 1-3: *is this tool allowed at this tier*, *is the target inside the
authorized scope*, and *has the human opened the gate*.

The engine is pure: given a spec, its arguments and the caller's context it
returns a decision. It never executes anything, so it is cheap to call from the
UI to explain *why* a run would be refused.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from kanban_core.models import Scope

from .effects import MUTATING_EFFECTS, is_mutating
from .spec import ToolSpec
from .targets import classify as classify_target
from .targets import escapes as target_escapes


@dataclass
class GuardrailDecision:
    """The verdict for one proposed tool invocation."""

    allowed: bool
    status: str  # allowed | needs_approval | denied
    tier: int
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "tier": self.tier,
            "reasons": self.reasons,
            "checks": self.checks,
        }


# ---------------------------------------------------------------------------
# the tier/flag split, stated once
# ---------------------------------------------------------------------------
# Two questions get asked about a scope, and conflating them is exactly the
# defect this pair exists to prevent:
#
#   * *Is a scope mandatory?*   Yes from T2 up - intrusive work is never
#     authorised by an absent scope. **Tier-driven**, and the only thing tier
#     is allowed to decide.
#   * *Must the target be inside the scope?*  Yes whenever the tool declares
#     ``requires_scope``. **Flag-driven.**
#
# So a caller must OR the flag with a fail-closed tier floor, never substitute
# one signal for the other: tier may widen the check, never narrow it.


def scope_is_mandatory(tier: int) -> bool:
    """T2+: work this intrusive is refused outright with no scope attached."""
    return tier >= 2


def scope_is_required(tier: int, requires_scope: bool) -> bool:
    """Should this invocation be checked against an attached scope?

    The **flag** decides. ``tier`` contributes only as a fail-closed floor, so
    a T1+ tool whose spec could not be resolved does not silently read as
    "needs no scope". Being OR'd, tier can only ever make the check stricter -
    it is never a proxy for the declaration.
    """
    return bool(requires_scope) or tier >= 1


def evaluate(
    spec: ToolSpec,
    args: Optional[dict[str, Any]] = None,
    *,
    scope: Optional[Scope | dict[str, Any]] = None,
    approved: bool = False,
    live: bool = False,
    live_unlocked: bool = False,
    caller: str = "agent",
) -> GuardrailDecision:
    """Decide whether this invocation may proceed.

    ``live`` is what the caller *wants*; ``live_unlocked`` is whether the operator
    has actually enabled live execution for this tool. Requesting live without
    the unlock is a denial, never a silent downgrade.
    """
    args = args or {}
    if isinstance(scope, dict):
        scope = Scope.model_validate(scope)

    reasons: list[str] = []
    checks: dict[str, Any] = {
        "tier": spec.tier,
        "caller": caller,
        "requires_scope": spec.requires_scope,
        "requires_approval": spec.requires_approval,
        "live_requested": live,
        "live_unlocked": live_unlocked,
    }
    if isinstance(scope, Scope):
        checks["scope_attached"] = True

    # -- stage 1: tier ------------------------------------------------------
    if not 0 <= spec.tier <= 3:
        return GuardrailDecision(False, "denied", spec.tier, [f"unknown guardrail tier {spec.tier}"], checks)

    target = spec.target_value(args)
    checks["target"] = target
    checks["scope"] = scope.summary() if scope else None

    if spec.tier >= 1 and not target:
        reasons.append("no target supplied: T1+ tools must declare what they act on")

    # -- stage 2 + 3: scope + authorization ---------------------------------
    #
    # Two distinct questions live here, and conflating them was a real bug:
    #
    #   1. *Is a scope mandatory?*  Yes for T2+ (intrusive work). Tier-driven.
    #   2. *Must the target be inside the scope?*  Yes whenever a scope is
    #      attached and the tool declares ``requires_scope``.
    #
    # The coverage check previously ran for T2+ only, so ``requires_scope=True``
    # on a T1 spec was decorative: a scoped T1 tool ran against any target at
    # all. The flag is now load-bearing at every tier - a scope that is attached
    # is a scope that is enforced.
    has_scope = scope is not None and bool(scope.targets or scope.cidrs)
    checks["has_scope"] = has_scope

    # Phase 16 - the fail-closed floor, actually applied.
    #
    # ``scope_is_required`` was documented as "the fail-closed floor the callers
    # add on top" of the flag, but no caller added it: every coverage check below
    # read ``spec.requires_scope`` directly. So a **T1** tool that under-declared
    # the flag had its scope check switched off entirely, and a live run against
    # ``evil.net`` with a scope covering only ``example.com`` came back
    # ``allowed`` with no reasons - reproduced, and pinned in
    # ``tests/test_phase16_scope_floor.py``.
    #
    # The floor is OR'd with the flag (never substituted for it), so it can only
    # ever make the check stricter, and T0 tools that legitimately touch nothing
    # stay un-required - ``scope_is_required(0, False)`` is ``False``.
    #
    # The floor is applied to the **coverage** checks (stage 2b and the declared
    # comparison below), which is where the hole actually was: a T1 tool that
    # under-declared the flag had its target check switched off, so a live run
    # against an out-of-scope host came back ``allowed``. It is deliberately
    # *not* applied to the "no scope attached" branch, which stays keyed on the
    # declaration (``spec.scope_required``): that branch is a policy statement
    # ("this tool needs authorization"), not a target comparison, and widening it
    # would refuse every un-flagged T0/T1 lookup that has no scope attached at
    # all - the over-correction direction the Phase 6 tests guard.
    scope_checked = scope_is_required(spec.tier, spec.requires_scope)
    checks["scope_checked"] = scope_checked

    # A tool that *declares* a scope requirement has told us it touches a target.
    # From T2 up a scope is mandatory and the branch below already refuses. Below
    # T2 a *dry run* with no scope is allowed (the operator still gets to see the
    # command and the reason), but a **live** run with no scope is refused,
    # because there is then nothing to compare the target against and the tool
    # would send real traffic to whatever host the caller named.
    if spec.scope_required and not has_scope and not scope_is_mandatory(spec.tier) and live:
        reasons.append(
            f"{spec.name} declares requires_scope but no authorization scope is "
            "attached; live execution needs a scope to check the target against"
        )

    if scope_checked and has_scope:
        if scope.is_expired():
            reasons.append("authorization scope has expired")
        # A loosely-scoped probe is tolerable; an *intrusive* action must point
        # at a ticket or contract on record.
        if spec.tier >= 2 and not scope.authorization_ref:
            reasons.append("scope has no authorization_ref (no ticket/contract on record)")
        checks["scope_covers_target"] = bool(target and scope.covers(target))
    elif spec.tier >= 2 and not has_scope:
        reasons.append(f"T{spec.tier} requires an authorization scope; none was attached")

    # -- stage 2b: no argument may name an out-of-scope address --------------
    #
    # Declared ``target_params`` are a promise the spec author makes by hand,
    # and a spec that promises incompletely is a spec that leaks.
    # ``dns_zone_transfer`` declares ``nameserver``; a spec that forgot to would
    # have sent an AXFR to whatever host the caller named, because only the
    # declared parameters were ever compared against the scope.
    #
    # So the target is now enforced **by value as well as by declaration**: any
    # argument that resolves to a network identifier must be inside the scope.
    # A spec author who forgets a declaration no longer opens a hole - at worst
    # they earn a clearly-worded refusal. Local values (paths, wordlists, enum
    # choices) are not addresses and are untouched, so this stays quiet enough
    # to keep switched on.
    value_escapes: list[str] = []
    if has_scope and scope_checked:
        value_escapes = target_escapes(
            args,
            scope=scope,
            declared_params=spec.target_params,
            skip_keys=spec.scope_skip_params,
        )
        if value_escapes:
            reasons.extend(value_escapes)
            checks["arg_scope_escapes"] = value_escapes
        checks["args_checked_by_value"] = True

    # The two layers are complements, not duplicates. Stage 2b covers every
    # declared *and* undeclared argument that classifies as an address. What it
    # cannot see is a **declared** value the classifier is deliberately silent
    # about - a single-label hostname such as ``fileserver``, which is
    # indistinguishable from ``admin`` or ``top100``. Those are checked here by
    # declaration, and only those, so one escape still yields exactly one
    # reason. Between them the two axes are covered: every address is checked,
    # and every declared parameter is checked.
    if scope_checked and has_scope:
        for key in spec.target_params:
            raw = args.get(key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            value = raw.strip()
            if classify_target(value) is not None:
                continue  # already enforced by value in stage 2b
            if scope.covers(value):
                continue
            reasons.append(
                f"target parameter '{key}' = '{value}' is outside the authorized "
                f"scope ({scope.summary()})"
            )

    if spec.requires_approval or spec.tier >= 2:
        checks["approved"] = approved
        if not approved:
            reasons.append(f"T{spec.tier} requires a human approval gate; none is open")

    # -- stage 4: sandbox ---------------------------------------------------
    if spec.tier >= 3 and not spec.requires_sandbox:
        reasons.append("T3 tool is not marked requires_sandbox (blueprint 08: T3 must be sandboxed)")

    # -- stage 4b: the local-footprint floor --------------------------------
    # The T3 rule above keys on *tier*, and tier describes intrusiveness toward
    # the **target**, not the local blast radius. So a T0/T1 tool that rewrites
    # the local filesystem or spawns a process was never required to be
    # sandboxed: ``log_rotate`` is T0, sends nothing, and its live template
    # (``logrotate /etc/logrotate.conf``) still rewrites files. The footprint a
    # spec states (or, until it states one, the footprint inferred from its
    # binary/tier/templates) now forces the sandbox from T1 up. Tier can only
    # widen this check, never narrow it - the same fail-closed direction as the
    # scope floor in stage 2.
    declared_effects = spec.enforced_effects()
    checks["effects"] = declared_effects
    checks["effects_declared"] = spec.effects_declared
    if spec.tier >= 1 and is_mutating(declared_effects) and not spec.requires_sandbox:
        reasons.append(
            f"T{spec.tier} tool '{spec.name}' declares mutating effects "
            f"{sorted(set(declared_effects) & MUTATING_EFFECTS)} but is not sandboxed"
        )

    # -- stage 5: live unlock ----------------------------------------------
    if live and not live_unlocked:
        reasons.append(
            f"live execution for '{spec.name}' is not unlocked; run in dry-run mode or enable it explicitly"
        )

    if reasons:
        status = "needs_approval" if all("approval" in r for r in reasons) else "denied"
        return GuardrailDecision(False, status, spec.tier, reasons, checks)

    return GuardrailDecision(True, "allowed", spec.tier, [], checks)
