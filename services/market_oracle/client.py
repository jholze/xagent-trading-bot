"""Public market data for the oracle (Gate.io spot, no API keys)."""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger("market_oracle.client")

GATE_BASE = "https://api.gateio.ws/api/v4"
BINANCE_FAPI = "https://fapi.binance.com"


def _ema(closes: list[float], period: int) -> float | None:
    if period <= 0 or len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / float(period)
    for v in closes[period:]:
        e = float(v) * k + e * (1.0 - k)
    return e


def _ret_pct(closes: list[float], bars_back: int) -> float | None:
    """Return % change from close[-1-bars_back] to close[-1]."""
    if bars_back < 1 or len(closes) < bars_back + 1:
        return None
    a = closes[-(bars_back + 1)]
    b = closes[-1]
    if a <= 0:
        return None
    return (b / a - 1.0) * 100.0


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

    def _enrich_from_candles(self, features: dict[str, float], label: str, pair: str) -> None:
        # 1h: ret_1h (need 2 bars), optional multi-bar
        try:
            c1h = self.fetch_candles(pair, interval="1h", limit=8)
            closes_1h = [c["close"] for c in c1h]
            r1 = _ret_pct(closes_1h, 1)
            if r1 is not None:
                features[f"{label}_ret_1h_pct"] = r1
        except Exception as e:
            log.warning("1h candles %s failed: %s", pair, e)

        # 4h: ret_4h (~1 bar), ret_12h (~3 bars), EMA20/50, trend
        try:
            c4h = self.fetch_candles(pair, interval="4h", limit=60)
            closes_4h = [c["close"] for c in c4h]
            r4 = _ret_pct(closes_4h, 1)
            if r4 is not None:
                features[f"{label}_ret_4h_pct"] = r4
            r12 = _ret_pct(closes_4h, 3)
            if r12 is not None:
                features[f"{label}_ret_12h_approx_pct"] = r12

            ema20 = _ema(closes_4h, 20)
            ema50 = _ema(closes_4h, 50)
            if ema20 is not None:
                features[f"{label}_ema20_4h"] = ema20
            if ema50 is not None:
                features[f"{label}_ema50_4h"] = ema50
            if closes_4h and ema20 is not None:
                c0 = closes_4h[-1]
                features[f"{label}_above_ema20_4h"] = 1.0 if c0 >= ema20 else 0.0
                if ema50 is not None:
                    features[f"{label}_above_ema50_4h"] = 1.0 if c0 >= ema50 else 0.0
                    # +1 stack up, -1 stack down, 0 mixed
                    if c0 >= ema20 and ema20 >= ema50:
                        features[f"{label}_trend_4h"] = 1.0
                    elif c0 < ema20 and ema20 < ema50:
                        features[f"{label}_trend_4h"] = -1.0
                    else:
                        features[f"{label}_trend_4h"] = 0.0
                else:
                    features[f"{label}_trend_4h"] = 1.0 if c0 >= ema20 else -1.0
            elif len(closes_4h) >= 6:
                # fallback SMA like MVP
                c0 = closes_4h[-1]
                sma = sum(closes_4h[-6:]) / 6.0
                features[f"{label}_trend_4h"] = 1.0 if c0 >= sma else -1.0
        except Exception as e:
            log.warning("4h candles %s failed: %s", pair, e)

        # 1d: ret_7d
        try:
            c1d = self.fetch_candles(pair, interval="1d", limit=10)
            closes_1d = [c["close"] for c in c1d]
            r7 = _ret_pct(closes_1d, 7)
            if r7 is not None:
                features[f"{label}_ret_7d_pct"] = r7
        except Exception as e:
            log.warning("1d candles %s failed: %s", pair, e)

    def fetch_all_tickers(self) -> list[dict[str, Any]]:
        """All Gate spot tickers (public)."""
        resp = self._session.get(f"{GATE_BASE}/spot/tickers", timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json() or []
        return data if isinstance(data, list) else []

    def fetch_breadth(
        self,
        *,
        top_n: int = 40,
        exclude_pairs: frozenset[str] | None = None,
    ) -> dict[str, float]:
        """Universe = top N USDT pairs by quote volume (ex BTC/ETH by default).

        Fail-open: empty dict if API fails or too few samples (unmeasured).
        """
        exclude = exclude_pairs or frozenset({"BTC_USDT", "ETH_USDT"})
        try:
            rows = self.fetch_all_tickers()
        except Exception as e:
            log.warning("breadth tickers failed: %s", e)
            return {}  # unmeasured — regime marks measured=False

        candidates: list[tuple[float, float]] = []  # (quote_vol, ret_24h)
        for row in rows:
            pair = str(row.get("currency_pair") or "")
            if not pair.endswith("_USDT"):
                continue
            if pair in exclude:
                continue
            # skip leveraged tokens noise
            base = pair.split("_")[0]
            if any(x in base for x in ("3L", "3S", "5L", "5S", "BULL", "BEAR")):
                continue
            try:
                qv = float(row.get("quote_volume") or row.get("base_volume") or 0)
                ret = float(row.get("change_percentage") or 0)
            except Exception:
                continue
            if qv <= 0:
                continue
            candidates.append((qv, ret))

        if len(candidates) < 8:
            log.warning("breadth: only %s liquid USDT pairs", len(candidates))
            return {}  # unmeasured — too few samples

        candidates.sort(key=lambda x: x[0], reverse=True)
        sample = candidates[: max(8, int(top_n))]
        rets = [r for _, r in sample]
        n = len(rets)
        green = sum(1 for r in rets if r > 0)
        rets_sorted = sorted(rets)
        mid = n // 2
        if n % 2:
            median = rets_sorted[mid]
        else:
            median = 0.5 * (rets_sorted[mid - 1] + rets_sorted[mid])

        return {
            "breadth_n": float(n),
            "breadth_pct_green": float(green) / float(n),
            "breadth_median_24h_pct": float(median),
            "breadth_mean_24h_pct": float(sum(rets) / n),
        }

    def fetch_btc_funding_rate_pct(self) -> tuple[float | None, str]:
        """BTC perpetual funding in percent (e.g. 0.01 = 0.01% per interval).

        Gate first, Binance fallback. Fail-open: (None, \"\") — unmeasured.
        """
        # Gate USDT-M futures contract
        try:
            resp = self._session.get(
                f"{GATE_BASE}/futures/usdt/contracts/BTC_USDT",
                timeout=self.timeout,
            )
            resp.raise_for_status()
            row = resp.json() or {}
            # funding_rate is decimal string e.g. "0.0001"
            raw = row.get("funding_rate")
            if raw is not None and str(raw) != "":
                return float(raw) * 100.0, "gate"
        except Exception as e:
            log.warning("gate funding failed: %s", e)

        try:
            resp = self._session.get(
                f"{BINANCE_FAPI}/fapi/v1/premiumIndex",
                params={"symbol": "BTCUSDT"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            row = resp.json() or {}
            raw = row.get("lastFundingRate")
            if raw is not None and str(raw) != "":
                return float(raw) * 100.0, "binance"
        except Exception as e:
            log.warning("binance funding failed: %s", e)

        return None, ""  # unmeasured — both venues failed

    def fetch_features(self, *, breadth_top_n: int = 40) -> dict[str, float]:
        """BTC/ETH multi-TF + structure + breadth + optional funding."""
        features: dict[str, float] = {}
        for label, pair in (("btc", "BTC_USDT"), ("eth", "ETH_USDT")):
            try:
                t = self.fetch_ticker(pair)
                features[f"{label}_last"] = float(t.get("last") or 0)
                features[f"{label}_ret_24h_pct"] = float(t.get("change_percentage") or 0)
            except Exception as e:
                log.warning("ticker %s failed: %s", pair, e)
            self._enrich_from_candles(features, label, pair)
        try:
            features.update(self.fetch_breadth(top_n=breadth_top_n))
        except Exception as e:
            log.warning("breadth failed: %s", e)
        try:
            fr, src = self.fetch_btc_funding_rate_pct()
            if fr is not None:
                features["btc_funding_rate_pct"] = float(fr)
                # encode source as 1=gate 2=binance for snapshot features (numeric only)
                features["btc_funding_source"] = 1.0 if src == "gate" else 2.0
        except Exception as e:
            log.warning("funding failed: %s", e)
        return features
