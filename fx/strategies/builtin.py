"""Starter strategy types. Copy one as a template for your own."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from fx.indicators import atr, donchian, ema, rsi
from fx.models import Direction, Signal
from fx.strategies.base import BarContext, FxStrategy, StrategyParams, register

Side = Literal["both", "long", "short"]


def _allowed(side: Side, direction: Direction) -> bool:
    return side == "both" or side == direction.value


def _atr_exits(ctx: BarContext, period: int, sl_mult: float | None, tp_mult: float | None) -> dict[str, float]:
    if not sl_mult and not tp_mult:
        return {}
    value = atr(ctx.highs, ctx.lows, ctx.closes, period)[-1]
    if value is None or value <= 0:
        return {}
    out = {}
    if sl_mult:
        out["sl_pips"] = round(ctx.to_pips(value * sl_mult), 1)
    if tp_mult:
        out["tp_pips"] = round(ctx.to_pips(value * tp_mult), 1)
    return out


class _AtrExitParams(StrategyParams):
    atr_period: int = Field(default=14, ge=2, le=200, description="ATR period for volatility-based exits")
    atr_sl_mult: float | None = Field(default=None, gt=0, le=20,
                                      description="If set, stop-loss = ATR x this (overrides the pip/amount stop)")
    atr_tp_mult: float | None = Field(default=None, gt=0, le=40,
                                      description="If set, take-profit = ATR x this (overrides the pip/amount target)")
    side: Side = Field(default="both", description="Trade long, short or both")


@register
class EmaCross(FxStrategy):
    kind = "ema_cross"
    title = "EMA crossover"
    description = "Buys when the fast EMA crosses above the slow EMA, sells on the opposite cross."

    class Params(_AtrExitParams):
        fast: int = Field(default=9, ge=2, le=200, description="Fast EMA length")
        slow: int = Field(default=21, ge=3, le=400, description="Slow EMA length")
        min_atr_pips: float = Field(default=0.0, ge=0, description="Skip quiet markets: minimum ATR in pips")

        @model_validator(mode="after")
        def _order(self) -> EmaCross.Params:
            if self.fast >= self.slow:
                raise ValueError("fast must be shorter than slow")
            return self

    params: Params

    @property
    def lookback(self) -> int:
        return self.params.slow * 4 + self.params.atr_period

    def on_bar(self, ctx: BarContext) -> Signal | None:
        p = self.params
        fast, slow = ema(ctx.closes, p.fast), ema(ctx.closes, p.slow)
        if None in (fast[-1], fast[-2], slow[-1], slow[-2]):
            return None
        if p.min_atr_pips:
            a = atr(ctx.highs, ctx.lows, ctx.closes, p.atr_period)[-1]
            if a is None or ctx.to_pips(a) < p.min_atr_pips:
                return None
        if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
            direction = Direction.LONG
        elif fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
            direction = Direction.SHORT
        else:
            return None
        if not _allowed(p.side, direction):
            return None
        return Signal(direction=direction, reason=f"EMA{p.fast} crossed {'above' if direction is Direction.LONG else 'below'} EMA{p.slow}",
                      **_atr_exits(ctx, p.atr_period, p.atr_sl_mult, p.atr_tp_mult))


@register
class Breakout(FxStrategy):
    kind = "breakout"
    title = "Range breakout"
    description = "Trades a close beyond the highest high / lowest low of the previous N candles."

    class Params(_AtrExitParams):
        channel: int = Field(default=20, ge=3, le=500, description="Look-back candles for the range")
        min_range_pips: float = Field(default=0.0, ge=0, description="Ignore ranges narrower than this")

    params: Params

    @property
    def lookback(self) -> int:
        return self.params.channel + self.params.atr_period * 3 + 2

    def on_bar(self, ctx: BarContext) -> Signal | None:
        p = self.params
        upper, lower = donchian(ctx.highs, ctx.lows, p.channel)
        hi, lo, close = upper[-1], lower[-1], ctx.closes[-1]
        if hi is None or lo is None:
            return None
        if p.min_range_pips and ctx.to_pips(hi - lo) < p.min_range_pips:
            return None
        prev_close = ctx.closes[-2]
        if close > hi and prev_close <= (upper[-2] or hi):
            direction = Direction.LONG
        elif close < lo and prev_close >= (lower[-2] or lo):
            direction = Direction.SHORT
        else:
            return None
        if not _allowed(p.side, direction):
            return None
        edge = hi if direction is Direction.LONG else lo
        return Signal(direction=direction, reason=f"close broke the {p.channel}-candle {'high' if direction is Direction.LONG else 'low'} {edge}",
                      **_atr_exits(ctx, p.atr_period, p.atr_sl_mult, p.atr_tp_mult))


@register
class RsiReversion(FxStrategy):
    kind = "rsi_reversion"
    title = "RSI mean reversion"
    description = "Buys when RSI climbs back above the oversold line, sells when it drops back below overbought."

    class Params(_AtrExitParams):
        period: int = Field(default=14, ge=2, le=100, description="RSI length")
        oversold: float = Field(default=30, gt=0, lt=50)
        overbought: float = Field(default=70, gt=50, lt=100)
        trend_ema: int | None = Field(default=None, ge=5, le=500,
                                      description="Optional: only trade in the direction of this EMA")

    params: Params

    @property
    def lookback(self) -> int:
        return max(self.params.period * 5, (self.params.trend_ema or 0) * 3) + self.params.atr_period

    def on_bar(self, ctx: BarContext) -> Signal | None:
        p = self.params
        r = rsi(ctx.closes, p.period)
        if r[-1] is None or r[-2] is None:
            return None
        if r[-2] < p.oversold <= r[-1]:
            direction = Direction.LONG
        elif r[-2] > p.overbought >= r[-1]:
            direction = Direction.SHORT
        else:
            return None
        if p.trend_ema:
            trend = ema(ctx.closes, p.trend_ema)[-1]
            if trend is None or (ctx.closes[-1] > trend) != (direction is Direction.LONG):
                return None
        if not _allowed(p.side, direction):
            return None
        return Signal(direction=direction, reason=f"RSI{p.period} {r[-2]:.1f} -> {r[-1]:.1f}",
                      **_atr_exits(ctx, p.atr_period, p.atr_sl_mult, p.atr_tp_mult))
