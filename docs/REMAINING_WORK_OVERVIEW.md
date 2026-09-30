# Remaining work — consolidated overview

**Date:** 2026-09-27 · **Covers:** the whole programme (Phases 1–5), not just Phase 4
**Baseline for all counts:** `documents/ai-native-kali_v3/BUILD_STATUS.md` (the last fully green tree)

---

## 0. Read this first — which tree is trustworthy

There are four versioned trees and they are **not** in the same state. This is the single most
important thing in this document, because every other number depends on it.

| Tree | State | Meaning |
|---|---|---|
| `documents/ai-native-kali` | Phase 1 | historical |
| `documents/ai-native-kali_v2` | Phase 2 | historical |
| `documents/ai-native-kali_v3` | **GREEN — last verified baseline** | 1249 tests, 0 failed, 12 skipped · 128/128 smoke · 73 wrappers · 7 services |
| `documents/ai-native-kali_v4` | **RED — current head, Phase 5 unfinished** | a new run added Phase 5 code and tests, and left the suite failing |

**The head (`_v4`) is not green.** A full-suite run against it reports failures; a repeat run
reported a *different count* (7 then 3), and the affected files pass when run in isolation.
That is a real **test-isolation / shared-state defect** in the new Phase 5 code, not a flake to
re-run away. Three of the failures are deterministic even for the file on its own.

`docs/VERIFICATION.md` in the head still describes itself as a *"Phase 4 acceptance run"*, and
its `make test` transcript omits the `tests` (packaging) path — so it reads `1108` where the
verified total is `1249`. The head's docs therefore **do not describe the head**.

### Verified baseline counts (from `_v3`)

| Signal | Value |
|---|---|
| Test suite | **1249 passed**, 0 failed, 12 skipped |
| `make smoke` | **128/128** |
| Tool wrappers | **73**, across 12 categories |
| Services healthy | **7** (8081–8087) |
| Board logic (Node) | **25 passed**, 0 failed |

Per component: `kanban-core` 99 · `agent-runtime` 155 · `tool-frontends` 685 (+12 skipped) ·
`observability` 32 · `hermes-shell` 82 · `board-ui` 17 · `memory-store` 148 ·
`tests` (packaging) 31 — sum exact: 99+155+685+32+82+17+148+31 = **1249**.

The head adds two Phase 5 test files — `observability/tests/test_phase5.py` (49 tests) and
`agent-runtime/tests/test_phase5_memory_seed.py` (19 tests) — **68 new tests whose suite does
not currently pass**. No new green total is claimed for the head.

### Status vocabulary

- **Blocked** — cannot be finished in this environment; an external dependency is named.
- **In progress — broken** — code exists, tests fail; not "nearly done".
- **Stubbed** — a placeholder exists but no real behaviour behind it.
- **Deferred** — deliberately postponed; the reason is recorded.
- **Unstarted** — not begun.
- **Policy** — deliberate and complete. A designed limit, not a gap.

---

## 1. ISO and packaging

| # | Item | Component | Why still open | What it takes to finish | Size | Pri |
|---|---|---|---|---|---|---|
| A1 | **Bootable ISO image** | `packaging/` | **Blocked.** The staged half is real (3 `.deb` metapackages with real payloads, a 251 KB chroot overlay, a sha256 manifest, reproducible across two independent builds). `make iso-full` correctly exits 1: `live-build ('lb') is not installed`. | A **Kali build host** with `live-build`, `xorriso` and **root**; then a real `lb build` and a boot test. Cannot be done in this sandbox — no root-level image tooling. | **L** | P2 (blocked) |
| A2 | **Hardware profiles** | `packaging/` | Unstarted. Nothing to profile until an image boots. | Depends on A1. Then per-profile package sets and a boot matrix on real hardware/VM. | M | P2 (blocked by A1) |
| A3 | **Offline model bundle** | `packaging/` + `agent-runtime/` | Unstarted. No model is bundled — the runtime points at *your* Ollama. | Choose and licence a quantised model, size it against the image budget, and bake it in. Preparation can start now; shipping depends on A1. | M | P2 (blocked by A1) |
| A4 | **Pak / Hermes distribution packaging** | `packaging/` | **Deferred** — only the live-build metapackages exist. | Decide the distribution format, then a second packaging target. | M | P2 |
| A5 | **Setup wizard wired to the shell** | `packaging/` + `hermes-shell/` | **Deferred.** The plan flags the risk explicitly: *a wizard that configures a system the ISO does not ship is worse than none.* | Keep the wizard in the same repo as the packaging config and drive it from the shell's own settings surface. | M | P1 |

