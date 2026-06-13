"""
paper_trading.py — 48h paper-trading simulation (no real money).

Runs the live engine in PAPER mode against real-time Kraken data for a fixed
duration, then prints a performance summary. This is the mandatory step
between backtest validation and going live.

    python -m tests.paper_trading --hours 48

Telegram notifications work normally if configured; orders are simulated.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone

from config import CONFIG, Mode
from modules.data_engine import DataEngine
from modules.engine import TradingEngine
from modules.logger import TradingLogger
from modules.order_manager import OrderManager
from modules.reporting import performance_summary
from modules.telegram_bot import TelegramBot


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-trading simulation")
    parser.add_argument("--hours", type=float, default=48.0, help="duration in hours")
    args = parser.parse_args()

    CONFIG.mode = Mode.PAPER
    logger = TradingLogger(CONFIG.log_dir, "paper_trading.log", "paper_trades.jsonl")
    data_engine = DataEngine(CONFIG, logger=logger, authenticated=False)
    order_manager = OrderManager(CONFIG, logger, data_engine=data_engine, paper=True)
    telegram = TelegramBot(CONFIG, logger)
    engine = TradingEngine(
        CONFIG, logger, data_engine, order_manager, telegram_bot=telegram, paper=True
    )
    telegram.controller = engine
    telegram.start()

    end_time = datetime.now(timezone.utc) + timedelta(hours=args.hours)
    logger.info("Paper trading started — running until %s UTC.", end_time.strftime("%Y-%m-%d %H:%M"))
    start_capital = engine.risk.capital

    while datetime.now(timezone.utc) < end_time:
        loop_start = time.time()
        try:
            engine.run_once()
        except Exception as exc:
            logger.error("Paper loop error: %s", exc)
        time.sleep(max(0.0, CONFIG.refresh_seconds - (time.time() - loop_start)))

    closed = logger.read_events("trade_closed")
    print("\n" + performance_summary(closed, engine.risk.capital))
    print(
        f"\nStart capital: {start_capital:.2f}€ -> End capital: {engine.risk.capital:.2f}€ "
        f"({(engine.risk.capital - start_capital):+.2f}€)"
    )
    logger.info("Paper trading finished. Final capital: %.2f€", engine.risk.capital)


if __name__ == "__main__":
    main()
