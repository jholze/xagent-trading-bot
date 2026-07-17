"""Ingest MarketEvents from oracle/santiment/fusion and news providers."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from intelligence.memory.embeddings import embed_event
from intelligence.memory.models import MarketEvent, utc_now_iso
from intelligence.memory.store import MemoryStore
from logger import log

_IMPACT_POS = re.compile(
    r"(etf[\s-]?approv|partnership|listing|upgrade|mainnet|breakthrough|surge)", re.I
)
_IMPACT_NEG = re.compile(
    r"(hack|exploit|sec[\s-]?charg|ban|delist|insolv|crash|liquidat|fraud|rug)", re.I
)
_TICKER = re.compile(r"\b([A-Z]{2,10})(?:/USDT)?\b")


def impact_from_text(text: str) -> float:
    t = text or ""
    score = 0.0
    if _IMPACT_NEG.search(t):
        score -= 0.55
    if _IMPACT_POS.search(t):
        score += 0.35
    return max(-1.0, min(1.0, score))


def extract_symbols(text: str, default: list[str] | None = None) -> list[str]:
    found = set()
    for m in _TICKER.findall(text or ""):
        if m in ("USD", "USDT", "THE", "AND", "FOR", "API", "CEO", "ETF", "SEC"):
            continue
        if len(m) <= 5:
            found.add(f"{m}/USDT")
    if not found and default:
        return list(default)
    return sorted(found)[:12]


def make_event_id(source: str, key: str) -> str:
    h = hashlib.sha256(f"{source}|{key}".encode()).hexdigest()[:20]
    return f"{source}:{h}"


def ingest_regime_event(
    *,
    source: str,
    regime: str,
    size_mult: float | None = None,
    rationale: str = "",
    store: MemoryStore | None = None,
) -> MarketEvent | None:
    """From oracle/santiment/fusion state."""
    store = store or MemoryStore()
    regime_u = (regime or "NEUTRAL").upper()
    impact = {
        "CRASH": -0.9,
        "RISK_OFF": -0.55,
        "WARMUP": -0.2,
        "NEUTRAL": 0.0,
        "RISK_ON": 0.35,
    }.get(regime_u, 0.0)
    desc = f"{source} regime={regime_u} size={size_mult} {rationale}".strip()
    eid = make_event_id(source, f"regime|{regime_u}|{utc_now_iso()[:13]}")  # hour bucket
    ev = MarketEvent(
        event_id=eid,
        timestamp=utc_now_iso(),
        event_type="regime_change",
        symbols=["BTC/USDT", "ETH/USDT"],
        impact_score=impact,
        description=desc[:500],
        source=source,
        metadata={"regime": regime_u, "size_mult": size_mult},
        embedding=embed_event(desc, event_type="regime_change"),
    )
    store.upsert_event(ev)
    return ev


def ingest_news_item(
    *,
    title: str,
    url: str = "",
    source: str = "news",
    published_at: str | None = None,
    body: str = "",
    symbols: list[str] | None = None,
    store: MemoryStore | None = None,
) -> MarketEvent | None:
    store = store or MemoryStore()
    text = f"{title} {body}".strip()
    if not text:
        return None
    key = url or title
    eid = make_event_id(source, key)
    # dedupe
    if store.get_event(eid):
        return store.get_event(eid)
    syms = symbols or extract_symbols(text, default=["BTC/USDT"])
    impact = impact_from_text(text)
    ev = MarketEvent(
        event_id=eid,
        timestamp=published_at or utc_now_iso(),
        event_type="news",
        symbols=syms,
        impact_score=impact,
        description=(title or "")[:400],
        source=source,
        url=url or "",
        metadata={"body_excerpt": (body or "")[:300]},
        embedding=embed_event(title, body, "news"),
    )
    store.upsert_event(ev)
    return ev


def sync_fusion_events(store: MemoryStore | None = None) -> int:
    """Pull current oracle/santiment/fusion into events (idempotent hour buckets)."""
    store = store or MemoryStore()
    n = 0
    try:
        from services.market_policy_fusion import get_global_market_bias

        bias = get_global_market_bias()
        if bias.get("active") and bias.get("regime"):
            if ingest_regime_event(
                source="fusion",
                regime=str(bias.get("regime")),
                size_mult=bias.get("size_mult"),
                rationale=str(bias.get("rationale") or ""),
                store=store,
            ):
                n += 1
    except Exception as e:
        log(f"memory fusion event sync: {e}", "DEBUG")
    try:
        from services.market_oracle_store import get_latest_snapshot

        ora = get_latest_snapshot() or {}
        st = ora.get("state") or ora.get("regime")
        if st:
            if ingest_regime_event(
                source="oracle",
                regime=str(st),
                size_mult=ora.get("size_mult"),
                rationale=str(ora.get("rationale") or ""),
                store=store,
            ):
                n += 1
    except Exception as e:
        log(f"memory oracle event sync: {e}", "DEBUG")
    try:
        from services.santiment_store import get_latest_snapshot as get_san

        san = get_san() or {}
        if san.get("regime"):
            if ingest_regime_event(
                source="santiment",
                regime=str(san.get("regime")),
                size_mult=san.get("size_mult"),
                rationale=str(san.get("rationale") or ""),
                store=store,
            ):
                n += 1
    except Exception as e:
        log(f"memory santiment event sync: {e}", "DEBUG")
    return n
