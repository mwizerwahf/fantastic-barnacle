"""Market data providers.

Official APIs first. ``PoliteFetcher`` is the only sanctioned path for web
scraping: it honours robots.txt and rate-limits per host.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, StockTradesRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from core.config import MARKET_TZ, Settings, missing_alpaca_keys_message
from core.exceptions import ConfigError, DataError, DataValidationError, StaleDataError, TradingBotError
from core.models import Bar, MarketSession, Quote
from core.observability import log_event
from core.resilience import call_external

T = TypeVar("T")
log = logging.getLogger(__name__)
NY = ZoneInfo(MARKET_TZ)

BAR_COLUMNS = ("open", "high", "low", "close", "volume")
STALE_MULTIPLIER = 2.0  # refuse bars older than 2x the bar interval
REGULAR_SESSION_MINUTES = 390  # only used to size lookback windows, never for market hours


class BaseDataProvider(ABC):
    @abstractmethod
    def get_bars(self, symbol: str, timeframe_minutes: int, lookback: int, *,
                 sessions: Sequence[MarketSession] | None = None) -> pd.DataFrame:
        """Return the last ``lookback`` *closed* bars, validated (see ``validate_bars``)."""

    @abstractmethod
    def get_latest_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def get_recent_trades(self, symbol: str, lookback: timedelta) -> pd.DataFrame:
        """Trade prints with columns ``price``, ``size`` and a tz-aware NY index."""


# --- Validation (pure) -----------------------------------------------------
def validate_bars(df: pd.DataFrame, *, symbol: str, timeframe_minutes: int,
                  now: datetime, sessions: Sequence[MarketSession] | None = None) -> pd.DataFrame:
    """Normalize and validate a bar frame. Raises on unusable data; never fills gaps."""
    if df.empty:
        raise DataValidationError(f"{symbol}: no bars returned")
    missing = [c for c in BAR_COLUMNS if c not in df.columns]
    if missing:
        raise DataValidationError(f"{symbol}: missing columns {missing}")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise DataValidationError(f"{symbol}: index must be a tz-aware DatetimeIndex")

    out = df.copy()
    out.index = out.index.tz_convert(NY)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    interval = pd.Timedelta(minutes=timeframe_minutes)

    # Drop the still-forming bar: a bar is closed once start + interval <= now.
    out = out[out.index + interval <= pd.Timestamp(now).tz_convert(NY)]
    if sessions:
        mask = pd.Series(False, index=out.index)
        for s in sessions:
            mask |= (out.index >= s.open) & (out.index < s.close)
        out = out[mask.to_numpy()]
    if out.empty:
        raise DataValidationError(f"{symbol}: no closed regular-session bars")

    if out[list(BAR_COLUMNS)].isna().any().any():
        raise DataValidationError(f"{symbol}: NaN values in OHLCV")
    bad = (out["high"] < out[["open", "close"]].max(axis=1)) | (out["low"] > out[["open", "close"]].min(axis=1))
    if bad.any() or (out["volume"] < 0).any() or (out["low"] <= 0).any():
        raise DataValidationError(f"{symbol}: {int(bad.sum())} inconsistent OHLC rows")
    last = out.iloc[-1]
    Bar(timestamp=out.index[-1].to_pydatetime(), **{c: float(last[c]) for c in BAR_COLUMNS})

    # Gaps: report, don't hide. Alpaca omits bars with no trades (common on IEX).
    session_key = out.index.normalize()
    deltas = out.index.to_series().groupby(session_key).diff()
    gaps = int((deltas > interval).sum())
    out.attrs["gaps"] = gaps
    if gaps:
        log_event(log, logging.WARNING, "data.gaps", symbol=symbol, gaps=gaps)

    last_close_time = out.index[-1] + interval
    age = pd.Timestamp(now).tz_convert(NY) - last_close_time
    if age > interval * STALE_MULTIPLIER:
        log_event(log, logging.WARNING, "data.stale", symbol=symbol, age_s=age.total_seconds())
        raise StaleDataError(f"{symbol}: last bar closed {age.total_seconds():.0f}s ago")
    return out


# --- Alpaca -----------------------------------------------------------------
class AlpacaDataProvider(BaseDataProvider):
    def __init__(self, settings: Settings, client: StockHistoricalDataClient | None = None,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        key = settings.alpaca_api_key.get_secret_value()
        secret = settings.alpaca_secret_key.get_secret_value()
        if client is None and not (key and secret):
            raise ConfigError(missing_alpaca_keys_message())
        self._client = client or StockHistoricalDataClient(key, secret)
        self._feed = DataFeed.SIP if settings.data_feed == "sip" else DataFeed.IEX
        self._timeout = settings.data_timeout_s
        self._attempts = settings.max_attempts
        self._backoff = settings.retry_backoff_s
        self._max_trades = settings.max_trades_per_fetch
        self._now = now_fn

    def _call(self, op: str, fn: Callable[[], T]) -> T:
        try:
            return call_external(fn, op=f"alpaca_data.{op}", timeout=self._timeout,
                                 attempts=self._attempts, backoff=self._backoff)
        except TradingBotError:
            raise
        except Exception as exc:
            raise DataError(f"alpaca_data.{op} failed: {exc!r}") from exc

    @staticmethod
    def _symbol_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if raw.empty:
            return raw
        if isinstance(raw.index, pd.MultiIndex):
            return raw.xs(symbol, level="symbol")
        return raw

    def get_bars(self, symbol: str, timeframe_minutes: int, lookback: int, *,
                 sessions: Sequence[MarketSession] | None = None) -> pd.DataFrame:
        now = self._now()
        days = math.ceil(lookback * timeframe_minutes / REGULAR_SESSION_MINUTES) + 4  # weekends/holidays
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
            start=now - timedelta(days=days),
            end=now,
            adjustment=Adjustment.RAW,
            feed=self._feed,
        )
        raw = self._symbol_frame(self._call("get_bars", lambda: self._client.get_stock_bars(req).df), symbol)
        raw = raw.rename(columns={"vwap": "bar_vwap"})
        df = validate_bars(raw, symbol=symbol, timeframe_minutes=timeframe_minutes, now=now, sessions=sessions)
        return df.tail(lookback)

    def get_latest_quote(self, symbol: str) -> Quote:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self._feed)
        q = self._call("get_latest_quote", lambda: self._client.get_stock_latest_quote(req))[symbol]
        return Quote(symbol=symbol, timestamp=q.timestamp, bid=float(q.bid_price or 0), ask=float(q.ask_price or 0),
                     bid_size=float(q.bid_size or 0), ask_size=float(q.ask_size or 0))

    def get_recent_trades(self, symbol: str, lookback: timedelta) -> pd.DataFrame:
        now = self._now()
        req = StockTradesRequest(symbol_or_symbols=symbol, start=now - lookback, end=now,
                                 limit=self._max_trades, feed=self._feed)
        raw = self._symbol_frame(self._call("get_trades", lambda: self._client.get_stock_trades(req).df), symbol)
        if raw.empty:
            return pd.DataFrame(columns=["price", "size"], index=pd.DatetimeIndex([], tz=NY))
        if len(raw) >= self._max_trades:
            # Pagination runs oldest -> newest, so truncation drops the *latest* prints.
            raise DataError(f"{symbol}: trade fetch truncated at {self._max_trades}; lower CVD_LOOKBACK_MINUTES")
        out = raw[["price", "size"]].astype(float)
        out.index = out.index.tz_convert(NY)
        # TODO(extend): filter trade condition codes (odd lots, out-of-sequence prints).
        return out.sort_index()


# --- Web scraping extension point ----------------------------------------------
class PoliteFetcher:
    """HTTP fetcher for supplementary sources (news, filings).

    Respects robots.txt and enforces a minimum interval between requests per
    host. You remain responsible for each site's Terms of Service.
    """

    def __init__(self, user_agent: str = "trading-bot-research/0.1", min_interval_s: float = 2.0,
                 timeout_s: float = 10.0) -> None:
        self._ua = user_agent
        self._min_interval = min_interval_s
        self._client = httpx.Client(timeout=timeout_s, headers={"User-Agent": user_agent}, follow_redirects=True)
        self._robots: dict[str, RobotFileParser] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = RobotFileParser()
            try:
                resp = self._client.get(f"{origin}/robots.txt")
                parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except httpx.HTTPError:
                parser.parse(["User-agent: *", "Disallow: /"])  # unknown robots policy: stay out
            self._robots[origin] = parser
        return parser.can_fetch(self._ua, url)

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last_hit.get(host, 0.0) + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def get_text(self, url: str) -> str:
        if not self._allowed(url):
            raise DataError(f"robots.txt disallows {url}")
        self._throttle(urlsplit(url).netloc)
        resp = self._client.get(url)
        resp.raise_for_status()
        # TODO(extend): parse into structured fields here; never forward raw HTML to the LLM.
        return resp.text
