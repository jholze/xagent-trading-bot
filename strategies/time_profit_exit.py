"""Time-based partial profit exit for volatile profiles (A/B testable)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from core.actions import SELL_PARTIAL_50
from core.models import MarketContext


@dataclass
class TimeProfitExitCandidate:
    action: str
    source: str
    priority: int
    rationale: str
    shadow_only: bool = False


def time_profit_exit_config(strategy_params: dict | None) -> dict:
    params = strategy_params or {}
    return dict(params.get("time_profit_exit") or {})


def symbol_in_active_bucket(symbol: str, active_fraction: float) -> bool:
    digest = hashlib.md5(symbol.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < max(0.0, min(1.0, float(active_fraction)))


def _hours_since(iso_ts: str | None, now: datetime) -> float | None:
    if not iso_ts:
        return None
    try:
        last_ts = datetime.fromisoformat(str(iso_ts).replace("Z", ""))
    except Exception:
        return None
    return (now - last_ts).total_seconds() / 3600.0


def _entry_timestamp(position: dict) -> str | None:
    return position.get("first_buy_at") or position.get("last_trade_at")


def _gain_pct(market: MarketContext) -> float:
    entry = market.average_entry
    if entry <= 0:
        return 0.0
    return (market.current_price / entry - 1) * 100


def _resolve_shadow_only(symbol: str, cfg: dict) -> bool:
    mode = str(cfg.get("mode", "active")).strip().lower()
    if mode in ("off", "disabled"):
        return False
    if not cfg.get("ab_test_enabled", False):
        return mode == "shadow"
    active_fraction = float(cfg.get("active_fraction", 0.5))
    return not symbol_in_active_bucket(symbol, active_fraction)


def evaluate_time_profit_exit(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
    *,
    now: datetime | None = None,
) -> TimeProfitExitCandidate | None:
    cfg = time_profit_exit_config(strategy_params)
    if not cfg.get("enabled", False):
        return None
    mode = str(cfg.get("mode", "active")).strip().lower()
    if mode in ("off", "disabled"):
        return None
    if not market.has_position or market.average_entry <= 0:
        return None
    if position.get("time_profit_exit_done"):
        return None

    now = now or datetime.now()
    hold_hours = float(cfg.get("hold_hours", 48))
    elapsed = _hours_since(_entry_timestamp(position), now)
    if elapsed is None or elapsed < hold_hours:
        return None

    min_gain = float(cfg.get("min_gain_pct", 0))
    gain = _gain_pct(market)
    if gain < min_gain:
        return None

    shadow = _resolve_shadow_only(market.symbol, cfg)
    return TimeProfitExitCandidate(
        action=SELL_PARTIAL_50,
        source="time_profit_exit",
        priority=4,
        rationale=(
            f"Time->profit exit ({elapsed:.0f}h held, gain={gain:.1f}%, sell 50%)"
        ),
        shadow_only=shadow,
    )