"""Hash-chained audit log for tool invocations.

Blueprint ref: section 04.6 - 'audit log of every agent action on the system'.

Same construction as the Kanban event chain (``prev_hash || canonical(record)``
-> SHA-256), so an operator can verify both logs with one mental model. The
chain is append-only and every row is re-derivable from its predecessor, which
is what makes tampering detectable rather than merely unlikely.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

GENESIS = "0" * 64
_LOCK = threading.RLock()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def chain_hash(prev_hash: str, record: dict[str, Any]) -> str:
    payload = dict(record)
    payload.pop("hash", None)
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()


class ToolAuditLog:
    """Append-only, hash-chained record of every tool invocation."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_audit (
                  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts           TEXT NOT NULL,
                  tool         TEXT NOT NULL,
                  tier         INTEGER NOT NULL,
                  caller       TEXT,
                  card_id      TEXT,
                  target       TEXT,
                  status       TEXT NOT NULL,
                  dry_run      INTEGER NOT NULL,
                  decision     TEXT,
                  command      TEXT,
                  duration_ms  INTEGER DEFAULT 0,
                  exit_code    INTEGER,
                  args         TEXT,
                  reasons      TEXT,
                  effects      TEXT,
                  prev_hash    TEXT NOT NULL,
                  hash         TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_tool ON tool_audit(tool);
                CREATE INDEX IF NOT EXISTS idx_audit_card ON tool_audit(card_id);
                """
            )
            self._migrate_effects_column()
            self._conn.commit()

    def _migrate_effects_column(self) -> None:
        """Add the ``effects`` column to a database created before Phase 17.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched, so a
        store on disk from an earlier phase would keep opening but refuse the new
        INSERT. The column is left NULL-able on purpose: a row written before the
        column existed cannot have its original hash re-derived *with* an effects
        field, so :meth:`verify_chain` includes the field only when it is
        non-NULL. That keeps an older chain verifiable instead of declaring its
        own history tampered with.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tool_audit)")}
        if "effects" not in cols:
            self._conn.execute("ALTER TABLE tool_audit ADD COLUMN effects TEXT")

    def close(self) -> None:
        with _LOCK:
            self._conn.close()

    def append(
        self,
        *,
        ts: str,
        tool: str,
        tier: int,
        status: str,
        dry_run: bool,
        caller: str = "agent",
        card_id: Optional[str] = None,
        target: Optional[str] = None,
        decision: Optional[str] = None,
        command: Optional[str] = None,
        duration_ms: int = 0,
        exit_code: Optional[int] = None,
        args: Optional[dict[str, Any]] = None,
        reasons: Optional[list[str]] = None,
        effects: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        with _LOCK:
            row = self._conn.execute("SELECT hash FROM tool_audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = row["hash"] if row else GENESIS
            record = {
                "ts": ts,
                "tool": tool,
                "tier": tier,
                "caller": caller,
                "card_id": card_id,
                "target": target,
                "status": status,
                "dry_run": bool(dry_run),
                "decision": decision,
                "command": command,
                "duration_ms": duration_ms,
                "exit_code": exit_code,
                "args": args or {},
                "reasons": reasons or [],
                "effects": effects or [],
                "prev_hash": prev_hash,
            }
            digest = chain_hash(prev_hash, record)
            cur = self._conn.execute(
                """INSERT INTO tool_audit
                     (ts,tool,tier,caller,card_id,target,status,dry_run,decision,command,
                      duration_ms,exit_code,args,reasons,effects,prev_hash,hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts,
                    tool,
                    tier,
                    caller,
                    card_id,
                    target,
                    status,
                    int(bool(dry_run)),
                    decision,
                    command,
                    duration_ms,
                    exit_code if exit_code is None else int(exit_code),
                    canonical(args or {}),
                    canonical(reasons or []),
                    canonical(effects or []),
                    prev_hash,
                    digest,
                ),
            )
            self._conn.commit()
        return {
            "seq": int(cur.lastrowid),
            "tool": tool,
            "status": status,
            "audit_hash": digest,
            "prev_hash": prev_hash,
        }

    def list(
        self,
        *,
        limit: int = 200,
        tool: Optional[str] = None,
        card_id: Optional[str] = None,
        since_seq: int = 0,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tool_audit WHERE seq > ?"
        params: list[Any] = [since_seq]
        if tool:
            sql += " AND tool=?"
            params.append(tool)
        if card_id:
            sql += " AND card_id=?"
            params.append(card_id)
        sql += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        with _LOCK:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "ts": row["ts"],
            "tool": row["tool"],
            "tier": row["tier"],
            "caller": row["caller"],
            "card_id": row["card_id"],
            "target": row["target"],
            "status": row["status"],
            "dry_run": bool(row["dry_run"]),
            "decision": row["decision"],
            "command": row["command"],
            "duration_ms": row["duration_ms"],
            "exit_code": row["exit_code"],
            "args": json.loads(row["args"] or "{}"),
            "reasons": json.loads(row["reasons"] or "[]"),
            "effects": (json.loads(row["effects"] or "[]") if "effects" in row.keys() else []),
            "prev_hash": row["prev_hash"],
            "audit_hash": row["hash"],
        }

    def counts(self) -> dict[str, int]:
        with _LOCK:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM tool_audit GROUP BY status").fetchall()
        out = {r["status"]: r["n"] for r in rows}
        out["total"] = sum(out.values())
        return out

    def verify_chain(self) -> dict[str, Any]:
        with _LOCK:
            rows = self._conn.execute("SELECT * FROM tool_audit ORDER BY seq ASC").fetchall()
        prev = GENESIS
        for row in rows:
            record = {
                "ts": row["ts"],
                "tool": row["tool"],
                "tier": row["tier"],
                "caller": row["caller"],
                "card_id": row["card_id"],
                "target": row["target"],
                "status": row["status"],
                "dry_run": bool(row["dry_run"]),
                "decision": row["decision"],
                "command": row["command"],
                "duration_ms": row["duration_ms"],
                "exit_code": row["exit_code"],
                "args": json.loads(row["args"] or "{}"),
                "reasons": json.loads(row["reasons"] or "[]"),
                "prev_hash": prev,
            }
            # Rows written before the effects column existed were hashed without
            # it; adding it unconditionally would report a valid older chain as
            # tampered. New rows always carry a value (possibly an empty list).
            stored_effects = row["effects"] if "effects" in row.keys() else None
            if stored_effects is not None:
                record["effects"] = json.loads(stored_effects or "[]")
            if row["prev_hash"] != prev or row["hash"] != chain_hash(prev, record):
                return {
                    "ok": False,
                    "checked": len(rows),
                    "broken_at_seq": row["seq"],
                    "reason": "hash mismatch - tool audit log has been tampered with",
                }
            prev = row["hash"]
        return {
            "ok": True,
            "checked": len(rows),
            "head_hash": prev,
            "chain_tip": rows[-1]["seq"] if rows else 0,
        }
