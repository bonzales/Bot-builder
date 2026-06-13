"""
logger.py — Centralized logging for the bot.

Two channels:
  * A standard rotating text log (human readable, INFO+).
  * A structured JSON-lines trade log (machine readable) used by /report,
    /history and the monthly review.

Every order, decision and lifecycle event flows through here so the monthly
review can reconstruct exactly what happened.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradingLogger:
    def __init__(
        self,
        log_dir: str,
        log_file: str = "trading-bot.log",
        trade_log_file: str = "trades.jsonl",
        level: int = logging.INFO,
    ) -> None:
        os.makedirs(log_dir, exist_ok=True)
        self.log_dir = log_dir
        self.trade_log_path = os.path.join(log_dir, trade_log_file)

        self._logger = logging.getLogger("trading-bot")
        self._logger.setLevel(level)
        self._logger.propagate = False

        if not self._logger.handlers:
            fmt = logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            file_handler = RotatingFileHandler(
                os.path.join(log_dir, log_file),
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
            )
            file_handler.setFormatter(fmt)
            self._logger.addHandler(file_handler)

            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(fmt)
            self._logger.addHandler(stream_handler)

    # --- text log passthroughs --- #
    def info(self, msg: str, *args: Any) -> None:
        self._logger.info(msg, *args)

    def warning(self, msg: str, *args: Any) -> None:
        self._logger.warning(msg, *args)

    def error(self, msg: str, *args: Any) -> None:
        self._logger.error(msg, *args)

    def debug(self, msg: str, *args: Any) -> None:
        self._logger.debug(msg, *args)

    # --- structured trade events --- #
    def log_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Append a structured event to the JSONL trade log."""
        record = {"ts": _utcnow_iso(), "event": event_type, **payload}
        try:
            with open(self.trade_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:  # never let logging crash the bot
            self._logger.error("Failed to write trade log: %s", exc)
        self._logger.info("[%s] %s", event_type, json.dumps(payload, default=str))

    def read_events(self, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Read back structured events (used by /history, /report, reviews)."""
        if not os.path.exists(self.trade_log_path):
            return []
        events: List[Dict[str, Any]] = []
        with open(self.trade_log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type is None or rec.get("event") == event_type:
                    events.append(rec)
        return events
