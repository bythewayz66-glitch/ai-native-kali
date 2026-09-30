# Kanban Phase 1 delivery plan — workstreams, owners, exit criteria

Blueprint ref: section 09 (roadmap), Kanban-first sequencing. This is the plan for
**Phase 1 (weeks 0–6)**: the Kanban core plus shell integration, the foundation everything
else stands on.

**Status column legend:** ✅ done and verified · 🔄 in progress · ⬜ not started.
Status as of the Phase 2 build (2026-09-26).

---

## Roles and owners

Single-letter owner codes are used in the tables below.

| Code | Owner | Responsibility |
|---|---|---|
| **K** | Kanban core | card/board model, state machine, event bus, persistence |
| **A** | Agent runtime | CrewAI bridge, crews, gate, model client |
| **T** | Tool frontends | tool registry, guardrails, sandbox, audit, MCP |
| **S** | Shell / UX | Hermes shell panel, board UI, Kanban surfaces |
| **O** | Observability | collector, panels, replay, alerts |
| **P** | Platform / Packaging | live-build, metapackages, session units, CI |
| **Sec** | Security review | threat model, guardrail sign-off, responsible-use copy |

The tool frontends workstream is interesting: T tools are runnable — see the run
instructions in `README.md` and the browser-based flow in
[docs/T2_TOOL_CALL_DATAFLOW.md](T2_TOOL_CALL_DATAFLOW.md) for a visual play-by-play.

---

## Workstream 1 — Kanban core (owner K)

The backbone. Nothing else is trustworthy until this is.

| # | Task | Owner | Depends on | Exit criteria | Status |
|---|---|---|---|---|---|
| 1.1 | Typed Card/Board/Trace/Artifact/Approval/Event models | K | — | models validate; field set matches Appendix A | ✅ |
| 1.2 | Six-column lifecycle + transition guards | K | 1.1 | every illegal edge refused; **agent cannot write `Done`** | ✅ |
| 1.3 | Event bus + hash-chained event log | K | 1.1 | `prev_hash‖record → sha256`; `verify` detects tampering | ✅ |
| 1.4 | SQLite persistence | K | 1.1 | survives restart; events + audit readable after | ✅ |
| 1.5 | REST CRUD + `/can-move` pre-flight | K | 1.2 | 409 with machine-readable reasons on refusal | ✅ |
| 1.6 | WebSocket `/ws/events` + `/ws/board/{id}` | K | 1.3 | snapshot-on-connect + heartbeat; board view streams | ✅ |
| 1.7 | Seed boards (agent, engagement, system, personal) | K | 1.4 | four boards, realistic starter cards | ✅ |
| 1.8 | State-machine + event-emission tests | K | 1.1–1.6 | 99 tests green | ✅ |

**Workstream exit:** a card can be created, moved only along legal edges, and every change
is independently re-verifiable from the event log. ✅ **Met.**

---

## Workstream 2 — Agent runtime & the gate (owner A)

Makes cards move on their own — safely.

| # | Task | Owner | Depends on | Exit criteria | Status |
|---|---|---|---|---|---|
| 2.1 | Role registry with **per-role tool ceilings** | A | 1.1 | role cannot exceed its tier even if the card binds a higher tool | ✅ |
| 2.2 | Six crews + board→crew resolution | A | 2.1 | each crew runs end to end | ✅ |
| 2.3 | Bridge loop: claim → scope pre-check → run → traces → `Review` | A | 1.5, 2.2 | card completes with no external nudge | ✅ |
| 2.4 | Board's own approval API as the human gate | A, K | 1.5 | gate maps to a pending approval on the card | ✅ |
| 2.5 | Event-driven claim over `/ws/events` (poll = fallback) | A | 1.6 | claim latency < 50 ms; claim source counted separately | ✅ |
| 2.6 | Approved-gate re-claim (parked card resumes) | A | 2.4, 2.5 | approve ⇒ run resumes; reject ⇒ no re-claim; timeout ⇒ blocked | ✅ |
| 2.7 | **Real local model** behind the same interface | A | 2.3 | Ollama-compatible; deterministic fallback with no model | ✅ |
| 2.8 | `@human_feedback` bound to the card gate | A | 2.6, 2.7 | gate blocks the LLM step, not just a check | ✅ |
| 2.9 | Bridge + gate + model tests | A | 2.1–2.8 | 88 tests green (29 bridge, 25 stream, 34 model/gate) | ✅ |

