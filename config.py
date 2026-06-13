"""
config.py — Central configuration for the autonomous trading bot.

Every tunable parameter lives here so the strategy can be revised without
touching business logic. Secrets are loaded from the environment (.env);
everything else is plain data that can be edited and re-deployed.

The values below are the *initial* defaults. They are meant to be validated
and, if necessary, replaced by the optimal combination produced by the
backtest engine (see backtest/backtest_engine.py) before going live.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


# --------------------------------------------------------------------------- #
# Secrets / credentials (never hard-code, never commit)
# --------------------------------------------------------------------------- #
@dataclass
class Credentials:
    kraken_api_key: str = field(default_factory=lambda: os.getenv("KRAKEN_API_KEY", ""))
    kraken_api_secret: str = field(default_factory=lambda: os.getenv("KRAKEN_API_SECRET", ""))
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    @property
    def has_kraken(self) -> bool:
        return bool(self.kraken_api_key and self.kraken_api_secret)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


# --------------------------------------------------------------------------- #
# Runtime modes
# --------------------------------------------------------------------------- #
class Mode:
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


@dataclass
class Config:
    # ----- General ----- #
    mode: str = os.getenv("BOT_MODE", Mode.PAPER)  # paper by default; never live by accident
    exchange_id: str = "kraken"
    base_currency: str = "EUR"
    initial_capital: float = 100.0

    # ----- Data engine ----- #
    # Broader basket of liquid EUR pairs with Kraken margin, for the
    # exploration backtest. Trim to the best performers before going live.
    pairs: List[str] = field(default_factory=lambda: [
        "BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR", "DOT/EUR",
    ])
    primary_timeframe: str = "1h"       # trend / EMA / MACD / ATR / OBV
    secondary_timeframe: str = "15m"    # RSI timing
    candle_lookback: int = 300          # candles to keep in memory per pair
    refresh_seconds: int = 60           # data refresh / decision loop cadence

    # ----- Indicators ----- #
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    obv_lookback: int = 3               # OBV trend confirmation window

    # ----- Strategy thresholds ----- #
    rsi_long_min: float = 35.0
    rsi_long_max: float = 55.0   # widened from 50: an uptrend keeps RSI elevated
    rsi_short_min: float = 50.0
    rsi_short_max: float = 65.0
    # MACD confirmation mode: "state" (MACD above/below signal — looser, the
    # strategy actually trades) or "cross" (exact crossover on the last candle —
    # original spec, but so strict it produced ~2 trades/year in backtest).
    macd_mode: str = "state"
    # How many of the 4 entry conditions must hold (EMA trend always mandatory).
    # 4 = strict; 3 = looser -> more frequent trades.
    min_conditions: int = 4
    # Strategy style: "pullback" (EMA+RSI+MACD+OBV, default), "breakout"
    # (Donchian trend-following), "ichimoku" or "meanrev" (oversold bounce).
    strategy_type: str = "pullback"
    # Mean-reversion entry (oversold bounce): RSI(14) < max, MACD bullish cross,
    # and volume > (1 + vol_increase) * average of the last N candles. Exits use
    # the same dynamic ATR stop + TP1 + breakeven + trailing as the others
    # (let profits run) — no fixed % brackets.
    meanrev_rsi_max: float = 32.0
    meanrev_vol_increase: float = 0.25
    meanrev_vol_lookback: int = 6
    breakout_trend_filter: bool = True
    donchian_period: int = 20
    ichimoku_tenkan: int = 9
    ichimoku_kijun: int = 26
    ichimoku_senkou_b: int = 52
    ichimoku_shift: int = 26
    volume_spike_mult: float = 3.0      # skip trade if volume > 300% of average
    volume_avg_period: int = 20
    allow_short: bool = True            # SHORT requires margin (see margin section)

    # ----- Risk management ----- #
    position_pct: float = 0.33          # 33% of capital used as collateral/margin per trade
    max_concurrent_trades: int = 3
    atr_sl_multiplier: float = 1.5      # stop loss = entry -/+ ATR * mult
    tp1_pct: float = 0.03               # +3% -> partial close
    tp1_close_fraction: float = 0.40    # close 40% at TP1
    breakeven_buffer_pct: float = 0.001 # +0.1% to cover fees after TP1
    trailing_trigger_pct: float = 0.01  # every +1% additional gain ...
    trailing_step_pct: float = 0.007    # ... raises SL by +0.7%
    daily_loss_limit_pct: float = 0.05  # -5% of capital -> pause until 00:00 UTC

    # ----- Margin & dynamic leverage -----
    # SHORT and leveraged LONG run on Kraken margin. The bot does NOT use a
    # fixed leverage: it picks one dynamically so that the loss if the ATR stop
    # is hit stays at ~`risk_per_trade_pct` of capital. Calmer markets (tighter
    # stop) -> higher leverage; volatile markets -> lower leverage. The result
    # is clamped to [min_leverage, max_leverage].
    use_margin: bool = True             # enable margin (required for shorting)
    dynamic_leverage: bool = True       # choose leverage from volatility/risk
    risk_per_trade_pct: float = 0.01    # target max loss per trade = 1% of capital
    max_leverage: float = 3.0           # SELF-IMPOSED ceiling (Kraken allows more; do NOT raise lightly)
    min_leverage: float = 1.0           # 1x effective == plain spot for longs
    # Kraken margin financing costs (from the live order form): ~0.02% open,
    # ~0.01% rollover every 4h. Modelled in the bot AND the backtest.
    margin_open_fee: float = 0.0002
    margin_rollover_fee: float = 0.0001
    rollover_hours: int = 4
    # Leverage tiers Kraken offers per pair (used to pick the API `leverage`
    # param). These are only FALLBACK defaults — the bot refreshes them from
    # the live exchange at startup (DataEngine.refresh_leverage_tiers), since
    # each market (BTC/EUR, SOL/EUR, ...) has its own max leverage.
    kraken_leverage_tiers: dict = field(
        default_factory=lambda: {
            "BTC/EUR": [2, 3, 4, 5, 10],
            "ETH/EUR": [2, 3, 4, 5],
            "SOL/EUR": [2, 3, 4, 5, 10],
        }
    )
    default_leverage_tiers: list = field(default_factory=lambda: [2, 3])

    # ----- Order execution ----- #
    order_retry_attempts: int = 3
    order_retry_backoff_seconds: float = 2.0
    taker_fee: float = 0.0026           # Kraken 0.26% per operation

    # ----- Logging ----- #
    log_dir: str = os.getenv("LOG_DIR", "logs")
    log_file: str = "trading-bot.log"
    trade_log_file: str = "trades.jsonl"
    state_file: str = "state.json"

    # ----- Backtest ----- #
    backtest_months: int = 12
    backtest_timeframe: str = "1h"

    # ----- Credentials ----- #
    credentials: Credentials = field(default_factory=Credentials)

    # --------------------------------------------------------------------- #
    def as_public_dict(self) -> dict:
        """Serializable view of parameters (no secrets) for /config command."""
        return {
            "mode": self.mode,
            "exchange": self.exchange_id,
            "pairs": self.pairs,
            "primary_timeframe": self.primary_timeframe,
            "secondary_timeframe": self.secondary_timeframe,
            "initial_capital": self.initial_capital,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "rsi_period": self.rsi_period,
            "rsi_long_range": [self.rsi_long_min, self.rsi_long_max],
            "rsi_short_range": [self.rsi_short_min, self.rsi_short_max],
            "macd_mode": self.macd_mode,
            "strategy_type": self.strategy_type,
            "min_conditions": self.min_conditions,
            "macd": [self.macd_fast, self.macd_slow, self.macd_signal],
            "atr_period": self.atr_period,
            "atr_sl_multiplier": self.atr_sl_multiplier,
            "position_pct": self.position_pct,
            "max_concurrent_trades": self.max_concurrent_trades,
            "tp1_pct": self.tp1_pct,
            "tp1_close_fraction": self.tp1_close_fraction,
            "trailing_trigger_pct": self.trailing_trigger_pct,
            "trailing_step_pct": self.trailing_step_pct,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "allow_short": self.allow_short,
            "use_margin": self.use_margin,
            "dynamic_leverage": self.dynamic_leverage,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_leverage": self.max_leverage,
            "min_leverage": self.min_leverage,
            "margin_open_fee": self.margin_open_fee,
            "margin_rollover_fee_4h": self.margin_rollover_fee,
            "taker_fee": self.taker_fee,
        }


# Singleton-style default instance used across the app.
CONFIG = Config()
