"""Memory-aware grid sell checks (pure, fail-open).

Uses coin-fact flags (same taxonomy as DCA) to decide whether a *grid harvest*
is still smart — without Grok on the hot path.

Does not replace stops/trails: those stay outside grid. This only gates
profit-taking / grid slices.
"""

from __future__ import annotations

from typing import Any

from core.actions import HOLD
from strategies.grid_plan import GridAction


def _flag(flags: Any, name: str) -> bool:
    return bool(getattr(flags, name, False))


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        v = cfg.get(key, default)
        return float(v if v is not None else default)
    except (TypeError, ValueError):
        return float(default)


def apply_grid_memory_sell_policy(
    act: GridAction,
    *,
    gain_pct: float | None,
    flags: Any = None,
    policy: dict | None = None,
) -> GridAction:
    """Adjust/block a grid sell using memory fact flags.

    Returns HOLD with rationale when memory says the harvest is a bad idea.
    """
    if not act or "SELL" not in str(act.action or "").upper():
        return act
    pol = dict(policy or {})
    if not pol.get("memory_enabled", True):
        return act
    if flags is None:
        return act

    try:
        g = float(gain_pct) if gain_pct is not None else None
    except (TypeError, ValueError):
        g = None

    # 1) Hard-negative (hack/exploit/sec): do not grid-slice — leave to stop/trail/full policy
    if _flag(flags, "hard_negative"):
        min_g = _f(pol, "memory_hard_neg_min_gain_pct", 5.0)
        if g is None or g < min_g:
            return GridAction(
                action=HOLD,
                rationale=(
                    f"{act.rationale} | memory: hard_negative — "
                    f"skip grid harvest (need stop/trail path, gain={g if g is not None else '?'}%)"
                ),
            )

    # 2) Structure risk: weak vs BTC / dump narrative — don't sell weak bounces as "profit"
    if _flag(flags, "structure_risk"):
        min_g = _f(pol, "memory_structure_min_gain_pct", 2.0)
        if g is None or g < min_g:
            return GridAction(
                action=HOLD,
                rationale=(
                    f"{act.rationale} | memory: structure_risk — "
                    f"skip grid sell until gain≥{min_g:.1f}% (now {g if g is not None else '?'}%)"
                ),
            )

    # 3) Momentum / breakout / catalyst: hold runners — skip early grid harvest
    if _flag(flags, "volume_breakout") or _flag(flags, "catalyst"):
        hold_below = _f(pol, "memory_runner_hold_below_gain_pct", 8.0)
        if g is not None and 0 <= g < hold_below:
            return GridAction(
                action=HOLD,
                rationale=(
                    f"{act.rationale} | memory: momentum — "
                    f"hold runner (gain {g:.1f}% < {hold_below:.0f}% harvest floor)"
                ),
            )

    # 4) Flow-only pumps: allow grid harvest earlier (no block) — optional tighter min
    if _flag(flags, "flow_only"):
        min_g = _f(pol, "memory_flow_only_min_gain_pct", 0.0)
        if g is not None and g < min_g:
            return GridAction(
                action=HOLD,
                rationale=(
                    f"{act.rationale} | memory: flow_only — "
                    f"need gain≥{min_g:.1f}% before grid harvest"
                ),
            )

    return act
