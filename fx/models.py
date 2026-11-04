"""Domain models for the forex engine. Validated on the way into SQLite and again on the way out."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GRANULARITY_SECONDS: dict[str, int] = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}
Granularity = Literal["M1", "M5", "M15", "M30", "H1", "H4"]

# People-style names: letters, spaces, apostrophes, hyphens. Unique forever (retired names stay taken).
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z' \-]{1,29}$")
INSTRUMENT_RE = re.compile(r"^[A-Z0-9]{3,6}_[A-Z0-9]{3,6}$")


def normalize_instrument(value: str) -> str:
    v = value.strip().upper().replace("/", "_")
    if "_" not in v and len(v) == 6:
        v = f"{v[:3]}_{v[3:]}"
    if not INSTRUMENT_RE.match(v):
        raise ValueError(f"{value!r} is not an instrument like EUR_USD")
    return v


def validate_name(value: str) -> str:
    v = " ".join(value.split())
    if not NAME_RE.match(v):
        raise ValueError("name must be 2-30 letters (spaces, ' and - allowed), e.g. Amara or Jean-Paul")
    return v


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


class StrategyStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class RiskConfig(BaseModel):
    """Per-strategy sizing and exits. Every trade must carry a broker-side stop-loss."""

    model_config = ConfigDict(extra="forbid")

    sizing: Literal["units", "risk_pct"] = "units"
    units: int = Field(default=1000, gt=0)
    risk_pct: float = Field(default=0.5, gt=0)

    # Per-trade exits, sent to OANDA with the entry (instant, survive a bot crash).
    # $ amounts are in the account currency and converted to a price at entry.
    sl_pips: float | None = Field(default=20.0, gt=0)
    sl_amount: float | None = Field(default=None, gt=0)
    tp_pips: float | None = Field(default=30.0, gt=0)
    tp_amount: float | None = Field(default=None, gt=0)

    # Watched by the bot (all of this strategy's open trades together).
    basket_tp_amount: float | None = Field(default=None, gt=0)
    basket_sl_amount: float | None = Field(default=None, gt=0)
    daily_loss_limit: float | None = Field(default=None, gt=0)

    max_open_trades: int = Field(default=1, ge=1)
    max_spread_pips: float = Field(default=2.0, gt=0)
    max_slippage_pips: float = Field(default=1.0, gt=0)
    close_before_weekend: bool = True
    ai_min_confidence: float = Field(default=0.6, ge=0, le=1)

    @model_validator(mode="after")
    def _exits(self) -> RiskConfig:
        if (self.sl_pips is None) == (self.sl_amount is None):
            raise ValueError("set exactly one stop-loss: sl_pips or sl_amount")
        if self.tp_pips is not None and self.tp_amount is not None:
            raise ValueError("set at most one take-profit: tp_pips or tp_amount")
        if self.sizing == "risk_pct" and self.sl_amount is not None:
            raise ValueError("risk_pct sizing needs sl_pips (sl_amount already fixes the loss)")
        return self


class StrategyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    account_id: int
    instruments: list[str] = Field(min_length=1, max_length=20)
    granularity: Granularity = "M5"
    params: dict[str, Any] = Field(default_factory=dict)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    ai_enabled: bool = False
    llm_profile_id: int | None = None   # None = the default AI model
    status: StrategyStatus = StrategyStatus.ACTIVE
    version: int = 1
    notes: str = Field(default="", max_length=500)
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_name(v)

    @field_validator("instruments")
    @classmethod
    def _instruments(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_instrument(i) for i in v))


class InstrumentSpec(BaseModel):
    name: str
    pip_location: int
    display_precision: int
    units_precision: int = 0
    min_units: float = 1.0

    @property
    def pip(self) -> float:
        return 10.0 ** self.pip_location

    @property
    def quote_currency(self) -> str:
        return self.name.split("_", 1)[1]


class Candle(BaseModel):
    time: datetime
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    volume: int = 0


class Price(BaseModel):
    instrument: str
    bid: float
    ask: float
    time: datetime
    tradeable: bool = True
    # account-currency value of 1 unit of the quote currency, for gains and for losses
    conv_gain: float | None = None
    conv_loss: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class Signal(BaseModel):
    direction: Direction
    reason: str = Field(max_length=300)
    # Optional per-signal overrides (e.g. ATR-based); otherwise the strategy's RiskConfig applies.
    sl_pips: float | None = Field(default=None, gt=0)
    tp_pips: float | None = Field(default=None, gt=0)
