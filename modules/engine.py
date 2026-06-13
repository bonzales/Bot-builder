"""
engine.py — The live/paper trading engine.

Orchestrates the decision loop:
  1. roll the trading day (reset daily counters at 00:00 UTC)
  2. refresh market data
  3. manage open positions (TP1 / trailing / stop) on the latest price
  4. scan for new entries when not paused / not halted

Also implements the BotController interface used by the Telegram commands and
persists state to disk so a crash + systemd restart resumes cleanly.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from . import telegram_bot as tg
from .indicators import add_all_indicators, compute_rsi_secondary
from .risk_manager import Position, RiskManager
from .strategy import LONG, make_strategy


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TradingEngine:
    def __init__(self, cfg, logger, data_engine, order_manager,
                 telegram_bot: Optional[tg.TelegramBot] = None,
                 paper: bool = True) -> None:
        self.cfg = cfg
        self.logger = logger
        self.data_engine = data_engine
        self.order_manager = order_manager
        self.telegram = telegram_bot
        self.paper = paper
        self.strategy = make_strategy(cfg)
        self.risk = RiskManager(cfg)
        self.open_positions: Dict[str, Position] = {}
        self.paused = False
        self.state_path = os.path.join(cfg.log_dir, cfg.state_file)
        self._running = False
        self._daily_alert_sent = False
        self._load_state()
        self._refresh_leverage_tiers()

    def _refresh_leverage_tiers(self) -> None:
        """Pull the real per-market margin tiers from Kraken (best-effort)."""
        if not self.cfg.use_margin:
            return
        try:
            tiers = self.data_engine.refresh_leverage_tiers(self.cfg.pairs)
            if tiers:
                self.cfg.kraken_leverage_tiers.update(tiers)
                self.logger.info("Leverage tiers refreshed from Kraken: %s", tiers)
        except Exception as exc:
            self.logger.warning(
                "Could not refresh leverage tiers (using defaults): %s", exc
            )

    # ================================================================== #
    # State persistence
    # ================================================================== #
    def _save_state(self) -> None:
        state = {
            "capital": self.risk.capital,
            "day_start_capital": self.risk.day_start_capital,
            "daily_realized_pnl": self.risk.daily_realized_pnl,
            "current_day": self.risk.current_day.isoformat(),
            "halted_today": self.risk.halted_today,
            "paused": self.paused,
            "positions": [p.to_dict() for p in self.open_positions.values()],
        }
        try:
            os.makedirs(self.cfg.log_dir, exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, default=str)
        except OSError as exc:
            self.logger.error("Failed to persist state: %s", exc)

    def _load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.error("Failed to load state: %s", exc)
            return
        self.risk.capital = state.get("capital", self.risk.capital)
        self.risk.day_start_capital = state.get("day_start_capital", self.risk.day_start_capital)
        self.risk.daily_realized_pnl = state.get("daily_realized_pnl", 0.0)
        self.risk.halted_today = state.get("halted_today", False)
        self.paused = state.get("paused", False)
        try:
            self.risk.current_day = datetime.fromisoformat(state["current_day"]).date()
        except Exception:
            self.risk.current_day = _utcnow().date()
        for pd_ in state.get("positions", []):
            try:
                pos = Position.from_dict(pd_)
                self.open_positions[pos.pair] = pos
            except Exception as exc:
                self.logger.error("Failed to restore position: %s", exc)
        self.logger.info(
            "State restored: capital=%.2f€, open=%d, paused=%s",
            self.risk.capital, len(self.open_positions), self.paused,
        )

    # ================================================================== #
    # Price helpers
    # ================================================================== #
    def _latest_price(self, pair: str, data: Dict) -> Optional[float]:
        price = self.data_engine.fetch_ticker_price(pair)
        if price is not None:
            return price
        df = data.get(pair, {}).get(self.cfg.primary_timeframe)
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
        return None

    # ================================================================== #
    # Position management
    # ================================================================== #
    def _manage_positions(self, data: Dict) -> None:
        for pair in list(self.open_positions.keys()):
            pos = self.open_positions[pair]
            price = self._latest_price(pair, data)
            if price is None:
                continue
            actions = self.risk.evaluate_position(pos, price)
            for action in actions:
                self._execute_action(pos, action)
                if pos.remaining_fraction <= 0:
                    break

    def _execute_action(self, pos: Position, action: Dict) -> None:
        kind = action["type"]
        if kind == "tp1":
            fraction = action["close_fraction"]
            qty = pos.quantity * fraction
            res = self.order_manager.market_exit(
                pos.pair, pos.side, qty, action["price"], reason="tp1_partial",
                leverage=pos.kraken_leverage,
            )
            fill = self.risk.register_partial_close(pos, res.price, fraction)
            self.logger.log_event("tp1", {**pos.to_dict(), **fill, "fill_price": res.price})
            if self.telegram:
                self.telegram.notify(
                    tg.partial_close_msg(pos, res.price, fill["net"], fraction)
                )

        elif kind == "trailing_update":
            self.logger.log_event(
                "trailing_update",
                {"id": pos.id, "pair": pos.pair, "new_sl": action["new_sl"]},
            )

        elif kind == "stop_hit":
            qty = pos.remaining_quantity()
            res = self.order_manager.market_exit(
                pos.pair, pos.side, qty, action["price"], reason="stop_loss",
                leverage=pos.kraken_leverage,
            )
            fill = self.risk.register_close(pos, res.price)
            self.logger.log_event(
                "trade_closed", {**pos.to_dict(), **fill, "fill_price": res.price}
            )
            if self.telegram:
                self.telegram.notify(
                    tg.trade_closed_msg(pos, res.price, fill["total_pct"], fill["total_net"])
                )
            self.open_positions.pop(pos.pair, None)
            self._maybe_alert_daily_limit()

    def _maybe_alert_daily_limit(self) -> None:
        if self.risk.daily_limit_reached() and not self._daily_alert_sent:
            self._daily_alert_sent = True
            if self.telegram:
                self.telegram.notify(
                    tg.daily_limit_msg(
                        self.risk.daily_realized_pnl, self.risk.daily_drawdown_pct
                    )
                )
            self.logger.warning(
                "Daily loss limit reached: %.2f€ — halting new trades until 00:00 UTC.",
                self.risk.daily_realized_pnl,
            )

    # ================================================================== #
    # Entry scanning
    # ================================================================== #
    def _scan_entries(self, data: Dict) -> None:
        if self.paused or self.risk.halted_today or self.risk.daily_limit_reached():
            return
        for pair in self.cfg.pairs:
            if pair in self.open_positions:
                continue
            pair_data = data.get(pair)
            if not pair_data:
                continue
            df_1h = pair_data.get(self.cfg.primary_timeframe)
            df_15m = pair_data.get(self.cfg.secondary_timeframe)
            if df_1h is None or df_15m is None or df_1h.empty or df_15m.empty:
                continue
            df_1h = add_all_indicators(df_1h, self.cfg)
            rsi_15m = compute_rsi_secondary(df_15m, self.cfg)
            signal = self.strategy.evaluate(pair, df_1h, rsi_15m)
            if signal is None:
                continue
            can, reason = self.risk.can_open(pair, list(self.open_positions.values()))
            if not can:
                self.logger.info("Signal on %s skipped: %s", pair, reason)
                continue
            self._open_position(signal)

    def _open_position(self, signal) -> None:
        size_eur = self.risk.position_size_eur()
        pos = self.risk.build_position(signal, size_eur)
        res = self.order_manager.market_entry(
            pos.pair, pos.side, pos.quantity, pos.entry_price,
            reason=signal.strategy_name, leverage=pos.kraken_leverage,
        )
        # Rebuild on the real fill price (live) so SL/TP/leverage/quantity stay
        # consistent with where we actually got filled.
        if res.price and abs(res.price - pos.entry_price) > 1e-12:
            signal.price = res.price
            pos = self.risk.build_position(signal, size_eur)
        self.open_positions[pos.pair] = pos
        self.logger.log_event("trade_opened", pos.to_dict())
        if self.telegram:
            sl_pct = (pos.sl_price - pos.entry_price) / pos.entry_price
            self.telegram.notify(tg.trade_opened_msg(pos, sl_pct, pos.tp1_price))

    # ================================================================== #
    # Main loop
    # ================================================================== #
    def run_once(self) -> None:
        if self.risk.roll_day_if_needed():
            self._daily_alert_sent = False
            self.logger.info("New trading day — daily counters reset.")
        data = self.data_engine.snapshot()
        if not data:
            self.logger.warning("No market data this cycle; skipping.")
            return
        self._manage_positions(data)
        self._scan_entries(data)
        self._save_state()

    def run_forever(self) -> None:
        self._running = True
        self.logger.info(
            "Engine started in %s mode (capital=%.2f€).",
            "PAPER" if self.paper else "LIVE", self.risk.capital,
        )
        while self._running:
            start = time.time()
            try:
                self.run_once()
            except Exception as exc:  # never die silently; systemd will also restart
                self.logger.error("Loop iteration failed: %s", exc)
            elapsed = time.time() - start
            time.sleep(max(0.0, self.cfg.refresh_seconds - elapsed))

    def stop(self) -> None:
        self._running = False

    # ================================================================== #
    # BotController interface (Telegram commands)
    # ================================================================== #
    def cmd_status(self) -> str:
        if not self.open_positions:
            return (
                f"📊 STATUS\nNessuna posizione aperta.\n"
                f"Capitale disponibile: {self.risk.capital:.2f}€\n"
                f"{'⏸ In pausa' if self.paused else '▶️ Operativo'}"
            )
        lines = ["📊 STATUS"]
        for pos in self.open_positions.values():
            price = self.data_engine.fetch_ticker_price(pos.pair) or pos.entry_price
            gain = pos.gain_pct(price)
            lines.append(
                f"{pos.pair} {pos.side.upper()} @ {pos.entry_price:.3f} | "
                f"now {price:.3f} ({gain:+.2%}) | phase {pos.phase} | "
                f"SL {pos.sl_price:.3f}"
            )
        lines.append(f"Capitale disponibile: {self.risk.capital:.2f}€")
        lines.append("⏸ In pausa" if self.paused else "▶️ Operativo")
        return "\n".join(lines)

    def _closed_trades(self) -> List[Dict]:
        return self.logger.read_events("trade_closed")

    def cmd_report(self) -> str:
        from .reporting import performance_summary
        closed = self._closed_trades()
        return performance_summary(closed, self.risk.capital)

    def cmd_pause(self) -> str:
        self.paused = True
        self._save_state()
        self.logger.log_event("command", {"command": "pause"})
        return "⏸ Bot in pausa. Nessun nuovo trade; posizioni aperte mantenute."

    def cmd_resume(self) -> str:
        self.paused = False
        self._save_state()
        self.logger.log_event("command", {"command": "resume"})
        return "▶️ Operatività ripresa."

    def cmd_config(self) -> str:
        cfg = self.cfg.as_public_dict()
        lines = ["⚙️ CONFIG"]
        for k, v in cfg.items():
            lines.append(f"{k}: {v}")
        return "\n".join(lines)

    def cmd_history(self) -> str:
        closed = self._closed_trades()[-10:]
        if not closed:
            return "📜 HISTORY\nNessun trade chiuso."
        lines = ["📜 HISTORY (ultimi 10)"]
        for t in closed:
            lines.append(
                f"{t.get('pair')} {str(t.get('side','')).upper()} "
                f"{t.get('total_pct',0):+.2%} ({t.get('total_net',0):+.2f}€)"
            )
        return "\n".join(lines)

    def cmd_drawdown(self) -> str:
        limit = self.risk.daily_loss_limit_eur
        used = -self.risk.daily_realized_pnl
        return (
            "📉 DRAWDOWN\n"
            f"Perdita giornaliera: {self.risk.daily_realized_pnl:+.2f}€ "
            f"({self.risk.daily_drawdown_pct:+.2%})\n"
            f"Limite giornaliero: -{limit:.2f}€ "
            f"(-{self.cfg.daily_loss_limit_pct:.0%})\n"
            f"{'🛑 LIMITE RAGGIUNTO' if self.risk.daily_limit_reached() else '✅ Entro i limiti'}"
        )
