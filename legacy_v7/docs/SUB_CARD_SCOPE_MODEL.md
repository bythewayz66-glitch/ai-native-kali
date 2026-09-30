# Sub-card scope model

**Phase 8, item 1.** This document is the design; `kanban-core/kanban_core/scope_model.py`
is the enforcement. They are written together on purpose — the previous four phases each
found the same authorization confusion re-derived at a new layer, and the fix that finally
held was to *state the rule once* and route every call site through it. This is that
treatment for the parent/child boundary.

---

## 1. Why this document exists

A sub-card is a card spawned by another card: a finding spawns a remediation task, a recon
card spawns a follow-up probe, an orchestrator splits work. The moment a card can create
another card, a new authorization question appears that no existing check answers:

> **Can a child card be authorized for something its parent was not?**

If the answer is ever "yes", the whole scope model is decorative: any card that can spawn
work can mint itself a wider authorization, and the guardrail engine will faithfully enforce
the *child's* scope while the *engagement's* scope is quietly exceeded. So the answer must be
a structural "no", not a convention.

## 2. The invariant

> **A child's scope must be a subset of its parent's scope — never wider.**

Everything below exists to make that one sentence checkable, and to make the ways it can be
violated fail loudly rather than silently.

## 3. What "subset" means when a scope is absent

This is the subtle half, and it is where a naive implementation gets it wrong.

The obvious check is `set(child.targets) <= set(parent.targets)`. It is wrong, because it
treats an **absent** scope as the *empty* set — and the empty set is a subset of everything.
So "parent has a scope, child has none" would pass, and the child would be unbounded.

**An absent scope is not the narrowest scope; it is the widest.** A card with no scope
attached is a card whose targets are unbounded. So in this model `None` behaves like the
*universal* set, not the empty one:

| parent scope | child scope | verdict | why |
|---|---|---|---|
| bounded | bounded, ⊆ parent | **allow** | the ordinary case |
| bounded | bounded, ⊄ parent | **refuse** | widening |
| bounded | absent | **refuse** | absent = unbounded ⊄ bounded |
| absent | bounded | **allow** | narrowing from unbounded |
| absent | absent | **allow** | nothing was narrowed, nothing was widened |

The third row is the one that matters. It is the case a set-subset check waves through, and
it is the case that turns a scope into a suggestion.

## 4. Inheritance: what a child gets by default

A child that does not specify a scope **inherits its parent's scope, copied**. Inheritance is
the default because the safe direction is the one that requires no thought: a caller who
forgets to narrow gets the parent's authorization, not an unbounded one.

A child that *does* specify a scope must satisfy §2 against the parent. There is no way to
express "no scope" for a child of a scoped parent — an explicitly empty scope is a widening
and is refused, exactly like an absent one. **You can narrow a scope; you cannot drop it.**

## 5. Narrowing: what a child may change

A child may:

- **drop** targets the parent had (a child that touches fewer hosts),
- **replace** a parent CIDR with a smaller one inside it (`10.0.0.0/16` → `10.0.0.0/24`),
- **replace** a parent CIDR with a single host inside it,
- **add** an `authorization_ref`, `authorized_by` or `expires_at` — narrowing in *time* and
  *provenance* is still narrowing.

A child may **not**:

- add a target the parent did not cover,
- widen a CIDR beyond the parent's,
- drop the parent's `authorization_ref` while keeping its targets (the child would then be
  authorized for the same hosts with less on record — see §6),
- carry a tier ceiling above the parent's.

CIDR containment is checked with `ipaddress.ip_network(...).subnet_of(...)`, not string
prefix matching: `10.0.0.0/8` and `10.0.0.0/24` share a prefix but the *first* is the wider
one, and a prefix comparison gets that backwards for the case that matters.

## 6. The tier/flag rule at the parent-child boundary

The rule the project has now stated once (`guardrails.scope_is_required`) is:

> **tier governs whether a scope is *mandated* (T2+); the flag governs whether a scope is
> *enforced* once attached.** Tier may widen the check, never narrow it.

At the parent/child boundary it applies as follows:

