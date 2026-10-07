"""The agent-facing desktop manager (item 3).

The point of these tests is that **the policy holds**, not that the plumbing
connects. An agent driving a desktop can go wrong in ways a user cannot:

* it can close the panel it reports through, or the terminal an operator is
  working in - so protected windows must refuse;
* it can name a process it has no business ending - so the allow-list is checked
  against the process's *real* ``comm``, never against the argument;
* it can end itself or the session's init - so pid 1 and its own pid refuse;
* it can act destructively by accident - so signals are dry-run unless ``live``.

The last test is a genuine one: it starts a real process and ends it, because a
manager that only ever ran against a fake ``/proc`` would be untested in the one
place it touches the kernel.
"""
from __future__ import annotations

import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from hermes_shell.agent_desktop import (
    NEVER_SIGNAL_PIDS,
    PROTECTED_WINDOW_IDS,
    TERMINABLE_COMMS,
    DesktopManager,
    FakeProcSource,
    ProcFS,
    ProcInfo,
)
from hermes_shell.wm import WindowManager


@pytest.fixture()
def wm():
    return WindowManager(width=1280, height=800)


@pytest.fixture()
def fake_procs():
    return FakeProcSource(
        [
            ProcInfo(pid=1, comm="init", state="S", ppid=0, cmdline="/sbin/init"),
            ProcInfo(pid=42, comm="nmap", state="R", ppid=7, cmdline="nmap -sV 10.0.0.5"),
            ProcInfo(pid=43, comm="bash", state="S", ppid=7, cmdline="bash -i"),
        ]
    )


@pytest.fixture()
def mgr(wm, fake_procs):
    return DesktopManager(wm, procs=fake_procs, launcher=None)


# ------------------------------------------------------------------ reads
def test_state_reports_windows_processes_and_the_protected_set(mgr, wm):
    wm.open("browser", "Board")
    state = mgr.state()
    assert state["counts"]["windows"] == 1
    assert state["counts"]["processes"] == 3
    assert {p["id"] for p in state["protected"]} == set(PROTECTED_WINDOW_IDS)
    assert state["terminable_comms"] == list(TERMINABLE_COMMS)


def test_processes_marks_what_is_terminable(mgr):
    procs = {p["pid"]: p for p in mgr.processes()}
    assert procs[42]["terminable"] is True  # nmap
    assert procs[43]["terminable"] is False  # bash
    only = mgr.processes(only_terminable=True)
    assert [p["pid"] for p in only] == [42]


def test_agent_view_is_compact_and_names_the_focus(mgr, wm):
    first = wm.open("browser", "Board")["windows"][-1]["id"]
    second = wm.open("terminal", "Session")["windows"][-1]["id"]
    wm.focus(second)
    view = mgr.agent_view()
    assert "DESKTOP" in view and "PROCESSES" in view
    assert second in view and first in view
    # only the terminable process is listed, and it is the one that matters
    assert "nmap" in view and "bash" not in view
    assert len(view.splitlines()) < 20, "the view is meant to fit in a prompt"


# ------------------------------------------------------- window policy
def test_closing_a_protected_window_is_refused(mgr, wm):
    panel = wm.open("hermes-panel", "Hermes")["windows"][-1]["id"]
    # Protection is keyed on the window's *app* as well as a literal id, so the
    # window the panel actually opened is refused even though its id was generated.
    assert mgr.is_protected(panel) is True
    result = mgr.close_window(panel)
    assert result["ok"] is False and "protected" in result["reason"]
    for protected in PROTECTED_WINDOW_IDS:
        assert mgr.is_protected(protected) is True
        assert mgr.close_window(protected)["ok"] is False
    assert wm.get(panel) is not None, "the protected window is still there"


def test_closing_an_ordinary_window_works(mgr, wm):
    wid = wm.open("browser", "Board")["windows"][-1]["id"]
    assert mgr.is_protected(wid) is False
    result = mgr.close_window(wid)
    assert result["ok"] is True
    assert result["window"]["id"] == wid, "the result names the window it closed"
    assert wm.get(wid) is None


