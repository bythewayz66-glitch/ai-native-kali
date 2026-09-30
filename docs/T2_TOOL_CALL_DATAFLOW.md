# Data flow: one tier-2 tool call, intent to audit record

Blueprint ref: section 03.9. This traces a **single T2 (intrusive) tool call** through
every component, end to end. Every step names the module and endpoint that really
implements it in this repository — this is a description of running code, not a design
sketch.

**Scenario.** The operator types: `scan example.com for web-server issues (authorized)`.

```
 key:  [S] shell      [K] kanban-core     [A] agent-runtime
       [T] tool-frontends                  [O] observability
```

---

## Stage 0 — Preconditions

Before any of this can run, three things must already be true. If any is missing the
call is refused at stage 6 or 7 and never reaches the sandbox.

| Precondition | Where it lives | Failure looks like |
|---|---|---|
| A signed authorization scope exists and covers the target | `kanban_core.models.Scope` (`authorization_ref`, `cidrs`, `targets`, `expires_at`) | card lands in **Blocked**, reason `scope check failed: ...` |
| The tool is registered with a tier and a typed parameter schema | `tool_frontends.spec.ToolSpec` + `registry` | `/tools/check` returns `denied: unknown tool` |
| Live execution is deliberately unlocked (`TOOLS_LIVE=1` **and** the tool named in `TOOLS_UNLOCK`) | `tool_frontends.policy.LivePolicy` | `decision: dry_run`, `live_blocked: not_unlocked` |

---

## Stage 1 — Intent  ·  `[S]`

The operator's sentence goes to the intent router (`POST /intent` on the tool layer).

```
"scan example.com for web-server issues (authorized)"
   -> { intent: "web_assessment", targets: ["example.com"],
        suggested_tools: [{name: "httpx_probe", tier: 1},
                          {name: "nikto_scan",  tier: 2}],
        requires_approval: true }
```

- Extraction is **lexical** today (`tool_frontends`: target regex + keyword→category map).
  A model backend is Phase 3 and reuses the same response shape.
- `requires_approval` is set because the highest suggested tier is ≥ T2.
- The router returns a *proposal*. It has no authority to execute anything.

## Stage 2 — Card creation  ·  `[S] → [K]`

The shell does not run a tool; it writes a card.

```
POST /api/cards
{
  "title":  "Web assessment: example.com",
  "board_id": "brd_engagement",
  "assignee": "web-specialist",
  "crew":     "vuln-assessment",
  "priority": "critical",
  "scope":  { "targets": ["example.com"], "authorization_ref": "ACME-SOW-2026-0912" },
  "tools":  [ {"name":"httpx_probe","tier":1,"args":{"target":"example.com"}},
              {"name":"nikto_scan", "tier":2,"args":{"target":"example.com"}} ]
}
```

- The **card is the task spec**: intent, context (scope), tool bindings and args, the
  assigned crew — all in one record. There is no second source of truth.
- Tiers are stamped from the registry, so a card cannot claim a tool is T0 when the
  registry says T2.
- `kanban_core.state_machine` permits `Backlog -> Assigned`; the assignment publishes
  `card.assigned`.

## Stage 3 — Event, then claim  ·  `[K] → [A]`

`kanban_core.service._emit` appends to the hash-chained event log and publishes on the
bus; `api.ws_events` streams it to subscribers.

```
[A] EventStreamWatcher  <- ws://.../ws/events   {"kind":"event","event":{...}}
      is_claimable(event)            -> card.assigned / moved-into-Assigned
      Bridge.process_event(event)    -> idempotency + single-flight guards
      Bridge.process_card(card_id, source="socket")
```

- The socket is the **primary** claim path; polling runs only while the stream is
  unhealthy (`Bridge.run_forever`). Claim source is counted separately
  (`stats.socket_claims` / `stats.poll_claims`) so a dead socket cannot masquerade as a
  working one.
- The same `event_id` never produces two claims, and a card already in flight is skipped.

## Stage 4 — Crew + role resolution  ·  `[A]`

```
crew = card.crew or board.default_crew        -> CrewDef("vuln-assessment")
role = card.assignee                          -> ROLE_REGISTRY["web-specialist"]  (ceiling T2)
```

`roles.py` gives each role a **tool ceiling** independent of the card. A role may only
touch a tool whose tier is ≤ its ceiling — this is what stops a recon role from being
talked into running `sqlmap_test`.

## Stage 5 — Approval gate  ·  `[A] ↔ [S]`

The highest tier on the card is T2, so the run **parks before any tool is invoked**.

```
[A] HumanFeedbackGate.open(card_id, tool="nikto_scan", tier=2)
      POST /api/cards/{id}/approvals   -> approvals:[{id:"apr_…", status:"pending"}]
    card stays in Assigned; no trace, no execution

[S] notification feed  ->  [ Approve ]  [ Reject ]
      POST /api/cards/{id}/approvals/{apr}/decide {approved: true, decided_by:"operator"}
      -> event card.approval.decided (payload.status="approved")
```

- The gate is the **card's own state**, not a side channel — the same record shows the
  request, the decision, who made it and when.
- Approval **re-claims** the card over the socket (`card.approval.decided` + `status ==
  approved`). Rejection deliberately does **not** re-claim: re-opening a gate the human
  just closed would be an infinite loop.
