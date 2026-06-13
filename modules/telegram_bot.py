"""
telegram_bot.py — Bidirectional Telegram interface.

Outbound: trade open / partial close / full close / daily-limit alerts.
Inbound : owner-only quick commands
          /status /report /pause /resume /config /history /drawdown

Security: every update is checked against the owner TELEGRAM_CHAT_ID; any
other sender is silently ignored. The bot NEVER asks for confirmation to
trade — commands only read state or pause/resume new entries.

The Application runs polling in a background thread with its own event loop,
so the synchronous main trading loop can push notifications via
``notify()`` without blocking.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional, Protocol

try:
    from telegram import Update
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    _TELEGRAM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TELEGRAM_AVAILABLE = False


class BotController(Protocol):
    """Interface the trading engine must expose for command handling."""
    def cmd_status(self) -> str: ...
    def cmd_report(self) -> str: ...
    def cmd_pause(self) -> str: ...
    def cmd_resume(self) -> str: ...
    def cmd_config(self) -> str: ...
    def cmd_history(self) -> str: ...
    def cmd_drawdown(self) -> str: ...


# --------------------------------------------------------------------------- #
# Message formatters (match the spec templates)
# --------------------------------------------------------------------------- #
def fmt_price(v: float) -> str:
    return f"{v:,.3f}".replace(",", "_").replace(".", ",").replace("_", ".")


def trade_opened_msg(pos, sl_pct: float, tp1_price: float) -> str:
    direction = "LONG" if pos.side == "long" else "SHORT"
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    return (
        "🟢 TRADE APERTO\n"
        f"Coppia: {pos.pair}\n"
        f"Direzione: {direction}\n"
        f"Prezzo entrata: {fmt_price(pos.entry_price)}€\n"
        f"Dimensione: {pos.size_eur:.2f}€\n"
        f"Stop Loss: {fmt_price(pos.sl_price)}€ ({sl_pct:+.2%})\n"
        f"Take Profit 1: {fmt_price(tp1_price)}€ (+3%)\n"
        f"Strategia: {pos.strategy_name}\n"
        f"⏰ {ts}"
    )


def partial_close_msg(pos, price: float, profit_eur: float, fraction: float) -> str:
    return (
        "🟡 CHIUSURA PARZIALE\n"
        f"Coppia: {pos.pair}\n"
        f"Chiuso: {int(fraction*100)}% posizione\n"
        f"Prezzo: {fmt_price(price)}€ (+3%)\n"
        f"Profitto: {profit_eur:+.2f}€\n"
        f"SL spostato a breakeven: {fmt_price(pos.sl_price)}€\n"
        "Residuo in trailing stop 🔄"
    )


def trade_closed_msg(pos, exit_price: float, total_pct: float, net: float) -> str:
    opened = pos.opened_at
    delta = datetime.now(timezone.utc) - opened
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes = rem // 60
    return (
        "🔴 TRADE CHIUSO\n"
        f"Coppia: {pos.pair}\n"
        f"Prezzo uscita: {fmt_price(exit_price)}€\n"
        f"Risultato: {total_pct:+.2%}\n"
        f"Profitto netto: {net:+.2f}€\n"
        f"Durata: {hours}h {minutes}m"
    )


def daily_limit_msg(loss_eur: float, loss_pct: float) -> str:
    return (
        "⚠️ DAILY LOSS LIMIT RAGGIUNTO\n"
        f"Perdita giornaliera: {loss_eur:+.2f}€ ({loss_pct:+.1%})\n"
        "Bot in pausa fino alle 00:00 UTC"
    )


# --------------------------------------------------------------------------- #
class TelegramBot:
    def __init__(self, cfg, logger, controller: Optional[BotController] = None) -> None:
        self.cfg = cfg
        self.logger = logger
        self.controller = controller
        self.enabled = _TELEGRAM_AVAILABLE and cfg.credentials.has_telegram
        self._app: Optional["Application"] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._owner_id = (
            int(cfg.credentials.telegram_chat_id)
            if cfg.credentials.telegram_chat_id.lstrip("-").isdigit()
            else None
        )

    # --- owner gate --- #
    def _is_owner(self, update: "Update") -> bool:
        chat = update.effective_chat
        return chat is not None and self._owner_id is not None and chat.id == self._owner_id

    # --- command dispatch --- #
    async def _handle(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE",
                      attr: str) -> None:
        if not self._is_owner(update):
            self.logger.warning("Ignored Telegram command from non-owner chat.")
            return
        if self.controller is None:
            await update.message.reply_text("Bot non ancora inizializzato.")
            return
        try:
            text = getattr(self.controller, attr)()
        except Exception as exc:
            text = f"Errore nell'esecuzione del comando: {exc}"
        await update.message.reply_text(text)

    def _make_cmd(self, attr: str):
        async def _cmd(update, context):
            await self._handle(update, context, attr)
        return _cmd

    # --- lifecycle --- #
    def start(self) -> None:
        if not self.enabled:
            self.logger.warning(
                "Telegram disabled (missing token/chat id or library); "
                "notifications will be logged only."
            )
            return

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._app = (
                Application.builder().token(self.cfg.credentials.telegram_bot_token).build()
            )
            self._app.add_handler(CommandHandler("status", self._make_cmd("cmd_status")))
            self._app.add_handler(CommandHandler("report", self._make_cmd("cmd_report")))
            self._app.add_handler(CommandHandler("pause", self._make_cmd("cmd_pause")))
            self._app.add_handler(CommandHandler("resume", self._make_cmd("cmd_resume")))
            self._app.add_handler(CommandHandler("config", self._make_cmd("cmd_config")))
            self._app.add_handler(CommandHandler("history", self._make_cmd("cmd_history")))
            self._app.add_handler(CommandHandler("drawdown", self._make_cmd("cmd_drawdown")))
            # ignore everything else silently
            self._app.add_handler(MessageHandler(filters.ALL, self._ignore))
            self._loop.run_until_complete(self._app.initialize())
            self._loop.run_until_complete(self._app.start())
            self._loop.run_until_complete(self._app.updater.start_polling())
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, name="telegram", daemon=True)
        self._thread.start()
        self.logger.info("Telegram bot started (owner id=%s).", self._owner_id)

    async def _ignore(self, update, context):
        return

    # --- outbound --- #
    def notify(self, text: str) -> None:
        """Thread-safe notification from the sync trading loop."""
        if not self.enabled or self._loop is None or self._app is None:
            self.logger.info("[TELEGRAM] %s", text.replace("\n", " | "))
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._app.bot.send_message(chat_id=self._owner_id, text=text),
                self._loop,
            )
        except Exception as exc:
            self.logger.error("Telegram notify failed: %s", exc)
