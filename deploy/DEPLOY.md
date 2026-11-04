# Deploying to EC2 (bot + dashboard at ecclesia.mwizerwa.com/tdpltfm)

What runs where:

- `trading-bot.service` runs `main.py` 24/7 and writes `state/journal.db` (decisions) and
  `state/risk_state.json` (kill switch and trade ledger).
- `trading-dashboard.service` runs the read-only dashboard on `127.0.0.1:8050`.
- nginx serves it at `https://ecclesia.mwizerwa.com/tdpltfm/` with a password prompt.

Nothing new is exposed: port 8050 listens on localhost only. **Do not open it in the
security group.** No AWS resources are created.

Commands marked **[AL2023]** are for Amazon Linux 2023 (login user `ec2-user`), and
**[Ubuntu]** for Ubuntu (login user `ubuntu`). Check which one you have with `cat /etc/os-release`.

---

## 0. Stop the bot on your PC first

Two bots trading the same Alpaca account would double every order. Press Ctrl+C in the
PowerShell window that is running `python main.py`, and don't start it there again.

## 1. Package and copy the code (PowerShell on your PC)

```powershell
cd D:\erwartem\$$$
tar -czf trading_bot.tgz --exclude=.venv --exclude=state --exclude=__pycache__ --exclude=.env trading_bot
scp -i C:\path\to\your-key.pem trading_bot.tgz ec2-user@ecclesia.mwizerwa.com:/tmp/
scp -i C:\path\to\your-key.pem trading_bot\.env ec2-user@ecclesia.mwizerwa.com:/tmp/trading_bot.env
# Optional: keep the trade ledger and kill-switch state
scp -i C:\path\to\your-key.pem trading_bot\state\risk_state.json ec2-user@ecclesia.mwizerwa.com:/tmp/
```

Use `ubuntu@` instead of `ec2-user@` on Ubuntu.

## 2. Python 3.11+ on the server

```bash
python3 --version
# [AL2023] if older than 3.11:
sudo dnf install -y python3.11 python3.11-pip && PY=python3.11
# [Ubuntu 24.04]: 3.12 is the default
sudo apt update && sudo apt install -y python3-venv && PY=python3
```

## 3. Install

```bash
sudo useradd --system --home-dir /opt/trading_bot --shell /usr/sbin/nologin tradingbot
sudo mkdir -p /opt && sudo tar -xzf /tmp/trading_bot.tgz -C /opt
sudo mkdir -p /opt/trading_bot/state
sudo mv /tmp/trading_bot.env /opt/trading_bot/.env
[ -f /tmp/risk_state.json ] && sudo mv /tmp/risk_state.json /opt/trading_bot/state/
sudo $PY -m venv /opt/trading_bot/.venv
sudo /opt/trading_bot/.venv/bin/pip install -r /opt/trading_bot/requirements.txt
sudo chown -R tradingbot:tradingbot /opt/trading_bot
sudo chmod 600 /opt/trading_bot/.env
rm -f /tmp/trading_bot.tgz
```

Check that `/opt/trading_bot/.env` still has `TRADING_MODE=paper`.

## 4. Start both services

```bash
sudo cp /opt/trading_bot/deploy/trading-bot.service /opt/trading_bot/deploy/trading-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot trading-dashboard
systemctl status trading-bot trading-dashboard --no-pager
curl -s http://127.0.0.1:8050/healthz        # -> {"ok": true}
```

## 5. nginx: password + route

```bash
# Password tool
sudo dnf install -y httpd-tools        # [AL2023]
sudo apt install -y apache2-utils      # [Ubuntu]
sudo htpasswd -c /etc/nginx/.htpasswd-tdpltfm <choose-a-username>   # prompts for a password
sudo chown root:nginx /etc/nginx/.htpasswd-tdpltfm      # [AL2023]
sudo chown root:www-data /etc/nginx/.htpasswd-tdpltfm   # [Ubuntu]
sudo chmod 640 /etc/nginx/.htpasswd-tdpltfm

# Rate limit against password guessing (http context)
sudo cp /opt/trading_bot/deploy/nginx-tdpltfm-ratelimit.conf /etc/nginx/conf.d/
```

Open the config that has the **HTTPS** `server` block for `ecclesia.mwizerwa.com`
(find it with `sudo grep -rl "ecclesia.mwizerwa.com" /etc/nginx/`). Paste the contents of
`deploy/nginx-tdpltfm-location.conf` inside that block, next to the existing `location /`. Then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

If the site has no HTTPS yet, set that up first (for example with certbot) before adding
the dashboard.

## 6. Verify

```bash
curl -sI https://ecclesia.mwizerwa.com/tdpltfm/ | head -1                 # 401 (password required)
curl -su <username> https://ecclesia.mwizerwa.com/tdpltfm/api/summary | head -c 300
```

Then open `https://ecclesia.mwizerwa.com/tdpltfm/` in a browser and log in.

## Day-to-day

| Task | Command |
|---|---|
| Live bot logs | `journalctl -u trading-bot -f` |
| Dashboard logs | `journalctl -u trading-dashboard -f` |
| Restart dashboard (safe anytime) | `sudo systemctl restart trading-dashboard` |
| Restart bot | `sudo systemctl restart trading-bot` (**flattens all positions**: do it outside market hours) |
| Stop trading | `sudo systemctl stop trading-bot` (cancels orders, closes positions) |

To update the code, repeat step 1. Then on the server, outside market hours:

```bash
sudo systemctl stop trading-bot trading-dashboard
sudo tar -xzf /tmp/trading_bot.tgz -C /opt && sudo chown -R tradingbot:tradingbot /opt/trading_bot
sudo /opt/trading_bot/.venv/bin/pip install -r /opt/trading_bot/requirements.txt
sudo systemctl start trading-bot trading-dashboard
```

The bot plus LiteLLM and pandas uses roughly 300–500 MB of RAM. On a 1 GB instance that is
also running your website, watch `free -m` for the first day.
