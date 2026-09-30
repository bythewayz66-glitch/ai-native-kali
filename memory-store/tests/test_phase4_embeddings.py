"""Phase 4: real embedding backend behind the ``VectorIndex`` interface.

Item 2 of the Phase 4 kickoff. The claim being tested is narrow and checkable:

* a model backend (Ollama-compatible) can be *selected* and reports itself;
* when the endpoint is unreachable it **degrades to the hashing vectoriser
  instead of raising**, and says so - because a memory store that refuses to work
  without a model would make an optional capability a hard dependency;
* the fallback is chosen once and then **latched**, so a dead endpoint does not
  turn every write into a timeout;
* swapping backends is **detectable** (``drift()``) and **repairable**
  (``backfill()``), because a vector from one embedder is geometrically
  meaningless to another and the failure is otherwise silent.

Everything here runs offline. The "model" is an in-process fake that stands in
for the HTTP response shape, so the suite stays hermetic and never needs Ollama.
"""
from __future__ import annotations

import json
import io
import math

import pytest

from memory_store import MemoryStore
from memory_store import vector as vector_mod
from memory_store.vector import (
    DIM,
    HashingEmbedder,
    OllamaEmbedder,
    cosine,
    embedder_report,
    load_embedder_from_env,
    reset_embedder,
    set_embedder,
    vector_backend,
)


# --------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _clean_embedder():
    """Every test starts from env-driven default and leaves it as it found it."""
    reset_embedder()
    yield
    reset_embedder()


class _FakeEmbedder:
    """A deterministic stand-in with a *different geometry* per instance.

    The point of parameterising on ``keys`` is that two instances disagree about
    the vector space - which is exactly what two real backends do. A test that
    reused one embedder for both sides could not catch a cross-backend mistake.
    """

    def __init__(self, name: str, keys: list[str]) -> None:
        self.name = name
        self._keys = keys
        self.dim = len(keys)

    def encode(self, text: str) -> list[float]:
        low = (text or "").lower()
        raw = [1.0 if key in low else 0.0 for key in self._keys]
        norm = math.sqrt(sum(v * v for v in raw))
        if norm == 0.0:
            return [0.0] * self.dim
        return [v / norm for v in raw]


def _fake_response(embedding: list[float]) -> io.BytesIO:
    """A minimal file-like object shaped like ``urlopen``'s return value."""
    body = json.dumps({"embedding": embedding}).encode("utf-8")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _Resp(body)


# ------------------------------------------------------- backend selection
class TestBackendSelection:
    def test_default_is_the_deterministic_hashing_vectoriser(self):
        impl = load_embedder_from_env({})
        assert isinstance(impl, HashingEmbedder)
        assert impl.name == "hashing-blake2b"
        assert vector_backend() == "hashing-blake2b"

    def test_env_selects_the_ollama_endpoint(self):
        impl = load_embedder_from_env(
            {
                "MEMORY_EMBEDDER": "ollama",
                "MEMORY_EMBED_MODEL": "mxbai-embed-large",
                "MEMORY_EMBED_URL": "http://embeddings.internal:1234",
            }
        )
        assert isinstance(impl, OllamaEmbedder)
        # The name must identify the model, not just the vendor: two models
        # produce different vectors and must not share a backend label.
        assert impl.name == "ollama:mxbai-embed-large"
        assert impl.endpoint == "http://embeddings.internal:1234/api/embeddings"

    def test_unknown_backend_is_a_loud_error_not_a_silent_fallback(self):
        """Asking for a model and quietly getting hashing would be a lie.

        The operator would have no way to know their embeddings were lexical, so
        an unrecognised value must fail at configuration time.
        """
        with pytest.raises(ValueError) as exc:
            load_embedder_from_env({"MEMORY_EMBEDDER": "openai"})
        assert "openai" in str(exc.value)

    def test_active_backend_is_reported_by_name(self):
        set_embedder(_FakeEmbedder("fake-model:v1", ["a", "b"]))
        assert vector_backend() == "fake-model:v1"
        report = embedder_report()
        assert report["backend"] == "fake-model:v1"
        assert report["degraded"] is False

    def test_explicit_choice_is_sticky_over_the_environment(self, monkeypatch):
        """Once set explicitly, an ambient env var must not overrule it."""
        set_embedder(_FakeEmbedder("fake-model:v1", ["a"]))
        monkeypatch.setenv("MEMORY_EMBEDDER", "hashing")
        assert vector_backend() == "fake-model:v1"
        reset_embedder()
        assert vector_backend() == "hashing-blake2b"


