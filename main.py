"""
main.py — Entry point for the autonomous trading bot.

Usage:
    python main.py --mode paper      # simulated, no real orders (default)
    python main.py --mode live       # real orders on Kraken (requires API keys)

The bot runs H24/7, makes its own entry/exit decisions and only notifies the
owner via Telegram. Live mode refuses to start without Kraken credentials and
prints an explicit warning — never go live without completing backtest +
48h paper trading first.
"""

from __future__ import annotations

import argparse
import sys

from config import CONFIG, Mode
from modules.data_engine import DataEngine
from modules.engine import TradingEngine
from modules.logger import TradingLogger
from modules.order_manager import OrderManager
from modules.telegram_bot import TelegramBot


def build_engine(mode: str) -> TradingEngine:
    CONFIG.mode = mode
    logger = TradingLogger(CONFIG.log_dir, CONFIG.log_file, CONFIG.trade_log_file)
    paper = mode != Mode.LIVE

    if mode == Mode.LIVE and not CONFIG.credentials.has_kraken:
        logger.error("LIVE mode requires KRAKEN_API_KEY and KRAKEN_API_SECRET in .env")
        sys.exit(1)

    data_engine = DataEngine(CONFIG, logger=logger, authenticated=not paper)
    order_manager = OrderManager(CONFIG, logger, data_engine=data_engine, paper=paper)

    telegram = TelegramBot(CONFIG, logger)
    engine = TradingEngine(
        CONFIG, logger, data_engine, order_manager, telegram_bot=telegram, paper=paper
    )
    # Wire the controller and start listening for commands.
    telegram.controller = engine
    telegram.start()
    return engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous crypto trading bot")
    parser.add_argument(
        "--mode",
        choices=[Mode.PAPER, Mode.LIVE],
        default=CONFIG.mode if CONFIG.mode in (Mode.PAPER, Mode.LIVE) else Mode.PAPER,
        help="paper (default, simulated) or live (real orders)",
    )
    args = parser.parse_args()

    if args.mode == Mode.LIVE:
        print("⚠️  Starting in LIVE mode — real orders will be placed on Kraken.")
    engine = build_engine(args.mode)
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        engine.stop()
        engine.logger.info("Engine stopped by user.")


if __name__ == "__main__":
    main()
