"""SQLite storage for the forex engine (WAL mode; engine and console share one file).

Secrets are stored encrypted (``*_enc``) with a short ``*_hint`` for display; read paths used by
the console never return the encrypted columns.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fx.models import Candle, StrategyRecord, StrategyStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL UNIQUE COLLATE NOCASE,
    environment TEXT NOT NULL CHECK (environment IN ('practice', 'live')),
    oanda_account_id TEXT NOT NULL,
    token_enc TEXT NOT NULL,
    token_hint TEXT NOT NULL,
    currency TEXT,
    hedging INTEGER,
    last_check TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (environment, oanda_account_id)
);
CREATE TABLE IF NOT EXISTS llm_profiles (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL UNIQUE COLLATE NOCASE,
    model TEXT NOT NULL,
    api_key_enc TEXT,
    api_key_hint TEXT,
    api_base TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategies (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    kind TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    llm_profile_id INTEGER REFERENCES llm_profiles(id),
    ai_enabled INTEGER NOT NULL DEFAULT 0,
    instruments TEXT NOT NULL,
    granularity TEXT NOT NULL,
    params TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'retired')),
    version INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    name TEXT NOT NULL COLLATE NOCASE,
    version INTEGER NOT NULL,
    config TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    strategy TEXT NOT NULL COLLATE NOCASE,
    strategy_version INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL,
    units REAL NOT NULL,
    client_id TEXT NOT NULL UNIQUE,
    broker_trade_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'open', 'closed', 'failed')),
    requested_price REAL,
    entry_price REAL,
    sl_price REAL,
    tp_price REAL,
    pip REAL NOT NULL,
    created_at TEXT NOT NULL,
    opened_at TEXT,
    exit_price REAL,
    closed_at TEXT,
    pnl REAL,
    pips REAL,
    financing REAL,
    close_reason TEXT,
    signal_reason TEXT,
    ai_model TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS trades_by_strategy ON trades (strategy, status);
CREATE INDEX IF NOT EXISTS trades_by_broker ON trades (account_id, broker_trade_id);
CREATE INDEX IF NOT EXISTS trades_by_close ON trades (closed_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    strategy TEXT,
    instrument TEXT,
    fields TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_by_strategy ON events (strategy, ts);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    result TEXT,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS candles (
    instrument TEXT NOT NULL,
    granularity TEXT NOT NULL,
    time TEXT NOT NULL,
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (instrument, granularity, time)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

CANDLES_KEPT = 2000          # per instrument/granularity
EVENTS_RETENTION_DAYS = 90


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StoreError(ValueError):
    """User-facing validation failure (duplicate name, unknown account, ...)."""


class FxStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # One short-lived connection per operation: safe across the engine's threads and the
        # console's request threads, and cheap for SQLite.
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- accounts
    def add_account(self, *, label: str, environment: str, oanda_account_id: str, token_enc: str,
                    token_hint: str) -> int:
        try:
            with self._conn() as c:
                cur = c.execute(
                    "INSERT INTO accounts (label, environment, oanda_account_id, token_enc, token_hint, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (label, environment, oanda_account_id, token_enc, token_hint, now_iso()))
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise StoreError("an account with this label or OANDA account ID already exists") from exc

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT id, label, environment, oanda_account_id, token_hint, currency, hedging,"
                             " last_check, last_error, created_at FROM accounts ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def account_secret(self, account_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT id, label, environment, oanda_account_id, token_enc FROM accounts WHERE id = ?",
                            (account_id,)).fetchone()
        return dict(row) if row else None

    def update_account_check(self, account_id: int, *, currency: str | None, hedging: bool | None,
                             error: str | None) -> None:
        with self._conn() as c:
            c.execute("UPDATE accounts SET currency = COALESCE(?, currency), hedging = COALESCE(?, hedging),"
                      " last_check = ?, last_error = ? WHERE id = ?",
                      (currency, None if hedging is None else int(hedging), now_iso(), error, account_id))

    # ---------------------------------------------------------------- AI models
    def add_llm_profile(self, *, label: str, model: str, api_key_enc: str | None, api_key_hint: str | None,
                        api_base: str | None, is_default: bool) -> int:
        try:
            with self._conn() as c:
                first = c.execute("SELECT COUNT(*) FROM llm_profiles").fetchone()[0] == 0
                if is_default or first:
                    c.execute("UPDATE llm_profiles SET is_default = 0")
                cur = c.execute(
                    "INSERT INTO llm_profiles (label, model, api_key_enc, api_key_hint, api_base, is_default, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (label, model, api_key_enc, api_key_hint, api_base, int(is_default or first), now_iso()))
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise StoreError("an AI model with this label already exists") from exc

    def update_llm_profile(self, profile_id: int, *, model: str, api_key_enc: str | None,
                           api_key_hint: str | None, api_base: str | None) -> None:
        with self._conn() as c:
            if api_key_enc is not None:
                c.execute("UPDATE llm_profiles SET api_key_enc = ?, api_key_hint = ? WHERE id = ?",
                          (api_key_enc, api_key_hint, profile_id))
            cur = c.execute("UPDATE llm_profiles SET model = ?, api_base = ? WHERE id = ?",
                            (model, api_base, profile_id))
            if cur.rowcount == 0:
                raise StoreError("unknown AI model")

    def set_default_llm(self, profile_id: int) -> None:
        with self._conn() as c:
            if not c.execute("SELECT 1 FROM llm_profiles WHERE id = ?", (profile_id,)).fetchone():
                raise StoreError("unknown AI model")
            c.execute("UPDATE llm_profiles SET is_default = (id = ?)", (profile_id,))

    def list_llm_profiles(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT id, label, model, api_key_hint, api_base, is_default, created_at"
                             " FROM llm_profiles ORDER BY id").fetchall()
        return [dict(r) | {"is_default": bool(r["is_default"])} for r in rows]

    def llm_profile_secret(self, profile_id: int | None) -> dict[str, Any] | None:
        """The given profile, or the default one when ``profile_id`` is None."""
        with self._conn() as c:
            if profile_id is None:
                row = c.execute("SELECT * FROM llm_profiles WHERE is_default = 1").fetchone()
            else:
                row = c.execute("SELECT * FROM llm_profiles WHERE id = ?", (profile_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- strategies
    @staticmethod
    def _strategy_from_row(row: sqlite3.Row) -> StrategyRecord:
        return StrategyRecord(
            name=row["name"], kind=row["kind"], account_id=row["account_id"],
            llm_profile_id=row["llm_profile_id"], ai_enabled=bool(row["ai_enabled"]),
            instruments=json.loads(row["instruments"]), granularity=row["granularity"],
            params=json.loads(row["params"]), risk=json.loads(row["risk"]), status=row["status"],
            version=row["version"], notes=row["notes"], created_at=row["created_at"], updated_at=row["updated_at"])

    @staticmethod
    def _config_json(rec: StrategyRecord) -> str:
        return rec.model_dump_json(include={"kind", "account_id", "instruments", "granularity", "params", "risk",
                                            "ai_enabled", "llm_profile_id"})

    def _check_refs(self, c: sqlite3.Connection, rec: StrategyRecord) -> None:
        if not c.execute("SELECT 1 FROM accounts WHERE id = ?", (rec.account_id,)).fetchone():
            raise StoreError("unknown account")
        if rec.llm_profile_id is not None and not c.execute(
                "SELECT 1 FROM llm_profiles WHERE id = ?", (rec.llm_profile_id,)).fetchone():
            raise StoreError("unknown AI model")

    def create_strategy(self, rec: StrategyRecord, note: str = "created") -> StrategyRecord:
        ts = now_iso()
        with self._conn() as c:
            if c.execute("SELECT 1 FROM strategies WHERE name = ?", (rec.name,)).fetchone():
                raise StoreError(f"the name {rec.name!r} is already taken (names are never reused)")
            self._check_refs(c, rec)
            c.execute(
                "INSERT INTO strategies (name, kind, account_id, llm_profile_id, ai_enabled, instruments, granularity,"
                " params, risk, status, version, notes, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (rec.name, rec.kind, rec.account_id, rec.llm_profile_id, int(rec.ai_enabled),
                 json.dumps(rec.instruments), rec.granularity, json.dumps(rec.params),
                 rec.risk.model_dump_json(), rec.status.value, rec.notes, ts, ts))
            c.execute("INSERT INTO strategy_versions (name, version, config, note, changed_at) VALUES (?, 1, ?, ?, ?)",
                      (rec.name, self._config_json(rec), note, ts))
        return self.get_strategy(rec.name)  # type: ignore[return-value]

    def update_strategy(self, rec: StrategyRecord, note: str = "") -> StrategyRecord:
        """Save an edit. Changes to trading config bump the version, so performance can be split by version."""
        ts = now_iso()
        with self._conn() as c:
            row = c.execute("SELECT * FROM strategies WHERE name = ?", (rec.name,)).fetchone()
            if not row:
                raise StoreError("unknown strategy")
            if row["status"] == StrategyStatus.RETIRED:
                raise StoreError("retired strategies cannot be edited; duplicate it under a new name")
            self._check_refs(c, rec)
            current = self._strategy_from_row(row)
            version = current.version
            if self._config_json(current) != self._config_json(rec):
                version += 1
                c.execute("INSERT INTO strategy_versions (name, version, config, note, changed_at)"
                          " VALUES (?, ?, ?, ?, ?)", (row["name"], version, self._config_json(rec), note, ts))
            c.execute(
                "UPDATE strategies SET kind = ?, account_id = ?, llm_profile_id = ?, ai_enabled = ?, instruments = ?,"
                " granularity = ?, params = ?, risk = ?, version = ?, notes = ?, updated_at = ? WHERE name = ?",
                (rec.kind, rec.account_id, rec.llm_profile_id, int(rec.ai_enabled), json.dumps(rec.instruments),
                 rec.granularity, json.dumps(rec.params), rec.risk.model_dump_json(), version, rec.notes, ts,
                 row["name"]))
        return self.get_strategy(rec.name)  # type: ignore[return-value]

    def set_strategy_status(self, name: str, status: StrategyStatus) -> None:
        with self._conn() as c:
            row = c.execute("SELECT status FROM strategies WHERE name = ?", (name,)).fetchone()
            if not row:
                raise StoreError("unknown strategy")
            if row["status"] == StrategyStatus.RETIRED:
                raise StoreError("retired strategies stay retired")
            if status is StrategyStatus.RETIRED and c.execute(
                    "SELECT 1 FROM trades WHERE strategy = ? AND status IN ('open', 'pending')", (name,)).fetchone():
                raise StoreError("close this strategy's open trades before retiring it")
            c.execute("UPDATE strategies SET status = ?, updated_at = ? WHERE name = ?", (status.value, now_iso(), name))

    def get_strategy(self, name: str) -> StrategyRecord | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM strategies WHERE name = ?", (name,)).fetchone()
        return self._strategy_from_row(row) if row else None

    def list_strategies(self) -> list[StrategyRecord]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM strategies ORDER BY created_at, name").fetchall()
        return [self._strategy_from_row(r) for r in rows]

    def strategy_versions(self, name: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT version, config, note, changed_at FROM strategy_versions WHERE name = ?"
                             " ORDER BY version DESC", (name,)).fetchall()
        return [{"version": r["version"], "config": json.loads(r["config"]), "note": r["note"],
                 "changed_at": r["changed_at"]} for r in rows]

    # ---------------------------------------------------------------- trades
    def insert_pending_trade(self, **fields: Any) -> int:
        fields = {"status": "pending", "created_at": now_iso(), **fields}
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._conn() as c:
            return int(c.execute(f"INSERT INTO trades ({cols}) VALUES ({marks})", tuple(fields.values())).lastrowid)

    def mark_trade_open(self, trade_id: int, *, broker_trade_id: str, entry_price: float, units: float,
                        opened_at: str, sl_price: float | None, tp_price: float | None) -> None:
        with self._conn() as c:
            c.execute("UPDATE trades SET status = 'open', broker_trade_id = ?, entry_price = ?, units = ?, opened_at = ?,"
                      " sl_price = COALESCE(?, sl_price), tp_price = COALESCE(?, tp_price), error = NULL"
                      " WHERE id = ? AND status IN ('pending', 'failed')",
                      (broker_trade_id, entry_price, units, opened_at, sl_price, tp_price, trade_id))

    def mark_trade_failed(self, trade_id: int, error: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE trades SET status = 'failed', error = ? WHERE id = ? AND status = 'pending'",
                      (error[:500], trade_id))

    def mark_trade_closed(self, account_id: int, broker_trade_id: str, *, exit_price: float | None, pnl: float,
                          financing: float, closed_at: str, reason: str) -> dict[str, Any] | None:
        """Idempotent: the transaction stream, a manual close and reconciliation may all report the same close."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM trades WHERE account_id = ? AND broker_trade_id = ? AND status = 'open'",
                            (account_id, broker_trade_id)).fetchone()
            if not row:
                return None
            pips = None
            if exit_price is not None and row["entry_price"]:
                sign = 1 if row["direction"] == "long" else -1
                pips = round((exit_price - row["entry_price"]) * sign / row["pip"], 1)
            c.execute("UPDATE trades SET status = 'closed', exit_price = ?, pnl = ?, financing = ?, closed_at = ?,"
                      " pips = ?, close_reason = ? WHERE id = ?",
                      (exit_price, round(pnl + financing, 4), financing, closed_at, pips, reason, row["id"]))
            return dict(row) | {"pnl": round(pnl + financing, 4), "pips": pips, "close_reason": reason}

    def trades(self, *, strategy: str | None = None, status: str | None = None, account_id: int | None = None,
               since: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        where, args = [], []
        for col, val in (("strategy = ?", strategy), ("status = ?", status), ("account_id = ?", account_id)):
            if val is not None:
                where.append(col)
                args.append(val)
        if since:
            where.append("COALESCE(closed_at, opened_at, created_at) >= ?")
            args.append(since)
        sql = "SELECT * FROM trades" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY COALESCE(closed_at, opened_at, created_at) DESC, id DESC LIMIT ?"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, (*args, limit)).fetchall()]

    def open_trades(self, *, account_id: int | None = None, strategy: str | None = None) -> list[dict[str, Any]]:
        return self.trades(status="open", account_id=account_id, strategy=strategy, limit=10_000)

    def pending_trades(self) -> list[dict[str, Any]]:
        return self.trades(status="pending", limit=10_000)

    def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def realized_since(self, strategy: str, since: str) -> float:
        with self._conn() as c:
            row = c.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE strategy = ? AND status = 'closed'"
                            " AND closed_at >= ?", (strategy, since)).fetchone()
        return float(row[0])

    # ---------------------------------------------------------------- events
    def add_event(self, event: str, *, level: str = "INFO", strategy: str | None = None,
                  instrument: str | None = None, **fields: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events (ts, level, event, strategy, instrument, fields) VALUES (?, ?, ?, ?, ?, ?)",
                      (datetime.now(UTC).isoformat(timespec="milliseconds"), level, event, strategy, instrument,
                       json.dumps(fields, default=str)))

    def events(self, *, strategy: str | None = None, limit: int = 200, since: str | None = None,
               names: set[str] | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if strategy:
            where.append("strategy = ?")
            args.append(strategy)
        if since:
            where.append("ts >= ?")
            args.append(since)
        if names:
            where.append(f"event IN ({', '.join('?' for _ in names)})")
            args.extend(sorted(names))
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(sql, (*args, limit)).fetchall()
        return [{"ts": r["ts"], "level": r["level"], "event": r["event"], "strategy": r["strategy"],
                 "instrument": r["instrument"], **json.loads(r["fields"])} for r in rows]

    def prune(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=EVENTS_RETENTION_DAYS)).isoformat()
        with self._conn() as c:
            c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM commands WHERE status IN ('done', 'failed') AND done_at < ?", (cutoff,))
            c.execute("""DELETE FROM candles WHERE (instrument, granularity, time) IN (
                             SELECT instrument, granularity, time FROM (
                                 SELECT instrument, granularity, time,
                                        ROW_NUMBER() OVER (PARTITION BY instrument, granularity ORDER BY time DESC) AS rn
                                 FROM candles) WHERE rn > ?)""", (CANDLES_KEPT,))

    # ---------------------------------------------------------------- commands (console -> engine)
    def enqueue_command(self, kind: str, target: str) -> int:
        with self._conn() as c:
            return int(c.execute("INSERT INTO commands (created_at, kind, target) VALUES (?, ?, ?)",
                                 (now_iso(), kind, target)).lastrowid)

    def claim_commands(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM commands WHERE status = 'queued' ORDER BY id").fetchall()
            if rows:
                c.execute(f"UPDATE commands SET status = 'running' WHERE id IN ({', '.join(str(r['id']) for r in rows)})")
        return [dict(r) for r in rows]

    def finish_command(self, command_id: int, ok: bool, result: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE commands SET status = ?, result = ?, done_at = ? WHERE id = ?",
                      ("done" if ok else "failed", result[:500], now_iso(), command_id))

    def get_command(self, command_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM commands WHERE id = ?", (command_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- candle cache
    def save_candles(self, instrument: str, granularity: str, candles: list[Candle]) -> None:
        if not candles:
            return
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO candles (instrument, granularity, time, o, h, l, c, volume)"
                          " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          [(instrument, granularity, k.time.isoformat(), k.o, k.h, k.l, k.c, k.volume) for k in candles])

    def load_candles(self, instrument: str, granularity: str, limit: int) -> list[Candle]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM candles WHERE instrument = ? AND granularity = ? ORDER BY time DESC LIMIT ?",
                             (instrument, granularity, limit)).fetchall()
        return [Candle(time=datetime.fromisoformat(r["time"]), o=r["o"], h=r["h"], l=r["l"], c=r["c"],
                       volume=r["volume"]) for r in reversed(rows)]

    # ---------------------------------------------------------------- key/value (engine status, halts)
    def kv_set(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                      " value = excluded.value, updated_at = excluded.updated_at",
                      (key, json.dumps(value, default=str), now_iso()))

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_prefix(self, prefix: str) -> dict[str, Any]:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM kv WHERE key LIKE ?", (prefix + "%",)).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}
