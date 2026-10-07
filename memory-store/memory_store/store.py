"""SQLite-backed memory store with full-text search.

Blueprint ref: section 03, layer L6.

Design notes that matter:

* **One database, two tables, two FTS indexes.** Episodic and semantic memory
  share a connection but never a table, so an episode can never be mistaken for
  a fact.
* **FTS5 with an external-content fallback.** FTS5 ships with CPython's bundled
  SQLite on every platform this project targets, but if a build lacks it the
  store degrades to a `LIKE` scan rather than refusing to start. A memory store
  that cannot boot is worse than a slow one.
* **Engagement scoping is mandatory, not a filter.** Every read takes an
  ``engagement`` and applies it in SQL. There is deliberately no
  ``search_all_engagements()`` method - the only way to cross a scope boundary
  is an explicit ``cross_engagement=True`` on the two admin endpoints, which is
  what the tests assert is impossible to do by accident.
* **Facts are superseded, not deleted.** History is the point.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from .graph import GraphIndex
from .models import ContextBundle, Episode, Fact, SearchHit, new_id, utcnow
from .vector import VectorIndex, vector_backend

_LOCK = threading.RLock()

#: FTS5 operators. They must never survive into a generated MATCH expression:
#: quoting neutralises them, but a literal search for the word "AND" is still
#: noise, so they are dropped outright.
_FTS_RESERVED = frozenset({"and", "or", "not", "near"})


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    FTS5 treats ``"``, ``*``, ``(``, ``-`` and friends as syntax, so raw user
    input can either raise or (worse) be silently misinterpreted. Tokens are
    reduced to alphanumerics, reserved operators and one-character fragments are
    dropped, and the rest are OR-ed together - which matches the intent of a
    natural-language query without ever being parsed as an operator.
    """
    tokens = re.findall(r"[A-Za-z0-9_]+", text or "")
    tokens = [t for t in tokens if len(t) > 1 and t.lower() not in _FTS_RESERVED]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:24])


