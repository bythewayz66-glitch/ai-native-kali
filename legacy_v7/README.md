# AI-native Kali — Phase 1 + 2 + 3 scaffold

Working code for the Kanban-first foundation of an AI-native security OS that
fuses **Kali Linux**, **Hermes Desktop** and **CrewAI**.

This repository is the built slice of the architecture blueprint: the orchestration
backbone, the agent bridge, the guardrailed tool layer, the observability service,
a demonstrable desktop-shell surface, and an interactive board. It runs on a laptop
with no LLM keys and no network.

> **Not** a finished distribution. See `BUILD_STATUS.md` for exactly what is
> implemented, what is stubbed, and what comes next.

---

## The one idea

**Every unit of work on the system is a card on a board.** Agent tasks, security
workflows, system jobs and user requests are all cards; cards move through
`Backlog → Assigned → Running → Review → Done/Blocked`; column transitions are
events on the bus; and every tool call a crew makes is written to a hash-chained
audit log. The board is simultaneously the task queue, the observability
surface and the human control plane.

Since Phase 2 the bridge **subscribes to the board's event stream**: a card that
becomes claimable is picked up the moment the event lands, with polling kept only
as a fallback while the socket is unhealthy. The two claim paths are counted
separately, so a dead socket cannot look healthy.

```
                    ┌──────────────────────┐
   Hermes Desktop   │   Hermes Shell panel │  taskbar widget, notification feed,
   (the session)    │        :8085         │  board columns, card drawer
                    └───────────┬──────────┘
                                │ HTTP (thin client)
                    ┌───────────▼──────────┐
                    │      kanban-core     │  cards, boards, lifecycle state machine,
                    │        :8081         │  event bus, hash-chained event log
                    └───┬──────────────┬───┘
          Assigned cards│              │events
        ┌───────────────▼───┐      ┌───▼──────────────────┐
        │   agent-runtime   │      │    observability     │  timeline, tokens/cost/latency,
        │       :8082       │      │        :8084         │  traces, alerts, card replay
        │  Kanban⇄CrewAI    │      └───▲──────────────────┘
        │  bridge + crews   │          │ audit rows
        └───────────┬───────┘      ┌───┴──────────────────┐
                    │ tool calls │   tool-frontends     │  typed specs, T0–T3 guardrails,
                    └───────────►│        :8083         │  scope checks, sandbox, audit log
                                 └──────────────────────┘
```

---

## Components

| Directory | Service | Port | What it actually does |
|---|---|---|---|
| `kanban-core/` | Orchestration backbone | 8081 | Card/board models, lifecycle state machine with transition guards, async event bus, SQLite store, hash-chained event log, REST + WebSocket API, seed boards |
| `agent-runtime/` | Kanban ⇄ CrewAI bridge | 8082 | Role registry with tier ceilings, crew definitions, CrewAI adapter (real CrewAI when installed, deterministic local runner otherwise), **event-stream claim** with poll fallback, local-model crew path, human-feedback gate, the bridge loop that claims cards and writes results back |
| `tool-frontends/` | Kali tool layer | 8083 | Typed tool specs, T0–T3 guardrail engine, **value-based scope enforcement** (every address-shaped argument is checked, declared or not), sandboxed execution (argv, no shell, rlimits), hash-chained tool audit log, **73 wrappers** across 12 categories, and **two transports**: HTTP and a real **MCP stdio** server |
| `observability/` | Collector + panels | 8084 | Ingests card events + tool audit rows; agent timeline, token/cost/latency, tool-call traces, model health, alerts, full card replay; live dashboard |
| `hermes-shell/` | Desktop shell panel | 8085 | The Kanban surfaces of the desktop shell: board columns, taskbar widget, notification feed, approval actions, per-card drawer. Thin client — all state comes from the APIs |
| `board-ui/` | Interactive board | 8086 | Standalone **drag-and-drop** board: six lanes, live WebSocket stream, card drawer, inline approvals, quick-add, and a refusal toast that shows the guard's own reasons. Drag decisions live in a pure, Node-tested module |
| `memory-store/` | L6 memory | 8087 | Episodic + semantic memory, SQLite + FTS5 search, facts keyed and superseded rather than overwritten. Phase 3 added **vector recall** (hashing embeddings + cosine) and a **knowledge graph** (typed entities, evidence-weighted edges), fused behind `/recall/bundle`. **Strictly scoped per engagement** — a missing scope is a `400`, never a wider result set |
| `packaging/` | ISO pipeline | — | live-build config tree, metapackage definitions, Hermes-as-session `.desktop`, systemd units + `kali-ai.target`, first-boot wizard |
| `docs/` | Design deliverables | — | Data-flow trace, CrewAI definitions + YAML, card/schema appendix, Phase 1 delivery plan |

