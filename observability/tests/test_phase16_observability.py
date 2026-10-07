"""Phase 16, items 5 and 6: cursor persistence and bounded latency.

Two slow leaks on a long-running collector, both fixed:

* the audit cursor lived in the server's loop state, so every restart re-pulled
  the chain from seq 0 - which replays old activity into the bounded buffers and
  can re-fire alerts; and
* every latency ever observed stayed resident so ``p95`` could be sorted out.

The tests pin the behaviour that makes each fix safe: the *numbers* a small stack
reports must be unchanged, and a restart must resume rather than replay.
"""
from __future__ import annotations

import json

import pytest

from observability.collector import Collector
from observability.metrics import (
    EXACT_SAMPLE_CAP,
    HISTOGRAM_BUCKETS_MS,
    Metrics,
)


def _observe(m: Metrics, values: list[int]) -> None:
    for value in values:
        m.observe_trace({"tool": "t", "duration_ms": value, "status": "ok"})


# ---------------------------------------------------------------------------
# item 6: bounded latency
# ---------------------------------------------------------------------------
class TestLatencyHistogram:
    def test_small_stack_keeps_exact_percentiles(self):
        """Regression: the numbers a test/panel already pinned must not move."""
        m = Metrics()
        _observe(m, [10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        lat = m.latency()
        assert lat["p50_ms"] == 50
        assert lat["p95_ms"] == 100
        assert lat["max_ms"] == 100
        assert lat["avg_ms"] == 55
        assert lat["exact"] is True

    def test_exact_samples_are_capped(self):
        m = Metrics()
        _observe(m, [7] * (EXACT_SAMPLE_CAP + 500))
        assert len(m.latencies) == EXACT_SAMPLE_CAP
        assert m.latency_count == EXACT_SAMPLE_CAP + 500

    def test_count_and_avg_survive_the_cap(self):
        """The cap must not lose the aggregate, only the per-sample detail."""
        m = Metrics()
        _observe(m, [1000] * (EXACT_SAMPLE_CAP + 1))
        lat = m.latency()
        assert lat["count"] == EXACT_SAMPLE_CAP + 1
        assert lat["avg_ms"] == 1000
        assert lat["max_ms"] == 1000
        assert lat["min_ms"] == 1000

    def test_histogram_percentile_is_conservative(self):
        """Above the cap the p95 is a bucket bound - never below the true value.

        A percentile answers "how slow can this get"; rounding the bucket down
        would make the answer optimistically wrong in the one direction that
        matters.
        """
        m = Metrics()
        _observe(m, [1234] * (EXACT_SAMPLE_CAP + 1))
        lat = m.latency()
        assert lat["exact"] is False
        assert lat["p95_ms"] >= 1234
        # 1234 falls in the (1000, 2000] bucket.
        assert lat["p95_ms"] == 2000

    def test_histogram_final_bucket_is_open_ended(self):
        m = Metrics()
        m.observe_trace({"tool": "t", "duration_ms": 10_000_000, "status": "ok"})
        assert m.latency()["max_ms"] == 10_000_000
        # The overflow bucket reports the observed max, not a fabricated bound.
        assert m.latency()["p95_ms"] == 10_000_000

    def test_histogram_buckets_sum_to_the_count(self):
        m = Metrics()
        _observe(m, [1, 3, 25, 400, 9000])
        assert sum(m.latency_histogram) == m.latency_count == 5

    def test_empty_stack_reports_zeros(self):
        lat = Metrics().latency()
        assert lat["count"] == 0
        assert lat["p95_ms"] == 0
        assert lat["avg_ms"] == 0

    def test_bucket_bounds_are_ascending(self):
        assert list(HISTOGRAM_BUCKETS_MS) == sorted(HISTOGRAM_BUCKETS_MS)

    def test_histogram_payload_labels_its_buckets(self):
        m = Metrics()
        _observe(m, [10, 10])
        rows = m.latency()["histogram"]
        assert len(rows) == len(HISTOGRAM_BUCKETS_MS) + 1
        assert rows[-1]["le_ms"] is None
        populated = [r for r in rows if r["count"]]
        assert populated and populated[0]["le_ms"] == 10 and populated[0]["count"] == 2


# ---------------------------------------------------------------------------
# item 5: cursor persistence
# ---------------------------------------------------------------------------
class _AuditCollector(Collector):
    """Collector whose audit fetch is served from a list, not HTTP."""

    def __init__(self, entries: list[dict], **kw) -> None:
        super().__init__(**kw)
        self._entries = entries
        self.requests: list[dict] = []

    def _fetch(self, url: str, params=None):
        if "/audit" in url:
            self.requests.append(dict(params or {}))
            since = int((params or {}).get("since_seq", 0) or 0)
            return {"entries": [e for e in self._entries if e["seq"] > since]}
        return {}


def _entries(up_to: int) -> list[dict]:
    return [{"seq": i, "tool": "nmap_scan", "decision": "allow"} for i in range(1, up_to + 1)]


class TestCursorPersistence:
    def test_cursor_defaults_to_zero(self, tmp_path):
        col = _AuditCollector([], cursor_path=str(tmp_path / "c.json"))
        assert col.cursor("audit") == 0

    def test_ingest_advances_and_persists_the_cursor(self, tmp_path):
        path = tmp_path / "c.json"
        col = _AuditCollector(_entries(3), cursor_path=str(path))
        added, cursor = col.ingest_tool_audit()
        assert added == 3
        assert cursor == 3
        assert json.loads(path.read_text())["audit"] == 3

    def test_restart_resumes_instead_of_replaying(self, tmp_path):
        """The behaviour the fix exists for: a new process must not re-pull seq 0."""
        path = tmp_path / "c.json"
        first = _AuditCollector(_entries(3), cursor_path=str(path))
        first.ingest_tool_audit()

        second = _AuditCollector(_entries(3), cursor_path=str(path))
        added, cursor = second.ingest_tool_audit()
        assert added == 0, "a restart must not replay rows it already consumed"
        assert cursor == 3
        assert second.requests[0]["since_seq"] == 3

    def test_restart_picks_up_new_rows(self, tmp_path):
        path = tmp_path / "c.json"
        _AuditCollector(_entries(2), cursor_path=str(path)).ingest_tool_audit()
        col = _AuditCollector(_entries(5), cursor_path=str(path))
        added, cursor = col.ingest_tool_audit()
        assert added == 3 and cursor == 5

    def test_corrupt_cursor_file_degrades_to_empty(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{not json")
        col = _AuditCollector(_entries(1), cursor_path=str(path))
        assert col.cursor("audit") == 0
        assert col.ingest_tool_audit()[0] == 1

    def test_explicit_since_seq_still_overrides(self, tmp_path):
        """An operator replaying from a known point must be able to."""
        path = tmp_path / "c.json"
        col = _AuditCollector(_entries(5), cursor_path=str(path))
        col.ingest_tool_audit()
        added, _ = col.ingest_tool_audit(since_seq=0)
        assert added == 5

    def test_status_reports_the_cursor(self, tmp_path):
        col = _AuditCollector(_entries(2), cursor_path=str(tmp_path / "c.json"))
        col.ingest_tool_audit()
        assert col.status()["cursors"]["audit"] == 2

    def test_seen_ids_are_bounded(self, tmp_path):
        col = _AuditCollector([], max_events=10, cursor_path=str(tmp_path / "c.json"))
        assert col._seen_events.maxlen == 40


class TestIngestIdempotence:
    def test_events_are_not_counted_twice(self, tmp_path):
        col = _AuditCollector([], cursor_path=str(tmp_path / "c.json"))

        def fake_fetch(url, params=None):
            return {"events": [{"event_id": "evt_1", "type": "card.moved", "actor": "a"}]}

        col._fetch = fake_fetch
        assert col.ingest_kanban_events() == 1
        assert col.ingest_kanban_events() == 0
        assert col.ingested_events == 1
