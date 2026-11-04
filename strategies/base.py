"""Strategy interface and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, TypeVar

import pandas as pd

from core.ai_router import AIRouter
from core.config import Settings
from core.exceptions import ConfigError
from core.models import AccountState, IndicatorSnapshot, Position, Quote, Signal


class BaseStrategy(ABC):
    name: ClassVar[str]

    def __init__(self, settings: Settings, ai: AIRouter) -> None:
        self.settings = settings
        self.ai = ai

    @property
    @abstractmethod
    def required_lookback(self) -> int:
        """Bars to fetch per cycle (enough for indicator warm-up and a full session VWAP)."""

    @abstractmethod
    def prefilter(self, snapshot: IndicatorSnapshot) -> bool:
        """Cheap deterministic gate. The AI is only called when this returns True."""

    @abstractmethod
    def build_context(self, bars: pd.DataFrame, snapshot: IndicatorSnapshot, account: AccountState,
                      position: Position | None = None) -> dict[str, Any]:
        """Compact, bounded JSON-serializable context for the LLM."""

    @abstractmethod
    def decide(self, bars: pd.DataFrame, snapshot: IndicatorSnapshot, account: AccountState,
               position: Position | None, quote: Quote | None) -> Signal: ...


S = TypeVar("S", bound=type[BaseStrategy])

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(cls: S) -> S:
    if cls.name in STRATEGY_REGISTRY:
        raise ConfigError(f"strategy {cls.name!r} registered twice")
    STRATEGY_REGISTRY[cls.name] = cls
    return cls


def build_strategy(name: str, settings: Settings, ai: AIRouter) -> BaseStrategy:
    try:
        return STRATEGY_REGISTRY[name](settings, ai)
    except KeyError:
        raise ConfigError(f"unknown strategy {name!r}; available: {sorted(STRATEGY_REGISTRY)}") from None
