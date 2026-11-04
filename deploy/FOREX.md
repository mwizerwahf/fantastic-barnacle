# Forex engine: setup on a 1 GB Ubuntu server

The forex bot is a separate program in the `fx/` folder, with its own SQLite database. It doesn't need the stock bot to be running.

- **Engine** (`python -m fx engine`). Runs every strategy, places orders at OANDA and records each fill.
- **Console** (`python -m fx console`). A web page behind nginx where you add accounts, AI models and strategies, and track each strategy's performance.

The console never contacts OANDA itself. It asks the engine to act, through the database.

Memory on 1 GB: about 50 MB per process, plus about 165 MB the first time a strategy uses AI. A 1 GB swap file is a safety net.

> Educational software, not financial advice. Forex trading with leverage can lose more than you expect. Run on an OANDA **practice** account until you have weeks of results you trust.

---

## 1. OANDA

1. Open an OANDA account and create a **practice** sub-account.
2. Go to **My Account › Manage API Access** and generate a token.
3. Note the account ID. It looks like `101-004-1234567-001`.
4. Check that the account allows **hedging**. Without it, two strategies can't hold opposite sides of the same pair.
   - After you add the account, the console's Accounts page shows `Hedging: on/off`.
   - If it shows off, the engine still works, but it refuses an entry that would offset another strategy's position.

## 2. Server preparation

Run these on the new server, as the `ubuntu` user:

```bash
sudo apt update && sudo apt install -y python3 python3-venv git nginx apache2-utils certbot python3-certbot-nginx sqlite3

# 1 GB swap: insurance against memory spikes
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. Code and Python environment

```bash
sudo git clone <your-repo-url> /opt/trading_bot
cd /opt/trading_bot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements-fx.txt     # remove the litellm line first if you'll never use AI

sudo useradd --system --home /opt/trading_bot --shell /usr/sbin/nologin fxbot
sudo mkdir -p fx_state && sudo chown fxbot:fxbot fx_state && sudo chmod 700 fx_state
```

## 4. `.env` and the master key

The master key encrypts every OANDA token and AI key you enter in the console.

```bash
.venv/bin/python -m fx keygen          # prints FX_MASTER_KEY=...
sudo nano /opt/trading_bot/.env
```

Put these lines in `.env`:

```
FX_MASTER_KEY=<the key printed above>
FX_ALLOW_LIVE=false
# only if AI models in the console leave "API key" empty:
GEMINI_API_KEY=<key>
```

Then lock the file down:

```bash
sudo chown fxbot:fxbot /opt/trading_bot/.env && sudo chmod 600 /opt/trading_bot/.env
sudo -u fxbot .venv/bin/python -m fx check   # verifies key + database
```

**Save a copy of `FX_MASTER_KEY` somewhere safe**, such as a password manager. Without it, the stored tokens can't be decrypted and you'd have to re-enter them.

## 5. Services

```bash
sudo cp deploy/fx-engine.service deploy/fx-console.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fx-engine fx-console
systemctl status fx-engine fx-console --no-pager
journalctl -u fx-engine -f            # live log (Ctrl+C to leave)
```

## 6. nginx with HTTPS and a password

Point a DNS record at the server first, for example `fx.example.com`. Then:

```bash
sudo certbot --nginx -d fx.example.com                       # creates the HTTPS server block
echo 'limit_req_zone $binary_remote_addr zone=fx:10m rate=4r/s;' | sudo tee /etc/nginx/conf.d/fx-ratelimit.conf
sudo htpasswd -c /etc/nginx/.htpasswd-fx <your-username>    # choose a strong password
sudo nano /etc/nginx/sites-enabled/default                  # paste deploy/nginx-fx.conf's two location blocks
                                                            # inside the "listen 443 ssl" server block
sudo nginx -t && sudo systemctl reload nginx
```

Open `https://fx.example.com/fx/`.

## 7. First strategy (in the console)

1. **Accounts & AI › Add an OANDA account.** Choose Practice, then enter the account ID and token. The engine tests the connection, and the row turns **Connected**.
2. *(Optional)* **Add an AI model**, for example `gemini/gemini-3.8-flash` with its key.
   - The first one you add becomes the default.
   - Each strategy can use the default or pick its own.
