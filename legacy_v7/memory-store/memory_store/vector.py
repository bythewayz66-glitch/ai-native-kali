"""Vector recall for the L6 memory store.

Workstream A, Phase 3. Blueprint ref: section 03, layer L6.

Why a hashing vectoriser and not an embedding model
---------------------------------------------------
The obvious implementation calls out to an embedding endpoint. That would be the
wrong default here, for the same reason the tool layer defaults to dry-run: the
memory store is on the critical path for *every* crew run, and a memory store
that refuses to start without a reachable model turns an optional capability
into a hard dependency.

So the embedding is a **deterministic hashing vectoriser**: tokens are hashed
into a fixed-dimension space and the vector is L2-normalised. Cosine similarity
between two of them is a real lexical similarity measure.

What that means, stated honestly:

* It matches vocabulary, not meaning. ``"open port 445"`` and ``"SMB exposed"``
  score low against each other even though a human would connect them. A
  transformer embedding would; this does not.
* It is **deterministic**, which is what makes recall testable with a fixed
  expectation instead of a threshold someone tuned until the test passed.
* It needs **no dependency** - no numpy, no model, no network - so it works on a
  live ISO, in CI, and offline.

``Embedder`` is defined as the seam: an Ollama embedding endpoint drops in
behind the same three functions without touching the store, and ``backend()``
reports which is in use so a deployment cannot be mistaken about it.

Why recall is separate from ``search()``
----------------------------------------
``MemoryStore.search()`` is FTS5/BM25 - exact terms, ranked by term frequency.
It cannot find a record when the query shares no literal token with it. Vector
recall is the complement: it ranks everything by similarity, including records
with no term overlap. ``MemoryStore.hybrid()`` fuses the two, because either one
alone has a failure mode the other covers.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional, Protocol

from .models import SearchHit

#: Embedding dimension. 384 is the usual small-transformer width, which keeps
#: the vector comparable in size to a transformer's without the model. Collision
#: pressure at this width is low for the vocabulary of a security engagement.
DIM = 384

#: Filler that carries no retrieval signal. Kept short deliberately - dropping
#: too much turns document vectors into bag-of-nouns and flattens the ranking.
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by for from had has have he her his i if in
    into is it its me my no not of on or our out she so than that the their them
    then there these they this to too was we were what when where which who will
    with would you your
    """.split()
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-:]*")


