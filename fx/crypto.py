"""Encryption at rest for broker tokens and LLM API keys."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from core.exceptions import ConfigError


def new_key() -> str:
    return Fernet.generate_key().decode()


def hint(secret: str) -> str:
    """Last four characters only, for display."""
    return "…" + secret[-4:] if len(secret) >= 8 else "…"


class SecretBox:
    def __init__(self, key: str) -> None:
        if not key:
            raise ConfigError("FX_MASTER_KEY is not set. Generate one with: python -m fx keygen")
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as exc:
            raise ConfigError("FX_MASTER_KEY is not a valid key. Generate one with: python -m fx keygen") from exc

    def seal(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def open(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ConfigError("stored secret cannot be decrypted: FX_MASTER_KEY changed?") from exc
