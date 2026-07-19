"""Retrieval: Mongo filter + local cosine over embeddings (Weaviate optional)."""

from __future__ import annotations

import re
from typing import Any

from intelligence.memory.embeddings import cosine, embed_text
from intelligence.memory.models import CoinProfile, Lesson, MarketEvent
from intelligence.memory.store import MemoryStore, resolve_memory_scope
from intelligence.memory.vector_weaviate import WeaviateIndex, weaviate_enabled

# Query → event_type hints so sparse but critical types aren't drowned by news flood.
_QUERY_TYPE_HINTS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (
        ("fomc", "fed", "cpi", "nfp", "macro", "calendar", "rate decision"),
        ("macro_scheduled", "macro_window", "macro_pressure", "macro_print", "macro_news"),
    ),
    (
        ("lunar", "lunarcrush", "lc_", "lc ", "social spike", "sentiment extreme", "galaxy"),
        ("lc_social_spike", "lc_sentiment_extreme", "lc_social_fade", "cmc_social", "cmc_trending"),
    ),
    (
        ("soft_block", "gross_loss", "gross loss", "rebuy", "cooloff", "sensor entry"),
        ("trade_outcome", "soft_block", "sensor_lesson", "gross_loss"),
    ),
    (
        ("unlock", "vesting"),
        ("token_unlock", "structure_risk"),
    ),
    (
        ("asia", "london", "session", "fakeout"),
        ("session_open", "session_regime", "session_pressure"),
    ),
]


def _query_tokens(query: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]{2,}", (query or "").lower()) if t}


def _event_blob(e: MarketEvent) -> str:
    return f"{e.event_type or ''} {e.description or ''} {e.source or ''}".lower()


def _keyword_boost(query: str, blob: str) -> float:
    """Overlap score so hash-embeddings still surface needle-rich events."""
    toks = _query_tokens(query)
    if not toks or not blob:
        return 0.0
    hits = sum(1 for t in toks if t in blob)
    # substring boost for multi-word needles already tokenized
    qlow = (query or "").lower()
    for phrase in ("soft_block", "gross_loss", "gross loss", "fomc", "lunarcrush", "lc_"):
        if phrase in qlow and phrase in blob:
            hits += 1.5
    return float(hits)


def _type_hints_for_query(query: str) -> list[str]:
    q = (query or "").lower()
    out: list[str] = []
    for needles, types in _QUERY_TYPE_HINTS:
        if any(n in q for n in needles):
            for t in types:
                if t not in out:
                    out.append(t)
    return out


def _score_event(query: str, qv: list[float], e: MarketEvent) -> float:
    blob = _event_blob(e)
    vec = e.embedding or embed_text(f"{e.event_type} {e.description}")
    cos = float(cosine(qv, vec) or 0.0)
    return cos + 0.12 * _keyword_boost(query, blob)


def _candidate_events(
    store: MemoryStore,
    *,
    query: str,
    symbol: str | None,
    event_type: str | None,
    since_iso: str | None,
) -> list[MarketEvent]:
    """Recent pool + type-hinted pulls so macro/LC/soft_block survive news floods."""
    by_id: dict[str, MarketEvent] = {}

    def _add(rows: list[MarketEvent] | None) -> None:
        for e in rows or []:
            eid = getattr(e, "event_id", None) or ""
            if eid and eid not in by_id:
                by_id[eid] = e

    if event_type:
        _add(
            store.list_events(
                symbol=symbol, event_type=event_type, since_iso=since_iso, limit=80
            )
        )
    else:
        _add(store.list_events(symbol=symbol, since_iso=since_iso, limit=80))
        for et in _type_hints_for_query(query):
            _add(
                store.list_events(
                    symbol=symbol, event_type=et, since_iso=since_iso, limit=24
                )
            )
            # Global (no symbol) for calendar/social that are BTC-tagged or multi-symbol
            if symbol:
                _add(
                    store.list_events(
                        symbol=None, event_type=et, since_iso=since_iso, limit=16
                    )
                )
    return list(by_id.values())


def similar_events(
    query: str,
    *,
    symbol: str | None = None,
    event_type: str | None = None,
    since_iso: str | None = None,
    k: int = 8,
    store: MemoryStore | None = None,
    filters: dict[str, Any] | None = None,
) -> list[MarketEvent]:
    """Hybrid retrieval: Weaviate (optional) + local cosine + keyword/type boosts."""
    store = store or MemoryStore()
    filters = filters or {}
    symbol = symbol or filters.get("symbol")
    event_type = event_type or filters.get("event_type")
    since_iso = since_iso or filters.get("since")

    local_pool = _candidate_events(
        store,
        query=query,
        symbol=symbol,
        event_type=event_type,
        since_iso=since_iso,
    )
    qv = embed_text(query)

    if weaviate_enabled():
        try:
            idx = WeaviateIndex()
            ids = idx.search_events(query, symbol=symbol, event_type=event_type, k=max(k, 12))
            wv_hits: list[MarketEvent] = []
            for eid in ids:
                e = store.get_event(eid)
                if e:
                    if since_iso and e.timestamp < since_iso:
                        continue
                    wv_hits.append(e)
            # Merge Weaviate hits into local pool, then hybrid-rank everything
            if wv_hits:
                by_id = {e.event_id: e for e in local_pool if getattr(e, "event_id", None)}
                for e in wv_hits:
                    if e.event_id:
                        by_id[e.event_id] = e
                local_pool = list(by_id.values())
        except Exception:
            pass

    scored = [(_score_event(query, qv, e), e) for e in local_pool]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:k]]


