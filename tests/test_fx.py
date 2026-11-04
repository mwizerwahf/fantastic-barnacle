from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fx.config import FxSettings
from fx.console import ConsoleService, make_handler
from fx.crypto import SecretBox, new_key
from fx.db import FxStore, StoreError
from fx.engine import Engine
from fx.indicators import atr, ema, rsi
from fx.models import Candle, Direction, InstrumentSpec, Price, RiskConfig, Signal, StrategyRecord, StrategyStatus
from fx.oanda import OandaError, OrderRejected
from fx.risk import HARD_MAX_RISK_PCT, Reject, RejectReason, Session, fx_session, plan_trade, trading_day_start

EURUSD = InstrumentSpec(name="EUR_USD", pip_location=-4, display_precision=5, units_precision=0, min_units=1)
USDJPY = InstrumentSpec(name="USD_JPY", pip_location=-2, display_precision=3, units_precision=0, min_units=1)
WED = datetime(2026, 11, 4, 14, 0, tzinfo=UTC)  # a Wednesday, market open


def price(bid: float = 1.10000, ask: float = 1.10010, conv: float = 1.0, inst: str = "EUR_USD") -> Price:
    return Price(instrument=inst, bid=bid, ask=ask, time=WED, conv_gain=conv, conv_loss=conv)


# ================================================================ models
def test_risk_config_requires_exactly_one_stop() -> None:
    with pytest.raises(ValidationError):
        RiskConfig(sl_pips=None, sl_amount=None)
    with pytest.raises(ValidationError):
        RiskConfig(sl_pips=10, sl_amount=5)
    with pytest.raises(ValidationError):
        RiskConfig(tp_pips=10, tp_amount=5)
    with pytest.raises(ValidationError):
        RiskConfig(sizing="risk_pct", sl_pips=None, sl_amount=5)


def test_names_and_instruments_are_normalised() -> None:
    rec = StrategyRecord(name="  jean-paul  ", kind="ema_cross", account_id=1, instruments=["eurusd", "GBP/USD", "EUR_USD"])
    assert rec.name == "jean-paul" and rec.instruments == ["EUR_USD", "GBP_USD"]
    for bad in ("x", "R2D2", "a" * 31):
        with pytest.raises(ValidationError):
            StrategyRecord(name=bad, kind="ema_cross", account_id=1, instruments=["EUR_USD"])


# ================================================================ sizing / exits
def test_pip_exits_and_price_bound_long_and_short() -> None:
    rc = RiskConfig(units=10_000, sl_pips=20, tp_pips=30, max_slippage_pips=1)
    long = plan_trade(rc, Signal(direction=Direction.LONG, reason="t"), price(), EURUSD, balance=10_000, open_count=0,
                      opposite_on_netting_account=False)
    assert (long.units, long.entry_ref, long.sl_price, long.tp_price, long.price_bound) == (10_000, 1.1001, 1.0981, 1.1031, 1.1002)
    assert long.risk_amount == 20.0  # 10k units * 20 pips * 0.0001
    short = plan_trade(rc, Signal(direction=Direction.SHORT, reason="t"), price(), EURUSD, balance=10_000, open_count=0,
                       opposite_on_netting_account=False)
    assert (short.units, short.sl_price, short.tp_price, short.price_bound) == (-10_000, 1.102, 1.097, 1.0999)


def test_amount_exits_convert_to_prices() -> None:
    # $15 stop / $45 target on 5,000 units of USD_JPY where 1 JPY = 0.0067 USD
    rc = RiskConfig(units=5000, sl_pips=None, sl_amount=15, tp_pips=None, tp_amount=45)
    p = price(150.000, 150.010, conv=0.0067, inst="USD_JPY")
    plan = plan_trade(rc, Signal(direction=Direction.LONG, reason="t"), p, USDJPY, balance=5000, open_count=0,
                      opposite_on_netting_account=False)
    assert plan.risk_amount == pytest.approx(15, abs=0.01)
    assert plan.sl_pips == pytest.approx(44.8, abs=0.1) and plan.tp_pips == pytest.approx(134.3, abs=0.1)


