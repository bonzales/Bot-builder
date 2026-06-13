"""
test_core.py — Offline sanity tests for indicators, strategy and the 3-phase
risk manager. No network required.

Run with:
    python -m pytest tests/test_core.py        (if pytest installed)
    python -m tests.test_core                  (standalone runner)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import Config
from modules import indicators
from modules.risk_manager import RiskManager
from modules.strategy import LONG, SHORT, Signal, Strategy


def _make_df(prices, volumes=None):
    n = len(prices)
    volumes = volumes if volumes is not None else [1000.0] * n
    return pd.DataFrame(
        {
            "timestamp": np.arange(n) * 3_600_000,
            "open": prices,
            "high": [p * 1.001 for p in prices],
            "low": [p * 0.999 for p in prices],
            "close": prices,
            "volume": volumes,
        }
    )


# ---------------------------------------------------------------- indicators
def test_ema_rsi_atr_obv():
    prices = list(np.linspace(100, 120, 60))
    df = _make_df(prices)
    e = indicators.ema(df["close"], 20)
    assert e.iloc[-1] > e.iloc[0]

    r = indicators.rsi(df["close"], 14).dropna()
    assert (r > 60).iloc[-1]  # steady uptrend -> high RSI

    a = indicators.atr(df, 14).dropna()
    assert (a > 0).all()

    o = indicators.obv(df)
    assert o.iloc[-1] > 0  # rising prices accumulate volume


# ----------------------------------------------------------------- strategy
def test_strategy_no_signal_on_flat():
    cfg = Config()
    prices = [100.0] * 80
    df = indicators.add_all_indicators(_make_df(prices), cfg)
    rsi = pd.Series([45.0] * 80)
    sig = Strategy(cfg).evaluate("BTC/EUR", df, rsi)
    assert sig is None


def test_volume_spike_blocks():
    cfg = Config()
    prices = list(np.linspace(100, 110, 80))
    vols = [1000.0] * 79 + [10000.0]  # huge spike on last bar
    df = indicators.add_all_indicators(_make_df(prices, vols), cfg)
    assert indicators.volume_spike(df, cfg.volume_spike_mult) if False else True
    from modules.strategy import volume_spike
    assert volume_spike(df, cfg.volume_spike_mult)


# -------------------------------------------------------------- risk manager
def test_position_sizing_and_sl():
    cfg = Config()
    cfg.use_margin = False  # pure spot: 1x, quantity == margin/price
    rm = RiskManager(cfg, starting_capital=100.0)
    assert rm.position_size_eur() == 33.0

    sig = Signal(pair="BTC/EUR", side=LONG, price=100.0, atr=2.0, rsi=40.0)
    pos = rm.build_position(sig, 33.0)
    # SL = 100 - 2*1.5 = 97
    assert abs(pos.sl_price - 97.0) < 1e-6
    # TP1 = +3%
    assert abs(pos.tp1_price - 103.0) < 1e-6
    assert abs(pos.quantity - 0.33) < 1e-6
    assert pos.leverage == 1.0 and not pos.is_margin


def test_dynamic_leverage_risk_targeted_and_capped():
    cfg = Config()
    rm = RiskManager(cfg, starting_capital=100.0)
    # Wide stop (volatile): atr=10 -> stop_distance = 15% -> tiny leverage, clamped to min.
    volatile = Signal(pair="BTC/EUR", side=LONG, price=100.0, atr=10.0, rsi=40.0)
    assert rm.select_leverage(volatile) == max(cfg.min_leverage, round(
        cfg.risk_per_trade_pct / (cfg.position_pct * 0.15), 2))

    # Tight stop (calm): atr tiny -> leverage would explode -> clamped to max.
    calm = Signal(pair="BTC/EUR", side=LONG, price=100.0, atr=0.05, rsi=40.0)
    assert rm.select_leverage(calm) == cfg.max_leverage

    # Loss at stop must never exceed the risk target (within the cap).
    sig = Signal(pair="ETH/EUR", side=LONG, price=100.0, atr=1.0, rsi=40.0)
    pos = rm.build_position(sig, rm.position_size_eur())
    stop_distance = abs(pos.entry_price - pos.sl_price)
    loss_at_stop = stop_distance * pos.quantity  # notional-based loss
    assert loss_at_stop <= cfg.risk_per_trade_pct * rm.capital + 1e-6


def test_short_lifecycle_margin_with_fees():
    cfg = Config()
    rm = RiskManager(cfg, starting_capital=100.0)
    sig = Signal(pair="BTC/EUR", side=SHORT, price=100.0, atr=2.0, rsi=60.0)
    pos = rm.build_position(sig, rm.position_size_eur())
    assert pos.is_margin and pos.leverage > 1.0
    assert pos.kraken_leverage >= 2          # shorts require a margin tier
    # SHORT SL above entry, TP1 below entry.
    assert pos.sl_price > pos.entry_price and pos.tp1_price < pos.entry_price
    # Hit TP1 (price drops 3%): partial close + breakeven.
    actions = rm.evaluate_position(pos, 97.0)
    assert any(a["type"] == "tp1" for a in actions)
    fill = rm.register_partial_close(pos, 97.0, cfg.tp1_close_fraction)
    assert fill["margin_fee"] > 0            # margin financing was charged
    assert rm.capital > 100.0                # profitable short


def test_three_phase_lifecycle_long():
    cfg = Config()
    cfg.use_margin = False  # isolate the SL/TP mechanics from leverage/fees
    rm = RiskManager(cfg, starting_capital=100.0)
    sig = Signal(pair="BTC/EUR", side=LONG, price=100.0, atr=2.0, rsi=40.0)
    pos = rm.build_position(sig, 33.0)

    # Below TP1: no action besides maybe nothing.
    assert rm.evaluate_position(pos, 101.0) == []

    # Hit TP1 (+3% => 103): partial close + breakeven SL.
    actions = rm.evaluate_position(pos, 103.0)
    kinds = [a["type"] for a in actions]
    assert "tp1" in kinds
    assert pos.tp1_done and pos.phase == 3
    rm.register_partial_close(pos, 103.0, cfg.tp1_close_fraction)
    assert abs(pos.remaining_fraction - 0.60) < 1e-6
    # SL moved to breakeven (>= entry).
    assert pos.sl_price >= pos.entry_price

    # Run to +6%: trailing SL should sit near +4.2% (0.7 * 6%).
    rm.evaluate_position(pos, 106.0)
    expected_sl = 100.0 * (1 + 0.06 * (cfg.trailing_step_pct / cfg.trailing_trigger_pct))
    assert abs(pos.sl_price - expected_sl) < 1e-6  # 104.2

    # Pull back to the trailing stop -> full close, profit locked.
    actions = rm.evaluate_position(pos, expected_sl - 0.01)
    assert any(a["type"] == "stop_hit" for a in actions)
    fill = rm.register_close(pos, pos.sl_price)
    assert fill["total_net"] > 0  # cannot lose after TP1
    assert rm.capital > 100.0


def test_daily_loss_limit():
    cfg = Config()
    rm = RiskManager(cfg, starting_capital=100.0)
    # Force a losing trade beyond -5%.
    sig = Signal(pair="ETH/EUR", side=LONG, price=100.0, atr=2.0, rsi=40.0)
    pos = rm.build_position(sig, 33.0)
    rm.daily_realized_pnl = -6.0  # -6€ on 100€ start = -6%
    assert rm.daily_limit_reached()
    can, reason = rm.can_open("BTC/EUR", [])
    assert not can and reason == "daily_loss_limit"


def test_strategy_factory_types():
    from modules.strategy import (
        BreakoutStrategy, IchimokuStrategy, Strategy, make_strategy,
    )
    cfg = Config()
    assert isinstance(make_strategy(cfg), Strategy)
    cfg.strategy_type = "breakout"
    assert isinstance(make_strategy(cfg), BreakoutStrategy)
    cfg.strategy_type = "ichimoku"
    assert isinstance(make_strategy(cfg), IchimokuStrategy)


def test_breakout_long_signal():
    from modules.strategy import BreakoutStrategy
    cfg = Config()
    # steady uptrend: last close breaks above the prior-20-bar high.
    prices = list(np.linspace(100, 160, 80))
    df = indicators.add_all_indicators(_make_df(prices), cfg)
    sig = BreakoutStrategy(cfg).evaluate("BTC/EUR", df)
    assert sig is not None and sig.side == LONG


def _run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ✓ {fn.__name__}")
    print(f"\n{passed}/{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
