"""Thin HTTP client for kanban-core.

The bridge talks to the board exactly the way any other client would - over the
public REST API - so there is no privileged back door into the orchestration
backbone. If the bridge wants to move a card, it must satisfy the same guards
the shell UI and a human operator do.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx


class KanbanError(RuntimeError):
    """Raised when kanban-core rejects an operation."""

    def __init__(self, message: str, status_code: int = 0, detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail

    @property
    def reasons(self) -> list[str]:
        """Guard reasons, when the rejection came from the state machine."""
        if isinstance(self.detail, dict):
            return list(self.detail.get("reasons") or [])
        return []


class KanbanClient:
    """Synchronous kanban-core client used by the bridge loop."""

    def __init__(self, base_url: str = "http://127.0.0.1:8081", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- transport -------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise KanbanError(f"kanban-core unreachable at {url}: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise KanbanError(
                f"{method} {path} -> {response.status_code}", response.status_code, detail
            )
        if not response.content:
            return None
        return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params or None)

    def post(self, path: str, payload: Optional[dict[str, Any]] = None) -> Any:
        return self._request("POST", path, json=payload or {})

    # -- convenience -----------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self.get("/health")

    def boards(self) -> list[dict[str, Any]]:
        return self.get("/api/boards")["boards"]

    def cards(
        self,
        *,
        board_id: Optional[str] = None,
        column: Optional[str] = None,
        assignee: Optional[str] = None,
        parent_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if board_id:
            params["board_id"] = board_id
        if column:
            params["column"] = column
        if assignee:
            params["assignee"] = assignee
        if parent_id:
            params["parent_id"] = parent_id
        return self.get("/api/cards", **params)["cards"]

    def card(self, card_id: str) -> dict[str, Any]:
        return self.get(f"/api/cards/{card_id}")

    def agents_queue(self) -> list[dict[str, Any]]:
        """Cards waiting for an agent to pick them up."""
        return self.cards(column="Assigned")

    def move(
        self,
        card_id: str,
        to_column: str,
        *,
        actor: str = "bridge",
        actor_is_agent: bool = True,
        force: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        return self.post(
            f"/api/cards/{card_id}/move",
            {
                "to_column": to_column,
                "actor": actor,
                "actor_is_agent": actor_is_agent,
                "force": force,
                "note": note,
            },
        )

    def assign(self, card_id: str, assignee: str, *, crew: Optional[str] = None, actor: str = "bridge") -> dict[str, Any]:
        return self.post(
            f"/api/cards/{card_id}/assign",
            {"assignee": assignee, "crew": crew, "actor": actor, "move_to_assigned": True},
        )

    def add_trace(self, card_id: str, trace: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/api/cards/{card_id}/traces", trace)

    def add_artifact(self, card_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/api/cards/{card_id}/artifacts", artifact)

    def set_result(self, card_id: str, result: str, *, actor: str = "bridge") -> dict[str, Any]:
        return self.post(f"/api/cards/{card_id}/result", {"result": result, "actor": actor})

    def request_approval(
        self, card_id: str, *, reason: str, requested_by: str, tool: Optional[str] = None, tier: Optional[int] = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": reason, "requested_by": requested_by}
        if tool:
            payload["tool"] = tool
        if tier is not None:
            payload["tier"] = tier
        return self.post(f"/api/cards/{card_id}/approvals", payload)

    def block(self, card_id: str, reason: str, *, actor: str = "bridge") -> dict[str, Any]:
        return self.post(f"/api/cards/{card_id}/block", {"reason": reason, "actor": actor})

    def create_card(self, **payload: Any) -> dict[str, Any]:
        return self.post("/api/cards", payload)

    def create_subcard(self, parent_id: str, **payload: Any) -> dict[str, Any]:
        """Spawn a child card whose scope must be inside its parent's.

        The narrowing rule is enforced server-side (kanban-core's scope_model),
        so a caller that proposes a widening gets a 409 rather than a wider card.
        The bridge deliberately does not pre-check: the board is the authority on
        what is in scope, and a client-side check would be a second opinion that
        could disagree with it.
        """
        return self.post(f"/api/cards/{parent_id}/subcards", payload)
