"""Minimal OANDA v20 client: only the endpoints the engine needs.

Reads go through ``call_external`` (hard timeout + retries on transient errors). Order placement
is never retried blindly: after an ambiguous failure the caller looks the trade up by client ID.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import httpx

from core.exceptions import BrokerError, OrderSubmissionError
from core.observability import log_event
from core.resilience import call_external
from fx.models import Candle, InstrumentSpec, Price

log = logging.getLogger(__name__)

HOSTS = {
    "practice": ("https://api-fxpractice.oanda.com", "https://stream-fxpractice.oanda.com"),
    "live": ("https://api-fxtrade.oanda.com", "https://stream-fxtrade.oanda.com"),
}


class OandaError(BrokerError):
    def __init__(self, message: str, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code  # read by core.resilience.is_transient
        self.code = code


class OrderRejected(OrderSubmissionError):
    """OANDA answered and refused the order (definitive: nothing was opened)."""


def parse_time(value: str) -> datetime:
    """OANDA RFC3339 with nanoseconds -> aware datetime."""
    v = value.rstrip("Z")
    if "." in v:
        head, frac = v.split(".", 1)
        v = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(v).replace(tzinfo=UTC)


def fmt_price(value: float, precision: int) -> str:
    return f"{value:.{precision}f}"


class OandaClient:
    def __init__(self, environment: str, account_id: str, token: str, *, timeout: float = 10.0,
                 attempts: int = 3, backoff: float = 0.5, transport: httpx.BaseTransport | None = None) -> None:
        if environment not in HOSTS:
            raise ValueError(f"unknown OANDA environment {environment!r}")
        rest, stream = HOSTS[environment]
        self.environment = environment
        self.account_id = account_id
        self._timeout = timeout
        self._attempts = attempts
        self._backoff = backoff
        headers = {"Authorization": f"Bearer {token}", "Accept-Datetime-Format": "RFC3339",
                   "Content-Type": "application/json"}
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        self._http = httpx.Client(base_url=rest, headers=headers, timeout=timeout, transport=transport, limits=limits)
        self._stream = httpx.Client(base_url=stream, headers=headers, transport=transport,
                                    timeout=httpx.Timeout(timeout, read=30.0))

    def close(self) -> None:
        self._http.close()
        self._stream.close()

    # ------------------------------------------------------------------ plumbing
    def _send(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = self._http.request(method, path, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            msg = body.get("errorMessage") or resp.reason_phrase or "request failed"
            raise OandaError(f"OANDA {resp.status_code}: {msg}", resp.status_code, body.get("errorCode"))
        return body

    def _get(self, path: str, op: str, **params: Any) -> dict[str, Any]:
        return call_external(lambda: self._send("GET", path, params=params or None), op=f"oanda.{op}",
                             timeout=self._timeout + 2, attempts=self._attempts, backoff=self._backoff)

    @property
    def _acct(self) -> str:
        return f"/v3/accounts/{self.account_id}"

    # ------------------------------------------------------------------ account / instruments
    def summary(self) -> dict[str, Any]:
        a = self._get(f"{self._acct}/summary", "summary")["account"]
        return {
            "currency": a.get("currency"), "balance": float(a.get("balance", 0)), "nav": float(a.get("NAV", 0)),
            "unrealized_pl": float(a.get("unrealizedPL", 0)), "margin_available": float(a.get("marginAvailable", 0)),
            "open_trades": int(a.get("openTradeCount", 0)), "hedging": bool(a.get("hedgingEnabled", False)),
        }

    def instruments(self) -> dict[str, InstrumentSpec]:
        out: dict[str, InstrumentSpec] = {}
        for i in self._get(f"{self._acct}/instruments", "instruments").get("instruments", []):
            out[i["name"]] = InstrumentSpec(
                name=i["name"], pip_location=int(i["pipLocation"]), display_precision=int(i["displayPrecision"]),
                units_precision=int(i.get("tradeUnitsPrecision", 0)), min_units=float(i.get("minimumTradeSize", 1)))
        return out

    def candles(self, instrument: str, granularity: str, *, count: int | None = None,
                since: datetime | None = None) -> list[Candle]:
        """Completed mid candles, oldest first. The still-forming candle is dropped."""
        # UTC alignment so H4 candles close on 00/04/08... UTC, matching the engine's schedule.
        params: dict[str, Any] = {"granularity": granularity, "price": "M", "count": min(count or 500, 5000),
                                  "alignmentTimezone": "UTC", "dailyAlignment": 0}
        if since is not None:
            params["from"] = since.astimezone(UTC).isoformat().replace("+00:00", "Z")
        body = self._get(f"/v3/instruments/{instrument}/candles", f"candles.{instrument}", **params)
        out = []
        for k in body.get("candles", []):
            if not k.get("complete"):
                continue
            m = k["mid"]
            out.append(Candle(time=parse_time(k["time"]), o=float(m["o"]), h=float(m["h"]), l=float(m["l"]),
                              c=float(m["c"]), volume=int(k.get("volume", 0))))
        return out

    def pricing(self, instruments: Iterable[str]) -> dict[str, Price]:
        """Fresh bid/ask plus the factors that convert quote-currency P/L into the account currency."""
        names = sorted(set(instruments))
        body = self._get(f"{self._acct}/pricing", "pricing", instruments=",".join(names), includeHomeConversions="true")
        conv = {h["currency"]: (float(h["accountGain"]), float(h["accountLoss"]))
                for h in body.get("homeConversions", [])}
        out: dict[str, Price] = {}
        for p in body.get("prices", []):
            quote = p["instrument"].split("_", 1)[1]
            gain, loss = conv.get(quote, (None, None))
            out[p["instrument"]] = Price(
                instrument=p["instrument"], bid=float(p["bids"][0]["price"]), ask=float(p["asks"][0]["price"]),
                time=parse_time(p["time"]), tradeable=bool(p.get("tradeable", True)), conv_gain=gain, conv_loss=loss)
        return out

    # ------------------------------------------------------------------ trading
    def market_order(self, *, instrument: str, units: float, client_id: str, tag: str, comment: str,
                     sl_price: float, tp_price: float | None, price_bound: float, precision: int,
                     units_precision: int = 0) -> dict[str, Any]:
        """Fill-or-kill market order with SL/TP attached. Returns the opened trade.

        ``price_bound`` is the worst acceptable fill: OANDA cancels the order instead of filling
        beyond it, which caps slippage from the decision-to-fill delay.
        """
        # The trade carries client_id (looked up as "@client_id" after an ambiguous failure); the order gets
        # its own ID so the two never collide.
        ext = {"id": client_id, "tag": tag[:50], "comment": comment[:100]}
        order_ext = ext | {"id": f"{client_id}-o"}
        order: dict[str, Any] = {
            "type": "MARKET", "instrument": instrument, "units": f"{units:.{units_precision}f}",
            "timeInForce": "FOK", "positionFill": "OPEN_ONLY", "priceBound": fmt_price(price_bound, precision),
            "clientExtensions": order_ext, "tradeClientExtensions": ext,
            "stopLossOnFill": {"price": fmt_price(sl_price, precision), "timeInForce": "GTC"},
        }
        if tp_price is not None:
            order["takeProfitOnFill"] = {"price": fmt_price(tp_price, precision), "timeInForce": "GTC"}

        def _submit() -> dict[str, Any]:
            return self._send("POST", f"{self._acct}/orders", content=json.dumps({"order": order}))

        try:
            body = call_external(_submit, op=f"oanda.order.{instrument}", timeout=self._timeout + 2, attempts=1,
                                 backoff=self._backoff)
        except OandaError as exc:
            if exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code not in (408, 429):
                raise OrderRejected(str(exc)) from exc
            raise
        fill = body.get("orderFillTransaction")
        if not fill or not fill.get("tradeOpened"):
            reason = (body.get("orderCancelTransaction") or {}).get("reason", "no fill")
            raise OrderRejected(f"order not filled: {reason}")
        opened = fill["tradeOpened"]
        return {"trade_id": str(opened["tradeID"]), "price": float(opened.get("price") or fill.get("price")),
                "units": float(opened["units"]), "time": fill.get("time")}

    def close_trade(self, trade_id: str) -> dict[str, Any]:
        def _close() -> dict[str, Any]:
            return self._send("PUT", f"{self._acct}/trades/{trade_id}/close", content=json.dumps({"units": "ALL"}))

        body = call_external(_close, op="oanda.close", timeout=self._timeout + 2, attempts=1, backoff=self._backoff)
        fill = body.get("orderFillTransaction") or {}
        for t in fill.get("tradesClosed", []):
            if str(t["tradeID"]) == str(trade_id):
                return {"price": float(t["price"]), "pnl": float(t.get("realizedPL", 0)),
                        "financing": float(t.get("financing", 0)), "time": fill.get("time")}
        raise OandaError(f"close of trade {trade_id} returned no fill")

    def trade(self, specifier: str) -> dict[str, Any] | None:
        """A trade by OANDA ID or ``@client_id``; None if OANDA does not know it."""
        try:
            return self._get(f"{self._acct}/trades/{specifier}", "trade")["trade"]
        except OandaError as exc:
            if exc.status_code == 404:
                return None
            raise

    def trades_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 50):
            body = self._get(f"{self._acct}/trades", "trades", ids=",".join(ids[i:i + 50]), state="ALL", count=500)
            out.extend(body.get("trades", []))
        return out

    def open_trades(self) -> list[dict[str, Any]]:
        return self._get(f"{self._acct}/openTrades", "open_trades").get("trades", [])

    # ------------------------------------------------------------------ streaming
    def stream_transactions(self, on_transaction: Callable[[dict[str, Any]], None], stop: threading.Event) -> None:
        """Block, delivering each transaction (fills, SL/TP hits) as it happens. Returns when the stream drops."""
        with self._stream.stream("GET", f"{self._acct}/transactions/stream") as resp:
            if resp.status_code >= 400:
                raise OandaError(f"transaction stream HTTP {resp.status_code}", resp.status_code)
            log_event(log, logging.INFO, "fx.stream.connected", account=self.account_id)
            for line in resp.iter_lines():
                if stop.is_set():
                    return
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") != "HEARTBEAT":
                    on_transaction(msg)
