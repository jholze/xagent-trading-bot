import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import requests

from data_manager import get_config, load_x_posts
from grok_x_search import fetch_posts_from_handle
from logger import log


@dataclass
class RawPost:
    post_id: str
    account: str
    text: str
    created_at: str = ""


def _backtest_config(config: dict = None) -> dict:
    cfg = config or get_config()
    return cfg.get("x_backtest", {})


def _perf_config(config: dict = None) -> dict:
    cfg = config or get_config()
    return cfg.get("x_performance", {})


class XDataProvider:
    def fetch_new_posts(self, accounts: list, limit_per_account: int = 5) -> List[RawPost]:
        raise NotImplementedError

    def fetch_historical_posts(
        self,
        handle: str,
        days: int = 60,
        max_posts: int = 50,
        config: dict = None,
    ) -> List[RawPost]:
        raise NotImplementedError

    def get_last_post_id(self, account: str) -> Optional[str]:
        """Highest numeric X tweet id seen for account (ignores mock/grok test ids)."""
        posts = load_x_posts().get("posts", [])
        best: int | None = None
        for post in posts:
            if post.get("account") != account:
                continue
            pid = str(post.get("post_id") or "").strip()
            if not pid.isdigit():
                continue
            val = int(pid)
            if best is None or val > best:
                best = val
        return str(best) if best is not None else None


class MockXProvider(XDataProvider):
    """Deterministic mock feed for development and tests."""

    MOCK_TWEETS = {
        "CryptoCapo_": [
            ("mock_capo_1", "BTC breaking key resistance with strong volume. Macro looks very bullish. Buying more now."),
            ("mock_capo_2", "HIGH looks weak. Resistance not breaking. Prefer to stay away or short."),
        ],
        "Pentosh1": [
            ("mock_pento_1", "SOL is overextended on the daily. Taking profits here. Short term bearish."),
        ],
        "SmartContracter": [
            ("mock_sc_1", "ETH forming a nice higher low. Good risk/reward for long position."),
        ],
        "TheCryptoDog": [
            ("mock_dog_1", "DOGE community is strong but price is consolidating. Watching for breakout."),
        ],
        "CryptoWizardd": [
            ("mock_wiz_1", "BNB breaking out of long consolidation. Volume picking up. Bullish bias."),
        ],
    }

    MOCK_HISTORICAL_TWEETS = {
        "CryptoCapo_": [
            ("mock_capo_h1", 5, "BTC breaking key resistance with strong volume. Macro looks very bullish. Buying more now."),
            ("mock_capo_h2", 12, "ETH looking strong, accumulating on dips. Long bias."),
            ("mock_capo_h3", 20, "SOL overextended, taking profits here. Short term bearish."),
            ("mock_capo_h4", 35, "Market looks choppy, staying sidelined for now."),
            ("mock_capo_h5", 48, "BNB breakout with volume. Adding to position."),
        ],
        "Pentosh1": [
            ("mock_pento_h1", 8, "SOL is overextended on the daily. Taking profits here. Short term bearish."),
            ("mock_pento_h2", 25, "BTC holding support, still bullish medium term."),
        ],
        "SmartContracter": [
            ("mock_sc_h1", 15, "ETH forming a nice higher low. Good risk/reward for long position."),
            ("mock_sc_h2", 40, "AVAX weak structure, prefer to stay away."),
        ],
    }

    def fetch_new_posts(self, accounts: list, limit_per_account: int = 5) -> List[RawPost]:
        results = []
        seen_ids = {p.get("post_id") for p in load_x_posts().get("posts", []) if p.get("post_id")}

        for acc in accounts:
            if not acc.get("enabled", True):
                continue
            handle = acc.get("handle", str(acc))
            tweets = self.MOCK_TWEETS.get(handle, [
                (f"mock_{handle}_default", "General bullish sentiment on major alts."),
            ])
            count = 0
            for post_id, text in tweets:
                if post_id in seen_ids:
                    continue
                results.append(RawPost(
                    post_id=post_id,
                    account=handle,
                    text=text,
                    created_at=datetime.now().isoformat(),
                ))
                count += 1
                if count >= limit_per_account:
                    break
        return results

    def fetch_historical_posts(
        self,
        handle: str,
        days: int = 60,
        max_posts: int = 50,
        config: dict = None,
    ) -> List[RawPost]:
        cfg = _backtest_config(config)
        max_posts = min(max_posts, cfg.get("max_posts", 50))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        results = []
        tweets = list(self.MOCK_HISTORICAL_TWEETS.get(handle, []))
        if not tweets:
            for i, (post_id, text) in enumerate(self.MOCK_TWEETS.get(handle, [
                (f"mock_{handle}_default", "General bullish sentiment on major alts."),
            ])):
                days_ago = min(10 + i * 7, max(days - 1, 1))
                tweets.append((post_id, days_ago, text))

        for post_id, days_ago, text in tweets:
            created = datetime.now(timezone.utc) - timedelta(days=days_ago)
            if created < cutoff:
                continue
            results.append(RawPost(
                post_id=post_id,
                account=handle,
                text=text,
                created_at=created.isoformat(),
            ))
            if len(results) >= max_posts:
                break

        results.sort(key=lambda p: p.created_at)
        return results


