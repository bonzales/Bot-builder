"""
order_manager.py — Order execution against Kraken (or simulated in paper mode).

Entries are market orders. Exits (partial TP1 close, stop/trailing close) are
triggered in software by the risk manager and executed as market orders for
reliability — the bot is the source of truth for stop levels rather than
relying on resting exchange stop orders that could be missed in a fast move.

Every order is logged with timestamp, price, quantity and reason. Network
errors are retried with exponential backoff (max 3 attempts).
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from .strategy import LONG, SHORT


class OrderResult:
    def __init__(self, success: bool, price: float, quantity: float,
                 order_id: Optional[str] = None, info: Optional[Dict] = None,
                 error: Optional[str] = None) -> None:
        self.success = success
        self.price = price
        self.quantity = quantity
        self.order_id = order_id
        self.info = info or {}
        self.error = error


class OrderManager:
    def __init__(self, cfg, logger, data_engine=None, paper: bool = True) -> None:
        self.cfg = cfg
        self.logger = logger
        self.data_engine = data_engine
        self.paper = paper

    # ------------------------------------------------------------------ #
    def _with_retry(self, fn, description: str):
        attempts = self.cfg.order_retry_attempts
        backoff = self.cfg.order_retry_backoff_seconds
        last_exc: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except Exception as exc:  # network / exchange error
                last_exc = exc
                self.logger.warning(
                    "%s failed (attempt %d/%d): %s", description, attempt, attempts, exc
                )
                if attempt < attempts:
                    time.sleep(backoff * (2 ** (attempt - 1)))
        raise RuntimeError(f"{description} failed after {attempts} attempts: {last_exc}")

    # ------------------------------------------------------------------ #
    def _ccxt_side(self, side: str, opening: bool) -> str:
        """Map internal side + open/close to a ccxt buy/sell."""
        if side == LONG:
            return "buy" if opening else "sell"
        # SHORT
        return "sell" if opening else "buy"

    # ------------------------------------------------------------------ #
    def _margin_params(self, leverage: int, reduce_only: bool = False) -> Dict:
        """Build ccxt order params for a Kraken margin order."""
        params: Dict = {}
        if leverage and leverage > 1:
            params["leverage"] = int(leverage)
            if reduce_only:
                # Close against the existing margin position rather than open a new one.
                params["reduce_only"] = True
        return params

    def market_entry(self, pair: str, side: str, quantity: float, ref_price: float,
                     reason: str, leverage: int = 1) -> OrderResult:
        if self.paper:
            result = OrderResult(True, ref_price, quantity, order_id=f"paper-{int(time.time()*1000)}")
        else:
            ccxt_side = self._ccxt_side(side, opening=True)
            params = self._margin_params(leverage)

            def _do():
                return self.data_engine.exchange.create_order(
                    pair, "market", ccxt_side, quantity, None, params
                )

            order = self._with_retry(_do, f"market_entry {pair}")
            fill_price = float(order.get("average") or order.get("price") or ref_price)
            result = OrderResult(True, fill_price, quantity, order_id=str(order.get("id")), info=order)

        self.logger.log_event(
            "order_entry",
            {
                "pair": pair, "side": side, "quantity": quantity, "leverage": leverage,
                "price": result.price, "reason": reason, "paper": self.paper,
                "order_id": result.order_id,
            },
        )
        return result

    def market_exit(self, pair: str, side: str, quantity: float, ref_price: float,
                    reason: str, leverage: int = 1) -> OrderResult:
        if self.paper:
            result = OrderResult(True, ref_price, quantity, order_id=f"paper-{int(time.time()*1000)}")
        else:
            ccxt_side = self._ccxt_side(side, opening=False)
            params = self._margin_params(leverage, reduce_only=True)

            def _do():
                return self.data_engine.exchange.create_order(
                    pair, "market", ccxt_side, quantity, None, params
                )

            order = self._with_retry(_do, f"market_exit {pair}")
            fill_price = float(order.get("average") or order.get("price") or ref_price)
            result = OrderResult(True, fill_price, quantity, order_id=str(order.get("id")), info=order)

        self.logger.log_event(
            "order_exit",
            {
                "pair": pair, "side": side, "quantity": quantity, "leverage": leverage,
                "price": result.price, "reason": reason, "paper": self.paper,
                "order_id": result.order_id,
            },
        )
        return result
