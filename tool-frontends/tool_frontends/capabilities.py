"""Capability manifest: one machine-readable statement of what the tool layer can do.

Blueprint ref: sections 05 and 08 - the tool schema an agent, an operator and an
external MCP client all read.

Why one manifest instead of per-surface answers
-----------------------------------------------
The registry's ``summary()``, the MCP ``tools/list`` shape and the shell's tool
panel each answered a piece of "what can this thing do". They could not be
compared, and they drifted: the MCP listing carried ``requires_scope`` but not
the local footprint, while the shell showed tier and category. An operator asking
"which of these tools writes to my filesystem?" had to read 74 specs.

This module answers the questions an operator actually asks, in one document:

* **what** - every tool with its tier, category, effects and guardrail flags,
* **totals** - counts by tier and category,
* **footprint** - the union of every effect any tool can have, so the layer's
  worst-case local reach is a single list rather than an inference,
* **gaps** - tools whose footprint is inferred rather than declared, and tools
  that mutate but are not sandboxed (which the registry gate should already
  prevent - reporting it here is how a bypass would be noticed).

It is derived entirely from the registry, so it cannot disagree with what the
enforcement path reads.
"""
from __future__ import annotations

from typing import Any, Iterable

from .effects import EFFECTS, describe as describe_effects
from .spec import TOOL_TIERS, ToolSpec


def _tool_entry(spec: ToolSpec) -> dict[str, Any]:
    report = spec.effect_report()
    return {
        "name": spec.name,
        "binary": spec.binary,
        "tier": spec.tier,
        "category": spec.category,
        "description": spec.description,
        "effects": report["effects"],
        "effects_declared": report["declared"],
        "inferred_effects": report["inferred"],
        "mutating": report["mutating"],
        "sensitive": report["sensitive"],
        "requires_scope": spec.requires_scope,
        "requires_approval": spec.requires_approval,
        "requires_sandbox": spec.requires_sandbox,
        "integration": spec.integration,
    }


def build_manifest(registry: Any) -> dict[str, Any]:
    """Assemble the capability manifest from a populated registry."""
    tools: list[ToolSpec] = registry.all()
    entries = [_tool_entry(s) for s in tools]

    by_tier: dict[int, int] = {t: 0 for t in range(4)}
    by_category: dict[str, int] = {}
    footprint: set[str] = set()
    mutating: list[str] = []
    sensitive: list[str] = []
    unsandboxed_mutators: list[str] = []
    inferred: list[str] = []

    for entry in entries:
        by_tier[entry["tier"]] = by_tier.get(entry["tier"], 0) + 1
        by_category[entry["category"]] = by_category.get(entry["category"], 0) + 1
        footprint.update(entry["effects"])
        if not entry["effects_declared"]:
            inferred.append(entry["name"])
        if entry["mutating"]:
            mutating.append(entry["name"])
            # A mutating tool without the sandbox flag is the exact state the
            # guardrail stage-4b floor refuses. It should therefore be impossible
            # for a *runnable* tool; listing it is how a bypass gets noticed.
            if not entry["requires_sandbox"] and entry["tier"] >= 1:
                unsandboxed_mutators.append(entry["name"])
        if entry["sensitive"]:
            sensitive.append(entry["name"])

    return {
        "version": "0.2.0",
        "tiers": TOOL_TIERS,
        "totals": {
            "tools": len(tools),
            "by_tier": by_tier,
            "by_category": dict(sorted(by_category.items())),
        },
        "effects_vocabulary": list(EFFECTS),
        "footprint": {
            "union": [e for e in EFFECTS if e in footprint],
            "mutating_tools": sorted(mutating),
            "sensitive_tools": sorted(sensitive),
        },
        "gaps": {
            "effects_inferred": sorted(inferred),
            "effects_inferred_count": len(inferred),
            "mutating_without_sandbox": sorted(unsandboxed_mutators),
            "mutating_without_sandbox_count": len(unsandboxed_mutators),
        },
        "tools": entries,
    }


def manifest_for(specs: Iterable[ToolSpec]) -> dict[str, Any]:
    """Manifest for an explicit tool list (used by tests and partial views)."""
    entries = [_tool_entry(s) for s in specs]
    return {"version": "0.2.0", "tools": entries, "totals": {"tools": len(entries)}}


def summarise(manifest: dict[str, Any]) -> str:
    """One-line human summary, for a log line or a health banner."""
    totals = manifest.get("totals", {})
    gaps = manifest.get("gaps", {})
    return (
        f"{totals.get('tools', 0)} tools across T0-T3; "
        f"{len(manifest.get('footprint', {}).get('union', []))} distinct effects; "
        f"{gaps.get('effects_inferred_count', 0)} footprint(s) inferred, "
        f"{gaps.get('mutating_without_sandbox_count', 0)} unsandboxed mutator(s)"
    )
