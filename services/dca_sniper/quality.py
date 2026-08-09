"""Deep-analysis quality: how rich is the context before we trust heavy DCA?"""

from __future__ import annotations

from typing import Any


def context_signal_flags(cand: dict[str, Any] | None, ctx: dict[str, Any] | None = None) -> dict[str, bool]:
    """Boolean evidence channels used in the deep pass."""
    c = cand or {}
    x = ctx if isinstance(ctx, dict) else (c.get("context") if isinstance(c.get("context"), dict) else {})
    x = x or {}

    entry_bias = str(c.get("entry_bias") or x.get("entry_bias") or "neutral").lower()
    try:
        size_bias = float(c.get("size_bias") if c.get("size_bias") is not None else x.get("size_bias") or 1.0)
    except (TypeError, ValueError):
        size_bias = 1.0
    try:
        rag = int(c.get("rag_hit_count") if c.get("rag_hit_count") is not None else x.get("rag_hit_count") or 0)
    except (TypeError, ValueError):
        rag = 0
    try:
        lessons = int(
            c.get("dca_lesson_count")
            if c.get("dca_lesson_count") is not None
            else x.get("dca_lesson_count")
            or 0
        )
    except (TypeError, ValueError):
        lessons = 0
    try:
        facts_n = int(
            c.get("fact_event_count")
            if c.get("fact_event_count") is not None
            else x.get("fact_event_count")
            or 0
        )
    except (TypeError, ValueError):
        facts_n = 0
    fact_summary = str(c.get("fact_summary") or x.get("fact_summary") or "").strip()

    reclaim = c.get("reclaim_ok")
    free_fall = c.get("free_fall")
    structure_known = reclaim is not None or free_fall is not None
    # multi-tf map optional
    by_tf = c.get("structure_by_tf") if isinstance(c.get("structure_by_tf"), dict) else {}
    if by_tf:
        structure_known = True

    return {
        "has_profile": entry_bias not in ("", "neutral") or abs(size_bias - 1.0) > 0.05,
        "has_rag": rag > 0,
        "has_lessons": lessons > 0,
        "has_facts": facts_n > 0 or bool(fact_summary),
        "has_structure": structure_known,
        "has_ta": c.get("rsi") is not None or c.get("atr_pct") is not None,
        "has_funding": c.get("funding_rate_pct") is not None,
        "has_cash_mode": bool(str(c.get("cash_mode") or x.get("cash_mode") or "").strip()),
    }


def context_quality(
    cand: dict[str, Any] | None,
    ctx: dict[str, Any] | None = None,
    *,
    min_signals: int = 3,
) -> dict[str, Any]:
    """Score how trustworthy the deep context is (0..n channels)."""
    flags = context_signal_flags(cand, ctx)
    score = sum(1 for v in flags.values() if v)
    thin = score < int(min_signals)
    return {
        "flags": flags,
        "score": score,
        "max_score": len(flags),
        "min_signals": int(min_signals),
        "thin": thin,
        "rich": not thin and score >= int(min_signals) + 1,
    }


def apply_quality_to_size(
    *,
    usdt: float,
    size_reason: str,
    quality: dict[str, Any],
    cfg: dict[str, Any] | None = None,
) -> tuple[float, str, list[str]]:
    """Demote/block heavy when context is thin; keep small if allowed.

    Returns (usdt, size_reason, extra_hard_fails).
    """
    cfg = cfg or {}
    extra: list[str] = []
    usdt = float(usdt or 0)
    reason = str(size_reason or "")
    thin = bool((quality or {}).get("thin"))
    require_for_heavy = bool(cfg.get("deep_require_context_for_heavy", True))
    allow_small_if_thin = bool(cfg.get("deep_allow_small_if_thin", True))
    block_all_if_empty = bool(cfg.get("deep_block_if_zero_signals", False))
    qscore = int((quality or {}).get("score") or 0)

    if block_all_if_empty and qscore <= 0 and usdt > 0:
        return 0.0, "context_empty", ["context_empty"]

    if not thin or not require_for_heavy:
        return usdt, reason, extra

    # Thin context: never HEAVY
    if reason == "DCA_HEAVY" or "HEAVY" in reason.upper():
        if allow_small_if_thin:
            small = float(cfg.get("small_dca_usdt") or 500)
            small = min(small, usdt) if usdt > 0 else small
            min_u = float(cfg.get("min_meaningful_usdt") or 200)
            if small >= min_u:
                return round(small, 2), "DCA_SMALL_context_thin", extra
            return 0.0, "context_thin_no_size", ["context_thin"]
        return 0.0, "context_thin_block_heavy", ["context_thin"]

    # Small already: optional keep
    if allow_small_if_thin:
        return usdt, reason if reason else "DCA_SMALL", extra
    return 0.0, "context_thin_block", ["context_thin"]
