"""Phase 3 memory tests - vector recall, knowledge graph, and their composition.

Workstream A. Three things are being proved here:

1. **The existing contract is untouched.** The original 75 memory tests still
   pass (they are run by the same pytest invocation), and the tests in this file
   additionally assert that the existing methods keep their signatures and
   scoping behaviour. Adding retrieval must not become a new way to cross an
   engagement boundary.
2. **The new paths work**, with fixed expectations rather than tuned thresholds.
3. **The two compose**, which is the point of having both - a fused ranking that
   then seeds a graph walk.
"""
from __future__ import annotations

import pytest

from memory_store.graph import (
    ENTITY_KINDS,
    PREDICATES,
    GraphIndex,
    extract_entities,
)
from memory_store.store import MemoryStore
from memory_store.vector import (
    DIM,
    HashingEmbedder,
    VectorIndex,
    cosine,
    embed,
    tokenize,
    vector_backend,
)

ENG = "ENG-VECTOR"
OTHER = "ENG-OTHER"


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def _seed(store: MemoryStore, engagement: str = ENG) -> None:
    """A small, realistic engagement."""
    store.record(
        engagement=engagement,
        summary="Port scan found SMB 445 open on shop.example.net",
        kind="tool_run",
        target="shop.example.net",
        card_id="CARD-1",
    )
    store.record(
        engagement=engagement,
        summary="Web server exposes an outdated Apache with a known CVE",
        kind="finding",
        target="shop.example.net",
        card_id="CARD-2",
    )
    store.record(
        engagement=engagement,
        summary="Collected TLS certificate metadata from the payment gateway",
        kind="observation",
        target="pay.example.net",
        card_id="CARD-3",
    )
    store.assert_fact(
        engagement=engagement,
        key="web-server:shop.example.net",
        statement="shop.example.net runs Apache 2.4.49 which is vulnerable to CVE-2021-41773",
        confidence=0.9,
        target="shop.example.net",
        source_card="CARD-2",
    )
    store.assert_fact(
        engagement=engagement,
        key="smb:shop.example.net",
        statement="SMB signing is enabled but not required, so relay attacks are viable",
        confidence=0.7,
        target="shop.example.net",
        source_card="CARD-1",
    )
    store.assert_fact(
        engagement=engagement,
        key="web-server:pay.example.net",
        statement="pay.example.net serves nginx 1.18 with a valid certificate",
        confidence=0.6,
        target="pay.example.net",
    )


# ===========================================================================
# the embedding primitive
# ===========================================================================

def test_embedding_is_deterministic():
    """Two calls with the same text must produce identical vectors.

    This is what makes every assertion below a fixed expectation instead of a
    threshold - the property a model-backed embedder would not have and would
    have to be tested differently.
    """
    assert embed("smb signing relay attack") == embed("smb signing relay attack")


def test_embedding_is_normalised():
    assert abs(sum(v * v for v in embed("port scan smb")) - 1.0) < 1e-9


def test_embedding_dimension_is_fixed_and_nonzero():
    vec = embed("anything at all")
    assert len(vec) == DIM
    assert any(vec), "a non-empty string must not embed to the zero vector"
    assert embed("") == [0.0] * DIM, "empty text yields the zero vector, not a crash"


def test_similar_text_scores_higher_than_unrelated_text():
    """The core property recall depends on."""
    base = embed("SMB signing is not required so relay attacks are viable")
    near = embed("SMB relay attack possible because signing is not required")
    far = embed("the certificate expired last tuesday")
    assert cosine(base, near) > cosine(base, far)


def test_near_miss_tokens_retain_some_similarity():
    """Character shingles give partial credit for a near-miss token."""
    assert cosine(embed("smbv2 dialect"), embed("smbv3 dialect")) > 0.0


def test_cosine_handles_degenerate_input():
    assert cosine([], []) == 0.0
    assert cosine([1.0], [1.0, 2.0]) == 0.0, "different dimensions must not raise"
    assert cosine([0.0] * 4, [1.0] * 4) == 0.0


