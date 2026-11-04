"""Plain-Python indicators (no pandas: keeps the engine well under 1 GB RAM).

All functions take oldest-first lists and return a list of the same length; values that are not
yet defined are ``None``.
"""

from __future__ import annotations

from collections.abc import Sequence

Series = list[float | None]


def sma(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: Sequence[float], n: int, start: int) -> Series:
    """Wilder smoothing of ``values[start:]``, seeded with a simple mean of the first n."""
    out: Series = [None] * len(values)
    if len(values) - start < n:
        return out
    prev = sum(values[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    if len(closes) <= n:
        return [None] * len(closes)
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = _wilder(gains, n, 1), _wilder(losses, n, 1)
    out: Series = []
    for g, lo in zip(ag, al):
        if g is None or lo is None:
            out.append(None)
        elif lo == 0:
            out.append(100.0 if g > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + g / lo))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> Series:
    tr = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    return _wilder(tr, n, 0)


def donchian(highs: Sequence[float], lows: Sequence[float], n: int) -> tuple[Series, Series]:
    """Highest high / lowest low of the n bars *before* each bar (so a breakout can be tested)."""
    upper: Series = [None] * len(highs)
    lower: Series = [None] * len(lows)
    for i in range(n, len(highs)):
        upper[i] = max(highs[i - n:i])
        lower[i] = min(lows[i - n:i])
    return upper, lower
