"""Agent role registry: who the agents are and which tools they may bind.

Blueprint ref: section 06 - 'agent roles, tool bindings to Kali binaries'.

A role is a *contract*, not a personality: it declares which board kinds it
serves, which tools it is allowed to call, and the maximum guardrail tier it may
ever touch. The runtime refuses to bind a tool above that ceiling, so a role
cannot escalate itself by editing a card.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AgentRole(BaseModel):
    """One agent identity in the runtime."""

    name: str
    display_name: str
    goal: str
    backstory: str = ""
    #: tool names this role may call
    tools: list[str] = Field(default_factory=list)
    #: highest guardrail tier this role may ever touch
    max_tier: int = 1
    #: board kinds this role works
    boards: list[str] = Field(default_factory=lambda: ["agent"])
    #: crew this role belongs to. **Empty means the role belongs to no pipeline
    #: crew** - it runs at the board level (the orchestrator), so it is not a step
    #: of any crew and no crew manifest should list it.
    crew: str = ""

    def may_use(self, tool: str, tier: int) -> bool:
        return tool in self.tools and tier <= self.max_tier


ROLE_REGISTRY: dict[str, AgentRole] = {
    "orchestrator": AgentRole(
        name="orchestrator",
        display_name="Orchestrator",
        goal="Keep the board honest: assign work, spot blocked cards, draft remediation cards.",
        backstory="Runs the board rather than the jobs. Never touches a tool above T0.",
        tools=["log_digest"],
        max_tier=0,
        boards=["agent", "system", "engagement", "personal"],
        # Empty on purpose: the orchestrator runs the *board*, it is not a step in
        # any pipeline crew. Phase 7 item 6 found this field reading "reporting",
        # which was both stale and a lie - no reporting crew lists an orchestrator
        # step, and the equivalence test that compares this field to the crew's
        # agent list is what surfaced it. An empty value states the truth; the
        # alternative would have been padding the reporting crew with a step it
        # never runs, in order to satisfy a misplaced assertion.
        crew="",
    ),
    "recon-specialist": AgentRole(
        name="recon-specialist",
        display_name="Recon Specialist",
        goal="Map the authorised attack surface without tripping anyone's IDS.",
        backstory="Starts passive (whois, DNS) and only escalates to active scanning once the "
        "target is confirmed to be inside the signed scope.",
        tools=["whois_lookup", "dns_lookup", "nmap_scan", "httpx_probe"],
        max_tier=1,
        boards=["engagement", "agent"],
        crew="recon",
    ),
    "web-specialist": AgentRole(
        name="web-specialist",
        display_name="Web Application Specialist",
        goal="Find web-layer weaknesses and prove them only as far as the rules of engagement allow.",
        backstory="Refuses to run an intrusive scan without an approved gate on the card, every time.",
        tools=["httpx_probe", "nikto_scan"],
        max_tier=2,
        boards=["engagement"],
        crew="vuln-assessment",
    ),
    "vuln-analyst": AgentRole(
        name="vuln-analyst",
        display_name="Vulnerability Analyst",
        goal="Turn raw scanner output into triaged, prioritised findings.",
        backstory="Reads traces rather than running new scans; escalates exploitable items as new cards.",
        tools=["httpx_probe", "nikto_scan"],
        max_tier=2,
        boards=["engagement"],
        crew="vuln-assessment",
    ),
    "report-writer": AgentRole(
        name="report-writer",
        display_name="Report Writer",
        goal="Produce the deliverable: findings, evidence, impact, remediation.",
        backstory="Works from card traces and artifacts so every claim in the report is traceable.",
        tools=[],
        max_tier=0,
        boards=["engagement", "agent"],
        crew="reporting",
    ),
    "system-agent": AgentRole(
        name="system-agent",
        display_name="System Agent",
        goal="Keep the OS itself healthy: logs, disk, updates, backups.",
        backstory="Only ever runs system maintenance tools in dry-run unless the card explicitly"
        "requests otherwise and the operator has approved it.",
        tools=["log_rotate"],
        max_tier=0,
        boards=["system"],
        crew="system",
    ),
    "remediation-specialist": AgentRole(
        name="remediation-specialist",
        display_name="Remediation Specialist",
        goal="Close a finding: verify the fix landed and the exposure is gone.",
        backstory="Works one finding at a time, on the narrowest scope that finding "
        "names - never the whole engagement. Re-checks rather than assuming, because "
        "'we patched it' and 'it is patched' are different claims.",
        # T1 at most: remediation *verifies* a fix (patch level, service exposure,
        # firewall rule, integrity baseline). It does not re-exploit, so it never
        # needs the T2 gate - and a remediation crew that could re-run an exploit
        # would be a second way to do the thing the gate exists to control.
        tools=[
            "patch_level_check",
            "service_exposure_check",
            "firewall_audit",
            "integrity_baseline",
        ],
        max_tier=1,
        boards=["engagement", "agent"],
        crew="remediation",
    ),
}


def get_role(name: str) -> Optional[AgentRole]:
    return ROLE_REGISTRY.get(name)


def require_role(name: str) -> AgentRole:
    role = ROLE_REGISTRY.get(name)
    if role is None:
        raise KeyError(f"unknown agent role '{name}'")
    return role
