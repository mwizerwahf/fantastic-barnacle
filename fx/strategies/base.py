"""Strategy plug-in contract.

A strategy *type* (``kind``) is a class registered here; a *strategy* is a named, configured
instance of a type stored in SQLite (e.g. "Amara" = ema_cross on EUR_USD, M5, fast 9 / slow 21).
Many named strategies can share a type, an account and a pair.

To add a type: subclass ``FxStrategy``, declare ``Params`` (a pydantic model: the console builds
its form from the JSON schema), implement ``on_bar``, decorate with ``@register``, and import the
module in ``fx/strategies/__init__.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from fx.models import Candle, InstrumentSpec, Signal


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class BarContext:
    instrument: str
    granularity: str
    candles: list[Candle]            # completed candles, oldest first; candles[-1] just closed
    spec: InstrumentSpec
    now: datetime
    open_trades: list[dict[str, Any]] = field(default_factory=list)  # this strategy, this instrument

    @property
    def closes(self) -> list[float]:
        return [k.c for k in self.candles]

    @property
    def highs(self) -> list[float]:
        return [k.h for k in self.candles]

    @property
    def lows(self) -> list[float]:
        return [k.l for k in self.candles]

    def to_pips(self, price_distance: float) -> float:
        return price_distance / self.spec.pip


class FxStrategy(ABC):
    kind: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def __init__(self, params: StrategyParams) -> None:
        self.params = params

    @property
    def lookback(self) -> int:
        """Completed candles needed for one decision."""
        return 100

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Called once per completed candle per instrument. Return a Signal to open a trade."""

    def ai_context(self, ctx: BarContext, signal: Signal) -> dict[str, Any]:
        """What the optional AI confirmation sees. Override to add strategy-specific features."""
        digits = ctx.spec.display_precision
        tail = ctx.candles[-30:]
        return {
            "instrument": ctx.instrument, "granularity": ctx.granularity, "strategy_type": self.kind,
            "proposed": {"direction": signal.direction.value, "reason": signal.reason},
            "columns": ["time", "o", "h", "l", "c", "v"],
            "candles": [[k.time.strftime("%m-%d %H:%M"), round(k.o, digits), round(k.h, digits),
                         round(k.l, digits), round(k.c, digits), k.volume] for k in tail],
        }


REGISTRY: dict[str, type[FxStrategy]] = {}


def register(cls: type[FxStrategy]) -> type[FxStrategy]:
    if cls.kind in REGISTRY:
        raise ValueError(f"duplicate strategy kind {cls.kind!r}")
    REGISTRY[cls.kind] = cls
    return cls


def build(kind: str, params: dict[str, Any]) -> FxStrategy:
    try:
        cls = REGISTRY[kind]
    except KeyError as exc:
        raise ValueError(f"unknown strategy type {kind!r}; known: {sorted(REGISTRY)}") from exc
    return cls(cls.Params.model_validate(params))


def validate_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validated params with defaults filled in, as stored."""
    return build(kind, params).params.model_dump()


def catalog() -> list[dict[str, Any]]:
    return [{"kind": k, "title": c.title, "description": c.description, "params": c.Params.model_json_schema()}
            for k, c in sorted(REGISTRY.items())]