**Workstream exit:** a card is claimed ~instantly, runs under a model or the deterministic
runner, and **cannot** proceed past an unanswered gate. ✅ **Met.**

---

## Workstream 3 — Tool frontends & guardrails (owner T)

The only path from an agent to a real binary.

| # | Task | Owner | Depends on | Exit criteria | Status |
|---|---|---|---|---|---|
| 3.1 | Typed tool schema + allowlist argument renderer | T | — | a value can never expand into extra argv tokens | ✅ |
| 3.2 | Guardrail engine (tier, target, scope, gate, live-unlock) | T | 3.1 | every decision returns reasons, never a bare boolean | ✅ |
| 3.3 | Sandboxed runner (no shell, rlimits, timeout, output caps) | T | 3.1 | `shell=False`; **dry-run executes nothing** | ✅ |
| 3.4 | Hash-chained tool audit log + `verify_chain` | T | 3.2 | tampering reported with the exact broken `seq` | ✅ |
| 3.5 | First real wrappers (7: whois, dig, nmap, httpx, nikto, sqlmap, log_rotate) | T | 3.1–3.4 | real argv, real tiers, real limits | ✅ |
| 3.6 | MCP stdio transport (JSON-RPC 2.0) | T | 3.5 | `initialize`/`tools/list`/`tools/call` + working client | ✅ |
| 3.7 | Shared live-unlock policy (HTTP + MCP agree) | T, Sec | 3.6 | one module answers the live question for both | ✅ |
| 3.8 | Guardrail + transport tests | T | 3.1–3.7 | 85 tests green (60 tool, 25 MCP) | ✅ |

**Workstream exit:** an external MCP client can call the tool layer, and every call —
allowed or refused — lands in a verifiable chain. ✅ **Met.**

---

## Workstream 4 — Shell, UX and Kanban surfaces (owner S)

| # | Task | Owner | Depends on | Exit criteria | Status |
|---|---|---|---|---|---|
| 4.1 | Shell panel: six live columns + board tabs | S | 1.6 | renders real board state, not fixtures | ✅ |
| 4.2 | Taskbar widget (cards/running/blocked/gates/runs) | S | 4.1 | counts match the API | ✅ |
| 4.3 | Notification feed with inline approve/reject | S | 2.4 | a human can clear a gate from the feed | ✅ |
| 4.4 | Card drawer: scope, result, traces, artifacts, replay | S | 4.1 | opens from any card | ✅ |
| 4.5 | Standalone drag-and-drop board app | S | 1.5, 1.6 | drag between lanes; **illegal drops refused with reasons** | ✅ |
| 4.6 | Escape untrusted card titles/labels | Sec, S | 4.1 | agent-authored text cannot inject markup | ✅ |
| 4.7 | Panel + board tests | S | 4.1–4.6 | 26 tests green (9 shell, 17 board) + Node suite | ✅ |

**Workstream exit:** a human can see the board and act on it (approve, drag, inspect)
without touching an API. ✅ **Met.**

---

## Workstream 5 — Observability (owner O)

| # | Task | Owner | Depends on | Exit criteria | Status |
|---|---|---|---|---|---|
| 5.1 | Collector with per-source cursors | O | 1.3, 3.4 | idempotent; survives restart | ✅ |
| 5.2 | Metrics: latency/tokens/cost, per-agent/tool/tier | O | 5.1 | **empty stack reports zeros, not plausible numbers** | ✅ |
| 5.3 | Panels (timeline, traces, tokens, audit, alerts) | O | 5.2 | one endpoint per blueprint 04.x panel | ✅ |
| 5.4 | Per-card replay (events + traces + audit merged) | O | 5.1 | an ordered, replayable history | ✅ |
| 5.5 | Alert rules (incl. `card_blocked`, chain-broken) | O | 5.2 | fires on real conditions only | ✅ |
| 5.6 | Live dashboard page | O | 5.3 | reads the live API | ✅ |
| 5.7 | Observability tests | O | 5.1–5.6 | 32 tests green | ✅ |

**Workstream exit:** every agent action is visible and replayable from one surface. ✅ **Met.**

---

## Workstream 6 — Platform, packaging & integration (owner P)

