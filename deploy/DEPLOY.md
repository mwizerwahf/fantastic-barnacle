# Deploying to EC2 (bot + dashboard at ecclesia.mwizerwa.com/tdpltfm)

What runs where:

- `trading-bot.service` runs `main.py` 24/7 and writes `state/journal.db` (decisions) and
  `state/risk_state.json` (kill switch and trade ledger).
- `trading-dashboard.service` runs the read-only dashboard on `127.0.0.1:8050`.
- nginx serves it at `https://ecclesia.mwizerwa.com/tdpltfm/` with a password prompt.

Nothing new is exposed: port 8050 listens on localhost only. **Do not open it in the
security group.** No AWS resources are created.

The code is a git clone owned by your login user, so `git pull` works as usual. The
services run as a separate `tradingbot` user that can read the code but can only write
to `state/`.

Commands marked **[AL2023]** are for Amazon Linux 2023 (login user `ec2-user`), and
**[Ubuntu]** for Ubuntu (login user `ubuntu`). Check which one you have with `cat /etc/os-release`.

---

## 0. Before you start

1. **Stop the bot on your PC.** Two bots trading the same Alpaca account would double every
   order. Press Ctrl+C where `python main.py` is running and don't start it there again.
2. **Make sure `.env` is not in the repo.** On the EC2, inside your clone, run:

   ```bash
   git log --all --oneline -- .env '*/.env'
   ```

   If this prints anything, your API keys were committed. Regenerate the Alpaca keys (Alpaca
   dashboard) and the LLM key (provider console) now, then remove the file from the repo.
   This matters most if the repo is public. The project's `.gitignore` now excludes `.env`
   and `state/`; commit it so this can't happen again.

## 1. Move the clone to /opt

The services can't read home directories (`ProtectHome=true`), so the clone has to live in `/opt`.

```bash
sudo mv ~/<your-repo-folder> /opt/
REPO=/opt/<your-repo-folder>

# APP = the folder that contains main.py (the repo root, or its trading_bot/ subfolder)
APP=$(dirname "$(find "$REPO" -maxdepth 2 -name main.py -not -path '*/.venv/*' | head -1)")
echo "$APP"
```

Every command below uses `$REPO` and `$APP`. If you open a new SSH session, set them again.
To keep them, add both lines to `~/.bashrc`:

```bash
echo "REPO=$REPO; APP=$APP" >> ~/.bashrc
```

## 2. Python 3.11+ and dependencies

```bash
python3 --version
# [AL2023] if older than 3.11:
sudo dnf install -y python3.11 python3.11-pip && PY=python3.11
# [Ubuntu 24.04]: 3.12 is the default
sudo apt update && sudo apt install -y python3-venv && PY=python3

$PY -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"
```

## 3. Service user, state folder and secrets

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin tradingbot
sudo mkdir -p "$APP/state"
sudo chown tradingbot:tradingbot "$APP/state"
```

Next, copy your `.env` from the PC. Run this in PowerShell on your PC (use `ubuntu@` on Ubuntu):

```powershell
scp -i C:\path\to\your-key.pem D:\erwartem\$$$\trading_bot\.env ec2-user@ecclesia.mwizerwa.com:/tmp/trading_bot.env
# Optional: keep the trade ledger and kill-switch state
scp -i C:\path\to\your-key.pem D:\erwartem\$$$\trading_bot\state\risk_state.json ec2-user@ecclesia.mwizerwa.com:/tmp/
```

Then, back on the EC2:

```bash
sudo install -o tradingbot -g tradingbot -m 600 /tmp/trading_bot.env "$APP/.env" && rm /tmp/trading_bot.env
[ -f /tmp/risk_state.json ] && sudo install -o tradingbot -g tradingbot -m 600 /tmp/risk_state.json "$APP/state/" && rm /tmp/risk_state.json
sudo grep TRADING_MODE "$APP/.env"      # should say paper
```

## 4. Start both services

The unit files assume `/opt/trading_bot`. This rewrites them to your actual path while
installing them:

```bash
for unit in trading-bot trading-dashboard; do
  sed "s#/opt/trading_bot#$APP#g" "$APP/deploy/$unit.service" | sudo tee /etc/systemd/system/$unit.service > /dev/null
done
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
sudo cp "$APP/deploy/nginx-tdpltfm-ratelimit.conf" /etc/nginx/conf.d/
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

## Updating the code

Do this **outside market hours**, because stopping the bot closes all positions:

```bash
cd "$REPO" && git pull
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"
sudo systemctl restart trading-bot trading-dashboard
```

If a pull changes a file in `deploy/*.service`, re-run the loop in step 4 before restarting.

## Day-to-day

| Task | Command |
|---|---|
| Live bot logs | `journalctl -u trading-bot -f` |
| Dashboard logs | `journalctl -u trading-dashboard -f` |
| Restart dashboard (safe anytime) | `sudo systemctl restart trading-dashboard` |
| Restart bot | `sudo systemctl restart trading-bot` (**flattens all positions**: do it outside market hours) |
| Stop trading | `sudo systemctl stop trading-bot` (cancels orders, closes positions) |
| Change settings | `sudo nano "$APP/.env"`, then restart the bot outside market hours |

The bot plus LiteLLM and pandas uses roughly 300–500 MB of RAM. On a 1 GB instance that is
also running your website, watch `free -m` for the first day.
