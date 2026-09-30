"""Remediation crew: turn a finding into a scoped remediation sub-card.

Phase 8, item 2. The design is `docs/SUB_CARD_SCOPE_MODEL.md`; the enforcement is
`kanban-core/kanban_core/scope_model.py`. This module is the *bridge* between the
two: it takes the findings a crew run produced and proposes one child card per
finding, with a scope narrowed from the finding's own target.

Two decisions worth stating, because both are the opposite of the obvious one:

**The bridge does not pre-check the narrowing.** It proposes a scope and lets the
board refuse it. The board is the authority on what is in scope; a client-side
check would be a second opinion that could disagree with it, and the whole point
of the shared scope helper is that there is exactly one opinion. A refusal comes
back as a 409 and is recorded here as a skip, not swallowed.

**A finding with no target inherits the parent's scope rather than getting none.**
An absent scope is the *widest* scope, so "no target" must not become "no scope" -
that would be a widening, and the board would (correctly) refuse it. Inheriting
keeps the child inside the parent by construction.

The spawn path is deterministic: it needs no model. That is deliberate - the
remediation crew's *run* falls back to the deterministic adapter when no model is
reachable, and the spawn must not be a second thing that breaks in the same
conditions.
"""
from __future__ import annotations

from typing import Any, Optional

from .client import KanbanError, KanbanClient

#: A finding may name its target under any of these keys. Ordered by specificity:
#: an explicit list beats a single target, which beats a bare host.
_TARGET_KEYS = ("targets", "target", "host", "asset")


def finding_targets(finding: dict[str, Any]) -> list[str]:
    """Every target a finding names, in declaration order, de-duplicated."""
    out: list[str] = []
    for key in _TARGET_KEYS:
        value = finding.get(key)
        if not value:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
    return out


def derive_scope(
    finding: dict[str, Any],
    parent_scope: Optional[dict[str, Any]],
    *,
    card_target: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """The scope a remediation sub-card should carry for *finding*.

    Narrowed to the finding's own target when it names one; otherwise the parent's
    scope is inherited (never dropped - see the module docstring). The result is a
    *proposal*: the board decides whether it is genuinely inside the parent.
    """
    targets = finding_targets(finding)
    if not targets and card_target:
        targets = [card_target]
    if targets:
        return {"targets": targets}
    # No target anywhere: inherit the parent's scope rather than sending none.
    if parent_scope:
        return dict(parent_scope)
    return None


def remediation_title(finding: dict[str, Any], *, index: int = 0) -> str:
    """A short, human-readable title for the spawned card."""
    statement = finding.get("statement") or finding.get("summary") or ""
    statement = " ".join(str(statement).split())
    if not statement:
        return f"Remediate finding {index + 1}"
    if len(statement) > 64:
        statement = statement[:61].rstrip() + "..."
    return f"Remediate: {statement}"


def spawn_remediation(
    client: KanbanClient,
    card: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    actor: str = "bridge",
    max_children: int = 5,
    stats: Any = None,
) -> list[dict[str, Any]]:
    """Open one scoped remediation sub-card per finding.

    Returns one record per finding: ``{"status": "created"|"refused"|"skipped",
    "child_id": ..., "reasons": [...]}``. Never raises for a refusal - a finding
    the board will not let us remediate is a fact to report, not a crash.
    """
    results: list[dict[str, Any]] = []
    parent_id = card.get("id") or card.get("card_id")
    if not parent_id:
        return results
    parent_scope = card.get("scope")
    card_target = card.get("target") or (card.get("meta") or {}).get("target")

    for index, finding in enumerate(findings[:max_children]):
        if not isinstance(finding, dict):
            continue
        statement = finding.get("statement") or finding.get("summary")
        if not statement:
            results.append({"status": "skipped", "reasons": ["finding has no statement"]})
            continue
        scope = derive_scope(finding, parent_scope, card_target=card_target)
        payload: dict[str, Any] = {
            "title": remediation_title(finding, index=index),
            "description": str(finding.get("detail") or statement),
            "priority": "high" if float(finding.get("confidence", 0.6)) >= 0.7 else "medium",
            "crew": "remediation",
            "labels": ["remediation", "auto-spawned"],
            "meta": {
                "spawned_from": parent_id,
                "finding_key": finding.get("key"),
                "confidence": finding.get("confidence"),
            },
            "actor": actor,
        }
        if scope is not None:
            payload["scope"] = scope
        try:
            child = client.create_subcard(parent_id, **payload)
        except KanbanError as exc:
            # A 409 is the board refusing a widening - expected, and reported.
            reasons = exc.reasons or [str(exc)]
            results.append({"status": "refused", "reasons": reasons})
            if stats is not None:
                stats.remediation_refused = getattr(stats, "remediation_refused", 0) + 1
            continue
        results.append(
            {
                "status": "created",
                "child_id": child.get("card_id") or child.get("id"),
                "scope": (child.get("scope") or {}),
            }
        )
        if stats is not None:
            stats.remediation_spawned = getattr(stats, "remediation_spawned", 0) + 1
    return results
