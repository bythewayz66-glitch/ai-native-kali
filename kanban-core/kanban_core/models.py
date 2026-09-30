"""Typed schemas for the Kanban orchestration backbone.

Blueprint ref: section 03.7 (data model & API).

A card is the universal unit of work on the system:
    card = task spec + context + tool bindings + approval gate
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def utcnow() -> str:
    """ISO-8601 UTC timestamp with millisecond precision."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------
class Column(str, Enum):
    """The card lifecycle. Blueprint: Backlog -> Assigned -> Running -> Review -> Done/Blocked."""

    BACKLOG = "Backlog"
    ASSIGNED = "Assigned"
    RUNNING = "Running"
    REVIEW = "Review"
    DONE = "Done"
    BLOCKED = "Blocked"


#: Columns rendered left-to-right on a board (Blocked is a side lane).
COLUMN_ORDER: list[Column] = [
    Column.BACKLOG,
    Column.ASSIGNED,
    Column.RUNNING,
    Column.REVIEW,
    Column.DONE,
]


def all_columns() -> list[Column]:
    return COLUMN_ORDER + [Column.BLOCKED]


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


#: Sort rank so boards can order cards deterministically.
PRIORITY_RANK: dict[str, int] = {
    Priority.CRITICAL.value: 0,
    Priority.HIGH.value: 1,
    Priority.MEDIUM.value: 2,
    Priority.LOW.value: 3,
}


class GuardrailTier(int, Enum):
    """Blueprint section 05: T0 passive .. T3 offensive/high-impact."""

    T0 = 0
    T1 = 1
    T2 = 2
    T3 = 3


#: Human-readable meaning of each guardrail tier.
TIER_LABELS: dict[int, str] = {
    0: "T0 passive - read-only lookups, no packets to target",
    1: "T1 low-impact - active but non-destructive probing",
    2: "T2 intrusive - exploitation / auth attempts, needs scope + approval",
    3: "T3 high-impact - exploit execution, wireless attacks, needs approval + sandbox",
}


BoardKind = Literal["agent", "engagement", "system", "personal"]

ApprovalStatus = Literal["pending", "approved", "rejected"]


# ---------------------------------------------------------------------------
# scope / authorization
# ---------------------------------------------------------------------------
class Scope(BaseModel):
    """Authorization scope attached to a card.

    A card with T2+ tool bindings may only enter Running when its scope covers
the targets it will touch (blueprint section 08, stage 2: scope enforcement).
    """

    targets: list[str] = Field(default_factory=list, description="Exact hosts, domains or *.wildcards")
    cidrs: list[str] = Field(default_factory=list, description="Authorized networks in CIDR form")
    authorization_ref: Optional[str] = Field(
        default=None, description="Ticket/contract reference proving authorization"
    )
    authorized_by: Optional[str] = None
    expires_at: Optional[str] = None

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _normalize(target: str) -> str:
        t = (target or "").strip().lower()
        if "://" in t:
            t = t.split("://", 1)[1]
        for sep in ("/", "?", "#"):
            t = t.split(sep, 1)[0]
        if t.startswith("["):  # bracketed IPv6 literal, may carry :port
            return t[1:].split("]", 1)[0]
        try:
            # A bare IPv6 address has no port syntax - accept it whole.
            ipaddress.ip_address(t)
            return t
        except ValueError:
            pass
        return t.split(":", 1)[0].rstrip(".")

    def is_expired(self, now: Optional[dt.datetime] = None) -> bool:
        if not self.expires_at:
            return False
        now = now or dt.datetime.now(dt.timezone.utc)
        try:
            exp = dt.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=dt.timezone.utc)
        return now > exp

    def covers(self, target: str) -> bool:
        """True when *target* is inside this scope."""
        if not target:
            return False
        norm = self._normalize(target)
        for raw in self.targets:
            allowed = self._normalize(raw.lstrip("*."))
            if not allowed:
                continue
            if norm == allowed or norm.endswith("." + allowed):
                return True
            # Media-access-control addresses (wireless BSSIDs/station MACs) and
            # any other colon-bearing identifier are compared exactly. Without
            # this branch a BSSID scope could never match: the scope entry was
            # kept in its original case while the target was lower-cased, so
            # every wireless tool failed its scope check for the wrong reason.
            if self._is_media_access_control(norm) and norm == self._normalize(raw):
                return True
        try:
            ip = ipaddress.ip_address(norm)
        except ValueError:
            return False
        for cidr in self.cidrs:
            try:
                if ip in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _is_media_access_control(value: str) -> bool:
        """True for a colon-separated hex identifier (a MAC/BSSID)."""
        parts = value.split(":")
        return len(parts) == 6 and all(
            len(p) == 2 and all(ch in "0123456789abcdef" for ch in p) for p in parts
        )

    def summary(self) -> str:
        parts = list(self.targets) + [f"{c}" for c in self.cidrs]
        return ", ".join(parts) if parts else "(empty scope)"


# ---------------------------------------------------------------------------
# card sub-objects
# ---------------------------------------------------------------------------
class ToolBinding(BaseModel):
    """Binds a card (or an agent role) to a tool frontend tool."""

    name: str
    tier: int = GuardrailTier.T0.value
    dry_run: bool = True
    args: dict[str, Any] = Field(default_factory=dict)


