"""Item 6 - T1 scope enforcement, and the crew/role boundary that hid it.

The audit that produced this file (``scripts/scope_boundary_audit.py``) found
two things the existing tests did not:

1. **Six T1 system tools declared ``requires_scope=False`` while taking a host
   in their ``target`` parameter.** ``scope_is_required(1, False)`` is ``True`` -
   a tier-1 *live* run with no scope was already refused - but that is the
   *mandatory* half. The *coverage* half only runs when the spec declares
   ``requires_scope``, so a live run with an attached scope for ``example.com``
   and ``target=evil.net`` returned ``allowed=True`` with ``reasons=[]``. The
   six are ``firewall_audit``, ``audit_policy_check``, ``patch_level_check``,
   ``service_exposure_check``, ``kernel_hardening_check`` and ``log_forensics``.

2. **``integrity_baseline`` declared ``target_params=["config"]``** - a *file
   path*. ``target_value()`` therefore returned ``/etc/aide/aide.conf`` and the
   scope checks compared a filesystem path against a host scope, which is a
   comparison that can never mean anything.

3. **``orchestrator`` bound a tool, ``log_digest``, that was not registered.**
   A role that binds a name nothing defines is a role whose authority silently
   stops being real - and it is the same shape as the tier-vs-flag confusion:
   a declaration that no engine actually enforces.

Each is pinned here so a later edit cannot quietly restore it.
"""
from __future__ import annotations

from tool_frontends.guardrails import evaluate, scope_is_mandatory, scope_is_required
from tool_frontends.registry import get_registry
from tool_frontends.runner import run_tool

from agent_runtime.roles import ROLE_REGISTRY

#: The six host-reaching T1 system tools the audit found.
HOST_REACHING_SYSTEM_TOOLS = (
    "firewall_audit",
    "audit_policy_check",
    "patch_level_check",
    "service_exposure_check",
    "kernel_hardening_check",
    "log_forensics",
)

#: The local-file forensics/RE tools whose argument is a path to an *artifact*.
ARTIFACT_PATH_TOOLS = ("binwalk_extract", "volatility_pslist", "r2_analyze")

_SCOPE = {"targets": ["example.com"], "authorization_ref": "AUDIT-1"}


class TestHostReachingSystemToolsDeclareScope:
    def test_the_six_declare_requires_scope(self):
        registry = get_registry()
        missing = [n for n in HOST_REACHING_SYSTEM_TOOLS if not registry.require(n).requires_scope]
        assert not missing, f"host-reaching T1 tools with requires_scope=False: {missing}"

    def test_each_is_tier_1(self):
        registry = get_registry()
        wrong = {n: registry.require(n).tier for n in HOST_REACHING_SYSTEM_TOOLS}
        assert all(t == 1 for t in wrong.values()), wrong

    def test_each_declares_target_in_target_params(self):
        registry = get_registry()
        missing = [n for n in HOST_REACHING_SYSTEM_TOOLS if "target" not in registry.require(n).target_params]
        assert not missing, f"tools not declaring 'target': {missing}"


class TestCoverageNowAppliesAtT1:
    """The escape itself: an attached scope must cover the named target."""

    def test_live_out_of_scope_target_is_refused_for_every_six(self):
        registry = get_registry()
        offenders: list[str] = []
        for name in HOST_REACHING_SYSTEM_TOOLS:
            decision = evaluate(
                registry.require(name),
                {"target": "evil.net"},
                scope=_SCOPE,
                live=True,
                live_unlocked=True,
            )
            if decision.allowed:
                offenders.append(f"{name}: allowed={decision.allowed} reasons={decision.reasons}")
            elif not any("outside the authorized scope" in r for r in decision.reasons):
                offenders.append(f"{name}: refused for the wrong reason: {decision.reasons}")
        assert not offenders, "T1 coverage escape:\n  " + "\n  ".join(offenders)

    def test_live_out_of_scope_target_is_refused_end_to_end(self):
        """The same check through the real runner, not just the engine."""
        registry = get_registry()
        offenders: list[str] = []
        for name in HOST_REACHING_SYSTEM_TOOLS:
            result = run_tool(
                registry.require(name),
                {"target": "evil.net"},
                scope=_SCOPE,
                approved=True,
                live=True,
                live_unlocked=True,
                caller="test",
            )
            if result.status != "denied":
                offenders.append(f"{name}: live out-of-scope run returned {result.status}")
        assert not offenders, offenders

    def test_live_in_scope_target_is_still_reachable(self):
        """The fix must not over-correct: an in-scope live run still gets through."""
        registry = get_registry()
        decision = evaluate(
            registry.require("patch_level_check"),
            {"target": "example.com"},
            scope=_SCOPE,
            live=True,
            live_unlocked=True,
        )
        assert decision.allowed is True, decision.reasons

    def test_live_without_any_scope_is_refused(self):
        registry = get_registry()
        offenders: list[str] = []
        for name in HOST_REACHING_SYSTEM_TOOLS:
            decision = evaluate(
                registry.require(name), {"target": "example.com"}, scope=None, live=True, live_unlocked=True
            )
            if decision.allowed:
                offenders.append(name)
        assert not offenders, f"live with no scope allowed for: {offenders}"

    def test_dry_run_without_scope_stays_visible(self):
        """The operator may always see what would run."""
        registry = get_registry()
        for name in HOST_REACHING_SYSTEM_TOOLS:
            decision = evaluate(registry.require(name), {"target": "example.com"}, scope=None, live=False)
            assert decision.allowed is True, f"{name}: {decision.reasons}"