def test_risk_pct_sizing_and_hard_cap() -> None:
    rc = RiskConfig(sizing="risk_pct", risk_pct=1.0, sl_pips=25)
    plan = plan_trade(rc, Signal(direction=Direction.LONG, reason="t"), price(), EURUSD, balance=2000, open_count=0,
                      opposite_on_netting_account=False)
    assert plan.units == 8000 and plan.risk_amount == pytest.approx(20)
    # fixed units whose stop would lose more than the hard cap -> rejected, never shrunk silently
    big = RiskConfig(units=100_000, sl_pips=50)
    with pytest.raises(Reject) as exc:
        plan_trade(big, Signal(direction=Direction.LONG, reason="t"), price(), EURUSD, balance=1000, open_count=0,
                   opposite_on_netting_account=False)
    assert exc.value.reason is RejectReason.RISK and f"{HARD_MAX_RISK_PCT:g}%" in exc.value.detail
    # a signal's ATR-based stop overrides the configured one
    atr_plan = plan_trade(RiskConfig(units=1000, sl_pips=20), Signal(direction=Direction.LONG, reason="t", sl_pips=12.5),
                          price(), EURUSD, balance=1000, open_count=0, opposite_on_netting_account=False)
    assert atr_plan.sl_pips == 12.5


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"p": price(1.1000, 1.1005)}, RejectReason.SPREAD),
    ({"open_count": 1}, RejectReason.MAX_OPEN),
    ({"opposite": True}, RejectReason.HEDGING),
    ({"p": Price(instrument="EUR_USD", bid=1.1, ask=1.1001, time=WED)}, RejectReason.NO_CONVERSION),
    ({"p": Price(instrument="EUR_USD", bid=1.1, ask=1.1001, time=WED, tradeable=False, conv_gain=1, conv_loss=1)},
     RejectReason.NOT_TRADEABLE),
])
def test_rejections(kwargs: dict[str, Any], reason: RejectReason) -> None:
    with pytest.raises(Reject) as exc:
        plan_trade(RiskConfig(), Signal(direction=Direction.LONG, reason="t"), kwargs.get("p", price()), EURUSD,
                   balance=10_000, open_count=kwargs.get("open_count", 0),
                   opposite_on_netting_account=kwargs.get("opposite", False))
    assert exc.value.reason is reason


def test_sessions_and_trading_day() -> None:
    ny = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731  (UTC times; New York is UTC-5 in November)
    assert fx_session(ny(2026, 11, 4, 14)) is Session.OPEN
    assert fx_session(ny(2026, 11, 6, 21, 35)) is Session.CLOSING          # Fri 16:35 NY
    assert fx_session(ny(2026, 11, 6, 21, 55)) is Session.WEEKEND_CLOSE    # Fri 16:55 NY
    assert fx_session(ny(2026, 11, 7, 12)) is Session.CLOSED               # Saturday
    assert fx_session(ny(2026, 11, 8, 22, 1)) is Session.CLOSED            # Sun 17:01 NY
    assert fx_session(ny(2026, 11, 8, 22, 10)) is Session.OPEN
    assert trading_day_start(ny(2026, 11, 4, 14)) == ny(2026, 11, 3, 22)   # 17:00 NY the day before
    assert trading_day_start(ny(2026, 11, 4, 23)) == ny(2026, 11, 4, 22)


def test_indicators() -> None:
    assert ema([1, 2, 3, 4, 5], 3)[-1] == pytest.approx(4.0)
    assert rsi([float(i) for i in range(30)], 14)[-1] == 100.0
    assert atr([2.0] * 20, [1.0] * 20, [1.5] * 20, 14)[-1] == pytest.approx(1.0)


# ================================================================ storage
def _store(tmp_path: Path) -> FxStore:
    return FxStore(tmp_path / "fx.db")


def _account(store: FxStore, box: SecretBox, label: str = "Main") -> int:
    return store.add_account(label=label, environment="practice", oanda_account_id=f"101-004-123456{len(label)}-001",
                             token_enc=box.seal("tok-" + "x" * 30), token_hint="…xxxx")


