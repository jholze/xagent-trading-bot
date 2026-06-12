"""CoinMarketCap trending symbols for enhanced dry-run watchlist overlay."""

from __future__ import annotations

import os
from typing import List, Tuple

import requests

from logger import log


class CMCTrendingProvider:
    """Fetch top trending crypto symbols from CMC Pro API with fallbacks."""

    BASE_URL = "https://pro-api.coinmarketcap.com/v1"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("CMC_API_KEY", "")

    def _headers(self) -> dict:
        return {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}

    def fetch_trending_symbols(self, limit: int = 15) -> Tuple[List[str], str]:
        """Return (symbols, source_label). Empty list if API unavailable."""
        if not self.api_key:
            log("CMC_API_KEY not set — skipping trending watchlist sync", "WARNING")
            return [], ""

        for fetcher, source in (
            (self._fetch_trending_latest, "trending/latest"),
            (self._fetch_gainers_losers, "trending/gainers-losers"),
            (self._fetch_listings_movers, "listings/latest"),
        ):
            symbols = fetcher(limit)
            if symbols:
                return symbols[:limit], source

        return [], ""

    def _fetch_trending_latest(self, limit: int) -> List[str]:
        try:
            url = f"{self.BASE_URL}/cryptocurrency/trending/latest"
            resp = requests.get(
                url,
                headers=self._headers(),
                params={"limit": limit},
                timeout=15,
            )
            if resp.status_code != 200:
                err = resp.json().get("status", {}).get("error_message", resp.status_code)
                log(f"CMC trending/latest unavailable: {err}", "WARNING")
                return []
            symbols = []
            for item in resp.json().get("data", []):
                sym = (item.get("symbol") or "").upper()
                if sym:
                    symbols.append(sym)
            return symbols
        except Exception as e:
            log(f"CMC trending/latest fetch error: {e}", "WARNING")
            return []

    def _fetch_gainers_losers(self, limit: int) -> List[str]:
        try:
            url = f"{self.BASE_URL}/cryptocurrency/trending/gainers-losers"
            resp = requests.get(
                url,
                headers=self._headers(),
                params={"time_period": "24h", "limit": limit},
                timeout=15,
            )
            if resp.status_code != 200:
                err = resp.json().get("status", {}).get("error_message", resp.status_code)
                log(f"CMC gainers-losers unavailable: {err}", "WARNING")
                return []
            data = resp.json().get("data", {})
            symbols = []
            for key in ("gainers", "losers"):
                for item in data.get(key, []):
                    sym = (item.get("symbol") or "").upper()
                    if sym and sym not in symbols:
                        symbols.append(sym)
            if symbols:
                log(f"CMC using gainers-losers fallback ({len(symbols)} symbols)", "INFO")
            return symbols
        except Exception as e:
            log(f"CMC gainers-losers fetch error: {e}", "WARNING")
            return []

    def _fetch_listings_movers(self, limit: int) -> List[str]:
        try:
            url = f"{self.BASE_URL}/cryptocurrency/listings/latest"
            resp = requests.get(
                url,
                headers=self._headers(),
                params={"limit": 200, "sort": "percent_change_24h", "sort_dir": "desc"},
                timeout=15,
            )
            if resp.status_code != 200:
                err = resp.json().get("status", {}).get("error_message", resp.status_code)
                log(f"CMC listings/latest unavailable: {err}", "WARNING")
                return []
            movers = []
            for item in resp.json().get("data", []):
                sym = (item.get("symbol") or "").upper()
                quote = item.get("quote", {}).get("USD") or item.get("quote", {}).get("USDT") or {}
                pct = abs(float(quote.get("percent_change_24h", 0) or 0))
                if sym:
                    movers.append((sym, pct))
            movers.sort(key=lambda x: x[1], reverse=True)
            symbols = [sym for sym, _ in movers[:limit]]
            if symbols:
                log(f"CMC using listings/latest fallback ({len(symbols)} symbols)", "INFO")
            return symbols
        except Exception as e:
            log(f"CMC listings movers fetch error: {e}", "WARNING")
            return []