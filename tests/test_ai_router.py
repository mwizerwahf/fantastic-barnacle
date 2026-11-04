from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.ai_router import AIRouter, parse_decision
from core.config import Settings
from core.exceptions import AIResponseError
from core.models import Action

VALID = {"action": "BUY", "confidence": 0.8, "reasoning": "trend above VWAP", "stop_loss": 99.0,
         "take_profit": 103.0}


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


class ScriptedCompletion:
    """Fake ``litellm.completion``: returns or raises the scripted outputs in order."""

    def __init__(self, *outputs: str | Exception) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return _response(out)


def make_router(fake: ScriptedCompletion, fallbacks: list[str] | None = None) -> AIRouter:
    settings = Settings(_env_file=None, ai_model="test/primary", ai_fallback_models=fallbacks or [],
                        ai_max_attempts=1, retry_backoff_s=0, ai_timeout_s=5)  # type: ignore[arg-type]
    return AIRouter(settings, completion_fn=fake)


CONTEXT = {"symbol": "SPY", "bias": "long", "bars": []}


def test_valid_json_returns_decision() -> None:
    fake = ScriptedCompletion(json.dumps(VALID))
    decision = make_router(fake).decide(CONTEXT)
    assert decision.action is Action.BUY and decision.stop_loss == 99.0
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "test/primary"


def test_fenced_json_is_accepted() -> None:
    fake = ScriptedCompletion(f"```json\n{json.dumps(VALID)}\n```")
    assert make_router(fake).decide(CONTEXT).action is Action.BUY


def test_malformed_json_twice_returns_hold() -> None:
    fake = ScriptedCompletion("I think you should buy!", '{"action": "BUY", ')
    decision = make_router(fake).decide(CONTEXT)
    assert decision.action is Action.HOLD and decision.confidence == 0.0
    assert len(fake.calls) == 2
    assert "rejected" in fake.calls[1]["messages"][-1]["content"]  # corrective prompt was sent


def test_malformed_then_valid_recovers() -> None:
    fake = ScriptedCompletion("not json", json.dumps(VALID))
    assert make_router(fake).decide(CONTEXT).action is Action.BUY


@pytest.mark.parametrize(
    "bad",
    [
        {**VALID, "confidence": 1.7},
        {**VALID, "action": "STRONG_BUY"},
        {**VALID, "stop_loss": 0},
        {**VALID, "reasoning": "x" * 501},
        {**VALID, "position_size_pct": 0.5},
        {k: v for k, v in VALID.items() if k != "take_profit"},
    ],
    ids=["confidence_range", "bad_action", "zero_stop", "long_reasoning", "extra_key", "missing_key"],
)
def test_schema_violations_return_hold(bad: dict[str, Any]) -> None:
    fake = ScriptedCompletion(json.dumps(bad), json.dumps(bad))
    assert make_router(fake).decide(CONTEXT).action is Action.HOLD
    assert len(fake.calls) == 2


def test_hold_without_levels_is_valid() -> None:
    hold = {"action": "HOLD", "confidence": 0.0, "reasoning": "chop", "stop_loss": 0, "take_profit": 0}
    assert make_router(ScriptedCompletion(json.dumps(hold))).decide(CONTEXT).action is Action.HOLD


def test_fallback_model_used_on_provider_error() -> None:
    fake = ScriptedCompletion(ConnectionError("down"), json.dumps(VALID))
    decision = make_router(fake, fallbacks=["test/fallback"]).decide(CONTEXT)
    assert decision.action is Action.BUY
    assert [c["model"] for c in fake.calls] == ["test/primary", "test/fallback"]


def test_all_providers_failing_returns_hold() -> None:
    fake = ScriptedCompletion(ConnectionError("a"), ConnectionError("b"))
    assert make_router(fake, fallbacks=["test/fallback"]).decide(CONTEXT).action is Action.HOLD


def test_api_base_only_sent_to_local_providers() -> None:
    fake = ScriptedCompletion(ConnectionError("cloud down"), json.dumps(VALID))
    settings = Settings(_env_file=None, ai_model="anthropic/x", ai_fallback_models=["ollama/llama3.1"],
                        ai_api_base="http://localhost:11434", ai_max_attempts=1,
                        retry_backoff_s=0)  # type: ignore[arg-type]
    AIRouter(settings, completion_fn=fake).decide(CONTEXT)
    assert "api_base" not in fake.calls[0]
    assert fake.calls[1]["api_base"] == "http://localhost:11434"


def test_url_in_fallback_models_is_rejected() -> None:
    with pytest.raises(ValueError, match="AI_API_BASE"):
        Settings(_env_file=None, ai_fallback_models="http://localhost:11434")  # type: ignore[arg-type]


def test_parse_decision_rejects_non_objects() -> None:
    for raw in ("", "[]", "null", "{}"):
        with pytest.raises(AIResponseError):
            parse_decision(raw)
