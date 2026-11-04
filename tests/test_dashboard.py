from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings
from core.journal import JournalHandler, read_events
from core.observability import log_event, new_correlation_id
from dashboard.app import DashboardService, make_handler
from dashboard.history import (
    Fill,
    by_symbol,
    daily_pnl,
    fills_from_orders,
    max_drawdown_pct,
    pair_round_trips,
    trade_stats,
)


def order(oid: str, side: str, qty: float, price: float, at: str, legs: list[dict] | None = None,
          symbol: str = "SPY") -> dict[str, Any]:
    return {"id": oid, "symbol": symbol, "side": side, "filled_qty": qty, "filled_avg_price": price,
            "filled_at": at, "legs": legs or []}


# --- Trade pairing -------------------------------------------------------------
def test_bracket_legs_pair_into_round_trip() -> None:
    entry = order("1", "buy", 10, 100.0, "2026-11-04T15:00:00+00:00", legs=[
        order("2", "sell", 10, 103.0, "2026-11-04T16:00:00+00:00"),       # take-profit filled
        {"id": "3", "symbol": "SPY", "side": "sell", "filled_qty": 0, "filled_avg_price": None,
         "filled_at": None, "legs": []},                                   # stop leg cancelled
    ])
    trades = pair_round_trips(fills_from_orders([entry]))
    assert len(trades) == 1
    t = trades[0]
    assert (t["direction"], t["qty"], t["pnl"], t["return_pct"]) == ("long", 10, 30.0, 3.0)


def test_short_and_partial_fills_fifo() -> None:
    fills = [
        Fill("QQQ", "sell", 5, 200.0, "2026-11-04T15:00:00", "a"),
        Fill("QQQ", "buy", 2, 198.0, "2026-11-04T15:10:00", "b"),
        Fill("QQQ", "buy", 3, 201.0, "2026-11-04T15:20:00", "c"),
    ]
    trades = pair_round_trips(fills)
    assert [t["pnl"] for t in trades] == [-3.0, 4.0]  # newest first
    assert all(t["direction"] == "short" for t in trades)


def test_trade_stats() -> None:
    trades = [
        {"pnl": 30.0, "opened_at": "2026-11-04T15:00:00+00:00", "closed_at": "2026-11-04T15:30:00+00:00"},
        {"pnl": -10.0, "opened_at": "2026-11-04T16:00:00+00:00", "closed_at": "2026-11-04T16:10:00+00:00"},
        {"pnl": 20.0},
    ]
    stats = trade_stats(trades)
    assert stats == {"trades": 3, "wins": 2, "losses": 1, "win_rate": 66.7, "net_pnl": 40.0,
                     "avg_win": 25.0, "avg_loss": -10.0, "profit_factor": 5.0, "expectancy": 13.33,
                     "best": 30.0, "worst": -10.0, "avg_hold_min": 20.0}
    assert trade_stats([])["win_rate"] is None


def test_daily_pnl_drawdown_and_by_symbol() -> None:
    points = [{"date": "2026-11-02", "equity": 101.0}, {"date": "2026-11-03", "equity": 99.0},
              {"date": "2026-11-04", "equity": 102.0}]
    assert [d["pnl"] for d in daily_pnl(points, 100.0)] == [1.0, -2.0, 3.0]
    assert max_drawdown_pct([100.0, 110.0, 99.0, 120.0]) == 10.0
    assert max_drawdown_pct([]) is None
    rows = by_symbol([{"symbol": "SPY", "pnl": 5.0}, {"symbol": "QQQ", "pnl": -2.0}, {"symbol": "SPY", "pnl": -1.0}])
    assert rows[0] == {"symbol": "SPY", "trades": 2, "net_pnl": 4.0, "win_rate": 50.0}


