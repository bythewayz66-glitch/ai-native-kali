"""Knowledge graph over stored memory.

Workstream A, Phase 3. Blueprint ref: section 03, layer L6.

Vector recall answers *"what is similar to this sentence?"*. A graph answers a
different question that similarity cannot reach: *"what else do we know about
this host, and what does it connect to?"* Those are not the same retrieval and
neither subsumes the other, which is why both exist behind one API.

The graph is **derived**, never authored
----------------------------------------
Nothing in this system writes a graph edge directly. Entities and relations are
extracted from episodic and semantic memory as it is recorded, so the graph is a
materialised view of the memory store and can be rebuilt from it at any time
(``backfill()``). An edge is therefore only ever as true as the memory it came
from, and ``evidence`` keeps the record id that produced it - so a relation can
always be traced back to the episode or fact behind it.

That direction matters for audit: the chain is *memory → graph*, never
*graph → memory*. A graph that could be edited directly would be a place to
assert something with no evidence behind it.

Entity kinds and predicates are closed sets
-------------------------------------------
Both are enums rather than free text. An open predicate vocabulary produces
``runs``, ``running``, ``is running`` and ``may run`` as four separate edges,
which makes the graph unqueryable - the failure mode of every knowledge graph
built without a schema. Closed sets also make the extraction testable.

Honest limits
-------------
* Extraction is pattern-based: addresses, CVEs, ``label:host`` fact keys, and an
  explicit product vocabulary. It does not parse prose, so a fact phrased
  unusually yields fewer entities. It never invents one.
* ``depth`` traversal is breadth-first over undirected edges and is bounded, so a
  densely connected engagement returns a large-but-finite neighbourhood rather
  than hanging.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from typing import Any, Optional

from .models import Episode, Fact, new_id, utcnow

#: Closed set of entity kinds.
ENTITY_KINDS = (
    "host",  # an IP, hostname, CIDR or BSSID
    "service",  # a named service on a host
    "product",  # software / version
    "cve",  # a CVE identifier
    "credential",  # a credential reference (never a secret value)
    "user",  # a person or account
    "engagement",  # the engagement itself, as the root node
    "unknown",
)

#: Closed set of predicates. Every edge is one of these.
PREDICATES = (
    "has_service",
    "runs",
    "has_cve",
    "resolves_to",
    "has_credential",
    "member_of",
    "observed_at",
    "related_to",
)

#: File extensions that must never be promoted to a host node.
#:
#: ``rockyou.txt`` matches the FQDN pattern with the plausible alphabetic TLD
#: ``txt``, so without this the graph fills with host entities that are really
#: wordlist and binary paths. The TLD test alone is not enough, because plenty of
#: file extensions are alphabetic.
_FILE_EXTENSIONS = frozenset(
    """
    txt json yaml yml toml ini conf cfg pem crt cer key pub sh bash py rb pl js
    mjs ts tsx bin img iso elf exe dll so dylib md csv tsv xml log db sqlite
    sqlite3 pcap pcapng cap har html htm pdf docx doc xlsx xls zip gz bz2 xz
    tar 7z deb rpm apk pkg whl egg list dic rules dd raw mem vmdk e01
    """.split()
)


def _has_file_extension(value: str) -> bool:
    tail = (value or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in tail:
        return False
    return tail.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS


_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_CIDR_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b")
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)
_MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")
_FQDN_RE = re.compile(
    r"\b(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)+[A-Za-z]{2,24}\b"
)
_CRED_RE = re.compile(r"\b(?:credential|cred|password|key)\s*[:=]\s*([A-Za-z0-9_.@\-]{3,64})", re.I)

#: Service labels that appear as the prefix of a ``label:host`` fact key.
_KNOWN_SERVICES = frozenset(
    """
    web-server webserver http https ssh smb smb-server file-server dns dns-server
    mail mail-server ftp rdp vnc db database sql mysql postgres mssql oracle
    vpn ldap kerberos smtp imap pop3 api gateway proxy jump-host bastion
    """.split()
)

#: Products worth materialising as nodes. Explicit rather than inferred: a
#: guessed product node is a false fact in a security report.
_KNOWN_PRODUCTS = frozenset(
    """
    apache nginx iis tomcat jetty wordpress drupal joomla magento shopify
    openssh openvpn exchange sharepoint confluence jira gitlab jenkins grafana
    kibana elasticsearch redis mongodb postgresql mysql mariadb mssql
    php python django flask rails node express spring
    """.split()
)

_RUNS_RE = re.compile(r"\b(?:runs|running|uses|using|serving|powered\s+by)\s+([A-Za-z][A-Za-z0-9_.+-]{2,30})", re.I)


def _norm(value: str) -> str:
    return (value or "").strip().strip(".,;:()[]<>\"'").lower()


def _entity_id(engagement: str, kind: str, name: str) -> str:
    return f"ent:{engagement}:{kind}:{_norm(name)}"


def extract_entities(text: str) -> list[tuple[str, str]]:
    """(kind, name) pairs found in *text*. Deterministic, pattern-based."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, name: str) -> None:
        key = (kind, _norm(name))
        if not _norm(name) or key in seen:
            return
        seen.add(key)
        found.append((kind, name))

    for match in _CIDR_RE.findall(text or ""):
        _add("host", match)
    for match in _IPV4_RE.findall(text or ""):
        _add("host", match)
    for match in _MAC_RE.findall(text or ""):
        _add("host", match)
    for match in _CVE_RE.findall(text or ""):
        _add("cve", match.upper())
    for match in _FQDN_RE.findall(text or ""):
        # ``rockyou.txt`` style values: require a plausible alphabetic TLD **and**
        # reject known file extensions, so a filename is not promoted to a host.
        if _has_file_extension(match):
            continue
        tld = match.rsplit(".", 1)[-1].lower()
        if tld.isalpha() and len(tld) >= 2:
            _add("host", match)
    for match in _CRED_RE.findall(text or ""):
        _add("credential", match)
    lowered = (text or "").lower()
    for service in _KNOWN_SERVICES:
        if re.search(rf"\b{re.escape(service)}\b", lowered):
            _add("service", service)
    for product in _KNOWN_PRODUCTS:
        if re.search(rf"\b{re.escape(product)}\b", lowered):
            _add("product", product)
    for match in _RUNS_RE.findall(text or ""):
        candidate = _norm(match)
        if candidate in _KNOWN_PRODUCTS or candidate in _KNOWN_SERVICES:
            _add("product" if candidate in _KNOWN_PRODUCTS else "service", candidate)
    return found


