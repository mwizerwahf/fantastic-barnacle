"""Model-agnostic AI decision layer built on LiteLLM.

Swap providers by changing only ``AI_MODEL`` (e.g. ``anthropic/...``,
``openai/...``, ``gemini/...``, ``ollama/...``). Every response is validated
against ``AIDecision``; anything else becomes HOLD.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

import litellm
from pydantic import ValidationError

from core.config import Settings
from core.exceptions import AIProviderError, AIResponseError
from core.models import AIDecision
from core.observability import log_event
from core.resilience import call_external, is_transient

log = logging.getLogger(__name__)

SYSTEM_PROMPT: Final[str] = """\
You are a disciplined intraday equity trading analyst. You review one symbol at a
time and recommend BUY, SELL (open a short) or HOLD. You never size positions;
a separate risk engine does that and may reject your recommendation.

INPUT: one JSON object with these fields
- symbol, timeframe_minutes, as_of (ISO-8601, America/New_York)
- bias: "long" or "short", the direction a deterministic prefilter already found.
  Recommend that direction or HOLD. Never recommend the opposite direction.
- columns: names for each bar row, normally
  ["time","o","h","l","c","v","vwap","rsi","atr","cvd"]
    time = bar start HH:MM (New York); o/h/l/c = prices; v = volume;
    vwap = session VWAP; rsi = RSI(14, Wilder); atr = ATR(14);
    cvd = cumulative volume delta, a tick-rule PROXY for order flow, not true Level 2.
- bars: array of rows, oldest first, last row = most recent closed bar.
- latest: {price, vwap, rsi, atr, cvd, cvd_change}
- position: the current position in this symbol or null.
- account: {equity, buying_power, open_positions}

OUTPUT: reply with ONLY one JSON object, no prose and no markdown:
{"action":"BUY"|"SELL"|"HOLD","confidence":0.0-1.0,"reasoning":"<=300 chars",
 "stop_loss":<absolute price>,"take_profit":<absolute price>}

RULES
- BUY: stop_loss below the latest price, take_profit above it. SELL: the reverse.
- Reward:risk |take_profit - price| / |price - stop_loss| must be at least 1.5.
- Put stops beyond recent structure and at least 0.5 x ATR from price.
- If the setup is weak, extended or ambiguous, return HOLD with confidence 0,
  stop_loss 0 and take_profit 0. Missing a trade is better than a bad one.
