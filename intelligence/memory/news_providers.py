"""Free news providers + RSS (no new paid APIs)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from urllib.request import Request, urlopen

from intelligence.memory.event_ingest import ingest_news_item
from intelligence.memory.store import MemoryStore
from logger import log

DEFAULT_RSS = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]


def _http_get(url: str, timeout: float = 15.0) -> bytes:
    req = Request(url, headers={"User-Agent": "xagent-memory/1.0 (+trading-bot)"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_rss_items(feed_url: str, limit: int = 20) -> list[dict[str, str]]:
    try:
        raw = _http_get(feed_url)
        root = ET.fromstring(raw)
    except Exception as e:
        log(f"rss fetch failed {feed_url}: {e}", "WARNING")
        return []
    items = []
    # RSS 2.0
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "url": link, "body": desc[:400], "published": pub})
    # Atom
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//a:entry", ns)[:limit]:
            title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("a:link", ns)
            link = (link_el.get("href") if link_el is not None else "") or ""
            summary = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
            if title:
                items.append({"title": title, "url": link, "body": summary[:400], "published": ""})
    return items


def fetch_coingecko_news(limit: int = 20) -> list[dict[str, str]]:
    """CoinGecko status updates / news-like feed (public)."""
    try:
        import json

        raw = _http_get("https://api.coingecko.com/api/v3/status_updates?per_page=20")
        data = json.loads(raw.decode("utf-8"))
        out = []
        for row in (data.get("status_updates") or [])[:limit]:
            desc = (row.get("description") or "").strip()
            proj = ((row.get("project") or {}).get("name") or "").strip()
            title = f"{proj}: {desc[:120]}" if proj else desc[:140]
            if not title:
                continue
            out.append(
                {
                    "title": title,
                    "url": (row.get("project") or {}).get("homepage") or "",
                    "body": desc[:400],
                    "published": str(row.get("created_at") or ""),
                }
            )
        return out
    except Exception as e:
        log(f"coingecko news fetch failed: {e}", "WARNING")
        return []


def poll_and_ingest_news(
    store: MemoryStore | None = None,
    *,
    rss_feeds: list[str] | None = None,
    use_coingecko: bool = True,
    max_per_source: int = 15,
) -> dict[str, int]:
    store = store or MemoryStore()
    counts = {"rss": 0, "coingecko": 0}
    feeds = rss_feeds if rss_feeds is not None else DEFAULT_RSS
    for feed in feeds:
        for item in fetch_rss_items(feed, limit=max_per_source):
            if ingest_news_item(
                title=item["title"],
                url=item.get("url") or "",
                source=f"rss:{feed.split('/')[2] if '//' in feed else 'rss'}",
                body=item.get("body") or "",
                published_at=None,
                store=store,
            ):
                counts["rss"] += 1
    if use_coingecko:
        for item in fetch_coingecko_news(limit=max_per_source):
            if ingest_news_item(
                title=item["title"],
                url=item.get("url") or "",
                source="coingecko",
                body=item.get("body") or "",
                store=store,
            ):
                counts["coingecko"] += 1
    log(f"memory news poll: {counts}", "INFO")
    return counts
