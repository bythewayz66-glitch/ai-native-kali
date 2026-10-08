"""Tool *effects*: what a tool does to the system, declared rather than implied.

Blueprint ref: section 08 - the sandbox stage of the authorization chain.

Why this exists
---------------
The tier vocabulary says how *intrusive* a tool is toward its target (T0 passive
.. T3 exploit). It says nothing about what the tool does to the **host it runs
on**. Those are different questions and the layer only answered the first one:

* ``log_rotate`` is T0 - it sends no packets to anyone - yet it **rewrites files
  on the local filesystem**.
* ``sqlmap_test`` is T3 and is sandboxed because it attacks a remote target, not
  because it is dangerous locally.

So a tool could rewrite the local filesystem, spawn processes, or leave a
credential on disk while every guardrail check passed, because nothing in the
spec had to state it. The audit log then recorded *that* a tool ran without ever
recording *what kind of thing it did*.

This module makes the local footprint a **declared, closed vocabulary**, so:

1. a spec author has to write the effect down (it shows up in a diff and a grep),
2. ``guardrails.evaluate`` can refuse a mutating tool that is not sandboxed, and
3. the capability manifest the shell, the MCP transport and an operator read all
   derive from one source, so two surfaces cannot disagree about what a tool does.

The vocabulary is closed for the same reason ``graph.PREDICATES`` is: free text
produces ``writes``, ``writing`` and ``may write`` as three effects, which makes
the set unqueryable and the gate unenforceable.

Declaration first, inference as a bootstrap
-------------------------------------------
``infer_effects`` exists because 74 specs already ship without a declaration, and
a migration that has to land in one commit is a migration that never lands.
Inference is applied **only when a spec declares nothing**, and the spec then
reports ``effects_declared=False``, which the registry surfaces as a count. So
the gap is measured rather than hidden, enforcement is live for every tool from
day one, and each later declaration flips one tool from "inferred" to "stated".

Inference is a keyword/table heuristic and it is documented as such - it is a
starting point for a human to confirm, never an authority. A wrong inferred
effect can only make the gate *stricter* (mutating sets all require sandboxing),
so the failure direction is safe.

What is deliberately *not* here
-------------------------------
No severity, no tier, no "risk score". An effect is a fact about a tool, not a
judgement about it: ``fs.write`` on a log-rotation tool is routine and on an
exploitation tool is alarming, and only the tier plus the gate can tell those
apart. Encoding severity here would create a second, competing source of truth.
"""
from __future__ import annotations

from typing import Any, Iterable

#: The closed set of declarable effects.
#:
#: ``fs.read``      - reads files or directories on the host
#: ``fs.write``     - creates, modifies or deletes anything on the host
#: ``net.inbound``  - listens for or accepts inbound network connections
#: ``net.outbound`` - opens outbound connections / sends packets
#: ``proc.spawn``   - starts a process other than itself
#: ``proc.signal``  - signals (stops, kills) another process
#: ``secrets``      - reads or writes secret material (keys, credentials, tokens)
#: ``persist``      - leaves state behind that survives the process (units, cron)
EFFECTS: tuple[str, ...] = (
    "fs.read",
    "fs.write",
    "net.inbound",
    "net.outbound",
    "proc.spawn",
    "proc.signal",
    "secrets",
    "persist",
)

#: Effects that change the host. A tool declaring any of these must be sandboxed
#: from T1 up (see ``guardrails.evaluate``).
MUTATING_EFFECTS: frozenset[str] = frozenset(
    {"fs.write", "proc.spawn", "proc.signal", "persist"}
)

#: Effects that touch secret material. Tracked separately because a tool that
#: reads secrets deserves an explicit gate even when it mutates nothing.
SENSITIVE_EFFECTS: frozenset[str] = frozenset({"secrets"})

#: The effect set implied by *contacting a remote target*, used to cross-check a
#: spec's declaration against its tier. A T1+ tool that reaches a network target
#: must declare an outbound effect; a spec that does not is under-declared.
NETWORK_EFFECTS: frozenset[str] = frozenset({"net.outbound", "net.inbound"})

