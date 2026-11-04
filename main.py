"""Execution loop.

Per cycle (aligned to bar close):
  gate (market open -> kill switch -> EOD) -> fetch data -> indicators
  -> strategy prefilter -> AI decision -> risk validation -> broker execution
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from types import FrameType

from dotenv import load_dotenv
from pydantic import ValidationError

from core.ai_router import AIRouter
from core.broker import BaseBroker, DryRunBroker, build_broker
from core.config import MARKET_TZ, Settings
from core.exceptions import TradingBotError
from core.journal import JournalHandler
from core.models import AccountState, Action, Clock, MarketSession, Position
from core.observability import log_event, new_correlation_id, setup_logging
from data.indicators import add_indicators, compute_all
from data.scraper import AlpacaDataProvider, BaseDataProvider
from risk.guardrails import GateResult, GateStatus, RiskManager
from strategies import build_strategy

log = logging.getLogger("trading_bot")
LIVE_WARNING_DELAY_S = 10
MAX_CLOSED_SLEEP_S = 900


class TradingBot:
    def __init__(self, settings: Settings, *, dry_run: bool) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self._stop = threading.Event()
        real_broker = build_broker(settings)
        self.broker: BaseBroker = DryRunBroker(real_broker) if dry_run else real_broker
        self.data: BaseDataProvider = AlpacaDataProvider(settings)
        self.ai = AIRouter(settings)
        self.strategy = build_strategy(settings.strategy, settings, self.ai)
        state_file = "risk_state.dry_run.json" if dry_run else "risk_state.json"
        self.risk = RiskManager(settings, self.broker, state_path=settings.state_dir / state_file)
        self._sessions_cache: tuple[str, list[MarketSession]] | None = None
        self._cycle = 0
        self._started = time.monotonic()

    # -- lifecycle --
    def request_stop(self, signum: int, _frame: FrameType | None) -> None:
        log_event(log, logging.WARNING, "shutdown.requested", signal=signal.Signals(signum).name)
        self._stop.set()

    def run(self, *, once: bool = False) -> None:
        try:
            while not self._stop.is_set():
                gate = self.run_cycle()
                if once:
                    break
                self._stop.wait(self._seconds_until_next_cycle(gate))
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        new_correlation_id()
        log_event(log, logging.WARNING, "shutdown.begin", flatten=self.settings.flatten_on_shutdown)
        try:
            self.broker.cancel_all_orders()
            if self.settings.flatten_on_shutdown:
                self.broker.close_all_positions()
        except Exception as exc:
            log_event(log, logging.CRITICAL, "shutdown.broker_error", error=repr(exc),
                      hint="check positions manually in the broker dashboard")
        log_event(log, logging.WARNING, "shutdown.complete")

    def _seconds_until_next_cycle(self, gate: GateResult) -> float:
        interval = self.settings.timeframe_minutes * 60
        now = time.time()
        wait = (now // interval + 1) * interval - now + self.settings.bar_publish_delay_s
        if gate.status is GateStatus.MARKET_CLOSED and gate.clock is not None:
            until_open = (gate.clock.next_open - datetime.now(UTC)).total_seconds()
            wait = max(wait, min(until_open, MAX_CLOSED_SLEEP_S))
        return max(wait, 1.0)

    # -- cycle --
    def run_cycle(self) -> GateResult:
        self._cycle += 1
        cid = new_correlation_id()
        started = time.perf_counter()
        stats = {"symbols": 0, "signals": 0, "approved": 0, "submitted": 0, "errors": 0}
        gate = self.risk.pre_cycle()
        if gate.allows_entries and gate.clock is not None:
            self._trade_symbols(gate.clock, stats)
        log_event(log, logging.INFO, "heartbeat", cycle=self._cycle, cid=cid, gate=gate.status.value,
                  gate_detail=gate.detail, dry_run=self.dry_run, mode=self.settings.trading_mode,
                  duration_ms=round((time.perf_counter() - started) * 1000),
                  uptime_s=round(time.monotonic() - self._started), **stats)
        return gate

    def _sessions(self, clock: Clock) -> list[MarketSession]:
        today = clock.timestamp.date()
        key = today.isoformat()
        if self._sessions_cache is None or self._sessions_cache[0] != key:
            self._sessions_cache = (key, self.broker.get_calendar(today - timedelta(days=10), today))
        return self._sessions_cache[1]

    def _trade_symbols(self, clock: Clock, stats: dict[str, int]) -> None:
        try:
            account = self.broker.get_account()
            positions = {p.symbol: p for p in self.broker.get_positions()}
            sessions = self._sessions(clock)
        except TradingBotError as exc:
            stats["errors"] += 1
            log_event(log, logging.ERROR, "cycle.state_unavailable", error=str(exc))
            return
        for symbol in self.settings.symbols:
            if self._stop.is_set():
                break
            stats["symbols"] += 1
            try:
                self._process_symbol(symbol, account, positions.get(symbol), sessions, clock, stats)
            except TradingBotError as exc:  # fail closed: skip the symbol, never guess
                stats["errors"] += 1
                log_event(log, logging.WARNING, "symbol.skipped", symbol=symbol,
                          error_type=type(exc).__name__, error=str(exc))
            except Exception:
                stats["errors"] += 1
                log.exception("symbol.unexpected_error", extra={"fields": {"symbol": symbol}})

    def _process_symbol(self, symbol: str, account: AccountState, position: Position | None,
                        sessions: list[MarketSession], clock: Clock, stats: dict[str, int]) -> None:
        tf = self.settings.timeframe_minutes
        bars = self.data.get_bars(symbol, tf, self.strategy.required_lookback, sessions=sessions)

        trades = None
        if self.settings.cvd_source == "trades":
            session_open = sessions[-1].open if sessions else clock.timestamp
            window = min(timedelta(minutes=self.settings.cvd_lookback_minutes), clock.timestamp - session_open)
            trades = self.data.get_recent_trades(symbol, max(window, timedelta(minutes=tf)))
        # TODO(extend): pass historical quotes to enable the quote-midpoint classification rule.
        enriched = add_indicators(bars, interval_minutes=tf, trades=trades)
        snapshot = compute_all(enriched, symbol=symbol, interval_minutes=tf)

        if not self.strategy.prefilter(snapshot):
            log_event(log, logging.DEBUG, "prefilter.rejected", symbol=symbol, close=snapshot.close,
                      vwap=snapshot.vwap, rsi=snapshot.rsi, cvd=snapshot.cvd)
            return

        quote = None
        try:
            quote = self.data.get_latest_quote(symbol)
        except TradingBotError as exc:
            log_event(log, logging.WARNING, "quote.unavailable", symbol=symbol, error=str(exc))

        sig = self.strategy.decide(enriched, snapshot, account, position, quote)
        log_event(log, logging.INFO, "signal", symbol=symbol, action=sig.action.value, confidence=sig.confidence,
                  entry=sig.entry_price, stop=sig.stop_loss, target=sig.take_profit, reasoning=sig.reasoning)
        if sig.action is Action.HOLD:
            return
        stats["signals"] += 1

        decision = self.risk.evaluate(sig, snapshot)
        if decision.approved is None:
            return
        stats["approved"] += 1
        order = self.broker.submit_bracket_order(decision.approved)
        stats["submitted"] += 1
        log_event(log, logging.INFO, "order.submitted", symbol=symbol, order_id=order.id,
                  client_order_id=order.client_order_id, status=order.status, qty=order.qty,
                  side=order.side, dry_run=self.dry_run)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Intraday AI trading bot (educational).")
    parser.add_argument("--dry-run", action="store_true", help="log approved orders without submitting them")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit (requires --dry-run)")
    args = parser.parse_args(argv)
    if args.once and not args.dry_run:
        # Shutdown flattens, so a live single cycle would close any position it just opened.
        parser.error("--once requires --dry-run")
    return args


def _live_warning(settings: Settings) -> None:
    banner = "!" * 72
    for line in (banner, "LIVE TRADING ENABLED: REAL MONEY WILL BE USED",
                 f"symbols={settings.symbols} model={settings.ai_model}",
                 f"Starting in {LIVE_WARNING_DELAY_S}s. Press Ctrl+C to abort.", banner):
        log.critical(line)
    time.sleep(LIVE_WARNING_DELAY_S)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # exposes provider API keys (ANTHROPIC_API_KEY, ...) to LiteLLM
    args = _parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, settings.log_json)
    journal_file = "journal.dry_run.db" if args.dry_run else "journal.db"
    logging.getLogger().addHandler(JournalHandler(settings.state_dir / journal_file))
    log_event(log, logging.INFO, "startup", tz=MARKET_TZ, dry_run=args.dry_run, **settings.public_summary())
    if not settings.is_paper and not args.dry_run:
        try:
            _live_warning(settings)
        except KeyboardInterrupt:
            log.warning("live start aborted by user")
            return 1

    try:
        bot = TradingBot(settings, dry_run=args.dry_run)
    except TradingBotError as exc:
        log_event(log, logging.CRITICAL, "startup.failed", error=str(exc))
        return 2
    signal.signal(signal.SIGINT, bot.request_stop)
    signal.signal(signal.SIGTERM, bot.request_stop)
    bot.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
