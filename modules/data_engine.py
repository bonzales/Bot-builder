"""
data_engine.py — Market data from Kraken via ccxt.

Fetches OHLCV candles for the configured pairs on the primary (1h) and
secondary (15m) timeframes, returning tidy pandas DataFrames. Used both by
the live loop and the backtest data fetcher.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import pandas as pd

try:  # ccxt is an external dependency; keep import soft for test environments.
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None


OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def ohlcv_to_df(raw: List[list]) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=OHLCV_COLUMNS)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


class DataEngine:
    def __init__(self, cfg, logger=None, authenticated: bool = False) -> None:
        self.cfg = cfg
        self.logger = logger
        if ccxt is None:
            raise RuntimeError("ccxt is not installed; run `pip install -r requirements.txt`")

        params = {"enableRateLimit": True}
        if authenticated and cfg.credentials.has_kraken:
            params["apiKey"] = cfg.credentials.kraken_api_key
            params["secret"] = cfg.credentials.kraken_api_secret
        exchange_class = getattr(ccxt, cfg.exchange_id)
        self.exchange = exchange_class(params)
        self._markets_loaded = False

    def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            self.exchange.load_markets()
            self._markets_loaded = True

    def fetch_ohlcv(
        self,
        pair: str,
        timeframe: str,
        limit: int = 300,
        since: Optional[int] = None,
    ) -> pd.DataFrame:
        """Fetch a single batch of candles."""
        self._ensure_markets()
        raw = self.exchange.fetch_ohlcv(pair, timeframe=timeframe, since=since, limit=limit)
        return ohlcv_to_df(raw)

    def fetch_ohlcv_paginated(
        self,
        pair: str,
        timeframe: str,
        since: int,
        until: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Page backwards/forwards through history from ``since`` (ms) to ``until``.
        Used by the backtest data fetcher to assemble 12 months of candles.
        """
        self._ensure_markets()
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        all_rows: List[list] = []
        cursor = since
        until = until or self.exchange.milliseconds()
        while cursor < until:
            batch = self.exchange.fetch_ohlcv(pair, timeframe=timeframe, since=cursor, limit=720)
            if not batch:
                break
            all_rows.extend(batch)
            cursor = batch[-1][0] + tf_ms
            if len(batch) < 2:
                break
            time.sleep(self.exchange.rateLimit / 1000.0)
        df = ohlcv_to_df(all_rows).drop_duplicates(subset="timestamp").reset_index(drop=True)
        return df[df["timestamp"] < until].reset_index(drop=True)

    def snapshot(self) -> Dict[str, Dict[str, pd.DataFrame]]:
        """
        Return current candles for all configured pairs on both timeframes:
            { pair: { "1h": df, "15m": df } }
        """
        out: Dict[str, Dict[str, pd.DataFrame]] = {}
        for pair in self.cfg.pairs:
            try:
                out[pair] = {
                    self.cfg.primary_timeframe: self.fetch_ohlcv(
                        pair, self.cfg.primary_timeframe, self.cfg.candle_lookback
                    ),
                    self.cfg.secondary_timeframe: self.fetch_ohlcv(
                        pair, self.cfg.secondary_timeframe, self.cfg.candle_lookback
                    ),
                }
            except Exception as exc:  # network / exchange hiccup: skip this round
                if self.logger:
                    self.logger.warning("Data fetch failed for %s: %s", pair, exc)
        return out

    def fetch_ticker_price(self, pair: str) -> Optional[float]:
        try:
            ticker = self.exchange.fetch_ticker(pair)
            return float(ticker["last"])
        except Exception as exc:
            if self.logger:
                self.logger.warning("Ticker fetch failed for %s: %s", pair, exc)
            return None

    def refresh_leverage_tiers(self, pairs: List[str]) -> Dict[str, list]:
        """
        Read the real margin leverage tiers Kraken offers for each pair, so the
        bot never relies on hard-coded guesses (each market has its own max,
        e.g. BTC/EUR and SOL/EUR = 10x, SOL/GBP = 3x, some are spot-only).

        Returns { pair: [sorted int tiers] }; pairs with no margin are omitted.
        Uses the short-side (`leverage_sell`) list as the binding constraint,
        falling back to the long side.
        """
        self._ensure_markets()
        out: Dict[str, list] = {}
        for pair in pairs:
            try:
                market = self.exchange.market(pair)
                info = market.get("info", {}) if isinstance(market, dict) else {}
                sell = info.get("leverage_sell") or []
                buy = info.get("leverage_buy") or []
                tiers = sorted({int(float(x)) for x in (sell or buy)})
                if tiers:
                    out[pair] = tiers
                elif self.logger:
                    self.logger.warning("%s appears to be spot-only (no margin).", pair)
            except Exception as exc:
                if self.logger:
                    self.logger.warning("Leverage tier fetch failed for %s: %s", pair, exc)
        return out
