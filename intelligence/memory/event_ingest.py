"""Ingest MarketEvents from oracle/santiment/fusion and news providers."""

from __future__ import annotations

import hashlib
import os
import re
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


_STOP_TICKERS = frozenset(
    {
        "USD", "USDT", "THE", "AND", "FOR", "API", "CEO", "ETF", "SEC", "USD",
        "NEW", "ALL", "NOW", "OUT", "TOP", "BIG", "HOT", "WHY", "HOW", "WHO",
        "NOT", "BUT", "ANY", "MAY", "CAN", "HAS", "HAD", "WAS", "ARE", "YOU",
    }
)


def extract_symbols(text: str, default: list[str] | None = None) -> list[str]:
    found = set()
    for m in _TICKER.findall(text or ""):
        if m in _STOP_TICKERS:
            continue
        if len(m) <= 5:
            found.add(f"{m}/USDT")
    if not found and default:
        return list(default)
    return sorted(found)[:12]


def match_universe_symbols(
    text: str,
    universe: list[str] | None,
    *,
    max_symbols: int = 8,
) -> list[str]:
    """Prefer coins from our book/watchlist when their base ticker appears in text.

    Example: universe has LAB/USDT → headline "LAB surges 40%" tags LAB/USDT
    instead of only generic BTC.
    """
    if not universe:
        return extract_symbols(text)
    blob = f" {text or ''} ".upper()
    # Normalize blob: word boundaries via non-alnum
    blob_pad = re.sub(r"[^A-Z0-9/]+", " ", blob)
    hits: list[str] = []
    seen: set[str] = set()
    for sym in universe:
        s = str(sym or "").upper().replace("-", "/")
        if not s:
            continue
        if "/" not in s:
            s = f"{s}/USDT"
        base = s.split("/")[0]
        if not base or base in _STOP_TICKERS or len(base) < 2:
            continue
        # whole-word base match (avoid matching "AI" inside "MAIN")
        if re.search(rf"(?<![A-Z0-9]){re.escape(base)}(?![A-Z0-9])", blob_pad):
            if s not in seen:
                seen.add(s)
                hits.append(s)
        if len(hits) >= max_symbols:
            break
    # Also add generic extract, but universe hits first
    for s in extract_symbols(text):
        if s not in seen:
            seen.add(s)
            hits.append(s)
        if len(hits) >= max_symbols:
            break
    return hits[:max_symbols]


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
    event_type: str = "news",
    store: MemoryStore | None = None,
    universe: list[str] | None = None,
    force_event_type_on_impact: bool = True,
) -> MarketEvent | None:
    store = store or MemoryStore()
    text = f"{title} {body}".strip()
    if not text:
        return None
    key = url or title
    eid = make_event_id(source, key)
    # dedupe — but merge symbols if we learn better tags for same article
    existing = store.get_event(eid)
    if existing and not universe:
        return existing
    if symbols is not None:
        syms = list(symbols)
    elif universe:
        syms = match_universe_symbols(text, universe)
    else:
        syms = extract_symbols(text, default=["BTC/USDT"])
    if not syms:
        syms = ["BTC/USDT"]
    if existing:
        # merge symbol tags for better book coverage
        merged = list(dict.fromkeys(list(existing.symbols or []) + syms))[:12]
        if merged != list(existing.symbols or []):
            existing.symbols = merged
            meta = dict(existing.metadata or {})
            meta["symbols_merged"] = True
            existing.metadata = meta
            store.upsert_event(existing)
        return existing
    impact = impact_from_text(text)
    et = (event_type or "news").strip() or "news"
    if force_event_type_on_impact and et == "news":
        # Classify high-signal headlines as structured event types
        low = text.lower()
        if re.search(r"\b(unlock|vesting|cliff)\b", low):
            et = "token_unlock"
        elif re.search(r"\b(hack|exploit|breach|rug)\b", low):
            et = "structure_risk"
        elif re.search(r"\b(listing|listed on|binance|gate\.io|bybit)\b", low):
            et = "listing"
        elif re.search(r"\b(sec|lawsuit|charge|ban|delist)\b", low):
            et = "structure_risk"
        elif re.search(r"\b(etf|fed|fomc|cpi|rate cut|rate hike)\b", low):
            et = "macro_news"
    meta = {
        "body_excerpt": (body or "")[:300],
        "universe_tagged": bool(universe),
    }
    # Mark if any of our book/watchlist coins are in symbols
    if universe:
        ub = {str(u).upper().replace("-", "/") for u in universe}
        hit = [s for s in syms if s in ub or s.split("/")[0] in {x.split("/")[0] for x in ub}]
        if hit:
            meta["book_symbols"] = hit[:8]
    ev = MarketEvent(
        event_id=eid,
        timestamp=published_at or utc_now_iso(),
        event_type=et,
        symbols=syms,
        impact_score=impact,
        description=(title or "")[:400],
        source=source,
        url=url or "",
        metadata=meta,
        embedding=embed_event(title, body, et),
    )
    store.upsert_event(ev)
    return ev


