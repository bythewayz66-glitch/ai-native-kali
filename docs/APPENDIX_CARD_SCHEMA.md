# Appendix A — Card JSON schema and event vocabulary

**Printable reference.** Generated from the live models (`kanban_core.models`) so the
types here are the ones the running service actually validates. Regenerate the field list
with the snippet in *§5. Keeping this honest*.

Version: `kanban-core 0.1.0`.

---

## 1. Enumerations

| Enum | Values | Notes |
|---|---|---|
| `Column` | `Backlog`, `Assigned`, `Running`, `Review`, `Done`, `Blocked` | the lifecycle; order is significant |
| `Priority` | `low`, `medium`, `high`, `critical` | `critical` does not bypass a gate |
| `assignee_kind` | `agent`, `human` | a human card is never auto-claimed |
| `Board.kind` | `agent`, `engagement`, `system`, `personal` | board class from blueprint 03.1 |
| `Approval.status` | `pending`, `approved`, `rejected` | only an `approved` decision re-claims |
| `Trace.status` | `ok`, `error`, `denied`, `blocked` | `denied` = guardrail refused; `blocked` = scope/state refused |
| `Artifact.kind` | `report`, `scan-output`, `pcap`, `log`, `screenshot`, `json`, `other` | |
| Guardrail tier | `0`, `1`, `2`, `3` | T0 passive → T3 high-impact |
| `assignee_kind` | `agent`, `human` | (listed twice above by design — it gates claiming) |

### Legal column transitions

```
Backlog  -> Assigned | Blocked
Assigned -> Running | Backlog | Blocked
Running  -> Review | Blocked
Review   -> Done | Running | Blocked
Blocked  -> Backlog | Assigned | Done
Done     -> (terminal)
```

Guards, checked in this order by `state_machine.can_move`:

| Guard | Rule | Machine code |
|---|---|---|
| illegal edge | the pair above is not declared | `illegal_edge` |
| agent cannot finish | an agent actor may never write `Done` | `agent_cannot_finish` |
| assignee required | entering `Running` needs an `assignee` | `no_assignee` |
| killed card | a killed card may only go to `Blocked` | `card_killed` |
| approval pending | `Review`/`Done` blocked while a gate is `pending` | `approval_pending` |

> Refusals are machine-readable. `POST /api/cards/{id}/can-move` returns
> `{"ok": false, "reasons": ["illegal_edge: Review -> Backlog is not a declared edge"]}`
> so the UI can explain *why*, not just refuse.

---

## 2. `Card` — the task record

The card is simultaneously task spec, context, audit surface and approval gate.

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `card_id` | string | auto | `card_…` | stable id; used by traces, audit, replay |
| `title` | string | **yes** | — | short human label |
| `board_id` | string | **yes** | — | owning board |
| `column` | `Column` | no | `Backlog` | current lifecycle position |
| `description` | string | no | `""` | full instructions for the crew |
| `assignee` | string \| null | no | `null` | role name or human id |
| `assignee_kind` | `agent`\|`human` | no | `agent` | `human` cards are not auto-claimed |
| `crew` | string \| null | no | `null` | overrides the board's `default_crew` |
| `priority` | `Priority` | no | `medium` | |
| `labels` | string[] | no | `[]` | free-form routing/filter tags |
| `scope` | `Scope` \| null | no | `null` | authorization envelope; **required for any T1+ tool** |
| `tools` | `ToolBinding[]` | no | `[]` | the permitted tool set — nothing else may run |
| `requires_approval` | boolean | no | `false` | force a gate even below T2 |
| `artifacts` | `Artifact[]` | no | `[]` | outputs |
| `traces` | `Trace[]` | no | `[]` | tool-call evidence |
| `approvals` | `Approval[]` | no | `[]` | human gates, newest last |
| `result` | string | no | `""` | crew's written conclusion |
| `parent_id` | string \| null | no | `null` | sub-card → parent (see §4) |
| `blocked_reason` | string \| null | no | `null` | why it is in `Blocked` |
| `killed` | boolean | no | `false` | operator kill switch engaged |
| `history` | `CardEventRef[]` | no | `[]` | inline state-change refs |
| `entered_column_at` | string (ISO) | auto | — | SLA/latency basis |
| `created_at` | string (ISO) | auto | — | |
| `updated_at` | string (ISO) | auto | — | |
| `meta` | object | no | `{}` | extension point |

