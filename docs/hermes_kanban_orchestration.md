# Hermes Kanban as the orchestration surface for OS-level agent workflows

**Item 4 of the Dream list.** A concrete integration design, with the working
proof of concept that backs it (`agent_runtime/desktop_orchestration.py`,
`agent-runtime/tests/test_desktop_orchestration.py`).

---

## 1. What the board already orchestrates, and what it could not

The board owns the **tool** plane today. A card carries a scope; entering work
hands the card to a crew; the crew calls tools through the guardrail engine; every
call is written to a hash-chained audit log. That loop is verified (155/155 smoke,
full card → crew → tool → trace → audit → Review).

What it could not reach is the **desktop** — the windows and processes the work is
performed in and watched through. So the board could say "run `nmap` against this
scope" but not "and put the terminal that shows it in front of me". Those are the
same workflow, split across two planes that knew nothing about each other.

## 2. The rule that makes the integration safe

    The board decides *what* should be surfaced.
    The desktop manager decides whether the agent is *allowed* to do it.
    The audit log records both.

Everything below follows from that one line. The board gains **no new capability**
— it gains a way to *ask* the desktop for something it already knows how to do,
and it cannot ask for anything the desktop's own policy refuses.

## 3. Architecture

```
   ┌──────────────┐   card.metadata.surface_window    ┌────────────────────┐
   │  Kanban card │ ────────────────────────────────► │ desktop_orchestrat.│
   └──────────────┘                                   │  plan_surface()    │
          ▲                                           └─────────┬──────────┘
          │ board state                                         │ DesktopClient
          │                                                     │ (HTTP, never import)
   ┌──────┴───────┐        /api/desktop         ┌───────────────▼────────────┐
   │ kanban-core  │ ◄───── audit row ────────── │      hermes-shell :8085    │
   └──────────────┘                             │  DesktopManager (policy)   │
                                                │  WindowManager  (state)    │
                                                └───────────────┬────────────┘
                                                                │
                                                     ┌──────────▼─────────┐
                                                     │ windows + /proc    │
                                                     └────────────────────┘
```

**Why HTTP and not an import.** It would be shorter to import
`hermes_shell.agent_desktop` and call it in-process. That would also be wrong: the
desktop's authorisation (protected windows, the process allow-list) and its audit
trail live behind the shell service, and a second in-process path is a second
place those rules can be got wrong — or skipped entirely by whatever imported it.
One enforcement point, reached one way.

## 4. The contract

A card opts in through its metadata. Both keys are optional; a card with neither
behaves exactly as before.

| Card metadata | Meaning |
|---|---|
| `metadata.surface_window` | A window **id**, an **app**, or a **title** fragment to surface when the card runs |
| `metadata.surface_app` | Alias for the above when the card means an application |

Resolution order is **id → app → title**, and the order is the policy:

- an **id** is exact — if the card names one it gets *that* window or nothing,
  because falling back would surface a different window than the card asserted;
- an **app** is what a card can realistically know before the window exists (it
  does not choose the id), and the launcher's single-instance rule means there is
  at most one window per app;
- a **title** fragment is the loosest match and is only used when nothing more
  specific resolved, because a fragment can match several windows.

## 5. What happens on a run

1. `plan_surface(card, desktop_state)` — pure, no I/O. Returns a plan with
   `action` (`focus` or `None`), the window, how it matched, and a reason.
2. If there is an action, `DesktopClient.apply("focus", window_id=...)` calls the
   shell's **own** `POST /api/desktop`. The shell's policy rules — and may refuse.
3. `audit_surface(...)` writes one row to the **same** `ToolAuditLog` as the tool
   calls: `tool="desktop_surface"`, `target=<window id>`, `decision=<action>`,
   `caller="kanban-orchestrator"`, `dry_run=True`.

The `dry_run=True` is deliberate and honest: the row records an intention and its
outcome, and claiming `dry_run=False` for something that is not a tool execution
would misreport what the chain holds.

## 6. Failure modes, and why each was chosen

| Failure | Behaviour | Why |
|---|---|---|
| **Shell unreachable** | Reported as `desktop: "unreachable"`; the card is otherwise unaffected | The desktop is how work is *watched*. A board that refused to advance because a UI was down would make observability a hard dependency of the thing it observes |
| **Card names a window nobody opened** | No-op with a reason (`no open window matches …`) | **The board never opens a window to satisfy a card.** Opening one would let a card name an app and have it launched — an execution path the tool tiers do not cover |
| **Window already focused** | No-op, reason `already the focused window` | Avoids a pointless desktop call on every run |
| **Shell refuses (protected window)** | Surfaced as `outcome.ok == false` with the shell's reason | A policy refusal is a *decision*, not a transport error; the agent must be able to plan around it |
| **Audit log unavailable** | The plan still runs; the row is dropped | An audit failure must not lose the work — same rule as the drop and attach paths |

## 7. Proof of concept (real, over HTTP)

`test_poc_board_orchestrates_the_real_desktop_over_http` starts the actual
`hermes_shell` FastAPI app on a real port and drives it with the board's own
client. Nothing is mocked. The capture from the live run:

```
hermes-shell live on :34969
desktop at boot: {'windows': 0, 'processes': 21}
opened: win_0001 win_0002

--- orchestrate(card) ---
{'card_id': 'crd_poc', 'action': 'focus', 'window_id': 'win_0001',
 'wanted': 'win_0001', 'matched_by': 'id',
 'reason': "surface 'win_0001' (matched by id) for operator visibility",
 'candidates': ['nmap run', 'Kanban board']}
focused now: win_0001 (want win_0001)
windows opened for ghost card: None
protected close: {'ok': False, 'refused': True,
  'reason': 'hermes-panel is protected: the agent may not close its own panel or the session'}

--- crew prompt block ---
DESKTOP STATE (live, from hermes-shell)
DESKTOP
   win_0002                 browser        normal     Kanban board
 * win_0001                 terminal       normal     nmap run
PROCESSES (0 terminable)

--- audit chain ---
{'tool': 'desktop_surface', 'status': 'ok', 'target': 'win_0001',
 'decision': 'focus', 'caller': 'kanban-orchestrator'}
chain verified: True
```

That is the whole loop: a card moved a real window's focus, a ghost card did
nothing, a protected window refused, and the decision joined the same
tamper-evident chain the tool calls live in.

## 8. Where this meets item 2

Item 2 puts **remembered** context into a crew prompt before a run
(`recall_context`). `crew_desktop_context(card, client)` supplies the **live**
half of the same injection — what the desk the work is visible on looks like right
now — and returns `""` when the shell is unreachable so a caller can concatenate
it unconditionally. Together they are the two halves of "what does the crew need
to know that is not on the card".

## 9. Deliberately out of scope

- **Opening windows on a card's behalf.** A card names a window; it does not
  cause one to exist. That boundary is the whole reason this integration adds no
  capability.
- **Driving the compositor.** This layer never speaks Wayland/X11. The GTK client
  in `hermes-shell/hermes_shell/gtk/` is the surface for a real compositor; the
  window model here is the shell's own managed-window state.
- **Cross-host orchestration.** The desktop manager manages this host's windows
  and processes. A card for another host surfaces nothing here, correctly.
- **Persisting the plan.** The audit row is the record; there is no separate plan
  store to drift from it.