1. **The child inherits the parent's scope *requirement*, not just its scope.** If the parent
   carried a scope, the child must carry one — regardless of the child's own
   `requires_scope` flag. A child whose flag is `False` does not thereby escape its parent's
   authorization; the flag can only ever *add* a requirement, never remove an inherited one.
   This is the same "tier may widen, never narrow" direction, applied to inheritance.
2. **The child's tier ceiling is the parent's ceiling.** A child may not bind a tool above
   the tier its parent was authorized for. The parent's ceiling is `parent.max_tier` by
   default — the fail-closed choice — and a parent that binds no tools (a *container* card)
   may declare an explicit ceiling in `meta["tier_ceiling"]`. The default is deliberately the
   strict one: a container that wants to spawn T2 work must say so, in a field an auditor can
   read, rather than getting it by omission.
3. **A child of a killed parent cannot run.** The kill switch is inherited. A parent killed
   mid-engagement must not leave live children behind it, or the kill switch is a suggestion.

## 7. Validation: where it runs, and what it refuses

Validation runs at **two** points, deliberately:

- **At creation** (`KanbanService.create_subcard`) — the child is refused before it exists.
  A refused spawn writes an audit entry too (§8): a refusal an operator cannot see is one
  they will re-attempt.
- **At `Running`** (`state_machine.guards_for`, when the parent is supplied) — the child's
  scope is re-checked against the parent's *current* scope. This is not redundant: a parent's
  scope can be edited after the child was created, and a child validated against yesterday's
  parent scope is not validated against today's. Re-checking at the moment of execution is
  the same defence-in-depth the tool layer already applies to arguments.

Refusals carry machine-readable reasons, one per cause, in the same vocabulary the rest of
the system uses (`GuardCodes`), so the board UI can render them without a second mapping.

## 8. Audit: what is written, and why

Every child creation appends one hash-chained event, `card.subcard.created`, carrying:

- `parent_id`, `child_id`,
- the **parent scope summary** and the **child scope summary** (so the narrowing is visible
  in the chain without re-deriving it),
- the **narrowing verdict** (`narrowed` / `inherited`),
- the parent's and child's tier ceilings.

The point of recording both summaries is that the *relationship* is the auditable fact, not
either scope alone. A chain that records only the child's scope cannot answer "was this child
ever wider than its parent?" after the parent has been edited. A refused spawn appends
`card.subcard.refused` with the reasons, for the same reason the tool layer records refused
calls: an attempt that leaves no trace is invisible to an auditor.

## 9. Lifecycle: how a child's state interacts with its parent's

- **Movement is independent.** A child moves through Backlog → Assigned → Running → Review →
  Done on its own; a parent does not gate its children's ordinary progress.
- **A parent cannot reach `Done` while it has open children.** "Open" means not `Done` and
  not `Blocked`. A parent marked Done with a live child is a card that claims to be finished
  while work it spawned is still running — the board would show a completed engagement with
  an active remediation task under it. The parent is refused with `open_children`, naming
  them, so the operator can see what is outstanding rather than being told "no".
- **Killing a parent freezes its children.** They are not moved (a kill is not a transition);
  they are refused at `Running` with `parent_killed`. Freezing rather than cascading a move
  keeps the kill switch's existing contract — it freezes a card where it stands — and avoids
  a kill silently rewriting the board.
- **A child may be killed on its own** without affecting the parent. Narrowing the blast
  radius of a kill is the point of a sub-card.

## 10. What this model deliberately does not do

- **It does not re-derive target detection.** Narrowing compares *scopes*, and the address
  shapes inside them are the ones `Scope.covers` already understands. A second classifier
  here would drift from the one the guardrail enforces — the exact failure Phase 3 spent a
  workstream removing.
- **It does not enforce the child's scope against the child's *arguments*.** That is the
  guardrail engine's job, at the tool boundary, and it already does it by value as well as by
  declaration. This model answers one question — *is this child's authorization inside its
  parent's?* — and leaves the rest where it belongs.
- **It does not make a child's scope immutable.** A parent's scope can be edited; the child
  is re-validated at `Running` (§7) rather than being frozen at creation. Freezing would be
  simpler and would also mean a legitimate narrowing of the parent could never propagate.
- **It does not decide whether a spawn is *wise*.** A child that narrows correctly and is
  still a bad idea is a judgement call, and the model does not pretend to make it.
