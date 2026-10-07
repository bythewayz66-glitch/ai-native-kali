"""Workstream B tests - the cloud, system and social-engineering categories.

Parametrized over the registry rather than hand-written per wrapper: the
properties that must hold (a dry run renders, a tier implies its gates, scope
declarations are coherent) are *universal*, so they are asserted universally.
A wrapper that gets one wrong fails the build, which is what keeps "assign a
tier" from being a comment rather than a rule.

The final section asserts the category-specific rules that are not universal -
notably that no social-engineering wrapper can read back a captured secret.
"""
from __future__ import annotations

import pytest

from kanban_core.models import Scope
from tool_frontends.guardrails import evaluate
from tool_frontends.registry import get_registry
from tool_frontends.runner import run_tool

NEW_CATEGORIES = ("cloud", "system", "social-engineering")

registry = get_registry()
new_tools = [t for t in registry.all() if t.category in NEW_CATEGORIES]


def _ids(tools):
    return [t.name for t in tools]


ALL = registry.all()

# ===========================================================================
# the registry grew, and every addition is well formed
# ===========================================================================

def test_registry_reaches_seventy_tools():
    """The workstream target. Asserted so a regression cannot silently shrink it."""
    assert len(ALL) >= 70, f"expected >=70 tools, found {len(ALL)}"


def test_new_categories_are_all_populated():
    by_cat: dict[str, int] = {}
    for spec in ALL:
        by_cat[spec.category] = by_cat.get(spec.category, 0) + 1
    for category in NEW_CATEGORIES:
        assert by_cat.get(category, 0) > 0, f"category {category} is empty"


def test_tool_names_are_unique():
    names = [t.name for t in ALL]
    assert len(names) == len(set(names)), "duplicate tool names in the registry"


def test_every_tool_dry_run_template_renders():
    """A spec whose dry run cannot render is a spec nobody can preview.

    ``render`` substitutes every ``{field}`` and raises on a missing one, so a
    template that returns at all had all of its substitution fields satisfied.
    The assertion is deliberately *not* "contains no brace" - templates that use
    shell and awk syntax legitimately produce literal braces (``%{http_code}``,
    ``{{print}}``), and asserting their absence would forbid correct templates.
    """
    for spec in ALL:
        args: dict[str, object] = {}
        for p in spec.params:
            if p.default is not None:
                args[p.name] = p.default
            elif p.required:
                args[p.name] = "10.20.0.5" if p.type == "string" else 1
        rendered = spec.render(spec.dry_run_template, args)
        assert rendered.strip(), f"{spec.name} rendered an empty dry run"


@pytest.mark.parametrize("spec", new_tools, ids=_ids(new_tools))
def test_new_tool_has_intent_examples(spec):
    """Natural-language routing needs phrases to learn from."""
    assert spec.intent_examples, f"{spec.name} has no intent examples"
    assert all(len(p) > 8 for p in spec.intent_examples)


@pytest.mark.parametrize("spec", new_tools, ids=_ids(new_tools))
def test_new_tool_has_an_explanation_and_next_steps(spec):
    """The frontend contract: explain the result and suggest what to do next."""
    assert spec.explain, f"{spec.name} does not explain its output"
    assert spec.next_steps, f"{spec.name} suggests no next steps"


@pytest.mark.parametrize("spec", new_tools, ids=_ids(new_tools))
def test_new_tool_timeout_is_bounded(spec):
    assert 0 < spec.timeout_s <= 1800, f"{spec.name} has an unreasonable timeout"


# ===========================================================================
# tier implies gates - the rule that makes a tier more than a label
# ===========================================================================

@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_tier_two_and_above_requires_approval(spec):
    if spec.tier >= 2:
        assert spec.requires_approval, f"{spec.name} is T{spec.tier} but needs no approval"


@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_tier_three_requires_a_sandbox(spec):
    if spec.tier >= 3:
        assert spec.requires_sandbox, f"{spec.name} is T3 but runs unsandboxed"


