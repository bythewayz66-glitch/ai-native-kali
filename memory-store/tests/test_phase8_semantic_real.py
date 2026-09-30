"""The semantic embedder measured against a *real* model endpoint.

Phase 7 proved the harness discriminates by plugging in a hand-built stand-in
(:class:`SynonymEmbedder`) and checking the number moved. That proved the *rig*
works; it did not prove a real embedding model helps, because no model was ever
reachable in CI.

This module closes that gap. It runs the same D5 harness against a live
Ollama-compatible endpoint when one is reachable, and **skips** when it is not -
so the suite stays green on a machine with no model, while a machine that has one
gets the real measurement. The recorded result (see ``docs/VERIFICATION.md``) is
recall@3 semantic 0.25 -> 1.00, a +0.75 delta, from ``nomic-embed-text``.

The skip is deliberate and is not a silent pass: a skipped test reports as
skipped, and the assertion it would have made is stated in the skip reason.
"""
from __future__ import annotations

import os

import pytest

from memory_store.quality import builtin_corpus, measure_with
from memory_store.vector import HashingEmbedder, OllamaEmbedder

#: Where a live endpoint is expected. Overridable so a build host can point the
#: test at its own service.
EMBED_URL = os.environ.get("MEMORY_EMBED_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("MEMORY_EMBED_MODEL", "nomic-embed-text")


def _live_embedder() -> OllamaEmbedder:
    """An embedder pointed at the endpoint, or skip if it does not answer."""
    impl = OllamaEmbedder(model=EMBED_MODEL, base_url=EMBED_URL, timeout=120.0)
    try:
        reachable = impl.available()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no model endpoint at {EMBED_URL}: {exc}")
    if not reachable:
        pytest.skip(
            f"no model endpoint at {EMBED_URL} (would assert semantic recall@3 > 0.25)"
        )
    return impl


class TestRealSemanticEmbedder:
    def test_a_real_model_beats_the_lexical_baseline_on_semantics(self):
        """The claim the whole swap rests on, measured rather than asserted.

        The lexical baseline scores 0.25 on the semantic family (one accidental
        collision out of four). A real embedding model must do strictly better,
        or bundling it is not worth the bytes.
        """
        memories, probes = builtin_corpus()
        hashing = measure_with(HashingEmbedder(), memories, probes, k=3)
        semantic = measure_with(_live_embedder(), memories, probes, k=3)

        assert semantic.by_family("semantic")["recall_at_k"] > hashing.by_family("semantic")["recall_at_k"]
        assert semantic.by_family("semantic")["recall_at_k"] > 0.25

    def test_a_real_model_does_not_cost_lexical_recall(self):
        """An improvement that trades away lexical recall is a trade, not a win."""
        memories, probes = builtin_corpus()
        hashing = measure_with(HashingEmbedder(), memories, probes, k=3)
        semantic = measure_with(_live_embedder(), memories, probes, k=3)
        assert semantic.by_family("lexical")["recall_at_k"] >= hashing.by_family("lexical")["recall_at_k"]

    def test_the_real_backend_reports_itself_as_not_degraded(self):
        """If the endpoint answered, the embedder must not be silently falling
        back to hashing - a degraded backend would make the numbers above a
        measurement of the *fallback*, not the model."""
        impl = _live_embedder()
        impl.encode("probe")
        assert impl.degraded is False
        assert impl.name == f"ollama:{EMBED_MODEL}"

    def test_the_real_backend_returns_a_usable_vector(self):
        impl = _live_embedder()
        vector = impl.encode("SMB file sharing is exposed on the file server")
        assert isinstance(vector, list) and len(vector) > 0
        assert all(isinstance(v, float) for v in vector)