class MemoryStore:
    """Episodic + semantic memory, scoped per engagement."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.fts = False
        self._migrate()
        # Phase 3 (Workstream A): the two derived retrieval indexes.
        #
        # Both own their own tables and are built from this connection, so they
        # hold **derived** data only - nothing here is a second source of truth.
        # Everything in them can be rebuilt from ``episodes`` and ``facts`` with
        # ``backfill()``, which is what makes it safe to add them to an existing
        # store: an older database starts with empty indexes and fills as it is
        # written to.
        self.vectors = VectorIndex(self._conn, _LOCK)
        self.graph = GraphIndex(self._conn, _LOCK)
        #: Non-fatal failures from the derived indexes. An embedding or graph
        #: extraction error must never fail a legitimate memory write, so it is
        #: recorded here and surfaced on /health instead of raised.
        self.derived_errors: list[str] = []

    # -- schema ----------------------------------------------------------
    def _migrate(self) -> None:
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                  episode_id  TEXT PRIMARY KEY,
                  ts          TEXT NOT NULL,
                  engagement  TEXT NOT NULL,
                  kind        TEXT NOT NULL,
                  card_id     TEXT,
                  agent       TEXT,
                  board_id    TEXT,
                  target      TEXT,
                  summary     TEXT NOT NULL,
                  detail      TEXT DEFAULT '',
                  data        TEXT DEFAULT '{}',
                  tags        TEXT DEFAULT '[]',
                  supersedes  TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_ep_eng   ON episodes(engagement, ts);
                CREATE INDEX IF NOT EXISTS idx_ep_card  ON episodes(card_id);
                CREATE INDEX IF NOT EXISTS idx_ep_trg   ON episodes(engagement, target);
                CREATE INDEX IF NOT EXISTS idx_ep_kind  ON episodes(engagement, kind);

                CREATE TABLE IF NOT EXISTS facts (
                  fact_id     TEXT PRIMARY KEY,
                  ts          TEXT NOT NULL,
                  updated_at  TEXT NOT NULL,
                  engagement  TEXT NOT NULL,
                  key         TEXT NOT NULL,
                  statement   TEXT NOT NULL,
                  detail      TEXT DEFAULT '',
                  confidence  REAL DEFAULT 0.5,
                  status      TEXT DEFAULT 'active',
                  source_card TEXT,
                  agent       TEXT,
                  target      TEXT,
                  tags        TEXT DEFAULT '[]'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_key
                  ON facts(engagement, key) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_fact_eng ON facts(engagement, confidence);
                """
            )
            self._conn.commit()
            self._migrate_fts()

    def _migrate_fts(self) -> None:
        """Create FTS5 mirrors when available; otherwise fall back to LIKE."""
        try:
            self._conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
                  episode_id UNINDEXED, engagement UNINDEXED,
                  summary, detail, tags, target
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                  fact_id UNINDEXED, engagement UNINDEXED,
                  statement, detail, key, tags, target
                );
                """
            )
            self._conn.commit()
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    def close(self) -> None:
        with _LOCK:
            self._conn.close()

    # -- writes ----------------------------------------------------------
    def record(
        self,
        *,
        engagement: str,
        summary: str,
        kind: str = "observation",
        detail: str = "",
        card_id: Optional[str] = None,
        agent: Optional[str] = None,
        board_id: Optional[str] = None,
        target: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
        tags: Optional[list[str]] = None,
        supersedes: Optional[str] = None,
        episode_id: Optional[str] = None,
    ) -> Episode:
        """Append one episode. Never edits an existing record."""
        if not engagement:
            raise ValueError("engagement is required: unscoped memory is a leak")
        ep = Episode(
            episode_id=episode_id or new_id("epi"),
            engagement=engagement,
            kind=kind,  # type: ignore[arg-type]
            card_id=card_id,
            agent=agent,
            board_id=board_id,
            target=target,
            summary=summary,
            detail=detail,
            data=data or {},
            tags=tags or [],
            supersedes=supersedes,
        )
        with _LOCK:
            self._conn.execute(
                """INSERT INTO episodes
                     (episode_id,ts,engagement,kind,card_id,agent,board_id,target,
                      summary,detail,data,tags,supersedes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ep.episode_id, ep.ts, ep.engagement, ep.kind, ep.card_id, ep.agent,
                    ep.board_id, ep.target, ep.summary, ep.detail,
                    json.dumps(ep.data, default=str), json.dumps(ep.tags), ep.supersedes,
                ),
            )
            if self.fts:
                self._conn.execute(
                    "INSERT INTO episodes_fts (episode_id,engagement,summary,detail,tags,target)"
                    " VALUES (?,?,?,?,?,?)",
                    (ep.episode_id, ep.engagement, ep.summary, ep.detail,
                     " ".join(ep.tags), ep.target or ""),
                )
            self._conn.commit()
        self._index_episode(ep)
        return ep

    def assert_fact(
        self,
        *,
        engagement: str,
        key: str,
        statement: str,
        detail: str = "",
        confidence: float = 0.5,
        source_card: Optional[str] = None,
        agent: Optional[str] = None,
        target: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> Fact:
        """Record a distilled fact, superseding any active fact with the same key.

        Re-asserting a key is the normal case - a second scan refreshes what we
        know - so the old row is marked ``superseded`` rather than overwritten.
        The previous statement survives for the audit trail.
        """
        if not engagement:
            raise ValueError("engagement is required: unscoped memory is a leak")
        if not (0.0 <= confidence <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")

        with _LOCK:
            previous = self._conn.execute(
                "SELECT fact_id FROM facts WHERE engagement=? AND key=? AND status='active'",
                (engagement, key),
            ).fetchone()
            if previous:
                self._conn.execute(
                    "UPDATE facts SET status='superseded', updated_at=? WHERE fact_id=?",
                    (utcnow(), previous["fact_id"]),
                )
                if self.fts:
                    self._conn.execute(
                        "DELETE FROM facts_fts WHERE fact_id=?", (previous["fact_id"],)
                    )
            fact = Fact(
                engagement=engagement,
                key=key,
                statement=statement,
                detail=detail,
                confidence=confidence,
                source_card=source_card,
                agent=agent,
                target=target,
                tags=tags or [],
            )
            self._conn.execute(
                """INSERT INTO facts
                     (fact_id,ts,updated_at,engagement,key,statement,detail,confidence,
                      status,source_card,agent,target,tags)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fact.fact_id, fact.ts, fact.updated_at, fact.engagement, fact.key,
                    fact.statement, fact.detail, fact.confidence, fact.status,
                    fact.source_card, fact.agent, fact.target, json.dumps(fact.tags),
                ),
            )
            if self.fts:
                self._conn.execute(
                    "INSERT INTO facts_fts (fact_id,engagement,statement,detail,key,tags,target)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (fact.fact_id, fact.engagement, fact.statement, fact.detail,
                     fact.key, " ".join(fact.tags), fact.target or ""),
                )
            self._conn.commit()
        self._index_fact(fact)
        return fact

    def retract_fact(self, fact_id: str, *, reason: str = "retracted") -> bool:
        """Mark a fact retracted. The row stays; only its status changes."""
        with _LOCK:
            cur = self._conn.execute(
                "UPDATE facts SET status='retracted', detail=detail||? , updated_at=?"
                " WHERE fact_id=? AND status='active'",
                (f"\n[retracted: {reason}]", utcnow(), fact_id),
            )
            if cur.rowcount and self.fts:
                self._conn.execute("DELETE FROM facts_fts WHERE fact_id=?", (fact_id,))
            self._conn.commit()
        if cur.rowcount:
            # A retracted fact must leave retrieval, not just the fact table:
            # otherwise a withdrawn claim keeps coming back from recall, which
            # is worse than never having retracted it. Its graph edges stay -
            # they are history, and the fact row that evidences them is intact.
            try:
                self.vectors.delete(fact_id)
            except Exception as exc:  # pragma: no cover - defensive
                self.derived_errors.append(f"vector delete {fact_id}: {exc}")
        return bool(cur.rowcount)

    # -- derived-index maintenance ---------------------------------------
    def _index_episode(self, episode: Episode) -> None:
        """Embed an episode and reflect it into the graph.

        Wrapped in a single guard on purpose: the derived indexes improve
        retrieval, and a failure in them must cost *retrieval quality*, never the
        memory write itself. A crew that cannot embed still records what it did.
        """
        try:
            self.vectors.upsert(
                episode.episode_id,
                "episodic",
                episode.engagement,
                f"{episode.summary} {episode.detail}",
                ts=episode.ts,
            )
            self.graph.observe_episode(episode)
        except Exception as exc:  # pragma: no cover - defensive
            self.derived_errors.append(f"index episode {episode.episode_id}: {exc}")

    def _index_fact(self, fact: Fact) -> None:
        """Embed a fact and reflect it into the graph."""
        try:
            self.vectors.upsert(
                fact.fact_id,
                "semantic",
                fact.engagement,
                f"{fact.statement} {fact.detail} {fact.key}",
                ts=fact.ts,
            )
            self.graph.observe_fact(fact)
        except Exception as exc:  # pragma: no cover - defensive
            self.derived_errors.append(f"index fact {fact.fact_id}: {exc}")

    # -- reads -----------------------------------------------------------
    def episodes(
        self,
        engagement: str,
        *,
        card_id: Optional[str] = None,
        target: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
        newest_first: bool = True,
    ) -> list[Episode]:
        """Read an engagement's history, oldest or newest first."""
        sql = "SELECT * FROM episodes WHERE engagement=?"
        params: list[Any] = [engagement]
        if card_id:
            sql += " AND card_id=?"
            params.append(card_id)
        if target:
            sql += " AND target=?"
            params.append(target)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY ts %s, rowid %s LIMIT ?" % (
            "DESC" if newest_first else "ASC",
            "DESC" if newest_first else "ASC",
        )
        params.append(limit)
        with _LOCK:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._episode(r) for r in rows]

    def facts(
        self,
        engagement: str,
        *,
        target: Optional[str] = None,
        status: str = "active",
        limit: int = 50,
    ) -> list[Fact]:
        """Active facts, most confident first."""
        sql = "SELECT * FROM facts WHERE engagement=?"
        params: list[Any] = [engagement]
        if status != "any":
            sql += " AND status=?"
            params.append(status)
        if target:
            sql += " AND target=?"
            params.append(target)
        sql += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        with _LOCK:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._fact(r) for r in rows]

    def get_fact(self, fact_id: str) -> Optional[Fact]:
        with _LOCK:
            row = self._conn.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        return self._fact(row) if row else None

    def search(
        self,
        engagement: str,
        query: str,
        *,
        kinds: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """Full-text search within one engagement.

        ``kinds`` selects which memory types to search: ``["episodic"]``,
        ``["semantic"]``, or both (the default).
        """
        kinds = kinds or ["episodic", "semantic"]
        hits: list[SearchHit] = []
        match = _fts_query(query)
        with _LOCK:
            if "episodic" in kinds:
                rows = self._search_table(
                    "episodes", "episodes_fts", "episode_id", engagement, match, query, limit
                )
                hits += [self._hit(r, "episodic", summary_key="summary") for r in rows]
            if "semantic" in kinds:
                rows = self._search_table(
                    "facts", "facts_fts", "fact_id", engagement, match, query, limit
                )
                hits += [self._hit(r, "semantic", summary_key="statement") for r in rows]
        hits.sort(key=lambda h: (-h.score, h.ts))
        return hits[:limit]

    def _search_table(
        self,
        table: str,
        fts_table: str,
        id_col: str,
        engagement: str,
        match: str,
        raw_query: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        if self.fts and match:
            try:
                return self._conn.execute(
                    f"""SELECT t.*, bm25({fts_table}) AS score
                        FROM {fts_table} f
                        JOIN {table} t ON t.{id_col} = f.{id_col}
                        WHERE {fts_table} MATCH ? AND f.engagement = ?
                        ORDER BY score LIMIT ?""",
                    (match, engagement, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                pass  # fall through to LIKE rather than failing the query
        needle = f"%{(raw_query or '').strip()}%"
        cols = "summary, detail" if table == "episodes" else "statement, detail, key"
        where = " OR ".join(f"{c} LIKE ?" for c in cols.split(", "))
        return self._conn.execute(
            f"SELECT *, 0.0 AS score FROM {table} WHERE engagement=? AND ({where}) LIMIT ?",
            [engagement, *[needle] * len(cols.split(", ")), limit],
        ).fetchall()

    # -- the read path crews actually use ---------------------------------
    def context(
        self,
        engagement: str,
        *,
        card_id: Optional[str] = None,
        target: Optional[str] = None,
        recent: int = 8,
        facts_limit: int = 12,
        episodes_limit: int = 10,
    ) -> ContextBundle:
        """Assemble the bounded memory bundle handed to a crew for one card."""
        if not engagement:
            raise ValueError("engagement is required: unscoped memory is a leak")

        recent_eps = self.episodes(engagement, limit=recent)
        active = self.facts(engagement, limit=facts_limit)

        card_eps: list[Episode] = []
        if card_id:
            card_eps = self.episodes(engagement, card_id=card_id, limit=episodes_limit, newest_first=False)

        target_eps: list[Episode] = []
        target_facts: list[Fact] = []
        if target:
            target_eps = self.episodes(engagement, target=target, limit=episodes_limit)
            target_facts = [
                f for f in self.facts(engagement, target=target, limit=facts_limit)
            ] or [f for f in active if f.target and f.target == target]

        total_eps = self.count_episodes(engagement)
        total_facts = len(active)
        return ContextBundle(
            engagement=engagement,
            card_id=card_id,
            target=target,
            recent_episodes=recent_eps,
            card_episodes=card_eps,
            facts=active,
            target_facts=target_facts,
            target_episodes=target_eps,
            counts={"episodes": total_eps, "facts": total_facts},
            truncated=total_eps > recent or total_facts > facts_limit,
        )

    def count_episodes(self, engagement: str) -> int:
        with _LOCK:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM episodes WHERE engagement=?", (engagement,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def engagements(self) -> list[dict[str, Any]]:
        """List known engagements with their memory volume.

        This is an inventory of scopes, not a read across them: it returns
        counts only, never content.
        """
        with _LOCK:
            rows = self._conn.execute(
                """SELECT e.engagement AS engagement,
                          (SELECT COUNT(*) FROM episodes x WHERE x.engagement=e.engagement) AS episodes,
                          (SELECT COUNT(*) FROM facts f
                            WHERE f.engagement=e.engagement AND f.status='active') AS facts
                   FROM (SELECT DISTINCT engagement FROM episodes
                         UNION SELECT DISTINCT engagement FROM facts) e
                   ORDER BY episodes DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with _LOCK:
            ep = self._conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]
            f_act = self._conn.execute(
                "SELECT COUNT(*) AS n FROM facts WHERE status='active'"
            ).fetchone()["n"]
            f_all = self._conn.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"]
        return {
            "episodes": int(ep),
            "facts_active": int(f_act),
            "facts_total": int(f_all),
            "facts_superseded": int(f_all - f_act),
            "fts": self.fts,
            "backend": "sqlite" + ("+fts5" if self.fts else "+like"),
            # Derived retrieval (Phase 3). Reported separately from the source
            # of truth above, so a caller can tell "the store holds this" from
            # "the store can find this by similarity".
            "vector": self.vectors.stats(),
            "graph": self.graph.stats(),
            "derived_index_errors": len(self.derived_errors),
        }

    # -- Phase 3 retrieval: vector, graph, and the fusion of both ---------
    #
    # Three retrieval paths now sit behind this store, and they are complements
    # rather than alternatives:
    #
    #   search()    FTS5/BM25   exact terms; cannot match without a shared token
    #   recall()    vector      similarity; finds records with no shared token,
    #                           but matches vocabulary rather than meaning
    #   hybrid()    both fused  fixes each one's blind spot
    #
    # All three take ``engagement`` and refuse to run without it, exactly like
    # the existing reads. A new retrieval path is not an excuse for a new way to
    # cross a scope boundary.
    def recall(
        self,
        engagement: str,
        query: str,
        *,
        kinds: Optional[list[str]] = None,
        top_k: int = 10,
        min_score: float = 0.01,
    ) -> list[SearchHit]:
        """Semantic-similarity recall within one engagement."""
        if not engagement:
            raise ValueError("engagement is required: unscoped recall is a leak")
        return self.vectors.search(
            engagement, query, kinds=kinds, top_k=top_k, min_score=min_score
        )

    def hybrid(
        self,
        engagement: str,
        query: str,
        *,
        kinds: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        """Fuse lexical (BM25) and vector results into one ranking.

        Fusion is **reciprocal rank fusion**, not a weighted sum of scores.
        BM25 and cosine are on incomparable scales - a BM25 score is unbounded
        and corpus-dependent, a cosine is bounded by 1 - so any weighted sum
        needs a normalisation step that is itself a tuning decision, and it
        drifts as the corpus grows. RRF only uses *rank*, so it needs no
        normalisation and cannot be skewed by one path's scale.

        A record found by both paths is boosted by construction (it earns a term
        from each), which is the behaviour you want: agreement between an exact
        match and a semantic one is the strongest signal available here.
        """
        if not engagement:
            raise ValueError("engagement is required: unscoped search is a leak")
        lexical = self.search(engagement, query, kinds=kinds, limit=limit * 3)
        semantic = self.recall(engagement, query, kinds=kinds, top_k=limit * 3)

        k = 60  # the standard RRF damping constant
        fused: dict[str, dict[str, Any]] = {}
        for rank, hit in enumerate(lexical):
            entry = fused.setdefault(
                hit.id, {"hit": hit, "rrf": 0.0, "paths": []}
            )
            entry["rrf"] += 1.0 / (k + rank + 1)
            entry["paths"].append("lexical")
        for rank, hit in enumerate(semantic):
            entry = fused.setdefault(
                hit.id, {"hit": hit, "rrf": 0.0, "paths": []}
            )
            entry["rrf"] += 1.0 / (k + rank + 1)
            entry["paths"].append("semantic")
            # Keep the richer record view - vector hits carry which backend
            # produced them, which is what the caller needs to judge the result.
            if not entry["hit"].record.get("retrieval"):
                entry["hit"] = hit

        out: list[SearchHit] = []
        for entry in fused.values():
            hit: SearchHit = entry["hit"]
            paths = sorted(set(entry["paths"]))
            out.append(
                hit.model_copy(
                    update={
                        "score": entry["rrf"],
                        "record": {
                            **hit.record,
                            "retrieval": "hybrid" if len(paths) > 1 else paths[0],
                            "matched_by": paths,
                        },
                    }
                )
            )
        out.sort(key=lambda h: (-h.score, h.ts))
        return out[:limit]

    def graph_query(
        self,
        engagement: str,
        *,
        entity: Optional[str] = None,
        predicate: Optional[str] = None,
        depth: int = 1,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Traverse the knowledge graph (see :mod:`memory_store.graph`)."""
        if not engagement:
            raise ValueError("engagement is required: unscoped graph reads are a leak")
        return self.graph.query(
            engagement, entity=entity, predicate=predicate, depth=depth, limit=limit
        )

    def graph_entities(
        self, engagement: str, *, kind: Optional[str] = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        if not engagement:
            raise ValueError("engagement is required: unscoped graph reads are a leak")
        return self.graph.entities(engagement, kind=kind, limit=limit)

    def graph_entity_profile(self, engagement: str, entity: str, *, limit: int = 200) -> dict[str, Any]:
        """Drill down into one entity (Phase 16): relations grouped by predicate."""
        if not engagement:
            raise ValueError("engagement is required: unscoped graph reads are a leak")
        return self.graph.entity_profile(engagement, entity, limit=limit)

    def retrieval_bundle(
        self,
        engagement: str,
        query: str,
        *,
        limit: int = 8,
        depth: int = 1,
    ) -> dict[str, Any]:
        """Combined retrieval for a crew: fused hits **plus** the graph around
        whatever they mention.

        This is the composition the workstream asks for, and the reason it is
        worth doing is that the two retrieval modes answer different questions
        about the *same* records. Fused search tells you which memories are
        relevant; the graph then tells you what those memories are connected to
        - including entities that no similar-sounding record mentions.

        Seeding is deliberate: the graph walk starts at the highest-ranked hit's
        target or the first address-shaped hit, rather than at an arbitrary node.
        Expanding from a random entity would return a large, plausible and
        almost entirely irrelevant neighbourhood.
        """
        hits = self.hybrid(engagement, query, limit=limit)
        seed: Optional[str] = None
        if hits:
            top = hits[0]
            candidates = [
                top.record.get("target"),
                top.record.get("key"),
                top.summary.split(":")[0] if ":" in top.summary else None,
            ]
            for candidate in candidates:
                if not candidate:
                    continue
                found = self.graph.find_entity(engagement, str(candidate))
                if found:
                    # Store the entity's *name*, not its id: ``GraphIndex.query``
                    # resolves its ``entity`` argument by name. Passing the id
                    # here looked correct and silently returned an empty
                    # neighbourhood, which is exactly the kind of failure a
                    # "returns a graph" assertion would not catch on its own.
                    seed = found["name"]
                    break

        graph = (
            self.graph.query(engagement, entity=seed, depth=depth, limit=200)
            if seed
            else {"engagement": engagement, "root": None, "depth": depth, "nodes": [], "edges": []}
        )
        return {
            "engagement": engagement,
            "query": query,
            "hits": [h.model_dump() for h in hits],
            "graph_seed": seed,
            "graph": graph,
            "counts": {
                "hits": len(hits),
                "nodes": len(graph.get("nodes") or []),
                "edges": len(graph.get("edges") or []),
            },
        }

    def backfill_derived(self, *, engagement: Optional[str] = None) -> dict[str, int]:
        """Rebuild both derived indexes from memory. Safe to run repeatedly."""
        vectors = self.vectors.backfill(self, engagement=engagement)
        graph = self.graph.backfill(self, engagement=engagement)
        return {"vectors": vectors, "graph_edges": graph}

    # -- row mapping -----------------------------------------------------
    @staticmethod
    def _episode(row: sqlite3.Row) -> Episode:
        return Episode(
            episode_id=row["episode_id"],
            ts=row["ts"],
            engagement=row["engagement"],
            kind=row["kind"],
            card_id=row["card_id"],
            agent=row["agent"],
            board_id=row["board_id"],
            target=row["target"],
            summary=row["summary"],
            detail=row["detail"] or "",
            data=json.loads(row["data"] or "{}"),
            tags=json.loads(row["tags"] or "[]"),
            supersedes=row["supersedes"],
        )

    @staticmethod
    def _fact(row: sqlite3.Row) -> Fact:
        return Fact(
            fact_id=row["fact_id"],
            ts=row["ts"],
            updated_at=row["updated_at"],
            engagement=row["engagement"],
            key=row["key"],
            statement=row["statement"],
            detail=row["detail"] or "",
            confidence=float(row["confidence"]),
            status=row["status"],
            source_card=row["source_card"],
            agent=row["agent"],
            target=row["target"],
            tags=json.loads(row["tags"] or "[]"),
        )

    def _hit(self, row: sqlite3.Row, kind: str, *, summary_key: str) -> SearchHit:
        # bm25() returns a negative score where more negative is a better match;
        # flip sign so every SearchHit has the intuitive "higher is better".
        raw = row["score"] if "score" in row.keys() else 0.0
        score = -float(raw or 0.0)
        return SearchHit(
            kind=kind,  # type: ignore[arg-type]
            id=row["episode_id"] if kind == "episodic" else row["fact_id"],
            engagement=row["engagement"],
            ts=row["ts"],
            summary=row[summary_key],
            score=score,
            record=dict(row),
        )


#: Process-wide default store (built per-app in the service).
_DEFAULT: Optional[MemoryStore] = None


def reset_store(store: Optional[MemoryStore] = None) -> None:
    global _DEFAULT
    _DEFAULT = store


def default_store() -> MemoryStore:
    """Return the process-wide store, creating it on first use."""
    global _DEFAULT
    if _DEFAULT is None:
        import os

        _DEFAULT = MemoryStore(os.environ.get("MEMORY_DB", ":memory:"))
    return _DEFAULT
