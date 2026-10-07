"""Local system-audit tool frontends.

Workstream B, Phase 3. Blueprint ref: section 05 - the system row, and the
system-maintenance board in section 03.

Why these are T0/T1, and which ones declare ``requires_scope``
--------------------------------------------------------------
The T0 tools in this module inspect **the machine they run on** and nothing
else: ``lynis``, ``find``, ``lsblk``, ``bootctl`` read local state. None reaches
a host, so none declares ``requires_scope`` - requiring an authorization ticket
to run ``df`` would be over-correction, and the address classifier is
correspondingly left alone for them (see ``tool_frontends/targets.py``).

The T1 tools are different in kind, and an audit of this module found the
difference was not being expressed. Six of them - ``firewall_audit``,
``audit_policy_check``, ``patch_level_check``, ``service_exposure_check``,
``kernel_hardening_check`` and ``log_forensics`` - name a host in their
``target`` parameter while reporting on a *local* collector. That target value is
what the operator reviews (it is the host the ruleset/patch-state/journal belongs
to), and it is exactly the value that must be covered by the attached scope when
the engine rules on the run.

They previously declared ``requires_scope=False``, which made their ``target``
decorative: a live run passed an out-of-scope host and the engine never compared
it. The Tier-1 fail-closed floor (``scope_is_required``) requires a scope for a
*live* run, but only a declared ``requires_scope`` switches on the *coverage*
check - so with the flag off, ``live=True`` plus an attached scope for
``example.com`` and ``target=evil.net`` returned ``allowed=True`` with no
reasons. That is the escape the audit caught; the flag is now set on those six,
and ``tests/test_system_audit_scope.py`` pins it.

``integrity_baseline`` is the reverse case and was also wrong: it declared
``target_params=["config"]`` and defaulted that to ``/etc/aide/aide.conf``. A file
path is not a network target, so ``target_value()`` returned the path and the
scope checks compared a filesystem path against a host scope. It now declares a
network scope (AIDE needs the host named) with no address-shaped parameter; the
config path is fixed in the template because there is only one sane value on a
host and making it an argument only created a fake target.

The tier still means something: T1 tools read security-relevant configuration and
anything that *changes* system state is deliberately absent from this module -
``log_rotate`` (T0, dry-run by default) remains the only mutating system tool and
it lives in ``wrappers/__init__.py`` next to the original reference set.
"""
from __future__ import annotations

from ..spec import ParamSpec, ToolSpec