| # | Task | Owner | Depends on | Exit criteria | Status |
|---|---|---|---|---|---|
| 6.1 | `make dev` boots the whole stack | P | 1–5 | all six services healthy on ports 8081–8086 | ✅ |
| 6.2 | Shared `PYTHONPATH` + health-gated startup | P | 6.1 | no manual ordering needed | ✅ |
| 6.3 | End-to-end smoke test | P | 6.1 | **86/86 checks**, non-zero exit on failure | ✅ |
| 6.4 | live-build config tree (`lb config` for kali-rolling) | P | — | real config, amd64 hybrid ISO | ✅ |
| 6.5 | Three metapackages with `DEBIAN/control` | P | 6.4 | core / shell / agents | ✅ |
| 6.6 | Hermes as a **session** (`.desktop` + `kali-ai.target`) | P | 6.5 | registers a Wayland session, not an app | ✅ |
| 6.7 | First-boot wizard (offline default, live tools OFF) | P, Sec | 6.6 | safe defaults out of the box | ✅ |
| 6.8 | CI: full suite on every change | P | 6.1–6.3 | 330 tests + Node suite + smoke in one run | ✅ |
| 6.9 | `docker-compose.yml` one-command bring-up | P | 6.1 | parity with `make dev` | ✅ |
| 6.10 | Build status doc (implemented vs stubbed) | P | all | written before delivery, kept current | ✅ |

**Workstream exit:** a new machine reaches a verified, running stack in one command. ✅ **Met.**

---

## Workstream 7 — Security & responsible use (owner Sec)

Runs **across** all workstreams from day 0 — it is a gate, not a phase.

| # | Task | Owner | Depends on | Exit criteria | Status |
|---|---|---|---|---|---|
| 7.1 | Threat model: what an agent might try, and what stops it | Sec | 1.1 | each threat has a named mitigation | ✅ |
| 7.2 | Scope enforcement at **two** independent layers | Sec, T, A | 1.5, 3.2 | a compromised bridge cannot bypass the tool layer | ✅ |
| 7.3 | Live execution off by default; per-tool unlock | Sec, T | 3.2 | no live call without an explicit operator act | ✅ |
| 7.4 | Refusals are explicit, never silent downgrades | Sec, T | 3.2 | a refused live call says `denied`, not `dry_run` | ✅ |
| 7.5 | Kill switch on a running card | Sec, K | 1.5 | operator can stop a card mid-flight | ✅ |
| 7.6 | Responsible-use + authorization copy | Sec | 6.7 | present in the wizard and the docs | ✅ |
| 7.7 | Guardrail regression suite | Sec, T | 7.1–7.6 | T2 gate holds, out-of-scope never enters `Running` | ✅ |

**Workstream exit:** every safety property has an automated test that would fail if it
regressed. ✅ **Met.**

---

## Sequencing (why this order)

```
W1  Kanban core            ██████░░░░░░░░░░░░░░░░  (weeks 1-2)
W2  Tool layer + guardrails  ░░██████░░░░░░░░░░░░  (weeks 2-4, parallel with W3)
W3  Runtime bridge + gate    ░░░░██████░░░░░░░░░░  (weeks 3-5, needs W1+W2)
W4  Shell + observability    ░░░░░░░░██████░░░░░░  (weeks 4-6, needs W1 state)
W5  Platform + CI            ░░░░░░░░░░░░██████░░  (continuous from week 1)
W6  Security review          ██░░██░░██░░██░░██░░  (gates every workstream)
```

W2 and W3 can overlap because the guardrail interface (typed spec in, decision out) was
frozen in week 1. W4 is deliberately last of the build tracks: a shell drawn before the
state machine settled would have been redrawn twice.

---

## Definition of done (Phase 1)

Phase 1 is complete when **all** of these hold on a clean checkout:

1. `make dev` brings six services to healthy with no manual steps.
2. `make smoke` passes **86/86**, including T2 gate and out-of-scope paths.
3. `make test` passes 330 Python tests + the Node logic suite.
4. A card created over REST is claimed over the **socket**, runs a real tool in dry-run,
   records a trace and a hash-chained audit row, and lands in `Review` — with no external
   nudge.
5. An approved gate resumes the parked card; a rejected gate does not.
6. `GET /api/audit/verify` and the MCP `kali/audit/verify` both report an intact chain.
7. No tool executes live without `TOOLS_LIVE=1` **and** a per-tool unlock.
8. Every item in this document marked ✅ has a test that fails if it regresses.

---

## What Phase 1 deliberately does **not** include

Listed so nobody mistakes scope for completeness.