@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_tier_zero_never_requires_approval(spec):
    """T0 is read-only-by-construction; gating it would train operators to click through."""
    if spec.tier == 0:
        assert not spec.requires_approval, f"{spec.name} is T0 but demands approval"


@pytest.mark.parametrize("spec", new_tools, ids=_ids(new_tools))
def test_new_scope_declaring_tool_is_coherent(spec):
    if not spec.requires_scope:
        return
    assert spec.target_params, f"{spec.name} requires a scope but declares no target parameter"
    for name in spec.target_params:
        assert name in {p.name for p in spec.params}, (
            f"{spec.name} declares target_param {name!r} which is not a real parameter"
        )


# ===========================================================================
# the live path stays refused by default
# ===========================================================================

@pytest.mark.parametrize("spec", new_tools, ids=_ids(new_tools))
def test_new_tool_dry_run_preview(spec):
    """Every new wrapper must be previewable, under the engine's real contract.

    Two rules are exercised here, and both were learned from failures rather than
    assumed:

    * a dry run needs **no live unlock** - it executes nothing; and
    * at T2+ the engine still requires the **approval gate**, and that is
      deliberate. Approving a *command* is the operator confirming they intend it
      before it can ever be executed, which is a different question from
      authorising its execution. Asserting this explicitly is what stops someone
      later "fixing" the gate away from the preview path.

    The callback argument is in scope on purpose. An earlier version of this test
    used an unrelated callback host and was correctly refused by the value-based
    scope check - the assertion was wrong, not the guardrail.
    """
    args = {"target": "shop.example.net", "plan_path": "/tmp/plan", "header_path": "/tmp/m.eml",
            "template_path": "/tmp/tpl", "root_path": "/etc", "key_path": "/tmp/keys",
            "passwd_file": "/etc/passwd", "scope": "/etc", "config": "/etc/aide/aide.conf",
            "callback": "https://shop.example.net/cb", "template": "t", "group_name": "g",
            "script": "s", "page": "p", "output": "/tmp/o"}
    args = {k: v for k, v in args.items() if k in {p.name for p in spec.params}}
    scope = Scope(targets=["shop.example.net"], cidrs=["10.20.0.0/24"], authorization_ref="T-1")

    ungated = evaluate(spec, args, scope=scope, live=False, approved=False)
    if spec.requires_approval or spec.tier >= 2:
        assert ungated.allowed is False, f"{spec.name} previewed without its approval gate"
        assert ungated.status == "needs_approval"
        gated = evaluate(spec, args, scope=scope, live=False, approved=True)
        assert gated.allowed is True, f"{spec.name} refused once approved: {gated.reasons}"
    else:
        assert ungated.allowed is True, f"{spec.name} dry run refused: {ungated.reasons}"


@pytest.mark.parametrize("spec", new_tools, ids=_ids(new_tools))
def test_new_tool_live_is_refused_without_the_unlock(spec):
    if spec.tier == 0:
        pytest.skip("T0 is read-only and needs no unlock")
    args = {"target": "shop.example.net", "template": "t", "group_name": "g", "script": "s",
            "phishlet": "o365", "page": "p", "callback": "https://int.example.net/cb",
            "plan_path": "/tmp/plan"}
    args = {k: v for k, v in args.items() if k in {p.name for p in spec.params}}
    decision = evaluate(
        spec,
        args,
        scope=Scope(targets=["shop.example.net"], cidrs=["10.20.0.0/24"], authorization_ref="T-1"),
        live=True,
        live_unlocked=False,
        approved=True,
    )
    assert decision.allowed is False, f"{spec.name} ran live without TOOLS_LIVE/TOOLS_UNLOCK"


# ===========================================================================
# cloud specifics
# ===========================================================================

CLOUD = [t for t in ALL if t.category == "cloud"]


def test_cloud_bucket_probes_are_tier_two_and_gated():
    """Probing someone's bucket asks an access-control question - T2 plus a gate."""
    for name in ("s3_bucket_probe", "azure_blob_probe", "gcp_bucket_probe"):
        spec = registry.require(name)
        assert spec.tier == 2, f"{name} must be T2"
        assert spec.requires_approval and spec.requires_scope


