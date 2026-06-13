#!/usr/bin/env bash
#
# setup.sh — One-shot installer for the trading bot on a fresh Ubuntu 24 VPS.
#
# Run it as root on the server, with a single command:
#
#   curl -fsSL https://raw.githubusercontent.com/bonzales/Bot-builder/main/deploy/setup.sh | bash
#
# It installs everything, then STOPS before going live. You still have to:
#   1. paste your credentials into /opt/trading-bot/.env
#   2. run the backtest and the 48h paper trading
#   3. enable the live service
# (the script prints these next steps at the end).

set -euo pipefail

REPO_URL="https://github.com/bonzales/Bot-builder.git"
APP_DIR="/opt/trading-bot"
LOG_DIR="/var/log/trading-bot"
BRANCH="${BOT_BRANCH:-main}"

echo "==> Updating system and installing dependencies..."
apt-get update -y
apt-get install -y git python3-venv python3-pip

echo "==> Creating service user and log directory..."
id -u trader >/dev/null 2>&1 || adduser --system --group --home "$APP_DIR" trader
mkdir -p "$LOG_DIR"
chown trader:trader "$LOG_DIR"

echo "==> Fetching the bot into $APP_DIR ..."
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch origin "$BRANCH"
    git -C "$APP_DIR" checkout "$BRANCH"
    git -C "$APP_DIR" pull origin "$BRANCH"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> Creating Python virtual environment and installing requirements..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Preparing the credentials file (.env) ..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"
chown -R trader:trader "$APP_DIR"

echo "==> Installing the systemd service (not started yet)..."
cp "$APP_DIR/deploy/trading-bot.service" /etc/systemd/system/trading-bot.service
systemctl daemon-reload

cat <<'STEPS'

============================================================
 ✅ Installazione completata. PROSSIMI PASSI (in ordine):
============================================================

 1) Inserisci le credenziali Kraken + Telegram:
      nano /opt/trading-bot/.env
    (poi salva con Ctrl+O, Invio, Ctrl+X)

 2) Backtest 12 mesi + ottimizzazione (NON serve la chiave API):
      sudo -u trader /opt/trading-bot/venv/bin/python -m backtest.report --months 12 --optimize

 3) Paper trading 48h su dati reali (NON serve la chiave API):
      sudo -u trader /opt/trading-bot/venv/bin/python -m tests.paper_trading --hours 48

 4) SOLO dopo aver validato backtest + paper -> avvia il live:
      systemctl enable --now trading-bot
      journalctl -u trading-bot -f

============================================================
STEPS
