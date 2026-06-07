import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import requests

from data_manager import load_cmc_posts
from logger import log


@dataclass
class RawCMCPost:
    post_id: str
    coin: str
    text: str
    author: str = "CMC Community"
    votes_bullish: int = 0
    votes_bearish: int = 0
    created_at: str = ""


class CMCCommunitySignal:
    """Normalized community signal — compatible with XSignal interface."""

    def __init__(
        self,
        coin: str,
        action: str,
        confidence: int,
        rationale: str = "",
        post_id: str = None,
        author: str = "CMC Community",
        votes_bullish: int = 0,
        votes_bearish: int = 0,
    ):
        self.account = author
        self.coin = coin.upper()
        self.action = action.upper()
        self.confidence = confidence
        self.rationale = rationale
        self.post_id = post_id
        self.source = "cmc"
        self.timestamp = datetime.now()
        self.trust_score = 65.0
        self.effective_confidence = float(confidence)
        self.votes_bullish = votes_bullish
        self.votes_bearish = votes_bearish
        self.score = 0.0


class CMCCommunityParser:
    """Parse CMC community posts into trading signals."""

    BULLISH = ("bullish", "moon", "buy", "accumulate", "breakout", "long", "pump", "undervalued")
    BEARISH = ("bearish", "sell", "dump", "short", "crash", "avoid", "overvalued", "top")

    def parse(self, post: RawCMCPost) -> CMCCommunitySignal:
        text = post.text.lower()
        bullish_hits = sum(1 for w in self.BULLISH if w in text)
        bearish_hits = sum(1 for w in self.BEARISH if w in text)

        total_votes = post.votes_bullish + post.votes_bearish
        if total_votes > 0:
            bull_ratio = post.votes_bullish / total_votes
            if bull_ratio >= 0.65:
                action, confidence = "BUY", int(60 + bull_ratio * 35)
            elif bull_ratio <= 0.35:
                action, confidence = "SELL", int(60 + (1 - bull_ratio) * 35)
            else:
                action, confidence = "HOLD", 50
        elif bullish_hits > bearish_hits:
            action = "BUY"
            confidence = min(90, 60 + bullish_hits * 8)
        elif bearish_hits > bullish_hits:
            action = "SELL"
            confidence = min(90, 60 + bearish_hits * 8)
        else:
            action = "HOLD"
            confidence = 45

        rationale = post.text[:120] if post.text else f"Community sentiment for {post.coin}"
        if total_votes > 0:
            rationale += f" (votes: {post.votes_bullish}↑/{post.votes_bearish}↓)"

        signal = CMCCommunitySignal(
            coin=post.coin,
            action=action,
            confidence=confidence,
            rationale=rationale,
            post_id=post.post_id,
            author=post.author,
            votes_bullish=post.votes_bullish,
            votes_bearish=post.votes_bearish,
        )
        signal.effective_confidence = confidence * (signal.trust_score / 100)
        return signal


class CMCDataProvider:
    def fetch_posts(self, watchlist: list, limit: int = 10) -> List[RawCMCPost]:
        raise NotImplementedError


class MockCMCProvider(CMCDataProvider):
    """Deterministic mock CMC community feed for dev and tests."""

    MOCK_POSTS = [
        ("cmc_mock_sol_1", "SOL", "SOL community very bullish on breakout. Strong accumulation phase.", 85, 12),
        ("cmc_mock_eth_1", "ETH", "ETH looking bearish near resistance. Community taking profits.", 20, 72),
        ("cmc_mock_aria_1", "ARIA", "ARIA holders optimistic — volume picking up, long term hold.", 64, 18),
        ("cmc_mock_btc_1", "BTC", "BTC consolidating. Neutral sentiment in community chat.", 40, 38),
    ]

    def fetch_posts(self, watchlist: list, limit: int = 10) -> List[RawCMCPost]:
        seen = {p.get("post_id") for p in load_cmc_posts().get("posts", []) if p.get("post_id")}
        watch_bases = {c.get("symbol", "").split("/")[0].upper() for c in watchlist}
        results = []
        for post_id, coin, text, bull, bear in self.MOCK_POSTS:
            if post_id in seen:
                continue
            if watch_bases and coin not in watch_bases:
                continue
            results.append(RawCMCPost(
                post_id=post_id,
                coin=coin,
                text=text,
                votes_bullish=bull,
                votes_bearish=bear,
                created_at=datetime.now().isoformat(),
            ))
            if len(results) >= limit:
                break
        return results


