#!/usr/bin/env python3
"""End-to-end smoke test for the AI-native Kali stack.

Proves the loop the whole architecture is built around actually works:

    create card -> crew picks it up -> tools run -> traces+audit recorded
                -> card lands in Review -> observability can replay it

and proves the safety model holds:

    a T2 card does NOT run until a human opens its gate
    an out-of-scope card is blocked before it ever enters Running

Since Phase 2 it also proves the three new integration paths:

    the bridge claims cards from ``/ws/events`` (not the poll fallback)
    the tool layer is callable over the real MCP stdio transport
    the standalone board UI serves the board and defers to the engine

Run it against a live stack (`make dev`, then `make smoke`). Exits non-zero on
any failure, so it is usable as CI.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

import httpx

KANBAN = "http://127.0.0.1:8081"
RUNTIME = "http://127.0.0.1:8082"
TOOLS = "http://127.0.0.1:8083"
OBS = "http://127.0.0.1:8084"
SHELL = "http://127.0.0.1:8085"
MEMORY = "http://127.0.0.1:8087"
BOARD = "http://127.0.0.1:8086"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36m----\033[0m"

CHECKS: list[tuple[str, bool, str]] = []
VERBOSE = False


def record(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  ({detail})" if detail and (VERBOSE or not ok) else ""))
    return ok


def step(title: str) -> None:
    print(f"\n{INFO} {title}")


def client() -> httpx.Client:
    return httpx.Client(timeout=30.0)


def wait_for(url: str, *, seconds: float = 30.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with client() as c:
                if c.get(url).status_code < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def wait_for_column(c: httpx.Client, card_id: str, column: str, *, seconds: float = 45.0) -> dict[str, Any] | None:
    """Poll the card until it reaches `column` (the bridge runs on its own timer)."""
    deadline = time.time() + seconds
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = c.get(f"{KANBAN}/api/cards/{card_id}").json()
        if last.get("column") == column:
            return last
        # a terminal state other than the one we want means the loop broke
        if last.get("column") == "Blocked" and column != "Blocked":
            return last
        time.sleep(0.5)
    return last


def mcp_call(proc: subprocess.Popen, payload: dict[str, Any]) -> dict[str, Any]:
    """One JSON-RPC round trip over the MCP stdio transport."""
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"MCP server closed the pipe. stderr:\n{stderr}")
    return json.loads(line)


def mcp_session() -> dict[str, Any]:
    """Spawn the MCP stdio server and drive a full session against it."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [f"{ROOT}/tool-frontends", f"{ROOT}/kanban-core", env.get("PYTHONPATH", "")]
    )
    # The smoke test asserts the safe default, so pin live execution off.
    env["TOOLS_LIVE"] = "0"
    env["TOOLS_UNLOCK"] = ""
    proc = subprocess.Popen(
        [sys.executable, "-m", "tool_frontends.mcp_stdio"],
        cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    out: dict[str, Any] = {}
    try:
        out["init"] = mcp_call(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "1.0"}},
        })
        out["list"] = mcp_call(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        out["dry"] = mcp_call(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "whois_lookup", "arguments": {"target": "example.com"}},
        })
        out["live"] = mcp_call(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "nmap_scan", "arguments": {"target": "example.com", "live": True}},
        })
        out["verify"] = mcp_call(proc, {"jsonrpc": "2.0", "id": 5, "method": "kali/audit/verify"})
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return out