#: Binaries whose **live** form mutates the local host. Explicit rather than
#: keyword-guessed, because these are the ones with a real blast radius.
_MUTATING_BINARIES: dict[str, tuple[str, ...]] = {
    "logrotate": ("fs.write",),
    "sysctl": ("fs.write",),
    "mount": ("fs.write",),
    "umount": ("fs.write",),
    "chmod": ("fs.write",),
    "chown": ("fs.write",),
    "install": ("fs.write",),
    "dd": ("fs.write",),
    "mkfs": ("fs.write",),
    "apt": ("fs.write", "proc.spawn"),
    "apt-get": ("fs.write", "proc.spawn"),
    "dpkg": ("fs.write", "proc.spawn"),
    "pip": ("fs.write", "proc.spawn"),
    "npm": ("fs.write", "proc.spawn"),
    "systemctl": ("proc.spawn", "persist"),
    "service": ("proc.spawn",),
    "crontab": ("fs.write", "persist"),
    "useradd": ("fs.write", "persist"),
    "usermod": ("fs.write", "persist"),
    "passwd": ("fs.write", "secrets"),
    "kill": ("proc.signal",),
    "pkill": ("proc.signal",),
    "killall": ("proc.signal",),
}

#: Binaries that are pure local reads.
_READ_BINARIES: frozenset[str] = frozenset(
    {"cat", "ls", "journalctl", "grep", "awk", "sed", "head", "tail", "find", "stat", "file"}
)

#: Substrings in a rendered template that imply a local write. Checked on the
#: live template only - a dry-run template that contains ``>`` is describing an
#: effect, not performing one. Deliberately excludes ``-o``: it is far more often
#: an output *format* (``kubectl -o json``) than a file, and a suggestion list
#: that flags read-only tools is a list nobody trusts.
_WRITE_HINTS: tuple[str, ...] = (" > ", ">>", "--output", " --write", " -w ")


def unknown(effects: Iterable[str]) -> list[str]:
    """Effects that are not in the closed vocabulary (an authoring error)."""
    return sorted({e for e in effects if e not in EFFECTS})


def is_mutating(effects: Iterable[str]) -> bool:
    return bool(set(effects) & MUTATING_EFFECTS)


def touches_secrets(effects: Iterable[str]) -> bool:
    return bool(set(effects) & SENSITIVE_EFFECTS)


def reaches_network(effects: Iterable[str]) -> bool:
    return bool(set(effects) & NETWORK_EFFECTS)


def normalize(effects: Iterable[str]) -> list[str]:
    """Keep only known effects, in canonical order, de-duplicated."""
    present = {e for e in effects if e in EFFECTS}
    return [e for e in EFFECTS if e in present]


def infer_effects(
    *,
    binary: str = "",
    tier: int = 0,
    requires_scope: bool = False,
    live_template: str = "",
    dry_run_template: str = "",
) -> list[str]:
    """Best-effort effects for a spec that declares none.

    Order of evidence, strongest first:

    1. the binary's known footprint (:data:`_MUTATING_BINARIES`),
    2. the tier/scope declaration - a T1+ or scoped tool reaches a network,
    3. write-shaped arguments in the **live** template,
    4. known read-only binaries.

    The result can only ever be *larger* than the true set along the mutating
    axis, and a larger set makes the sandbox gate stricter rather than looser -
    which is the safe direction for a bootstrap.
    """
    out: set[str] = set()

    key = (binary or "").strip().lower().split("/")[-1]
    out.update(_MUTATING_BINARIES.get(key, ()))

    # A scoped or T1+ tool talks to a target over the network.
    if requires_scope or tier >= 1:
        out.add("net.outbound")

    live = live_template or ""
    if any(hint in live for hint in _WRITE_HINTS):
        out.add("fs.write")

    if not out and key in _READ_BINARIES:
        out.add("fs.read")

    return normalize(out)


def describe(effects: Iterable[str], *, declared: bool = True) -> dict[str, Any]:
    """A machine-readable statement of a spec's footprint."""
    listed = normalize(effects)
    return {
        "effects": listed,
        "declared": declared,
        "mutating": is_mutating(listed),
        "sensitive": touches_secrets(listed),
        "network": reaches_network(listed),
    }
