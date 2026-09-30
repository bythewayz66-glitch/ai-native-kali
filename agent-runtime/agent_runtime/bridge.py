"""The Kanban <-> CrewAI bridge.

Blueprint ref: section 03.2 - 'cards trigger crews; crew agents claim cards, move
them through columns, write results back to the card'.

This is the heart of the Phase 1 loop:

    watch Assigned  ->  claim (Assigned -> Running)
                    ->  run the crew's tools (guardrailed, audited)
                    ->  attach traces + artifacts
                    ->  write the result back onto the card
                    ->  release to Review (or Blocked)

The bridge **never** writes Done: that column belongs to the human verifier
(blueprint 03.5), and the state machine enforces it independently of the bridge's
intentions.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .client import KanbanClient, KanbanError
from .crews import CrewDef, crew_for_board, get_crew
from .roles import AgentRole, get_role


@dataclass
class BridgeStats:
    """Rolling counters, surfaced on the runtime's /health endpoint."""

    polls: int = 0
    claimed: int = 0
    runs_ok: int = 0
    runs_partial: int = 0
    runs_blocked: int = 0
    runs_failed: int = 0
    gated: int = 0
    released: int = 0
    skipped: int = 0
    errors: int = 0

    #: The two claim paths are counted separately. Without this the socket
    #: subscription could be dead and every card still processed by the poll
    #: fallback, while `/health` looked perfectly healthy.
    socket_claims: int = 0
    poll_claims: int = 0
    events_dispatched: int = 0
    events_skipped: int = 0
    fallback_polls: int = 0
    last_claim_source: Optional[str] = None

    last_error: Optional[str] = None
    last_poll_at: Optional[float] = None

    #: Recall is counted separately from writes. A bridge whose recall path is
    #: silently failing (store down, vector index broken) would otherwise look
    #: identical to one where the engagement is simply empty - the crew gets a
    #: context-less prompt either way, and only these counters tell them apart.
    memory_recalls: int = 0
    memory_failures: int = 0
    memory_hits: int = 0
    #: Phase 5, item 1: the graph seed is what makes recall act on the *plan*,
    #: not just decorate the prompt. A bridge that recalls records but never
    #: derives a seed would still look healthy on every counter above, so the
    #: seed is counted separately from the recall.
    memory_seeds: int = 0
    recall_prompt_chars: int = 0

    #: Phase 8, item 2: findings turned into scoped remediation sub-cards, and
    #: spawns the board refused. Counted separately because a bridge whose spawn
    #: path is silently failing (every proposal refused) would otherwise look
    #: identical to one whose crews simply found nothing to remediate.
    remediation_spawned: int = 0
    remediation_refused: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "claimed": self.claimed,
            "socket_claims": self.socket_claims,
            "poll_claims": self.poll_claims,
            "events_dispatched": self.events_dispatched,
            "events_skipped": self.events_skipped,
            "fallback_polls": self.fallback_polls,
            "last_claim_source": self.last_claim_source,
            "runs_ok": self.runs_ok,
            "runs_partial": self.runs_partial,
            "runs_blocked": self.runs_blocked,
            "runs_failed": self.runs_failed,
            "gated": self.gated,
            "released": self.released,
            "skipped": self.skipped,
            "errors": self.errors,
            # Recall counters, surfaced so a silently-broken memory path stays
            # distinguishable from "this engagement is simply empty".
            "memory_recalls": self.memory_recalls,
            "memory_failures": self.memory_failures,
            "memory_hits": self.memory_hits,
            "memory_seeds": self.memory_seeds,
            "recall_prompt_chars": self.recall_prompt_chars,
            "remediation_spawned": self.remediation_spawned,
            "remediation_refused": self.remediation_refused,
            "last_error": self.last_error,
            "last_poll_at": self.last_poll_at,
        }


@dataclass
class CardOutcome:
    """Result of processing one card, returned by :meth:`Bridge.process_card`."""

    card_id: str
    status: str  # ok | partial | blocked | gated | failed | skipped
    column: Optional[str] = None
    crew: Optional[str] = None
    summary: str = ""
    traces: int = 0
    artifacts: int = 0
    reasons: list[str] = field(default_factory=list)
    run: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "status": self.status,
            "column": self.column,
            "crew": self.crew,
            "summary": self.summary,
            "traces": self.traces,
            "artifacts": self.artifacts,
            "reasons": self.reasons,
            "run": self.run,
        }


