"""Human-in-the-loop gate bound to the Kanban approval state.

Blueprint ref: section 03.5 - "Kanban as the human-in-the-loop control plane -
approval gates as card states".

CrewAI ships ``@human_feedback``, which pauses a task until a human answers.
This module implements the same idea against the *real* control plane instead of
a console prompt: when an agent wants to run a T2+ tool, it opens a pending
approval on the card and then **blocks** until a human decides in the shell, a
reviewer approves over the API, or the wait times out.

The gate is deliberately boring about failure: a timeout is a *rejection*, never
an implicit approval. An agent that cannot get an answer must not proceed to
send exploit traffic.
"""
from __future__ import annotations

import functools
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("agent_runtime.gate")

#: Outcome strings for the gate, used as a small closed vocabulary.
GATE_APPROVED = "approved"
GATE_REJECTED = "rejected"
GATE_TIMEOUT = "timeout"
GATE_ERROR = "error"


@dataclass
class GateDecision:
    """What the human decided (or that nobody decided in time)."""

    outcome: str
    approved: bool = False
    decided_by: Optional[str] = None
    note: str = ""
    approval_id: Optional[str] = None
    waited_ms: int = 0
    polls: int = 0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "approved": self.approved,
            "decided_by": self.decided_by,
            "note": self.note,
            "approval_id": self.approval_id,
            "waited_ms": self.waited_ms,
            "polls": self.polls,
            "reason": self.reason,
        }


class GateTimeout(RuntimeError):
    """Raised by :meth:`HumanFeedbackGate.request` when nobody decided in time."""