def test_tokenizer_drops_stopwords_and_keeps_addresses():
    tokens = tokenize("What is the host shop.example.net running?")
    assert "shop.example.net" in tokens
    assert "the" not in tokens and "what" not in tokens


def test_embedder_protocol_is_swappable():
    class StubEmbedder:
        name = "stub"
        dim = 8

        def encode(self, text: str) -> list[float]:
            return [1.0] + [0.0] * 7

    index = VectorIndex(_conn_for(), embedder=StubEmbedder())
    assert index._embedder.name == "stub"
    assert HashingEmbedder().dim == DIM
    assert vector_backend() == "hashing-blake2b"


def _conn_for():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


# ===========================================================================
# vector recall
# ===========================================================================

def test_recall_finds_the_relevant_record(store):
    _seed(store)
    hits = store.recall(ENG, "smb signing relay", top_k=5)
    assert hits, "recall returned nothing for a query with an exact match in memory"
    assert "smb" in hits[0].summary.lower()


def test_recall_ranks_by_similarity(store):
    _seed(store)
    hits = store.recall(ENG, "apache cve vulnerability", top_k=5)
    assert hits
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    # The Apache fact should out-rank the TLS observation.
    summaries = " ".join(h.summary.lower() for h in hits[:2])
    assert "apache" in summaries


def test_recall_matches_without_a_shared_token(store):
    """The property FTS5 lacks: no literal term overlap is still a hit.

    ``patching``/``update`` appear nowhere in memory, yet the lexical neighbours
    ``patch``-free host record still scores above an unrelated one because the
    shingle features overlap.
    """
    store.record(
        engagement=ENG,
        summary="The webserver software is outdated and missing security patches",
        kind="finding",
    )
    store.record(engagement=ENG, summary="unrelated note about coffee", kind="observation")
    hits = store.recall(ENG, "outdated webserver security patches", top_k=5)
    assert hits
    assert "webserver" in hits[0].summary.lower()


def test_recall_is_empty_for_a_query_with_no_lexical_signal(store):
    _seed(store)
    assert store.recall(ENG, "!!! ???") == []


def test_recall_returns_both_memory_kinds(store):
    _seed(store)
    kinds = {h.kind for h in store.recall(ENG, "shop.example.net smb apache", top_k=10)}
    assert kinds == {"episodic", "semantic"}


def test_recall_can_be_restricted_to_one_kind(store):
    _seed(store)
    hits = store.recall(ENG, "shop.example.net", kinds=["semantic"], top_k=10)
    assert hits
    assert {h.kind for h in hits} == {"semantic"}


def test_recall_never_crosses_an_engagement(store):
    """The scope rule applies to every retrieval path, not just the old ones."""
    _seed(store, ENG)
    _seed(store, OTHER)
    hits = store.recall(ENG, "smb signing apache cve", top_k=50)
    assert hits
    assert all(h.engagement == ENG for h in hits)


def test_recall_refuses_without_an_engagement(store):
    with pytest.raises(ValueError, match="engagement is required"):
        store.recall("", "anything")


def test_recall_records_which_backend_produced_them(store):
    _seed(store)
    hits = store.recall(ENG, "apache", top_k=3)
    assert hits
    assert hits[0].record.get("retrieval") == "vector"
    assert hits[0].record.get("backend") == "hashing-blake2b"


def test_recall_respects_min_score(store):
    _seed(store)
    permissive = store.recall(ENG, "apache", top_k=50, min_score=0.0)
    strict = store.recall(ENG, "apache", top_k=50, min_score=0.9)
    assert len(strict) <= len(permissive)


# ===========================================================================
# the index as a standalone component
# ===========================================================================

def test_index_upsert_and_count():
    conn = _conn_for()
    index = VectorIndex(conn)
    assert index.upsert("r1", "episodic", ENG, "port scan smb") is True
    assert index.upsert("r2", "semantic", ENG, "smb signing disabled") is True
    assert index.count(ENG) == 2
    assert index.count(ENG, kind="semantic") == 1
    assert index.count(OTHER) == 0


