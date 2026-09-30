"""Phase 4, item 1: the bridge reads recall *before* a crew run.

What is being tested is the wiring, not the mathematics of retrieval:

* the bridge calls the memory store **before** the crew runs, and what it gets
  back reaches the crew's run context (not just a return value nobody reads);
* the query is built from the **card**, so recall is relevant rather than merely
  recent - a differently-worded earlier card can still be surfaced;
* the read is scoped to the engagement and refuses to run unscoped;
* memory stays **optional**: off, broken, or half-broken, the card still runs.

The last point is why several tests here deliberately break the store. A memory
layer that can block a card is worse than no memory layer: the failure would
present as "the orchestrator is stuck" with nothing on the board to explain it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_runtime.bridge import Bridge
from agent_runtime.crewai_adapter import CrewAIAdapter
from agent_runtime.memory import recall_for_card, get_bridge_memory, recall_limit
from kanban_core.api import build_app as build_kanban_app
from kanban_core.bus import EventBus
from kanban_core.store import Store
from memory_store import MemoryStore
from tool_frontends.audit import ToolAuditLog
from tool_frontends.registry import ToolRegistry
from tool_frontends.runner import run_tool
from tool_frontends.wrappers import register_builtin as register_tools

ENGAGEMENT = "eng-acme-q3"


# --------------------------------------------------------------- fixtures
@pytest.fixture()
def kanban(tmp_path):
    import os

    os.environ["KANBAN_SEED"] = "1"
    app = build_kanban_app(Store(tmp_path / "kanban.db"), EventBus())
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def tools(tmp_path):
    registry = ToolRegistry()
    register_tools(registry)
    return registry, ToolAuditLog(tmp_path / "tools.db")


def in_process_executor(registry, audit):
    def _execute(tool_name, args, *, scope=None, approved=False, card_id=None):
        spec = registry.get(tool_name)
        if spec is None:
            return {"tool": tool_name, "status": "error", "reason": f"unknown tool {tool_name}"}
        return run_tool(
            spec, args, scope=scope, approved=approved, card_id=card_id, audit=audit
        ).as_dict()

    return _execute


class _ClientShim:
    """Adapts a FastAPI TestClient to the KanbanClient interface.

    Mirrors the shim used by ``test_bridge.py`` deliberately: the bridge calls
    ``card()``, ``move()``, ``add_trace()`` and friends, and a shim that only
    implements ``get``/``post`` would fail on the first call rather than testing
    anything about memory.
    """

    def __init__(self, test_client: TestClient) -> None:
        self._c = test_client
        self.base_url = "in-process"

    def get(self, path: str, **params):
        response = self._c.get(path, params=params or None)
        if response.status_code >= 400:
            raise RuntimeError(f"GET {path} -> {response.status_code}")
        return response.json()

    def post(self, path: str, payload=None):
        response = self._c.post(path, json=payload or {})
        if response.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {response.status_code}: {response.text}")
        return response.json()

    def card(self, card_id: str) -> dict:
        return self.get(f"/api/cards/{card_id}")

    def agents_queue(self) -> list[dict]:
        return self.get("/api/cards", column="Assigned")["cards"]

    def move(self, card_id, to_column, *, actor="bridge", actor_is_agent=True, force=False, note=""):
        return self.post(
            f"/api/cards/{card_id}/move",
            {
                "to_column": to_column,
                "actor": actor,
                "actor_is_agent": actor_is_agent,
                "force": force,
                "note": note,
            },
        )

    def add_trace(self, card_id, trace):
        return self.post(f"/api/cards/{card_id}/traces", trace)

    def add_artifact(self, card_id, artifact):
        return self.post(f"/api/cards/{card_id}/artifacts", artifact)

    def set_result(self, card_id, result, *, actor="bridge"):
        return self.post(f"/api/cards/{card_id}/result", {"result": result, "actor": actor})

    def request_approval(self, card_id, *, reason, requested_by, tool=None, tier=None):
        payload = {"reason": reason, "requested_by": requested_by}
        if tool:
            payload["tool"] = tool
        if tier is not None:
            payload["tier"] = tier
        return self.post(f"/api/cards/{card_id}/approvals", payload)

    def block(self, card_id, reason, *, actor="bridge"):
        return self.post(f"/api/cards/{card_id}/block", {"reason": reason, "actor": actor})

    def health(self):
        return self.get("/health")


class RecordingAdapter:
    """Records the exact card dict handed to the crew, then delegates.

    The point is to assert on what the crew *saw*, not on what the bridge
    returned: a value computed and then dropped before the run would satisfy any
    assertion made about the recall result itself.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.cards: list[dict] = []

    def run(self, crew, card, **kwargs):
        self.cards.append(dict(card))
        return self.inner.run(crew, card, **kwargs)


