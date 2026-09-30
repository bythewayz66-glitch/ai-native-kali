"""Seed data: the four board classes from blueprint section 03.1.

agent board            - the crew's own work queue
security-engagement    - a scoped pentest engagement (authorization-bound)
system-maintenance     - OS hygiene: updates, disk, log rotation
personal               - the human's own tasks
"""
from __future__ import annotations

from .models import Column, Scope, ToolBinding
from .service import KanbanService

#: Deterministic ids so the smoke test and UI can reference boards directly.
BOARD_AGENT = "brd_agent"
BOARD_ENGAGEMENT = "brd_engagement"
BOARD_SYSTEM = "brd_system"
BOARD_PERSONAL = "brd_personal"


def seed(service: KanbanService, *, with_engagement: bool = True) -> dict[str, str]:
    """Create the default boards and a realistic starter set of cards."""
    boards = {
        "agent": service.create_board(
            "Agent Board",
            "agent",
            board_id=BOARD_AGENT,
            description="Crew work queue - every agent task is a card here.",
            default_crew="recon",
        ),
        "system": service.create_board(
            "System Maintenance",
            "system",
            board_id=BOARD_SYSTEM,
            description="OS hygiene jobs: updates, disk, log rotation, backups.",
        ),
        "personal": service.create_board(
            "Personal",
            "personal",
            board_id=BOARD_PERSONAL,
            description="The human operator's own tasks and reminders.",
        ),
    }
    if with_engagement:
        boards["engagement"] = service.create_board(
            "Engagement ACME-2026-Q3",
            "engagement",
            board_id=BOARD_ENGAGEMENT,
            description="Scoped security engagement. Every T2+ card is bound to the signed authorization.",
            default_crew="recon",
        )

    # -- agent board -----------------------------------------------------
    service.create_card(
        "Summarise the last 24h of tool-call traces",
        BOARD_AGENT,
        description="Read the observability trace store and produce a short digest for the operator.",
        priority="low",
        assignee="report-writer",
        crew="reporting",
        tools=[ToolBinding(name="log_digest", tier=0, dry_run=True)],
        labels=["observability", "daily"],
    )
    service.create_card(
        "Review blocked cards and propose unblock actions",
        BOARD_AGENT,
        description="Sweep the Blocked lane and draft remediation cards for the operator to approve.",
        priority="medium",
        assignee="orchestrator",
        crew="reporting",
        labels=["triage"],
    )

    # -- engagement board ------------------------------------------------
    scope = Scope(
        targets=["scanme.nmap.org", "example.com"],
        cidrs=["192.0.2.0/24"],
        authorization_ref="ACME-SOW-2026-0912",
        authorized_by="acme-ciso@example.com",
        expires_at="2026-12-31T23:59:59+00:00",
    )
    service.create_card(
        "Recon: scanme.nmap.org",
        BOARD_ENGAGEMENT,
        description="Passive-first recon of the authorized host: whois, DNS, then a T1 TCP top-ports scan.",
        priority="high",
        assignee="recon-specialist",
        crew="recon",
        scope=scope,
        tools=[
            ToolBinding(name="whois_lookup", tier=0, args={"target": "scanme.nmap.org"}),
            ToolBinding(name="dns_lookup", tier=0, args={"target": "scanme.nmap.org"}),
            ToolBinding(name="nmap_scan", tier=1, args={"target": "scanme.nmap.org", "ports": "top100"}),
        ],
        labels=["recon", "acme"],
    )
    service.create_card(
        "Web assessment: example.com",
        BOARD_ENGAGEMENT,
        description="T2 web assessment. Requires an approved human gate before it may run.",
        priority="critical",
        assignee="web-specialist",
        crew="vuln-assessment",
        scope=scope,
        tools=[
            ToolBinding(name="httpx_probe", tier=1, args={"target": "example.com"}),
            ToolBinding(name="nikto_scan", tier=2, args={"target": "example.com"}),
        ],
        requires_approval=True,
        labels=["webapp", "acme", "needs-gate"],
    )
    service.create_card(
        "Produce engagement report for ACME",
        BOARD_ENGAGEMENT,
        description="Roll up findings from the recon + web cards into the deliverable report.",
        priority="medium",
        assignee="report-writer",
        crew="reporting",
        labels=["reporting", "acme"],
        column=Column.BACKLOG,
    )

    # -- system board ----------------------------------------------------
    service.create_card(
        "Rotate /var/log and vacuum journal",
        BOARD_SYSTEM,
        description="Weekly hygiene. Safe, non-interactive.",
        priority="low",
        assignee="system-agent",
        tools=[ToolBinding(name="log_rotate", tier=0, dry_run=True)],
        labels=["hygiene"],
    )
    service.create_card(
        "Check disk pressure on / and /var",
        BOARD_SYSTEM,
        description="Alert if any filesystem exceeds 85% utilisation.",
        priority="medium",
        assignee="system-agent",
        labels=["disk", "monitor"],
    )

    # -- personal board --------------------------------------------------
    service.create_card(
        "Renew engagement authorization for Q4",
        BOARD_PERSONAL,
        description="Operator task. Blocks the Q4 engagement board until signed.",
        priority="high",
        assignee="operator",
        assignee_kind="human",
        labels=["admin"],
    )

    return {name: board.board_id for name, board in boards.items()}
