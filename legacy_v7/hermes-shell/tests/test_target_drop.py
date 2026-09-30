"""Target drag-and-drop tests (Phase 7, item 2).

The load-bearing claims here are security claims, not UI claims:

* every address shape a user can drop is recognised - via the **shared**
  classifier, so the drag handler and the guardrail engine cannot disagree;
* a non-target payload is refused outright rather than wrapped in a scope;
* the scope the created card carries is the one the engine's matching rules will
  actually accept (host for a URL, ``cidrs`` for a CIDR);
* the drop decision lands in the hash-chained tool audit log, refusals included.
"""
from __future__ import annotations

import pytest

from hermes_shell.target_drop import (
    DROP_TARGET_KINDS,
    audit_drop,
    card_payload,
    plan_drop,
    scope_for,
    scope_payload,
    title_for,
)

# (payload, expected kind, expected normalized value)
SHAPES = [
    ("10.10.0.5", "ipv4", "10.10.0.5"),
    ("10.10.0.0/24", "cidr", "10.10.0.0/24"),
    ("shop.example.net", "hostname", "shop.example.net"),
    ("https://shop.example.net/login?x=1", "url", "shop.example.net"),
    ("aa:bb:cc:dd:ee:ff", "mac", "aa:bb:cc:dd:ee:ff"),
    ("mail.example.net:25", "hostname", "mail.example.net"),
]


class TestAcceptedShapes:
    @pytest.mark.parametrize("payload,kind,normalized", SHAPES)
    def test_each_address_shape_is_accepted(self, payload, kind, normalized):
        plan = plan_drop(payload, board_id="brd_agent")
        assert plan.accepted, plan.reason
        assert plan.kind == kind
        assert plan.target == normalized

    @pytest.mark.parametrize("payload,kind,normalized", SHAPES)
    def test_accepted_drops_all_stay_inside_the_declared_kind_set(self, payload, kind, normalized):
        plan = plan_drop(payload)
        assert plan.kind in DROP_TARGET_KINDS
        assert plan.target == normalized

    def test_ipv6_is_accepted(self):
        plan = plan_drop("2001:db8::1")
        assert plan.accepted and plan.kind == "ipv6"

    def test_a_tor_hidden_service_is_accepted(self):
        plan = plan_drop("expyuzz4wqqyqhjn.onion")
        assert plan.accepted and plan.kind == "onion"

    def test_a_host_with_a_port_is_classified_as_a_hostname(self):
        # The shared classifier strips :port before matching the host pattern,
        # so a scan argument like mail.example.net:25 scopes to the host.
        plan = plan_drop("mail.example.net:25")
        assert plan.kind == "hostname"
        assert plan.scope_targets == ["mail.example.net"]

    def test_a_bssid_is_classified_as_a_mac_by_the_shared_classifier(self):
        # The classifier has no separate bssid kind; recording the real one
        # keeps the drop in step with what the engine will match on.
        plan = plan_drop("aa:bb:cc:11:22:33")
        assert plan.kind == "mac"

    @pytest.mark.parametrize("payload,kind,_n", SHAPES)
    def test_the_classifier_is_the_shared_one(self, payload, kind, _n):
        # If this import ever stops resolving to the engine's classifier, this
        # test fails - which is the point: a second detector drifting from the
        # enforced one is the defect class this repo keeps fixing.
        from tool_frontends.targets import classify

        assert classify(payload) == kind


class TestRejectedPayloads:
    @pytest.mark.parametrize(
        "payload",
        [
            "",
            "   ",
            None,
            "a note about the engagement",
            "rockyou.txt",
            "/etc/passwd",
            "./wordlist.lst",
            "%%%not-a-target%%%",
            "hello world please scan this host",
        ],
    )
    def test_a_non_target_is_refused(self, payload):
        plan = plan_drop(payload, board_id="brd_agent")
        assert plan.accepted is False
        assert plan.reason

    def test_a_refused_drop_carries_no_scope(self):
        plan = plan_drop("just some prose")
        assert plan.accepted is False
        assert plan.scope_targets == [] and plan.scope_cidrs == []

    def test_a_refused_drop_cannot_be_built_into_a_card(self):
        plan = plan_drop("not a target at all")
        with pytest.raises(ValueError):
            card_payload(plan)

    def test_an_email_is_refused_because_no_tool_takes_it_as_a_target(self):
        # A real target *kind* for tool parameters, but not for scoping a card.
        plan = plan_drop("analyst@example.net")
        assert plan.accepted is False
        assert "cannot scope a card" in plan.reason or "not a target" in plan.reason

    def test_a_dict_without_a_payload_is_refused(self):
        plan = plan_drop({"board_id": "brd_agent"})
        assert plan.accepted is False
        assert "no payload" in plan.reason


