"""In-process sniper tick (bot-local, no HTTP) — staging sharp without sidecar."""

from __future__ import annotations

import time
from typing import Any

from logger import log
from services.dca_sniper.config import dca_sniper_config, dca_sniper_enabled
from services.dca_sniper.engine import _as_candidate_views, cash_plan
from services.dca_sniper.pure import select_focus_batch
from services.dca_sniper import state as sniper_state

_last_tick = 0.0


def maybe_tick_dca_sniper(*, force: bool = False) -> dict[str, Any] | None:
    """Run one sniper cycle against local bot APIs (no network).

    Uses bot_http pure builders + execute_sniper_dca directly.
    """
    global _last_tick
    cfg = dca_sniper_config()
    if not dca_sniper_enabled() and not force:
        return None
    if cfg.get("notify_only"):
        # user asked for sharp staging — still skip if someone re-enabled notify_only
        log("dca_sniper in-process: notify_only=true → no execute", "WARNING")
        return {"skipped": "notify_only"}

    interval = float(cfg.get("poll_interval_sec") or 180)
    now = time.time()
    if not force and (now - _last_tick) < interval:
        return None
    _last_tick = now

    try:
        from services.dca_sniper.bot_http import (
            _build_candidates,
            _snapshot_cash,
            execute_fund_sell,
            execute_sniper_dca,
        )
    except Exception as e:
        log(f"dca_sniper in-process import fail: {e}", "WARNING")
        return {"error": str(e)}

    cash = _snapshot_cash()
    winners = []
    # winners optional for fund path — rebuild light from cash helper if needed
    rows = _build_candidates()
    views = _as_candidate_views(rows, cash, cfg)
    open_focus = sum(1 for v in views if v.recovery_hold or v.sniper_focus)
    batch = select_focus_batch(
        views,
        spendable_dca=float(cash.get("spendable_dca") or 0),
        max_focus_slots=int(cfg["max_focus_slots"]),
        min_cash_after_focus=float(cfg["min_cash_after_focus"]),
        open_focus_count=open_focus,
        heavy_min_score=max(3.0, float(cfg["heavy_min_score"]) - 2.0),
    )
    audit: dict[str, Any] = {
        "ts": now,
        "mode": "in_process_sharp",
        "n_candidates": len(rows),
        "n_focus": len(batch),
        "actions": [],
    }
    if not batch:
        sniper_state.add_decision({"action": "WAIT", "reason": "no_focus"})
        audit["actions"].append({"action": "WAIT"})
        return audit

    spendable_left = float(cash.get("spendable_dca") or 0)
    free_above = max(
        0.0,
        float(cash.get("balance") or 0) - float(cash.get("cash_floor_abs") or 0),
    )
    focus_set = []
    for v in batch:
        size_reason = str((v.checklist or {}).get("size_reason") or "DCA_SMALL")
        usdt = float(v.usdt_suggest or 0)
        plan = cash_plan(
            need_usdt=usdt,
            spendable_dca=spendable_left,
            free_cash_above_floor=free_above,
            soft_claim_enabled=bool(cfg.get("soft_claim_enabled")),
            soft_claim_max_usdt=float(cash.get("equity") or 10000)
            * (float(cfg.get("soft_claim_max_pct_equity") or 3) / 100.0),
        )
        if plan["action"] == "NEED_CASH" and cfg.get("fund_from_winner_enabled"):
            # best-effort: skip fund here if no winners list; size down
            usdt = min(usdt, spendable_left)
            plan = cash_plan(
                need_usdt=usdt,
                spendable_dca=spendable_left,
                free_cash_above_floor=free_above,
                soft_claim_enabled=bool(cfg.get("soft_claim_enabled")),
                soft_claim_max_usdt=float(cash.get("equity") or 10000)
                * (float(cfg.get("soft_claim_max_pct_equity") or 3) / 100.0),
            )
        if plan["action"] != "DCA_HEAVY" and usdt > spendable_left:
            audit["actions"].append(
                {"action": plan["action"], "symbol": v.symbol, "plan": plan}
            )
            continue
        if usdt < float(cfg["min_meaningful_usdt"]):
            continue
        body, status = execute_sniper_dca(
            {
                "symbol": v.symbol,
                "timeframe": v.timeframe,
                "usdt": usdt,
                "price": v.mark,
                "reason_code": size_reason,
                "set_recovery_hold": True,
                "heavy": size_reason == "DCA_HEAVY",
                "score": v.score,
                "checklist": v.checklist,
            }
        )
        dec = {
            "action": size_reason,
            "symbol": v.symbol,
            "usdt": usdt,
            "executed": bool(body.get("executed")),
            "message": body.get("message"),
            "status": status,
            "sharp": True,
        }
        sniper_state.add_decision(dec)
        audit["actions"].append(dec)
        log(
            f"dca_sniper SHARP {v.symbol} {size_reason} usdt={usdt:.0f} "
            f"exec={body.get('executed')} msg={str(body.get('message') or '')[:80]}",
            "INFO",
        )
        if body.get("executed"):
            spendable_left = max(0.0, spendable_left - usdt)
            focus_set.append(
                {
                    "symbol": v.symbol,
                    "timeframe": v.timeframe,
                    "usdt": usdt,
                    "since": now,
                }
            )
    if focus_set:
        sniper_state.set_focus(focus_set)
    return audit
