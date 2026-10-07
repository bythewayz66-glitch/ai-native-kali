"""The Kanban board as the orchestration surface for OS-level agent workflows.

Item 4 of the Dream list: "Hermes Kanban as a central orchestration tool for
OS-level agent workflows - explore and document a concrete integration design,
with a working proof-of-concept if feasible."

The design in one paragraph
---------------------------
The board already orchestrates *tool* work: a card drives a crew, the crew calls
tools through the guardrail engine, and every call lands in a hash-chained audit
log. What it could not do is orchestrate the **desktop** - the windows and
processes the work is actually performed and watched in. This module closes that
loop, and the rule that makes it safe is the same one the tool layer already
follows:

    the board decides *what* should be surfaced; the desktop manager decides
    whether the agent is *allowed* to do it; the audit log records both.

So this is a deliberately thin layer. It reads the desktop state from the Hermes
shell, plans the smallest desktop action that satisfies a card's stated need
(focus the window the card names), applies it through the shell's own
``/api/desktop`` surface - *not* around it - and records the decision in the same
hash-chained log as the tool calls. It has no other capabilities.

Why the action goes through HTTP and not through an import
----------------------------------------------------------
It would be shorter to import ``hermes_shell.agent_desktop`` and call it. That
would also be wrong: the desktop manager's authorisation (protected windows, the
process allow-list) and its audit trail live behind the shell service, and a
second in-process path would be a second place those rules could be got wrong -
or skipped entirely by whatever imported it. One enforcement point, reached one
way. The cost is a network hop; the benefit is that the desktop cannot be driven
by a caller that never met the shell's policy.

What "orchestration" means here, concretely
-------------------------------------------
* **A card can ask for a window.** ``metadata.surface_window`` (a window id, an
  app name, or a title fragment) says "this work is watched in that window".
* **Entering work surfaces it.** When the card runs, the window is brought
  forward so the operator sees the thing the card is talking about - and a card
  that names a window nobody opened is *reported*, not silently ignored.
* **The decision is auditable.** ``surface_window`` resolution and its outcome
  go into the tool audit chain, so "why did the desktop change" is answerable in
  the same place as "why did this command run".
* **The board never invents a window.** If nothing matches, the plan is a
  no-op with a reason. Opening a window to satisfy a card would let a card name
  an app and have it launched - an execution path the tool tiers do not cover.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

#: Default shell endpoint. 8085 is the Hermes shell in the service map.
DEFAULT_SHELL_URL = "http://127.0.0.1:8085"


class DesktopUnreachable(RuntimeError):
    """The shell could not be reached, or refused the request at the transport layer.

    Distinct from a *policy* refusal, which comes back as ``{"ok": False}`` with
    HTTP 200. The distinction is load-bearing: an unreachable shell means the
    board should carry on without the desktop (the desktop is an aid, not a
    dependency), while a refusal is a decision worth surfacing to the operator.
    """


class DesktopClient:
    """A minimal client for the shell's agent desktop surface.

    Uses ``urllib`` rather than a third-party client for the same reason the
    shell does: this runs on a live image where the only guaranteed HTTP stack is
    the standard library, and an orchestration hop that can fail because a Python
    package moved is a hop that will.
    """

    def __init__(self, base_url: str = DEFAULT_SHELL_URL, *, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise DesktopUnreachable(f"hermes-shell {exc.code} on {path}: {detail[:200]}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DesktopUnreachable(f"hermes-shell unreachable at {self.base_url}: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw  # the agent-view route is plain text

    def state(self) -> dict[str, Any]:
        """The full desktop model: windows, focus, processes, protected set."""
        model = self._request("GET", "/api/desktop")
        return model if isinstance(model, dict) else {}

    def agent_view(self) -> str:
        """The compact text view to carry in a crew prompt."""
        view = self._request("GET", "/api/desktop/agent-view")
        return view if isinstance(view, str) else json.dumps(view)

    def apply(self, action: str, **payload: Any) -> dict[str, Any]:
        """One desktop intention. Refusals come back as ``{"ok": False, ...}``."""
        result = self._request("POST", "/api/desktop", {"action": action, **payload})
        return result if isinstance(result, dict) else {"ok": False, "reason": "malformed reply"}

    def reachable(self) -> bool:
        try:
            self.state()
            return True
        except DesktopUnreachable:
            return False


@dataclass
class SurfacePlan:
    """What the board wants the desktop to do for one card, and why."""

    card_id: str
    action: Optional[str] = None  # 'focus' or None
    window_id: Optional[str] = None
    wanted: Optional[str] = None
    matched_by: Optional[str] = None  # 'id' | 'app' | 'title' | None
    reason: str = ""
    candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "action": self.action,
            "window_id": self.window_id,
            "wanted": self.wanted,
            "matched_by": self.matched_by,
            "reason": self.reason,
            "candidates": list(self.candidates),
        }


def _metadata(card: Any) -> dict[str, Any]:
    if isinstance(card, dict):
        meta = card.get("metadata") or {}
        return meta if isinstance(meta, dict) else {}
    meta = getattr(card, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _card_id(card: Any) -> str:
    if isinstance(card, dict):
        return str(card.get("id") or card.get("card_id") or "")
    return str(getattr(card, "id", "") or "")


def plan_surface(card: Any, desktop: dict[str, Any]) -> SurfacePlan:
    """Decide the smallest desktop action that satisfies a card. Pure.

    Resolution order is id -> app -> title fragment, and the order is the policy:

    * an **id** is exact and unambiguous - if the card names one, it gets that
      window or nothing, because falling back would surface a *different* window
      than the one the card asserted;
    * an **app** is what a card can realistically know before the window exists
      (it does not choose the id), and there is at most one window per app under
      the launcher's single-instance rule;
    * a **title** fragment is the loosest match and is only used when nothing
      more specific resolved, because a fragment can match several windows.
    """
    card_id = _card_id(card)
    meta = _metadata(card)
    wanted = meta.get("surface_window") or meta.get("surface_app")
    windows = desktop.get("windows") or []
    titles = [str(w.get("title") or "") for w in windows]

    if not wanted:
        return SurfacePlan(card_id=card_id, reason="card does not ask for a window", candidates=titles)
    wanted = str(wanted)

    target = next((w for w in windows if str(w.get("id")) == wanted), None)
    matched_by = "id"
    if target is None:
        target = next((w for w in windows if str(w.get("app")) == wanted), None)
        matched_by = "app"
    if target is None:
        target = next((w for w in windows if wanted.lower() in str(w.get("title") or "").lower()), None)
        matched_by = "title"
    if target is None:
        return SurfacePlan(
            card_id=card_id,
            wanted=wanted,
            reason=(
                f"no open window matches '{wanted}' - the board does not open windows to "
                "satisfy a card, so this is reported rather than acted on"
            ),
            candidates=titles,
        )

    window_id = str(target.get("id"))
    if desktop.get("focused") == window_id:
        return SurfacePlan(
            card_id=card_id,
            wanted=wanted,
            window_id=window_id,
            matched_by=matched_by,
            reason=f"'{wanted}' is already the focused window",
            candidates=titles,
        )
    return SurfacePlan(
        card_id=card_id,
        action="focus",
        window_id=window_id,
        wanted=wanted,
        matched_by=matched_by,
        reason=f"surface '{wanted}' (matched by {matched_by}) for operator visibility",
        candidates=titles,
    )


def audit_surface(audit: Any, plan: SurfacePlan, outcome: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Record a desktop decision in the hash-chained tool audit log.

    Same log as the tool calls, so a chain verification sees the desktop change
    beside the work it was surfaced for. Recorded as ``dry_run=True``: the entry
    describes an intention and its outcome, and claiming ``dry_run=False`` for
    something that is not a tool execution would misreport what the chain holds.
    """
    if audit is None:
        return None
    try:
        from datetime import datetime, timezone

        return audit.append(
            ts=datetime.now(timezone.utc).isoformat(),
            tool="desktop_surface",
            tier=0,
            status="ok" if outcome.get("ok") else "skipped",
            dry_run=True,
            caller="kanban-orchestrator",
            target=plan.window_id,
            decision=plan.action or "none",
            args={
                "card_id": plan.card_id,
                "wanted": plan.wanted,
                "matched_by": plan.matched_by,
                "reason": plan.reason,
                "candidates": plan.candidates,
            },
            reasons=None if outcome.get("ok") else [str(outcome.get("reason") or plan.reason)],
        )
    except Exception:  # noqa: BLE001 - an audit failure must not lose the plan
        return None