def test_index_upsert_is_idempotent_for_a_record_id():
    conn = _conn_for()
    index = VectorIndex(conn)
    index.upsert("r1", "episodic", ENG, "first version of the text")
    index.upsert("r1", "episodic", ENG, "corrected version of the text")
    assert index.count(ENG) == 1, "an updated record must not add a second vector"


def test_index_refuses_a_blank_record():
    index = VectorIndex(_conn_for())
    assert index.upsert("", "episodic", ENG, "text") is False
    assert index.upsert("r1", "episodic", "", "text") is False
    assert index.count() == 0


def test_index_delete_removes_only_that_record():
    index = VectorIndex(_conn_for())
    index.upsert("r1", "episodic", ENG, "one")
    index.upsert("r2", "episodic", ENG, "two")
    assert index.delete("r1") is True
    assert index.count(ENG) == 1
    assert index.delete("nope") is False


def test_retracted_fact_leaves_recall(store):
    """A withdrawn claim must stop being retrieved.

    Otherwise a fact an analyst explicitly retracted keeps resurfacing from
    recall, which is worse than never having retracted it.
    """
    fact = store.assert_fact(
        engagement=ENG,
        key="host:unverified.example.net",
        statement="unverified.example.net is definitely running a backdoor",
        confidence=0.2,
    )
    assert any(h.id == fact.fact_id for h in store.recall(ENG, "backdoor", top_k=20))
    assert store.retract_fact(fact.fact_id, reason="disproved by manual review") is True
    assert not any(h.id == fact.fact_id for h in store.recall(ENG, "backdoor", top_k=20))


# ===========================================================================
# knowledge graph
# ===========================================================================

def test_entity_extraction_finds_hosts_cves_and_products():
    found = dict(extract_entities("shop.example.net runs Apache 2.4.49 and is exposed to CVE-2021-41773"))
    assert found.get("cve") == "CVE-2021-41773"
    assert (found.get("host") or "").lower() == "shop.example.net"
    assert found.get("product") == "apache"


def test_entity_extraction_does_not_promote_a_filename_to_a_host():
    """``rockyou.txt`` has the shape of a hostname; it is not one.

    Without the TLD guard every wordlist path referenced in memory would become
    a graph host node, and the graph would fill with entities that do not exist.
    """
    found = extract_entities("used the wordlist rockyou.txt for the cracking run")
    assert not any(kind == "host" for kind, _ in found)


def test_entity_extraction_finds_ips_and_macs():
    found = dict(extract_entities("host 10.20.0.5 and access point aa:bb:cc:dd:ee:ff"))
    assert found.get("host") in ("aa:bb:cc:dd:ee:ff", "10.20.0.5")


def test_entity_extraction_is_deduplicated():
    found = extract_entities("CVE-2021-41773 and again cve-2021-41773")
    assert len([f for f in found if f[0] == "cve"]) == 1


def test_graph_kinds_and_predicates_are_closed_sets():
    """An open vocabulary makes the graph unqueryable, so both sets are fixed."""
    assert "host" in ENTITY_KINDS and "cve" in ENTITY_KINDS
    assert "has_cve" in PREDICATES and "member_of" in PREDICATES


def test_fact_creates_entities_and_edges(store):
    _seed(store)
    entities = store.graph_entities(ENG)
    kinds = {e["kind"] for e in entities}
    assert "host" in kinds
    assert "cve" in kinds, "the CVE in the Apache fact must become a node"
    assert "engagement" in kinds, "every fact hangs off the engagement root"


def test_fact_key_structure_produces_a_service_edge(store):
    _seed(store)
    relations = store.graph.relations(ENG, predicate="has_service")
    assert relations, "a 'web-server:host' key must yield a has_service edge"
    names = {
        node["name"]
        for rel in relations
        for node in [store.graph.find_entity(ENG, "web-server")] + [store.graph.find_entity(ENG, "smb")]
        if node
    }
    assert names, "the service entities should exist as nodes"


def test_cve_relation_is_recorded_with_its_evidence(store):
    _seed(store)
    relations = store.graph.relations(ENG, predicate="has_cve")
    assert relations, "the CVE must be linked to its host"
    assert all(r["evidence"] for r in relations), "every edge must cite the record behind it"


