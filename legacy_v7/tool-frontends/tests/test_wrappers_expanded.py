"""Tests for the expanded tool-frontend set (Phase 3 tool breadth).

The first seven frontends have their own suite in ``test_tools.py``. This file
covers everything added since, and - more importantly - asserts the *registry
invariants* across the whole set, so a future wrapper cannot be merged with a
missing tier flag, a missing dry-run template, or a dangerous command shape.

The invariant tests matter more than the per-tool ones: adding a wrapper is
easy, and the only thing standing between a typo in a new spec and a live
exploit command is a test like ``test_every_tier2_requires_scope_and_approval``.
"""
from __future__ import annotations

import pytest

from tool_frontends.guardrails import evaluate
from tool_frontends.registry import get_registry
from tool_frontends.runner import run_tool
from kanban_core.models import Scope


@pytest.fixture(scope="module")
def registry():
    return get_registry()


@pytest.fixture(scope="module")
def tools(registry):
    return registry.all()


def by_name(tools, name):
    match = [t for t in tools if t.name == name]
    assert match, f"tool '{name}' is not registered"
    return match[0]


# ---------------------------------------------------------------------------
# Set-level invariants
# ---------------------------------------------------------------------------
def test_registry_grew_past_the_reference_set(tools):
    """The Phase 3 goal was to move well beyond the original seven wrappers."""
    assert len(tools) >= 28, f"expected a materially wider tool set, got {len(tools)}"


def test_no_duplicate_tool_names(tools):
    names = [t.name for t in tools]
    assert len(names) == len(set(names))


def test_every_tool_has_a_safe_dry_run_template(tools):
    """Without this a tool could only ever run live - the whole safety story."""
    missing = [t.name for t in tools if not t.dry_run_template]
    assert missing == [], f"tools without a dry_run_template: {missing}"


def test_every_tier2_requires_scope_and_approval(tools):
    bad = [
        t.name
        for t in tools
        if t.tier >= 2 and not (t.requires_scope and t.requires_approval)
    ]
    assert bad == [], f"T2+ tools missing scope/approval flags: {bad}"


def test_every_tier3_is_sandboxed(tools):
    bad = [t.name for t in tools if t.tier >= 3 and not t.requires_sandbox]
    assert bad == [], f"T3 tools missing requires_sandbox: {bad}"


def test_every_tool_declares_a_target_parameter(tools):
    """Scope enforcement needs to know *what* a tool acts on."""
    bad = [t.name for t in tools if not t.target_params]
    assert bad == [], f"tools with no target_params: {bad}"


def test_all_four_tiers_are_populated(tools):
    tiers = {t.tier for t in tools}
    assert tiers == {0, 1, 2, 3}, f"tier coverage is incomplete: {sorted(tiers)}"


def test_all_three_integration_patterns_are_used(tools):
    patterns = {t.integration for t in tools}
    assert patterns == {"cli_wrapper", "gui_panel", "agent_skill"}


def test_categories_cover_the_blueprint_table(tools):
    """Blueprint section 05 lists these categories; the registry must serve them."""
    expected = {
        "information-gathering",
        "vulnerability-analysis",
        "web-application",
        "password-attacks",
        "wireless",
        "forensics",
        "reverse-engineering",
        "exploitation",
        "reporting",
        "system",
    }
    present = {t.category for t in tools}
    assert expected <= present, f"missing categories: {sorted(expected - present)}"


def test_every_tool_explains_itself_and_suggests_next_steps(tools):
    """Blueprint 05: a frontend explains results and proposes next steps."""
    missing_explain = [t.name for t in tools if not t.explain]
    missing_next = [t.name for t in tools if not t.next_steps]
    assert missing_explain == [], f"tools without an explanation: {missing_explain}"
    assert missing_next == [], f"tools without next steps: {missing_next}"


def test_mcp_listing_reflects_the_whole_registry(registry, tools):
    listing = registry.mcp_listing()
    assert len(listing) == len(tools)
    for entry in listing:
        assert entry["name"]
        assert entry["inputSchema"]["type"] == "object"
        assert "tier" in entry["annotations"]


# ---------------------------------------------------------------------------
# New category: SMB
# ---------------------------------------------------------------------------
def test_smb_enum_is_t1_and_needs_scope(registry):
    spec = registry.require("smb_enum")
    assert spec.tier == 1
    assert spec.requires_scope is True
    assert spec.requires_approval is False


def test_smb_enum_dry_run_executes_nothing(registry):
    spec = registry.require("smb_enum")
    result = run_tool(spec, {"target": "fileserver.example.net"})
    assert result.dry_run is True
    assert result.status == "dry_run"
    assert "enum4linux-ng" in result.command
    assert result.exit_code is None


