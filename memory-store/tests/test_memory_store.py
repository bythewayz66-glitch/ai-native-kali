"""Tests for the L6 memory store.

The tests are ordered by how badly a failure would hurt:

1. **Scoping** - a leak across engagements is the one bug that cannot be
   tolerated in a security product, so it gets the most coverage.
2. **Episodic append-only integrity** - evidence must not be editable.
3. **Semantic supersede semantics** - a fact must not silently vanish.
4. **Search, context assembly, persistence** - the features.

The HTTP suite at the bottom runs the real FastAPI app, so the "no unscoped
read" rule is verified at the wire, not only in the library.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

from memory_store import MemoryStore
from memory_store.models import ContextBundle
from memory_store.store import _fts_query


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def client(monkeypatch):
    import memory_store.store as store_mod

    monkeypatch.setenv("MEMORY_DB", ":memory:")
    store_mod.reset_store(MemoryStore(":memory:"))
    from memory_store.server import app

    with TestClient(app) as c:
        yield c
    store_mod.reset_store(None)


# ===========================================================================
# 1. Engagement scoping - the property that must never regress
# ===========================================================================
class TestScoping:
    def test_engagement_is_mandatory_on_write(self, store):
        with pytest.raises(ValueError, match="engagement is required"):
            store.record(engagement="", summary="orphan")

    def test_engagement_is_mandatory_when_asserting_a_fact(self, store):
        with pytest.raises(ValueError, match="engagement is required"):
            store.assert_fact(engagement="", key="k", statement="s")

    def test_engagement_is_mandatory_on_context(self, store):
        with pytest.raises(ValueError, match="engagement is required"):
            store.context("")

    def test_episodes_do_not_leak_between_engagements(self, store):
        store.record(engagement="acme", summary="acme host enumerated")
        store.record(engagement="globex", summary="globex host enumerated")

        acme = store.episodes("acme")
        globex = store.episodes("globex")
        assert [e.summary for e in acme] == ["acme host enumerated"]
        assert [e.summary for e in globex] == ["globex host enumerated"]

    def test_facts_do_not_leak_between_engagements(self, store):
        store.assert_fact(engagement="acme", key="db", statement="acme runs postgres")
        store.assert_fact(engagement="globex", key="db", statement="globex runs mysql")

        assert store.facts("acme")[0].statement == "acme runs postgres"
        assert store.facts("globex")[0].statement == "globex runs mysql"

    def test_the_same_fact_key_can_exist_in_two_engagements(self, store):
        """The active-key uniqueness index is per engagement, not global."""
        store.assert_fact(engagement="acme", key="web", statement="nginx 1.24")
        store.assert_fact(engagement="globex", key="web", statement="apache 2.4")
        assert len(store.facts("acme")) == 1
        assert len(store.facts("globex")) == 1

    def test_search_is_confined_to_one_engagement(self, store):
        store.record(engagement="acme", summary="ssh exposed on bastion")
        store.record(engagement="globex", summary="ssh exposed on gateway")

        hits = store.search("acme", "ssh exposed")
        assert hits, "expected a hit in the owning engagement"
        assert {h.engagement for h in hits} == {"acme"}

    def test_search_for_another_engagements_term_returns_nothing(self, store):
        store.record(engagement="globex", summary="kerberoastable service account")
        assert store.search("acme", "kerberoastable") == []

    def test_context_never_includes_another_engagement(self, store):
        store.record(engagement="globex", summary="globex-only secret")
        store.record(engagement="acme", summary="acme work")
        bundle = store.context("acme")
        assert all(e.engagement == "acme" for e in bundle.recent_episodes)
        assert all(f.engagement == "acme" for f in bundle.facts)
        assert "globex-only secret" not in bundle.as_prompt_block()

    def test_engagements_inventory_returns_counts_only(self, store):
        """The one cross-scope view must not become a content leak."""
        store.record(engagement="acme", summary="a secret about acme")
        store.assert_fact(engagement="acme", key="k", statement="a secret fact")
        rows = store.engagements()
        assert rows and rows[0]["engagement"] == "acme"
        assert rows[0]["episodes"] == 1 and rows[0]["facts"] == 1
        blob = json.dumps(rows)
        assert "a secret about acme" not in blob
        assert "a secret fact" not in blob


# ===========================================================================
# 2. Episodic memory - append-only, ordered
# ===========================================================================
class TestEpisodic:
    def test_records_are_returned_newest_first_by_default(self, store):
        for i in range(5):
            store.record(engagement="acme", summary=f"step {i}", episode_id=f"epi_{i}")
        summaries = [e.summary for e in store.episodes("acme")]
        assert summaries == ["step 4", "step 3", "step 2", "step 1", "step 0"]

    def test_oldest_first_is_available_for_replay(self, store):
        for i in range(3):
            store.record(engagement="acme", summary=f"step {i}", episode_id=f"epi_{i}")
        summaries = [e.summary for e in store.episodes("acme", newest_first=False)]
        assert summaries == ["step 0", "step 1", "step 2"]

    def test_episodes_carry_full_provenance(self, store):
        ep = store.record(
            engagement="acme",
            summary="nmap found 22/tcp open on bastion",
            kind="tool_run",
            card_id="crd_1",
            agent="recon-specialist",
            board_id="brd_eng",
            target="bastion.acme.test",
            data={"tool": "nmap_scan", "open_ports": [22, 443]},
            tags=["recon", "ssh"],
        )
        assert ep.card_id == "crd_1"
        assert ep.agent == "recon-specialist"
        assert ep.target == "bastion.acme.test"
        assert ep.data["open_ports"] == [22, 443]
        assert ep.tags == ["recon", "ssh"]

    def test_filter_by_card(self, store):
        store.record(engagement="acme", summary="on card 1", card_id="crd_1")
        store.record(engagement="acme", summary="on card 2", card_id="crd_2")
        rows = store.episodes("acme", card_id="crd_1")
        assert [e.summary for e in rows] == ["on card 1"]

    def test_filter_by_target(self, store):
        store.record(engagement="acme", summary="hit a", target="a.acme.test")
        store.record(engagement="acme", summary="hit b", target="b.acme.test")
        rows = store.episodes("acme", target="a.acme.test")
        assert [e.summary for e in rows] == ["hit a"]

    def test_filter_by_kind(self, store):
        store.record(engagement="acme", summary="saw a thing", kind="observation")
        store.record(engagement="acme", summary="found a hole", kind="finding")
        rows = store.episodes("acme", kind="finding")
        assert [e.summary for e in rows] == ["found a hole"]

    def test_repeated_summaries_are_both_kept(self, store):
        """Episodic memory is a log, not a set - duplicates are meaningful."""
        store.record(engagement="acme", summary="repeated check", episode_id="epi_a")
        store.record(engagement="acme", summary="repeated check", episode_id="epi_b")
        assert store.count_episodes("acme") == 2

    def test_episodes_are_not_mutable_through_the_api(self, store):
        """There is deliberately no update path for an episode."""
        assert not hasattr(store, "update_episode")
        assert not hasattr(store, "edit_episode")
        assert not hasattr(store, "delete_episode")


# ===========================================================================
# 3. Semantic memory - supersede, never overwrite
# ===========================================================================
class TestSemantic:
    def test_asserting_a_key_twice_supersedes_rather_than_overwrites(self, store):
        first = store.assert_fact(
            engagement="acme", key="web-server", statement="nginx 1.18", confidence=0.4
        )
        second = store.assert_fact(
            engagement="acme", key="web-server", statement="nginx 1.24", confidence=0.9
        )

        active = store.facts("acme")
        assert [f.statement for f in active] == ["nginx 1.24"]

        old = store.get_fact(first.fact_id)
        assert old is not None
        assert old.status == "superseded"
        assert old.statement == "nginx 1.18", "the superseded statement must survive"

        assert store.get_fact(second.fact_id).status == "active"

    def test_supersede_history_is_queryable(self, store):
        store.assert_fact(engagement="acme", key="os", statement="debian 11")
        store.assert_fact(engagement="acme", key="os", statement="debian 12")
        everything = store.facts("acme", status="any")
        assert len(everything) == 2
        assert {f.status for f in everything} == {"active", "superseded"}

    def test_superseded_facts_drop_out_of_search(self, store):
        store.assert_fact(engagement="acme", key="os", statement="the server runs debian bullseye")
        store.assert_fact(engagement="acme", key="os", statement="the server runs debian bookworm")

        hits = store.search("acme", "bullseye", kinds=["semantic"])
        assert hits == [], "a superseded fact must not be served as current knowledge"
        assert store.search("acme", "bookworm", kinds=["semantic"])

    def test_retract_keeps_the_row_but_removes_it_from_active(self, store):
        fact = store.assert_fact(engagement="acme", key="weak", statement="port 23 open")
        assert store.retract_fact(fact.fact_id, reason="false positive") is True

        assert store.facts("acme") == []
        kept = store.get_fact(fact.fact_id)
        assert kept.status == "retracted"
        assert "false positive" in kept.detail

    def test_retracting_twice_is_a_no_op(self, store):
        fact = store.assert_fact(engagement="acme", key="k", statement="s")
        assert store.retract_fact(fact.fact_id) is True
        assert store.retract_fact(fact.fact_id) is False

    def test_retracting_an_unknown_id_is_false_not_an_error(self, store):
        assert store.retract_fact("fct_nope") is False

    def test_retracting_frees_the_key_for_re_assertion(self, store):
        fact = store.assert_fact(engagement="acme", key="cred", statement="bad guess")
        store.retract_fact(fact.fact_id)
        again = store.assert_fact(engagement="acme", key="cred", statement="confirmed")
        assert store.get_fact(again.fact_id).status == "active"

    def test_confidence_is_validated(self, store):
        with pytest.raises(ValueError, match="confidence"):
            store.assert_fact(engagement="acme", key="k", statement="s", confidence=1.4)
        with pytest.raises(ValueError, match="confidence"):
            store.assert_fact(engagement="acme", key="k", statement="s", confidence=-0.1)

    def test_facts_are_ordered_by_confidence(self, store):
        store.assert_fact(engagement="acme", key="a", statement="weak", confidence=0.2)
        store.assert_fact(engagement="acme", key="b", statement="strong", confidence=0.95)
        store.assert_fact(engagement="acme", key="c", statement="middling", confidence=0.5)
        assert [f.statement for f in store.facts("acme")] == ["strong", "middling", "weak"]

    def test_filter_facts_by_target(self, store):
        store.assert_fact(engagement="acme", key="a", statement="about a", target="a.test")
        store.assert_fact(engagement="acme", key="b", statement="about b", target="b.test")
        assert [f.statement for f in store.facts("acme", target="a.test")] == ["about a"]


# ===========================================================================
# 4. Search
# ===========================================================================
class TestSearch:
    def test_searches_both_kinds_by_default(self, store):
        store.record(engagement="acme", summary="smb signing not required on fileserver")
        store.assert_fact(
            engagement="acme", key="smb", statement="fileserver has signing disabled"
        )
        hits = store.search("acme", "signing")
        assert {h.kind for h in hits} == {"episodic", "semantic"}

    def test_kind_filter_returns_only_that_memory(self, store):
        store.record(engagement="acme", summary="tls weak cipher observed")
        store.assert_fact(engagement="acme", key="tls", statement="tls weak cipher confirmed")
        only_ep = store.search("acme", "cipher", kinds=["episodic"])
        assert {h.kind for h in only_ep} == {"episodic"}
        only_sem = store.search("acme", "cipher", kinds=["semantic"])
        assert {h.kind for h in only_sem} == {"semantic"}

    def test_search_finds_text_inside_detail(self, store):
        store.record(
            engagement="acme",
            summary="scan complete",
            detail="the scan reported an unauthenticated redis instance",
        )
        assert store.search("acme", "unauthenticated redis")

    def test_search_returns_no_hits_for_unrelated_text(self, store):
        store.record(engagement="acme", summary="dns enumeration")
        assert store.search("acme", "kernel panic") == []

    def test_search_hits_carry_an_id_and_a_summary(self, store):
        rec = store.record(engagement="acme", summary="whois looked up example.test")
        hits = store.search("acme", "whois")
        assert hits[0].id == rec.episode_id
        assert "whois" in hits[0].summary

    @pytest.mark.parametrize(
        "query",
        [
            "plain words",
            'quotes " inside',
            "a AND b OR c",
            "star*",
            "paren( )s",
            "NEAR(foo bar)",
            "semi;colon",
            "-minus +plus",
            "",  # empty query must not raise
        ],
    )
    def test_fts_queries_are_never_parsed_as_operators(self, store, query):
        """Free text must not be able to break - or steer - the FTS5 parser."""
        store.record(engagement="acme", summary="baseline record")
        store.search("acme", query)  # must not raise

    def test_fts_query_builder_strips_operators(self):
        assert _fts_query('"quoted"' ) == '"quoted"'
        assert "AND" not in _fts_query("a AND b")
        assert _fts_query("") == ""
        assert _fts_query("!!!") == ""

    def test_search_works_when_fts_is_unavailable(self, store, monkeypatch):
        """Engagements that predate an FTS-less build must still be searchable."""
        store.record(engagement="acme", summary="fallback search target phrase")
        monkeypatch.setattr(store, "fts", False)
        hits = store.search("acme", "fallback search")
        assert hits and hits[0].summary.startswith("fallback")


# ===========================================================================
# 5. Context bundle - the read path crews use
# ===========================================================================
class TestContext:
    def test_bundle_assembles_all_four_sections(self, store):
        store.record(engagement="acme", summary="prior work on target", target="web.acme.test")
        store.record(engagement="acme", summary="this card already ran", card_id="crd_9")
        store.assert_fact(
            engagement="acme",
            key="web",
            statement="web runs nginx",
            target="web.acme.test",
        )
        store.assert_fact(engagement="acme", key="net", statement="egress proxied")
        bundle = store.context("acme", card_id="crd_9", target="web.acme.test")

        assert bundle.engagement == "acme"
        assert [e.summary for e in bundle.card_episodes] == ["this card already ran"]
        assert [e.summary for e in bundle.target_episodes] == ["prior work on target"]
        assert "web runs nginx" in [f.statement for f in bundle.target_facts]
        assert "egress proxied" in [f.statement for f in bundle.facts]

    def test_card_episodes_are_chronological_for_a_plan(self, store):
        for i in range(3):
            store.record(engagement="acme", summary=f"attempt {i}", card_id="crd_1", episode_id=f"e{i}")
        bundle = store.context("acme", card_id="crd_1")
        assert [e.summary for e in bundle.card_episodes] == ["attempt 0", "attempt 1", "attempt 2"]

    def test_recent_episodes_are_newest_first(self, store):
        for i in range(5):
            store.record(engagement="acme", summary=f"act {i}", episode_id=f"e{i}")
        bundle = store.context("acme", recent=3)
        assert [e.summary for e in bundle.recent_episodes] == ["act 4", "act 3", "act 2"]

    def test_bundle_reports_counts_and_truncation(self, store):
        for i in range(20):
            store.record(engagement="acme", summary=f"act {i}", episode_id=f"e{i}")
        bundle = store.context("acme", recent=5)
        assert bundle.counts["episodes"] == 20
        assert bundle.truncated is True

    def test_empty_engagement_yields_an_empty_but_valid_bundle(self, store):
        bundle = store.context("brand-new")
        assert bundle.recent_episodes == []
        assert bundle.facts == []
        assert bundle.counts["episodes"] == 0
        assert bundle.truncated is False

    def test_prompt_block_is_bounded(self, store):
        for i in range(60):
            store.record(engagement="acme", summary="x" * 200, episode_id=f"e{i}")
        block = store.context("acme", recent=60).as_prompt_block(max_chars=500)
        assert len(block) <= 560, "the prompt block must stay inside its budget"
        assert "truncated" in block

    def test_prompt_block_labels_its_sections(self, store):
        store.assert_fact(engagement="acme", key="k", statement="a durable fact")
        store.record(engagement="acme", summary="an action taken", card_id="crd_1")
        block = store.context("acme", card_id="crd_1").as_prompt_block()
        assert "ENGAGEMENT MEMORY (acme)" in block
        assert "Established facts:" in block
        assert "a durable fact" in block
        assert "Already done on this card:" in block

    def test_bundle_round_trips_through_json_for_the_api(self, store):
        store.record(engagement="acme", summary="something")
        bundle = store.context("acme")
        assert isinstance(bundle, ContextBundle)
        json.dumps(bundle.model_dump())  # must be serialisable


# ===========================================================================
# 6. Persistence
# ===========================================================================
class TestPersistence:
    def test_memory_survives_a_restart(self, tmp_path):
        db = tmp_path / "memory.db"
        first = MemoryStore(db)
        first.record(engagement="acme", summary="recorded before restart")
        first.assert_fact(engagement="acme", key="k", statement="fact before restart")
        first.close()

        second = MemoryStore(db)
        assert [e.summary for e in second.episodes("acme")] == ["recorded before restart"]
        assert [f.statement for f in second.facts("acme")] == ["fact before restart"]
        second.close()

    def test_search_still_works_after_a_restart(self, tmp_path):
        db = tmp_path / "memory.db"
        first = MemoryStore(db)
        first.record(engagement="acme", summary="a searchable phrase survives")
        first.close()

        second = MemoryStore(db)
        assert second.search("acme", "searchable phrase")
        second.close()

    def test_supersede_history_survives_a_restart(self, tmp_path):
        db = tmp_path / "memory.db"
        first = MemoryStore(db)
        first.assert_fact(engagement="acme", key="k", statement="v1")
        first.assert_fact(engagement="acme", key="k", statement="v2")
        first.close()

        second = MemoryStore(db)
        assert [f.statement for f in second.facts("acme")] == ["v2"]
        assert len(second.facts("acme", status="any")) == 2
        second.close()


# ===========================================================================
# 7. Stats
# ===========================================================================
class TestStats:
    def test_stats_count_episodes_and_fact_states(self, store):
        store.record(engagement="acme", summary="one")
        store.record(engagement="acme", summary="two")
        store.assert_fact(engagement="acme", key="a", statement="s1")
        store.assert_fact(engagement="acme", key="a", statement="s2")  # supersedes s1
        store.assert_fact(engagement="acme", key="b", statement="s3")

        stats = store.stats()
        assert stats["episodes"] == 2
        assert stats["facts_active"] == 2
        assert stats["facts_total"] == 3
        assert stats["facts_superseded"] == 1
        assert stats["backend"].startswith("sqlite")

    def test_stats_report_the_actual_search_backend(self, store):
        assert store.stats()["fts"] in (True, False)
        assert ("fts5" in store.stats()["backend"]) == store.stats()["fts"]


# ===========================================================================
# 8. HTTP API
# ===========================================================================
class TestApi:
    def test_health_reports_backend_and_volume(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["service"] == "memory-store"
        assert "episodes" in body

    def test_post_and_read_an_episode(self, client):
        created = client.post(
            "/episodes",
            json={"engagement": "acme", "summary": "enumerated shares", "kind": "tool_run"},
        )
        assert created.status_code == 201
        episode_id = created.json()["episode_id"]

        listed = client.get("/episodes", params={"engagement": "acme"}).json()
        assert listed["count"] == 1
        assert listed["episodes"][0]["episode_id"] == episode_id

    def test_post_episode_without_engagement_is_400(self, client):
        r = client.post("/episodes", json={"summary": "no scope"})
        assert r.status_code == 400
        assert "engagement" in json.dumps(r.json())

    def test_post_episode_without_summary_is_400(self, client):
        r = client.post("/episodes", json={"engagement": "acme"})
        assert r.status_code == 400

    def test_get_without_engagement_is_400_not_a_cross_scope_read(self, client):
        for path in ("/episodes", "/facts", "/search?q=x", "/context"):
            r = client.get(path)
            assert r.status_code == 400, f"{path} allowed an unscoped read"

    def test_assert_and_list_facts(self, client):
        r = client.post(
            "/facts",
            json={"engagement": "acme", "key": "web", "statement": "nginx", "confidence": 0.8},
        )
        assert r.status_code == 201
        listed = client.get("/facts", params={"engagement": "acme"}).json()
        assert listed["facts"][0]["statement"] == "nginx"

    def test_fact_without_a_key_is_400(self, client):
        r = client.post("/facts", json={"engagement": "acme", "statement": "s"})
        assert r.status_code == 400

    def test_bad_confidence_is_400_not_500(self, client):
        r = client.post(
            "/facts",
            json={"engagement": "acme", "key": "k", "statement": "s", "confidence": 5},
        )
        assert r.status_code == 400

    def test_retract_endpoint(self, client):
        fid = client.post(
            "/facts", json={"engagement": "acme", "key": "k", "statement": "s"}
        ).json()["fact_id"]
        assert client.post(f"/facts/{fid}/retract", json={"reason": "wrong"}).status_code == 200
        assert client.get("/facts", params={"engagement": "acme"}).json()["count"] == 0
        assert client.post(f"/facts/{fid}/retract", json={}).status_code == 404

    def test_search_endpoint_reports_its_backend(self, client):
        client.post("/episodes", json={"engagement": "acme", "summary": "findable needle"})
        body = client.get("/search", params={"engagement": "acme", "q": "needle"}).json()
        assert body["count"] >= 1
        assert body["backend"] in ("fts5", "like")

    def test_search_rejects_an_unknown_kind(self, client):
        r = client.get("/search", params={"engagement": "acme", "q": "x", "kind": "telepathy"})
        assert r.status_code == 400

    def test_context_endpoint_returns_a_prompt_block(self, client):
        client.post("/episodes", json={"engagement": "acme", "summary": "prior step"})
        body = client.get("/context", params={"engagement": "acme", "target": "a.test"}).json()
        assert "prompt_block" in body
        assert "ENGAGEMENT MEMORY (acme)" in body["prompt_block"]

    def test_context_max_chars_is_respected(self, client):
        for i in range(40):
            client.post(
                "/episodes", json={"engagement": "acme", "summary": "y" * 200}
            )
        body = client.get("/context", params={"engagement": "acme", "max_chars": 400}).json()
        assert len(body["prompt_block"]) <= 460

    def test_engagements_endpoint_lists_scopes_without_content(self, client):
        client.post("/episodes", json={"engagement": "acme", "summary": "private detail"})
        body = client.get("/engagements").json()
        assert body["engagements"][0]["engagement"] == "acme"
        assert "private detail" not in json.dumps(body)

    def test_two_clients_cannot_see_each_other_over_http(self, client):
        client.post("/episodes", json={"engagement": "acme", "summary": "acme only"})
        assert (
            client.get("/episodes", params={"engagement": "globex"}).json()["count"] == 0
        )
        assert (
            client.get("/search", params={"engagement": "globex", "q": "acme only"}).json()[
                "count"
            ]
            == 0
        )


# ===========================================================================
# 9. Agent-runtime integration
# ===========================================================================
class TestBridgeIntegration:
    """The bridge must honour the memory layer without becoming dependent on it."""

    def test_recall_context_returns_none_when_memory_is_unconfigured(self):
        from agent_runtime.bridge import Bridge

        bridge = Bridge.__new__(Bridge)  # no constructed deps needed
        bridge.memory = None
        bridge.memory_engagement = "acme"
        assert Bridge.recall_context(bridge, card_id="crd_1", target="a.test") is None

    def test_recall_context_builds_a_bundle_when_memory_is_present(self):
        """Updated in Phase 4: ``recall_context`` now returns a *recall bundle*
        rather than the raw ``MemoryContext``.

        The contract changed deliberately in item 1 - the bridge reads recall
        (similarity-matched records plus the composed graph bundle) before a crew
        run instead of only reading recent context. The property this test
        protected is unchanged and still asserted: with memory configured, the
        bundle carries what the engagement knows and renders it for a prompt.
        """
        from agent_runtime.bridge import Bridge

        bridge = Bridge.__new__(Bridge)
        bridge.memory = MemoryStore(":memory:")
        bridge.memory_engagement = "acme"
        bridge.memory.record(engagement="acme", summary="earlier recon", target="a.test")

        bundle = Bridge.recall_context(bridge, card_id="crd_1", target="a.test")

        assert bundle is not None
        assert bundle["engagement"] == "acme"
        assert "earlier recon" in bundle["prompt_block"]
        # the new retrieval products are present, even if empty
        for key in ("used", "hits", "backend", "graph_seed", "errors", "context"):
            assert key in bundle
        bridge.memory.close()

    def test_recall_context_swallows_a_broken_store(self):
        """Memory is an enhancement; it must never take the bridge down.

        Updated in Phase 4: ``recall_for_card`` isolates each retrieval call, so a
        store that raises on every method yields an **empty bundle with the
        failures recorded** rather than ``None``. The property is the same - the
        bridge is not taken down - and the failure is now *visible* in ``errors``
        instead of being indistinguishable from "the engagement is empty".
        """
        from agent_runtime.bridge import Bridge

        class Exploding:
            def context(self, *a, **k):
                raise RuntimeError("memory backend on fire")

            def hybrid(self, *a, **k):
                raise RuntimeError("memory backend on fire")

            def retrieval_bundle(self, *a, **k):
                raise RuntimeError("memory backend on fire")

        bridge = Bridge.__new__(Bridge)
        bridge.memory = Exploding()
        bridge.memory_engagement = "acme"

        bundle = Bridge.recall_context(bridge, card_id="crd_1", target="a.test")

        assert bundle is not None, "a broken store must not raise out of recall_context"
        assert bundle["hits"] == 0
        assert len(bundle["errors"]) == 3, bundle["errors"]
        assert bundle["prompt_block"]  # still renderable, so the crew prompt is valid
