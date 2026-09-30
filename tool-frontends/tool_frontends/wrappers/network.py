"""Network-facing Kali tool frontends: SMB, DNS, TLS, web.

Blueprint ref: section 05 - the SMB, DNS, TLS and web-application rows of the
Kali tool table. Split out from the original ``wrappers/__init__.py`` so the
first seven frontends stay readable as the reference set and each new category
gets a home.

Every spec here follows the same contract as the originals:

* a **dry-run template** that is always safe and executes nothing,
* a **live template** reachable only through the guardrail engine,
* an explicit tier, and
* the guardrail flags that tier requires (T2 needs scope + approval).

Note the deliberate jump in authority: ``nmap_scan`` (T1) enumerates ports, but
``gobuster_dirs`` (T2) hammers a web server with thousands of requests. Those are
not the same act and they do not share a tier.
"""
from __future__ import annotations

from ..spec import ParamSpec, ToolSpec

NETWORK_TOOLS: list[ToolSpec] = [
    # ------------------------------------------------------- SMB (T1/T2) ---
    ToolSpec(
        name="smb_enum",
        binary="enum4linux-ng",
        category="information-gathering",
        tier=1,
        description="Enumerate SMB shares, users, groups and OS details from a file server.",
        intent_examples=[
            "enumerate the smb shares",
            "list users on the file server",
            "what shares are exposed on this host",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host or IP running SMB"),
            ParamSpec(
                name="depth",
                type="enum",
                default="shares",
                choices=["shares", "users", "all"],
                description="How far to enumerate",
            ),
        ],
        dry_run_template="enum4linux-ng -A {target}",
        live_template="enum4linux-ng -A {target}",
        requires_scope=True,
        explain=(
            "Share names hint at what data sits on the host; user lists become username "
            "candidates for later (gated) credential work. Read-only: nothing is written to the target."
        ),
        next_steps=[
            "smb_signing_check to see whether relay attacks are viable",
            "record share names as findings; do not mount or alter anything",
        ],
        timeout_s=120,
    ),
    ToolSpec(
        name="smb_signing_check",
        binary="nmap",
        category="vulnerability-analysis",
        tier=1,
        description="Check whether SMB signing is required, optional or disabled on a host.",
        intent_examples=[
            "is smb signing enforced",
            "check for smb relay exposure",
            "smb signing mode on the target",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host or IP running SMB"),
            ParamSpec(name="port", type="integer", default=445, description="SMB port"),
        ],
        dry_run_template="nmap -p {port} --script smb2-security-mode {target}",
        live_template="nmap -p {port} --script smb2-security-mode {target}",
        requires_scope=True,
        explain=(
            "'Message signing enabled but not required' means an attacker positioned on the "
            "network could relay authentication. It is a finding, not an exploit."
        ),
        next_steps=["add to the report as a hardening item", "do not attempt relay: it is its own T3 card"],
        timeout_s=120,
    ),
    # --------------------------------------------------------- DNS (T1) ---
    ToolSpec(
        name="dns_zone_transfer",
        binary="dig",
        category="information-gathering",
        tier=1,
        description="Attempt a DNS zone transfer (AXFR) against an authoritative nameserver.",
        intent_examples=["try a zone transfer", "axfr the domain", "can we list all dns records"],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Authoritative nameserver or domain"),
            ParamSpec(name="nameserver", type="string", required=True, description="Nameserver to query"),
        ],
        # BOTH parameters reach the network: ``target`` is the zone being asked
        # for and ``nameserver`` is the host the query is actually sent to. Only
        # the second was declared before, and since scope enforcement compared
        # *declared* parameters only, the AXFR went to whatever host the caller
        # named. Declared here, and additionally guarded by value in the
        # guardrail engine (tool_frontends/targets.py).
        target_params=["target", "nameserver"],
        dry_run_template="dig AXFR @{nameserver} {target}",
        live_template="dig AXFR @{nameserver} {target}",
        requires_scope=True,
        explain=(
            "A successful AXFR hands you the entire zone - a high-value finding caused by "
            "misconfiguration. Failure is the normal, secure outcome."
        ),
        next_steps=["dns_enum for records the transfer did not reveal", "report a successful transfer as high severity"],
        timeout_s=45,
    ),
    ToolSpec(
        name="dns_enum",
        binary="dnsrecon",
        category="information-gathering",
        tier=1,
        description="Enumerate DNS records, subdomains and nameservers for a domain.",
        intent_examples=["enumerate dns records", "find subdomains", "dnsrecon the domain"],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Domain to enumerate"),
            ParamSpec(
                name="record_type",
                type="enum",
                default="std",
                choices=["std", "srv", "axfr", "zonewalk"],
                description="Enumeration mode",
            ),
        ],
        dry_run_template="dnsrecon -d {target} -t {record_type}",
        live_template="dnsrecon -d {target} -t {record_type}",
        requires_scope=True,
        explain="Subdomains usually expand the real attack surface well beyond the apex domain.",
        next_steps=["dns_lookup on each new name", "check whether new hosts fall inside the scope before probing them"],
        timeout_s=120,
    ),
    # --------------------------------------------------------- TLS (T1) ---
    ToolSpec(
        name="tls_probe",
        binary="openssl",
        category="information-gathering",
        tier=1,
        description="Read a TLS certificate chain and negotiated protocol for an endpoint.",
        intent_examples=["check the tls certificate", "what cert is this server presenting", "tls handshake details"],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Hostname"),
            ParamSpec(name="port", type="integer", default=443, description="TLS port"),
        ],
        dry_run_template="openssl s_client -connect {target}:{port} -servername {target}",
        live_template="openssl s_client -connect {target}:{port} -servername {target}",
        requires_scope=True,
        explain=(
            "Certificate subjects and SANs reveal sibling hostnames; expiry and issuer reveal who "
            "operates the service."
        ),
        next_steps=["sslscan_scan for cipher and protocol weaknesses", "add newly discovered SAN hosts as scope check candidates"],
        timeout_s=30,
    ),
    ToolSpec(
        name="sslscan_scan",
        binary="sslscan",
        category="vulnerability-analysis",
        tier=1,
        description="Enumerate supported TLS protocols, ciphers and known weaknesses.",
        intent_examples=["scan the tls configuration", "is sslv3 still enabled", "check cipher strength"],
        integration="cli_wrapper",
        params=[ParamSpec(name="target", type="string", required=True, description="Hostname or host:port")],
        dry_run_template="sslscan --no-colour {target}",
        live_template="sslscan --no-colour {target}",
        requires_scope=True,
        explain="Legacy protocols and export-grade ciphers are downgrade-attack prerequisites.",
        next_steps=["map findings to the TLS hardening baseline", "close the card once the cipher list is recorded"],
        timeout_s=90,
    ),
    # --------------------------------------------------------- Web (T1/T2) -
    ToolSpec(
        name="whatweb_fingerprint",
        binary="whatweb",
        category="web-application",
        tier=1,
        description="Fingerprint a web application's CMS, framework, server and plugins.",
        intent_examples=["what cms is this site running", "fingerprint the web app", "whatweb the target"],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host or URL"),
            ParamSpec(
                name="aggression",
                type="enum",
                default="passive",
                choices=["passive", "stealthy", "aggressive"],
                description="Detection aggression",
            ),
        ],
        dry_run_template="whatweb --aggression 1 https://{target}",
        live_template="whatweb --aggression 1 https://{target}",
        requires_scope=True,
        explain="A known CMS version narrows the CVE set worth testing; the version string is the finding.",
        next_steps=["wpscan_scan if WordPress was detected (T2, needs a gate)", "record the exact version for the report"],
        timeout_s=60,
    ),
    ToolSpec(
        name="gobuster_dirs",
        binary="gobuster",
        category="web-application",
        tier=2,
        description="Brute-force web content: directories, files and endpoints from a wordlist.",
        intent_examples=["find hidden directories", "brute force the web paths", "enumerate endpoints on the site"],
        integration="gui_panel",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host or URL"),
            ParamSpec(name="wordlist", type="string", default="/usr/share/wordlists/dirb/common.txt", description="Wordlist path"),
            ParamSpec(name="threads", type="integer", default=10, description="Concurrency"),
        ],
        dry_run_template="gobuster dir -u https://{target} -w {wordlist} -t {threads} --no-error",
        live_template="gobuster dir -u https://{target} -w {wordlist} -t {threads} --no-error",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "Thousands of requests will be logged and may trip a WAF or rate limiter. Exposed admin "
            "paths and backups are the usual high-value hits."
        ),
        next_steps=["inspect any discovered admin or backup path by hand", "stop early if the target starts returning 429"],
        timeout_s=300,
    ),
    ToolSpec(
        name="wpscan_scan",
        binary="wpscan",
        category="web-application",
        tier=2,
        description="Scan a WordPress installation for vulnerable plugins, themes and users.",
        intent_examples=["scan the wordpress site", "are the wordpress plugins vulnerable", "find wp users"],
        integration="gui_panel",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host or URL"),
            ParamSpec(
                name="enumerate",
                type="enum",
                default="vp",
                choices=["vp", "u", "ap", "at"],
                description="What to enumerate",
            ),
        ],
        dry_run_template="wpscan --url https://{target} --enumerate {enumerate} --no-banner --disable-tls-checks",
        live_template="wpscan --url https://{target} --enumerate {enumerate} --no-banner --disable-tls-checks",
        requires_scope=True,
        requires_approval=True,
        explain="Plugin versions map directly to public CVEs, so findings are usually immediately actionable.",
        next_steps=["cross-check each plugin version against exploit-db", "escalate exploitable plugins as separate T3 cards"],
        timeout_s=300,
    ),
    # ------------------------------------------ Vulnerability analysis (T2) -
    ToolSpec(
        name="nuclei_scan",
        binary="nuclei",
        category="vulnerability-analysis",
        tier=2,
        description="Template-driven vulnerability scanning against a target.",
        intent_examples=["run nuclei against the target", "template scan for known vulns", "check the site for cves"],
        integration="agent_skill",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host or URL"),
            ParamSpec(
                name="severity",
                type="enum",
                default="medium,high,critical",
                choices=["info", "low", "medium", "high", "critical", "medium,high,critical"],
                description="Severity filter",
            ),
        ],
        dry_run_template="nuclei -u https://{target} -severity {severity} -no-color",
        live_template="nuclei -u https://{target} -severity {severity} -no-color",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "Nuclei templates range from harmless banner checks to active exploitation attempts, so "
            "treat every hit as an unverified lead until a human confirms it."
        ),
        next_steps=["manually verify each hit before reporting it", "do not auto-escalate to exploitation"],
        timeout_s=600,
    ),
]
