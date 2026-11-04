"""Process-level settings for the forex engine and console.

Everything per strategy (account, pairs, risk, AI model) lives in the SQLite database and is
edited from the console. Only machine-level settings and the master key come from ``.env``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class FxSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FX_", env_file=".env", env_file_encoding="utf-8-sig",
                                      extra="ignore")

    db_path: Path = Path("fx_state/fx.db")
    # Fernet key that encrypts broker tokens and LLM keys at rest. Generate: python -m fx keygen
    master_key: SecretStr = SecretStr("")
    # Live OANDA accounts can be added from the console but are never traded unless this is true.
    allow_live: bool = False

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8060, ge=1, le=65535)

    http_timeout_s: float = Field(default=10.0, gt=0)
    http_attempts: int = Field(default=3, ge=1, le=6)
    http_backoff_s: float = Field(default=0.5, ge=0)
    ai_timeout_s: float = Field(default=30.0, gt=0)
    ai_max_tokens: int = Field(default=4000, gt=0)
    ai_workers: int = Field(default=3, ge=1, le=8)

    bar_publish_delay_s: float = Field(default=3.0, ge=0)
    reload_s: float = Field(default=5.0, gt=0)          # how often strategy edits are picked up
    basket_poll_s: float = Field(default=2.0, ge=1.0)   # $-basket / daily-loss watch, only while needed
    account_refresh_s: float = Field(default=60.0, ge=5.0)
    reconcile_s: float = Field(default=300.0, ge=30.0)

    log_level: str = "INFO"
    log_json: bool = True