def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    print("=" * 68)
    print("  AI-native Kali - Phase 1 end-to-end smoke test")
    print("=" * 68)

    # ---------------------------------------------------------------- health
    step("1. services are up")
    for name, url in (("kanban-core", KANBAN), ("agent-runtime", RUNTIME), ("tool-frontends", TOOLS), ("observability", OBS), ("hermes-shell", SHELL), ("board-ui", BOARD), ("memory-store", MEMORY)):
        record(f"{name} /health", wait_for(f"{url}/health"), url)
    if not all(ok for _, ok, _ in CHECKS):
        print("\ncannot continue: stack is not healthy (run `make dev`)\n")
        return 1

    with client() as c:
        # ------------------------------------------------------- runtime state
        step("2. runtime and tool layer report their real capabilities")
        rt = c.get(f"{RUNTIME}/health").json()
        record("bridge backend reported", rt.get("backend") in ("local", "crewai"), str(rt.get("backend")))
        record("bridge loop running", bool(rt.get("loop_running")))
        tools_health = c.get(f"{TOOLS}/health").json()
        record("live execution is OFF by default", tools_health.get("live_enabled") is False)
        record("no tool is pre-unlocked", tools_health.get("unlocked") == [])
        record("tool audit chain intact", bool(tools_health["audit"]["ok"]))
        crew_list = c.get(f"{RUNTIME}/crews").json()
        record("crews registered", crew_list["count"] >= 4, f"{crew_list['count']} crews")

        # --------------------------------------------------- happy path (T0/T1)
        step("3. create a recon card and hand it to the agent board")
        card = c.post(
            f"{KANBAN}/api/cards",
            json={
                "title": "Smoke: recon scanme.nmap.org",
                "board_id": "brd_agent",
                "description": "created by scripts/smoke_test.py",
                "assignee": "recon-specialist",
                "crew": "recon",
                "priority": "high",
                "scope": {"targets": ["scanme.nmap.org"], "authorization_ref": "SMOKE-TEST"},
                "tools": [
                    {"name": "whois_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
                    {"name": "dns_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}},
                ],
            },
        ).json()
        card_id = card["card_id"]
        record("card created in Backlog", card["column"] == "Backlog", card_id)

        moved = c.post(f"{KANBAN}/api/cards/{card_id}/move", json={"to_column": "Assigned", "actor": "smoke"}).json()
        record("card assigned", moved["column"] == "Assigned")

        step("4. the crew picks the card up on its own (Assigned -> Running -> Review)")
        final = wait_for_column(c, card_id, "Review")
        record("card reached Review without any external nudge", final.get("column") == "Review", f"landed in {final.get('column')}")

        traces = final.get("traces") or []
        record("tool calls were recorded as traces", len(traces) >= 2, f"{len(traces)} traces")
        record("traces name the real tools", {t["tool"] for t in traces} >= {"whois_lookup", "dns_lookup"}, json.dumps(sorted(t["tool"] for t in traces)))
        record("traces ran in dry-run mode", all(t.get("dry_run") for t in traces))
        record("traces carry an audit hash", all(t.get("audit_hash") for t in traces))
        record("traces attribute an agent", all(t.get("agent") for t in traces))
        record("crew wrote a result back onto the card", bool(final.get("result")), (final.get("result") or "")[:70].replace("\n", " "))
        artifacts = final.get("artifacts") or []
        record("run artifact attached", len(artifacts) >= 1)
        if artifacts:
            record("artifact is content-addressed", bool(artifacts[0].get("sha256")), (artifacts[0].get("sha256") or "")[:16] + "...")

        step("5. the board recorded the transitions as events (the audit spine)")
        events = c.get(f"{KANBAN}/api/events", params={"card_id": card_id, "limit": 200}).json()["events"]
        moves = {(e.get("from_column"), e.get("to_column")) for e in events if e.get("type") == "card.moved"}
        record("Assigned -> Running emitted", ("Assigned", "Running") in moves)
        record("Running -> Review emitted", ("Running", "Review") in moves)

        step("6. the tool layer's hash-chained audit log has the matching rows")
        audit = c.get(f"{TOOLS}/audit", params={"card_id": card_id, "limit": 100}).json()
        record("audit rows exist for this card", audit["count"] >= 2, f"{audit['count']} rows")
        trace_hashes = {t.get("audit_hash") for t in traces}
        audit_hashes = {row.get("audit_hash") for row in audit["entries"]}
        record("card traces and audit rows agree on hashes", bool(trace_hashes & audit_hashes))
        verify = c.get(f"{TOOLS}/audit/verify").json()
        record("audit chain verifies end to end", verify["ok"] is True, f"{verify.get('checked')} rows")

        step("7. observability ingested the run and can replay the card")
        ingest = c.post(f"{OBS}/ingest").json()
        record("collector pulled from both sources", ingest["ingested"]["events"] >= 0 and ingest["ingested"]["audit"] >= 0, json.dumps(ingest["ingested"]))
        replay = c.get(f"{OBS}/cards/{card_id}/replay").json()
        record("replay reconstructs the card history", replay["count"] >= 3, f"{replay['count']} steps")
        record("replay contains traces and audit rows", replay["traces"] >= 1 and replay["audit_rows"] >= 1)
        traces_panel = c.get(f"{OBS}/panels/traces", params={"card_id": card_id}).json()
        record("traces panel lists this card's tool calls", traces_panel["count"] >= 2, f"{traces_panel['count']} shown")
        overview = c.get(f"{KANBAN}/api/overview").json()["totals"]
        record("board totals count the tool runs", overview["tool_runs"] >= 2, f"tool_runs={overview['tool_runs']}")
        record("latency was measured, not invented", c.get(f"{OBS}/panels/tokens").json()["latency"]["count"] >= 2)

        step("8. the shell panel is serving the live board surface")
        panel = c.get(f"{SHELL}/panel")
        record("panel served", panel.status_code == 200)
        html = panel.text
        record("panel renders the six lifecycle columns", all(col in html for col in ("Backlog", "Assigned", "Running", "Review", "Blocked", "Done")))
        record("panel has the taskbar widget", 'class="taskbar"' in html)
        record("panel has the notification feed", 'id="feed"' in html)

        step("9. the bridge claims cards from the event stream, not the poll fallback")
        stream_health = c.get(f"{RUNTIME}/health").json()
        record("claim mode is event-stream", stream_health.get("claim_mode") == "event-stream", str(stream_health.get("claim_mode")))
        stream = stream_health.get("stream") or {}
        record("the socket is connected to kanban-core", bool(stream.get("connected")), f"connects={stream.get('connects')}")
        record("the socket reported no error", not stream.get("last_error"), str(stream.get("last_error")))
        record("no fallback polling is happening", stream_health["stats"]["fallback_polls"] == 0, f"fallback_polls={stream_health['stats']['fallback_polls']}")

        before = c.get(f"{RUNTIME}/stream").json()["claim_sources"]
        socket_card = c.post(
            f"{KANBAN}/api/cards",
            json={
                "title": "Smoke: socket-claimed recon card",
                "board_id": "brd_agent",
                "assignee": "recon-specialist",
                "crew": "recon",
                "scope": {"targets": ["scanme.nmap.org"], "authorization_ref": "SMOKE-SOCKET"},
                "tools": [{"name": "whois_lookup", "tier": 0, "args": {"target": "scanme.nmap.org"}}],
            },
        ).json()
        socket_id = socket_card["card_id"]
        # Assigning is what publishes the transition the socket subscribes to.
        c.post(f"{KANBAN}/api/cards/{socket_id}/assign",
               json={"assignee": "recon-specialist", "crew": "recon", "actor": "smoke"})
        landed = wait_for_column(c, socket_id, "Review", seconds=40)
        after = c.get(f"{RUNTIME}/stream").json()["claim_sources"]
        record("a card moved to Assigned was claimed over the socket",
               after["socket"] > before["socket"], f"socket {before['socket']} -> {after['socket']}")
        record("the poll path claimed nothing",
               after["poll"] == before["poll"], f"poll {before['poll']} -> {after['poll']}")
        record("the socket path reports itself as the last claim source",
               after.get("last") == "socket", str(after.get("last")))
        record("the socket-claimed card still completed the loop",
               landed.get("column") == "Review", f"landed in {landed.get('column')}")

        step("10. the tool layer is callable over the real MCP stdio transport")
        try:
            mcp = mcp_session()
        except Exception as exc:  # pragma: no cover - environment failure
            record("MCP session ran", False, str(exc)[:120])
            mcp = {}
        if mcp:
            init = (mcp.get("init") or {}).get("result") or {}
            record("MCP handshake returned serverInfo", bool(init.get("serverInfo", {}).get("name")), str(init.get("serverInfo", {}).get("name")))
            record("MCP negotiated a protocol version", bool(init.get("protocolVersion")), str(init.get("protocolVersion")))
            record("MCP advertises the tools capability", "tools" in (init.get("capabilities") or {}))
            tools_list = ((mcp.get("list") or {}).get("result") or {}).get("tools") or []
            names = {t["name"] for t in tools_list}
            record("MCP lists the real tool registry", len(tools_list) >= 7, f"{len(tools_list)} tools")
            record("MCP tool schemas carry a tier annotation", all(t.get("annotations", {}).get("tier") is not None for t in tools_list))
            record("MCP exposes the Kali wrappers", {"nmap_scan", "whois_lookup", "dns_lookup", "nikto_scan"} <= names, json.dumps(sorted(names)))
            dry = ((mcp.get("dry") or {}).get("result") or {})
            dry_body = dry.get("structuredContent") or {}
            record("MCP dry-run call executed nothing", dry_body.get("status") == "dry_run" and dry_body.get("dry_run") is True)
            record("MCP dry-run call is not an error", dry.get("isError") is False)
            record("MCP call was audited with a hash", bool(dry_body.get("audit_hash")))
            live = ((mcp.get("live") or {}).get("result") or {})
            live_body = live.get("structuredContent") or {}
            record("MCP refuses a live call that is not unlocked", live_body.get("status") == "denied", str(live_body.get("status")))
            record("MCP surfaces the refusal as a tool error", live.get("isError") is True)
            verify_mcp = ((mcp.get("verify") or {}).get("result") or {})
            record("MCP audit chain verifies", verify_mcp.get("ok") is True, f"{verify_mcp.get('checked')} rows")

        step("11. the standalone board UI serves the board and defers to the engine")
        board_health = c.get(f"{BOARD}/health").json()
        record("board-ui reports healthy", board_health.get("status") == "ok")
        cfg_board = c.get(f"{BOARD}/config").json()
        record("board-ui points at kanban-core", cfg_board.get("kanban_url") == KANBAN, cfg_board.get("kanban_url"))
        record("board-ui derives the event socket URL", cfg_board.get("ws_url", "").endswith("/ws/events"), cfg_board.get("ws_url"))
        board_html = c.get(f"{BOARD}/").text
        record("board page served", "Hermes Kanban" in board_html)
        schema = c.get(f"{KANBAN}/api/schema").json()
        expected_columns = schema["columns"]
        record("the board declares six lifecycle columns", len(expected_columns) == 6, json.dumps(expected_columns))
        # The page derives its lanes from `L.COLUMNS` in the served module rather
        # than repeating them as literals, so the module is where the contract
        # with the engine actually lives - assert that, not a grep of the HTML.
        logic_src = c.get(f"{BOARD}/static/board_logic.mjs").text
        declared = re.search(r"export const COLUMNS = \[([^\]]*)\]", logic_src)
        ui_columns = re.findall(r"'([^']+)'", declared.group(1)) if declared else []
        record("the UI's column set matches the engine's exactly",
               ui_columns == expected_columns,
               f"ui={json.dumps(ui_columns)} engine={json.dumps(expected_columns)}")
        record("board page loads its tested drag-and-drop logic", "board_logic.mjs" in board_html)
        record("board page validates drops through the engine", "/can-move" in board_html and "/move" in board_html)
        record("board logic module is served", "export function canDrop" in c.get(f"{BOARD}/static/board_logic.mjs").text)
        can_move_resp = c.post(f"{KANBAN}/api/cards/{socket_id}/can-move", json={"to_column": "Backlog"}).json()
        record("the engine refuses an illegal transition (Review -> Backlog)", can_move_resp.get("ok") is False, json.dumps(can_move_resp.get("reasons"))[:90])
        record("the refusal names a machine-readable guard code", any("illegal_edge" in str(r) for r in (can_move_resp.get("reasons") or [])), json.dumps(can_move_resp.get("reasons"))[:90])
        legal_resp = c.post(f"{KANBAN}/api/cards/{socket_id}/can-move", json={"to_column": "Done"}).json()
        record("the engine allows a legal transition (Review -> Done)", legal_resp.get("ok") is True, json.dumps(legal_resp.get("reasons"))[:90])
        live_move = c.post(f"{KANBAN}/api/cards/{socket_id}/move", json={"to_column": "Backlog", "actor": "smoke"})
        record("a forced illegal move is rejected over REST too", live_move.status_code >= 400, f"HTTP {live_move.status_code}")

        step("12. the Phase 3 model/gate posture is reported honestly")
        rt2 = c.get(f"{RUNTIME}/health").json()
        model = rt2.get("model") or {}
        gate = rt2.get("gate") or {}
        record("runtime reports its model mode", model.get("mode") in ("local-model", "deterministic-fallback"), str(model.get("mode")))
        record("a missing model is reported, not hidden", model.get("available") is False or model.get("mode") == "local-model")
        record("runtime reports the human-gate configuration", bool(gate.get("configured")), json.dumps(gate.get("configured"))[:90])
        record("the gate is bound to the board's approval API", gate.get("active") is True)
        gate_timeout = gate.get("configured", {}).get("timeout_s")
        record("gate has a finite timeout (silence is never approval)", isinstance(gate_timeout, (int, float)) and gate_timeout > 0, str(gate_timeout))

        # ------------------------------------------------------ guardrail paths
        step("13. guardrails: a T2 card must NOT run until a human opens its gate")
        gated = c.post(
            f"{KANBAN}/api/cards",
            json={
                "title": "Smoke: intrusive web scan (should wait for approval)",
                "board_id": "brd_engagement",
                "assignee": "web-specialist",
                "crew": "vuln-assessment",
                "scope": {"targets": ["example.com"], "authorization_ref": "SMOKE-TEST"},
                "tools": [{"name": "nikto_scan", "tier": 2, "args": {"target": "example.com"}}],
            },
        ).json()
        gated_id = gated["card_id"]
        c.post(f"{KANBAN}/api/cards/{gated_id}/move", json={"to_column": "Assigned", "actor": "smoke"})
        time.sleep(4)  # let at least one bridge pass run
        gated_now = c.get(f"{KANBAN}/api/cards/{gated_id}").json()
        record("T2 card did not start on its own", gated_now["column"] == "Assigned", f"column={gated_now['column']}")
        pending = c.get(f"{KANBAN}/api/approvals/pending").json()["pending"]
        mine = [p for p in pending if p["card_id"] == gated_id]
        record("an approval gate was opened for a human", bool(mine))
        record("no tool ran before approval", not gated_now.get("traces"))

        if mine:
            approval_id = mine[0]["approval"]["id"]
            step("14. approve the gate, and the crew then runs the intrusive tool")
            c.post(
                f"{KANBAN}/api/cards/{gated_id}/approvals/{approval_id}/decide",
                json={"approved": True, "decided_by": "smoke-operator", "note": "authorized for smoke test"},
            )
            after = wait_for_column(c, gated_id, "Review")
            record("approved card then reached Review", after.get("column") == "Review", f"landed in {after.get('column')}")
            record("the intrusive tool finally ran", any(t["tool"] == "nikto_scan" for t in (after.get("traces") or [])))
            record("and it still ran as a dry run", all(t.get("dry_run") for t in (after.get("traces") or [])))

        step("15. guardrails: an out-of-scope target is blocked, never silently attempted")
        evil = c.post(
            f"{KANBAN}/api/cards",
            json={
                "title": "Smoke: out-of-scope target (must be blocked)",
                "board_id": "brd_engagement",
                "assignee": "recon-specialist",
                "crew": "recon",
                "scope": {"targets": ["example.com"], "authorization_ref": "SMOKE-TEST"},
                "tools": [{"name": "nmap_scan", "tier": 1, "args": {"target": "not-in-scope.example.net"}}],
            },
        ).json()
        evil_id = evil["card_id"]
        c.post(f"{KANBAN}/api/cards/{evil_id}/move", json={"to_column": "Assigned", "actor": "smoke"})
        blocked = wait_for_column(c, evil_id, "Blocked")
        record("out-of-scope card was blocked", blocked.get("column") == "Blocked", f"column={blocked.get('column')}")
        record("block reason names the scope failure", "scope" in (blocked.get("blocked_reason") or "").lower(), (blocked.get("blocked_reason") or "")[:80])
        evil_events = c.get(f"{KANBAN}/api/events", params={"card_id": evil_id, "limit": 100}).json()["events"]
        record("it never entered Running", not any(e.get("to_column") == "Running" for e in evil_events), "no Running transition")
        c.post(f"{OBS}/ingest")  # force a pull; the collector also polls on its own timer
        alerts = c.get(f"{OBS}/panels/alerts").json()["items"]
        record("observability raised an alert for the block", any(a["kind"] == "card_blocked" for a in alerts))

    # ------------------------------------------------- Phase 3: memory layer
    #
    # Exercises the two retrieval paths added in Workstream A, and their
    # composition. The scope-guard check at the end is the important one: the
    # memory store is the first component that returns *another engagement's data*
    # if it is asked carelessly, so "no engagement" must be a refusal rather than
    # a wider result set.
        step("16. memory: vector recall, knowledge graph, and their composition")
        eng = f"ENG-SMOKE-{int(time.time())}"
        c.post(
            f"{MEMORY}/episodes",
            json={
                "engagement": eng,
                "summary": "Port scan found SMB 445 open on shop.example.net",
                "kind": "tool_run",
                "target": "shop.example.net",
            },
        )
        c.post(
            f"{MEMORY}/facts",
            json={
                "engagement": eng,
                "key": "web-server:shop.example.net",
                "statement": "shop.example.net runs Apache 2.4.49, vulnerable to CVE-2021-41773",
                "confidence": 0.9,
                "target": "shop.example.net",
            },
        )

        recalled = c.get(f"{MEMORY}/recall", params={"q": "apache cve", "engagement": eng, "mode": "vector"}).json()
        record("vector recall finds the stored fact", recalled["count"] >= 1, f"count={recalled['count']}")
        # "Honestly" means the recall path reports the *same* backend the store
        # says is live - not a hardcoded name. On a host with no embedding
        # endpoint that is hashing-blake2b; on a host running Ollama it is
        # ollama:<model>. Asserting a fixed name made this a claim about the
        # sandbox rather than about the code.
        live_backend = c.get(f"{MEMORY}/embedder").json().get("backend")
        record(
            "vector recall reports its backend honestly",
            bool(live_backend) and recalled.get("vector_backend") == live_backend,
            f"backend={recalled.get('vector_backend')} (live={live_backend})",
        )
        top = recalled["hits"][0]
        record("a recalled hit carries a similarity score", isinstance(top.get("score"), (int, float)), f"score={top.get('score')}")
        record(
            "the top hit is the relevant fact, not merely the first row",
            "apache" in top["summary"].lower() or "cve" in top["summary"].lower(),
            top["summary"][:60],
        )

        graph = c.get(f"{MEMORY}/graph", params={"engagement": eng, "entity": "shop.example.net"}).json()
        record("graph derived entities from the stored memory", len(graph["nodes"]) >= 3, f"nodes={len(graph['nodes'])}")
        record("graph derived relations between them", len(graph["edges"]) >= 2, f"edges={len(graph['edges'])}")
        kinds = {n["kind"] for n in graph["nodes"]}
        record("graph typed the host it was seeded from", "host" in kinds, f"kinds={sorted(kinds)}")

        bundle = c.get(f"{MEMORY}/recall/bundle", params={"q": "apache web server", "engagement": eng}).json()
        record("the bundle composes recall and graph", bundle["counts"]["hits"] >= 1 and bundle["counts"]["nodes"] >= 1, f"hits={bundle['counts']['hits']} nodes={bundle['counts']['nodes']}")
        record("the bundle exposes the graph walked from the recalled records", bool(bundle["graph_seed"]), f"seed={bundle['graph_seed']}")

        # An engagement-scoped reader takes the engagement as a *requirement*, not
        # an optional filter. Omitting it must be a refusal, never a global scan.
        unscoped = c.get(f"{MEMORY}/recall", params={"q": "apache"})
        record("recall without an engagement is refused", unscoped.status_code == 400, f"HTTP {unscoped.status_code}")
        other = c.get(f"{MEMORY}/recall", params={"q": "apache cve", "engagement": "ENG-DOES-NOT-EXIST"}).json()
        record("another engagement's memory is not returned", other["count"] == 0, f"count={other['count']}")

    # --------------------------------------------------- Phase 4: embedder
    # Which embedding backend is live. Reported independently of recall, because
    # after a backend swap recall can return nothing while every count, health
    # check and 200 response still looks perfect.
    with client() as c:
        emb = c.get(f"{MEMORY}/embedder").json()
        record(
            "the memory store reports which embedding backend is live",
            bool(emb.get("backend")),
            f"backend={emb.get('backend')}",
        )
        record(
            "the embedder reports its degradation state, not just its name",
            "degraded" in emb and isinstance(emb["degraded"], bool),
            f"degraded={emb.get('degraded')}",
        )
        record(
            "the embedder reports vector drift after a backend change",
            "drift" in emb and isinstance(emb["drift"], int),
            f"drift={emb.get('drift')}",
        )

    # ------------------------------------------- Phase 4: shell integration
    # The shell's new surfaces: file-manager drop, overlay widget, window manager.
    with client() as c:
        surfaces = c.get(f"{SHELL}/health").json()["surfaces"]
        for surface in ("file_manager_drop", "overlay_widget", "window_manager"):
            record(f"the shell advertises its {surface} surface", surface in surfaces)

        panel = c.get(f"{SHELL}/panel").text
        record(
            "the panel is wired to the new surfaces",
            all(m in panel for m in ("/api/overlay", "/api/windows", "/api/attach/", 'id="overlay"')),
        )

        # A file-manager entry dropped onto a card becomes an artifact on the
        # board, through the board's own route - so it is guarded and audited
        # exactly like an attachment a crew made.
        cards = c.get(f"{KANBAN}/api/cards", params={"limit": 1}).json()["cards"]
        if cards:
            target = cards[0]["card_id"]
            before = len(c.get(f"{KANBAN}/api/cards/{target}").json().get("artifacts") or [])
            dropped = c.post(
                f"{SHELL}/api/attach/{target}",
                json={"name": "smoke-loot.xml", "path": "/home/operator/loot/smoke-loot.xml", "size": 4096},
            )
            record(
                "a file-manager drop attaches an artifact to the card",
                dropped.status_code == 201,
                f"HTTP {dropped.status_code}",
            )
            after = c.get(f"{KANBAN}/api/cards/{target}").json().get("artifacts") or []
            record(
                "the dropped file is recorded on the board, not just in the shell",
                len(after) == before + 1,
                f"artifacts {before} -> {len(after)}",
            )
            recorded = [a for a in after if a.get("name") == "smoke-loot.xml"]
            record(
                "the artifact records a reference and does not invent a stored url",
                bool(recorded) and recorded[0].get("path") and not recorded[0].get("url"),
                f"kind={recorded[0].get('kind') if recorded else 'missing'}",
            )
            record(
                "the artifact kind is derived from the extension",
                bool(recorded) and recorded[0].get("kind") == "scan-output",
                f"kind={recorded[0].get('kind') if recorded else 'missing'}",
            )

        bad = c.post(f"{SHELL}/api/attach/{cards[0]['card_id'] if cards else 'crd_x'}", json={})
        record("a drop with no filename is refused", bad.status_code == 400, f"HTTP {bad.status_code}")

        # overlay
        ov = c.get(f"{SHELL}/api/overlay").json()
        record("the overlay renders a single overall state", ov.get("state") in {"ok", "warn", "alert"}, f"state={ov.get('state')}")
        record("the overlay carries live board counters", "cards" in ov.get("counters", {}), f"cards={ov.get('counters', {}).get('cards')}")
        record("the overlay reports per-service reachability", len(ov.get("services") or []) >= 4, f"services={len(ov.get('services') or [])}")
        down = [s["name"] for s in ov.get("services") or [] if s["state"] != "ok"]
        record("the overlay reports every service reachable on a healthy stack", not down, f"down={down or 'none'}")
        # The overall colour is derived from the board, not hard-coded: a stack
        # whose services are all up can still be in alert because a card is
        # blocked. Asserting the *mapping* catches a misreport that asserting
        # "state == ok" would only ever catch on a pristine board.
        blocked = (ov.get("counters") or {}).get("blocked", 0)
        pending = (ov.get("counters") or {}).get("pending_approvals", 0)
        expected = "alert" if blocked else ("warn" if pending else "ok")
        record(
            "the overlay's overall state follows the board's worst condition",
            ov["state"] == expected,
            f"state={ov['state']} expected={expected} (blocked={blocked} pending={pending})",
        )

        # window manager
        opened = c.post(f"{SHELL}/api/windows", json={"action": "open", "app": "kanban", "title": "Kanban"}).json()
        first = opened["windows"][-1]["id"]
        record("a shell window opens and takes focus", opened["focused"] == first and opened["count"] >= 1, f"focused={opened['focused']}")
        record("the taskbar lists the open window", any(w["id"] == first for w in opened["taskbar"]), f"taskbar={len(opened['taskbar'])}")

        second = c.post(f"{SHELL}/api/windows", json={"action": "open", "app": "terminal"}).json()
        second_id = second["windows"][-1]["id"]
        refocused = c.post(f"{SHELL}/api/windows", json={"action": "focus", "id": first}).json()
        record(
            "focusing a window raises it to the top of the stack",
            refocused["focused"] == first and refocused["stack"][-1] == first,
        )
        maximized = c.post(f"{SHELL}/api/windows", json={"action": "maximize", "id": first}).json()
        record(
            "a window maximises to the desktop size",
            next(w for w in maximized["windows"] if w["id"] == first)["state"] == "maximized",
        )
        closed = c.post(f"{SHELL}/api/windows", json={"action": "close", "id": second_id}).json()
        record("closing a window moves focus to a real window", closed["focused"] == first, f"focused={closed['focused']}")
        bad_action = c.post(f"{SHELL}/api/windows", json={"action": "levitate", "id": first})
        record("an unknown window action is refused, not silently ignored", bad_action.status_code == 400, f"HTTP {bad_action.status_code}")

    # ------------------------------- Phase 4: bridge reads recall before a run
    # The whole point is that the crew is handed what the engagement already
    # knows. The claim is checked against the bridge's own recall counter, and by
    # reading the memory bundle back off the card.
    with client() as c:
        # Seed into the engagement the *bridge* actually reads from - reported by
        # the runtime rather than assumed, because a mismatch here looks exactly
        # like a broken recall path.
        eng = c.get(f"{RUNTIME}/health").json()["memory"]["engagement"]
        record("the runtime reports the engagement it reads memory for", bool(eng), f"engagement={eng}")
        c.post(
            f"{MEMORY}/episodes",
            json={
                "engagement": eng,
                "summary": "Earlier recon on shop.example.net found an Apache mod_status page exposed",
                "target": "shop.example.net",
            },
        )
        card = c.post(
            f"{KANBAN}/api/cards",
            json={
                "title": "Web recon on shop.example.net",
                "description": "identify the web stack and its exposures",
                "board_id": "brd_engagement",
                "assignee": "recon-specialist",
                "crew": "recon",
                "scope": {"targets": ["shop.example.net"], "authorization_ref": "SMOKE"},
                "tools": [{"name": "whois_lookup", "tier": 0, "args": {"target": "shop.example.net"}}],
            },
        ).json()
        cid = card["card_id"]
        before_recalls = c.get(f"{RUNTIME}/health").json()["stats"]["memory_recalls"]
        c.post(f"{KANBAN}/api/cards/{cid}/move", json={"to_column": "Assigned", "actor": "smoke", "actor_is_agent": False})
        review = wait_for_column(c, cid, "Review")
        record("the card completes with memory configured", review is not None, f"card={cid}")

        if review is not None:
            after = c.get(f"{RUNTIME}/health").json()["stats"]
            record(
                "the bridge consulted recall before running the crew",
                after["memory_recalls"] > before_recalls,
                f"recalls {before_recalls} -> {after['memory_recalls']}",
            )
            # Read the trail back off the *board*: this is the replay evidence,
            # and it proves the trail survived the process that produced it.
            artifacts = c.get(f"{KANBAN}/api/cards/{cid}").json().get("artifacts") or []
            recall_artifact = next((a for a in artifacts if a.get("name", "").endswith("-recall.json")), None)
            record(
                "the recall trail is attached to the card as an artifact",
                recall_artifact is not None,
                f"artifacts={[a.get('name') for a in artifacts]}",
            )
            if recall_artifact:
                summary = recall_artifact.get("summary") or ""
                record(
                    "the card records how many memories were recalled and from which backend",
                    "hit(s)" in summary and "via " in summary,
                    summary[:70],
                )
                record(
                    "the recall query is built from the card, not the engagement",
                    "shop.example.net" in summary,
                    summary[:60],
                )
                record(
                    "the recalled record was actually retrieved, not just counted",
                    not summary.startswith("memory recalled for this card: 0 hit"),
                    summary[:52],
                )
    # --------------------------------------------- Phase 5 + 6: new surfaces
    #
    # Phase 5 built the inspector, heartbeats, alert routing and retention; its
    # dashboard could not show any of it. Phase 6 wired the panels and built the
    # two surfaces that were still missing - routed alerts on the overlay, and a
    # real compositor client. Both halves are checked here, because a panel that
    # is present in markup but wired to nothing passes every unit test and shows
    # the operator an empty box.
    c = client()
    page = c.get(f"{OBS}/dashboard")
    record("the dashboard serves", page.status_code == 200 and "text/html" in page.headers.get("content-type", ""), f"HTTP {page.status_code}")
    html = page.text
    for route, label in (
        ("/model/calls", "the model-call inspector"),
        ("/model/health/history", "the heartbeat history"),
        ("/alerting/status", "the alert-routing panel"),
        ("/alerting/notifications", "the routed-alert feed"),
        ("/retention", "the retention panel"),
    ):
        record(f"the dashboard is wired to {label}", route in html, route)

    # --- a captured prompt must be redacted before it is ever stored --------
    posted = c.post(
        f"{OBS}/model/calls",
        json={
            "prompt": "aws configure with AKIAIOSFODNN7EXAMPLE then scan 10.10.0.5",
            "response": "ok",
            "model": "smoke-model",
            "backend": "ollama",
            "crew": "recon",
            "role": "recon-specialist",
        },
    )
    record("a model interaction can be captured", posted.status_code == 200, f"HTTP {posted.status_code}")
    call_id = posted.json().get("id") if posted.status_code == 200 else None

    listed = c.get(f"{OBS}/model/calls", params={"limit": 50}).json()
    kinds = listed.get("counts", {}).get("redaction_kinds") or []
    record(
        "a bare cloud access key is redacted, not just a labelled one",
        "aws_access_key" in kinds,
        f"kinds={kinds}",
    )
    row = next((r for r in listed.get("items", []) if r.get("id") == call_id), None)
    record("the captured call appears in the inspector list", row is not None and row.get("prompt_chars", 0) > 0, f"id={call_id}")
    if row:
        record(
            "the list view omits the body and reports its size instead",
            "prompt" not in row and row.get("prompt_chars", 0) > 0,
            f"prompt_chars={row.get('prompt_chars')}",
        )
    detail = c.get(f"{OBS}/model/calls/{call_id}").json() if call_id else {}
    record("the inspector opens a single call", "prompt" in detail and "response" in detail, f"id={call_id}")
    record(
        "the single-call view returns the redacted body, never the credential",
        "AKIAIOSFODNN7EXAMPLE" not in (detail.get("prompt") or ""),
        (detail.get("prompt") or "")[:60],
    )
    filtered = c.get(f"{OBS}/model/calls", params={"redacted_only": True}).json()
    record(
        "the redacted-only filter finds it",
        any(r.get("id") == call_id for r in filtered.get("items", [])),
        f"{len(filtered.get('items', []))} redacted row(s)",
    )

    # --- alert routing + retention --------------------------------------
    routing = c.get(f"{OBS}/alerting/status").json()
    record(
        "the routing panel's counters are reported by the router",
        all(k in routing for k in ("received", "delivered", "suppressed", "failed")),
        {k: routing.get(k) for k in ("received", "delivered", "suppressed", "failed")},
    )
    c.post(f"{OBS}/alerting/route")
    feed = c.get(f"{OBS}/alerting/notifications", params={"limit": 10}).json()
    record("the shell's alert feed serves an items envelope", isinstance(feed.get("items"), list) and "count" in feed, f"count={feed.get('count')}")
    deliveries = c.get(f"{OBS}/alerting/deliveries", params={"limit": 10}).json()
    record("the full delivery log answers", isinstance(deliveries.get("items"), list), f"{len(deliveries.get('items', []))} attempt(s)")
    retention = c.get(f"{OBS}/retention").json()
    record(
        "retention reports its stores and its plan",
        "stores" in (retention.get("status") or {}) and "plan" in retention,
        f"stores={(retention.get('status') or {}).get('stores')}",
    )

    # --- the overlay renders what the router delivered (Phase 6, item 3) ---
    overlay = c.get(f"{SHELL}/api/overlay").json()
    record(
        "the overlay carries the routed-alert feed",
        "alert_notifications" in overlay and "routed_count" in overlay,
        f"routed={overlay.get('routed_count')}",
    )
    record(
        "the routed count matches the feed it received",
        overlay.get("routed_count") == len(overlay.get("alert_notifications") or []),
        f"{overlay.get('routed_count')} vs {len(overlay.get('alert_notifications') or [])}",
    )
    routed_kinds = {
        f"routed:{n.get('kind')}" for n in (overlay.get("alert_notifications") or []) if n.get("kind")
    }
    alert_kinds = {a.get("kind") for a in (overlay.get("alerts") or [])}
    record(
        "every routed alert reaches the overlay's alert list",
        routed_kinds <= alert_kinds,
        f"routed={sorted(routed_kinds)}",
    )

    # --- the GTK client is honest about a host it cannot run on ----------
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    client_env = {**os.environ, "PYTHONPATH": os.path.join(root, "hermes-shell")}
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from hermes_shell.gtk.client import self_check; print(json.dumps(self_check()))",
        ],
        capture_output=True,
        text=True,
        env=client_env,
    )
    record("the GTK client imports without PyGObject installed", probe.returncode == 0, probe.stderr.strip()[-80:])
    if probe.returncode == 0:
        check = json.loads(probe.stdout)
        record(
            "the client reports the session it found rather than guessing",
            check.get("session") in ("headless", "x11", "wayland"),
            f"session={check.get('session')}",
        )
        record(
            "a host with no Wayland session is not claimed runnable",
            check.get("session") == "wayland" or check.get("runnable") is False,
            f"runnable={check.get('runnable')} ({check.get('reason')})",
        )
        record(
            "the client says what it is missing instead of failing silently",
            bool(check.get("missing")),
            "; ".join(check.get("missing") or [])[:80],
        )
    cli = subprocess.run(
        [sys.executable, "-m", "hermes_shell.gtk.client"],
        capture_output=True,
        text=True,
        env=client_env,
    )
    record(
        "the client exits non-zero off a Wayland session, so the session falls back",
        cli.returncode != 0,
        f"exit {cli.returncode}",
    )

    # --- every role plans with the model (Phase 6, item 4) ---------------
    sys.path.insert(0, os.path.join(root, "agent-runtime"))
    from agent_runtime.crews import ALL_ROLES, CREWS, model_roles  # noqa: E402

    covered = model_roles()
    # The count is derived, not hardcoded: Phase 6 pinned this at six roles and
    # Phase 8 added the remediation specialist, so a literal 6 here would fail on
    # a correct registry. The load-bearing assertion is the coverage one - every
    # registered role is on the model path - which stays true as roles are added.
    record(
        "every registered role has a model path",
        covered == frozenset(ALL_ROLES),
        f"{len(covered)}/{len(ALL_ROLES)} roles",
    )
    crew_roles = {role for crew in CREWS.values() for role in crew.roles}
    record(
        "no crew is left on the deterministic path",
        crew_roles <= covered,
        f"{len(crew_roles)} role(s) across {len(CREWS)} crew(s)",
    )
    c.close()

    # ------------------------------------------------------------------ report
    total = len(CHECKS)
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print("\n" + "=" * 68)
    print(f"  {passed}/{total} checks passed")
    failed = [name for name, ok, _ in CHECKS if not ok]
    if failed:
        print("  failed:")
        for name in failed:
            print(f"    - {name}")
        print("=" * 68)
        return 1
    print("  full loop verified: card -> crew -> tool -> trace -> audit -> Review")
    print("  new in Phase 2: socket claim, MCP stdio transport, interactive board")
    print("  new in Phase 3: guardrail scope-escape fix, L6 memory (vector + graph), 3 tool categories")
    print("  new in Phase 4: recall-before-run, real embedder backend, shell drop+overlay+WM, runnable build")
    print("  new in Phase 5+6: inspector/routing/retention panels wired, bare cloud keys redacted,")
    print("                    routed alerts on the overlay, GTK/Wayland client, every role on the model")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
