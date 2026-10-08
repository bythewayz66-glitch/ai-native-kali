"""Built-in Kali tool frontends.

Blueprint ref: section 05. Each spec pairs a real Kali binary with:

* the natural-language intents that should route to it,
* a **dry-run template** (always safe - describes, executes nothing),
* a **live template** (the real command),
* guardrail flags (scope / approval / sandbox),
* result explanation + suggested next steps for the operator.

Tier spread is deliberate: T0 (whois, dig), T1 (nmap, httpx), T2 (nikto),
T3 (sqlmap) so every guardrail path has at least one real tool behind it.
"""
from __future__ import annotations

from ..registry import ToolRegistry
from ..spec import ParamSpec, ToolSpec
from .cloud import CLOUD_TOOLS
from .network import NETWORK_TOOLS
from .offense import OFFENSE_TOOLS
from .social_engineering import SOCIAL_ENGINEERING_TOOLS
from .system_audit import SYSTEM_AUDIT_TOOLS

#: The original reference set of frontends (one per guardrail tier).
BUILTIN_TOOLS: list[ToolSpec] = [
    # ---------------------------------------------------------------- T0 ---
    ToolSpec(
        name="whois_lookup",
        binary="whois",
        category="information-gathering",
        tier=0,
        description="Registration and ownership data for a domain (passive, no contact with the host).",
        intent_examples=[
            "who owns this domain",
            "whois lookup for the target",
            "registration details for example.com",
        ],
        params=[ParamSpec(name="target", type="string", required=True, description="Domain name")],
        dry_run_template="whois {target}",
        live_template="whois {target}",
        explain="Returns registrar, creation/expiry dates and nameservers. Useful for attribution and expiry-based scope decisions.",
        next_steps=["dns_lookup on the same domain", "nmap_scan if the host is in scope"],
        timeout_s=30,
    ),
    ToolSpec(
        name="dns_lookup",
        binary="dig",
        category="information-gathering",
        tier=0,
        description="Resolve DNS records for a name (passive lookups against a resolver).",
        intent_examples=["resolve the domain", "what IPs does this host have", "dns records for the target"],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Hostname to resolve"),
            ParamSpec(
                name="record_type",
                type="enum",
                default="A",
                choices=["A", "AAAA", "MX", "TXT", "NS", "CNAME"],
                description="DNS record type",
            ),
        ],
        dry_run_template="dig +short {record_type} {target}",
        live_template="dig +short {record_type} {target}",
        explain="Resolved addresses tell you where the host actually lives; NS/MX reveal infrastructure providers.",
        next_steps=["whois_lookup for registration context", "nmap_scan the resolved address"],
        timeout_s=20,
    ),
    # ---------------------------------------------------------------- T1 ---
    ToolSpec(
        name="nmap_scan",
        binary="nmap",
        category="information-gathering",
        tier=1,
        description="TCP port scan to enumerate reachable services. Active but non-destructive.",
        intent_examples=["scan the host for open ports", "what services are running", "port scan this subnet"],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host, IP or CIDR"),
            ParamSpec(name="ports", type="string", default="top100", max_length=128, description="Port spec"),
        ],
        dry_run_template="nmap -sT --top-ports 100 {target}",
        live_template="nmap -sT --top-ports 100 {target}",
        requires_scope=True,
        requires_approval=False,
        explain="Open ports map to the attack surface; compare against the expected-service baseline before escalating.",
        next_steps=["httpx_probe on any web port found", "nikto_scan if an HTTP service is exposed (T2, needs a gate)"],
        timeout_s=120,
    ),
    ToolSpec(
        name="httpx_probe",
        binary="httpx",
        category="web-application",
        tier=1,
        description="HTTP probe: status, title, tech stack and headers for a web endpoint.",
        intent_examples=["probe the web service", "fingerprint this website", "what is this web app running"],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host or URL"),
            ParamSpec(name="status_code", type="boolean", default=True, description="Show status codes"),
        ],
        dry_run_template="httpx -u https://{target} -status-code -title -tech-detect",
        live_template="httpx -u https://{target} -status-code -title -tech-detect",
        requires_scope=True,
        explain="Technology detection narrows the CVE set worth checking and informs which web tools to run next.",
        next_steps=["nikto_scan for known web-server issues (T2, needs a gate)", "check the tech stack versions against known CVEs"],
        timeout_s=45,
    ),
    # ---------------------------------------------------------------- T2 ---
    ToolSpec(
        name="nikto_scan",
        binary="nikto",
        category="web-application",
        tier=2,
        description="Web server vulnerability scan. Intrusive: sends many probes and can be logged or blocked.",
        intent_examples=["scan the web server for vulnerabilities", "check this site for known issues", "nikto the target"],
        integration="gui_panel",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host or URL"),
            ParamSpec(name="tuning", type="string", default="1", max_length=16, description="Nikto tuning options"),
        ],
        dry_run_template="nikto -h {target} -Tuning {tuning} -maxtime 60s -nointeractive",
        live_template="nikto -h {target} -Tuning {tuning} -maxtime 60s -nointeractive",
        requires_scope=True,
        requires_approval=True,
        explain="Findings are server-configuration issues - missing security headers, exposed admin paths, outdated components.",
        next_steps=["triage findings against the report template", "escalate exploitable items as separate T3 cards"],
        timeout_s=180,
    ),
    # ---------------------------------------------------------------- T3 ---
    ToolSpec(
        name="sqlmap_test",
        binary="sqlmap",
        category="exploitation",
        tier=3,
        description="SQL injection detection and exploitation. High impact: can alter data, so it runs sandboxed.",
        intent_examples=["test this parameter for sql injection", "can we exploit the database", "sqlmap the url"],
        integration="agent_skill",
        params=[
            ParamSpec(name="target", type="string", required=True, max_length=512, description="Target URL"),
            ParamSpec(name="level", type="integer", default=1, description="Test depth 1-5"),
            ParamSpec(name="risk", type="integer", default=1, description="Risk of payloads 1-3"),
            ParamSpec(name="batch", type="boolean", default=True, description="Never prompt interactively"),
        ],
        dry_run_template="sqlmap -u {target} --level={level} --risk={risk} --batch --smart",
        live_template="sqlmap -u {target} --level={level} --risk={risk} --batch --smart --flush-session",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain="A confirmed injection is a critical finding. Record the exact payload and only ever test read-only extraction against production data.",
        next_steps=["stop and brief the operator before any data extraction", "attach the request/response pair to the engagement report"],
        timeout_s=300,
    ),
]


