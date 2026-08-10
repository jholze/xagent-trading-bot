"""CoinMarketCap trending symbols for enhanced dry-run watchlist overlay."""

from __future__ import annotations

import os
from typing import List, Tuple

import requests

from data.cmc_capabilities import filter_source_priority, probe_capabilities
from logger import log


def _optional_mcap_max_usd(cfg: dict) -> float | None:
    """No upper cap when unset, null, or <= 0."""
    if "listings_mcap_max_usd" not in cfg:
        return None
    raw = cfg.get("listings_mcap_max_usd")
    if raw is None:
        return None
    val = float(raw)
    return val if val > 0 else None


def listings_filter_config(cmc_config: dict | None = None) -> dict:
    from core.config import get_bot_config

    cfg = cmc_config or get_bot_config().cmc_config
    entry = get_bot_config().entry_sensor_15m_config
    return {
        "mode": str(cfg.get("listings_fallback_mode", "mcap_band_momentum")),
        "scan_limit": int(cfg.get("listings_scan_limit", 500)),
        "mcap_min_usd": float(cfg.get("listings_mcap_min_usd", entry.get("market_cap_min_usd", 5_000_000))),
        "mcap_max_usd": _optional_mcap_max_usd(cfg),
        "min_pct_change_24h": float(cfg.get("listings_min_pct_change_24h", 3)),
        "max_pct_change_24h": float(cfg.get("listings_max_pct_change_24h", 35)),
        "min_volume_24h_usd": float(cfg.get("listings_min_volume_24h_usd", 500_000)),
        "exclude_symbols": {s.upper() for s in (cfg.get("trending_watchlist") or {}).get("exclude_symbols", [])},
    }


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
                lambda n: self._fetch_listings_fallback(n, include_losers=include_losers),
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
                    filt = listings_filter_config()
                    log(
                        f"CMC listings fallback ({filt['mode']}, {len(symbols)} symbols)",
                        "INFO",
                    )
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

    def _fetch_listings_raw(self, *, limit: int, sort: str, sort_dir: str) -> list[dict]:
        url = f"{self.BASE_URL}/cryptocurrency/listings/latest"
        resp = requests.get(
            url,
            headers=self._headers(),
            params={
                "limit": max(limit, 50),
                "sort": sort,
                "sort_dir": sort_dir,
                "convert": "USD",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            err = resp.json().get("status", {}).get("error_message", resp.status_code)
            log(f"CMC listings/latest ({sort}/{sort_dir}) unavailable: {err}", "WARNING")
            return []
        return list(resp.json().get("data", []) or [])

    def _listing_quote(self, item: dict) -> dict:
        return (item.get("quote") or {}).get("USD") or {}

    def _passes_listings_filter(self, item: dict, filt: dict) -> tuple[bool, float]:
        sym = (item.get("symbol") or "").upper()
        if not sym or sym in filt["exclude_symbols"]:
            return False, 0.0
        q = self._listing_quote(item)
        mcap = float(q.get("market_cap") or 0)
        chg = float(q.get("percent_change_24h") or 0)
        vol = float(q.get("volume_24h") or 0)
        if mcap < filt["mcap_min_usd"]:
            return False, chg
        mcap_max = filt.get("mcap_max_usd")
        if mcap_max is not None and mcap > mcap_max:
            return False, chg
        if abs(chg) > filt["max_pct_change_24h"]:
            return False, chg
        if vol < filt["min_volume_24h_usd"]:
            return False, chg
        return True, chg

    def _fetch_listings_mcap_momentum(self, limit: int) -> List[str]:
        """Builder-safe fallback: momentum within entry-sensor market-cap band."""
        filt = listings_filter_config()
        rows = self._fetch_listings_raw(
            limit=filt["scan_limit"],
            sort="market_cap",
            sort_dir="desc",
        )
        candidates: list[tuple[float, str]] = []
        for item in rows:
            ok, chg = self._passes_listings_filter(item, filt)
            if not ok or chg < filt["min_pct_change_24h"]:
                continue
            sym = (item.get("symbol") or "").upper()
            candidates.append((chg, sym))
        candidates.sort(reverse=True)
        return [sym for _, sym in candidates[:limit]]

    def _fetch_listings_sorted(self, limit: int, sort_dir: str) -> List[str]:
        symbols = []
        for item in self._fetch_listings_raw(
            limit=limit,
            sort="percent_change_24h",
            sort_dir=sort_dir,
        ):
            sym = (item.get("symbol") or "").upper()
            if sym:
                symbols.append(sym)
        return symbols[:limit]

    def _fetch_listings_fallback(self, limit: int, *, include_losers: bool = True) -> List[str]:
        try:
            filt = listings_filter_config()
            if filt["mode"] == "mcap_band_momentum":
                return self._fetch_listings_mcap_momentum(limit)
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
            log(f"CMC listings fallback error: {e}", "WARNING")
            return []

    def fetch_listings_momentum_details(self, limit: int = 15) -> list[dict]:
        """Return filtered listing rows for signal generation (symbol, pct, mcap, volume)."""
        try:
            filt = listings_filter_config()
            rows = self._fetch_listings_raw(
                limit=filt["scan_limit"],
                sort="market_cap",
                sort_dir="desc",
            )
            out: list[tuple[float, dict]] = []
            for item in rows:
                ok, chg = self._passes_listings_filter(item, filt)
                if not ok or chg < filt["min_pct_change_24h"]:
                    continue
                q = self._listing_quote(item)
                out.append((
                    chg,
                    {
                        "symbol": (item.get("symbol") or "").upper(),
                        "pct_change_24h": chg,
                        "market_cap": float(q.get("market_cap") or 0),
                        "volume_24h": float(q.get("volume_24h") or 0),
                    },
                ))
            out.sort(key=lambda x: x[0], reverse=True)
            return [row for _, row in out[:limit]]
        except Exception as e:
            log(f"CMC listings momentum details error: {e}", "WARNING")
            return []