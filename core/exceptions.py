"""Custom exception hierarchy.

Every layer raises a subclass of ``TradingBotError`` so the execution loop can
catch domain failures in one place and *fail closed* (no trade).
"""

from __future__ import annotations


class TradingBotError(Exception):
    """Root of all bot-specific errors."""


class ConfigError(TradingBotError):
    """Invalid or unsafe configuration."""


class ExternalCallTimeout(TradingBotError, TimeoutError):
    """An external call (broker, data, LLM) exceeded its deadline."""


# --- Market data -----------------------------------------------------------
class DataError(TradingBotError):
    """Market data could not be fetched or is unusable."""


class DataValidationError(DataError):
    """Market data failed schema / sanity validation."""


class StaleDataError(DataError):
    """Market data is older than the allowed freshness window."""


# --- Broker ----------------------------------------------------------------
class BrokerError(TradingBotError):
    """Broker API call failed."""


class OrderSubmissionError(BrokerError):
    """An order could not be submitted (or was refused before submission)."""


# --- AI ----------------------------------------------------------------------
class AIError(TradingBotError):
    """Base for AI-layer failures."""


class AIResponseError(AIError):
    """The model returned malformed JSON or violated the decision schema."""


class AIProviderError(AIError):
    """Transport/provider failure (timeout, rate limit, auth, bad request)."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


# --- Strategy / risk -------------------------------------------------------
class StrategyError(TradingBotError):
    """Strategy misconfiguration or internal failure."""


class RiskError(TradingBotError):
    """Base for risk-layer failures."""


class ApprovalForgeryError(RiskError):
    """Attempt to build an ApprovedOrder outside the risk layer."""


class RiskStateError(RiskError):
    """Persisted risk state is unreadable; the bot refuses to start."""
