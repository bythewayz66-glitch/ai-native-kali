"""Phase 17 - tool footprints (effects), the capability manifest, and the checks
that keep a declared footprint honest.

These tests are written against the *contract* rather than against a snapshot:
each one states a fact that must hold for any registry, so adding a tool or a
wrapper cannot quietly make a check vacuous. Two of them are regression guards
for defects found while building this feature:

* the inference must not read ``-o json`` as a file write (the over-catch that
  broke 11 existing tests), and
* ``audit_effects().suggested_mutators`` must read the *inferred* set, because
  reading the enforced one made the list permanently empty.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tool_frontends.audit import ToolAuditLog
from tool_frontends.capabilities import build_manifest, summarise
from tool_frontends.effects import (
    EFFECTS,
    MUTATING_EFFECTS,
    SENSITIVE_EFFECTS,
    describe,
    infer_effects,
    is_mutating,
    normalize,
    reaches_network,
    touches_secrets,
    unknown,
)
from tool_frontends.guardrails import evaluate
from tool_frontends.registry import ToolRegistry, get_registry
from tool_frontends.spec import ParamSpec, ToolSpec


def _spec(**kwargs) -> ToolSpec:
    base = dict(
        name="probe_tool",
        binary="true",
        category="system",
        tier=1,
        description="a test tool",
        params=[ParamSpec(name="target", type="string")],
        dry_run_template="true {target}",
        live_template="true {target}",
    )
    base.update(kwargs)
    return ToolSpec(**base)


# --------------------------------------------------------------- vocabulary
class TestVocabulary:
    def test_vocabulary_is_closed_and_unique(self):
        assert len(EFFECTS) == len(set(EFFECTS))
        for e in EFFECTS:
            assert e == e.strip().lower() and " " not in e, f"effect '{e}' must be a bare lowercase token"
        # Effects name a verb and an object, except two that name a *class*: the
        # data class ``secrets`` and the state class ``persist``. That pair is
        # asserted exactly, so a third un-namespaced effect cannot be added by
        # accident (it would be an addition to the closed set, which is a
        # deliberate act and should require editing this assertion).
        unnamespaced = {e for e in EFFECTS if "." not in e}
        assert unnamespaced == {"secrets", "persist"}, unnamespaced

    def test_mutating_and_sensitive_sets_are_subsets_of_the_vocabulary(self):
        assert MUTATING_EFFECTS <= set(EFFECTS)
        assert SENSITIVE_EFFECTS <= set(EFFECTS)
        # A mutating effect that also counted as sensitive would make the two
        # gates overlap in a way neither documents.
        assert not (MUTATING_EFFECTS & SENSITIVE_EFFECTS)

    def test_unknown_effects_are_reported_not_dropped(self):
        assert unknown(["fs.write", "teleport", "fs.write"]) == ["teleport"]

    def test_normalize_orders_dedups_and_drops_unknown(self):
        assert normalize(["fs.write", "fs.read", "fs.write", "nope"]) == ["fs.read", "fs.write"]

    def test_predicates_split_the_axes_correctly(self):
        assert is_mutating(["fs.write"])
        assert not is_mutating(["fs.read", "net.outbound"])
        assert touches_secrets(["secrets"])
        assert reaches_network(["net.outbound"])
        assert not reaches_network(["fs.read"])


# --------------------------------------------------------------- inference
class TestInference:
    def test_infers_outbound_for_a_scoped_or_t1_tool(self):
        assert "net.outbound" in infer_effects(binary="nmap", tier=1)

    def test_infers_fs_write_for_a_known_mutating_binary(self):
        assert "fs.write" in infer_effects(binary="logrotate", tier=0)

    def test_infers_local_read_only_binary(self):
        assert infer_effects(binary="journalctl", tier=0) == ["fs.read"]

    def test_makes_no_claim_about_an_unknown_plain_binary(self):
        assert infer_effects(binary="some-unknown-thing", tier=0) == []

    def test_write_hint_is_read_from_the_live_template_only(self):
        # A dry-run template that contains '>' is *describing* a write.
        dry = infer_effects(binary="x", tier=0, dry_run_template="echo a > b")
        live = infer_effects(binary="x", tier=0, live_template="echo a > b")
        assert "fs.write" not in dry
        assert "fs.write" in live

    def test_output_format_flag_is_not_a_write(self):
        """Regression: ``-o json`` is a format, not a file.

        Reading it as a write flagged ``kubectl``/``sysctl`` as mutators and
        denied 11 read-only tools, which is why enforcement is declared-only and
        the suggestion table reads this same function.
        """
        assert "fs.write" not in infer_effects(
            binary="kubectl", tier=0, live_template="kubectl get secrets -o json"
        )


# --------------------------------------------------------------- spec
class TestSpecFootprint:
    def test_a_declared_footprint_is_enforced_verbatim(self):
        spec = _spec(effects=["proc.spawn", "fs.write"])
        assert spec.effects_declared is True
        assert spec.enforced_effects() == ["fs.write", "proc.spawn"]

    def test_an_undeclared_footprint_enforces_nothing(self):
        spec = _spec(effects=[])
        assert spec.effects_declared is False
        assert spec.enforced_effects() == []

    def test_inference_never_leaks_into_enforcement(self):
        # tier=1 + a mutating binary would infer fs.write; enforcement must stay
        # empty because nothing was declared. That is what makes the gate
        # deterministic instead of heuristic-driven.
        spec = _spec(binary="logrotate", tier=1, effects=[])
        assert "fs.write" in spec.inferred_effects()
        assert spec.enforced_effects() == []

    def test_effect_report_exposes_both_sets_and_says_which_is_enforced(self):
        report = _spec(binary="logrotate", tier=1, effects=[]).effect_report()
        assert report["declared"] is False
        assert report["enforced"] is False
        assert report["effects"] == []
        assert "fs.write" in report["inferred"]
        assert report["mutating"] is False  # enforced set, not inferred


# --------------------------------------------------------------- registry
class TestRegistryFootprint:
    def test_register_rejects_an_unknown_effect(self):
        reg = ToolRegistry()
        with pytest.raises(ValueError) as exc:
            reg.register(_spec(name="bad_effect", effects=["fs.write", "teleport"]))
        assert "teleport" in str(exc.value)

    def test_register_accepts_a_known_effect(self):
        reg = ToolRegistry()
        reg.register(_spec(name="ok_effect", effects=["net.outbound"]))
        assert reg.get("ok_effect").enforced_effects() == ["net.outbound"]

    def test_builtin_registry_reports_the_migration_backlog(self):
        audit = get_registry().audit_effects()
        assert audit["checked"] == len(get_registry().all())
        assert audit["declared"] + audit["inferred"] == audit["checked"]
        assert "log_rotate" in audit["declared_tools"]

    def test_suggested_mutators_reads_the_inferred_set(self):
        """Regression: the recommendation must not read the enforced (empty) set."""
        audit = get_registry().audit_effects()
        # Every suggested mutator is undeclared by definition (declared ones are
        # not candidates), and each one really does infer a mutating effect.
        for name in audit["suggested_mutators"]:
            spec = get_registry().get(name)
            assert spec.effects_declared is False
            assert is_mutating(spec.inferred_effects())

    def test_summary_carries_the_effects_block(self):
        effects = get_registry().summary()["effects"]
        assert set(effects) == {"checked", "declared", "inferred"}


# --------------------------------------------------------------- manifest
class TestCapabilityManifest:
    def test_manifest_has_the_sections_an_operator_reads(self):
        manifest = build_manifest(get_registry())
        for key in ("version", "totals", "effects_vocabulary", "footprint", "gaps", "tools"):
            assert key in manifest
        assert manifest["effects_vocabulary"] == list(EFFECTS)

    def test_manifest_is_derived_from_the_same_specs(self):
        manifest = build_manifest(get_registry())
        assert manifest["totals"]["tools"] == len(get_registry().all())
        names = {t["name"] for t in manifest["tools"]}
        assert names == {s.name for s in get_registry().all()}

    def test_no_registered_mutator_is_unsandboxed(self):
        """The invariant the guardrail floor exists to keep.

        A registered tool that declares a mutating effect must carry the sandbox
        flag, so the manifest's ``mutating_without_sandbox`` gap has to be empty.
        If this fails, a spec was edited to drop the flag and the footprint floor
        is the only thing still refusing its runs.
        """
        gaps = build_manifest(get_registry())["gaps"]
        assert gaps["mutating_without_sandbox_count"] == 0, gaps["mutating_without_sandbox"]

    def test_manifest_summary_is_a_single_line(self):
        line = summarise(build_manifest(get_registry()))
        assert line.count("\n") == 0
        assert "tools" in line


# --------------------------------------------------------------- guardrails
class TestFootprintFloor:
    def test_a_mutating_t1_tool_without_the_sandbox_is_refused(self):
        spec = _spec(name="mutator", tier=1, effects=["fs.write"], requires_sandbox=False)
        decision = evaluate(spec, {"target": "example.com"})
        assert decision.allowed is False
        assert any("mutating effects" in r for r in decision.reasons)

    def test_the_sandbox_flag_clears_that_specific_reason(self):
        spec = _spec(name="mutator", tier=1, effects=["fs.write"], requires_sandbox=True)
        decision = evaluate(spec, {"target": "example.com"})
        assert not any("mutating effects" in r for r in decision.reasons)

    def test_a_t0_mutator_is_below_the_floor(self):
        # T0 is the local-maintenance tier; the floor deliberately starts at T1,
        # because a T0 tool cannot reach a target regardless of its local writes.
        spec = _spec(name="t0_mutator", tier=0, effects=["fs.write"], requires_sandbox=False)
        decision = evaluate(spec, {})
        assert not any("mutating effects" in r for r in decision.reasons)

    def test_an_undeclared_tool_is_not_denied_by_the_heuristic(self):
        spec = _spec(name="undeclared", binary="logrotate", tier=1, effects=[], requires_sandbox=False)
        decision = evaluate(spec, {"target": "example.com"})
        assert not any("mutating effects" in r for r in decision.reasons)

    def test_the_effective_footprint_is_recorded_on_the_decision(self):
        decision = evaluate(_spec(effects=["net.outbound"]), {"target": "example.com"})
        assert decision.checks["effects"] == ["net.outbound"]
        assert decision.checks["effects_declared"] is True


# --------------------------------------------------------------- audit
class TestAuditFootprint:
    def test_the_footprint_is_written_into_the_row(self):
        log = ToolAuditLog(":memory:")
        log.append(
            ts="2026-01-01T00:00:00Z", tool="log_rotate", tier=0, status="dry_run",
            dry_run=True, effects=["fs.write"],
        )
        assert log.list()[0]["effects"] == ["fs.write"]
        log.close()

    def test_chain_verifies_with_the_footprint_present(self):
        log = ToolAuditLog(":memory:")
        for i in range(3):
            log.append(
                ts=f"2026-01-01T00:00:0{i}Z", tool="t", tier=1, status="ok",
                dry_run=False, effects=["net.outbound"],
            )
        assert log.verify_chain()["ok"] is True
        log.close()

    def test_tampering_with_the_footprint_breaks_the_chain(self):
        log = ToolAuditLog(":memory:")
        log.append(
            ts="2026-01-01T00:00:00Z", tool="t", tier=1, status="ok",
            dry_run=False, effects=["fs.read"],
        )
        log._conn.execute("UPDATE tool_audit SET effects=? WHERE seq=1", ('["fs.write"]',))
        log._conn.commit()
        verdict = log.verify_chain()
        assert verdict["ok"] is False
        assert verdict["broken_at_seq"] == 1
        log.close()

    def test_a_pre_phase17_database_gains_the_column_and_still_verifies(self, tmp_path: Path):
        """Migration path: an on-disk DB written before the column existed.

        The old rows were hashed without an effects field, so ``verify_chain``
        must include the field only when it is non-NULL - otherwise opening an
        older log would report its own untouched history as tampered with.
        """
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE tool_audit (
                 seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, tool TEXT NOT NULL,
                 tier INTEGER NOT NULL, caller TEXT, card_id TEXT, target TEXT,
                 status TEXT NOT NULL, dry_run INTEGER NOT NULL, decision TEXT, command TEXT,
                 duration_ms INTEGER DEFAULT 0, exit_code INTEGER, args TEXT, reasons TEXT,
                 prev_hash TEXT NOT NULL, hash TEXT NOT NULL)"""
        )
        conn.commit()
        conn.close()

        log = ToolAuditLog(db)
        cols = {r[1] for r in log._conn.execute("PRAGMA table_info(tool_audit)")}
        assert "effects" in cols
        log.append(
            ts="2026-01-01T00:00:00Z", tool="t", tier=0, status="dry_run",
            dry_run=True, effects=["fs.read"],
        )
        assert log.verify_chain()["ok"] is True
        assert log.list()[0]["effects"] == ["fs.read"]
        log.close()


# --------------------------------------------------------------- describe()
def test_describe_is_the_public_shape_of_a_footprint():
    report = describe(["fs.write", "secrets"], declared=True)
    assert report == {
        "effects": ["fs.write", "secrets"],
        "declared": True,
        "mutating": True,
        "sensitive": True,
        "network": False,
    }
