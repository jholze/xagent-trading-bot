"""Deep analysis pass for DCA sniper — Memory/RAG/facts + policy before size.

Reuses #79 stack:
  build_dca_context → multi-TF structure → analyze_candidate → evaluate_dca_policy → size
  + quality gate (no heavy on thin context)

Fail-open on I/O; policy skip beats size. Never calls Grok.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from logger import log
from services.dca_sniper.checklist import analyze_candidate
from services.dca_sniper.quality import apply_quality_to_size, context_quality
from services.dca_sniper.evidence import (
    apply_evidence_size_adjust,
    apply_evidence_to_candidate,
    gather_evidence,
)
from services.dca_sniper.santiment_enrich import (
    apply_santiment_size,
    apply_santiment_to_candidate,
    build_santiment_enrichment,
)


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
    quality: dict[str, Any] = field(default_factory=dict)
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


def _enrich_structure_multi_tf(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Fill reclaim/free_fall from multi-TF when missing or force multi."""
    out = dict(row)
    force = bool(cfg.get("deep_structure_multi_tf", True))
    need = force or out.get("reclaim_ok") is None or out.get("free_fall") is None
    if not need:
        return out
    try:
        from services.dca_sniper.structure import structure_flags_multi_tf

        tfs = cfg.get("deep_structure_timeframes") or ("15m", "1h", "4h")
        if isinstance(tfs, str):
            tfs = [x.strip() for x in tfs.split(",") if x.strip()]
        multi = structure_flags_multi_tf(str(out.get("symbol") or ""), tfs)
        # Prefer multi aggregate over unknown; keep existing True/False if multi None
        if multi.get("free_fall") is not None:
            out["free_fall"] = multi.get("free_fall")
        if multi.get("reclaim_ok") is not None:
            out["reclaim_ok"] = multi.get("reclaim_ok")
        if multi.get("structure_ok") is not None:
            out["structure_ok"] = multi.get("structure_ok")
        out["structure_by_tf"] = multi.get("structure_by_tf") or {}
        out["structure_source"] = "multi_tf"
    except Exception as e:
        log(f"dca_sniper multi-tf structure skip: {e}", "DEBUG")
        out.setdefault("structure_source", "snapshot")
    return out


def _enrich_social_soft(row: dict[str, Any]) -> dict[str, Any]:
    """Best-effort social/noise flags from coin facts / profile (fail-open).

    Santiment regime is applied fully via ``_enrich_santiment``; this only
    covers text heuristics when Santiment pack is disabled/unavailable.
    """
    out = dict(row)
    summary = str(out.get("fact_summary") or "").lower()
    if "social" in summary or "viral" in summary or "pump" in summary:
        out["social_noise"] = True
    return out