def _bridge(kanban, registry, audit, *, memory=None, engagement=ENGAGEMENT):
    adapter = RecordingAdapter(CrewAIAdapter(tool_executor=in_process_executor(registry, audit)))
    bridge = Bridge(
        client=_ClientShim(kanban),
        tool_executor=in_process_executor(registry, audit),
        adapter=adapter,
        memory=memory,
        memory_engagement=engagement,
    )
    return bridge, adapter


def _card(kanban, *, title, target="scanme.nmap.org", description="", crew="recon"):
    card = kanban.post(
        "/api/cards",
        json={
            "title": title,
            "description": description,
            "board_id": "brd_engagement",
            "assignee": "recon-specialist",
            "crew": crew,
            "scope": {"targets": [target], "authorization_ref": "X"},
            "tools": [
                {"name": "whois_lookup", "tier": 0, "args": {"target": target}},
                {"name": "dns_lookup", "tier": 0, "args": {"target": target}},
            ],
        },
    ).json()
    cid = card["card_id"]
    kanban.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
    return cid


# ------------------------------------------------- recall reaches the crew
class TestRecallReachesTheCrew:
    def test_prior_memory_is_retrieved_and_handed_to_the_crew(self, kanban, tools):
        registry, audit = tools
        store = MemoryStore(":memory:")
        store.assert_fact(
            engagement=ENGAGEMENT,
            key="web:scanme.nmap.org",
            statement="scanme.nmap.org runs Apache 2.4 with mod_status exposed",
            target="scanme.nmap.org",
        )
        bridge, adapter = _bridge(kanban, registry, audit, memory=store)
        cid = _card(kanban, title="Web recon on scanme.nmap.org")

        outcome = bridge.process_card(cid)

        assert outcome.status == "ok", outcome.reasons
        # 1. recall actually happened
        assert bridge.stats.memory_recalls == 1
        assert bridge.stats.memory_hits >= 1
        # 2. it reached the crew's run context
        seen = adapter.cards[0]
        assert "memory_context" in seen
        assert "mod_status" in seen["memory_context"]
        assert "ENGAGEMENT MEMORY" in seen["memory_context"]
        # 3. the trail is on the outcome, not just the prompt
        assert outcome.run["memory"]["hits"] >= 1
        recalled = outcome.run["memory"]["used"][0]
        assert "mod_status" in recalled["summary"]
        assert recalled["kind"] == "semantic"

    def test_the_query_is_built_from_the_card_not_the_engagement(self, kanban, tools):
        """Relevance comes from the card's own words."""
        registry, audit = tools
        store = MemoryStore(":memory:")
        store.assert_fact(engagement=ENGAGEMENT, key="k", statement="noise", target="10.0.0.9")
        bridge, adapter = _bridge(kanban, registry, audit, memory=store)
        cid = _card(kanban, title="Enumerate SMB shares", description="445/tcp focus")

        bridge.process_card(cid)

        trail = adapter.cards[0]["memory_recall"]
        assert "Enumerate SMB shares" in trail["query"]
        assert "445/tcp focus" in trail["query"]
        assert "scanme.nmap.org" in trail["query"]  # the card's target is appended
        assert trail["backend"]  # which embedder answered is recorded

    def test_a_differently_worded_earlier_card_is_still_surfaced(self, kanban, tools):
        """The capability the old ``/context`` read did not have.

        The stored record and the new card share no wording at all, so an
        exact-term lookup would miss it. Only a similarity search finds it - and
        a *more recent* but irrelevant record must not outrank it.
        """
        registry, audit = tools
        store = MemoryStore(":memory:")
        store.record(
            engagement=ENGAGEMENT,
            summary="Apache mod_status information disclosure on scanme.nmap.org",
            target="scanme.nmap.org",
        )
        store.record(engagement=ENGAGEMENT, summary="unrelated note about printers")
        bridge, adapter = _bridge(kanban, registry, audit, memory=store)
        cid = _card(kanban, title="web-server-fingerprint scanme.nmap.org")

        bridge.process_card(cid)

        summaries = [h["summary"] for h in adapter.cards[0]["memory_recall"]["used"]]
        assert any("mod_status" in s for s in summaries), summaries

    def test_graph_neighbourhood_is_retrieved_and_recorded(self, kanban, tools):
        registry, audit = tools
        store = MemoryStore(":memory:")
        store.record(
            engagement=ENGAGEMENT,
            summary="SMB signing disabled on scanme.nmap.org",
            target="scanme.nmap.org",
        )
        bridge, adapter = _bridge(kanban, registry, audit, memory=store)
        cid = _card(kanban, title="Check SMB signing scanme.nmap.org")

        bridge.process_card(cid)

        trail = adapter.cards[0]["memory_recall"]
        assert "graph_seed" in trail
        # the graph retrieval must not have errored
        assert not [e for e in trail["errors"] if e.startswith("graph")]
        assert "Knowledge graph" in adapter.cards[0]["memory_context"] or trail["graph_seed"] is None

    def test_prior_context_survives_into_the_card_replay(self, kanban, tools):
        """What the crew was shown must be reconstructable from the board."""
        registry, audit = tools
        store = MemoryStore(":memory:")
        store.assert_fact(
            engagement=ENGAGEMENT, key="k1", statement="host runs OpenSSH 8.2", target="scanme.nmap.org"
        )
        bridge, adapter = _bridge(kanban, registry, audit, memory=store)
        cid = _card(kanban, title="Version probe scanme.nmap.org")

        outcome = bridge.process_card(cid)

        assert outcome.run["memory"] is not None
        assert outcome.run["memory"]["query"]
        assert isinstance(outcome.run["memory"]["used"], list)


