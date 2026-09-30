"""Retention policy for the observability stores (blueprint 04.8).

The uncomfortable finding this module is built around
----------------------------------------------------
The obvious way to write a retention policy is "delete anything older than N
days". Applied to this system, that destroys the two artefacts that make it
auditable: the Kanban **event log** and the **tool audit log** are hash-chained -
each row's digest covers its predecessor's. Delete a row from the middle and
every subsequent row's digest stops matching, so ``verify_chain`` returns
"tampered with" for a log that was merely pruned by a retention job.

That is worse than losing the data. It means the integrity check can no longer
distinguish "someone edited the audit trail" from "someone ran the cleanup" -
and the moment a tamper alarm has a benign explanation, it stops being a tamper
alarm.

So retention here is split by store kind, and the split is enforced rather than
documented:

* **Chained stores are never pruned.** A policy that asks for it is *refused with
  a reason* - not silently skipped, because a policy that appears to apply and
  does nothing is how an operator comes to believe data is being removed when it
  is accumulating.
* **Non-chained stores are pruned** by age, by count, or both. These are
  diagnostic buffers (model-call captures, the collector's event/trace views),
  and losing the oldest of them costs nothing evidentiary.

What stays true after a sweep: ``verify_chain`` on the tool audit log still
returns ``ok``, and the sweep reports how many rows it removed from each
non-chained store so the result is auditable in its own right.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, MutableSequence, Optional

#: Stores whose rows are hash-chained and therefore must never be pruned.
#: Kept as a module constant so a new chained store is a one-line addition that
#: the refusal test then covers automatically.
CHAINED_STORES: frozenset[str] = frozenset({"kanban_events", "tool_audit", "card_events"})

#: The retention policy's default: keep the diagnostic buffers for a day, and
#: keep the chains forever.
DEFAULT_POLICIES: dict[str, dict[str, Any]] = {
    "kanban_events": {"max_age_s": None, "pinned": True},
    "tool_audit": {"max_age_s": None, "pinned": True},
    "card_events": {"max_age_s": None, "pinned": True},
    "model_calls": {"max_age_s": 86400.0, "max_rows": 500},
    "obs_events": {"max_age_s": 86400.0, "max_rows": 5000},
    "obs_traces": {"max_age_s": 86400.0, "max_rows": 5000},
}


@dataclass
class StorePolicy:
    """Retention for one store."""

    name: str
    max_age_s: Optional[float] = None
    max_rows: Optional[int] = None
    pinned: bool = False

    @property
    def chained(self) -> bool:
        return self.name in CHAINED_STORES

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_age_s": self.max_age_s,
            "max_rows": self.max_rows,
            "pinned": self.pinned,
            "chained": self.chained,
            "enforceable": not (self.pinned or self.chained),
        }


@dataclass
class StoreVerdict:
    """What the policy says will happen (or is refused) for one store."""

    name: str
    chained: bool
    pinned: bool
    enforceable: bool
    prune_candidates: int = 0
    kept: int = 0
    refused: bool = False
    refusal_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "chained": self.chained,
            "pinned": self.pinned,
            "enforceable": self.enforceable,
            "prune_candidates": self.prune_candidates,
            "kept": self.kept,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
        }


class RetentionPolicy:
    """The declared policy, with its refusals made explicit."""

    def __init__(self, policies: Optional[dict[str, dict[str, Any]]] = None) -> None:
        source = dict(DEFAULT_POLICIES)
        if policies:
            for name, spec in policies.items():
                source[name] = {**source.get(name, {}), **spec}
        self._policies: dict[str, StorePolicy] = {}
        for name, spec in source.items():
            self._policies[name] = StorePolicy(
                name=name,
                max_age_s=spec.get("max_age_s"),
                max_rows=spec.get("max_rows"),
                pinned=bool(spec.get("pinned", False)),
            )

    def policy(self, name: str) -> StorePolicy:
        return self._policies.get(name) or StorePolicy(name=name)

    def stores(self) -> list[dict[str, Any]]:
        return [p.as_dict() for p in self._policies.values()]

    def refusal_for(self, policy: StorePolicy) -> Optional[str]:
        """Why this store cannot be pruned, or ``None`` if it can.

        A chained store is refused **even when pinned is false**, because the
        chaining is a property of the data rather than of the configuration - an
        operator cannot unpin it by editing an env var, and pretending otherwise
        would make the invariant configurable when it is structural.
        """
        if policy.chained:
            return (
                f"'{policy.name}' is hash-chained; pruning it would break "
                "verify_chain() and make real tampering indistinguishable from cleanup"
            )
        if policy.pinned:
            return f"'{policy.name}' is pinned by policy"
        if policy.max_age_s is None and policy.max_rows is None:
            return f"'{policy.name}' has no retention limit set"
        return None


class Store:
    """A prunable store, wrapping whatever the collector actually buffers.

    Two shapes have to be supported and they are genuinely different: the
    collector's buffers are ``deque`` (bounded, and iterable but *not* sliceable),
    while a test or a future SQLite-backed store is a list or a dataclass store
    with its own ``record``/``list`` methods. Rather than make the sweeper know
    about all of them, a ``Store`` is a read/sum-accessor/remove triple and the
    sweeper only ever sees that.

    ``record_count`` is separate from ``items()`` on purpose: reading the full
    contents of a large store just to count it is how a retention planner becomes
    the heaviest thing in the process.
    """

    def __init__(
        self,
        name: str,
        *,
        items: Optional[Callable[[], list[dict[str, Any]]]] = None,
        take_all: Optional[Callable[[], list[dict[str, Any]]]] = None,
        clear: Optional[Callable[[], int]] = None,
        count: Optional[Callable[[], int]] = None,
        ts_key: str = "ts",
        label: Optional[str] = None,
    ) -> None:
        self.name = name
        self._items = items
        self._take_all = take_all
        self._clear = clear
        self._count = count
        self.ts_key = ts_key
        self.label = label or name

    def items(self) -> list[dict[str, Any]]:
        if self._items is not None:
            return list(self._items())
        if self._take_all is not None:
            # Only for stores whose contents we can legitimately drain. Used by
            # the dataclass-backed captures, which expose no partial-remove API.
            return list(self._take_all())
        return []

    def record_count(self) -> int:
        if self._count is not None:
            return int(self._count())
        return len(self.items())

    def replace(self, remaining: list[dict[str, Any]]) -> None:
        """Rewrite the store to ``remaining``.

        A store with a real ``clear`` gets cleared then refilled - the alternative
        for a deque is to rotate it one element at a time, which is slower and
        easier to get wrong.
        """
        if self._clear is not None:
            self._clear()
        if not remaining:
            return
        self.extend(remaining)

    def extend(self, rows: list[dict[str, Any]]) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def for_deque(name: str, buffer: "MutableSequence[dict[str, Any]]", *, ts_key: str = "ts") -> "Store":
        return ListStore(name, buffer, ts_key=ts_key)

    @classmethod
    def for_capture(cls, name: str, capture: Any, *, ts_key: str = "ts") -> "Store":
        """Wrap a ``ModelCallStore``-shaped object (record/list/clear)."""
        return CaptureStore(name, capture, ts_key=ts_key)


class ListStore(Store):
    """A store backed by a mutable sequence (list or deque)."""

    def __init__(self, name: str, buffer: MutableSequence[dict[str, Any]], *, ts_key: str = "ts") -> None:
        self.buffer = buffer
        super().__init__(
            name,
            items=lambda: list(buffer),
            count=lambda: len(buffer),
            clear=lambda: (buffer.clear() or len([])),
            ts_key=ts_key,
            label=f"buffer:{type(buffer).__name__}",
        )

    def extend(self, rows: list[dict[str, Any]]) -> None:
        self.buffer.extend(rows)

    def replace(self, remaining: list[dict[str, Any]]) -> None:
        self.buffer.clear()
        if remaining:
            self.buffer.extend(remaining)


class CaptureStore(Store):
    """A store backed by a ``ModelCallStore``-shaped object.

    ``ModelCallStore`` has no "remove the oldest N" method, so pruning means
    reading what it holds, deciding what survives, and rewriting. That is
    acceptable for a bounded diagnostic ring and would not be for a large store -
    which is why a chained store can never be routed here.
    """

    def __init__(self, name: str, capture: Any, *, ts_key: str = "ts") -> None:
        self.capture = capture
        super().__init__(
            name,
            items=lambda: capture.list(limit=max(1, int(getattr(capture, "max_records", 500) or 500))),
            count=lambda: _capture_count(capture),
            clear=lambda: capture.clear(),
            ts_key=ts_key,
            label="capture:ModelCallStore",
        )

    def replace(self, remaining: list[dict[str, Any]]) -> None:
        self.capture.clear()
        for row in remaining:
            # ``record`` is the only mutation API the capture exposes, so the
            # survivors are re-recorded. The row shape it accepts is the same one
            # ``list()`` returns.
            self.capture.record(
                prompt=row.get("prompt", ""),
                response=row.get("response", ""),
                model=row.get("model", ""),
                backend=row.get("backend", ""),
                card_id=row.get("card_id"),
                crew=row.get("crew"),
                role=row.get("role"),
                ok=bool(row.get("ok", True)),
                error=row.get("error"),
                latency_ms=int(row.get("latency_ms", 0) or 0),
                prompt_tokens=int(row.get("prompt_tokens", 0) or 0),
                completion_tokens=int(row.get("completion_tokens", 0) or 0),
                total_tokens=int(row.get("total_tokens", 0) or 0),
                cost_usd=float(row.get("cost_usd", 0.0) or 0.0),
            )

    def extend(self, rows: list[dict[str, Any]]) -> None:  # pragma: no cover - unused
        self.replace(rows)


def _capture_count(capture: Any) -> int:
    """How many records a capture currently holds.

    ``ModelCallStore`` tracks a lifetime ``captured`` counter and a live
    ``_records`` deque; the live depth is what retention acts on. Read through the
    documented ``counts()`` accessor rather than the private deque, falling back
    to the private attribute only for a duck-typed stand-in in tests.
    """
    counts = getattr(capture, "counts", None)
    if callable(counts):
        try:
            return int(counts().get("buffered", 0))
        except Exception:  # noqa: BLE001 - a weird store must not break a sweep
            pass
    records = getattr(capture, "_records", None)
    return len(records) if records is not None else 0


def prune_store(store: Store, *, policy: StorePolicy, now: float) -> tuple[int, int]:
    """Prune one store in place. Returns ``(removed, kept)``.

    Age first, then count. The order matters: a store limited only by
    ``max_rows`` would otherwise trim by count while keeping arbitrarily old rows,
    which is the opposite of what a retention limit is for.

    Rows with no parseable timestamp are **kept**. A row whose age cannot be
    determined must not be deleted on a guess - that is how a timestamp-format
    change silently becomes a mass deletion.
    """
    rows = store.items()
    before = len(rows)
    if not policy.max_age_s and policy.max_rows is None:
        # Nothing to do, and - importantly - nothing is read back into the store,
        # so a bounded store with no limit is not rewritten on every sweep.
        return 0, store.record_count()

    if policy.max_age_s is not None:
        cutoff = now - float(policy.max_age_s)
        rows = [r for r in rows if not _expired(r, cutoff, store.ts_key)]
        removed = before - len(rows)
    else:
        removed = 0

    if policy.max_rows is not None and len(rows) > int(policy.max_rows):
        excess = len(rows) - int(policy.max_rows)
        rows = rows[excess:]
        removed += excess

    if removed:
        store.replace(rows)
    return removed, len(rows)


def _expired(row: Any, cutoff: float, ts_key: str) -> bool:
    ts = row.get(ts_key) if isinstance(row, dict) else None
    return isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts < cutoff


class _ProbeStore(Store):
    """A throwaway store holding a copy, used to *plan* a prune without doing it.

    ``plan`` has to answer "how many rows would go?" and the only honest way to
    answer that is to run the same decision against the same data. Running it
    against a copy keeps planning free of side effects - a planner that mutates
    is a planner that deletes data when an operator merely asks what it would do.
    """

    def __init__(self, name: str, rows: list[dict[str, Any]], ts_key: str = "ts") -> None:
        self._rows = list(rows)
        super().__init__(
            name,
            items=lambda: list(self._rows),
            count=lambda: len(self._rows),
            clear=lambda: (self._rows.clear() or 0),
            ts_key=ts_key,
            label="probe",
        )

    def extend(self, rows: list[dict[str, Any]]) -> None:
        self._rows.extend(rows)

    def replace(self, remaining: list[dict[str, Any]]) -> None:
        self._rows[:] = list(remaining)


class RetentionSweeper:
    """Applies the policy to the buffers it is given.

    ``stores`` maps a store name to a :class:`Store`. A store whose name is a
    chained store is never touched - even if one is passed in and a policy asks
    for it - which is what makes the invariant hold regardless of how the caller
    wires it up.
    """

    def __init__(
        self,
        policy: Optional[RetentionPolicy] = None,
        *,
        stores: Optional[dict[str, Store]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy or RetentionPolicy()
        self.stores = dict(stores or {})
        self._clock = clock
        self._lock = threading.RLock()
        self.sweeps = 0
        self.removed_total = 0
        self.last_sweep_at: Optional[float] = None
        self.refusals: list[dict[str, Any]] = []

    def add_store(self, store: Store) -> None:
        self.stores[store.name] = store

    def add_buffer(self, name: str, records: MutableSequence[dict[str, Any]], *, ts_key: str = "ts") -> None:
        """Register a plain list/deque as a store (the common case)."""
        self.stores[name] = Store.for_deque(name, records, ts_key=ts_key)

    def _names(self) -> list[str]:
        return sorted(set(self.policy_stores()) | set(self.stores))

    # ---------------------------------------------------------------- plan
    def plan(self, *, now: Optional[float] = None) -> dict[str, Any]:
        """What a sweep would do, without doing it."""
        now = self._clock() if now is None else now
        verdicts: list[StoreVerdict] = []
        for name in self._names():
            policy = self.policy.policy(name)
            store = self.stores.get(name)
            count = store.record_count() if store is not None else 0
            refusal = self.policy.refusal_for(policy)
            if refusal:
                verdicts.append(
                    StoreVerdict(
                        name=name,
                        chained=policy.chained,
                        pinned=policy.pinned,
                        enforceable=False,
                        kept=count,
                        refused=True,
                        refusal_reason=refusal,
                    )
                )
                continue
            # Count what *would* go by measuring against a copy of the decision -
            # planning must never mutate, or asking "what would you delete?"
            # would delete it.
            candidates = 0
            kept = count
            if store is not None:
                probe = _ProbeStore(name, store.items(), store.ts_key)
                removed, kept = prune_store(probe, policy=policy, now=now)
                candidates = removed
            verdicts.append(
                StoreVerdict(
                    name=name,
                    chained=policy.chained,
                    pinned=policy.pinned,
                    enforceable=True,
                    prune_candidates=candidates,
                    kept=kept,
                )
            )
        return {
            "planned_at": now,
            "stores": [v.as_dict() for v in verdicts],
            "prunable": sum(v.prune_candidates for v in verdicts),
            "refused": [v.name for v in verdicts if v.refused],
        }

    def policy_stores(self) -> list[str]:
        return [p["name"] for p in self.policy.stores()]

    # --------------------------------------------------------------- sweep
    def sweep(self, *, now: Optional[float] = None) -> dict[str, Any]:
        """Apply the policy. Returns a per-store report."""
        now = self._clock() if now is None else now
        report: list[dict[str, Any]] = []
        removed_total = 0
        refused: list[dict[str, Any]] = []

        with self._lock:
            for name in self._names():
                policy = self.policy.policy(name)
                refusal = self.policy.refusal_for(policy)
                store = self.stores.get(name)

                if refusal:
                    # Reported as a refusal, not skipped: see the module docstring.
                    entry = {
                        "name": name,
                        "action": "refused",
                        "reason": refusal,
                        "removed": 0,
                        "kept": store.record_count() if store is not None else 0,
                        "chained": policy.chained,
                    }
                    report.append(entry)
                    refused.append({"name": name, "reason": refusal})
                    continue

                if store is None:
                    report.append(
                        {"name": name, "action": "no-store", "removed": 0, "kept": 0, "chained": policy.chained}
                    )
                    continue

                removed, kept = prune_store(store, policy=policy, now=now)
                removed_total += removed
                report.append(
                    {
                        "name": name,
                        "action": "pruned",
                        "removed": removed,
                        "kept": kept,
                        "chained": policy.chained,
                    }
                )

            self.sweeps += 1
            self.removed_total += removed_total
            self.last_sweep_at = now
            self.refusals = refused

        return {
            "swept_at": now,
            "stores": report,
            "removed": removed_total,
            "refused": refused,
            "chained_stores_untouched": [r["name"] for r in report if r["action"] == "refused" and r["chained"]],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "sweeps": self.sweeps,
                "removed_total": self.removed_total,
                "last_sweep_at": self.last_sweep_at,
                "stores": {name: store.record_count() for name, store in self.stores.items()},
                "refusals": self.refusals,
                "policies": self.policy.stores(),
                "chained_stores": sorted(CHAINED_STORES),
            }


def assert_chains_survive(sweeper: RetentionSweeper, verifiers: dict[str, Callable[[], dict[str, Any]]]) -> dict[str, Any]:
    """Sweep, then re-verify every chain. Returns the verdicts.

    This is the check that turns "we do not prune chained logs" from a comment
    into an assertion: it runs the real sweeper against the real stores and then
    asks each chain to verify itself.
    """
    sweep = sweeper.sweep()
    verdicts = {}
    for name, verify in verifiers.items():
        try:
            verdicts[name] = verify()
        except Exception as exc:  # noqa: BLE001 - a verifier that raises is a failure, not a crash
            verdicts[name] = {"ok": False, "reason": f"verifier raised: {exc}"}
    return {"sweep": sweep, "chains": verdicts, "all_ok": all(v.get("ok") for v in verdicts.values())}