def ingest_webhook_signal(
    signal: Any,
    *,
    store: MemoryStore | None = None,
) -> MarketEvent | None:
    """Map ExternalSignal (news_alert / volume etc.) → MarketEvent. Never sole BUY."""
    store = store or MemoryStore()
    if signal is None:
        return None
    # duck-type ExternalSignal
    source = getattr(signal, "source", None) or (signal.get("source") if isinstance(signal, dict) else "webhook")
    symbol = getattr(signal, "symbol", None) or (signal.get("symbol") if isinstance(signal, dict) else "")
    event_type = getattr(signal, "event_type", None) or (
        signal.get("event_type") if isinstance(signal, dict) else "generic"
    )
    strength = float(
        getattr(signal, "strength", None)
        if not isinstance(signal, dict)
        else signal.get("strength", 0.5)
        or 0.5
    )
    raw = getattr(signal, "raw", None) or (signal.get("raw") if isinstance(signal, dict) else {}) or {}
    ts = getattr(signal, "timestamp", None) or (
        signal.get("timestamp") if isinstance(signal, dict) else None
    )

    title = (
        str(raw.get("title") or raw.get("headline") or raw.get("message") or raw.get("text") or "")
        .strip()
    )
    if not title:
        title = f"{event_type} {symbol}".strip()
    url = str(raw.get("url") or raw.get("link") or "").strip()
    body = str(raw.get("body") or raw.get("description") or "").strip()
    mem_type = "news" if event_type in ("news_alert", "news") else f"webhook_{event_type}"
    # Map strength → impact: high strength news can be ± depending on keywords
    base_impact = impact_from_text(f"{title} {body}")
    if base_impact == 0.0 and event_type == "news_alert":
        base_impact = max(-0.4, min(0.4, (strength - 0.5) * 0.8))
    syms = [symbol] if symbol else extract_symbols(f"{title} {body}", default=["BTC/USDT"])
    return ingest_news_item(
        title=title,
        url=url,
        source=f"webhook:{source}",
        published_at=ts,
        body=body,
        symbols=syms,
        event_type=mem_type,
        store=store,
    )


def ingest_x_post(
    *,
    text: str,
    author: str = "",
    url: str = "",
    symbols: list[str] | None = None,
    store: MemoryStore | None = None,
    enabled: bool | None = None,
) -> MarketEvent | None:
    """Feature-flagged X/Twitter bridge → social_headline MarketEvent."""
    if enabled is None:
        try:
            from core.config import get_bot_config

            mem = (get_bot_config().raw.get("memory") or {}).get("x_bridge") or {}
            enabled = bool(mem.get("enabled", False))
        except Exception:
            enabled = os.environ.get("MEMORY_X_BRIDGE", "").strip() in ("1", "true", "yes")
    if not enabled:
        return None
    text = (text or "").strip()
    if not text:
        return None
    store = store or MemoryStore()
    title = text[:200]
    return ingest_news_item(
        title=title,
        url=url,
        source=f"x:{author or 'unknown'}",
        body=text[:400],
        symbols=symbols,
        event_type="social_headline",
        store=store,
    )


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
