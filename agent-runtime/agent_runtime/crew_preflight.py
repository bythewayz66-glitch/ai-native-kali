"""Crew pre-flight: prove a crew can actually run before it is dispatched.

Why this exists
---------------
Phase 15's boundary audit found two declaration-level defects that a run would
only surface as a confusing failure halfway through:

* a crew step bound a tool that **was not in the registry at all**, so the role's
  authority was a declaration nothing enforced; and
* a tool declared its scope parameter as a *file path*, so the scope check
  compared ``/etc/aide/aide.conf`` against a host and refused legitimate work.

Both are visible without executing anything. Checking them up front turns "the
crew failed for reasons the operator has to reverse-engineer" into "this crew
cannot be dispatched, and here is the line to fix".

What it checks, and what it deliberately does not
-------------------------------------------------
It checks three things, each of which is a *fact about the declaration*:

1. every tool a step names exists in the registry;
2. no step's tools exceed the crew's own ``max_tier`` ceiling; and
3. each step's role exists.

It does **not** try to decide whether a tool is appropriate for a role, or
whether a target is authorised - the first is a judgement the registry cannot
make, and the second is the guardrail engine's job at call time, with the scope
in hand. A pre-flight that guessed at either would be a second, weaker
authorisation path, which is worse than none.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .crews import CrewDef
from .roles import ROLE_REGISTRY

#: Machine-readable codes, in the vocabulary the rest of the system uses.
class PreflightCodes:
    UNKNOWN_TOOL = "crew_unknown_tool"
    TIER_EXCEEDED = "crew_tier_exceeded"
    UNKNOWN_ROLE = "crew_unknown_role"
    EMPTY_CREW = "crew_empty"


@dataclass
class PreflightReport:
    """Whether a crew may be dispatched, and why not if it may not."""

    crew: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tools_checked: int = 0
    unknown_tools: list[str] = field(default_factory=list)
    tier_exceeded: list[str] = field(default_factory=list)
    unknown_roles: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "crew": self.crew,
            "ok": self.ok,
            "problems": self.problems,
            "warnings": self.warnings,
            "tools_checked": self.tools_checked,
            "unknown_tools": self.unknown_tools,
            "tier_exceeded": self.tier_exceeded,
            "unknown_roles": self.unknown_roles,
        }


def _registry_tools(registry: Any) -> Optional[dict[str, Any]]:
    """Resolve the tool registry, or ``None`` if the tool layer is unavailable.

    Returning ``None`` rather than raising matters: the pre-flight runs inside the
    bridge's claim path, and a missing tool layer must not crash the card - it
    must degrade to \"cannot prove this crew is dispatchable\" and let the caller
    decide. Import is local because this module is imported by the bridge, which
    must load even when only the tool layer is absent.
    """
    if registry is not None:
        try:
            return {getattr(s, "name", ""): s for s in registry.all()}
        except Exception:  # noqa: BLE001
            # An injected object that is not a registry is still "unavailable",
            # not fatal. This path was the one the docstring above promised to
            # guard and did not: only the *import* branch was wrapped, so passing
            # an object without ``.all()`` raised straight through the bridge's
            # claim path. Caught by
            # test_missing_registry_warns_rather_than_failing.
            return None
    try:
        from tool_frontends.registry import get_registry  # noqa: PLC0415

        return {s.name: s for s in get_registry().all()}
    except Exception:  # noqa: BLE001 - any import/shape failure means "unavailable"
        return None


def preflight_crew(crew: CrewDef, *, registry: Any = None) -> PreflightReport:
    """Check one crew against the registry and its own ceiling.

    ``registry`` is injectable so tests can exercise the failure modes without
    the real tool layer, and so a caller that already holds the registry does not
    pay to rebuild it.
    """
    tools = _registry_tools(registry)
    report = PreflightReport(crew=crew.name, ok=True)

    if not crew.steps:
        report.ok = False
        report.problems.append(
            f"{PreflightCodes.EMPTY_CREW}: crew '{crew.name}' has no steps, so it can never do anything"
        )
        return report

    for step in crew.steps:
        if step.role not in ROLE_REGISTRY:
            report.ok = False
            report.unknown_roles.append(step.role)
            report.problems.append(
                f"{PreflightCodes.UNKNOWN_ROLE}: step names role '{step.role}', which is not in the role registry"
            )
        for tool_name in step.tools:
            report.tools_checked += 1
            if tools is None:
                report.warnings.append(
                    "tool registry unavailable: crew tool bindings could not be verified"
                )
                break
            spec = tools.get(tool_name)
            if spec is None:
                report.ok = False
                report.unknown_tools.append(tool_name)
                report.problems.append(
                    f"{PreflightCodes.UNKNOWN_TOOL}: step '{step.role}' binds '{tool_name}', "
                    "which is not in the tool registry"
                )
                continue
            tier = int(getattr(spec, "tier", 0))
            if tier > crew.max_tier:
                report.ok = False
                report.tier_exceeded.append(f"{tool_name}(T{tier})")
                report.problems.append(
                    f"{PreflightCodes.TIER_EXCEEDED}: step '{step.role}' binds '{tool_name}' "
                    f"at T{tier}, above the crew ceiling T{crew.max_tier}"
                )
    return report


def preflight_all(*, registry: Any = None) -> dict[str, PreflightReport]:
    """Every registered crew, pre-flighted. Used by the audit and the smoke run."""
    from .crews import CREWS  # noqa: PLC0415

    tools = _registry_tools(registry) if registry is not None else None
    return {name: preflight_crew(crew, registry=registry) for name, crew in CREWS.items()}


def known_tool_names() -> set[str]:
    """The names the tool layer actually provides, for a caller building a crew."""
    tools = _registry_tools(None)
    return set(tools or {})
