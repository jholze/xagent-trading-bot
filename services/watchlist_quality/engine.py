"""Watchlist Quality Engine — shadow scoring + AI fuse + soft entry (Epic #124).

Shadow (mode=shadow): scores + optional AI critic; never mutates membership.
Soft (mode=soft): pure transform available via soft.apply_soft_watchlist / soft_scan_order;
run_shadow_score still reports behavior_change=false for the *score* artifact only.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from logger import log
from services.watchlist_quality.config import (
    ai_config,
    ai_shadow_enabled,
    vol_floor_t1_usd,
    wqe_mode,
    wqe_shadow_active,
)
from services.watchlist_quality.scoring import CoinQualityScore, score_coin_from_watchlist_row
from services.watchlist_quality.store import save_quality_scores


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _regime_hints(config: dict | None) -> tuple[float | None, str | None]:
    """Best-effort fusion/sensor hints — fail-open (None, None)."""
    try:
        from services.market_policy_fusion import get_fused_policy

        pol = get_fused_policy(config or {})
        if not pol:
            return None, None
        size_mult = getattr(pol, "size_mult", None)
        if size_mult is None and isinstance(pol, dict):
            size_mult = pol.get("size_mult")
        sensor = getattr(pol, "sensor_policy", None)
        if sensor is None and isinstance(pol, dict):
            sensor = pol.get("sensor_policy") or pol.get("entry_policy")
        return (
            float(size_mult) if size_mult is not None else None,
            str(sensor) if sensor else None,
        )
    except Exception:
        return None, None


def score_watchlist(
    coins: list[dict[str, Any]],
    *,
    config: dict | None = None,
    venue_by_symbol: dict[str, dict] | None = None,
    ledger_scope: str | None = None,
    tenant_id: str = "default",
) -> list[CoinQualityScore]:
    """Score each coin; pure given inputs (memory may hit cache)."""
    venue_by_symbol = venue_by_symbol or {}
    size_mult, sensor_pol = _regime_hints(config)
    out: list[CoinQualityScore] = []
    for coin in coins or []:
        if not isinstance(coin, dict):
            continue
        sym = str(coin.get("symbol") or "").strip()
        if not sym:
            continue
        try:
            sc = score_coin_from_watchlist_row(
                coin,
                config=config,
                venue=venue_by_symbol.get(sym),
                regime_size_mult=size_mult,
                sensor_policy=sensor_pol,
                ledger_scope=ledger_scope,
                tenant_id=tenant_id,
            )
            out.append(sc)
        except Exception as e:
            log(f"wqe score_coin failed {sym}: {e}", "DEBUG")
    return out


def _should_run_ai_for(sc: CoinQualityScore, ai: dict[str, Any]) -> bool:
    min_det = float(ai.get("min_det_score_to_call") or 0.0)
    if sc.quality_score < min_det:
        return False
    only = ai.get("only_tiers_hint") or []
    if only and sc.tier_hint not in only:
        # still allow T1 if prefer / soft_block flags for safety review
        if "memory_soft_block" not in sc.flags and "memory_prefer" not in sc.flags:
            return False
    return True


def enrich_with_ai(
    scored: list[CoinQualityScore],
    *,
    config: dict | None = None,
    llm_json_fn=None,
    rag_pack_fn=None,
) -> list[CoinQualityScore]:
    """Attach RAG + LLM critic + quality_shadow_ai. Fail-open per coin."""
    if not ai_shadow_enabled(config):
        for sc in scored:
            sc.quality_shadow_ai = sc.quality_score
            sc.ai = {"source": "disabled"}
        return scored

    ai = ai_config(config)
    max_n = int(ai.get("max_coins_per_cycle") or 12)
    max_adjust = float(ai.get("max_adjust") or 0.2)

    # Prefer lower-tier / borderline coins for critic budget
    candidates = sorted(
        scored,
        key=lambda s: (0 if s.tier_hint in ("T2", "T3") else 1, s.quality_score),
    )
    to_call = [s for s in candidates if _should_run_ai_for(s, ai)][:max_n]
    call_set = {s.symbol for s in to_call}

    from services.watchlist_quality.ai_critic import fuse_quality, run_ai_critic
    from services.watchlist_quality.rag_pack import build_rag_pack

    if rag_pack_fn is None:
        rag_pack_fn = build_rag_pack

    size_mult, sensor_pol = _regime_hints(config)
    regime = {"size_mult": size_mult, "sensor_policy": sensor_pol}

    for sc in scored:
        if sc.symbol not in call_set:
            sc.quality_shadow_ai = sc.quality_score
            sc.ai = {"source": "skipped_budget"}
            continue
        try:
            pack = rag_pack_fn(sc.symbol, config=config)
            critic = run_ai_critic(
                symbol=sc.symbol,
                quality_score=sc.quality_score,
                scores=sc.scores,
                tier_hint=sc.tier_hint,
                flags=sc.flags,
                memory=sc.memory,
                metrics=sc.metrics,
                regime=regime,
                rag_pack=pack,
                config=config,
                llm_json_fn=llm_json_fn,
            )
            sc.ai = critic.to_dict()
            sc.quality_shadow_ai = round(
                fuse_quality(sc.quality_score, critic, max_adjust=max_adjust), 4
            )
            if critic.stance in ("demote", "avoid_new"):
                sc.flags = list(sc.flags) + [f"ai_{critic.stance}"]
        except Exception as e:
            sc.quality_shadow_ai = sc.quality_score
            sc.ai = {"source": "error", "error": f"{type(e).__name__}"}
            log(f"wqe ai enrich failed {sc.symbol}: {e}", "DEBUG")
    return scored


def run_shadow_score(
    coins: list[dict[str, Any]] | None = None,
    *,
    config: dict | None = None,
    persist: bool = True,
    venue_by_symbol: dict[str, dict] | None = None,
    llm_json_fn=None,
    rag_pack_fn=None,
    open_symbols: set[str] | list[str] | None = None,
    tenant_id: str = "default",
) -> dict[str, Any]:
    """Score candidates, optional AI fuse, log + persist. Shadow: no membership change.

    Returns summary dict for tests/ops. Always sets behavior_change=false for score path.
    When mode=soft, also returns ``soft_scan`` list (transform only; does not write watchlist).
    """
    if config is None:
        try:
            from core.config import get_bot_config

            config = get_bot_config().raw
        except Exception:
            config = {}

    mode = wqe_mode(config)
    if mode == "off":
        return {"mode": "off", "n_in": 0, "scored": 0, "skipped": True}

    if not wqe_shadow_active(config) and mode not in ("soft", "enforce"):
        return {"mode": mode, "n_in": 0, "scored": 0, "skipped": True}

    if coins is None:
        try:
            from data_manager import load_effective_watchlist

            coins = load_effective_watchlist()
        except Exception as e:
            log(f"wqe shadow: load watchlist failed: {e}", "WARNING")
            return {"mode": mode, "error": str(e), "scored": 0}

    scored = score_watchlist(
        coins, config=config, venue_by_symbol=venue_by_symbol
    )
    scored = enrich_with_ai(
        scored, config=config, llm_json_fn=llm_json_fn, rag_pack_fn=rag_pack_fn
    )

    qs = sorted(s.quality_score for s in scored)
    qai = sorted(
        (s.quality_shadow_ai if s.quality_shadow_ai is not None else s.quality_score)
        for s in scored
    )
    p50 = _percentile(qs, 0.5)
    p90 = _percentile(qs, 0.9)
    mem_sb = sum(1 for s in scored if "memory_soft_block" in s.flags)
    mem_pref = sum(1 for s in scored if "memory_prefer" in s.flags)
    n_t1 = sum(1 for s in scored if s.tier_hint == "T1")
    n_t2 = sum(1 for s in scored if s.tier_hint == "T2")
    n_t3 = sum(1 for s in scored if s.tier_hint == "T3")
    vol_low = sum(1 for s in scored if "vol_low" in s.flags)
    ai_ok = sum(1 for s in scored if (s.ai or {}).get("source") == "ok")
    ai_err = sum(1 for s in scored if (s.ai or {}).get("source") == "error")

    summary: dict[str, Any] = {
        "mode": mode,
        "n_in": len(coins or []),
        "scored": len(scored),
        "score_p50": round(p50, 4) if p50 is not None else None,
        "score_p90": round(p90, 4) if p90 is not None else None,
        "score_mean": round(statistics.mean(qs), 4) if qs else None,
        "score_ai_p50": round(_percentile(qai, 0.5) or 0, 4) if qai else None,
        "n_T1_hint": n_t1,
        "n_T2_hint": n_t2,
        "n_T3_hint": n_t3,
        "memory_soft_block": mem_sb,
        "memory_prefer": mem_pref,
        "vol_low": vol_low,
        "ai_ok": ai_ok,
        "ai_error": ai_err,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "behavior_change": False,  # score artifact never rewrites membership
        "coins": [s.to_dict() for s in scored],
    }

    # Soft transform (preview only in summary — callers may apply)
    if mode in ("soft", "enforce"):
        try:
            from services.watchlist_quality.soft import apply_soft_watchlist

            coin_rows = []
            for s in scored:
                row = {
                    "symbol": s.symbol,
                    "quality_score": s.quality_score,
                    "quality_shadow_ai": s.quality_shadow_ai,
                    "quote_vol_24h": (s.metrics or {}).get("quote_vol_24h"),
                    "source": (s.metrics or {}).get("source"),
                    "tier_hint": s.tier_hint,
                }
                coin_rows.append(row)
            soft_list = apply_soft_watchlist(
                coin_rows,
                open_symbols=open_symbols,
                min_quote_vol_usd=vol_floor_t1_usd(config),
                use_ai_score=True,
            )
            summary["soft_scan"] = soft_list
            summary["soft_n"] = len(soft_list)
            summary["soft_vol_floor"] = vol_floor_t1_usd(config)
            # membership change is available as soft_scan but not auto-applied to data_manager
            summary["soft_preview"] = True
        except Exception as e:
            log(f"wqe soft preview failed: {e}", "DEBUG")

    log(
        "watchlist_quality_sync "
        f"mode={mode} n_in={summary['n_in']} scored={summary['scored']} "
        f"p50={summary['score_p50']} p90={summary['score_p90']} "
        f"ai_p50={summary.get('score_ai_p50')} ai_ok={ai_ok} ai_err={ai_err} "
        f"T1={n_t1} T2={n_t2} T3={n_t3} "
        f"mem_sb={mem_sb} mem_pref={mem_pref} vol_low={vol_low} "
        f"behavior_change=false",
        "INFO",
    )
    try:
        from services.watchlist_quality.event_log import log_sync_summary

        summary["tenant_id"] = tenant_id
        log_sync_summary(summary, config=config)
    except Exception as e:
        log(f"wqe event_log sync failed: {e}", "DEBUG")

    try:
        from services.watchlist_quality.metrics import note_ai, note_scored

        note_scored(len(scored))
        for _ in range(ai_ok):
            note_ai(True)
        for _ in range(ai_err):
            note_ai(False)
    except Exception:
        pass

    if persist:
        payload = {
            "updated_at": summary["updated_at"],
            "mode": mode,
            "tenant_id": tenant_id,
            "summary": {
                k: v
                for k, v in summary.items()
                if k not in ("updated_at", "coins", "soft_scan")
            },
            "coins": summary["coins"],
        }
        if "soft_scan" in summary:
            payload["soft_scan"] = summary["soft_scan"]
        ok = save_quality_scores(payload, tenant_id=tenant_id)
        if not ok:
            log("watchlist_quality_sync persist failed", "WARNING")
        summary["persisted"] = ok

    return summary


def maybe_run_shadow_after_watchlist_load(
    coins: list[dict[str, Any]] | None = None,
    *,
    config: dict | None = None,
) -> dict[str, Any] | None:
    """Fail-open entry for callers (trending sync / cycle). No membership change."""
    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config().raw
        if not wqe_shadow_active(config):
            return None
        return run_shadow_score(coins, config=config, persist=True)
    except Exception as e:
        log(f"wqe shadow skipped: {e}", "DEBUG")
        return None


def apply_soft_to_effective_candidates(
    coins: list[dict[str, Any]],
    *,
    config: dict | None = None,
    open_symbols: set[str] | list[str] | None = None,
    scored_summary: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Public soft path: when mode=soft|enforce, filter+sort; else return coins unchanged order-safe.

    Does not write watchlist files — caller decides whether to use result as scan set.
    """
    mode = wqe_mode(config)
    if mode not in ("soft", "enforce"):
        return list(coins or [])

    # Merge scores from summary if provided
    by_sym: dict[str, dict] = {}
    if scored_summary and scored_summary.get("coins"):
        for c in scored_summary["coins"]:
            if isinstance(c, dict) and c.get("symbol"):
                by_sym[str(c["symbol"])] = c

    merged: list[dict[str, Any]] = []
    for c in coins or []:
        if not isinstance(c, dict):
            continue
        sym = str(c.get("symbol") or "").strip()
        row = dict(c)
        if sym in by_sym:
            sc = by_sym[sym]
            row.setdefault("quality_score", sc.get("quality_score"))
            if sc.get("quality_shadow_ai") is not None:
                row["quality_shadow_ai"] = sc.get("quality_shadow_ai")
            if row.get("quote_vol_24h") is None:
                m = sc.get("metrics") or {}
                if m.get("quote_vol_24h") is not None:
                    row["quote_vol_24h"] = m.get("quote_vol_24h")
        merged.append(row)

    from services.watchlist_quality.soft import apply_soft_watchlist

    return apply_soft_watchlist(
        merged,
        open_symbols=open_symbols,
        min_quote_vol_usd=vol_floor_t1_usd(config),
        use_ai_score=True,
    )
