"""Persist DCA policy decisions to Trading Memory + optional RAG (#98 D4).

LEDGER SAFETY: only memory_* / RAG chunks — never orders/positions/trade_history.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from strategies.dca_policy import DcaContext, DcaPolicyResult


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_dca_decision_text(
    *,
    symbol: str,
    result: DcaPolicyResult,
    ctx: DcaContext | None,
    shadow: bool,
    base_usdt: float,
    final_usdt: float,
    applied: str,
) -> str:
    codes = ",".join(result.reason_codes) if result.reason_codes else "-"
    mode = (ctx.cash_mode if ctx else "") or "-"
    sm = float(ctx.fusion_size_mult) if ctx else 1.0
    action = "skip" if result.skip else "buy_dca"
    shadow_s = "shadow" if shadow else "live"
    size_bias = float(ctx.size_bias) if ctx else 1.0
    entry_bias = (ctx.entry_bias if ctx else "") or "neutral"
    dca_n = int(ctx.dca_lesson_count) if ctx else 0
    return (
        f"DCA decision {symbol}: action={action} applied={applied} {shadow_s} "
        f"cash_mode={mode} fusion_sm={sm:.2f} size_bias={size_bias:.2f} "
        f"entry_bias={entry_bias} dca_lessons={dca_n} mult={result.size_mult} "
        f"reasons=[{codes}] usdt={base_usdt:.0f}->{final_usdt:.0f} "
        f"policy_v{result.policy_version}"
    )


def persist_dca_decision_event(
    *,
    symbol: str,
    result: DcaPolicyResult,
    ctx: DcaContext | None = None,
    shadow: bool = False,
    base_usdt: float = 0.0,
    final_usdt: float = 0.0,
    applied: str = "apply",
    policy_cfg: dict | None = None,
    store: Any = None,
    index_rag: bool = True,
) -> str:
    """Write MarketEvent + optional RAG chunk. Returns event_id or '' on fail/disabled."""
    cfg = policy_cfg or {}
    if not cfg.get("persist_events", True):
        return ""

    sym = str(symbol or "").strip()
    if sym and "/" not in sym:
        sym = f"{sym}/USDT"
    if not sym:
        return ""

    text = build_dca_decision_text(
        symbol=sym,
        result=result,
        ctx=ctx,
        shadow=shadow,
        base_usdt=base_usdt,
        final_usdt=final_usdt,
        applied=applied,
    )
    ts = _utc_now_iso()
    raw_id = f"{sym}|{ts}|{applied}|{result.skip}|{result.size_mult}|{','.join(result.reason_codes)}"
    event_id = "dca_" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:20]

    meta = {
        "kind": "dca_decision",
        "applied": applied,
        "shadow": bool(shadow),
        "size_mult": float(result.size_mult),
        "skip": bool(result.skip),
        "reason_codes": list(result.reason_codes),
        "policy_version": result.policy_version,
        "base_usdt": float(base_usdt),
        "final_usdt": float(final_usdt),
        "cash_mode": (ctx.cash_mode if ctx else "") or "",
        "fusion_size_mult": float(ctx.fusion_size_mult) if ctx else 1.0,
        # P6 observability
        "size_bias": float(ctx.size_bias) if ctx else 1.0,
        "entry_bias": (ctx.entry_bias if ctx else "") or "neutral",
        "dca_lesson_count": int(ctx.dca_lesson_count) if ctx else 0,
        "dca_lesson_summary": (ctx.dca_lesson_summary if ctx else "") or "",
    }

    try:
        from intelligence.memory.models import MarketEvent
        from intelligence.memory.store import MemoryStore, memory_enabled

        if not memory_enabled():
            return ""
        ms = store if store is not None else MemoryStore()
        ev = MarketEvent(
            event_id=event_id,
            timestamp=ts,
            event_type="dca_decision",
            symbols=[sym],
            impact_score=0.0 if not result.skip else -0.1,
            description=text[:2000],
            source="dca_policy",
            metadata=meta,
        )
        if not ms.upsert_event(ev):
            return ""
    except Exception:
        return ""

    if index_rag and cfg.get("index_rag", True):
        try:
            from hermes.memory.rag_retriever import RagRetriever
            from intelligence.memory.rag_config import rag_enabled

            if rag_enabled():
                RagRetriever().add_to_memory(
                    text,
                    {
                        "type": "dca_decision",
                        "symbol": sym,
                        "source": "dca_policy",
                        "source_id": event_id,
                    },
                )
        except Exception:
            pass  # fail-open: event already in Mongo

    return event_id
