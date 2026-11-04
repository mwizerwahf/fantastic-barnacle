"""Non-bypassable risk controls.

``RiskManager`` is the only producer of ``ApprovedOrder``. It reads account,
positions, orders and the market clock straight from the broker, so callers
cannot feed it stale or fabricated state. Hard limits below are ceilings and
floors that configuration can tighten but never loosen.
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from core.broker import BaseBroker
from core.config import MARKET_TZ, Settings
from core.exceptions import RiskStateError, TradingBotError
from core.models import (
    AccountState,
    Action,
    ApprovedOrder,
    Clock,
    IndicatorSnapshot,
    MarketSession,
    OrderRequest,
    OrderSide,
    Signal,
    bind_approval_verifier,
)
from core.observability import get_correlation_id, log_event

log = logging.getLogger(__name__)
NY = ZoneInfo(MARKET_TZ)

# --- Hard limits (config may only make these stricter) ----------------------
HARD_MAX_RISK_PER_TRADE_PCT: Final[float] = 0.02
HARD_MAX_POSITION_NOTIONAL_PCT: Final[float] = 0.50
HARD_MIN_REWARD_RISK: Final[float] = 1.5
HARD_MIN_CONFIDENCE: Final[float] = 0.6
HARD_MIN_STOP_ATR_MULT: Final[float] = 0.5
HARD_MAX_DAILY_DRAWDOWN_PCT: Final[float] = 0.03
HARD_MIN_EOD_FLATTEN_MINUTES: Final[int] = 15
HARD_MAX_CONCURRENT_POSITIONS: Final[int] = 5
HARD_MAX_TRADES_PER_DAY: Final[int] = 20
HARD_MAX_APPROVAL_TTL_S: Final[float] = 60.0
PDT_WINDOW_SESSIONS: Final[int] = 5
_EPS: Final[float] = 1e-9

_SEAL: Final[object] = object()
bind_approval_verifier(lambda token: token is _SEAL)


class RejectionReason(StrEnum):
    NOT_AN_ENTRY = "not_an_entry"
    INVALID_SIGNAL = "invalid_signal"
    MARKET_CLOSED = "market_closed"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    EOD_WINDOW = "eod_window"
    OPENING_BLACKOUT = "opening_blackout"
    BROKER_UNAVAILABLE = "broker_unavailable"
    ACCOUNT_RESTRICTED = "account_restricted"
    SHORTING_DISABLED = "shorting_disabled"
    LOW_CONFIDENCE = "low_confidence"
    INVALID_STOP_SIDE = "invalid_stop_side"
    INVALID_TARGET_SIDE = "invalid_target_side"
    REWARD_RISK_TOO_LOW = "reward_risk_too_low"
    MISSING_ATR = "missing_atr"
    STOP_TOO_TIGHT = "stop_too_tight"
    DUPLICATE_SYMBOL = "duplicate_symbol"
    MAX_CONCURRENT_POSITIONS = "max_concurrent_positions"
    MAX_TRADES_PER_DAY = "max_trades_per_day"
    PDT_LIMIT = "pdt_limit"
    SIZE_BELOW_ONE = "size_below_one"
    INTERNAL_ERROR = "internal_error"


class GateStatus(StrEnum):
    OK = "ok"
    MARKET_CLOSED = "market_closed"
    KILL_SWITCH = "kill_switch"
    EOD_FLATTEN = "eod_flatten"
    OPENING_BLACKOUT = "opening_blackout"
    BROKER_UNAVAILABLE = "broker_unavailable"


@dataclass(frozen=True, slots=True)
class GateResult:
    status: GateStatus
    clock: Clock | None = None
    detail: str = ""

    @property
    def allows_entries(self) -> bool:
        return self.status is GateStatus.OK


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: ApprovedOrder | None
    reason: RejectionReason | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SizeResult:
    qty: int
    risk_dollars: float
    notional: float
    limited_by: str


# --- Limits -----------------------------------------------------------------
def _clamp_max(name: str, requested: float, hard: float) -> float:
    if requested > hard:
        log_event(log, logging.WARNING, "risk.config_clamped", param=name, requested=requested, applied=hard)
        return hard
    return requested


def _clamp_min(name: str, requested: float, hard: float) -> float:
    if requested < hard:
        log_event(log, logging.WARNING, "risk.config_clamped", param=name, requested=requested, applied=hard)
        return hard
    return requested


@dataclass(frozen=True, slots=True)
class RiskLimits:
    risk_per_trade_pct: float
    max_position_notional_pct: float
    min_reward_risk: float
    min_confidence: float
    min_stop_atr_mult: float
    max_daily_drawdown_pct: float
    eod_flatten_minutes: int
    max_concurrent_positions: int
    max_trades_per_day: int
    approval_ttl_s: float
    open_blackout_minutes: int
    allow_short: bool
    pdt_equity_threshold: float
    pdt_max_day_trades: int

    @classmethod
    def from_settings(cls, s: Settings) -> RiskLimits:
        return cls(
            risk_per_trade_pct=_clamp_max("risk_per_trade_pct", s.risk_per_trade_pct, HARD_MAX_RISK_PER_TRADE_PCT),
            max_position_notional_pct=_clamp_max("max_position_notional_pct", s.max_position_notional_pct,
                                                 HARD_MAX_POSITION_NOTIONAL_PCT),
            min_reward_risk=_clamp_min("min_reward_risk", s.min_reward_risk, HARD_MIN_REWARD_RISK),
            min_confidence=_clamp_min("min_confidence", s.min_confidence, HARD_MIN_CONFIDENCE),
            min_stop_atr_mult=_clamp_min("min_stop_atr_mult", s.min_stop_atr_mult, HARD_MIN_STOP_ATR_MULT),
            max_daily_drawdown_pct=_clamp_max("max_daily_drawdown_pct", s.max_daily_drawdown_pct,
                                              HARD_MAX_DAILY_DRAWDOWN_PCT),
            eod_flatten_minutes=int(_clamp_min("eod_flatten_minutes", s.eod_flatten_minutes,
                                               HARD_MIN_EOD_FLATTEN_MINUTES)),
            max_concurrent_positions=int(_clamp_max("max_concurrent_positions", s.max_concurrent_positions,
                                                    HARD_MAX_CONCURRENT_POSITIONS)),
            max_trades_per_day=int(_clamp_max("max_trades_per_day", s.max_trades_per_day, HARD_MAX_TRADES_PER_DAY)),
            approval_ttl_s=_clamp_max("approval_ttl_s", s.approval_ttl_s, HARD_MAX_APPROVAL_TTL_S),
            open_blackout_minutes=s.open_blackout_minutes,
            allow_short=s.allow_short,
            # PDT values are deliberately not hard-clamped: the regulation itself may change.
            pdt_equity_threshold=s.pdt_equity_threshold,
            pdt_max_day_trades=s.pdt_max_day_trades,
        )


# --- Pure helpers ---------------------------------------------------------------
def round_to_tick(price: float) -> float:
    """Alpaca/Reg NMS: 2 decimals at or above $1, 4 decimals below."""
    return round(price, 2 if price >= 1 else 4)


def compute_position_size(*, equity: float, buying_power: float, entry: float, stop: float,
                          risk_pct: float, max_notional_pct: float) -> SizeResult:
    per_share = abs(entry - stop)
    if equity <= 0 or entry <= 0 or per_share <= 0:
        return SizeResult(0, 0.0, 0.0, "invalid_inputs")
    budget = equity * risk_pct
    caps = {
        "risk": math.floor(budget / per_share + _EPS),
        "buying_power": math.floor(max(buying_power, 0.0) / entry + _EPS),
        "notional_cap": math.floor(equity * max_notional_pct / entry + _EPS),
    }
    limited_by = min(caps, key=lambda k: caps[k])
    qty = max(caps[limited_by], 0)
    return SizeResult(qty, round(qty * per_share, 2), round(qty * entry, 2), limited_by)


def in_eod_window(clock: Clock, minutes: int) -> bool:
    return clock.timestamp >= clock.next_close - timedelta(minutes=minutes)


# --- Persistent state ---------------------------------------------------------
class RiskStateStore:
    """Halt flag and entry ledger on disk. A restart cannot clear a halt."""

    RETENTION_DAYS: Final[int] = 30

    def __init__(self, path: Path) -> None:
        self._path = path
        self._halt: dict[str, str] | None = None
        self._entries: list[dict[str, str]] = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._halt = data.get("halt")
                self._entries = list(data.get("entries", []))
            except (OSError, ValueError, AttributeError) as exc:
                raise RiskStateError(f"cannot read risk state {path}: {exc}; inspect or remove it manually") from exc

    def is_halted(self, session: date) -> bool:
        return self._halt is not None and self._halt.get("session_date") == session.isoformat()

    def set_halt(self, session: date, reason: str, at: datetime) -> None:
        self._halt = {"session_date": session.isoformat(), "reason": reason, "at": at.isoformat()}
        self._save()

    def record_entry(self, session: date, symbol: str, client_order_id: str) -> None:
        cutoff = (session - timedelta(days=self.RETENTION_DAYS)).isoformat()
        self._entries = [e for e in self._entries if e["date"] >= cutoff]
        self._entries.append({"date": session.isoformat(), "symbol": symbol, "client_order_id": client_order_id})
        self._save()

    def entries_on(self, session: date) -> int:
        return sum(1 for e in self._entries if e["date"] == session.isoformat())

    def entries_within(self, sessions: set[date]) -> int:
        keys = {d.isoformat() for d in sessions}
        return sum(1 for e in self._entries if e["date"] in keys)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"halt": self._halt, "entries": self._entries}, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


# --- Risk manager ---------------------------------------------------------------
class _Reject(Exception):
    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class RiskManager:
    def __init__(self, settings: Settings, broker: BaseBroker, *, state_path: Path | None = None) -> None:
        self.limits = RiskLimits.from_settings(settings)
        self._broker = broker
        self._state = RiskStateStore(state_path or settings.state_dir / "risk_state.json")
        self._calendar: tuple[date, list[MarketSession]] | None = None
        log_event(log, logging.INFO, "risk.limits", **asdict(self.limits))

    # -- session helpers --
    def _sessions(self, today: date) -> list[MarketSession]:
        if self._calendar is None or self._calendar[0] != today:
            sessions = self._broker.get_calendar(today - timedelta(days=14), today)
            self._calendar = (today, sorted(sessions, key=lambda s: s.date))
        return self._calendar[1]

    def _today_session(self, clock: Clock) -> tuple[MarketSession, list[MarketSession]] | None:
        today = clock.timestamp.astimezone(NY).date()
        sessions = [s for s in self._sessions(today) if s.date <= today]
        if not sessions or sessions[-1].date != today:
            return None
        return sessions[-1], sessions[-PDT_WINDOW_SESSIONS:]

    def is_halted(self, session: date) -> bool:
        return self._state.is_halted(session)

    # -- forced actions --
    def force_flatten(self, reason: str, *, only_if_exposed: bool = False) -> None:
        if only_if_exposed:
            try:
                if not self._broker.get_positions() and not self._broker.get_open_orders():
                    return
            except TradingBotError:
                pass  # can't confirm we're flat, so flatten anyway
        log_event(log, logging.WARNING, "risk.force_flatten", reason=reason)
        for action in (self._broker.cancel_all_orders, self._broker.close_all_positions):
            try:
                action()
            except Exception as exc:
                log_event(log, logging.CRITICAL, "risk.flatten_failed", reason=reason, step=action.__name__,
                          error=repr(exc))

    def _drawdown_breached(self, account: AccountState) -> bool:
        start = account.last_equity
        return start > 0 and account.equity <= start * (1.0 - self.limits.max_daily_drawdown_pct) + _EPS

    def _trigger_halt(self, session: date, account: AccountState, now: datetime) -> None:
        drawdown = 1.0 - account.equity / account.last_equity
        self._state.set_halt(session, f"daily drawdown {drawdown:.2%}", now)
        log_event(log, logging.CRITICAL, "risk.kill_switch", session=session.isoformat(), equity=account.equity,
                  start_equity=account.last_equity, drawdown_pct=round(drawdown * 100, 3))
        self.force_flatten("kill_switch")

    # -- per-cycle gate (order: market open -> kill switch -> EOD window) --
    def pre_cycle(self) -> GateResult:
        try:
            clock = self._broker.get_clock()
            if not clock.is_open:
                return GateResult(GateStatus.MARKET_CLOSED, clock)
            found = self._today_session(clock)
            if found is None:
                return GateResult(GateStatus.MARKET_CLOSED, clock, "no calendar session for today")
            session, _ = found
            if self._state.is_halted(session.date):
                self.force_flatten("kill_switch_active", only_if_exposed=True)
                return GateResult(GateStatus.KILL_SWITCH, clock, "halted for session")
            account = self._broker.get_account()
            if self._drawdown_breached(account):
                self._trigger_halt(session.date, account, clock.timestamp)
                return GateResult(GateStatus.KILL_SWITCH, clock, "drawdown limit breached")
            if in_eod_window(clock, self.limits.eod_flatten_minutes):
                self.force_flatten("eod_window", only_if_exposed=True)
                return GateResult(GateStatus.EOD_FLATTEN, clock)
            if clock.timestamp < session.open + timedelta(minutes=self.limits.open_blackout_minutes):
                return GateResult(GateStatus.OPENING_BLACKOUT, clock)
            return GateResult(GateStatus.OK, clock)
        except TradingBotError as exc:
            log_event(log, logging.ERROR, "risk.gate_error", error=str(exc))
            return GateResult(GateStatus.BROKER_UNAVAILABLE, detail=str(exc))

    # -- order approval --
    def evaluate(self, signal: Signal, snapshot: IndicatorSnapshot) -> RiskDecision:
        try:
            approved = self._evaluate(signal, snapshot)
        except _Reject as rej:
            return self._rejected(signal, rej.reason, rej.detail)
        except TradingBotError as exc:
            return self._rejected(signal, RejectionReason.BROKER_UNAVAILABLE, str(exc))
        except Exception as exc:  # fail closed on anything unexpected
            log.exception("risk.internal_error")
            return self._rejected(signal, RejectionReason.INTERNAL_ERROR, repr(exc))
        log_event(log, logging.INFO, "risk.approved", **approved.model_dump(mode="json"))
        return RiskDecision(approved=approved)

    def _rejected(self, signal: Signal, reason: RejectionReason, detail: str) -> RiskDecision:
        log_event(log, logging.INFO, "risk.rejected", symbol=signal.symbol, action=signal.action.value,
                  reason=reason.value, detail=detail)
        return RiskDecision(approved=None, reason=reason, detail=detail)

    def _evaluate(self, signal: Signal, snapshot: IndicatorSnapshot) -> ApprovedOrder:
        lim = self.limits
        if signal.action is Action.HOLD:
            raise _Reject(RejectionReason.NOT_AN_ENTRY, "HOLD signals are never orders")
        if snapshot.symbol != signal.symbol:
            raise _Reject(RejectionReason.INVALID_SIGNAL, "snapshot/signal symbol mismatch")

        # Market state, re-checked for every order.
        clock = self._broker.get_clock()
        if not clock.is_open:
            raise _Reject(RejectionReason.MARKET_CLOSED, "market closed")
        found = self._today_session(clock)
        if found is None:
            raise _Reject(RejectionReason.MARKET_CLOSED, "no calendar session for today")
        session, pdt_window = found
        now = clock.timestamp
        if self._state.is_halted(session.date):
            raise _Reject(RejectionReason.KILL_SWITCH_ACTIVE, "trading halted for session")
        if in_eod_window(clock, lim.eod_flatten_minutes):
            raise _Reject(RejectionReason.EOD_WINDOW, f"within {lim.eod_flatten_minutes}m of close")
        if now < session.open + timedelta(minutes=lim.open_blackout_minutes):
            raise _Reject(RejectionReason.OPENING_BLACKOUT, f"first {lim.open_blackout_minutes}m after open")

        account = self._broker.get_account()
        if account.trading_blocked or account.equity <= 0 or account.last_equity <= 0:
            raise _Reject(RejectionReason.ACCOUNT_RESTRICTED, "account blocked or equity baseline invalid")
        if self._drawdown_breached(account):
            self._trigger_halt(session.date, account, now)
            raise _Reject(RejectionReason.KILL_SWITCH_ACTIVE, "drawdown limit breached")

        side = OrderSide.BUY if signal.action is Action.BUY else OrderSide.SELL
        if side is OrderSide.SELL and not (lim.allow_short and account.shorting_enabled):
            raise _Reject(RejectionReason.SHORTING_DISABLED, "short entries disabled")

        # Signal sanity.
        if signal.confidence < lim.min_confidence:
            raise _Reject(RejectionReason.LOW_CONFIDENCE, f"{signal.confidence:.2f} < {lim.min_confidence:.2f}")
        if not (signal.entry_price and signal.stop_loss and signal.take_profit):
            raise _Reject(RejectionReason.INVALID_SIGNAL, "entry/stop/target missing")
        req = OrderRequest(symbol=signal.symbol, side=side, entry_price=round_to_tick(signal.entry_price),
                           stop_loss=round_to_tick(signal.stop_loss), take_profit=round_to_tick(signal.take_profit),
                           confidence=signal.confidence)
        long = side is OrderSide.BUY
        if (req.stop_loss >= req.entry_price) if long else (req.stop_loss <= req.entry_price):
            raise _Reject(RejectionReason.INVALID_STOP_SIDE, f"stop {req.stop_loss} vs entry {req.entry_price}")
        if (req.take_profit <= req.entry_price) if long else (req.take_profit >= req.entry_price):
            raise _Reject(RejectionReason.INVALID_TARGET_SIDE, f"target {req.take_profit} vs entry {req.entry_price}")
        if req.reward_risk + _EPS < lim.min_reward_risk:
            raise _Reject(RejectionReason.REWARD_RISK_TOO_LOW, f"R:R {req.reward_risk:.2f} < {lim.min_reward_risk}")
        if snapshot.atr is None or snapshot.atr <= 0:
            raise _Reject(RejectionReason.MISSING_ATR, "ATR unavailable")
        if req.per_share_risk + _EPS < lim.min_stop_atr_mult * snapshot.atr:
            raise _Reject(RejectionReason.STOP_TOO_TIGHT,
                          f"stop distance {req.per_share_risk:.4f} < {lim.min_stop_atr_mult} x ATR {snapshot.atr:.4f}")

        # Exposure.
        positions = self._broker.get_positions()
        orders = self._broker.get_open_orders()
        exposed = {p.symbol for p in positions} | {o.symbol for o in orders}
        if req.symbol in exposed:
            raise _Reject(RejectionReason.DUPLICATE_SYMBOL, f"{req.symbol} already has a position or open order")
        if len(exposed) >= lim.max_concurrent_positions:
            raise _Reject(RejectionReason.MAX_CONCURRENT_POSITIONS, f"{len(exposed)} symbols exposed")
        trades_today = self._state.entries_on(session.date)
        if trades_today >= lim.max_trades_per_day:
            raise _Reject(RejectionReason.MAX_TRADES_PER_DAY, f"{trades_today} entries today")

        # PDT: every entry is a day trade because nothing is held overnight.
        if min(account.equity, account.last_equity) < lim.pdt_equity_threshold:
            used = max(account.daytrade_count, self._state.entries_within({s.date for s in pdt_window}))
            if account.pattern_day_trader or used + 1 > lim.pdt_max_day_trades:
                raise _Reject(RejectionReason.PDT_LIMIT,
                              f"{used} day trades in {PDT_WINDOW_SESSIONS} sessions, limit {lim.pdt_max_day_trades}")

        size = compute_position_size(equity=account.equity, buying_power=account.buying_power,
                                     entry=req.entry_price, stop=req.stop_loss,
                                     risk_pct=lim.risk_per_trade_pct, max_notional_pct=lim.max_position_notional_pct)
        if size.qty < 1:
            raise _Reject(RejectionReason.SIZE_BELOW_ONE, f"computed qty {size.qty} (limited by {size.limited_by})")

        cid = get_correlation_id()
        approved = ApprovedOrder(
            _seal=_SEAL,
            symbol=req.symbol, side=req.side, qty=size.qty, entry_price=req.entry_price,
            stop_loss=req.stop_loss, take_profit=req.take_profit, risk_dollars=size.risk_dollars,
            notional=size.notional, confidence=req.confidence, strategy=signal.strategy,
            client_order_id=f"tb-{cid}-{req.symbol}-{uuid.uuid4().hex[:8]}"[:48],
            correlation_id=cid, approved_at=now, expires_at=now + timedelta(seconds=lim.approval_ttl_s),
        )
        # Counted on approval (not fill) so the limit can never be under-counted.
        self._state.record_entry(session.date, req.symbol, approved.client_order_id)
        return approved