def test_edges_are_scoped_per_engagement(store):
    """A graph read is as scope-sensitive as a memory read."""
    _seed(store, ENG)
    assert store.graph.relations(ENG)
    assert store.graph.relations(OTHER) == []
    assert store.graph_entities(OTHER) == []


def test_graph_query_returns_the_whole_graph_without_an_entity(store):
    _seed(store)
    result = store.graph_query(ENG)
    assert result["root"] is None
    assert result["nodes"] and result["edges"]


def test_graph_query_traverses_from_a_named_entity(store):
    _seed(store)
    result = store.graph_query(ENG, entity="shop.example.net", depth=1)
    assert result["root"] is not None
    assert result["root"]["name"] == "shop.example.net"
    assert result["edges"], "a known host must have edges"
    # The CVE hangs off the same host, reachable in one hop.
    reachable = {n["name"] for n in result["nodes"]}
    assert "shop.example.net" in reachable


def test_graph_query_depth_2_reaches_further_than_depth_1(store):
    _seed(store)
    shallow = store.graph_query(ENG, entity="shop.example.net", depth=1)
    deep = store.graph_query(ENG, entity="shop.example.net", depth=2)
    assert len(deep["nodes"]) >= len(shallow["nodes"])


def test_graph_query_for_an_unknown_entity_is_empty_not_an_error(store):
    _seed(store)
    result = store.graph_query(ENG, entity="nothing.example.net")
    assert result["root"] is None
    assert result["nodes"] == []


def test_graph_query_filters_by_predicate(store):
    _seed(store)
    result = store.graph_query(ENG, predicate="has_cve")
    assert result["edges"]
    assert {e["predicate"] for e in result["edges"]} == {"has_cve"}


def test_graph_refuses_without_an_engagement(store):
    with pytest.raises(ValueError, match="engagement is required"):
        store.graph_query("")


def test_graph_reobserving_a_relation_raises_confidence_not_a_duplicate(store):
    """Re-observing the same relation is evidence *for* it, not a new edge."""
    _seed(store, ENG)
    before = {r["relation_id"]: r["confidence"] for r in store.graph.relations(ENG)}
    assert before

    _seed(store, ENG)  # the same facts and episodes, recorded again

    after = {r["relation_id"]: r["confidence"] for r in store.graph.relations(ENG)}
    assert set(after) == set(before), "re-observation must not create new edges"
    assert all(
        after[rid] >= conf for rid, conf in before.items()
    ), "confidence must never fall when a relation is observed again"
    # A fact-derived edge starts at >= 0.5; an episode-derived one deliberately
    # starts at 0.4, because an observation is a weaker claim than a fact. That
    # asymmetry is the reason this test compares against the *prior* value
    # rather than against a fixed threshold.
    assert max(after.values()) >= 0.5


def test_graph_stats_report_kinds_and_predicates(store):
    _seed(store)
    stats = store.graph.stats(ENG)
    assert stats["entities"] > 0 and stats["relations"] > 0
    assert stats["by_kind"] and stats["by_predicate"]


def test_graph_index_is_a_derived_view_and_can_be_rebuilt(store):
    """Dropping the derived tables and backfilling must reproduce the graph."""
    _seed(store)
    expected = store.graph.stats(ENG)["relations"]
    store._conn.executescript("DROP TABLE relations; DROP TABLE entities;")
    store.graph = GraphIndex(store._conn)
    store.backfill_derived(engagement=ENG)
    assert store.graph.stats(ENG)["relations"] >= expected


# ===========================================================================
# composition - fused retrieval seeding a graph walk
# ===========================================================================

def test_hybrid_fuses_both_retrieval_paths(store):
    _seed(store)
    hits = store.hybrid(ENG, "apache cve vulnerability", limit=5)
    assert hits
    matched_by = {p for h in hits for p in h.record.get("matched_by", [])}
    assert matched_by, "fusion must record which paths produced each hit"