def test_names_are_unique_forever_and_edits_are_versioned(tmp_path: Path) -> None:
    store, box = _store(tmp_path), SecretBox(new_key())
    acc = _account(store, box)
    rec = StrategyRecord(name="Amara", kind="ema_cross", account_id=acc, instruments=["EUR_USD"],
                         params={"fast": 9, "slow": 21})
    store.create_strategy(rec)
    with pytest.raises(StoreError):
        store.create_strategy(rec.model_copy(update={"name": "amara"}))   # case-insensitive
    same = store.update_strategy(rec.model_copy(update={"notes": "just a note"}))
    assert same.version == 1                                             # notes don't change trading
    v2 = store.update_strategy(rec.model_copy(update={"params": {"fast": 5, "slow": 21}}), "faster EMA")
    assert v2.version == 2 and [v["version"] for v in store.strategy_versions("Amara")] == [2, 1]
    store.set_strategy_status("Amara", StrategyStatus.RETIRED)
    with pytest.raises(StoreError):
        store.create_strategy(rec)
    with pytest.raises(StoreError):
        store.update_strategy(rec)


def test_secrets_are_encrypted_at_rest(tmp_path: Path) -> None:
    store, box = _store(tmp_path), SecretBox(new_key())
    _account(store, box)
    raw = sqlite3.connect(tmp_path / "fx.db").execute("SELECT token_enc FROM accounts").fetchone()[0]
    assert "tok-" not in raw and box.open(raw).startswith("tok-")
    assert "token_enc" not in store.list_accounts()[0]


# ================================================================ engine with a fake OANDA
def candles_for_cross(n: int = 120, end: datetime = WED, secs: int = 300) -> list[Candle]:
    """Downtrend then a sharp rally on the last candle: EMA9 crosses above EMA21 exactly at the end."""
    closes = [1.1100 - i * 0.0002 for i in range(n - 1)] + [1.1100 - (n - 2) * 0.0002 + 0.0120]
    start = end - timedelta(seconds=secs * n)
    return [Candle(time=start + timedelta(seconds=secs * i), o=c, h=c + 0.0003, l=c - 0.0003, c=c, volume=100)
            for i, c in enumerate(closes)]


class FakeOanda:
    def __init__(self, hedging: bool = True) -> None:
        self.hedging = hedging
        self.candle_data: dict[str, list[Candle]] = {}
        self.orders: list[dict[str, Any]] = []
        self.trades: dict[str, dict[str, Any]] = {}
        self.unrealized: dict[str, float] = {}
        self.fail_next_order: Exception | None = None
        self.calls: list[str] = []
        self._next = 100

    def close(self) -> None: ...

    def summary(self) -> dict[str, Any]:
        self.calls.append("summary")
        return {"currency": "USD", "balance": 10_000.0, "nav": 10_000.0, "unrealized_pl": 0.0,
                "margin_available": 10_000.0, "open_trades": len(self.trades), "hedging": self.hedging}

    def instruments(self) -> dict[str, InstrumentSpec]:
        return {"EUR_USD": EURUSD, "USD_JPY": USDJPY}

    def candles(self, instrument: str, granularity: str, *, count: int | None = None, since: datetime | None = None):
        self.calls.append(f"candles:{instrument}")
        data = self.candle_data.get(instrument, [])
        return [k for k in data if since is None or k.time >= since]

    def pricing(self, instruments):
        self.calls.append("pricing")
        return {i: price(inst=i) for i in instruments}

    def market_order(self, **kw: Any) -> dict[str, Any]:
        self.orders.append(kw)
        if self.fail_next_order is not None:
            exc, self.fail_next_order = self.fail_next_order, None
            if isinstance(exc, TimeoutError):  # the order went through but the reply was lost
                self._open(kw)
            raise exc
        return self._open(kw)

    def _open(self, kw: dict[str, Any]) -> dict[str, Any]:
        self._next += 1
        tid = str(self._next)
        fill_price = 1.10012 if kw["units"] > 0 else 1.09998
        self.trades[tid] = {"id": tid, "instrument": kw["instrument"], "price": str(fill_price), "state": "OPEN",
                            "initialUnits": str(kw["units"]), "currentUnits": str(kw["units"]),
                            "clientExtensions": {"id": kw["client_id"]}, "openTime": WED.isoformat()}
        return {"trade_id": tid, "price": fill_price, "units": kw["units"], "time": None}

    def close_trade(self, trade_id: str) -> dict[str, Any]:
        t = self.trades[trade_id]
        t["state"] = "CLOSED"
        return {"price": 1.1005, "pnl": self.unrealized.get(trade_id, 3.0), "financing": 0.0, "time": None}

    def trade(self, specifier: str) -> dict[str, Any] | None:
        if specifier.startswith("@"):
            return next((t for t in self.trades.values() if t["clientExtensions"]["id"] == specifier[1:]), None)
        return self.trades.get(specifier)

    def trades_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        return [self.trades[i] for i in ids if i in self.trades]

    def open_trades(self) -> list[dict[str, Any]]:
        self.calls.append("open_trades")
        return [t | {"unrealizedPL": str(self.unrealized.get(t["id"], 0.0)), "financing": "0"}
                for t in self.trades.values() if t["state"] == "OPEN"]

    def stream_transactions(self, on_transaction, stop: threading.Event) -> None:
        stop.wait()


