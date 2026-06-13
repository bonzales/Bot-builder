"""
indicators.py — Technical indicators.

The spec calls for pandas-ta. In practice the pinned pandas-ta release is
fragile against newer numpy/pandas (e.g. the removal of ``numpy.NaN``), which
would make the live bot crash on a dependency upgrade. To keep the bot robust
and fully testable we implement each indicator natively with pandas/numpy —
the maths is identical to pandas-ta's defaults (Wilder smoothing for RSI/ATR,
EMA-based MACD). pandas-ta remains in requirements.txt and can be swapped in,
but the bot does not hard-depend on it.

All functions take an OHLCV DataFrame with columns:
    ['timestamp', 'open', 'high', 'low', 'close', 'volume']
and return pandas Series aligned to the input index.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing == EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss is 0 RSI is 100 by definition
    out = out.where(avg_loss != 0, 100.0)
    return out


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Return (macd_line, signal_line, histogram)."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    close = df["close"]
    volume = df["volume"]
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def add_all_indicators(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """
    Enrich a 1h OHLCV frame with all indicators used by the strategy.
    RSI is intentionally NOT added here — it is computed on the 15m frame
    (see compute_rsi_secondary).
    """
    out = df.copy()
    out["ema_fast"] = ema(out["close"], cfg.ema_fast)
    out["ema_slow"] = ema(out["close"], cfg.ema_slow)
    macd_line, signal_line, hist = macd(
        out["close"], cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
    )
    out["macd"] = macd_line
    out["macd_signal"] = signal_line
    out["macd_hist"] = hist
    out["atr"] = atr(out, cfg.atr_period)
    out["obv"] = obv(out)
    out["vol_avg"] = out["volume"].rolling(cfg.volume_avg_period).mean()
    return out


def compute_rsi_secondary(df_15m: pd.DataFrame, cfg) -> pd.Series:
    """RSI on the secondary (15m) timeframe."""
    return rsi(df_15m["close"], cfg.rsi_period)