**Honest read:** packaging is *reproducible and verified* up to the point where it needs root
and a Kali host. Nothing about the bootable image is "almost there" — it is blocked on tooling
that is not present.

---

## 2. Shell and desktop depth

| # | Item | Component | Why still open | What it takes to finish | Size | Pri |
|---|---|---|---|---|---|---|
| B1 | **Real compositor client** | `hermes-shell/` | **Blocked.** `wm.py` is a genuine, tested state machine (open / focus / minimize / maximize / restore / close, z-order, taskbar; unknown action → `400`). But the session is still a **browser-hosted panel**, not a GTK/Qt/Wayland client. | A **real compositor and display server**. A headless sandbox cannot host or verify one. | **L** | P2 (blocked) |
| B2 | **Start-menu integration** | `hermes-shell/` | Unstarted. | A launcher surface over the installed tools; small, self-contained. | S–M | P1 |
| B3 | **Drag *targets* onto cards** | `hermes-shell/` + `board-ui/` | Unstarted — only *files* can be dropped today. | A target/entity picker fed by the memory knowledge graph, plus the drop handler. | M | P1 |
| B4 | **Overlay alert routing** | `hermes-shell/` + `observability/` | Half done. The overlay exists and reflects live board + service state; the *routing* of alerts to it is the Phase 5 observability item (F4). | Finish F4, then surface the delivered alerts in the overlay. | S | P1 (after F4) |
| B5 | **Window-manager depth** | `hermes-shell/` | Unstarted — tiling, snapping, virtual desktops are not implemented. | Extend the state machine; testable headless. | M | P2 |
| B6 | **Full file-manager surface** | `hermes-shell/` | Stubbed beyond the attach path. Dropping an entry onto a card works (through the board's own guarded route); there is no real filesystem browser. | A browse surface over the sandbox FS. | M | P2 |

---

## 3. Crews and the model path

| # | Item | Component | Why still open | What it takes to finish | Size | Pri |
|---|---|---|---|---|---|---|
| C1 | **Only 2 of the crews are specced for the model path** | `agent-runtime/` | **Partly by design.** `recon` and `vuln-assessment` have `agents.yaml`/`tasks.yaml`; `reporting` and `system` have none. An equivalence test asserts the specced set is exactly `{recon, vuln-assessment}` — so the gap is enforced, not hidden. | Author the remaining crews' YAML and widen the equivalence test. | M | P1 |
| C2 | **`crewai` is an optional adapter** | `agent-runtime/` | **Deferred.** Used only if `crewai` is installed, so CI stays key-free and GPU-free. | Pin a version and add a real install path + a contract test. | S–M | P2 |
| C3 | **Local model serving** | `agent-runtime/` + `packaging/` | **Blocked on a reachable endpoint.** Client + Ollama embedder are implemented; no model is bundled and none is reachable here. | Point `MODEL_BASE_URL` / `MEMORY_EMBED_URL` at a real Ollama and validate end to end. | S to wire · external to validate | P1 (blocked) |
| C4 | **Memory seed → the plan** *(Phase 5, item 1)* | `agent-runtime/` | **In progress — BROKEN.** The bridge now derives a *graph seed* from recall and hands it to the planner (`bridge._memory_seed`, `model_client._memory_section`), and counts it (`BridgeStats.memory_seeds`). But `test_phase5_memory_seed.py` **fails 3 of 19 tests deterministically**, and 2 further `test_llm_crew.py` tests fail only in the combined run. | Fix the shared-state/isolation defect and the three deterministic failures, then re-verify the whole suite. **Do not report this as nearly done** — the head is red. | M | **P0** |
| C5 | **Sub-cards** | `kanban-core/` + `agent-runtime/` | Unstarted. Named in the Phase 3 plan: a card may spawn a child card that **cannot widen scope**. | Model parent/child links, inherit-and-narrow scope on the child, block escalation. | M–L | P2 |
| C6 | **Remediation crew** | `agent-runtime/` | Unstarted. | A new crew with its own tier ceilings and YAML, plus the gate wiring. | L | P2 |

**Note:** the human-in-the-loop `@human_feedback` gate itself is **implemented and working**
(Phase 2) and verified by the smoke run — a card in `Review` blocks until a human acts. What is
missing is the *gate wiring for the crews that have no YAML yet* (C1), not the gate.

---

## 4. Embeddings and memory

| # | Item | Component | Why still open | What it takes to finish | Size | Pri |
|---|---|---|---|---|---|---|
| D1 | **Embedder swap never exercised against a real model** | `memory-store/` | **Blocked on a reachable endpoint.** Selection, fallback, latching and `drift` are all covered with a *stubbed* endpoint; no real embedding model was reachable here. | A live Ollama with an embedding model; run the same assertions against it. | S | P1 (blocked) |
| D2 | **Vector backend is lexical, not semantic** | `memory-store/` | **Implemented with a declared caveat.** The hashing fallback finds what a phrase *looks like*, not what it *means*: `"unpatched apache"` will not match `"CVE-2021-41773"` by itself. The Ollama backend is in behind the same `VectorIndex` interface. | Swap the default backend and **quantify** the improvement — which needs D5. | M | P1 |
| D3 | **Graph extraction is rule-based** | `memory-store/` | **Implemented with a declared caveat.** Entity kinds come from patterns (IP/FQDN/MAC/CVE) and fact-key prefixes; a fact written without a recognisable shape contributes **no edges** — so the graph grows with evidence *formatting*, not just volume. | Broaden the extractor, or move to a model-assisted pass. | M | P2 |
| D4 | **Richer graph queries** | `memory-store/` | Unstarted. Query/retrieval and the composed `/recall/bundle` exist; path queries and entity drill-down do not. | Add traversal queries + a surface for them. | M | P2 |
| D5 | **Retrieval-quality harness** | `memory-store/` | Unstarted. There is no fixed corpus and no recall@k metric, so D2 cannot be measured — only asserted. | A labelled corpus, recall@k and MRR, run against both backends. | M | P1 |

---

## 5. Tool breadth and depth

| # | Item | Component | Why still open | What it takes to finish | Size | Pri |
|---|---|---|---|---|---|---|
| E1 | **Per-category depth** | `tool-frontends/` | Target **met** (73 vs the blueprint's ~70, across 12 categories). What remains is *depth*: some categories are thin (reporting, wireless, password-attacks were called out). | Add wrappers where a category is shallow. Each needs schema + tier + scope validation + dry-run default + tests. | S–M each | P1 |
| E2 | **Live tool execution is locked off** | `tool-frontends/` | **Policy, not a gap.** `TOOLS_LIVE=0` is the correct default for a security platform; unlocking is a deliberate operator act. | Nothing to build. Keep the default and document the unlock. | — | — |
| E3 | **Scope exemptions are a human decision** | `tool-frontends/` | **Policy boundary, disclosed.** The lint proves an exemption was *written down*, not that it was *correct*. Two uses exist (`wifi_deauth.client`, local-only `target=localhost` tools). | Reviewer sign-off per exemption. A lint cannot do this. | — | ongoing |

**Note:** every new wrapper is enforced by a test that **fails the build** if the wrapper omits
its guardrail tier — so E1 cannot silently regress the safety model.

---

## 6. Observability *(Phase 5 — code exists, tree is red)*

All four modules below are substantial real code, imported by a 49-test Phase 5 suite. They are
listed as **in progress — broken** because the suite does not pass.

| # | Item | Component | Why still open | What it takes to finish | Size | Pri |
|---|---|---|---|---|---|---|
| F1 | **Prompt/response inspector** | `observability/model_calls.py` | In progress — broken. Redacts secrets **before** storage (and counts what it removed) and truncates with a declared `truncated: true`. | Make the suite green; verify the redaction count surfaces. | M | **P0** |
| F2 | **Model-serving heartbeats** | `observability/model_health.py` | In progress — broken. Records every attempt (not just successes) plus `transitions`, so "down for an hour" is distinguishable from "flapped once". One test failed in a combined run and passed alone → isolation defect. | Fix the isolation defect. | M | **P0** |
| F3 | **Retention policy** | `observability/retention.py` | In progress — broken. Correctly **refuses** to prune hash-chained stores, because pruning one row breaks every later digest and makes `verify_chain` report tampering — a benign explanation for a tamper alarm. | Make the suite green. | M | **P0** |
| F4 | **Alert routing** | `observability/alerting.py` | In progress — broken. Dedupe per (kind, card) with a window; severity routing with a floor so `info` never leaves the shell feed. | Make the suite green; unblocks B4. | M | **P0** |
| F5 | **Collector buffers are in-memory deques** | `observability/collector.py` | **Deferred.** Re-derivable from the two source-of-truth logs, so a restart re-pulls rather than resuming. | Persist the cursors/buffers if restart continuity is wanted. | S–M | P2 |
| F6 | **Percentiles use nearest-rank** | `observability/` | **Deferred.** Fine at this scale. | Move to a histogram when latency volume grows. | S | P2 |

---

## 7. Docs and verification

| # | Item | Where | Why still open | What it takes to finish | Size | Pri |
|---|---|---|---|---|---|---|
| G1 | **The head's docs describe a different tree** | `_v4/BUILD_STATUS.md`, `README.md`, `docs/VERIFICATION.md` | The head has **no Phase 5 section** in `BUILD_STATUS.md` or `README.md`; `VERIFICATION.md` still self-describes as *"Phase 4 acceptance run"* and its transcript omits the packaging path (reads `1108`, not `1249`); three per-component headings are stale from Phase 3 (`agent-runtime` 137, `hermes-shell` 9, `memory-store` 129) and contradict the correct 155 / 82 / 148. | Update all three to state the Phase 5 work as **unfinished**, with the failing-test evidence. | S | **P0** |
| G2 | **No Phase 5 completion report** | `webpages/` | Not written — correctly, because Phase 5 is not complete. | Write it **after** P0 lands and the suite is green. | S–M | P1 |
| G3 | **Green baseline vs red head is unresolved** | repo root | The head is red while the last verified tree is green, and nothing says which one a reader should trust. | Either finish Phase 5 to green, **or** roll the head back to `_v3`'s verified state. Decide explicitly. | S | **P0** |

---

## 8. Sequencing

**P0 — do first (nothing else can be trusted while the head is red)**
1. **G3** — decide: finish Phase 5, or roll the head back to `_v3`.
2. **C4** — fix the memory-seed isolation defect + 3 deterministic failures.
3. **F1–F4** — finish the Phase 5 observability modules.
4. **G1** — bring the head's docs in line with reality.

**P1 — next**
- **C1** (remaining crews' YAML) · **D5** (retrieval harness) · **D2** (real embedding default, measured)
- **E1** (per-category tool depth) · **B2 / B3** (start menu, target drag) · **B4** (after F4) · **G2**
- **C3 · D1** — same blocker: a reachable model endpoint.

**P2 — later or blocked**
- **Blocked:** **A1** (Kali build host + `live-build` + `xorriso` + root) → **A2, A3** · **B1** (real compositor)
- **Not blocked, just not started:** **A4, A5, B5, B6, C2, C5, C6, D3, D4, F5, F6**

---

## 9. Genuinely blocked vs merely unstarted

**Genuinely blocked — external dependency, named:**

| Item | Blocker |
|---|---|
| A1 bootable ISO | Kali build host · `live-build` · `xorriso` · **root** |
| A2 hardware profiles, A3 model bundle | depend on A1 |
| B1 real compositor client | a real compositor / display server (cannot be hosted headless) |
| C3 model serving, D1 embedder verification | a **reachable model endpoint** |

**Merely unstarted — no external dependency, just work:** A4, A5, B2, B3, B5, B6, C1, C2,
C5, C6, D3, D4, D5, E1, F5, F6.

**Not gaps at all — deliberate policy:** E2 (live execution locked off), E3 (scope exemptions
are a human decision).

**In progress but broken — reported as such, not as nearly-done:** C4, F1, F2, F3, F4.

---

## 10. The one-line honest summary

Phase 1–4 are verified green at **1249 tests / 128 smoke checks / 73 wrappers** in
`_v3`. Phase 5 is **partially written and currently red** in `_v4` — five items (C4, F1–F4) have
real code and a failing suite, and the decision in **G3** (finish or roll back) blocks
everything else. The bootable ISO, the real compositor and live-model verification are **blocked
on hardware or endpoints this environment does not have**; everything else is ordinary
unstarted work.
