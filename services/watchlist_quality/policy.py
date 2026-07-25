"""WQE-R7: Grid / sandbox / Hermes policy helpers vs WQE tiers."""

from __future__ import annotations

from typing import Any

from services.watchlist_quality.config import wqe_mode
from services.watchlist_quality.store import load_quality_scores


def _tier_map() -> dict[str, str]:
    data = load_quality_scores()
    out: dict[str, str] = {}
    for c in data.get("coins") or []:
        if isinstance(c, dict) and c.get("symbol"):
            out[str(c["symbol"])] = str(c.get("tier") or c.get("tier_hint") or "")
    return out


def filter_for_grid(
    coins: list[dict[str, Any]],
    *,
    config: dict | None = None,
    allow_t2: bool = True,
) -> list[dict[str, Any]]:
    """Prefer T1 (+ optional T2). Fail-open to input if WQE off or no scores."""
    if wqe_mode(config) == "off":
        return list(coins or [])
    tiers = _tier_map()
    if not tiers:
        return list(coins or [])
    allowed = {"T1", "POS"}
    if allow_t2:
        allowed.add("T2")
    out = []
    for c in coins or []:
        if not isinstance(c, dict):
            continue
        sym = str(c.get("symbol") or "")
        t = tiers.get(sym) or c.get("tier") or c.get("tier_hint") or "T2"
        if t in allowed or c.get("is_open"):
            out.append(c)
    # never empty grid if we had candidates
    return out or list(coins or [])


def hermes_pool_flags(symbol: str) -> dict[str, Any]:
    """Annotate Hermes learning pool membership (never removes learning symbols)."""
    data = load_quality_scores()
    for c in data.get("coins") or []:
        if isinstance(c, dict) and c.get("symbol") == symbol:
            return {
                "wqe_tier": c.get("tier") or c.get("tier_hint"),
                "wqe_score": c.get("quality_shadow_ai") or c.get("quality_score"),
                "memory_soft_block": "memory_soft_block" in (c.get("flags") or []),
                "learn_ok": True,
            }
    return {"learn_ok": True, "wqe_tier": None}


POLICY_TABLE = """
| Consumer | WQE off | soft | enforce |
|----------|---------|------|---------|
| Cycle scan | full effective | filtered+sorted | tiers+caps |
| Grid | full | prefer T1/T2 | T1 (+T2 cfg) |
| Hermes pool | full | learn all + flags | learn all + flags |
| Sandbox | full (research) | full optional | full optional |
| Sensor | full | sensor_universe | sensor_universe |
"""
