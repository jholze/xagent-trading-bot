"""Persist DCA policy decisions to Trading Memory + optional RAG (#98 D4).

LEDGER SAFETY: only memory_* / RAG chunks — never orders/positions/trade_history.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from strategies.dca_policy import DcaContext, DcaPolicyResult

# #822: skip decisions for one lot (tenant + symbol; positions carry no lot id)
# take at most this many rows in memory_market_events: row "first" keeps the
# first skip of the family, row "latest" is incremented atomically with count,
# last_ts, the latest reason codes / applied path and a reason_counts map.
# Kay decided N = 2. This is a code default, not a config knob.
SKIP_EVENTS_PER_LOT = 2
# Family restarts when its newest row is older than memory.coin_facts.lookback_hours
# (read from config; code default 72 h in intelligence/memory/coin_facts.py).
_DEFAULT_RESET_HOURS = 72.0


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


def skip_coalesce_key(tenant_id: str, symbol: str) -> str:
    """Stable key for one lot's skip family. Tenant is part of it (#721)."""
    raw = f"{tenant_id}|{symbol}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def skip_family_reset_hours() -> float:
    """``memory.coin_facts.lookback_hours`` (existing setting), 72 h fallback."""
    try:
        from intelligence.memory.coin_facts import coin_facts_config

        hours = float(coin_facts_config().get("lookback_hours") or _DEFAULT_RESET_HOURS)
        return hours if hours > 0 else _DEFAULT_RESET_HOURS
    except Exception:
        return _DEFAULT_RESET_HOURS


def _persist_coalesced_skip(
    *,
    ms: Any,
    tid: str,
    sym: str,
    ts: str,
    text: str,
    meta: dict,
) -> str:
    """Count a skip into its lot family via the store's atomic write.

    Raises on store errors; the caller turns that into a no-write.
    """
    from intelligence.memory.models import MarketEvent, memory_timestamp_unix

    key = skip_coalesce_key(tid, sym)
    template = MarketEvent(
        event_id=f"dca_skip_{key}",
        timestamp=ts,
        event_type="dca_decision",
        symbols=[sym],
        impact_score=-0.1,
        description=text[:1800],
        source="dca_policy",
        metadata={**meta, "coalesce_key": key, "coalesce_limit": SKIP_EVENTS_PER_LOT},
        tenant_id=tid,
    )
    reset_before = memory_timestamp_unix(ts) - skip_family_reset_hours() * 3600.0
    event_id, _created = ms.record_coalesced_skip(
        template,
        first_id=f"dca_skip_{key}_first",
        latest_id=f"dca_skip_{key}_latest",
        reset_before_unix=reset_before,
    )
    return event_id


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
    tenant and symbol (lot). Their RAG chunk is written by the cycle indexer
    (``index_store_into_rag``), not here, so each row has one live chunk.
    Non-skip decisions keep one row each. This only changes what is stored;
    the caller has already decided.
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

    try:
        from intelligence.memory.models import MarketEvent
        from intelligence.memory.store import MemoryStore, memory_enabled

        if not memory_enabled():
            return ""
        ms = store if store is not None else MemoryStore()
        if result.skip:
            try:
                return _persist_coalesced_skip(
                    ms=ms, tid=tid, sym=sym, ts=ts, text=text, meta=meta
                )
            except Exception as e:
                # Never fall back to a per-timestamp row: that is the flood.
                log(f"DCA skip event coalesce failed (not written): {e}", "WARNING")
                return ""
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
