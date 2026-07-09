"""Force full exit after prolonged time in profit (runners exempt)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.actions import SELL_FULL
from core.models import MarketContext


@dataclass
class ProfitMaxLifetimeCandidate:
    action: str
    source: str
    priority: int
    rationale: str
    shadow_only: bool = False


def profit_max_lifetime_config(strategy_params: dict | None) -> dict:
    return dict((strategy_params or {}).get("profit_max_lifetime") or {})


def _gain_pct(market: MarketContext) -> float:
    entry = market.average_entry
    if entry <= 0:
        return 0.0
    return (market.current_price / entry - 1) * 100


def _peak_gain_pct(market: MarketContext, position: dict) -> float:
    entry = market.average_entry
    if entry <= 0:
        return 0.0
    recent_high = float(position.get("recent_high") or 0) or market.current_price
    return (recent_high / entry - 1) * 100


def _hours_since(iso_ts: str | None, now: datetime) -> float | None:
    if not iso_ts:
        return None
    try:
        last_ts = datetime.fromisoformat(str(iso_ts).replace("Z", ""))
    except Exception:
        return None
    return (now - last_ts).total_seconds() / 3600.0


def evaluate_profit_max_lifetime(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
    *,
    now: datetime | None = None,
) -> ProfitMaxLifetimeCandidate | None:
    cfg = profit_max_lifetime_config(strategy_params)
    if not cfg.get("enabled", False):
        return None
    mode = str(cfg.get("mode", "live")).strip().lower()
    if mode in ("off", "disabled"):
        return None
    if not market.has_position or market.average_entry <= 0:
        return None
    if position.get("profit_max_lifetime_done"):
        return None

    armed_at = position.get("profit_armed_at")
    if not armed_at:
        return None

    skip_peak = float(cfg.get("skip_if_peak_above_pct", 40.0))
    if _peak_gain_pct(market, position) >= skip_peak:
        return None

    min_gain = float(cfg.get("min_gain_pct", 1.0))
    gain = _gain_pct(market)
    if gain < min_gain:
        return None

    now = now or datetime.now()
    max_hours = float(cfg.get("max_hours", 96))
    elapsed = _hours_since(armed_at, now)
    if elapsed is None or elapsed < max_hours:
        return None

    shadow = mode == "shadow"
    return ProfitMaxLifetimeCandidate(
        action=SELL_FULL,
        source="profit_max_lifetime",
        priority=6,
        rationale=f"Life->max profit ({elapsed:.0f}h in profit, gain={gain:.1f}%)",
        shadow_only=shadow,
    )


def sync_profit_armed_at(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Set profit_armed_at on first crossing of arm_gain_pct. Returns True if changed."""
    cfg = profit_max_lifetime_config(strategy_params)
    if not cfg.get("enabled", False):
        return False
    if position.get("profit_armed_at"):
        return False
    arm_gain = float(cfg.get("arm_gain_pct", 3.0))
    if _gain_pct(market) < arm_gain:
        return False
    position["profit_armed_at"] = (now or datetime.now()).isoformat()
    return True