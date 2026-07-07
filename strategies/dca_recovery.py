"""DCA recovery — post-partial accumulation in the loss band."""

from __future__ import annotations

from strategies.dca import (
    DCACandidate,
    DCADecision,
    _evaluate_scoring,
    _hours_since,
    _near_stop_loss,
    _unrealized_loss_pct,
    _volatility_tier,
    dca_config,
)
from strategies.positions import position_notional_usdt

RECOVERY_TIER_DEFAULTS = {
    "volatile": {
        "loss_pct_min": -25.0,
        "loss_pct_max": -2.0,
        "max_rounds": 2,
        "interval_hours": 8.0,
        "remainder_size_ratio": 0.35,
        "sl_proximity_pct": 12.0,
    },
    "stable": {
        "loss_pct_min": -15.0,
        "loss_pct_max": -2.0,
        "max_rounds": 1,
        "interval_hours": 18.0,
        "remainder_size_ratio": 0.30,
        "sl_proximity_pct": 10.0,
    },
}

RECOVERY_BASE_DEFAULTS = {
    "enabled": False,
    "mode": "shadow",
    "max_sold_percent": 0.85,
    "min_remainder_usdt": 150.0,
    "cascade_min_drop_pct": 4.0,
    "cascade_score_discount": 1,
    "scoring_inherit": True,
}


def recovery_config(strategy_params: dict | None) -> dict:
    """Merge dca.recovery with tier defaults."""
    dca = dca_config(strategy_params)
    raw = dict(dca.get("recovery") or {})
    tier = _volatility_tier(strategy_params)
    tier_defaults = dict(RECOVERY_TIER_DEFAULTS.get(tier, RECOVERY_TIER_DEFAULTS["stable"]))
    cfg = {**RECOVERY_BASE_DEFAULTS, **tier_defaults, **raw}
    return cfg


def recovery_enabled(strategy_params: dict | None) -> bool:
    return bool(recovery_config(strategy_params).get("enabled", False))


def in_recovery_phase(position: dict) -> bool:
    step = int(position.get("exit_ladder_step", 0) or 0)
    sold = float(position.get("sold_percent", 0) or 0)
    return step > 0 or sold >= 0.01


def _effective_max_recovery_rounds(position: dict, cfg: dict) -> int:
    cfg_max = int(cfg.get("max_rounds", 1))
    frozen = int(position.get("dca_recovery_max_rounds", 0) or 0)
    if frozen <= 0:
        position["dca_recovery_max_rounds"] = cfg_max
        return cfg_max
    return frozen


def _cascade_ref_price(position: dict, entry: float) -> float:
    for key in ("last_recovery_ref_price", "last_buy_price", "average_entry"):
        val = float(position.get(key, 0) or 0)
        if val > 0:
            return val
    return entry


def _cascade_active(position: dict, current_price: float, entry: float, cfg: dict) -> bool:
    ref = _cascade_ref_price(position, entry)
    if ref <= 0 or current_price <= 0:
        return False
    drop_pct = (1.0 - (current_price / ref)) * 100.0
    return drop_pct >= float(cfg.get("cascade_min_drop_pct", 4.0))


def recovery_usdt_amount(
    cfg: dict,
    strategy_params: dict | None,
    *,
    score: int = 0,
    loss_pct: float = 0.0,
    round_index: int = 0,
) -> float:
    from strategies.dca_sizing import compute_dca_usdt

    dca = dca_config(strategy_params)
    base = float(dca.get("fixed_usdt", 20))
    ratio = float(cfg.get("remainder_size_ratio", 0.35))
    scoring = dict(dca.get("scoring") or {})
    return compute_dca_usdt(
        base_usdt=base,
        score=score,
        max_score=int(scoring.get("max_score", 10)),
        min_score=int(scoring.get("min_score", 6)),
        loss_pct=loss_pct,
        round_index=round_index,
        max_rounds=int(cfg.get("max_rounds", 2)),
        dca_cfg=dca,
        is_recovery=True,
        recovery_ratio=ratio,
    )


