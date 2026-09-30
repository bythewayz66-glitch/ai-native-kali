"""Typed schema for the L6 memory layer.

Blueprint ref: section 03, layer **L6 - memory / knowledge stores**. This was the
one layer the blueprint named that the Phase 1 build had not implemented; it is
listed as a stated gap in ``docs/CREWAI_DEFINITIONS.md``.

Two kinds of memory, deliberately kept apart:

* **episodic** - an append-only, ordered record of what happened on an
  engagement: this card ran, this tool returned this, this gate was approved.
  It answers *"what did we already do?"*
* **semantic** - distilled, reusable statements: this host runs that server, this
  credential worked, this subnet is out of scope. It answers *"what do we know?"*

The distinction is not decoration. Episodic records are evidence and must never
be edited; semantic records are conclusions and are expected to be superseded.
Collapsing them into one table makes it impossible to tell a fact from a log
line, which is exactly the confusion an auditor cannot afford.

Everything is scoped by ``engagement``. Cross-engagement leakage is the single
most damaging failure mode for a memory store in a security context, so the
scope is a required field on every read and write rather than an optional
filter.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

MemoryKind = Literal["episodic", "semantic"]

#: Episode categories. Kept closed so observability can aggregate reliably.
EpisodeType = Literal[
    "observation",  # something noticed about the target
    "tool_run",  # a tool was invoked
    "finding",  # a confirmed weakness
    "decision",  # a human or agent chose something
    "hypothesis",  # an unverified lead
    "gate",  # an approval was requested/decided
    "error",  # something failed
]


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Episode(BaseModel):
    """One event in an engagement's ordered history."""

    episode_id: str = Field(default_factory=lambda: new_id("epi"))
    ts: str = Field(default_factory=utcnow)

    #: Required. Memory without a scope is memory that leaks.
    engagement: str
    kind: EpisodeType = "observation"

    #: Provenance - what produced this.
    card_id: Optional[str] = None
    agent: Optional[str] = None
    board_id: Optional[str] = None
    target: Optional[str] = None

    summary: str
    detail: str = ""

    #: Free-form structured payload (tool output digest, finding fields, ...).
    data: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    #: Set when this episode supersedes or is superseded by another.
    supersedes: Optional[str] = None


class Fact(BaseModel):
    """A distilled, reusable statement of knowledge."""

    fact_id: str = Field(default_factory=lambda: new_id("fct"))
    ts: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    engagement: str
    key: str  # short stable identifier, e.g. "web-server:shop.example.net"
    statement: str
    detail: str = ""

    #: 0.0-1.0. A scanner hit is not the same as a confirmed finding.
    confidence: float = 0.5
    status: Literal["active", "superseded", "retracted"] = "active"

    source_card: Optional[str] = None
    agent: Optional[str] = None
    target: Optional[str] = None
    tags: list[str] = Field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = utcnow()


class SearchHit(BaseModel):
    """One result from a memory search."""

    kind: MemoryKind
    id: str
    engagement: str
    ts: str
    summary: str
    score: float = 0.0
    record: dict[str, Any] = Field(default_factory=dict)


class ContextBundle(BaseModel):
    """What a crew is handed when it picks up a card.

    This is the read path that actually matters: rather than a raw dump, the
    bundle is bounded and ordered so it can be pasted into a model prompt
    without blowing the context window.
    """

    engagement: str
    card_id: Optional[str] = None
    target: Optional[str] = None

    #: Most recent episodes on this engagement (newest first).
    recent_episodes: list[Episode] = Field(default_factory=list)
    #: Episodes already recorded against this exact card.
    card_episodes: list[Episode] = Field(default_factory=list)
    #: Active facts, highest confidence first.
    facts: list[Fact] = Field(default_factory=list)
    #: Facts that mention the target, if one was supplied.
    target_facts: list[Fact] = Field(default_factory=list)
    #: Everything recorded about this target before, newest first.
    target_episodes: list[Episode] = Field(default_factory=list)

    counts: dict[str, int] = Field(default_factory=dict)
    truncated: bool = False

    def as_prompt_block(self, *, max_chars: int = 4000) -> str:
        """Render the bundle as a bounded, human-readable prompt section."""
        lines: list[str] = [f"ENGAGEMENT MEMORY ({self.engagement})"]
        if self.target_facts:
            lines.append("\nKnown about the target:")
            for f in self.target_facts:
                lines.append(f"  - [{f.confidence:.2f}] {f.statement}")
        if self.facts:
            lines.append("\nEstablished facts:")
            for f in self.facts:
                lines.append(f"  - [{f.confidence:.2f}] {f.statement}")
        if self.card_episodes:
            lines.append("\nAlready done on this card:")
            for e in self.card_episodes:
                lines.append(f"  - {e.ts} {e.kind}: {e.summary}")
        if self.target_episodes:
            lines.append("\nPrior work on this target:")
            for e in self.target_episodes:
                lines.append(f"  - {e.ts} {e.kind}: {e.summary}")
        elif self.recent_episodes:
            lines.append("\nRecent activity:")
            for e in self.recent_episodes:
                lines.append(f"  - {e.ts} {e.kind}: {e.summary}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n  ... (truncated)"
        return text
