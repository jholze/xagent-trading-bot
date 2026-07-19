"""CMC AI page URL + HTML parse → CoinFactDraft list (#103).

Network fetch is optional; inject fetch_fn for fixtures/CI.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Callable
from urllib.request import Request, urlopen

from intelligence.memory.coin_facts import (
    CoinFactDraft,
    classify_latest_updates_bullet,
    classify_prediction_driver,
    classify_price_analysis_snippet,
    normalize_symbol,
)

FetchFn = Callable[[str], str]

CMC_AI_BASE = "https://coinmarketcap.com/cmc-ai/{slug}/{path}/"
_ENDPOINT_PATHS = {
    "latest_updates": "latest-updates",
    "price_analysis": "price-analysis",
    "price_prediction": "price-prediction",
}
_SOURCE_BY_ENDPOINT = {
    "latest_updates": "cmc_ai_updates",
    "price_analysis": "cmc_ai_price",
    "price_prediction": "cmc_ai_prediction",
}

# Common ticker → CMC slug overrides
_SLUG_OVERRIDES = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "xrp",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "ALLO": "allora",
    "ZBT": "zero-base-token",  # best-effort; fail-open if wrong
}


def resolve_cmc_slug(symbol: str) -> str | None:
    """Ticker/pair → CMC AI slug. Heuristic; fail-open None only if empty."""
    s = normalize_symbol(symbol)
    if not s:
        return None
    base = s.split("/")[0].strip().upper()
    if not base:
        return None
    if base in _SLUG_OVERRIDES:
        return _SLUG_OVERRIDES[base]
    # default: lowercase ticker (works for many mid-caps with matching slug)
    return base.lower()


def build_cmc_ai_urls(slug: str) -> dict[str, str]:
    out = {}
    for key, path in _ENDPOINT_PATHS.items():
        out[key] = CMC_AI_BASE.format(slug=slug, path=path)
    return out


def default_fetch(url: str, timeout: float = 20.0) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "xagent-memory/1.0 (+trading-bot; research)",
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _strip_tags(raw: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw or "")
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"(?is)<[^>]+>", "\n", t)
    t = html_lib.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def _bullet_lines(text: str) -> list[str]:
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        s = re.sub(r"^[\-\*\u2022•]+\s*", "", s)
        if len(s) < 12:
            continue
        # skip nav chrome
        if re.match(r"^(Home|Markets|NFTs|Portfolio|Watchlist)\b", s, re.I):
            continue
        lines.append(s[:500])
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for s in lines:
        key = s[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def parse_latest_updates_html(
    page_html: str,
    *,
    symbol: str,
    slug: str,
) -> list[CoinFactDraft]:
    text = _strip_tags(page_html)
    drafts: list[CoinFactDraft] = []
    for line in _bullet_lines(text)[:40]:
        d = classify_latest_updates_bullet(line)
        if d:
            d.source = "cmc_ai_updates"
            d.metadata = {**(d.metadata or {}), "slug": slug, "endpoint": "latest_updates"}
            drafts.append(d)
    return drafts


def parse_price_analysis_html(
    page_html: str,
    *,
    symbol: str,
    slug: str,
) -> list[CoinFactDraft]:
    text = _strip_tags(page_html)
    drafts: list[CoinFactDraft] = []
    for line in _bullet_lines(text)[:40]:
        d = classify_price_analysis_snippet(line)
        if d:
            d.source = "cmc_ai_price"
            d.metadata = {**(d.metadata or {}), "slug": slug, "endpoint": "price_analysis"}
            drafts.append(d)
    # also try whole-page window if no bullets matched short lines
    if not drafts and len(text) > 40:
        d = classify_price_analysis_snippet(text[:800])
        if d:
            d.metadata = {**(d.metadata or {}), "slug": slug, "endpoint": "price_analysis"}
            drafts.append(d)
    return drafts


def parse_price_prediction_html(
    page_html: str,
    *,
    symbol: str,
    slug: str,
) -> list[CoinFactDraft]:
    text = _strip_tags(page_html)
    drafts: list[CoinFactDraft] = []
    section = ""
    for line in _bullet_lines(text)[:50]:
        low = line.lower()
        if "bullish" in low and "impact" in low:
            section = "bullish"
            continue
        if "bearish" in low and "impact" in low:
            section = "bearish"
            continue
        if "mixed" in low and "impact" in low:
            section = "mixed"
            continue
        d = classify_prediction_driver(line, section=section)
        if d and d.event_type != "ignore_target":
            d.source = "cmc_ai_prediction"
            d.metadata = {
                **(d.metadata or {}),
                "slug": slug,
                "endpoint": "price_prediction",
                "section": section,
            }
            drafts.append(d)
        # ignore_target deliberately dropped for policy storage
    return drafts


_PARSERS = {
    "latest_updates": parse_latest_updates_html,
    "price_analysis": parse_price_analysis_html,
    "price_prediction": parse_price_prediction_html,
}


def fetch_and_parse_coin(
    symbol: str,
    *,
    fetch_fn: FetchFn | None = None,
    slug: str | None = None,
) -> list[CoinFactDraft]:
    """Fetch all three CMC AI endpoints (or skip on error) and parse."""
    slug = slug or resolve_cmc_slug(symbol)
    if not slug:
        return []
    fetch = fetch_fn or default_fetch
    urls = build_cmc_ai_urls(slug)
    sym = normalize_symbol(symbol)
    all_drafts: list[CoinFactDraft] = []
    for endpoint, url in urls.items():
        parser = _PARSERS.get(endpoint)
        if not parser:
            continue
        try:
            page = fetch(url)
        except Exception:
            continue
        try:
            all_drafts.extend(parser(page, symbol=sym, slug=slug))
        except Exception:
            continue
    return all_drafts
