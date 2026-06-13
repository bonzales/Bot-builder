"""
reporting.py — Shared performance metrics for live reports and backtests.

Computes win rate, profit factor, max drawdown, Sharpe ratio and trade stats
from a list of closed-trade records (the JSONL ``trade_closed`` events or
backtest trade dicts).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional


def _net(t: Dict) -> float:
    return float(t.get("total_net", t.get("net", 0.0)))


def compute_metrics(trades: List[Dict]) -> Dict:
    if not trades:
        return {
            "trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
            "net_total": 0.0, "max_drawdown": 0.0, "sharpe": 0.0,
            "best": None, "worst": None, "avg_trade": 0.0,
        }
    nets = [_net(t) for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else math.inf

    # Equity curve & max drawdown
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    # Sharpe (per-trade returns, annualization left to caller context)
    mean = sum(nets) / len(nets)
    var = sum((n - mean) ** 2 for n in nets) / len(nets)
    std = math.sqrt(var)
    sharpe = (mean / std * math.sqrt(len(nets))) if std > 0 else 0.0

    best = max(trades, key=_net)
    worst = min(trades, key=_net)

    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "profit_factor": profit_factor,
        "net_total": sum(nets),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "best": {"pair": best.get("pair"), "net": _net(best)},
        "worst": {"pair": worst.get("pair"), "net": _net(worst)},
        "avg_trade": mean,
    }


def _parse_ts(t: Dict) -> Optional[datetime]:
    ts = t.get("ts") or t.get("closed_at")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _window(trades: List[Dict], days: int) -> List[Dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for t in trades:
        ts = _parse_ts(t)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            out.append(t)
    return out


def performance_summary(closed_trades: List[Dict], capital: float) -> str:
    """Text summary for the Telegram /report command (7d + 30d windows)."""
    def block(label: str, trades: List[Dict]) -> str:
        m = compute_metrics(trades)
        pf = "∞" if m["profit_factor"] == math.inf else f"{m['profit_factor']:.2f}"
        return (
            f"— {label} —\n"
            f"Trade: {m['trades']} | Win rate: {m['win_rate']:.0%}\n"
            f"P&L netto: {m['net_total']:+.2f}€ | Profit factor: {pf}\n"
            f"Max drawdown: {m['max_drawdown']:.2f}€ | Sharpe: {m['sharpe']:.2f}"
        )

    last7 = _window(closed_trades, 7)
    last30 = _window(closed_trades, 30)
    return (
        "📈 REPORT PERFORMANCE\n"
        f"Capitale attuale: {capital:.2f}€\n\n"
        f"{block('Ultimi 7 giorni', last7)}\n\n"
        f"{block('Ultimi 30 giorni', last30)}"
    )