#: System tools used by the system-maintenance board (not Kali binaries).
SYSTEM_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="log_rotate",
        binary="logrotate",
        category="system",
        tier=0,
        description="Rotate system logs and vacuum the journal. Dry-run by default.",
        intent_examples=["rotate the logs", "clean up old journals"],
        params=[
            ParamSpec(name="config", type="string", default="/etc/logrotate.conf", description="Config path"),
        ],
        dry_run_template="logrotate --debug {config}",
        live_template="logrotate {config}",
        # Phase 17: the first tool to state its local footprint. ``logrotate`` is
        # T0 - it contacts nothing - but its live form rewrites files, which tier
        # alone never said. Declaring it is what makes the footprint floor real:
        # before this, the flag that gates the sandbox and the audit row that
        # describes the call both had nothing to read.
        effects=["fs.write"],
        explain="Debug mode prints what would be rotated without touching a file.",
        next_steps=["verify free space improved", "close the hygiene card"],
        timeout_s=60,
    ),
    ToolSpec(
        name="log_digest",
        binary="journalctl",
        category="system",
        tier=0,
        description="Summarise recent service and tool-call logs into a short digest for the operator.",
        intent_examples=[
            "summarise the last 24h of traces",
            "digest the logs",
            "what happened on the system today",
        ],
        params=[
            ParamSpec(
                name="window",
                type="enum",
                default="24h",
                choices=["1h", "24h", "7d"],
                description="Time window to summarise",
            ),
        ],
        # T0 and purely local: it reads the journal on the host it runs on and
        # contacts nothing, so it declares no scope. The board-level orchestrator
        # role binds this tool, so it has to be a real registered spec - the
        # boundary audit was flagging the name as dangling because it never was.
        dry_run_template="journalctl --since '-24h' --no-pager | tail -200",
        live_template="journalctl --since '-24h' --no-pager | tail -200",
        explain=(
            "A digest is a read of what already happened - it changes nothing, which is why "
            "it is the one tool the board-level orchestrator role is allowed to call."
        ),
        next_steps=["raise a card for anything the digest flags", "attach the digest to the board review"],
        timeout_s=60,
    ),
]


#: Every frontend the layer ships, in registration order.
ALL_TOOLS: list[ToolSpec] = (
    BUILTIN_TOOLS
    + SYSTEM_TOOLS
    + NETWORK_TOOLS
    + OFFENSE_TOOLS
    + CLOUD_TOOLS
    + SYSTEM_AUDIT_TOOLS
    + SOCIAL_ENGINEERING_TOOLS
)


def register_builtin(registry: ToolRegistry) -> ToolRegistry:
    """Register every built-in frontend. Idempotent per registry instance."""
    for spec in ALL_TOOLS:
        if registry.get(spec.name) is None:
            registry.register(spec)
    return registry
