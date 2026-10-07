"""Offensive and analytical Kali tool frontends.

Blueprint ref: section 05 - the password-attack, wireless, exploitation, forensics,
reverse-engineering and reporting rows.

This file holds the tools with the largest blast radius, so the tier discipline
matters most here:

* **T0** - local or offline only (``strings``, ``readelf``, ``hashid``, an
  evidence hash, a report render). Nothing leaves the machine.
* **T1** - low-impact probing (firmware inspection, memory-image parsing).
* **T2** - intrusive: credential attempts, wireless survey, content discovery.
* **T3** - high impact: online brute force, deauthentication, exploit execution.
  Requires scope **and** approval **and** the sandbox.

A note on the wireless tools: they need a monitor-mode interface and a radio,
which a container does not have. They are declared honestly with dry-run
templates and will report ``available: false`` where the binary is absent rather
than pretending to have surveyed the air.
"""
from __future__ import annotations

from ..spec import ParamSpec, ToolSpec

OFFENSE_TOOLS: list[ToolSpec] = [
    # ------------------------------------------- Offline / passive (T0) -----
    ToolSpec(
        name="searchsploit_query",
        binary="searchsploit",
        category="vulnerability-analysis",
        tier=0,
        description="Search the local exploit-db copy for a product, version or CVE. Fully offline.",
        intent_examples=[
            "search exploit-db for this version",
            "are there public exploits for this software",
            "look up the cve in exploit-db",
        ],
        integration="cli_wrapper",
        params=[ParamSpec(name="query", type="string", required=True, description="Product, version or CVE")],
        target_params=["query"],
        dry_run_template="searchsploit --id {query}",
        live_template="searchsploit --id {query}",
        explain="Tells you whether known public exploits exist before anyone spends time developing one.",
        next_steps=["read the referenced exploit before considering it", "never run an exploit outside an approved T3 card"],
        timeout_s=30,
    ),
    ToolSpec(
        name="hashid_identify",
        binary="hashid",
        category="password-attacks",
        tier=0,
        description="Identify the algorithm behind a hash string. Purely local computation.",
        intent_examples=["what type of hash is this", "identify this hash", "is this an ntlm or md5 hash"],
        integration="cli_wrapper",
        params=[ParamSpec(name="hash", type="string", required=True, max_length=256, description="Hash string")],
        target_params=["hash"],
        dry_run_template="hashid -m {hash}",
        live_template="hashid -m {hash}",
        explain="Choosing the wrong algorithm wastes the entire cracking budget, so identify first.",
        next_steps=["select the matching cracking mode", "confirm the result against two independent identifiers"],
        timeout_s=15,
    ),
    ToolSpec(
        name="forensics_hash",
        binary="sha256sum",
        category="forensics",
        tier=0,
        description="Compute an integrity hash of a local file for evidence handling.",
        intent_examples=["hash this evidence file", "get the sha256 of the image", "verify evidence integrity"],
        integration="cli_wrapper",
        params=[ParamSpec(name="evidence", type="string", required=True, description="Local file path")],
        target_params=["evidence"],
        dry_run_template="sha256sum {evidence}",
        live_template="sha256sum {evidence}",
        explain="A hash taken at acquisition, and re-verified later, is what makes evidence defensible.",
        next_steps=["record the digest in the case log", "re-verify before and after every analysis step"],
        timeout_s=120,
    ),
    ToolSpec(
        name="re_strings",
        binary="strings",
        category="reverse-engineering",
        tier=0,
        description="Extract printable strings from a local binary. Reads a file, contacts nothing.",
        intent_examples=["pull strings from this binary", "what text is embedded in the file", "look for urls in the binary"],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="binary_path", type="string", required=True, description="Local file path"),
            ParamSpec(name="min_length", type="integer", default=6, description="Minimum string length"),
        ],
        target_params=["binary_path"],
        dry_run_template="strings -n {min_length} {binary_path}",
        live_template="strings -n {min_length} {binary_path}",
        explain="Embedded paths, URLs, keys and error messages are the fastest route into an unknown binary.",
        next_steps=["re_readelf for the binary structure", "r2_analyze for control flow around interesting strings"],
        timeout_s=120,
    ),
    ToolSpec(
        name="re_readelf",
        binary="readelf",
        category="reverse-engineering",
        tier=0,
        description="Dump ELF headers, sections and symbols for a local binary.",
        intent_examples=["read the elf headers", "what symbols does this binary have", "inspect the binary sections"],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="binary_path", type="string", required=True, description="Local file path"),
            ParamSpec(
                name="section",
                type="enum",
                default="headers",
                choices=["headers", "sections", "symbols", "notes"],
                description="Which view to dump",
            ),
        ],
        target_params=["binary_path"],
        dry_run_template="readelf -{section} {binary_path}",
        live_template="readelf -{section} {binary_path}",
        explain="Hardening flags, linked libraries and symbol tables frame everything else you do to the binary.",
        next_steps=["check for NX/PIE/canary before planning any dynamic work", "re_strings on any interesting symbol"],
        timeout_s=60,
    ),
    ToolSpec(
        name="pandoc_report",
        binary="pandoc",
        category="reporting",
        tier=0,
        description="Render an engagement report from markdown into HTML or PDF.",
        intent_examples=["render the report", "generate the engagement report pdf", "turn the notes into an html report"],
        integration="agent_skill",
        params=[
            ParamSpec(name="source", type="string", required=True, description="Markdown source path"),
            ParamSpec(
                name="format",
                type="enum",
                default="html",
                choices=["html", "pdf", "docx"],
                description="Output format",
            ),
        ],
        target_params=["source"],
        dry_run_template="pandoc {source} -o engagement-report.{format}",
        live_template="pandoc {source} -o engagement-report.{format}",
        explain="A consistent renderer keeps every report the same shape, which makes them reviewable at speed.",
        next_steps=["attach the rendered artifact to the card", "have the reviewer column sign off before it leaves the engagement"],
        timeout_s=120,
    ),
    # ------------------------------------- Forensics / RE, active (T1) -----
    ToolSpec(
        name="binwalk_extract",
        binary="binwalk",
        category="forensics",
        tier=1,
        description="Inspect a firmware image for embedded filesystems and compressed blobs.",
        intent_examples=["analyse the firmware image", "what is inside this firmware", "carve the firmware"],
        integration="cli_wrapper",
        params=[ParamSpec(name="image", type="string", required=True, description="Firmware image path")],
        target_params=["image"],
        requires_scope=True,
        dry_run_template="binwalk {image}",
        live_template="binwalk {image}",
        explain="The signature list tells you what the vendor bundled - often including a busybox shell and hardcoded keys.",
        next_steps=["extract and re_hash the rootfs", "re_strings the extracted binaries for credentials"],
        timeout_s=300,
    ),
    ToolSpec(
        name="volatility_pslist",
        binary="vol.py",
        category="forensics",
        tier=1,
        description="List processes from a memory image using Volatility 3.",
        intent_examples=["list processes in the memory dump", "what was running in this capture", "volatility pslist the image"],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="image", type="string", required=True, description="Memory image path"),
        ],
        target_params=["image"],
        requires_scope=True,
        dry_run_template="vol.py -f {image} windows.pslist.PsList",
        live_template="vol.py -f {image} windows.pslist.PsList",
        explain="Process trees expose injected code, unusual parents and processes that had already exited.",
        next_steps=["pivot to network and handle views for the suspicious PIDs", "hash every artifact you extract"],
        timeout_s=600,
    ),
    ToolSpec(
        name="r2_analyze",
        binary="radare2",
        category="reverse-engineering",
        tier=1,
        description="Static analysis pass over a local binary: functions, calls and cross-references.",
        intent_examples=["analyse the binary with radare2", "list the functions in this file", "static analysis of the binary"],
        integration="agent_skill",
        params=[
            ParamSpec(name="binary_path", type="string", required=True, description="Local file path"),
            ParamSpec(
                name="analysis_level",
                type="enum",
                default="standard",
                choices=["quick", "standard", "deep"],
                description="Analysis depth",
            ),
        ],
        target_params=["binary_path"],
        requires_scope=True,
        dry_run_template="radare2 -q -c 'aa; afl' {binary_path}",
        live_template="radare2 -q -c 'aa; afl' {binary_path}",
        explain="The function list plus xrefs turns an opaque binary into a map you can navigate.",
        next_steps=["name the interesting functions and save the project", "re_strings around any flag-handling function"],
        timeout_s=300,
    ),
    # --------------------------------------------- Intrusive (T2) ----------
    ToolSpec(
        name="wifi_survey",
        binary="airodump-ng",
        category="wireless",
        tier=2,
        description="Passive 802.11 survey on a single channel, recording beacon and client data.",
        intent_examples=["survey the wireless networks", "what aps are in range", "capture beacons on the target bssid"],
        integration="gui_panel",
        params=[
            ParamSpec(name="interface", type="string", required=True, description="Monitor-mode interface"),
            ParamSpec(name="bssid", type="string", required=True, description="Target BSSID (MAC)"),
            ParamSpec(
                name="band",
                type="enum",
                default="bg",
                choices=["a", "b", "g", "bg", "abg"],
                description="Radio band",
            ),
        ],
        target_params=["bssid"],
        dry_run_template="airodump-ng --band {band} --bssid {bssid} --write survey {interface}",
        live_template="airodump-ng --band {band} --bssid {bssid} --write survey {interface}",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "Even passive capture is regulated in most jurisdictions and records other people's "
            "traffic, so the BSSID must be inside an authorized scope with a signed reference."
        ),
        next_steps=["hand the capture to a T3 cracking card if authorised", "delete captures once the engagement closes"],
        timeout_s=300,
    ),
    # ------------------------------------------- High impact (T3) ----------
    ToolSpec(
        name="hydra_bruteforce",
        binary="hydra",
        category="password-attacks",
        tier=3,
        description="Online credential attack against a network service. High impact: fails accounts, trips lockouts.",
        intent_examples=["brute force the ssh login", "try credentials against the service", "hydra the target"],
        integration="agent_skill",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host or IP"),
            ParamSpec(
                name="service",
                type="enum",
                default="ssh",
                choices=["ssh", "ftp", "smb", "rdp", "http-post-form"],
                description="Service to attack",
            ),
            ParamSpec(name="username", type="string", default="root", description="Username or user list"),
            ParamSpec(name="wordlist", type="string", default="/usr/share/wordlists/rockyou.txt", description="Password list"),
            ParamSpec(name="threads", type="integer", default=4, description="Parallel tasks"),
        ],
        dry_run_template="hydra -l {username} -P {wordlist} -t {threads} {target} {service}",
        live_template="hydra -l {username} -P {wordlist} -t {threads} -f {target} {service}",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain=(
            "Locked-out service accounts and fail2ban bans are the immediate consequence. Four threads "
            "is already enough to lock an account; never raise it without a written justification."
        ),
        next_steps=["stop at the first valid credential", "brief the operator before using any credential found"],
        timeout_s=900,
    ),
    ToolSpec(
        name="wifi_deauth",
        binary="aireplay-ng",
        category="wireless",
        tier=3,
        description="Send deauthentication frames to force a client to reassociate. Disrupts live users.",
        intent_examples=["deauth the client", "force a handshake capture", "disconnect the station from the ap"],
        integration="agent_skill",
        params=[
            ParamSpec(name="interface", type="string", required=True, description="Monitor-mode interface"),
            ParamSpec(name="bssid", type="string", required=True, description="Access point BSSID"),
            ParamSpec(name="client", type="string", default="FF:FF:FF:FF:FF:FF", description="Client MAC or broadcast"),
            ParamSpec(name="count", type="integer", default=5, description="Number of bursts"),
        ],
        target_params=["bssid"],
        # ``client`` defaults to the broadcast MAC. It is address-shaped but is
        # not an independent target: it is a station *inside* the audited AP's
        # own network, and the AP itself is already scope-checked above. It is
        # listed here rather than left implicit so the authoring lint sees an
        # explicit, reviewable decision instead of a blind spot.
        scope_skip_params=["client"],
        dry_run_template="aireplay-ng --deauth {count} -a {bssid} -c {client} {interface}",
        live_template="aireplay-ng --deauth {count} -a {bssid} -c {client} --ignore-negative-one {interface}",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain=(
            "This actively disconnects real users from a live network. It is disruptive, obvious in "
            "logs, and illegal outside an authorized engagement."
        ),
        next_steps=["never broadcast-deauth in production", "record the exact frame count in the report"],
        timeout_s=120,
    ),
    ToolSpec(
        name="metasploit_run",
        binary="msfconsole",
        category="exploitation",
        tier=3,
        description="Execute a Metasploit module against a target. Runs a real exploit payload.",
        intent_examples=["run the metasploit module", "exploit the target with msf", "launch the exploit against the host"],
        integration="agent_skill",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Target host or IP"),
            ParamSpec(name="module", type="string", required=True, max_length=128, description="Module path"),
            ParamSpec(name="payload", type="string", default="cmd/unix/generic", description="Payload to use"),
        ],
        dry_run_template="msfconsole -q -x 'use {module}; set RHOSTS {target}; check'",
        live_template="msfconsole -q -x 'use {module}; set RHOSTS {target}; set PAYLOAD {payload}; run'",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain=(
            "A successful exploit can change the target's state, so the module must have been reviewed "
            "and the payload chosen deliberately. Dry-run uses `check`, which is non-destructive."
        ),
        next_steps=["capture the session id and stop", "never pivot to other hosts without a fresh scope and gate"],
        timeout_s=600,
    ),
]