class Bridge:
    """Polls the board and drives cards through the crew pipeline."""

    def __init__(
        self,
        client: Optional[KanbanClient] = None,
        *,
        tool_executor: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None,
        adapter: Any = None,
        agent_name: str = "bridge",
        max_tier: int = 3,
        respect_gates: bool = True,
        llm_runner: Any = None,
        model: Any = None,
        gate: Any = None,
        memory: Any = None,
        memory_engagement: Optional[str] = None,
    ) -> None:
        self.client = client or KanbanClient()
        self.agent_name = agent_name
        self.max_tier = max_tier
        self.respect_gates = respect_gates
        self.stats = BridgeStats()
        self._executor = tool_executor
        #: Idempotency + single-flight for the event-driven path: the socket
        #: worker and a fallback poll can both see the same card.
        self._claim_lock = threading.RLock()
        self._seen_event_ids: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=5000)
        self._in_flight: set[str] = set()
        if adapter is None:
            from .crewai_adapter import CrewAIAdapter

            adapter = CrewAIAdapter(tool_executor=self._execute_tool)
        self.adapter = adapter

        # -- Phase 3: model-driven crews behind the same card gate ---------
        # The gate is built against the board's own approval API, so the human
        # control plane is the card itself rather than a side channel. The LLM
        # runner is attached only when a model is configured; otherwise the
        # deterministic adapter stays the sole execution path.
        if gate is None and respect_gates:
            from .gate import HumanFeedbackGate, default_gate_config, reset_gate

            config = default_gate_config()
            gate = HumanFeedbackGate(
                self.client,
                poll_interval_s=config["poll_interval_s"],
                timeout_s=config["timeout_s"],
                auto_approve_in_dry_run=config["auto_approve_dry_run"],
            )
            reset_gate(gate)
        self.gate = gate

        if llm_runner is None and model is None:
            from .model_client import ModelConfig, get_model_client

            if ModelConfig.from_env().enabled:
                model = get_model_client()
        if llm_runner is None and model is not None:
            from .llm_crew import LLMCrewRunner

            llm_runner = LLMCrewRunner(
                self.adapter,
                model=model,
                gate=self.gate,
                tier_lookup=self._tier_lookup,
            )
        self.model = model
        self.llm_runner = llm_runner

        # -- L6 memory: prior context in, results out -----------------------
        # Optional by design. The bridge behaves identically with memory
        # absent, which is what keeps the memory layer from becoming a
        # load-bearing dependency for the whole orchestration path.
        if memory is None:
            from .memory import _default_engagement, get_bridge_memory

            memory = get_bridge_memory()
        else:
            from .memory import _default_engagement  # noqa: PLC0415

        self.memory = memory
        self.memory_engagement = memory_engagement or _default_engagement()

    # -- tool invocation --------------------------------------------------
    def _execute_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        scope: Optional[dict[str, Any]] = None,
        approved: bool = False,
        card_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Invoke a tool through the injected executor.

        The card's own authorization scope, gate state and id are threaded through
        to the tool layer on every call. Without the scope the guardrail engine
        would be evaluating an unscoped request - precisely the hole blueprint
        section 08 exists to close - and without the card id the tool audit log
        cannot be joined back to the card that caused the call.
        """
        if self._executor is None:
            raise RuntimeError(
                "no tool executor configured: pass tool_executor=<callable> or an explicit adapter"
            )
        return self._executor(tool_name, args, scope=scope, approved=approved, card_id=card_id)

    # -- scope pre-check --------------------------------------------------
    def _scope_reasons(self, card: dict[str, Any]) -> list[str]:
        """Reject out-of-scope work *before* claiming the card.

        The tool layer enforces this too (and remains the authoritative gate);
        doing it here as well means an out-of-scope card never even enters the
        Running column, so the board never shows work that was never allowed.

        **The board is deliberately stricter than the transport.** Any tool
        that declares ``requires_scope`` needs a scope *attached* for the bridge
        to claim it, regardless of tier; the guardrail engine only *mandates* a
        scope from T2 up. That asymmetry is intentional - refusing to display
        unauthorised work is cheap, and a card that never entered Running is a
        card an auditor never has to reason about. Both layers agree on the
        part that matters: once a scope is attached, coverage is enforced.
        """
        from tool_frontends.registry import get_registry
        from tool_frontends.targets import classify as classify_target
        from tool_frontends.targets import escapes as target_escapes

        scope = card.get("scope")
        registry = get_registry()
        reasons: list[str] = []
        for binding in card.get("tools") or []:
            spec = registry.get(binding.get("name", ""))
            if spec is None or not spec.requires_scope:
                continue
            args = binding.get("args") or {}
            target = spec.target_value(args)
            if not target:
                continue
            if not scope or not (scope.get("targets") or scope.get("cidrs")):
                reasons.append(f"{spec.name} requires an authorization scope; this card has none")
                continue
            from kanban_core.models import Scope as _Scope

            parsed = _Scope.model_validate(scope)

            # Value-based check, mirroring guardrails.evaluate() stage 2b. Without
            # this the board and the transport would disagree: a card whose
            # *declared* target was in scope but whose second argument named an
            # out-of-scope host would enter Running, and only the tool layer -
            # after the fact - would refuse it. The whole point of the board
            # pre-check is that unauthorised work is never displayed as running.
            for escape in target_escapes(
                args,
                scope=parsed,
                declared_params=spec.target_params,
                skip_keys=spec.scope_skip_params,
            ):
                reasons.append(f"{spec.name}: {escape}")

            # Declared parameters the classifier is silent about (single-label
            # hostnames) are checked by declaration, exactly as the engine does.
            for key in spec.target_params:
                raw = args.get(key)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                value = raw.strip()
                if classify_target(value) is not None:
                    continue
                if not parsed.covers(value):
                    reasons.append(
                        f"{spec.name}: target parameter '{key}' = '{value}' is outside "
                        f"the authorized scope"
                    )
        return reasons

    # -- gate bookkeeping -------------------------------------------------
    @staticmethod
    def _gate_state(card: dict[str, Any]) -> str:
        """One of: none | pending | approved | rejected."""
        approvals = card.get("approvals") or []
        if any(a.get("status") == "pending" for a in approvals):
            return "pending"
        if any(a.get("status") == "approved" for a in approvals):
            return "approved"
        if any(a.get("status") == "rejected" for a in approvals):
            return "rejected"
        return "none"

    # -- the loop ---------------------------------------------------------
    def poll_once(self, *, board_id: Optional[str] = None, limit: int = 20) -> list[CardOutcome]:
        """Process every card currently sitting in Assigned."""
        self.stats.polls += 1
        self.stats.last_poll_at = time.time()
        try:
            queue = self.client.agents_queue()
        except KanbanError as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            return []

        if board_id:
            queue = [c for c in queue if c.get("board_id") == board_id]

        outcomes: list[CardOutcome] = []
        for card in queue[:limit]:
            outcomes.append(self.process_card(card["card_id"], source="poll"))
        return outcomes

    # -- event-driven claim path -----------------------------------------
    def _remember_event(self, event_id: str) -> None:
        if event_id in self._seen_event_ids:
            return
        if len(self._seen_order) == self._seen_order.maxlen:
            self._seen_event_ids.discard(self._seen_order[0])
        self._seen_order.append(event_id)
        self._seen_event_ids.add(event_id)

    def process_event(self, event: dict[str, Any]) -> Optional[CardOutcome]:
        """Claim the card a bus event points at - once per event.

        Called from the event-stream worker thread. Two guards make this safe to
        run alongside the polling fallback:

        * **idempotency** - the same ``event_id`` never produces two claims, and
        * **single-flight** - a card already being processed is skipped instead
          of claimed a second time.

        Returns ``None`` when the event carried no actionable card.
        """
        if not isinstance(event, dict):
            return None
        card_id = event.get("card_id")
        if not card_id:
            return None

        event_id = event.get("event_id")
        with self._claim_lock:
            if event_id and event_id in self._seen_event_ids:
                self.stats.events_skipped += 1
                return None
            if event_id:
                self._remember_event(str(event_id))
            if card_id in self._in_flight:
                self.stats.events_skipped += 1
                return None
            self._in_flight.add(card_id)

        try:
            self.stats.events_dispatched += 1
            return self.process_card(card_id, source="socket")
        finally:
            with self._claim_lock:
                self._in_flight.discard(card_id)

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        watcher: Any = None,
        interval: float = 2.0,
        poll_when_connected: bool = False,
    ) -> None:
        """Take work forever: the event stream first, polling as the fallback.

        The poll only runs while the stream is *not* healthy (plus once at
        startup, which also covers the window before the socket has connected).
        With ``poll_when_connected=True`` both paths run, which is useful for
        debugging but means a card may be claimed by either - so the default
        keeps the socket as the real claim path rather than a decoy.
        """
        first = True
        while not stop_event.is_set():
            healthy = bool(watcher is not None and getattr(watcher, "connected", False))
            if first or poll_when_connected or not healthy:
                if not first and not healthy and watcher is not None and watcher.state.enabled:
                    self.stats.fallback_polls += 1
                try:
                    self.poll_once()
                except Exception as exc:  # pragma: no cover - defensive
                    self.stats.errors += 1
                    self.stats.last_error = str(exc)
            first = False
            stop_event.wait(interval)

    def process_card(self, card_id: str, *, source: str = "poll") -> CardOutcome:
        """Claim, run and release a single card.

        *source* records which path picked the card up (``socket`` or ``poll``)
        so the two can be told apart in the runtime's stats.
        """
        try:
            card = self.client.card(card_id)
        except KanbanError as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            return CardOutcome(card_id=card_id, status="failed", reasons=[str(exc)])

        if card.get("killed"):
            self.stats.skipped += 1
            return CardOutcome(card_id=card_id, status="skipped", reasons=["card was killed"])

        if card.get("column") != "Assigned":
            self.stats.skipped += 1
            return CardOutcome(
                card_id=card_id,
                status="skipped",
                column=card.get("column"),
                reasons=[f"card is in {card.get('column')}, not Assigned"],
            )

        crew = self._resolve_crew(card)
        role = self._resolve_role(card, crew)
        gate = self._gate_state(card)

        # --- human-in-the-loop gate --------------------------------------
        if self.respect_gates:
            if gate == "pending":
                # Already parked on an operator decision; do not re-ask.
                self.stats.skipped += 1
                return CardOutcome(
                    card_id=card_id,
                    status="gated",
                    column=card.get("column"),
                    crew=crew.name,
                    reasons=["waiting on a pending approval decision"],
                )
            if gate == "rejected":
                self.stats.runs_blocked += 1
                reason = "approval was rejected by the operator; re-request the gate to proceed"
                self._block(card_id, reason)
                return CardOutcome(
                    card_id=card_id, status="blocked", column="Blocked", crew=crew.name, reasons=[reason]
                )
            if card.get("is_gated") and gate == "none":
                self._remember_gate(card, crew, role)
                return self._open_gate(card, crew, role)

        # --- scope pre-check (before claiming, so out-of-scope work never
        #     reaches the Running column) ---------------------------------
        scope_reasons = self._scope_reasons(card)
        if scope_reasons:
            self.stats.runs_blocked += 1
            self._block(card_id, "scope check failed: " + "; ".join(scope_reasons))
            return CardOutcome(
                card_id=card_id,
                status="blocked",
                column="Blocked",
                crew=crew.name,
                reasons=scope_reasons,
            )

        # --- claim --------------------------------------------------------
        try:
            self.client.move(
                card_id,
                "Running",
                actor=role or self.agent_name,
                actor_is_agent=True,
                note=f"claimed by {crew.name} crew",
            )
            self.stats.claimed += 1
            self.stats.last_claim_source = source
            if source == "socket":
                self.stats.socket_claims += 1
            else:
                self.stats.poll_claims += 1
        except KanbanError as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            reasons = exc.reasons or [str(exc)]
            self._block(card_id, f"bridge could not claim the card: {'; '.join(reasons)}")
            return CardOutcome(card_id=card_id, status="blocked", reasons=reasons, crew=crew.name)

        # --- recall: hand the crew what the engagement knows ----------------
        # Retrieval by similarity to *this* card, not merely recent activity.
        # Bounded so it fits a prompt. Absent or broken memory simply means the
        # crew runs with no prior-context section.
        recall = self.recall_context(
            card_id=card_id, target=self._card_target(card), card=card
        )
        if recall is not None:
            card["memory_context"] = recall["prompt_block"]
            # Recorded on the card as well as in the run, so what the crew was
            # shown is reconstructable from the board alone during a replay.
            card["memory_recall"] = {
                "query": recall["query"],
                "backend": recall["backend"],
                "hits": recall["hits"],
                "graph_seed": recall["graph_seed"],
                "errors": recall["errors"],
                "used": [
                    {
                        "id": h.get("id"),
                        "kind": h.get("kind"),
                        "score": h.get("score"),
                        "summary": h.get("summary"),
                    }
                    for h in recall["used"]
                ],
            }
            self.stats.memory_recalls += 1
            self.stats.memory_hits += recall["hits"]
            self.stats.recall_prompt_chars += len(recall.get("prompt_block") or "")
            # The graph seed is what lets recall change the *plan*: it names the
            # entity the engagement's knowledge graph was walked from, which the
            # planner can prefer a target from. Deriving it is a distinct step
            # from retrieving records, so it is counted separately - see
            # BridgeStats.memory_seeds.
            seed = self._memory_seed(recall)
            if seed:
                card["memory_seed"] = seed
                self.stats.memory_seeds += 1

        run = self._run_crew(crew, card, role, gate)
        traces = self._write_traces(card_id, run, agent=role or self.agent_name)
        artifacts = self._write_artifacts(card_id, card, run, crew)
        self.client.set_result(card_id, run.summary, actor=role or self.agent_name)
        self._record_outcome(card, run, role=role, crew=crew)

        # --- remediation: turn findings into scoped sub-cards --------------
        # Deterministic and model-free on purpose: the crew's *run* falls back to
        # the deterministic adapter when no model is reachable, and the spawn must
        # not be a second thing that breaks in the same conditions.
        self._spawn_remediation(card, run)

        # --- release -------------------------------------------------------
        status = run.status
        if status in ("ok", "partial"):
            try:
                self.client.move(
                    card_id,
                    "Review",
                    actor=role or self.agent_name,
                    actor_is_agent=True,
                    note=f"crew '{crew.name}' finished ({status})",
                )
                self.stats.released += 1
                if status == "ok":
                    self.stats.runs_ok += 1
                else:
                    self.stats.runs_partial += 1
                column = "Review"
            except KanbanError as exc:
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                self._block(card_id, f"could not release to Review: {'; '.join(exc.reasons or [str(exc)])}")
                column = "Blocked"
                status = "blocked"
                self.stats.runs_blocked += 1
        else:
            self.stats.runs_blocked += 1
            self._block(card_id, "; ".join(run.errors) or f"crew finished with status {status}")
            column = "Blocked"

        return CardOutcome(
            card_id=card_id,
            status=status if column == "Review" else "blocked",
            column=column,
            crew=crew.name,
            summary=run.summary,
            traces=traces,
            artifacts=artifacts,
            reasons=list(run.errors),
            run=run.as_dict(),
        )

    # -- L6 memory --------------------------------------------------------
    def recall_context(
        self,
        *,
        card_id: Optional[str] = None,
        target: Optional[str] = None,
        card: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Retrieve the memory bundle for a card, or ``None`` when memory is off.

        This is the read the crew's run context is built from. Memory is an
        enhancement, never a dependency: a failure here returns ``None`` so the
        crew runs without prior context rather than not at all - and the failure
        is counted, so a broken store is visible on ``/health`` instead of
        looking like an empty engagement.
        """
        if getattr(self, "memory", None) is None:
            return None
        try:
            from .memory import recall_for_card

            return recall_for_card(
                self.memory,
                self.memory_engagement,
                card_id=card_id,
                target=target,
                card=card,
            )
        except Exception as exc:  # noqa: BLE001 - a broken store must not stop work
            stats = getattr(self, "stats", None)
            if stats is not None:
                stats.errors += 1
                stats.memory_failures += 1
                stats.last_error = f"memory recall failed: {exc}"
            return None

    @staticmethod
    def _memory_seed(recall: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The bounded graph seed handed to the planner (Phase 5, item 1).

        Returns the entity the graph was walked from plus a **bounded** list of
        its neighbours. Bounded because this text goes into a prompt: an
        unscoped entity list from a busy engagement would crowd out the card
        itself, which is the failure mode where recall makes the plan *worse*.

        Empty when the engagement has no graph yet, so the planner's memory
        section degrades to the prose block rather than claiming a seed it does
        not have.
        """
        seed = recall.get("graph_seed")
        graph = recall.get("graph") or {}
        entities = [
            str(n.get("name"))
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict) and n.get("name") and str(n.get("name")) != str(seed)
        ]
        if not seed and not entities:
            return None
        return {
            "seed": seed,
            "entities": entities[:12],
            "relations": len(graph.get("edges") or []),
        }

    @staticmethod
    def _card_target(card: dict[str, Any]) -> Optional[str]:
        """Best-effort primary target for a card, for memory lookups."""
        try:
            from tool_frontends.registry import get_registry

            registry = get_registry()
            for binding in card.get("tools") or []:
                spec = registry.get(binding.get("name", ""))
                if spec is None:
                    continue
                found = spec.target_value(binding.get("args") or {})
                if found:
                    return found
        except Exception:  # noqa: BLE001 - a lookup must not stop a run
            pass
        scope = card.get("scope") or {}
        targets = scope.get("targets") or []
        return targets[0] if targets else None

    def _remember_gate(self, card: dict[str, Any], crew: Any, role: Optional[str]) -> None:
        """Record that a human decision was requested - the HITL audit trail."""
        if getattr(self, "memory", None) is None:
            return
        try:
            self.memory.record(
                engagement=self.memory_engagement,
                summary=f"approval requested for '{card.get('title')}' ({crew.name} crew)",
                kind="gate",
                card_id=card.get("id"),
                agent=role or self.agent_name,
                board_id=card.get("board_id"),
                target=self._card_target(card),
                data={"crew": crew.name, "gate": "pending", "tier": self._tier_for_card(card)},
                tags=[crew.name, "approval"],
            )
        except Exception:  # noqa: BLE001
            pass

    def _tier_for_card(self, card: dict[str, Any]) -> Optional[int]:
        tiers = [
            self._tier_lookup(b.get("name", "")) for b in (card.get("tools") or [])
        ]
        known = [t for t in tiers if t is not None]
        return max(known) if known else None

    def _record_outcome(self, card: dict[str, Any], run: Any, *, role: Optional[str], crew: Any) -> None:
        """Write a finished run back into memory.

        Two records, deliberately different in kind: an *episode* (this crew ran
        this card, and this is what came back) and, when the run produced
        durable knowledge, a *fact* keyed so a later run supersedes it rather
        than duplicating it.
        """
        if getattr(self, "memory", None) is None:
            return
        try:
            target = self._card_target(card)
            status = getattr(run, "status", "unknown")
            self.memory.record(
                engagement=self.memory_engagement,
                summary=(
                    f"{crew.name} crew finished '{card.get('title')}' "
                    f"with status {status}"
                ),
                kind="tool_run" if status in ("ok", "partial") else "error",
                detail=getattr(run, "summary", "") or "",
                card_id=card.get("id"),
                agent=role or self.agent_name,
                board_id=card.get("board_id"),
                target=target,
                data={
                    "crew": crew.name,
                    "status": status,
                    "errors": list(getattr(run, "errors", None) or []),
                },
                tags=[crew.name, str(status)],
            )
            for finding in getattr(run, "findings", None) or []:
                statement = finding.get("statement") or finding.get("summary")
                if not statement:
                    continue
                self.memory.assert_fact(
                    engagement=self.memory_engagement,
                    key=finding.get("key") or f"{(card.get('id') or 'card')}:{statement[:48]}",
                    statement=statement,
                    detail=finding.get("detail", ""),
                    confidence=float(finding.get("confidence", 0.6)),
                    source_card=card.get("id"),
                    agent=role or self.agent_name,
                    target=target,
                    tags=[crew.name],
                )
        except Exception as exc:  # noqa: BLE001
            stats = getattr(self, "stats", None)
            if stats is not None:
                stats.errors += 1
                stats.last_error = f"memory write failed: {exc}"

    # -- helpers ----------------------------------------------------------
    def _spawn_remediation(self, card: dict[str, Any], run: Any) -> list[dict[str, Any]]:
        """Open a scoped remediation sub-card for each finding the run produced.

        The narrowing rule is enforced by the board, not here - see
        ``remediation.spawn_remediation``. A refusal is recorded as a skip and
        counted, never raised: a finding the board will not let us remediate is a
        fact to report, not a crash.
        """
        findings = list(getattr(run, "findings", None) or [])
        if not findings:
            return []
        from .remediation import spawn_remediation

        try:
            return spawn_remediation(
                self.client, card, findings, actor=self.agent_name, stats=self.stats
            )
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            self.stats.last_error = f"remediation spawn failed: {exc}"
            return []

    def _run_crew(
        self, crew: CrewDef, card: dict[str, Any], role: Optional[str], gate_state: str
    ) -> Any:
        """Run a crew through the model-driven runner when one is attached.

        The ``approved`` flag comes from the card's *own* approval state, so a
        crew can never claim it was cleared to run intrusive work when the board
        says otherwise. When no model is configured the deterministic adapter
        runs instead - same roles, same tools, same traces.
        """
        approved = gate_state == "approved"
        if self.llm_runner is not None:
            run = self.llm_runner.run(
                crew,
                card,
                role_lookup=get_role,
                scope=card.get("scope"),
                approved=approved,
                spec_lookup=self._spec_lookup,
            )
        else:
            run = self.adapter.run(
                crew,
                card,
                role_lookup=get_role,
                scope=card.get("scope"),
                approved=approved,
            )
        # Carry the recall trail onto the run so it survives into the outcome,
        # the trace and the board - not just the prompt that consumed it.
        if run is not None:
            run.memory = card.get("memory_recall")
        return run

    @staticmethod
    def _spec_lookup(tool_name: str) -> Any:
        from tool_frontends.registry import get_registry

        return get_registry().get(tool_name)

    def _tier_lookup(self, tool_name: str) -> Optional[int]:
        spec = self._spec_lookup(tool_name)
        return spec.tier if spec is not None else None

    def _resolve_crew(self, card: dict[str, Any]) -> CrewDef:
        named = card.get("crew")
        if named:
            crew = get_crew(named)
            if crew is not None:
                return crew
        # fall back to the board's default crew, then to any crew serving it
        try:
            board = self.client.get(f"/api/boards/{card['board_id']}", view=False)
            default = board.get("default_crew")
        except KanbanError:
            default = None
            board = {}
        if default and get_crew(default):
            return get_crew(default)  # type: ignore[return-value]
        fallback = crew_for_board(board.get("kind", "agent"))
        if fallback is not None:
            return fallback
        raise KanbanError(f"no crew could be resolved for card {card.get('card_id')}")

    def _resolve_role(self, card: dict[str, Any], crew: CrewDef) -> Optional[str]:
        assignee = card.get("assignee")
        if assignee and get_role(assignee) and assignee in crew.roles:
            return assignee
        if assignee and get_role(assignee):
            return assignee
        return crew.roles[0] if crew.roles else None

    def _open_gate(
        self,
        card: dict[str, Any],
        crew: CrewDef,
        role: Optional[str],
        *,
        reasons: Optional[list[str]] = None,
    ) -> CardOutcome:
        """Park the card on a human approval gate instead of running it."""
        card_id = card["card_id"]
        top_tool = None
        top_tier = card.get("max_tier", 0)
        for tool in card.get("tools") or []:
            if tool.get("tier", 0) == top_tier:
                top_tool = tool.get("name")
                break
        try:
            self.client.request_approval(
                card_id,
                reason=(
                    f"T{top_tier} work needs an operator gate before '{crew.display_name}' may run"
                    + (f" ({'; '.join(reasons)})" if reasons else "")
                ),
                requested_by=role or self.agent_name,
                tool=top_tool,
                tier=top_tier,
            )
            self.stats.gated += 1
            return CardOutcome(
                card_id=card_id,
                status="gated",
                column=card.get("column"),
                crew=crew.name,
                reasons=reasons or [f"tier T{top_tier} gate opened"],
            )
        except KanbanError as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            return CardOutcome(card_id=card_id, status="failed", reasons=[str(exc)], crew=crew.name)

    def _write_traces(self, card_id: str, run: Any, *, agent: str) -> int:
        """Attach one trace per tool *invocation* to the card.

        A step that attempts two tools produces two traces, and a call the
        guardrails refused is still a real event worth recording - an attempt that
        never happened is invisible to an auditor, which is the opposite of what
        blueprint 04.3 asks for.
        """
        written = 0
        for step in run.steps:
            for call in step.tool_calls:
                for attempt in call.get("attempts") or [call]:
                    try:
                        self.client.add_trace(card_id, self._trace_from(attempt, agent=agent))
                        written += 1
                    except KanbanError:
                        self.stats.errors += 1
        return written

    def _trace_from(self, call: dict[str, Any], *, agent: str) -> dict[str, Any]:
        """Normalise one tool outcome into a card trace."""
        status = call.get("status")
        if status in ("ok", "dry_run"):
            status = "ok"
        elif status not in ("denied", "blocked", "error"):
            status = "error"
        # `stdout` is preferred, but a dry run reports the rendered command in
        # `command`, and the operator needs to see which command was withheld.
        stdout = call.get("stdout") or call.get("command") or ""
        stderr = call.get("stderr") or "; ".join(call.get("reasons") or [])
        return {
            "tool": call.get("tool", "unknown"),
            "tier": self._tier_for(call.get("tool", "")),
            "status": status,
            "duration_ms": call.get("duration_ms", 0) or 0,
            "stdout_tail": stdout[:500],
            "stderr_tail": stderr[:500],
            "dry_run": bool(call.get("dry_run", True)),
            # The audit hash is what lets the board's trace be verified against
            # the tool layer's independent hash chain.
            "audit_hash": call.get("audit_hash"),
            "agent": agent,
        }

    def _write_artifacts(self, card_id: str, card: dict[str, Any], run: Any, crew: CrewDef) -> int:
        """Attach the run's own output as a reproducible artifact.

        The JSON blob is content-addressed (sha256 over the canonical run
        record), so the same run always yields the same digest and an operator
        can prove the report was generated from *this* evidence.
        """
        written = 0
        payload = json.dumps(run.as_dict(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        try:
            self.client.add_artifact(
                card_id,
                {
                    "name": f"{crew.name}-run.json",
                    "kind": "json",
                    "sha256": digest,
                    "bytes": len(payload.encode("utf-8")),
                    "produced_by": crew.name,
                    "summary": run.summary[:400],
                },
            )
            written += 1
        except KanbanError:
            self.stats.errors += 1

        # The recall trail gets its own artifact rather than living only in the
        # prompt that consumed it. What the crew was shown is evidence: an
        # operator asking "why did it do that?" needs to recover the recalled
        # context from the board, after the run, without the process that held it.
        memory = getattr(run, "memory", None)
        if memory:
            recall_payload = json.dumps(memory, sort_keys=True, separators=(",", ":"))
            try:
                self.client.add_artifact(
                    card_id,
                    {
                        "name": f"{crew.name}-recall.json",
                        "kind": "json",
                        "sha256": hashlib.sha256(recall_payload.encode("utf-8")).hexdigest(),
                        "bytes": len(recall_payload.encode("utf-8")),
                        "produced_by": crew.name,
                        "summary": (
                            f"memory recalled for this card: {memory.get('hits', 0)} hit(s) "
                            f"via {memory.get('backend', 'unknown')}; "
                            f"query={(memory.get('query') or '')[:80]!r}"
                        ),
                    },
                )
                written += 1
            except KanbanError:
                self.stats.errors += 1
        return written

    def _tier_for(self, tool_name: str) -> int:
        from tool_frontends.registry import get_registry

        spec = get_registry().get(tool_name)
        return spec.tier if spec else 0

    def _block(self, card_id: str, reason: str) -> None:
        try:
            self.client.block(card_id, reason, actor=self.agent_name)
        except KanbanError:
            self.stats.errors += 1