# --- Journal ---------------------------------------------------------------------
def test_journal_records_only_decision_events(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    handler = JournalHandler(path)
    logger = logging.getLogger("test.journal")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        cid = new_correlation_id()
        log_event(logger, logging.INFO, "risk.rejected", symbol="SPY", reason="pdt_limit", detail="3 used")
        log_event(logger, logging.INFO, "external.retry", op="x", attempt=1)  # not journaled
        log_event(logger, logging.INFO, "signal", symbol="QQQ", action="BUY", reasoning="<script>x</script>")
    finally:
        logger.removeHandler(handler)
        handler.close()
    events = read_events(path)
    assert [e["event"] for e in events] == ["signal", "risk.rejected"]
    assert events[1]["cid"] == cid and events[1]["reason"] == "pdt_limit"
    assert [e["event"] for e in read_events(path, symbol="SPY")] == ["risk.rejected"]
    assert read_events(tmp_path / "missing.db") == []


# --- HTTP service ------------------------------------------------------------------
class FakeHistory:
    def account(self) -> dict[str, Any]:
        return {"equity": 100_250.0, "last_equity": 100_000.0, "day_pl": 250.0, "day_pl_pct": 0.25,
                "buying_power": 400_000.0, "cash": 100_000.0, "daytrade_count": 1,
                "pattern_day_trader": False, "status": "ACTIVE", "trading_blocked": False}

    def positions(self) -> list[dict[str, Any]]:
        return []

    def clock(self) -> dict[str, Any]:
        return {"is_open": True, "next_open": None, "next_close": None}

    def orders(self, days: int) -> list[dict[str, Any]]:
        return [order("1", "buy", 10, 100.0, "2026-11-04T15:00:00+00:00",
                      legs=[order("2", "sell", 10, 99.0, "2026-11-04T15:30:00+00:00")])]

    def equity(self, period: str, timeframe: str) -> dict[str, Any]:
        return {"period": period, "timeframe": timeframe, "base_value": 100_000.0,
                "points": [{"t": datetime.now(UTC).isoformat(), "equity": 100_250.0, "pl": 250.0}]}


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    settings = Settings(_env_file=None, state_dir=tmp_path)  # type: ignore[arg-type]
    (tmp_path / "risk_state.json").write_text(json.dumps({"halt": {"session_date": "2026-11-04",
                                                                   "reason": "daily drawdown 3.10%"}}))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DashboardService(settings, FakeHistory())))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if resp.headers["Content-Type"].startswith("application/json") else body
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_api_endpoints(server: str) -> None:
    status, summary = _get(server + "/api/summary")
    assert status == 200 and summary["account"]["day_pl"] == 250.0
    assert summary["halt"]["reason"] == "daily drawdown 3.10%"
    assert summary["bot"]["state"] == "unknown"  # no journal yet
    status, trades = _get(server + "/api/trades?days=30")
    assert status == 200 and trades["stats"]["net_pnl"] == -10.0
    assert _get(server + "/api/equity?range=1D")[1]["timeframe"] == "5Min"
    assert _get(server + "/api/decisions")[1] == {"events": []}
    risk = summary["risk"]
    assert risk["daily_loss"] == {"used": 0.0, "limit": 3.0}
    assert risk["positions"] == {"used": 0, "limit": 2} and risk["trades_today"]["limit"] == 6
    assert risk["day_trades"]["applies"] is False  # paper equity above the PDT threshold

    status, perf = _get(server + "/api/performance?days=30")
    assert status == 200 and perf["stats"]["trades"] == 1 and perf["daily"][0]["pnl"] == 250.0
    status, cfg = _get(server + "/api/config")
    assert status == 200 and cfg["trading"]["mode"] == "paper"
    assert {"rule": "Risk per trade", "value": 1.0, "hard": 2.0, "unit": "% of equity", "kind": "max"} in cfg["limits"]
    assert "alpaca_api_key" not in json.dumps(cfg)
    status, ai = _get(server + "/api/ai-usage")
    assert status == 200 and ai["today"]["calls"] == 0 and ai["week"]["cost_usd"] is None


def test_static_and_safety(server: str) -> None:
    status, body = _get(server + "/")
    assert status == 200 and b"Trading Console" in body
    assert _get(server + "/static/app.js")[0] == 200
    assert _get(server + "/static/../core/config.py")[0] == 404
    assert _get(server + "/nope")[0] == 404
    req = urllib.request.Request(server + "/api/summary", method="POST", data=b"")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 405