def test_smb_signing_check_uses_the_script_flag(registry):
    spec = registry.require("smb_signing_check")
    result = run_tool(spec, {"target": "10.0.0.5"})
    assert "smb2-security-mode" in result.command
    assert "-p 445" in result.command


# ---------------------------------------------------------------------------
# New category: DNS
# ---------------------------------------------------------------------------
def test_dns_zone_transfer_enforces_scope_on_the_nameserver(registry):
    """Both parameters the command touches must be inside the scope.

    This test originally asserted ``target_params == ["nameserver"]`` - i.e. that
    only the nameserver mattered and the **zone name in ``target`` needed no
    authorization at all**. That is wrong in both directions:

    * ``dig AXFR @{nameserver} {target}`` sends the zone name to the
      nameserver, so enumerating a zone you are not authorized for is exactly
      the act the scope exists to prevent; and
    * declaring ``nameserver`` *instead of* ``target`` meant the parameter the
      spec called its target was the one parameter never checked.

    Phase 3 corrected the contract to require both, and to enforce it **by
    value** as well, so an undeclared second host could not slip through. The
    original intent - "a nameserver outside the scope is refused even though the
    domain matches" - is preserved and asserted below.
    """
    spec = registry.require("dns_zone_transfer")
    assert set(spec.target_params) == {"target", "nameserver"}

    # The nameserver sits on a *different* domain on purpose. An earlier revision
    # of this test used ``ns1.example.net``, which a scope of ``example.net``
    # legitimately covers under the subdomain rule - so it proved the opposite of
    # what it claimed.
    args = {"target": "example.net", "nameserver": "ns1.dnshost.net"}

    # Both hosts authorized -> allowed.
    both = Scope(targets=["example.net", "ns1.dnshost.net"], authorization_ref="TICKET-1")
    assert evaluate(spec, args, scope=both).allowed is True, evaluate(spec, args, scope=both).reasons

    # Nameserver outside the scope -> refused, even though the domain matches.
    scope2 = Scope(targets=["example.net"], authorization_ref="TICKET-1")
    denied = evaluate(spec, args, scope=scope2)
    assert denied.allowed is False
    assert any("ns1.dnshost.net" in r for r in denied.reasons)

    # Zone name outside the scope -> also refused. This is the half the original
    # declaration missed, and the reason the AXFR would have run at all.
    scope3 = Scope(targets=["ns1.dnshost.net"], authorization_ref="TICKET-1")
    zone_denied = evaluate(spec, args, scope=scope3)
    assert zone_denied.allowed is False
    assert any("example.net" in r for r in zone_denied.reasons)



def test_dns_enum_renders_the_mode(registry):
    spec = registry.require("dns_enum")
    result = run_tool(spec, {"target": "example.net", "record_type": "srv"})
    assert "dnsrecon" in result.command
    assert "-t srv" in result.command


# ---------------------------------------------------------------------------
# New category: TLS
# ---------------------------------------------------------------------------
def test_tls_probe_builds_an_sni_correct_command(registry):
    """Without -servername the handshake returns the wrong virtual host."""
    spec = registry.require("tls_probe")
    result = run_tool(spec, {"target": "shop.example.net", "port": 8443})
    assert "-servername shop.example.net" in result.command
    assert "shop.example.net:8443" in result.command


def test_sslscan_is_scope_gated_but_ungated_by_approval(registry):
    spec = registry.require("sslscan_scan")
    assert spec.tier == 1
    assert spec.requires_scope is True
    assert spec.requires_approval is False


# ---------------------------------------------------------------------------
# New category: web
# ---------------------------------------------------------------------------
def test_gobuster_is_t2_and_needs_an_open_gate(registry):
    spec = registry.require("gobuster_dirs")
    assert spec.tier == 2
    assert spec.requires_scope and spec.requires_approval

    scope = Scope(targets=["shop.example.net"], authorization_ref="TICKET-1")
    args = {"target": "shop.example.net"}

    without_gate = evaluate(spec, args, scope=scope, approved=False)
    assert without_gate.allowed is False
    assert any("approval" in r for r in without_gate.reasons)

    with_gate = evaluate(spec, args, scope=scope, approved=True)
    assert with_gate.allowed is True, with_gate.reasons


def test_wpscan_and_nuclei_are_gated_t2(registry):
    for name in ("wpscan_scan", "nuclei_scan"):
        spec = registry.require(name)
        assert spec.tier == 2, name
        assert spec.requires_approval, name


def test_whatweb_defaults_to_passive_aggression(registry):
    spec = registry.require("whatweb_fingerprint")
    result = run_tool(spec, {"target": "example.net"})
    assert "--aggression 1" in result.command


