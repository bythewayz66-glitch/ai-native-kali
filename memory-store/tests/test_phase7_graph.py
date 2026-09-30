"""Richer graph queries (Phase 7, item 6).

Three additions, each answering a question ``query()`` could not:

* ``neighbors`` - the neighbourhood *filtered* by node kind and edge predicate, so
  a planner can ask "what hosts are near this" instead of receiving every node
  and spending its prompt budget on unrelated ones.
* ``shortest_path`` - "how are these two things connected", which a
  neighbourhood cannot express: it cannot tell one hop from six.
* ``entities_by_kind`` - cheap orientation before a traversal is affordable.

The graph is built directly through ``upsert_entity`` / ``add_relation`` rather
than through text extraction, so these tests exercise the traversal and not the
entity recogniser.
"""
from __future__ import annotations

import pytest

from memory_store.graph import GraphIndex

ENG = "ENG-GRAPH"


@pytest.fixture()
def graph():
    import sqlite3
    import threading

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    index = GraphIndex(conn, threading.RLock())
    yield index
    conn.close()


@pytest.fixture()
def diamond(graph):
    """h2 -> h1 -> s1 -> c1, plus an unrelated host so filters have something to
    exclude.

    Deliberately not a straight line: ``h_other`` hangs off nothing, so a filter
    that silently returns everything would be caught.
    """
    h1 = graph.upsert_entity(ENG, "host", "10.10.0.5")
    h2 = graph.upsert_entity(ENG, "host", "mail.example.net")
    s1 = graph.upsert_entity(ENG, "service", "smb")
    c1 = graph.upsert_entity(ENG, "cve", "CVE-2021-41773")
    other = graph.upsert_entity(ENG, "host", "192.0.2.99")
    cred = graph.upsert_entity(ENG, "credential", "svc_backup")

    graph.add_relation(ENG, h1, s1, "has_service")
    graph.add_relation(ENG, s1, c1, "has_cve")
    graph.add_relation(ENG, h2, h1, "resolves_to")
    graph.add_relation(ENG, h1, cred, "has_credential")
    return {"h1": h1, "h2": h2, "s1": s1, "c1": c1, "other": other, "cred": cred}


def names(result):
    return {n["name"] for n in result["nodes"]}


class TestNeighbors:
    def test_depth_one_returns_immediate_neighbours(self, graph, diamond):
        result = graph.neighbors(ENG, "10.10.0.5", depth=1)
        assert result["root"]["name"] == "10.10.0.5"
        # `mail.example.net` is included because it `resolves_to` 10.10.0.5: the
        # traversal is **undirected**, so an incoming edge is adjacency too.
        # Direction is an artefact of which end was recorded first.
        assert names(result) == {"10.10.0.5", "smb", "svc_backup", "mail.example.net"}

    def test_depth_two_walks_one_further(self, graph, diamond):
        result = graph.neighbors(ENG, "10.10.0.5", depth=2)
        assert "cve-2021-41773" in names(result)

    def test_depth_is_capped_and_the_cap_is_reported(self, graph, diamond):
        """A clamped traversal must not report the depth it was asked for.

        The request asked for 99; the answer is 4, and the result has to say 4 -
        otherwise a caller reading ``depth`` would believe a 99-hop search had run.
        """
        result = graph.neighbors(ENG, "10.10.0.5", depth=99)
        assert result["depth"] == 4

    def test_kind_filter_narrows_the_nodes(self, graph, diamond):
        result = graph.neighbors(ENG, "10.10.0.5", depth=1, kinds=["service"])
        # Only the service, plus the root the caller asked about.
        assert names(result) == {"10.10.0.5", "smb"}

    def test_the_root_survives_a_kind_filter_that_excludes_it(self, graph, diamond):
        """A result that omits the thing you asked about is not an answer."""
        result = graph.neighbors(ENG, "10.10.0.5", depth=1, kinds=["cve"])
        assert "10.10.0.5" in names(result)

    def test_predicate_filter_narrows_the_traversal(self, graph, diamond):
        result = graph.neighbors(ENG, "10.10.0.5", depth=2, predicates=["has_service"])
        # has_service leads only to smb; has_cve is filtered out, so the CVE is
        # two hops away *over a filtered path* and therefore unreachable.
        assert names(result) == {"10.10.0.5", "smb"}

    def test_an_unrelated_host_is_not_pulled_in(self, graph, diamond):
        result = graph.neighbors(ENG, "10.10.0.5", depth=3)
        assert "192.0.2.99" not in names(result)

    def test_every_returned_edge_touches_a_returned_node(self, graph, diamond):
        """A graph that references a node the caller cannot see is not a graph."""
        result = graph.neighbors(ENG, "10.10.0.5", depth=2, kinds=["host"])
        visible = {n["entity_id"] for n in result["nodes"]}
        for edge in result["edges"]:
            assert edge["src_id"] in visible or edge["dst_id"] in visible

    def test_an_unscoped_read_is_refused(self, graph, diamond):
        with pytest.raises(ValueError, match="engagement is required"):
            graph.neighbors("", "10.10.0.5")

    def test_an_unknown_entity_returns_an_empty_graph_not_an_error(self, graph, diamond):
        result = graph.neighbors(ENG, "nothing.example.net")
        assert result["root"] is None
        assert result["nodes"] == [] and result["edges"] == []

    def test_the_engagement_boundary_is_respected(self, graph, diamond):
        """A neighbour in another engagement must not appear."""
        graph.upsert_entity("ENG-OTHER", "host", "10.10.0.5")
        result = graph.neighbors(ENG, "10.10.0.5", depth=2)
        assert all("ENG-OTHER" not in n["entity_id"] for n in result["nodes"])

    def test_the_filters_are_echoed_back(self, graph, diamond):
        result = graph.neighbors(ENG, "10.10.0.5", kinds=["host"], predicates=["resolves_to"])
        assert result["kinds"] == ["host"]
        assert result["predicates"] == ["resolves_to"]