| Deferred | Why | Lands in |
|---|---|---|
| ~70 tool wrappers (7 today) | breadth follows a proven loop | Phase 2 |
| Real window-manager chrome (tiling, start menu, file manager) | panel-first proved the integration surface cheaply | Phase 2 |
| Prompt/response inspector, model-serving heartbeats | needs a real serving layer | Phase 3 |
| Memory/knowledge stores | needs real crews | Phase 3 |
| A built ISO | needs a Kali build host, root and network | Phase 4 |
| Desktop widget/overlay, drag-and-drop targets from the file manager | depends on real shell chrome | Phase 2/3 |

---

## Immediate next actions (Phase 2)

| Action | Owner | Exit criteria |
|---|---|---|
| Grow wrappers 7 → 25 (add SMB, DNS, TLS, system categories) | T | each has a real schema, tier, limits and tests |
| Add file-manager drag-and-drop onto cards | S | dropping a target file opens a proposed card |
| Desktop overlay widget for the board | S | always-visible card state |
| Retention policy + alert routing | O | logs bounded; alerts reach the shell |
| First ISO dry-run build on a Kali host | P | `lb build` completes; image boots |

---

## Phase 1 → Phase 4 rollup

Phase 1 is the detailed plan above. This is the whole programme at a glance, so the
phase boundaries and the hand-offs between them are explicit rather than implied.

The sequencing rule throughout: **nothing is scheduled before the thing it depends on
is real.** That is why Kanban core precedes everything (the board *is* the work queue),
and why the ISO comes last — an ISO built earlier would package components that were
still moving.

| Phase | Ships | Depends on | Exit criteria | Owner |
|---|---|---|---|---|
| **1 — Kanban foundation** | `kanban-core`, `agent-runtime` (bridge + gate), `tool-frontends` (7 wrappers), `observability`, `hermes-shell`, packaging skeleton | — | Six-column lifecycle with guards; card → crew → tool → trace → audit → `Review` with no external nudge; guardrail paths hold | K, A, T, O, S, P |
| **2 — Real transports & interactive surfaces** | Event-stream claim, **MCP stdio** transport, drag-and-drop `board-ui`, local-model crew path, **L6 memory store**, tool breadth 7 → 30 | Phase 1 loop verified | Claims come from the socket with **0** fallback polls; an external MCP client drives the tool layer; memory is engagement-isolated; 494 tests green | A, T, S, O |
| **3 — Orchestration depth** | All crews on the model path; vector + graph recall behind the memory API; sub-cards; remediation crew; desktop file-manager drag-and-drop and overlay widget | Phase 2 transports and memory | Every crew runs with a model **or** the deterministic runner; a card can spawn a child card that cannot widen scope; memory recall improves a run measurably | A, T, S |
| **4 — Distribution** | ISO pipeline executed for real; metapackage payloads; wizard wired to the shell; hardware profiles; offline model bundle | Phases 1–3 stable | `lb build` produces a bootable image; first-boot wizard configures the model mode; Hermes starts as the session, not an app | P, Sec |

### Dependencies that are easy to get wrong

- **The board must precede the crews.** A crew needs somewhere to claim work from; a
  crew-first build would have to invent a queue and then rebuild it.
- **The gate must precede the model path.** Gate-first means the gate is load-bearing
  from day one; model-first means a gate is retrofitted and can always be argued around.
- **Memory must precede Phase 3 crews.** A crew with no recall repeats work and cannot
  be shown to improve; the store has to exist first for its value to be measurable.
- **The ISO must come last.** It is a packaging step, not a capability step — building
  it early produces an image whose contents change underneath it every week.

### Cross-phase risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| Tool breadth outruns the guardrail invariants | 30 wrappers are only safe because every one is tier-checked; a rushed 70 could include one that is not | Tests enforce the invariants per wrapper, so a non-compliant wrapper fails the build rather than shipping |
| Phase-3 crews require a model that a build host may not have | CI must not depend on a GPU or an API key | The deterministic runner stays in the tree permanently as the no-model path |
| The wizard and the ISO diverge | A wizard that configures a system the ISO does not ship is worse than none | Phase 4 keeps the wizard in the same repo as the packaging config, wired to the shell's own settings surface |

---

## Cross-references

- One call, traced end to end: [T2_TOOL_CALL_DATAFLOW.md](T2_TOOL_CALL_DATAFLOW.md)
- Crew definitions and gate wiring: [CREWAI_DEFINITIONS.md](CREWAI_DEFINITIONS.md)
- Field-level schema: [APPENDIX_CARD_SCHEMA.md](APPENDIX_CARD_SCHEMA.md)
- What is actually built: [../BUILD_STATUS.md](../BUILD_STATUS.md)