def tokenize(text: str) -> list[str]:
    """Lowercased content tokens, stopwords removed."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        token = raw.lower().strip("._-:")
        if not token or token in _STOPWORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        out.append(token)
    return out


def _shingles(token: str, size: int = 3) -> list[str]:
    """Character n-grams of a token.

    Included at a lower weight so ``smb`` and ``smbv2``, or ``example.net`` and
    ``example.com``, retain some similarity - the near-misses a pure bag of
    words scores at zero.
    """
    if len(token) < size:
        return []
    return [token[i : i + size] for i in range(len(token) - size + 1)]


def _bucket(feature: str, dim: int) -> tuple[int, float]:
    """Hash a feature to (index, sign).

    The sign comes from a second, independent hash bit. Without it every
    collision *adds* and the vector drifts toward a positive blob that makes all
    documents look alike; with it collisions cancel in expectation.
    """
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


def embed(text: str, *, dim: int = DIM) -> list[float]:
    """Deterministic hashing embedding, L2-normalised.

    Unigrams carry the signal; character shingles (weight 0.35) add tolerance to
    near-miss tokens. Sublinear term weighting (``1 + log tf``) stops a repeated
    word from dominating a long document - the same correction TF-IDF makes.
    """
    tokens = tokenize(text)
    if not tokens:
        return [0.0] * dim

    counts: dict[str, float] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0.0) + 1.0
        for shingle in _shingles(token):
            key = f"#{shingle}"
            counts[key] = counts.get(key, 0.0) + 0.35

    vec = [0.0] * dim
    for feature, tf in counts.items():
        weight = 1.0 + math.log(tf)
        index, sign = _bucket(feature, dim)
        vec[index] += sign * weight

    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:  # pragma: no cover - only if every feature cancelled
        return [0.0] * dim
    return [v / norm for v in vec]


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    """Cosine similarity of two embeddings.

    Both sides are already unit-length, so this is a dot product - but the norms
    are computed anyway, because a vector loaded from storage may have been
    written by a different ``dim`` or hand-edited, and silently returning a
    number above 1.0 would be worse than paying for two square roots.
    """
    av, bv = list(a), list(b)
    if len(av) != len(bv) or not av:
        return 0.0
    dot = sum(x * y for x, y in zip(av, bv))
    na = math.sqrt(sum(x * x for x in av))
    nb = math.sqrt(sum(y * y for y in bv))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


class Embedder(Protocol):
    """The seam for swapping in a real embedding model.

    A model-backed implementation returns the same shape - a list of floats of
    fixed dimension - so nothing downstream changes. ``vector_backend()`` on the
    store reports which is live.
    """

    name: str
    dim: int

    def encode(self, text: str) -> list[float]:  # pragma: no cover - protocol
        ...


class HashingEmbedder:
    """The default, dependency-free embedder."""

    name = "hashing-blake2b"
    dim = DIM

    def encode(self, text: str) -> list[float]:
        return embed(text, dim=DIM)


_DEFAULT_EMBEDDER: Embedder = HashingEmbedder()


class OllamaEmbedder:
    """Embedding model behind an Ollama-compatible ``/api/embeddings`` endpoint.

    The point of this class is not that it speaks HTTP - it is that it
    **degrades instead of failing**. A memory store sits on the critical path of
    every crew run, so an unreachable embedding endpoint must never be able to
    stop a card. On the first failure the embedder latches to the deterministic
    hashing vector and reports itself as degraded, so the caller always receives
    a usable vector *and* an explicit statement of which implementation produced
    it (``describe()`` / ``active_embedder().degraded``).

    Latching is deliberate. Retrying a dead endpoint once per record would turn
    a fast write path into a wall of timeouts, and the card would appear blocked
    by a dependency the operator can find nothing wrong with. One failure, one
    latch, a clearly-labelled fallback - and ``reset()`` to try again.
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://127.0.0.1:11434",
        *,
        timeout: float = 5.0,
        fallback: Optional[Embedder] = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/api/embeddings"
        self.timeout = timeout
        #: Reported dim starts at the hashing width and is corrected to whatever
        #: the endpoint actually returns on first success.
        self.dim = DIM
        self._fallback = fallback or HashingEmbedder()
        self._degraded = False
        self.last_error: Optional[str] = None
        self.calls = 0
        self.degraded_calls = 0
        self._lock = threading.RLock()

    @property
    def name(self) -> str:
        """Stable identifier. Used as the ``backend`` column value, so it must
        not change with health - degradation is reported separately."""
        return f"ollama:{self.model}"

    @property
    def degraded(self) -> bool:
        return self._degraded

    def reset(self) -> None:
        """Clear the degraded latch so the endpoint is attempted again."""
        with self._lock:
            self._degraded = False
            self.last_error = None

    def describe(self) -> dict[str, Any]:
        """Which implementation is *actually* answering, stated plainly."""
        return {
            "backend": self.name,
            "model": self.model,
            "endpoint": self.endpoint,
            "degraded": self._degraded,
            "fallback": self._fallback.name,
            "dim": self.dim,
            "calls": self.calls,
            "degraded_calls": self.degraded_calls,
            "last_error": self.last_error,
        }

    def _degrade(self, exc: Exception) -> None:
        with self._lock:
            if not self._degraded:
                self._degraded = True
                self.last_error = f"{type(exc).__name__}: {exc}"

    def encode(self, text: str) -> list[float]:
        with self._lock:
            self.calls += 1
            if self._degraded:
                self.degraded_calls += 1
                return self._fallback.encode(text)
        try:
            body = json.dumps({"model": self.model, "prompt": text or ""}).encode("utf-8")
            request = urllib.request.Request(
                self.endpoint, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            vector = payload.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise ValueError("response contained no 'embedding' list")
            vector = [float(v) for v in vector]
            with self._lock:
                self.dim = len(vector)
            return vector
        except Exception as exc:  # noqa: BLE001 - any failure falls back, never raises
            self._degrade(exc)
            with self._lock:
                self.degraded_calls += 1
            return self._fallback.encode(text)

    def available(self) -> bool:
        """One-shot probe: True when the endpoint answers with a usable vector.

        Clears the latch first, so this is also how an operator re-checks a
        service that has come back up.
        """
        self.reset()
        self.encode("probe")
        return not self._degraded


#: The process-wide embedder. ``None`` means "not resolved yet"; it is filled
#: from the environment on first use and can be swapped explicitly (tests,
#: ``set_embedder``) without the environment overwriting the choice afterwards.
_ACTIVE: Optional[Embedder] = None
_EMBEDDER_LOCK = threading.RLock()


def load_embedder_from_env(env: Optional[dict[str, str]] = None) -> Embedder:
    """Choose an embedder from the environment.

    Phase 7, item 5 made ``auto`` available, and it is the recommended setting on
    an image that *may* have a model endpoint:

    * ``MEMORY_EMBEDDER=auto`` (recommended) - probe the endpoint once and use the
      semantic backend when it answers, else the deterministic hashing
      vectoriser. This is what lets a booted ISO with a bundled model use it
      without configuration, while the same image offline still works.
    * ``MEMORY_EMBEDDER=hashing`` - force the deterministic vectoriser.
    * ``MEMORY_EMBEDDER=ollama`` - force the model endpoint, even if it will
      degrade. Use when the deployment *requires* semantic recall and wants a
      missing endpoint to be visible on ``/embedder`` rather than silently
      swapped.

    An unrecognised value remains a **loud configuration error**, not a silent
    fallback: a deployment that asked for a model and quietly got the hashing
    vectoriser would have no way to tell its embeddings were lexical.

    ``auto`` probes with a short timeout so a dead endpoint costs the caller a
    moment at start-up rather than a hang on the critical path.
    """
    env = env if env is not None else dict(os.environ)
    choice = (env.get("MEMORY_EMBEDDER") or "auto").strip().lower()
    if choice in ("hashing", "hashing-blake2b", "blake2b", "local"):
        return HashingEmbedder()
    if choice in ("ollama", "model"):
        return OllamaEmbedder(
            model=env.get("MEMORY_EMBED_MODEL", "nomic-embed-text"),
            base_url=env.get("MEMORY_EMBED_URL", "http://127.0.0.1:11434"),
            timeout=float(env.get("MEMORY_EMBED_TIMEOUT", "5")),
        )
    if choice in ("auto", "automatic"):
        candidate = OllamaEmbedder(
            model=env.get("MEMORY_EMBED_MODEL", "nomic-embed-text"),
            base_url=env.get("MEMORY_EMBED_URL", "http://127.0.0.1:11434"),
            # Short: this runs on the critical path at first use, and "no endpoint"
            # is an answer, not something to wait on.
            timeout=float(env.get("MEMORY_EMBED_PROBE_TIMEOUT", "1.5")),
        )
        # ``available()`` is the real probe (it clears the latch and tries an
        # encode). Guarded because an unexpected error must select the fallback,
        # never propagate: auto-selection runs before the store can report health.
        try:
            reachable = bool(candidate.available())
        except Exception:  # noqa: BLE001
            reachable = False
        return candidate if reachable else HashingEmbedder()
    raise ValueError(
        f"MEMORY_EMBEDDER={choice!r} is not a known backend (use 'auto', 'hashing' or 'ollama')"
    )


def active_embedder() -> Embedder:
    """The embedder everything should use, resolved from the environment once."""
    global _ACTIVE
    with _EMBEDDER_LOCK:
        if _ACTIVE is None:
            _ACTIVE = load_embedder_from_env()
        return _ACTIVE


def set_embedder(embedder: Embedder) -> Embedder:
    """Swap the process-wide embedder; returns the previous one.

    Deliberately sticky: once set explicitly, the environment is no longer
    consulted, so a test (or an operator script) cannot be overruled by an
    ambient variable. Use ``reset_embedder()`` to go back to env-driven choice.
    """
    global _ACTIVE
    with _EMBEDDER_LOCK:
        previous = active_embedder()
        _ACTIVE = embedder
    return previous


def reset_embedder() -> None:
    """Forget the resolved choice so the next use re-reads the environment."""
    global _ACTIVE
    with _EMBEDDER_LOCK:
        _ACTIVE = None


def embedder_report() -> dict[str, Any]:
    """A machine-readable statement of which embedder is live and its health."""
    impl = active_embedder()
    if hasattr(impl, "describe"):
        return impl.describe()
    return {"backend": impl.name, "dim": getattr(impl, "dim", DIM), "degraded": False}


def vector_backend() -> str:
    """Stable identifier of the embedder in use."""
    return active_embedder().name


class VectorIndex:
    """Persistent vector index over episodes and facts.

    Vectors live in their own table keyed by record id, so the index can be
    dropped, rebuilt and back-filled without ever writing to the episodic or
    semantic tables. That separation is what lets ``backfill()`` be safe: it
    reads memory and writes only derived data.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: Optional[threading.RLock] = None,
        *,
        embedder: Optional[Embedder] = None,
    ) -> None:
        self._conn = conn
        self._lock = lock or threading.RLock()
        self._embedder = embedder or active_embedder()
        self._migrate()

    # -- schema ----------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                  record_id   TEXT PRIMARY KEY,
                  kind        TEXT NOT NULL,
                  engagement  TEXT NOT NULL,
                  ts          TEXT NOT NULL,
                  dim         INTEGER NOT NULL,
                  backend     TEXT NOT NULL,
                  vector      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vec_eng  ON embeddings(engagement, kind);
                CREATE INDEX IF NOT EXISTS idx_vec_kind ON embeddings(kind);
                """
            )
            self._conn.commit()

    # -- writes ----------------------------------------------------------
    def upsert(
        self,
        record_id: str,
        kind: str,
        engagement: str,
        text: str,
        *,
        ts: Optional[str] = None,
    ) -> bool:
        """Embed and store one record. Returns True when a vector was written."""
        if not record_id or not engagement:
            return False
        vec = self._embedder.encode(text or "")
        if not any(vec):
            return False
        with self._lock:
            self._conn.execute(
                """INSERT INTO embeddings (record_id,kind,engagement,ts,dim,backend,vector)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(record_id) DO UPDATE SET
                     kind=excluded.kind,
                     engagement=excluded.engagement,
                     ts=excluded.ts,
                     dim=excluded.dim,
                     backend=excluded.backend,
                     vector=excluded.vector""",
                (
                    record_id,
                    kind,
                    engagement,
                    ts or "",
                    len(vec),
                    getattr(self._embedder, "name", "unknown"),
                    json.dumps([round(v, 6) for v in vec]),
                ),
            )
            self._conn.commit()
        return True

    def delete(self, record_id: str) -> bool:
        """Remove a record's vector (used when a fact is retracted)."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM embeddings WHERE record_id=?", (record_id,))
            self._conn.commit()
        return bool(cur.rowcount)

    # -- reads -----------------------------------------------------------
    def search(
        self,
        engagement: str,
        query: str,
        *,
        kinds: Optional[list[str]] = None,
        top_k: int = 10,
        min_score: float = 0.01,
    ) -> list[SearchHit]:
        """Rank an engagement's memory by cosine similarity to *query*.

        Every record is scored - there is no approximate index - because an
        engagement holds thousands of records, not millions, and a **complete**
        ranking means recall cannot silently miss a relevant record the way an
        ANN index can. When that stops being true the trade-off changes, and the
        docstring in ``docs/MEMORY_STORE.md`` records that this is the reason.
        """
        if not engagement:
            raise ValueError("engagement is required: unscoped recall is a leak")
        qvec = self._embedder.encode(query or "")
        if not any(qvec):
            return []

        rows = self._candidates(engagement, kinds)
        scored: list[SearchHit] = []
        for row in rows:
            try:
                vec = json.loads(row["vector"])
            except (ValueError, TypeError):
                continue  # a corrupt vector must not fail the whole query
            score = cosine(qvec, vec)
            if score < min_score:
                continue
            scored.append(
                SearchHit(
                    kind=row["kind"],  # type: ignore[arg-type]
                    id=row["record_id"],
                    engagement=row["engagement"],
                    ts=row["ts"] or "",
                    summary=row["summary"] or "",
                    score=score,
                    record={
                        "record_id": row["record_id"],
                        "kind": row["kind"],
                        "backend": row["backend"],
                        "retrieval": "vector",
                        # Carried so a caller can seed a graph walk from the
                        # record it just retrieved (see MemoryStore.retrieval_bundle).
                        "target": row["target"],
                        "key": row["key"],
                    },
                )
            )
        scored.sort(key=lambda h: -h.score)
        return scored[:top_k]

    def _candidates(self, engagement: str, kinds: Optional[list[str]]) -> list[sqlite3.Row]:
        sql = """
            SELECT e.record_id, e.kind, e.engagement, e.ts, e.vector, e.backend,
                   COALESCE(ep.summary, f.statement) AS summary,
                   COALESCE(ep.target, f.target)     AS target,
                   f.key                             AS key
              FROM embeddings e
              LEFT JOIN episodes ep ON e.kind='episodic' AND ep.episode_id = e.record_id
              LEFT JOIN facts    f  ON e.kind='semantic' AND f.fact_id     = e.record_id
             WHERE e.engagement = ?
        """
        params: list[Any] = [engagement]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND e.kind IN ({placeholders})"
            params.extend(kinds)
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def count(self, engagement: Optional[str] = None, kind: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM embeddings WHERE 1=1"
        params: list[Any] = []
        if engagement:
            sql += " AND engagement=?"
            params.append(engagement)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def backfill(
        self,
        store: Any,
        *,
        engagement: Optional[str] = None,
        reembed_drift: bool = True,
    ) -> int:
        """Embed any episode or fact whose stored vector is missing or stale.

        Safe to run repeatedly: it only ever adds derived rows, so a partial run
        resumes rather than restarting.

        ``reembed_drift`` decides what happens to a record whose vector came from
        a *different* backend. With it on (the default) those records are
        re-embedded with the active backend, which is the whole point of the flag
        being on by default: after ``MEMORY_EMBEDDER`` changes, a vector from the
        old embedder is geometrically meaningless to the new one, so recall
        quietly returns nothing while every count stays healthy. Turning it off
        keeps the existing vectors and the drift is then visible on ``stats()``.
        """
        written = 0
        targets: list[tuple[str, str, str, str, str]] = []

        def _collect(eng: str) -> None:
            for ep in store.episodes(eng, limit=10_000):
                targets.append(
                    (ep.episode_id, "episodic", eng, ep.summary + " " + (ep.detail or ""), ep.ts)
                )
            for fact in store.facts(eng, status="any", limit=10_000):
                if fact.status != "active":
                    continue
                targets.append(
                    (fact.fact_id, "semantic", eng, fact.statement + " " + (fact.detail or ""), fact.ts)
                )

        if engagement:
            _collect(engagement)
        else:
            for row in store.engagements():
                _collect(row["engagement"])

        # Which backend each record was last embedded with. Comparing this
        # against the active backend is what makes a backend swap actually work:
        # a vector from one embedder is geometrically meaningless to another, so
        # without re-embedding, recall after a swap silently drops to zero while
        # every count still looks healthy.
        existing = self._existing_backends()
        for record_id, kind, eng, text, ts in targets:
            stored = existing.get(record_id)
            if stored == self._embedder.name:
                continue  # already embedded with the active backend
            if stored is not None and not reembed_drift:
                continue  # old backend, and drift repair was switched off
            if self.upsert(record_id, kind, eng, text, ts=ts):
                written += 1
        return written

    def _existing_backends(self) -> dict[str, str]:
        """record_id -> the backend its stored vector came from."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_id, backend FROM embeddings"
            ).fetchall()
        return {row["record_id"]: row["backend"] for row in rows}

    def backend_counts(self) -> dict[str, int]:
        """How many vectors came from each backend. Mixed values are visible."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT backend, COUNT(*) AS n FROM embeddings GROUP BY backend"
            ).fetchall()
        return {row["backend"]: int(row["n"]) for row in rows}

    def drift(self) -> int:
        """Vectors embedded with a backend other than the active one.

        Non-zero means recall is degraded until ``backfill()`` re-embeds. This is
        surfaced rather than left implicit because the failure is invisible from
        the outside - row counts and health checks all stay green.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM embeddings WHERE backend != ?",
                (getattr(self._embedder, "name", "unknown"),),
            ).fetchone()
        return int(row["n"]) if row else 0

    def stats(self) -> dict[str, Any]:
        """Vector-index health, including which embedder is actually answering.

        ``drift`` is the field to watch after changing ``MEMORY_EMBEDDER``: every
        other number here can look perfect while recall returns nothing.
        """
        return {
            "vectors": self.count(),
            "backend": getattr(self._embedder, "name", "unknown"),
            "backend_report": (
                self._embedder.describe()
                if hasattr(self._embedder, "describe")
                else {"backend": getattr(self._embedder, "name", "unknown"), "degraded": False}
            ),
            "backends": self.backend_counts(),
            "drift": self.drift(),
            "dim": getattr(self._embedder, "dim", DIM),
        }
