"""Service layer: the only sanctioned way to mutate Kanban state.

Blueprint ref: sections 03.2, 03.4, 03.5, 03.7.

Every mutation funnels through here so three invariants hold:

1. **State machine first** - a move is validated before anything is written.
2. **Bus + audit agree** - each mutation appends one hash-chained event and
   publishes that same event on the bus.
3. **Card history is replayable** - the card carries its own ordered history,
   so any card can be replayed end-to-end (observability 04.8).
"""
from __future__ import annotations

from typing import Any, Optional

from .bus import EventBus
from .models import (
    Approval,
    Artifact,
    Board,
    Card,
    Column,
    Event,
    EventType,
    Scope,
    ToolBinding,
    Trace,
    utcnow,
)
from .state_machine import (
    AGENT_WRITABLE,
    TransitionError,
    can_move,
    extract_targets,
    next_columns,
    validate_transition,
)
from .scope_model import (
    ScopeNarrowingError,
    audit_payload,
    narrowing_verdict,
    open_children,
    parent_tier_ceiling,
    refusal_payload,
    resolve_child_scope,
    validate_child,
)
from .store import Store


class NotFound(Exception):
    """Raised for unknown board/card ids."""


class KanbanService:
    """Board + card operations over a store, publishing to a bus."""

    def __init__(self, store: Store, bus: Optional[EventBus] = None) -> None:
        self.store = store
        self.bus = bus or EventBus()

    # ------------------------------------------------------------- events
    def _emit(
        self,
        type_: str,
        *,
        card: Optional[Card] = None,
        board_id: Optional[str] = None,
        from_column: Optional[str] = None,
        to_column: Optional[str] = None,
        actor: str = "system",
        payload: Optional[dict[str, Any]] = None,
    ) -> Event:
        """Append to the audit chain and fan out on the bus."""
        event = Event(
            ts=utcnow(),
            type=type_,
            board_id=board_id or (card.board_id if card else None),
            card_id=card.card_id if card else None,
            from_column=from_column,
            to_column=to_column,
            actor=actor,
            payload=payload or {},
        )
        stored = self.store.append_event(event)
        if card is not None:
            card.note(type_, detail=str(payload or ""), actor=actor)
        self.bus.publish(stored)
        return stored

    # ------------------------------------------------------------- boards
    def create_board(
        self,
        name: str,
        kind: str = "agent",
        *,
        description: str = "",
        default_crew: Optional[str] = None,
        board_id: Optional[str] = None,
    ) -> Board:
        board = Board(name=name, kind=kind, description=description, default_crew=default_crew)  # type: ignore[arg-type]
        if board_id:
            board.board_id = board_id
        self.store.create_board(board)
        self._emit(EventType.BOARD_CREATED, board_id=board.board_id, payload={"name": name, "kind": kind})
        return board

    def get_board(self, board_id: str) -> Board:
        board = self.store.get_board(board_id)
        if board is None:
            raise NotFound(f"board {board_id} not found")
        return board

    def list_boards(self) -> list[Board]:
        return self.store.list_boards()

    def board_view(self, board_id: str) -> dict[str, Any]:
        """Board rendered as columns -> cards, ready for the shell UI."""
        board = self.get_board(board_id)
        cards = self.store.list_cards(board_id=board_id)
        columns: dict[str, list[dict[str, Any]]] = {c.value: [] for c in Column}
        for card in sorted(cards, key=lambda c: (c.priority_rank, c.created_at)):
            columns[card.column.value].append(self.card_view(card))
        counts = {name: len(items) for name, items in columns.items()}
        return {
            "board": board.model_dump(),
            "columns": columns,
            "counts": counts,
            "total": len(cards),
        }

    # -------------------------------------------------------------- cards
    def create_card(
        self,
        title: str,
        board_id: str,
        *,
        description: str = "",
        priority: str = "medium",
        assignee: Optional[str] = None,
        assignee_kind: str = "agent",
        crew: Optional[str] = None,
        scope: Optional[Scope | dict[str, Any]] = None,
        tools: Optional[list[ToolBinding | dict[str, Any]]] = None,
        requires_approval: bool = False,
        labels: Optional[list[str]] = None,
        parent_id: Optional[str] = None,
        column: Column | str = Column.BACKLOG,
        meta: Optional[dict[str, Any]] = None,
        card_id: Optional[str] = None,
        actor: str = "system",
    ) -> Card:
        self.get_board(board_id)  # 404 early if the board is unknown

        if isinstance(scope, dict):
            scope = Scope.model_validate(scope)
        parsed_tools: list[ToolBinding] = []
        for item in tools or []:
            parsed_tools.append(item if isinstance(item, ToolBinding) else ToolBinding.model_validate(item))

        card = Card(
            title=title,
            board_id=board_id,
            description=description,
            priority=priority,  # type: ignore[arg-type]
            assignee=assignee,
            assignee_kind=assignee_kind,  # type: ignore[arg-type]
            crew=crew,
            scope=scope,
            tools=parsed_tools,
            requires_approval=requires_approval,
            labels=labels or [],
            parent_id=parent_id,
            meta=meta or {},
        )
        if card_id:
            card.card_id = card_id

        target_column = Column(column)
        if target_column is not Column.BACKLOG:
            # Creating straight into a later column (used by child-card spawning).
            card.column = target_column
            card.entered_column_at = utcnow()

        self.store.save_card(card)
        self._emit(
            EventType.CARD_CREATED,
            card=card,
            actor=actor,
            payload={
                "title": title,
                "column": card.column.value,
                "priority": card.priority.value,
                "max_tier": card.max_tier,
                "is_gated": card.is_gated,
                "parent_id": card.parent_id,
            },
        )
        self.store.save_card(card)
        return card

    # ---------------------------------------------------------- sub-cards
    def _family(self, card: Card) -> tuple[Optional[Card], list[Card]]:
        """The card's parent (if any) and its children.

        Resolved here rather than inside the state machine because the store is
        the only thing that can answer it cheaply, and the state machine is
        deliberately pure - it takes cards, not a database.
        """
        parent = self.store.get_card(card.parent_id) if card.parent_id else None
        children = self.store.list_cards(parent_id=card.card_id)
        return parent, children

    def create_subcard(
        self,
        parent_id: str,
        title: str,
        *,
        description: str = "",
        priority: str = "medium",
        assignee: Optional[str] = None,
        crew: Optional[str] = None,
        scope: Optional[Scope | dict[str, Any]] = None,
        tools: Optional[list[ToolBinding | dict[str, Any]]] = None,
        requires_approval: bool = False,
        labels: Optional[list[str]] = None,
        column: Column | str = Column.BACKLOG,
        meta: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> Card:
        """Spawn a child card whose scope is inside its parent's.

        The one rule this method exists to enforce: **a child's scope must be a
        subset of its parent's, never wider.** See
        ``docs/SUB_CARD_SCOPE_MODEL.md`` for the design and
        ``scope_model.validate_child`` for the check.

        ``scope=None`` means *inherit the parent's scope*, which is the safe
        default: a caller who forgets to narrow gets the parent's authorization
        rather than an unbounded one. An explicitly empty scope is a widening and
        is refused, not silently upgraded to the parent's.
        """
        parent = self.get_card(parent_id)
        parsed_tools: list[ToolBinding] = [
            item if isinstance(item, ToolBinding) else ToolBinding.model_validate(item)
            for item in (tools or [])
        ]
        child_tier = max([t.tier for t in parsed_tools], default=0)
        resolved = resolve_child_scope(parent, scope)

        reasons = validate_child(parent, resolved, child_tier=child_tier)
        if reasons:
            # Record the refusal *before* raising. A refused spawn that leaves no
            # trace is one an operator will re-attempt - the same argument the
            # tool layer makes for recording refused calls.
            self._emit(
                EventType.CARD_SUBCARD_REFUSED,
                card=parent,
                actor=actor,
                payload=refusal_payload(parent, reasons, requested=resolved),
            )
            self.store.save_card(parent)
            raise ScopeNarrowingError(
                f"child of {parent_id} would widen its parent's scope", reasons
            )

        child = self.create_card(
            title,
            parent.board_id,
            description=description,
            priority=priority,
            assignee=assignee,
            crew=crew,
            scope=resolved,
            tools=parsed_tools,
            requires_approval=requires_approval,
            labels=labels,
            parent_id=parent_id,
            column=column,
            meta=meta,
            actor=actor,
        )
        # The audit entry records the *relationship* - both scope summaries and
        # the verdict - not just the child's scope. A chain that recorded only the
        # child could not answer "was this child ever wider than its parent?"
        # after the parent was edited.
        self._emit(
            EventType.CARD_SUBCARD_CREATED,
            card=child,
            actor=actor,
            payload=audit_payload(parent, child, verdict=narrowing_verdict(parent, child.scope)),
        )
        self.store.save_card(child)
        return child

    def get_card(self, card_id: str) -> Card:
        card = self.store.get_card(card_id)
        if card is None:
            raise NotFound(f"card {card_id} not found")
        return card

    def list_cards(self, **kwargs: Any) -> list[Card]:
        return self.store.list_cards(**kwargs)

    def card_view(self, card: Card) -> dict[str, Any]:
        """Add computed fields the UI and the bridge both rely on."""
        data = card.model_dump(mode="json")
        parent, children = self._family(card)
        ok, reasons = (
            (True, [])
            if card.column is Column.DONE
            else can_move(card, Column.RUNNING, parent=parent, children=children)
        )
        data["max_tier"] = card.max_tier
        data["is_gated"] = card.is_gated
        data["has_pending_approval"] = card.pending_approval is not None
        data["next_columns"] = (
            [] if card.killed else next_columns(card, parent=parent, children=children)
        )
        data["can_run"] = bool(ok or card.column in (Column.RUNNING, Column.REVIEW))
        data["blocked_reasons"] = reasons
        # Family facts the board renders: a parent shows how much spawned work is
        # still open, and a child shows what it was spawned from.
        data["child_count"] = len(children)
        data["open_child_count"] = len(open_children(children))
        data["parent_killed"] = bool(parent is not None and parent.killed)
        return data

    # ------------------------------------------------------------- moves
    def move_card(
        self,
        card_id: str,
        to_column: Column | str,
        *,
        actor: str = "system",
        force: bool = False,
        actor_is_agent: bool = False,
        note: str = "",
    ) -> Card:
        card = self.get_card(card_id)
        target = Column(to_column) if isinstance(to_column, str) else to_column

        allowed = AGENT_WRITABLE if actor_is_agent else None
        parent, children = self._family(card)
        validate_transition(
            card, target, force=force, allowed=allowed, parent=parent, children=children
        )

        frm = card.column
        card.column = target
        card.entered_column_at = utcnow()
        if target is not Column.BLOCKED:
            card.blocked_reason = None
        card.note("column.entered", f"{frm.value} -> {target.value} by {actor} {note}".strip(), actor=actor)
        self.store.save_card(card)

        event_type = EventType.CARD_BLOCKED if target is Column.BLOCKED else EventType.CARD_MOVED
        self._emit(
            event_type,
            card=card,
            from_column=frm.value,
            to_column=target.value,
            actor=actor,
            payload={
                "assignee": card.assignee,
                "crew": card.crew,
                "priority": card.priority.value,
                "note": note,
                "forced": force,
            },
        )
        self.store.save_card(card)
        return card

    def assign_card(
        self,
        card_id: str,
        assignee: str,
        *,
        crew: Optional[str] = None,
        actor: str = "system",
        move_to_assigned: bool = True,
    ) -> Card:
        card = self.get_card(card_id)
        card.assignee = assignee
        card.assignee_kind = "agent" if crew else card.assignee_kind
        if crew:
            card.crew = crew
        self.store.save_card(card)
        self._emit(
            EventType.CARD_ASSIGNED,
            card=card,
            actor=actor,
            payload={"assignee": assignee, "crew": crew},
        )
        if move_to_assigned and card.column is Column.BACKLOG:
            card = self.move_card(card_id, Column.ASSIGNED, actor=actor, note=f"auto-assigned to {assignee}")
        self.store.save_card(card)
        return card

    def block_card(self, card_id: str, reason: str, *, actor: str = "system") -> Card:
        card = self.get_card(card_id)
        card.blocked_reason = reason
        self.store.save_card(card)
        card = self.move_card(card_id, Column.BLOCKED, actor=actor, force=True, note=reason)
        card.blocked_reason = reason
        self.store.save_card(card)
        return card

    def kill_card(self, card_id: str, *, actor: str = "human", reason: str = "operator kill switch") -> Card:
        """The kill switch (blueprint 03.5). Freezes the card where it stands."""
        card = self.get_card(card_id)
        card.killed = True
        card.blocked_reason = reason
        card.note("killed", f"{reason} by {actor}", actor=actor)
        self.store.save_card(card)
        self._emit(EventType.CARD_KILLED, card=card, actor=actor, payload={"reason": reason})
        self.store.save_card(card)
        return card

    # ----------------------------------------------------------- trace/art
    def add_trace(self, card_id: str, trace: Trace | dict[str, Any], *, actor: str = "agent") -> Card:
        card = self.get_card(card_id)
        parsed = trace if isinstance(trace, Trace) else Trace.model_validate(trace)
        card.traces.append(parsed)
        self.store.save_card(card)
        self._emit(
            EventType.CARD_TRACE_ADDED,
            card=card,
            actor=actor,
            payload={
                "tool": parsed.tool,
                "tier": parsed.tier,
                "status": parsed.status,
                "duration_ms": parsed.duration_ms,
                "dry_run": parsed.dry_run,
                "audit_hash": parsed.audit_hash,
            },
        )
        self.store.save_card(card)
        return card

    def add_artifact(self, card_id: str, artifact: Artifact | dict[str, Any], *, actor: str = "agent") -> Card:
        card = self.get_card(card_id)
        parsed = artifact if isinstance(artifact, Artifact) else Artifact.model_validate(artifact)
        card.artifacts.append(parsed)
        self.store.save_card(card)
        self._emit(
            EventType.CARD_ARTIFACT_ADDED,
            card=card,
            actor=actor,
            payload={"name": parsed.name, "kind": parsed.kind, "sha256": parsed.sha256, "bytes": parsed.bytes},
        )
        self.store.save_card(card)
        return card

    def set_result(self, card_id: str, result: str, *, actor: str = "agent") -> Card:
        card = self.get_card(card_id)
        card.result = result
        self.store.save_card(card)
        self._emit(EventType.CARD_UPDATED, card=card, actor=actor, payload={"result_len": len(result)})
        self.store.save_card(card)
        return card

    # ----------------------------------------------------------- approvals
    def request_approval(
        self,
        card_id: str,
        *,
        reason: str,
        requested_by: str = "agent",
        tool: Optional[str] = None,
        tier: Optional[int] = None,
        actor: str = "agent",
    ) -> Card:
        """Open a human gate. The card parks where it is until decided."""
        card = self.get_card(card_id)
        approval = Approval(
            requested_by=requested_by,
            reason=reason,
            tool=tool,
            tier=tier if tier is not None else card.max_tier,
        )
        card.approvals.append(approval)
        card.note("approval.requested", reason, actor=actor)
        self.store.save_card(card)
        self._emit(
            EventType.CARD_APPROVAL_REQUESTED,
            card=card,
            actor=actor,
            payload={
                "approval_id": approval.id,
                "reason": reason,
                "tool": tool,
                "tier": approval.tier,
                "scope": card.scope.summary() if card.scope else None,
            },
        )
        self.store.save_card(card)
        return card

    def decide_approval(
        self,
        card_id: str,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str = "human",
        note: str = "",
    ) -> Card:
        card = self.get_card(card_id)
        target: Optional[Approval] = None
        for approval in card.approvals:
            if approval.id == approval_id:
                target = approval
                break
        if target is None:
            raise NotFound(f"approval {approval_id} not found on card {card_id}")
        if target.status != "pending":
            raise TransitionError(f"approval {approval_id} was already {target.status}")

        target.status = "approved" if approved else "rejected"
        target.decided_by = decided_by
        target.decided_at = utcnow()
        target.note = note
        card.note("approval.decided", f"{target.status} by {decided_by}", actor=decided_by)
        self.store.save_card(card)
        self._emit(
            EventType.CARD_APPROVAL_DECIDED,
            card=card,
            actor=decided_by,
            payload={"approval_id": approval_id, "status": target.status, "note": note},
        )
        self.store.save_card(card)
        return card

    # ------------------------------------------------------------ overview
    def board_metrics(self, board_id: str) -> dict[str, Any]:
        """Per-board rollup used by the observability dashboard."""
        cards = self.store.list_cards(board_id=board_id)
        by_column: dict[str, int] = {c.value: 0 for c in Column}
        tokens = cost = 0
        latency = 0
        for card in cards:
            by_column[card.column.value] += 1
            for trace in card.traces:
                latency += int(trace.duration_ms or 0)
                tokens += int((trace.args or {}).get("__tokens", 0) or 0)
                cost += float((trace.args or {}).get("__cost_usd", 0.0) or 0.0)
        runs = sum(len(c.traces) for c in cards)
        return {
            "board_id": board_id,
            "cards": len(cards),
            "by_column": by_column,
            "blocked": by_column.get(Column.BLOCKED.value, 0),
            "pending_approvals": sum(1 for c in cards if c.pending_approval),
            "killed": sum(1 for c in cards if c.killed),
            "tool_runs": runs,
            "tokens": tokens,
            "cost_usd": round(cost, 6),
            "total_latency_ms": latency,
            "avg_latency_ms": int(latency / runs) if runs else 0,
        }

    # ---------------------------------------------------------- replay API
    def replay_card(self, card_id: str) -> dict[str, Any]:
        """Full execution history for one card (observability 04.8)."""
        card = self.get_card(card_id)
        events = self.store.list_events(card_id=card_id, limit=1000)
        return {
            "card_id": card_id,
            "title": card.title,
            "board_id": card.board_id,
            "current_column": card.column.value,
            "killed": card.killed,
            "history": [h.model_dump() for h in card.history],
            "traces": [t.model_dump() for t in card.traces],
            "artifacts": [a.model_dump() for a in card.artifacts],
            "approvals": [a.model_dump() for a in card.approvals],
            "events": [
                {
                    "seq": e.seq,
                    "ts": e.ts,
                    "type": e.type,
                    "actor": e.actor,
                    "from": e.from_column,
                    "to": e.to_column,
                    "audit_hash": e.audit_hash,
                }
                for e in events
            ],
        }

    def chain_verdict(self) -> dict[str, Any]:
        return self.store.verify_chain()

    def scoped_targets(self, card: Card) -> list[str]:
        return extract_targets(card)
