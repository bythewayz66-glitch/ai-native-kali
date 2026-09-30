# Data model & API reference

Blueprint section 03.7. Schemas live in `kanban-core/kanban_core/models.py`;
this file is the readable reference.

---

## Card

The unit of work. Everything on the system that can be *done* is one of these.

| Field | Type | Meaning |
|---|---|---|
| `card_id` | str | `crd_...` |
| `title` | str | What the work is |
| `description` | str | Free-form context handed to the crew |
| `board_id` | str | Board this card belongs to |
| `column` | enum | `Backlog` / `Assigned` / `Running` / `Review` / `Blocked` / `Done` |
| `assignee` | str? | Agent role or human name |
| `assignee_kind` | enum | `agent` / `human` / `unassigned` |
| `crew` | str? | Which crew should run it |
| `priority` | enum | `low` / `medium` / `high` / `critical` |
| `scope` | `Scope?` | Authorization envelope (see below) |
| `tools` | `ToolBinding[]` | Named tool calls the crew is allowed to make |
| `artifacts` | `Artifact[]` | Produced files/reports, content-addressed |
| `traces` | `Trace[]` | One per tool invocation, carrying the audit hash |
| `approvals` | `Approval[]` | Human gates, in order |
| `result` | str? | What the crew wrote back |
| `blocked_reason` | str? | Why it is in `Blocked` |
| `parent_id` | str? | For cards spawned as sub-cards |
| `pending_approval` | `Approval?` | Derived: the open gate, if any |
| `is_gated` | bool | Derived: requires an approval before running |
| `max_tier` | int | Derived: highest guardrail tier among `tools` |
| `killed` | bool | Kill switch |

## Scope

| Field | Type | Meaning |
|---|---|---|
| `targets` | str[] | Exact hosts/domains, `*.example.com` wildcards allowed |
| `cidrs` | str[] | CIDR ranges |
| `ports` | str[] | Optional port restriction |
| `authorization_ref` | str? | Ticket/contract reference. **Required for T2+** |
| `expires_at` | ISO ts? | An expired scope authorizes nothing |

`Scope.covers(target)` accepts a bare host, a URL (the host is extracted), or an
IP, and matches wildcards in the standard one-label way
(`*.example.com` covers `api.example.com` but not `example.com`).

## Trace

| Field | Type | Meaning |
|---|---|---|
| `tool` | str | Registered tool name |
| `tier` | int | 0–3 |
| `status` | enum | `ok` / `denied` / `blocked` / `error` |
| `duration_ms` | int | Measured, not estimated |
| `stdout_tail` / `stderr_tail` | str | Last 500 chars |
| `dry_run` | bool | False only for a real, unlocked execution |
| `audit_hash` | str? | Joins to the tool layer's independent hash chain |
| `agent` | str? | Which role made the call |

## Event

Every mutation publishes one. `event_type` values:

`board.created`, `card.created`, `card.moved`, `card.assigned`,
`card.trace.added`, `card.artifact.added`, `card.approval.requested`,
`card.approval.decided`, `card.killed`, `card.blocked`, `card.updated`.

| Field | Meaning |
|---|---|
| `event_id` | `evt_...` |
| `seq` | Monotonic; the resume cursor |
| `type` | From the catalogue above |
| `board_id` / `card_id` | Correlation |
| `from_column` / `to_column` | Populated on `card.moved` |
| `actor` | Who caused it (agent name, `bridge`, `operator`) |
| `payload` | Type-specific detail (e.g. a block's `reason`) |
| `audit_hash` | Chain link |

## ToolSpec

The `tool-frontends` schema. See `tool_frontends/spec.py`.

| Field | Meaning |
|---|---|
| `name` / `binary` | Registry key and the real executable |
| `category` | recon, web-application, exploitation, system, … |
| `tier` | 0 passive · 1 low-impact active · 2 intrusive · 3 high-impact |
| `params` | `ParamSpec[]`: name, type, required, default, choices, limits |
| `dry_run_template` | Always present; renders the command without running it |
| `live_template` | The real command; required for T2+ |
| `requires_scope` | Must be true for T2+ (enforced at registration) |
| `requires_approval` | Must be true for T2+ |
| `requires_sandbox` | Must be true for T3 |
| `explain` / `next_steps` | Result explanation and suggested follow-ups for the operator |

## Lifecycle transitions

```
Backlog ──► Assigned ──► Running ──► Review ──► Done
                 │            │          │
                 ▼            ▼          ▼
              Blocked ◄──────┴──────────┘
```

Guards enforced by the state machine (not by convention):

- `Running` may only be entered from `Assigned` — work cannot skip the queue.
- `Done` may only be written by a **human** actor. An agent attempt is refused
  with reasons, which is the property the whole human-in-the-loop design rests on.
- `Blocked` is reachable from any active column, and carries a reason.
- A card in `Done` is terminal.

## REST surface (kanban-core)

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Also reports the event-chain verification |
| GET | `/api/schema` | The full data model, machine-readable |
| GET/POST | `/api/boards` | List / create |
| GET | `/api/boards/{id}` | Includes `default_crew` |
| GET | `/api/boards/{id}/metrics` | Column counts, WIP, blocked |
| POST/GET | `/api/cards` | Create / list (filter by `board_id`, `column`) |
| GET | `/api/cards/{id}` | Includes derived gate + tier fields |
| POST | `/api/cards/{id}/move` | The state machine applies |
| POST | `/api/cards/{id}/can-move` | Dry-run a transition |
| POST | `/api/cards/{id}/assign` | Assignee + crew |
| POST | `/api/cards/{id}/kill` | Kill switch |
| POST | `/api/cards/{id}/block` | With reason |
| POST/GET | `/api/cards/{id}/traces` | Append / list tool traces |
| POST | `/api/cards/{id}/artifacts` | Content-addressed artifact |
| POST | `/api/cards/{id}/result` | Crew result text |
| POST | `/api/cards/{id}/approvals` | Open a gate |
| POST | `/api/cards/{id}/approvals/{aid}/decide` | Approve / reject |
| GET | `/api/approvals/pending` | What the shell's feed renders |
| GET | `/api/cards/{id}/replay` | Merged ordered history |
| POST | `/api/scope/check` | Is a target inside a scope |
| GET | `/api/events` | `since_seq`, `limit`, `card_id`, `board_id`, `type_prefix` |
| GET | `/api/timeline` | Newest-first, hash-carrying |
| GET | `/api/overview` | Totals for the taskbar widget |
| GET | `/api/audit` · `/api/audit/verify` | Event-chain rows / verification |
| WS | `/ws/events` · `/ws/board/{id}` | Snapshot then live events |

## Tool layer surface

| Method | Path | Notes |
|---|---|---|
| GET | `/tools` | Filter by `category`, `tier` |
| GET | `/mcp/tools/list` | MCP `tools/list` shape |
| POST | `/tools/check` | Explain a decision **without** executing or auditing |
| POST | `/tools/call` | Guardrailed invocation (dry-run by default) |
| POST | `/mcp/tools/call` | MCP `tools/call` shape (content blocks) |
| POST | `/intent` | Natural language → candidate tools |
| GET | `/audit` · `/audit/verify` | Tool audit chain |

## Hash chain construction

Both chains use the same rule, so one mental model covers both:

```
record.prev_hash = previous record's hash   (all-zero for the first)
record.hash      = sha256(prev_hash + canonical(record))
```

`canonical` is `json.dumps(..., sort_keys=True, separators=(',', ':'))` — key
order cannot change the digest. Hash fields themselves are excluded before
hashing. Verification walks from the genesis record and reports the exact
sequence number where the chain breaks.
