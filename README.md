# Autonomous Crypto Trading Bot

A fully autonomous H24/7 trading bot for crypto on **Kraken**. It makes its own
entry/exit decisions from predefined rules — no human confirmation — and sends
Telegram notifications for information only. Built for a Hetzner VPS (Ubuntu 24),
starting capital 100€.

> ⚠️ Trading involves real financial risk. Never run in `live` mode before
> completing the backtest **and** the 48h paper-trading run.

## Architecture

```
trading-bot/
├── main.py                 # Entry point (--mode paper|live)
├── config.py               # All tunable parameters (single source of truth)
├── modules/
│   ├── data_engine.py      # Kraken market data via ccxt
│   ├── indicators.py       # EMA, RSI, MACD, ATR, OBV
│   ├── strategy.py         # 4-condition LONG/SHORT signal logic
│   ├── risk_manager.py     # Sizing, ATR stop, 3-phase trade management
│   ├── order_manager.py    # Order execution + retry
│   ├── engine.py           # Decision loop + Telegram command controller
│   ├── telegram_bot.py     # Bidirectional, owner-only notifications/commands
│   ├── reporting.py        # Shared performance metrics
│   └── logger.py           # Text log + structured JSONL trade log
├── backtest/
│   ├── data_fetcher.py     # Download/cache 12 months of history
│   ├── backtest_engine.py  # Event-driven backtest + parameter optimizer
│   └── report.py           # Metrics, equity-curve chart, CLI runner
├── tests/
│   ├── paper_trading.py    # 48h live-data simulation (no real money)
│   └── test_core.py        # Offline unit tests (no network)
└── deploy/
    ├── trading-bot.service # systemd unit (auto-restart)
    └── DEPLOY.md           # VPS deployment guide
```

## Strategy

Signals require **all four** conditions on the latest closed candle (RSI on 15m,
the rest on 1h):

| | LONG | SHORT |
|---|---|---|
| Trend | EMA20 > EMA50 | EMA20 < EMA50 |
| Momentum | RSI 35–50 | RSI 50–65 |
| Confirmation | MACD bullish cross | MACD bearish cross |
| Volume | OBV rising vs last 3 | OBV falling vs last 3 |

SHORT trades use Kraken margin (see *Short & dynamic leverage* below).

No trade if: daily loss limit hit, a position is already open on the pair, or
volume spikes > 300% of average.

## Risk management

- **Sizing:** 33% of capital per trade → up to 3 concurrent positions.
- **Stop loss:** ATR × 1.5 from entry.
- **Phase 1 — Entry:** initial ATR stop.
- **Phase 2 — TP1 (+3%):** close 40%, move SL to breakeven (+0.1% for fees) →
  the trade can no longer close at a loss.
- **Phase 3 — Trailing (remaining 60%):** SL trails at 70% of peak gain
  (+4%→+2.8%, +5%→+3.5%, +6%→+4.2%), ratcheting only upward.
- **Daily loss limit:** −5% of capital → pause until 00:00 UTC.

### Short & dynamic leverage (margin)

SHORT trades and leveraged LONGs run on **Kraken margin**. The bot does **not**
use a fixed leverage: it picks one **dynamically from volatility** so the loss
if the ATR stop is hit stays at ~**1% of capital** (`risk_per_trade_pct`):

```
leverage = risk_per_trade_pct / (position_pct × stop_distance_pct)   # clamped to [1x, 3x]
```

Calm market (tight stop) → higher leverage; volatile market → lower leverage,
**at constant risk**. The ceiling is a self-imposed **3x** (`max_leverage`) —
well below the 10x Kraken allows — and is editable in `config.py`. Margin
financing costs (≈0.02% open + ≈0.01% rollover / 4h) are modelled in both the
live bot and the backtest. Set `allow_short = False` / `use_margin = False` to
revert to spot-only LONG.

> ⚠️ Leverage amplifies losses too. Because exits are software-managed, a bot
> outage during a sharp move is the main residual risk — systemd auto-restart
> mitigates it; validate on small size before scaling.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Kraken + Telegram credentials
```

## Workflow (do this in order)

```bash
# 1. Backtest 12 months + optimize key parameters
python -m backtest.report --months 12 --optimize

# 2. Update config.py with the agreed optimal parameters

# 3. Paper-trade on live data for 48h
python -m tests.paper_trading --hours 48

# 4. Only then: go live
python main.py --mode live
```

`python main.py` defaults to **paper** mode. Live mode refuses to start without
Kraken credentials.

## Telegram commands (owner-only)

`/status` `/report` `/pause` `/resume` `/config` `/history` `/drawdown`

Any sender other than the configured `TELEGRAM_CHAT_ID` is ignored.

## Tests

```bash
python -m tests.test_core     # offline sanity checks for indicators/strategy/risk
```

## Indicators note

Indicators are implemented natively in `modules/indicators.py` (Wilder RSI/ATR,
EMA-based MACD) to stay robust across numpy/pandas versions. `pandas-ta` is kept
in `requirements.txt` for parity but the bot does not hard-depend on it.

## Deployment

See [`deploy/DEPLOY.md`](deploy/DEPLOY.md) — systemd service with auto-restart,
logs in `/var/log/trading-bot/`.