class Trace(BaseModel):
    """One tool invocation, attached to the card (blueprint 04.3 tool-call traces)."""

    id: str = Field(default_factory=lambda: new_id("trc"))
    ts: str = Field(default_factory=utcnow)
    tool: str
    tier: int = 0
    args: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error", "denied", "blocked"] = "ok"

    duration_ms: int = 0
    exit_code: Optional[int] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    dry_run: bool = True
    audit_hash: Optional[str] = None
    agent: Optional[str] = None


class Artifact(BaseModel):
    """A durable output produced by a card's execution."""

    id: str = Field(default_factory=lambda: new_id("art"))
    ts: str = Field(default_factory=utcnow)
    name: str
    kind: Literal["report", "scan-output", "pcap", "log", "screenshot", "json", "other"] = "other"
    path: Optional[str] = None
    url: Optional[str] = None
    sha256: Optional[str] = None
    bytes: int = 0
    produced_by: Optional[str] = None
    summary: str = ""


class Approval(BaseModel):
    """Human-in-the-loop gate as a first-class card state (blueprint 03.5)."""

    id: str = Field(default_factory=lambda: new_id("apr"))
    ts: str = Field(default_factory=utcnow)
    requested_by: str
    reason: str
    tool: Optional[str] = None
    tier: int = 0
    status: ApprovalStatus = "pending"
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    note: str = ""


class CardEventRef(BaseModel):
    """Compact audit-friendly record of a card's own history (for replay)."""

    ts: str = Field(default_factory=utcnow)
    type: str
    actor: str = "system"
    detail: str = ""


# ---------------------------------------------------------------------------
# board + card
# ---------------------------------------------------------------------------
class Board(BaseModel):
    board_id: str = Field(default_factory=lambda: new_id("brd"))
    name: str
    kind: BoardKind = "agent"
    description: str = ""
    created_at: str = Field(default_factory=utcnow)
    default_crew: Optional[str] = None


class Card(BaseModel):
    card_id: str = Field(default_factory=lambda: new_id("crd"))
    title: str
    board_id: str
    column: Column = Column.BACKLOG
    description: str = ""

    # assignment
    assignee: Optional[str] = Field(default=None, description="Agent role name or human user id")
    assignee_kind: Literal["agent", "human"] = "agent"
    crew: Optional[str] = None

    priority: Priority = Priority.MEDIUM
    labels: list[str] = Field(default_factory=list)

    # task spec
    scope: Optional[Scope] = None
    tools: list[ToolBinding] = Field(default_factory=list)
    requires_approval: bool = False

    # results
    artifacts: list[Artifact] = Field(default_factory=list)
    traces: list[Trace] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    result: str = ""

    # bookkeeping
    parent_id: Optional[str] = None
    blocked_reason: Optional[str] = None
    killed: bool = False
    history: list[CardEventRef] = Field(default_factory=list)
    entered_column_at: str = Field(default_factory=utcnow)
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    meta: dict[str, Any] = Field(default_factory=dict)

    # -- derived helpers -------------------------------------------------
    @property
    def max_tier(self) -> int:
        return max([t.tier for t in self.tools], default=0)

    @property
    def pending_approval(self) -> Optional[Approval]:
        for a in self.approvals:
            if a.status == "pending":
                return a
        return None

    @property
    def is_gated(self) -> bool:
        """Does this card need a human gate before it may execute?"""
        return self.requires_approval or any(t.tier >= GuardrailTier.T2.value for t in self.tools)

    @property
    def approved(self) -> bool:
        return any(a.status == "approved" for a in self.approvals)

    @property
    def priority_rank(self) -> int:
        return PRIORITY_RANK.get(self.priority.value, 9)

    # -- mutation helpers (keep updated_at honest) -----------------------
    def touch(self) -> None:
        self.updated_at = utcnow()

    def note(self, type_: str, detail: str = "", actor: str = "system") -> None:
        self.history.append(CardEventRef(type=type_, actor=actor, detail=detail))
        if len(self.history) > 500:
            self.history = self.history[-500:]
        self.touch()


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
class Event(BaseModel):
    """A column transition / card mutation on the bus (blueprint 03.2)."""

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    seq: Optional[int] = None
    ts: str = Field(default_factory=utcnow)
    type: str
    board_id: Optional[str] = None
    card_id: Optional[str] = None
    from_column: Optional[str] = None
    to_column: Optional[str] = None
    actor: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    audit_hash: Optional[str] = None


#: Event type constants (avoid typos across modules).
class EventType:
    BOARD_CREATED = "board.created"
    CARD_CREATED = "card.created"
    CARD_MOVED = "card.moved"
    CARD_ASSIGNED = "card.assigned"
    CARD_TRACE_ADDED = "card.trace.added"
    CARD_ARTIFACT_ADDED = "card.artifact.added"
    CARD_APPROVAL_REQUESTED = "card.approval.requested"
    CARD_APPROVAL_DECIDED = "card.approval.decided"
    CARD_KILLED = "card.killed"
    CARD_BLOCKED = "card.blocked"
    CARD_UPDATED = "card.updated"
    #: Phase 8: a child card was spawned from a parent, or a spawn was refused.
    #: Both are recorded - a refusal an operator cannot see is one they re-attempt.
    CARD_SUBCARD_CREATED = "card.subcard.created"
    CARD_SUBCARD_REFUSED = "card.subcard.refused"


class AuditRecord(BaseModel):
    seq: int
    ts: str
    kind: str
    actor: str = "system"
    card_id: Optional[str] = None
    board_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str
