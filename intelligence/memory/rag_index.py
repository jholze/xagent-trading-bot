"""Idempotent indexing of Trading Memory entities into RAG chunks.

Never reads/writes ledger orders/positions collections — only MemoryStore + RagStore.
"""

from __future__ import annotations

from typing import Any

from hermes.memory.rag_retriever import RagRetriever
from intelligence.memory.rag_config import rag_config, rag_enabled
from intelligence.memory.store import MemoryStore
from logger import log


def index_fusion_snapshot(
    bias: dict[str, Any] | None,
    *,
    retriever: RagRetriever | None = None,
    config: dict | None = None,
) -> str:
    """Optional C8: index fusion/oracle state as a RAG chunk.

    Default off via memory.rag.index_market_context. Returns chunk_id or "".
    """
    if not bias or not isinstance(bias, dict):
        return ""
    cfg = rag_config(config)
    if not cfg.get("enabled") or not cfg.get("index_market_context"):
        return ""
    try:
        regime = bias.get("regime") or "unknown"
        size = bias.get("size_mult")
        sensor = bias.get("sensor_policy") or "active"
        source = bias.get("source") or ",".join(bias.get("sources") or []) or "?"
        block = bool(bias.get("block_buys"))
        rationale = (bias.get("rationale") or "")[:180]
        text = (
            f"Fusion market_context regime={regime} size_mult={size} "
            f"sensor={sensor} source={source} block_buys={block} "
            f"warmup={bool(bias.get('warmup_active'))} {rationale}"
        ).strip()
        meta = {
            "type": "market_context",
            "source_id": f"fusion|{regime}|{size}|{sensor}|{source}",
            "source": "fusion",
            "regime": str(regime),
        }
        rag = retriever or RagRetriever(config=config)
        return rag.add_to_memory(text, meta) or ""
    except Exception as e:
        log(f"rag_index fusion snapshot: {e}", "WARNING")
        return ""


def index_store_into_rag(
    memory_store: MemoryStore | None = None,
    retriever: RagRetriever | None = None,
    *,
    config: dict | None = None,
    lesson_limit: int = 40,
    trade_limit: int = 40,
    event_limit: int = 40,
) -> dict[str, int]:
    """Upsert lessons/trades/events as RAG chunks. Fail-open."""
    out = {"lessons": 0, "trades": 0, "events": 0, "skipped": 0, "errors": 0}
    if not rag_enabled(config):
        out["skipped"] = 1
        return out
    cfg = rag_config(config)
    if not cfg.get("index_on_cycle", True):
        out["skipped"] = 1
        return out

    store = memory_store or MemoryStore()
    rag = retriever or RagRetriever(config=config)
    # One Weaviate ready/schema check for the whole batch (perf review P0)
    try:
        rag.prepare_weaviate()
    except Exception:
        pass

    try:
        for les in store.list_lessons(limit=lesson_limit):
            text = (getattr(les, "text", None) or "").strip()
            if not text:
                continue
            symbols = list(getattr(les, "symbols", None) or [])
            meta = {
                "type": "lesson",
                "source_id": getattr(les, "lesson_id", "") or "",
                "symbol": symbols[0] if symbols else "",
                "source": "reflector",
            }
            if rag.add_to_memory(text, meta):
                out["lessons"] += 1
    except Exception as e:
        log(f"rag_index lessons: {e}", "WARNING")
        out["errors"] += 1

    try:
        for tr in store.list_trades(limit=trade_limit):
            sym = getattr(tr, "symbol", "") or ""
            direction = getattr(tr, "direction", "") or ""
            pnl = getattr(tr, "pnl_usdt", None)
            reason = (getattr(tr, "reason", None) or "")[:200]
            outcome = getattr(tr, "outcome", "") or ""
            text = (
                f"Trade {sym} {direction} outcome={outcome} "
                f"pnl_usdt={pnl} reason={reason}"
            ).strip()
            meta = {
                "type": "trade",
                "source_id": getattr(tr, "trade_id", "") or "",
                "symbol": sym,
                "source": getattr(tr, "source", "") or "memory",
                "ledger_scope": getattr(tr, "ledger_scope", "") or "",
            }
            if rag.add_to_memory(text, meta):
                out["trades"] += 1
    except Exception as e:
        log(f"rag_index trades: {e}", "WARNING")
        out["errors"] += 1

    try:
        for ev in store.list_events(limit=event_limit):
            desc = (getattr(ev, "description", None) or "").strip()
            if not desc:
                continue
            symbols = list(getattr(ev, "symbols", None) or [])
            text = f"{getattr(ev, 'event_type', 'event')}: {desc}"
            meta = {
                "type": "event",
                "source_id": getattr(ev, "event_id", "") or "",
                "symbol": symbols[0] if symbols else "",
                "source": getattr(ev, "source", "") or "event",
            }
            if rag.add_to_memory(text, meta):
                out["events"] += 1
    except Exception as e:
        log(f"rag_index events: {e}", "WARNING")
        out["errors"] += 1

    log(
        f"rag_index done lessons={out['lessons']} trades={out['trades']} "
        f"events={out['events']} errors={out['errors']}",
        "INFO",
    )
    return out
