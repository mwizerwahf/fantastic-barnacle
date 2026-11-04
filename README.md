# Intraday AI Trading Bot (scaffold)

> **DISCLAIMER: Educational software. Not financial advice.** Trading carries
> substantial risk of loss. This code has not been audited. Paper-trade it
> extensively (weeks, across varied market conditions) before even considering
> live use. You alone are responsible for any trades it places.

A modular intraday bot for small accounts (~$500–$25k). The pipeline runs once per
bar close:

```
gate (market open → kill switch → EOD window) → fetch bars/trades → indicators
  → deterministic prefilter → AI decision (LiteLLM) → risk validation → bracket order
```

No position is ever held overnight. The AI only proposes direction, stop and target.
Position size and every other decision is made in code.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # fill in Alpaca paper keys + one LLM provider key
python -m pytest -q         # run from the trading_bot/ directory
python main.py --dry-run --once   # one cycle, logs approved orders, submits nothing
python main.py --dry-run          # continuous dry run
python main.py                    # paper trading (default mode)
```

Python 3.11+ required. Get paper API keys from the Alpaca dashboard (Paper Trading section).

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | - | Required. Paper keys for paper mode. |
| `TRADING_MODE` | `paper` | `live` also requires `CONFIRM_LIVE_TRADING=yes`. Live start prints a warning and waits 10 s. |
| `DATA_FEED` | `iex` | `sip` needs a paid Alpaca data plan. |
| `SYMBOLS` | `SPY,QQQ` | Comma-separated. |
| `TIMEFRAME_MINUTES` | `5` | `1`, `5` or `15`. |
| `CVD_SOURCE` | `trades` | `trades` (tick rule on prints) or `bars` (cheaper, cruder). |
| `AI_MODEL` | `anthropic/claude-sonnet-5-5` | Any LiteLLM model string. |
| `AI_FALLBACK_MODELS` | empty | Comma-separated, tried on provider failure. |
| `AI_API_BASE` | empty | e.g. `http://localhost:11434` for Ollama. |
| `STRATEGY` | `intraday_momentum` | Registry key. |
| `ALLOW_SHORT` | `false` | Alpaca also requires a margin account (≥ $2,000 equity) to short. |
| `STATE_DIR` | `state` | Halt flag and entry ledger. Don't delete it mid-session. |
| `FLATTEN_ON_SHUTDOWN` | `true` | Shutdown always cancels orders; this also closes positions. |
| `LOG_JSON` | `true` | `false` for human-readable key=value logs. |

Risk variables are listed in the table below and in `.env.example`.

## Swapping models

Change only `AI_MODEL` (keys are read from the provider's usual env var):

```bash
AI_MODEL=anthropic/claude-sonnet-5-5     # ANTHROPIC_API_KEY
AI_MODEL=openai/<model-name>             # OPENAI_API_KEY
AI_MODEL=gemini/<model-name>             # GEMINI_API_KEY
AI_MODEL=ollama/llama3.1                 # AI_API_BASE=http://localhost:11434
AI_FALLBACK_MODELS=openai/<model-name>   # optional chain
```

The router asks for native structured output (JSON schema) when LiteLLM reports
support for it and falls back to JSON mode otherwise. Every reply is validated
against the decision schema either way. Invalid output gets one corrective retry,
then becomes `HOLD`. Provider errors move on to the next fallback model. If every
model fails, the result is `HOLD`.

## Adding a strategy

1. Create `strategies/my_strategy.py`, subclass `BaseStrategy`, set `name = "my_strategy"`,
   and decorate the class with `@register_strategy`.
2. Add `from strategies import my_strategy` to `strategies/__init__.py`.
3. Set `STRATEGY=my_strategy`. `main.py` needs no changes.

## Risk rules

Enforced in `risk/guardrails.py`. `RiskManager` reads account, positions, orders,
clock and calendar straight from the broker, and it is the only code that can create
an `ApprovedOrder`, the only type `submit_bracket_order` accepts. Config values can
be **stricter but never looser** than the hard limit. A looser value is clamped and
logged as `risk.config_clamped`.

| Rule | Default | Hard limit | Behaviour |
|---|---|---|---|
| Risk per trade | 1% of equity | max 2% | `shares = floor(risk$ / abs(entry − stop))` |
| Max position notional | 25% of equity | max 50% | Also capped at buying power. Reject if shares < 1. |
| Min reward:risk | 1.5 | min 1.5 | |
| Min confidence | 0.6 | min 0.6 | |
| Min stop distance | 0.5 × ATR(14) | min 0.5 × ATR | Avoids stops inside normal noise. |
| Stop/target side | n/a | always | Long: stop < entry < target. Short: the reverse. |
| Daily drawdown kill switch | 3% below `last_equity` | max 3% | Cancel all orders, flatten, halt for the session. Halt is saved to disk. |
| EOD flatten | 15 min before broker close | min 15 min | Blocks entries, cancels orders, closes positions. Follows early closes. |
| Opening blackout | 5 min | configurable | No entries right after the open. |
| Max concurrent positions | 2 | max 5 | Open orders count as exposure. |
| Max trades per day | 6 | max 20 | Counted at approval time. |
| One position per symbol | n/a | always | |
| PDT guard | equity < $25k → ≤ 3 day trades / 5 sessions | configurable | Uses the higher of the broker's count and the local ledger. Kept configurable because the rule may change. |
| Approval TTL | 30 s | max 60 s | The broker refuses expired approvals. |
| Fail closed | n/a | always | Any error, timeout, stale data (> 2 bar intervals) or invalid AI output → no trade. |

Protection lives at the broker: every entry is a bracket order (market entry, stop-loss
and take-profit legs), so stops stay in place if the bot crashes.

## Dashboard and server deployment

The bot records its decisions (signals, AI reasoning, risk rejections, orders) in
`state/journal.db`. `python -m dashboard.app` serves a read-only dashboard on
`127.0.0.1:8050` that combines that journal with Alpaca's order and equity history.
See [deploy/DEPLOY.md](deploy/DEPLOY.md) for running the bot and the dashboard on EC2
behind nginx with a password.

## Forex engine (separate program)

`fx/` is an independent multi-strategy forex engine for OANDA with its own SQLite database and web console:
named strategies (one per "person"), each with its own pairs, sizing, pip or money-amount exits, optional AI model
and its own account (or a shared one), running side by side, even on the same pair. Performance is tracked per
strategy and per version. Setup on a 1 GB server: [deploy/FOREX.md](deploy/FOREX.md).

```bash
python -m fx keygen    # FX_MASTER_KEY for .env
python -m fx engine    # trading engine
python -m fx console   # web console on 127.0.0.1:8060
```

## Known limitations

- **CVD is a proxy.** Alpaca's standard feeds don't provide Level 2. Trades are
  classified with the tick rule. On IEX that covers only IEX prints, a small share
  of total volume.
- Entry is a market order, so the actual fill (and the real risk per share) can differ
  slightly from the reference price used for sizing.
- If the bot is down at the close, positions are only protected by their bracket legs.
  Run it under a supervisor (systemd, Docker restart policy) and watch the logs.
- `--dry-run` keeps a separate risk ledger (`risk_state.dry_run.json`).
- Web scraping (`PoliteFetcher`) respects robots.txt and rate limits, but you are
  responsible for each site's Terms of Service.