def test_focus_and_minimize_refuse_unknown_windows_as_data(mgr):
    for action in ("focus_window", "close_window", "minimize_window"):
        result = getattr(mgr, action)("nope-123")
        assert result["ok"] is False and result["refused"] is True
    assert len(mgr.refusals) == 3, "every refusal is recorded for the agent to read"


def test_refusals_do_not_raise(mgr):
    """An agent must be able to plan around a refusal, so it is data, not an exception."""
    assert mgr.apply("close", {"window_id": "hermes-session"})["ok"] is False
    assert mgr.apply("nonsense", {})["ok"] is False


# ----------------------------------------------------- process policy
def test_terminate_is_dry_run_by_default(mgr):
    result = mgr.terminate_process(42)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["signal"] == "SIGTERM"
    assert "would send SIGTERM" in result["detail"]


def test_terminate_refuses_a_process_not_on_the_allow_list(mgr):
    result = mgr.terminate_process(43)  # bash
    assert result["ok"] is False
    assert "allow-list" in result["reason"]


def test_terminate_refuses_init_and_itself(mgr):
    for pid in NEVER_SIGNAL_PIDS:
        assert mgr.terminate_process(pid)["ok"] is False
    import os

    result = mgr.terminate_process(os.getpid())
    assert result["ok"] is False
    assert "running agent" in result["reason"]


def test_terminate_refuses_an_unknown_pid(mgr):
    result = mgr.terminate_process(99999)
    assert result["ok"] is False and "no such process" in result["reason"]


def test_the_allow_list_is_checked_against_real_comm_not_the_argument(wm):
    """A caller that could describe the process could describe a permitted one."""
    procs = FakeProcSource([ProcInfo(pid=77, comm="systemd", state="S", ppid=1, cmdline="systemd")])
    mgr = DesktopManager(wm, procs=procs)
    # 77 is *called* systemd; the caller cannot relabel it as 'nmap'
    result = mgr.terminate_process(77, live=True)
    assert result["ok"] is False and "not on the terminable allow-list" in result["reason"]


def test_live_terminate_actually_ends_a_real_process(wm):
    """The one test that touches the kernel: start a process, end it, confirm."""
    proc = subprocess.Popen(["sleep", "300"])
    try:
        mgr = DesktopManager(wm, procs=ProcFS(), terminable_comms=("sleep",))
        # /proc reports the comm; give the kernel a moment to publish it
        deadline = time.time() + 5
        listed = None
        while time.time() < deadline:
            listed = next((p for p in mgr.processes() if p["pid"] == proc.pid), None)
            if listed is not None:
                break
            time.sleep(0.05)
        assert listed is not None, "the spawned process never appeared in /proc"
        assert listed["comm"] == "sleep"

        result = mgr.terminate_process(proc.pid, live=True)
        assert result["ok"] is True and result["dry_run"] is False

        proc.wait(timeout=5)
        assert proc.returncode is not None, "the process should be gone"
    finally:
        if proc.poll() is None:
            proc.kill()


# --------------------------------------------------------- HTTP surface
def test_routes_expose_the_desktop_and_refuse_cleanly():
    from hermes_shell.server import build_app

    client = TestClient(build_app())
    state = client.get("/api/desktop")
    assert state.status_code == 200
    body = state.json()
    assert set(body) >= {"windows", "focused", "processes", "protected", "counts"}

    view = client.get("/api/desktop/agent-view")
    assert view.status_code == 200
    assert "DESKTOP" in view.text

    # a protected close is answered 200 with ok=false - a policy outcome, not an
    # HTTP error, so the agent can plan around it
    refused = client.post("/api/desktop", json={"action": "close", "window_id": "hermes-panel"})
    assert refused.status_code == 200
    assert refused.json()["ok"] is False

    assert client.post("/api/desktop", json={}).status_code == 400
    unknown = client.post("/api/desktop", json={"action": "explode"})
    assert unknown.status_code == 200 and unknown.json()["ok"] is False
