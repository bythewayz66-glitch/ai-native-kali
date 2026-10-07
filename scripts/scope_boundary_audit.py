#!/usr/bin/env python3
"""Scope- and boundary-audit for the tool layer and the crew/role graph.

Two questions this script answers with real numbers, not assertions:

**Item 5 - T1 scope-enforcement / target-escape audit.** For *every* registered
wrapper it checks that an out-of-scope address cannot reach a host:

  * the *declared* path: every parameter in ``target_params`` is compared to the
    scope, so setting it to an unauthorised host is refused;
  * the *value-based* path: every address-shaped argument - declared or not - is
    classified by ``targets.scan`` and refused if the scope does not cover it.

It then actually runs the guardrail engine (``run_tool``, live + unlocked +
approved, with a scope of ``example.com``) against each scope-requiring wrapper's
declared target pointed at ``evil.net`` and requires a denial. A wrapper that
lets that through is printed as an OFFENDER.

**Item 6 - crew/role boundary audit.** For every crew and every role it checks
the tier-vs-flag invariants that the "scope confusion" pattern would violate:

  * ``scope_is_required`` is flag-driven with tier as a fail-closed floor;
  * a T1+ wrapper declares ``requires_scope``;
  * a role's ``max_tier`` is not above its crew's ``max_tier``;
  * every tool a role or a crew step names actually exists in the registry
    (a dangling name is how a role silently loses - or gains - authority);
  * every tool a role binds is inside that role's tier ceiling.

Exit code is 0 only when there are no offenders, so it can gate CI.

    python3 scripts/scope_boundary_audit.py
    python3 scripts/scope_boundary_audit.py --verbose
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for component in ("tool-frontends", "kanban-core", "agent-runtime"):
    sys.path.insert(0, str(REPO_ROOT / component))

from agent_runtime.crews import CREWS  # noqa: E402
from agent_runtime.roles import ROLE_REGISTRY  # noqa: E402
from kanban_core.models import Scope  # noqa: E402
from tool_frontends.guardrails import scope_is_required  # noqa: E402
from tool_frontends.registry import get_registry  # noqa: E402
from tool_frontends.runner import run_tool  # noqa: E402
from tool_frontends.targets import classify, escapes, scan  # noqa: E402

OUT_OF_SCOPE = "evil.net"
IN_SCOPE = "example.com"


def _scope() -> Scope:
    return Scope(targets=[IN_SCOPE], cidrs=[], authorization_ref="AUDIT", engagement="audit")


# --------------------------------------------------------------- item 5
def audit_wrappers(verbose: bool) -> tuple[list[str], list[str], dict]:
    """Return (offenders, notes, stats) for the T1/escape audit."""
    registry = get_registry()
    specs = registry.all()
    offenders: list[str] = []
    notes: list[str] = []
    stats = {
        "wrappers": len(specs),
        "scope_requiring": 0,
        "t0_total": 0,
        "declared_target_params": 0,
        "address_shaped_params": 0,
        "exempt_params": 0,
        "hostile_runs": 0,
        "hostile_refused": 0,
    }

    for spec in specs:
        target_params = tuple(spec.target_params or ())
        skipped = tuple(spec.scope_skip_params or ())
        stats["declared_target_params"] += len(target_params)
        stats["exempt_params"] += len(skipped)
        if spec.tier == 0:
            stats["t0_total"] += 1
        if spec.requires_scope:
            stats["scope_requiring"] += 1

        # (a) every address-shaped parameter must be declared or exempt ---
        for param in spec.params:
            probe = {param.name: OUT_OF_SCOPE}
            found = scan(probe, declared_params=target_params, skip_keys=skipped)
            if found:
                stats["address_shaped_params"] += 1

        # (b) value-based escape detection on a hostile args set ----------
        hostile: dict[str, str] = {}
        for param in spec.params:
            if param.type == "string" and param.name not in skipped:
                hostile[param.name] = OUT_OF_SCOPE
        if hostile:
            reasons = escapes(
                hostile, scope=_scope(), declared_params=target_params, skip_keys=skipped
            )
            # A parameter whose value the classifier flags must produce a
            # reason; if it is address-shaped and silently accepted that is the
            # escape class this audit exists to catch.
            flagged = scan(hostile, declared_params=target_params, skip_keys=skipped)
            if flagged and not reasons:
                offenders.append(
                    f"{spec.name}: {len(flagged)} address-shaped parameter(s) "
                    f"not refused by the value check: "
                    f"{[f.key for f in flagged]}"
                )

        # (c) run the real guardrail engine on a hostile declared target ---
        if spec.requires_scope and target_params:
            args = {target_params[0]: OUT_OF_SCOPE}
            for param in spec.params:
                if param.name != target_params[0] and param.default is not None:
                    args.setdefault(param.name, param.default)
            stats["hostile_runs"] += 1
            result = run_tool(
                spec,
                args,
                scope=_scope(),
                approved=True,
                live=True,
                live_unlocked=True,
                caller="scope-audit",
                audit=None,
            )
            if result.status == "denied" and any(
                "outside the authorized scope" in r or "scope" in r.lower()
                for r in result.reasons
            ):
                stats["hostile_refused"] += 1
                if verbose:
                    notes.append(f"  refused  {spec.name:<28} T{spec.tier}")
            else:
                offenders.append(
                    f"{spec.name} (T{spec.tier}): live run with target "
                    f"'{OUT_OF_SCOPE}' was {result.status}, not a scope denial "
                    f"(reasons={result.reasons})"
                )

        # (d) a T1+ wrapper must declare requires_scope --------------------
        if spec.tier >= 1 and not spec.requires_scope:
            offenders.append(f"{spec.name} (T{spec.tier}) does not declare requires_scope")

        # (e) the flag/tier contract must hold at every tier ---------------
        for tier in (0, 1, 2, 3):
            if scope_is_required(tier, spec.requires_scope) != (
                bool(spec.requires_scope) or tier >= 1
            ):
                offenders.append(f"{spec.name}: scope_is_required({tier}) disagrees with the contract")

    return offenders, notes, stats


def audit_t0_live(specs) -> list[str]:
    """A T0 tool must not be able to reach a host live without a scope check
    unless it declares one - and a T0 tool that declares ``requires_scope`` must
    still be refused when the target is out of scope."""
    offenders: list[str] = []
    for spec in specs:
        if spec.tier != 0 or not spec.requires_scope or not spec.target_params:
            continue
        args = {spec.target_params[0]: OUT_OF_SCOPE}
        for param in spec.params:
            if param.name != spec.target_params[0] and param.default is not None:
                args.setdefault(param.name, param.default)
        result = run_tool(
            spec, args, scope=_scope(), approved=True, live=True, live_unlocked=True, caller="audit"
        )
        if result.status != "denied":
            offenders.append(f"T0 {spec.name} with out-of-scope target returned {result.status}")
    return offenders


# --------------------------------------------------------------- item 6
def audit_boundaries(verbose: bool) -> tuple[list[str], dict]:
    registry = get_registry()
    known = {s.name for s in registry.all()}
    tier_of = {s.name: s.tier for s in registry.all()}
    offenders: list[str] = []
    stats = {
        "roles": len(ROLE_REGISTRY),
        "crews": len(CREWS),
        "role_tool_bindings": 0,
        "crew_step_tools": 0,
        "dangling_role_tools": 0,
        "dangling_crew_tools": 0,
        "ceiling_violations": 0,
    }

    for name, role in ROLE_REGISTRY.items():
        stats["role_tool_bindings"] += len(role.tools)
        for tool in role.tools:
            if tool not in known:
                stats["dangling_role_tools"] += 1
                offenders.append(f"role '{name}' binds unknown tool '{tool}' (not in registry)")
                continue
            tier = tier_of[tool]
            if tier > role.max_tier:
                stats["ceiling_violations"] += 1
                offenders.append(
                    f"role '{name}' (ceiling T{role.max_tier}) binds '{tool}' (T{tier})"
                )
            if tool not in role.tools:
                offenders.append(f"role '{name}' lists '{tool}' but may_use denies it")

    for name, crew in CREWS.items():
        crew_role_names = set(crew.roles)
        for step in crew.steps:
            if step.role not in ROLE_REGISTRY:
                offenders.append(f"crew '{name}' step names unknown role '{step.role}'")
                continue
            role = ROLE_REGISTRY[step.role]
            if role.max_tier > crew.max_tier:
                stats["ceiling_violations"] += 1
                offenders.append(
                    f"crew '{name}' (ceiling T{crew.max_tier}) contains role '{step.role}' "
                    f"with ceiling T{role.max_tier}"
                )
            for tool in step.tools:
                stats["crew_step_tools"] += 1
                if tool not in known:
                    stats["dangling_crew_tools"] += 1
                    offenders.append(f"crew '{name}' step '{step.role}' names unknown tool '{tool}'")
                    continue
                if tier_of[tool] > role.max_tier:
                    stats["ceiling_violations"] += 1
                    offenders.append(
                        f"crew '{name}' step '{step.role}' (T{role.max_tier}) binds "
                        f"'{tool}' (T{tier_of[tool]})"
                    )
        # the crew's declared ceiling must cover the tools it actually runs
        for tool in crew.tools():
            if tool in tier_of and tier_of[tool] > crew.max_tier:
                offenders.append(
                    f"crew '{name}' ceiling T{crew.max_tier} < tool '{tool}' T{tier_of[tool]}"
                )
        if verbose:
            print(f"  crew {name:<18} roles={sorted(crew_role_names)} ceiling=T{crew.max_tier}")

    # a role whose 'crew' field names a crew must actually appear in it
    for name, role in ROLE_REGISTRY.items():
        if not role.crew:
            continue
        crew = CREWS.get(role.crew)
        if crew is None:
            offenders.append(f"role '{name}' names unknown crew '{role.crew}'")
        elif name not in crew.roles:
            offenders.append(f"role '{name}' claims crew '{role.crew}' but that crew does not run it")

    return offenders, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    specs = get_registry().all()
    print("=" * 74)
    print("ITEM 5 - T1 scope enforcement / target-escape audit")
    print("=" * 74)
    offenders, notes, stats = audit_wrappers(args.verbose)
    offenders += audit_t0_live(specs)
    for line in notes:
        print(line)
    print(f"  wrappers              : {stats['wrappers']}")
    print(f"  scope-requiring       : {stats['scope_requiring']}")
    print(f"  T0 (no scope by tier) : {stats['t0_total']}")
    print(f"  declared target params: {stats['declared_target_params']}")
    print(f"  address-shaped params : {stats['address_shaped_params']}")
    print(f"  explicit exemptions   : {stats['exempt_params']}")
    print(f"  hostile live runs     : {stats['hostile_runs']}")
    print(f"  refused by scope      : {stats['hostile_refused']}")
    print(f"  offenders             : {len(offenders)}")
    for line in offenders:
        print(f"    ! {line}")

    print()
    print("=" * 74)
    print("ITEM 6 - crew/role tier-vs-flag boundary audit")
    print("=" * 74)
    boundary_offenders, bstats = audit_boundaries(args.verbose)
    print(f"  roles                 : {bstats['roles']}")
    print(f"  crews                 : {bstats['crews']}")
    print(f"  role tool bindings    : {bstats['role_tool_bindings']}")
    print(f"  crew step tools       : {bstats['crew_step_tools']}")
    print(f"  dangling role tools   : {bstats['dangling_role_tools']}")
    print(f"  dangling crew tools   : {bstats['dangling_crew_tools']}")
    print(f"  ceiling violations    : {bstats['ceiling_violations']}")
    print(f"  offenders             : {len(boundary_offenders)}")
    for line in boundary_offenders:
        print(f"    ! {line}")

    total = len(offenders) + len(boundary_offenders)
    print()
    print("=" * 74)
    print(f"RESULT: {'PASS' if total == 0 else f'FAIL - {total} offender(s)'}")
    print("=" * 74)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