def orchestrate(card: Any, client: DesktopClient, *, audit: Any = None) -> dict[str, Any]:
    """Surface the window a card names, and report what happened.

    An unreachable shell is reported as ``desktop: "unreachable"`` and the card is
    otherwise unaffected. That is deliberate: the desktop is how the operator
    *watches* the work, and a board that refused to advance because a UI was down
    would make observability a hard dependency of the thing it observes.
    """
    plan: Optional[SurfacePlan] = None
    try:
        desktop = client.state()
    except DesktopUnreachable as exc:
        result = {
            "card_id": _card_id(card),
            "desktop": "unreachable",
            "planned": None,
            "outcome": {"ok": False, "reason": str(exc)},
        }
        return result

    plan = plan_surface(card, desktop)
    outcome: dict[str, Any]
    if plan.action:
        try:
            outcome = client.apply(plan.action, window_id=plan.window_id)
        except DesktopUnreachable as exc:
            outcome = {"ok": False, "reason": str(exc)}
    else:
        outcome = {"ok": False, "skipped": True, "reason": plan.reason}
        if plan.window_id and "already the focused" in plan.reason:
            outcome = {"ok": True, "skipped": True, "reason": plan.reason}

    audit_surface(audit, plan, outcome)
    return {
        "card_id": plan.card_id,
        "desktop": "ok",
        "planned": plan.as_dict(),
        "outcome": outcome,
    }


def crew_desktop_context(card: Any, client: DesktopClient) -> str:
    """The desktop block to prepend to a crew prompt for this card.

    This is where item 4 meets item 2: the bridge consults memory before a crew
    run; this supplies the *live* half of the same context injection - what the
    desk the work is visible on actually looks like right now. Returns "" when
    the shell cannot be reached, so a caller can concatenate it unconditionally.
    """
    try:
        view = client.agent_view()
    except DesktopUnreachable:
        return ""
    if not view.strip():
        return ""
    return f"DESKTOP STATE (live, from hermes-shell)\n{view}\n"