### `Scope`

| Field | Type | Required | Default |
|---|---|---|---|
| `targets` | string[] | no | `[]` |
| `cidrs` | string[] | no | `[]` |
| `authorization_ref` | string \| null | no | `null` |
| `authorized_by` | string \| null | no | `null` |
| `expires_at` | string (ISO) \| null | no | `null` |

A target is permitted only if it matches `targets` exactly **or** falls inside one of
`cidrs`, and `expires_at` has not passed.

### `ToolBinding`

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | string | **yes** | — | must exist in the tool registry |
| `tier` | integer | no | `0` | cross-checked against the registry |
| `dry_run` | boolean | no | `true` | per-binding dry-run preference |
| `args` | object | no | `{}` | exact tool arguments to run |

### `Trace`

| Field | Type | Required | Default |
|---|---|---|---|
| `id` | string | auto | `trc_…` |
| `ts` | string (ISO) | auto | — |
| `tool` | string | **yes** | — |
| `tier` | integer | no | `0` |
| `args` | object | no | `{}` |
| `status` | `ok`\|`error`\|`denied`\|`blocked` | no | `ok` |
| `duration_ms` | integer | no | `0` |
| `exit_code` | integer \| null | no | `null` |
| `stdout_tail` | string | no | `""` |
| `stderr_tail` | string | no | `""` |
| `dry_run` | boolean | no | `true` |
| `audit_hash` | string \| null | no | `null` | joins the tool audit chain |
| `agent` | string \| null | no | `null` |

### `Artifact`

| Field | Type | Required | Default |
|---|---|---|---|
| `id` | string | auto | `art_…` |
| `ts` | string (ISO) | auto | — |
| `name` | string | **yes** | — |
| `kind` | see §1 | no | `other` |
| `path`, `url` | string \| null | no | `null` |
| `sha256` | string \| null | no | `null` | content-addressed |
| `bytes` | integer | no | `0` |
| `produced_by` | string \| null | no | `null` |
| `summary` | string | no | `""` |

### `Approval`

| Field | Type | Required | Default |
|---|---|---|---|
| `id` | string | auto | `apr_…` |
| `ts` | string (ISO) | auto | — |
| `requested_by` | string | **yes** | — |
| `reason` | string | **yes** | — |
| `tool` | string \| null | no | `null` |
| `tier` | integer | no | `0` |
| `status` | `pending`\|`approved`\|`rejected` | no | `pending` |
| `decided_by` | string \| null | no | `null` |
| `decided_at` | string (ISO) \| null | no | `null` |
| `note` | string | no | `""` |

### `Board`

| Field | Type | Required | Default |
|---|---|---|---|
| `board_id` | string | auto | `brd_…` |
| `name` | string | **yes** | — |
| `kind` | see §1 | no | `agent` |
| `description` | string | no | `""` |
| `created_at` | string (ISO) | auto | — |
| `default_crew` | string \| null | no | `null` |

---

## 3. Event vocabulary

Every state change appends one event to the **hash-chained** log and fans out on the
WebSocket bus. `seq` is monotonic; `prev_hash`/`hash` make the log tamper-evident.

### `Event` envelope

| Field | Type | Required | Default |
|---|---|---|---|
| `event_id` | string | auto | `evt_…` |
| `seq` | integer \| null | assigned | `null` |
| `ts` | string (ISO) | auto | — |
| `type` | string | **yes** | — |
| `board_id` | string \| null | no | — |
| `card_id` | string \| null | no | — |
| `from_column` | string \| null | no | `null` |
| `to_column` | string \| null | no | `null` |
| `actor` | string | no | `system` |

### Type catalogue

| `type` | Fired when | Claimable? | Payload keys |
|---|---|---|---|
| `board.created` | a board is created | no | — |
| `card.created` | a card is created | if `payload.column == "Assigned"` | `column` |
| `card.moved` | any column transition | **yes if `to_column == "Assigned"`** | `from`, `to` |
| `card.assigned` | assignee/crew set | **yes** | `assignee`, `crew` |
| `card.approval.requested` | a gate is opened | no | `approval_id`, `tool`, `tier` |
| `card.approval.decided` | a human decides | **yes, only if `status == "approved"`** | `approval_id`, `status`, `note` |
| `card.trace.added` | a tool call is recorded | no | `tool`, `tier`, `status`, `audit_hash` |
| `card.artifact.added` | an artifact is attached | no | `name`, `kind`, `sha256` |
| `card.blocked` | a card enters `Blocked` | no | `reason` |
| `card.killed` | the kill switch fires | no | `reason` |
| `card.updated` | result/description/priority changed | no | `field` |