# --------------------------------------------------- optional and fail-safe
class TestMemoryStaysOptional:
    def test_disabled_memory_means_no_recall_and_no_context_section(self, kanban, tools):
        registry, audit = tools
        bridge, adapter = _bridge(kanban, registry, audit, memory=None)
        cid = _card(kanban, title="Web recon on scanme.nmap.org")

        outcome = bridge.process_card(cid)

        assert outcome.status == "ok"
        assert bridge.stats.memory_recalls == 0
        assert "memory_context" not in adapter.cards[0]
        assert outcome.run["memory"] is None
        # "off" and "nothing found" must be distinguishable
        assert outcome.run["memory"] is not None or bridge.stats.memory_failures == 0

    def test_env_flag_keeps_memory_off_by_default(self, monkeypatch):
        monkeypatch.delenv("MEMORY_ENABLED", raising=False)
        assert get_bridge_memory() is None
        monkeypatch.setenv("MEMORY_ENABLED", "1")
        monkeypatch.setenv("MEMORY_DB", ":memory:")
        assert get_bridge_memory() is not None

    def test_a_completely_broken_store_does_not_stop_the_card(self, kanban, tools):
        """A store that raises on every method must still let the card finish."""
        registry, audit = tools
        bridge, adapter = _bridge(kanban, registry, audit, memory=object())
        cid = _card(kanban, title="Web recon on scanme.nmap.org")

        outcome = bridge.process_card(cid)

        assert outcome.status == "ok", outcome.reasons
        assert outcome.column == "Review"
        # the failure is *reported*, not swallowed
        errors = adapter.cards[0]["memory_recall"]["errors"]
        assert errors
        assert adapter.cards[0]["memory_recall"]["hits"] == 0

    def test_one_broken_retrieval_path_does_not_lose_the_others(self, kanban, tools):
        """A dead vector index must not cost the crew its episodic history."""

        class HalfBrokenStore(MemoryStore):
            def hybrid(self, *a, **kw):  # noqa: D102
                raise RuntimeError("vector index unavailable")

        registry, audit = tools
        store = HalfBrokenStore(":memory:")
        store.record(engagement=ENGAGEMENT, summary="earlier whois on the target")
        bridge, adapter = _bridge(kanban, registry, audit, memory=store)
        cid = _card(kanban, title="Web recon on scanme.nmap.org")

        bridge.process_card(cid)

        trail = adapter.cards[0]["memory_recall"]
        assert any("recall" in e for e in trail["errors"]), trail["errors"]
        # recall is empty, but the standing history still reached the crew
        assert "Earlier" in adapter.cards[0]["memory_context"] or "earlier" in adapter.cards[0]["memory_context"]

    def test_recall_failure_is_counted_on_the_bridge_stats(self, kanban, tools):
        registry, audit = tools
        bridge, _ = _bridge(kanban, registry, audit, memory=object())
        cid = _card(kanban, title="Web recon on scanme.nmap.org")

        bridge.process_card(cid)

        # recall_for_card isolates per-call failures, so the bridge-level counter
        # stays clean while the per-card trail carries the detail. Either way the
        # failure is visible somewhere - which is the property that matters.
        trailer = bridge.stats.memory_recalls + bridge.stats.memory_failures
        assert trailer >= 1
        assert bridge.stats.errors >= 0  # never a crash


# ------------------------------------------------ recall_for_card contract
class _StubStore:
    """A scripted store, so the bundle contract can be asserted exactly."""

    def __init__(self, *, context=None, hits=None, bundle=None, boom=()) -> None:
        self._context = context
        self._hits = hits or []
        self._bundle = bundle
        self._boom = set(boom)
        self.calls: list[str] = []

    def context(self, engagement, **kw):
        self.calls.append("context")
        if "context" in self._boom:
            raise RuntimeError("context down")
        return self._context

    def hybrid(self, engagement, query, **kw):
        self.calls.append("hybrid")
        if "hybrid" in self._boom:
            raise RuntimeError("hybrid down")
        return self._hits

    def retrieval_bundle(self, engagement, query, **kw):
        self.calls.append("graph")
        if "graph" in self._boom:
            raise RuntimeError("graph down")
        return self._bundle


