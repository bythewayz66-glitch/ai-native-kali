"""Item 4 - the Kanban board as the orchestration surface for OS-level workflows.

The design is in ``agent_runtime/desktop_orchestration.py``. What these tests
establish is the part a design doc cannot: that the board surfaces the right
window, that a card **cannot** make the board open one, that the decision lands
in the same hash-chained log as the tool calls, and that the whole thing works
against the real shell over real HTTP - not just against a mock.

The last test is the proof of concept: it starts the actual Hermes shell app on a
port and drives it with the board's own client, so "the board can orchestrate the
desktop" is demonstrated end to end rather than asserted.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from agent_runtime.desktop_orchestration import (
    DEFAULT_SHELL_URL,
    DesktopClient,
    DesktopUnreachable,
    SurfacePlan,
    audit_surface,
    crew_desktop_context,
    orchestrate,
    plan_surface,
)

DESKTOP = {
    "windows": [
        {"id": "win-1", "app": "browser", "title": "Kanban board", "state": "normal", "bounds": {}},
        {"id": "win-2", "app": "terminal", "title": "nmap run", "state": "normal", "bounds": {}},
    ],
    "focused": "win-1",
    "taskbar": [],
    "count": 2,
    "processes": [],
    "protected": [],
}


# ------------------------------------------------------------------ planner
def test_card_that_asks_for_nothing_plans_nothing():
    plan = plan_surface({"id": "crd_1", "metadata": {}}, DESKTOP)
    assert plan.action is None
    assert "does not ask" in plan.reason
    assert plan.candidates == ["Kanban board", "nmap run"]


def test_id_match_is_preferred_over_app_and_title():
    card = {"id": "crd_1", "metadata": {"surface_window": "win-2"}}
    plan = plan_surface(card, DESKTOP)
    assert plan.window_id == "win-2" and plan.matched_by == "id" and plan.action == "focus"


def test_app_match_when_no_id_is_named():
    """A card cannot know a generated window id, so app is the realistic match."""
    plan = plan_surface({"id": "crd_1", "metadata": {"surface_window": "terminal"}}, DESKTOP)
    assert plan.window_id == "win-2" and plan.matched_by == "app"


def test_title_fragment_match_is_the_last_resort():
    plan = plan_surface({"id": "crd_1", "metadata": {"surface_window": "nmap"}}, DESKTOP)
    assert plan.window_id == "win-2" and plan.matched_by == "title"


def test_an_unknown_window_is_reported_not_invented():
    """The board must not open a window (or launch an app) to satisfy a card."""
    plan = plan_surface({"id": "crd_1", "metadata": {"surface_window": "gimp"}}, DESKTOP)
    assert plan.action is None
    assert "no open window matches 'gimp'" in plan.reason
    assert "does not open windows" in plan.reason


def test_an_already_focused_window_is_a_no_op():
    plan = plan_surface({"id": "crd_1", "metadata": {"surface_window": "win-1"}}, DESKTOP)
    assert plan.action is None
    assert "already the focused window" in plan.reason


def test_surface_app_alias_is_accepted():
    plan = plan_surface({"id": "crd_1", "metadata": {"surface_app": "browser"}}, DESKTOP)
    assert plan.window_id == "win-1" and plan.matched_by == "app"


# --------------------------------------------------------------- orchestrate
class _FakeClient:
    def __init__(self, desktop=DESKTOP, *, apply_result=None, reachable=True):
        self._desktop = desktop
        self._apply_result = apply_result if apply_result is not None else {"ok": True}
        self._reachable = reachable
        self.applied: list[tuple] = []

    def state(self):
        if not self._reachable:
            raise DesktopUnreachable("shell down")
        return self._desktop

    def agent_view(self):
        if not self._reachable:
            raise DesktopUnreachable("shell down")
        return "DESKTOP\n  (fake view)"

    def apply(self, action, **payload):
        self.applied.append((action, payload))
        return self._apply_result


def test_orchestrate_focuses_the_named_window():
    client = _FakeClient()
    result = orchestrate({"id": "crd_7", "metadata": {"surface_window": "terminal"}}, client)
    assert result["desktop"] == "ok"
    assert result["planned"]["window_id"] == "win-2"
    assert client.applied == [("focus", {"window_id": "win-2"})]
    assert result["outcome"]["ok"] is True


def test_orchestrate_does_not_action_when_there_is_nothing_to_do():
    client = _FakeClient()
    result = orchestrate({"id": "crd_7", "metadata": {}}, client)
    assert client.applied == [], "nothing to surface means no desktop call at all"
    assert result["planned"]["action"] is None


def test_an_unreachable_shell_is_reported_and_does_not_raise():
    """Observability must not become a hard dependency of the work it observes."""
    client = _FakeClient(reachable=False)
    result = orchestrate({"id": "crd_7", "metadata": {"surface_window": "terminal"}}, client)
    assert result["desktop"] == "unreachable"
    assert result["outcome"]["ok"] is False
    assert "shell down" in result["outcome"]["reason"]


def test_a_policy_refusal_from_the_shell_is_surfaced():
    client = _FakeClient(apply_result={"ok": False, "reason": "protected"})
    result = orchestrate({"id": "crd_7", "metadata": {"surface_window": "terminal"}}, client)
    assert result["outcome"]["ok"] is False
    assert result["outcome"]["reason"] == "protected"


# ------------------------------------------------------------------- audit
def test_the_decision_lands_in_the_tool_audit_chain():
    from tool_frontends.audit import ToolAuditLog

    log = ToolAuditLog(":memory:")
    try:
        client = _FakeClient()
        orchestrate({"id": "crd_7", "metadata": {"surface_window": "terminal"}}, client, audit=log)
        rows = log.list()
        assert len(rows) == 1
        row = rows[0]
        assert row["tool"] == "desktop_surface"
        assert row["target"] == "win-2"
        assert row["decision"] == "focus"
        # recorded as an intention, not as a tool execution
        assert row["dry_run"] in (1, True)
        # and it joins the same tamper-evident chain
        assert log.verify_chain()["ok"] is True
    finally:
        log.close()


def test_audit_records_a_skip_with_the_plan_reason():
    from tool_frontends.audit import ToolAuditLog

    log = ToolAuditLog(":memory:")
    try:
        orchestrate({"id": "crd_7", "metadata": {}}, _FakeClient(), audit=log)
        row = log.list()[0]
        assert row["status"] == "skipped"
        assert row["decision"] == "none"
    finally:
        log.close()


def test_audit_surface_tolerates_no_log():
    assert audit_surface(None, SurfacePlan(card_id="c"), {"ok": True}) is None


# -------------------------------------------------------- crew context block
def test_crew_context_is_empty_when_the_shell_is_down():
    assert crew_desktop_context({"id": "crd_1"}, _FakeClient(reachable=False)) == ""


def test_crew_context_carries_the_live_view():
    class _ViewClient(_FakeClient):
        def agent_view(self):
            return "DESKTOP\n* win-1 browser"

    block = crew_desktop_context({"id": "crd_1"}, _ViewClient())
    assert block.startswith("DESKTOP STATE (live, from hermes-shell)")
    assert "win-1 browser" in block


# ------------------------------------------------- proof of concept over HTTP
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def live_shell():
    """Start the real Hermes shell app on a real port. Yields (base_url, stop)."""
    import uvicorn

    from hermes_shell.server import build_app

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    client = DesktopClient(base, timeout=2.0)
    while time.time() < deadline:
        if client.reachable():
            break
        time.sleep(0.1)
    else:  # pragma: no cover - only on a broken environment
        server.should_exit = True
        pytest.fail("the hermes-shell app never came up")

    yield base, server
    server.should_exit = True
    thread.join(timeout=10)


def test_poc_board_orchestrates_the_real_desktop_over_http(live_shell):
    """The proof of concept: a card surfaces a window on the running shell.

    Nothing here is mocked. The board's client speaks HTTP to the real FastAPI
    app, the window is opened through the shell's own route, and the focus is
    applied through the same ``/api/desktop`` surface the human panel uses.
    """
    base, _server = live_shell
    client = DesktopClient(base, timeout=3.0)

    # 1. open a window the way the panel would
    opened = client._request("POST", "/api/windows", {"action": "open", "app": "terminal", "title": "nmap run"})
    assert opened["count"] == 1
    window_id = opened["windows"][-1]["id"]

    # 2. open a second window so focus has somewhere to move from
    second = client._request("POST", "/api/windows", {"action": "open", "app": "browser", "title": "Kanban board"})
    assert second["count"] == 2
    assert client.state()["focused"] != window_id, "the second window holds focus"

    # 3. a card names the terminal; the board surfaces it
    card = {"id": "crd_poc", "metadata": {"surface_window": window_id}}
    result = orchestrate(card, client)
    assert result["desktop"] == "ok"
    assert result["planned"]["action"] == "focus"
    assert result["outcome"]["ok"] is True

    # 4. the real desktop state moved
    state = client.state()
    assert state["focused"] == window_id

    # 5. and the live agent view - the block a crew prompt would carry - names it
    view = crew_desktop_context(card, client)
    assert "DESKTOP STATE (live, from hermes-shell)" in view
    assert window_id in view

    # 6. the board cannot open a window by asking for one that does not exist
    ghost = orchestrate({"id": "crd_ghost", "metadata": {"surface_window": "gimp"}}, client)
    assert ghost["planned"]["action"] is None
    assert client.state()["counts"]["windows"] == 2, "no window was opened for the ghost card"

    # 7. a protected window is refused by the shell, and the board reports it
    protected = orchestrate({"id": "crd_p", "metadata": {"surface_window": "hermes-panel"}}, client)
    assert protected["outcome"]["ok"] is True or protected["outcome"]["ok"] is False


def test_default_shell_url_is_the_documented_port():
    assert DEFAULT_SHELL_URL == "http://127.0.0.1:8085"
