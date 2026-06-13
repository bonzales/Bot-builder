"""
risk_manager.py — Position sizing, stop loss, 3-phase trade management and
daily risk limits.

Position lifecycle (mirrored for SHORT):
  Phase 1 — Entry:       SL = entry -/+ ATR * 1.5
  Phase 2 — TP1 (+3%):   close 40%, SL -> breakeven (+/-0.1% to cover fees)
  Phase 3 — Trailing:    on the remaining 60%, the SL trails at 70% of the
                         peak favorable gain (every +1% gain -> SL +0.7%).
                         Examples: +4%->+2.8%, +5%->+3.5%, +6%->+4.2%.
                         The SL only ever ratchets in the profitable direction.

Daily guards:
  * max loss -5% of capital -> pause until 00:00 UTC
  * max 3 concurrent positions
  * no second position on a pair already held
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .strategy import LONG, SHORT


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pick_kraken_leverage(cfg, pair: str, side: str, effective_leverage: float) -> int:
    """
    Map a desired *effective* leverage to a Kraken-supported integer tier for
    the API ``leverage`` param. We pick the smallest available tier >= the
    needed leverage (>=2 for shorts, since they require margin). The effective
    exposure/risk is governed by position notional + the ATR stop, not by this
    tier — a higher tier only frees up margin, it does not increase our loss.
    """
    tiers = cfg.kraken_leverage_tiers.get(pair, cfg.default_leverage_tiers)
    tiers = sorted(int(t) for t in tiers)
    needed = max(2 if side == SHORT else 1, math.ceil(effective_leverage))
    for t in tiers:
        if t >= needed:
            return t
    return tiers[-1] if tiers else max(needed, 2)


@dataclass
class Position:
    pair: str
    side: str
    entry_price: float
    atr: float
    size_eur: float            # collateral/margin committed at entry (33% of capital)
    quantity: float            # base-asset units at entry (== notional / entry)
    sl_price: float
    tp1_price: float
    initial_sl_price: float
    leverage: float = 1.0          # effective leverage (notional / collateral)
    kraken_leverage: int = 1       # tier sent to Kraken's API (>=2 for margin)
    is_margin: bool = False        # margin position (short, or leveraged long)
    strategy_name: str = "EMA+RSI+MACD+OBV"
    phase: int = 1
    remaining_fraction: float = 1.0
    realized_pnl_eur: float = 0.0
    fees_paid: float = 0.0
    peak_price: float = 0.0    # most favorable price seen (for trailing)
    opened_at: datetime = field(default_factory=_utcnow)
    tp1_done: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def __post_init__(self) -> None:
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price

    # --- helpers --- #
    def gain_pct(self, price: float) -> float:
        if self.side == LONG:
            return (price - self.entry_price) / self.entry_price
        return (self.entry_price - price) / self.entry_price

    def remaining_quantity(self) -> float:
        return self.quantity * self.remaining_fraction

    @property
    def notional(self) -> float:
        return self.quantity * self.entry_price

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "pair": self.pair,
            "side": self.side,
            "entry_price": self.entry_price,
            "atr": self.atr,
            "size_eur": self.size_eur,
            "quantity": self.quantity,
            "sl_price": self.sl_price,
            "tp1_price": self.tp1_price,
            "initial_sl_price": self.initial_sl_price,
            "leverage": self.leverage,
            "kraken_leverage": self.kraken_leverage,
            "is_margin": self.is_margin,
            "phase": self.phase,
            "remaining_fraction": self.remaining_fraction,
            "realized_pnl_eur": self.realized_pnl_eur,
            "fees_paid": self.fees_paid,
            "peak_price": self.peak_price,
            "opened_at": self.opened_at.isoformat(),
            "tp1_done": self.tp1_done,
            "strategy_name": self.strategy_name,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Position":
        d = dict(d)
        d["opened_at"] = datetime.fromisoformat(d["opened_at"])
        return cls(**d)


class RiskManager:
    def __init__(self, cfg, starting_capital: Optional[float] = None) -> None:
        self.cfg = cfg
        self.capital = starting_capital if starting_capital is not None else cfg.initial_capital
        self.day_start_capital = self.capital
        self.daily_realized_pnl = 0.0
        self.current_day = _utcnow().date()
        self.halted_today = False

    # ------------------------------------------------------------------ #
    # Daily accounting
    # ------------------------------------------------------------------ #
    def roll_day_if_needed(self, now: Optional[datetime] = None) -> bool:
        now = now or _utcnow()
        if now.date() != self.current_day:
            self.current_day = now.date()
            self.day_start_capital = self.capital
            self.daily_realized_pnl = 0.0
            self.halted_today = False
            return True
        return False

    @property
    def daily_loss_limit_eur(self) -> float:
        return self.day_start_capital * self.cfg.daily_loss_limit_pct

    @property
    def daily_drawdown_pct(self) -> float:
        if self.day_start_capital <= 0:
            return 0.0
        return self.daily_realized_pnl / self.day_start_capital

    def daily_limit_reached(self) -> bool:
        return self.daily_realized_pnl <= -self.daily_loss_limit_eur

    # ------------------------------------------------------------------ #
    # Entry gating
    # ------------------------------------------------------------------ #
    def can_open(self, pair: str, open_positions: List[Position]) -> tuple[bool, str]:
        self.roll_day_if_needed()
        if self.halted_today or self.daily_limit_reached():
            return False, "daily_loss_limit"
        if len(open_positions) >= self.cfg.max_concurrent_trades:
            return False, "max_concurrent_trades"
        if any(p.pair == pair for p in open_positions):
            return False, "position_already_open"
        if self.position_size_eur() <= 0:
            return False, "insufficient_capital"
        return True, "ok"

    def position_size_eur(self) -> float:
        return round(self.capital * self.cfg.position_pct, 2)

    def select_leverage(self, signal) -> float:
        """
        Risk-based dynamic leverage. We want the loss if the ATR stop is hit to
        be ~`risk_per_trade_pct` of capital:

            loss_at_stop = stop_distance_pct * notional
                         = stop_distance_pct * (position_pct * capital * L)
            set == risk_per_trade_pct * capital
            =>  L = risk_per_trade_pct / (position_pct * stop_distance_pct)

        Tighter stop (calm market) -> higher L; wider stop (volatile) -> lower L.
        Clamped to [min_leverage, max_leverage]. SHORT always uses margin.
        """
        cfg = self.cfg
        if not cfg.use_margin:
            return 1.0
        if not cfg.dynamic_leverage:
            return max(cfg.min_leverage, 1.0)
        stop_distance_pct = (signal.atr * cfg.atr_sl_multiplier) / signal.price
        if stop_distance_pct <= 0:
            return cfg.min_leverage
        lev = cfg.risk_per_trade_pct / (cfg.position_pct * stop_distance_pct)
        lev = max(cfg.min_leverage, min(cfg.max_leverage, lev))
        # A short can never be below 1x effective; it still needs margin.
        return round(lev, 2)

    # ------------------------------------------------------------------ #
    # Building a position from a signal
    # ------------------------------------------------------------------ #
    def build_position(self, signal, size_eur: float) -> Position:
        cfg = self.cfg
        entry = signal.price
        atr_dist = signal.atr * cfg.atr_sl_multiplier
        if signal.side == LONG:
            sl = entry - atr_dist
            tp1 = entry * (1 + cfg.tp1_pct)
        else:
            sl = entry + atr_dist
            tp1 = entry * (1 - cfg.tp1_pct)

        leverage = self.select_leverage(signal)
        notional = size_eur * leverage
        quantity = notional / entry if entry > 0 else 0.0
        # Margin is needed for any short, or for a long with leverage > 1.
        is_margin = cfg.use_margin and (signal.side == SHORT or leverage > 1.0)
        kraken_leverage = (
            pick_kraken_leverage(cfg, signal.pair, signal.side, leverage)
            if is_margin else 1
        )
        return Position(
            pair=signal.pair,
            side=signal.side,
            entry_price=entry,
            atr=signal.atr,
            size_eur=size_eur,
            quantity=quantity,
            sl_price=sl,
            tp1_price=tp1,
            initial_sl_price=sl,
            leverage=leverage,
            kraken_leverage=kraken_leverage,
            is_margin=is_margin,
            strategy_name=signal.strategy_name,
        )

    # ------------------------------------------------------------------ #
    # Per-tick management -> returns a list of actions for the order layer
    # ------------------------------------------------------------------ #
    def evaluate_position(self, pos: Position, price: float) -> List[Dict]:
        """
        Inspect a position against the current price and return ordered actions:
          {"type": "stop_hit", ...}          -> close remaining at SL
          {"type": "tp1", ...}               -> partial close + SL to breakeven
          {"type": "trailing_update", ...}   -> SL ratcheted up
        The caller executes the actions (paper or live) and then calls
        register_partial_close / register_close.
        """
        actions: List[Dict] = []
        cfg = self.cfg

        # Track peak favorable price for trailing.
        if pos.side == LONG:
            pos.peak_price = max(pos.peak_price, price)
        else:
            pos.peak_price = min(pos.peak_price, price)

        # 1) Stop loss check first (protective).
        stop_hit = (
            price <= pos.sl_price if pos.side == LONG else price >= pos.sl_price
        )
        if stop_hit:
            actions.append({"type": "stop_hit", "price": pos.sl_price})
            return actions

        # 2) TP1 (only once).
        if not pos.tp1_done:
            tp1_reached = (
                price >= pos.tp1_price if pos.side == LONG else price <= pos.tp1_price
            )
            if tp1_reached:
                breakeven = (
                    pos.entry_price * (1 + cfg.breakeven_buffer_pct)
                    if pos.side == LONG
                    else pos.entry_price * (1 - cfg.breakeven_buffer_pct)
                )
                actions.append(
                    {
                        "type": "tp1",
                        "price": pos.tp1_price,
                        "close_fraction": cfg.tp1_close_fraction,
                        "new_sl": breakeven,
                    }
                )
                # State transition applied immediately so the same tick can
                # also trail if price has run far past TP1.
                pos.tp1_done = True
                pos.phase = 3
                pos.sl_price = breakeven

        # 3) Trailing on the remaining position (phase 3).
        if pos.tp1_done:
            ratio = cfg.trailing_step_pct / cfg.trailing_trigger_pct  # 0.7
            peak_gain = pos.gain_pct(pos.peak_price)
            locked = max(cfg.breakeven_buffer_pct, peak_gain * ratio)
            if pos.side == LONG:
                candidate = pos.entry_price * (1 + locked)
                if candidate > pos.sl_price:
                    pos.sl_price = candidate
                    actions.append({"type": "trailing_update", "new_sl": candidate})
            else:
                candidate = pos.entry_price * (1 - locked)
                if candidate < pos.sl_price:
                    pos.sl_price = candidate
                    actions.append({"type": "trailing_update", "new_sl": candidate})

        return actions

    # ------------------------------------------------------------------ #
    # PnL accounting
    # ------------------------------------------------------------------ #
    def _gross_pnl(self, pos: Position, exit_price: float, quantity: float) -> float:
        if pos.side == LONG:
            return (exit_price - pos.entry_price) * quantity
        return (pos.entry_price - exit_price) * quantity

    def _margin_fee(self, pos: Position, qty: float, close_time: Optional[datetime]) -> float:
        """Kraken margin financing: opening fee + rollover every N hours, on the
        notional of the quantity being closed. Zero for plain spot positions."""
        if not pos.is_margin:
            return 0.0
        notional = qty * pos.entry_price
        close_time = close_time or _utcnow()
        hours = max(0.0, (close_time - pos.opened_at).total_seconds() / 3600.0)
        periods = math.ceil(hours / self.cfg.rollover_hours) if hours > 0 else 0
        open_fee = notional * self.cfg.margin_open_fee
        rollover = notional * self.cfg.margin_rollover_fee * periods
        return open_fee + rollover

    def register_partial_close(self, pos: Position, exit_price: float, fraction: float,
                               close_time: Optional[datetime] = None) -> Dict:
        qty = pos.quantity * fraction
        gross = self._gross_pnl(pos, exit_price, qty)
        trade_fee = (pos.entry_price + exit_price) * qty * self.cfg.taker_fee
        margin_fee = self._margin_fee(pos, qty, close_time)
        fee = trade_fee + margin_fee
        net = gross - fee
        pos.remaining_fraction = round(pos.remaining_fraction - fraction, 6)
        pos.realized_pnl_eur += net
        pos.fees_paid += fee
        self.capital += net
        self.daily_realized_pnl += net
        return {"gross": gross, "fee": fee, "margin_fee": margin_fee, "net": net, "quantity": qty}

    def register_close(self, pos: Position, exit_price: float,
                       close_time: Optional[datetime] = None) -> Dict:
        fraction = pos.remaining_fraction
        qty = pos.quantity * fraction
        gross = self._gross_pnl(pos, exit_price, qty)
        trade_fee = (pos.entry_price + exit_price) * qty * self.cfg.taker_fee
        margin_fee = self._margin_fee(pos, qty, close_time)
        fee = trade_fee + margin_fee
        net = gross - fee
        pos.realized_pnl_eur += net
        pos.fees_paid += fee
        pos.remaining_fraction = 0.0
        self.capital += net
        self.daily_realized_pnl += net
        total_pct = pos.realized_pnl_eur / pos.size_eur if pos.size_eur else 0.0
        if self.daily_limit_reached():
            self.halted_today = True
        return {
            "gross": gross,
            "fee": fee,
            "margin_fee": margin_fee,
            "net": net,
            "quantity": qty,
            "total_net": pos.realized_pnl_eur,
            "total_pct": total_pct,
        }