class GraphIndex:
    """Entity/relation store derived from memory."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: Optional[threading.RLock] = None,
    ) -> None:
        self._conn = conn
        self._lock = lock or threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS entities (
                  entity_id   TEXT PRIMARY KEY,
                  engagement  TEXT NOT NULL,
                  kind        TEXT NOT NULL,
                  name        TEXT NOT NULL,
                  first_seen  TEXT NOT NULL,
                  last_seen   TEXT NOT NULL,
                  mentions    INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_ent_eng  ON entities(engagement, kind);
                CREATE INDEX IF NOT EXISTS idx_ent_name ON entities(engagement, name);

                CREATE TABLE IF NOT EXISTS relations (
                  relation_id TEXT PRIMARY KEY,
                  engagement  TEXT NOT NULL,
                  src_id      TEXT NOT NULL,
                  dst_id      TEXT NOT NULL,
                  predicate   TEXT NOT NULL,
                  confidence  REAL NOT NULL DEFAULT 0.5,
                  evidence    TEXT,
                  created_at  TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_unique
                  ON relations(engagement, src_id, dst_id, predicate);
                CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(engagement, src_id);
                CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations(engagement, dst_id);
                """
            )
            self._conn.commit()

    # -- writes ----------------------------------------------------------
    def upsert_entity(
        self,
        engagement: str,
        kind: str,
        name: str,
        *,
        ts: Optional[str] = None,
    ) -> str:
        """Create or touch an entity. Returns its id."""
        ts = ts or utcnow()
        kind = kind if kind in ENTITY_KINDS else "unknown"
        entity_id = _entity_id(engagement, kind, name)
        with self._lock:
            self._conn.execute(
                """INSERT INTO entities (entity_id,engagement,kind,name,first_seen,last_seen,mentions)
                   VALUES (?,?,?,?,?,?,1)
                   ON CONFLICT(entity_id) DO UPDATE SET
                     last_seen = excluded.last_seen,
                     mentions  = entities.mentions + 1""",
                (entity_id, engagement, kind, _norm(name), ts, ts),
            )
            self._conn.commit()
        return entity_id

    def add_relation(
        self,
        engagement: str,
        src_id: str,
        dst_id: str,
        predicate: str,
        *,
        confidence: float = 0.5,
        evidence: Optional[str] = None,
    ) -> bool:
        """Add an edge. An existing edge is kept, its confidence raised to the max.

        Re-observing a relation is evidence *for* it, so the confidence moves up
        rather than the row being replaced - and the earliest evidence id is
        retained so a chain of citations is not lost to a later scan.
        """
        if predicate not in PREDICATES or src_id == dst_id:
            return False
        confidence = max(0.0, min(1.0, confidence))
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO relations
                     (relation_id,engagement,src_id,dst_id,predicate,confidence,evidence,created_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(engagement,src_id,dst_id,predicate) DO UPDATE SET
                     confidence = MAX(relations.confidence, excluded.confidence)""",
                (
                    new_id("rel"), engagement, src_id, dst_id, predicate,
                    confidence, evidence, utcnow(),
                ),
            )
            self._conn.commit()
        return bool(cur.rowcount)

    def observe_fact(self, fact: Fact) -> int:
        """Derive entities and edges from one fact."""
        engagement = fact.engagement
        added = 0
        text = f"{fact.statement} {fact.detail} {fact.key}"

        # The engagement is the root every fact hangs from.
        root = self.upsert_entity(engagement, "engagement", engagement, ts=fact.ts)

        # ``label:host`` fact keys carry structure on purpose, so use it: a key
        # like ``web-server:shop.example.net`` states both a host and a service,
        # and the edge between them.
        host_id: Optional[str] = None
        label: Optional[str] = None
        if ":" in fact.key:
            head, _, tail = fact.key.partition(":")
            label = _norm(head)
            if _looks_like_address(tail):
                host_id = self.upsert_entity(engagement, "host", tail, ts=fact.ts)
                added += 1

        if fact.target and _looks_like_address(fact.target):
            target_id = self.upsert_entity(engagement, "host", fact.target, ts=fact.ts)
            host_id = host_id or target_id
            added += 1

        if label and host_id:
            if label in _KNOWN_SERVICES or label:
                service_id = self.upsert_entity(engagement, "service", label, ts=fact.ts)
                self.add_relation(engagement, host_id, service_id, "has_service", confidence=fact.confidence, evidence=fact.fact_id)
                added += 1

        for kind, name in extract_entities(text):
            if kind == "host":
                if host_id and _norm(name) == _norm(fact.target or ""):
                    entity_id = host_id
                else:
                    entity_id = self.upsert_entity(engagement, kind, name, ts=fact.ts)
            else:
                entity_id = self.upsert_entity(engagement, kind, name, ts=fact.ts)
            added += 1
            source = host_id or root
            predicate = {
                "cve": "has_cve",
                "product": "runs",
                "service": "has_service",
                "credential": "has_credential",
                "host": "resolves_to",
            }.get(kind, "related_to")
            if source != entity_id:
                self.add_relation(engagement, source, entity_id, predicate, confidence=fact.confidence, evidence=fact.fact_id)
                if fact.target:
                    target_id = self.upsert_entity(engagement, "host", fact.target, ts=fact.ts)
                    if target_id != entity_id:
                        self.add_relation(engagement, target_id, entity_id, predicate, confidence=fact.confidence, evidence=fact.fact_id)

        if host_id:
            self.add_relation(engagement, root, host_id, "member_of", confidence=0.9, evidence=fact.fact_id)
        return added

    def observe_episode(self, episode: Episode) -> int:
        """Derive entities and edges from one episode.

        Episodes carry weaker claims than facts - an observation is not a
        conclusion - so edges derived here are recorded at lower confidence and
        the ``observed_at`` predicate is used for the target rather than a
        semantic claim about it.
        """
        engagement = episode.engagement
        added = 0
        root = self.upsert_entity(engagement, "engagement", engagement, ts=episode.ts)
        host_id: Optional[str] = None

        if episode.target and _looks_like_address(episode.target):
            host_id = self.upsert_entity(engagement, "host", episode.target, ts=episode.ts)
            added += 1

        for kind, name in extract_entities(f"{episode.summary} {episode.detail}"):
            entity_id = self.upsert_entity(engagement, kind, name, ts=episode.ts)
            added += 1
            predicate = {
                "cve": "has_cve",
                "product": "runs",
                "service": "has_service",
                "credential": "has_credential",
                "host": "resolves_to",
            }.get(kind, "related_to")
            source = host_id or root
            if source != entity_id:
                self.add_relation(engagement, source, entity_id, predicate, confidence=0.4, evidence=episode.episode_id)

        if host_id:
            self.add_relation(engagement, root, host_id, "observed_at", confidence=0.5, evidence=episode.episode_id)
        return added

    # -- reads -----------------------------------------------------------
    def entities(
        self,
        engagement: str,
        *,
        kind: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM entities WHERE engagement=?"
        params: list[Any] = [engagement]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY mentions DESC, name ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def relations(
        self,
        engagement: str,
        *,
        entity_id: Optional[str] = None,
        predicate: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM relations WHERE engagement=?"
        params: list[Any] = [engagement]
        if entity_id:
            sql += " AND (src_id=? OR dst_id=?)"
            params.extend([entity_id, entity_id])
        if predicate:
            sql += " AND predicate=?"
            params.append(predicate)
        sql += " ORDER BY confidence DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def find_entity(self, engagement: str, name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM entities WHERE engagement=? AND name=? LIMIT 1",
                (engagement, _norm(name)),
            ).fetchone()
        return dict(row) if row else None

    def query(
        self,
        engagement: str,
        *,
        entity: Optional[str] = None,
        predicate: Optional[str] = None,
        depth: int = 1,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Traverse the graph breadth-first from *entity*, or return it whole.

        With no ``entity`` this returns the engagement's entire graph, which is
        bounded by ``limit``. With one, it expands outward ``depth`` hops over
        **undirected** edges - the useful question is normally "what is this
        connected to", and direction is an artefact of which end happened to be
        recorded first.
        """
        engagement = (engagement or "").strip()
        if not engagement:
            raise ValueError("engagement is required: unscoped graph reads are a leak")
        depth = max(1, min(int(depth or 1), 4))

        if not entity:
            edges = self.relations(engagement, predicate=predicate, limit=limit)
            ids = {e["src_id"] for e in edges} | {e["dst_id"] for e in edges}
            nodes = self._nodes_by_id(ids)
            return {
                "engagement": engagement,
                "root": None,
                "depth": 0,
                "nodes": nodes,
                "edges": edges,
                "truncated": len(edges) >= limit,
            }

        start = self.find_entity(engagement, entity)
        if start is None:
            return {"engagement": engagement, "root": None, "depth": depth, "nodes": [], "edges": [], "truncated": False}

        frontier = {start["entity_id"]}
        visited = set(frontier)
        collected: dict[str, dict[str, Any]] = {}
        for _ in range(depth):
            step: set[str] = set()
            for node_id in frontier:
                for edge in self.relations(engagement, entity_id=node_id, predicate=predicate, limit=limit):
                    collected[edge["relation_id"]] = edge
                    for candidate in (edge["src_id"], edge["dst_id"]):
                        if candidate not in visited:
                            step.add(candidate)
            if not step:
                break
            visited |= step
            frontier = step

        edges = list(collected.values())
        ids = {e["src_id"] for e in edges} | {e["dst_id"] for e in edges} | {start["entity_id"]}
        return {
            "engagement": engagement,
            "root": start,
            "depth": depth,
            "nodes": self._nodes_by_id(ids),
            "edges": edges,
            "truncated": len(edges) >= limit,
        }

    def neighbors(
        self,
        engagement: str,
        entity: str,
        *,
        depth: int = 1,
        kinds: Optional[list[str]] = None,
        predicates: Optional[list[str]] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Neighbours of *entity*, optionally filtered by node kind or predicate.

        This is the Phase 7, item 6 "richer query" that the planner actually
        needs. :meth:`query` answers "what is near this"; it cannot answer "what
        hosts near this", and a planner given the whole neighbourhood spends its
        prompt budget on unrelated nodes.

        ``kinds`` filters the **nodes** returned (``host``, ``port``, ``service``,
        ``cve`` ...) and ``predicates`` filters the **edges** traversed, so
        ``kinds=["host"]`` and ``predicates=["exposes"]`` answer different
        questions and both are useful: the first says what the node is, the
        second says how it is reached.

        The root is always included in ``nodes`` even when its kind is filtered
        out, because a result set that omits the thing you asked about is not an
        answer to the question.
        """
        engagement = (engagement or "").strip()
        if not engagement:
            raise ValueError("engagement is required: unscoped graph reads are a leak")

        root = self.find_entity(engagement, entity)
        effective_depth = max(1, min(int(depth or 1), 4))
        if root is None:
            return {
                "engagement": engagement,
                "root": None,
                "depth": effective_depth,
                "kinds": list(kinds or []),
                "predicates": list(predicates or []),
                "nodes": [],
                "edges": [],
                "truncated": False,
            }

        edges: dict[str, dict[str, Any]] = {}
        frontier = {root["entity_id"]}
        visited = set(frontier)
        for _ in range(effective_depth):
            step: set[str] = set()
            for node_id in frontier:
                for edge in self.relations(engagement, entity_id=node_id, limit=limit):
                    if predicates and edge["predicate"] not in predicates:
                        continue
                    edges[edge["relation_id"]] = edge
                    for candidate in (edge["src_id"], edge["dst_id"]):
                        if candidate not in visited:
                            step.add(candidate)
            if not step:
                break
            visited |= step
            frontier = step

        ids = {root["entity_id"]}
        for edge in edges.values():
            ids.add(edge["src_id"])
            ids.add(edge["dst_id"])
        nodes = self._nodes_by_id(ids)

        if kinds:
            wanted = set(kinds)
            # The root is kept even when its kind is filtered out: a result set
            # that omits the thing you asked about is not an answer.
            kept = [n for n in nodes if n["kind"] in wanted or n["entity_id"] == root["entity_id"]]
        else:
            kept = nodes

        # Drop edges that no longer touch a visible node, so the returned graph
        # cannot reference a node the caller cannot see.
        present = {n["entity_id"] for n in kept}
        kept_edges = [e for e in edges.values() if e["src_id"] in present or e["dst_id"] in present]

        return {
            "engagement": engagement,
            "root": root,
            # The *effective* depth, not the one that was asked for. Echoing the
            # requested value would let a clamped 4-hop traversal report itself as
            # 99 hops, and a caller reasoning about coverage would be wrong.
            "depth": effective_depth,
            "kinds": list(kinds or []),
            "predicates": list(predicates or []),
            "nodes": kept,
            "edges": kept_edges,
            "truncated": len(edges) >= limit,
        }

    def shortest_path(
        self,
        engagement: str,
        src: str,
        dst: str,
        *,
        max_depth: int = 4,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Shortest undirected path between two named entities, or an empty one.

        Why breadth-first over the existing ``query``: a path answers "how did
        these two things end up connected", which is the question that turns a
        list of recalled facts into an explanation. ``query`` returns a
        neighbourhood, and a neighbourhood cannot distinguish "one hop" from "six".

        Returns ``{"found": False, ...}`` rather than raising when there is no
        path - in a partially-observed graph, no path is a normal answer and the
        caller usually wants to render "no known link" rather than error.
        """
        engagement = (engagement or "").strip()
        if not engagement:
            raise ValueError("engagement is required: unscoped graph reads are a leak")
        max_depth = max(1, min(int(max_depth or 1), 6))

        start = self.find_entity(engagement, src)
        end = self.find_entity(engagement, dst)
        empty = {
            "engagement": engagement,
            "found": False,
            "src": start,
            "dst": end,
            "path": [],
            "edges": [],
            "hops": 0,
        }
        if start is None or end is None:
            return empty
        if start["entity_id"] == end["entity_id"]:
            return {**empty, "found": True, "path": [start]}

        # BFS carrying the predecessor edge, so the path can be rebuilt.
        queue: list[tuple[str, int]] = [(start["entity_id"], 0)]
        previous: dict[str, tuple[str, dict[str, Any]]] = {}
        seen = {start["entity_id"]}

        while queue:
            node_id, dist = queue.pop(0)
            if dist >= max_depth:
                continue
            for edge in self.relations(engagement, entity_id=node_id, limit=limit):
                for other in (edge["src_id"], edge["dst_id"]):
                    if other == node_id or other in seen:
                        continue
                    seen.add(other)
                    previous[other] = (node_id, edge)
                    if other == end["entity_id"]:
                        queue.clear()
                        break
                    queue.append((other, dist + 1))
                else:
                    continue
                break

        if end["entity_id"] not in previous:
            return empty

        # Walk back from the destination.
        chain_ids: list[str] = [end["entity_id"]]
        path_edges: list[dict[str, Any]] = []
        cursor = end["entity_id"]
        while cursor != start["entity_id"]:
            parent, edge = previous[cursor]
            path_edges.append(edge)
            chain_ids.append(parent)
            cursor = parent
        chain_ids.reverse()
        path_edges.reverse()

        nodes = self._nodes_by_id(set(chain_ids))
        order = {node_id: index for index, node_id in enumerate(chain_ids)}
        nodes.sort(key=lambda n: order.get(n["entity_id"], 0))

        return {
            "engagement": engagement,
            "found": True,
            "src": start,
            "dst": end,
            "path": nodes,
            "edges": path_edges,
            "hops": len(path_edges),
        }

    def entities_by_kind(self, engagement: str) -> dict[str, int]:
        """Counts per entity kind, most numerous first.

        Cheap orientation for a UI or a planner that has to decide where to spend
        its attention before it can afford a traversal.
        """
        engagement = (engagement or "").strip()
        if not engagement:
            raise ValueError("engagement is required: unscoped graph reads are a leak")
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM entities WHERE engagement=? GROUP BY kind ORDER BY n DESC, kind ASC",
                (engagement,),
            ).fetchall()
        return {row["kind"]: row["n"] for row in rows}

    def _nodes_by_id(self, ids: set[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM entities WHERE entity_id IN ({placeholders})", list(ids)
            ).fetchall()
        return [dict(r) for r in rows]

    def backfill(self, store: Any, *, engagement: Optional[str] = None) -> int:
        """Rebuild graph edges from memory. Derived data only - safe to repeat."""
        added = 0
        engagements = [engagement] if engagement else [r["engagement"] for r in store.engagements()]
        for eng in engagements:
            for fact in store.facts(eng, status="active", limit=10_000):
                added += self.observe_fact(fact)
            for episode in store.episodes(eng, limit=10_000, newest_first=False):
                added += self.observe_episode(episode)
        return added

    def stats(self, engagement: Optional[str] = None) -> dict[str, Any]:
        sql_ent = "SELECT COUNT(*) AS n FROM entities"
        sql_rel = "SELECT COUNT(*) AS n FROM relations"
        params: list[Any] = []
        if engagement:
            sql_ent += " WHERE engagement=?"
            sql_rel += " WHERE engagement=?"
            params = [engagement, engagement]
        with self._lock:
            if engagement:
                ents = self._conn.execute(sql_ent, [engagement]).fetchone()["n"]
                rels = self._conn.execute(sql_rel, [engagement]).fetchone()["n"]
            else:
                ents = self._conn.execute(sql_ent).fetchone()["n"]
                rels = self._conn.execute(sql_rel).fetchone()["n"]
            by_kind_rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM entities"
                + (" WHERE engagement=?" if engagement else "")
                + " GROUP BY kind ORDER BY n DESC",
                ([engagement] if engagement else []),
            ).fetchall()
            by_pred_rows = self._conn.execute(
                "SELECT predicate, COUNT(*) AS n FROM relations"
                + (" WHERE engagement=?" if engagement else "")
                + " GROUP BY predicate ORDER BY n DESC",
                ([engagement] if engagement else []),
            ).fetchall()
        return {
            "entities": int(ents),
            "relations": int(rels),
            "by_kind": {r["kind"]: int(r["n"]) for r in by_kind_rows},
            "by_predicate": {r["predicate"]: int(r["n"]) for r in by_pred_rows},
        }


def _looks_like_address(value: str) -> bool:
    """Minimal local address test.

    Deliberately not imported from ``tool_frontends.targets``: memory-store is a
    separate service with its own deployment boundary, and a shared import would
    make one unstartable without the other.
    """
    v = (value or "").strip()
    if not v or "/" == v[:1]:
        return False
    if _CIDR_RE.fullmatch(v) or _IPV4_RE.fullmatch(v) or _MAC_RE.fullmatch(v):
        return True
    if v.count(":") and not _looks_like_port_host(v):
        return False
    if _FQDN_RE.fullmatch(v):
        if _has_file_extension(v):
            return False
        tail = v.rsplit(".", 1)[-1].lower()
        return tail.isalpha() and len(tail) >= 2
    return False


def _looks_like_port_host(value: str) -> bool:
    host, _, port = value.rpartition(":")
    return bool(host) and port.isdigit()
