"""L6 memory wiring for the bridge.

Kept in its own module so ``bridge.py`` does not import the memory package
unless memory is actually configured. The whole point of this layer being
optional is that a deployment without it behaves exactly as before; an import
at module scope would make the memory package a hard dependency of the bridge,
which is the opposite of that intent.
"""
from __future__ import annotations

import os
from typing import Any, Optional


def default_engagement() -> str:
    """The engagement a bridge belongs to when none is configured."""
    return os.environ.get("MEMORY_ENGAGEMENT", "default")


def get_bridge_memory() -> Optional[Any]:
    """Return a memory store for the bridge, or ``None`` when disabled.

    Controlled by ``MEMORY_ENABLED`` (default off) and ``MEMORY_DB``
    (default a local SQLite file next to the runtime). Memory is off by
    default so that existing deployments and the test suite are unaffected
    unless someone opts in.
    """
    if os.environ.get("MEMORY_ENABLED", "0") not in ("1", "true", "yes"):
        return None
    try:
        from memory_store import MemoryStore
    except ImportError:
        return None
    return MemoryStore(os.environ.get("MEMORY_DB", "memory.db"))


def recall_limit() -> int:
    """How many recalled records to hand a crew. Bounded, because this text is
    pasted into a prompt and an unbounded recall would crowd out the card."""
    try:
        return max(1, int(os.environ.get("MEMORY_RECALL_LIMIT", "6")))
    except ValueError:
        return 6


def _query_for(card: Optional[dict[str, Any]], target: Optional[str]) -> str:
    """The text used to retrieve relevant memory for a card.

    Built from the card's own words rather than from the engagement as a whole:
    a query made of the card's title, description and target is what makes recall
    *relevant* instead of merely recent. The target is appended because a card
    titled "enumerate shares" says nothing about which host it concerns.
    """
    parts: list[str] = []
    if card:
        parts.append(str(card.get("title") or ""))
        parts.append(str(card.get("description") or ""))
    if target:
        parts.append(str(target))
    query = " ".join(p for p in parts if p).strip()
    return query or (target or "")


def recall_for_card(
    store: Any,
    engagement: str,
    *,
    card_id: Optional[str] = None,
    target: Optional[str] = None,
    card: Optional[dict[str, Any]] = None,
    limit: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Retrieve the memory a crew should start from, in one bundle.

    Three things come back, and they are deliberately not the same thing:

    * ``context`` - the bounded history and standing facts, i.e. what the old
      ``/context`` read produced. Kept because it is what makes a run aware of
      its own prior attempts.
    * ``used`` - the records **recalled by similarity** to this card's own text.
      This is the part the old path could not do: a relevant record from an
      earlier, differently-worded card would never have been surfaced.
    * ``graph`` - the neighbourhood around the top hit, so the crew also learns
      what the relevant record is *connected to*.

    Any single retrieval failing must not lose the others: a store whose vector
    index is broken should still hand over recent history. Every sub-call is
    therefore isolated and reported in ``errors`` rather than raised.
    """
    if store is None or not engagement:
        return None

    limit = limit if limit is not None else recall_limit()
    query = _query_for(card, target)
    errors: list[str] = []

    def _safe(name: str, fn: Any, default: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one retrieval must not sink the rest
            errors.append(f"{name}: {exc}")
            return default

    context = _safe(
        "context",
        lambda: store.context(engagement, card_id=card_id, target=target),
        None,
    )
    used: list[dict[str, Any]] = []
    backend = "unknown"
    if query:
        hits = _safe("recall", lambda: store.hybrid(engagement, query, limit=limit), [])
        used = [h.model_dump() for h in (hits or [])]
        try:
            from memory_store.vector import vector_backend

            backend = vector_backend()
        except Exception:  # noqa: BLE001 - reporting must never break retrieval
            backend = "unknown"

    graph: dict[str, Any] = {"nodes": [], "edges": []}
    graph_seed: Optional[str] = None
    if query:
        bundle = _safe(
            "graph",
            lambda: store.retrieval_bundle(engagement, query, limit=limit),
            None,
        )
        if bundle:
            graph = bundle.get("graph") or graph
            graph_seed = bundle.get("graph_seed")

    prompt_block = _render(
        engagement=engagement,
        context=context,
        used=used,
        graph=graph,
        graph_seed=graph_seed,
        errors=errors,
    )

    return {
        "engagement": engagement,
        "query": query,
        "backend": backend,
        "hits": len(used),
        "used": used,
        "graph_seed": graph_seed,
        "graph": graph,
        "context": context,
        "errors": errors,
        "prompt_block": prompt_block,
    }


def _render(
    *,
    engagement: str,
    context: Any,
    used: list[dict[str, Any]],
    graph: dict[str, Any],
    graph_seed: Optional[str],
    errors: list[str],
    max_chars: int = 4000,
) -> str:
    """Render the bundle as a bounded prompt section.

    Recall comes first, before the standing history: the retrieved records are
    the ones chosen for *this* card, and a model reading top-down should meet
    the most relevant material first. The history then provides the "what have we
    already tried" that affinity ranking cannot express.
    """
    lines: list[str] = [f"ENGAGEMENT MEMORY ({engagement}) - retrieved for this card"]

    if used:
        lines.append("\nMost relevant prior records (semantic + lexical recall):")
        for hit in used:
            kind = "fact" if hit.get("kind") == "semantic" else "event"
            record = hit.get("record") or {}
            how = record.get("retrieval", "recall")
            lines.append(
                f"  - [{kind} {float(hit.get('score') or 0.0):.3f} {how}] {hit.get('summary', '')}"
            )
    elif not errors:
        lines.append("\nMost relevant prior records: (none - new engagement)")

    if graph_seed:
        nodes = [n.get("name") for n in (graph.get("nodes") or []) if n.get("name")][:12]
        edges = graph.get("edges") or []
        lines.append(f"\nKnowledge graph around '{graph_seed}':")
        lines.append(f"  entities: {', '.join(nodes) if nodes else '(none)'}")
        if edges:
            lines.append(f"  relations: {len(edges)}")

    block = getattr(context, "as_prompt_block", None)
    if callable(block):
        try:
            lines.append("\n" + block())
        except Exception:  # noqa: BLE001
            pass

    if errors:
        # Surfaced, not hidden: a partially-retrieved context is materially
        # different from a complete one, and the crew should know which it got.
        lines.append("\nRetrieval notes: " + "; ".join(errors))

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n  ... (truncated)"
    return text


#: Kept as a module-level alias so ``_default_engagement`` resolves in bridge.py.
def _default_engagement() -> str:
    return default_engagement()