def similar_coin_situations(
    profile: CoinProfile | str,
    *,
    k: int = 8,
    store: MemoryStore | None = None,
    ledger_scope: str | None = None,
) -> list[CoinProfile]:
    """Find coins with similar risk/history situation (vector over profile summary)."""
    store = store or MemoryStore()
    scope = resolve_memory_scope(ledger_scope)
    if isinstance(profile, str):
        prof = store.get_profile(profile, ledger_scope=scope)
        if not prof:
            return []
        profile = prof
    scope = resolve_memory_scope(profile.ledger_scope or scope)
    query = (
        f"{profile.symbol} size_bias={profile.size_bias} entry={profile.entry_bias} "
        f"win={profile.win_rate} pnl={profile.total_pnl_usdt} {profile.rationale}"
    )
    if weaviate_enabled():
        try:
            idx = WeaviateIndex()
            rows = idx.search_similar_profiles(query, k=k + 2, ledger_scope=scope)
            out = []
            for row in rows:
                sym = row.get("symbol")
                if not sym or sym == profile.symbol:
                    continue
                p = store.get_profile(sym, ledger_scope=scope)
                if p:
                    out.append(p)
            if out:
                return out[:k]
        except Exception:
            pass
    # local cosine over profile embeddings / text
    qv = profile.embedding or embed_text(query)
    scored = []
    for p in store.list_profiles(tenant_id=profile.tenant_id, limit=100):
        if p.symbol == profile.symbol:
            continue
        if p.ledger_scope and p.ledger_scope != scope:
            continue
        text = f"{p.symbol} size_bias={p.size_bias} entry={p.entry_bias} {p.rationale}"
        vec = p.embedding or embed_text(text)
        scored.append((cosine(qv, vec), p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:k]]


def social_events_for(
    symbol: str,
    *,
    k: int = 8,
    store: MemoryStore | None = None,
) -> list[MarketEvent]:
    """Recent CMC/LC social events for a symbol (local list + optional vector)."""
    from intelligence.memory.social_ingest import (
        EVT_CMC_QUOTE_EXTREME,
        EVT_CMC_SOCIAL,
        EVT_CMC_TRENDING,
        EVT_LC_FADE,
        EVT_LC_SENTIMENT,
        EVT_LC_SPIKE,
    )

    types = {
        EVT_CMC_SOCIAL,
        EVT_CMC_TRENDING,
        EVT_CMC_QUOTE_EXTREME,
        EVT_LC_SPIKE,
        EVT_LC_FADE,
        EVT_LC_SENTIMENT,
    }
    hits = similar_events(
        f"social {symbol}",
        symbol=symbol,
        k=max(k * 2, 16),
        store=store,
    )
    out = [e for e in hits if e.event_type in types]
    if out:
        return out[:k]
    store = store or MemoryStore()
    return [
        e
        for e in store.list_events(symbol=symbol, limit=40)
        if e.event_type in types
    ][:k]


def lessons_for(symbol: str, *, k: int = 5, store: MemoryStore | None = None) -> list[Lesson]:
    store = store or MemoryStore()
    if weaviate_enabled():
        try:
            idx = WeaviateIndex()
            rows = idx.search_lessons(f"lessons for {symbol}", k=k)
            out = []
            for row in rows:
                lid = row.get("lesson_id")
                if not lid:
                    continue
                for L in store.list_lessons(symbol=symbol, limit=50):
                    if L.lesson_id == lid:
                        out.append(L)
                        break
            if out:
                return out[:k]
        except Exception:
            pass
    return store.list_lessons(symbol=symbol, limit=k)


def compact_context(
    symbol: str,
    store: MemoryStore | None = None,
    *,
    ledger_scope: str | None = None,
) -> str:
    """One-line context for decision rationale."""
    store = store or MemoryStore()
    scope = resolve_memory_scope(ledger_scope)
    parts = []
    prof = store.get_profile(symbol, ledger_scope=scope)
    if prof and prof.rationale:
        parts.append(f"mem:{prof.rationale[:80]}")
    les = lessons_for(symbol, k=1, store=store)
    if les:
        parts.append(f"lesson:{les[0].text[:80]}")
    ev = store.list_events(symbol=symbol, limit=1)
    if ev:
        parts.append(f"event:{ev[0].description[:60]}")
    return " | ".join(parts)
