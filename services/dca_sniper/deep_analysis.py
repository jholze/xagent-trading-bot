"""Deep analysis pass for DCA sniper — Memory/RAG/facts + policy before size.

Reuses #79 stack:
  build_dca_context → analyze_candidate → evaluate_dca_policy → size

Fail-open on I/O; policy skip beats size. Never calls Grok.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from logger import log
from services.dca_sniper.checklist import analyze_candidate


@dataclass
class DeepAnalysisResult:
    score: float = 0.0
    hard_fail: list[str] = field(default_factory=list)
    checklist: dict[str, Any] = field(default_factory=dict)
    usdt: float = 0.0
    size_reason: str = ""
    policy_skip: bool = False
    policy_mult: float = 1.0
    policy_reasons: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    deep: bool = True
    enriched_row: dict[str, Any] = field(default_factory=dict)


def enrich_candidate_from_context(
    cand: dict[str, Any],
    ctx: Any,
) -> dict[str, Any]:
    """Map DcaContext fields onto candidate snapshot for checklist layers."""
    out = dict(cand)
    entry_bias = str(getattr(ctx, "entry_bias", None) or out.get("entry_bias") or "neutral")
    out["entry_bias"] = entry_bias
    hard_neg = bool(getattr(ctx, "fact_hard_negative", False))
    unlock = bool(getattr(ctx, "fact_unlock", False))
    out["hard_negative"] = hard_neg
    out["unlock_risk"] = hard_neg or unlock
    # Social: prefer explicit cand flag; context has no dedicated social yet
    out["social_block"] = bool(out.get("social_block") or out.get("block_buys"))
    if bool(getattr(ctx, "block_buys", False)):
        out["social_block"] = True
        out["block_buys"] = True
    out["rag_hit_count"] = int(getattr(ctx, "rag_hit_count", 0) or 0)
    out["dca_lesson_count"] = int(getattr(ctx, "dca_lesson_count", 0) or 0)
    out["dca_lesson_summary"] = str(getattr(ctx, "dca_lesson_summary", "") or "")[:120]
    out["cash_mode"] = str(getattr(ctx, "cash_mode", "") or "")
    out["fusion_size_mult"] = float(getattr(ctx, "fusion_size_mult", 1.0) or 1.0)
    out["size_bias"] = float(getattr(ctx, "size_bias", 1.0) or 1.0)
    out["fact_unlock"] = unlock
    out["fact_hard_negative"] = hard_neg
    out["fact_summary"] = str(getattr(ctx, "fact_summary", "") or "")[:160]
    out["fact_event_count"] = int(getattr(ctx, "fact_event_count", 0) or 0)
    try:
        out["context"] = ctx.to_dict() if hasattr(ctx, "to_dict") else {}
    except Exception:
        out["context"] = {}
    return out


def _build_context(
    row: dict[str, Any],
    cash: dict[str, Any],
    *,
    score_seed: int,
    include_rag: bool,
    config_raw: dict | None,
) -> Any:
    from strategies.dca_context import build_dca_context

    symbol = str(row.get("symbol") or "")
    pos = {
        "symbol": symbol,
        "amount": row.get("amount"),
        "average_entry": row.get("average_entry"),
        "dca_rounds": row.get("dca_rounds"),
        "recovery_hold": row.get("recovery_hold"),
        "sniper_focus": row.get("sniper_focus"),
    }
    # Prefer bot cash snapshot for spendable when context risk path fails
    ctx = build_dca_context(
        symbol=symbol,
        position=pos,
        score=int(score_seed),
        max_score=10,
        loss_pct=float(row.get("loss_pct") or 0),
        config_raw=config_raw,
        include_rag=include_rag,
    )
    if ctx.spendable_dca is None and cash.get("spendable_dca") is not None:
        try:
            ctx.spendable_dca = float(cash.get("spendable_dca"))
        except (TypeError, ValueError):
            pass
    if not ctx.cash_mode and cash.get("cash_mode"):
        ctx.cash_mode = str(cash.get("cash_mode") or "")
    # Funding extreme from candidate snapshot
    try:
        fr = row.get("funding_rate_pct")
        if fr is not None and abs(float(fr)) > 0.05:
            ctx.extreme_funding = True
    except (TypeError, ValueError):
        pass
    return ctx


def deep_analyze_candidate(
    row: dict[str, Any],
    cash: dict[str, Any],
    cfg: dict[str, Any],
    *,
    config_raw: dict | None = None,
    size_fn=None,
) -> DeepAnalysisResult:
    """Full deep pass. size_fn defaults to engine._size_for_row (injectable for tests)."""
    if size_fn is None:
        from services.dca_sniper.engine import _size_for_row as size_fn
    include_rag = bool(cfg.get("deep_include_rag", True))
    apply_policy = bool(cfg.get("deep_apply_policy", True))
    row0 = dict(row)
    row0.setdefault("sniper_cfg", cfg)

    # Seed score from shallow technical pass (no context yet)
    seed = analyze_candidate(row0, cash)
    seed_score = int(round(float(seed.get("score") or 0)))

    try:
        ctx = _build_context(
            row0,
            cash,
            score_seed=max(seed_score, 1),
            include_rag=include_rag,
            config_raw=config_raw,
        )
    except Exception as e:
        log(f"dca_sniper deep context fail {row0.get('symbol')}: {e}", "DEBUG")
        # fail-open: shallow analysis only
        usdt, reason = size_fn(row0, seed, cash, cfg)
        hard = list(seed.get("hard_fail") or [])
        if usdt <= 0 and reason:
            hard = hard + [reason]
        return DeepAnalysisResult(
            score=float(seed.get("score") or 0),
            hard_fail=hard,
            checklist={**(seed.get("checklist") or {}), "size_reason": reason, "deep_error": str(e)[:80]},
            usdt=float(usdt or 0),
            size_reason=str(reason or ""),
            deep=False,
            enriched_row=row0,
        )

    enriched = enrich_candidate_from_context(row0, ctx)
    enriched["sniper_cfg"] = cfg
    analysis = analyze_candidate(enriched, cash)
    # refresh ctx score for policy score_boost
    try:
        ctx.score = int(round(float(analysis.get("score") or 0)))
    except (TypeError, ValueError):
        pass

    from strategies.dca_policy import (
        apply_policy_to_usdt,
        dca_policy_config,
        evaluate_dca_policy,
        emit_dca_policy_audit,
    )

    # Resolve policy cfg: prefer config_raw.dca.policy, force enabled for sniper apply
    dca_sec = {}
    try:
        raw = config_raw
        if raw is None:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        dca_sec = dict((raw or {}).get("dca") or {}) if isinstance(raw, dict) else {}
    except Exception:
        dca_sec = {}
    pcfg = dca_policy_config(dca_sec)
    # Sniper deep path applies policy for real unless deep_policy_shadow
    shadow = bool(cfg.get("deep_policy_shadow", False)) or not apply_policy
    pcfg = {**pcfg, "enabled": True, "shadow": shadow}

    policy = evaluate_dca_policy(ctx, pcfg)
    policy_reasons = list(policy.reason_codes or [])
    hard = list(analysis.get("hard_fail") or [])
    usdt = 0.0
    size_reason = ""

    if apply_policy and policy.skip and not shadow:
        hard = hard + [f"policy_skip:{','.join(policy_reasons) or 'skip'}"]
        size_reason = "policy_skip"
        usdt = 0.0
    else:
        usdt, size_reason = size_fn(enriched, analysis, cash, cfg)
        if usdt > 0 and apply_policy and not shadow:
            usdt = apply_policy_to_usdt(
                usdt,
                policy,
                spendable_dca=float(cash.get("spendable_dca") or 0) or None,
                shadow=False,
            )
            usdt = round(float(usdt), 2)
            if usdt < float(cfg.get("min_meaningful_usdt") or 200):
                size_reason = "policy_size_too_small"
                usdt = 0.0
        if usdt <= 0 and size_reason and size_reason not in hard:
            hard = hard + [size_reason]

    checklist = {
        **(analysis.get("checklist") or {}),
        "size_reason": size_reason,
        "policy_mult": policy.size_mult,
        "policy_skip": bool(policy.skip),
        "policy_reasons": policy_reasons,
        "deep": True,
        "rag_hit_count": int(getattr(ctx, "rag_hit_count", 0) or 0),
        "dca_lesson_count": int(getattr(ctx, "dca_lesson_count", 0) or 0),
        "cash_mode": str(getattr(ctx, "cash_mode", "") or ""),
    }

    # Persist / audit (fail-open)
    try:
        emit_dca_policy_audit(
            symbol=str(enriched.get("symbol") or ""),
            result=policy,
            ctx=ctx,
            shadow=shadow,
            base_usdt=float(usdt or 0),
            final_usdt=float(usdt or 0),
            applied="dca_sniper_deep",
        )
    except Exception as e:
        log(f"dca_sniper deep audit fail: {e}", "DEBUG")

    return DeepAnalysisResult(
        score=float(analysis.get("score") or 0),
        hard_fail=hard,
        checklist=checklist,
        usdt=float(usdt or 0),
        size_reason=str(size_reason or ""),
        policy_skip=bool(policy.skip) and apply_policy and not shadow,
        policy_mult=float(policy.size_mult),
        policy_reasons=policy_reasons,
        context=enriched.get("context") or {},
        deep=True,
        enriched_row=enriched,
    )