class HumanFeedbackGate:
    """Opens approval gates on cards and waits for a human decision.

    ``sleeper`` and ``clock`` are injectable so the tests can prove the blocking
    behaviour without spending real seconds.
    """

    def __init__(
        self,
        client: Any,
        *,
        poll_interval_s: float = 0.5,
        timeout_s: float = 120.0,
        sleeper: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        auto_approve_in_dry_run: bool = True,
    ) -> None:
        self.client = client
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self._sleep = sleeper
        self._clock = clock
        #: Dry-run tool calls touch nothing, so demanding a human click for every
        #: one would train operators to approve without reading. Live calls always
        #: gate - and the runtime's own T2 path passes approved=False until a
        #: human actually decides.
        self.auto_approve_in_dry_run = auto_approve_in_dry_run
        self.opened = 0
        self.approved = 0
        self.rejected = 0
        self.timed_out = 0
        self.errors = 0
        self.last: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------- opening
    def open(
        self,
        card_id: str,
        *,
        reason: str,
        requested_by: str,
        tool: Optional[str] = None,
        tier: Optional[int] = None,
    ) -> dict[str, Any]:
        """Create a pending approval on the card and return it."""
        body = self.client.request_approval(
            card_id, reason=reason, requested_by=requested_by, tool=tool, tier=tier
        )
        self.opened += 1
        approval = (body or {}).get("pending_approval") or {}
        if not approval:
            # Fall back to the last approval on the card when the view differs.
            approvals = (body or {}).get("approvals") or []
            approval = approvals[-1] if approvals else {}
        return approval

    # ------------------------------------------------------------ waiting
    def wait(
        self,
        card_id: str,
        *,
        approval_id: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> GateDecision:
        """Block until the pending approval is decided, or the timeout fires."""
        timeout = self.timeout_s if timeout_s is None else timeout_s
        started = self._clock()
        polls = 0
        while True:
            polls += 1
            try:
                card = self.client.card(card_id)
            except Exception as exc:
                self.errors += 1
                decision = GateDecision(
                    outcome=GATE_ERROR,
                    note=str(exc)[:300],
                    approval_id=approval_id,
                    polls=polls,
                    waited_ms=int((self._clock() - started) * 1000),
                    reason="could not read the card to check the gate",
                )
                self.last = decision.as_dict()
                return decision

            approvals = card.get("approvals") or []
            target = None
            if approval_id:
                target = next((a for a in approvals if a.get("id") == approval_id), None)
            if target is None and approvals:
                # Newest approval wins: a re-opened gate supersedes an old one.
                target = approvals[-1]

            if target is not None and target.get("status") in ("approved", "rejected"):
                decision = GateDecision(
                    outcome=GATE_APPROVED if target["status"] == "approved" else GATE_REJECTED,
                    approved=target["status"] == "approved",
                    decided_by=target.get("decided_by"),
                    note=target.get("note") or "",
                    approval_id=target.get("id"),
                    polls=polls,
                    waited_ms=int((self._clock() - started) * 1000),
                    reason=target.get("reason") or "",
                )
                if decision.approved:
                    self.approved += 1
                else:
                    self.rejected += 1
                self.last = decision.as_dict()
                return decision

            if (self._clock() - started) >= timeout:
                self.timed_out += 1
                decision = GateDecision(
                    outcome=GATE_TIMEOUT,
                    approved=False,
                    approval_id=(target or {}).get("id") or approval_id,
                    polls=polls,
                    waited_ms=int((self._clock() - started) * 1000),
                    reason=f"no human decision within {timeout}s",
                )
                self.last = decision.as_dict()
                return decision

            self._sleep(self.poll_interval_s)

    # ------------------------------------------------------ convenience API
    def request(
        self,
        card_id: str,
        *,
        reason: str,
        requested_by: str,
        tool: Optional[str] = None,
        tier: Optional[int] = None,
        timeout_s: Optional[float] = None,
        dry_run: bool = False,
    ) -> GateDecision:
        """Open a gate and wait for it - the call an agent actually makes."""
        if dry_run and self.auto_approve_in_dry_run:
            decision = GateDecision(
                outcome=GATE_APPROVED,
                approved=True,
                decided_by="policy:dry-run",
                note="dry-run calls touch nothing, so the gate is waived",
                reason=reason,
            )
            self.last = decision.as_dict()
            return decision
        approval = self.open(card_id, reason=reason, requested_by=requested_by, tool=tool, tier=tier)
        return self.wait(card_id, approval_id=approval.get("id"), timeout_s=timeout_s)

    def decide(
        self, card_id: str, approval_id: str, *, approved: bool, decided_by: str, note: str = ""
    ) -> dict[str, Any]:
        """Record a human decision (used by the shell, tests and the API)."""
        return self.client.post(
            f"/api/cards/{card_id}/approvals/{approval_id}/decide",
            {"approved": approved, "decided_by": decided_by, "note": note},
        )

    # --------------------------------------------------- CrewAI-compatible
    def human_feedback(self, *, reason: Optional[str] = None, timeout_s: Optional[float] = None):
        """Decorator mirroring CrewAI's ``@human_feedback`` over the card gate.

        Wrap a function that performs an intrusive step; the wrapped call opens a
        gate first and only runs the body when a human approved.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                card_id = kwargs.pop("card_id", None)
                if card_id is None and args and isinstance(args[0], dict):
                    card_id = args[0].get("card_id")
                if not card_id:
                    raise ValueError("human_feedback requires a card_id to gate against")
                decision = self.request(
                    card_id,
                    reason=reason or f"approval required for {func.__name__}",
                    requested_by=kwargs.pop("requested_by", "agent"),
                    tool=kwargs.pop("tool", None),
                    tier=kwargs.pop("tier", None),
                    timeout_s=timeout_s,
                    dry_run=bool(kwargs.pop("dry_run", False)),
                )
                if not decision.approved:
                    raise GateTimeout(
                        f"gate {decision.outcome} for card {card_id}: {decision.reason or decision.note}"
                    )
                kwargs["gate_decision"] = decision
                return func(*args, **kwargs)

            return wrapper

        return decorator

    def status(self) -> dict[str, Any]:
        return {
            "opened": self.opened,
            "approved": self.approved,
            "rejected": self.rejected,
            "timed_out": self.timed_out,
            "errors": self.errors,
            "poll_interval_s": self.poll_interval_s,
            "timeout_s": self.timeout_s,
            "auto_approve_dry_run": self.auto_approve_in_dry_run,
            "last": self.last,
        }


#: Process-wide default gate (built per-app in the runtime server).
_DEFAULT: Optional[HumanFeedbackGate] = None


def reset_gate(gate: Optional[HumanFeedbackGate] = None) -> None:
    global _DEFAULT
    _DEFAULT = gate


def get_gate() -> Optional[HumanFeedbackGate]:
    return _DEFAULT


def default_gate_config() -> dict[str, Any]:
    """Gate settings from the environment, for ``/health``."""
    return {
        "poll_interval_s": float(os.environ.get("GATE_POLL_INTERVAL", "0.5") or 0.5),
        "timeout_s": float(os.environ.get("GATE_TIMEOUT", "120") or 120),
        "auto_approve_dry_run": os.environ.get("GATE_AUTO_APPROVE_DRY_RUN", "1") == "1",
    }


def gate_status() -> dict[str, Any]:
    """Gate posture for ``/health`` - even before any gate has been opened."""
    config = default_gate_config()
    live = _DEFAULT
    status: dict[str, Any] = {"configured": config, "active": live is not None}
    if live is not None:
        status["counters"] = live.status()
    return status
