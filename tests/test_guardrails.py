from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from core.broker import BaseBroker
from core.config import Settings
from core.exceptions import ApprovalForgeryError
from core.models import (
    AccountState,
    Action,
    ApprovedOrder,
    Clock,
    IndicatorSnapshot,
    MarketSession,
    OrderInfo,
    OrderSide,
    Position,
    PositionSide,
    Signal,
)
from risk.guardrails import (
    HARD_MAX_DAILY_DRAWDOWN_PCT,
    HARD_MAX_RISK_PER_TRADE_PCT,
    HARD_MIN_CONFIDENCE,
    HARD_MIN_EOD_FLATTEN_MINUTES,
    HARD_MIN_REWARD_RISK,
    GateStatus,
    RejectionReason,
    RiskManager,
    compute_position_size,
)

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 4)


def at(hour: int, minute: int, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)


class FakeBroker(BaseBroker):
    def __init__(self, *, equity: float = 10_000, last_equity: float = 10_000, buying_power: float = 40_000,
                 daytrade_count: int = 0, now: datetime | None = None, close: datetime | None = None,
                 positions: list[Position] | None = None, orders: list[OrderInfo] | None = None,
                 shorting_enabled: bool = True) -> None:
        self.equity, self.last_equity, self.buying_power = equity, last_equity, buying_power
        self.daytrade_count = daytrade_count
        self.now = now or at(11, 0)
        self.close = close or at(16, 0)
        self.positions = positions or []
        self.orders = orders or []
        self.shorting_enabled = shorting_enabled
        self.calls: list[str] = []

    def get_account(self) -> AccountState:
        return AccountState(equity=self.equity, last_equity=self.last_equity, buying_power=self.buying_power,
                            cash=self.equity, daytrade_count=self.daytrade_count,
                            shorting_enabled=self.shorting_enabled)

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def get_open_orders(self) -> list[OrderInfo]:
        return list(self.orders)

    def get_clock(self) -> Clock:
        return Clock(timestamp=self.now, is_open=True, next_open=at(9, 30, TODAY + timedelta(days=1)),
                     next_close=self.close)

    def get_calendar(self, start: date, end: date) -> list[MarketSession]:
        return [
            MarketSession(date=d.date(), open=at(9, 30, d.date()),
                          close=self.close if d.date() == TODAY else at(16, 0, d.date()))
            for d in pd.bdate_range(start, end)
        ]

    def submit_bracket_order(self, approved: ApprovedOrder) -> OrderInfo:
        self.calls.append("submit")
        return OrderInfo(id="1", client_order_id=approved.client_order_id, symbol=approved.symbol,
                         side=approved.side.value, qty=approved.qty, status="new")

    def cancel_all_orders(self) -> None:
        self.calls.append("cancel_all_orders")

    def close_all_positions(self) -> None:
        self.calls.append("close_all_positions")


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(_env_file=None, state_dir=tmp_path, **overrides)  # type: ignore[arg-type]


def make_signal(action: Action = Action.BUY, entry: float = 50.0, stop: float = 49.0, target: float = 52.0,
                confidence: float = 0.8, symbol: str = "SPY") -> Signal:
    return Signal(symbol=symbol, action=action, strategy="test", confidence=confidence, entry_price=entry,
                  stop_loss=stop, take_profit=target, created_at=at(11, 0))


def make_snapshot(symbol: str = "SPY", atr: float | None = 1.0, close: float = 50.0) -> IndicatorSnapshot:
    return IndicatorSnapshot(symbol=symbol, timestamp=at(10, 55), close=close, vwap=close - 0.5, rsi=60.0,
                             atr=atr, cvd=1000.0, cvd_change=100.0, cvd_source="trades", bars_in_session=17)


def a_position(symbol: str = "QQQ") -> Position:
    return Position(symbol=symbol, side=PositionSide.LONG, qty=10, avg_entry_price=100.0, market_value=1000.0,
                    unrealized_pl=0.0)


# --- Sizing ---------------------------------------------------------------------
def test_sizing_is_risk_based() -> None:
    size = compute_position_size(equity=10_000, buying_power=1e9, entry=50, stop=49, risk_pct=0.01,
                                 max_notional_pct=0.5)
    assert (size.qty, size.risk_dollars, size.limited_by) == (100, 100.0, "risk")


def test_sizing_capped_by_notional_and_buying_power() -> None:
    by_notional = compute_position_size(equity=10_000, buying_power=1e9, entry=50, stop=49, risk_pct=0.01,
                                        max_notional_pct=0.25)
    assert (by_notional.qty, by_notional.limited_by) == (50, "notional_cap")
    by_bp = compute_position_size(equity=10_000, buying_power=1_000, entry=50, stop=49, risk_pct=0.01,
                                  max_notional_pct=0.5)
    assert (by_bp.qty, by_bp.limited_by) == (20, "buying_power")


