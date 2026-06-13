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
from modules.strategy import LONG, SHORT, make_strategy


@dataclass
class BacktestResult:
    pair: str
    trades: List[Dict] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    timestamps: List[pd.Timestamp] = field(default_factory=list)
    final_capital: float = 0.0
    metrics: Dict = field(default_factory=dict)
    liquidations: int = 0
    ruined: bool = False          # account hit ~0 (wiped out)


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
        self.strategy = make_strategy(cfg)

    def run_pair(self, pair: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame) -> BacktestResult:
        cfg = self.cfg
        df = add_all_indicators(df_1h, cfg).reset_index(drop=True)
        rsi_series = _align_rsi_to_1h(df, df_15m, cfg.rsi_period)

        risk = RiskManager(cfg)
        result = BacktestResult(pair=pair)
        pos: Optional[Position] = None
        # Enough history for the slowest indicator (Ichimoku cloud = 52 + 26 shift).
        warmup = max(cfg.ema_slow, cfg.macd_slow, cfg.atr_period,
                     getattr(cfg, "ichimoku_senkou_b", 52) + getattr(cfg, "ichimoku_shift", 26)) + 2

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

            # Account wiped out? Stop trading — you can't trade with no money.
            if risk.capital <= 0.01:
                result.ruined = True
                break

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
                        # Stamp the real bar time so margin financing fees use
                        # the actual holding duration (not wall-clock now()).
                        pos.opened_at = ts.to_pydatetime()
                        result.trades_opened = getattr(result, "trades_opened", 0) + 1

            # day rollover by date
            risk.roll_day_if_needed(ts.to_pydatetime())
            result.equity_curve.append(risk.capital)
            result.timestamps.append(ts)

        # Close any dangling position at the last close.
        if pos is not None:
            last_ts = df.iloc[-1]["datetime"]
            fill = risk.register_close(pos, float(df.iloc[-1]["close"]), close_time=last_ts.to_pydatetime())
            result.trades.append(self._trade_record(pos, df.iloc[-1]["close"], fill, last_ts))

        result.final_capital = max(0.0, risk.capital)
        result.metrics = compute_metrics(result.trades)
        return result

    def _protective_exit(self, pos: Position, adverse: float):
        """
        Whichever protective level the adverse move hits FIRST: the stop loss
        or (for leveraged margin) the liquidation price. As price moves against
        the trade it crosses the nearer level first; if the stop sits beyond the
        liquidation price (e.g. very high leverage or a wide stop), you get
        liquidated and lose the whole margin. Returns (price, reason) or None.
        """
        liq = pos.liquidation_price()
        levels = [(pos.sl_price, "stop")]
        if liq is not None:
            levels.append((liq, "liquidation"))
        if pos.side == LONG:
            crossed = [(lvl, name) for lvl, name in levels if adverse <= lvl]
            if not crossed:
                return None
            return max(crossed, key=lambda x: x[0])      # highest = hit first
        crossed = [(lvl, name) for lvl, name in levels if adverse >= lvl]
        if not crossed:
            return None
        return min(crossed, key=lambda x: x[0])          # lowest = hit first

    def _manage(self, risk, pos, adverse, favorable, result, ts) -> bool:
        close_time = ts.to_pydatetime()
        # Protective exit on the adverse extreme: stop loss OR liquidation.
        hit = self._protective_exit(pos, adverse)
        if hit:
            price, reason = hit
            fill = risk.register_close(pos, price, close_time=close_time)
            rec = self._trade_record(pos, price, fill, ts)
            rec["exit_reason"] = reason
            result.trades.append(rec)
            if reason == "liquidation":
                result.liquidations += 1
            return True
        # Favorable extreme: TP1 partial close + trailing-stop ratchet.
        actions = risk.evaluate_position(pos, favorable)
        for action in actions:
            if action["type"] == "tp1":
                risk.register_partial_close(pos, action["price"], action["close_fraction"], close_time=close_time)
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
    # Include wider stops (2.5x, 3.0x) so we can see if giving trades more room
    # to breathe helps. Note: a wider stop lowers risk-based leverage, keeping
    # per-trade risk ~constant.
    atr_mults = [1.0, 1.5, 2.0, 2.5, 3.0]
    # Broadened toward higher/wider bands: a strict <=50 cap on the long RSI
    # almost never coincides with an EMA uptrend, which starved the strategy.
    rsi_ranges = [(35, 50), (35, 55), (40, 60), (45, 65), (30, 50)]
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
