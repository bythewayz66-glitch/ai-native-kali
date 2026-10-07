"""Hermes shell panel: the desktop surface that renders the Kanban board.

Blueprint ref: section 03.3 (Kanban <-> Hermes Desktop shell integration).

This is the demonstrable slice of the desktop shell: a full-height panel that
renders live board state plus the three shell affordances the blueprint calls
for - a taskbar widget, a notification feed and a per-card drawer that opens the
relevant tool window two-way. It is a thin client: every pixel comes from
kanban-core / agent-runtime / observability over HTTP, so what you see is real
system state, not a mock-up.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .agent_desktop import DesktopManager
from .attach import artifact_payload, entry_from_drop
from .file_manager import BrowseRefused, FileManager
from .launcher import AppRegistry, Launcher, build_registry
from .overlay import OverlayClient, collect, compose, probe_services
from .setup_wizard import SetupWizard, probe_readiness
from .target_drop import audit_drop, card_payload, plan_drop, scope_payload
from .wm import WindowManager

STATIC_DIR = Path(__file__).parent / "static"


def _forward(method: str, path: str, payload: Optional[dict[str, Any]] = None, *, status: int = 200) -> JSONResponse:
    """Call Kanban core on the shell's behalf and pass the outcome through.

    The status code is preserved rather than flattened to 200/500, so the panel's
    rejection-of-an-illegal-transition path works identically whether the card was
    refused by the shell or by the board. Collapsing a 409 into a 200 with an
    error body is how a UI ends up reporting success for a refused move.
    """
    base = os.environ.get("KANBAN_URL", "http://127.0.0.1:8081").rstrip("/")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base}{path}",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return JSONResponse(parsed, status_code=response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"detail": raw or str(exc)}
        return JSONResponse(parsed, status_code=exc.code)
    except Exception as exc:  # noqa: BLE001 - an unreachable board is a 502, not a 500
        return JSONResponse({"detail": f"kanban-core unreachable: {exc}"}, status_code=502)


def build_app(*, autostart: bool = False) -> FastAPI:  # noqa: ARG001 - symmetry with siblings
    app = FastAPI(
        title="AI-native Kali - Hermes Shell Panel",
        version="0.1.0",
        description="Desktop shell surface: live Kanban board, taskbar widget, notification feed.",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )

    #: One window manager per shell process. The browser holds no window state of
    #: its own - it sends intentions and renders what comes back - so a reloaded
    #: panel shows the same windows rather than an empty desktop.
    wm = WindowManager(width=int(os.environ.get("SHELL_W", "1280")), height=int(os.environ.get("SHELL_H", "800")))

    #: Built once and reused: the registry scans the filesystem and the tool
    #: catalogue, so rebuilding it per request would make the launcher's latency
    #: proportional to the number of installed tools.
    _registry_cache: dict[str, Any] = {}

    def _registry() -> AppRegistry:
        if "reg" not in _registry_cache:
            _registry_cache["reg"] = build_registry()
        return _registry_cache["reg"]

    #: The launcher is a singleton for the same reason the window manager is: its
    #: whole contract is "an already-open app is focused, not duplicated", and it
    #: remembers what it opened. Built per request it would forget every launch, so
    #: clicking an app twice would open two windows - the exact bug single-instance
    #: behaviour exists to prevent. The route tests caught this.
    _launcher_cache: dict[str, Any] = {}

    def _launcher() -> Launcher:
        if "l" not in _launcher_cache:
            _launcher_cache["l"] = Launcher(_registry(), wm)
        return _launcher_cache["l"]

    _audit_cache: dict[str, Any] = {}

    #: The file manager is a singleton like the window manager and launcher, and
    #: it is *stateless* besides its configuration - so one instance is enough and
    #: rebuilding it per request would only re-normalise the roots each time. The
    #: roots come from the environment so an image can widen or narrow them
    #: without a code change; the default stays narrow (see file_manager).
    _fm_cache: dict[str, Any] = {}

    def _files() -> FileManager:
        if "fm" not in _fm_cache:
            raw = os.environ.get("SHELL_FILE_ROOTS", "")
            roots = tuple(r for r in raw.split(":") if r) or None
            _fm_cache["fm"] = FileManager(roots=roots) if roots else FileManager()
        return _fm_cache["fm"]

    def _audit() -> Any:
        """The tool audit log for drop events.

        Behind a flag, like the memory store: a shell whose audit log cannot be
        opened must still let a user work, and an unrecorded drop is better than
        a drop that appears to fail for an unrelated reason.
        """
        if "log" not in _audit_cache:
            try:
                from pathlib import Path as _Path

                from tool_frontends.audit import ToolAuditLog

                path = _Path(os.environ.get("SHELL_AUDIT_LOG", "var/tool-audit.jsonl"))
                path.parent.mkdir(parents=True, exist_ok=True)
                _audit_cache["log"] = ToolAuditLog(path)
            except Exception:  # noqa: BLE001 - auditing is best-effort here
                _audit_cache["log"] = None
        return _audit_cache["log"]

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "hermes-shell",
            "version": "0.1.0",
            "panel": "/panel",
            "surfaces": [
                "kanban_board",
                "taskbar_widget",
                "notification_feed",
                "card_drawer",
                "observability",
                "file_manager_drop",
                "overlay_widget",
                "alert_routed_feed",
                "window_manager",
                "app_launcher",
                "target_drop",
                "setup_wizard",
            ],
        }

    @app.get("/config")
    def config() -> dict[str, Any]:
        """Endpoints the panel's JS should talk to.

        Served by the shell rather than hardcoded in the page so the same HTML
        works in docker-compose, in `make dev`, and in the ISO.
        """
        return {
            "kanban_url": os.environ.get("KANBAN_URL", "http://127.0.0.1:8081"),
            "runtime_url": os.environ.get("RUNTIME_URL", "http://127.0.0.1:8082"),
            "tools_url": os.environ.get("TOOLS_URL", "http://127.0.0.1:8083"),
            "observability_url": os.environ.get("OBS_URL", "http://127.0.0.1:8084"),
            "memory_url": os.environ.get("MEMORY_URL", "http://127.0.0.1:8087"),
            "poll_ms": int(os.environ.get("SHELL_POLL_MS", "3000")),
        }

    # ------------------------------------------------- file-manager drop
    @app.post("/api/attach/{card_id}", status_code=201)
    def attach(card_id: str, entry: dict[str, Any] = Body(...)) -> JSONResponse:
        """Attach a file-manager entry to a card as an artifact.

        The shell builds the artifact and forwards it to Kanban core rather than
        writing to the board's database itself: attachment is a board operation
        and must go through the same route (and therefore the same guards and
        audit) as an attachment made by a crew. A shell that could write
        artifacts directly would be a second, unaudited way to change a card.
        """
        try:
            normalised = entry_from_drop(entry)
            payload = artifact_payload(
                filename=normalised["name"],
                path=normalised["path"],
                size=normalised["size"],
                produced_by="hermes-shell",
                summary=f"attached from the Hermes file manager on card {card_id}",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _forward("POST", f"/api/cards/{card_id}/artifacts", payload, status=201)

    # ------------------------------------------------ floating overlay
    @app.get("/api/overlay")
    def overlay_state() -> JSONResponse:
        """The overlay's render model, gathered from the live services.

        Merges two kinds of truth: what the board says (counters, gates) and what
        the services say about themselves (reachability). The merge is ``max``,
        so one downed service cannot be hidden behind an otherwise healthy board.
        """
        kanban_url = os.environ.get("KANBAN_URL", "http://127.0.0.1:8081")
        services = [
            {"name": name, "url": os.environ.get(env, default)}
            for name, env, default in (
                ("kanban-core", "KANBAN_URL", "http://127.0.0.1:8081"),
                ("agent-runtime", "RUNTIME_URL", "http://127.0.0.1:8082"),
                ("tool-frontends", "TOOLS_URL", "http://127.0.0.1:8083"),
                ("observability", "OBS_URL", "http://127.0.0.1:8084"),
                ("memory-store", "MEMORY_URL", "http://127.0.0.1:8087"),
            )
        ]
        client = OverlayClient(
            kanban_url,
            services=probe_services(services),
            # Phase 6 item 3: the overlay renders what the alert router delivered,
            # not just what the board implies. Pointed at the observability
            # service because that is where routing lives - the shell must not
            # grow a second, divergent copy of the dedupe/severity policy.
            obs_url=os.environ.get("OBS_URL", "http://127.0.0.1:8084"),
        )
        model = compose(collect(client))
        return JSONResponse(model)

    # ------------------------------------------------- window manager (4a)
    @app.get("/api/windows")
    def windows() -> JSONResponse:
        """The desktop state: z-order bottom-to-top, focus, taskbar list."""
        return JSONResponse(wm.snapshot())

    @app.post("/api/windows")
    def window_action(body: dict[str, Any] = Body(...)) -> JSONResponse:
        """Apply one window intention (open/focus/close/minimize/maximize/move/...).

        Returns the resulting desktop state, so the panel never has to guess what
        its click did. A 400 for an unknown action rather than a silent no-op: a
        typo in the shell's JS should be visible immediately, not look like a
        window that refused to move.
        """
        action = str(body.get("action") or "")
        if not action:
            raise HTTPException(status_code=400, detail="action is required")
        state = wm.apply(action, body)
        if state is None:
            known = (
                "open focus close minimize maximize restore toggle_maximize move resize"
            ).split()
            # An unknown *window id* is normal (a stale click); an unknown action
            # is a programming error - so they are answered differently.
            if action not in known:
                raise HTTPException(
                    status_code=400, detail=f"unknown window action {action!r} (known: {', '.join(known)})"
                )
            return JSONResponse({"detail": "no such window", "windows": wm.snapshot()}, status_code=404)
        return JSONResponse(state)

    # ------------------------------ agent desktop manager (item 3)
    _desktop_cache: dict[str, Any] = {}

    def _desktop() -> DesktopManager:
        """The agent-facing desktop surface, sharing the panel's own window manager.

        Sharing ``wm`` is the point: a window the agent opens is the same window
        the human sees in the taskbar, and a window the human closes is gone for
        the agent too. A second manager would let the two disagree about what the
        desktop *is* - the exact class of divergence this repository keeps fixing.
        """
        if "mgr" not in _desktop_cache:
            _desktop_cache["mgr"] = DesktopManager(wm, launcher=_launcher())
        return _desktop_cache["mgr"]

    @app.get("/api/desktop")
    def desktop_state() -> JSONResponse:
        """Windows, processes, protected windows - the layer the agent manages through."""
        return JSONResponse(_desktop().state())

    @app.get("/api/desktop/agent-view", response_class=PlainTextResponse)
    def desktop_agent_view() -> Any:
        """The compact text view a crew prompt can carry in context."""
        return _desktop().agent_view()

    @app.post("/api/desktop")
    def desktop_action(body: dict[str, Any] = Body(...)) -> JSONResponse:
        """Apply one agent desktop intention (focus/close/minimize/open/terminate).

        A refusal answers 200 carrying ``{"ok": false, "reason": ...}`` rather
        than a 4xx. The agent is expected to *plan around* a protected window, and
        an HTTP error would make that ordinary policy outcome look like a
        transport failure.
        """
        action = str(body.get("action") or "")
        if not action:
            raise HTTPException(status_code=400, detail="action is required")
        return JSONResponse(_desktop().apply(action, body))

    @app.get("/api/launcher")
    def launcher_index(q: str = "", kind: str = "") -> JSONResponse:
        """The application registry, optionally filtered (Phase 7, item 1).

        ``q`` runs the ranked search; an empty ``q`` lists entries in display
        order. Both are the same call so the launcher surface has one data path.
        """
        registry = _registry()
        entries = (
            registry.search(q, limit=int(os.environ.get("SHELL_LAUNCH_LIMIT", "30")), kind=kind or None)
            if q.strip()
            else registry.entries(kind=kind or None)
        )
        return JSONResponse(
            {
                "query": q,
                "kind": kind,
                "counts": registry.counts(),
                "entries": [e.as_dict() for e in entries],
            }
        )

    @app.post("/api/launcher/launch")
    def launcher_launch(body: dict[str, Any] = Body(...)) -> JSONResponse:
        """Launch an app, which opens it as a managed window (Phase 7, item 1).

        Launching through the *same* window manager the panel renders means a
        launched app is an ordinary window: focusable, minimisable, in the
        taskbar, and subject to the same state machine. Opening a window by any
        other path would create one the WM does not know about.
        """
        entry_id = str(body.get("id") or "")
        if not entry_id:
            raise HTTPException(status_code=400, detail="id is required")
        try:
            result = _launcher().launch(entry_id, payload=body.get("payload") or {})
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no such launcher entry {entry_id!r}") from None
        except PermissionError as exc:
            # A refused launch is a policy outcome, not a server error.
            return JSONResponse({"detail": str(exc)}, status_code=403)
        return JSONResponse(result)

    # ------------------------------------------- file manager (item 9)
    @app.get("/api/files")
    def files_list(path: str = "") -> JSONResponse:
        """Browse the filesystem, confined to the configured roots (Phase 16).

        No ``path`` returns just the status - the roots and their readability -
        which is what the surface needs to draw its tree root. With a ``path`` it
        returns that directory's listing.

        A path outside the roots is a **403 with a reason**, not a 500: it is a
        policy outcome, like a refused launch, and treating it as a server error
        would both mislead the caller and invite a retry.
        """
        manager = _files()
        status = manager.status()
        if not path:
            return JSONResponse({**status, "path": None, "entries": [], "count": 0})
        try:
            listing = manager.list_dir(path)
        except BrowseRefused as exc:
            return JSONResponse({"detail": exc.reason, **status}, status_code=403)
        return JSONResponse(listing)

    @app.get("/api/files/preview")
    def files_preview(path: str, max_bytes: int = 0) -> JSONResponse:
        """Read the head of a text file, for a preview pane. Refuses binary."""
        try:
            body = _files().read_preview(path, max_bytes=max_bytes or None)
        except BrowseRefused as exc:
            return JSONResponse({"detail": exc.reason}, status_code=403)
        return JSONResponse(body)

    @app.get("/api/files/search")
    def files_search(path: str, q: str, limit: int = 200) -> JSONResponse:
        """Name search, one level deep - a recursive walk is unbounded work."""
        try:
            body = _files().search(path, q, limit=max(1, min(limit, 1000)))
        except BrowseRefused as exc:
            return JSONResponse({"detail": exc.reason}, status_code=403)
        return JSONResponse(body)

    # ------------------------------------------- target drop (item 2)
    @app.post("/api/target-drop", status_code=201)
    def target_drop(body: dict[str, Any] = Body(...)) -> JSONResponse:
        """Drop a target onto a board or card, creating a scoped card.

        Two independent gates, both of which must pass:

        * the payload must classify as an address shape the shared classifier
          recognises - a file-manager path or an arbitrary string is refused with
          a 422, because minting a scoped card from an unclassified string is how
          a scope check starts passing everything;
        * an accepted drop is recorded in the hash-chained tool audit log, so the
          card's scope is reconstructable from the chain rather than only from the
          board row.
        """
        plan = plan_drop(body)
        if not plan.accepted:
            # Record the refusal before answering. An audit that only sees the
            # drops that worked cannot explain the one a user swears they made,
            # and the refusal is exactly the event someone will ask about.
            audit_drop(_audit(), plan, actor=str(body.get("actor") or "hermes-shell"))
            return JSONResponse(
                {"detail": plan.reason or "payload is not a target", "kind": plan.kind}, status_code=422
            )
        summary = audit_drop(_audit(), plan, actor=str(body.get("actor") or "hermes-shell"))
        card = card_payload(plan, description=str(body.get("description") or ""))
        return JSONResponse(
            {
                "accepted": True,
                "kind": plan.kind,
                "normalized": plan.target,
                "card": card,
                "scope": scope_payload(plan),
                "audit": summary,
            }
        )

    # --------------------------------------------- setup wizard (item 6)
    @app.get("/api/setup")
    def setup_state() -> JSONResponse:
        """The first-boot step model and what this host can actually do.

        Readiness is probed rather than assumed, and reported as warnings the
        shell can display - not as a refusal, because every reduced mode still
        works.
        """
        wizard = SetupWizard(readiness=probe_readiness())
        return JSONResponse(
            {
                "steps": [
                    {
                        "key": s.key,
                        "title": s.title,
                        "prompt": s.prompt,
                        "requires": s.requires,
                        "help": s.help,
                        "default": s.default,
                        "optional": s.optional,
                    }
                    for s in wizard.steps()
                ],
                "state": wizard.state(),
            }
        )

    @app.get("/", response_class=HTMLResponse)
    @app.get("/panel", response_class=HTMLResponse)
    def panel() -> Any:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="shell panel index.html is missing")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.get("/desktop-entry")
    def desktop_entry() -> JSONResponse:
        """The .desktop entry contents, for inspecting the session wiring."""
        path = Path(__file__).resolve().parents[2] / "packaging" / "hermes-session" / "hermes-shell.desktop"
        if not path.exists():
            return JSONResponse({"name": "hermes-shell.desktop", "body": "", "found": False})
        return JSONResponse({"name": path.name, "body": path.read_text(encoding="utf-8"), "found": True})

    return app


app = build_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "hermes_shell.server:app",
        host=os.environ.get("SHELL_HOST", "127.0.0.1"),
        port=int(os.environ.get("SHELL_PORT", "8085")),
        reload=False,
    )
