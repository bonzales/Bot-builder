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


def _macd_bullish(df: pd.DataFrame, mode: str) -> bool:
    """
    MACD confirmation for LONG.
      * "cross": the exact bullish crossover on the last closed candle (strict).
      * "state": MACD line above its signal line (bullish regime, looser).
    The strict "cross" rarely coincides with the other three filters, which is
    why the default is "state" — it keeps the MACD confirmation while letting
    the strategy actually trade.
    """
    if mode == "cross":
        return _macd_bullish_cross(df)
    last = df.iloc[-1]
    return last["macd"] > last["macd_signal"]


def _macd_bearish(df: pd.DataFrame, mode: str) -> bool:
    if mode == "cross":
        return _macd_bearish_cross(df)
    last = df.iloc[-1]
    return last["macd"] < last["macd_signal"]


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
        macd_mode = getattr(cfg, "macd_mode", "state")
        # How many of the 4 conditions must hold. The EMA trend is always
        # mandatory (it sets the direction); `min_conditions` then requires at
        # least (min_conditions - 1) of the remaining three (RSI / MACD / OBV).
        # 4 = strict (all), 3 = looser -> more trades.
        min_conditions = int(getattr(cfg, "min_conditions", 4))
        need_others = max(0, min_conditions - 1)

        # ---- LONG (EMA uptrend mandatory) ---- #
        if trend_up:
            others = {
                f"rsi {cfg.rsi_long_min}-{cfg.rsi_long_max}": cfg.rsi_long_min <= rsi_val <= cfg.rsi_long_max,
                f"macd_bull_{macd_mode}": _macd_bullish(df_1h, macd_mode),
                "obv_rising": _obv_rising(df_1h, cfg.obv_lookback),
            }
            if sum(others.values()) >= need_others:
                conditions = {"ema20>ema50": True, **others}
                return Signal(
                    pair=pair, side=LONG, price=price, atr=atr_val, rsi=rsi_val,
                    reasons=[k for k, v in conditions.items() if v], conditions=conditions,
                )

        # ---- SHORT (EMA downtrend mandatory) ---- #
        if cfg.allow_short and trend_down:
            others = {
                f"rsi {cfg.rsi_short_min}-{cfg.rsi_short_max}": cfg.rsi_short_min <= rsi_val <= cfg.rsi_short_max,
                f"macd_bear_{macd_mode}": _macd_bearish(df_1h, macd_mode),
                "obv_falling": _obv_falling(df_1h, cfg.obv_lookback),
            }
            if sum(others.values()) >= need_others:
                conditions = {"ema20<ema50": True, **others}
                return Signal(
                    pair=pair, side=SHORT, price=price, atr=atr_val, rsi=rsi_val,
                    reasons=[k for k, v in conditions.items() if v], conditions=conditions,
                )

        return None


class BreakoutStrategy:
    """
    Trend-following breakout (Donchian). Crypto trends strongly, so 'buy the
    breakout and ride it' is one of the more robust styles:
      LONG  : close breaks above the prior-N-bar high, with EMA uptrend.
      SHORT : close breaks below the prior-N-bar low, with EMA downtrend.
    Same ATR stop / TP1 / trailing / leverage machinery as the other strategies.
    """
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    @property
    def name(self) -> str:
        return "Breakout(Donchian)+EMA"

    def evaluate(self, pair, df_1h, rsi_15m=None):
        cfg = self.cfg
        min_len = max(getattr(cfg, "donchian_period", 20), cfg.ema_slow, cfg.atr_period) + 2
        if len(df_1h) < min_len:
            return None
        last = df_1h.iloc[-1]
        atr_val = float(last["atr"])
        price = float(last["close"])
        if pd.isna(atr_val) or atr_val <= 0 or pd.isna(last.get("donchian_high")):
            return None
        if volume_spike(df_1h, cfg.volume_spike_mult):
            return None

        use_trend = getattr(cfg, "breakout_trend_filter", True)
        trend_up = (last["ema_fast"] > last["ema_slow"]) or not use_trend
        trend_down = (last["ema_fast"] < last["ema_slow"]) or not use_trend

        if price > float(last["donchian_high"]) and trend_up:
            sig = Signal(pair=pair, side=LONG, price=price, atr=atr_val, rsi=50.0,
                         reasons=["breakout_high", "ema_up"])
            sig.strategy_label = self.name
            return sig
        if cfg.allow_short and price < float(last["donchian_low"]) and trend_down:
            sig = Signal(pair=pair, side=SHORT, price=price, atr=atr_val, rsi=50.0,
                         reasons=["breakout_low", "ema_down"])
            sig.strategy_label = self.name
            return sig
        return None


class IchimokuStrategy:
    """
    Ichimoku trend system (multiday-style on 1h):
      LONG  : price above the cloud (max of Senkou A/B) AND Tenkan > Kijun.
      SHORT : price below the cloud (min of Senkou A/B) AND Tenkan < Kijun.
    """
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    @property
    def name(self) -> str:
        return "Ichimoku"

    def evaluate(self, pair, df_1h, rsi_15m=None):
        cfg = self.cfg
        min_len = getattr(cfg, "ichimoku_senkou_b", 52) + getattr(cfg, "ichimoku_shift", 26) + 2
        if len(df_1h) < min_len:
            return None
        last = df_1h.iloc[-1]
        atr_val = float(last["atr"])
        price = float(last["close"])
        for col in ("tenkan", "kijun", "senkou_a", "senkou_b"):
            if pd.isna(last.get(col)):
                return None
        if pd.isna(atr_val) or atr_val <= 0:
            return None
        if volume_spike(df_1h, cfg.volume_spike_mult):
            return None

        cloud_top = max(last["senkou_a"], last["senkou_b"])
        cloud_bot = min(last["senkou_a"], last["senkou_b"])
        if price > cloud_top and last["tenkan"] > last["kijun"]:
            sig = Signal(pair=pair, side=LONG, price=price, atr=atr_val, rsi=50.0,
                         reasons=["above_cloud", "tenkan>kijun"])
            sig.strategy_label = self.name
            return sig
        if cfg.allow_short and price < cloud_bot and last["tenkan"] < last["kijun"]:
            sig = Signal(pair=pair, side=SHORT, price=price, atr=atr_val, rsi=50.0,
                         reasons=["below_cloud", "tenkan<kijun"])
            sig.strategy_label = self.name
            return sig
        return None


def make_strategy(cfg):
    """Factory: pick the strategy implementation from cfg.strategy_type."""
    stype = getattr(cfg, "strategy_type", "pullback")
    if stype == "breakout":
        return BreakoutStrategy(cfg)
    if stype == "ichimoku":
        return IchimokuStrategy(cfg)
    return Strategy(cfg)