class GrokXSearchProvider(XDataProvider):
    """Fetch X posts via xAI Grok x_search tool — uses XAI_API_KEY only."""

    def __init__(self, config: dict = None):
        cfg = config or get_config()
        perf = _perf_config(cfg)
        self._live_search_days = int(perf.get("live_search_days", 2))
        self._cache_ttl_sec = int(perf.get("x_search_cache_ttl_sec", 900))
        self._parallel_accounts = int(perf.get("parallel_account_fetch", 3))
        self._search_cache: dict[str, Tuple[float, List[RawPost]]] = {}

    def _cache_get(self, handle: str) -> Optional[List[RawPost]]:
        entry = self._search_cache.get(handle)
        if not entry:
            return None
        cached_at, posts = entry
        if time.time() - cached_at >= self._cache_ttl_sec:
            return None
        return posts

    def _cache_set(self, handle: str, posts: List[RawPost]):
        self._search_cache[handle] = (time.time(), posts)

    def fetch_historical_posts(
        self,
        handle: str,
        days: int = 60,
        max_posts: int = 50,
        config: dict = None,
    ) -> List[RawPost]:
        cfg = _backtest_config(config)
        max_posts = min(max_posts, cfg.get("max_posts", 50))
        posts = fetch_posts_from_handle(handle, days=days, max_posts=max_posts)
        results = []
        for i, post in enumerate(posts):
            post_id = post.get("post_id") or f"grok_{handle}_{i}"
            text = post.get("text", "").strip()
            if not text:
                continue
            created = post.get("created_at") or datetime.now(timezone.utc).isoformat()
            results.append(RawPost(
                post_id=str(post_id),
                account=handle.replace("@", ""),
                text=text,
                created_at=created,
            ))
        results.sort(key=lambda p: p.created_at)
        return results

    def _posts_for_handle(
        self,
        handle: str,
        limit_per_account: int,
        seen_ids: set,
        use_cache: bool,
    ) -> List[RawPost]:
        if use_cache:
            cached = self._cache_get(handle)
            if cached is not None:
                return cached

        posts = self.fetch_historical_posts(
            handle,
            days=self._live_search_days,
            max_posts=limit_per_account,
        )
        if use_cache:
            self._cache_set(handle, posts)
        return posts

    def fetch_new_posts(self, accounts: list, limit_per_account: int = 5) -> List[RawPost]:
        seen_ids = {p.get("post_id") for p in load_x_posts().get("posts", []) if p.get("post_id")}
        enabled = [acc for acc in accounts if acc.get("enabled", True)]
        if not enabled:
            return []

        handles = [acc.get("handle", str(acc)) for acc in enabled]
        posts_by_handle: dict[str, List[RawPost]] = {}

        if self._parallel_accounts > 1 and len(handles) > 1:
            workers = min(self._parallel_accounts, len(handles))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._posts_for_handle,
                        handle,
                        limit_per_account,
                        seen_ids,
                        True,
                    ): handle
                    for handle in handles
                }
                for future in as_completed(futures):
                    handle = futures[future]
                    try:
                        posts_by_handle[handle] = future.result()
                    except Exception as e:
                        log(f"Grok X Search parallel fetch failed for @{handle}: {e}", "WARNING")
                        posts_by_handle[handle] = []
        else:
            for handle in handles:
                posts_by_handle[handle] = self._posts_for_handle(
                    handle, limit_per_account, seen_ids, use_cache=True,
                )

        results = []
        for handle in handles:
            count = 0
            for post in posts_by_handle.get(handle, []):
                if post.post_id in seen_ids:
                    continue
                results.append(post)
                count += 1
                if count >= limit_per_account:
                    break
        return results