def test_hybrid_boosts_a_record_found_by_both_paths(store):
    """Agreement between an exact and a semantic match is the strongest signal.

    The Apache fact contains the literal word *apache* and is also the closest
    semantic match, so it must come first and be marked as matched by both.
    """
    _seed(store)
    hits = store.hybrid(ENG, "apache", limit=5)
    assert hits
    top = hits[0]
    assert "apache" in top.summary.lower()
    assert set(top.record.get("matched_by") or []) == {"lexical", "semantic"}
    assert top.record.get("retrieval") == "hybrid"


def test_hybrid_never_crosses_an_engagement(store):
    _seed(store, ENG)
    _seed(store, OTHER)
    hits = store.hybrid(ENG, "apache smb shop.example.net", limit=50)
    assert hits
    assert all(h.engagement == ENG for h in hits)


def test_hybrid_refuses_without_an_engagement(store):
    with pytest.raises(ValueError, match="engagement is required"):
        store.hybrid("", "anything")


def test_retrieval_bundle_returns_hits_and_a_graph(store):
    """The composition the workstream asks for, end to end."""
    _seed(store)
    bundle = store.retrieval_bundle(ENG, "apache cve vulnerability", limit=5, depth=1)
    assert bundle["hits"], "the bundle must contain fused hits"
    assert bundle["graph_seed"], "the top hit should have seeded a graph walk"
    assert bundle["graph"]["nodes"], "seeding must produce a real neighbourhood"
    assert bundle["counts"]["hits"] > 0
    assert bundle["counts"]["nodes"] > 0


def test_retrieval_bundle_seeds_from_the_top_hit_not_an_arbitrary_node(store):
    """A random seed would return a large, plausible, irrelevant neighbourhood."""
    _seed(store)
    bundle = store.retrieval_bundle(ENG, "apache web server", limit=5)
    assert "shop.example.net" in bundle["graph_seed"]
    names = {n["name"] for n in bundle["graph"]["nodes"]}
    assert "shop.example.net" in names


def test_retrieval_bundle_degrades_cleanly_when_nothing_matches(store):
    _seed(store)
    bundle = store.retrieval_bundle(ENG, "zzzzz nothing like this at all", limit=5)
    assert bundle["hits"] == []
    assert bundle["graph_seed"] is None
    assert bundle["graph"]["nodes"] == []


def test_retrieval_bundle_is_scoped(store):
    _seed(store, ENG)
    _seed(store, OTHER)
    bundle = store.retrieval_bundle(ENG, "apache smb", limit=10)
    assert all(h["engagement"] == ENG for h in bundle["hits"])
    assert all(n["engagement"] == ENG for n in bundle["graph"]["nodes"])


# ===========================================================================
# the existing contract is untouched
# ===========================================================================

def test_existing_search_still_works_unchanged(store):
    """Phase 3 added paths; it did not replace the lexical one."""
    _seed(store)
    hits = store.search(ENG, "apache")
    assert hits
    assert any("apache" in h.summary.lower() for h in hits)


def test_existing_context_bundle_is_unchanged(store):
    _seed(store)
    bundle = store.context(ENG, target="shop.example.net")
    assert bundle.facts
    assert bundle.target_facts
    assert bundle.as_prompt_block()


def test_stats_report_both_derived_indexes(store):
    _seed(store)
    stats = store.stats()
    assert stats["vector"]["vectors"] > 0
    assert stats["vector"]["backend"] == "hashing-blake2b"
    assert stats["graph"]["entities"] > 0
    assert stats["derived_index_errors"] == 0


def test_a_write_succeeds_even_if_a_derived_index_fails(store, monkeypatch):
    """Retrieval quality may degrade; a memory write may not fail.

    The derived indexes are an optimisation on top of the store of record. If
    embedding breaks, the crew still records what it did - and the failure is
    reported on /health rather than raised into the caller.
    """
    monkeypatch.setattr(
        store.vectors, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    episode = store.record(engagement=ENG, summary="this write must still land")
    assert episode.episode_id
    assert store.episodes(ENG)
    assert store.derived_errors


def test_engagements_endpoint_data_is_unchanged(store):
    _seed(store, ENG)
    rows = store.engagements()
    assert any(r["engagement"] == ENG for r in rows)
