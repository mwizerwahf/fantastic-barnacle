"""Timeout + retry wrapper shared by every external call (broker, data, LLM)."""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import TypeVar

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from core.exceptions import ExternalCallTimeout
from core.observability import log_event

T = TypeVar("T")
log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Worker threads enforce hard deadlines on SDKs that don't expose a timeout.
# A timed-out call may keep running in its thread, but its result is discarded.
_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="ext-call")


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def is_transient(exc: BaseException) -> bool:
    """True for errors worth retrying: timeouts, connection drops, 429/5xx."""
    if isinstance(exc, (ExternalCallTimeout, TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    try:
        import requests  # transitive dependency of alpaca-py

        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
    except ImportError:
        pass
    status = _status_code(exc)
    return status in RETRYABLE_STATUS if status is not None else False


def call_external(
    fn: Callable[[], T],
    *,
    op: str,
    timeout: float,
    attempts: int,
    backoff: float,
) -> T:
    """Run ``fn`` with a hard deadline, retrying transient failures with jittered backoff."""

    def _once() -> T:
        future = _EXECUTOR.submit(contextvars.copy_context().run, fn)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout as exc:
            future.cancel()
            raise ExternalCallTimeout(f"{op} timed out after {timeout:.1f}s") from exc

    def _before_sleep(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        log_event(log, logging.WARNING, "external.retry", op=op, attempt=state.attempt_number, error=repr(exc))

    retrying = Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=backoff, max=max(backoff * 8, backoff)) + wait_random(0, backoff),
        retry=retry_if_exception(is_transient),
        before_sleep=_before_sleep,
        reraise=True,
    )
    return retrying(_once)
