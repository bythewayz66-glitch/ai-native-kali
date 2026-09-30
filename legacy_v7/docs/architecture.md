# Architecture — how the pieces fit

Companion to the full architecture blueprint (`docs/architecture.md` is the
engineer's view; the blueprint is the design view). This file answers one
question: **which file implements which architectural promise.**

---

## Component responsibilities

| Component | Owns | Does *not* own |
|---|---|---|
| `kanban-core` | Card/board state, the lifecycle state machine, the event bus, the hash-chained **event** log | Deciding what work to do, or running anything |
| `agent-runtime` | Crew/role definitions, claiming cards, running crews, writing traces + results back | Persisting state (it uses the public API), or tool execution guardrails |
| `tool-frontends` | Tool schemas, tier/scope/approval enforcement, sandboxed execution, the hash-chained **tool** audit log | Deciding whether work is authorized in the first place |
| `observability` | Reading the two event streams, projecting them into panels, replaying a card | Producing events (it is purely a consumer) |
| `hermes-shell` | Rendering board state and acting on it (approve/reject/quick-add) | Any state of its own — it is a thin client |

## The request flow, end to end

```
Hermes shell / smoke test / any client
        |  POST /api/cards, POST /api/cards/{id}/move
        v
  kanban-core  --(card.moved event)-->  event log  (hash-chained)
        ^                                     |
        |  GET /api/cards?board_id=...         |  GET /api/events
        |  (bridge polls the Assigned queue)   v
  agent-runtime (bridge loop)          observability (collector loop)
        |                                     ^
        |  POST /tools/call                   |  GET /audit
        v                                     |
  tool-frontends  --(audit append, hash-chained)--+
        |
        |  dry-run (default) or sandboxed live run
        v
   the Kali binary
```

Every arrow is an HTTP call over a public API. There is no privileged back
door, which is why the smoke test can assert the same guarantees an external
client would get.

## Design invariants (and where they are enforced)

| Invariant | Enforced in |
|---|---|
| An agent may never move a card to `Done` | `kanban_core/state_machine.py` |
| A T2+ tool cannot run without a scope **and** an open human gate | `tool_frontends/guardrails.py` |
| An out-of-scope card never enters `Running` | `agent_runtime/bridge.py` (`_scope_reasons`, pre-claim) |
| A refused live run is an explicit denial, never a silent dry run | `tool_frontends/runner.py` / `guardrails.py` |
| A tool argument can never expand into extra argv tokens | `tool_frontends/spec.py` (`sanitize`), `runner.py` (`shell=False`) |
| No tool executes unless an operator unlocks it by name | `tool_frontends/server.py` (`_live_allowed`) |
| Tool calls are attributable to the card that caused them | `agent_runtime/bridge.py` → `server.py` → `/tools/call` `card_id` |
| Every state change and every tool call is tamper-evident | `kanban_core/store.py`, `tool_frontends/audit.py` |

The invariants are listed separately from the code because most of them are
asserted in tests rather than merely intended — that is the point of the list.

## Where the blueprint's sections live

| Blueprint section | Implementation |
|---|---|
| 03.1 boards as the universal abstraction | `kanban_core/seed.py` (agent / engagement / system / personal boards) |
| 03.2 Kanban ⇄ CrewAI bridge, column transitions as events | `agent_runtime/bridge.py`, `kanban_core/bus.py` |
| 03.3 Kanban ⇄ shell integration | `hermes_shell/static/index.html` + `hermes_shell/server.py` |
| 03.4 the board *is* the observability surface | `observability/collector.py`, `metrics.py`, `/dashboard` |
| 03.5 Kanban as the human control plane | `kanban_core/models.py` (`Approval`), `bridge.py` (`_open_gate`) |
| 03.6 tool frontends emit cards | `tool_frontends/server.py` (`/intent`), wrapper `next_steps` |
| 03.7 data model & API | `kanban_core/models.py`, `api.py` — see `docs/data-model.md` |
| 04.x observability panels | `observability/server.py` (one endpoint per panel) |
| 05 tool frontends, T0–T3 | `tool_frontends/wrappers/__init__.py` |
| 07 packaging | `packaging/` |
| 08 guardrails | `tool_frontends/guardrails.py`, `runner.py`, `audit.py` |

## Why the local crew runner exists

`crewai_adapter.py` has two backends. With `crewai` installed it builds real
agents, tasks and tools. Without it, a deterministic local runner executes the
*same pipeline* — real tool calls, real guardrails, real audit rows — minus the
LLM.

This is not a mock. It is what makes the entire system testable in CI with no
API keys, and it is why the tests assert behaviour (denials, hashes, column
transitions) rather than prompt text. The LLM is a *variable* in the loop, not a
prerequisite for the loop to be correct.
