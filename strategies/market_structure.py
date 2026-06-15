"""Bollinger-band and volume structure signals for DecisionEngine merge."""

from __future__ import annotations

from dataclasses import dataclass

from core.actions import BUY_STRONG, SELL_PARTIAL_20, SELL_PARTIAL_30
from core.models import MarketContext


@dataclass
class MarketStructureCandidate:
    action: str
    source: str
    priority: int
    rationale: str


def _gain_pct(market: MarketContext) -> float:
    entry = market.average_entry
    if entry <= 0:
        return 0.0
    return (market.current_price / entry - 1) * 100


def _tier_done(position: dict, tier: str) -> bool:
    return bool((position.get("rsi_sell_tiers_done") or {}).get(tier))


def evaluate_market_structure_sells(
    market: MarketContext,
    params: dict,
    position: dict,
) -> list[MarketStructureCandidate]:
    if not market.has_position or market.average_entry <= 0:
        return []

    candidates: list[MarketStructureCandidate] = []
    gain = _gain_pct(market)
    rsi = market.rsi
    vol = market.vol_multiplier
    price = market.current_price
    recent_high = float(position.get("recent_high") or price)
    drop_pct = (1 - price / recent_high) * 100 if recent_high > 0 else 0.0

    if params.get("bb_sell_enabled", True):
        upper = getattr(market, "upper_bb", 0) or 0
        ratio = float(params.get("bb_sell_upper_ratio", 0.99))
        rsi_min = float(params.get("bb_sell_rsi_min", 62))
        if upper > 0 and price >= upper * ratio and rsi >= rsi_min and not _tier_done(position, "30"):
            candidates.append(
                MarketStructureCandidate(
                    action=SELL_PARTIAL_30,
                    source="bb_upper",
                    priority=3,
                    rationale=f"BB->upper extension (price>={upper * ratio:.4f}, RSI={rsi:.1f})",
                )
            )

    if params.get("vol_exhaustion_sell_enabled", True):
        ex_max = float(params.get("vol_exhaustion_max", 0.75))
        ex_gain = float(params.get("vol_exhaustion_min_gain_pct", 25))
        ex_rsi = float(params.get("vol_exhaustion_rsi_min", 60))
        if gain >= ex_gain and vol <= ex_max and rsi >= ex_rsi and not _tier_done(position, "20"):
            candidates.append(
                MarketStructureCandidate(
                    action=SELL_PARTIAL_20,
                    source="vol_exhaustion",
                    priority=2,
                    rationale=f"Vol->exhaustion ({vol:.2f}x, gain={gain:.0f}%)",
                )
            )

    if params.get("vol_dump_sell_enabled", True):
        dump_drop = float(params.get("vol_dump_price_drop_pct", 15))
        dump_vol = float(params.get("vol_dump_min_multiplier", 1.4))
        dump_gain = float(params.get("vol_dump_requires_prior_gain_pct", 5))
        if drop_pct >= dump_drop and vol >= dump_vol and gain >= dump_gain and not _tier_done(position, "20"):
            candidates.append(
                MarketStructureCandidate(
                    action=SELL_PARTIAL_20,
                    source="vol_dump",
                    priority=2,
                    rationale=f"Vol->dump guard (drop={drop_pct:.0f}%, vol={vol:.2f}x)",
                )
            )

    return candidates


def evaluate_market_structure_buy_boost(
    market: MarketContext,
    params: dict,
    tech_buy: bool,
    cmc_buy: bool,
) -> MarketStructureCandidate | None:
    if market.has_position or not params.get("vol_buy_boost_enabled", False):
        return None
    if not (tech_buy or cmc_buy):
        return None
    vol_min = float(params.get("vol_buy_boost_min", 1.3))
    lower = market.lower_bb
    if lower <= 0:
        return None
    near_lower = market.current_price <= lower * 1.02
    if market.vol_multiplier >= vol_min and near_lower:
        return MarketStructureCandidate(
            action=BUY_STRONG,
            source="vol_buy_boost",
            priority=2,
            rationale=f"Vol->buy boost ({market.vol_multiplier:.2f}x near lower BB)",
        )
    return None