class FakeAI:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def complete_json(self, system: str, user: str) -> str:
        self.calls += 1
        return self.reply


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[dict[str, Any]]:
    settings = FxSettings(_env_file=None, db_path=tmp_path / "fx.db", bar_publish_delay_s=0)  # type: ignore[call-arg]
    store, box = FxStore(settings.db_path), SecretBox(new_key())
    acc = _account(store, box)
    fake = FakeOanda()
    fake.candle_data["EUR_USD"] = candles_for_cross()
    clock = {"now": WED}
    ai = FakeAI('{"approve": true, "confidence": 0.8, "reason": "trend"}')
    engine = Engine(settings, store, box, client_factory=lambda env, a, tok: fake,  # type: ignore[arg-type,return-value]
                    ai_factory=lambda model, **kw: ai, clock=lambda: clock["now"])
    yield {"engine": engine, "store": store, "fake": fake, "acc": acc, "clock": clock, "ai": ai, "box": box}
    engine.stop()


def _strategy(store: FxStore, acc: int, name: str, **kw: Any) -> None:
    store.create_strategy(StrategyRecord(name=name, kind="ema_cross", account_id=acc, instruments=["EUR_USD"],
                                         granularity="M5", params={"fast": 9, "slow": 21},
                                         risk=kw.pop("risk", RiskConfig(units=1000, sl_pips=20, tp_pips=30)), **kw))


def _bar(rig: dict[str, Any]) -> None:
    """Arm the schedule, then move past the next candle close and run once."""
    e: Engine = rig["engine"]
    e.reload()
    e.run_due_bars(WED - timedelta(seconds=1))
    e.run_due_bars(WED + timedelta(seconds=5))


def _next_cross(rig: dict[str, Any]) -> None:
    rig["fake"].candle_data["EUR_USD"] = candles_for_cross(end=WED + timedelta(seconds=300))
    rig["engine"].run_due_bars(WED + timedelta(seconds=305))