def test_sizing_floors_shares() -> None:
    size = compute_position_size(equity=10_000, buying_power=1e9, entry=50, stop=49.7, risk_pct=0.01,
                                 max_notional_pct=0.5)
    assert size.qty == 100  # risk allows 333, notional cap 5000/50 = 100
    size = compute_position_size(equity=1_000, buying_power=1e9, entry=50, stop=47, risk_pct=0.01,
                                 max_notional_pct=0.5)
    assert size.qty == 3  # floor(10 / 3)


def test_evaluate_approves_and_sizes(tmp_path: Path) -> None:
    broker = FakeBroker()
    rm = RiskManager(make_settings(tmp_path), broker)
    decision = rm.evaluate(make_signal(), make_snapshot())
    assert decision.approved is not None, decision
    order = decision.approved
    assert (order.qty, order.side, order.stop_loss, order.take_profit) == (50, OrderSide.BUY, 49.0, 52.0)
    assert order.risk_dollars == 50.0


def test_size_below_one_is_rejected(tmp_path: Path) -> None:
    broker = FakeBroker(equity=500, last_equity=500)
    rm = RiskManager(make_settings(tmp_path), broker)
    decision = rm.evaluate(make_signal(entry=200, stop=190, target=215), make_snapshot(atr=5.0, close=200))
    assert decision.reason is RejectionReason.SIZE_BELOW_ONE


