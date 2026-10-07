"""The **agent-facing** desktop surface: the layer the OS agent manages windows and
processes through.

Item 3 of the Dream list: "AI desktop manager layer prototype for Kali Linux -
window/process management surface exposed to the agent layer, integrated into the
existing desktop/Hermes session code."

What was already there, and what this adds
------------------------------------------
``wm.py`` is the window *state machine* and ``launcher.py`` turns registry entries
into managed windows. Both are driven from the panel over ``/api/windows`` and
``/api/launcher/launch`` - that is, by a **human** clicking. There was no surface
for the agent itself: given "the crew needs the httpx result on screen", nothing
in the shell could answer "which window is that, is it open, bring it forward".

This module is that surface, and the design constraint that shapes all of it is
that an agent driving a desktop is **not** a user driving a desktop:

1. **The agent gets intentions, not exec.** It can ask to focus or close a
   *window*, or to end an allow-listed *process*. It cannot spawn a shell, and it
   cannot name a signal to send - ``terminate_process`` is the only process verb
   and it always raises ``SIGTERM``. A generic "run this" surface would make the
   whole tier system upstream decorative, because the desktop would be the
   unguarded back door into the same machine the tool layer carefully scopes.

2. **Some windows are protected.** The Hermes panel itself and the terminal the
   session runs in cannot be closed by the agent. An agent that can close its own
   UI, or the shell an operator is mid-command in, has a failure mode where the
   human loses their context and cannot see why. Refusals are returned as data
   (``{"ok": False, "reason": ...}``) rather than raised, because the agent must
   be able to *plan around* a protected window.

3. **Process actions are dry-run unless explicitly live.** Same rule the tool
   layer uses (``log_rotate`` and friends): the default call describes what it
   would do. ``live=True`` is the reviewed act.

4. **It is probeable without a real desktop.** :class:`ProcessSource` is the seam.
   In the image it reads ``/proc``; in tests it is a fake. Every policy decision
   above is therefore testable in CI with no X server, no Wayland compositor and
   no processes to kill - which is the only way this stays covered on a headless
   build host.

What this is *not*
------------------
A prototype, and deliberately shallow: it manages processes and windows **within
this host's namespace**. It does not talk to a compositor (no Wayland protocol
here - the GTK client in ``gtk/`` is the surface for that), does not know about
X11 windows, and does not persist anything. The point is to fix the *shape* of
the agent's desktop authority before anything depends on it.
"""
from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

#: Windows the agent may never close. The panel is the agent's own output surface
#: and ``hermes-session`` is the operator's terminal - closing either is how an
#: agent makes its own work unobservable.
PROTECTED_WINDOW_IDS = ("hermes-panel", "hermes-session")

#: Programs an agent may end. An allow-list rather than a deny-list: the set of
#: processes an agent has a legitimate reason to reap is small and known (a scan
#: it started, a stuck probe), while the set it must never touch is everything
#: else. Enumerating the permitted few fails closed.
TERMINABLE_COMMS = (
    "nmap",
    "nikto",
    "sqlmap",
    "httpx",
    "masscan",
    "gobuster",
    "ffuf",
    "hydra",
    "curl",
    "dig",
    "whois",
)

#: Never signalled, whatever the allow-list says: pid 1 is the init the whole
#: session depends on, and the running agent must not end itself mid-plan.
NEVER_SIGNAL_PIDS = (1,)


@dataclass
class ProcInfo:
    """One process as the manager sees it. A subset of ``/proc/<pid>/stat``."""

    pid: int
    comm: str
    state: str = "?"
    ppid: int = 0
    cmdline: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "comm": self.comm,
            "state": self.state,
            "ppid": self.ppid,
            "cmdline": self.cmdline[:200],
            "terminable": self.comm in TERMINABLE_COMMS,
        }


class ProcessSource(Protocol):
    """Where process truth comes from. ``/proc`` in the image, a fake in tests."""

    def list(self) -> list[ProcInfo]:  # pragma: no cover - protocol
        ...


class ProcFS:
    """Read the real process table from ``/proc``.

    Implemented against ``/proc`` directly rather than pulling in ``psutil``: the
    image already has ``/proc``, and a manager whose only dependency is the kernel
    cannot be broken by a Python packaging change on a live host. Unreadable
    entries are skipped - a process exiting mid-scan is normal, not an error.
    """

    def __init__(self, root: str = "/proc") -> None:
        self.root = Path(root)

    def list(self) -> list[ProcInfo]:
        out: list[ProcInfo] = []
        try:
            entries = sorted(
                (p for p in self.root.iterdir() if p.name.isdigit()),
                key=lambda p: int(p.name),
            )
        except OSError:
            return out
        for entry in entries:
            try:
                pid = int(entry.name)
                stat = (entry / "stat").read_text(errors="replace")
                # comm is in parentheses and may contain spaces, so split on the
                # last ')' rather than on whitespace.
                open_paren = stat.index("(")
                close_paren = stat.rindex(")")
                comm = stat[open_paren + 1 : close_paren]
                rest = stat[close_paren + 2 :].split()
                state = rest[0] if rest else "?"
                ppid = int(rest[1]) if len(rest) > 1 else 0
                raw = (entry / "cmdline").read_bytes()
                cmdline = raw.replace(b"\x00", b" ").decode(errors="replace").strip()
                out.append(ProcInfo(pid=pid, comm=comm, state=state, ppid=ppid, cmdline=cmdline))
            except (OSError, ValueError, IndexError):
                continue
        return out


