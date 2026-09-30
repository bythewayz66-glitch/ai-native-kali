"""Crew definitions: security workflows mapped onto crews.

Blueprint ref: section 06 - 'how crews map to security workflows (recon crew,
vuln-assessment crew, reporting crew)'.

A crew is a named pipeline: an ordered list of roles, each with the tools it
will reach for and a guardrail ceiling. The bridge executes the pipeline and
writes each step back to the card.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from agent_runtime.roles import ROLE_REGISTRY


class CrewStep(BaseModel):
    """One role's contribution to a crew run."""

    role: str
    tools: list[str] = Field(default_factory=list)
    description: str = ""


class CrewDef(BaseModel):
    """A crew: the workflow that a card triggers."""

    name: str
    display_name: str
    goal: str
    #: board kinds this crew serves
    boards: list[str] = Field(default_factory=list)
    steps: list[CrewStep] = Field(default_factory=list)
    #: highest tier any step in this crew may touch
    max_tier: int = 1

    @property
    def roles(self) -> list[str]:
        return [s.role for s in self.steps]

    def tools(self) -> list[str]:
        out: list[str] = []
        for step in self.steps:
            for tool in step.tools:
                if tool not in out:
                    out.append(tool)
        return out


CREWS: dict[str, CrewDef] = {
    "recon": CrewDef(
        name="recon",
        display_name="Recon Crew",
        goal="Map the authorised attack surface: passive first, then low-impact active probing.",
        boards=["engagement", "agent"],
        max_tier=1,
        steps=[
            CrewStep(
                role="recon-specialist",
                tools=["whois_lookup", "dns_lookup", "nmap_scan", "httpx_probe"],
                description="Enumerate ownership, DNS records and reachable services on the target.",
            ),
        ],
    ),
    "vuln-assessment": CrewDef(
        name="vuln-assessment",
        display_name="Vulnerability Assessment Crew",
        goal="Identify and triage vulnerabilities on assets already confirmed in scope.",
        boards=["engagement"],
        max_tier=2,
        steps=[
            CrewStep(
                role="web-specialist",
                tools=["httpx_probe", "nikto_scan"],
                description="Fingerprint the web stack, then run an intrusive web scan under the card's gate.",
            ),
            CrewStep(
                role="vuln-analyst",
                tools=[],
                description="Triage the raw output into ranked findings with remediation notes.",
            ),
        ],
    ),
    "reporting": CrewDef(
        name="reporting",
        display_name="Reporting Crew",
        goal="Turn card traces and artifacts into the deliverable report.",
        boards=["engagement", "agent"],
        max_tier=0,
        steps=[
            CrewStep(
                role="report-writer",
                tools=[],
                description="Assemble findings, evidence and remediation guidance from the card history.",
            ),
        ],
    ),
    "system": CrewDef(
        name="system",
        display_name="System Maintenance Crew",
        goal="Keep the host itself healthy.",
        boards=["system"],
        max_tier=0,
        steps=[
            CrewStep(
                role="system-agent",
                tools=["log_rotate"],
                description="Run OS hygiene jobs in dry-run unless explicitly approved.",
            ),
        ],
    ),
    "remediation": CrewDef(
        name="remediation",
        display_name="Remediation Crew",
        goal="Close a finding: verify the fix landed and the exposure is gone.",
        boards=["engagement", "agent"],
        # T1: remediation verifies, it does not re-exploit. See the role's note.
        max_tier=1,
        steps=[
            CrewStep(
                role="remediation-specialist",
                tools=[
                    "patch_level_check",
                    "service_exposure_check",
                    "firewall_audit",
                    "integrity_baseline",
                ],
                description=(
                    "Re-check the finding's target: patch level, service exposure, "
                    "firewall rule and integrity baseline, on the narrowed scope only."
                ),
            ),
        ],
    ),
}


def get_crew(name: str) -> Optional[CrewDef]:
    return CREWS.get(name)


def crew_for_board(board_kind: str) -> Optional[CrewDef]:
    """Default crew for a board kind, used when a card names no crew."""
    for crew in CREWS.values():
        if board_kind in crew.boards:
            return crew
    return None


def require_crew(name: str) -> CrewDef:
    crew = CREWS.get(name)
    if crew is None:
        raise KeyError(f"unknown crew '{name}'")
    return crew


# ---------------------------------------------------------------------------
# Which roles run on the model path (Phase 6, item 4)
# ---------------------------------------------------------------------------
# Phase 2 put a real model behind the *recon* and *vuln-assessment* crews - the
# two that happened to have YAML manifests - and left the rest on the
# deterministic runner. That made model coverage a side effect of who wrote a
# manifest, which is not a reason.
#
# Phase 6 makes coverage a property of the *role*: every role in the registry
# plans with the model when one is reachable, and every one of them still falls
# back to the deterministic runner when none is. The fallback is not a legacy
# path - a build host with no GPU or no API key is the normal CI case, so a role
# that could not fall back would make the suite unrunnable.
#
# The sets are derived from the registry rather than written out as six names:
# a seventh role must widen coverage on its own, and a test pins the count so it
# cannot silently shrink back to the two specced crews.

#: Every registered role, in declaration order.
ALL_ROLES: tuple[str, ...] = tuple(ROLE_REGISTRY)


def model_roles() -> frozenset[str]:
    """Roles whose planning step may call a model. Currently: all of them."""
    return frozenset(ROLE_REGISTRY)


def role_uses_model(role: str) -> bool:
    """Does this role have a model-backed plan path?

    False only for a role that is not registered at all - such a role has no
    tool ceiling and therefore nothing safe to plan with.
    """
    return role in ROLE_REGISTRY


def crew_roles(crew: str) -> tuple[str, ...]:
    """Roles a named crew runs, in execution order."""
    return tuple(require_crew(crew).roles)
