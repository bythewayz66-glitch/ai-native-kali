"""Phase 16, items 2 and 3: broader entity extraction and entity drill-down.

The extraction tests are mostly **negative**: the failure that matters is a false
node, because the graph feeds security reports and a fabricated "port 30" or a
"host" that is really ``rockyou.txt`` is a wrong fact, not a missing one.
"""
from __future__ import annotations

import pytest

from memory_store.graph import extract_entities
from memory_store.store import MemoryStore


def _kinds(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for kind, name in extract_entities(text):
        out.setdefault(kind, []).append(name)
    return out


# ---------------------------------------------------------------------------
# item 3: broader extraction
# ---------------------------------------------------------------------------
class TestExtractionBreadth:
    def test_url_is_its_own_entity(self):
        kinds = _kinds("fetched https://api.example.com/v1/users ok")
        assert any(n.startswith("https://api.example.com/v1/users") for n in kinds.get("url", []))

    def test_email_is_a_person_not_a_host(self):
        kinds = _kinds("contact admin@corp.example.com for access")
        assert "admin@corp.example.com" in kinds.get("email", [])

    def test_sha256_digest_becomes_one_hash_node(self):
        digest = "a" * 64
        kinds = _kinds(f"sha256 {digest} matches the firmware")
        assert kinds.get("hash") == [digest]

    def test_sha1_inside_a_sha256_is_not_a_second_node(self):
        digest = "b" * 64
        kinds = _kinds(f"digest {digest}")
        assert len(kinds.get("hash", [])) == 1

    def test_host_port_yields_a_port_node(self):
        kinds = _kinds("nmap found 10.0.0.5:8443 open")
        assert "8443" in kinds.get("port", [])

    def test_clock_time_is_not_a_port(self):
        """The false-positive guard: ``12:30`` must not become port 30."""
        kinds = _kinds("the scan ran at 12:30 and finished at 16:9 scale")
        assert kinds.get("port", []) == []

    def test_username_label_becomes_a_user_node(self):
        kinds = _kinds("username: svc_backup on the jump host")
        assert "svc_backup" in kinds.get("user", [])

    def test_platform_is_recognised(self):
        kinds = _kinds("target is running Ubuntu 22.04 LTS")
        assert "ubuntu" in kinds.get("platform", [])

    def test_file_extension_is_still_not_a_host(self):
        """Regression: the Phase 6 guard must survive the new shapes."""
        kinds = _kinds("pulled /usr/share/wordlists/rockyou.txt")
        assert kinds.get("host", []) == []

    def test_existing_shapes_are_unchanged(self):
        kinds = _kinds("CVE-2021-44228 on 10.0.0.5 running apache ssh")
        assert "10.0.0.5" in kinds.get("host", [])
        assert "CVE-2021-44228" in kinds.get("cve", [])
        assert "apache" in kinds.get("product", [])
        assert "ssh" in kinds.get("service", [])


# ---------------------------------------------------------------------------
# item 2: entity drill-down
# ---------------------------------------------------------------------------
@pytest.fixture()
def store() -> MemoryStore:
    return MemoryStore(":memory:")


def _seed(store: MemoryStore, eng: str = "eng_x") -> None:
    store.record(
        engagement=eng,
        summary="discovered 10.0.0.5:443 running apache",
        target="10.0.0.5",
    )
    store.record(
        engagement=eng,
        summary="password: hunter2 works on 10.0.0.5",
        target="10.0.0.5",
    )
    store.record(
        engagement=eng,
        summary="CVE-2021-44228 affects 10.0.0.5 apache",
        target="10.0.0.5",
    )
    store.backfill_derived(engagement=eng)


class TestEntityProfile:
    def test_profile_groups_relations_by_predicate(self, store):
        _seed(store)
        profile = store.graph_entity_profile("eng_x", "10.0.0.5")
        assert profile["found"] is True
        assert profile["counts"]["relations"] >= 1
        assert profile["relations"], "a seeded entity with edges must expose them"

    def test_profile_counts_neighbour_kinds(self, store):
        _seed(store)
        profile = store.graph_entity_profile("eng_x", "10.0.0.5")
        assert isinstance(profile["kinds"], dict)
        assert sum(profile["kinds"].values()) >= 1

    def test_unknown_entity_is_found_false_not_an_error(self, store):
        _seed(store)
        profile = store.graph_entity_profile("eng_x", "203.0.113.250")
        assert profile["found"] is False
        assert profile["relations"] == {}

    def test_unscoped_profile_is_refused(self, store):
        with pytest.raises(ValueError):
            store.graph_entity_profile("", "10.0.0.5")

    def test_profile_does_not_leak_across_engagements(self, store):
        _seed(store, "eng_x")
        store.record(engagement="eng_y", summary="nothing here", target="10.0.0.9")
        store.backfill_derived(engagement="eng_y")
        profile = store.graph_entity_profile("eng_y", "10.0.0.5")
        # The entity belongs to eng_x; from eng_y it must not resolve.
        assert profile["found"] is False