# ---------------------------------------------------------------------------
# New categories: offline analysis (T0)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,args,needle",
    [
        ("searchsploit_query", {"query": "apache 2.4.49"}, "searchsploit"),
        ("hashid_identify", {"hash": "5f4dcc3b5aa765d61d8327deb882cf99"}, "hashid"),
        ("forensics_hash", {"evidence": "/cases/img.dd"}, "sha256sum"),
        ("re_strings", {"binary_path": "/bin/ls"}, "strings"),
        ("re_readelf", {"binary_path": "/bin/ls"}, "readelf"),
        ("pandoc_report", {"source": "notes.md"}, "pandoc"),
    ],
)
def test_offline_tools_are_t0_and_render(registry, name, args, needle):
    spec = registry.require(name)
    assert spec.tier == 0, f"{name} should be passive/offline"
    result = run_tool(spec, args)
    assert result.dry_run is True
    assert result.allowed is True, result.reasons
    assert needle in result.command


# ---------------------------------------------------------------------------
# Regression: the T1 scope-enforcement gap (found while adding these wrappers)
# ---------------------------------------------------------------------------
def test_t1_tool_with_an_attached_scope_is_actually_enforced(registry):
    """A scope that is attached must be a scope that is enforced.

    The guardrail engine used to run its coverage check for T2+ only, so
    ``requires_scope=True`` on a T1 spec was decorative - a scoped T1 tool ran
    against any target at all. Every T1 tool in this file declares
    ``requires_scope``, which is what made the gap visible.
    """
    spec = registry.require("smb_enum")
    assert spec.tier == 1

    scope = Scope(targets=["authorized.example.net"], authorization_ref="TICKET-1")
    denied = evaluate(spec, {"target": "evil.example.org"}, scope=scope)
    assert denied.allowed is False, "a T1 tool ignored the scope it was given"
    assert any("outside the authorized scope" in r for r in denied.reasons)

    allowed = evaluate(spec, {"target": "authorized.example.net"}, scope=scope)
    assert allowed.allowed is True, allowed.reasons


def test_t1_tool_without_a_scope_still_works(registry):
    """Only T2+ *mandates* a scope; a T1 probe should not demand one."""
    spec = registry.require("tls_probe")
    decision = evaluate(spec, {"target": "example.net"})
    assert decision.allowed is True, decision.reasons


def test_t1_and_t2_agree_on_scope_error_wording(registry):
    """The engine and the bridge pre-check must not describe the same refusal
    two different ways - a drifting message is how a second code path appears."""
    scope = Scope(targets=["allowed.example.net"], authorization_ref="T-1")
    t1 = evaluate(registry.require("smb_enum"), {"target": "evil.example.org"}, scope=scope)
    t2 = evaluate(
        registry.require("nikto_scan"),
        {"target": "evil.example.org"},
        scope=scope,
        approved=True,
    )
    assert t1.allowed is False and t2.allowed is False
    assert any("outside the authorized scope" in r for r in t1.reasons)
    assert any("outside the authorized scope" in r for r in t2.reasons)


