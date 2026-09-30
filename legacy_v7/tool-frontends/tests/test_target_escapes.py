"""Workstream C regression tests - the target-escape class.

Every test in the first half of this file is written to **fail against the
pre-Phase-3 behaviour**. They are not describing what the code does; they are
proving a specific defect is closed.

The escapes
-----------
1. ``dns_zone_transfer`` declares ``target`` and ``nameserver``; the AXFR is
   actually sent to ``nameserver``. Scope enforcement compared declared
   parameters by hand, and only ``target`` was declared - so the query went to
   whatever host the caller named. (Fixed by declaring the parameter.)
2. The *class* behind it: any wrapper with an address-shaped parameter that was
   never declared. A per-wrapper fix does not close this, because the next
   wrapper re-opens it. Closed by enforcing scope **by value** as well as by
   declaration, and by a lint that fails the build on any undeclared
   address-shaped parameter.
3. A T1 tool declaring ``requires_scope`` could still run **live** with no scope
   attached, because the coverage check only ran when a scope was present.
"""
from __future__ import annotations

import pytest

from kanban_core.models import Scope
from tool_frontends.guardrails import evaluate
from tool_frontends.registry import get_registry
from tool_frontends.spec import ParamSpec, ToolSpec
from tool_frontends.targets import classify, escapes, normalize, scan


def _scope(*, targets=("shop.example.net",), cidrs=("10.20.0.0/24",), ref="ENG-1042"):
    return Scope(targets=list(targets), cidrs=list(cidrs), authorization_ref=ref)


# ===========================================================================
# the classifier - what is and is not an address
# ===========================================================================

ADDRESSES = [
    ("10.20.0.5", "ipv4"),
    ("192.168.1.1/24", "cidr"),
    ("2001:db8::1", "ipv6"),
    ("aa:bb:cc:dd:ee:ff", "mac"),
    ("https://shop.example.net/checkout", "url"),
    ("http://10.20.0.5:8080/api", "url"),
    ("shop.example.net", "hostname"),
    ("mail.example.net:25", "hostname"),
    ("shop.example.net.", "hostname"),
    ("analyst@example.net", "email"),
    ("abcdefghij234567.onion", "onion"),
]

NOT_ADDRESSES = [
    ("/usr/share/wordlists/dirb/common.txt", "local path"),
    ("/usr/share/wordlists/rockyou.txt", "wordlist path"),
    ("rockyou.txt", "bare filename with a known extension"),
    ("./capture.pcap", "relative path"),
    ("/evidence/disk-image.dd", "evidence path"),
    ("top100", "port spec, no dot"),
    ("root", "username"),
    ("ssh", "service enum"),
    ("passive", "aggression enum"),
    ("medium,high,critical", "severity list"),
    ("exploit/windows/smb/ms17_010_eternalblue", "metasploit module path"),
    ("cmd/unix/generic", "payload path"),
    ("headers", "readelf section enum"),
    ("scan", "generic word"),
]


@pytest.mark.parametrize("value,kind", ADDRESSES)
def test_classifier_recognises_addresses(value, kind):
    assert classify(value) == kind


@pytest.mark.parametrize("value,why", NOT_ADDRESSES)
def test_classifier_leaves_local_values_alone(value, why):
    """A false positive here makes the control noisy, and a noisy control is off."""
    assert classify(value) is None, f"{value!r} should not classify as an address ({why})"


