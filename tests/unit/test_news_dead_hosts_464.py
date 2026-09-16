"""#464: CoinGecko status_updates and unkeyed CryptoCompare must not be called."""

from __future__ import annotations

import json
from unittest.mock import patch

from intelligence.memory.news_providers import (
    CRYPTOCOMPARE_NEWS_URL,
    SKIPPED_NO_ENDPOINT,
    SKIPPED_NO_KEY,
    fetch_coingecko_news,
    fetch_cryptocompare_news,
    poll_and_ingest_news,
)
from intelligence.memory.store import InMemoryMemoryStore

_RSS = b"""<?xml version="1.0"?>
<rss><channel>
  <item><title>BTC ETF approval breakthrough</title>
  <link>https://example.com/etf</link>
  <description>big news</description></item>
</channel></rss>"""

_LLAMA = [
    {
        "name": "Aave",
        "symbol": "AAVE",
        "change_1d": 20.5,
        "tvl": 1_000_000_000,
    }
]


def _is_dead_host(url: str) -> bool:
    u = url.lower()
    return "status_updates" in u or "cryptocompare" in u or "min-api.cryptocompare.com" in u


def test_fetch_coingecko_news_never_requests_status_updates():
    requested: list[str] = []

    def capture(url, timeout=15.0, headers=None):
        requested.append(url)
        raise AssertionError(f"unexpected HTTP {url}")

    with patch("intelligence.memory.news_providers._http_get", side_effect=capture):
        items = fetch_coingecko_news(limit=10)

    assert items == []
    assert requested == []
    assert not any("status_updates" in u for u in requested)


def test_fetch_cryptocompare_news_no_http_without_key(monkeypatch):
    monkeypatch.delenv("CRYPTOCOMPARE_API_KEY", raising=False)
    requested: list[str] = []

    def capture(url, timeout=15.0, headers=None):
        requested.append(url)
        raise AssertionError(f"unexpected HTTP {url}")

    with patch("intelligence.memory.news_providers._http_get", side_effect=capture):
        items = fetch_cryptocompare_news(limit=10)

    assert items == []
    assert requested == []


def test_fetch_cryptocompare_news_keyed_sends_apikey(monkeypatch):
    monkeypatch.setenv("CRYPTOCOMPARE_API_KEY", "test-key-464")
    captured: list[tuple[str, dict | None]] = []

    def fake_get(url, timeout=15.0, headers=None):
        captured.append((url, headers))
        return json.dumps(
            {
                "Data": [
                    {
                        "title": "Keyed headline",
                        "url": "https://example.com/cc",
                        "body": "body",
                        "published_on": 1,
                    }
                ]
            }
        ).encode()

    with patch("intelligence.memory.news_providers._http_get", side_effect=fake_get):
        items = fetch_cryptocompare_news(limit=5)

    assert len(items) == 1
    assert items[0]["title"] == "Keyed headline"
    assert captured == [(CRYPTOCOMPARE_NEWS_URL, {"authorization": "Apikey test-key-464"})]


def test_poll_skip_markers_and_live_sources_stay_on(monkeypatch):
    """Unkeyed poll: skip markers, no dead-host HTTP, RSS + DeFiLlama still run."""
    monkeypatch.delenv("CRYPTOCOMPARE_API_KEY", raising=False)
    requested: list[str] = []
    store = InMemoryMemoryStore()

    def fake_get(url, timeout=15.0, headers=None):
        requested.append(url)
        if _is_dead_host(url):
            raise AssertionError(f"dead host requested: {url}")
        if "llama.fi" in url:
            return json.dumps(_LLAMA).encode()
        if "rss" in url or "feed" in url or "coindesk" in url:
            return _RSS
        raise RuntimeError(f"skip {url}")

    with patch("intelligence.memory.news_providers._http_get", side_effect=fake_get):
        counts = poll_and_ingest_news(
            store,
            rss_feeds=["https://example.com/rss"],
            use_coingecko=True,
            use_free_crypto_news=False,
            use_defillama=True,
            scrape_sources=[],
            max_per_source=5,
            universe=[],
            config={
                "memory": {
                    "news": {
                        "coingecko_news": True,
                        "cryptocompare_extra": True,
                        "free_crypto_news": False,
                        "tag_universe": False,
                    },
                    "onchain": {"defillama": True},
                }
            },
        )

    assert counts["coingecko"] == SKIPPED_NO_ENDPOINT
    assert counts["cryptocompare"] == SKIPPED_NO_KEY
    assert counts["coingecko"] != 0
    assert counts["cryptocompare"] != 0
    assert counts["rss"] >= 1
    assert counts["defillama"] >= 1
    assert not any(_is_dead_host(u) for u in requested)
    assert any("llama.fi" in u for u in requested)
    assert any("rss" in u or "feed" in u for u in requested)