class TestIntegrityBaselineIsNotAFilePathTarget:
    def test_declares_a_host_not_a_config_path(self):
        spec = get_registry().require("integrity_baseline")
        assert spec.target_params == ["target"]
        assert "config" not in [p.name for p in spec.params]
        assert spec.requires_scope is True

    def test_default_target_is_not_address_shaped(self):
        """A default that classifies as a host would be refused for the wrong reason."""
        from tool_frontends.targets import classify

        spec = get_registry().require("integrity_baseline")
        default = spec.target_value(spec.defaults())
        # 'localhost' is not an FQDN by the classifier, so the declared check
        # handles it; the point is that it is not a *path* any more.
        assert default == "localhost"
        assert classify(default) is None

    def test_out_of_scope_host_is_refused(self):
        decision = evaluate(
            get_registry().require("integrity_baseline"),
            {"target": "evil.net"},
            scope=_SCOPE,
            live=True,
            live_unlocked=True,
        )
        assert decision.allowed is False
        assert any("outside the authorized scope" in r for r in decision.reasons)


class TestArtifactPathToolsHaveTheirDeclaredTargetEnforced:
    """These take a path to a forensic artifact; the artifact reaches a host."""

    def test_each_declares_requires_scope(self):
        registry = get_registry()
        missing = [n for n in ARTIFACT_PATH_TOOLS if not registry.require(n).requires_scope]
        assert not missing, f"artifact-path tools without scope enforcement: {missing}"

    def test_undeclared_target_is_refused(self):
        registry = get_registry()
        offenders: list[str] = []
        for name in ARTIFACT_PATH_TOOLS:
            spec = registry.require(name)
            key = spec.target_params[0]
            decision = evaluate(
                spec, {key: "evil.net"}, scope=_SCOPE, live=True, live_unlocked=True
            )
            if decision.allowed:
                offenders.append(name)
        assert not offenders, offenders


class TestTierFlagContractStillHolds:
    """The rule from Phase 6 must survive this change."""

    def test_flag_governs_and_tier_is_a_floor(self):
        for tier in (0, 1, 2, 3):
            assert scope_is_required(tier, True) is True
        assert scope_is_required(1, False) is True  # fail-closed floor
        assert scope_is_required(0, False) is False  # not over-corrected
        assert scope_is_mandatory(2) is True
        assert scope_is_mandatory(1) is False


class TestOrchestratorToolIsReal:
    """A role may not bind a tool name the registry does not define."""

    def test_log_digest_is_registered(self):
        assert get_registry().get("log_digest") is not None, (
            "the orchestrator role binds log_digest; a dangling name means the "
            "role's authority is a declaration nothing enforces"
        )

    def test_log_digest_is_t0_and_local(self):
        spec = get_registry().require("log_digest")
        assert spec.tier == 0
        assert spec.requires_scope is False

    def test_every_role_tool_resolves_and_respects_its_ceiling(self):
        registry = get_registry()
        problems: list[str] = []
        for name, role in ROLE_REGISTRY.items():
            for tool in role.tools:
                spec = registry.get(tool)
                if spec is None:
                    problems.append(f"role '{name}' binds unknown tool '{tool}'")
                    continue
                if spec.tier > role.max_tier:
                    problems.append(
                        f"role '{name}' (ceiling T{role.max_tier}) binds '{tool}' (T{spec.tier})"
                    )
        assert not problems, "\n  ".join(problems)

    def test_orchestrator_ceiling_allows_its_tool(self):
        orch = ROLE_REGISTRY["orchestrator"]
        spec = get_registry().require("log_digest")
        assert orch.may_use("log_digest", spec.tier)