class TestShortestPath:
    def test_a_multi_hop_path_is_found_and_ordered(self, graph, diamond):
        result = graph.shortest_path(ENG, "mail.example.net", "CVE-2021-41773")
        assert result["found"] is True
        assert [n["name"] for n in result["path"]] == [
            "mail.example.net",
            "10.10.0.5",
            "smb",
            "cve-2021-41773",
        ]
        assert result["hops"] == 3
        assert len(result["edges"]) == 3

    def test_the_path_starts_at_the_source_and_ends_at_the_destination(self, graph, diamond):
        result = graph.shortest_path(ENG, "mail.example.net", "cve-2021-41773")
        assert result["path"][0]["entity_id"] == result["src"]["entity_id"]
        assert result["path"][-1]["entity_id"] == result["dst"]["entity_id"]

    def test_the_shortest_of_two_routes_is_returned(self, graph, diamond):
        """A direct edge must win over a longer way round."""
        graph.add_relation(ENG, diamond["h2"], diamond["c1"], "related_to")
        result = graph.shortest_path(ENG, "mail.example.net", "CVE-2021-41773")
        assert result["hops"] == 1

    def test_a_node_is_its_own_zero_hop_path(self, graph, diamond):
        result = graph.shortest_path(ENG, "10.10.0.5", "10.10.0.5")
        assert result["found"] is True and result["hops"] == 0

    def test_no_path_is_reported_as_not_found_rather_than_raised(self, graph, diamond):
        result = graph.shortest_path(ENG, "10.10.0.5", "192.0.2.99")
        assert result["found"] is False
        assert result["path"] == [] and result["hops"] == 0

    def test_an_unknown_endpoint_is_not_found(self, graph, diamond):
        assert graph.shortest_path(ENG, "10.10.0.5", "ghost.example.net")["found"] is False

    def test_max_depth_bounds_the_search(self, graph, diamond):
        # Three hops needed; two allowed.
        assert graph.shortest_path(ENG, "mail.example.net", "CVE-2021-41773", max_depth=2)["found"] is False

    def test_traversal_is_undirected(self, graph, diamond):
        """Direction is an artefact of which end was recorded first, so a path
        reachable only backwards must still be found."""
        result = graph.shortest_path(ENG, "CVE-2021-41773", "mail.example.net")
        assert result["found"] is True and result["hops"] == 3

    def test_an_unscoped_read_is_refused(self, graph, diamond):
        with pytest.raises(ValueError, match="engagement is required"):
            graph.shortest_path("", "a", "b")


class TestEntitiesByKind:
    def test_counts_are_reported_per_kind(self, graph, diamond):
        counts = graph.entities_by_kind(ENG)
        assert counts["host"] == 3  # h1, h2, other
        assert counts["cve"] == 1
        assert counts["service"] == 1

    def test_the_most_numerous_kind_comes_first(self, graph, diamond):
        assert list(graph.entities_by_kind(ENG))[0] == "host"

    def test_it_is_scoped_to_the_engagement(self, graph, diamond):
        graph.upsert_entity("ENG-OTHER", "cve", "CVE-2000-0001")
        assert graph.entities_by_kind("ENG-OTHER") == {"cve": 1}

    def test_an_unscoped_read_is_refused(self, graph, diamond):
        with pytest.raises(ValueError, match="engagement is required"):
            graph.entities_by_kind("")


class TestBackwardsCompatibility:
    def test_the_original_query_still_works(self, graph, diamond):
        """Phase 3's ``query`` contract must not change under item 6."""
        whole = graph.query(ENG)
        # Five, not six: `query()` builds its node set from the edges, and
        # 192.0.2.99 has no edges - an isolated entity is not reachable by any
        # traversal, which is the correct behaviour rather than a gap.
        assert len(whole["nodes"]) == 5
        rooted = graph.query(ENG, entity="10.10.0.5", depth=1)
        assert rooted["root"]["name"] == "10.10.0.5"

    def test_neighbors_agrees_with_query_on_the_unfiltered_case(self, graph, diamond):
        """The new traversal and the old one must not disagree about what is
        adjacent - an inconsistency here would mean two different graphs."""
        old = graph.query(ENG, entity="10.10.0.5", depth=1)
        new = graph.neighbors(ENG, "10.10.0.5", depth=1)
        assert {n["entity_id"] for n in old["nodes"]} == {n["entity_id"] for n in new["nodes"]}
