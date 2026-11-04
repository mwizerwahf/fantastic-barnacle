"""Shared pydantic contracts passed between layers."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from core.exceptions import ApprovalForgeryError


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


# --- Market data -------------------------------------------------------------
class Bar(_Frozen):
    timestamp: AwareDatetime  # bar *start* time
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Bar:
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError("inconsistent OHLC values")
        return self


class Quote(_Frozen):
    symbol: str
    timestamp: AwareDatetime
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    bid_size: float = Field(default=0, ge=0)
    ask_size: float = Field(default=0, ge=0)

    @property
    def is_valid(self) -> bool:
        return self.bid > 0 and self.ask >= self.bid

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2 if self.is_valid else None


class IndicatorSnapshot(_Frozen):
    symbol: str
    timestamp: AwareDatetime
    close: float
    vwap: float | None
    rsi: float | None
    atr: float | None
    cvd: float | None
    cvd_change: float | None
    cvd_source: str
    bars_in_session: int

    @model_validator(mode="before")
    @classmethod
    def _nan_to_none(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in data.items()}
        return data


# --- Broker state ---------------------------------------------------------------
class Clock(_Frozen):
    timestamp: AwareDatetime
    is_open: bool
    next_open: AwareDatetime
    next_close: AwareDatetime


class MarketSession(_Frozen):
    date: date
    open: AwareDatetime
    close: AwareDatetime


class AccountState(_Frozen):
    equity: float
    last_equity: float  # equity at previous session close (day's starting equity)
    buying_power: float
    cash: float
    daytrade_count: int = 0
    pattern_day_trader: bool = False
    trading_blocked: bool = False
    shorting_enabled: bool = False


class Position(_Frozen):
    symbol: str
    side: PositionSide
    qty: float = Field(gt=0)
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


class OrderInfo(_Frozen):
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float | None
    status: str
    order_class: str | None = None
    submitted_at: datetime | None = None


# --- Decisions --------------------------------------------------------------
class AIDecision(_Frozen):
    """The exact schema every LLM response must satisfy."""

    action: Action
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)
    stop_loss: float = Field(ge=0.0)
    take_profit: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _levels_required_for_entries(self) -> AIDecision:
        if self.action is not Action.HOLD and (self.stop_loss <= 0 or self.take_profit <= 0):
            raise ValueError("BUY/SELL require positive stop_loss and take_profit")
        return self

    @classmethod
    def hold(cls, reason: str) -> AIDecision:
        return cls(action=Action.HOLD, confidence=0.0, reasoning=reason[:500], stop_loss=0.0, take_profit=0.0)


class Signal(_Frozen):
    """Strategy output. A *recommendation*; only the risk layer can turn it into an order."""

    symbol: str
    action: Action
    strategy: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _entries_have_levels(self) -> Signal:
        if self.action is not Action.HOLD:
            for name in ("entry_price", "stop_loss", "take_profit"):
                value = getattr(self, name)
                if value is None or value <= 0:
                    raise ValueError(f"{self.action} signal requires positive {name}")
        return self

    @classmethod
    def hold(cls, symbol: str, strategy: str, reason: str, created_at: datetime) -> Signal:
        return cls(symbol=symbol, action=Action.HOLD, strategy=strategy, confidence=0.0,
                   reasoning=reason, created_at=created_at)


class OrderRequest(_Frozen):
    """Unsized, tick-rounded trade intent derived from a Signal inside the risk layer."""

    symbol: str
    side: OrderSide
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    confidence: float

    @property
    def per_share_risk(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_risk(self) -> float:
        risk = self.per_share_risk
        return abs(self.take_profit - self.entry_price) / risk if risk > 0 else 0.0


# --- Sealed approval ----------------------------------------------------------
# ``risk.guardrails`` binds a verifier for its private seal exactly once at
# import time. Without that seal, ApprovedOrder cannot be built, copied or
# validated from raw data. (Python has no true privacy; this blocks every
# ordinary construction path, so a bypass has to be deliberate.)
_approval_verifier: Callable[[object], bool] | None = None


def bind_approval_verifier(verifier: Callable[[object], bool]) -> None:
    global _approval_verifier
    if _approval_verifier is not None:
        raise ApprovalForgeryError("approval verifier is already bound")
    _approval_verifier = verifier


class ApprovedOrder(_Frozen):
    symbol: str
    side: OrderSide
    qty: int = Field(ge=1)
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    risk_dollars: float = Field(gt=0)
    notional: float = Field(gt=0)
    confidence: float
    strategy: str
    client_order_id: str
    correlation_id: str
    approved_at: AwareDatetime
    expires_at: AwareDatetime

    def __init__(self, *, _seal: object = None, **data: Any) -> None:
        verifier = _approval_verifier
        if verifier is None or not verifier(_seal):
            raise ApprovalForgeryError("ApprovedOrder can only be created by risk.guardrails.RiskManager")
        super().__init__(**data)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    @classmethod
    def model_construct(cls, *args: Any, **kwargs: Any) -> ApprovedOrder:
        raise ApprovalForgeryError("model_construct is disabled for ApprovedOrder")

    @classmethod
    def model_validate(cls, *args: Any, **kwargs: Any) -> ApprovedOrder:
        raise ApprovalForgeryError("model_validate is disabled for ApprovedOrder")

    @classmethod
    def model_validate_json(cls, *args: Any, **kwargs: Any) -> ApprovedOrder:
        raise ApprovalForgeryError("model_validate_json is disabled for ApprovedOrder")

    @classmethod
    def model_validate_strings(cls, *args: Any, **kwargs: Any) -> ApprovedOrder:
        raise ApprovalForgeryError("model_validate_strings is disabled for ApprovedOrder")

    def model_copy(self, *args: Any, **kwargs: Any) -> ApprovedOrder:
        raise ApprovalForgeryError("model_copy is disabled for ApprovedOrder")

    def __copy__(self) -> ApprovedOrder:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> ApprovedOrder:
        return self