# --- Hard-cap clamp ---------------------------------------------------------------
def test_config_cannot_loosen_hard_limits(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = make_settings(tmp_path, risk_per_trade_pct=0.05, min_reward_risk=1.0, min_confidence=0.3,
                             max_daily_drawdown_pct=0.10, eod_flatten_minutes=5)
    with caplog.at_level(logging.WARNING):
        limits = RiskManager(settings, FakeBroker()).limits
    assert limits.risk_per_trade_pct == HARD_MAX_RISK_PER_TRADE_PCT
    assert limits.min_reward_risk == HARD_MIN_REWARD_RISK
    assert limits.min_confidence == HARD_MIN_CONFIDENCE
    assert limits.max_daily_drawdown_pct == HARD_MAX_DAILY_DRAWDOWN_PCT
    assert limits.eod_flatten_minutes == HARD_MIN_EOD_FLATTEN_MINUTES
    assert sum(r.getMessage() == "risk.config_clamped" for r in caplog.records) == 5


def test_config_can_tighten_limits(tmp_path: Path) -> None:
    limits = RiskManager(make_settings(tmp_path, risk_per_trade_pct=0.005, min_confidence=0.8),
                         FakeBroker()).limits
    assert (limits.risk_per_trade_pct, limits.min_confidence) == (0.005, 0.8)


# --- Drawdown kill-switch -------------------------------------------------------------
def test_drawdown_halts_flattens_and_persists(tmp_path: Path) -> None:
    broker = FakeBroker(equity=9_690, last_equity=10_000, positions=[a_position()])
    rm = RiskManager(make_settings(tmp_path), broker)
    assert rm.pre_cycle().status is GateStatus.KILL_SWITCH
    assert broker.calls == ["cancel_all_orders", "close_all_positions"]

    # Restart with recovered equity: the halt survives on disk.
    recovered = FakeBroker(equity=10_100, last_equity=10_000)
    restarted = RiskManager(make_settings(tmp_path), recovered)
    assert restarted.pre_cycle().status is GateStatus.KILL_SWITCH
    assert restarted.evaluate(make_signal(), make_snapshot()).reason is RejectionReason.KILL_SWITCH_ACTIVE


def test_drawdown_below_limit_does_not_halt(tmp_path: Path) -> None:
    broker = FakeBroker(equity=9_710, last_equity=10_000)
    assert RiskManager(make_settings(tmp_path), broker).pre_cycle().status is GateStatus.OK
    assert broker.calls == []


# --- EOD window ---------------------------------------------------------------------
def test_eod_window_flattens_and_blocks_entries(tmp_path: Path) -> None:
    broker = FakeBroker(now=at(15, 50), positions=[a_position()])
    rm = RiskManager(make_settings(tmp_path), broker)
    assert rm.pre_cycle().status is GateStatus.EOD_FLATTEN
    assert broker.calls == ["cancel_all_orders", "close_all_positions"]
    assert rm.evaluate(make_signal(), make_snapshot()).reason is RejectionReason.EOD_WINDOW


def test_eod_window_follows_broker_early_close(tmp_path: Path) -> None:
    early = at(13, 0)
    assert RiskManager(make_settings(tmp_path), FakeBroker(now=at(12, 46), close=early)).pre_cycle().status \
        is GateStatus.EOD_FLATTEN
    assert RiskManager(make_settings(tmp_path), FakeBroker(now=at(12, 44), close=early)).pre_cycle().status \
        is GateStatus.OK


def test_opening_blackout(tmp_path: Path) -> None:
    rm = RiskManager(make_settings(tmp_path), FakeBroker(now=at(9, 33)))
    assert rm.pre_cycle().status is GateStatus.OPENING_BLACKOUT
    assert rm.evaluate(make_signal(), make_snapshot()).reason is RejectionReason.OPENING_BLACKOUT


# --- Signal sanity --------------------------------------------------------------------
def test_wrong_side_stop_rejected_for_long(tmp_path: Path) -> None:
    rm = RiskManager(make_settings(tmp_path), FakeBroker())
    decision = rm.evaluate(make_signal(stop=51.0, target=53.0), make_snapshot())
    assert decision.reason is RejectionReason.INVALID_STOP_SIDE


def test_wrong_side_stop_rejected_for_short(tmp_path: Path) -> None:
    rm = RiskManager(make_settings(tmp_path, allow_short=True), FakeBroker())
    decision = rm.evaluate(make_signal(action=Action.SELL, stop=49.0, target=47.0), make_snapshot())
    assert decision.reason is RejectionReason.INVALID_STOP_SIDE


@pytest.mark.parametrize(
    ("kwargs", "atr", "reason"),
    [
        ({"confidence": 0.5}, 1.0, RejectionReason.LOW_CONFIDENCE),
        ({"target": 51.0}, 1.0, RejectionReason.REWARD_RISK_TOO_LOW),
        ({"stop": 49.8, "target": 51.0}, 1.0, RejectionReason.STOP_TOO_TIGHT),
        ({}, None, RejectionReason.MISSING_ATR),
        ({"action": Action.SELL, "stop": 51.0, "target": 47.0}, 1.0, RejectionReason.SHORTING_DISABLED),
    ],
)
def test_signal_sanity_rejections(tmp_path: Path, kwargs: dict, atr: float | None,
                                  reason: RejectionReason) -> None:
    rm = RiskManager(make_settings(tmp_path), FakeBroker())
    assert rm.evaluate(make_signal(**kwargs), make_snapshot(atr=atr)).reason is reason


# --- Exposure --------------------------------------------------------------------
def test_exposure_limits(tmp_path: Path) -> None:
    rm = RiskManager(make_settings(tmp_path), FakeBroker(positions=[a_position("SPY")]))
    assert rm.evaluate(make_signal(), make_snapshot()).reason is RejectionReason.DUPLICATE_SYMBOL
    rm = RiskManager(make_settings(tmp_path), FakeBroker(positions=[a_position("QQQ"), a_position("IWM")]))
    assert rm.evaluate(make_signal(), make_snapshot()).reason is RejectionReason.MAX_CONCURRENT_POSITIONS


# --- PDT -----------------------------------------------------------------------------
def test_pdt_blocks_small_account_at_limit(tmp_path: Path) -> None:
    rm = RiskManager(make_settings(tmp_path), FakeBroker(equity=5_000, last_equity=5_000, daytrade_count=3))
    assert rm.evaluate(make_signal(), make_snapshot()).reason is RejectionReason.PDT_LIMIT


def test_pdt_not_applied_above_threshold(tmp_path: Path) -> None:
    rm = RiskManager(make_settings(tmp_path), FakeBroker(equity=30_000, last_equity=30_000, daytrade_count=3))
    decision = rm.evaluate(make_signal(), make_snapshot())
    assert decision.approved is not None and decision.approved.qty == 150


def test_pdt_counts_local_entries(tmp_path: Path) -> None:
    rm = RiskManager(make_settings(tmp_path, pdt_max_day_trades=1),
                     FakeBroker(equity=5_000, last_equity=5_000))
    assert rm.evaluate(make_signal(symbol="SPY"), make_snapshot("SPY")).approved is not None
    second = rm.evaluate(make_signal(symbol="QQQ"), make_snapshot("QQQ"))
    assert second.reason is RejectionReason.PDT_LIMIT


# --- Sealed approvals ----------------------------------------------------------------
def test_approved_order_cannot_be_forged() -> None:
    fields = dict(symbol="SPY", side=OrderSide.BUY, qty=1, entry_price=50, stop_loss=49, take_profit=52,
                  risk_dollars=1, notional=50, confidence=0.9, strategy="x", client_order_id="x",
                  correlation_id="x", approved_at=at(11, 0), expires_at=at(11, 1))
    with pytest.raises(ApprovalForgeryError):
        ApprovedOrder(**fields)
    with pytest.raises(ApprovalForgeryError):
        ApprovedOrder(_seal=object(), **fields)
    with pytest.raises(ApprovalForgeryError):
        ApprovedOrder.model_construct(**fields)
    with pytest.raises(ApprovalForgeryError):
        ApprovedOrder.model_validate(fields)
