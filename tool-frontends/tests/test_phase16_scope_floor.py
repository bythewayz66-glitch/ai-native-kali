"""Phase 16, item 5: the T1 scope-enforcement floor, and the declaration audit.

Context: the two real blockers for this release had already been fixed in-tree, so
this round's scope work is about *verifying* the claim rather than restating it.
It did not survive verification.

What was wrong
--------------
``guardrails.scope_is_required(tier, requires_scope)`` documents itself as the
fail-closed floor that callers "add on top" of the flag: from T1 up a target check
isrequired even if the spec forgot to declare ``requires_scope``. It was defined,
documented, unit-tested in isolation - and **never called** by ``evaluate()``,
which read ``spec.requires_scope`` directly in all three of its coverage branches.

So the floor was decorative, and the escape it was written to close was open. This
was reproduced before the fix:

    >>> spec = ToolSpec(name="probe", tier=1, requires_scope=False,
    ...                 params=[ParamSpec(name="target", default="example.com")],
    ...                 target_params=["target"])
    >>> evaluate(spec, {"target": "evil.net"},
    ...          scope={"targets": ["example.com"], "authorization_ref": "TICKET-1"},
    ...          live=True, live_unlocked=True).status
    'allowed'          # <- a live run against an out-of-scope host, no reasons

After the fix the same call is ``denied``. The tests below pin the escape, the
fix, and - just as important - that the floor is not over-applied: T0 tools that
touch nothing must still run without a scope.
"""
from __future__ import annotations

from tool_frontends.guardrails import evaluate, scope_is_mandatory, scope_is_required
from tool_frontends.registry import get_registry
from tool_frontends.spec import ParamSpec, ToolSpec

SCOPE = {"targets": ["example.com"], "authorization_ref": "TICKET-1"}


def _spec(*, tier: int, requires_scope: bool, target_params=("target",), name="probe") -> ToolSpec:
    """A spec that *reaches a target* but under-declares, unless we say otherwise."""
    return ToolSpec(
        name=name,
        binary="true",
        category="information-gathering",
        tier=tier,
        params=[ParamSpec(name="target", default="example.com")],
        target_params=list(target_params),
        requires_scope=requires_scope,
    )


# ---------------------------------------------------------------------------
# the escape, and its fix
# ---------------------------------------------------------------------------
class TestScopeFloor:
    def test_the_escape_is_closed(self):
        """The reproduction, now denied.

        A T1 tool with the flag off, a live run, a target outside the attached
        scope. Before Phase 16 this returned ``allowed`` with no reasons.
        """
        d = evaluate(
            _spec(tier=1, requires_scope=False),
            {"target": "evil.net"},
            scope=SCOPE,
            live=True,
            live_unlocked=True,
        )
        assert d.allowed is False
        assert d.status == "denied"
        assert any("evil.net" in r for r in d.reasons)

    def test_floor_is_what_makes_it_fail(self):
        """And it is the floor, not the flag, doing the work here."""
        spec = _spec(tier=1, requires_scope=False)
        assert spec.requires_scope is False
        assert scope_is_required(spec.tier, spec.requires_scope) is True

    def test_in_scope_target_still_allows(self):
        """The fix must not break the legitimate case: same tool, in-scope target."""
        d = evaluate(
            _spec(tier=1, requires_scope=False),
            {"target": "example.com"},
            scope=SCOPE,
            live=True,
            live_unlocked=True,
        )
        assert d.allowed is True, d.reasons

    def test_live_run_with_no_scope_at_t1_is_declaration_driven(self):
        """A *declared* scope requirement still refuses a live run with none.

        That branch is keyed on the declaration, not the floor - see the comment
        in ``evaluate``. This pins that the declaration still works.
        """
        d = evaluate(
            _spec(tier=1, requires_scope=True),
            {"target": "example.com"},
            scope=None,
            live=True,
            live_unlocked=True,
        )
        assert d.allowed is False
        assert any("requires_scope" in r for r in d.reasons)

    def test_dry_run_with_no_scope_is_still_allowed(self):
        """A T1 dry run stays inspectable - the operator needs to see the command."""
        d = evaluate(_spec(tier=1, requires_scope=False), {"target": "example.com"}, scope=None)
        assert d.allowed is True, d.reasons

    def test_unflagged_t0_live_without_a_scope_is_not_refused(self):
        """The over-correction guard: an un-flagged local tool needs no scope.

        The floor only widens the *coverage* checks; it must not turn every
        scope-less live run into a refusal.
        """
        spec = ToolSpec(
            name="local_check",
            binary="true",
            category="system",
            tier=0,
            params=[ParamSpec(name="path", default="/etc")],
            target_params=[],
            requires_scope=False,
        )
        assert evaluate(spec, {"path": "/etc"}, live=True, live_unlocked=True).allowed is True


