"""
report.py — Backtest report generation and CLI runner.

As a script it performs the full setup-phase backtest:
    python -m backtest.report --months 12 [--optimize] [--no-cache]

It downloads (or loads cached) history, runs the backtest on all pairs,
prints the required metrics (win rate, profit factor, max drawdown, Sharpe,
trades/day, best/worst trade), saves an equity-curve chart, and — with
--optimize — grid-searches the key parameters and reports the best combo.
"""

from __future__ import annotations

import argparse
import math
import os
from typing import Dict, List

from config import CONFIG
from modules.logger import TradingLogger
from modules.reporting import compute_metrics

from .backtest_engine import Backtester, BacktestResult, optimize
from .data_fetcher import fetch_history

REPORT_DIR = os.path.join(os.path.dirname(__file__), "reports")


def _fmt_pf(pf: float) -> str:
    return "∞" if pf == math.inf else f"{pf:.2f}"


def _trading_days(results: Dict[str, BacktestResult]) -> float:
    spans = []
    for r in results.values():
        if r.timestamps:
            spans.append((r.timestamps[-1] - r.timestamps[0]).days or 1)
    return max(spans) if spans else 1


def render_text_report(results: Dict[str, BacktestResult]) -> str:
    all_trades: List[Dict] = []
    for r in results.values():
        all_trades.extend(r.trades)
    agg = compute_metrics(all_trades)
    days = _trading_days(results)
    trades_per_day = agg["trades"] / days if days else 0.0

    lines = ["=" * 60, "BACKTEST REPORT", "=" * 60, ""]
    for pair, r in results.items():
        m = r.metrics
        lines.append(
            f"{pair:>9} | trades={m['trades']:>3} | win={m['win_rate']:.0%} | "
            f"PF={_fmt_pf(m['profit_factor'])} | net={m['net_total']:+.2f}€ | "
            f"maxDD={m['max_drawdown']:.2f}€ | final={r.final_capital:.2f}€"
        )
    lines += ["", "-" * 60, "AGGREGATE", "-" * 60]
    lines.append(f"Total trades        : {agg['trades']}")
    lines.append(f"Win rate            : {agg['win_rate']:.1%}")
    lines.append(f"Profit factor       : {_fmt_pf(agg['profit_factor'])}")
    lines.append(f"Net P&L             : {agg['net_total']:+.2f}€")
    lines.append(f"Max drawdown        : {agg['max_drawdown']:.2f}€")
    lines.append(f"Sharpe ratio        : {agg['sharpe']:.2f}")
    lines.append(f"Avg trades / day    : {trades_per_day:.2f}")
    if agg["best"]:
        lines.append(f"Best trade          : {agg['best']['pair']} {agg['best']['net']:+.2f}€")
    if agg["worst"]:
        lines.append(f"Worst trade         : {agg['worst']['pair']} {agg['worst']['net']:+.2f}€")
    lines.append("=" * 60)
    return "\n".join(lines)


def save_equity_curve(results: Dict[str, BacktestResult], path: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    os.makedirs(os.path.dirname(path), exist_ok=True)
    plt.figure(figsize=(11, 6))
    for pair, r in results.items():
        if r.timestamps and r.equity_curve:
            plt.plot(r.timestamps, r.equity_curve, label=pair)
    plt.title("Backtest equity curve")
    plt.xlabel("Time")
    plt.ylabel("Capital (€)")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=120)
    plt.close()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the setup-phase backtest")
    parser.add_argument("--months", type=int, default=CONFIG.backtest_months)
    parser.add_argument("--optimize", action="store_true", help="grid-search key parameters")
    parser.add_argument("--no-cache", action="store_true", help="force re-download of history")
    args = parser.parse_args()

    logger = TradingLogger(CONFIG.log_dir, "backtest.log", "backtest_trades.jsonl")
    logger.info("Fetching %d months of history for %s …", args.months, CONFIG.pairs)
    history = fetch_history(CONFIG, months=args.months, logger=logger, use_cache=not args.no_cache)

    bt = Backtester(CONFIG)
    results = bt.run_all(history)

    report = render_text_report(results)
    print("\n" + report + "\n")
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, "backtest_report.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)

    curve_path = os.path.join(REPORT_DIR, "equity_curve.png")
    if save_equity_curve(results, curve_path):
        print(f"Equity curve saved to {curve_path}")

    if args.optimize:
        print("\nOptimizing parameters (this may take a while)…\n")
        combos = optimize(CONFIG, history, logger=logger)
        print("Top 5 parameter combinations by net P&L:")
        print("-" * 60)
        for c in combos[:5]:
            p, m = c["params"], c["metrics"]
            print(
                f"ATR×{p['atr_sl_multiplier']} | RSI{p['rsi_long_range']} | "
                f"TP1={p['tp1_pct']:.0%} | step={p['trailing_step_pct']:.1%} "
                f"-> net={m['net_total']:+.2f}€ | PF={_fmt_pf(m['profit_factor'])} | "
                f"win={m['win_rate']:.0%} | trades={m['trades']}"
            )
        best = combos[0]["params"]
        print("\n👉 Suggested optimal parameters (validate before going live):")
        print(f"   {best}")


if __name__ == "__main__":
    main()
