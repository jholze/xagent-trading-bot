"""Public market data for the oracle (Gate.io spot, no API keys)."""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger("market_oracle.client")

GATE_BASE = "https://api.gateio.ws/api/v4"


class MarketDataClient:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._session = requests.Session()

    def fetch_ticker(self, pair: str = "BTC_USDT") -> dict[str, Any]:
        """Return last price and 24h change_percentage for a Gate pair."""
        resp = self._session.get(
            f"{GATE_BASE}/spot/tickers",
            params={"currency_pair": pair},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return {}
        row = data[0] if isinstance(data, list) else data
        return {
            "pair": pair,
            "last": float(row.get("last") or 0),
            "change_percentage": float(row.get("change_percentage") or 0),
            "high_24h": float(row.get("high_24h") or 0),
            "low_24h": float(row.get("low_24h") or 0),
        }

    def fetch_candles(
        self,
        pair: str = "BTC_USDT",
        *,
        interval: str = "4h",
        limit: int = 24,
    ) -> list[dict[str, float]]:
        """Gate candlesticks: [t, vol, close, high, low, open, ...]"""
        resp = self._session.get(
            f"{GATE_BASE}/spot/candlesticks",
            params={"currency_pair": pair, "interval": interval, "limit": limit},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        rows = resp.json() or []
        out = []
        for r in rows:
            if not r or len(r) < 6:
                continue
            out.append(
                {
                    "t": float(r[0]),
                    "vol": float(r[1]),
                    "close": float(r[2]),
                    "high": float(r[3]),
                    "low": float(r[4]),
                    "open": float(r[5]),
                }
            )
        return out

    def fetch_features(self) -> dict[str, float]:
        """BTC/ETH 24h returns + simple 4h trend scores."""
        features: dict[str, float] = {}
        for label, pair in (("btc", "BTC_USDT"), ("eth", "ETH_USDT")):
            try:
                t = self.fetch_ticker(pair)
                features[f"{label}_last"] = float(t.get("last") or 0)
                features[f"{label}_ret_24h_pct"] = float(t.get("change_percentage") or 0)
            except Exception as e:
                log.warning("ticker %s failed: %s", pair, e)
            try:
                candles = self.fetch_candles(pair, interval="4h", limit=12)
                if len(candles) >= 3:
                    c0 = candles[-1]["close"]
                    c3 = candles[-4]["close"] if len(candles) >= 4 else candles[0]["close"]
                    if c3 > 0:
                        features[f"{label}_ret_12h_approx_pct"] = (c0 / c3 - 1.0) * 100.0
                    # crude trend: last close vs SMA of last 6 closes
                    closes = [c["close"] for c in candles[-6:]]
                    sma = sum(closes) / len(closes)
                    features[f"{label}_trend_4h"] = 1.0 if c0 >= sma else -1.0
            except Exception as e:
                log.warning("candles %s failed: %s", pair, e)
        return features
