"""Optional AI confirmation for forex signals, pluggable per provider.

The provider is picked from the model string's prefix (``gemini/...``, ``anthropic/...``). Any
prefix without a dedicated provider goes through LiteLLM. To add a new provider (e.g. Jev):

    @register_provider("jev")
    class JevProvider:
        def __init__(self, model, *, api_key, api_base, timeout, max_tokens): ...
        def complete_json(self, system: str, user: str) -> str: ...

Every failure is fail-closed: the trade is skipped, never guessed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.observability import log_event

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You review a proposed forex trade from a rule-based strategy.
Reply with JSON only: {"approve": true|false, "confidence": 0.0-1.0, "reason": "<= 200 chars"}.
Approve only if recent price action supports the proposed direction. If unsure, reject."""


class AIVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    approve: bool
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(default="", max_length=300)

    @classmethod
    def reject(cls, reason: str) -> AIVerdict:
        return cls(approve=False, confidence=0.0, reason=reason[:300])


class AIProvider(Protocol):
    def complete_json(self, system: str, user: str) -> str: ...


ProviderFactory = Callable[..., AIProvider]
PROVIDERS: dict[str, ProviderFactory] = {}


def register_provider(prefix: str) -> Callable[[ProviderFactory], ProviderFactory]:
    def deco(factory: ProviderFactory) -> ProviderFactory:
        PROVIDERS[prefix] = factory
        return factory
    return deco


class LiteLLMProvider:
    """Any LiteLLM model string. Imported lazily: litellm is heavy and many setups never use AI."""

    def __init__(self, model: str, *, api_key: str | None, api_base: str | None, timeout: float,
                 max_tokens: int) -> None:
        self.model = model
        self._kwargs: dict[str, Any] = {"timeout": timeout, "max_tokens": max_tokens, "temperature": 0,
                                        "drop_params": True, "response_format": {"type": "json_object"}}
        if api_key:
            self._kwargs["api_key"] = api_key
        if api_base:
            self._kwargs["api_base"] = api_base

    def complete_json(self, system: str, user: str) -> str:
        import litellm

        resp = litellm.completion(model=self.model, messages=[{"role": "system", "content": system},
                                                              {"role": "user", "content": user}], **self._kwargs)
        content = resp.choices[0].message.content
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        return content or ""


def build_provider(model: str, *, api_key: str | None = None, api_base: str | None = None,
                   timeout: float = 30.0, max_tokens: int = 4000) -> AIProvider:
    factory = PROVIDERS.get(model.split("/", 1)[0], LiteLLMProvider)
    return factory(model, api_key=api_key, api_base=api_base, timeout=timeout, max_tokens=max_tokens)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_verdict(raw: str) -> AIVerdict:
    match = _JSON_RE.search(raw or "")
    if not match:
        raise ValueError("no JSON object in AI reply")
    return AIVerdict.model_validate(json.loads(match.group(0)))


def confirm(provider: AIProvider, model: str, context: dict[str, Any], *, strategy: str) -> AIVerdict:
    started = time.perf_counter()
    try:
        raw = provider.complete_json(SYSTEM_PROMPT, json.dumps(context, separators=(",", ":"), default=str))
        verdict = parse_verdict(raw)
    except (ValueError, ValidationError) as exc:
        verdict = AIVerdict.reject(f"invalid AI reply: {exc}")
    except Exception as exc:  # provider/transport failure
        verdict = AIVerdict.reject(f"AI unavailable: {type(exc).__name__}: {exc}")
    log_event(log, logging.INFO, "fx.ai.verdict", strategy=strategy, model=model, approve=verdict.approve,
              confidence=verdict.confidence, latency_ms=round((time.perf_counter() - started) * 1000))
    return verdict
