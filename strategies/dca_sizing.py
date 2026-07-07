"""Adaptive DCA position sizing from score, loss depth, and round."""

from __future__ import annotations


def sizing_config(dca_cfg: dict | None) -> dict:
    defaults = {
        "enabled": True,
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
) -> float:
    """Score-adaptive USDT size; recovery uses smaller base ratio."""
    cfg = sizing_config(dca_cfg)
    if not cfg.get("enabled", True):
        if is_recovery:
            ratio = recovery_ratio if recovery_ratio is not None else float(cfg["recovery_base_ratio"])
            return round(max(float(cfg["min_usdt"]), base_usdt * ratio), 2)
        return round(float(base_usdt), 2)

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

    base = float(base_usdt)
    if is_recovery:
        ratio = recovery_ratio if recovery_ratio is not None else float(cfg["recovery_base_ratio"])
        base = base * ratio

    sized = base * score_mult * loss_mult * round_mult
    sized = max(float(cfg["min_usdt"]), min(float(cfg["max_usdt"]), sized))
    return round(sized, 2)