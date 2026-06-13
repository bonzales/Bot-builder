"""
strategy.py — Entry / exit signal logic.

The strategy combines four confirmations evaluated on the latest *closed*
candle. RSI is read from the 15m timeframe; everything else from the 1h frame.

LONG  (all true):  EMA20 > EMA50, RSI in [35,50], MACD bullish crossover,
                   OBV rising vs last 3 candles.
SHORT (all true):  EMA20 < EMA50, RSI in [50,65], MACD bearish crossover,
                   OBV falling vs last 3 candles.

Hard blocks (handled by the caller / risk manager, but volume spike is here):
  * daily loss limit reached
  * position already open on the pair
  * abnormal volume spike (> 300% of average)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

LONG = "long"
SHORT = "short"


@dataclass
class Signal:
    pair: str
    side: str                      # LONG / SHORT
    price: float                   # last close on 1h
    atr: float
    rsi: float
    reasons: List[str] = field(default_factory=list)
    conditions: Dict[str, bool] = field(default_factory=dict)

    @property
    def strategy_name(self) -> str:
        return "EMA+RSI+MACD+OBV"


def _macd_bullish_cross(df: pd.DataFrame) -> bool:
    """MACD line crosses above signal line on the latest closed candle."""
    if len(df) < 2:
        return False
    prev, last = df.iloc[-2], df.iloc[-1]
    return prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]


def _macd_bearish_cross(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, last = df.iloc[-2], df.iloc[-1]
    return prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]


def _obv_rising(df: pd.DataFrame, lookback: int) -> bool:
    if len(df) < lookback + 1:
        return False
    return df["obv"].iloc[-1] > df["obv"].iloc[-1 - lookback]


def _obv_falling(df: pd.DataFrame, lookback: int) -> bool:
    if len(df) < lookback + 1:
        return False
    return df["obv"].iloc[-1] < df["obv"].iloc[-1 - lookback]


def volume_spike(df: pd.DataFrame, mult: float) -> bool:
    """True when the latest volume is an abnormal spike (> mult * average)."""
    last = df.iloc[-1]
    vol_avg = last.get("vol_avg")
    if vol_avg is None or pd.isna(vol_avg) or vol_avg <= 0:
        return False
    return last["volume"] > mult * vol_avg


class Strategy:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def evaluate(
        self,
        pair: str,
        df_1h: pd.DataFrame,
        rsi_15m: pd.Series,
    ) -> Optional[Signal]:
        """
        Return a Signal (LONG/SHORT) if all conditions align, else None.
        ``df_1h`` must already contain indicator columns (add_all_indicators).
        ``rsi_15m`` is the RSI series computed on the 15m frame.
        """
        cfg = self.cfg
        # Need enough history for indicators to be valid.
        min_len = max(cfg.ema_slow, cfg.macd_slow, cfg.atr_period, cfg.obv_lookback) + 2
        if len(df_1h) < min_len or rsi_15m.dropna().empty:
            return None

        last = df_1h.iloc[-1]
        atr_val = float(last["atr"])
        price = float(last["close"])
        rsi_val = float(rsi_15m.dropna().iloc[-1])

        if pd.isna(atr_val) or atr_val <= 0:
            return None

        # Volume guard applies to both directions.
        if volume_spike(df_1h, cfg.volume_spike_mult):
            return None

        trend_up = last["ema_fast"] > last["ema_slow"]
        trend_down = last["ema_fast"] < last["ema_slow"]

        # ---- LONG ---- #
        long_conditions = {
            "ema20>ema50": bool(trend_up),
            f"rsi {cfg.rsi_long_min}-{cfg.rsi_long_max}": cfg.rsi_long_min <= rsi_val <= cfg.rsi_long_max,
            "macd_bull_cross": _macd_bullish_cross(df_1h),
            "obv_rising": _obv_rising(df_1h, cfg.obv_lookback),
        }
        if all(long_conditions.values()):
            return Signal(
                pair=pair, side=LONG, price=price, atr=atr_val, rsi=rsi_val,
                reasons=list(long_conditions.keys()), conditions=long_conditions,
            )

        # ---- SHORT ---- #
        if cfg.allow_short:
            short_conditions = {
                "ema20<ema50": bool(trend_down),
                f"rsi {cfg.rsi_short_min}-{cfg.rsi_short_max}": cfg.rsi_short_min <= rsi_val <= cfg.rsi_short_max,
                "macd_bear_cross": _macd_bearish_cross(df_1h),
                "obv_falling": _obv_falling(df_1h, cfg.obv_lookback),
            }
            if all(short_conditions.values()):
                return Signal(
                    pair=pair, side=SHORT, price=price, atr=atr_val, rsi=rsi_val,
                    reasons=list(short_conditions.keys()), conditions=short_conditions,
                )

        return None
