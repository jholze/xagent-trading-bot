"""Free news providers + RSS + scrape + DeFiLlama (no new paid APIs)."""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.request import Request, urlopen

from intelligence.memory.event_ingest import ingest_news_item
from intelligence.memory.store import MemoryStore
from logger import log

DEFAULT_RSS = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]

# free-crypto-news style aggregators (public RSS / free JSON)
FREE_CRYPTO_NEWS_FEEDS = [
    "https://www.reddit.com/r/CryptoCurrency/new.rss",
    "https://bitcoinmagazine.com/.rss/full/",
]


def _http_get(url: str, timeout: float = 15.0) -> bytes:
    import ssl

    req = Request(
        url,
        headers={
            "User-Agent": "xagent-memory/1.0 (+trading-bot; research)",
            "Accept": "application/json, application/rss+xml, text/xml, */*",
        },
    )
    ctx = None
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            ctx = ssl.create_default_context()
        except Exception:
            ctx = None
    with urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def fetch_rss_items(feed_url: str, limit: int = 20) -> list[dict[str, str]]:
    try:
        raw = _http_get(feed_url)
        root = ET.fromstring(raw)
    except Exception as e:
        log(f"rss fetch failed {feed_url}: {e}", "WARNING")
        return []
    items: list[dict[str, str]] = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            items.append(
                {
                    "title": html.unescape(re.sub(r"<[^>]+>", " ", title)).strip(),
                    "url": link,
                    "body": html.unescape(re.sub(r"<[^>]+>", " ", desc))[:400],
                    "published": pub,
                }
            )
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//a:entry", ns)[:limit]:
            title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("a:link", ns)
            link = (link_el.get("href") if link_el is not None else "") or ""
            summary = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
            if title:
                items.append(
                    {
                        "title": html.unescape(title),
                        "url": link,
                        "body": html.unescape(re.sub(r"<[^>]+>", " ", summary))[:400],
                        "published": "",
                    }
                )
    return items


def fetch_cryptocompare_news(limit: int = 20) -> list[dict[str, str]]:
    """CryptoCompare free news (no API key for basic feed). CoinGecko status_updates is 404."""
    try:
        raw = _http_get("https://min-api.cryptocompare.com/data/v2/news/?lang=EN")
        data = json.loads(raw.decode("utf-8"))
        out = []
        for row in (data.get("Data") or [])[:limit]:
            title = (row.get("title") or "").strip()
            if not title:
                continue
            out.append(
                {
                    "title": title,
                    "url": (row.get("url") or row.get("guid") or "").strip(),
                    "body": (row.get("body") or "")[:400],
                    "published": str(row.get("published_on") or ""),
                }
            )
        return out
    except Exception as e:
        log(f"cryptocompare news fetch failed: {e}", "WARNING")
        return []


def fetch_coingecko_news(limit: int = 20) -> list[dict[str, str]]:
    """CoinGecko public endpoints — try status_updates then fall back to CryptoCompare."""
    try:
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
        if out:
            return out
    except Exception as e:
        log(f"coingecko status_updates unavailable: {e}", "DEBUG")
    # Working free path used as coingecko-class news
    return fetch_cryptocompare_news(limit=limit)