class XApiV2Provider(XDataProvider):
    """X (Twitter) API v2 — requires X_API_BEARER_TOKEN in environment."""

    def __init__(self, bearer_token: str = None, base_url: str = None):
        self.bearer_token = bearer_token or os.getenv("X_API_BEARER_TOKEN", "")
        self.base_url = (base_url or os.getenv("X_API_BASE_URL", "https://api.x.com/2")).rstrip("/")
        self._user_id_cache: dict[str, str] = {}

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.bearer_token}"}

    def _get_user_id(self, username: str) -> Optional[str]:
        if username in self._user_id_cache:
            return self._user_id_cache[username]
        if not self.bearer_token:
            log("X_API_BEARER_TOKEN not set — cannot fetch live posts", "WARNING")
            return None
        try:
            url = f"{self.base_url}/users/by/username/{username}"
            resp = requests.get(url, headers=self._headers(), params={"user.fields": "id"}, timeout=15)
            if resp.status_code != 200:
                log(f"X API user lookup failed for @{username}: {resp.status_code}", "WARNING")
                return None
            user_id = resp.json().get("data", {}).get("id")
            if user_id:
                self._user_id_cache[username] = user_id
            return user_id
        except Exception as e:
            log(f"X API user lookup error for @{username}: {e}", "WARNING")
            return None

    def fetch_new_posts(self, accounts: list, limit_per_account: int = 5) -> List[RawPost]:
        if not self.bearer_token:
            log("Skipping live X fetch — no bearer token configured", "WARNING")
            return []

        seen_ids = {p.get("post_id") for p in load_x_posts().get("posts", []) if p.get("post_id")}
        results = []

        for acc in accounts:
            if not acc.get("enabled", True):
                continue
            handle = acc.get("handle", str(acc))
            user_id = self._get_user_id(handle)
            if not user_id:
                continue

            since_id = self.get_last_post_id(handle) or acc.get("last_post_id")
            params = {
                "max_results": max(5, min(limit_per_account, 10)),
                "tweet.fields": "created_at,text",
                "exclude": "retweets,replies",
            }
            if since_id:
                params["since_id"] = since_id

            try:
                url = f"{self.base_url}/users/{user_id}/tweets"
                resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
                if resp.status_code != 200:
                    log(f"X API tweets failed for @{handle}: {resp.status_code}", "WARNING")
                    continue
                tweets = resp.json().get("data", [])
                for tweet in reversed(tweets):
                    post_id = tweet.get("id")
                    text = tweet.get("text", "")
                    if not post_id or not text or post_id in seen_ids:
                        continue
                    results.append(RawPost(
                        post_id=post_id,
                        account=handle,
                        text=text,
                        created_at=tweet.get("created_at", ""),
                    ))
            except Exception as e:
                log(f"X API fetch error for @{handle}: {e}", "WARNING")

        return results

    def fetch_historical_posts(
        self,
        handle: str,
        days: int = 60,
        max_posts: int = 50,
        config: dict = None,
    ) -> List[RawPost]:
        cfg = _backtest_config(config)
        max_posts = min(max_posts, cfg.get("max_posts", 50))
        if not self.bearer_token:
            log("Skipping historical X fetch — no bearer token configured", "WARNING")
            return []

        user_id = self._get_user_id(handle)
        if not user_id:
            return []

        start_time = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        results = []
        pagination_token = None

        while len(results) < max_posts:
            batch_size = min(max(max_posts - len(results), 5), 100)
            params = {
                "max_results": batch_size,
                "tweet.fields": "created_at,text",
                "exclude": "retweets,replies",
                "start_time": start_time,
            }
            if pagination_token:
                params["pagination_token"] = pagination_token

            try:
                url = f"{self.base_url}/users/{user_id}/tweets"
                resp = requests.get(url, headers=self._headers(), params=params, timeout=20)
                if resp.status_code != 200:
                    log(f"X API historical tweets failed for @{handle}: {resp.status_code}", "WARNING")
                    break
                payload = resp.json()
                tweets = payload.get("data", [])
                for tweet in tweets:
                    post_id = tweet.get("id")
                    text = tweet.get("text", "")
                    if not post_id or not text:
                        continue
                    results.append(RawPost(
                        post_id=post_id,
                        account=handle,
                        text=text,
                        created_at=tweet.get("created_at", ""),
                    ))
                    if len(results) >= max_posts:
                        break
                pagination_token = payload.get("meta", {}).get("next_token")
                if not pagination_token or not tweets:
                    break
            except Exception as e:
                log(f"X API historical fetch error for @{handle}: {e}", "WARNING")
                break

        results.sort(key=lambda p: p.created_at)
        return results


def get_x_provider(config: dict = None) -> XDataProvider:
    from data_manager import get_config
    cfg = config or get_config()
    if cfg.get("use_mock_x_data", True):
        return MockXProvider()
    if cfg.get("use_grok_x_search", True):
        return GrokXSearchProvider(cfg)
    return XApiV2Provider()