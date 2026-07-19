"""Coin-fact factors for DCA policy (#103) — pure, table-driven.

Hard-neg skip → caution size-downs (unlock may escalate to skip) →
soft boosts only when not flow-only / not oversold-gated → noise tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default) if cfg.get(key) is not None else default)
    except (TypeError, ValueError):
        return float(default)


def _flag(ctx: Any, name: str) -> bool:
    return bool(getattr(ctx, name, False))


@dataclass(frozen=True)
class _CautionRule:
    """Size-down when flag is set (before soft boosts)."""

    flag: str
    mult_key: str
    code: str
    default: float


# Severe → mild (audit order)
_CAUTION: tuple[_CautionRule, ...] = (
    _CautionRule("fact_unlock", "fact_unlock_mult", "fact_unlock", 0.5),
    _CautionRule("fact_structure_risk", "fact_structure_risk_mult", "fact_structure_risk", 0.5),
    _CautionRule("fact_profit_taking", "fact_profit_taking_mult", "fact_profit_taking", 0.7),
    _CautionRule("fact_flow_only", "fact_flow_only_mult", "fact_flow_only", 0.8),
)


@dataclass(frozen=True)
class _BoostRule:
    flag: str
    mult_key: str
    code: str
    default: float
    oversold_only: bool = False


_BOOSTS: tuple[_BoostRule, ...] = (
    _BoostRule("fact_volume_breakout", "fact_volume_breakout_mult", "fact_volume_breakout", 1.1),
    _BoostRule("fact_catalyst", "fact_catalyst_mult", "fact_catalyst", 1.1, oversold_only=True),
    _BoostRule("fact_utility", "fact_utility_mult", "fact_utility", 1.1, oversold_only=True),
)


def apply_coin_fact_policy(
    ctx: Any,
    cfg: dict,
    *,
    mult: float,
    skip: bool,
    reasons: list[str],
) -> tuple[float, bool, list[str]]:
    """Apply coin-fact flags. Returns (mult, skip, reasons). No I/O."""

    def _noise_tag() -> None:
        if _flag(ctx, "fact_noise_only") and not any(c.startswith("fact_") for c in reasons):
            reasons.append("fact_noise_ignore")

    if skip:
        _noise_tag()
        return mult, skip, reasons

    if _flag(ctx, "fact_hard_negative"):
        skip = True
        reasons.append("fact_hard_negative")
        return mult, skip, reasons

    for rule in _CAUTION:
        if not _flag(ctx, rule.flag):
            continue
        mult *= _f(cfg, rule.mult_key, rule.default)
        reasons.append(rule.code)
        # Unlock can terminate further fact sizing
        if rule.flag == "fact_unlock":
            try:
                min_imp = float(getattr(ctx, "fact_min_impact", 0.0) or 0.0)
            except (TypeError, ValueError):
                min_imp = 0.0
            if mult < 0.35 or min_imp <= -0.8:
                skip = True
                reasons.append("fact_unlock_skip")
                _noise_tag()
                return mult, skip, reasons

    if skip:
        _noise_tag()
        return mult, skip, reasons

    flow_only = _flag(ctx, "fact_flow_only")
    try:
        loss = float(getattr(ctx, "loss_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        loss = 0.0
    oversold = loss <= -5.0

    if not flow_only:
        for rule in _BOOSTS:
            if not _flag(ctx, rule.flag):
                continue
            if rule.oversold_only and not oversold:
                continue
            mult *= _f(cfg, rule.mult_key, rule.default)
            reasons.append(rule.code)

    _noise_tag()
    return mult, skip, reasons
