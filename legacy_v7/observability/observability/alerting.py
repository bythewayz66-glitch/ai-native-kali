"""Alert routing: from a derived alert to somewhere a human will see it (04.7).

The gap this closes
-------------------
Phase 1-4 derived alerts correctly (blocked cards, guardrail denials, slow calls,
and - with Phase 5 - model outages) and rendered them on a dashboard. That is
detection, not notification: an alert that only exists on a page nobody has open
is not an alert. This module adds the routing layer, and three decisions in it
are the difference between a useful pager and one that gets muted.

Three decisions
---------------
1. **Dedupe is per (kind, card), with a window.** A model that is down produces a
   transition; a card that is blocked produces one alert per observed event, and
   the collector re-reads the same events every few seconds until its cursor
   advances. Without dedupe the same block would page someone twenty times. The
   window is a real time budget, not "once ever" - a card that is still blocked an
   hour later is worth a reminder, whereas one that re-alerts every poll is not.
2. **Severity routing, with a floor.** ``info`` alerts never leave the shell feed;
   ``high`` ones go to a webhook as well. An operator who can route *everything*
   to a pager will eventually route nothing, because the volume teaches them to
   ignore it.
3. **A sink failure never propagates.** The router is called from inside the
   collector's poll loop. If a webhook raising an exception could escape, a flaky
   endpoint would take down alert collection for *all* sinks - so every delivery
   is isolated, recorded with its outcome, and retried on a bounded budget.

What the delivery log is for
----------------------------
It answers "did anyone actually get told?", which is the question asked *after*
an incident. It records every attempt including the failures and the dedupe
suppressions, because "no alert was sent" and "no alert fired" are different
answers and only the log can tell them apart.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: Severity -> rank. Higher is more urgent.
_SEVERITY_RANK = {"low": 0, "info": 0, "medium": 1, "warn": 1, "high": 2, "alert": 2, "critical": 3}

#: Where each severity is allowed to go. The shell feed is the floor: every alert
#: reaches it, because a status surface an operator already watches should not
#: hide anything. Everywhere else has a minimum severity.
DEFAULT_ROUTES: dict[str, str] = {
    "shell": "low",       # everything
    "webhook": "medium",  # a human is expected to act
}

DEFAULT_DEDUPE_WINDOW_S = 120.0


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(str(severity or "").lower(), 0)


@dataclass
class Delivery:
    """One routing attempt, including the ones that did not happen."""

    ts: float
    alert_key: str
    sink: str
    severity: str
    action: str  # delivered | suppressed | failed | skipped
    status: Optional[int] = None
    attempt: int = 1
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "alert_key": self.alert_key,
            "sink": self.sink,
            "severity": self.severity,
            "action": self.action,
            "status": self.status,
            "attempt": self.attempt,
            "error": self.error,
        }


def alert_key(alert: dict[str, Any]) -> str:
    """Stable identity for an alert, used for dedupe.

    ``kind`` plus the card (or, for service-level alerts with no card, the
    message) - so "the card is blocked" is one identity while the *reason*
    changes underneath it, and a second, genuinely different block on the same
    card is suppressed inside the window. That is the intended trade: the reason
    is in the payload of the alert that was delivered.
    """
    kind = str(alert.get("kind") or "alert")
    scope = alert.get("card_id") or alert.get("model") or alert.get("service") or "system"
    return f"{kind}:{scope}"


class AlertRouter:
    """Routes derived alerts to sinks, with dedupe and a delivery log."""

    def __init__(
        self,
        *,
        routes: Optional[dict[str, str]] = None,
        dedupe_window_s: float = DEFAULT_DEDUPE_WINDOW_S,
        webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        max_retries: int = 2,
        timeout_s: float = 5.0,
        clock: Callable[[], float] = time.time,
        transport: Optional[Callable[[str, dict[str, Any], float], tuple[int, str]]] = None,
        log_size: int = 500,
    ) -> None:
        self.routes = dict(DEFAULT_ROUTES if routes is None else routes)
        self.dedupe_window_s = dedupe_window_s
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        self.max_retries = max(0, int(max_retries))
        self.timeout_s = timeout_s
        self._clock = clock
        self._transport = transport
        self._lock = threading.RLock()
        #: key -> last *delivery* time. Only a real delivery refreshes this, so a
        #: suppressed alert does not extend the window indefinitely.
        self._last_delivered: dict[str, float] = {}
        self.log: deque[Delivery] = deque(maxlen=log_size)
        self.feed: deque[dict[str, Any]] = deque(maxlen=200)
        self.received = 0
        self.delivered = 0
        self.suppressed = 0
        self.failed = 0

    # ------------------------------------------------------------- routing
    def _sinks_for(self, severity: str) -> list[str]:
        rank = severity_rank(severity)
        return [name for name, floor in self.routes.items() if rank >= severity_rank(floor)]

    def route(self, alert: dict[str, Any], *, now: Optional[float] = None) -> dict[str, Any]:
        """Route one alert. Returns a per-sink report; never raises."""
        now = self._clock() if now is None else now
        severity = str(alert.get("severity") or "low")
        key = alert_key(alert)
        sinks = self._sinks_for(severity)

        with self._lock:
            self.received += 1
            last = self._last_delivered.get(key)
            duplicate = last is not None and (now - last) < self.dedupe_window_s

        if duplicate:
            with self._lock:
                self.suppressed += 1
                self.log.append(
                    Delivery(ts=now, alert_key=key, sink="*", severity=severity, action="suppressed")
                )
            return {
                "key": key,
                "severity": severity,
                "suppressed": True,
                "reason": f"identical alert delivered {(now - float(last)):.0f}s ago",
                "sinks": [],
            }

        results: list[dict[str, Any]] = []
        for sink in sinks:
            results.append(self._deliver(sink, key, alert, severity, now))

        # The window is refreshed only when at least one sink accepted (or, for
        # the shell-only tier, when the feed took it), so a total delivery
        # failure does not suppress the retry.
        accepted = any(r["action"] == "delivered" for r in results)
        with self._lock:
            if accepted:
                self._last_delivered[key] = now
                self.feed.append(
                    {
                        "ts": now,
                        "key": key,
                        "severity": severity,
                        "kind": alert.get("kind"),
                        "card_id": alert.get("card_id"),
                        "message": alert.get("message") or alert.get("text") or "",
                    }
                )

        return {"key": key, "severity": severity, "suppressed": False, "sinks": results}

    def route_many(self, alerts: list[dict[str, Any]], *, now: Optional[float] = None) -> dict[str, Any]:
        """Route a batch. Each alert is independent, so one bad row cannot stop
        the rest - the same rule the collector's per-source isolation follows."""
        results = []
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            results.append(self.route(alert, now=now))
        return {
            "routed": len(results),
            "delivered": sum(1 for r in results if not r["suppressed"]),
            "suppressed": sum(1 for r in results if r["suppressed"]),
            "results": results,
        }

    # ------------------------------------------------------------ delivery
    def _deliver(self, sink: str, key: str, alert: dict[str, Any], severity: str, now: float) -> dict[str, Any]:
        if sink == "shell":
            with self._lock:
                self.delivered += 1
                self.log.append(
                    Delivery(ts=now, alert_key=key, sink="shell", severity=severity, action="delivered")
                )
            return {"sink": "shell", "action": "delivered", "status": 200}

        if sink == "webhook":
            if not self.webhook_url:
                # Not a failure: no webhook is configured is a *configuration*
                # state, and recording it as a failure would make every alert
                # look undeliverable on a shell-only deployment.
                with self._lock:
                    self.log.append(
                        Delivery(
                            ts=now, alert_key=key, sink="webhook", severity=severity,
                            action="skipped", error="no webhook configured",
                        )
                    )
                return {"sink": "webhook", "action": "skipped", "reason": "no webhook configured"}
            return self._deliver_webhook(key, alert, severity, now)

        with self._lock:
            self.log.append(
                Delivery(
                    ts=now, alert_key=key, sink=sink, severity=severity,
                    action="skipped", error=f"unknown sink '{sink}'",
                )
            )
        return {"sink": sink, "action": "skipped", "reason": f"unknown sink '{sink}'"}

    def _deliver_webhook(self, key: str, alert: dict[str, Any], severity: str, now: float) -> dict[str, Any]:
        body = {
            "key": key,
            "severity": severity,
            "kind": alert.get("kind"),
            "card_id": alert.get("card_id"),
            "message": alert.get("message") or alert.get("text") or "",
            "ts": alert.get("ts") or now,
            "source": "ai-native-kali/observability",
        }
        last_error: Optional[str] = None
        for attempt in range(1, self.max_retries + 2):
            try:
                status, detail = self._post(body)
            except Exception as exc:  # noqa: BLE001 - a sink must never reach the caller
                last_error = f"{type(exc).__name__}: {exc}"
                status, detail = 0, last_error
            if 200 <= int(status) < 300:
                with self._lock:
                    self.delivered += 1
                    self.log.append(
                        Delivery(
                            ts=now, alert_key=key, sink="webhook", severity=severity,
                            action="delivered", status=int(status), attempt=attempt,
                        )
                    )
                return {"sink": "webhook", "action": "delivered", "status": int(status), "attempt": attempt}
            last_error = detail or f"HTTP {status}"

        with self._lock:
            self.failed += 1
            self.log.append(
                Delivery(
                    ts=now, alert_key=key, sink="webhook", severity=severity, action="failed",
                    status=int(status) if isinstance(status, int) else None,
                    attempt=self.max_retries + 1, error=last_error,
                )
            )
        return {"sink": "webhook", "action": "failed", "error": last_error, "attempts": self.max_retries + 1}

    def _post(self, body: dict[str, Any]) -> tuple[int, str]:
        if self._transport is not None:
            return self._transport(self.webhook_url or "http://webhook.invalid", body, self.timeout_s)
        headers = {"Content-Type": "application/json"}
        if self.webhook_secret:
            headers["X-AI-Kali-Secret"] = self.webhook_secret
        request = urllib.request.Request(
            str(self.webhook_url), data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return response.status, response.read().decode("utf-8", "replace")[:200]
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")[:200]
        except Exception as exc:  # noqa: BLE001
            return 0, f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------- reports
    def deliveries(self, limit: int = 50, *, action: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [d.as_dict() for d in reversed(self.log)]
        if action:
            rows = [r for r in rows if r["action"] == action]
        return rows[:limit]

    def notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        """What the shell's notification feed shows."""
        with self._lock:
            return list(self.feed)[-limit:][::-1]

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "received": self.received,
                "delivered": self.delivered,
                "suppressed": self.suppressed,
                "failed": self.failed,
                "feed_size": len(self.feed),
                "dedupe_window_s": self.dedupe_window_s,
                "routes": dict(self.routes),
                "webhook_configured": bool(self.webhook_url),
                "max_retries": self.max_retries,
                "kinds_seen": sorted({d.alert_key.split(":", 1)[0] for d in self.log}),
            }

    def clear_window(self, *, key: Optional[str] = None) -> dict[str, Any]:
        """Forget the dedupe window (tests, and a manual 're-notify' action)."""
        with self._lock:
            if key is None:
                cleared = len(self._last_delivered)
                self._last_delivered.clear()
            else:
                cleared = 1 if self._last_delivered.pop(key, None) is not None else 0
        return {"cleared": cleared}
