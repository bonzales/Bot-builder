"""
backtest_engine.py — Event-driven backtest reusing the live strategy & risk
logic so simulated trades obey exactly the same rules as the live bot.

Run ONCE during setup to validate parameters (not before every trade).

For each 1h bar:
  * indicators are computed on data up to that bar (no look-ahead)
  * RSI is read from the most recent 15m candle at/below the bar timestamp
  * open positions are managed intrabar (pessimistic: stop checked before TP)
  * a new entry may open at the bar close when all conditions align

Commissions: Kraken taker fee (0.26%) applied on both legs via RiskManager.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from modules.indicators import add_all_indicators, rsi as rsi_fn
from modules.reporting import compute_metrics
from modules.risk_manager import Position, RiskManager
from modules.strategy import LONG, SHORT, Strategy


@dataclass
class BacktestResult:
    pair: str
    trades: List[Dict] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    timestamps: List[pd.Timestamp] = field(default_factory=list)
    final_capital: float = 0.0
    metrics: Dict = field(default_factory=dict)


def _align_rsi_to_1h(df_1h: pd.DataFrame, df_15m: pd.DataFrame, period: int) -> pd.Series:
    """RSI computed on 15m, forward-aligned to each 1h bar timestamp."""
    rsi_15m = rsi_fn(df_15m["close"], period)
    aligned = pd.Series(index=df_1h.index, dtype="float64")
    ts_15m = df_15m["timestamp"].values
    for i, ts in enumerate(df_1h["timestamp"].values):
        # most recent 15m candle at or before this 1h close
        idx = ts_15m.searchsorted(ts, side="right") - 1
        aligned.iloc[i] = rsi_15m.iloc[idx] if idx >= 0 else float("nan")
    return aligned


class Backtester:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.strategy = Strategy(cfg)

    def run_pair(self, pair: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame) -> BacktestResult:
        cfg = self.cfg
        df = add_all_indicators(df_1h, cfg).reset_index(drop=True)
        rsi_series = _align_rsi_to_1h(df, df_15m, cfg.rsi_period)

        risk = RiskManager(cfg)
        result = BacktestResult(pair=pair)
        pos: Optional[Position] = None
        warmup = max(cfg.ema_slow, cfg.macd_slow, cfg.atr_period) + 2

        for i in range(warmup, len(df)):
            bar = df.iloc[i]
            ts = bar["datetime"]

            # 1) Manage an open position intrabar (pessimistic ordering).
            if pos is not None:
                adverse = bar["low"] if pos.side == LONG else bar["high"]
                favorable = bar["high"] if pos.side == LONG else bar["low"]

                closed = self._manage(risk, pos, adverse, favorable, result, ts)
                if closed:
                    pos = None

            # 2) Look for a new entry at this bar's close.
            if pos is None and not risk.halted_today and not risk.daily_limit_reached():
                window = df.iloc[: i + 1]
                rsi_window = rsi_series.iloc[: i + 1]
                signal = self.strategy.evaluate(pair, window, rsi_window)
                if signal is not None:
                    can, _ = risk.can_open(pair, [])
                    if can:
                        size = risk.position_size_eur()
                        pos = risk.build_position(signal, size)
                        result.trades_opened = getattr(result, "trades_opened", 0) + 1

            # day rollover by date
            risk.roll_day_if_needed(ts.to_pydatetime())
            result.equity_curve.append(risk.capital)
            result.timestamps.append(ts)

        # Close any dangling position at the last close.
        if pos is not None:
            fill = risk.register_close(pos, float(df.iloc[-1]["close"]))
            result.trades.append(self._trade_record(pos, df.iloc[-1]["close"], fill, df.iloc[-1]["datetime"]))

        result.final_capital = risk.capital
        result.metrics = compute_metrics(result.trades)
        return result

    def _manage(self, risk, pos, adverse, favorable, result, ts) -> bool:
        # Stop check first (pessimistic).
        actions = risk.evaluate_position(pos, adverse)
        for action in actions:
            if action["type"] == "stop_hit":
                fill = risk.register_close(pos, action["price"])
                result.trades.append(self._trade_record(pos, action["price"], fill, ts))
                return True
            if action["type"] == "tp1":
                fill = risk.register_partial_close(pos, action["price"], action["close_fraction"])
        # Favorable extreme: TP1 / trailing.
        actions = risk.evaluate_position(pos, favorable)
        for action in actions:
            if action["type"] == "tp1":
                risk.register_partial_close(pos, action["price"], action["close_fraction"])
            elif action["type"] == "stop_hit":
                fill = risk.register_close(pos, action["price"])
                result.trades.append(self._trade_record(pos, action["price"], fill, ts))
                return True
        return False

    def _trade_record(self, pos: Position, exit_price: float, fill: Dict, ts) -> Dict:
        return {
            "pair": pos.pair,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": float(exit_price),
            "opened_at": pos.opened_at.isoformat(),
            "closed_at": str(ts),
            "total_net": fill.get("total_net", fill.get("net", 0.0)),
            "total_pct": fill.get("total_pct", 0.0),
            "size_eur": pos.size_eur,
        }

    def run_all(self, history: Dict[str, Dict[str, pd.DataFrame]]) -> Dict[str, BacktestResult]:
        results: Dict[str, BacktestResult] = {}
        for pair, frames in history.items():
            results[pair] = self.run_pair(
                pair, frames[self.cfg.primary_timeframe], frames[self.cfg.secondary_timeframe]
            )
        return results


# --------------------------------------------------------------------------- #
# Parameter optimization
# --------------------------------------------------------------------------- #
def optimize(cfg, history: Dict[str, Dict[str, pd.DataFrame]], logger=None) -> List[Dict]:
    """
    Grid-search the key parameters and rank combinations by net P&L.
    Returns a sorted list of {params, metrics} dicts (best first).
    """
    atr_mults = [1.0, 1.5, 2.0]
    rsi_ranges = [(30, 45), (35, 50), (40, 55)]
    tp1_values = [0.02, 0.03, 0.04]
    trailing_steps = [0.005, 0.007, 0.010]

    combos: List[Dict] = []
    for atr_mult in atr_mults:
        for rsi_lo, rsi_hi in rsi_ranges:
            for tp1 in tp1_values:
                for step in trailing_steps:
                    trial = copy.deepcopy(cfg)
                    trial.atr_sl_multiplier = atr_mult
                    trial.rsi_long_min, trial.rsi_long_max = rsi_lo, rsi_hi
                    trial.tp1_pct = tp1
                    trial.trailing_step_pct = step

                    bt = Backtester(trial)
                    results = bt.run_all(history)
                    all_trades: List[Dict] = []
                    for r in results.values():
                        all_trades.extend(r.trades)
                    metrics = compute_metrics(all_trades)
                    combos.append(
                        {
                            "params": {
                                "atr_sl_multiplier": atr_mult,
                                "rsi_long_range": [rsi_lo, rsi_hi],
                                "tp1_pct": tp1,
                                "trailing_step_pct": step,
                            },
                            "metrics": metrics,
                        }
                    )
                    if logger:
                        logger.info(
                            "ATR=%.1f RSI=%s TP1=%.0f%% step=%.1f%% -> net=%.2f€ trades=%d",
                            atr_mult, (rsi_lo, rsi_hi), tp1 * 100, step * 100,
                            metrics["net_total"], metrics["trades"],
                        )

    combos.sort(key=lambda c: (c["metrics"]["net_total"], c["metrics"]["profit_factor"]), reverse=True)
    return combos
