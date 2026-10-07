"""Card dependencies: "this card waits for that card".

Why an edge and not the ``Blocked`` column
------------------------------------------
The board already has a ``Blocked`` column, so it is tempting to model "B waits
for A" by parking B there. That loses the reason: ``Blocked`` means *a human must
act*, while a dependency means *another card must finish*. Collapsing the two
makes the board unable to tell "I am stuck" from "I am waiting", and the operator
ends up clearing blockers that were never blockers.

So a dependency is an edge between cards, and the column stays a statement about
*this* card alone.

Direction, and the one guard that matters
-----------------------------------------
``card.meta["depends_on"] = [A]`` reads "this card needs A". The guard that
matters is the **negative** one: a card whose dependency has not finished may not
enter ``Running``.

The positive direction - auto-starting the dependant when the dependency lands -
is deliberately *not* implemented. Starting work is a human or agent decision,
and a system that starts cards on its own is the runaway the kill switch exists
to stop. A card that is merely *ready* is still a card somebody has to pick up.

Two structural failures worth naming
------------------------------------
* a card that depends on **itself** can never run - the guard would deadlock it
  forever with no explanation; and
* a **cycle** (A waits for B, B waits for A) does the same to every card in it.

Both are refused when the edge is added rather than detected at run time, because
a cycle discovered at move time is a board that has already been lied to.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .models import Card, Column

#: The meta key that carries dependency edges. A list of card ids.
DEPENDS_ON_KEY = "depends_on"

#: A card in one of these columns has finished; its dependants stop waiting.
#: ``Done`` only - ``Blocked`` is not "finished", it is "parked", and treating it
#: as satisfied would let a blocked prerequisite release its dependants.
SATISFIED_COLUMNS = (Column.DONE,)


class DependencyError(Exception):
    """Raised when a dependency edge may not be created.

    Carries machine-readable ``reasons`` in the same vocabulary the rest of the
    system uses, so the board can render the refusal without a second mapping.
    """

    def __init__(self, message: str, reasons: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.reasons = reasons or [message]


class DependencyCodes:
    """Stable machine-readable codes for dependency refusals and guards."""

    UNMET = "dependency_unmet"
    MISSING = "dependency_missing"
    SELF = "dependency_self"
    CYCLE = "dependency_cycle"


# ---------------------------------------------------------------------------
# reading the edges
# ---------------------------------------------------------------------------
def dependency_ids(card: Card) -> list[str]:
    """The card ids *card* waits for, in declaration order, de-duplicated.

    Reads from ``meta`` rather than a first-class field so that a card written by
    an older version - which had no dependencies - parses unchanged, and a card
    with a malformed value degrades to "no dependencies" instead of failing to
    load. The value is validated on the way *in* (see :func:`edge_problems`), so a
    malformed one can only arrive from a hand-edited document.
    """
    raw = (card.meta or {}).get(DEPENDS_ON_KEY) or []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
    return out


def is_satisfied(card: Card) -> bool:
    """Has *card* finished, from the point of view of something waiting on it?"""
    return card.column in SATISFIED_COLUMNS


def unmet_reasons(card: Card, dependencies: Iterable[Card], *, present: Optional[set[str]] = None) -> list[str]:
    """One reason per dependency of *card* that has not finished.

    A dependency named by the card but absent from ``dependencies`` is reported
    separately (``dependency_missing``) rather than silently skipped: a card
    waiting on something that does not exist can never become ready, and a guard
    that ignored it would let the card run *past* a prerequisite nobody can see.
    """
    reasons: list[str] = []
    by_id = {dep.card_id: dep for dep in dependencies}
    for dep_id in dependency_ids(card):
        dep = by_id.get(dep_id)
        if dep is None:
            if present is not None and dep_id in present:
                continue  # resolved elsewhere; caller says it exists
            reasons.append(
                f"{DependencyCodes.MISSING}: depends on {dep_id}, which does not exist"
            )
            continue
        if not is_satisfied(dep):
            reasons.append(
                f"{DependencyCodes.UNMET}: waiting on {dep.card_id} "
                f"({dep.column.value}) - '{dep.title}'"
            )
    return reasons


def dependency_reasons(
    card: Card,
    to_column: str,
    *,
    dependencies: Optional[list[Card]] = None,
) -> list[str]:
    """Guards for a proposed move, from the dependency graph.

    Only ``Running`` is gated. Moving *into* a dependency on the way to Done is
    not a violation - it is the path a card takes once its prerequisite landed.
    """
    if to_column != Column.RUNNING.value:
        return []
    return unmet_reasons(card, dependencies or [])


# ---------------------------------------------------------------------------
# validating a new edge
# ---------------------------------------------------------------------------
def reachable(start: str, edges: dict[str, list[str]], *, limit: int = 500) -> set[str]:
    """Every card id reachable from *start* by following dependency edges.

    Iterative, not recursive: a board is user-authored data and a deep chain must
    not be able to exhaust the stack. ``limit`` bounds the walk so a pathological
    board cannot hang the API.
    """
    seen: set[str] = set()
    frontier = [start]
    while frontier and len(seen) < limit:
        node = frontier.pop()
        for nxt in edges.get(node, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            frontier.append(nxt)
    return seen


def edge_problems(
    card_id: str,
    depends_on: str,
    *,
    edges: dict[str, list[str]],
) -> list[str]:
    """Reasons the edge ``card_id -> depends_on`` may not be added. Empty = ok.

    ``edges`` is the *current* graph (every card's ``dependency_ids``), which the
    caller derives from the store. Passing the graph in rather than a store keeps
    this module pure and testable without a database.
    """
    reasons: list[str] = []
    if not depends_on or depends_on == card_id:
        reasons.append(
            f"{DependencyCodes.SELF}: a card cannot depend on itself - it would "
            "never be able to run"
        )
        return reasons
    # Adding card_id -> depends_on closes a loop exactly when card_id is already
    # reachable *from* depends_on.
    if card_id in reachable(depends_on, edges):
        path = _shortest_cycle_path(card_id, depends_on, edges)
        rendered = " -> ".join([card_id, depends_on, *path[1:]]) if path else f"{card_id} -> {depends_on}"
        reasons.append(
            f"{DependencyCodes.CYCLE}: that edge closes a dependency loop ({rendered})"
        )
    return reasons


def _shortest_cycle_path(card_id: str, depends_on: str, edges: dict[str, list[str]]) -> list[str]:
    """Path ``depends_on -> ... -> card_id`` for the refusal message, or ``[]``.

    Operators act on a concrete loop ("A -> B -> A") and not on the word "cycle",
    so the message names the cards. Breadth-first, so the loop shown is the
    shortest one - the easiest to see and to break.
    """
    queue: list[list[str]] = [[depends_on]]
    seen = {depends_on}
    while queue:
        path = queue.pop(0)
        tail = path[-1]
        if tail == card_id:
            return path
        if len(path) > 64:
            continue
        for nxt in edges.get(tail, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            queue.append([*path, nxt])
    return []


# ---------------------------------------------------------------------------
# the tree
# ---------------------------------------------------------------------------
def build_tree(
    root: Card,
    *,
    children_by_parent: dict[str, list[Card]],
    cards_by_id: dict[str, Card],
    max_depth: int = 8,
) -> dict[str, Any]:
    """Nest a card, its children and its blockers into one render-ready tree.

    The board shows a parent card that spawned work and a card that waits on
    something else, and those two relationships are drawn in the same panel. A
    caller that fetched them separately would render two graphs that disagree, so
    they are assembled here from one snapshot of the store.

    ``max_depth`` bounds the recursion. A parent/child loop cannot exist through
    the API (a card is created with a parent, and the parent already exists), but
    the tree is built from stored documents rather than from the API's own
    invariants, so the bound is here rather than assumed.
    """

    def _blockers(card: Card) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for dep_id in dependency_ids(card):
            dep = cards_by_id.get(dep_id)
            out.append(
                {
                    "card_id": dep_id,
                    "title": dep.title if dep else None,
                    "column": dep.column.value if dep else None,
                    "satisfied": bool(dep and is_satisfied(dep)),
                    "missing": dep is None,
                }
            )
        return out

    def _node(card: Card, depth: int) -> dict[str, Any]:
        kids = children_by_parent.get(card.card_id, [])
        return {
            "card_id": card.card_id,
            "title": card.title,
            "column": card.column.value,
            "priority": card.priority.value,
            "assignee": card.assignee,
            "killed": card.killed,
            "max_tier": card.max_tier,
            "blockers": _blockers(card),
            "waiting": any(not b["satisfied"] for b in _blockers(card)),
            "child_count": len(kids),
            "open_child_count": sum(1 for k in kids if k.column.value not in ("Done", "Blocked")),
            "truncated": depth >= max_depth,
            "children": [] if depth >= max_depth else [_node(k, depth + 1) for k in kids],
        }

    tree = _node(root, 0)
    return {
        "root_id": root.card_id,
        "max_depth": max_depth,
        "tree": tree,
        "counts": _counts(tree),
    }


def _counts(node: dict[str, Any]) -> dict[str, int]:
    """Totals for a rendered tree: nodes, blockers and how many are unresolved."""
    nodes = 1
    blockers = len(node.get("blockers") or [])
    waiting = 1 if node.get("waiting") else 0
    for child in node.get("children") or []:
        sub = _counts(child)
        nodes += sub["nodes"]
        blockers += sub["blockers"]
        waiting += sub["waiting"]
    return {"nodes": nodes, "blockers": blockers, "waiting": waiting}
