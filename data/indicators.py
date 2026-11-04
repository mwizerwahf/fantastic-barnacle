"""Pure, vectorized indicator functions (no I/O).

Order-flow note: Alpaca's standard feeds do not expose Level 2 / true order
flow. ``cumulative_volume_delta`` is a *proxy*: each trade print is classified
as buyer- or seller-initiated by the quote-midpoint rule (when quotes are
supplied) or the tick rule, and signed volume is accumulated. On the IEX feed
it reflects only IEX prints, a small slice of consolidated volume.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.config import MARKET_TZ
from core.models import IndicatorSnapshot

RSI_PERIOD = 14
ATR_PERIOD = 14
CVD_CHANGE_BARS = 5


def _session_key(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return index.tz_convert(MARKET_TZ).normalize()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP from typical price, reset at each New York session date."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    key = _session_key(df.index)
    cum_pv = (typical * df["volume"]).groupby(key).cumsum()
    cum_v = df["volume"].groupby(key).cumsum()
    return (cum_pv / cum_v.where(cum_v > 0)).rename("vwap")


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI with Wilder smoothing (EMA, alpha = 1/period)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    out = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out.where(avg_gain.notna()).rename("rsi")


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Average True Range with Wilder smoothing."""
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean().rename("atr")


def classify_trades(trades: pd.DataFrame, quotes: pd.DataFrame | None = None) -> pd.Series:
    """+1 buyer-initiated, -1 seller-initiated, 0 unknown.

    Quote-midpoint rule when ``quotes`` (columns ``bid``, ``ask``) are given,
    falling back to the tick rule for at-mid prints or missing quotes.
    """
    price = trades["price"]
    tick = np.sign(price.diff()).replace(0.0, np.nan).ffill().fillna(0.0)
    if quotes is None or quotes.empty:
        return tick.astype(int)
    merged = pd.merge_asof(trades.sort_index(), quotes[["bid", "ask"]].sort_index(),
                           left_index=True, right_index=True, direction="backward")
    valid = (merged["bid"] > 0) & (merged["ask"] >= merged["bid"])
    mid_sign = np.sign(merged["price"] - (merged["bid"] + merged["ask"]) / 2.0).where(valid)
    return mid_sign.where(mid_sign != 0).fillna(tick).astype(int)


def cumulative_volume_delta(trades: pd.DataFrame, bar_index: pd.DatetimeIndex, interval_minutes: int,
                            quotes: pd.DataFrame | None = None) -> pd.Series:
    """CVD aligned to bar start times. NaN before the first trade in the window."""
    if trades.empty:
        return pd.Series(np.nan, index=bar_index, name="cvd")
    trades = trades.copy()
    trades.index = trades.index.tz_convert(MARKET_TZ)
    signed = classify_trades(trades, quotes) * trades["size"]
    per_bar = signed.groupby(signed.index.floor(f"{interval_minutes}min")).sum()
    delta = per_bar.reindex(bar_index)
    in_window = bar_index >= per_bar.index.min()
    delta = delta.where(~in_window, delta.fillna(0.0))
    return delta.cumsum().rename("cvd")


def bar_volume_delta_proxy(df: pd.DataFrame) -> pd.Series:
    """Cruder CVD proxy from bars alone: sign(close - open) * volume, per session."""
    signed = np.sign(df["close"] - df["open"]) * df["volume"]
    return signed.groupby(_session_key(df.index)).cumsum().rename("cvd")


def add_indicators(df: pd.DataFrame, *, interval_minutes: int, trades: pd.DataFrame | None = None,
                   quotes: pd.DataFrame | None = None) -> pd.DataFrame:
    out = df.copy()
    out["vwap"] = session_vwap(out)
    out["rsi"] = rsi(out["close"])
    out["atr"] = atr(out)
    if trades is not None:
        out["cvd"] = cumulative_volume_delta(trades, out.index, interval_minutes, quotes)
        out.attrs["cvd_source"] = "trades"
    else:
        out["cvd"] = bar_volume_delta_proxy(out)
        out.attrs["cvd_source"] = "bars"
    return out


def compute_all(df: pd.DataFrame, *, symbol: str, interval_minutes: int = 5) -> IndicatorSnapshot:
    """Snapshot of the latest bar. Accepts raw bars (uses bar-proxy CVD) or ``add_indicators`` output."""
    if not {"vwap", "rsi", "atr", "cvd"}.issubset(df.columns):
        df = add_indicators(df, interval_minutes=interval_minutes)
    last = df.iloc[-1]
    cvd = df["cvd"]
    cvd_change = float(cvd.iloc[-1] - cvd.iloc[-1 - CVD_CHANGE_BARS]) if len(cvd) > CVD_CHANGE_BARS else np.nan
    session = _session_key(df.index)
    return IndicatorSnapshot(
        symbol=symbol,
        timestamp=df.index[-1].to_pydatetime(),
        close=float(last["close"]),
        vwap=float(last["vwap"]),
        rsi=float(last["rsi"]),
        atr=float(last["atr"]),
        cvd=float(last["cvd"]),
        cvd_change=cvd_change,
        cvd_source=str(df.attrs.get("cvd_source", "bars")),
        bars_in_session=int((session == session[-1]).sum()),
    )