def test_normalize_reduces_to_host_form():
    assert normalize("https://shop.example.net:8443/checkout?x=1") == "shop.example.net"
    assert normalize("https://10.20.0.5/api") == "10.20.0.5"
    assert normalize("analyst@example.net") == "example.net"
    assert normalize("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"


def test_scan_marks_declared_vs_undeclared():
    found = scan(
        {"target": "shop.example.net", "nameserver": "ns1.other.net", "ports": "top100"},
        declared_params=["target"],
    )
    by_key = {f.key: f for f in found}
    assert set(by_key) == {"target", "nameserver"}, "ports must not be treated as an address"
    assert by_key["target"].declared is True
    assert by_key["nameserver"].declared is False


# ===========================================================================
# ESCAPE 1 - the concrete dns_zone_transfer hole
# ===========================================================================

def test_regression_zone_transfer_nameserver_escape_is_refused():
    """PRE-FIX: allowed=True. The AXFR went to an unauthorised nameserver.

    ``target`` is in scope, so the old declared-parameter check passed, and
    nothing ever looked at ``nameserver`` - the host the packet is actually
    sent to.
    """
    spec = get_registry().require("dns_zone_transfer")
    decision = evaluate(
        spec,
        {"target": "shop.example.net", "nameserver": "ns1.attacker.example"},
        scope=_scope(),
    )
    assert decision.allowed is False, "an out-of-scope nameserver must refuse the whole call"
    assert "ns1.attacker.example" in " ".join(decision.reasons)


def test_zone_transfer_in_scope_nameserver_still_runs():
    """The fix must not break the legitimate case."""
    spec = get_registry().require("dns_zone_transfer")
    decision = evaluate(
        spec,
        {"target": "shop.example.net", "nameserver": "shop.example.net"},
        scope=_scope(),
    )
    assert decision.allowed is True, decision.reasons


def test_zone_transfer_declares_both_reaching_parameters():
    """The declaration half of the fix, asserted so it cannot be quietly undone."""
    spec = get_registry().require("dns_zone_transfer")
    assert set(spec.target_params) == {"target", "nameserver"}


# ===========================================================================
# ESCAPE 2 - the class: any undeclared address-shaped parameter
# ===========================================================================

def _spec_with_undeclared_address(tier: int = 1) -> ToolSpec:
    """A wrapper that reaches a second host without declaring it.

    This is deliberately not a real tool: it is the shape of the bug, kept in
    the test file so the class stays closed even if every shipped wrapper were
    rewritten.
    """
    return ToolSpec(
        name="hypothetical_probe",
        binary="probe",
        category="information-gathering",
        tier=tier,
        params=[
            ParamSpec(name="target", type="string", required=True),
            # The second host. Never declared in target_params.
            ParamSpec(name="relay", type="string", required=True),
        ],
        target_params=["target"],
        dry_run_template="probe {target} via {relay}",
        live_template="probe {target} via {relay}",
        requires_scope=True,
        requires_approval=(tier >= 2),
        requires_sandbox=(tier >= 3),
    )


def test_regression_undeclared_address_parameter_is_refused():
    """PRE-FIX: allowed=True. ``relay`` was invisible to scope enforcement.

    This is the class test. A per-wrapper fix to dns_zone_transfer would not
    make this pass; only enforcing coverage *by value* does.
    """
    spec = _spec_with_undeclared_address()
    decision = evaluate(
        spec,
        {"target": "shop.example.net", "relay": "relay.attacker.example"},
        scope=_scope(),
        approved=True,
    )
    assert decision.allowed is False, "an undeclared address parameter must still be scope-checked"
    joined = " ".join(decision.reasons)
    assert "relay" in joined and "relay.attacker.example" in joined
    assert decision.checks.get("arg_scope_escapes")


def test_regression_undeclared_ipv4_parameter_is_refused():
    """The same class with a raw IP rather than a hostname."""
    spec = _spec_with_undeclared_address()
    decision = evaluate(
        spec,
        {"target": "shop.example.net", "relay": "203.0.113.9"},
        scope=_scope(),
        approved=True,
    )
    assert decision.allowed is False
    assert "203.0.113.9" in " ".join(decision.reasons)


def test_regression_undeclared_mac_parameter_is_refused():
    """The same class for a wireless target keyed on a BSSID."""
    spec = ToolSpec(
        name="hypothetical_wifi",
        binary="probe",
        category="wireless",
        tier=2,
        params=[
            ParamSpec(name="target", type="string", required=True),
            ParamSpec(name="ap", type="string", required=True),
        ],
        target_params=["target"],
        dry_run_template="probe {target} ap {ap}",
        live_template="probe {target} ap {ap}",
        requires_scope=True,
        requires_approval=True,
    )
    decision = evaluate(
        spec,
        {"target": "10.20.0.5", "ap": "DE:AD:BE:EF:00:01"},
        scope=_scope(targets=["11.22.33.44"], cidrs=["10.20.0.0/24"]),
        approved=True,
    )
    assert decision.allowed is False
    assert "DE:AD:BE:EF:00:01" in " ".join(decision.reasons)


def test_undeclared_parameter_inside_scope_is_allowed():
    """No false positive: an in-scope second host is fine."""
    spec = _spec_with_undeclared_address()
    decision = evaluate(
        spec,
        {"target": "shop.example.net", "relay": "10.20.0.9"},
        scope=_scope(),
        approved=True,
    )
    assert decision.allowed is True, decision.reasons


def test_value_check_only_applies_to_scope_declaring_tools():
    """A local-only tool with an address-shaped string must not be refused.

    ``requires_scope`` is what marks a tool as reaching the network. Without
    this guard, an offline tool handed something address-shaped would be
    refused for a target it never contacts.
    """
    spec = get_registry().require("re_strings")  # T0, offline, requires_scope=False
    decision = evaluate(
        spec,
        {"binary_path": "/tmp/sample.bin", "min_length": 6},
        scope=_scope(),
    )
    assert decision.allowed is True, decision.reasons


# ===========================================================================
# ESCAPE 3 - T1 live execution with no scope attached
# ===========================================================================

def test_regression_t1_live_without_scope_is_refused():
    """PRE-FIX: allowed=True. A scoped T1 tool could run live with no scope.

    The coverage check only executed when a scope was present, so a live run
    with none was never compared against anything - it went straight to argv.
    """
    spec = get_registry().require("smb_enum")
    decision = evaluate(
        spec,
        {"target": "203.0.113.9", "depth": "shares"},
        scope=None,
        live=True,
        live_unlocked=True,
    )
    assert decision.allowed is False, "live execution with no scope has nothing to check against"
    assert any("requires_scope" in r or "scope" in r for r in decision.reasons)


def test_t1_dry_run_without_scope_still_allowed():
    """The operator may still *see* what would run without a scope attached."""
    spec = get_registry().require("smb_enum")
    decision = evaluate(
        spec,
        {"target": "203.0.113.9", "depth": "shares"},
        scope=None,
        live=False,
    )
    assert decision.allowed is True, decision.reasons


def test_t2_without_scope_still_refused():
    """The pre-existing T2 rule must survive the rewrite."""
    spec = get_registry().require("nikto_scan")
    decision = evaluate(spec, {"target": "shop.example.net"}, scope=None, approved=True)
    assert decision.allowed is False
    assert any("requires an authorization scope" in r for r in decision.reasons)


# ===========================================================================
# ESCAPE 4 - single-label declared targets are still enforced
# ===========================================================================

def test_single_label_declared_target_is_still_scope_checked():
    """The classifier ignores ``fileserver``; the declared check must not.

    This is the seam between the two layers, so it is asserted directly.
    """
    spec = get_registry().require("smb_enum")
    decision = evaluate(
        spec,
        {"target": "fileserver", "depth": "shares"},
        scope=_scope(),
    )
    assert decision.allowed is False, "a single-label target outside scope must still refuse"
    assert "fileserver" in " ".join(decision.reasons)


def test_single_label_target_inside_scope_is_allowed():
    spec = get_registry().require("smb_enum")
    decision = evaluate(
        spec,
        {"target": "fileserver", "depth": "shares"},
        scope=_scope(targets=["fileserver"]),
    )
    assert decision.allowed is True, decision.reasons


def test_one_escape_yields_one_reason():
    """No duplicate reasons for the same escape."""
    spec = get_registry().require("dns_zone_transfer")
    decision = evaluate(
        spec,
        {"target": "shop.example.net", "nameserver": "ns1.attacker.example"},
        scope=_scope(),
    )
    matching = [r for r in decision.reasons if "ns1.attacker.example" in r]
    assert len(matching) == 1, matching


# ===========================================================================
# the authoring lint - no wrapper may carry an undeclared address parameter
# ===========================================================================

def test_every_address_shaped_parameter_is_declared_or_explicitly_skipped():
    """Fail the build if a wrapper can reach a host nobody declared.

    The runtime classifier closes the class, but it is biased against
    single-label hostnames. This lint is the other half: it forces every
    address-shaped parameter to be either declared as a target or written down
    in ``scope_skip_params`` with a reason. Adding a tool that quietly reaches a
    second host now breaks the build instead of shipping.
    """
    offenders: list[str] = []
    for spec in get_registry().all():
        for param in spec.params:
            if param.default is None:
                continue
            if classify(param.default) is None:
                continue
            if param.name in spec.target_params or param.name in spec.scope_skip_params:
                continue
            offenders.append(
                f"{spec.name}.{param.name}={param.default!r} is address-shaped but is "
                f"neither in target_params nor in scope_skip_params"
            )
    assert not offenders, "undeclared address-shaped parameters:\n  " + "\n  ".join(offenders)


def test_scope_skip_params_are_real_and_justified():
    """An exemption must name a real parameter and cannot be a catch-all."""
    problems: list[str] = []
    for spec in get_registry().all():
        names = {p.name for p in spec.params}
        for skipped in spec.scope_skip_params:
            if skipped not in names:
                problems.append(f"{spec.name}: scope_skip_params names unknown parameter {skipped!r}")
            if skipped in spec.target_params:
                problems.append(f"{spec.name}: {skipped!r} is both a target and a skip")
    assert not problems, "\n  ".join(problems)


def test_no_scope_requiring_wrapper_has_an_empty_target_list():
    problems = [
        s.name
        for s in get_registry().all()
        if s.requires_scope and not s.target_params
    ]
    assert not problems, f"requires_scope with no target_params: {problems}"


def test_scope_declaring_wrappers_declare_every_non_local_string_param():
    """Every string parameter of a scope-requiring tool is either a target,
    skipped, or a known non-address vocabulary.

    This is the strictest form of the lint and the one that actually catches a
    new wrapper reaching a second host: if the value could plausibly be a host
    and it is not declared, this fails.
    """
    non_address_vocab = {
        ("whois_lookup", "record_type"),
        ("dns_lookup", "record_type"),
        ("dns_enum", "record_type"),
        ("nmap_scan", "ports"),
        ("nikto_scan", "tuning"),
        ("nikto_scan", "target"),
        ("gobuster_dirs", "wordlist"),
        ("hydra_bruteforce", "username"),
        ("hydra_bruteforce", "service"),
        ("hydra_bruteforce", "wordlist"),
        ("sqlmap_test", "target"),
        ("metasploit_run", "module"),
        ("metasploit_run", "payload"),
        ("wifi_survey", "interface"),
        ("wifi_survey", "band"),
        ("wifi_deauth", "interface"),
        ("wifi_deauth", "client"),
        ("smb_enum", "depth"),
        ("wpscan_scan", "enumerate"),
        ("whatweb_fingerprint", "aggression"),
        ("nuclei_scan", "severity"),
        ("sslscan_scan", "target"),
        ("httpx_probe", "target"),
        ("tls_probe", "target"),
        ("smb_signing_check", "target"),
        ("dns_zone_transfer", "nameserver"),
    }
    offenders: list[str] = []
    for spec in get_registry().all():
        if not spec.requires_scope:
            continue
        for param in spec.params:
            if param.type != "string":
                continue
            if param.name in spec.target_params or param.name in spec.scope_skip_params:
                continue
            if (spec.name, param.name) in non_address_vocab:
                continue
            offenders.append(f"{spec.name}.{param.name}")
    assert not offenders, (
        "scope-declaring wrappers with an unexplained string parameter "
        f"(declare it, skip it with a reason, or add it to the vocabulary): {offenders}"
    )


# ===========================================================================
# the escape helper directly
# ===========================================================================

def test_escapes_helper_reports_only_out_of_scope_addresses():
    scope = _scope()
    found = escapes(
        {"target": "shop.example.net", "nameserver": "10.20.0.7", "ports": "top100"},
        scope=scope,
        declared_params=["target", "nameserver"],
    )
    assert found == []

    found = escapes(
        {"target": "shop.example.net", "nameserver": "8.8.8.8"},
        scope=scope,
        declared_params=["target", "nameserver"],
    )
    assert len(found) == 1
    assert "8.8.8.8" in found[0]


def test_escapes_helper_returns_nothing_without_a_scope():
    """No scope means nothing to compare against; that is the engine's job to
    refuse, not the classifier's."""
    assert escapes({"target": "anywhere.example"}, scope=None) == []


def test_skip_keys_suppresses_a_deliberate_exemption():
    scope = _scope(targets=["11:22:33:44:55:66"])
    args = {"target": "11:22:33:44:55:66", "client": "ff:ff:ff:ff:ff:ff"}
    assert escapes(args, scope=scope, declared_params=["target"]) != []
    assert escapes(args, scope=scope, declared_params=["target"], skip_keys=["client"]) == []
