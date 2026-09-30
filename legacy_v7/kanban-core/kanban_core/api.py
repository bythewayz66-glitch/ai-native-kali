"""FastAPI application: REST + WebSocket surface for the Kanban backbone.

Blueprint ref: section 03.7 (API) - REST for card CRUD, WebSocket for live
column-transition events.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .bus import EventBus
from .models import (
    Artifact,
    Card,
    Column,
    EventType,
    Scope,
    Trace,
    all_columns,
)
from .seed import seed
from .scope_model import ScopeNarrowingError
from .service import KanbanService, NotFound
from .state_machine import TransitionError, can_move
from .store import Store

DB_PATH = os.environ.get("KANBAN_DB", "var/db/kanban.db")


# --------------------------------------------------------------------- DTOs
class CardCreate(BaseModel):
    title: str
    board_id: str
    description: str = ""
    priority: str = "medium"
    assignee: Optional[str] = None
    assignee_kind: str = "agent"
    crew: Optional[str] = None
    scope: Optional[dict[str, Any]] = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    requires_approval: bool = False
    labels: list[str] = Field(default_factory=list)
    parent_id: Optional[str] = None
    column: str = Column.BACKLOG.value
    meta: dict[str, Any] = Field(default_factory=dict)


class MoveRequest(BaseModel):
    to_column: str
    actor: str = "human"
    force: bool = False
    actor_is_agent: bool = False
    note: str = ""


class SubCardCreate(BaseModel):
    """Spawn a child card from a parent (Phase 8).

    ``scope`` omitted means *inherit the parent's scope* - the safe default. An
    explicitly empty scope is a widening and is refused with 409, not silently
    upgraded to the parent's.
    """

    title: str
    description: str = ""
    priority: str = "medium"
    assignee: Optional[str] = None
    crew: Optional[str] = None
    scope: Optional[dict[str, Any]] = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    requires_approval: bool = False
    labels: list[str] = Field(default_factory=list)
    column: str = Column.BACKLOG.value
    meta: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"


class AssignRequest(BaseModel):
    assignee: str
    crew: Optional[str] = None
    actor: str = "orchestrator"
    move_to_assigned: bool = True


class ApprovalRequest(BaseModel):
    reason: str
    requested_by: str = "agent"
    tool: Optional[str] = None
    tier: Optional[int] = None


class ApprovalDecision(BaseModel):
    approved: bool
    decided_by: str = "operator"
    note: str = ""


class KillRequest(BaseModel):
    reason: str = "operator kill switch"
    actor: str = "operator"


class TraceCreate(BaseModel):
    tool: str
    tier: int = 0
    args: dict[str, Any] = Field(default_factory=dict)
    status: str = "ok"
    duration_ms: int = 0
    exit_code: Optional[int] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    dry_run: bool = True
    audit_hash: Optional[str] = None
    agent: Optional[str] = None


class ArtifactCreate(BaseModel):
    name: str
    kind: str = "other"
    path: Optional[str] = None
    url: Optional[str] = None
    sha256: Optional[str] = None
    bytes_: int = Field(default=0, alias="bytes")
    produced_by: Optional[str] = None
    summary: str = ""

    model_config = {"populate_by_name": True}


class BoardCreate(BaseModel):
    name: str
    kind: str = "agent"
    description: str = ""
    default_crew: Optional[str] = None
    board_id: Optional[str] = None


# ----------------------------------------------------------------- app core
def build_app(store: Optional[Store] = None, bus: Optional[EventBus] = None) -> FastAPI:
    store = store or Store(DB_PATH)
    bus = bus or EventBus()
    service = KanbanService(store, bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store
        app.state.bus = bus
        app.state.service = service
        if os.environ.get("KANBAN_SEED", "1") == "1" and not store.list_boards():
            seed(service)
        yield
        # The store is intentionally left open for TestClient reuse.

    app = FastAPI(
        title="AI-native Kali - Kanban Core",
        version="0.1.0",
        description="Orchestration backbone. Every unit of work on the system is a card.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.store = store
    app.state.bus = bus
    app.state.service = service

    def svc() -> KanbanService:
        return app.state.service

    def not_found(exc: NotFound) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc))

    def bad_transition(exc: TransitionError) -> HTTPException:
        return HTTPException(status_code=409, detail={"error": "transition_rejected", "reasons": exc.reasons})

    # ------------------------------------------------------------- system
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "kanban-core",
            "version": "0.1.0",
            "boards": len(svc().list_boards()),
            "bus": svc().bus.stats(),
            "audit": svc().chain_verdict(),
            "schema": {
                "columns": [c.value for c in all_columns()],
                "lifecycle": "Backlog -> Assigned -> Running -> Review -> Done | Blocked",
            },
        }

    @app.get("/api/schema")
    def schema() -> dict[str, Any]:
        return {
            "columns": [c.value for c in all_columns()],
            "card_fields": list(Card.model_fields.keys()),
            "event_types": [v for k, v in vars(EventType).items() if not k.startswith("_") and isinstance(v, str)],
            "guardrail_tiers": {0: "passive", 1: "low-impact", 2: "intrusive", 3: "high-impact"},
        }

    @app.get("/api/audit/verify")
    def audit_verify() -> dict[str, Any]:
        return svc().chain_verdict()

    @app.get("/api/audit")
    def audit(since_seq: int = 0, limit: int = 200, card_id: Optional[str] = None) -> dict[str, Any]:
        events = svc().store.list_events(since_seq=since_seq, card_id=card_id, limit=limit)
        return {"events": [e.model_dump() for e in events], "count": len(events)}

    @app.get("/api/events")
    def events(
        since_seq: int = 0,
        limit: int = 200,
        card_id: Optional[str] = None,
        board_id: Optional[str] = None,
        type_prefix: Optional[str] = None,
    ) -> dict[str, Any]:
        events = svc().store.list_events(
            since_seq=since_seq,
            limit=limit,
            card_id=card_id,
            board_id=board_id,
            type_prefix=type_prefix,
        )
        return {"events": [e.model_dump() for e in events], "count": len(events)}

    # ------------------------------------------------------------- boards
    @app.post("/api/boards", status_code=201)
    def create_board(body: BoardCreate) -> dict[str, Any]:
        board = svc().create_board(
            body.name,
            body.kind,
            description=body.description,
            default_crew=body.default_crew,
            board_id=body.board_id,
        )
        return board.model_dump()

    @app.get("/api/boards")
    def list_boards() -> dict[str, Any]:
        return {"boards": [b.model_dump() for b in svc().list_boards()]}

    @app.get("/api/boards/{board_id}")
    def get_board(board_id: str, view: bool = True) -> dict[str, Any]:
        try:
            if view:
                return svc().board_view(board_id)
            return svc().get_board(board_id).model_dump()
        except NotFound as exc:
            raise not_found(exc)

    @app.get("/api/boards/{board_id}/metrics")
    def board_metrics(board_id: str) -> dict[str, Any]:
        try:
            return svc().board_metrics(board_id)
        except NotFound as exc:
            raise not_found(exc)

    # -------------------------------------------------------------- cards
    @app.post("/api/cards", status_code=201)
    def create_card(body: CardCreate) -> dict[str, Any]:
        try:
            card = svc().create_card(
                body.title,
                body.board_id,
                description=body.description,
                priority=body.priority,
                assignee=body.assignee,
                assignee_kind=body.assignee_kind,
                crew=body.crew,
                scope=body.scope,
                tools=body.tools,
                requires_approval=body.requires_approval,
                labels=body.labels,
                parent_id=body.parent_id,
                column=body.column,
                meta=body.meta,
            )
        except NotFound as exc:
            raise not_found(exc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return svc().card_view(card)

    @app.get("/api/cards")
    def list_cards(
        board_id: Optional[str] = None,
        column: Optional[str] = None,
        assignee: Optional[str] = None,
        parent_id: Optional[str] = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        cards = svc().list_cards(
            board_id=board_id, column=column, assignee=assignee, parent_id=parent_id, limit=limit
        )
        return {"cards": [svc().card_view(c) for c in cards], "count": len(cards)}

    @app.get("/api/cards/{card_id}")
    def get_card(card_id: str) -> dict[str, Any]:
        try:
            return svc().card_view(svc().get_card(card_id))
        except NotFound as exc:
            raise not_found(exc)

    @app.get("/api/cards/{card_id}/replay")
    def replay(card_id: str) -> dict[str, Any]:
        try:
            return svc().replay_card(card_id)
        except NotFound as exc:
            raise not_found(exc)

    @app.get("/api/cards/{card_id}/children")
    def children(card_id: str) -> dict[str, Any]:
        cards = svc().list_cards(parent_id=card_id)
        return {"cards": [svc().card_view(c) for c in cards], "count": len(cards)}

    @app.post("/api/cards/{card_id}/subcards", status_code=201)
    def create_subcard(card_id: str, body: SubCardCreate) -> dict[str, Any]:
        """Spawn a child card whose scope is inside its parent's.

        A child that would widen its parent's scope is refused with 409 and the
        refusal is written to the hash chain - see docs/SUB_CARD_SCOPE_MODEL.md.
        """
        try:
            child = svc().create_subcard(
                card_id,
                body.title,
                description=body.description,
                priority=body.priority,
                assignee=body.assignee,
                crew=body.crew,
                scope=body.scope,
                tools=body.tools,
                requires_approval=body.requires_approval,
                labels=body.labels,
                column=body.column,
                meta=body.meta,
                actor=body.actor,
            )
        except NotFound as exc:
            raise not_found(exc)
        except ScopeNarrowingError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "scope_widened", "reasons": exc.reasons},
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return svc().card_view(child)

    @app.post("/api/cards/{card_id}/move")
    def move(card_id: str, body: MoveRequest) -> dict[str, Any]:
        try:
            card = svc().move_card(
                card_id,
                body.to_column,
                actor=body.actor,
                force=body.force,
                actor_is_agent=body.actor_is_agent,
                note=body.note,
            )
        except NotFound as exc:
            raise not_found(exc)
        except TransitionError as exc:
            raise bad_transition(exc)
        return svc().card_view(card)

    @app.post("/api/cards/{card_id}/can-move")
    def can_move_endpoint(card_id: str, body: MoveRequest) -> dict[str, Any]:
        try:
            card = svc().get_card(card_id)
        except NotFound as exc:
            raise not_found(exc)
        parent, kids = svc()._family(card)
        ok, reasons = can_move(
            card, Column(body.to_column), force=body.force, parent=parent, children=kids
        )
        return {"ok": ok, "reasons": reasons}

    @app.post("/api/cards/{card_id}/assign")
    def assign(card_id: str, body: AssignRequest) -> dict[str, Any]:
        try:
            card = svc().assign_card(
                card_id,
                body.assignee,
                crew=body.crew,
                actor=body.actor,
                move_to_assigned=body.move_to_assigned,
            )
        except NotFound as exc:
            raise not_found(exc)
        except TransitionError as exc:
            raise bad_transition(exc)
        return svc().card_view(card)

    @app.post("/api/cards/{card_id}/kill")
    def kill(card_id: str, body: KillRequest) -> dict[str, Any]:
        try:
            card = svc().kill_card(card_id, actor=body.actor, reason=body.reason)
        except NotFound as exc:
            raise not_found(exc)
        return svc().card_view(card)

    @app.post("/api/cards/{card_id}/block")
    def block(card_id: str, body: KillRequest) -> dict[str, Any]:
        try:
            card = svc().block_card(card_id, body.reason, actor=body.actor)
        except NotFound as exc:
            raise not_found(exc)
        except TransitionError as exc:
            raise bad_transition(exc)
        return svc().card_view(card)

    @app.post("/api/cards/{card_id}/traces", status_code=201)
    def add_trace(card_id: str, body: TraceCreate) -> dict[str, Any]:
        try:
            card = svc().add_trace(card_id, Trace(**body.model_dump()))
        except NotFound as exc:
            raise not_found(exc)
        return svc().card_view(card)

    @app.post("/api/cards/{card_id}/artifacts", status_code=201)
    def add_artifact(card_id: str, body: ArtifactCreate) -> dict[str, Any]:
        payload = body.model_dump()
        payload["bytes"] = payload.pop("bytes_", 0)
        try:
            card = svc().add_artifact(card_id, Artifact(**payload))
        except NotFound as exc:
            raise not_found(exc)
        return svc().card_view(card)

    @app.post("/api/cards/{card_id}/result")
    def set_result(card_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            card = svc().set_result(card_id, payload.get("result", ""), actor=payload.get("actor", "agent"))
        except NotFound as exc:
            raise not_found(exc)
        return svc().card_view(card)

    # ---------------------------------------------------------- approvals
    @app.post("/api/cards/{card_id}/approvals", status_code=201)
    def request_approval(card_id: str, body: ApprovalRequest) -> dict[str, Any]:
        try:
            card = svc().request_approval(
                card_id,
                reason=body.reason,
                requested_by=body.requested_by,
                tool=body.tool,
                tier=body.tier,
            )
        except NotFound as exc:
            raise not_found(exc)
        return svc().card_view(card)

    @app.post("/api/cards/{card_id}/approvals/{approval_id}/decide")
    def decide_approval(card_id: str, approval_id: str, body: ApprovalDecision) -> dict[str, Any]:
        try:
            card = svc().decide_approval(
                card_id, approval_id, approved=body.approved, decided_by=body.decided_by, note=body.note
            )
        except NotFound as exc:
            raise not_found(exc)
        except TransitionError as exc:
            raise bad_transition(exc)
        return svc().card_view(card)

    @app.get("/api/approvals/pending")
    def pending_approvals() -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for card in svc().list_cards(limit=1000):
            approval = card.pending_approval
            if approval is not None:
                out.append(
                    {
                        "card_id": card.card_id,
                        "title": card.title,
                        "board_id": card.board_id,
                        "column": card.column.value,
                        "approval": approval.model_dump(),
                        "max_tier": card.max_tier,
                    }
                )
        return {"pending": out, "count": len(out)}

    # ------------------------------------------------------------- scope
    @app.post("/api/scope/check")
    def scope_check(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raw = body.get("scope") or {}
        scope = Scope.model_validate(raw)
        target = body.get("target", "")
        return {
            "target": target,
            "covered": scope.covers(target),
            "expired": scope.is_expired(),
            "scope": scope.summary(),
        }

    # -------------------------------------------------- observability feed
    @app.get("/api/timeline")
    def timeline(limit: int = 100) -> dict[str, Any]:
        events = svc().store.list_events(limit=limit)
        items = [
            {
                "seq": e.seq,
                "ts": e.ts,
                "type": e.type,
                "actor": e.actor,
                "card_id": e.card_id,
                "board_id": e.board_id,
                "from": e.from_column,
                "to": e.to_column,
                "payload": e.payload,
                "audit_hash": e.audit_hash,
            }
            for e in events
        ]
        items.sort(key=lambda i: i["seq"] or 0, reverse=True)
        return {"timeline": items, "count": len(items)}

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        boards = svc().list_boards()
        metrics = [svc().board_metrics(b.board_id) for b in boards]
        cards = svc().list_cards(limit=5000)
        return {
            "boards": [b.model_dump() for b in boards],
            "metrics": metrics,
            "totals": {
                "cards": len(cards),
                "running": sum(1 for c in cards if c.column is Column.RUNNING),
                "blocked": sum(1 for c in cards if c.column is Column.BLOCKED),
                "pending_approvals": sum(1 for c in cards if c.pending_approval),
                "tool_runs": sum(len(c.traces) for c in cards),
                "tokens": sum(int((t.args or {}).get("__tokens", 0) or 0) for c in cards for t in c.traces),
                "cost_usd": round(
                    sum(float((t.args or {}).get("__cost_usd", 0.0) or 0.0) for c in cards for t in c.traces), 6
                ),
            },
        }

    # ----------------------------------------------------------- WebSocket
    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = svc().bus.subscribe()
        try:
            backlog = svc().bus.recent(limit=25)
            await websocket.send_json(
                {"kind": "snapshot", "events": [e.model_dump() for e in backlog], "bus": svc().bus.stats()}
            )
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    await websocket.send_json({"kind": "event", "event": event.model_dump()})
                except asyncio.TimeoutError:
                    await websocket.send_json({"kind": "heartbeat", "bus": svc().bus.stats()})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            svc().bus.unsubscribe(queue)

    @app.websocket("/ws/board/{board_id}")
    async def ws_board(websocket: WebSocket, board_id: str) -> None:
        await websocket.accept()
        queue = svc().bus.subscribe()
        try:
            await websocket.send_json({"kind": "board", "view": svc().board_view(board_id)})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if event.board_id != board_id:
                        continue
                    await websocket.send_json(
                        {"kind": "board", "view": svc().board_view(board_id), "event": event.model_dump()}
                    )
                except asyncio.TimeoutError:
                    await websocket.send_json({"kind": "heartbeat", "view": svc().board_view(board_id)})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            svc().bus.unsubscribe(queue)

    return app


app = build_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "kanban_core.api:app",
        host=os.environ.get("KANBAN_HOST", "127.0.0.1"),
        port=int(os.environ.get("KANBAN_PORT", "8081")),
        reload=False,
    )
