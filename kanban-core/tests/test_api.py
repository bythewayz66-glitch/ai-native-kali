"""REST + WebSocket API tests (FastAPI TestClient)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kanban_core.api import build_app
from kanban_core.bus import EventBus
from kanban_core.store import Store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_SEED", "1")
    app = build_app(Store(tmp_path / "api.db"), EventBus())
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def blank_client(tmp_path):
    app = build_app(Store(tmp_path / "blank.db"), EventBus())
    with TestClient(app) as c:
        yield c


class TestSystemEndpoints:
    def test_health(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["service"] == "kanban-core"
        assert body["boards"] == 4
        assert body["audit"]["ok"] is True
        assert body["schema"]["columns"] == ["Backlog", "Assigned", "Running", "Review", "Done", "Blocked"]

    def test_schema_exposes_columns_and_events(self, client):
        body = client.get("/api/schema").json()
        assert "Backlog" in body["columns"]
        assert "scope" in body["card_fields"]
        assert "traces" in body["card_fields"]
        assert "approvals" in body["card_fields"]
        assert any(e == "card.moved" for e in body["event_types"])
        assert body["guardrail_tiers"]["2"] == "intrusive"

    def test_audit_verify(self, client):
        body = client.get("/api/audit/verify").json()
        assert body["ok"] is True and body["checked"] > 0

    def test_audit_and_events_lists(self, client):
        assert client.get("/api/audit").json()["count"] > 0
        assert client.get("/api/events").json()["count"] > 0


class TestSeededBoards:
    def test_four_boards_seeded(self, client):
        boards = client.get("/api/boards").json()["boards"]
        assert {b["kind"] for b in boards} == {"agent", "engagement", "system", "personal"}

    def test_agent_board_view_has_columns(self, client):
        view = client.get("/api/boards/brd_agent").json()
        assert set(view["columns"].keys()) >= {"Backlog", "Assigned", "Running", "Review", "Done", "Blocked"}
        assert view["total"] >= 2
        assert isinstance(view["counts"]["Backlog"], int)

    def test_engagement_board_seeded_with_scope(self, client):
        view = client.get("/api/boards/brd_engagement").json()
        cards = [c for col in view["columns"].values() for c in col]
        scoped = [c for c in cards if c.get("scope")]
        assert scoped
        assert scoped[0]["scope"]["authorization_ref"] == "ACME-SOW-2026-0912"

    def test_unknown_board_404(self, client):
        assert client.get("/api/boards/nope").status_code == 404

    def test_board_metrics(self, client):
        body = client.get("/api/boards/brd_engagement/metrics").json()
        assert body["board_id"] == "brd_engagement"
        assert "by_column" in body

    def test_overview_totals(self, client):
        body = client.get("/api/overview").json()
        assert len(body["boards"]) == 4
        assert body["totals"]["cards"] > 0


class TestCardCRUD:
    def test_create_and_fetch_card(self, client):
        created = client.post(
            "/api/cards",
            json={"title": "ping example.com", "board_id": "brd_agent", "priority": "high"},
        )
        assert created.status_code == 201
        card = created.json()
        assert card["column"] == "Backlog"
        assert card["priority"] == "high"
        assert card["max_tier"] == 0 and card["is_gated"] is False
        fetched = client.get(f"/api/cards/{card['card_id']}").json()
        assert fetched["card_id"] == card["card_id"]

    def test_create_card_unknown_board_404(self, client):
        assert client.post("/api/cards", json={"title": "x", "board_id": "nope"}).status_code == 404

    def test_list_cards_filter_by_column(self, client):
        body = client.get("/api/cards", params={"board_id": "brd_agent", "column": "Backlog"}).json()
        assert all(c["column"] == "Backlog" for c in body["cards"])

    def test_get_unknown_card_404(self, client):
        assert client.get("/api/cards/nope").status_code == 404

    def test_card_with_t2_tool_is_gated(self, client):
        card = client.post(
            "/api/cards",
            json={
                "title": "nikto",
                "board_id": "brd_engagement",
                "tools": [{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
                "scope": {"targets": ["example.com"]},
            },
        ).json()
        assert card["is_gated"] is True
        assert card["max_tier"] == 2


class TestMovement:
    def _card(self, client, **kwargs):
        payload = {"title": "t", "board_id": "brd_agent", "assignee": "recon-specialist"}
        payload.update(kwargs)
        return client.post("/api/cards", json=payload).json()

    def test_legal_move(self, client):
        card = self._card(client)
        moved = client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Assigned"})
        assert moved.status_code == 200
        assert moved.json()["column"] == "Assigned"

    def test_illegal_move_returns_409_with_reasons(self, client):
        card = self._card(client)
        bad = client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Done"})
        assert bad.status_code == 409
        assert bad.json()["detail"]["error"] == "transition_rejected"
        assert bad.json()["detail"]["reasons"]

    def test_can_move_endpoint_explains_guard(self, client):
        card = self._card(client, assignee=None)
        client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Assigned"})
        body = client.post(f"/api/cards/{card['card_id']}/can-move", json={"to_column": "Running"}).json()
        assert body["ok"] is False
        assert any("no_assignee" in r for r in body["reasons"])

    def test_assign_moves_to_assigned_column(self, client):
        card = self._card(client, assignee=None)
        body = client.post(
            f"/api/cards/{card['card_id']}/assign", json={"assignee": "recon-specialist", "crew": "recon"}
        ).json()
        assert body["column"] == "Assigned"
        assert body["crew"] == "recon"

    def test_agent_cannot_write_done(self, client):
        card = self._card(client)
        client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Assigned"})
        client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Running", "actor_is_agent": True})
        client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Review", "actor_is_agent": True})
        blocked = client.post(
            f"/api/cards/{card['card_id']}/move", json={"to_column": "Done", "actor_is_agent": True}
        )
        assert blocked.status_code == 409
        human = client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Done", "actor": "operator"})
        assert human.status_code == 200

    def test_kill_switch(self, client):
        card = self._card(client)
        client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Assigned"})
        client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Running", "actor_is_agent": True})
        killed = client.post(f"/api/cards/{card['card_id']}/kill", json={"reason": "stop now"}).json()
        assert killed["killed"] is True
        after = client.post(f"/api/cards/{card['card_id']}/move", json={"to_column": "Review"})
        assert after.status_code == 409
        assert any("card_killed" in r for r in after.json()["detail"]["reasons"])

    def test_block_endpoint(self, client):
        card = self._card(client)
        body = client.post(f"/api/cards/{card['card_id']}/block", json={"reason": "needs creds"}).json()
        assert body["column"] == "Blocked"
        assert body["blocked_reason"] == "needs creds"


class TestApprovalAPI:
    def test_pending_approval_flows_through_api(self, client):
        card = client.post(
            "/api/cards",
            json={
                "title": "web assessment",
                "board_id": "brd_engagement",
                "assignee": "web-specialist",
                "requires_approval": True,
                "scope": {"targets": ["example.com"]},
                "tools": [{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
            },
        ).json()
        cid = card["card_id"]
        client.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})

        gated = client.post(f"/api/cards/{cid}/move", json={"to_column": "Running", "actor_is_agent": True})
        assert gated.status_code == 409

        requested = client.post(
            f"/api/cards/{cid}/approvals", json={"reason": "T2 needs a human", "tool": "nikto_scan", "tier": 2}
        )
        assert requested.status_code == 201
        approval = requested.json()["approvals"][-1]
        assert approval["status"] == "pending"

        pending = client.get("/api/approvals/pending").json()
        assert any(p["card_id"] == cid for p in pending["pending"])

        decided = client.post(
            f"/api/cards/{cid}/approvals/{approval['id']}/decide",
            json={"approved": True, "decided_by": "operator", "note": "authorized"},
        )
        assert decided.status_code == 200
        assert decided.json()["has_pending_approval"] is False

        ran = client.post(f"/api/cards/{cid}/move", json={"to_column": "Running", "actor_is_agent": True})
        assert ran.status_code == 200
        assert client.get("/api/approvals/pending").json()["count"] == 0

    def test_reject_keeps_gate_closed(self, client):
        card = client.post(
            "/api/cards",
            json={"title": "x", "board_id": "brd_engagement", "assignee": "a", "requires_approval": True},
        ).json()
        cid = card["card_id"]
        client.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        approval = client.post(f"/api/cards/{cid}/approvals", json={"reason": "gate"}).json()["approvals"][-1]
        client.post(
            f"/api/cards/{cid}/approvals/{approval['id']}/decide", json={"approved": False, "decided_by": "operator"}
        )
        blocked = client.post(f"/api/cards/{cid}/move", json={"to_column": "Running", "actor_is_agent": True})
        assert blocked.status_code == 409


class TestTracesArtifactsReplay:
    def test_trace_and_artifact_endpoints(self, client):
        card = client.post("/api/cards", json={"title": "t", "board_id": "brd_agent"}).json()
        cid = card["card_id"]
        t = client.post(
            f"/api/cards/{cid}/traces",
            json={"tool": "nmap_scan", "tier": 1, "status": "ok", "duration_ms": 321, "dry_run": True},
        )
        assert t.status_code == 201
        a = client.post(
            f"/api/cards/{cid}/artifacts",
            json={"name": "out.json", "kind": "scan-output", "bytes": 64, "sha256": "ab"},
        )
        assert a.status_code == 201
        assert len(a.json()["artifacts"]) == 1
        assert a.json()["artifacts"][0]["bytes"] == 64

    def test_replay_endpoint(self, client):
        card = client.post("/api/cards", json={"title": "t", "board_id": "brd_agent"}).json()
        cid = card["card_id"]
        client.post(f"/api/cards/{cid}/move", json={"to_column": "Assigned"})
        replay = client.get(f"/api/cards/{cid}/replay").json()
        assert replay["card_id"] == cid
        assert any(e["type"] == "card.moved" for e in replay["events"])

    def test_children_endpoint(self, client):
        parent = client.post("/api/cards", json={"title": "parent", "board_id": "brd_agent"}).json()
        client.post(
            "/api/cards",
            json={"title": "child", "board_id": "brd_agent", "parent_id": parent["card_id"]},
        )
        kids = client.get(f"/api/cards/{parent['card_id']}/children").json()
        assert kids["count"] == 1


class TestScopeAndTimeline:
    def test_scope_check_endpoint(self, client):
        body = client.post(
            "/api/scope/check",
            json={"target": "api.example.com", "scope": {"targets": ["*.example.com"]}},
        ).json()
        assert body["covered"] is True
        outside = client.post(
            "/api/scope/check", json={"target": "evil.net", "scope": {"targets": ["example.com"]}}
        ).json()
        assert outside["covered"] is False

    def test_timeline_is_newest_first(self, client):
        tl = client.get("/api/timeline").json()["timeline"]
        assert tl
        seqs = [item["seq"] for item in tl]
        assert seqs == sorted(seqs, reverse=True)
        assert all(item["audit_hash"] for item in tl)


class TestEventFiltering:
    """/api/events must be filterable per card.

    Consumers (the bridge's block check, the shell's card drawer, the collector's
    replay) all need one card's history; a filter that is silently ignored makes
    them reason over every other card's transitions too.
    """

    def _make_card(self, client, board, title):
        return client.post("/api/cards", json={"title": title, "board_id": board}).json()

    def test_filter_by_card_id(self, client):
        board = client.get("/api/boards").json()["boards"][0]["board_id"]
        a = self._make_card(client, board, "card A")
        b = self._make_card(client, board, "card B")
        client.post(f"/api/cards/{a['card_id']}/move", json={"to_column": "Assigned"})
        client.post(f"/api/cards/{b['card_id']}/move", json={"to_column": "Assigned"})

        a_events = client.get("/api/events", params={"card_id": a["card_id"]}).json()["events"]
        assert a_events
        assert {e["card_id"] for e in a_events} == {a["card_id"]}
        assert all(e["card_id"] != b["card_id"] for e in a_events)

    def test_filter_by_board_id(self, client):
        boards = client.get("/api/boards").json()["boards"]
        first, second = boards[0]["board_id"], boards[-1]["board_id"]
        self._make_card(client, second, "on another board")
        events = client.get("/api/events", params={"board_id": second}).json()["events"]
        assert events
        assert {e["board_id"] for e in events} == {second}

    def test_filter_by_type_prefix(self, client):
        events = client.get("/api/events", params={"type_prefix": "card."}).json()["events"]
        assert events
        assert all(e["type"].startswith("card.") for e in events)
        assert client.get("/api/events", params={"type_prefix": "no.such."}).json()["count"] == 0

    def test_unfiltered_returns_everything(self, client):
        everything = client.get("/api/events", params={"limit": 500}).json()["count"]
        filtered = client.get("/api/events", params={"type_prefix": "card."}).json()["count"]
        assert everything >= filtered > 0

    def test_unknown_card_returns_empty_not_everything(self, client):
        assert client.get("/api/events", params={"card_id": "crd_does_not_exist"}).json()["events"] == []


class TestWebSocket:
    def test_ws_events_sends_snapshot_then_live_event(self, client):
        with client.websocket_connect("/ws/events") as ws:
            snapshot = ws.receive_json()
            assert snapshot["kind"] == "snapshot"
            assert "bus" in snapshot
            client.post("/api/cards", json={"title": "ws test", "board_id": "brd_agent"})
            message = ws.receive_json()
            assert message["kind"] == "event"
            assert message["event"]["type"] == "card.created"

    def test_ws_board_streams_board_view(self, client):
        with client.websocket_connect("/ws/board/brd_agent") as ws:
            first = ws.receive_json()
            assert first["kind"] == "board"
            assert first["view"]["board"]["board_id"] == "brd_agent"
            assert "columns" in first["view"]
            client.post("/api/cards", json={"title": "live", "board_id": "brd_agent"})
            update = ws.receive_json()
            assert update["kind"] == "board"
            assert update["view"]["total"] >= 3


class TestBlankApp:
    def test_no_seed_when_env_disabled(self, blank_client):
        assert blank_client.get("/api/boards").json()["boards"] == []
        created = blank_client.post("/api/boards", json={"name": "Fresh", "kind": "agent"})
        assert created.status_code == 201
        assert blank_client.get("/health").json()["boards"] == 1