def test_expired_scope_stops_a_t1_tool_too(registry):
    spec = registry.require("sslscan_scan")
    expired = Scope(
        targets=["example.net"],
        authorization_ref="T-1",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    decision = evaluate(spec, {"target": "example.net"}, scope=expired)
    assert decision.allowed is False
    assert any("expired" in r for r in decision.reasons)


def test_bssid_scope_matches_case_insensitively(registry):
    """A MAC scope must not silently fail because of letter case."""
    spec = registry.require("wifi_survey")
    scope = Scope(targets=["AA:BB:CC:DD:EE:FF"], authorization_ref="W-1")
    args = {"interface": "wlan0mon", "bssid": "aa:bb:cc:dd:ee:ff"}
    decision = evaluate(spec, args, scope=scope, approved=True)
    assert decision.allowed is True, decision.reasons


def test_offline_tools_do_not_demand_a_scope(registry):
    """A local file has no network scope to authorise - demanding one is noise."""
    for name in ("re_strings", "re_readelf", "hashid_identify", "forensics_hash"):
        spec = registry.require(name)
        assert spec.requires_scope is False, name


def test_re_readelf_section_parameter_is_an_enum(registry):
    spec = registry.require("re_readelf")
    result = run_tool(spec, {"binary_path": "/bin/ls", "section": "symbols"})
    assert "-symbols" in result.command

    bad = run_tool(spec, {"binary_path": "/bin/ls", "section": "nonsense"})
    assert bad.allowed is False
    assert any("must be one of" in r for r in bad.reasons)


# ---------------------------------------------------------------------------
# Forensics (T1)
# ---------------------------------------------------------------------------
def test_binwalk_and_volatility_are_t1(registry):
    for name in ("binwalk_extract", "volatility_pslist"):
        assert registry.require(name).tier == 1, name


def test_r2_analyze_is_t1_and_offline_safe(registry):
    spec = registry.require("r2_analyze")
    assert spec.tier == 1
    result = run_tool(spec, {"binary_path": "/tmp/sample.bin"})
    assert result.dry_run is True


# ---------------------------------------------------------------------------
# Wireless (T2 survey / T3 deauth)
# ---------------------------------------------------------------------------
def test_wifi_survey_is_t2_and_scoped_on_the_bssid(registry):
    spec = registry.require("wifi_survey")
    assert spec.tier == 2
    assert spec.target_params == ["bssid"]

    scope = Scope(targets=["aa:bb:cc:dd:ee:ff"], authorization_ref="WIFI-2026-1")
    args = {"interface": "wlan0mon", "bssid": "aa:bb:cc:dd:ee:ff"}
    assert evaluate(spec, args, scope=scope, approved=True).allowed is True

    out_of_scope = Scope(targets=["11:22:33:44:55:66"], authorization_ref="WIFI-2026-1")
    denied = evaluate(spec, args, scope=out_of_scope, approved=True)
    assert denied.allowed is False


def test_wifi_deauth_is_t3_sandboxed_and_never_gate_free(registry):
    spec = registry.require("wifi_deauth")
    assert spec.tier == 3
    assert spec.requires_sandbox is True
    assert spec.requires_approval is True

    scope = Scope(targets=["aa:bb:cc:dd:ee:ff"], authorization_ref="WIFI-2026-1")
    args = {"interface": "wlan0mon", "bssid": "aa:bb:cc:dd:ee:ff"}
    decision = evaluate(spec, args, scope=scope, approved=False)
    assert decision.allowed is False
    assert any("approval" in r for r in decision.reasons)


# ---------------------------------------------------------------------------
# High-impact: password attacks and exploitation
# ---------------------------------------------------------------------------
def test_hydra_is_t3_and_refuses_without_a_gate(registry):
    spec = registry.require("hydra_bruteforce")
    assert spec.tier == 3
    scope = Scope(targets=["10.0.0.9"], authorization_ref="TICKET-9")
    denied = evaluate(spec, {"target": "10.0.0.9"}, scope=scope, approved=False)
    assert denied.allowed is False
    assert denied.status == "needs_approval"


def test_metasploit_dry_run_uses_the_non_destructive_check(registry):
    """Dry-run must not simply be the live command with a flag flipped."""
    spec = registry.require("metasploit_run")
    assert "check" in spec.dry_run_template
    assert "run" in spec.live_template
    assert spec.dry_run_template != spec.live_template

    result = run_tool(spec, {"target": "10.0.0.9", "module": "exploit/unix/ftp/vsftpd_234_backdoor"})
    assert result.dry_run is True
    assert "; check" in result.command


def test_sqlmap_and_metasploit_are_the_only_t3_scope_flagged_tools(registry):
    """A guard against a new wrapper quietly landing at T3 without the flags."""
    t3 = [t for t in registry.by_tier(3)]
    assert len(t3) >= 3
    for spec in t3:
        assert spec.requires_scope and spec.requires_approval and spec.requires_sandbox, spec.name


# ---------------------------------------------------------------------------
# Guardrail parity with the first suite
# ---------------------------------------------------------------------------
def test_live_request_is_denied_not_downgraded_for_a_new_tool(registry):
    """The 'never a silent downgrade' rule must hold for every new wrapper too."""
    spec = registry.require("smb_enum")
    result = run_tool(
        spec,
        {"target": "fileserver.example.net"},
        scope=Scope(targets=["fileserver.example.net"], authorization_ref="T-1"),
        live=True,
        live_unlocked=False,
    )
    assert result.allowed is False
    assert result.status == "denied"
    assert result.dry_run is False
    assert any("not unlocked" in r for r in result.reasons)


def test_unknown_parameter_is_refused_on_a_new_tool(registry):
    spec = registry.require("dns_enum")
    result = run_tool(spec, {"target": "example.net", "bogus_flag": "1"})
    assert result.allowed is False
    assert any("unknown parameter" in r for r in result.reasons)


def test_intent_router_finds_the_new_tools(registry):
    """Natural-language routing is only useful if the new tools are reachable."""
    cases = {
        "enumerate the smb shares on the file server": "smb_enum",
        "try a zone transfer against the nameserver": "dns_zone_transfer",
        "check the tls certificate": "tls_probe",
        "what cms is this site running": "whatweb_fingerprint",
        "find hidden directories on the site": "gobuster_dirs",
        "brute force the ssh login": "hydra_bruteforce",
        "what type of hash is this": "hashid_identify",
        "pull strings from this binary": "re_strings",
    }
    for phrase, expected in cases.items():
        hits = registry.resolve_intent(phrase)
        assert hits, f"no tool matched: {phrase!r}"
        assert hits[0].name == expected, f"{phrase!r} routed to {hits[0].name}, expected {expected}"