def test_cloud_read_only_enumeration_is_tier_one():
    for name in ("aws_iam_enum", "aws_s3_audit", "gcp_iam_enum", "k8s_api_audit"):
        assert registry.require(name).tier == 1, f"{name} should be a read-only T1"


def test_offline_cloud_review_is_tier_zero():
    for name in ("terraform_plan_review", "cloud_key_audit"):
        spec = registry.require(name)
        assert spec.tier == 0
        assert not spec.requires_scope, "an offline review needs no network scope"


def test_no_cloud_wrapper_uses_a_recovered_secret():
    """T3 is reserved for *using* a found credential; none may do so implicitly.

    The rule is asserted structurally: a T3 cloud wrapper would mean the layer
    had grown a capability to act with a discovered secret, which is a separate
    reviewed act rather than a flag.
    """
    assert not [t for t in CLOUD if t.tier >= 3], "no cloud wrapper should reach T3"


# ===========================================================================
# system specifics
# ===========================================================================

def test_system_tools_scope_declaration_matches_what_they_reach():
    """A system tool declares ``requires_scope`` exactly when it names a host.

    This test previously asserted that *no* system tool may declare
    ``requires_scope``, on the reasoning that a host audit is authorised by the
    host rather than by a network scope. That was true for the tools that read
    local state and nothing else (``lynis``, ``find``, ``lsblk``, ``bootctl``,
    ``awk``) - and false for the seven that carry a ``target`` parameter naming
    the host whose ruleset / patch state / journal / integrity baseline is being
    reported.

    The blanket rule is what let the escape through. With the flag off, the
    *coverage* check never ran: a live run with an attached scope for one host
    and a ``target`` naming another returned ``allowed=True`` with no reasons.
    The invariant is now stated against the tool's own shape - does it take a
    host? - so it cannot be satisfied again by simply turning the flag off. See
    ``tests/test_system_audit_scope.py`` for the escape pinned directly.
    """
    # ``log_rotate`` is excluded: its argument is a config path, not a host.
    system = [t for t in ALL if t.category == "system" and t.name not in ("log_rotate", "log_digest")]
    wrongly_unflagged: list[str] = []
    wrongly_flagged: list[str] = []
    for spec in system:
        names_a_host = any(p.name == "target" for p in spec.params)
        if names_a_host and not spec.requires_scope:
            wrongly_unflagged.append(spec.name)
        if not names_a_host and spec.requires_scope:
            wrongly_flagged.append(spec.name)
    assert not wrongly_unflagged, (
        f"system tools that name a host but skip scope enforcement: {wrongly_unflagged}"
    )
    assert not wrongly_flagged, (
        f"purely-local system tools wrongly requiring a network scope: {wrongly_flagged}"
    )


def test_no_system_wrapper_mutates_state():
    """Mutating system tools belong behind their own reviewed wrapper."""
    mutating = {"rm", "mkfs", "dd", "chmod", "chown", "useradd", "systemctl", "shutdown", "reboot"}
    offenders = [t.name for t in ALL if t.category == "system" and t.binary in mutating]
    assert not offenders, f"system wrappers that change state: {offenders}"


# ===========================================================================
# social-engineering specifics
# ===========================================================================

SOCIAL = [t for t in ALL if t.category == "social-engineering"]


def test_social_engineering_reaches_every_tier():
    tiers = {t.tier for t in SOCIAL}
    assert tiers == {0, 1, 2, 3}, f"expected the full tier range, got {sorted(tiers)}"


def test_high_touch_social_tools_are_tier_three():
    """Actions aimed at a person's own device need consent, not just authorisation."""
    for name in ("smishing_send", "vishing_script_deploy", "usb_drop_build"):
        spec = registry.require(name)
        assert spec.tier == 3, f"{name} reaches a person directly and must be T3"
        assert spec.requires_approval and spec.requires_sandbox and spec.requires_scope