3. **Strategies › New strategy.** Fill in:
   - a name, such as *Amara*
   - a type
   - pairs, such as `EUR_USD, GBP_USD`
   - a timeframe
   - a size: fixed units, or a percentage of the balance at risk
   - a stop-loss (required), in pips or as a money amount
   - a take-profit, if you want one

   Saving makes the strategy active, and the engine picks it up within 5 seconds.

Each strategy has its own page with its P&L curve, stats, versions, results by pair, trades and activity. Changing a trading setting starts a new **version**, so you can compare v1 against v2. To A/B test, use **Duplicate**: the copy starts paused under a new name, so you can change it and run both side by side.

## 8. How exits work

| Exit | Where it lives | Speed |
|---|---|---|
| Stop-loss / take-profit in **pips** | At OANDA, attached to the order | Instant, and works even if the server is down |
| Stop-loss / take-profit as a **money amount** | Converted to a price at entry and sent to OANDA | Same as pips |
| Close all at total profit or loss | The engine checks the strategy's open trades every 2 s | About 2 s |
| Daily loss limit | Same check; it also pauses the strategy until 17:00 New York | About 2 s |
| Weekend close (on by default) | The engine, from Friday 16:50 New York | About 1 s |

Every entry is a fill-or-kill market order with a worst-acceptable price (**max slippage**). If the price has moved further than that, OANDA refuses the order and nothing is filled. Stopping or restarting the engine **does not close trades**, because they keep their broker-side stop-loss and take-profit.

Hard limits are in code (`fx/risk.py`); settings can be stricter but never looser:
- at most 2% of the balance lost at the stop-loss, per trade
- at most 500,000 units per trade
- at most 10 open trades per strategy
- spread at most 10 pips
- slippage at most 5 pips

## 9. Running it day to day

- **Update:**
  ```bash
  cd /opt/trading_bot && sudo git pull && sudo systemctl restart fx-engine fx-console
  ```
  This is safe at any time, because trades stay protected at the broker.
- **Back up the database daily.** It holds the encrypted keys and all history:
  ```bash
  echo '15 22 * * * fxbot sqlite3 /opt/trading_bot/fx_state/fx.db ".backup /opt/trading_bot/fx_state/backup-$(date +\%a).db"' | sudo tee /etc/cron.d/fx-backup
  ```
- **Live trading:** add the live account in the console, then set `FX_ALLOW_LIVE=true` in `.env` and restart `fx-engine`. Until you do, live accounts are stored but never traded.

## 10. Adding your own strategy type

Create `fx/strategies/my_strategy.py`:

```python
from pydantic import Field
from fx.models import Direction, Signal
from fx.strategies.base import BarContext, FxStrategy, StrategyParams, register
from fx.indicators import ema

@register
class Pullback(FxStrategy):
    kind = "pullback"                      # stored in the database; never rename
    title = "Trend pullback"
    description = "Buys dips to the 20 EMA while above the 100 EMA."

    class Params(StrategyParams):          # the console builds the form from these fields
        fast: int = Field(default=20, ge=2, description="Pullback EMA")
        slow: int = Field(default=100, ge=10, description="Trend EMA")

    @property
    def lookback(self) -> int:
        return self.params.slow * 3

    def on_bar(self, ctx: BarContext) -> Signal | None:
        fast, slow = ema(ctx.closes, self.params.fast), ema(ctx.closes, self.params.slow)
        if None in (fast[-1], slow[-1]):
            return None
        if ctx.closes[-1] > slow[-1] and ctx.lows[-1] <= fast[-1] < ctx.closes[-1]:
            return Signal(direction=Direction.LONG, reason="pullback to EMA20 in an uptrend")
        return None
```

Then add `from fx.strategies import my_strategy  # noqa: F401` to `fx/strategies/__init__.py` and restart both services. The new type appears in the console's strategy-type list.

## 11. Adding an AI provider (e.g. Jev)

The model string's prefix chooses the provider. Anything without its own provider goes through LiteLLM. To add one, put this in `fx/ai.py` or a module it imports:

```python
@register_provider("jev")
class JevProvider:
    def __init__(self, model, *, api_key, api_base, timeout, max_tokens): ...
    def complete_json(self, system: str, user: str) -> str:
        """Return the model's text reply (JSON)."""
```

Then, in the console, add an AI model such as `jev/<model-name>`.