def _enrich_santiment(
    row: dict[str, Any],
    cfg: dict[str, Any],
    *,
    config_raw: dict | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Apply global + per-asset Santiment Pro pack. Fail-open."""
    if not bool(cfg.get("deep_santiment_enabled", True)):
        return row, None
    try:
        pack = build_santiment_enrichment(
            str(row.get("symbol") or ""),
            config_raw=config_raw,
            # Default OFF: global Redis regime is free; per-asset burns API quota.
            fetch_asset=bool(cfg.get("deep_santiment_asset_fetch", False)),
            asset_ttl_sec=float(cfg.get("deep_santiment_asset_ttl_sec") or 21600),
            lean=bool(cfg.get("deep_santiment_lean", True)),
            micro=bool(cfg.get("deep_santiment_micro", True)),
        )
        enriched = apply_santiment_to_candidate(row, pack)
        return enriched, pack
    except Exception as e:
        log(f"dca_sniper santiment enrich skip: {e}", "DEBUG")
        return row, None


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
    try:
        fr = row.get("funding_rate_pct")
        if fr is not None and abs(float(fr)) > 0.05:
            ctx.extreme_funding = True
    except (TypeError, ValueError):
        pass
    # Force fact flags for sniper (fail-open) even if global coin_facts toggle is off
    try:
        from intelligence.memory.coin_facts import (
            apply_fact_flags_to_context,
            summarize_facts_for_symbol,
        )
        flags = summarize_facts_for_symbol(symbol, config_raw=config_raw)
        apply_fact_flags_to_context(ctx, flags)
    except Exception:
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
    evidence_pack = None
    santiment_pack: dict[str, Any] | None = None

    # Multi-TF structure before scoring
    row0 = _enrich_structure_multi_tf(row0, cfg)
    row0 = _enrich_social_soft(row0)
    # Santiment Pro: global regime (Redis) + optional per-asset metrics
    row0, santiment_pack = _enrich_santiment(row0, cfg, config_raw=config_raw)

    # Seed score from technical pass
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
        usdt, reason = size_fn(row0, seed, cash, cfg)
        hard = list(seed.get("hard_fail") or [])
        q = context_quality(row0, None, min_signals=int(cfg.get("deep_min_context_signals") or 3))
        usdt, reason, extra = apply_quality_to_size(
            usdt=usdt, size_reason=reason, quality=q, cfg=cfg
        )
        hard = hard + extra
        if santiment_pack is not None:
            usdt, reason, s_extra = apply_santiment_size(
                usdt, reason, santiment_pack, cfg=cfg
            )
            hard = list(dict.fromkeys(hard + s_extra))
        if usdt <= 0 and reason:
            hard = list(dict.fromkeys(hard + [reason]))
        return DeepAnalysisResult(
            score=float(seed.get("score") or 0),
            hard_fail=hard,
            checklist={
                **(seed.get("checklist") or {}),
                "size_reason": reason,
                "deep_error": str(e)[:80],
                "quality": q,
                "santiment": {
                    "regime": (santiment_pack or {}).get("regime"),
                    "combined_size_mult": (santiment_pack or {}).get("combined_size_mult"),
                    "social_block": (santiment_pack or {}).get("social_block"),
                }
                if santiment_pack
                else None,
            },
            usdt=float(usdt or 0),
            size_reason=str(reason or ""),
            quality=q,
            deep=False,
            enriched_row=row0,
        )

    enriched = enrich_candidate_from_context(row0, ctx)
    enriched["sniper_cfg"] = cfg
    # keep structure fields from row0
    for k in ("reclaim_ok", "free_fall", "structure_ok", "structure_by_tf", "structure_source"):
        if k in row0:
            enriched[k] = row0[k]
    for k in (
        "social_noise",
        "social_caution",
        "santiment_regime",
        "santiment",
        "santiment_fresh",
        "santiment_exchange_distribution",
        "block_buys",
        "social_block",
    ):
        if k in row0:
            enriched[k] = row0[k]
    if santiment_pack is not None:
        enriched = apply_santiment_to_candidate(enriched, santiment_pack)

    # News/facts/path/wallet evidence (memory-first; wallet adapter optional)
    evidence_pack = None
    try:
        if bool(cfg.get("deep_gather_evidence", True)):
            evidence_pack = gather_evidence(
                str(enriched.get("symbol") or ""),
                config_raw=config_raw,
                lookback_hours=float(cfg.get("deep_news_lookback_hours") or 72),
                wallet_provider=cfg.get("_wallet_provider"),  # tests inject
            )
            enriched = apply_evidence_to_candidate(enriched, evidence_pack)
            # Force fact flags onto ctx for policy when evidence found hard news
            if evidence_pack.hard_news:
                if any(
                    n.event_type in ("hack", "exploit", "sec_alert", "delisting")
                    for n in evidence_pack.news
                ):
                    ctx.fact_hard_negative = True
                if any(
                    n.event_type in ("unlock", "supply_unlock", "supply_overhang")
                    for n in evidence_pack.news
                ):
                    ctx.fact_unlock = True
            if evidence_pack.news and not getattr(ctx, "fact_event_count", 0):
                ctx.fact_event_count = len(evidence_pack.news)
                if not getattr(ctx, "fact_summary", ""):
                    ctx.fact_summary = evidence_pack.news[0].description[:160]
            # re-sync cand flags from ctx after forced facts
            enriched = enrich_candidate_from_context(enriched, ctx)
            for k in ("reclaim_ok", "free_fall", "structure_ok", "structure_by_tf", "evidence", "news_brief", "facts_fresh", "hard_news", "path_stats", "wallet"):
                if k in row0 or k in enriched:
                    pass
            # preserve evidence after re-enrich
            if evidence_pack is not None:
                enriched = apply_evidence_to_candidate(enriched, evidence_pack)
    except Exception as e:
        log(f"dca_sniper evidence gather skip: {e}", "DEBUG")

    analysis = analyze_candidate(enriched, cash)
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

    dca_sec: dict[str, Any] = {}
    try:
        raw = config_raw
        if raw is None:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        dca_sec = dict((raw or {}).get("dca") or {}) if isinstance(raw, dict) else {}
    except Exception:
        dca_sec = {}
    pcfg = dca_policy_config(dca_sec)
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

    # Quality gate: no HEAVY on thin memory/structure context
    q = context_quality(
        enriched,
        enriched.get("context") if isinstance(enriched.get("context"), dict) else None,
        min_signals=int(cfg.get("deep_min_context_signals") or 3),
    )
    usdt, size_reason, q_extra = apply_quality_to_size(
        usdt=usdt, size_reason=size_reason, quality=q, cfg=cfg
    )
    hard = list(dict.fromkeys(hard + q_extra))
    if evidence_pack is not None:
        usdt, size_reason, e_extra = apply_evidence_size_adjust(
            usdt, size_reason, evidence_pack, cfg=cfg
        )
        hard = list(dict.fromkeys(hard + e_extra))
    if santiment_pack is not None:
        usdt, size_reason, s_extra = apply_santiment_size(
            usdt, size_reason, santiment_pack, cfg=cfg
        )
        hard = list(dict.fromkeys(hard + s_extra))
    if usdt <= 0 and size_reason and size_reason not in hard:
        hard = hard + [size_reason]

    asset_score = {}
    if santiment_pack and isinstance(santiment_pack.get("asset"), dict):
        asset_score = (santiment_pack["asset"].get("score") or {}) if isinstance(
            santiment_pack["asset"].get("score"), dict
        ) else {}

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
        "quality": q,
        "evidence": (evidence_pack.to_dict() if evidence_pack is not None else enriched.get("evidence")),
        "news_brief": enriched.get("news_brief"),
        "facts_fresh": enriched.get("facts_fresh"),
        "hard_news": enriched.get("hard_news"),
        "structure_source": enriched.get("structure_source"),
        "structure_by_tf": {
            k: {kk: vv for kk, vv in (v or {}).items() if kk != "bars"}
            for k, v in (enriched.get("structure_by_tf") or {}).items()
        }
        if isinstance(enriched.get("structure_by_tf"), dict)
        else None,
        "santiment": {
            "regime": (santiment_pack or {}).get("regime"),
            "snapshot_fresh": (santiment_pack or {}).get("snapshot_fresh"),
            "combined_size_mult": (santiment_pack or {}).get("combined_size_mult"),
            "global_size_mult": (santiment_pack or {}).get("global_size_mult"),
            "asset_size_mult": (santiment_pack or {}).get("asset_size_mult"),
            "social_block": (santiment_pack or {}).get("social_block"),
            "social_caution": (santiment_pack or {}).get("social_caution"),
            "rationale": (santiment_pack or {}).get("rationale"),
            "asset_available": bool(
                ((santiment_pack or {}).get("asset") or {}).get("available")
            ),
            "asset_hints": list(asset_score.get("hints") or []),
            "scores": (santiment_pack or {}).get("scores"),
        }
        if santiment_pack
        else None,
    }

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

    # Structured operator log line for soak visibility
    try:
        flags = q.get("flags") or {}
        log(
            f"dca_sniper deep {enriched.get('symbol')} score={analysis.get('score')} "
            f"q={q.get('score')}/{q.get('max_score')} thin={q.get('thin')} "
            f"rag={flags.get('has_rag')} facts={flags.get('has_facts')} "
            f"lessons={flags.get('has_lessons')} struct={flags.get('has_structure')} "
            f"san={flags.get('has_santiment')} "
            f"regime={(santiment_pack or {}).get('regime')} "
            f"usdt={usdt} reason={size_reason} policy={policy_reasons}",
            "INFO",
        )
    except Exception:
        pass

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
        quality=q,
        deep=True,
        enriched_row=enriched,
    )
