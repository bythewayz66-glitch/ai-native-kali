"""Card lifecycle state machine.

Blueprint ref: section 03.2 (column transitions as the event bus) and
section 03.5 (approval gates as card states).

The lifecycle is the system's spine:

    Backlog -> Assigned -> Running -> Review -> Done
                    ^          |        |
                    |          v        v
                    +------ Blocked <--+

Every legal edge is declared once here so the REST API, the CrewAI bridge and
the shell UI all agree on what a valid move is. Illegal moves raise
``TransitionError`` unless the caller explicitly forces them (admin override),
which is itself audited.
"""
from __future__ import annotations

from .models import Card, Column, GuardrailTier
from .scope_model import lifecycle_reasons


class TransitionError(Exception):
    """Raised when a card move is rejected by the state machine or a guard."""

    def __init__(self, message: str, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons: list[str] = reasons or [message]


#: Declared legal edges. Anything not listed here is illegal.
ALLOWED_TRANSITIONS: dict[Column, set[Column]] = {
    Column.BACKLOG: {Column.ASSIGNED, Column.BLOCKED},
    Column.ASSIGNED: {Column.RUNNING, Column.BACKLOG, Column.BLOCKED},
    Column.RUNNING: {Column.REVIEW, Column.BLOCKED},
    Column.REVIEW: {Column.DONE, Column.RUNNING, Column.BLOCKED},
    Column.DONE: set(),  # terminal
    Column.BLOCKED: {Column.BACKLOG, Column.ASSIGNED},
}

#: Columns an agent may write to. Done is a human/verifier decision.
AGENT_WRITABLE: set[Column] = {Column.ASSIGNED, Column.RUNNING, Column.REVIEW, Column.BLOCKED}


class GuardCodes:
    """Stable machine-readable guard codes (used by API + UI)."""

    SELF_TRANSITION = "self_transition"
    ILLEGAL_EDGE = "illegal_edge"
    KILLED = "card_killed"
    NO_ASSIGNEE = "no_assignee"
    SCOPE_MISSING = "scope_missing"
    SCOPE_EXPIRED = "scope_expired"
    SCOPE_TARGET = "target_out_of_scope"
    APPROVAL_PENDING = "approval_pending"
    APPROVAL_REJECTED = "approval_rejected"
    TIER_NEEDS_APPROVAL = "tier_requires_approval"
    UNRESOLVED_APPROVAL = "unresolved_approval_on_done"


# ---------------------------------------------------------------------------
# helpers shared with the service layer
# ---------------------------------------------------------------------------
def extract_targets(card: Card) -> list[str]:
    """Pull the targets a card will touch out of its tool bindings' arguments."""
    keys = ("target", "host", "hosts", "url", "domain", "cidr", "subnet", "ip", "victim")
    out: list[str] = []
    for binding in card.tools:
        for key, value in (binding.args or {}).items():
            if key.lower() not in keys:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
    return out


def _scope_guards(card: Card) -> list[str]:
    """Scope/authorization guards for T2+ cards."""
    reasons: list[str] = []
    if card.max_tier < GuardrailTier.T2.value:
        return reasons

    if card.scope is None or not (card.scope.targets or card.scope.cidrs):
        reasons.append(f"{GuardCodes.SCOPE_MISSING}: T2+ card has no authorization scope")
        return reasons

    if card.scope.is_expired():
        reasons.append(f"{GuardCodes.SCOPE_EXPIRED}: authorization expired at {card.scope.expires_at}")

    for target in extract_targets(card):
        if not card.scope.covers(target):
            reasons.append(f"{GuardCodes.SCOPE_TARGET}: {target} is outside the authorized scope")
    return reasons


def _approval_guards(card: Card) -> list[str]:
    """Approval-gate guards for gated cards."""
    reasons: list[str] = []
    pending = card.pending_approval
    if pending is not None:
        reasons.append(f"{GuardCodes.APPROVAL_PENDING}: waiting on approval {pending.id} ({pending.reason})")
        return reasons
    if card.is_gated and not card.approved:
        if any(a.status == "rejected" for a in card.approvals):
            reasons.append(f"{GuardCodes.APPROVAL_REJECTED}: approval was rejected; re-request to proceed")
        else:
            reasons.append(
                f"{GuardCodes.TIER_NEEDS_APPROVAL}: card is gated (tier T{card.max_tier}) and has no approved gate"
            )
    return reasons


def guards_for(
    card: Card,
    to_column: Column,
    *,
    parent: Card | None = None,
    children: list[Card] | None = None,
) -> list[str]:
    """Return every guard violation blocking ``card`` from entering ``to_column``.

    ``parent`` and ``children`` are optional and supplied by the service layer,
    which is the only place that can cheaply resolve a card's family. They add the
    two parent/child lifecycle rules (a child of a killed parent may not run; a
    parent may not be Done with open children) without changing the signature for
    any caller that has no family to pass - which is why they are keyword-only and
    default to ``None``.
    """
    reasons: list[str] = []

    if card.killed:
        reasons.append(f"{GuardCodes.KILLED}: card was killed by an operator")
        return reasons

    reasons.extend(lifecycle_reasons(card, to_column.value, parent=parent, children=children))

    if to_column is Column.RUNNING:
        if not card.assignee:
            reasons.append(f"{GuardCodes.NO_ASSIGNEE}: a card must be assigned before it can run")
        reasons.extend(_scope_guards(card))
        reasons.extend(_approval_guards(card))

    if to_column is Column.REVIEW:
        unresolved = card.pending_approval
        if unresolved is not None:
            reasons.append(
                f"{GuardCodes.APPROVAL_PENDING}: cannot leave Running while approval {unresolved.id} is undecided"
            )

    if to_column is Column.DONE:
        if card.pending_approval is not None:
            reasons.append(f"{GuardCodes.UNRESOLVED_APPROVAL}: decision required before Done")

    return reasons


# ---------------------------------------------------------------------------
# validation entrypoints
# ---------------------------------------------------------------------------
def validate_transition(
    card: Card,
    to_column: Column,
    *,
    force: bool = False,
    allowed: set[Column] | None = None,
    parent: Card | None = None,
    children: list[Card] | None = None,
) -> None:
    """Raise ``TransitionError`` when the move is not permitted.

    ``force=True`` bypasses *edge* legality (admin override) but never bypasses
the kill-switch: a killed card stays killed.
    """
    frm = card.column

    if card.killed:
        raise TransitionError(
            f"card {card.card_id} was killed and is frozen in {frm.value}",
            [f"{GuardCodes.KILLED}: card was killed by an operator"],
        )

    if frm == to_column:
        raise TransitionError(
            f"card is already in {to_column.value}",
            [f"{GuardCodes.SELF_TRANSITION}: card is already in {to_column.value}"],
        )

    legal = ALLOWED_TRANSITIONS.get(frm, set())
    if allowed is not None and to_column not in allowed:
        raise TransitionError(
            f"actor is not permitted to move a card from {frm.value} to {to_column.value}",
            [f"{GuardCodes.ILLEGAL_EDGE}: actor restriction on {frm.value} -> {to_column.value}"],
        )

    if to_column not in legal and not force:
        raise TransitionError(
            f"illegal transition {frm.value} -> {to_column.value}",
            [f"{GuardCodes.ILLEGAL_EDGE}: {frm.value} -> {to_column.value} is not a declared edge"],
        )

    violations = guards_for(card, to_column, parent=parent, children=children)
    if violations and not (force and to_column is not Column.RUNNING):
        # `force` may override edge legality, but scope/approval guards on
        # Running are hard stops: they are the safety contract, not plumbing.
        raise TransitionError(
            f"guards blocked {frm.value} -> {to_column.value}",
            violations,
        )


def can_move(
    card: Card,
    to_column: Column,
    *,
    force: bool = False,
    parent: Card | None = None,
    children: list[Card] | None = None,
) -> tuple[bool, list[str]]:
    """Non-raising variant used by the API to render 'why can't I move this?'."""
    try:
        validate_transition(card, to_column, force=force, parent=parent, children=children)
    except TransitionError as exc:
        return False, exc.reasons
    return True, []


def next_columns(
    card: Card,
    *,
    parent: Card | None = None,
    children: list[Card] | None = None,
) -> list[str]:
    """Every column the card could legally move to right now (for the UI)."""
    out: list[str] = []
    for col in ALLOWED_TRANSITIONS.get(card.column, set()):
        ok, _ = can_move(card, col, parent=parent, children=children)
        if ok:
            out.append(col.value)
    return sorted(out)