> **Why `card.approval.decided` is claimable (approved only).** The bridge opens the gate
> and the card stays in `Assigned` throughout, so nothing else would re-claim it. A
> *rejection* is deliberately not claimable: re-claiming would re-open the gate the human
> just closed, forever.

### Payload examples

```json
{"event_id":"evt_9f1c","seq":41,"ts":"2026-09-26T19:24:02.881Z","type":"card.moved",
 "board_id":"brd_engagement","card_id":"card_7ac2","from_column":"Assigned",
 "to_column":"Running","actor":"recon-specialist","payload":{"note":"claimed by crew"}}
```

```json
{"event_id":"evt_4d20","seq":42,"ts":"2026-09-26T19:24:02.930Z","type":"card.approval.decided",
 "board_id":"brd_engagement","card_id":"card_7ac2","actor":"operator",
 "payload":{"approval_id":"apr_31b","status":"approved","note":"authorized for Q3 SOW"}}
```

```json
{"event_id":"evt_2b77","seq":43,"ts":"2026-09-26T19:24:02.960Z","type":"card.trace.added",
 "board_id":"brd_engagement","card_id":"card_7ac2","actor":"web-specialist",
 "payload":{"tool":"nikto_scan","tier":2,"status":"ok","audit_hash":"9f2c…"}}
```

### Transport frames (`/ws/events`)

| `kind` | Meaning |
|---|---|
| `snapshot` | 25 recent events on connect (replay-on-attach) |
| `event` | one new event |
| `heartbeat` | 15 s keep-alive with bus stats |

`/ws/board/{board_id}` additionally sends `kind: "board"` with a full board view.

---

## 4. REST surface (condensed)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | service + bus + audit-chain verdict |
| `GET` | `/api/schema` | columns, card fields, event types, tiers |
| `GET` | `/api/audit/verify` | verify the hash chain end to end |
| `GET` | `/api/events` | filter by `since_seq`, `card_id`, `board_id`, `type_prefix` |
| `GET` | `/api/timeline` | newest-first feed for the shell |
| `GET` | `/api/overview` | totals across boards |
| `POST`/`GET` | `/api/boards` | create / list |
| `GET` | `/api/boards/{id}` | board view (cards grouped by column) |
| `GET` | `/api/boards/{id}/metrics` | per-board rollup |
| `POST`/`GET` | `/api/cards` | create / list (filter by board, column, assignee, parent) |
| `GET` | `/api/cards/{id}` | full card |
| `GET` | `/api/cards/{id}/replay` | ordered history: events + traces + audit |
| `GET` | `/api/cards/{id}/children` | sub-cards |
| `POST` | `/api/cards/{id}/move` | transition (409 + reasons on refusal) |
| `POST` | `/api/cards/{id}/can-move` | **dry-run** a transition (`{ok, reasons}`) |
| `POST` | `/api/cards/{id}/assign` | set assignee/crew, optionally → `Assigned` |
| `POST` | `/api/cards/{id}/block` | block with a reason |
| `POST` | `/api/cards/{id}/kill` | kill switch |
| `POST` | `/api/cards/{id}/traces` | append a tool-call trace |
| `POST` | `/api/cards/{id}/artifacts` | attach an artifact |
| `POST` | `/api/cards/{id}/result` | set the result text |
| `POST` | `/api/cards/{id}/approvals` | open a gate |
| `POST` | `/api/cards/{id}/approvals/{aid}/decide` | approve / reject |
| `GET` | `/api/approvals/pending` | every open gate, for the shell |
| `POST` | `/api/scope/check` | test a target against a scope |

---

## 5. Keeping this honest

Regenerate the field lists and compare:

```bash
cd documents/ai-native-kali
PYTHONPATH=kanban-core python3 -c "
from kanban_core.models import Card, Event, EventType
print(list(Card.model_fields))
print([v for k,v in vars(EventType).items() if not k.startswith('_') and isinstance(v,str)])
"
```

If they differ from this appendix, the appendix is stale — the models are the contract.
