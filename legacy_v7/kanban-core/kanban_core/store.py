"""SQLite persistence + hash-chained audit log.

Blueprint ref: section 03.7 (persistence) and section 04.6 (hash-chained audit
log). Two stores live here:

* the *working store* - boards and cards, JSON documents behind indexed columns;
* the *event/audit log* - append-only, every row chained to its predecessor by
  SHA-256, so tampering with history is detectable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from .models import (
    AuditRecord,
    Board,
    Card,
    Column,
    Event,
    utcnow,
)

GENESIS = "0" * 64


#: Advisory lock. Multiple writer threads are normal (uvicorn + bridge).
_LOCK = threading.RLock()


def canonical(obj: Any) -> str:
    """Deterministic JSON - the byte string that gets hashed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def chain_hash(prev_hash: str, record: dict[str, Any]) -> str:
    payload = dict(record)
    payload.pop("hash", None)
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()


class Store:
    """Synchronous SQLite store. Fast enough to call directly from handlers."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        with _LOCK:
            self._conn.close()

    def migrate(self) -> None:
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS boards (
                  board_id     TEXT PRIMARY KEY,
                  name         TEXT NOT NULL,
                  kind         TEXT NOT NULL,
                  description  TEXT DEFAULT '',
                  default_crew TEXT,
                  created_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cards (
                  card_id      TEXT PRIMARY KEY,
                  board_id     TEXT NOT NULL,
                  title        TEXT NOT NULL,
                  column_name  TEXT NOT NULL,
                  assignee     TEXT,
                  priority     TEXT NOT NULL,
                  parent_id    TEXT,
                  created_at   TEXT NOT NULL,
                  updated_at   TEXT NOT NULL,
                  doc          TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cards_board  ON cards(board_id);
                CREATE INDEX IF NOT EXISTS idx_cards_column ON cards(column_name);
                CREATE INDEX IF NOT EXISTS idx_cards_parent ON cards(parent_id);

                CREATE TABLE IF NOT EXISTS events (
                  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id     TEXT UNIQUE NOT NULL,
                  ts           TEXT NOT NULL,
                  type         TEXT NOT NULL,
                  board_id     TEXT,
                  card_id      TEXT,
                  from_column  TEXT,
                  to_column    TEXT,
                  actor        TEXT,
                  payload      TEXT NOT NULL,
                  prev_hash    TEXT NOT NULL,
                  hash         TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_card ON events(card_id);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            self._conn.commit()

    # --------------------------------------------------------------- boards
    def create_board(self, board: Board) -> Board:
        with _LOCK:
            self._conn.execute(
                "INSERT INTO boards (board_id,name,kind,description,default_crew,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    board.board_id,
                    board.name,
                    board.kind,
                    board.description,
                    board.default_crew,
                    board.created_at,
                ),
            )
            self._conn.commit()
        return board

    def get_board(self, board_id: str) -> Optional[Board]:
        with _LOCK:
            row = self._conn.execute("SELECT * FROM boards WHERE board_id=?", (board_id,)).fetchone()
        return self._row_to_board(row) if row else None

    def list_boards(self) -> list[Board]:
        with _LOCK:
            rows = self._conn.execute("SELECT * FROM boards ORDER BY created_at").fetchall()
        return [self._row_to_board(r) for r in rows]

    @staticmethod
    def _row_to_board(row: sqlite3.Row) -> Board:
        return Board(
            board_id=row["board_id"],
            name=row["name"],
            kind=row["kind"],
            description=row["description"] or "",
            default_crew=row["default_crew"],
            created_at=row["created_at"],
        )

    # ---------------------------------------------------------------- cards
    def save_card(self, card: Card) -> Card:
        card.touch()
        with _LOCK:
            self._conn.execute(
                """INSERT INTO cards
                     (card_id,board_id,title,column_name,assignee,priority,parent_id,created_at,updated_at,doc)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(card_id) DO UPDATE SET
                     board_id=excluded.board_id, title=excluded.title, column_name=excluded.column_name,
                     assignee=excluded.assignee, priority=excluded.priority, parent_id=excluded.parent_id,
                     updated_at=excluded.updated_at, doc=excluded.doc""",
                (
                    card.card_id,
                    card.board_id,
                    card.title,
                    card.column.value,
                    card.assignee,
                    card.priority.value,
                    card.parent_id,
                    card.created_at,
                    card.updated_at,
                    card.model_dump_json(),
                ),
            )
            self._conn.commit()
        return card

    def get_card(self, card_id: str) -> Optional[Card]:
        with _LOCK:
            row = self._conn.execute("SELECT doc FROM cards WHERE card_id=?", (card_id,)).fetchone()
        return Card.model_validate_json(row["doc"]) if row else None

    def list_cards(
        self,
        *,
        board_id: Optional[str] = None,
        column: Optional[Column | str] = None,
        assignee: Optional[str] = None,
        parent_id: Optional[str] = None,
        limit: int = 1000,
    ) -> list[Card]:
        sql = "SELECT doc FROM cards WHERE 1=1"
        params: list[Any] = []
        if board_id:
            sql += " AND board_id=?"
            params.append(board_id)
        if column:
            sql += " AND column_name=?"
            params.append(column.value if isinstance(column, Column) else str(column))
        if assignee:
            sql += " AND assignee=?"
            params.append(assignee)
        if parent_id:
            sql += " AND parent_id=?"
            params.append(parent_id)
        sql += " LIMIT ?"
        params.append(limit)
        with _LOCK:
            rows = self._conn.execute(sql, params).fetchall()
        return [Card.model_validate_json(r["doc"]) for r in rows]

    def delete_card(self, card_id: str) -> bool:
        with _LOCK:
            cur = self._conn.execute("DELETE FROM cards WHERE card_id=?", (card_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------------- events
    def append_event(self, event: Event) -> Event:
        """Append to the hash chain and return the event with audit fields set."""
        with _LOCK:
            row = self._conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = row["hash"] if row else GENESIS

            record = {
                "ts": event.ts,
                "type": event.type,
                "board_id": event.board_id,
                "card_id": event.card_id,
                "from_column": event.from_column,
                "to_column": event.to_column,
                "actor": event.actor,
                "payload": event.payload,
                "event_id": event.event_id,
                "prev_hash": prev_hash,
            }
            digest = chain_hash(prev_hash, record)
            cur = self._conn.execute(
                """INSERT INTO events
                     (event_id,ts,type,board_id,card_id,from_column,to_column,actor,payload,prev_hash,hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id,
                    event.ts,
                    event.type,
                    event.board_id,
                    event.card_id,
                    event.from_column,
                    event.to_column,
                    event.actor,
                    canonical(event.payload),
                    prev_hash,
                    digest,
                ),
            )
            self._conn.commit()
            event.seq = int(cur.lastrowid)
            event.audit_hash = digest
        return event

    def list_events(
        self,
        *,
        since_seq: int = 0,
        card_id: Optional[str] = None,
        board_id: Optional[str] = None,
        type_prefix: Optional[str] = None,
        limit: int = 500,
    ) -> list[Event]:
        sql = "SELECT * FROM events WHERE seq > ?"
        params: list[Any] = [since_seq]
        if card_id:
            sql += " AND card_id=?"
            params.append(card_id)
        if board_id:
            sql += " AND board_id=?"
            params.append(board_id)
        if type_prefix:
            sql += " AND type LIKE ?"
            params.append(f"{type_prefix}%")
        sql += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        with _LOCK:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            seq=row["seq"],
            ts=row["ts"],
            type=row["type"],
            board_id=row["board_id"],
            card_id=row["card_id"],
            from_column=row["from_column"],
            to_column=row["to_column"],
            actor=row["actor"] or "system",
            payload=json.loads(row["payload"] or "{}"),
            audit_hash=row["hash"],
        )

    # -- audit integrity -------------------------------------------------
    def verify_chain(self) -> dict[str, Any]:
        """Recompute the whole chain. Returns a verdict, never raises."""
        with _LOCK:
            rows = self._conn.execute("SELECT * FROM events ORDER BY seq ASC").fetchall()
        prev = GENESIS
        for row in rows:
            record = {
                "ts": row["ts"],
                "type": row["type"],
                "board_id": row["board_id"],
                "card_id": row["card_id"],
                "from_column": row["from_column"],
                "to_column": row["to_column"],
                "actor": row["actor"],
                "payload": json.loads(row["payload"] or "{}"),
                "event_id": row["event_id"],
                "prev_hash": prev,
            }
            expected = chain_hash(prev, record)
            if row["prev_hash"] != prev or row["hash"] != expected:
                return {
                    "ok": False,
                    "checked": len(rows),
                    "broken_at_seq": row["seq"],
                    "reason": "hash mismatch - audit log has been tampered with",
                }
            prev = row["hash"]
        head = prev
        return {
            "ok": True,
            "checked": len(rows),
            "head_hash": head,
            "genesis": GENESIS,
            "chain_tip": rows[-1]["seq"] if rows else 0,
        }

    def record_audit(self, record: AuditRecord) -> AuditRecord:  # pragma: no cover - convenience
        event = Event(
            ts=record.ts or utcnow(),
            type=record.kind,
            board_id=record.board_id,
            card_id=record.card_id,
            actor=record.actor,
            payload=record.payload,
        )
        stored = self.append_event(event)
        record.seq = stored.seq or 0
        record.hash = stored.audit_hash or ""
        return record

    # -- meta ------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        with _LOCK:
            self._conn.execute(
                "INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with _LOCK:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
