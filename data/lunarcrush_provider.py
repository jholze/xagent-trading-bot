import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from logger import log


@dataclass
class RawLCMetrics:
    symbol: str
    galaxy_score: float = 0.0
    galaxy_score_previous: float = 0.0
    alt_rank: int = 0
    alt_rank_previous: int = 0
    sentiment: float = 50.0
    percent_change_24h: float = 0.0
    interactions_24h: int = 0
    topic: str = ""
    fetched_at: str = ""


class LunarCrushDataProvider:
    def fetch_for_watchlist(self, watchlist: list) -> List[RawLCMetrics]:
        raise NotImplementedError


class MockLunarCrushProvider(LunarCrushDataProvider):
    """Deterministic LC metrics for dev and tests."""

    MOCK = {
        "SOL": RawLCMetrics("SOL", 74, 62, 45, 120, 76, 8.5, 2_400_000, "sol solana"),
        "ETH": RawLCMetrics("ETH", 58, 61, 210, 195, 62, -2.1, 8_000_000, "eth ethereum"),
        "ARIA": RawLCMetrics("ARIA", 71, 55, 88, 240, 72, 12.0, 450_000, "aria"),
        "BTC": RawLCMetrics("BTC", 52, 54, 380, 360, 58, -1.5, 80_000_000, "btc bitcoin"),
    }

    def fetch_for_watchlist(self, watchlist: list) -> List[RawLCMetrics]:
        bases = {c.get("symbol", "").split("/")[0].upper() for c in watchlist}
        now = datetime.now(timezone.utc).isoformat()
        out = []
        for base in sorted(bases):
            m = self.MOCK.get(base)
            if not m:
                continue
            out.append(
                RawLCMetrics(
                    m.symbol,
                    m.galaxy_score,
                    m.galaxy_score_previous,
                    m.alt_rank,
                    m.alt_rank_previous,
                    m.sentiment,
                    m.percent_change_24h,
                    m.interactions_24h,
                    m.topic,
                    now,
                )
            )
        return out


class LunarCrushApiProvider(LunarCrushDataProvider):
    BASE_URL = "https://lunarcrush.com/api4/public/coins/list/v2"

    def __init__(self, api_key: str = None, cache_ttl_sec: int = 900):
        self.api_key = api_key or os.getenv("LUNARCRUSH_API_KEY", "")
        self.cache_ttl_sec = cache_ttl_sec
        self._cache_data: dict = {}
        self._cache_at: float = 0.0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _fetch_list(self) -> list:
        if not self.api_key:
            log("LUNARCRUSH_API_KEY not set — skipping LC fetch", "WARNING")
            return []
        now = time.time()
        if self._cache_data and now - self._cache_at < self.cache_ttl_sec:
            return self._cache_data.get("rows", [])

        try:
            resp = requests.get(
                self.BASE_URL,
                headers=self._headers(),
                params={"limit": 1000, "sort": "galaxy_score", "desc": "true"},
                timeout=20,
            )
            if resp.status_code != 200:
                err = resp.text[:200]
                log(f"LunarCrush list API HTTP {resp.status_code}: {err}", "WARNING")
                return self._cache_data.get("rows", [])
            rows = resp.json().get("data", [])
            self._cache_data = {"rows": rows}
            self._cache_at = now
            return rows
        except Exception as e:
            log(f"LunarCrush fetch error: {e}", "WARNING")
            return self._cache_data.get("rows", [])

    def fetch_for_watchlist(self, watchlist: list) -> List[RawLCMetrics]:
        bases = {c.get("symbol", "").split("/")[0].upper() for c in watchlist if c.get("symbol")}
        if not bases:
            return []

        rows = self._fetch_list()
        by_symbol: Dict[str, dict] = {}
        for row in rows:
            sym = str(row.get("symbol", "")).upper()
            if sym and sym not in by_symbol:
                by_symbol[sym] = row

        now = datetime.now(timezone.utc).isoformat()
        results: List[RawLCMetrics] = []
        missing = [b for b in bases if b not in by_symbol]
        for base in sorted(bases - set(missing)):
            results.append(self._row_to_metrics(by_symbol[base], now))

        for base in missing:
            single = self._fetch_coin(base)
            if single:
                results.append(single)
        return results

    def _row_to_metrics(self, row: dict, fetched_at: str) -> RawLCMetrics:
        return RawLCMetrics(
            symbol=str(row.get("symbol", "")).upper(),
            galaxy_score=float(row.get("galaxy_score") or 0),
            galaxy_score_previous=float(row.get("galaxy_score_previous") or row.get("galaxy_score") or 0),
            alt_rank=int(row.get("alt_rank") or 0),
            alt_rank_previous=int(row.get("alt_rank_previous") or row.get("alt_rank") or 0),
            sentiment=float(row.get("sentiment") or 50),
            percent_change_24h=float(row.get("percent_change_24h") or 0),
            interactions_24h=int(row.get("interactions_24h") or 0),
            topic=str(row.get("topic") or ""),
            fetched_at=fetched_at,
        )

    def _fetch_coin(self, symbol: str) -> Optional[RawLCMetrics]:
        if not self.api_key:
            return None
        try:
            url = f"https://lunarcrush.com/api4/public/coins/{symbol.lower()}/v1"
            resp = requests.get(url, headers=self._headers(), timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json().get("data") or {}
            now = datetime.now(timezone.utc).isoformat()
            return RawLCMetrics(
                symbol=symbol.upper(),
                galaxy_score=float(data.get("galaxy_score") or 0),
                galaxy_score_previous=float(data.get("galaxy_score") or 0),
                alt_rank=int(data.get("alt_rank") or 0),
                alt_rank_previous=int(data.get("alt_rank") or 0),
                sentiment=50.0,
                percent_change_24h=float(data.get("percent_change_24h") or 0),
                interactions_24h=0,
                topic=str(data.get("topic") or symbol.lower()),
                fetched_at=now,
            )
        except Exception as e:
            log(f"LunarCrush coin fetch {symbol}: {e}", "DEBUG")
            return None


def get_lc_provider() -> LunarCrushDataProvider:
    from core.config import get_bot_config

    cfg = get_bot_config().lunarcrush_config
    if cfg.get("use_mock", True):
        return MockLunarCrushProvider()
    return LunarCrushApiProvider(
        api_key=os.getenv(cfg.get("api_key_env", "LUNARCRUSH_API_KEY"), ""),
        cache_ttl_sec=int(cfg.get("cache_ttl_sec", 900)),
    )