"""

CORRECTIVE_PROMPT: Final[str] = (
    "Your previous reply was rejected: {error}. Reply again with ONLY a single JSON object "
    'with exactly these keys: "action", "confidence", "reasoning", "stop_loss", "take_profit". '
    "No markdown, no extra keys."
)

DECISION_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "stop_loss": {"type": "number"},
        "take_profit": {"type": "number"},
    },
    "required": ["action", "confidence", "reasoning", "stop_loss", "take_profit"],
    "additionalProperties": False,
}

# AI_API_BASE is sent only to these self-hosted providers, never to cloud models in the chain.
LOCAL_PROVIDERS: Final[frozenset[str]] = frozenset({"ollama", "ollama_chat", "hosted_vllm", "lm_studio"})

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)

CompletionFn = Callable[..., Any]


def parse_decision(raw: str | None) -> AIDecision:
    """Parse and validate raw model text. Raises ``AIResponseError``; never guesses."""
    if raw is None or not raw.strip():
        raise AIResponseError("empty response")
    text = raw.strip()
    if fenced := _FENCE.match(text):
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise AIResponseError("no JSON object found") from None
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise AIResponseError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise AIResponseError("top-level JSON value must be an object")
    try:
        return AIDecision.model_validate(payload)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'root'}: {e['msg']}" for e in exc.errors())
        raise AIResponseError(f"schema violation: {details}") from exc


def _extract_text(response: Any) -> str:
    message = response.choices[0].message
    content = getattr(message, "content", None)
    if isinstance(content, list):  # content-part style responses
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    if content:
        return str(content)
    tool_calls = getattr(message, "tool_calls", None)  # some providers return schema output as a tool call
    if tool_calls:
        return str(tool_calls[0].function.arguments)
    return ""


class AIRouter:
    def __init__(self, settings: Settings, completion_fn: CompletionFn | None = None) -> None:
        self._models = [settings.ai_model, *[m for m in settings.ai_fallback_models if m != settings.ai_model]]
        self._completion = completion_fn or litellm.completion
        self._timeout = settings.ai_timeout_s
        self._attempts = settings.ai_max_attempts
        self._backoff = settings.retry_backoff_s
        self._temperature = settings.ai_temperature
        self._max_tokens = settings.ai_max_tokens
        self._api_base = settings.ai_api_base
        self._format_cache: dict[str, dict[str, Any]] = {}
        litellm.drop_params = True  # silently drop params a provider doesn't support
        litellm.suppress_debug_info = True

    @property
    def models(self) -> list[str]:
        return list(self._models)

    def decide(self, context: Mapping[str, Any]) -> AIDecision:
        """Return a validated decision. Never raises: every failure is HOLD."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, separators=(",", ":"), default=str)},
        ]
        for model in self._models:
            try:
                return self._decide_with(model, messages)
            except AIProviderError as exc:
                log_event(log, logging.WARNING, "ai.provider_failed", model=model, error=str(exc))
        log_event(log, logging.ERROR, "ai.all_providers_failed", models=self._models)
        return AIDecision.hold("fail-closed: all AI providers failed")

    def _decide_with(self, model: str, messages: list[dict[str, str]]) -> AIDecision:
        raw = self._complete(model, messages)
        try:
            decision = parse_decision(raw)
        except AIResponseError as first:
            log_event(log, logging.WARNING, "ai.invalid_response", model=model, error=str(first), raw=raw[:2000])
            corrective = [*messages, {"role": "assistant", "content": raw},
                          {"role": "user", "content": CORRECTIVE_PROMPT.format(error=first)}]
            try:
                raw_retry = self._complete(model, corrective)
                decision = parse_decision(raw_retry)
            except (AIResponseError, AIProviderError) as second:
                log_event(log, logging.ERROR, "ai.invalid_response_final", model=model,
                          error=str(second), raw=raw[:2000])
                return AIDecision.hold(f"fail-closed: invalid AI output ({second})")
        log_event(log, logging.INFO, "ai.decision", model=model, action=decision.action.value,
                  confidence=decision.confidence, stop_loss=decision.stop_loss,
                  take_profit=decision.take_profit, reasoning=decision.reasoning)
        return decision

    def _response_format(self, model: str) -> dict[str, Any]:
        if model not in self._format_cache:
            fmt: dict[str, Any] = {"type": "json_object"}
            try:
                if litellm.supports_response_schema(model=model):
                    fmt = {"type": "json_schema",
                           "json_schema": {"name": "trade_decision", "schema": DECISION_JSON_SCHEMA, "strict": True}}
            except Exception:  # unknown model in LiteLLM's map: fall back to JSON mode
                pass
            self._format_cache[model] = fmt
        return self._format_cache[model]

    def _complete(self, model: str, messages: list[dict[str, str]]) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "timeout": self._timeout,
            "response_format": self._response_format(model),
        }
        if self._api_base and model.split("/", 1)[0] in LOCAL_PROVIDERS:
            kwargs["api_base"] = self._api_base
        try:
            return self._invoke(model, kwargs)
        except AIProviderError as exc:
            if exc.transient:
                raise
            # Provider refused structured output; prompt-only JSON is still validated.
            log_event(log, logging.WARNING, "ai.structured_output_rejected", model=model, error=str(exc))
            kwargs.pop("response_format")
            return self._invoke(model, kwargs)

    def _invoke(self, model: str, kwargs: dict[str, Any]) -> str:
        started = time.perf_counter()
        try:
            response = call_external(lambda: self._completion(**kwargs), op=f"llm.{model}",
                                     timeout=self._timeout + 5, attempts=self._attempts, backoff=self._backoff)
        except Exception as exc:
            raise AIProviderError(f"{model}: {exc!r}", transient=is_transient(exc)) from exc
        usage = getattr(response, "usage", None)
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            cost = None
        log_event(log, logging.INFO, "ai.call", model=model,
                  latency_ms=round((time.perf_counter() - started) * 1000),
                  prompt_tokens=getattr(usage, "prompt_tokens", None),
                  completion_tokens=getattr(usage, "completion_tokens", None),
                  cost_usd=cost)
        return _extract_text(response)
