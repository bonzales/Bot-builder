"""
data_fetcher.py — Download and cache historical Kraken candles for backtesting.

Pulls >= 12 months of 1h candles (and 15m for RSI) for each configured pair
and caches them as CSV under ``backtest/data/`` so repeated runs are fast and
offline-friendly.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Dict

import pandas as pd

from modules.data_engine import DataEngine, OHLCV_COLUMNS

CACHE_DIR = os.path.join(os.path.dirname(__file__), "data")


def _cache_path(pair: str, timeframe: str, exchange: str) -> str:
    safe = pair.replace("/", "_")
    return os.path.join(CACHE_DIR, f"{exchange}_{safe}_{timeframe}.csv")


def _map_symbol(pair: str, exchange: str) -> str:
    """Translate a config pair to the data venue's symbol. Most venues with
    deep history quote in USDT, so EUR pairs map to USDT for backtest data."""
    if exchange == "kraken":
        return pair
    return pair.replace("/EUR", "/USDT")


def fetch_history(cfg, months: int = 12, logger=None, use_cache: bool = True,
                  data_exchange: str = None) -> Dict[str, Dict[str, pd.DataFrame]]:
    """
    Return { pair: { "1h": df, "15m": df } } covering ``months`` of history,
    pulled from ``data_exchange`` (defaults to cfg.backtest_data_exchange).
    """
    data_exchange = data_exchange or getattr(cfg, "backtest_data_exchange", "binance")
    os.makedirs(CACHE_DIR, exist_ok=True)
    engine = DataEngine(cfg, logger=logger, authenticated=False, exchange_id=data_exchange)
    since = int((datetime.now(timezone.utc) - timedelta(days=months * 31)).timestamp() * 1000)

    out: Dict[str, Dict[str, pd.DataFrame]] = {}
    for pair in cfg.pairs:
        symbol = _map_symbol(pair, data_exchange)
        out[pair] = {}
        for tf in (cfg.primary_timeframe, cfg.secondary_timeframe):
            path = _cache_path(pair, tf, data_exchange)
            if use_cache and os.path.exists(path):
                df = pd.read_csv(path)
                if logger:
                    logger.info("Loaded cached %s %s (%d candles).", pair, tf, len(df))
            else:
                if logger:
                    logger.info("Downloading %s %s history from %s (as %s)…",
                                pair, tf, data_exchange, symbol)
                df = engine.fetch_ohlcv_paginated(symbol, tf, since=since)
                df.to_csv(path, index=False)
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            out[pair][tf] = df.reset_index(drop=True)
            if logger:
                logger.info("%s %s: %d candles (~%d days).", pair, tf, len(df),
                            len(df) // (24 if tf == "1h" else 96))
    return out


def load_cached(cfg, data_exchange: str = None) -> Dict[str, Dict[str, pd.DataFrame]]:
    """Load only from cache (raises if missing)."""
    data_exchange = data_exchange or getattr(cfg, "backtest_data_exchange", "binance")
    out: Dict[str, Dict[str, pd.DataFrame]] = {}
    for pair in cfg.pairs:
        out[pair] = {}
        for tf in (cfg.primary_timeframe, cfg.secondary_timeframe):
            path = _cache_path(pair, tf, data_exchange)
            if not os.path.exists(path):
                raise FileNotFoundError(f"No cached data for {pair} {tf}; run fetch_history first.")
            df = pd.read_csv(path)
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            out[pair][tf] = df.reset_index(drop=True)
    return out
