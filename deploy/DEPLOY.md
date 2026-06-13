# Deployment — VPS Hetzner (Ubuntu 24)

Deploy only **after** completing the backtest and the 48h paper-trading run.
The systemd unit launches the bot in `live` mode and restarts it on crash.

## 0. One-command install (recommended)

On a fresh Ubuntu 24 server, as root, run a single line — it installs
everything and then prints the remaining steps:

```bash
curl -fsSL https://raw.githubusercontent.com/bonzales/Bot-builder/main/deploy/setup.sh | bash
```

Then just: edit `/opt/trading-bot/.env`, run the backtest + 48h paper trading,
and finally `systemctl enable --now trading-bot`. The manual steps below are
the same thing spelled out, if you prefer doing it by hand.

## 1. System prep

```bash
sudo adduser --system --group --home /opt/trading-bot trader
sudo mkdir -p /var/log/trading-bot
sudo chown trader:trader /var/log/trading-bot
```

## 2. Install the bot

```bash
sudo -u trader -H bash
cd /opt/trading-bot
git clone <repo-url> .
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill in real Kraken + Telegram credentials
exit
```

Edit `/opt/trading-bot/.env` with the real values (root or trader):

```
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

`chmod 600 /opt/trading-bot/.env` so secrets stay private.

## 3. Install the service

```bash
sudo cp deploy/trading-bot.service /etc/systemd/system/trading-bot.service
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
sudo systemctl start trading-bot
```

## 4. Operate

```bash
sudo systemctl status trading-bot     # health
journalctl -u trading-bot -f          # live logs (also in /var/log/trading-bot)
sudo systemctl restart trading-bot    # after a config change + redeploy
sudo systemctl stop trading-bot       # full stop
```

Quick day-to-day control is via Telegram (`/pause`, `/resume`, `/status`, …);
systemd is for lifecycle/restart and crash recovery.

## Going live checklist

1. `python -m backtest.report --months 12 --optimize` → review metrics, pick params
2. Update `config.py` with the agreed optimal parameters
3. `python -m tests.paper_trading --hours 48` → confirm behaviour on live data
4. Only then set `BOT_MODE=live` and start the service
