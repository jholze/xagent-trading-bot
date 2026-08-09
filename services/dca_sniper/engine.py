"""One sniper cycle: fetch → analyze → size → cash plan → execute (auto)."""

from __future__ import annotations

import time
import uuid
from typing import Any

from logger import log
from services.dca_sniper.checklist import analyze_candidate
from services.dca_sniper.client import DcaSniperBotClient
from services.dca_sniper.config import dca_sniper_config
from services.dca_sniper.pure import (
    CandidateView,
    cash_plan,
    compute_heavy_size,
    is_grid_excluded,
    profile_key,
    rank_priority,
    select_focus_batch,
)
from services.dca_sniper import state as sniper_state


def _size_for_row(
    row: dict[str, Any],
    analysis: dict[str, Any],
    cash: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[float, str]:
    """Return (usdt, reason_code) using best-path rules: reclaim + small-before-heavy."""
    from services.dca_sniper.policy import dd_band_ok, dd_pct_from_loss, reclaim_allows_dca

    loss = dd_pct_from_loss(float(row.get("loss_pct") or 0))
    ok_band, band_why = dd_band_ok(float(row.get("loss_pct") or 0), cfg)
    if not ok_band:
        return 0.0, band_why

    reclaim = row.get("reclaim_ok")
    free_fall = row.get("free_fall")
    ok_rc, rc_why = reclaim_allows_dca(
        reclaim_ok=reclaim if reclaim is None else bool(reclaim),
        free_fall=free_fall if free_fall is None else bool(free_fall),
        require_reclaim=bool(cfg.get("require_reclaim_for_dca", True)),
    )
    if not ok_rc:
        return 0.0, rc_why
    # unknown reclaim: allow small only if not free-fall; never heavy
    reclaim_yes = reclaim is True

    notional = float(row.get("notional") or 0)
    spendable = float(cash.get("spendable_dca") or 0)
    equity = float(cash.get("equity") or cash.get("balance") or 0)
    prof = profile_key(
        str(row.get("strategy_profile") or ""),
        str(row.get("strategy_class") or ""),
        str(row.get("symbol") or ""),
    )
    score = float(analysis.get("score") or 0)
    heavy_ok = (
        reclaim_yes
        and score >= float(cfg.get("heavy_min_score") or 6.5)
        and loss <= float(cfg.get("max_dd_pct_for_heavy") or 55)
        and not bool(cfg.get("prefer_small_before_heavy") is False and False)
    )
    if cfg.get("heavy_only_on_reclaim", True) and not reclaim_yes:
        heavy_ok = False
    if cfg.get("prefer_small_before_heavy", True):
        # small by default; heavy only if score clearly strong
        heavy_ok = heavy_ok and score >= float(cfg.get("heavy_min_score") or 6.5) + 0.5

    if heavy_ok:
        usdt = compute_heavy_size(
            rest_notional=notional,
            score=score,
            heavy_min_score=float(cfg["heavy_min_score"]),
            profile=prof,
            profile_f=dict(cfg.get("profile_f") or {}),
            spendable_dca=spendable,
            max_single_add_usdt=float(cfg["max_single_add_usdt"]),
            max_bag_pct_equity=float(cfg["max_bag_pct_equity"]),
            equity=equity if equity > 0 else max(notional * 5, 10000),
            bag_now=notional,
            min_meaningful_usdt=float(cfg["min_meaningful_usdt"]),
        )
        return usdt, "DCA_HEAVY"

    # capital-light path (A4 style)
    if reclaim_yes or reclaim is None:
        small = float(cfg.get("small_dca_usdt") or 500)
        small = min(small, spendable, float(cfg["max_single_add_usdt"]))
        if small < float(cfg["min_meaningful_usdt"]):
            return 0.0, "size_too_small"
        # bag-relative soft boost if deep loss
        if loss >= 25:
            small = min(small * 1.25, float(cfg["max_single_add_usdt"]), spendable)
        return round(small, 2), "DCA_SMALL"
    return 0.0, "no_reclaim"


def _as_candidate_views(
    rows: list[dict[str, Any]],
    cash: dict[str, Any],
    cfg: dict[str, Any],
) -> list[CandidateView]:
    views: list[CandidateView] = []
    for row in rows:
        if is_grid_excluded(
            strategy_profile=str(row.get("strategy_profile") or ""),
            strategy_class=str(row.get("strategy_class") or ""),
            has_grid_plan=bool(row.get("has_grid_plan")),
            exclude_grid=bool(cfg.get("exclude_grid", True)),
        ):
            continue
        row = dict(row)
        row.setdefault("sniper_cfg", cfg)
        analysis = analyze_candidate(row, cash)
        usdt, size_reason = _size_for_row(row, analysis, cash, cfg)
        loss = float(row.get("loss_pct") or 0)
        score = float(analysis["score"])
        hard = list(analysis.get("hard_fail") or [])
        if usdt <= 0 and size_reason:
            hard = hard + [size_reason]
        v = CandidateView(
            symbol=str(row.get("symbol") or ""),
            timeframe=str(row.get("timeframe") or "1h"),
            average_entry=float(row.get("average_entry") or 0),
            amount=float(row.get("amount") or 0),
            mark=float(row.get("mark") or 0),
            loss_pct=loss,
            notional=float(row.get("notional") or 0),
            dca_rounds=int(row.get("dca_rounds") or 0),
            recovery_hold=bool(row.get("recovery_hold")),
            sniper_focus=bool(row.get("sniper_focus")),
            strategy_profile=str(row.get("strategy_profile") or ""),
            strategy_class=str(row.get("strategy_class") or ""),
            has_grid_plan=bool(row.get("has_grid_plan")),
            score=score,
            checklist={**(analysis.get("checklist") or {}), "size_reason": size_reason},
            hard_fail=hard if usdt <= 0 else list(analysis.get("hard_fail") or []),
            usdt_suggest=usdt,
            rank_priority=rank_priority(score, loss, float(row.get("notional") or 0)),
        )
        views.append(v)
    views.sort(key=lambda x: x.rank_priority, reverse=True)
    return views


def run_cycle(
    client: DcaSniperBotClient | None = None,
    *,
    config: dict | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Full auto cycle. dry_run=True → no execute/fund-sell.

    Staging sharp: config notify_only=false executes live via bot Risk path.
    """
    cfg = dca_sniper_config(config)
    # notify_only was the log-only gate — force dry when set
    if cfg.get("notify_only"):
        dry_run = True
    client = client or DcaSniperBotClient()
    audit: dict[str, Any] = {
        "ts": time.time(),
        "actions": [],
        "n_candidates": 0,
        "n_focus": 0,
        "sharp": not dry_run and bool(cfg.get("enabled")),
    }

    if not cfg.get("enabled") and not dry_run:
        audit["skipped"] = "disabled"
        return audit

    cash_resp = client.cash()
    winners = list(cash_resp.get("winners") or [])
    cash = {
        "spendable_dca": float(cash_resp.get("spendable_dca") or 0),
        "spendable_new": float(cash_resp.get("spendable_new") or 0),
        "cash_floor_abs": float(cash_resp.get("cash_floor_abs") or 0),
        "balance": float(cash_resp.get("balance") or 0),
        "equity": float(cash_resp.get("equity") or cash_resp.get("balance") or 0),
        "cash_mode": str(cash_resp.get("cash_mode") or ""),
    }

    free_above = max(
        0.0,
        float(cash.get("balance") or 0) - float(cash.get("cash_floor_abs") or 0),
    )
    cand_resp = client.candidates()
    rows = list(cand_resp.get("candidates") or [])
    audit["n_candidates"] = len(rows)

    views = _as_candidate_views(rows, cash, cfg)
    open_focus = sum(1 for v in views if v.recovery_hold or v.sniper_focus)
    # select_focus_batch uses heavy_min_score as quality floor; allow smalls via usdt>0
    batch = select_focus_batch(
        views,
        spendable_dca=float(cash["spendable_dca"]),
        max_focus_slots=int(cfg["max_focus_slots"]),
        min_cash_after_focus=float(cfg["min_cash_after_focus"]),
        open_focus_count=open_focus,
        heavy_min_score=max(3.0, float(cfg["heavy_min_score"]) - 2.0),
    )
    audit["n_focus"] = len(batch)
    audit["ranked_top"] = [
        {"symbol": v.symbol, "score": v.score, "usdt": v.usdt_suggest, "loss": v.loss_pct}
        for v in views[:5]
    ]

    if not batch:
        dec = {"action": "WAIT", "reason": "no_heavy_yes_or_cash", "cash": cash}
        sniper_state.add_decision(dec)
        audit["actions"].append(dec)
        return audit

    focus_set: list[dict[str, Any]] = []
    spendable_left = float(cash["spendable_dca"])

    for v in batch:
        analysis_id = str(uuid.uuid4())
        plan = cash_plan(
            need_usdt=v.usdt_suggest,
            spendable_dca=spendable_left,
            free_cash_above_floor=free_above,
            soft_claim_enabled=bool(cfg.get("soft_claim_enabled")),
            soft_claim_max_usdt=float(cash.get("equity") or 10000)
            * (float(cfg.get("soft_claim_max_pct_equity") or 3) / 100.0),
        )
        action = plan["action"]
        if action == "NEED_CASH" and cfg.get("fund_from_winner_enabled"):
            # Try one fund-from-winner (analysed list from bot cash.winners)
            if winners and not dry_run:
                w = winners[0]
                fund_resp = client.fund_sell(
                    {
                        "symbol": w.get("symbol"),
                        "timeframe": w.get("timeframe") or "1h",
                        "price": w.get("mark"),
                        "partial": True,
                    }
                )
                fund_dec = {
                    "action": "FUND_SELL",
                    "symbol": w.get("symbol"),
                    "executed": bool(fund_resp.get("executed")),
                    "message": fund_resp.get("message"),
                    "for_focus": v.symbol,
                }
                sniper_state.add_decision(fund_dec)
                audit["actions"].append(fund_dec)
                if fund_resp.get("executed"):
                    # refresh cash after fund
                    cash_resp = client.cash()
                    spendable_left = float(cash_resp.get("spendable_dca") or spendable_left)
                    free_above = max(
                        0.0,
                        float(cash_resp.get("balance") or 0)
                        - float(cash_resp.get("cash_floor_abs") or 0),
                    )
                    winners = list(cash_resp.get("winners") or [])
                    plan = cash_plan(
                        need_usdt=v.usdt_suggest,
                        spendable_dca=spendable_left,
                        free_cash_above_floor=free_above,
                        soft_claim_enabled=bool(cfg.get("soft_claim_enabled")),
                        soft_claim_max_usdt=float(cash.get("equity") or 10000)
                        * (float(cfg.get("soft_claim_max_pct_equity") or 3) / 100.0),
                    )
                    action = plan["action"]
            if action == "NEED_CASH":
                smaller = min(v.usdt_suggest, spendable_left)
                if smaller >= float(cfg["min_meaningful_usdt"]):
                    v.usdt_suggest = round(smaller, 2)
                    plan = cash_plan(
                        need_usdt=v.usdt_suggest,
                        spendable_dca=spendable_left,
                        free_cash_above_floor=free_above,
                        soft_claim_enabled=bool(cfg.get("soft_claim_enabled")),
                        soft_claim_max_usdt=float(cash.get("equity") or 10000)
                        * (float(cfg.get("soft_claim_max_pct_equity") or 3) / 100.0),
                    )
                    action = plan["action"]

        if action != "DCA_HEAVY":
            dec = {
                "action": action,
                "symbol": v.symbol,
                "score": v.score,
                "plan": plan,
                "checklist_hard": v.hard_fail,
            }
            sniper_state.add_decision(dec)
            audit["actions"].append(dec)
            continue

        usdt = float(v.usdt_suggest)
        if usdt > spendable_left and plan.get("claim", 0) <= 0:
            usdt = spendable_left
        if usdt < float(cfg["min_meaningful_usdt"]):
            dec = {"action": "SKIP", "symbol": v.symbol, "reason": "size_too_small"}
            sniper_state.add_decision(dec)
            audit["actions"].append(dec)
            continue

        size_reason = str((v.checklist or {}).get("size_reason") or "DCA_HEAVY")
        is_heavy = size_reason == "DCA_HEAVY"
        payload = {
            "symbol": v.symbol,
            "timeframe": v.timeframe,
            "usdt": usdt,
            "price": v.mark,
            "reason_code": size_reason,
            "set_recovery_hold": True,
            "heavy": is_heavy,
            "analysis_id": analysis_id,
            "score": v.score,
            "checklist": v.checklist,
        }
        if dry_run:
            dec = {
                "action": size_reason,
                "dry_run": True,
                "symbol": v.symbol,
                "usdt": usdt,
                "score": v.score,
                "plan": plan,
            }
            sniper_state.add_decision(dec)
            audit["actions"].append(dec)
            spendable_left = max(0.0, spendable_left - usdt)
            focus_set.append(
                {
                    "symbol": v.symbol,
                    "timeframe": v.timeframe,
                    "usdt": usdt,
                    "since": time.time(),
                }
            )
            continue

        resp = client.execute(payload)
        dec = {
            "action": size_reason,
            "symbol": v.symbol,
            "usdt": usdt,
            "score": v.score,
            "plan": plan,
            "executed": bool(resp.get("executed")),
            "message": resp.get("message"),
            "analysis_id": analysis_id,
            "sharp": True,
        }
        sniper_state.add_decision(dec)
        audit["actions"].append(dec)
        log(
            f"dca_sniper cycle {v.symbol} usdt={usdt:.0f} score={v.score} "
            f"exec={resp.get('executed')} msg={str(resp.get('message') or '')[:60]}",
            "INFO",
        )
        if resp.get("executed"):
            spendable_left = max(0.0, spendable_left - usdt)
            focus_set.append(
                {
                    "symbol": v.symbol,
                    "timeframe": v.timeframe,
                    "usdt": usdt,
                    "since": time.time(),
                }
            )

    if focus_set:
        sniper_state.set_focus(focus_set)
    audit["spendable_left"] = spendable_left
    return audit
