"""Persist DCA policy decisions to Trading Memory + optional RAG (#98 D4).

LEDGER SAFETY: only memory_* / RAG chunks — never orders/positions/trade_history.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from strategies.dca_policy import DcaContext, DcaPolicyResult

# #822: identical skip decisions (same tenant, symbol, applied path, reason set)
# take at most this many rows in memory_market_events: slot "first" keeps the
# first skip, slot "latest" is updated in place with count and last_ts. Kay
# decided N = 2. This is a code default, not a config knob.
SKIP_EVENTS_PER_LOT = 2


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


def skip_coalesce_key(
    tenant_id: str, symbol: str, applied: str, reason_codes: tuple[str, ...] | list[str]
) -> str:
    """Stable key for one family of identical skips. Tenant is part of it (#721)."""
    codes = ",".join(sorted(str(c) for c in (reason_codes or ())))
    raw = f"{tenant_id}|{symbol}|{applied}|{codes}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _index_rag(text: str, sym: str, event_id: str) -> None:
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
                    # #822: one chunk per event id, even when the text changes.
                    "chunk_key": event_id,
                },
            )
    except Exception:
        pass  # fail-open: event already in Mongo


def _persist_coalesced_skip(
    *,
    ms: Any,
    tid: str,
    sym: str,
    ts: str,
    text: str,
    meta: dict,
    applied: str,
    result: DcaPolicyResult,
) -> tuple[str, bool]:
    """Write a skip into its first/latest slot. Returns (event_id, slot_created).

    Raises on store errors; the caller turns that into a no-write.
    """
    from intelligence.memory.models import MarketEvent

    key = skip_coalesce_key(tid, sym, applied, result.reason_codes)
    first_id = f"dca_skip_{key}_first"
    latest_id = f"dca_skip_{key}_latest"

    def _own(ev: Any) -> Any:
        # Ids already carry the tenant; this is a second check.
        return ev if ev is not None and ev.tenant_id == tid else None

    first = _own(ms.get_event(first_id))
    meta = {**meta, "coalesce_key": key, "coalesce_limit": SKIP_EVENTS_PER_LOT}
    if first is None:
        event_id, created = first_id, True
        meta.update({"slot": "first", "count": 1, "first_ts": ts, "last_ts": ts})
    else:
        latest = _own(ms.get_event(latest_id))
        prev = latest if latest is not None else first
        try:
            prev_count = int((prev.metadata or {}).get("count") or 1)
        except (TypeError, ValueError):
            prev_count = 1
        first_ts = str((first.metadata or {}).get("first_ts") or first.timestamp)
        event_id, created = latest_id, latest is None
        meta.update(
            {
                "slot": "latest",
                "count": prev_count + 1,
                "first_ts": first_ts,
                "last_ts": ts,
            }
        )
    desc = f"{text} count={meta['count']} first_ts={meta['first_ts']} last_ts={ts}"
    ev = MarketEvent(
        event_id=event_id,
        timestamp=ts,
        event_type="dca_decision",
        symbols=[sym],
        impact_score=-0.1,
        description=desc[:2000],
        source="dca_policy",
        metadata=meta,
        tenant_id=tid,
    )
    if not ms.upsert_event(ev):
        return "", False
    return event_id, created


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
    """Write MarketEvent + optional RAG chunk. Returns event_id or '' on fail/disabled.

    Skip decisions are coalesced (#822): at most ``SKIP_EVENTS_PER_LOT`` rows per
    tenant, symbol, applied path and reason set. Non-skip decisions keep one row
    each. This only changes what is stored; the caller has already decided.
    """
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
    from core.tenant_context import current_tenant_context
    from logger import log

    # No context must not be stored as the default tenant. The cycle caller
    # sets a context; a bare call is a bug and writes nothing.
    ctx_now = current_tenant_context()
    if ctx_now is None:
        log(
            "DCA decision event not written: no tenant context",
            "ERROR",
        )
        return ""
    # Tenant is part of the id so two books in the same second do not overwrite.
    tid = ctx_now.tenant_id
    ts = _utc_now_iso()
    raw_id = (
        f"{tid}|{sym}|{ts}|{applied}|{result.skip}|{result.size_mult}|"
        f"{','.join(result.reason_codes)}"
    )
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

    want_rag = bool(index_rag and cfg.get("index_rag", True))
    try:
        from intelligence.memory.models import MarketEvent
        from intelligence.memory.store import MemoryStore, memory_enabled

        if not memory_enabled():
            return ""
        ms = store if store is not None else MemoryStore()
        if result.skip:
            try:
                event_id, created = _persist_coalesced_skip(
                    ms=ms,
                    tid=tid,
                    sym=sym,
                    ts=ts,
                    text=text,
                    meta=meta,
                    applied=applied,
                    result=result,
                )
            except Exception as e:
                # Never fall back to a per-timestamp row: that is the flood.
                log(f"DCA skip event coalesce failed (not written): {e}", "WARNING")
                return ""
            # RAG only when a slot is new; the cycle indexer refreshes it in place.
            if event_id and created and want_rag:
                _index_rag(text, sym, event_id)
            return event_id
        ev = MarketEvent(
            event_id=event_id,
            timestamp=ts,
            event_type="dca_decision",
            symbols=[sym],
            impact_score=0.0 if not result.skip else -0.1,
            description=text[:2000],
            source="dca_policy",
            metadata=meta,
            tenant_id=tid,
        )
        if not ms.upsert_event(ev):
            return ""
    except Exception:
        return ""

    if want_rag:
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
