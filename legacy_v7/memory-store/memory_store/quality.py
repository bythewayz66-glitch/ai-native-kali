"""Retrieval-quality harness: measure recall, don't assert it (Phase 7, item 5).

Why this exists
---------------
The Phase 3 report said plainly that the vector backend matches vocabulary, not
meaning, and that ``"open port 445"`` and ``"SMB exposed"`` score low against each
other. That was a *claim*. A claim about retrieval quality is only worth
something if it is measured, and it can only be measured against a fixed corpus
with known-relevant records.

So this module is a small bench: a pinned corpus, a set of queries with gold
labels, and recall@k / MRR computed from actual store behaviour. Run it with the
hashing backend and you get a number; run it with a model endpoint and you get a
different number, and the difference is the improvement - or the absence of one.

What the corpus is designed to expose
-------------------------------------
Queries are split into two families, and the split is the point:

* **lexical** - the query shares terms with the relevant record. Any reasonable
  retriever should score well here. If this family scores badly, the *harness*
  is broken, not the embedder.
* **semantic** - the query shares **no token** with the relevant record; the link
  is only in meaning (``"SMB"`` vs ``"port 445"``). The hashing vectoriser should
  score near zero here. **That near-zero is the honest baseline**: it is the
  measurement that justifies swapping in a model, and the same number measured
  after the swap is what proves the swap helped.

Reporting both families separately is deliberate. A single blended recall figure
would let a strong lexical score hide a total semantic failure - which is exactly
the confusion this harness was written to end.

The ``synonym`` stand-in
------------------------
:class:`SynonymEmbedder` is a tiny hand-built embedder that maps known domain
synonym groups onto shared dimensions. It is not a model. It exists so the
harness can be tested for *discriminating power*: a measurement rig that returns
the same number no matter what is plugged in measures nothing, and the only way
to prove otherwise without shipping a model into CI is to plug in a stand-in with
known-better behaviour and check the number moves.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

#: Default cut-off for recall@k.
DEFAULT_K = 3


@dataclass(frozen=True)
class Memory:
    """One record to seed into the store."""

    id: str
    engagement: str
    summary: str
    kind: str = "observation"
    detail: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Probe:
    """One query with its gold-relevant record ids."""

    query: str
    relevant: tuple[str, ...]
    family: str = "lexical"
    engagement: str = "ENG-QUALITY"


@dataclass
class ProbeResult:
    query: str
    family: str
    ranked: list[str]
    hit_rank: Optional[int] = None

    @property
    def hit(self) -> bool:
        return self.hit_rank is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "family": self.family,
            "ranked": list(self.ranked),
            "hit_rank": self.hit_rank,
        }


@dataclass
class QualityReport:
    """Measured retrieval quality for one backend."""

    backend: str
    k: int
    probes: list[ProbeResult] = field(default_factory=list)

    @property
    def hits(self) -> int:
        return sum(1 for p in self.probes if p.hit)

    @property
    def recall_at_k(self) -> float:
        if not self.probes:
            return 0.0
        return round(self.hits / len(self.probes), 4)

    @property
    def mrr(self) -> float:
        """Mean reciprocal rank of the first relevant hit (0 when none)."""
        if not self.probes:
            return 0.0
        total = sum(1.0 / p.hit_rank for p in self.probes if p.hit_rank)
        return round(total / len(self.probes), 4)

    def by_family(self, family: str) -> dict[str, Any]:
        subset = [p for p in self.probes if p.family == family]
        if not subset:
            return {"family": family, "n": 0, "recall_at_k": 0.0, "mrr": 0.0}
        hits = sum(1 for p in subset if p.hit)
        mrr = sum(1.0 / p.hit_rank for p in subset if p.hit_rank) / len(subset)
        return {
            "family": family,
            "n": len(subset),
            "recall_at_k": round(hits / len(subset), 4),
            "mrr": round(mrr, 4),
        }

    def families(self) -> dict[str, Any]:
        names = sorted({p.family for p in self.probes})
        return {name: self.by_family(name) for name in names}

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "k": self.k,
            "n": len(self.probes),
            "recall_at_k": self.recall_at_k,
            "mrr": self.mrr,
            "families": self.families(),
            "probes": [p.as_dict() for p in self.probes],
        }

    def summary_line(self) -> str:
        lexical = self.by_family("lexical")
        semantic = self.by_family("semantic")
        return (
            f"{self.backend}: recall@{self.k}={self.recall_at_k:.2f} mrr={self.mrr:.2f} "
            f"| lexical={lexical['recall_at_k']:.2f} semantic={semantic['recall_at_k']:.2f}"
        )


# --------------------------------------------------------------------- corpus


def builtin_corpus() -> tuple[list[Memory], list[Probe]]:
    """The pinned corpus. Fixed on purpose: numbers are only comparable to numbers
    measured against the same records."""
    eng = "ENG-QUALITY"
    memories = [
        Memory("m_smb", eng, "SMB file sharing is exposed on the file server",
               detail="samba smbd port 445 anonymous access permitted", tags=("smb", "file-sharing")),
        Memory("m_web", eng, "Outdated Apache web server on the shop host",
               detail="httpd 2.4.49 mod_status reachable, path traversal CVE-2021-41773",
               tags=("web", "apache")),
        Memory("m_ssh", eng, "Remote shell login service is reachable",
               detail="openssh 8.2 password authentication allowed", tags=("remote-access",)),
        Memory("m_db", eng, "Database service accepting external connections",
               detail="postgresql 13 listening, weak password for the application user",
               tags=("database",)),
        Memory("m_tls", eng, "Encryption certificate is long expired",
               detail="self-signed x509 expired 2019, hostname mismatch",
               tags=("tls",)),
    ]
    probes = [
        # -- lexical: the query shares terms with the record ------------------
        # A retriever that scores badly here is broken; this family is the
        # harness's own sanity check.
        Probe("SMB exposed file sharing", ("m_smb",), "lexical"),
        Probe("outdated apache web server", ("m_web",), "lexical"),
        Probe("postgresql weak password", ("m_db",), "lexical"),
        Probe("expired x509 certificate", ("m_tls",), "lexical"),
        # -- semantic: NO token shared with the record, meaning only ---------
        #
        # These are the load-bearing probes, and the no-overlap property is
        # asserted by a test rather than trusted: an earlier draft of this corpus
        # had "open port 445" labelled semantic when the record literally
        # contained "port 445", which made the lexical backend look semantic and
        # the whole measurement meaningless. A "semantic" probe that shares a
        # token measures vocabulary matching, not meaning.
        Probe("network drive accessible to anyone", ("m_smb",), "semantic"),
        Probe("guests can read the shared directory", ("m_smb",), "semantic"),
        Probe("padlock warning in the browser", ("m_tls",), "semantic"),
        Probe("can I connect without credentials", ("m_ssh",), "semantic"),
    ]
    return memories, probes


def seed(store: Any, memories: Iterable[Memory]) -> int:
    """Record every memory into *store*. Returns how many were written.

    The ``episode_id`` is pinned to :attr:`Memory.id` so a probe's gold label can
    name a specific record. Without that the store mints its own ids and the only
    way to grade a result would be by matching summary text - which would make
    the harness grade the *embedder* on string equality, exactly the confusion it
    exists to remove.
    """
    written = 0
    for memory in memories:
        store.record(
            engagement=memory.engagement,
            summary=memory.summary,
            kind=memory.kind,
            detail=memory.detail,
            tags=list(memory.tags),
            episode_id=memory.id,
        )
        written += 1
    return written


def _rank_of(ranked: list[str], relevant: tuple[str, ...]) -> Optional[int]:
    for position, record_id in enumerate(ranked, start=1):
        if record_id in relevant:
            return position
    return None


def measure(
    store: Any,
    probes: Iterable[Probe],
    *,
    k: int = DEFAULT_K,
    backend: Optional[str] = None,
) -> QualityReport:
    """Run every probe against *store* and compute recall@k and MRR.

    A probe that retrieves nothing at all is a miss, not an error: an empty
    result is a real retrieval outcome and the harness must score it as zero
    rather than crash, or a total semantic failure would look like a test error.
    """
    if backend is None:
        try:
            from .vector import vector_backend

            backend = vector_backend()
        except Exception:  # noqa: BLE001
            backend = "unknown"

    report = QualityReport(backend=backend, k=k)
    for probe in probes:
        hits = store.recall(probe.engagement, probe.query, top_k=k)
        ranked = [h.id for h in hits]
        report.probes.append(
            ProbeResult(
                query=probe.query,
                family=probe.family,
                ranked=ranked,
                hit_rank=_rank_of(ranked, probe.relevant),
            )
        )
    return report


def measure_with(
    embedder: Any,
    memories: Iterable[Memory],
    probes: Iterable[Probe],
    *,
    k: int = DEFAULT_K,
) -> QualityReport:
    """Measure a *specific* embedder without touching the process-wide one.

    Builds a throwaway in-memory store, **swaps its vector index onto the given
    embedder**, seeds it and measures. This is what makes before/after comparison
    possible in one process, and what lets the harness prove it discriminates
    (see module docstring).

    The swap is deliberate rather than passing an embedder to ``MemoryStore``:
    the store resolves its embedder from the process-wide registry at
    construction, and the whole point here is to bypass that registry rather than
    mutate it (mutating it would make two measurements in one process interfere).
    """
    from .store import MemoryStore

    store = MemoryStore(":memory:")
    try:
        store.vectors._embedder = embedder  # noqa: SLF001 - see docstring
        seed(store, memories)
        return measure(store, probes, k=k, backend=getattr(embedder, "name", "unknown"))
    finally:
        store.close()


def compare(
    memories: Iterable[Memory],
    probes: Iterable[Probe],
    embedders: dict[str, Any],
    *,
    k: int = DEFAULT_K,
) -> dict[str, Any]:
    """Measure several embedders over the same corpus and report the deltas.

    The delta is reported per family, because "semantic got better" and "lexical
    stayed the same" are different claims and a blended delta cannot express
    either.
    """
    memories = list(memories)
    probes = list(probes)
    reports = {name: measure_with(impl, memories, probes, k=k) for name, impl in embedders.items()}

    names = list(reports)
    base = reports[names[0]] if names else None
    deltas: dict[str, Any] = {}
    if base is not None:
        for name, report in reports.items():
            if name == names[0]:
                continue
            deltas[name] = {
                "vs": names[0],
                "recall_at_k": round(report.recall_at_k - base.recall_at_k, 4),
                "mrr": round(report.mrr - base.mrr, 4),
                "lexical": round(
                    report.by_family("lexical")["recall_at_k"]
                    - base.by_family("lexical")["recall_at_k"],
                    4,
                ),
                "semantic": round(
                    report.by_family("semantic")["recall_at_k"]
                    - base.by_family("semantic")["recall_at_k"],
                    4,
                ),
            }

    return {
        "k": k,
        "baseline": names[0] if names else None,
        "reports": {name: r.as_dict() for name, r in reports.items()},
        "summary": {name: r.summary_line() for name, r in reports.items()},
        "deltas": deltas,
    }


# --------------------------------------------------------------- stand-ins


class SynonymEmbedder:
    """A hand-built embedder that groups domain synonyms onto shared dimensions.

    **Not a model.** This is a test instrument: it stands in for what a trained
    embedding does (map *meaning*, not vocabulary) so the harness's ability to
    detect an improvement can be asserted in CI, where no model endpoint exists.
    Its scores are meaningless as a statement about real embeddings.
    """

    name = "synonym-standin"

    #: Each group shares one dimension. Curated, not learned - this is a test
    #: instrument, so the grouping is stated in full rather than inferred.
    GROUPS: tuple[tuple[str, ...], ...] = (
        (
            "smb", "samba", "cifs", "445", "file sharing", "file-sharing",
            "network drive", "share", "shared", "folder",
        ),
        ("apache", "httpd", "web", "80", "443", "website", "http", "browser"),
        (
            "ssh", "openssh", "remote shell", "remote-access", "log in", "login",
            "connect", "credentials",
        ),
        ("postgres", "postgresql", "database", "db", "5432"),
        (
            "tls", "x509", "certificate", "cert", "expired", "ssl", "padlock",
            "identity", "untrusted",
        ),
    )

    def __init__(self) -> None:
        self.dim = len(self.GROUPS) + 8

    def encode(self, text: str) -> list[float]:
        lowered = (text or "").lower()
        vector = [0.0] * self.dim
        for index, group in enumerate(self.GROUPS):
            if any(term in lowered for term in group):
                vector[index] = 1.0
        # A little lexical signal so ties break deterministically.
        for token in re.split(r"[^a-z0-9]+", lowered):
            if token:
                vector[len(self.GROUPS) + (hash(token) % 8)] += 0.05
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]
