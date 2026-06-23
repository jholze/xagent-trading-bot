"""Aggregate CMC signals for volatile/trending coins with tier metadata."""

from __future__ import annotations

import os
from typing import List

import requests

from data.cmc_community_provider import CMCCommunityParser, CMCCommunitySignal, RawCMCPost
from data.cmc_trending_provider import CMCTrendingProvider
from core.config import get_bot_config
from data_manager import load_cmc_posts
from logger import log


_TIER_TRUST = {
    "trending": 72.0,
    "community": 68.0,
    "quote": 60.0,
}


def _apply_tier(signal: CMCCommunitySignal, tier: str, trending_rank: int = 0) -> CMCCommunitySignal:
    signal.signal_tier = tier
    signal.trending_rank = trending_rank
    signal.trust_score = _TIER_TRUST.get(tier, 65.0)
    signal.effective_confidence = float(signal.confidence) * (signal.trust_score / 100.0)
    return signal


class CMCVolatileSignalAggregator:
    """Fetch trending/community/content/quote signals with separate budgets."""

    BASE_URL = "https://pro-api.coinmarketcap.com/v1"

    def __init__(self, api_key: str = None, parser: CMCCommunityParser = None):
        self.api_key = api_key or os.getenv("CMC_API_KEY", "")
        self.parser = parser or CMCCommunityParser()
        self.trending_provider = CMCTrendingProvider(api_key=self.api_key)

    def _headers(self) -> dict:
        return {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}

    def _budgets(self) -> dict:
        cfg = get_bot_config().cmc_config
        return {
            "community_trending": int(cfg.get("budget_community_trending", 12)),
            "market_trending": int(cfg.get("budget_market_trending", 12)),
            "content": int(cfg.get("budget_content", 8)),
            "quotes": int(cfg.get("budget_quotes", 12)),
        }

    def _seen_ids(self) -> set:
        return {p.get("post_id") for p in load_cmc_posts().get("posts", []) if p.get("post_id")}

    def _fetch_community_trending(self, limit: int) -> List[RawCMCPost]:
        if not self.api_key or limit <= 0:
            return []
        try:
            url = f"{self.BASE_URL}/community/trending/token"
            resp = requests.get(url, headers=self._headers(), params={"limit": limit}, timeout=15)
            if resp.status_code != 200:
                return []
            posts = []
            for rank, item in enumerate(resp.json().get("data", []), start=1):
                symbol = item.get("symbol") or item.get("name", "")
                if not symbol:
                    continue
                bull = int(item.get("bullish_percent", item.get("votes_bullish", 50)))
                bear = int(item.get("bearish_percent", item.get("votes_bearish", 50)))
                posts.append(RawCMCPost(
                    post_id=f"cmc_trend_{symbol}_{item.get('id', bull)}",
                    coin=symbol.upper(),
                    text=item.get("description", f"Community trending: {symbol}"),
                    author="CMC Trending",
                    votes_bullish=max(0, bull),
                    votes_bearish=max(0, bear),
                    created_at=item.get("last_updated", ""),
                ))
                posts[-1].trending_rank = rank  # type: ignore[attr-defined]
            return posts
        except Exception as e:
            log(f"CMC community trending fetch error: {e}", "WARNING")
            return []

    def _source_priority(self) -> list:
        return get_bot_config().trending_watchlist_config.get("source_priority") or [
            "trending/latest",
            "trending/gainers-losers",
            "listings/latest",
        ]

    def _fetch_market_trending_posts(self, limit: int) -> List[RawCMCPost]:
        symbols, source = self.trending_provider.fetch_trending_symbols(
            limit=limit,
            source_priority=self._source_priority(),
        )
        posts = []
        for rank, sym in enumerate(symbols, start=1):
            posts.append(RawCMCPost(
                post_id=f"cmc_mkt_trend_{sym}_{rank}",
                coin=sym,
                text=f"CMC market trending #{rank} ({source})",
                author="CMC Market Trending",
                votes_bullish=62,
                votes_bearish=38,
            ))
            posts[-1].trending_rank = rank  # type: ignore[attr-defined]
        return posts

    def _fetch_content(self, symbols: list, limit: int) -> List[RawCMCPost]:
        if not self.api_key or not symbols or limit <= 0:
            return []
        try:
            import re
            url = f"{self.BASE_URL}/content/latest"
            resp = requests.get(
                url,
                headers=self._headers(),
                params={"symbol": ",".join(symbols[:20]), "limit": limit},
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            posts = []
            for item in resp.json().get("data", []):
                title = item.get("title", "")
                body = item.get("subtitle", "") or item.get("content", "") or title
                coin = ""
                for meta in item.get("meta", {}).get("cryptocurrency", []) or []:
                    coin = meta.get("symbol", "")
                    if coin:
                        break
                if not coin:
                    match = re.search(r"\b([A-Z]{2,10})\b", title)
                    coin = match.group(1) if match else "UNKNOWN"
                posts.append(RawCMCPost(
                    post_id=str(item.get("id", f"cmc_content_{coin}")),
                    coin=coin,
                    text=f"{title}. {body}".strip(),
                    author=item.get("source_name", "CMC Content"),
                    created_at=item.get("released_at", ""),
                ))
            return posts
        except Exception as e:
            log(f"CMC content fetch error: {e}", "WARNING")
            return []

    def _fetch_quotes(self, symbols: list, limit: int) -> List[RawCMCPost]:
        if not self.api_key or not symbols or limit <= 0:
            return []
        from data.cmc_community_provider import CMCProApiProvider

        provider = CMCProApiProvider(api_key=self.api_key)
        return provider._fetch_quotes_sentiment(symbols, limit)

    def fetch_signals(self, watchlist: list) -> List[CMCCommunitySignal]:
        if not self.api_key:
            log("CMC_API_KEY not set — skipping live CMC fetch", "WARNING")
            return []

        budgets = self._budgets()
        seen = self._seen_ids()
        results: list[CMCCommunitySignal] = []
        result_ids: set = set()

        def add_post(post: RawCMCPost, tier: str):
            if post.post_id in seen or post.post_id in result_ids:
                return
            signal = self.parser.parse(post)
            signal.quotes_fallback = post.author in ("CMC Market", "CMC Market Trending")
            rank = int(getattr(post, "trending_rank", 0) or 0)
            _apply_tier(signal, tier, trending_rank=rank)
            results.append(signal)
            result_ids.add(post.post_id)

        for post in self._fetch_community_trending(budgets["community_trending"]):
            add_post(post, "trending")

        for post in self._fetch_market_trending_posts(budgets["market_trending"]):
            add_post(post, "trending")

        trending_bases = list({
            getattr(s, "coin", "") for s in results if getattr(s, "coin", "")
        })
        watch_bases = [c.get("symbol", "").split("/")[0] for c in watchlist if c.get("symbol")]
        content_symbols = list(dict.fromkeys(trending_bases + watch_bases[:10]))
        for post in self._fetch_content(content_symbols, budgets["content"]):
            add_post(post, "community")

        core_bases = [
            c.get("symbol", "").split("/")[0]
            for c in watchlist
            if c.get("source") not in ("cmc_trending",)
        ]
        for post in self._fetch_quotes(core_bases, budgets["quotes"]):
            add_post(post, "quote")

        return results