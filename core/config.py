"""Runtime configuration loaded from environment variables / ``.env``.

Risk values here are *requests*: ``risk/guardrails.py`` clamps them to its
hard limits, so config can make the bot stricter but never looser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MARKET_TZ = "America/New_York"


def missing_alpaca_keys_message() -> str:
    env_path = Path.cwd() / ".env"
    found = "found but the keys are empty or misspelled" if env_path.exists() else "NOT FOUND"
    return f"ALPACA_API_KEY and ALPACA_SECRET_KEY must be set ({env_path}: {found})"


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",  # tolerate a BOM added by Windows editors
        extra="ignore",
        case_sensitive=False,
    )

    # --- Broker -------------------------------------------------------------
    broker: Literal["alpaca", "ibkr"] = "alpaca"
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_secret_key: SecretStr = SecretStr("")
    trading_mode: Literal["paper", "live"] = "paper"
    confirm_live_trading: str = ""

    # --- Market data --------------------------------------------------------
    data_feed: Literal["iex", "sip"] = "iex"
    symbols: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["SPY", "QQQ"])
    timeframe_minutes: int = 5
    bar_publish_delay_s: float = Field(default=5.0, ge=0)
    cvd_source: Literal["trades", "bars"] = "trades"
    cvd_lookback_minutes: int = Field(default=60, gt=0)
    max_trades_per_fetch: int = Field(default=50_000, gt=0)

    # --- AI -----------------------------------------------------------------
    ai_model: str = "anthropic/claude-sonnet-5-5"
    ai_fallback_models: Annotated[list[str], NoDecode] = Field(default_factory=list)
    ai_api_base: str | None = None
    ai_timeout_s: float = Field(default=30.0, gt=0)
    ai_max_attempts: int = Field(default=2, ge=1)
    ai_temperature: float = Field(default=0.0, ge=0, le=2)
    ai_max_tokens: int = Field(default=4000, gt=0)  # thinking models spend part of this on reasoning
    ai_context_bars: int = Field(default=30, ge=5, le=200)

    # --- Strategy -----------------------------------------------------------
    strategy: str = "intraday_momentum"

    # --- Risk (requests; clamped by risk/guardrails.py) ---------------------
    risk_per_trade_pct: float = Field(default=0.01, gt=0)
    max_position_notional_pct: float = Field(default=0.25, gt=0)
    min_reward_risk: float = Field(default=1.5, gt=0)
    min_confidence: float = Field(default=0.6, ge=0, le=1)
    min_stop_atr_mult: float = Field(default=0.5, ge=0)
    max_daily_drawdown_pct: float = Field(default=0.03, gt=0)
    eod_flatten_minutes: int = Field(default=15, ge=0)
    max_concurrent_positions: int = Field(default=2, ge=1)
    max_trades_per_day: int = Field(default=6, ge=1)
    open_blackout_minutes: int = Field(default=5, ge=0)
    allow_short: bool = False
    pdt_equity_threshold: float = Field(default=25_000.0, ge=0)
    pdt_max_day_trades: int = Field(default=3, ge=0)
    approval_ttl_s: float = Field(default=30.0, gt=0)

    # --- Runtime ------------------------------------------------------------
    state_dir: Path = Path("state")
    flatten_on_shutdown: bool = True
    broker_timeout_s: float = Field(default=15.0, gt=0)
    data_timeout_s: float = Field(default=15.0, gt=0)
    max_attempts: int = Field(default=3, ge=1)
    retry_backoff_s: float = Field(default=0.5, ge=0)
    log_level: str = "INFO"
    log_json: bool = True

    # --- Dashboard (read-only; keep it bound to localhost behind nginx) -------
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8050, gt=0, lt=65536)

    @field_validator("symbols", "ai_fallback_models", mode="before")
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("symbols")
    @classmethod
    def _normalize_symbols(cls, value: list[str]) -> list[str]:
        symbols = list(dict.fromkeys(s.upper() for s in value))
        if not symbols:
            raise ValueError("at least one symbol is required")
        return symbols

    @field_validator("timeframe_minutes")
    @classmethod
    def _check_timeframe(cls, value: int) -> int:
        if value not in (1, 5, 15):
            raise ValueError("timeframe_minutes must be 1, 5 or 15")
        return value

    @field_validator("ai_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ai_model must not be empty")
        return value.strip()

    @field_validator("ai_fallback_models")
    @classmethod
    def _check_fallbacks(cls, value: list[str]) -> list[str]:
        urls = [m for m in value if "://" in m]
        if urls:
            raise ValueError(f"AI_FALLBACK_MODELS takes model names like 'ollama/llama3.1', not URLs {urls}; "
                             "put the server URL in AI_API_BASE")
        return value

    @model_validator(mode="after")
    def _guard_live_mode(self) -> Settings:
        if self.trading_mode == "live" and self.confirm_live_trading.strip().lower() != "yes":
            raise ValueError("TRADING_MODE=live also requires CONFIRM_LIVE_TRADING=yes")
        return self

    @property
    def is_paper(self) -> bool:
        return self.trading_mode != "live"

    def public_summary(self) -> dict[str, Any]:
        """Config snapshot safe for logging (secrets excluded)."""
        return self.model_dump(exclude={"alpaca_api_key", "alpaca_secret_key"}, mode="json")