class TestScopeShape:
    def test_a_cidr_goes_into_the_cidrs_field(self):
        targets, cidrs = scope_for("cidr", "10.10.0.0/24")
        assert targets == [] and cidrs == ["10.10.0.0/24"]

    def test_an_address_goes_into_targets(self):
        targets, cidrs = scope_for("ipv4", "10.10.0.5")
        assert targets == ["10.10.0.5"] and cidrs == []

    def test_a_url_scopes_to_its_host(self):
        plan = plan_drop("https://shop.example.net/login")
        assert plan.scope_targets == ["shop.example.net"]
        assert plan.scope_cidrs == []
        # The raw URL must not be the scope: the engine matches on host.
        assert "shop.example.net" in plan.scope_targets

    def test_a_host_with_a_port_scopes_to_the_host(self):
        plan = plan_drop("mail.example.net:25")
        assert plan.scope_targets == ["mail.example.net"]
        assert plan.scope_cidrs == []

    def test_the_created_card_scope_is_accepted_by_the_engine_model(self):
        # The real check: the scope we hand the board must parse and cover the
        # target under the same model the guardrail engine uses.
        from kanban_core.models import Scope

        for payload, _kind, normalized in SHAPES:
            plan = plan_drop(payload, board_id="brd_agent")
            scope = Scope.model_validate(scope_payload(plan))
            assert scope.covers(normalized), f"scope from {payload!r} does not cover {normalized!r}"

    def test_a_cidr_scope_covers_an_address_inside_it(self):
        from kanban_core.models import Scope

        plan = plan_drop("10.10.0.0/24")
        scope = Scope.model_validate(scope_payload(plan))
        assert scope.covers("10.10.0.77") is True
        assert scope.covers("192.0.2.1") is False

    def test_authorization_metadata_is_carried_only_when_supplied(self):
        bare = scope_payload(plan_drop("10.10.0.5"))
        assert "authorization_ref" not in bare and "authorized_by" not in bare
        attributed = scope_payload(plan_drop("10.10.0.5"), authorization_ref="TICKET-42", authorized_by="dana")
        assert attributed["authorization_ref"] == "TICKET-42"
        assert attributed["authorized_by"] == "dana"


class TestDropTargets:
    def test_a_column_drop_creates_a_new_card(self):
        plan = plan_drop({"target": "10.10.0.5", "board_id": "brd_agent", "column": "Backlog"})
        assert plan.card_id is None
        assert plan.verdict["drop_mode"] == "new-card"
        assert plan.column == "Backlog"

    def test_a_card_drop_attaches_to_that_card(self):
        plan = plan_drop({"target": "10.10.0.5", "board_id": "brd_agent", "card_id": "crd_1"})
        assert plan.card_id == "crd_1"
        assert plan.verdict["drop_mode"] == "attach-to-card"

    def test_titles_read_as_work_not_as_a_bare_address(self):
        assert title_for("cidr", "10.0.0.0/8").startswith("Enumerate network")
        assert title_for("mac", "aa:bb:cc:dd:ee:ff").startswith("Identify device")
        assert "10.0.0.0/8" in title_for("cidr", "10.0.0.0/8")

    def test_card_payload_carries_title_scope_and_board(self):
        plan = plan_drop({"target": "shop.example.net", "board_id": "brd_eng"})
        body = card_payload(plan, crew="recon")
        assert body["board_id"] == "brd_eng"
        assert body["crew"] == "recon"
        assert body["scope"]["targets"] == ["shop.example.net"]
        assert "shop.example.net" in body["title"]

    def test_card_payload_defaults_to_a_reproducible_description(self):
        plan = plan_drop({"target": "10.10.0.5", "board_id": "brd_agent"})
        body = card_payload(plan)
        assert "10.10.0.5" in body["description"] and "ipv4" in body["description"]


class TestAuditRouting:
    @pytest.fixture()
    def audit(self):
        from tool_frontends.audit import ToolAuditLog

        log = ToolAuditLog(":memory:")
        yield log
        log.close()

    def test_an_accepted_drop_is_recorded(self, audit):
        plan = plan_drop({"target": "10.10.0.5", "board_id": "brd_agent"})
        summary = audit_drop(audit, plan, actor="tester")
        assert summary is not None and summary["tool"] == "target_drop"
        rows = audit.list(tool="target_drop")
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"
        assert rows[0]["target"] == "10.10.0.5"

    def test_a_refused_drop_is_also_recorded(self, audit):
        # An audit trail that only records successes cannot explain the refusal
        # a user will ask about.
        plan = plan_drop("not a target")
        audit_drop(audit, plan, actor="tester")
        rows = audit.list(tool="target_drop")
        assert len(rows) == 1
        assert rows[0]["status"] == "denied"
        assert rows[0]["target"] is None

    def test_the_drop_keeps_the_hash_chain_intact(self, audit):
        audit_drop(audit, plan_drop("10.10.0.5"), actor="tester")
        audit_drop(audit, plan_drop("not a target"), actor="tester")
        audit_drop(audit, plan_drop("shop.example.net:443"), actor="tester")
        verdict = audit.verify_chain()
        assert verdict["ok"] is True
        assert verdict["checked"] == 3

    def test_the_scope_is_recoverable_from_the_audit_row(self, audit):
        plan = plan_drop({"target": "https://shop.example.net/login", "board_id": "brd_eng"})
        audit_drop(audit, plan, actor="tester")
        args = audit.list(tool="target_drop")[0]["args"]
        assert args["scope"]["targets"] == ["shop.example.net"]
        assert args["kind"] == "url"

    def test_a_missing_audit_log_is_not_an_error(self):
        assert audit_drop(None, plan_drop("10.10.0.5")) is None

    def test_a_broken_audit_log_does_not_lose_the_drop(self):
        class Exploding:
            def append(self, **_kwargs):
                raise RuntimeError("audit store is down")

        assert audit_drop(Exploding(), plan_drop("10.10.0.5")) is None
