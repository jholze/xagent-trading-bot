"""Retrieval: Mongo filter + local cosine over embeddings (Weaviate optional)."""

from __future__ import annotations

from typing import Any

from intelligence.memory.embeddings import cosine, embed_text
from intelligence.memory.models import CoinProfile, Lesson, MarketEvent
from intelligence.memory.store import MemoryStore
from intelligence.memory.vector_weaviate import WeaviateIndex, weaviate_enabled


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
    """Vector + metadata filter retrieval for MarketEvents."""
    store = store or MemoryStore()
    filters = filters or {}
    symbol = symbol or filters.get("symbol")
    event_type = event_type or filters.get("event_type")
    since_iso = since_iso or filters.get("since")

    if weaviate_enabled():
        try:
            idx = WeaviateIndex()
            ids = idx.search_events(query, symbol=symbol, event_type=event_type, k=k)
            out = []
            for eid in ids:
                e = store.get_event(eid)
                if e:
                    if since_iso and e.timestamp < since_iso:
                        continue
                    out.append(e)
            if out:
                return out[:k]
        except Exception:
            pass
    # local fallback
    qv = embed_text(query)
    events = store.list_events(symbol=symbol, event_type=event_type, since_iso=since_iso, limit=80)
    scored = []
    for e in events:
        vec = e.embedding or embed_text(f"{e.event_type} {e.description}")
        scored.append((cosine(qv, vec), e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:k]]


def similar_coin_situations(
    profile: CoinProfile | str,
    *,
    k: int = 8,
    store: MemoryStore | None = None,
) -> list[CoinProfile]:
    """Find coins with similar risk/history situation (vector over profile summary)."""
    store = store or MemoryStore()
    if isinstance(profile, str):
        prof = store.get_profile(profile)
        if not prof:
            return []
        profile = prof
    query = (
        f"{profile.symbol} size_bias={profile.size_bias} entry={profile.entry_bias} "
        f"win={profile.win_rate} pnl={profile.total_pnl_usdt} {profile.rationale}"
    )
    if weaviate_enabled():
        try:
            idx = WeaviateIndex()
            rows = idx.search_similar_profiles(
                query, k=k + 2, ledger_scope=profile.ledger_scope
            )
            out = []
            for row in rows:
                sym = row.get("symbol")
                if not sym or sym == profile.symbol:
                    continue
                p = store.get_profile(sym, ledger_scope=profile.ledger_scope)
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
        text = f"{p.symbol} size_bias={p.size_bias} entry={p.entry_bias} {p.rationale}"
        vec = p.embedding or embed_text(text)
        scored.append((cosine(qv, vec), p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:k]]


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


def compact_context(symbol: str, store: MemoryStore | None = None) -> str:
    """One-line context for decision rationale."""
    store = store or MemoryStore()
    parts = []
    prof = store.get_profile(symbol)
    if prof and prof.rationale:
        parts.append(f"mem:{prof.rationale[:80]}")
    les = lessons_for(symbol, k=1, store=store)
    if les:
        parts.append(f"lesson:{les[0].text[:80]}")
    ev = store.list_events(symbol=symbol, limit=1)
    if ev:
        parts.append(f"event:{ev[0].description[:60]}")
    return " | ".join(parts)
