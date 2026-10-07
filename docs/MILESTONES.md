# Milestones and checkpoints

**Item 10 of the Dream list.** A dated checkpoint to review progress, and the
standing item about contacting a teammate/maintainer if access is still unclear.

**Set:** 2026-10-06 · **Author:** agent run on `main`

---

## M1 — Progress review · **by 2026-10-20** (two weeks from set)

A single review, not a rolling standup. The point is to decide *what to keep
doing*, which needs enough elapsed time for the answer to be real.

**Read first**, in this order:

1. `HANDOFF.md` → the "what remains" list. If it is stale, fixing it *is* the
   first review item.
2. `BUILD_STATUS.md` → per-component counts.
3. `scripts/scope_boundary_audit.py` output → `RESULT: PASS` or the offender list.

**Questions the review must answer:**

| Question | Why it is on the list |
|---|---|
| Did the ISO boot to the Hermes session on a ≥4 GB host? | `bootable_iso: true` but `hermes_session_reached: false` is the largest open gap; it is the difference between "an image" and "a working system" |
| Did the live crew run on the bundled model with a larger memory cgroup? | The 2 GB cgroup forced the smaller model; the 3b path is unverified until it runs somewhere with room |
| Is the local `opencode` clone green on the four handoff checks (`docs/local_opencode_workflow.md` §"What handoff is ready means")? | The handoff is only complete when it reproduces off the sandbox |
| Are the file-manager drop surface and start-menu polish done? | The remaining Phase 6 items; only relevant if they are still the agreed next work |

**Definition of done for M1:** each row above has a written answer in
`BUILD_STATUS.md`, and the two open gaps either have evidence or are explicitly
re-scoped as not-planned. A row that is still "unknown" is a failed review, not a
neutral one.

**Re-set or close:** if the answer to three or more rows is "not done and not
started", the honest move is to re-scope the project rather than re-book the same
review — that pattern means the plan, not the calendar, is the problem.

---

## C1 — Teammate / maintainer contact · **by 2026-10-13** (next week)

**The item:** if access is still unclear, contact a teammate or maintainer.

**What "still unclear" means here, concretely** — access to any of:

- the **Unity / Petrichor** project (separate private repo; `UNITY_LICENSE`
  questions in `docs/unity_license_setup.md` assume an account and an org);
- a host able to build and boot the ISO (≥15 GB disk to build, ≥4 GB RAM free to
  boot) — if no such host exists in the team, that is a conversation, not a build
  problem;
- the **model endpoint / crew runtime** for the live crew path if it is meant to
  run against a shared service rather than a local model.

**Why it is dated rather than left open:** every one of these is blocking work
that cannot be unblocked from inside the repo. An open-ended "ask someone at some
point" reliably becomes "never asked", and the cost of asking early is a message;
the cost of asking late is a stalled phase.

**The ask, when it happens — keep it to three lines:**

> We are at `<state>` on AI-native Kali / Petrichor. I am blocked on `<one
> specific thing>` (untested because `<reason>`). Can you `<specific action>` by
> `<date>`, or point me at who can?

Naming one thing and one action is what makes it answerable. A general "how is
access going" gets a general reply.

---

## Standing practice from this round

- **Every number in the status docs cites the command that produced it.** Two
  documents in this repo have been corrected in previous phases for stating a
  count that no longer held; the rule is what keeps that from recurring.
- **A review that finds nothing is a review that did not look.** Both rows above
  began as "unknown" and one of them (`bootable_iso` vs `hermes_session_reached`)
  only became visible because a claim was checked against a command.
- **Checkpoints are dated in absolute terms.** "In two weeks" drifts; `2026-10-20`
  does not.
