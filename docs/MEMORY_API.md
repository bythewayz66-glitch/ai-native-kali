# Memory store — API reference (L6)

The blueprint's **L6 layer**: what crews remember between runs. It is a standalone
service on **:8087**, scoped strictly per engagement, with four retrieval paths.

Phase 3 added **vector recall** and a **knowledge graph**. Both are *derived* indexes:
they own their own tables, and everything in them can be rebuilt from `episodes` and
`facts` with `backfill()`. That is what makes them safe to add to an existing store —
an older database starts with empty indexes and fills as it is written to.

```
records ──┬── FTS5  (keyword)      exact tokens, BM25-ish ranking
          ├── vectors (semantic)   cosine over hashing embeddings
          └── graph (relational)   typed entities + evidence-weighted edges
```

---

## The one rule

**`engagement` is a requirement on every read, not an optional filter.**

Omitting it is a `400` with an explanation — never a wider result set. A default scope
in a security product is a breach waiting for a typo. This holds on all four retrieval
paths, including the two added in Phase 3.

---

## Records

### `Episode` — episodic memory

Something that happened. Append-only.

| Field | Type | Notes |
|---|---|---|
| `episode_id` | string | `epi_…` |
| `ts` | ISO 8601 | set on write |
| `engagement` | string | **required** |
| `kind` | enum | `tool_run`, `finding`, `decision`, `note`, `error` |
| `summary` | string | one line; this is what recall returns |
| `detail` | string | full text, searched and embedded |
| `card_id` | string? | the card that produced it |
| `agent` | string? | the role that produced it |
| `board_id` | string? | |
| `target` | string? | the host in scope |
| `tags` | string[] | |

### `Fact` — semantic memory

Something believed. Keyed, and **superseded rather than overwritten** — a superseded
statement leaves active reads and search while remaining in history for audit.

| Field | Type | Notes |
|---|---|---|
| `fact_id` | string | `fct_…` |
| `ts` / `updated_at` | ISO 8601 | |
| `engagement` | string | **required** |
| `key` | string | e.g. `web-server:shop.example.net` — the graph reads this |
| `statement` | string | the claim |
| `detail` | string | |
| `confidence` | float 0–1 | |
| `status` | enum | `active`, `superseded`, `retracted` |
| `target` | string? | |
| `tags` | string[] | |

---

## Endpoints

### Records

| Method | Path | Notes |
|---|---|---|
| `POST` | `/episodes` | write an episode |
| `POST` | `/facts` | write a fact |
| `POST` | `/facts/{id}/retract` | **deletes its vector row too** — a retracted claim must not keep returning from recall |
| `GET` | `/engagements` | |
| `GET` | `/health` | includes derived-index error counts |
| `GET` | `/docs` | interactive |

### Retrieval

| Method | Path | Params |
|---|---|---|
| `GET` | `/search` | `engagement`*, `q`, `kind`, `limit` — keyword (FTS5, or `LIKE` if unavailable) |
| `GET` | `/recall` | `engagement`*, `q`, `mode`, `kind`, `limit`, `min_score` |
| `GET` | `/graph` | `engagement`*, `entity`, `depth`, `limit` |
| `GET` | `/context` | `engagement`*, `target` — the prompt block a crew gets |

\* required.

#### `/recall` — vector and hybrid retrieval

`mode` is one of:

- **`vector`** — cosine similarity over hashing embeddings.
- **`hybrid`** (default) — keyword hits **fused** with vector hits. A record found by
  both outranks one found by either alone, so exact-token matches are not buried by
  similarity scoring and near-misses are not lost to tokenisation.
- **`keyword`** — the `/search` path, for comparison and fallback.

Each hit carries `score` (0–1), `retrieval` (`vector`, `keyword`, or `both`), `kind`,
`summary`, `target` and `key`. The response always reports `vector_backend` — a swap of
embedding backend is **visible** in every response rather than silent.

#### `/graph` — the knowledge graph

Entities and relations derived from stored memory.

**Entity kinds:** `host`, `service`, `product`, `cve`, `endpoint`, `engagement`.

**Relations:** `runs`, `exposes`, `has_vuln`, `part_of`, `observed_on`, `in_engagement`.

**Confidence is evidence-weighted.** A relation derived from a **fact** (a crew
concluded it) outranks one derived from an **episode** (something was observed). Re-
observing a relation raises its confidence rather than duplicating the edge — so the
graph converges as evidence accumulates instead of growing without bound.

**Seeding.** `?entity=shop.example.net` returns that neighbourhood. Omit `entity` and
the whole engagement graph is returned.

#### `/recall/bundle` — the composition

The endpoint crews actually call. Runs keyword + vector recall, takes the recalled
records as **graph seeds**, walks the graph from them, and returns both together:

```json
{
  "query": "apache web server",
  "engagement": "ENG-SMOKE",
  "mode": "hybrid",
  "vector_backend": "hashing-blake2b",
  "counts": {"hits": 2, "nodes": 6, "edges": 6},
  "graph_seed": "shop.example.net",
  "hits": [ ... ],
  "nodes": [ ... ],
  "edges": [ ... ]
}
```

Why the fusion matters more than either half: recall answers *"what do we know that
looks like this question"*, the graph answers *"what is connected to it"*. A crew asked
about `shop.example.net` wants the CVE **and** the fact that the same host exposes SMB —
the second is reachable only by traversal, because no reasonable query mentions both.

---

## Honest limitations

These are stated here because they are the boundary of what the layer claims, and a
reader deciding whether to trust it needs them more than they need the endpoint list.

| Limitation | Detail |
|---|---|
| **The vector backend is lexical, not semantic.** | Hashing character n-grams (blake2b) into a fixed dim, L2-normalised. It finds what a phrase *looks like*, not what it *means*: `"unpatched apache"` will not match `"CVE-2021-41773"` on embeddings alone. It is a real retrieval layer with real ranking, and it reports itself as `hashing-blake2b` rather than implying more. A learned model drops in behind the same interface. |
| **Graph extraction is rule-based.** | Entity kinds come from patterns (IPv4/FQDN/MAC/CVE) and from fact-key prefixes (`web-server:host`). A fact written without a recognisable shape contributes no edges, so the graph grows with evidence *formatting*, not just volume. |
| **Subdomain matching is suffix-based.** | A scope of `example.net` covers `ns1.example.net`, because it genuinely is inside that domain. |
| **Filenames are not hosts.** | `rockyou.txt` matches an FQDN pattern with the plausible TLD `txt`; a file-extension blocklist prevents it being promoted to a host node. The TLD test alone is not enough, because many extensions are alphabetic. |
| **Derived-index failures are non-fatal.** | An embedding or extraction error never fails a memory write — it is recorded and surfaced on `/health`. The trade is deliberate: a crew that cannot embed still records what it did. |
| **No vector index acceleration.** | Cosine is computed over the engagement's rows in Python. Correct and fast at this scale; an ANN index is needed at ten-thousand-plus records per engagement. |

---

## Tests

**54 tests** in `memory-store/tests/test_phase3_memory.py`, covering:

- vector recall ranking and that it returns the relevant record, not the first row;
- the existing API contract unchanged (all Phase 2 tests still pass);
- graph entity/relation extraction, typed kinds, and confidence weighting;
- re-observation raising confidence instead of duplicating an edge;
- the filename/TLD guard (`rockyou.txt` is not a host);
- retraction removing a record from recall;
- **recall + graph composing** in the bundle, seeded from recalled records;
- the scope guard on **every** new endpoint — `400` without an engagement, and zero
  cross-engagement hits.

Run them with:

```bash
python3 -m pytest memory-store/tests -q
```
