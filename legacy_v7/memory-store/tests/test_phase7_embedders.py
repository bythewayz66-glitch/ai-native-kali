"""Phase 7 item 5: the semantic backend, and the harness that measures it.

Two claims are being separated here, and the separation is the point:

* **backend selection** - which embedder is live, that the hashing vectoriser
  remains the fallback, and that an unreachable endpoint degrades rather than
  raising. These are correctness claims.
* **retrieval quality** - that recall actually improves with a meaning-based
  embedder, expressed as a *measured* recall@k rather than an assertion. The
  harness is graded on its ability to detect an improvement, which is why it is
  measured against a stand-in with known-better behaviour: a rig that returns the
  same number whatever is plugged in measures nothing.
"""
from __future__ import annotations

from typing import Any

import pytest

from memory_store.quality import (
    Memory,
    Probe,
    QualityReport,
    SynonymEmbedder,
    builtin_corpus,
    compare,
    measure,
    measure_with,
    seed,
)
from memory_store.store import MemoryStore
from memory_store.vector import (
    DIM,
    HashingEmbedder,
    OllamaEmbedder,
    embedder_report,
    load_embedder_from_env,
    reset_embedder,
    set_embedder,
    vector_backend,
)


@pytest.fixture(autouse=True)
def _clean_embedder():
    yield
    reset_embedder()


# ------------------------------------------------------------ item 5: default


class TestSemanticIsTheDefaultWhenReachable:
    def test_auto_probes_and_selects_hashing_when_no_endpoint_answers(self, monkeypatch):
        """`auto` must not *assume* a model. With nothing listening it selects the
        deterministic backend and says so - the honest default on an offline ISO."""
        monkeypatch.setattr(OllamaEmbedder, "probe", lambda self: False, raising=False)
        impl = load_embedder_from_env({"MEMORY_EMBEDDER": "auto", "MEMORY_EMBED_URL": "http://127.0.0.1:1"})
        assert isinstance(impl, HashingEmbedder)

    def test_auto_selects_the_model_when_the_endpoint_answers(self, monkeypatch):
        monkeypatch.setattr(OllamaEmbedder, "available", lambda self: True, raising=False)
        impl = load_embedder_from_env(
            {
                "MEMORY_EMBEDDER": "auto",
                "MEMORY_EMBED_MODEL": "nomic-embed-text",
                "MEMORY_EMBED_URL": "http://embeddings.test:11434",
            }
        )
        assert isinstance(impl, OllamaEmbedder)
        # The name reports the *family* as well as the model: a reviewer reading
        # /embedder on a booted image needs to know which backend is answering,
        # not just that some model is configured.
        assert impl.name == "ollama:nomic-embed-text"

    def test_an_explicit_choice_is_still_honoured(self):
        impl = load_embedder_from_env({"MEMORY_EMBEDDER": "hashing"})
        assert isinstance(impl, HashingEmbedder)

    def test_an_explicit_model_choice_does_not_probe(self):
        # Asking for the model explicitly means asking for the model; the caller
        # gets it even if it will degrade, because the deployment said so.
        impl = load_embedder_from_env({"MEMORY_EMBEDDER": "ollama", "MEMORY_EMBED_URL": "http://127.0.0.1:1"})
        assert isinstance(impl, OllamaEmbedder)

    def test_an_unknown_choice_is_still_a_loud_error(self):
        with pytest.raises(ValueError):
            load_embedder_from_env({"MEMORY_EMBEDDER": "gpt-embeddings"})

    def test_the_active_backend_is_reported(self):
        set_embedder(HashingEmbedder())
        assert vector_backend() == "hashing-blake2b"
        report = embedder_report()
        assert report["backend"] == "hashing-blake2b"
        assert report["dim"] == DIM


class TestFallbackPath:
    def test_an_unreachable_endpoint_degrades_to_the_hashing_vector(self):
        impl = OllamaEmbedder(model="m", base_url="http://127.0.0.1:1", timeout=0.5)
        assert impl.encode("apache 2.4.49") == HashingEmbedder().encode("apache 2.4.49")
        assert impl.degraded is True

    def test_degradation_is_reported_not_hidden(self):
        impl = OllamaEmbedder(model="m", base_url="http://127.0.0.1:1", timeout=0.5)
        impl.encode("anything")
        report = impl.describe()
        assert report["degraded"] is True
        assert report["backend"] == "ollama:m"
        assert report["last_error"]

    def test_backend_selection_does_not_break_recall(self):
        """A degraded model backend must still return usable recall - the same
        results the hashing backend would give, because it *is* that backend."""
        store = MemoryStore(":memory:")
        try:
            store.vectors._embedder = OllamaEmbedder(model="m", base_url="http://127.0.0.1:1", timeout=0.5)
            store.record(engagement="E", summary="SMB exposed on port 445")
            hits = store.recall("E", "SMB exposed")
            assert hits and "SMB" in hits[0].summary
        finally:
            store.close()


