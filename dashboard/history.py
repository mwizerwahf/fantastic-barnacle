"""Pure helpers that turn Alpaca order history into fills and round-trip trades."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Fill:
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    price: float
    filled_at: str  # ISO-8601
    order_id: str


def fills_from_orders(orders: list[dict[str, Any]]) -> list[Fill]:
    """Flatten bracket legs and keep filled quantity only, oldest first."""
    fills: list[Fill] = []
    stack = list(orders)
    while stack:
        order = stack.pop()
        stack.extend(order.get("legs") or [])
        qty, price, at = order.get("filled_qty"), order.get("filled_avg_price"), order.get("filled_at")
        if qty and price and at and float(qty) > 0:
            fills.append(Fill(order["symbol"], order["side"], float(qty), float(price), at, order["id"]))
    return sorted(fills, key=lambda f: (f.filled_at, f.order_id))


def pair_round_trips(fills: list[Fill]) -> list[dict[str, Any]]:
    """FIFO-match opening and closing fills per symbol. Newest trade first."""
    lots: dict[str, deque[list[Any]]] = defaultdict(deque)  # [side, qty, price, opened_at]
    trades: list[dict[str, Any]] = []
    for f in fills:
        book = lots[f.symbol]
        remaining = f.qty
        while remaining > _EPS and book and book[0][0] != f.side:
            lot = book[0]
            matched = min(remaining, lot[1])
            long = lot[0] == "buy"
            pnl = (f.price - lot[2]) * matched if long else (lot[2] - f.price) * matched
            trades.append({
                "symbol": f.symbol, "direction": "long" if long else "short", "qty": matched,
                "entry_price": lot[2], "exit_price": f.price, "opened_at": lot[3], "closed_at": f.filled_at,
                "pnl": round(pnl, 2), "return_pct": round(pnl / (lot[2] * matched) * 100, 3),
            })
            lot[1] -= matched
            remaining -= matched
            if lot[1] <= _EPS:
                book.popleft()
        if remaining > _EPS:
            book.append([f.side, remaining, f.price, f.filled_at])
    return list(reversed(trades))


def _hold_minutes(trade: dict[str, Any]) -> float | None:
    try:
        opened = datetime.fromisoformat(trade["opened_at"])
        closed = datetime.fromisoformat(trade["closed_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (closed - opened).total_seconds() / 60


def trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_loss = -sum(losses)
    holds = [h for h in (_hold_minutes(t) for t in trades) if h is not None]
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "net_pnl": round(sum(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else None,
        "best": max(pnls) if pnls else None,
        "worst": min(pnls) if pnls else None,
        "avg_hold_min": round(sum(holds) / len(holds), 1) if holds else None,
    }


def by_symbol(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        groups[t["symbol"]].append(t["pnl"])
    rows = [
        {"symbol": sym, "trades": len(p), "net_pnl": round(sum(p), 2),
         "win_rate": round(sum(1 for x in p if x > 0) / len(p) * 100, 1)}
        for sym, p in groups.items()
    ]
    return sorted(rows, key=lambda r: r["net_pnl"], reverse=True)


def daily_pnl(points: list[dict[str, Any]], base_value: float | None) -> list[dict[str, Any]]:
    """Day-over-day equity change from daily portfolio-history points (``date``, ``equity``)."""
    out: list[dict[str, Any]] = []
    prev = base_value if base_value else (points[0]["equity"] if points else None)
    for p in points:
        if prev:
            out.append({"date": p["date"], "pnl": round(p["equity"] - prev, 2),
                        "pct": round((p["equity"] - prev) / prev * 100, 3)})
        prev = p["equity"]
    return out


def max_drawdown_pct(equities: list[float]) -> float | None:
    """Largest peak-to-trough decline, in percent (positive number)."""
    peak, worst = None, 0.0
    for eq in equities:
        if eq is None or eq <= 0:
            continue
        peak = eq if peak is None or eq > peak else peak
        worst = max(worst, (peak - eq) / peak)
    return round(worst * 100, 3) if peak is not None else None