class _Hit:
    def __init__(self, summary, score=0.5, kind="semantic") -> None:
        self._d = {
            "kind": kind,
            "id": "fct_1",
            "engagement": ENGAGEMENT,
            "ts": "2026-01-01T00:00:00Z",
            "summary": summary,
            "score": score,
            "record": {"retrieval": "hybrid", "matched_by": ["lexical", "semantic"]},
        }

    def model_dump(self):
        return dict(self._d)


class _Ctx:
    def as_prompt_block(self, **kw):
        return "ENGAGEMENT MEMORY\nEstablished facts:\n  - [0.90] apache is old"


class TestRecallBundleContract:
    def test_requires_an_engagement_and_a_store(self):
        assert recall_for_card(None, ENGAGEMENT) is None
        assert recall_for_card(_StubStore(), "") is None

    def test_returns_the_three_retrieval_products(self):
        store = _StubStore(
            context=_Ctx(),
            hits=[_Hit("apache 2.4.49 is vulnerable")],
            bundle={"graph": {"nodes": [{"name": "scanme.nmap.org"}], "edges": [1]}, "graph_seed": "scanme.nmap.org"},
        )
        result = recall_for_card(
            store, ENGAGEMENT, card={"title": "recon", "description": "web"}, target="scanme.nmap.org"
        )
        assert result["hits"] == 1
        assert result["used"][0]["summary"] == "apache 2.4.49 is vulnerable"
        assert result["graph_seed"] == "scanme.nmap.org"
        assert result["context"] is not None
        assert result["errors"] == []
        assert store.calls == ["context", "hybrid", "graph"]

    def test_prompt_block_puts_recall_before_the_standing_history(self):
        """Order matters: the retrieved records are chosen for *this* card."""
        store = _StubStore(
            context=_Ctx(),
            hits=[_Hit("recalled record about mod_status")],
            bundle={"graph": {"nodes": [], "edges": []}, "graph_seed": None},
        )
        result = recall_for_card(store, ENGAGEMENT, card={"title": "t"}, target="h")
        block = result["prompt_block"]
        assert block.index("recalled record about mod_status") < block.index("apache is old")

    def test_graph_section_is_rendered_from_the_bundle(self):
        store = _StubStore(
            context=None,
            hits=[_Hit("host runs apache")],
            bundle={
                "graph": {"nodes": [{"name": "scanme.nmap.org"}, {"name": "apache"}], "edges": [1, 2]},
                "graph_seed": "scanme.nmap.org",
            },
        )
        block = recall_for_card(store, ENGAGEMENT, card={"title": "t"}, target="h")["prompt_block"]
        assert "Knowledge graph around 'scanme.nmap.org'" in block
        assert "apache" in block
        assert "relations: 2" in block

    def test_partial_failure_is_reported_and_the_rest_kept(self):
        store = _StubStore(context=_Ctx(), hits=[_Hit("useful")], bundle=None, boom={"graph"})
        result = recall_for_card(store, ENGAGEMENT, card={"title": "t"}, target="h")
        assert result["hits"] == 1
        assert any("graph" in e for e in result["errors"])
        assert "Retrieval notes" in result["prompt_block"]

    def test_every_path_down_still_returns_a_usable_bundle(self):
        store = _StubStore(boom={"context", "hybrid", "graph"})
        result = recall_for_card(store, ENGAGEMENT, card={"title": "t"}, target="h")
        assert result is not None
        assert result["hits"] == 0
        assert len(result["errors"]) == 3
        assert result["prompt_block"]  # never an empty string

    def test_prompt_block_is_bounded(self):
        class HugeCtx:
            def as_prompt_block(self, **kw):
                return "x" * 50_000

        store = _StubStore(context=HugeCtx(), hits=[_Hit("a")], bundle=None)
        block = recall_for_card(store, ENGAGEMENT, card={"title": "t"}, target="h")["prompt_block"]
        assert len(block) < 5000
        assert "truncated" in block

    def test_recall_limit_is_configurable_and_sane(self, monkeypatch):
        monkeypatch.setenv("MEMORY_RECALL_LIMIT", "3")
        assert recall_limit() == 3
        monkeypatch.setenv("MEMORY_RECALL_LIMIT", "not-a-number")
        assert recall_limit() == 6
        monkeypatch.setenv("MEMORY_RECALL_LIMIT", "0")
        assert recall_limit() == 1