def test_two_strategies_open_their_own_trades_on_the_same_pair(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara")
    _strategy(rig["store"], rig["acc"], "Kofi", risk=RiskConfig(units=2000, sl_pips=None, sl_amount=4, tp_pips=10))
    _bar(rig)
    fake: FakeOanda = rig["fake"]
    assert len(fake.orders) == 2
    assert fake.calls.count("candles:EUR_USD") == 1            # one shared fetch for both strategies
    by_tag = {o["tag"]: o for o in fake.orders}
    assert by_tag["fx:Amara"]["sl_price"] == 1.0981 and by_tag["fx:Amara"]["tp_price"] == 1.1031
    assert by_tag["fx:Kofi"]["sl_price"] == 1.0981            # $4 on 2,000 units = 20 pips
    rows = rig["store"].open_trades()
    assert {r["strategy"] for r in rows} == {"Amara", "Kofi"} and all(r["broker_trade_id"] for r in rows)


def test_stream_close_records_pnl_and_pips(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara")
    _bar(rig)
    row = rig["store"].open_trades()[0]
    rig["engine"]._on_transaction(rig["acc"], {"type": "ORDER_FILL", "reason": "TAKE_PROFIT_ORDER", "time": WED.isoformat(),
                                               "tradesClosed": [{"tradeID": row["broker_trade_id"], "price": "1.10312",
                                                                 "realizedPL": "3.0", "financing": "-0.02"}]})
    closed = rig["store"].trades(status="closed")[0]
    assert (closed["pnl"], closed["pips"], closed["close_reason"]) == (2.98, 30.0, "take_profit")
    # the same fill reported twice (stream + reconcile) is recorded once
    rig["engine"]._record_close(rig["acc"], row["broker_trade_id"], price=1.1, pnl=99, financing=0, closed_at=None,
                                reason="x")
    assert rig["store"].trades(status="closed")[0]["pnl"] == 2.98


def test_paused_strategy_and_max_open_trades(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara", status=StrategyStatus.PAUSED)
    _bar(rig)
    assert rig["fake"].orders == []
    rig["store"].set_strategy_status("Amara", StrategyStatus.ACTIVE)
    _bar(rig)
    _next_cross(rig)                               # a fresh cross on the next candle, 1 already open
    assert len(rig["fake"].orders) == 1
    assert rig["store"].events(names={"risk.rejected"})[0]["reason"] == "max_open_trades"


def test_ai_gate_and_per_strategy_model(rig: dict[str, Any]) -> None:
    store: FxStore = rig["store"]
    gem = store.add_llm_profile(label="Gemini", model="gemini/gemini-3.8-flash", api_key_enc=rig["box"].seal("k" * 20),
                                api_key_hint="…kkkk", api_base=None, is_default=True)
    _strategy(store, rig["acc"], "Amara", ai_enabled=True, llm_profile_id=gem)
    rig["ai"].reply = '{"approve": false, "confidence": 0.9, "reason": "choppy"}'
    rig["engine"].reload()
    rig["engine"]._ai_pool = _Inline()  # run AI synchronously in the test
    _bar(rig)
    assert rig["ai"].calls == 1 and rig["fake"].orders == []
    assert store.events(names={"risk.rejected"})[0]["reason"] == "ai_rejected"


class _Inline:
    def submit(self, fn, *args):  # noqa: ANN001, ANN201
        fn(*args)

    def shutdown(self, **kw: Any) -> None: ...


def test_ambiguous_order_is_recovered_by_client_id(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara")
    rig["fake"].fail_next_order = TimeoutError("reply lost")
    _bar(rig)
    rows = rig["store"].open_trades()
    assert len(rows) == 1 and rows[0]["broker_trade_id"] == "101"


def test_broker_rejection_marks_trade_failed(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara")
    rig["fake"].fail_next_order = OrderRejected("order not filled: BOUNDS_VIOLATION")
    _bar(rig)
    assert rig["store"].trades(status="failed")[0]["error"].endswith("BOUNDS_VIOLATION")


def test_basket_take_profit_closes_all_of_that_strategy_only(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara",
              risk=RiskConfig(units=1000, sl_pips=20, tp_pips=30, basket_tp_amount=5))
    _strategy(rig["store"], rig["acc"], "Kofi")
    _bar(rig)
    fake: FakeOanda = rig["fake"]
    amara = rig["store"].open_trades(strategy="Amara")[0]["broker_trade_id"]
    fake.unrealized = {amara: 6.0}
    rig["engine"].watch_baskets(WED)
    assert [t["strategy"] for t in rig["store"].open_trades()] == ["Kofi"]
    assert rig["store"].trades(status="closed")[0]["close_reason"] == "basket_take_profit"


def test_daily_loss_limit_halts_the_strategy(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara",
              risk=RiskConfig(units=1000, sl_pips=20, tp_pips=30, daily_loss_limit=4, max_open_trades=3))
    _bar(rig)
    rig["fake"].unrealized = {rig["store"].open_trades()[0]["broker_trade_id"]: -4.5}
    rig["engine"].watch_baskets(WED)
    assert rig["store"].open_trades() == [] and rig["engine"]._halted("Amara", WED)
    _next_cross(rig)
    assert len(rig["fake"].orders) == 1
    assert rig["store"].events(names={"risk.rejected"})[0]["reason"] == "daily_loss_limit"


def test_netting_account_blocks_opposite_side(rig: dict[str, Any]) -> None:
    fake: FakeOanda = rig["fake"]
    fake.hedging = False
    fake.trades["9"] = {"id": "9", "instrument": "EUR_USD", "price": "1.1", "state": "OPEN", "initialUnits": "-500",
                        "currentUnits": "-500", "clientExtensions": {"id": "manual"}}
    _strategy(rig["store"], rig["acc"], "Amara")
    _bar(rig)
    assert fake.orders == [] and rig["store"].events(names={"risk.rejected"})[0]["reason"] == "account_not_hedging"


def test_live_account_is_never_traded_without_allow_live(tmp_path: Path) -> None:
    settings = FxSettings(_env_file=None, db_path=tmp_path / "fx.db")  # type: ignore[call-arg]
    store, box = FxStore(settings.db_path), SecretBox(new_key())
    acc = store.add_account(label="Real", environment="live", oanda_account_id="001-004-1234567-001",
                            token_enc=box.seal("t" * 30), token_hint="…tttt")
    made: list[str] = []
    engine = Engine(settings, store, box, client_factory=lambda *a: made.append("x"))  # type: ignore[arg-type,return-value]
    assert engine._ensure_account(acc) is None and made == []
    assert "FX_ALLOW_LIVE" in store.list_accounts()[0]["last_error"]


def test_weekend_guard_closes_flagged_strategies(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara")
    _strategy(rig["store"], rig["acc"], "Kofi", risk=RiskConfig(units=1000, close_before_weekend=False))
    _bar(rig)
    rig["engine"].weekend_guard(datetime(2026, 11, 6, 21, 55, tzinfo=UTC))
    assert [t["strategy"] for t in rig["store"].open_trades()] == ["Kofi"]


def test_close_command_from_console(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara")
    _bar(rig)
    row = rig["store"].open_trades()[0]
    cid = rig["store"].enqueue_command("close_trade", str(row["id"]))
    rig["engine"].process_commands()
    assert rig["store"].get_command(cid)["status"] == "done"
    assert rig["store"].trades(status="closed")[0]["close_reason"] == "closed_from_console"


def test_closed_trade_404_triggers_reconcile(rig: dict[str, Any]) -> None:
    _strategy(rig["store"], rig["acc"], "Amara")
    _bar(rig)
    row = rig["store"].open_trades()[0]
    fake: FakeOanda = rig["fake"]
    fake.trades[row["broker_trade_id"]] |= {"state": "CLOSED", "realizedPL": "-2.0", "averageClosePrice": "1.0981"}
    fake.close_trade = lambda tid: (_ for _ in ()).throw(OandaError("gone", 404))  # type: ignore[method-assign]
    assert rig["engine"].close_trade(row, "x") is True
    assert rig["store"].trades(status="closed")[0]["pnl"] == -2.0


# ================================================================ console
@pytest.fixture
def console(tmp_path: Path) -> Iterator[dict[str, Any]]:
    settings = FxSettings(_env_file=None, db_path=tmp_path / "fx.db")  # type: ignore[call-arg]
    store, box = FxStore(settings.db_path), SecretBox(new_key())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ConsoleService(settings, store, box)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "store": store, "box": box}
    httpd.shutdown()
    httpd.server_close()


def _call(url: str, body: dict[str, Any] | None = None, *, csrf: bool = True,
          origin: str | None = None) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["X-FX-Request"] = "1"
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers,
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if resp.headers["Content-Type"].startswith("application/json") else raw
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_console_onboarding_flow(console: dict[str, Any]) -> None:
    u = console["url"]
    token = "abcdef0123456789abcdef0123456789-ffffffffffffffff"
    status, res = _call(u + "/api/accounts", {"label": "Practice 1", "environment": "practice",
                                              "oanda_account_id": "101-004-1234567-001", "token": token})
    assert status == 200 and res["id"] == 1
    accounts = _call(u + "/api/accounts")[1]
    assert token not in json.dumps(accounts) and accounts["accounts"][0]["token_hint"] == "…ffff"
    assert console["store"].get_command(res["command"])["kind"] == "check_account"

    assert _call(u + "/api/llms", {"label": "Gemini", "model": "gemini/gemini-3.8-flash", "api_key": "k" * 30})[0] == 200
    assert "k" * 30 not in json.dumps(_call(u + "/api/llms")[1])

    body = {"name": "Amara", "kind": "breakout", "account_id": 1, "instruments": "EURUSD, gbp_usd",
            "granularity": "M15", "params": {"channel": 30}, "risk": {"sl_pips": 15, "tp_pips": 45},
            "ai_enabled": True, "llm_profile_id": 1}
    status, res = _call(u + "/api/strategies", body)
    assert status == 200 and res["strategy"]["instruments"] == ["EUR_USD", "GBP_USD"]
    assert res["strategy"]["params"]["atr_period"] == 14            # defaults filled in
    assert _call(u + "/api/strategies", body)[0] == 400              # name taken
    status, err = _call(u + "/api/strategies", body | {"name": "Elena", "params": {"channel": 1}})
    assert status == 400 and "channel" in err["error"]
    status, err = _call(u + "/api/strategies", body | {"name": "Elena", "risk": {"sl_pips": None}})
    assert status == 400 and "stop-loss" in err["error"]

    status, res = _call(u + "/api/strategies/update", body | {"params": {"channel": 40}, "change_note": "wider"})
    assert status == 200 and res["strategy"]["version"] == 2
    status, res = _call(u + "/api/strategies/duplicate", {"name": "Amara", "new_name": "Elena"})
    assert status == 200 and res["strategy"]["status"] == "paused" and res["strategy"]["params"]["channel"] == 40

    detail = _call(u + "/api/strategy?name=Amara")[1]
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    assert _call(u + "/api/overview")[1]["counts"]["strategies"] == 2
    meta = _call(u + "/api/meta")[1]
    assert {c["kind"] for c in meta["catalog"]} == {"ema_cross", "breakout", "rsi_reversion"}


def test_console_refuses_cross_site_writes(console: dict[str, Any]) -> None:
    u = console["url"]
    body = {"kind": "close_strategy", "target": "Amara"}
    assert _call(u + "/api/commands", body, csrf=False)[0] == 403
    assert _call(u + "/api/commands", body, origin="https://evil.example")[0] == 403
    host = u.removeprefix("http://")
    assert _call(u + "/api/commands", body, origin=f"http://{host}")[0] == 200
    assert _call(u + "/static/../fx/db.py")[0] == 404


# ================================================================ OANDA client wire format
def test_oanda_client_requests_and_parsing() -> None:
    import httpx

    from fx.oanda import OandaClient

    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path.endswith("/candles"):
            return httpx.Response(200, json={"candles": [
                {"complete": True, "volume": 12, "time": "2026-11-04T13:55:00.000000000Z",
                 "mid": {"o": "1.10000", "h": "1.10100", "l": "1.09900", "c": "1.10050"}},
                {"complete": False, "volume": 3, "time": "2026-11-04T14:00:00.000000000Z",
                 "mid": {"o": "1.1", "h": "1.1", "l": "1.1", "c": "1.1"}}]})
        if path.endswith("/pricing"):
            return httpx.Response(200, json={
                "prices": [{"instrument": "USD_JPY", "time": "2026-11-04T14:00:01.123456789Z", "tradeable": True,
                            "bids": [{"price": "150.010"}], "asks": [{"price": "150.020"}]}],
                "homeConversions": [{"currency": "JPY", "accountGain": "0.00666", "accountLoss": "0.00667",
                                     "positionValue": "0.0067"}]})
        if path.endswith("/orders"):
            return httpx.Response(201, json={"orderFillTransaction": {
                "time": "2026-11-04T14:00:02.000000000Z", "tradeOpened": {"tradeID": "77", "units": "-5000", "price": "150.008"}}})
        return httpx.Response(404, json={"errorMessage": "nope"})

    c = OandaClient("practice", "101-004-1-001", "tok", transport=httpx.MockTransport(handler), attempts=1)
    candles = c.candles("EUR_USD", "M5", count=2)
    assert len(candles) == 1 and candles[0].c == 1.1005 and candles[0].time == datetime(2026, 11, 4, 13, 55, tzinfo=UTC)
    assert seen[-1].url.params["alignmentTimezone"] == "UTC" and seen[-1].headers["Authorization"] == "Bearer tok"
    p = c.pricing(["USD_JPY"])["USD_JPY"]
    assert (p.bid, p.ask, p.conv_gain, p.conv_loss) == (150.01, 150.02, 0.00666, 0.00667)
    fill = c.market_order(instrument="USD_JPY", units=-5000, client_id="fx-amara-1", tag="fx:Amara", comment="Amara v1",
                          sl_price=150.25, tp_price=149.6, price_bound=149.99, precision=3)
    assert fill == {"trade_id": "77", "price": 150.008, "units": -5000.0, "time": "2026-11-04T14:00:02.000000000Z"}
    order = json.loads(seen[-1].content)["order"]
    assert order["units"] == "-5000" and order["timeInForce"] == "FOK" and order["priceBound"] == "149.990"
    assert order["stopLossOnFill"]["price"] == "150.250" and order["takeProfitOnFill"]["price"] == "149.600"
    assert order["tradeClientExtensions"]["id"] == "fx-amara-1" != order["clientExtensions"]["id"]
    assert c.trade("@missing") is None
    with pytest.raises(OandaError):
        c.close_trade("1")