- Timeout is finite (`GATE_TIMEOUT`, default 120 s) and resolves to
  `no human decision within Ns`. **Silence is never approval.**

## Stage 6 — Scope validation (first of two)  ·  `[A]`

Before moving the card, the bridge checks every bound tool's target against the card's
scope.

```
for binding in card.tools:
    scope.covers(binding.args["target"])   ->  True | False
    scope.is_expired()                     ->  False
```

Out-of-scope ⇒ the bridge writes a block reason and moves the card to **Blocked**. The
card **never enters Running** (asserted by the smoke test).

## Stage 7 — Claim  ·  `[A] → [K]`

```
POST /api/cards/{id}/move {to_column:"Running", actor_is_agent:false}
   -> state_machine: Assigned -> Running  (requires an assignee; guards pass)
   -> event card.moved (Assigned -> Running)
```

The agent is acting under the human's approval recorded in stage 5, which is why the
move is legitimate rather than an agent escalating its own privileges.

## Stage 8 — Guardrails, then the sandbox  ·  `[A] → [T]`

```
[T] guardrails.evaluate(spec, args, scope, approved, live_allowed)
       tier_ok | target_in_scope | scope_not_expired
       gate_approved (T2+) | sandboxed (T3) | live_unlocked
    -> Decision(allowed, decision, reasons)
```

If `allowed`:

```
[T] runner.run_tool(...)   python frontends, never a shell string
       shlex.split + allowlist render()   -> argv list, whitespace stripped
       subprocess.run(shell=False)        -> no shell, no PATH inheritance
       RLIMIT_CPU / RLIMIT_AS / RLIMIT_NPROC, wall-clock timeout, output caps
```

- **Dry-run is the default and executes nothing** — it returns the exact argv that
  *would* have run. That is what makes the whole loop testable in CI.
- A live request that is not unlocked returns `denied` with an explicit reason. It is
  never silently downgraded to a dry run.
- Scope is enforced here a **second** time, independently of the bridge — a compromised
  bridge cannot bypass the tool layer's own check.

## Stage 9 — Trace capture  ·  `[A] → [K]`

```json
POST /api/cards/{id}/traces
{ "tool":"nikto_scan", "tier":2, "args":{"target":"example.com"},
  "status":"ok", "duration_ms":38, "dry_run":true,
  "audit_hash":"9f2c…", "agent":"web-specialist" }
```

Trace fields are the ones observability consumes directly: tool, tier, agent, card,
status, latency, dry-run flag, audit hash. Because the hash is copied onto the trace,
**card history and audit chain are joinable**, which is what makes replay trustworthy.

## Stage 10 — Hash-chained audit write  ·  `[T]`

```
prev = last.audit_hash (or 64 zeros)
h    = sha256(prev + "\u2016" + canonical_json(record))
append {seq, ts, tool, card_id, target, status, prev_hash, hash}
```

- Appended for **every** decision, including refusals and dry runs — a refused call is
  evidence too.
- `verify_chain()` walks the log and reports the exact `seq` where a break appears, so
  tampering is *detectable*, not merely discouraged.

## Stage 11 — Result and release  ·  `[A] → [K]`

```
POST /api/cards/{id}/artifacts   (content-addressed, sha256)
POST /api/cards/{id}/result      (human-readable summary)
POST /api/cards/{id}/move        Running -> Review
```

The crew releases to **Review**, never to `Done`. `state_machine` enforces that an agent
cannot write `Done` — accepting the work is a human act.

## Stage 12 — Ingest, panels, replay  ·  `[O]`

```
[O] collector.pull()   -> events + traces + audit rows (per-source cursors)
    /panels/timeline | /panels/traces | /panels/tokens | /panels/audit
    /panels/alerts   -> card_blocked, chain_broken, gate_waiting, …
    /cards/{id}/replay -> one ordered history: state changes + traces + audit rows
```

Replay is the payoff of stages 3, 9 and 10 sharing identifiers: a reviewer can reconstruct
exactly what the agent did, under whose approval, with what result, and verify the hashes
did not move afterwards.

---

## Timing, in the common case

| Stage | Typical | Notes |
|---|---|---|
| 1–2 intent → card | ~5 ms | local |
| 3 socket claim | ~10–40 ms | event-driven; the old poll added up to 2 s |
| 4–6 resolve/gate/scope | ~15 ms | plus **human time** at T2+ — unbounded by design |
| 7–8 claim + execute | ~30 ms dry-run | live execution is bounded by the tool, not the platform |
| 9–11 traces, audit, release | ~20 ms | two API writes + one hash |
| 12 ingest → panels | ≤ 2 s | collector interval |

**The dominant cost is the human gate, and that is the point.** Everything else is
milliseconds.

## What each safety property is enforced by

| Property | Enforced in | Bypassable by a compromised… |
|---|---|---|
| target in scope | bridge **and** tool layer | bridge: no (tool layer re-checks) |
| T2+ needs a human | gate (card state) + guardrail re-check | agent: no (approval read from the card) |
| agent cannot finish work | `state_machine` | agent: no |
| nothing live by default | `LivePolicy` | agent: no (operator env only) |
| what happened is knowable | hash-chained audit + joinable traces | — (detectable, not preventable) |