def _check_recovery_hard_gates(
    market,
    position: dict,
    strategy_params: dict | None,
    cfg: dict,
) -> tuple[bool, str | None, float]:
    if not cfg.get("enabled", False):
        return False, "recovery_disabled", 0.0
    if not market.has_position or market.average_entry <= 0:
        return False, "no_position", 0.0
    if not in_recovery_phase(position):
        return False, "not_recovery_phase", 0.0

    loss_pct = _unrealized_loss_pct(market.average_entry, market.current_price)
    if loss_pct >= 0:
        return False, "gain_positive_rotation_path", 0.0

    sold = float(position.get("sold_percent", 0) or 0)
    max_sold = float(cfg.get("max_sold_percent", 0.85))
    if sold >= max_sold:
        return False, f"sold>={max_sold:.0%}", 0.0

    notional = position_notional_usdt(position)
    min_remainder = float(cfg.get("min_remainder_usdt", 150))
    if notional < min_remainder:
        return False, f"remainder<{min_remainder:.0f}", 0.0

    loss_min = float(cfg.get("loss_pct_min", -25))
    loss_max = float(cfg.get("loss_pct_max", -2))
    if loss_pct < loss_min or loss_pct > loss_max:
        return False, f"loss_pct {loss_pct:.1f}% outside [{loss_min}, {loss_max}]", 0.0

    proximity_cfg = dict(cfg)
    if cfg.get("sl_proximity_pct") is not None:
        proximity_cfg["sl_proximity_pct"] = cfg["sl_proximity_pct"]
    if _near_stop_loss(loss_pct, strategy_params or {}, proximity_cfg):
        return False, "near_stop_loss", 0.0

    max_rounds = _effective_max_recovery_rounds(position, cfg)
    rounds = int(position.get("dca_recovery_rounds", 0) or 0)
    if rounds >= max_rounds:
        return False, "max_recovery_rounds", 0.0

    interval_hours = float(cfg.get("interval_hours", 8))
    elapsed = _hours_since(position.get("last_dca_recovery_at"))
    if elapsed is not None and elapsed < interval_hours:
        return False, "recovery_interval", 0.0

    return True, None, loss_pct


def should_dca_recovery(market, position: dict, strategy_params: dict | None) -> DCADecision:
    cfg = recovery_config(strategy_params)
    ok, blocked_reason, loss_pct = _check_recovery_hard_gates(
        market, position, strategy_params, cfg,
    )
    if not ok:
        return DCADecision(should_dca=False, blocked_reason=blocked_reason)

    dca = dca_config(strategy_params)
    scoring_cfg = dict(dca.get("scoring") or {})
    mode = str(cfg.get("mode", "shadow"))
    cascade = _cascade_active(position, market.current_price, market.average_entry, cfg)
    rounds = int(position.get("dca_recovery_rounds", 0) or 0)

    if scoring_cfg.get("enabled", False) and cfg.get("scoring_inherit", True):
        decision = _evaluate_scoring(market, loss_pct, dca, strategy_params, position)
        if cascade:
            discount = int(cfg.get("cascade_score_discount", 1))
            min_score = int(scoring_cfg.get("min_score", 6)) - discount
            min_core = int(scoring_cfg.get("min_core_criteria_met", 3)) - discount
            core_keys = ("atr_distance", "rsi", "funding", "btc_underperf")
            core_met = sum(1 for k in core_keys if decision.breakdown.get(k, 0) > 0)
            passed = decision.score >= min_score and core_met >= min_core
            if not passed:
                decision.should_dca = False
                decision.blocked_reason = (
                    f"cascade score {decision.score}/{decision.max_score} "
                    f"(core {core_met}/{min_core}, need {min_score}): {decision.breakdown}"
                )
                return decision
            decision.should_dca = True
            decision.blocked_reason = None
        elif not decision.should_dca:
            return decision
        decision.usdt_amount = recovery_usdt_amount(
            cfg, strategy_params, score=decision.score, loss_pct=loss_pct, round_index=rounds,
        )
        decision.shadow_only = mode == "shadow"
        return decision

    usdt_amount = recovery_usdt_amount(
        cfg, strategy_params, score=0, loss_pct=loss_pct, round_index=rounds,
    )
    return DCADecision(
        should_dca=True,
        score=0,
        usdt_amount=usdt_amount,
        shadow_only=mode == "shadow",
    )


def evaluate_dca_recovery(market, position: dict, strategy_params: dict | None) -> DCACandidate | None:
    """Return BUY_DCA candidate for post-partial recovery when gates pass."""
    decision = should_dca_recovery(market, position, strategy_params)
    if not decision.should_dca:
        return None

    cfg = recovery_config(strategy_params)
    rounds = int(position.get("dca_recovery_rounds", 0) or 0)
    max_rounds = _effective_max_recovery_rounds(position, cfg)
    loss_pct = _unrealized_loss_pct(market.average_entry, market.current_price)
    sold = float(position.get("sold_percent", 0) or 0)

    if decision.score > 0:
        core = {k: v for k, v in decision.breakdown.items() if k != "bb_support" and v > 0}
        rationale = (
            f"DCA-Recovery score {decision.score}/{decision.max_score} "
            f"loss {loss_pct:.1f}% sold {sold:.0%} round {rounds + 1}/{max_rounds} "
            f"[{', '.join(f'{k}={v}' for k, v in core.items())}]"
        )
    else:
        rationale = (
            f"DCA-Recovery loss {loss_pct:.1f}% sold {sold:.0%} "
            f"(round {rounds + 1}/{max_rounds})"
        )

    from core.actions import BUY_DCA

    return DCACandidate(
        action=BUY_DCA,
        source="dca_recovery",
        rationale=rationale,
        usdt_amount=decision.usdt_amount,
        shadow_only=decision.shadow_only,
        score=decision.score,
        breakdown=dict(decision.breakdown),
    )