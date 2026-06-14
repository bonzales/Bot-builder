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

from .backtest_engine import Backtester, BacktestResult, optimize, walk_forward
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
    any_ruined = False
    total_liq = 0
    for pair, r in results.items():
        m = r.metrics
        total_liq += r.liquidations
        flag = ""
        if r.ruined:
            any_ruined = True
            flag = "  <<< CONTO AZZERATO"
        elif r.liquidations:
            flag = f"  ({r.liquidations} liquidazioni)"
        lines.append(
            f"{pair:>9} | trades={m['trades']:>3} | win={m['win_rate']:.0%} | "
            f"PF={_fmt_pf(m['profit_factor'])} | net={m['net_total']:+.2f}€ | "
            f"maxDD={m['max_drawdown']:.2f}€ | final={r.final_capital:.2f}€{flag}"
        )
    if any_ruined or total_liq:
        lines.append("")
        lines.append(f"⚠️  Liquidazioni totali: {total_liq}")
        if any_ruined:
            lines.append("⚠️  Almeno un mercato ha AZZERATO il conto (perdita totale).")
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


def apply_aggressive_preset(cfg) -> None:
    """
    HIGH-RISK preset for educational backtesting only: all capital on a single
    trade, fixed 10x leverage, wide stop. This is the "try to double it fast"
    configuration — it also makes liquidation (total loss) very likely.
    """
    cfg.position_pct = 1.0          # all-in
    cfg.max_concurrent_trades = 1
    cfg.use_margin = True
    cfg.dynamic_leverage = False    # fixed leverage instead of risk-based
    cfg.min_leverage = 10.0
    cfg.max_leverage = 10.0
    cfg.atr_sl_multiplier = 6.0     # wide stop -> "let it ride" (liquidation can bite)
    cfg.daily_loss_limit_pct = 1.0  # effectively off, so we see the full picture


def apply_active_preset(cfg) -> None:
    """
    More active middle-ground preset: looser entries (3 of 4 conditions) for
    more frequent trades, moderate leverage capped at 5x (still risk-based and
    dynamic), and a slightly higher 2% risk per trade. Normal sizing (33%, up
    to 3 concurrent) and the protective stop stay in place.
    """
    cfg.min_conditions = 3          # 3 of 4 -> more trades
    cfg.use_margin = True
    cfg.dynamic_leverage = True
    cfg.max_leverage = 5.0
    cfg.risk_per_trade_pct = 0.02   # 2% per trade (vs 1% conservative)


def render_walkforward(wf: dict) -> str:
    p = wf["params"]
    ins = wf["in_sample"]
    oos = wf["out_of_sample"]

    def block(title, m):
        return (
            f"{title}\n"
            f"  Trade: {m['trades']} | Win: {m['win_rate']:.0%} | "
            f"PF: {_fmt_pf(m['profit_factor'])} | Net: {m['net_total']:+.2f}€"
        )

    oos_pf = oos["profit_factor"]
    oos_net = oos["net_total"]
    edge_real = (oos_pf > 1.0 and oos_net > 0 and oos["trades"] >= 10)
    verdict = (
        "✅ Regge anche fuori campione — potrebbe esserci un edge reale (da confermare con altri dati)."
        if edge_real else
        "❌ NON regge sui dati mai visti → era OVERFITTING. Da scartare."
    )

    return "\n".join([
        "=" * 60,
        "WALK-FORWARD — VALIDAZIONE FUORI CAMPIONE",
        "=" * 60,
        f"Parametri scelti sul training: ATR×{p['atr_sl_multiplier']} | "
        f"RSI{p['rsi_long_range']} | TP1={p['tp1_pct']:.0%} | step={p['trailing_step_pct']:.1%}",
        "",
        block("IN-SAMPLE (dati usati per ottimizzare):", ins),
        "",
        block("OUT-OF-SAMPLE (dati MAI visti):", oos),
        "",
        "-" * 60,
        verdict,
        "=" * 60,
    ])


