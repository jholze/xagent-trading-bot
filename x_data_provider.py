import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import requests

from data_manager import load_x_posts
from logger import log


@dataclass
class RawPost:
    post_id: str
    account: str
    text: str
    created_at: str = ""


class XDataProvider:
    def fetch_new_posts(self, accounts: list, limit_per_account: int = 5) -> List[RawPost]:
        raise NotImplementedError

    def get_last_post_id(self, account: str) -> Optional[str]:
        posts = load_x_posts().get("posts", [])
        last_id = None
        for post in posts:
            if post.get("account") == account and post.get("post_id"):
                last_id = post["post_id"]
        return last_id


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


class XApiV2Provider(XDataProvider):
    """X (Twitter) API v2 — requires X_API_BEARER_TOKEN in environment."""

    def __init__(self, bearer_token: str = None, base_url: str = None):
        self.bearer_token = bearer_token or os.getenv("X_API_BEARER_TOKEN", "")
        self.base_url = (base_url or os.getenv("X_API_BASE_URL", "https://api.twitter.com/2")).rstrip("/")
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
                "max_results": min(limit_per_account, 10),
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


def get_x_provider(config: dict = None) -> XDataProvider:
    from data_manager import get_config
    cfg = config or get_config()
    if cfg.get("use_mock_x_data", True):
        return MockXProvider()
    return XApiV2Provider()