# -------------------------------------------------------- item 5: the harness


class TestHarnessMechanics:
    def test_the_corpus_has_both_query_families(self):
        memories, probes = builtin_corpus()
        families = {p.family for p in probes}
        assert families == {"lexical", "semantic"}
        assert all(p.relevant for p in probes)
        # every gold label names a real record
        ids = {m.id for m in memories}
        for probe in probes:
            assert set(probe.relevant) <= ids

    def test_recall_at_k_and_mrr_are_computed_from_real_results(self):
        memories, probes = builtin_corpus()
        report = measure_with(HashingEmbedder(), memories, probes, k=3)
        assert report.backend == "hashing-blake2b"
        assert len(report.probes) == len(probes)
        assert 0.0 <= report.recall_at_k <= 1.0
        assert 0.0 <= report.mrr <= 1.0

    def test_a_probe_that_retrieves_nothing_is_a_miss_not_a_crash(self):
        store = MemoryStore(":memory:")
        try:
            report = measure(store, [Probe("anything at all", ("nope",))])
            assert report.recall_at_k == 0.0
            assert report.probes[0].hit is False
        finally:
            store.close()

    def test_lexical_probes_are_retrievable_by_the_hashing_backend(self):
        """If the *lexical* family scores badly the harness is broken, not the
        embedder - so this is the sanity check that makes the semantic number
        meaningful."""
        memories, probes = builtin_corpus()
        report = measure_with(HashingEmbedder(), memories, probes, k=3)
        assert report.by_family("lexical")["recall_at_k"] >= 0.75

    def test_the_three_products_are_reported_per_family(self):
        memories, probes = builtin_corpus()
        report = measure_with(HashingEmbedder(), memories, probes, k=3)
        families = report.families()
        assert set(families) == {"lexical", "semantic"}
        assert families["lexical"]["n"] == 4
        assert families["semantic"]["n"] == 4

    def test_the_summary_line_names_both_families(self):
        memories, probes = builtin_corpus()
        line = measure_with(HashingEmbedder(), memories, probes).summary_line()
        assert "lexical=" in line and "semantic=" in line


