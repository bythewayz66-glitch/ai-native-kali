"""Sub-card scope model: how a child card inherits and narrows its parent's scope.

Phase 8, item 1. The design is `docs/SUB_CARD_SCOPE_MODEL.md`; this module is the
enforcement, and the two are meant to be read together.

The invariant
-------------
A child's scope must be a **subset** of its parent's - never wider. Everything
here exists to make that one sentence checkable.

The subtle half of "subset"
---------------------------
An **absent** scope is not the narrowest scope; it is the widest. A card with no
scope attached is a card whose targets are unbounded, so ``None`` behaves like
the *universal* set here, not the empty one. That single decision is what makes
"parent has a scope, child has none" a **widening** and therefore a refusal - the
case a naive ``set(child.targets) <= set(parent.targets)`` check waves straight
through, because the empty set is trivially a subset of everything.

Why this is a module and not a method
-------------------------------------
The same authorization confusion has been found and fixed at four separate layers
(guardrail engine, argument classifier, plan validator, and the tier/flag split
itself). Each time, the rule was re-derived locally by whoever needed it, and each
re-derivation was a fresh chance to get it backwards. So the rule lives here, in
one place, with the reasoning attached, and every call site routes through it.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Optional

from .models import Card, Scope


class ScopeNarrowingError(Exception):
    """Raised when a child card's scope would widen its parent's.

    Carries machine-readable ``reasons`` in the same vocabulary the rest of the
    system uses, so the board UI can render a refusal without a second mapping.
    """

    def __init__(self, message: str, reasons: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.reasons: list[str] = reasons or [message]


# ---------------------------------------------------------------------------
# guard codes (mirrors state_machine.GuardCodes; kept here so this module has no
# import cycle with the state machine, which imports *this* one)
# ---------------------------------------------------------------------------
class SubCardCodes:
    """Stable machine-readable codes for parent/child refusals."""

    SCOPE_WIDENED = "subcard_scope_widened"
    SCOPE_DROPPED = "subcard_scope_dropped"
    TIER_ESCALATED = "subcard_tier_escalated"
    PARENT_KILLED = "parent_killed"
    OPEN_CHILDREN = "open_children"
    PARENT_MISSING = "parent_missing"
    SELF_PARENT = "subcard_self_parent"


# ---------------------------------------------------------------------------
# scope predicates
# ---------------------------------------------------------------------------
def scope_is_empty(scope: Optional[Scope]) -> bool:
    """True when a scope carries no targets and no networks.

    An empty scope and an absent scope are the same thing to this model: both
    mean "unbounded", which is why they are treated identically below.
    """
    return scope is None or not (scope.targets or scope.cidrs)


def _cidr_covered(cidr: str, parent: Scope) -> bool:
    """Is the whole network *cidr* inside *parent*?

    Uses real subnet containment rather than string prefixes. ``10.0.0.0/8`` and
    ``10.0.0.0/24`` share a prefix, but the first is the *wider* network - a
    prefix comparison gets that backwards for exactly the case that matters.
    """
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    for parent_cidr in parent.cidrs:
        try:
            parent_net = ipaddress.ip_network(parent_cidr, strict=False)
        except ValueError:
            continue
        if net.version == parent_net.version and net.subnet_of(parent_net):
            return True
    # A single-host network is also covered when the parent names that host as a
    # target - ``10.0.0.5/32`` is inside a parent that lists ``10.0.0.5``.
    if net.num_addresses == 1 and parent.covers(str(net.network_address)):
        return True
    return False


def is_subset(child: Optional[Scope], parent: Optional[Scope]) -> bool:
    """Is *child* inside *parent*? Absent means unbounded, not empty.

    See the module docstring: this is the one place the "absent = widest"
    decision is made, and every caller inherits it.
    """
    if scope_is_empty(parent):
        # The parent is unbounded, so anything is inside it - including nothing.
        return True
    if scope_is_empty(child):
        # The child is unbounded while the parent is not: a widening.
        return False
    for target in child.targets:
        if not parent.covers(target):
            return False
    for cidr in child.cidrs:
        if not _cidr_covered(cidr, parent):
            return False
    return True


def widening_reasons(child: Optional[Scope], parent: Optional[Scope]) -> list[str]:
    """Every specific way *child* escapes *parent*, one reason per cause.

    Specific reasons rather than a single "not a subset": an operator who is told
    *which* target escaped can fix it, and one who is told "invalid scope" cannot.
    """
    if scope_is_empty(parent):
        return []
    if scope_is_empty(child):
        return [
            f"{SubCardCodes.SCOPE_DROPPED}: the parent is scoped "
            f"({parent.summary()}) but the child carries no scope, which is "
            "unbounded rather than narrower"
        ]
    reasons: list[str] = []
    for target in child.targets:
        if not parent.covers(target):
            reasons.append(
                f"{SubCardCodes.SCOPE_WIDENED}: child target '{target}' is outside "
                f"the parent scope ({parent.summary()})"
            )
    for cidr in child.cidrs:
        if not _cidr_covered(cidr, parent):
            reasons.append(
                f"{SubCardCodes.SCOPE_WIDENED}: child network '{cidr}' is not inside "
                f"the parent scope ({parent.summary()})"
            )
    return reasons


# ---------------------------------------------------------------------------
# tier ceiling
# ---------------------------------------------------------------------------
def parent_tier_ceiling(parent: Card) -> int:
    """The highest tier a child of *parent* may bind.

    ``parent.max_tier`` by default - the fail-closed choice, since a parent that
    bound no tools has a ceiling of 0. A *container* card (one that exists to
    spawn work rather than to run tools) may declare an explicit ceiling in
    ``meta["tier_ceiling"]``, which is a field an auditor can read rather than a
    capability that arrives by omission.
    """
    declared = (parent.meta or {}).get("tier_ceiling")
    if declared is None:
        return parent.max_tier
    try:
        return max(0, min(3, int(declared)))
    except (TypeError, ValueError):
        return parent.max_tier


def tier_reasons(parent: Card, child_tier: int) -> list[str]:
    """Reasons a child at *child_tier* exceeds its parent's authorization."""
    ceiling = parent_tier_ceiling(parent)
    if child_tier > ceiling:
        return [
            f"{SubCardCodes.TIER_ESCALATED}: child binds T{child_tier} work but the "
            f"parent ceiling is T{ceiling}"
        ]
    return []