Every component imports the others **from the source tree** (`PYTHONPATH`), so
there is no install step and no packaging step to get in the way of running it.

---

## Run it

```bash
# one-time
python3 -m pip install -r requirements-dev.txt

# boot all six services (background, logs in var/log/)
make dev

# prove the whole loop end to end (86 checks)
make smoke

# the full test suite, plus the board's Node logic suite
make test && make test-js

# drive the tool layer over the real MCP stdio transport
make mcp

# stop
make stop
```

`make dev` prints the four URLs that matter:

| URL | What it is |
|---|---|
| http://127.0.0.1:8086/ | **Interactive board** — drag cards between lanes, approve gates inline |
| http://127.0.0.1:8085/panel | **Hermes shell panel** — live board, taskbar widget, notification feed, card drawer |
| http://127.0.0.1:8084/dashboard | **Observability dashboard** — timeline, traces, tokens, audit chain |
| http://127.0.0.1:8081/docs | Kanban core REST API docs |
| http://127.0.0.1:8087/docs | **Memory store** — episodes, facts, search, context |

Other targets: `make status`, `make logs`, `make test`, `make clean`. With Docker:
`docker compose up --build`.

### Running a single component

```bash
# everything shares one PYTHONPATH; export it once
export PYTHONPATH="$PWD/kanban-core:$PWD/tool-frontends:$PWD/agent-runtime:$PWD/observability:$PWD/hermes-shell:$PWD/board-ui:$PWD/memory-store"

# then any one service on its own
python3 -m uvicorn kanban_core.api:app        --port 8081
export KANBAN_SEED=1   # seeds the example boards
python3 -m uvicorn tool_frontends.server:app  --port 8083
python3 -m uvicorn agent_runtime.server:app   --port 8082
python3 -m uvicorn observability.server:app   --port 8084
python3 -m uvicorn hermes_shell.server:app    --port 8085
python3 -m uvicorn board_ui.server:app        --port 8086
python3 -m uvicorn memory_store.server:app    --port 8087

# the bridge only uses memory when asked; off by default so the system runs
# identically without it
MEMORY_ENABLED=1 MEMORY_DB=var/memory.db python3 -m uvicorn agent_runtime.server:app --port 8082

# tests for just one component
python3 -m pytest tool-frontends -p no:cacheprovider
```

---

## The loop, concretely

1. A card is created on a board and moved to **Assigned**.
2. The board publishes `card.assigned`; the bridge **sees it on the event stream**
   and claims the card — no poll timer involved.
3. The bridge resolves the crew for the board, and — if the card carries T2+ work —
   **opens an approval gate instead of running it**. The card stays in `Assigned`,
   parked. A human approving it publishes a decision the bridge re-claims on.
4. With the gate satisfied (or not required), the bridge moves the card to
   **Running** and executes the crew.
5. The crew's tools are invoked one at a time through `tool-frontends`, which
   validates arguments, enforces the tier, checks the target against the card's
   authorization scope, executes in dry-run (or sandboxed live), and appends a
   hash-chained audit row.
6. Traces and a content-addressed artifact are written back onto the card, the
   result text is attached, and the card moves to **Review**.
7. **Done belongs to the human.** The bridge never writes it — the state machine
   will refuse the transition.

`make smoke` asserts all of the above (86 checks), plus: the card really was claimed
over the socket with **zero fallback polls**; a T2 card waits for approval and then runs
after it is granted; an out-of-scope card is blocked before it ever enters Running; the
tool layer answers a full session over the **MCP stdio transport**; and the board UI
refuses an illegal drop with the engine's own reasons.

---

## Safety model

This is a security tooling platform, so the safety properties are load-bearing
rather than decorative. They are all enforced in code and covered by tests:

- **Dry-run by default.** `TOOLS_LIVE=0` means nothing executes, even when a
  caller asks for a live run — and a refused live request returns an explicit
  *denial*, never a silent downgrade to dry-run (a caller must never believe a
  scan happened when it did not).