def apply_original_preset(cfg) -> None:
    """
    The very first strategy, exactly as specified: all FOUR conditions required,
    MACD *exact* bullish/bearish crossover, RSI 35-50 (long) / 50-65 (short),
    spot 1x (no leverage). ATR stop + TP1 + breakeven + trailing as in the spec.
    """
    cfg.strategy_type = "pullback"
    cfg.macd_mode = "cross"            # exact crossover, not "momentum state"
    cfg.rsi_long_min, cfg.rsi_long_max = 35.0, 50.0
    cfg.rsi_short_min, cfg.rsi_short_max = 50.0, 65.0
    cfg.min_conditions = 4             # all four
    cfg.use_margin = False             # spot, 1x — no leverage/short margin
    cfg.atr_sl_multiplier = 1.5
    cfg.tp1_pct = 0.03
    cfg.trailing_step_pct = 0.007


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the setup-phase backtest")
    parser.add_argument("--months", type=int, default=CONFIG.backtest_months)
    parser.add_argument("--optimize", action="store_true", help="grid-search key parameters")
    parser.add_argument("--no-cache", action="store_true", help="force re-download of history")
    parser.add_argument("--aggressive", action="store_true",
                        help="HIGH-RISK preset: all-in, fixed 10x leverage, wide stop")
    parser.add_argument("--active", action="store_true",
                        help="More trades (3/4 conditions) + moderate 5x dynamic leverage, 2% risk")
    parser.add_argument("--original", action="store_true",
                        help="Original spec: 4 exact conditions, MACD crossover, RSI 35-50, spot 1x")
    parser.add_argument("--strategy", choices=["pullback", "breakout", "ichimoku", "meanrev"],
                        default=None, help="strategy style to backtest")
    parser.add_argument("--data-exchange", default=None,
                        help="venue for historical data (default binance; kraken is limited to ~720 candles)")
    parser.add_argument("--walkforward", action="store_true",
                        help="out-of-sample test: optimize on first 2/3, validate on unseen last 1/3")
    parser.add_argument("--train-frac", type=float, default=0.67,
                        help="fraction of data used for in-sample optimization (default 0.67)")
    args = parser.parse_args()

    if args.original:
        apply_original_preset(CONFIG)
        print("📜 STRATEGIA ORIGINALE: 4 condizioni esatte, MACD crossover, RSI 35-50, spot 1x.\n")

    if args.strategy:
        CONFIG.strategy_type = args.strategy
        print(f"📐 Strategia: {args.strategy}\n")

    if args.aggressive:
        apply_aggressive_preset(CONFIG)
        print("⚠️  MODALITÀ AGGRESSIVA: tutto il capitale, leva 10x fissa, stop largo.")
        print("    Backtest a scopo dimostrativo — rischio di azzerare il conto.\n")
    elif args.active:
        apply_active_preset(CONFIG)
        print("⚙️  MODALITÀ ATTIVA: 3/4 condizioni, leva dinamica max 5x, rischio 2%.\n")

    logger = TradingLogger(CONFIG.log_dir, "backtest.log", "backtest_trades.jsonl")
    logger.info("Fetching %d months of history for %s …", args.months, CONFIG.pairs)
    history = fetch_history(CONFIG, months=args.months, logger=logger,
                            use_cache=not args.no_cache, data_exchange=args.data_exchange)

    if args.walkforward:
        print("\n🔬 VALIDAZIONE FUORI CAMPIONE (walk-forward)…")
        print(f"   Ottimizzo sui primi {args.train_frac*100:.0f}% dei dati, "
              f"poi testo sul {(1-args.train_frac)*100:.0f}% MAI VISTO.\n")
        wf = walk_forward(CONFIG, history, logger=logger, train_frac=args.train_frac)
        print(render_walkforward(wf))
        return

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