# ---------------------------------------------------------------------------
# the one entry point
# ---------------------------------------------------------------------------
def validate_child(
    parent: Card,
    child_scope: Optional[Scope],
    *,
    child_tier: int = 0,
) -> list[str]:
    """Every reason this child may not be spawned from *parent*. Empty = allowed.

    Returns problems rather than raising so a caller can report all of them at
    once - the same choice ``packaging.profiles.validate`` makes, and for the same
    reason: a caller that stops at the first problem makes the operator fix them
    one round-trip at a time.
    """
    reasons: list[str] = []
    if parent.killed:
        reasons.append(
            f"{SubCardCodes.PARENT_KILLED}: parent {parent.card_id} was killed; "
            "the kill switch is inherited"
        )
    reasons.extend(widening_reasons(child_scope, parent.scope))
    reasons.extend(tier_reasons(parent, child_tier))
    return reasons


def inherit_scope(parent: Card) -> Optional[Scope]:
    """The scope a child gets when it does not specify one: the parent's, copied.

    A copy, not the same object: a child that later narrows its own scope must not
    mutate the parent's, and sharing the instance would make that a live bug
    rather than a design decision.
    """
    if parent.scope is None:
        return None
    return parent.scope.model_copy(deep=True)


def resolve_child_scope(parent: Card, requested: Optional[Scope | dict[str, Any]]) -> Optional[Scope]:
    """Turn a requested child scope into the scope the child will actually carry.

    ``None`` means "inherit" (the safe default). An explicitly empty scope is
    *not* treated as "inherit" - it is a widening and is left for
    :func:`validate_child` to refuse, because silently upgrading a caller's
    explicit empty scope to the parent's would hide the mistake.
    """
    if requested is None:
        return inherit_scope(parent)
    if isinstance(requested, dict):
        return Scope.model_validate(requested)
    return requested


def narrowing_verdict(parent: Card, child_scope: Optional[Scope]) -> str:
    """``inherited`` when the child carries the parent's scope unchanged, else ``narrowed``.

    Recorded in the audit entry so the chain states the *relationship*, not just
    the two scopes - see the design doc, section 8.
    """
    if scope_is_empty(parent.scope) and scope_is_empty(child_scope):
        return "unscoped"
    if parent.scope is not None and child_scope is not None:
        if (
            sorted(child_scope.targets) == sorted(parent.scope.targets)
            and sorted(child_scope.cidrs) == sorted(parent.scope.cidrs)
        ):
            return "inherited"
    return "narrowed"


def audit_payload(parent: Card, child: Card, *, verdict: str) -> dict[str, Any]:
    """The payload written to the hash chain for a child creation."""
    return {
        "parent_id": parent.card_id,
        "child_id": child.card_id,
        "parent_scope": parent.scope.summary() if parent.scope else None,
        "child_scope": child.scope.summary() if child.scope else None,
        "narrowing": verdict,
        "parent_tier_ceiling": parent_tier_ceiling(parent),
        "child_max_tier": child.max_tier,
    }


def refusal_payload(parent: Card, reasons: list[str], *, requested: Optional[Scope]) -> dict[str, Any]:
    """The payload written when a spawn is refused.

    A refusal that leaves no trace is one an operator will re-attempt, which is
    the same argument the tool layer makes for recording refused calls.
    """
    return {
        "parent_id": parent.card_id,
        "parent_scope": parent.scope.summary() if parent.scope else None,
        "requested_scope": requested.summary() if requested else None,
        "reasons": list(reasons),
    }


# ---------------------------------------------------------------------------
# lifecycle helpers
# ---------------------------------------------------------------------------
#: A child in one of these columns is still "open" - it has not finished and has
#: not been parked. A parent may not be Done while it has one.
OPEN_COLUMNS = ("Backlog", "Assigned", "Running", "Review")


def open_children(children: list[Card]) -> list[Card]:
    """The children that are neither Done nor Blocked."""
    return [c for c in children if c.column.value in OPEN_COLUMNS]


def lifecycle_reasons(
    card: Card,
    to_column: str,
    *,
    parent: Optional[Card] = None,
    children: Optional[list[Card]] = None,
) -> list[str]:
    """Parent/child lifecycle guards for a proposed move.

    Two rules, both about a card not being able to claim a state its family
    contradicts:

    * a child of a killed parent may not run (the kill switch is inherited);
    * a parent may not be Done while it has open children (a completed engagement
      with a live remediation task under it is a lie the board would tell).
    """
    reasons: list[str] = []
    if parent is not None and to_column == "Running" and parent.killed:
        reasons.append(
            f"{SubCardCodes.PARENT_KILLED}: parent {parent.card_id} was killed; "
            "this child is frozen with it"
        )
    if to_column == "Done" and children:
        still_open = open_children(children)
        if still_open:
            names = ", ".join(c.card_id for c in still_open[:5])
            more = "" if len(still_open) <= 5 else f" (+{len(still_open) - 5} more)"
            reasons.append(
                f"{SubCardCodes.OPEN_CHILDREN}: {len(still_open)} child card(s) are still "
                f"open: {names}{more}"
            )
    return reasons