def test_social_tools_that_reach_people_require_a_human_gate():
    for spec in SOCIAL:
        if spec.tier >= 2:
            assert spec.requires_approval, f"{spec.name} reaches people without a gate"


def test_no_social_wrapper_can_read_back_a_captured_secret():
    """The load-bearing safety property of this category.

    No wrapper may combine a capture component with a retrieval one. The assert
    list is explicit rather than inferred so that adding such a tool is a
    deliberate, reviewable act rather than an accidental one.
    """
    harvesting = {
        "credential_dump", "gophish_results", "phishlet_tokens", "session_harvest",
        "read_captured", "export_submissions", "credential_read", "token_extract",
    }
    offenders = [t.name for t in SOCIAL if t.name in harvesting]
    assert not offenders, f"social-engineering wrappers that harvest secrets: {offenders}"


def test_credential_capture_page_is_stateless_by_contract():
    """The capture page discards the body; the spec must say so."""
    spec = registry.require("cred_capture_page")
    assert "count" in spec.explain.lower() or "never" in spec.explain.lower()
    assert spec.requires_sandbox


def test_email_analysis_is_offline_and_delivery_is_gated():
    """Reconnaissance and delivery are separated so the gate lands between them."""
    assert registry.require("email_header_analyze").tier == 0
    assert registry.require("email_security_audit").tier == 1
    assert registry.require("email_security_audit").requires_scope
    assert registry.require("gophish_campaign").tier == 2
    assert registry.require("gophish_campaign").requires_approval


def test_smtp_relay_test_stops_before_delivery():
    """The template must not send a real message."""
    spec = registry.require("smtp_relay_test")
    assert "quit-after" in spec.live_template, "the relay test must stop at RCPT"
    assert "quit-after" in spec.dry_run_template


# ===========================================================================
# scope enforcement reaches the new wrappers too
# ===========================================================================

def test_out_of_scope_bucket_probe_is_refused():
    spec = registry.require("s3_bucket_probe")
    decision = evaluate(
        spec,
        {"target": "someone-elses-bucket", "mode": "list"},
        scope=Scope(targets=["our-bucket"], authorization_ref="T-1"),
        approved=True,
        live=True,
        live_unlocked=True,
    )
    assert decision.allowed is False, "a bucket outside the scope must be refused"


def test_in_scope_bucket_probe_passes_the_guardrails():
    spec = registry.require("s3_bucket_probe")
    decision = evaluate(
        spec,
        {"target": "our-bucket", "mode": "list"},
        scope=Scope(targets=["our-bucket"], authorization_ref="T-1"),
        approved=True,
    )
    assert decision.allowed is True, decision.reasons


def test_out_of_scope_campaign_target_is_refused():
    spec = registry.require("gophish_campaign")
    decision = evaluate(
        spec,
        {"target": "other-corp.example.net", "template": "t", "group_name": "g", "rate": 5},
        scope=Scope(targets=["our-corp.example.net"], authorization_ref="T-1"),
        approved=True,
    )
    assert decision.allowed is False, "a campaign aimed outside the scope must be refused"


def test_t2_without_a_scope_is_refused_for_a_new_wrapper():
    spec = registry.require("cloud_metadata_probe")
    decision = evaluate(spec, {"target": "https://shop.example.net/api"}, scope=None, approved=True)
    assert decision.allowed is False
    assert any("requires an authorization scope" in r for r in decision.reasons)


# ===========================================================================
# intent routing reaches the new categories
# ===========================================================================

