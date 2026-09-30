# CrewAI definitions: recon and vuln-assessment crews

Blueprint ref: section 06 — *"how crews map to security workflows (recon crew,
vuln-assessment crew, reporting crew), agent roles, tool bindings to Kali binaries,
human-in-the-loop approval gates, memory/knowledge stores."*

These are the **complete** definitions for the two crews that ship with a full CrewAI
spec (YAML) in Phase 2. Every crew additionally has a Python definition that the bridge
actually executes.

| Form | Path | Used by |
|---|---|---|
| Python (authoritative) | `agent-runtime/agent_runtime/roles.py`, `crews.py` | the deterministic local runner **and** CI |
| CrewAI YAML | `docs/crews/recon*.yaml`, `docs/crews/vuln_assessment*.yaml` | a real LLM run via `crewai` |

**The Python registry is authoritative**, because the Phase 1/2 loop must be provable with
no model and no API key. The YAML mirrors it exactly, and
`agent-runtime/tests/test_crew_yaml.py` parses the YAML and asserts the two agree on
roles, tool bindings, tier ceilings, board kinds and step order. Two sources of truth for
one contract is a drift hazard; that test is the guard.

---

## Guardrail tiers

Tiers come from the **tool registry** (`tool_frontends/registry.py`), never from a crew
file or from a model's opinion. A crew cannot raise its own tier.

| Tier | Meaning | Requirements |
|---|---|---|
| **T0** | passive / public data | none |
| **T1** | low-impact active (DNS, TCP top-ports, HTTP probe) | target must be **in scope** |
| **T2** | intrusive (web-server scanner, service enumeration) | in scope **+ approved human gate** |
| **T3** | high-impact (exploitation, credential attacks) | in scope + gate + sandbox |

Every tool call passes `guardrails.evaluate` regardless of what the LLM asked for, and
scope is enforced **twice**: once by the bridge before claiming, once by the tool layer at
execution.

---

## Agent roles (the whole registry)

Six roles ship today. `max_tier` is a *ceiling*: the runtime refuses to bind a tool above
it, so a role cannot escalate itself by editing a card.

| Role | Display name | Tools (tiers) | Ceiling | Boards | Crew |
|---|---|---|---|---|---|
| `orchestrator` | Orchestrator | `log_digest` (T0) | T0 | agent, system, engagement, personal | reporting |
| `recon-specialist` | Recon Specialist | `whois_lookup` (T0), `dns_lookup` (T0), `nmap_scan` (T1), `httpx_probe` (T1) | T1 | engagement, agent | recon |
| `web-specialist` | Web Application Specialist | `httpx_probe` (T1), `nikto_scan` (T2) | T2 | engagement | vuln-assessment |
| `vuln-analyst` | Vulnerability Analyst | `httpx_probe` (T1), `nikto_scan` (T2) | T2 | engagement | vuln-assessment |
| `report-writer` | Report Writer | *(none)* | T0 | engagement, agent | reporting |
| `system-agent` | System Agent | `log_rotate` (T0) | T0 | system | system |

`report-writer` and `orchestrator` deliberately bind no intrusive tooling at all: the
roles that write up work are not the roles that can run it.

---

## Crew 1 — `recon`

**Purpose:** map the authorised attack surface — passive first, then low-impact active
probing. **Boards:** `engagement`, `agent`. **Ceiling:** T1.
**File:** `docs/crews/recon.yaml`, `recon_agents.yaml`, `recon_tasks.yaml`.

```yaml
recon:
  name: recon
  display_name: Recon Crew
  goal: >
    Map the authorised attack surface: passive first, then low-impact active probing.
  process: sequential
  boards: [engagement, agent]
  max_tier: 1
  agents: [recon-specialist]        # ordered = execution order
  tasks:  [surface_enumeration]
```

The single `surface_enumeration` task is where the passive-first discipline actually lives:

```yaml
surface_enumeration:
  description: >
    Enumerate ownership, DNS records and reachable services on {target}. Begin
    passive: WHOIS registration, then DNS records (A, AAAA, MX, TXT, NS, CNAME).
    Only then probe for reachable services. Never scan beyond the ports the card
    authorized, and never attempt service exploitation.
  expected_output: >
    A structured surface map: registrar and registration dates, DNS records by
    type, reachable services with port/state/service, plus an explicit list of
    what was NOT tested.
  agent: recon-specialist
  tools: [whois_lookup, dns_lookup, nmap_scan, httpx_probe]
  guardrail_tier: 1
  requires_approval: false
```

**Gate wiring:** none needed. T1 with an in-scope target is permitted unattended. The
moment a card binds a T2 tool, the gate section below applies.

**Why `expected_output` demands the gaps.** An agent that reports only what it found reads
as a clean bill of health. Requiring "what was NOT tested" is what keeps a recon summary
honest.

---

## Crew 2 — `vuln-assessment`

**Purpose:** identify and triage vulnerabilities on assets already confirmed in scope.
**Board:** `engagement`. **Ceiling:** T2 — **every T2 step is gated.**
**File:** `docs/crews/vuln_assessment.yaml`, `vuln_assessment_agents.yaml`,
`vuln_assessment_tasks.yaml`.

```yaml
vuln-assessment:
  name: vuln-assessment
  display_name: Vulnerability Assessment Crew
  goal: >
    Identify and triage vulnerabilities on assets already confirmed in scope.
  process: sequential
  boards: [engagement]
  max_tier: 2
  agents: [web-specialist, vuln-analyst]
  tasks:  [http_fingerprint, web_server_scan, triage_findings]
```

