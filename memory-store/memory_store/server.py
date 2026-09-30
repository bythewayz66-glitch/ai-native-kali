"""HTTP service for the L6 memory store (port 8087).

Blueprint ref: section 03, layer L6.

The API is deliberately small, and every read requires an ``engagement``. There
is no endpoint that returns memory across engagements - the only cross-scope
view is ``/engagements``, which returns counts and never content.

Endpoints
---------
``GET  /health``                     liveness, backend and volume
``POST /episodes``                   append one episode
``GET  /episodes``                   read an engagement's history
``POST /facts``                      assert a fact (supersedes the same key)
``GET  /facts``                      active facts, most confident first
``POST /facts/{id}/retract``         retract a fact (kept, not deleted)
``GET  /search``                     full-text search within one engagement
``GET  /context``                    the bounded bundle handed to a crew
``GET  /engagements``                scope inventory (counts only)
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .models import Episode, Fact, SearchHit
from .store import MemoryStore, reset_store
from .vector import vector_backend

app = FastAPI(
    title="AI-native Kali - L6 memory store",
    description="Episodic + semantic memory for the orchestration layer, scoped per engagement.",
    version="1.0.0",
)


def _store() -> MemoryStore:
    from .store import _DEFAULT  # noqa: PLC0415

    if _DEFAULT is None:
        reset_store(MemoryStore(os.environ.get("MEMORY_DB", ":memory:")))
        from .store import _DEFAULT as fresh  # noqa: PLC0415

        return fresh  # type: ignore[return-value]
    return _DEFAULT


def _require_engagement(engagement: Optional[str]) -> str:
    if not engagement or not engagement.strip():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "engagement is required",
                "why": "memory is scoped per engagement; an unscoped read could leak between clients",
            },
        )
    return engagement


def _parse_kinds(kind: Optional[str]) -> Optional[list[str]]:
    """Shared parser for ``kind=a,b`` query values.

    Extracted so ``/search`` and ``/recall`` cannot drift apart on which memory
    kinds are addressable - a validation rule duplicated in two places is a rule
    that will eventually hold in one of them.
    """
    if not kind:
        return None
    kinds = [k.strip() for k in kind.split(",") if k.strip()]
    bad = [k for k in kinds if k not in ("episodic", "semantic")]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown memory kind(s): {bad}")
    return kinds


@app.get("/health")
def health() -> dict[str, Any]:
    store = _store()
    return {"status": "ok", "service": "memory-store", "port": 8087, **store.stats()}


@app.get("/stats")
def stats() -> dict[str, Any]:
    return _store().stats()


@app.get("/embedder")
def embedder() -> dict[str, Any]:
    """Which embedding backend is live, and whether it is healthy.

    Exposed as its own endpoint because "which backend answered" is a question an
    operator needs answered *independently* of recall: after changing
    ``MEMORY_EMBEDDER``, recall can silently return nothing while every count and
    health check still looks perfect. ``probe=true`` also clears the degraded
    latch and re-tests the model endpoint, so a service that has come back up can
    be re-adopted without a restart.
    """
    from memory_store.vector import embedder_report

    report = embedder_report()
    return {
        "service": "memory-store",
        **report,
        "vectors": _store().vectors.count(),
        "drift": _store().vectors.drift(),
        "backends": _store().vectors.backend_counts(),
    }


@app.post("/embedder/probe")
def embedder_probe() -> dict[str, Any]:
    """Re-test the model endpoint (clears the degraded latch first)."""
    from memory_store.vector import active_embedder, embedder_report

    impl = active_embedder()
    available = impl.available() if hasattr(impl, "available") else True
    return {"service": "memory-store", "available": available, **embedder_report()}


# ---------------------------------------------------------------- episodes
@app.post("/episodes", status_code=201)
def add_episode(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    engagement = _require_engagement(payload.get("engagement"))
    if not payload.get("summary"):
        raise HTTPException(status_code=400, detail="summary is required")
    try:
        episode = _store().record(
            engagement=engagement,
            summary=payload["summary"],
            kind=payload.get("kind", "observation"),
            detail=payload.get("detail", ""),
            card_id=payload.get("card_id"),
            agent=payload.get("agent"),
            board_id=payload.get("board_id"),
            target=payload.get("target"),
            data=payload.get("data") or {},
            tags=payload.get("tags") or [],
            supersedes=payload.get("supersedes"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return episode.model_dump()


@app.get("/episodes")
def list_episodes(
    engagement: Optional[str] = Query(None, description="Required: memory is scoped per engagement"),
    card_id: Optional[str] = None,
    target: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    newest_first: bool = True,
) -> dict[str, Any]:
    eng = _require_engagement(engagement)
    rows = _store().episodes(
        eng, card_id=card_id, target=target, kind=kind, limit=limit, newest_first=newest_first
    )
    return {"engagement": eng, "count": len(rows), "episodes": [e.model_dump() for e in rows]}


# ------------------------------------------------------------------- facts
@app.post("/facts", status_code=201)
def add_fact(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    engagement = _require_engagement(payload.get("engagement"))
    if not payload.get("key") or not payload.get("statement"):
        raise HTTPException(status_code=400, detail="key and statement are required")
    try:
        fact = _store().assert_fact(
            engagement=engagement,
            key=payload["key"],
            statement=payload["statement"],
            detail=payload.get("detail", ""),
            confidence=float(payload.get("confidence", 0.5)),
            source_card=payload.get("source_card"),
            agent=payload.get("agent"),
            target=payload.get("target"),
            tags=payload.get("tags") or [],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return fact.model_dump()


@app.get("/facts")
def list_facts(
    engagement: Optional[str] = Query(None, description="Required: memory is scoped per engagement"),
    target: Optional[str] = None,
    status: str = "active",
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    eng = _require_engagement(engagement)
    rows = _store().facts(eng, target=target, status=status, limit=limit)
    return {"engagement": eng, "count": len(rows), "facts": [f.model_dump() for f in rows]}


@app.post("/facts/{fact_id}/retract")
def retract(fact_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    ok = _store().retract_fact(fact_id, reason=payload.get("reason", "retracted"))
    if not ok:
        raise HTTPException(status_code=404, detail="no active fact with that id")
    return {"retracted": fact_id, "reason": payload.get("reason", "retracted")}


# ------------------------------------------------------------------ search
@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    engagement: Optional[str] = Query(None, description="Required: memory is scoped per engagement"),
    kind: Optional[str] = Query(None, description="episodic | semantic | comma-separated"),
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    eng = _require_engagement(engagement)
    kinds = _parse_kinds(kind)
    hits = _store().search(eng, q, kinds=kinds, limit=limit)
    return {
        "engagement": eng,
        "query": q,
        "count": len(hits),
        "backend": "fts5" if _store().fts else "like",
        "hits": [h.model_dump() for h in hits],
    }


# ----------------------------------------------------------------- context
@app.get("/context")
def context(
    engagement: Optional[str] = Query(None, description="Required: memory is scoped per engagement"),
    card_id: Optional[str] = None,
    target: Optional[str] = None,
    max_chars: int = Query(4000, ge=200, le=20000),
) -> dict[str, Any]:
    """The bundle a crew is handed before it works a card."""
    eng = _require_engagement(engagement)
    bundle = _store().context(eng, card_id=card_id, target=target)
    body = bundle.model_dump()
    body["prompt_block"] = bundle.as_prompt_block(max_chars=max_chars)
    return body


# ------------------------------------------------------- recall (Phase 3)
#
# Vector, hybrid and graph retrieval were added in Phase 3 behind the *same*
# scoping rule as everything above: ``engagement`` is required, and omitting it
# is a 400 rather than a wider result. A new retrieval path is not a new way to
# cross a scope boundary, so the guard is reused rather than re-implemented.
@app.get("/recall")
def recall(
    q: str = Query(..., min_length=1),
    engagement: Optional[str] = Query(None, description="Required: memory is scoped per engagement"),
    kind: Optional[str] = Query(None, description="episodic | semantic | comma-separated"),
    top_k: int = Query(10, ge=1, le=100),
    min_score: float = Query(0.01, ge=0.0, le=1.0),
    mode: str = Query("hybrid", description="hybrid | vector"),
) -> dict[str, Any]:
    """Similarity recall. ``mode=vector`` is pure cosine; ``hybrid`` fuses it
    with BM25 by reciprocal rank."""
    eng = _require_engagement(engagement)
    kinds = _parse_kinds(kind)
    store = _store()
    if mode not in ("hybrid", "vector"):
        raise HTTPException(status_code=400, detail="mode must be 'hybrid' or 'vector'")
    if mode == "vector":
        hits = store.recall(eng, q, kinds=kinds, top_k=top_k, min_score=min_score)
    else:
        hits = store.hybrid(eng, q, kinds=kinds, limit=top_k)
    return {
        "engagement": eng,
        "query": q,
        "mode": mode,
        "count": len(hits),
        "vector_backend": vector_backend(),
        "hits": [h.model_dump() for h in hits],
    }


@app.get("/recall/bundle")
def recall_bundle(
    q: str = Query(..., min_length=1),
    engagement: Optional[str] = Query(None, description="Required: memory is scoped per engagement"),
    limit: int = Query(8, ge=1, le=50),
    depth: int = Query(1, ge=1, le=4),
) -> dict[str, Any]:
    """Fused retrieval **plus** the graph neighbourhood around the top hit.

    This is the composition a crew calls when it wants both "what is relevant"
    and "what is that connected to" in one round trip.
    """
    eng = _require_engagement(engagement)
    return _store().retrieval_bundle(eng, q, limit=limit, depth=depth)


# -------------------------------------------------------- graph (Phase 3)
@app.get("/graph")
def graph(
    engagement: Optional[str] = Query(None, description="Required: memory is scoped per engagement"),
    entity: Optional[str] = Query(None, description="Start node (name); omit for the whole graph"),
    predicate: Optional[str] = Query(None, description="Filter to one relation predicate"),
    depth: int = Query(1, ge=1, le=4),
    limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    """Traverse the knowledge graph for one engagement."""
    eng = _require_engagement(engagement)
    return _store().graph_query(eng, entity=entity, predicate=predicate, depth=depth, limit=limit)


@app.get("/graph/entities")
def graph_entities(
    engagement: Optional[str] = Query(None, description="Required: memory is scoped per engagement"),
    kind: Optional[str] = Query(None, description="host | service | product | cve | credential | ..."),
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    eng = _require_engagement(engagement)
    rows = _store().graph_entities(eng, kind=kind, limit=limit)
    return {"engagement": eng, "count": len(rows), "entities": rows}


@app.get("/graph/stats")
def graph_stats(
    engagement: Optional[str] = Query(None, description="Required: memory is scoped per engagement"),
) -> dict[str, Any]:
    eng = _require_engagement(engagement)
    return {"engagement": eng, **_store().graph.stats(eng)}


@app.post("/backfill")
def backfill(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Rebuild the derived indexes (vectors + graph) from stored memory.

    Idempotent: it only ever writes derived rows, so it is safe to run against a
    store written by an earlier version. An optional ``engagement`` limits the
    work; without one, every engagement is rebuilt.
    """
    engagement = payload.get("engagement")
    if engagement is not None:
        engagement = _require_engagement(engagement)
    return _store().backfill_derived(engagement=engagement)


# ------------------------------------------------------------ engagements
@app.get("/engagements")
def engagements() -> dict[str, Any]:
    rows = _store().engagements()
    return {"count": len(rows), "engagements": rows}
