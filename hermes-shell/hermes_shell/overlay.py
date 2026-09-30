"""The floating overlay widget: one live status surface for the whole system.

Item 3 of the Phase 4 kickoff, second half. The overlay is the small always-on
surface a desktop shell shows above the window stack - board counters, service
health and the things asking for a human.

Two halves, deliberately split
------------------------------
``collect(client)`` does the I/O: it asks Kanban core and each service what is
happening. ``compose(sources)`` is pure and turns those answers into what the
overlay renders. The split is what makes the overlay testable at all - the
interesting behaviour is "what does the operator see when the observability
service is down but the board is busy?", and that question should not require a
running stack to answer.

The rule the severity ordering encodes
--------------------------------------
An overlay that reports ``ok`` while a card is blocked is worse than no overlay,
because it is actively reassuring. So severity is **max**, not mean: any blocked
card, any pending approval, any unreachable service, and the whole overlay goes
red or amber. Averages are how status panels learn to lie.
"""
from __future__ import annotations

from typing import Any, Optional

#: Severity ordering. ``max`` over this ranking is the overlay's overall state.
_SEVERITY = {"ok": 0, "warn": 1, "alert": 2}

#: How a routed alert's severity maps onto the overlay's three states.
#:
#: The Phase 5 router already applies a *floor* of its own - ``info`` never
#: leaves the shell feed - so anything that reaches here was judged worth
#: showing. ``low``/``info`` therefore stay ``ok`` (visible but not alarming),
#: and an **unknown** value maps to ``warn`` rather than ``ok``: a severity the
#: overlay does not recognise is a reason to look, not a reason to relax.
_ROUTED_SEVERITY = {"critical": "alert", "high": "alert", "medium": "warn", "low": "ok", "info": "ok"}


def _route_severity(value: Any) -> str:
    return _ROUTED_SEVERITY.get(str(value or "").lower(), "warn")


def _worst(states: list[str]) -> str:
    if not states:
        return "ok"
    return max(states, key=lambda s: _SEVERITY.get(s, 0))


def health_row(name: str, url: str, ok: bool, detail: str = "") -> dict[str, Any]:
    """One service's contribution to the overlay."""
    return {
        "name": name,
        "url": url,
        "state": "ok" if ok else "alert",
        "detail": detail or ("up" if ok else "unreachable"),
    }


def compose(sources: dict[str, Any]) -> dict[str, Any]:
    """Fold the collected sources into the overlay's render model.

    ``sources`` is what :func:`collect` gathered (or, in tests, anything with the
    same shape): ``totals``, ``health``, ``pending``, an optional
    ``recent_event`` and an optional ``alert_notifications``. Every field is
    optional, because a half-reachable system is
    precisely the case the overlay exists to describe.
    """
    totals = sources.get("totals") or {}
    health: list[dict[str, Any]] = list(sources.get("health") or [])
    pending: list[dict[str, Any]] = list(sources.get("pending") or [])
    recent = sources.get("recent_event")
    errors: list[str] = list(sources.get("errors") or [])
    #: Alerts the Phase 5 router actually delivered to the shell feed. These are
    #: *routed* alerts - deduped, severity-floored and attributed by the
    #: observability service - which is why the overlay shows them beside its own
    #: board-derived ones rather than trying to re-derive them from the board.
    routed: list[dict[str, Any]] = list(sources.get("alert_notifications") or [])

    running = int(totals.get("running") or 0)
    blocked = int(totals.get("blocked") or 0)
    gates = int(totals.get("pending_approvals") or 0)
    cards = int(totals.get("cards") or 0)
    tool_runs = int(totals.get("tool_runs") or 0)
    cost = float(totals.get("cost_usd") or 0.0)

    alerts: list[dict[str, Any]] = []

    for service in health:
        if service.get("state") != "ok":
            alerts.append(
                {
                    "severity": "alert",
                    "kind": "service",
                    "text": f"{service.get('name')} unreachable ({service.get('detail')})",
                }
            )
    if blocked:
        alerts.append(
            {
                "severity": "alert",
                "kind": "blocked",
                "text": f"{blocked} card{'s' if blocked != 1 else ''} blocked",
            }
        )
    if gates:
        alerts.append(
            {
                "severity": "warn",
                "kind": "gate",
                "text": f"{gates} approval{'s' if gates != 1 else ''} waiting on a human",
            }
        )
    for error in errors:
        alerts.append({"severity": "warn", "kind": "collector", "text": str(error)})

    # Routed alerts are the point of the B4 item: an alert the router judged
    # worth delivering must be able to colour the overlay, exactly like a blocked
    # card can. Dropping them here would leave the routing layer's work visible
    # only in the observability page - the one place an operator is *not* looking
    # when they are working the board.
    for note in routed:
        alerts.append(
            {
                "severity": _route_severity(note.get("severity")),
                "kind": f"routed:{note.get('kind') or 'alert'}",
                "text": str(note.get("message") or note.get("text") or "")[:200],
            }
        )

    if cards and running:
        pass  # busy is normal and is not an alert
    elif cards and not running and not blocked and not gates:
        alerts.append(
            {
                "severity": "ok",
                "kind": "idle",
                "text": f"{cards} card{'s' if cards != 1 else ''}, nothing running",
            }
        )

    # ``max``, never a mean: one red alert must be able to colour the overlay.
    state = _worst([a["severity"] for a in alerts if a["severity"] in _SEVERITY]) if alerts else "ok"
    if state == "ok" and not alerts:
        state = "ok"

    service_states = [s.get("state", "ok") for s in health]
    if _worst(service_states) == "alert":
        state = "alert"

    return {
        "state": state,
        "headline": _headline(state, running, blocked, gates, health),
        "counters": {
            "cards": cards,
            "running": running,
            "blocked": blocked,
            "gates": gates,
            "tool_runs": tool_runs,
            "cost_usd": round(cost, 6),
        },
        "services": health,
        "pending": pending,
        "alerts": alerts,
        "alert_notifications": routed,
        "routed_count": len(routed),
        "recent_event": recent,
        "generated_from": sorted(sources.keys()),
    }