class CMCProApiProvider(CMCDataProvider):
    """CoinMarketCap Pro API — community trending + content fallback."""

    BASE_URL = "https://pro-api.coinmarketcap.com/v1"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("CMC_API_KEY", "")

    def _headers(self) -> dict:
        return {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}

    def _fetch_trending_tokens(self, limit: int) -> List[RawCMCPost]:
        if not self.api_key:
            return []
        try:
            url = f"{self.BASE_URL}/community/trending/token"
            resp = requests.get(url, headers=self._headers(), params={"limit": limit}, timeout=15)
            if resp.status_code != 200:
                log(f"CMC trending token API failed: {resp.status_code}", "WARNING")
                return []
            posts = []
            for item in resp.json().get("data", []):
                symbol = item.get("symbol") or item.get("name", "")
                if not symbol:
                    continue
                bull = int(item.get("bullish_percent", item.get("votes_bullish", 50)))
                bear = int(item.get("bearish_percent", item.get("votes_bearish", 50)))
                if bull + bear <= 100 and bull <= 100:
                    bull_v = bull
                    bear_v = bear
                else:
                    bull_v = max(0, bull)
                    bear_v = max(0, bear)
                posts.append(RawCMCPost(
                    post_id=f"cmc_trend_{symbol}_{item.get('id', bull_v)}",
                    coin=symbol.upper(),
                    text=item.get("description", f"Community trending: {symbol}"),
                    author="CMC Trending",
                    votes_bullish=bull_v,
                    votes_bearish=bear_v,
                    created_at=item.get("last_updated", datetime.now().isoformat()),
                ))
            return posts
        except Exception as e:
            log(f"CMC trending token fetch error: {e}", "WARNING")
            return []

    def _fetch_content_latest(self, symbols: list, limit: int) -> List[RawCMCPost]:
        if not self.api_key or not symbols:
            return []
        try:
            url = f"{self.BASE_URL}/content/latest"
            resp = requests.get(
                url,
                headers=self._headers(),
                params={"symbol": ",".join(symbols[:20]), "limit": limit},
                timeout=15,
            )
            if resp.status_code != 200:
                log(f"CMC content API failed: {resp.status_code}", "WARNING")
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
                    created_at=item.get("released_at", datetime.now().isoformat()),
                ))
            return posts
        except Exception as e:
            log(f"CMC content fetch error: {e}", "WARNING")
            return []

    def fetch_posts(self, watchlist: list, limit: int = 10) -> List[RawCMCPost]:
        if not self.api_key:
            log("CMC_API_KEY not set — skipping live CMC fetch", "WARNING")
            return []

        seen = {p.get("post_id") for p in load_cmc_posts().get("posts", []) if p.get("post_id")}
        results = []
        for post in self._fetch_trending_tokens(limit):
            if post.post_id not in seen:
                results.append(post)

        symbols = [c.get("symbol", "").split("/")[0] for c in watchlist if c.get("symbol")]
        for post in self._fetch_content_latest(symbols, limit):
            if post.post_id not in seen and post.post_id not in {r.post_id for r in results}:
                results.append(post)

        return results[:limit]


def get_cmc_provider(config: dict = None) -> CMCDataProvider:
    from data_manager import get_config
    cfg = config or get_config()
    cmc_cfg = cfg.get("cmc", {})
    if cmc_cfg.get("use_mock", True):
        return MockCMCProvider()
    return CMCProApiProvider()