def fetch_free_crypto_news(limit: int = 15) -> list[dict[str, str]]:
    """Aggregate free RSS feeds (Reddit crypto + Bitcoin Magazine etc.)."""
    out: list[dict[str, str]] = []
    for feed in FREE_CRYPTO_NEWS_FEEDS:
        for item in fetch_rss_items(feed, limit=max(3, limit // len(FREE_CRYPTO_NEWS_FEEDS))):
            item = dict(item)
            item["source_tag"] = "free_crypto_news"
            out.append(item)
        if len(out) >= limit:
            break
    return out[:limit]


def scrape_list_page(
    list_url: str,
    *,
    title_pattern: str = r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>([^<]{20,200})</a>",
    limit: int = 10,
) -> list[dict[str, str]]:
    """Lightweight HTML scrape adapter — excerpt only, prefer RSS when available."""
    try:
        raw = _http_get(list_url).decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"scrape failed {list_url}: {e}", "WARNING")
        return []
    items = []
    seen = set()
    for m in re.finditer(title_pattern, raw, re.I):
        url, title = m.group(1), m.group(2)
        title = html.unescape(re.sub(r"\s+", " ", title)).strip()
        if not title or title in seen:
            continue
        if url.startswith("/"):
            # relative
            from urllib.parse import urljoin

            url = urljoin(list_url, url)
        seen.add(title)
        items.append({"title": title, "url": url, "body": "", "published": ""})
        if len(items) >= limit:
            break
    return items


def fetch_defillama_tvl_shocks(limit: int = 8) -> list[dict[str, Any]]:
    """DeFiLlama free protocols — flag large 1d TVL moves as onchain events."""
    try:
        raw = _http_get("https://api.llama.fi/protocols")
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        log(f"defillama fetch failed: {e}", "WARNING")
        return []
    shocks = []
    for row in data if isinstance(data, list) else []:
        try:
            change = float(row.get("change_1d") or 0)
            tvl = float(row.get("tvl") or 0)
        except (TypeError, ValueError):
            continue
        if tvl < 50_000_000:  # ignore tiny protocols
            continue
        if abs(change) < 12:  # ≥12% 1d move
            continue
        name = (row.get("name") or row.get("slug") or "protocol").strip()
        symbol = (row.get("symbol") or "").strip().upper()
        shocks.append(
            {
                "name": name,
                "symbol": symbol,
                "change_1d": change,
                "tvl": tvl,
                "title": f"DeFiLlama TVL shock: {name} {change:+.1f}% 1d (TVL ${tvl/1e9:.2f}B)",
            }
        )
    shocks.sort(key=lambda x: abs(x["change_1d"]), reverse=True)
    return shocks[:limit]


def ingest_defillama_events(store: MemoryStore | None = None) -> int:
    store = store or MemoryStore()
    n = 0
    for shock in fetch_defillama_tvl_shocks():
        syms = []
        if shock.get("symbol") and len(shock["symbol"]) <= 6:
            syms = [f"{shock['symbol']}/USDT"]
        ev = ingest_news_item(
            title=shock["title"],
            url=f"https://defillama.com/protocol/{shock.get('name', '').lower().replace(' ', '-')}",
            source="defillama",
            body=f"tvl_change_1d={shock['change_1d']} tvl={shock['tvl']}",
            symbols=syms or ["BTC/USDT", "ETH/USDT"],
            event_type="onchain_tvl_shock",
            store=store,
        )
        if ev:
            n += 1
    return n


def poll_and_ingest_news(
    store: MemoryStore | None = None,
    *,
    rss_feeds: list[str] | None = None,
    use_coingecko: bool = True,
    use_free_crypto_news: bool = True,
    use_defillama: bool = True,
    scrape_sources: list[dict[str, Any]] | None = None,
    max_per_source: int = 15,
    config: dict | None = None,
    universe: list[str] | None = None,
    boost: bool = False,
) -> dict[str, int]:
    """Ingest news/events into memory_market_events.

    universe: open book + watchlist symbols — prefer tagging those coins.
    boost: higher per-source limits (for 6m backfill).
    """
    store = store or MemoryStore()
    mem = {}
    if config is None:
        try:
            from core.config import get_bot_config

            mem = (get_bot_config().raw.get("memory") or {})
        except Exception:
            mem = {}
    else:
        mem = (config or {}).get("memory") or {}
    news_cfg = mem.get("news") or {}
    onchain_cfg = mem.get("onchain") or {}

    if rss_feeds is None:
        rss_feeds = list(news_cfg.get("rss_feeds") or DEFAULT_RSS)
    if use_coingecko is True:
        use_coingecko = bool(news_cfg.get("coingecko_news", True))
    if use_free_crypto_news is True:
        use_free_crypto_news = bool(news_cfg.get("free_crypto_news", True))
    if scrape_sources is None:
        scrape_sources = list(news_cfg.get("scrape_sources") or [])
    if use_defillama is True:
        use_defillama = bool(onchain_cfg.get("defillama", True))

    # Prefer book/watchlist universe when caller did not pass one
    if universe is None and news_cfg.get("tag_universe", True):
        try:
            from intelligence.memory.coin_facts_ingest import coin_fact_universe

            universe = coin_fact_universe(config if isinstance(config, dict) else None)
        except Exception:
            universe = None

    if boost:
        max_per_source = max(max_per_source, int(news_cfg.get("backfill_max_per_source") or 40))
    else:
        max_per_source = int(news_cfg.get("max_per_source") or max_per_source)

    counts = {
        "rss": 0,
        "coingecko": 0,
        "free_crypto_news": 0,
        "scrape": 0,
        "defillama": 0,
        "cryptocompare": 0,
        "universe_tagged": 0,
        "max_per_source": max_per_source,
    }

    def _ingest(title: str, url: str, source: str, body: str = "", published: str | None = None) -> bool:
        ev = ingest_news_item(
            title=title,
            url=url or "",
            source=source,
            body=body or "",
            published_at=published,
            store=store,
            universe=universe,
        )
        if not ev:
            return False
        meta = ev.metadata or {}
        if meta.get("book_symbols"):
            counts["universe_tagged"] += 1
        return True

    for feed in rss_feeds:
        host = feed.split("/")[2] if "//" in feed else "rss"
        for item in fetch_rss_items(feed, limit=max_per_source):
            if _ingest(
                item["title"],
                item.get("url") or "",
                f"rss:{host}",
                item.get("body") or "",
                item.get("published") or None,
            ):
                counts["rss"] += 1

    if use_coingecko:
        for item in fetch_coingecko_news(limit=max_per_source):
            src = "coingecko" if "coingecko" in (item.get("url") or "") else "cryptocompare"
            if _ingest(
                item["title"],
                item.get("url") or "",
                src,
                item.get("body") or "",
                item.get("published") or None,
            ):
                counts["coingecko"] += 1
                if src == "cryptocompare":
                    counts["cryptocompare"] += 1

    # Explicit CryptoCompare pull (always when boost or config flag)
    if boost or bool(news_cfg.get("cryptocompare_extra", True)):
        try:
            for item in fetch_cryptocompare_news(limit=max_per_source):
                if _ingest(
                    item["title"],
                    item.get("url") or "",
                    "cryptocompare",
                    item.get("body") or "",
                    item.get("published") or None,
                ):
                    counts["cryptocompare"] += 1
        except Exception as e:
            log(f"cryptocompare extra: {e}", "DEBUG")

    if use_free_crypto_news:
        for item in fetch_free_crypto_news(limit=max_per_source):
            if _ingest(
                item["title"],
                item.get("url") or "",
                "free_crypto_news",
                item.get("body") or "",
            ):
                counts["free_crypto_news"] += 1

    default_title_pat = r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>([^<]{20,200})</a>"
    for src in scrape_sources:
        if not isinstance(src, dict) or not src.get("list_url"):
            continue
        for item in scrape_list_page(
            str(src["list_url"]),
            title_pattern=str(src.get("title_pattern") or default_title_pat),
            limit=int(src.get("limit") or max_per_source),
        ):
            if _ingest(
                item["title"],
                item.get("url") or "",
                f"scrape:{src.get('name') or 'page'}",
                item.get("body") or "",
            ):
                counts["scrape"] += 1

    if use_defillama:
        try:
            counts["defillama"] = ingest_defillama_events(store)
        except Exception as e:
            log(f"defillama ingest: {e}", "WARNING")

    log(f"memory news poll: {counts}", "INFO")
    return counts


def poll_news_for_backfill(
    store: MemoryStore | None = None,
    *,
    universe: list[str] | None = None,
    config: dict | None = None,
    rounds: int = 2,
) -> dict[str, int]:
    """Heavier news/event ingest for 6m backfill (multiple rounds + boost)."""
    store = store or MemoryStore()
    totals: dict[str, int] = {}
    for i in range(max(1, int(rounds))):
        c = poll_and_ingest_news(
            store,
            config=config,
            universe=universe,
            boost=True,
            max_per_source=40,
        )
        for k, v in (c or {}).items():
            if isinstance(v, int):
                totals[k] = int(totals.get(k) or 0) + int(v)
        totals["rounds"] = i + 1
    return totals