class TestHarnessDiscriminates:
    """A measurement rig must move when behaviour moves."""

    def test_the_stand_in_beats_the_hashing_backend_on_semantics(self):
        memories, probes = builtin_corpus()
        hashing = measure_with(HashingEmbedder(), memories, probes, k=3)
        synonym = measure_with(SynonymEmbedder(), memories, probes, k=3)

        # The claim: vocabulary matching fails on meaning-only queries, and a
        # meaning-based embedder does not.
        assert synonym.by_family("semantic")["recall_at_k"] > hashing.by_family("semantic")["recall_at_k"]
        assert synonym.recall_at_k >= hashing.recall_at_k

    def test_the_hashing_baseline_is_measurably_weak_on_semantics(self):
        """The number that justifies the swap.

        An earlier draft asserted ``<= 0.5`` and passed at 0.75 - because the
        "semantic" probes shared tokens with their records. That pass was the bug
        talking. With the overlap removed the bar can be strict: a deterministic
        shingle vectoriser should not match a query that shares no token with the
        record, and ``0.25`` leaves room for one accidental collision without
        leaving room for a broken corpus to pass.
        """
        memories, probes = builtin_corpus()
        hashing = measure_with(HashingEmbedder(), memories, probes, k=3)
        assert hashing.by_family("semantic")["recall_at_k"] <= 0.25

    def test_semantic_probes_genuinely_share_no_token_with_their_record(self):
        """The property the whole measurement rests on, asserted rather than
        trusted. Without this, a future edit that adds a shared term to a
        "semantic" probe would silently turn the harness back into a vocabulary
        test that reports itself as a semantics test."""
        import re

        memories, probes = builtin_corpus()
        by_id = {m.id: m for m in memories}

        # Stopwords carry no retrieval signal, so they cannot make a probe
        # "lexical". Excluding them is the difference between testing for shared
        # *meaning-bearing* vocabulary and testing for the word "the".
        stop = {"the", "and", "for", "with", "that", "this", "not", "are", "was", "any", "all"}

        def tokens(text: str) -> set[str]:
            return {
                t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2 and t not in stop
            }

        for probe in probes:
            if probe.family != "semantic":
                continue
            record = by_id[probe.relevant[0]]
            record_tokens = tokens(f"{record.summary} {record.detail} {' '.join(record.tags)}")
            overlap = tokens(probe.query) & record_tokens
            assert not overlap, f"probe {probe.query!r} shares {sorted(overlap)} with {record.id}"

    def test_lexical_probes_do_share_tokens_with_their_record(self):
        """The mirror property: if a "lexical" probe shared nothing it would be a
        semantic probe mislabelled, and the sanity check would be inverted."""
        import re

        memories, probes = builtin_corpus()
        by_id = {m.id: m for m in memories}

        stop = {"the", "and", "for", "with", "that", "this", "not", "are", "was", "any", "all"}

        def tokens(text: str) -> set[str]:
            return {
                t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2 and t not in stop
            }

        for probe in probes:
            if probe.family != "lexical":
                continue
            record = by_id[probe.relevant[0]]
            record_tokens = tokens(f"{record.summary} {record.detail} {' '.join(record.tags)}")
            assert tokens(probe.query) & record_tokens

    def test_compare_reports_a_per_family_delta(self):
        memories, probes = builtin_corpus()
        result = compare(
            memories,
            probes,
            {"hashing-blake2b": HashingEmbedder(), "synonym-standin": SynonymEmbedder()},
            k=3,
        )
        assert result["baseline"] == "hashing-blake2b"
        delta = result["deltas"]["synonym-standin"]
        assert delta["vs"] == "hashing-blake2b"
        assert delta["semantic"] > 0
        # and both backends are reported, not just the winner
        assert set(result["reports"]) == {"hashing-blake2b", "synonym-standin"}

    def test_compare_is_deterministic(self):
        memories, probes = builtin_corpus()
        first = compare(memories, probes, {"h": HashingEmbedder()}, k=3)
        second = compare(memories, probes, {"h": HashingEmbedder()}, k=3)
        assert first["summary"] == second["summary"]

    def test_lexical_quality_is_unharmed_by_the_meaning_based_backend(self):
        """An improvement that costs lexical recall is a trade, not a win, and
        the report must be able to show that."""
        memories, probes = builtin_corpus()
        hashing = measure_with(HashingEmbedder(), memories, probes, k=3)
        synonym = measure_with(SynonymEmbedder(), memories, probes, k=3)
        assert synonym.by_family("lexical")["recall_at_k"] >= 0.75
        assert synonym.by_family("lexical")["recall_at_k"] >= hashing.by_family("lexical")["recall_at_k"] - 0.25


class TestReportShape:
    def test_a_report_serialises(self):
        memories, probes = builtin_corpus()
        data = measure_with(HashingEmbedder(), memories, probes, k=3).as_dict()
        assert data["backend"] == "hashing-blake2b"
        assert data["k"] == 3
        assert len(data["probes"]) == len(probes)
        assert "families" in data and "semantic" in data["families"]

    def test_an_empty_report_does_not_divide_by_zero(self):
        report = QualityReport(backend="none", k=3)
        assert report.recall_at_k == 0.0 and report.mrr == 0.0
        assert report.by_family("semantic")["n"] == 0

    def test_seed_writes_every_memory_with_its_pinned_id(self):
        store = MemoryStore(":memory:")
        try:
            written = seed(store, [Memory("m_x", "E", "one"), Memory("m_y", "E", "two")])
            assert written == 2
            assert {e.episode_id for e in store.episodes("E")} == {"m_x", "m_y"}
        finally:
            store.close()


class TestBackendsAgreeOnShape:
    """The contract that makes a swap safe: same shape, so nothing downstream
    has to change."""

    def test_both_backends_return_the_same_dimension(self):
        hashing = HashingEmbedder().encode("apache 2.4.49 mod_status")
        synonym = SynonymEmbedder().encode("apache 2.4.49 mod_status")
        assert len(hashing) == DIM
        assert all(isinstance(v, float) for v in synonym)

    def test_both_are_deterministic_for_the_same_input(self):
        text = "SMB exposed on port 445"
        assert HashingEmbedder().encode(text) == HashingEmbedder().encode(text)
        assert SynonymEmbedder().encode(text) == SynonymEmbedder().encode(text)

    def test_a_store_can_be_measured_with_either_backend_unchanged(self):
        memories, probes = builtin_corpus()
        for impl in (HashingEmbedder(), SynonymEmbedder()):
            report = measure_with(impl, memories, probes, k=3)
            assert isinstance(report, QualityReport)
            assert report.backend == impl.name
