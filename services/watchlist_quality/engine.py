"""Watchlist Quality Engine — shadow scoring (W2).

Computes scores + logs + persists. Does **not** change effective watchlist membership
or scan order when mode is ``shadow`` or ``off``.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from logger import log
from services.watchlist_quality.config import wqe_mode, wqe_shadow_active
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


def run_shadow_score(
    coins: list[dict[str, Any]] | None = None,
    *,
    config: dict | None = None,
    persist: bool = True,
    venue_by_symbol: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Score candidates, log summary, optionally persist. Never mutates watchlist.

    Returns summary dict for tests/ops.
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
    qs = sorted(s.quality_score for s in scored)
    p50 = _percentile(qs, 0.5)
    p90 = _percentile(qs, 0.9)
    mem_sb = sum(1 for s in scored if "memory_soft_block" in s.flags)
    mem_pref = sum(1 for s in scored if "memory_prefer" in s.flags)
    n_t1 = sum(1 for s in scored if s.tier_hint == "T1")
    n_t2 = sum(1 for s in scored if s.tier_hint == "T2")
    n_t3 = sum(1 for s in scored if s.tier_hint == "T3")
    vol_low = sum(1 for s in scored if "vol_low" in s.flags)

    summary = {
        "mode": mode,
        "n_in": len(coins or []),
        "scored": len(scored),
        "score_p50": round(p50, 4) if p50 is not None else None,
        "score_p90": round(p90, 4) if p90 is not None else None,
        "score_mean": round(statistics.mean(qs), 4) if qs else None,
        "n_T1_hint": n_t1,
        "n_T2_hint": n_t2,
        "n_T3_hint": n_t3,
        "memory_soft_block": mem_sb,
        "memory_prefer": mem_pref,
        "vol_low": vol_low,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "behavior_change": False,  # W2 invariant
    }

    log(
        "watchlist_quality_sync "
        f"mode={mode} n_in={summary['n_in']} scored={summary['scored']} "
        f"p50={summary['score_p50']} p90={summary['score_p90']} "
        f"T1={n_t1} T2={n_t2} T3={n_t3} "
        f"mem_sb={mem_sb} mem_pref={mem_pref} vol_low={vol_low} "
        f"behavior_change=false",
        "INFO",
    )

    if persist:
        payload = {
            "updated_at": summary["updated_at"],
            "mode": mode,
            "summary": {k: v for k, v in summary.items() if k != "updated_at"},
            "coins": [s.to_dict() for s in scored],
        }
        ok = save_quality_scores(payload)
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
