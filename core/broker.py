"""Broker abstraction and adapters.

Order entry accepts only ``ApprovedOrder`` (minted by the risk layer), and every
entry is a bracket order so stop-loss/take-profit live at the broker even if
this process dies.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, QueryOrderStatus, TimeInForce
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from core.config import MARKET_TZ, Settings
from core.exceptions import BrokerError, ConfigError, OrderSubmissionError, TradingBotError
from core.models import (
    AccountState,
    ApprovedOrder,
    Clock,
    MarketSession,
    OrderInfo,
    OrderSide,
    Position,
    PositionSide,
)
from core.observability import log_event
from core.resilience import call_external

T = TypeVar("T")
log = logging.getLogger(__name__)
NY = ZoneInfo(MARKET_TZ)


class BaseBroker(ABC):
    @abstractmethod
    def get_account(self) -> AccountState: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_open_orders(self) -> list[OrderInfo]: ...

    @abstractmethod
    def get_clock(self) -> Clock: ...

    @abstractmethod
    def get_calendar(self, start: date, end: date) -> list[MarketSession]: ...

    @abstractmethod
    def submit_bracket_order(self, approved: ApprovedOrder) -> OrderInfo: ...

    @abstractmethod
    def cancel_all_orders(self) -> None: ...

    @abstractmethod
    def close_all_positions(self) -> None: ...


# --- Alpaca ------------------------------------------------------------------
def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _enum_str(value: Any) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _ny(dt: datetime) -> datetime:
    return (dt.replace(tzinfo=NY) if dt.tzinfo is None else dt).astimezone(NY)


def _to_order_info(order: Any) -> OrderInfo:
    return OrderInfo(
        id=str(order.id),
        client_order_id=str(order.client_order_id),
        symbol=str(order.symbol),
        side=_enum_str(order.side),
        qty=_f(order.qty) if order.qty is not None else None,
        status=_enum_str(order.status),
        order_class=_enum_str(order.order_class) if order.order_class is not None else None,
        submitted_at=order.submitted_at,
    )


class AlpacaBroker(BaseBroker):
    def __init__(self, settings: Settings, client: TradingClient | None = None) -> None:
        key = settings.alpaca_api_key.get_secret_value()
        secret = settings.alpaca_secret_key.get_secret_value()
        if client is None and not (key and secret):
            raise ConfigError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
        self._client = client or TradingClient(key, secret, paper=settings.is_paper)
        self._timeout = settings.broker_timeout_s
        self._attempts = settings.max_attempts
        self._backoff = settings.retry_backoff_s
        log_event(log, logging.INFO, "broker.init", broker="alpaca", paper=settings.is_paper)

    def _call(self, op: str, fn: Callable[[], T], *, attempts: int | None = None) -> T:
        try:
            return call_external(fn, op=f"alpaca.{op}", timeout=self._timeout,
                                 attempts=attempts or self._attempts, backoff=self._backoff)
        except TradingBotError:
            raise
        except Exception as exc:
            raise BrokerError(f"alpaca.{op} failed: {exc!r}") from exc

    def get_account(self) -> AccountState:
        acct = self._call("get_account", self._client.get_account)
        return AccountState(
            equity=_f(acct.equity),
            last_equity=_f(acct.last_equity),
            buying_power=_f(acct.buying_power),
            cash=_f(acct.cash),
            daytrade_count=int(acct.daytrade_count or 0),
            pattern_day_trader=bool(acct.pattern_day_trader),
            trading_blocked=bool(acct.trading_blocked or acct.account_blocked),
            shorting_enabled=bool(acct.shorting_enabled),
        )

    def get_positions(self) -> list[Position]:
        raw = self._call("get_positions", self._client.get_all_positions)
        return [
            Position(
                symbol=p.symbol,
                side=PositionSide(_enum_str(p.side)),
                qty=abs(_f(p.qty)),
                avg_entry_price=_f(p.avg_entry_price),
                market_value=_f(p.market_value),
                unrealized_pl=_f(p.unrealized_pl),
            )
            for p in raw
            if abs(_f(p.qty)) > 0
        ]

    def get_open_orders(self) -> list[OrderInfo]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500)
        return [_to_order_info(o) for o in self._call("get_orders", lambda: self._client.get_orders(filter=req))]

    def get_clock(self) -> Clock:
        c = self._call("get_clock", self._client.get_clock)
        return Clock(timestamp=_ny(c.timestamp), is_open=bool(c.is_open),
                     next_open=_ny(c.next_open), next_close=_ny(c.next_close))

    def get_calendar(self, start: date, end: date) -> list[MarketSession]:
        req = GetCalendarRequest(start=start, end=end)
        days = self._call("get_calendar", lambda: self._client.get_calendar(filters=req))
        return [MarketSession(date=d.date, open=_ny(d.open), close=_ny(d.close)) for d in days]

    def submit_bracket_order(self, approved: ApprovedOrder) -> OrderInfo:
        if not isinstance(approved, ApprovedOrder):
            raise OrderSubmissionError("submit_bracket_order accepts only ApprovedOrder")
        if approved.is_expired(datetime.now(UTC)):
            raise OrderSubmissionError(f"approval {approved.client_order_id} expired; re-evaluate")
        req = MarketOrderRequest(
            symbol=approved.symbol,
            qty=approved.qty,
            side=AlpacaOrderSide.BUY if approved.side is OrderSide.BUY else AlpacaOrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=approved.take_profit),
            stop_loss=StopLossRequest(stop_price=approved.stop_loss),
            client_order_id=approved.client_order_id,
        )
        try:
            # Safe to retry: a duplicate client_order_id is rejected by Alpaca.
            order = self._call("submit_order", lambda: self._client.submit_order(order_data=req))
        except BrokerError as exc:
            existing = self._find_by_client_id(approved.client_order_id)
            if existing is not None:
                log_event(log, logging.WARNING, "order.recovered_after_error",
                          client_order_id=approved.client_order_id, error=str(exc))
                return existing
            raise OrderSubmissionError(str(exc)) from exc
        return _to_order_info(order)

    def _find_by_client_id(self, client_order_id: str) -> OrderInfo | None:
        try:
            order = self._call("get_order_by_client_id",
                               lambda: self._client.get_order_by_client_id(client_order_id), attempts=1)
        except BrokerError:
            return None
        return _to_order_info(order)

    def cancel_all_orders(self) -> None:
        self._call("cancel_orders", self._client.cancel_orders)
        log_event(log, logging.WARNING, "broker.cancel_all_orders")

    def close_all_positions(self) -> None:
        self._call("close_all_positions", lambda: self._client.close_all_positions(cancel_orders=True))
        log_event(log, logging.WARNING, "broker.close_all_positions")


class DryRunBroker(BaseBroker):
    """Passes reads through to a real broker; logs writes instead of sending them."""

    def __init__(self, inner: BaseBroker) -> None:
        self._inner = inner

    def get_account(self) -> AccountState:
        return self._inner.get_account()

    def get_positions(self) -> list[Position]:
        return self._inner.get_positions()

    def get_open_orders(self) -> list[OrderInfo]:
        return self._inner.get_open_orders()

    def get_clock(self) -> Clock:
        return self._inner.get_clock()

    def get_calendar(self, start: date, end: date) -> list[MarketSession]:
        return self._inner.get_calendar(start, end)

    def submit_bracket_order(self, approved: ApprovedOrder) -> OrderInfo:
        if not isinstance(approved, ApprovedOrder):
            raise OrderSubmissionError("submit_bracket_order accepts only ApprovedOrder")
        log_event(log, logging.INFO, "dry_run.order", **approved.model_dump(mode="json"))
        return OrderInfo(id=f"dry-{approved.client_order_id}", client_order_id=approved.client_order_id,
                         symbol=approved.symbol, side=approved.side.value, qty=approved.qty,
                         status="dry_run", order_class="bracket")

    def cancel_all_orders(self) -> None:
        log_event(log, logging.WARNING, "dry_run.cancel_all_orders")

    def close_all_positions(self) -> None:
        log_event(log, logging.WARNING, "dry_run.close_all_positions")


class IBKRBroker(BaseBroker):
    """Adapter stub showing how another broker plugs in.

    TODO(extend): implement with ``ib_async`` / the IBKR Client Portal API.
    Bracket orders map to a parent + attached STP and LMT children with OCA.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_account(self) -> AccountState:
        raise NotImplementedError("IBKRBroker.get_account")

    def get_positions(self) -> list[Position]:
        raise NotImplementedError("IBKRBroker.get_positions")

    def get_open_orders(self) -> list[OrderInfo]:
        raise NotImplementedError("IBKRBroker.get_open_orders")

    def get_clock(self) -> Clock:
        raise NotImplementedError("IBKRBroker.get_clock")

    def get_calendar(self, start: date, end: date) -> list[MarketSession]:
        raise NotImplementedError("IBKRBroker.get_calendar")

    def submit_bracket_order(self, approved: ApprovedOrder) -> OrderInfo:
        raise NotImplementedError("IBKRBroker.submit_bracket_order")

    def cancel_all_orders(self) -> None:
        raise NotImplementedError("IBKRBroker.cancel_all_orders")

    def close_all_positions(self) -> None:
        raise NotImplementedError("IBKRBroker.close_all_positions")


def build_broker(settings: Settings) -> BaseBroker:
    if settings.broker == "alpaca":
        return AlpacaBroker(settings)
    if settings.broker == "ibkr":
        return IBKRBroker(settings)
    raise ConfigError(f"unknown broker {settings.broker!r}")
