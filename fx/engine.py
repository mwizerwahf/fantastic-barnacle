"""The forex engine: one process, many named strategies, one or more OANDA accounts.

Main loop (1 s tick):
  commands from the console -> config reload -> strategies whose candle just closed ->
  basket / daily-loss watch (only while needed) -> weekend guard -> reconcile -> heartbeat.

API usage is kept to what decisions need:
  * candles: one request per (pair, timeframe) per closed candle, shared by every strategy on it,
    incremental from the SQLite cache;
  * a fresh price only when a strategy actually wants to trade;
  * one transaction stream per account delivers stop-loss / take-profit fills as they happen;
  * open-trade P/L polling only while a strategy with a $ basket or daily-loss limit has trades open.

Exits are broker-side (SL/TP attached to every entry), so stopping the engine leaves every trade
protected. The engine never closes positions on shutdown.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from core.observability import log_event, new_correlation_id
from fx import ai as fx_ai
from fx.config import FxSettings
from fx.crypto import SecretBox
from fx.db import FxStore
from fx.models import GRANULARITY_SECONDS, Candle, InstrumentSpec, Signal, StrategyRecord, StrategyStatus
from fx.oanda import OandaClient, OandaError, OrderRejected, parse_time
from fx.risk import Reject, RejectReason, Session, fx_session, plan_trade, trading_day_start
from fx.strategies import BarContext, FxStrategy, build

log = logging.getLogger(__name__)

CLOSE_REASONS = {
    "STOP_LOSS_ORDER": "stop_loss", "TAKE_PROFIT_ORDER": "take_profit", "TRAILING_STOP_LOSS_ORDER": "trailing_stop",
    "MARKET_ORDER_TRADE_CLOSE": "closed_at_broker", "MARKET_ORDER_MARGIN_CLOSEOUT": "margin_closeout",
    "MARKET_ORDER_POSITION_CLOSEOUT": "position_closeout", "LINKED_TRADE_CLOSE": "linked_close",
}
HEARTBEAT_S = 10.0
CANDLES_IN_MEMORY = 600
NEW_CANDLE_GRACE_S = 90.0     # give up waiting for a candle this long after its close
PENDING_GIVE_UP_S = 120.0

ClientFactory = Callable[[str, str, str], OandaClient]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())[:20] or "s"


class Engine:
    def __init__(self, settings: FxSettings, store: FxStore, box: SecretBox, *,
                 client_factory: ClientFactory | None = None,
                 ai_factory: Callable[..., fx_ai.AIProvider] = fx_ai.build_provider,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.s = settings
        self.store = store
        self.box = box
        self._client_factory = client_factory or (lambda env, acct, token: OandaClient(
            env, acct, token, timeout=settings.http_timeout_s, attempts=settings.http_attempts,
            backoff=settings.http_backoff_s))
        self._ai_factory = ai_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event()
        self._exec_lock = threading.Lock()   # plan + submit one order at a time: exact open counts

        self._clients: dict[int, OandaClient] = {}
        self._accounts: dict[int, dict[str, Any]] = {}       # meta + cached summary
        self._specs: dict[int, dict[str, InstrumentSpec]] = {}
        self._streams: dict[int, threading.Thread] = {}
        self._strategies: dict[str, tuple[StrategyRecord, FxStrategy]] = {}
        self._invalid: dict[str, str] = {}                     # name -> config error
        self._last_bucket: dict[tuple[str, str], int] = {}
        self._candles: dict[tuple[str, str], list[Candle]] = {}
        self._last_bar: dict[str, str] = {}
        self._close_attempt: dict[str, float] = {}
        self._ai_pool = ThreadPoolExecutor(max_workers=settings.ai_workers, thread_name_prefix="fx-ai")
        self._timers: dict[str, float] = defaultdict(float)
        self._started = time.monotonic()

    # ================================================================ lifecycle
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._event("engine.start", allow_live=self.s.allow_live)
        self.reload()
        self.reconcile_all()
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(1.0)
        finally:
            self._event("engine.stop", note="open trades keep their broker-side stop-loss / take-profit")
            self._ai_pool.shutdown(wait=False, cancel_futures=True)
            self._heartbeat(stopped=True)
            for client in self._clients.values():
                client.close()

    def tick(self) -> None:
        now = self._clock()
        new_correlation_id()
        steps: list[tuple[str, float, Callable[[], None]]] = [
            ("commands", 0, self.process_commands),
            ("reload", self.s.reload_s, self.reload),
            ("bars", 0, lambda: self.run_due_bars(now)),
            ("basket", self.s.basket_poll_s, lambda: self.watch_baskets(now)),
            ("weekend", 0, lambda: self.weekend_guard(now)),
            ("reconcile", self.s.reconcile_s, self.reconcile_all),
            ("heartbeat", HEARTBEAT_S, self._heartbeat),
            ("prune", 86400, self.store.prune),
        ]
        mono = time.monotonic()
        for name, every, fn in steps:
            if every and mono - self._timers[name] < every:
                continue
            self._timers[name] = mono
            try:
                fn()
            except Exception:
                log.exception("fx.step_failed", extra={"fields": {"step": name}})

    # ================================================================ events
    def _event(self, event: str, level: int = logging.INFO, *, strategy: str | None = None,
               instrument: str | None = None, **fields: Any) -> None:
        log_event(log, level, f"fx.{event}", strategy=strategy, instrument=instrument, **fields)
        try:
            self.store.add_event(event, level=logging.getLevelName(level), strategy=strategy, instrument=instrument,
                                 **fields)
        except Exception:
            log.exception("fx.event_write_failed")

    # ================================================================ accounts
    def _ensure_account(self, account_id: int, *, force: bool = False) -> OandaClient | None:
        if account_id in self._clients and not force:
            return self._clients[account_id]
        secret = self.store.account_secret(account_id)
        if not secret:
            return None
        meta = {"id": account_id, "label": secret["label"], "environment": secret["environment"],
                "summary": None, "summary_at": 0.0, "error": None, "stream": "starting"}
        if secret["environment"] == "live" and not self.s.allow_live:
            meta["error"] = "live account: set FX_ALLOW_LIVE=true on the server to trade it"
            self._accounts[account_id] = meta
            self.store.update_account_check(account_id, currency=None, hedging=None, error=meta["error"])
            return None
        try:
            client = self._client_factory(secret["environment"], secret["oanda_account_id"],
                                          self.box.open(secret["token_enc"]))
            summary = client.summary()
            specs = client.instruments()
        except Exception as exc:
            meta["error"] = f"{type(exc).__name__}: {exc}"
            self._accounts[account_id] = meta
            self.store.update_account_check(account_id, currency=None, hedging=None, error=meta["error"])
            self._event("account.unavailable", logging.ERROR, account=secret["label"], error=meta["error"])
            return None
        old = self._clients.pop(account_id, None)
        if old is not None:
            old.close()
        meta.update(summary=summary, summary_at=time.monotonic(), currency=summary["currency"],
                    hedging=summary["hedging"])
        self._accounts[account_id] = meta
        self._clients[account_id] = client
        self._specs[account_id] = specs
        self.store.update_account_check(account_id, currency=summary["currency"], hedging=summary["hedging"],
                                        error=None)
        self._event("account.ready", account=secret["label"], environment=secret["environment"],
                    currency=summary["currency"], hedging=summary["hedging"], balance=summary["balance"])
        if account_id not in self._streams or not self._streams[account_id].is_alive():
            t = threading.Thread(target=self._stream_loop, args=(account_id,), daemon=True,
                                 name=f"fx-stream-{account_id}")
            self._streams[account_id] = t
            t.start()
        return client

    def _balance(self, account_id: int) -> float:
        meta = self._accounts[account_id]
        if meta["summary"] is None or time.monotonic() - meta["summary_at"] > self.s.account_refresh_s:
            meta["summary"] = self._clients[account_id].summary()
            meta["summary_at"] = time.monotonic()
        return float(meta["summary"]["balance"])

    def _refresh_summaries(self) -> None:
        for account_id, client in list(self._clients.items()):
            meta = self._accounts[account_id]
            if time.monotonic() - meta["summary_at"] < self.s.account_refresh_s:
                continue
            try:
                meta["summary"] = client.summary()
                meta["summary_at"] = time.monotonic()
                meta["error"] = None
            except Exception as exc:
                meta["error"] = f"{type(exc).__name__}: {exc}"

    # ================================================================ streams (exits as they happen)
    def _stream_loop(self, account_id: int) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            client = self._clients.get(account_id)
            if client is None:
                return
            try:
                self._accounts[account_id]["stream"] = "connected"
                client.stream_transactions(lambda t: self._on_transaction(account_id, t), self._stop)
                backoff = 1.0
            except Exception as exc:
                self._accounts[account_id]["stream"] = "reconnecting"
                log_event(log, logging.WARNING, "fx.stream.error", account=account_id, error=repr(exc))
            if self._stop.is_set():
                return
            try:
                self.reconcile_account(account_id)  # catch anything missed while disconnected
            except Exception:
                log.exception("fx.reconcile_failed")
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _on_transaction(self, account_id: int, txn: dict[str, Any]) -> None:
        if txn.get("type") != "ORDER_FILL":
            return
        reason = CLOSE_REASONS.get(txn.get("reason", ""), (txn.get("reason") or "closed").lower())
        for closed in txn.get("tradesClosed", []) or []:
            self._record_close(account_id, str(closed["tradeID"]), price=float(closed.get("price") or txn.get("price")),
                               pnl=float(closed.get("realizedPL", 0)), financing=float(closed.get("financing", 0)),
                               closed_at=txn.get("time"), reason=reason)
        reduced = txn.get("tradeReduced")
        if reduced:
            self._event("trade.reduced", logging.WARNING, trade_id=reduced.get("tradeID"), units=reduced.get("units"),
                        note="partial close at the broker; P/L recorded when the trade fully closes")

    def _record_close(self, account_id: int, broker_trade_id: str, *, price: float | None, pnl: float,
                      financing: float, closed_at: str | None, reason: str) -> None:
        row = self.store.mark_trade_closed(account_id, broker_trade_id, exit_price=price, pnl=pnl, financing=financing,
                                           closed_at=self._iso(closed_at), reason=reason)
        if row:
            if account_id in self._accounts:
                self._accounts[account_id]["summary_at"] = 0.0
            self._event("trade.closed", strategy=row["strategy"], instrument=row["instrument"], trade=row["id"],
                        direction=row["direction"], units=row["units"], pnl=row["pnl"], pips=row["pips"],
                        reason=reason)

    def _iso(self, value: str | None) -> str:
        if not value:
            return self._clock().isoformat(timespec="seconds")
        try:
            return parse_time(value).isoformat(timespec="seconds")
        except ValueError:
            return value

    # ================================================================ config
    def reload(self) -> None:
        seen: set[str] = set()
        for rec in self.store.list_strategies():
            if rec.status is StrategyStatus.RETIRED:
                continue
            seen.add(rec.name)
            current = self._strategies.get(rec.name)
            if current and current[0].version == rec.version and current[0].kind == rec.kind:
                self._strategies[rec.name] = (rec, current[1])   # status / notes may have changed
                continue
            try:
                strategy = build(rec.kind, rec.params)
            except Exception as exc:
                if self._invalid.get(rec.name) != str(exc):
                    self._event("strategy.invalid", logging.ERROR, strategy=rec.name, error=str(exc))
                self._invalid[rec.name] = str(exc)
                self._strategies.pop(rec.name, None)
                continue
            self._invalid.pop(rec.name, None)
            self._strategies[rec.name] = (rec, strategy)
            self._event("strategy.loaded", strategy=rec.name, kind=rec.kind, version=rec.version,
                        instruments=rec.instruments, granularity=rec.granularity, status=rec.status.value)
        for name in set(self._strategies) - seen:
            self._strategies.pop(name)
            self._event("strategy.unloaded", strategy=name)
        accounts = {rec.account_id for rec, _ in self._strategies.values()}
        accounts |= {t["account_id"] for t in self.store.open_trades()}
        for account_id in accounts:
            if account_id not in self._clients:
                # don't hammer a broken account: retry at most once a minute
                last = self._accounts.get(account_id, {}).get("tried", 0.0)
                if time.monotonic() - last < 60 and account_id in self._accounts:
                    continue
                self._ensure_account(account_id)
                self._accounts.setdefault(account_id, {})["tried"] = time.monotonic()
        self._refresh_summaries()

    # ================================================================ bars -> signals
    def _due_groups(self) -> dict[tuple[str, str], list[str]]:
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for name, (rec, _) in self._strategies.items():
            if rec.status is StrategyStatus.ACTIVE:
                for inst in rec.instruments:
                    groups[(inst, rec.granularity)].append(name)
        return groups

    def run_due_bars(self, now: datetime) -> None:
        for (inst, gran), names in self._due_groups().items():
            secs = GRANULARITY_SECONDS[gran]
            bucket = int((now.timestamp() - self.s.bar_publish_delay_s) // secs)
            key = (inst, gran)
            last = self._last_bucket.get(key)
            if last is None:
                self._last_bucket[key] = bucket   # start on the next close, never on stale data
                continue
            if bucket <= last:
                continue
            try:
                ready = self._process_group(inst, gran, names, now, bucket)
            except Exception as exc:
                ready = True
                self._event("data.error", logging.WARNING, instrument=inst, granularity=gran, error=repr(exc))
            if ready:
                self._last_bucket[key] = bucket

    def _data_client(self, names: list[str]) -> tuple[int, OandaClient] | None:
        for name in names:
            account_id = self._strategies[name][0].account_id
            if account_id in self._clients:
                return account_id, self._clients[account_id]
        return None

    def _candles_for(self, client: OandaClient, inst: str, gran: str, need: int) -> list[Candle]:
        key = (inst, gran)
        cached = self._candles.get(key) or self.store.load_candles(inst, gran, max(need, 50))
        secs = GRANULARITY_SECONDS[gran]
        if cached and (self._clock() - cached[-1].time).total_seconds() < secs * 450:
            fresh = client.candles(inst, gran, since=cached[-1].time, count=500)
        else:
            fresh = client.candles(inst, gran, count=need + 5)
            cached = []
        merged = {k.time: k for k in cached}
        known = set(merged)
        merged.update({k.time: k for k in fresh})
        out = [merged[t] for t in sorted(merged)][-max(need + 5, min(CANDLES_IN_MEMORY, need * 2)):]
        self._candles[key] = out
        self.store.save_candles(inst, gran, [k for k in fresh if k.time not in known])
        return out

    def _process_group(self, inst: str, gran: str, names: list[str], now: datetime, bucket: int) -> bool:
        """Returns False to retry next tick (the just-closed candle is not published yet)."""
        found = self._data_client(names)
        if found is None:
            return True
        account_id, client = found
        need = max(self._strategies[n][1].lookback for n in names) + 2
        candles = self._candles_for(client, inst, gran, need)
        secs = GRANULARITY_SECONDS[gran]
        expected = datetime.fromtimestamp((bucket - 1) * secs, UTC)
        if not candles or candles[-1].time < expected:
            waited = now.timestamp() - bucket * secs
            return waited > NEW_CANDLE_GRACE_S   # quiet market / weekend: skip this candle
        session = fx_session(now)
        for name in names:
            rec, strategy = self._strategies[name]
            spec = self._specs.get(rec.account_id, {}).get(inst) or self._specs[account_id].get(inst)
            if spec is None:
                self._event("strategy.unknown_instrument", logging.WARNING, strategy=name, instrument=inst)
                continue
            self._last_bar[name] = candles[-1].time.isoformat()
            if len(candles) < strategy.lookback:
                continue
            open_here = [t for t in self.store.open_trades(strategy=name) if t["instrument"] == inst]
            ctx = BarContext(instrument=inst, granularity=gran, candles=candles, spec=spec, now=now,
                             open_trades=open_here)
            try:
                signal = strategy.on_bar(ctx)
            except Exception as exc:
                self._event("strategy.error", logging.ERROR, strategy=name, instrument=inst, error=repr(exc))
                continue
            if signal is None:
                continue
            self._event("signal", strategy=name, instrument=inst, direction=signal.direction.value,
                        reason=signal.reason, version=rec.version)
            if session is not Session.OPEN:
                self._reject(name, inst, Reject(RejectReason.MARKET_CLOSED, f"forex session is {session.value}"))
                continue
            if self._halted(name, now):
                self._reject(name, inst, Reject(RejectReason.DAILY_LOSS, "daily loss limit hit; resumes at 17:00 New York"))
                continue
            if rec.ai_enabled:
                self._ai_pool.submit(self._ai_then_execute, name, ctx, signal)
            else:
                self.execute(name, ctx, signal)
        return True

    # ================================================================ AI confirmation
    def _ai_then_execute(self, name: str, ctx: BarContext, signal: Signal) -> None:
        new_correlation_id()
        try:
            rec, strategy = self._strategies[name]
            profile = self.store.llm_profile_secret(rec.llm_profile_id)
            if profile is None:
                self._reject(name, ctx.instrument, Reject(RejectReason.AI, "no AI model configured"))
                return
            api_key = self.box.open(profile["api_key_enc"]) if profile.get("api_key_enc") else None
            provider = self._ai_factory(profile["model"], api_key=api_key, api_base=profile.get("api_base"),
                                        timeout=self.s.ai_timeout_s, max_tokens=self.s.ai_max_tokens)
            verdict = fx_ai.confirm(provider, profile["model"], strategy.ai_context(ctx, signal), strategy=name)
            self._event("ai.verdict", strategy=name, instrument=ctx.instrument, model=profile["model"],
                        approve=verdict.approve, confidence=verdict.confidence, reason=verdict.reason)
            if not verdict.approve or verdict.confidence < rec.risk.ai_min_confidence:
                self._reject(name, ctx.instrument, Reject(
                    RejectReason.AI, f"{'approved' if verdict.approve else 'rejected'} at confidence "
                                     f"{verdict.confidence:.2f} (needs {rec.risk.ai_min_confidence:.2f})"))
                return
            current = self._strategies.get(name)
            if current is None or current[0].status is not StrategyStatus.ACTIVE:
                return  # paused while the AI was thinking
            self.execute(name, ctx, signal, ai_model=profile["model"])
        except Exception:
            log.exception("fx.ai_execute_failed", extra={"fields": {"strategy": name}})

    # ================================================================ execution
    def _reject(self, name: str, inst: str, rej: Reject) -> None:
        self._event("risk.rejected", strategy=name, instrument=inst, reason=rej.reason.value, detail=rej.detail)

    def execute(self, name: str, ctx: BarContext, signal: Signal, *, ai_model: str | None = None) -> None:
        rec, _ = self._strategies[name]
        inst = ctx.instrument
        with self._exec_lock:
            client = self._clients.get(rec.account_id)
            if client is None:
                err = self._accounts.get(rec.account_id, {}).get("error") or "account not connected"
                reason = RejectReason.LIVE_BLOCKED if "FX_ALLOW_LIVE" in err else RejectReason.NOT_TRADEABLE
                self._reject(name, inst, Reject(reason, err))
                return
            meta = self._accounts[rec.account_id]
            spec = self._specs[rec.account_id].get(inst)
            if spec is None:
                self._reject(name, inst, Reject(RejectReason.NOT_TRADEABLE, f"{inst} is not available on this account"))
                return
            try:
                price = client.pricing([inst])[inst]
                balance = self._balance(rec.account_id)
                mine = self.store.open_trades(strategy=name) + [t for t in self.store.pending_trades()
                                                                if t["strategy"].lower() == name.lower()]
                opposite = False
                if not meta.get("hedging"):
                    for t in client.open_trades():
                        if t["instrument"] == inst and (float(t["currentUnits"]) > 0) != (signal.direction.sign > 0):
                            opposite = True
                plan = plan_trade(rec.risk, signal, price, spec, balance=balance, open_count=len(mine),
                                  opposite_on_netting_account=opposite)
            except Reject as rej:
                self._reject(name, inst, rej)
                return
            except Exception as exc:
                self._event("order.skipped", logging.WARNING, strategy=name, instrument=inst, error=repr(exc))
                return

            client_id = f"fx-{_slug(name)}-{uuid.uuid4().hex[:10]}"
            row_id = self.store.insert_pending_trade(
                strategy=rec.name, strategy_version=rec.version, account_id=rec.account_id, instrument=inst,
                direction=plan.direction.value, units=abs(plan.units), client_id=client_id,
                requested_price=plan.entry_ref, sl_price=plan.sl_price, tp_price=plan.tp_price, pip=spec.pip,
                signal_reason=signal.reason, ai_model=ai_model)
            try:
                fill = client.market_order(
                    instrument=inst, units=plan.units, client_id=client_id, tag=f"fx:{rec.name}",
                    comment=f"{rec.name} v{rec.version}", sl_price=plan.sl_price, tp_price=plan.tp_price,
                    price_bound=plan.price_bound, precision=spec.display_precision,
                    units_precision=spec.units_precision)
            except OrderRejected as exc:
                self.store.mark_trade_failed(row_id, str(exc))
                self._event("order.rejected", logging.WARNING, strategy=name, instrument=inst, error=str(exc))
                return
            except Exception as exc:
                # Ambiguous (timeout / dropped connection): the order may or may not exist.
                self._event("order.unknown", logging.ERROR, strategy=name, instrument=inst, error=repr(exc),
                            client_id=client_id)
                self._resolve_pending(self.store.get_trade(row_id) or {}, client)
                return
            self.store.mark_trade_open(row_id, broker_trade_id=fill["trade_id"], entry_price=fill["price"],
                                       units=abs(fill["units"]), opened_at=self._iso(fill.get("time")),
                                       sl_price=plan.sl_price, tp_price=plan.tp_price)
            meta["summary_at"] = 0.0
            slippage = (fill["price"] - plan.entry_ref) * plan.direction.sign / spec.pip
            self._event("order.filled", strategy=name, instrument=inst, trade=row_id, direction=plan.direction.value,
                        units=abs(fill["units"]), price=fill["price"], sl=plan.sl_price, tp=plan.tp_price,
                        risk=plan.risk_amount, slippage_pips=round(slippage, 2), spread_pips=plan.spread_pips,
                        version=rec.version, ai_model=ai_model)

    def close_trade(self, trade: dict[str, Any], reason: str) -> bool:
        client = self._clients.get(trade["account_id"]) or self._ensure_account(trade["account_id"])
        if client is None or not trade.get("broker_trade_id"):
            return False
        try:
            res = client.close_trade(trade["broker_trade_id"])
        except OandaError as exc:
            if exc.status_code == 404:  # already closed at the broker
                self.reconcile_account(trade["account_id"])
                return True
            self._event("trade.close_failed", logging.ERROR, strategy=trade["strategy"], instrument=trade["instrument"],
                        trade=trade["id"], error=str(exc))
            return False
        self._record_close(trade["account_id"], trade["broker_trade_id"], price=res["price"], pnl=res["pnl"],
                           financing=res["financing"], closed_at=res.get("time"), reason=reason)
        return True

    def close_strategy(self, name: str, reason: str) -> int:
        closed = 0
        for t in self.store.open_trades(strategy=name):
            closed += self.close_trade(t, reason)
        return closed

    # ================================================================ bot-side exits
    def _halted(self, name: str, now: datetime) -> bool:
        return self.store.kv_get(f"halt:{name.lower()}") == trading_day_start(now).isoformat()

    def watch_baskets(self, now: datetime) -> None:
        watched = [(rec, self.store.open_trades(strategy=rec.name)) for rec, _ in self._strategies.values()
                   if rec.risk.basket_tp_amount or rec.risk.basket_sl_amount or rec.risk.daily_loss_limit]
        watched = [(rec, trades) for rec, trades in watched if trades]
        if not watched:
            return
        unrealized: dict[tuple[int, str], float] = {}
        for account_id in {t["account_id"] for _, trades in watched for t in trades}:
            client = self._clients.get(account_id)
            if client is None:
                continue
            for t in client.open_trades():
                unrealized[(account_id, str(t["id"]))] = float(t.get("unrealizedPL", 0)) + float(t.get("financing", 0))
        day_start = trading_day_start(now).isoformat()
        for rec, trades in watched:
            if any((t["account_id"], t["broker_trade_id"]) not in unrealized for t in trades):
                continue  # account unreachable or a trade just closed: decide next poll
            open_pl = sum(unrealized[(t["account_id"], t["broker_trade_id"])] for t in trades)
            r = rec.risk
            reason = None
            if r.basket_tp_amount and open_pl >= r.basket_tp_amount:
                reason = "basket_take_profit"
            elif r.basket_sl_amount and open_pl <= -r.basket_sl_amount:
                reason = "basket_stop_loss"
            elif r.daily_loss_limit and self.store.realized_since(rec.name, day_start) + open_pl <= -r.daily_loss_limit:
                reason = "daily_loss_limit"
                self.store.kv_set(f"halt:{rec.name.lower()}", day_start)
            if reason:
                self._event("basket.exit", logging.WARNING, strategy=rec.name, reason=reason,
                            open_pl=round(open_pl, 2), trades=len(trades))
                self.close_strategy(rec.name, reason)

    def weekend_guard(self, now: datetime) -> None:
        if fx_session(now) is not Session.WEEKEND_CLOSE:
            return
        for rec, _ in self._strategies.values():
            if not rec.risk.close_before_weekend:
                continue
            if time.monotonic() - self._close_attempt.get(rec.name, 0.0) < 30:
                continue
            if self.store.open_trades(strategy=rec.name):
                self._close_attempt[rec.name] = time.monotonic()
                self.close_strategy(rec.name, "weekend_close")

    # ================================================================ reconciliation
    def reconcile_all(self) -> None:
        for account_id in list(self._clients):
            try:
                self.reconcile_account(account_id)
            except Exception as exc:
                log_event(log, logging.WARNING, "fx.reconcile_failed", account=account_id, error=repr(exc))

    def reconcile_account(self, account_id: int) -> None:
        client = self._clients.get(account_id)
        if client is None:
            return
        open_rows = {t["broker_trade_id"]: t for t in self.store.open_trades(account_id=account_id)
                     if t["broker_trade_id"]}
        if open_rows:
            for bt in client.trades_by_ids(list(open_rows)):
                if bt.get("state") == "CLOSED":
                    self._record_close(account_id, str(bt["id"]), price=_f(bt.get("averageClosePrice")),
                                       pnl=float(bt.get("realizedPL", 0)), financing=float(bt.get("financing", 0)),
                                       closed_at=bt.get("closeTime"), reason="closed_at_broker")
        for t in self.store.pending_trades():
            if t["account_id"] == account_id:
                self._resolve_pending(t, client)

    def _resolve_pending(self, row: dict[str, Any], client: OandaClient) -> None:
        if not row or row.get("status") != "pending":
            return
        try:
            bt = client.trade("@" + row["client_id"])
        except Exception as exc:
            log_event(log, logging.WARNING, "fx.pending_lookup_failed", trade=row["id"], error=repr(exc))
            return
        if bt is None:
            age = (self._clock() - datetime.fromisoformat(row["created_at"])).total_seconds()
            if age > PENDING_GIVE_UP_S:
                self.store.mark_trade_failed(row["id"], "order not found at broker")
            return
        self.store.mark_trade_open(row["id"], broker_trade_id=str(bt["id"]), entry_price=float(bt["price"]),
                                   units=abs(float(bt["initialUnits"])), opened_at=self._iso(bt.get("openTime")),
                                   sl_price=None, tp_price=None)
        self._event("order.recovered", strategy=row["strategy"], instrument=row["instrument"], trade=row["id"])
        if bt.get("state") == "CLOSED":
            self._record_close(row["account_id"], str(bt["id"]), price=_f(bt.get("averageClosePrice")),
                               pnl=float(bt.get("realizedPL", 0)), financing=float(bt.get("financing", 0)),
                               closed_at=bt.get("closeTime"), reason="closed_at_broker")

    # ================================================================ console commands
    def process_commands(self) -> None:
        for cmd in self.store.claim_commands():
            ok, result = False, "unknown command"
            try:
                if cmd["kind"] == "close_trade":
                    trade = self.store.get_trade(int(cmd["target"]))
                    if trade and trade["status"] == "open":
                        ok = self.close_trade(trade, "closed_from_console")
                        result = "closed" if ok else "close failed: see activity"
                    else:
                        ok, result = True, "trade is not open"
                elif cmd["kind"] == "close_strategy":
                    n = self.close_strategy(cmd["target"], "closed_from_console")
                    ok, result = True, f"closed {n} trade(s)"
                elif cmd["kind"] == "check_account":
                    client = self._ensure_account(int(cmd["target"]), force=True)
                    meta = self._accounts.get(int(cmd["target"]), {})
                    ok = client is not None
                    result = "connected" if ok else (meta.get("error") or "failed")
            except Exception as exc:
                ok, result = False, f"{type(exc).__name__}: {exc}"
            self.store.finish_command(cmd["id"], ok, result)
            self._event("command", kind=cmd["kind"], target=cmd["target"], ok=ok, result=result)

    # ================================================================ heartbeat
    def _heartbeat(self, stopped: bool = False) -> None:
        now = self._clock()
        accounts = {}
        for account_id, meta in self._accounts.items():
            if "label" not in meta:
                continue
            accounts[str(account_id)] = {
                "label": meta["label"], "environment": meta.get("environment"), "currency": meta.get("currency"),
                "hedging": meta.get("hedging"), "connected": account_id in self._clients, "error": meta.get("error"),
                "stream": meta.get("stream"), "summary": meta.get("summary")}
        self.store.kv_set("engine", {
            "ts": now.isoformat(timespec="seconds"), "state": "stopped" if stopped else "running",
            "uptime_s": round(time.monotonic() - self._started), "session": fx_session(now).value,
            "allow_live": self.s.allow_live, "accounts": accounts, "invalid": self._invalid,
            "strategies": {n: {"last_bar": self._last_bar.get(n), "halted": self._halted(n, now)}
                           for n in self._strategies},
            "next_bar": {f"{i}/{g}": datetime.fromtimestamp((b + 1) * GRANULARITY_SECONDS[g], UTC).isoformat()
                         for (i, g), b in self._last_bucket.items()},
        })


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

