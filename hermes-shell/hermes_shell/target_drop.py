"""Dragging a *target* onto a board - the class of drop that creates work.

Phase 7, item 2. Phase 4 added dropping a **file** onto a card, which produces an
artifact. This is the other drop: a target (an IP, FQDN, CIDR, URL, MAC or
BSSID) dropped on a column or a card, which must produce a **scoped card**.

Why this is a security-relevant path and not a UI nicety
--------------------------------------------------------
A dropped target is an *authorisation request*. The card it creates will carry a
scope, and crews will then run tools against whatever that scope covers. So the
classification of the payload is not cosmetic - it decides what the system is
about to be allowed to touch. Three consequences shape this module:

1. **One classifier, not two.** Target detection reuses
   ``tool_frontends.targets.classify`` and ``normalize``. A second, "lighter"
   detector written for the drag handler would drift from the one the guardrail
   engine enforces on, and the drift would show up as a card whose scope covers
   something the engine reads differently. That divergence is exactly the class
   of defect this repository has now fixed three times at three layers, so the
   import is deliberate even though it couples the shell to tool-frontends'
   *pure* module (never its server).

2. **Only a classified target may create a card.** ``plan_drop`` returns
   ``accepted: False`` with a reason for anything else. A payload that is not an
   address is *never* wrapped into a scope "just in case": an empty or guessing
   scope on a card is worse than no card, because a crew will treat it as
   authorisation.

3. **The scope is derived from the classified value, not the raw string.** For a
   URL the scope is the host, not the URL - a scope listing
   ``https://shop.example.net/login`` does not cover ``shop.example.net`` under
   the engine's matching rules, so a card scoped from the raw string would be
   refused by the very engine that is supposed to protect it. For an IP it is the
   IP, for a CIDR it goes in ``cidrs``, for a hostname the hostname.

The audit trail
---------------
Every accepted drop is routed through the existing guardrail engine and the
hash-chained tool audit log via :func:`audit_drop`. The board's card-audit chain
records the card; this records the *decision to create it*, with the classifier's
verdict, so "why does this card exist and who authorised it" is answerable from
the tool audit log alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

#: The address shapes a drop may carry.
#:
#: This is the *accepted* subset of the kinds the shared classifier can return.
#: Two adjustments, both deliberate:
#:
#: * ``email`` and ``phone`` are real target kinds for tool *parameters* and are
#:   excluded here: a card scoped to a phone number has no meaning to any tool on
#:   this image, so accepting one would mint a card whose scope nothing can use.
#: * ``bssid`` and ``fqdn`` are accepted for symmetry, but note that the shared
#:   classifier actually reports a BSSID as ``mac`` and an FQDN as ``hostname`` -
#:   it has no distinct kind for either. They are listed so that a future
#:   classifier split does not silently start refusing drops.
#:
#: Written as a plain tuple, not derived from ``targets.KINDS``, because this is
#: a *policy* decision about what may scope a card, not a restatement of what the
#: classifier can see. Tying it to the classifier's tuple would mean a new kind
#: upstream silently widened the drop surface.
DROP_TARGET_KINDS = ("ipv4", "ipv6", "cidr", "hostname", "fqdn", "url", "mac", "bssid", "onion")


@dataclass
class DropPlan:
    """What a drop would create, decided without touching any service."""

    accepted: bool
    kind: Optional[str] = None
    #: The value as classified (host for a URL, address for a CIDR).
    target: Optional[str] = None
    #: The scope the card should carry.
    scope_targets: list[str] = field(default_factory=list)
    scope_cidrs: list[str] = field(default_factory=list)
    #: The column the card would land in.
    column: str = "Backlog"
    title: str = ""
    reason: str = ""
    board_id: Optional[str] = None
    #: The card this drop attaches to, when dropping onto a card rather than a
    #: column.
    card_id: Optional[str] = None
    verdict: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "kind": self.kind,
            "target": self.target,
            "scope_targets": list(self.scope_targets),
            "scope_cidrs": list(self.scope_cidrs),
            "column": self.column,
            "title": self.title,
            "reason": self.reason,
            "board_id": self.board_id,
            "card_id": self.card_id,
            "verdict": dict(self.verdict),
        }


def _classify(value: Any) -> tuple[Optional[str], Optional[str]]:
    """(kind, normalized) using the shared classifier. Never raises.

    Imported inside the function so that a shell missing tool-frontends (a
    partially-installed image) degrades to "cannot classify" - a refusal - rather
    than failing to import at all. A refusal is the safe direction.
    """
    try:
        from tool_frontends.targets import classify, normalize
    except Exception:  # noqa: BLE001 - absent classifier means "refuse", not "crash"
        return None, None
    try:
        kind = classify(value)
        if kind is None:
            return None, None
        return str(kind), str(normalize(str(value), kind))
    except Exception:  # noqa: BLE001
        return None, None


def scope_for(kind: str, normalized: str) -> tuple[list[str], list[str]]:
    """The (targets, cidrs) a card should carry for a classified drop.

    See the module docstring: the scope is built from the *normalized* value so
    it matches what the guardrail engine will compare against. A CIDR goes in
    ``cidrs`` (the field the engine's network matching reads); everything else
    goes in ``targets``.
    """
    if kind == "cidr":
        return [], [normalized]
    return [normalized], []


def title_for(kind: str, normalized: str) -> str:
    """A card title that reads as work, not as a bare address."""
    verb = {
        "cidr": "Enumerate network",
        "mac": "Identify device",
        "bssid": "Assess wireless access point",
        "url": "Assess web application",
        "onion": "Assess hidden service",
        "ipv6": "Enumerate host",
    }.get(kind, "Recon")
    return f"{verb} {normalized}"


def plan_drop(
    payload: Any,
    *,
    board_id: Optional[str] = None,
    column: str = "Backlog",
    card_id: Optional[str] = None,
    authorization_ref: Optional[str] = None,
    authorized_by: Optional[str] = None,
) -> DropPlan:
    """Decide what a drop would create. Pure - no I/O, no side effects.

    Accepts either a bare string (``"10.0.0.5"``) or a payload object carrying a
    ``target``/``value``/``text`` field plus drop context, which is what a real
    drag event delivers.
    """
    raw: Any = payload
    if isinstance(payload, dict):
        # ``text`` is what a browser DataTransfer carries; ``target`` is what the
        # file manager emits and what the board's own API calls it.
        raw = payload.get("target") or payload.get("value") or payload.get("text")
        board_id = payload.get("board_id") or board_id
        column = payload.get("column") or column
        card_id = payload.get("card_id") or card_id
        authorization_ref = payload.get("authorization_ref") or authorization_ref
        authorized_by = payload.get("authorized_by") or authorized_by

    if raw is None or not str(raw).strip():
        return DropPlan(accepted=False, reason="drop carried no payload", board_id=board_id, card_id=card_id)

    kind, normalized = _classify(str(raw).strip())
    if kind is None or not normalized:
        return DropPlan(
            accepted=False,
            reason=(
                f"'{str(raw).strip()[:60]}' is not a target: only an address, CIDR, "
                "hostname or URL can create a scoped card"
            ),
            board_id=board_id,
            card_id=card_id,
        )
    if kind not in DROP_TARGET_KINDS:
        return DropPlan(
            accepted=False,
            kind=kind,
            reason=(
                f"'{normalized}' classified as {kind}, which cannot scope a card "
                "(no tool on this image takes it as a target)"
            ),
            board_id=board_id,
            card_id=card_id,
        )

    targets, cidrs = scope_for(kind, normalized)
    verdict: dict[str, Any] = {
        "classifier": "tool_frontends.targets.classify",
        "kind": kind,
        "normalized": normalized,
        "raw": str(raw).strip()[:200],
    }
    # A drop that lands on a card is *scoped* to it; a drop that lands on a
    # column creates a new card. Recording which, in the verdict, keeps the two
    # auditable apart after the fact.
    return DropPlan(
        accepted=True,
        kind=kind,
        target=normalized,
        scope_targets=targets,
        scope_cidrs=cidrs,
        column=column or "Backlog",
        title=title_for(kind, normalized),
        board_id=board_id,
        card_id=card_id,
        verdict={
            **verdict,
            "drop_mode": "attach-to-card" if card_id else "new-card",
            "authorization_ref": authorization_ref,
            "authorized_by": authorized_by,
        },
    )


def scope_payload(plan: DropPlan, *, authorization_ref: Optional[str] = None, authorized_by: Optional[str] = None) -> dict[str, Any]:
    """The ``scope`` object for ``POST /api/cards``.

    ``authorization_ref`` and ``authorized_by`` are carried through when the drop
    supplied them. They are *not* invented: a card whose scope claims an
    authorisation that nobody actually issued is a forged authorisation record,
    which is worse than an unattributed one.
    """
    scope: dict[str, Any] = {"targets": list(plan.scope_targets), "cidrs": list(plan.scope_cidrs)}
    if authorization_ref:
        scope["authorization_ref"] = authorization_ref
    if authorized_by:
        scope["authorized_by"] = authorized_by
    return scope


def card_payload(plan: DropPlan, *, description: str = "", crew: Optional[str] = None) -> dict[str, Any]:
    """The ``POST /api/cards`` body for an accepted drop."""
    if not plan.accepted:
        raise ValueError(f"cannot build a card for a refused drop: {plan.reason}")
    body: dict[str, Any] = {
        "title": plan.title,
        "board_id": plan.board_id,
        "column": plan.column,
        "scope": scope_payload(plan),
        "description": description
        or f"Created by dropping target {plan.target} ({plan.kind}) onto the board.",
    }
    if crew:
        body["crew"] = crew
    return body


def audit_drop(audit: Any, plan: DropPlan, *, actor: str = "hermes-shell", card_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Record the drop decision in the hash-chained tool audit log.

    Routed through the *existing* ``ToolAuditLog`` rather than a new log, so the
    drop sits in the same tamper-evident chain as every tool call - an operator
    verifying the chain sees the authorisation decision beside the calls it
    authorised.

    ``status`` distinguishes an accepted drop from a refused one. A refusal is
    recorded too: "someone dragged a non-target and we declined" is exactly the
    kind of event an audit trail exists for, and recording only successes makes
    the trail unable to explain the refusals a user will ask about.
    """
    if audit is None:
        return None
    try:
        from datetime import datetime, timezone

        return audit.append(
            ts=datetime.now(timezone.utc).isoformat(),
            tool="target_drop",
            tier=0,
            status="ok" if plan.accepted else "denied",
            dry_run=True,
            caller=actor,
            # The classified target is what the audit row should key on. For a
            # refused drop there is no target, and None is the honest value -
            # writing the raw string would make a non-target look like one.
            target=plan.target,
            decision=plan.verdict.get("drop_mode"),
            args={
                **plan.verdict,
                "accepted": plan.accepted,
                "reason": plan.reason,
                "scope": scope_payload(plan) if plan.accepted else {},
            },
            reasons=[plan.reason] if plan.reason else None,
            card_id=card_id or plan.card_id,
        )
    except Exception:  # noqa: BLE001 - an audit failure must not lose the drop
        return None
