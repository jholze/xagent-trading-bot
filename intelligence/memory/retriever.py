"""Retrieval: Mongo filter + local cosine over embeddings (Weaviate optional)."""

from __future__ import annotations

from intelligence.memory.embeddings import cosine, embed_text
from intelligence.memory.models import CoinProfile, Lesson, MarketEvent
from intelligence.memory.store import MemoryStore
from intelligence.memory.vector_weaviate import WeaviateIndex, weaviate_enabled


def similar_events(
    query: str,
    *,
    symbol: str | None = None,
    k: int = 8,
    store: MemoryStore | None = None,
) -> list[MarketEvent]:
    store = store or MemoryStore()
    if weaviate_enabled():
        try:
            idx = WeaviateIndex()
            ids = idx.search_events(query, symbol=symbol, k=k)
            out = []
            for eid in ids:
                e = store.get_event(eid)
                if e:
                    out.append(e)
            if out:
                return out
        except Exception:
            pass
    # local fallback
    qv = embed_text(query)
    events = store.list_events(symbol=symbol, limit=80)
    scored = []
    for e in events:
        vec = e.embedding or embed_text(f"{e.event_type} {e.description}")
        scored.append((cosine(qv, vec), e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:k]]


def lessons_for(symbol: str, *, k: int = 5, store: MemoryStore | None = None) -> list[Lesson]:
    store = store or MemoryStore()
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
