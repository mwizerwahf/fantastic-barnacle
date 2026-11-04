"""Read-only trading dashboard.

Serves a static page plus JSON endpoints built from Alpaca's API and the bot's
decision journal. It binds to localhost only; nginx adds TLS and a password.
It has no code path that places, cancels or closes anything.

Run: python -m dashboard.app
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import parse_qs, urlsplit

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
from dotenv import load_dotenv

from core.config import Settings
from core.exceptions import ConfigError
from core.journal import read_events
from core.observability import log_event, setup_logging
from core.resilience import call_external
from dashboard.history import fills_from_orders, pair_round_trips, trade_stats

T = TypeVar("T")
log = logging.getLogger("dashboard")
STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {"index.html", "app.js", "style.css"}
CACHE_TTL_S = 15.0
DECISION_EVENTS = {"signal", "ai.decision", "ai.invalid_response_final", "ai.all_providers_failed",
                   "risk.approved", "risk.rejected", "risk.kill_switch", "risk.force_flatten",
                   "order.submitted", "order.recovered_after_error", "symbol.skipped"}
EQUITY_RANGES = {"1D": ("1D", "5Min"), "1W": ("1W", "1H"), "1M": ("1M", "1D"), "3M": ("3M", "1D")}


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _order_dict(o: Any) -> dict[str, Any]:
    return {
        "id": str(o.id), "client_order_id": o.client_order_id, "symbol": o.symbol,
        "side": _plain(o.side), "type": _plain(o.order_type or o.type), "status": _plain(o.status),
        "order_class": _plain(o.order_class), "qty": _num(o.qty), "filled_qty": _num(o.filled_qty),
        "filled_avg_price": _num(o.filled_avg_price), "limit_price": _num(o.limit_price),
        "stop_price": _num(o.stop_price), "submitted_at": _plain(o.submitted_at), "filled_at": _plain(o.filled_at),
        "by_bot": str(o.client_order_id or "").startswith("tb-"),
        "legs": [_order_dict(leg) for leg in (o.legs or [])],
    }


class HistorySource(Protocol):
    def account(self) -> dict[str, Any]: ...
    def positions(self) -> list[dict[str, Any]]: ...
    def clock(self) -> dict[str, Any]: ...
    def orders(self, days: int) -> list[dict[str, Any]]: ...
    def equity(self, period: str, timeframe: str) -> dict[str, Any]: ...


class AlpacaHistory:
    """Read-only view of the Alpaca account. Only GET endpoints are used."""

    def __init__(self, settings: Settings, client: TradingClient | None = None) -> None:
        key = settings.alpaca_api_key.get_secret_value()
        secret = settings.alpaca_secret_key.get_secret_value()
        if client is None and not (key and secret):
            raise ConfigError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
        self._client = client or TradingClient(key, secret, paper=settings.is_paper)
        self._timeout = settings.broker_timeout_s
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _cached(self, key: str, fn: Callable[[], T]) -> T:
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        value = call_external(fn, op=f"dashboard.{key}", timeout=self._timeout, attempts=2, backoff=0.5)
        with self._lock:
            self._cache[key] = (time.monotonic() + CACHE_TTL_S, value)
        return value

    def account(self) -> dict[str, Any]:
        a = self._cached("account", self._client.get_account)
        equity, last = _num(a.equity) or 0.0, _num(a.last_equity) or 0.0
        return {
            "equity": equity, "last_equity": last, "day_pl": round(equity - last, 2),
            "day_pl_pct": round((equity - last) / last * 100, 3) if last else None,
            "buying_power": _num(a.buying_power), "cash": _num(a.cash),
            "daytrade_count": a.daytrade_count, "pattern_day_trader": a.pattern_day_trader,
            "status": _plain(a.status), "trading_blocked": bool(a.trading_blocked or a.account_blocked),
        }

    def positions(self) -> list[dict[str, Any]]:
        return [
            {"symbol": p.symbol, "side": _plain(p.side), "qty": _num(p.qty), "avg_entry_price": _num(p.avg_entry_price),
             "current_price": _num(p.current_price), "market_value": _num(p.market_value),
             "unrealized_pl": _num(p.unrealized_pl), "unrealized_plpc": _num(p.unrealized_plpc)}
            for p in self._cached("positions", self._client.get_all_positions)
        ]

    def clock(self) -> dict[str, Any]:
        c = self._cached("clock", self._client.get_clock)
        return {"is_open": c.is_open, "next_open": _plain(c.next_open), "next_close": _plain(c.next_close)}

    def orders(self, days: int) -> list[dict[str, Any]]:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=start, nested=True, limit=500)
        raw = self._cached(f"orders:{days}", lambda: self._client.get_orders(filter=req))
        return [_order_dict(o) for o in raw]

    def equity(self, period: str, timeframe: str) -> dict[str, Any]:
        req = GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
        h = self._cached(f"equity:{period}:{timeframe}", lambda: self._client.get_portfolio_history(req))
        points = [
            {"t": datetime.fromtimestamp(ts, UTC).isoformat(), "equity": eq, "pl": pl}
            for ts, eq, pl in zip(h.timestamp or [], h.equity or [], h.profit_loss or [])
            if eq is not None
        ]
        return {"period": period, "timeframe": timeframe, "base_value": h.base_value, "points": points}


class DashboardService:
    def __init__(self, settings: Settings, source: HistorySource) -> None:
        self._settings = settings
        self._source = source
        self._journal = settings.state_dir / "journal.db"
        self._risk_state = settings.state_dir / "risk_state.json"

    def _bot_status(self) -> dict[str, Any]:
        beats = read_events(self._journal, limit=1, events={"heartbeat"})
        if not beats:
            return {"state": "unknown", "last_heartbeat": None}
        last = beats[0]
        age = (datetime.now(UTC) - datetime.fromisoformat(last["ts"])).total_seconds()
        # The bot sleeps up to 15 minutes between heartbeats while the market is closed.
        limit = max(self._settings.timeframe_minutes * 60 * 3, 20 * 60)
        return {"state": "running" if age <= limit else "stale", "last_heartbeat": last["ts"],
                "age_s": round(age), "gate": last.get("gate"), "mode": last.get("mode")}

    def _halt(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._risk_state.read_text(encoding="utf-8")).get("halt")
        except (OSError, ValueError):
            return None

    def summary(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": self._settings.trading_mode,
            "account": self._source.account(),
            "positions": self._source.positions(),
            "clock": self._source.clock(),
            "bot": self._bot_status(),
            "halt": self._halt(),
            "symbols": self._settings.symbols,
        }

    def orders(self, days: int) -> dict[str, Any]:
        return {"days": days, "orders": self._source.orders(days)}

    def trades(self, days: int) -> dict[str, Any]:
        trades = pair_round_trips(fills_from_orders(self._source.orders(days)))
        return {"days": days, "stats": trade_stats(trades), "trades": trades}

    def equity(self, range_key: str) -> dict[str, Any]:
        period, timeframe = EQUITY_RANGES.get(range_key, EQUITY_RANGES["1M"])
        return self._source.equity(period, timeframe)

    def decisions(self, limit: int, symbol: str | None) -> dict[str, Any]:
        return {"events": read_events(self._journal, limit=limit, events=DECISION_EVENTS, symbol=symbol)}


def _int_param(query: dict[str, list[str]], name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(query.get(name, [default])[0])))
    except ValueError:
        return default


def make_handler(service: DashboardService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tdpltfm"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            log_event(log, logging.DEBUG, "http", line=fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, default=str).encode(), "application/json")

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            path = url.path.rstrip("/") or "/"
            routes: dict[str, Callable[[], Any]] = {
                "/api/summary": service.summary,
                "/api/orders": lambda: service.orders(_int_param(query, "days", 7, 1, 90)),
                "/api/trades": lambda: service.trades(_int_param(query, "days", 30, 1, 90)),
                "/api/equity": lambda: service.equity(query.get("range", ["1M"])[0]),
                "/api/decisions": lambda: service.decisions(_int_param(query, "limit", 200, 1, 1000),
                                                            (query.get("symbol", [""])[0] or None)),
                "/healthz": lambda: {"ok": True},
            }
            if path in routes:
                try:
                    self._json(HTTPStatus.OK, routes[path]())
                except Exception as exc:
                    log_event(log, logging.ERROR, "dashboard.api_error", path=path, error=repr(exc))
                    self._json(HTTPStatus.BAD_GATEWAY, {"error": f"{type(exc).__name__}: upstream request failed"})
                return
            name = "index.html" if path == "/" else path.removeprefix("/static/")
            if name in STATIC_FILES:
                body = (STATIC_DIR / name).read_bytes()
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self._send(HTTPStatus.OK, body, f"{ctype}; charset=utf-8")
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read-only"})

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def main() -> None:
    load_dotenv()
    settings = Settings()
    setup_logging(settings.log_level, settings.log_json)
    service = DashboardService(settings, AlpacaHistory(settings))
    server = ThreadingHTTPServer((settings.dashboard_host, settings.dashboard_port), make_handler(service))
    log_event(log, logging.INFO, "dashboard.start", host=settings.dashboard_host, port=settings.dashboard_port,
              mode=settings.trading_mode)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