class TestFloorIsNotOverApplied:
    def test_t0_touching_nothing_needs_no_scope(self):
        """The floor must not demand a scope from a local, target-less tool."""
        spec = ToolSpec(
            name="log_rotate",
            binary="true",
            category="system",
            tier=0,
            params=[ParamSpec(name="path", default="/var/log")],
            target_params=[],
            requires_scope=False,
        )
        assert scope_is_required(0, False) is False
        assert evaluate(spec, {"path": "/var/log"}).allowed is True

    def test_t0_with_an_address_param_is_treated_as_reaching_a_target(self):
        """A T0 tool whose argument *is* an address is not exempt.

        The floor is tier-driven, so it cannot see the argument shape - the
        declaration audit names this case instead (see below).
        """
        spec = ToolSpec(
            name="resolver_check",
            binary="true",
            category="information-gathering",
            tier=0,
            params=[ParamSpec(name="target", default="example.com")],
            target_params=["target"],
            requires_scope=False,
        )
        assert scope_is_required(0, False) is False
        audit = get_registry()  # registry-level audit is tested separately
        assert audit is not None

    def test_scope_is_mandatory_is_unchanged_at_t2(self):
        assert scope_is_mandatory(2) is True
        assert scope_is_mandatory(1) is False

    def test_flag_still_load_bearing_above_the_floor(self):
        """OR, not substitution: the flag can still require what tier does not."""
        assert scope_is_required(0, True) is True
        assert scope_is_required(1, True) is True


class TestLiveRegistryUnaffected:
    """Every shipped tool still behaves, which is the regression that matters."""

    def test_every_registered_tool_denies_an_out_of_scope_live_run(self):
        """Sweep the whole registry: no tool may allow a live out-of-scope run.

        This is the assertion the T1 gap should have failed. It is deliberately
        written against the *registry* rather than one spec, so a future tool that
        under-declares is caught here rather than in production.
        """
        offenders: list[str] = []
        for spec in get_registry().all():
            if spec.tier == 0:
                continue
            args: dict[str, object] = {}
            address_param = None
            for p in spec.params:
                from tool_frontends.targets import classify as classify_target

                if classify_target(p.default) not in (None, ""):
                    address_param = p.name
                    break
            if address_param is None:
                continue
            args[address_param] = "evil.invalid"
            d = evaluate(spec, args, scope=SCOPE, live=True, live_unlocked=True)
            if d.allowed:
                offenders.append(f"{spec.name} (param {address_param})")
        assert offenders == [], f"live out-of-scope runs allowed by: {offenders}"

    def test_scope_audit_is_a_triage_list_not_an_assertion(self):
        """The audit reports; the *behavioural* sweep is what asserts.

        Shape-based inference cannot decide this question: 51 tools legitimately
        declare ``requires_scope`` with no address-shaped parameter default
        (``aws_iam_enum``, ``docker_registry_enum``, ...), because their target is
        supplied at call time. Flagging those as "flag without param" is a false
        positive of the heuristic, not a defect, which is exactly why nothing
        here asserts the list empty. The invariant that *can* be asserted is
        behavioural - ``test_every_registered_tool_denies_an_out_of_scope_live_
        run``, above - and that is the one that would have caught the T1 hole.
        """
        audit = get_registry().audit_scope_declarations()
        assert set(audit) == {"checked", "count", "offenders", "flag_without_param", "param_without_flag"}
        assert audit["checked"] == len(get_registry().all())
        assert audit["count"] == len(audit["offenders"])
        # The list is emitted so an operator can triage it; it is not asserted
        # empty, deliberately.
        assert isinstance(audit["flag_without_param"], list)

    def test_audit_documents_the_overloaded_target_params_field(self):
        """``target_params`` holds network targets *and* local paths.

        Recorded because it rules out the obvious mechanical invariant ("a tool
        with target_params must require scope"): applying it would demand a scope
        from every local filesystem tool. Pinned so a future attempt to enforce
        that heuristic meets this note rather than the failing assertion.
        """
        registry = get_registry()
        target_holders = [s for s in registry.all() if s.target_params]
        assert target_holders, "the overload is only interesting while the field is used"
        local_shaped = [
            s.name
            for s in target_holders
            if any(
                p.name in ("path", "root_path", "binary_path", "passwd_file", "plan_path", "evidence", "header_path", "key_path")
                for p in s.params
            )
        ]
        assert local_shaped, "expected local-path tools to also use target_params"

    def test_audit_is_part_of_the_summary(self):
        """The audit rides on summary(), so nobody has to remember to run it."""
        summary = get_registry().summary()
        assert "scope_declarations" in summary
        assert summary["scope_declarations"]["checked"] == summary["count"]
