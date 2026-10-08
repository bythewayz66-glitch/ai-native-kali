#!/usr/bin/env python3
"""Verify the hash-chained tool audit log on this host.

A one-command check an operator can run after a crew finishes, or from a systemd
timer, to answer the only question that matters about the audit log: *is the chain
still intact?* Tampering with any row - including its declared footprint - breaks
the chain from that row onward, so a single head-hash comparison is not enough and
this walks the whole chain.

    python3 scripts/verify_tool_audit.py                    # default DB
    python3 scripts/verify_tool_audit.py --db /var/lib/...  # explicit path
    python3 scripts/verify_tool_audit.py --limit 5 --list   # show recent rows

Exit code 0 only when the chain verifies, so it can gate a deployment or a
smoke check. Reading a store that does not exist is a *failure*, not a pass:
"no audit log" and "an audit log with no entries" are different answers and
only one of them is safe to ignore.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Importing tool_frontends pulls in the package __init__, which imports the
# guardrail engine, which imports kanban_core.models - so the audit module cannot
# be loaded without the sibling components on the path, even though it does not
# use them itself.
for component in ("tool-frontends", "kanban-core", "memory-store"):
    path = REPO_ROOT / component
    if path.is_dir():
        sys.path.insert(0, str(path))

from tool_frontends.audit import ToolAuditLog  # noqa: E402

DEFAULT_DB = os.environ.get("TOOLS_AUDIT_DB", "/var/lib/ai-native-kali/tool-audit.db")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help="path to the audit database")
    parser.add_argument("--limit", type=int, default=10, help="rows to show with --list")
    parser.add_argument("--list", action="store_true", help="print the most recent rows")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if not Path(args.db).exists():
        result = {
            "ok": False,
            "db": args.db,
            "reason": "audit database does not exist - no audit log is not a clean audit log",
        }
        print(json.dumps(result) if args.json else f"FAIL: {result['reason']} ({args.db})")
        return 1

    log = ToolAuditLog(args.db)
    try:
        verdict = log.verify_chain()
        counts = log.counts()
    finally:
        log.close()

    if args.json:
        print(json.dumps({"db": args.db, **verdict, "counts": counts}, sort_keys=True))
    else:
        print(f"database : {args.db}")
        print(f"rows     : {verdict['checked']}")
        print(f"statuses : {json.dumps(counts, sort_keys=True)}")
        if verdict["ok"]:
            print(f"head     : {verdict['head_hash']}")
            print("RESULT: PASS - chain intact")
        else:
            print(f"broken at: seq {verdict['broken_at_seq']}")
            print(f"RESULT: FAIL - {verdict['reason']}")

    if args.list:
        reader = ToolAuditLog(args.db)
        try:
            rows = reader.list(limit=args.limit)
        finally:
            reader.close()
        for row in rows[-args.limit:]:
            print(
                f"  seq={row['seq']:<5} {row['ts']} {row['tool']:<24} "
                f"status={row['status']:<10} effects={row['effects']}"
            )

    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
