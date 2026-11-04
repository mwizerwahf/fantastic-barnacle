"""VWAP / RSI / CVD momentum with an AI confirmation step."""

from __future__ import annotations

import math
from typing import Any, ClassVar

import pandas as pd

from core.models import AccountState, Action, IndicatorSnapshot, Position, Quote, Signal
from data.indicators import RSI_PERIOD
from strategies.base import BaseStrategy, register_strategy

SESSION_MINUTES = 390  # sizing the lookback window only
MAX_ENTRY_SPREAD_PCT = 0.005


def _price_decimals(price: float) -> int:
    return 2 if price >= 1 else 4


def _r(value: Any, digits: int) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


@register_strategy
class IntradayMomentumStrategy(BaseStrategy):
    name: ClassVar[str] = "intraday_momentum"

    # TODO(extend): move thresholds into Settings if you want to tune them per deployment.
    LONG_RSI: ClassVar[tuple[float, float]] = (55.0, 75.0)
    SHORT_RSI: ClassVar[tuple[float, float]] = (25.0, 45.0)

    @property
    def required_lookback(self) -> int:
        session_bars = math.ceil(SESSION_MINUTES / self.settings.timeframe_minutes)
        return session_bars + max(self.settings.ai_context_bars, 3 * RSI_PERIOD)

    def bias(self, s: IndicatorSnapshot) -> Action | None:
        if s.vwap is None or s.rsi is None or s.atr is None or s.cvd is None or s.atr <= 0:
            return None
        if s.close > s.vwap and self.LONG_RSI[0] <= s.rsi <= self.LONG_RSI[1] and s.cvd > 0:
            return Action.BUY
        if s.close < s.vwap and self.SHORT_RSI[0] <= s.rsi <= self.SHORT_RSI[1] and s.cvd < 0:
            return Action.SELL
        return None

    def prefilter(self, snapshot: IndicatorSnapshot) -> bool:
        return self.bias(snapshot) is not None

    def build_context(self, bars: pd.DataFrame, snapshot: IndicatorSnapshot, account: AccountState,
                      position: Position | None = None) -> dict[str, Any]:
        px = _price_decimals(snapshot.close)
        tail = bars.tail(self.settings.ai_context_bars)
        rows = [
            [ts.strftime("%H:%M"), _r(r.open, px), _r(r.high, px), _r(r.low, px), _r(r.close, px),
             int(r.volume), _r(r.vwap, px), _r(r.rsi, 1), _r(r.atr, px + 1), _r(r.cvd, 0)]
            for ts, r in zip(tail.index, tail.itertuples(index=False))
        ]
        bias = self.bias(snapshot)
        return {
            "symbol": snapshot.symbol,
            "timeframe_minutes": self.settings.timeframe_minutes,
            "as_of": snapshot.timestamp.isoformat(),
            "bias": "long" if bias is Action.BUY else "short" if bias is Action.SELL else None,
            "columns": ["time", "o", "h", "l", "c", "v", "vwap", "rsi", "atr", "cvd"],
            "bars": rows,
            "latest": {
                "price": _r(snapshot.close, px), "vwap": _r(snapshot.vwap, px), "rsi": _r(snapshot.rsi, 1),
                "atr": _r(snapshot.atr, px + 1), "cvd": _r(snapshot.cvd, 0), "cvd_change": _r(snapshot.cvd_change, 0),
            },
            "position": None if position is None else {
                "side": position.side.value, "qty": position.qty, "avg_entry_price": position.avg_entry_price,
            },
            "account": {"equity": round(account.equity, 2), "buying_power": round(account.buying_power, 2)},
        }

    def _entry_price(self, snapshot: IndicatorSnapshot, quote: Quote | None, action: Action) -> float:
        if quote is not None and quote.mid and (quote.ask - quote.bid) / quote.mid <= MAX_ENTRY_SPREAD_PCT:
            return quote.ask if action is Action.BUY else quote.bid
        return snapshot.close

    def decide(self, bars: pd.DataFrame, snapshot: IndicatorSnapshot, account: AccountState,
               position: Position | None, quote: Quote | None) -> Signal:
        now = snapshot.timestamp
        bias = self.bias(snapshot)
        if bias is None:
            return Signal.hold(snapshot.symbol, self.name, "prefilter: no setup", now)
        if position is not None:
            # TODO(extend): signal-driven exits; today exits are bracket legs + EOD flatten.
            return Signal.hold(snapshot.symbol, self.name, "position already open", now)

        decision = self.ai.decide(self.build_context(bars, snapshot, account, position))
        if decision.action is Action.HOLD:
            return Signal.hold(snapshot.symbol, self.name, decision.reasoning, now)
        if decision.action is not bias:
            return Signal.hold(snapshot.symbol, self.name, f"AI {decision.action} contradicts bias {bias}", now)
        return Signal(
            symbol=snapshot.symbol,
            action=decision.action,
            strategy=self.name,
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            entry_price=self._entry_price(snapshot, quote, decision.action),
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            created_at=now,
        )