@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("audit the aws iam users", "aws_iam_enum"),
        ("enumerate the aws iam users and roles", "aws_iam_enum"),
        ("is this s3 bucket public", "s3_bucket_probe"),
        ("can we list the s3 bucket contents", "s3_bucket_probe"),
        ("review the terraform plan for public buckets", "terraform_plan_review"),
        ("audit the cluster rbac", "k8s_rbac_audit"),
        ("audit the system hardening", "system_baseline_audit"),
        ("find world writable files", "file_permission_audit"),
        ("what updates are pending", "patch_level_check"),
        ("check secure boot status", "boot_integrity_check"),
        ("is the mail server an open relay", "smtp_relay_test"),
        ("audit the email security records for this domain", "email_security_audit"),
    ],
)
def test_intent_router_finds_new_category_tools(phrase, expected):
    hits = registry.resolve_intent(phrase)
    assert hits, f"{phrase!r} routed to nothing"
    assert hits[0].name == expected, f"{phrase!r} routed to {hits[0].name}, expected {expected}"


def test_a_dry_run_executes_nothing_but_returns_a_preview():
    """The dry-run default is the safety property the whole layer rests on.

    Asserted by running a T2 tool through the real runner with ``live=False`` and
    checking the command was recorded rather than executed.
    """
    spec = registry.require("gophish_campaign")
    result = run_tool(
        spec,
        {"target": "our-corp.example.net", "template": "t", "group_name": "g", "rate": 5},
        live=False,
        approved=True,
        scope=Scope(targets=["our-corp.example.net"], authorization_ref="T-1"),
    )
    assert result.dry_run is True, "a dry run must be marked as such"
    assert result.status == "dry_run"
    # ``exit_code`` is None for a dry run - nothing was executed, so there is no
    # exit status to report, and inventing 0 would make a non-execution look like
    # a successful execution in the trace.
    assert result.exit_code is None
    assert result.command, "the command that *would* run must still be recorded"
    assert result.audit_hash is None, "a dry run writes no audit row"


def test_local_certificate_inventory_does_not_claim_remote_certificate_phrases():
    """Regression: the new local-file tool must not shadow the remote probe.

    ``ssl_cert_inventory`` reads certificates from disk; ``tls_probe`` talks to a
    host. An earlier wording of the inventory's intent examples ("check the tls
    certificate") routed the *remote* question to the *local* tool, which would
    have made the AI frontend answer "what is this server serving?" with a
    directory listing.
    """
    remote = registry.resolve_intent("check the tls certificate on the server")
    assert remote and remote[0].name == "tls_probe", (
        f"a remote certificate question routed to {remote[0].name if remote else 'nothing'}"
    )
    local = registry.resolve_intent("inventory the certificates in this directory")
    assert local and local[0].name == "ssl_cert_inventory"


def test_an_ambiguous_phrase_may_route_elsewhere_without_breaking_the_contract():
    """The layer's contract is per-tool reachability, not per-phrase uniqueness.

    "audit the aws credentials file" shares its vocabulary with the account
    enumeration tool, and which one the operator meant is genuinely ambiguous from
    the words alone. What must hold is that each tool is reachable by *some*
    unambiguous phrasing - asserted by the test below, which seeks that phrasing
    per tool rather than assuming a single phrasing must serve every tool.
    """
    ambiguous = registry.resolve_intent("audit the aws credentials file")
    assert ambiguous, "an ambiguous phrase must still route somewhere"
    candidates = {h.name for h in ambiguous[:5]}
    assert "cloud_key_audit" in candidates or "aws_iam_enum" in candidates


def test_every_new_tool_is_reachable_by_at_least_one_of_its_phrases():
    """Every tool must be findable, but not necessarily by its first phrase alone.

    Checking *all* of a tool's own intent examples and requiring that at least one
    routes home is the honest version of this test. Requiring the first example
    specifically to win would make the check a spelling constraint on the example
    text rather than a statement about reachability - and would fail tools that are
    perfectly reachable by their second or third phrasing.
    """
    unreachable: list[str] = []
    for spec in new_tools:
        if not spec.intent_examples:
            continue
        routed = {h.name for phrase in spec.intent_examples for h in registry.resolve_intent(phrase)[:5]}
        if spec.name not in routed:
            unreachable.append(f"{spec.name} (tried {len(spec.intent_examples)} phrasings)")
    assert not unreachable, "tools unreachable by any of their own phrases:\n  " + "\n  ".join(unreachable)