def _headline(state: str, running: int, blocked: int, gates: int, health: list[dict[str, Any]]) -> str:
    """One line an operator can read without looking further."""
    down = [s.get("name") for s in health if s.get("state") != "ok"]
    if down:
        return f"{len(down)} service(s) down: {', '.join(str(d) for d in down)}"
    if blocked:
        return f"{blocked} blocked - needs attention"
    if gates:
        return f"{gates} awaiting approval"
    if running:
        return f"{running} running"
    return "idle - all services up"


def collect(client: Any) -> dict[str, Any]:
    """Gather what the overlay needs from Kanban core (and note what it could not).

    Never raises: a collector that fails is exactly when the operator most needs
    the overlay to say so, so failures become ``errors`` and an ``alert``-coloured
    service row rather than an exception the shell has to catch.
    """
    errors: list[str] = []
    result: dict[str, Any] = {"health": list(getattr(client, "services", []) or [])}

    def _safe(name: str, fn: Any, default: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - the overlay reports failures, it does not raise
            errors.append(f"{name}: {exc}")
            return default

    overview = _safe("overview", lambda: client.overview(), {}) or {}
    result["totals"] = overview.get("totals") or {}
    result["boards"] = len(overview.get("boards") or [])
    result["pending"] = _safe("pending", lambda: client.pending_approvals(), []) or []

    events = _safe("events", lambda: client.recent_events(limit=1), []) or []
    result["recent_event"] = events[0] if events else None

    # Phase 5 item F4 / Phase 6 item 3: pick up what the *alert router* delivered.
    # Only attempted when the shell has been pointed at an observability service;
    # a shell bound to the board alone keeps calling exactly the board endpoints
    # it always did. Failures are swallowed rather than recorded as errors,
    # because observability's reachability is already reported by the service
    # probe above - logging it twice would double-count one outage.
    if getattr(client, "obs_url", None):
        try:
            result["alert_notifications"] = client.notifications(limit=20) or []
        except Exception:  # noqa: BLE001 - reachability is already reported by the probe
            result["alert_notifications"] = []

    result["errors"] = errors
    return result


class OverlayClient:
    """Thin HTTP client for the overlay, pointed at whatever the shell is bound to.

    Kept minimal on purpose: the overlay needs three reads, and giving it a
    general-purpose API client would invite it to grow a second, divergent view
    of board state.
    """

    def __init__(
        self,
        kanban_url: str,
        services: Optional[list[dict[str, Any]]] = None,
        obs_url: Optional[str] = None,
    ) -> None:
        self.kanban_url = kanban_url.rstrip("/")
        self.services = services or []
        #: Where to ask for routed alerts. ``None`` means "board only" - the
        #: overlay then touches no endpoint it did not touch before Phase 6.
        self.obs_url = obs_url.rstrip("/") if obs_url else None

    def _get(self, path: str, **params: Any) -> Any:
        import json
        import urllib.parse
        import urllib.request

        url = f"{self.kanban_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=3.0) as response:
            return json.loads(response.read().decode("utf-8"))

    def overview(self) -> dict[str, Any]:
        return self._get("/api/overview")

    def pending_approvals(self) -> list[dict[str, Any]]:
        body = self._get("/api/approvals/pending")
        return body.get("approvals") or body.get("pending") or []

    def recent_events(self, limit: int = 1) -> list[dict[str, Any]]:
        return self._get("/api/events", limit=limit).get("events") or []

    def notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        """Alerts the router delivered, read from the observability service.

        The router already deduped them per (kind, card) and dropped anything
        below the severity floor, so the overlay renders what it is given
        instead of re-implementing that policy - two implementations of one
        policy is the drift this repository keeps designing against.
        """
        import json
        import urllib.request

        url = f"{self.obs_url}/alerting/notifications?limit={int(limit)}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=3.0) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body.get("items") or body.get("notifications") or []


def probe_services(services: list[dict[str, Any]], *, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Best-effort reachability probe, one row per configured service."""
    import urllib.request

    rows: list[dict[str, Any]] = []
    for service in services:
        url = str(service.get("url") or "")
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=timeout) as response:
                ok = 200 <= response.status < 300
            rows.append(health_row(str(service.get("name")), url, ok))
        except Exception as exc:  # noqa: BLE001 - unreachable is the answer, not an error
            rows.append(health_row(str(service.get("name")), url, False, f"{type(exc).__name__}"))
    return rows
