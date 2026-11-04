"""Turns a strategy signal into a concrete, bounded OANDA order, or a rejection.

Hard limits are code constants: a strategy's settings can be stricter, never looser.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo

from fx.models import Direction, InstrumentSpec, Price, RiskConfig, Signal

HARD_MAX_RISK_PCT: Final[float] = 2.0          # loss at stop-loss, % of account balance, per trade
HARD_MAX_UNITS: Final[int] = 500_000           # 5 standard lots per trade
HARD_MAX_OPEN_TRADES: Final[int] = 10          # per strategy
HARD_MAX_SPREAD_PIPS: Final[float] = 10.0
HARD_MAX_SLIPPAGE_PIPS: Final[float] = 5.0

NY = ZoneInfo("America/New_York")
ENTRY_CUTOFF_FRI = (16, 30)   # no new trades after Friday 16:30 New York
WEEKEND_CLOSE_FRI = (16, 50)  # close_before_weekend strategies flatten from here
REOPEN_SUN = (17, 5)          # spreads are wild for the first minutes after the Sunday open


class Session(StrEnum):
    OPEN = "open"
    CLOSING = "closing"       # Friday afternoon: entries blocked
    WEEKEND_CLOSE = "weekend_close"
    CLOSED = "closed"


def fx_session(now: datetime) -> Session:
    t = now.astimezone(NY)
    hm = (t.hour, t.minute)
    wd = t.weekday()  # Monday 0 ... Sunday 6
    if wd == 5 or (wd == 6 and hm < REOPEN_SUN) or (wd == 4 and hm >= (17, 0)):
        return Session.CLOSED
    if wd == 4 and hm >= WEEKEND_CLOSE_FRI:
        return Session.WEEKEND_CLOSE
    if wd == 4 and hm >= ENTRY_CUTOFF_FRI:
        return Session.CLOSING
    return Session.OPEN


def trading_day_start(now: datetime) -> datetime:
    """Forex days roll at 17:00 New York; daily loss limits reset there."""
    t = now.astimezone(NY)
    start = t.replace(hour=17, minute=0, second=0, microsecond=0)
    if t < start:
        start -= timedelta(days=1)
    return start.astimezone(UTC)


class RejectReason(StrEnum):
    MARKET_CLOSED = "market_closed"
    NOT_TRADEABLE = "not_tradeable"
    SPREAD = "spread_too_wide"
    MAX_OPEN = "max_open_trades"
    HEDGING = "account_not_hedging"
    NO_CONVERSION = "no_currency_conversion"
    TOO_SMALL = "size_below_minimum"
    RISK = "risk_too_large"
    STOP_TOO_CLOSE = "stop_inside_spread"
    DAILY_LOSS = "daily_loss_limit"
    AI = "ai_rejected"
    LIVE_BLOCKED = "live_trading_disabled"


class Reject(Exception):
    def __init__(self, reason: RejectReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class TradePlan:
    direction: Direction
    units: float                 # signed: + long, - short
    entry_ref: float             # ask for longs, bid for shorts, at plan time
    sl_price: float
    tp_price: float | None
    price_bound: float
    risk_amount: float           # account currency lost if the stop is hit
    sl_pips: float
    tp_pips: float | None
    spread_pips: float


def _round_units(units: float, precision: int) -> float:
    factor = 10 ** precision
    return math.floor(units * factor) / factor


def plan_trade(rc: RiskConfig, signal: Signal, price: Price, spec: InstrumentSpec, *, balance: float,
               open_count: int, opposite_on_netting_account: bool) -> TradePlan:
    pip = spec.pip
    if not price.tradeable:
        raise Reject(RejectReason.NOT_TRADEABLE, f"{spec.name} is not tradeable right now")
    spread_pips = (price.ask - price.bid) / pip
    max_spread = min(rc.max_spread_pips, HARD_MAX_SPREAD_PIPS)
    if spread_pips > max_spread:
        raise Reject(RejectReason.SPREAD, f"spread {spread_pips:.1f} pips > {max_spread:g}")
    max_open = min(rc.max_open_trades, HARD_MAX_OPEN_TRADES)
    if open_count >= max_open:
        raise Reject(RejectReason.MAX_OPEN, f"{open_count} open, limit {max_open}")
    if opposite_on_netting_account:
        raise Reject(RejectReason.HEDGING, "account has hedging off and holds the opposite side of this pair")
    if price.conv_loss is None or price.conv_gain is None or balance <= 0:
        raise Reject(RejectReason.NO_CONVERSION, f"cannot value {spec.quote_currency} P/L in the account currency")

    direction = signal.direction
    entry = price.ask if direction is Direction.LONG else price.bid
    sl_pips_cfg = signal.sl_pips or rc.sl_pips

    risk_pct = min(rc.risk_pct, HARD_MAX_RISK_PCT)
    if rc.sizing == "risk_pct":
        if sl_pips_cfg is None:  # RiskConfig validation forbids this; never size blind
            raise Reject(RejectReason.RISK, "risk_pct sizing needs a pip stop-loss")
        units = balance * risk_pct / 100 / (sl_pips_cfg * pip * price.conv_loss)
    else:
        units = float(rc.units)
    units = _round_units(min(units, HARD_MAX_UNITS), spec.units_precision)
    if units < max(spec.min_units, 1 / 10 ** spec.units_precision):
        raise Reject(RejectReason.TOO_SMALL, f"{units:g} units is below the {spec.min_units:g} minimum")

    if sl_pips_cfg is not None:
        sl_dist = sl_pips_cfg * pip
    else:
        if rc.sl_amount is None:
            raise Reject(RejectReason.RISK, "no stop-loss configured")
        sl_dist = rc.sl_amount / (units * price.conv_loss)
    if sl_dist <= price.ask - price.bid:
        raise Reject(RejectReason.STOP_TOO_CLOSE, f"stop {sl_dist / pip:.1f} pips is inside the spread")

    risk_amount = units * sl_dist * price.conv_loss
    cap = balance * HARD_MAX_RISK_PCT / 100
    if risk_amount > cap * 1.0001:
        raise Reject(RejectReason.RISK, f"loss at stop {risk_amount:.2f} > hard cap {cap:.2f} "
                                        f"({HARD_MAX_RISK_PCT:g}% of balance); use fewer units or a tighter stop")

    tp_pips_cfg = signal.tp_pips or rc.tp_pips
    if tp_pips_cfg is not None:
        tp_dist: float | None = tp_pips_cfg * pip
    elif rc.tp_amount is not None:
        tp_dist = rc.tp_amount / (units * price.conv_gain)
    else:
        tp_dist = None

    s = direction.sign
    digits = spec.display_precision
    slip = min(rc.max_slippage_pips, HARD_MAX_SLIPPAGE_PIPS) * pip
    return TradePlan(
        direction=direction, units=units * s, entry_ref=entry,
        sl_price=round(entry - s * sl_dist, digits),
        tp_price=None if tp_dist is None else round(entry + s * tp_dist, digits),
        price_bound=round(entry + s * slip, digits), risk_amount=round(risk_amount, 2),
        sl_pips=round(sl_dist / pip, 1), tp_pips=None if tp_dist is None else round(tp_dist / pip, 1),
        spread_pips=round(spread_pips, 2))