- **Live execution is opt-in per tool.** `TOOLS_LIVE=1` *and* the tool named in
  `TOOLS_UNLOCK` are both required; `TOOLS_MAX_TIER` caps the ceiling.
- **Guardrail tiers T0–T3.** T2+ requires an authorization scope *and* a human
  approval; T3 additionally requires sandboxing. A tool spec that claims a tier
  without the matching flags is rejected at registration time.
- **Scope is enforced twice.** The bridge refuses to even claim an out-of-scope
  card, and the tool layer independently re-checks the target on every call.
- **No shell, ever.** Commands are tokenised with `shlex` and executed with
  `shell=False`; substituted values are allowlist-sanitised with whitespace
  removed so a value can never expand into extra arguments.
- **Two independent hash chains.** Every card event and every tool invocation is
  append-only and hash-linked (`prev_hash || canonical(record) → sha256`), with a
  verification endpoint for each.
- **One safety implementation, two transports.** The MCP stdio server and the HTTP
  server share the registry, guardrail engine, runner and audit log — and both answer
the live-unlock question from `policy.py`, so they cannot drift apart.
- **The engine is the authority.** The board UI checks a drop locally for speed and
  then submits it; kanban-core decides. A bug in the page cannot legalise a
transition the state machine refuses.
- **Silence is never approval.** A gate that times out blocks the step; it does not
default to yes.

---

## Where things live

```
kanban-core/kanban_core/        models, state_machine, bus, store, service, api, seed
kanban-core/tests/              state machine, events, API
agent-runtime/agent_runtime/    bridge, ws_events, crewai_adapter, llm_crew, model_client,
                                gate, crews, roles, client, server
agent-runtime/tests/            bridge, ws events, llm crew, crew-yaml equivalence
tool-frontends/tool_frontends/  spec, guardrails, policy, runner, audit, registry,
                                mcp_stdio, wrappers/, server
tool-frontends/examples/        mcp_client.py (a working MCP client)
observability/observability/    collector, metrics, server, static/dashboard.html
hermes-shell/hermes_shell/      server, static/index.html
board-ui/board_ui/              server, static/index.html, static/board_logic.mjs
board-ui/tests/                 server contract tests, Node logic tests
memory-store/memory_store/      models (Episode, Fact), store (SQLite + FTS5),
                                vector (hashing embeddings + cosine), graph (entities,
                                relations), server
docs/                           data-flow, CrewAI definitions, crews/*.yaml, schema appendix,
                                Phase 1 delivery plan
packaging/                      live-build/, metapackages/, hermes-session/, build-iso.sh
scripts/                        dev.sh, stop.sh, status.sh, smoke_test.py
```

---

## Design notes

- **The bridge only writes Review.** Moving a card to Done requires a human; the
  state machine enforces it independently of the bridge's intentions, so the
  property does not depend on the bridge behaving.
- **The bridge talks to the board over the public REST API.** There is no
  privileged back door, so it must satisfy the same guards any other client does.
- **The local crew runner is not a mock.** It executes the real pipeline — real
  tool calls, real guardrails, real audit rows — without an LLM in the loop. That
  is what makes the whole system testable in CI, and it is why the tests assert
  behaviour (denials, hashes, transitions) rather than prompt text.
- **Observability invents nothing.** Every number the dashboard shows is derived
  from ingested data; an empty stack reports zeros rather than plausible-looking
  figures.

---

## Status and next

`BUILD_STATUS.md` lists what is implemented, what is stubbed and what comes next.
In short: the loop is real and verified (**1108 Python tests** across seven components +
25 Node tests, 98 smoke checks); the tool layer now carries **73 wrappers** across 12
categories, and the L6 memory layer has vector recall and a knowledge graph. The ISO is
the remaining work.

The design documents live in `docs/`:

| Document | Read it for |
|---|---|
| `docs/T2_TOOL_CALL_DATAFLOW.md` | one gated tool call traced end to end, and which layer enforces which safety property |
| `docs/CREWAI_DEFINITIONS.md` | every role, both crews, and how the human gate is wired |
| `docs/APPENDIX_CARD_SCHEMA.md` | the field-level card schema and the event vocabulary |
| `docs/MEMORY_API.md` | the L6 retrieval paths — vector, graph, hybrid — and their honest limits |
| `docs/VERIFICATION.md` | the Phase 3 acceptance run, captured verbatim |
| `docs/PHASE1_DELIVERY_PLAN.md` | workstreams, owners, exit criteria, definition of done |