class FakeProcSource:
    """An in-memory ``/proc``. Used by the tests, and useful for a dry demo."""

    def __init__(self, procs: Optional[list[ProcInfo]] = None) -> None:
        self._procs = list(procs or [])

    def set(self, procs: list[ProcInfo]) -> None:
        self._procs = list(procs)

    def list(self) -> list[ProcInfo]:
        return list(self._procs)


class DesktopManager:
    """The agent's single entry point to desktop state and desktop actions."""

    def __init__(
        self,
        wm: Any,
        *,
        procs: Optional[ProcessSource] = None,
        launcher: Any = None,
        protected_windows: tuple[str, ...] = PROTECTED_WINDOW_IDS,
        terminable_comms: tuple[str, ...] = TERMINABLE_COMMS,
        actor: str = "agent",
    ) -> None:
        self.wm = wm
        self.procs = procs or ProcFS()
        self.launcher = launcher
        self.protected_windows = tuple(protected_windows)
        self.terminable_comms = tuple(terminable_comms)
        self.actor = actor
        #: Every refusal, kept so the agent can report *why* a plan step did not
        #: happen. A silent no-op is how a desktop manager becomes untrustworthy.
        self.refusals: list[dict[str, Any]] = []

    # ------------------------------------------------------------- reads
    def windows(self) -> list[dict[str, Any]]:
        return self.wm.snapshot()["windows"]

    def processes(self, *, only_terminable: bool = False) -> list[dict[str, Any]]:
        procs = [p.as_dict() for p in self.procs.list()]
        if only_terminable:
            procs = [p for p in procs if p["terminable"]]
        return procs

    def state(self) -> dict[str, Any]:
        """The full desktop model: windows, processes and what is protected."""
        snap = self.wm.snapshot()
        return {
            "windows": snap["windows"],
            "focused": snap["focused"],
            "taskbar": snap["taskbar"],
            "processes": self.processes(),
            "protected": [{"id": wid, "reason": "agent may not close this window"} for wid in self.protected_windows],
            "terminable_comms": list(self.terminable_comms),
            "counts": {
                "windows": snap["count"],
                "processes": len(self.procs.list()),
            },
        }

    def agent_view(self) -> str:
        """A compact text view an LLM can hold in context.

        Deliberately terse - one line per window and per *interesting* process -
        because this is meant to be prepended to a crew prompt, and dumping the
        host's whole process table into a context window is both expensive and
        unhelpful. Only the terminable processes are listed: those are the ones an
        agent can actually act on.
        """
        snap = self.wm.snapshot()
        lines = ["DESKTOP"]
        if not snap["windows"]:
            lines.append("  (no windows open)")
        for win in snap["windows"]:
            marker = "*" if win["id"] == snap["focused"] else " "
            lines.append(f" {marker} {win['id']:<24} {win['app']:<14} {win['state']:<10} {win['title']}")
        term = [p.as_dict() for p in self.procs.list() if p.comm in self.terminable_comms]
        lines.append(f"PROCESSES ({len(term)} terminable)")
        for proc in term[:20]:
            lines.append(f"    {proc['pid']:<8} {proc['comm']:<10} {proc['cmdline'][:70]}")
        if len(term) > 20:
            lines.append(f"    ... {len(term) - 20} more")
        return "\n".join(lines)

    # ------------------------------------------------------------ policy
    def _refuse(self, reason: str, **extra: Any) -> dict[str, Any]:
        entry = {"ok": False, "refused": True, "reason": reason, **extra}
        self.refusals.append(entry)
        return entry

    def is_protected(self, window_id: str) -> bool:
        """Is this window one the agent may not close?

        Two identifiers count, and both are needed: the window *id*, and the
        *app* it belongs to. Matching only the id would let a protected window be
        closed by addressing the window the panel actually opened - the one that
        exists in practice, since a real panel does not choose its own id.
        """
        window = self.wm.get(window_id)
        app = getattr(window, "app", "") if window is not None else ""
        return any(
            window_id == pid
            or window_id.startswith(pid)
            or (app and (app == pid or app.startswith(pid)))
            for pid in self.protected_windows
        )

    # ----------------------------------------------------------- actions
    def focus_window(self, window_id: str) -> dict[str, Any]:
        if self.wm.get(window_id) is None:
            return self._refuse(f"no such window: {window_id}", window_id=window_id)
        window = self.wm.get(window_id)
        state = self.wm.focus(window_id)
        return {
            "ok": True,
            "action": "focus",
            "window_id": window_id,
            "window": window.as_dict() if window is not None else None,
            "state": state,
        }

    def close_window(self, window_id: str) -> dict[str, Any]:
        if self.is_protected(window_id):
            return self._refuse(
                f"{window_id} is protected: the agent may not close its own panel or the session",
                window_id=window_id,
            )
        if self.wm.get(window_id) is None:
            return self._refuse(f"no such window: {window_id}", window_id=window_id)
        window = self.wm.get(window_id)
        state = self.wm.close(window_id)
        return {
            "ok": True,
            "action": "close",
            "window_id": window_id,
            "window": window.as_dict() if window is not None else None,
            "state": state,
            "focused": state["focused"] if state else None,
        }

    def minimize_window(self, window_id: str) -> dict[str, Any]:
        if self.wm.get(window_id) is None:
            return self._refuse(f"no such window: {window_id}", window_id=window_id)
        window = self.wm.get(window_id)
        state = self.wm.minimize(window_id)
        return {
            "ok": True,
            "action": "minimize",
            "window_id": window_id,
            "window": window.as_dict() if window is not None else None,
            "state": state,
        }

    def open_window(self, app: str, *, title: str = "", w: Optional[int] = None, h: Optional[int] = None) -> dict[str, Any]:
        """Open a window for an app, preferring the launcher when one is wired.

        Going through the launcher matters: it is what enforces single-instance
        ("focus the already-open app instead of duplicating it") and what records
        which registry entry a window came from. Opening directly on the WM would
        give the agent a way to bypass both.
        """
        if self.launcher is not None:
            try:
                return {"ok": True, "action": "launch", "result": self.launcher.launch(app)}
            except KeyError:
                return self._refuse(f"no such launcher entry: {app}")
            except PermissionError as exc:
                return self._refuse(str(exc))
        state = self.wm.open(app, title or app, w=w, h=h)
        opened = state["windows"][-1] if state and state.get("windows") else None
        return {"ok": True, "action": "open", "window": opened, "state": state}

    def terminate_process(self, pid: int, *, live: bool = False) -> dict[str, Any]:
        """End an allow-listed process. Dry-run unless ``live=True``.

        The allow-list is checked against the process's *current* ``comm``, read
        from the source at call time - never against anything the caller supplied.
        A caller that could name the process could name one it is not allowed to
        end.
        """
        # Checked before the process table is consulted: the manager must refuse
        # to end itself even when its own pid is not visible to the source it was
        # given (a fake source in tests, a restricted /proc in the image). The
        # refusal is about the caller, not about what the table happens to show.
        if pid == os.getpid():
            return self._refuse("refusing to signal the running agent", pid=pid)
        info = next((p for p in self.procs.list() if p.pid == pid), None)
        if info is None:
            return self._refuse(f"no such process: {pid}", pid=pid)
        if pid in NEVER_SIGNAL_PIDS:
            return self._refuse(f"pid {pid} is never signalled", pid=pid)
        if info.comm not in self.terminable_comms:
            return self._refuse(
                f"'{info.comm}' is not on the terminable allow-list", pid=pid, comm=info.comm
            )
        if not live:
            return {
                "ok": True,
                "action": "terminate",
                "pid": pid,
                "comm": info.comm,
                "signal": "SIGTERM",
                "dry_run": True,
                "detail": f"would send SIGTERM to {pid} ({info.comm})",
            }
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            return self._refuse(f"could not signal {pid}: {exc}", pid=pid)
        return {
            "ok": True,
            "action": "terminate",
            "pid": pid,
            "comm": info.comm,
            "signal": "SIGTERM",
            "dry_run": False,
        }

    def apply(self, action: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """One entry point for the agent, mirroring ``WindowManager.apply``.

        Unknown actions are refused as data rather than raising, so an agent that
        guesses a verb gets a reason it can act on instead of a 500.
        """
        payload = payload or {}
        wid = payload.get("window_id") or payload.get("id")
        if action == "focus":
            return self.focus_window(str(wid))
        if action == "close":
            return self.close_window(str(wid))
        if action == "minimize":
            return self.minimize_window(str(wid))
        if action == "open":
            return self.open_window(
                str(payload.get("app") or ""),
                title=str(payload.get("title") or ""),
                w=payload.get("w"),
                h=payload.get("h"),
            )
        if action == "terminate":
            return self.terminate_process(int(payload.get("pid") or 0), live=bool(payload.get("live")))
        return self._refuse(f"unknown desktop action: {action!r}")
