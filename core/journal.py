"""Decision journal: selected log events persisted to SQLite for the dashboard.

Attached as a logging handler, so every existing ``log_event`` call for a
journaled event is recorded with no changes at the call sites. A journal
failure is reported by logging and never interrupts trading.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from core.observability import get_correlation_id

JOURNAL_EVENTS: Final[frozenset[str]] = frozenset({
    "startup", "heartbeat", "shutdown.begin", "shutdown.complete",
    "signal", "ai.decision", "ai.invalid_response_final", "ai.all_providers_failed",
    "risk.approved", "risk.rejected", "risk.kill_switch", "risk.force_flatten", "risk.flatten_failed",
    "order.submitted", "order.recovered_after_error", "dry_run.order",
    "symbol.skipped", "cycle.state_unavailable", "risk.gate_error",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    level  TEXT NOT NULL,
    event  TEXT NOT NULL,
    cid    TEXT NOT NULL,
    symbol TEXT,
    fields TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_event ON events (event, ts);
"""


class JournalHandler(logging.Handler):
    def __init__(self, path: Path, *, retention_days: int = 90) -> None:
        super().__init__(level=logging.INFO)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))

    def emit(self, record: logging.LogRecord) -> None:
        event = record.getMessage()
        if event not in JOURNAL_EVENTS:
            return
        try:
            fields: dict[str, Any] = dict(getattr(record, "fields", {}) or {})
            self._conn.execute(
                "INSERT INTO events (ts, level, event, cid, symbol, fields) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
                 record.levelname, event, get_correlation_id(), fields.get("symbol"),
                 json.dumps(fields, default=str)),
            )
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            super().close()


def read_events(path: Path, *, limit: int = 200, events: set[str] | None = None,
                symbol: str | None = None) -> list[dict[str, Any]]:
    """Newest-first events, read through a read-only connection."""
    if not path.exists():
        return []
    sql = "SELECT ts, level, event, cid, symbol, fields FROM events"
    clauses: list[str] = []
    params: list[Any] = []
    if events:
        clauses.append(f"event IN ({','.join('?' * len(events))})")
        params.extend(sorted(events))
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 2000)))
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [
        {"ts": ts, "level": level, "event": event, "cid": cid, "symbol": sym, **json.loads(fields)}
        for ts, level, event, cid, sym, fields in rows
    ]
