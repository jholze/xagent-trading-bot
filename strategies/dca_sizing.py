"""Adaptive DCA position sizing from score, loss depth, round, and position notional."""

from __future__ import annotations


def sizing_config(dca_cfg: dict | None) -> dict:
    defaults = {
        "enabled": True,
        "base_mode": "max",
        "notional_ratio": 0.0,
        "recovery_notional_ratio": 0.0,
        "min_multiplier": 0.45,
        "max_multiplier": 1.0,
        "loss_depth_boost": 0.15,
        "round_decay": 0.12,
        "min_usdt": 80.0,
        "max_usdt": 450.0,
        "recovery_base_ratio": 0.35,
    }
    raw = dict((dca_cfg or {}).get("sizing") or {})
    return {**defaults, **raw}


def _cap_limits(cfg: dict, *, is_recovery: bool) -> tuple[float, float]:
    if is_recovery:
        min_cap = float(cfg.get("recovery_min_usdt") or cfg["min_usdt"])
        max_cap = float(cfg.get("recovery_max_usdt") or cfg["max_usdt"])
    else:
        min_cap = float(cfg["min_usdt"])
        max_cap = float(cfg["max_usdt"])
    return min_cap, max_cap


def resolve_dca_base_usdt(
    *,
    base_usdt: float,
    position_notional_usdt: float,
    cfg: dict,
    is_recovery: bool = False,
    recovery_ratio: float | None = None,
) -> float:
    """Blend fixed_usdt anchor with a fraction of open position notional."""
    ratio_key = "recovery_notional_ratio" if is_recovery else "notional_ratio"
    notional_ratio = float(cfg.get(ratio_key, 0) or 0)
    fixed_base = float(base_usdt)
    if is_recovery:
        rec_ratio = recovery_ratio if recovery_ratio is not None else float(cfg["recovery_base_ratio"])
        fixed_base = fixed_base * rec_ratio

    if notional_ratio <= 0 or position_notional_usdt <= 0:
        return fixed_base

    notional_base = position_notional_usdt * notional_ratio
    mode = str(cfg.get("base_mode", "max")).lower()
    if mode == "notional":
        return notional_base
    if mode == "fixed":
        return fixed_base
    return max(fixed_base, notional_base)


def compute_dca_usdt(
    *,
    base_usdt: float,
    score: int,
    max_score: int,
    min_score: int,
    loss_pct: float,
    round_index: int,
    max_rounds: int,
    dca_cfg: dict | None,
    is_recovery: bool = False,
    recovery_ratio: float | None = None,
    position_notional_usdt: float = 0.0,
) -> float:
    """Score-adaptive USDT size; optional notional fraction raises base for larger lots."""
    cfg = sizing_config(dca_cfg)
    min_cap, max_cap = _cap_limits(cfg, is_recovery=is_recovery)

    if not cfg.get("enabled", True):
        if is_recovery:
            ratio = recovery_ratio if recovery_ratio is not None else float(cfg["recovery_base_ratio"])
            return round(max(min_cap, base_usdt * ratio), 2)
        return round(float(base_usdt), 2)

    base = resolve_dca_base_usdt(
        base_usdt=base_usdt,
        position_notional_usdt=position_notional_usdt,
        cfg=cfg,
        is_recovery=is_recovery,
        recovery_ratio=recovery_ratio,
    )

    span = max(1, int(max_score) - int(min_score))
    score_norm = max(0.0, min(1.0, (int(score) - int(min_score)) / span))
    min_mult = float(cfg["min_multiplier"])
    max_mult = float(cfg["max_multiplier"])
    score_mult = min_mult + (max_mult - min_mult) * score_norm

    loss_boost = float(cfg["loss_depth_boost"])
    loss_mult = 1.0
    if loss_pct < 0:
        loss_mult = 1.0 + min(loss_boost, abs(loss_pct) / 20.0 * loss_boost)

    decay = float(cfg["round_decay"])
    round_mult = max(0.55, 1.0 - int(round_index) * decay)

    sized = base * score_mult * loss_mult * round_mult
    sized = max(min_cap, min(max_cap, sized))
    return round(sized, 2)