SYSTEM_AUDIT_TOOLS: list[ToolSpec] = [
    # =====================================================================
    # Baseline and hardening posture (T0 - read local state only)
    # =====================================================================
    ToolSpec(
        name="system_baseline_audit",
        binary="lynis",
        category="system",
        tier=0,
        description="Run a local hardening audit and summarise the findings by severity.",
        intent_examples=[
            "audit the system hardening",
            "run a local security baseline",
            "how hardened is this host",
        ],
        params=[
            ParamSpec(name="profile", type="string", default="/etc/lynis/default.prf", description="Audit profile"),
            ParamSpec(
                name="level",
                type="enum",
                default="summary",
                choices=["summary", "detail"],
                description="How much output to keep",
            ),
        ],
        target_params=["profile"],
        scope_skip_params=["level"],
        dry_run_template="lynis audit system --profile {profile} --quick",
        live_template="lynis audit system --profile {profile} --quick",
        explain=(
            "Lynis scores the host across hundreds of checks. The value is not the score but the "
            "handful of warnings you can actually fix today."
        ),
        next_steps=["triage warnings into cards on the system-maintenance board", "re-run after fixes to confirm the delta"],
        timeout_s=600,
    ),
    ToolSpec(
        name="suid_sgid_audit",
        binary="find",
        category="system",
        tier=0,
        description="Locate setuid and setgid binaries, flagging unusual ones.",
        intent_examples=["find suid binaries", "any setuid binaries on this host", "check for privilege escalation binaries"],
        params=[
            ParamSpec(name="root_path", type="string", default="/", description="Filesystem root to search"),
        ],
        target_params=["root_path"],
        dry_run_template="find {root_path} -xdev -type f -perm -4000 -o -perm -2000",
        live_template="find {root_path} -xdev -type f -perm -4000 -o -perm -2000",
        explain=(
            "A setuid binary is only a risk if it is also unusual; the point of the audit is to "
            "surface the two nobody recognises among the forty that package managers installed."
        ),
        next_steps=["verify each unusual binary against its package", "check versions of any non-standard setuid tool"],
        timeout_s=300,
    ),
    ToolSpec(
        name="file_permission_audit",
        binary="find",
        category="system",
        tier=0,
        description="Find world-writable files and directories outside expected locations.",
        intent_examples=["find world writable files", "are there insecure file permissions", "check for writable system paths"],
        params=[
            ParamSpec(name="root_path", type="string", default="/etc", description="Tree to audit"),
        ],
        target_params=["root_path"],
        dry_run_template="find {root_path} -xdev -type f -perm -0002 -ls",
        live_template="find {root_path} -xdev -type f -perm -0002 -ls",
        explain=(
            "A world-writable file in /etc is a local privilege-escalation primitive with no exploit "
            "required - anything can rewrite it."
        ),
        next_steps=["tighten permissions on each hit", "check whether the writable file is sourced by a service"],
        timeout_s=300,
    ),
    ToolSpec(
        name="user_account_audit",
        binary="awk",
        category="system",
        tier=0,
        description="Audit local accounts for empty passwords, uid 0 duplicates and stale logins.",
        intent_examples=["audit local user accounts", "any accounts with empty passwords", "check for duplicate root accounts"],
        params=[
            ParamSpec(name="passwd_file", type="string", default="/etc/passwd", description="Passwd file to read"),
        ],
        target_params=["passwd_file"],
        # Braces are doubled because ``ToolSpec.render`` uses ``str.format``:
        # a single ``{print}`` is read as a substitution field named ``print`` and
        # the template fails to render. This was caught by the universal
        # "every dry-run template renders" test, not by anything specific to awk.
        dry_run_template="awk -F: '($3==0){{{{print}}}}' {passwd_file}",
        live_template="awk -F: '($3==0){{{{print}}}}' {passwd_file}",
        explain=(
            "A second account with uid 0 is a backdoor regardless of intent, and it survives the "
            "password change everyone thinks fixed the problem."
        ),
        next_steps=["investigate any unexpected uid 0 account immediately", "confirm password policy on service accounts"],
        timeout_s=60,
    ),
    ToolSpec(
        name="cron_audit",
        binary="bash",
        category="system",
        tier=0,
        description="List scheduled jobs across cron and systemd timers, flagging writable scripts.",
        intent_examples=["audit the cron jobs", "what scheduled tasks exist", "check systemd timers"],
        params=[
            ParamSpec(name="scope", type="string", default="/etc", description="Directory tree to inspect"),
        ],
        target_params=["scope"],
        dry_run_template="bash -c 'ls -la {scope}/cron* /var/spool/cron/* 2>/dev/null; systemctl list-timers --all'",
        live_template="bash -c 'ls -la {scope}/cron* /var/spool/cron/* 2>/dev/null; systemctl list-timers --all'",
        explain=(
            "A scheduled job running a script that a non-root user can edit is persistence *and* "
            "privilege escalation in one entry."
        ),
        next_steps=["resolve every job whose script is group- or world-writable", "remove jobs with no owning package"],
        timeout_s=120,
    ),
    ToolSpec(
        name="disk_encryption_check",
        binary="lsblk",
        category="system",
        tier=0,
        description="Check which block devices are encrypted and whether any plaintext volumes remain.",
        intent_examples=["is the disk encrypted", "check for unencrypted volumes", "verify full disk encryption"],
        params=[
            ParamSpec(name="format", type="enum", default="tree", choices=["tree", "json", "list"], description="Output format"),
        ],
        scope_skip_params=["format"],
        dry_run_template="lsblk -f",
        live_template="lsblk -f",
        explain=(
            "Encryption is assessed per block device, and the finding is usually one partition "
            "somebody added later without it."
        ),
        next_steps=["raise a card for any plaintext data volume", "confirm the boot volume's LUKS header is intact"],
        timeout_s=60,
    ),
    ToolSpec(
        name="boot_integrity_check",
        binary="bootctl",
        category="system",
        tier=0,
        description="Report Secure Boot state, boot loader integrity and measured-boot availability.",
        intent_examples=["check secure boot status", "is the bootloader signed", "verify boot integrity"],
        params=[],
        dry_run_template="bootctl status",
        live_template="bootctl status",
        explain=(
            "Secure Boot off means an attacker with brief physical access can install a persistent "
            "bootkit; on a laptop that leaves the building, that matters."
        ),
        next_steps=["enable Secure Boot where the hardware supports it", "compare measured-boot PCRs against known-good"],
        timeout_s=60,
    ),
    # =====================================================================
    # Security-relevant configuration (T1 - reads policy, changes nothing)
    # =====================================================================
    ToolSpec(
        name="firewall_audit",
        binary="nft",
        category="system",
        tier=1,
        description="Dump and review the active firewall ruleset for permissive or missing policies.",
        intent_examples=["audit the firewall rules", "show me the iptables rules", "is the default policy deny"],
        params=[
            ParamSpec(
                name="backend",
                type="enum",
                default="nft",
                choices=["nft", "iptables", "ufw"],
                description="Firewall backend in use",
            ),
            # ``target`` is declared even though this tool inspects the *local*
            # host: the guardrail engine refuses any T1+ tool that supplies no
            # target, on the principle that a tool must state what it acts on.
            # The value is the host the ruleset belongs to.
            ParamSpec(name="target", type="string", default="localhost", description="Host whose ruleset is reviewed"),
        ],
        target_params=["target"],
        scope_skip_params=["backend"],
        requires_scope=True,
        dry_run_template="nft list ruleset",
        live_template="nft list ruleset",
        explain=(
            "A default-accept policy with per-service allow rules is the common finding: it looks "
            "configured and permits everything that was not explicitly blocked."
        ),
        next_steps=["change the default policy to drop on input", "verify no rule permits 0.0.0.0/0 on a management port"],
        timeout_s=90,
    ),
    ToolSpec(
        name="audit_policy_check",
        binary="auditctl",
        category="system",
        tier=1,
        description="List active auditd rules and report gaps in the coverage baseline.",
        intent_examples=["check the auditd rules", "is auditing enabled", "what is being audited on this host"],
        params=[
            # Declared to satisfy the engine's "state what you act on" rule for T1+.
            ParamSpec(name="target", type="string", default="localhost", description="Host whose audit policy is reviewed"),
        ],
        target_params=["target"],
        requires_scope=True,
        dry_run_template="auditctl -l",
        live_template="auditctl -l",
        explain=(
            "Without audit rules there is no record of who ran what, which turns any later "
            "investigation into guesswork."
        ),
        next_steps=["add rules for privilege escalation and credential file access", "confirm the audit log is forwarded off-host"],
        timeout_s=60,
    ),
    ToolSpec(
        name="patch_level_check",
        binary="apt",
        category="system",
        tier=1,
        description="List pending security updates and report how far behind the host is.",
        intent_examples=["what updates are pending", "is this host patched", "list security updates"],
        params=[
            ParamSpec(
                name="filter",
                type="enum",
                default="security",
                choices=["security", "all"],
                description="Which updates to list",
            ),
            ParamSpec(name="target", type="string", default="localhost", description="Host being assessed"),
        ],
        target_params=["target"],
        scope_skip_params=["filter"],
        requires_scope=True,
        dry_run_template="apt list --upgradable",
        live_template="apt list --upgradable",
        explain=(
            "Pending security updates are the single highest-yield finding in most environments, "
            "and they need no exploitation skill to be real."
        ),
        next_steps=["schedule the security updates on the maintenance board", "check whether any pending CVE is network-reachable"],
        timeout_s=180,
    ),
    ToolSpec(
        name="service_exposure_check",
        binary="ss",
        category="system",
        tier=1,
        description="List listening sockets and flag services bound to all interfaces unnecessarily.",
        intent_examples=["what is listening on this host", "which services are exposed", "check listening ports"],
        params=[
            ParamSpec(name="protocol", type="enum", default="tcp", choices=["tcp", "udp", "all"], description="Protocol to list"),
            ParamSpec(name="target", type="string", default="localhost", description="Host whose sockets are listed"),
        ],
        target_params=["target"],
        scope_skip_params=["protocol"],
        requires_scope=True,
        dry_run_template="ss -tulpn",
        live_template="ss -tulpn",
        explain=(
            "A development database bound to 0.0.0.0 is exposure that no firewall rule was ever "
            "written for, because nobody knew the port was open."
        ),
        next_steps=["rebind internal services to localhost", "cross-check each listener against the intended service list"],
        timeout_s=60,
    ),
    ToolSpec(
        name="kernel_hardening_check",
        binary="sysctl",
        category="system",
        tier=1,
        description="Review kernel hardening parameters: ASLR, IP forwarding, kptr restrictions, core dumps.",
        intent_examples=["check the kernel hardening settings", "is aslr enabled", "review sysctl security parameters"],
        params=[
            ParamSpec(name="target", type="string", default="localhost", description="Host whose kernel parameters are read"),
        ],
        target_params=["target"],
        requires_scope=True,
        dry_run_template="sysctl -a",
        live_template="sysctl -a",
        explain=(
            "IP forwarding enabled on a host that is not a router silently turns it into one, which "
            "defeats network segmentation it was assumed to have."
        ),
        next_steps=["apply the CIS sysctl baseline", "confirm the values survive a reboot (sysctl.d, not a live call)"],
        timeout_s=60,
    ),
    ToolSpec(
        name="log_forensics",
        binary="journalctl",
        category="system",
        tier=1,
        description="Search the journal for authentication failures, privilege escalation and service restarts.",
        intent_examples=["search the logs for failed logins", "any privilege escalation in the journal", "review recent auth events"],
        params=[
            ParamSpec(name="target", type="string", default="localhost", description="Host whose journal is searched"),
            ParamSpec(name="pattern", type="string", default="authentication failure", description="Search pattern"),
            ParamSpec(name="since", type="string", default="7 days ago", description="Time window"),
        ],
        target_params=["target"],
        scope_skip_params=["pattern", "since"],
        requires_scope=True,
        dry_run_template="journalctl --since '{since}' --grep '{pattern}' --no-pager",
        live_template="journalctl --since '{since}' --grep '{pattern}' --no-pager",
        explain=(
            "A burst of authentication failures from one source is the cheapest intrusion signal "
            "available, and it is already on the disk."
        ),
        next_steps=["pivot on the source address for each burst", "check whether any burst ended in a success"],
        timeout_s=180,
    ),
    ToolSpec(
        name="integrity_baseline",
        binary="aide",
        category="system",
        tier=1,
        description="Compare the filesystem against its integrity baseline and report drift.",
        intent_examples=["check file integrity", "has anything changed on the system", "run the integrity check"],
        # No address-shaped argument: the AIDE configuration path is fixed in the
        # template (only one sane value exists on a host). The scope covers the
        # *host* the baseline belongs to, which is what the operator reviews.
        params=[
            ParamSpec(name="target", type="string", default="localhost", description="Host whose integrity baseline is checked"),
        ],
        target_params=["target"],
        requires_scope=True,
        dry_run_template="aide --check --config /etc/aide/aide.conf",
        live_template="aide --check --config /etc/aide/aide.conf",
        explain=(
            "Drift in /etc or /usr/bin is the most reliable sign of compromise, and it is also the "
            "most reliable source of false positives - so it needs review, not automation."
        ),
        next_steps=["investigate every change not explained by a package update", "re-baseline only after review"],
        timeout_s=600,
    ),
]

# Tier distribution: T0 x7, T1 x7. No T2/T3. The T0 tools are purely local and
# declare no scope; the seven T1 tools all inspect a named host and therefore all
# declare ``requires_scope`` - see the module docstring for the audit that made
# that distinction explicit rather than leaving six of them looking local.