# ------------------------------------------------------ degradation / latch
class TestDegradation:
    def test_unreachable_endpoint_degrades_instead_of_raising(self, monkeypatch):
        """A dead model endpoint must never stop a card.

        Port 1 is used deliberately: connecting to it fails immediately rather
        than hanging, so the test is fast and the failure is unambiguous.
        """
        impl = OllamaEmbedder(model="m", base_url="http://127.0.0.1:1", timeout=0.5)
        vec = impl.encode("smb share exposed")
        assert impl.degraded is True
        assert impl.last_error  # the reason is recorded, not swallowed
        assert len(vec) == DIM  # a usable vector still comes back
        assert any(vec)
        assert impl.describe()["fallback"] == "hashing-blake2b"

    def test_fallback_vector_matches_the_hashing_embedder_exactly(self):
        """The degraded path must be the *same* vectoriser, not an approximation.

        If it differed, a deployment that degraded mid-run would end up with two
        incompatible vector populations and recall would silently split.
        """
        impl = OllamaEmbedder(model="m", base_url="http://127.0.0.1:1", timeout=0.5)
        assert impl.encode("apache 2.4.49 mod_status") == HashingEmbedder().encode(
            "apache 2.4.49 mod_status"
        )

    def test_failure_is_latched_so_a_dead_endpoint_is_not_retried_per_call(self, monkeypatch):
        """Proves the latch, not just the outcome.

        Retrying a dead endpoint on every record would convert a fast write path
        into a wall of timeouts, so the network must be attempted **once** and
        then not again.
        """
        attempts = {"n": 0}

        def _boom(request, timeout=None):  # noqa: ARG001
            attempts["n"] += 1
            raise OSError("connection refused")

        monkeypatch.setattr(vector_mod.urllib.request, "urlopen", _boom)
        impl = OllamaEmbedder(model="m", base_url="http://127.0.0.1:9")
        impl.encode("first")
        # Reset the counters *after* the initial failure, because that first call
        # is both the one that tries the network and the one that latches.
        impl.calls = 0
        impl.degraded_calls = 0
        for _ in range(4):
            impl.encode("anything")
        assert attempts["n"] == 1, "the dead endpoint must be attempted exactly once"
        assert impl.calls == 4
        assert impl.degraded_calls == 4  # all four served from the fallback

    def test_reset_allows_the_endpoint_to_be_tried_again(self, monkeypatch):
        """Recovery must be possible without restarting the process."""
        state = {"fail": True}

        def _maybe(request, timeout=None):  # noqa: ARG001
            if state["fail"]:
                raise OSError("down")
            return _fake_response([0.1, 0.2, 0.3])

        monkeypatch.setattr(vector_mod.urllib.request, "urlopen", _maybe)
        impl = OllamaEmbedder(model="m", base_url="http://x")
        impl.encode("first")
        assert impl.degraded is True
        state["fail"] = False
        assert impl.available() is True  # clears the latch and probes
        assert impl.degraded is False
        assert impl.dim == 3  # learned the endpoint's real width

    def test_successful_call_returns_the_model_vector(self, monkeypatch):
        monkeypatch.setattr(
            vector_mod.urllib.request,
            "urlopen",
            lambda request, timeout=None: _fake_response([0.5, 0.5, 0.5, 0.5]),
        )
        impl = OllamaEmbedder(model="m", base_url="http://x")
        vec = impl.encode("hello")
        assert vec == [0.5, 0.5, 0.5, 0.5]
        assert impl.degraded is False
        assert impl.dim == 4

    def test_malformed_response_degrades_rather_than_raising(self, monkeypatch):
        """A 200 with the wrong body is still a failure - and must be survivable."""

        class _Bad(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(
            vector_mod.urllib.request,
            "urlopen",
            lambda request, timeout=None: _Bad(json.dumps({"ok": True}).encode()),
        )
        impl = OllamaEmbedder(model="m", base_url="http://x")
        vec = impl.encode("hello")
        assert impl.degraded is True
        assert len(vec) == DIM


# ------------------------------------------------- drift: swap and repair
class TestBackendDriftAndRepair:
    def test_store_reports_the_active_backend_and_no_drift(self):
        store = MemoryStore(":memory:")
        try:
            store.record(engagement="eng-a", summary="SMB exposed on 445")
            store.backfill_derived(engagement="eng-a")
            vec_stats = store.stats()["vector"]
            assert vec_stats["backend"] == "hashing-blake2b"
            assert vec_stats["drift"] == 0
            assert vec_stats["backends"] == {"hashing-blake2b": 1}
        finally:
            store.close()

    def test_swapping_backends_is_detected_and_repair_restores_recall(self):
        """The failure this guards is invisible from the outside.

        After a backend swap, recall can return **nothing** while every row count,
        health check and 200 response still looks perfect. ``drift()`` is what
        makes it visible and ``backfill()`` is what fixes it.
        """
        store = MemoryStore(":memory:")
        try:
            backend_a = _FakeEmbedder("fake-a", ["smb", "apache"])
            backend_b = _FakeEmbedder("fake-b", ["smb", "apache", "port"])
            store.vectors._embedder = backend_a

            store.record(engagement="eng-a", summary="SMB service exposed on 445")
            store.backfill_derived(engagement="eng-a")
            assert store.recall("eng-a", "smb"), "recall works within one backend"

            # Swap to a backend with a different vector space (different width).
            store.vectors._embedder = backend_b
            assert store.vectors.drift() == 1, "the swap must be visible"

            # The query now embeds to a 3-vector while the stored vector is a
            # 2-vector: cosine is undefined, and recall silently finds nothing.
            assert store.recall("eng-a", "smb") == []

            repaired = store.vectors.backfill(store, engagement="eng-a")
            assert repaired == 1
            assert store.vectors.drift() == 0
            assert store.vectors.backend_counts() == {"fake-b": 1}
            assert store.recall("eng-a", "smb"), "repair restores recall"
        finally:
            store.close()

    def test_drift_repair_can_be_switched_off_and_the_drift_stays_visible(self):
        store = MemoryStore(":memory:")
        try:
            store.vectors._embedder = _FakeEmbedder("fake-a", ["smb"])
            store.record(engagement="eng-a", summary="smb share")
            store.backfill_derived(engagement="eng-a")

            store.vectors._embedder = _FakeEmbedder("fake-b", ["smb", "port"])
            store.vectors.backfill(store, engagement="eng-a", reembed_drift=False)
            assert store.vectors.drift() == 1  # kept, and still reported
            assert store.vectors.backend_counts() == {"fake-a": 1}
        finally:
            store.close()

    def test_writes_are_embedded_eagerly_so_recall_works_without_a_backfill(self):
        """Backfill is a repair path, not a required step.

        A crew writes a finding and then wants it findable. If recall only worked
        after an operator ran a backfill, the memory layer would appear to lose
        data - so the write path embeds immediately and ``backfill()`` exists to
        repair drift and fill gaps, not to make recall possible.
        """
        store = MemoryStore(":memory:")
        try:
            store.record(engagement="eng-a", summary="nmap found port 8080 open")
            assert store.vectors.count() == 1, "the write path embeds eagerly"
            assert store.recall("eng-a", "port 8080")
        finally:
            store.close()

    def test_backfill_is_idempotent_and_does_not_re_embed_healthy_records(self):
        """Running it twice must not duplicate or churn vectors."""
        store = MemoryStore(":memory:")
        try:
            store.record(engagement="eng-a", summary="one")
            store.record(engagement="eng-a", summary="two")
            # Already embedded by the write path, so there is nothing to fill.
            assert store.vectors.backfill(store, engagement="eng-a") == 0
            assert store.vectors.backfill(store, engagement="eng-a") == 0
            assert store.vectors.count() == 2
        finally:
            store.close()

    def test_recall_is_consistent_across_repeated_queries_with_one_backend(self):
        """Same backend, same query shape -> same ranking, every time.

        Determinism is the property that lets recall be asserted in a test at
        all; without it every expectation would be a threshold tuned until the
        test passed.
        """
        store = MemoryStore(":memory:")
        try:
            store.record(engagement="eng-a", summary="apache mod_status exposed")
            store.record(engagement="eng-a", summary="smb signing disabled")
            store.record(engagement="eng-a", summary="tls certificate expired")
            store.backfill_derived(engagement="eng-a")
            runs = [
                [h.id for h in store.recall("eng-a", "apache mod_status", top_k=3)]
                for _ in range(3)
            ]
            assert runs[0] == runs[1] == runs[2]
            assert runs[0], "a matching record must be returned"
            # and the top hit really is the apache record
            top = store.recall("eng-a", "apache mod_status", top_k=1)[0]
            assert "apache" in top.summary
        finally:
            store.close()

    def test_recall_requires_an_engagement(self):
        """A new retrieval path is not a new way to cross a scope boundary."""
        store = MemoryStore(":memory:")
        try:
            with pytest.raises(ValueError):
                store.recall("", "anything")
            with pytest.raises(ValueError):
                store.vectors.search("", "anything")
        finally:
            store.close()

    def test_cosine_rejects_mismatched_dimensions_instead_of_lying(self):
        assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0
        assert cosine([], []) == 0.0
        assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
