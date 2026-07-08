"""CoinMarketCap trending symbols for enhanced dry-run watchlist overlay."""

from __future__ import annotations

import os
from typing import List, Tuple

import requests

from data.cmc_capabilities import filter_source_priority, probe_capabilities
from logger import log


class CMCTrendingProvider:
    """Fetch top trending crypto symbols from CMC Pro API with plan-aware fallbacks."""

    BASE_URL = "https://pro-api.coinmarketcap.com/v1"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("CMC_API_KEY", "")

    def _headers(self) -> dict:
        return {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}

    def fetch_trending_symbols(
        self,
        limit: int = 15,
        source_priority: List[str] | None = None,
        *,
        include_losers: bool = True,
        capabilities: dict | None = None,
    ) -> Tuple[List[str], str]:
        """Return (symbols, source_label). Empty list if API unavailable."""
        if not self.api_key:
            log("CMC_API_KEY not set — skipping trending watchlist sync", "WARNING")
            return [], ""

        fetchers = {
            "trending/latest": (self._fetch_trending_latest, "trending/latest"),
            "trending/gainers-losers": (self._fetch_gainers_losers, "trending/gainers-losers"),
            "listings/latest": (
                lambda n: self._fetch_listings_movers(n, include_losers=include_losers),
                "listings/latest",
            ),
        }
        priority = source_priority or [
            "trending/latest",
            "trending/gainers-losers",
            "listings/latest",
        ]
        caps = capabilities or probe_capabilities(self.api_key)
        priority = filter_source_priority(priority, caps, api_key=self.api_key)

        for key in priority:
            entry = fetchers.get(key)
            if not entry:
                continue
            fetcher, source = entry
            symbols = fetcher(limit)
            if symbols:
                if source == "listings/latest":
                    log(f"CMC listings movers watchlist ({len(symbols)} symbols)", "INFO")
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
                log(f"CMC using gainers-losers ({len(symbols)} symbols)", "INFO")
            return symbols
        except Exception as e:
            log(f"CMC gainers-losers fetch error: {e}", "WARNING")
            return []

    def _fetch_listings_sorted(self, limit: int, sort_dir: str) -> List[str]:
        url = f"{self.BASE_URL}/cryptocurrency/listings/latest"
        resp = requests.get(
            url,
            headers=self._headers(),
            params={
                "limit": max(limit, 50),
                "sort": "percent_change_24h",
                "sort_dir": sort_dir,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            err = resp.json().get("status", {}).get("error_message", resp.status_code)
            log(f"CMC listings/latest ({sort_dir}) unavailable: {err}", "WARNING")
            return []
        symbols = []
        for item in resp.json().get("data", []):
            sym = (item.get("symbol") or "").upper()
            if sym:
                symbols.append(sym)
        return symbols[:limit]

    def _fetch_listings_movers(self, limit: int, *, include_losers: bool = True) -> List[str]:
        try:
            gainers = self._fetch_listings_sorted(limit, "desc")
            if not gainers:
                return []
            if not include_losers:
                return gainers
            losers = self._fetch_listings_sorted(max(5, limit // 2), "asc")
            merged: list[str] = []
            for sym in gainers + losers:
                if sym not in merged:
                    merged.append(sym)
            return merged[:limit]
        except Exception as e:
            log(f"CMC listings movers fetch error: {e}", "WARNING")
            return []