The execution order is the safety design: **fingerprint, then gate, then scan, then
triage.** The intrusive step never comes first, so the crew has to establish that the
service is even reachable before it asks a human for permission to hit it harder.

```yaml
http_fingerprint:
  agent: web-specialist
  tools: [httpx_probe]
  guardrail_tier: 1
  requires_approval: false

web_server_scan:
  description: >
    Run an intrusive web-server configuration scan against {target}. Prepare the
    exact arguments, then WAIT for a human gate. Do not proceed on timeout, and
    do not attempt to work around the gate.
  agent: web-specialist
  tools: [nikto_scan]
  guardrail_tier: 2
  requires_approval: true
  on_reject: skip
  on_timeout: block

triage_findings:
  agent: vuln-analyst
  tools: []                 # consumes scan results; runs no new tooling
  guardrail_tier: 0
  requires_approval: false
```

**Gate wiring — the part that matters.**

```yaml
human_in_the_loop:
  mechanism: card_approval
  trigger: "any task with guardrail_tier >= 2"
  request:
    opens: POST /api/cards/{card_id}/approvals
    card_state: "stays in Assigned - the work is parked, not queued"
    surfaces_in: "hermes shell notification feed + board card"
  decision:
    approved: "event card.approval.decided(status=approved) -> socket re-claims the card"
    rejected: "recorded on the card; the step is skipped; no re-claim"
    timeout:  "GATE_TIMEOUT (default 120s) -> outcome=timeout -> step blocked, not run"
  invariants:
    - an approval is read from the card, so a crew cannot assert it was cleared
    - the tool layer re-checks approval independently of the bridge
    - rejection does not re-open the gate (that would loop)
```

The gate is **the card's own state**, not a side channel: the same record carries the
request, the decision, who made it and when. Because the bridge opens the gate and the
card stays in `Assigned`, an approval **re-claims** the card over the socket — otherwise
the parked work would sit idle until a fallback poll, or forever on a socket-only
deployment. A *rejection* deliberately does not re-claim: re-claiming would re-open the
gate the human just closed, in a loop.

**Silence is never approval.** The timeout resolves to `outcome=timeout` and the step is
blocked, not run.

---

## Other crews (Python only, no YAML yet)

| Crew | Roles | Tools | Ceiling | Boards |
|---|---|---|---|---|
| `reporting` | report-writer | *(none)* | T0 | engagement, agent |
| `system` | system-agent | `log_rotate` (T0) | T0 | system |

`test_crew_yaml.py` asserts these stay internally consistent (each step's role belongs to
that crew, and every named tool is within the role's bindings) even though they have no
YAML spec yet — and it asserts that the set of YAML-specced crews is exactly
`{recon, vuln-assessment}`, so "no spec" can never be mistaken for "spec'd and fine".

---

## Python ↔ YAML equivalence (asserted in CI)

`agent-runtime/tests/test_crew_yaml.py` parses the YAML and checks:

| Property | Assertion |
|---|---|
| manifest key | the crew is declared under its own name |
| goal / display_name | match `CrewDef` exactly |
| boards | match `CrewDef.boards`, in order |
| `max_tier` | matches `CrewDef.max_tier` |
| **agent order** | matches `CrewDef.roles` — order is execution order |
| agent set | matches the crew's roles |
| **tool bindings** | each YAML `tools:` list matches the role registry exactly |
| tier ceiling | `guardrail.max_tier` matches `role.max_tier` |
| boards per agent | match `role.boards` |
| crew back-reference | matches `role.crew` |
| task set | covers the manifest's task list |
| task → role | every task runs as a role in that crew |
| **no escalation** | no task's `guardrail_tier` exceeds its role's ceiling |
| tool containment | a task never uses a tool its role cannot bind |
| crew containment | a task never uses a tool outside the crew's binding |
| **gating** | every T2+ task has `requires_approval: true` |
| gate honesty | no sub-T2 task asks for approval (a stale flag trains reflexive approving) |

---

## Running a real crew

```bash
export MODEL_ENABLED=1                          # opt in to a local model
export MODEL_BASE_URL=http://127.0.0.1:11434    # Ollama-compatible
export MODEL_NAME=llama3.1
export GATE_TIMEOUT=300
python3 -m uvicorn agent_runtime.server:app --port 8082
```

With `MODEL_ENABLED=0` (the default) the same crews run through the deterministic local
runner: identical roles, tools, traces and guardrails, no model required. That is what
keeps the whole pipeline provable in CI, and it is why the Python registry stays
authoritative.

## Memory and knowledge stores (Phase 3)

The crews above are stateless per card today. The stores that make them accumulate
knowledge are **not implemented** — recorded here so the gap is explicit rather than
implied:

| Store | Purpose | Status |
|---|---|---|
| card traces + artifacts | per-card working memory | ✅ built |
| event log (hash-chained) | durable history, replay | ✅ built |
| engagement scope registry | per-engagement authorization facts | 🔄 scope is per-card today |
| host/service memory | what we already know about a target | ⬜ Phase 3 |
| finding library | past findings, dedup and precedent | ⬜ Phase 3 |
| tool-outcome memory | which arguments produced signal | ⬜ Phase 3 |

`crewai`'s memory backends can be attached when the model path is enabled, but nothing in
this repository pretends they are wired yet.
