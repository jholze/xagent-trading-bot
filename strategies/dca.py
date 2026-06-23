"""DCA accumulation — add to open positions before first exit-ladder sell."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.actions import BUY_DCA
from core.config import get_bot_config
from core.models import MarketContext


@dataclass
class DCACandidate:
    action: str
    source: str
    rationale: str
    usdt_amount: float
    shadow_only: bool = False


def dca_config(strategy_params: dict | None) -> dict:
    params = strategy_params or {}
    return dict(params.get("dca") or {})


def dca_enabled(strategy_params: dict | None) -> bool:
    cfg = dca_config(strategy_params)
    return bool(cfg.get("enabled", False))


def _unrealized_loss_pct(entry: float, price: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    return (price / entry - 1.0) * 100.0


def _hours_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        last_ts = datetime.fromisoformat(str(iso_ts).replace("Z", ""))
    except Exception:
        return None
    return (datetime.now() - last_ts).total_seconds() / 3600.0


def _in_accumulation_phase(position: dict) -> bool:
    step = int(position.get("exit_ladder_step", 0) or 0)
    sold = float(position.get("sold_percent", 0) or 0)
    return step == 0 and sold < 0.01


def _near_stop_loss(
    loss_pct: float,
    strategy_params: dict,
    cfg: dict,
) -> bool:
    """Block DCA when unrealized loss is within sl_proximity_pct of the stop trigger."""
    proximity = float(cfg.get("sl_proximity_pct", 15))
    if proximity <= 0:
        return False
    stop_pct = float(
        strategy_params.get("stop_loss_pct")
        or get_bot_config().stop_loss_pct
    )
    margin = stop_pct + loss_pct
    if margin <= 0:
        return False
    buffer = stop_pct * (proximity / 100.0)
    return margin < buffer


def evaluate_dca_addon(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
) -> DCACandidate | None:
    """Return a BUY_DCA candidate when accumulation-only rules pass."""
    cfg = dca_config(strategy_params)
    if not cfg.get("enabled", False):
        return None
    if not market.has_position or market.average_entry <= 0:
        return None
    if not _in_accumulation_phase(position):
        return None

    loss_pct = _unrealized_loss_pct(market.average_entry, market.current_price)
    loss_min = float(cfg.get("loss_pct_min", -20))
    loss_max = float(cfg.get("loss_pct_max", -3))
    if loss_pct > loss_max or loss_pct < loss_min:
        return None
    if _near_stop_loss(loss_pct, strategy_params or {}, cfg):
        return None

    max_rounds = int(cfg.get("max_rounds", 3))
    rounds = int(position.get("dca_rounds", 0) or 0)
    if rounds >= max_rounds:
        return None

    interval_hours = float(cfg.get("interval_hours", 12))
    elapsed = _hours_since(position.get("last_dca_at"))
    if elapsed is not None and elapsed < interval_hours:
        return None

    fixed_usdt = float(cfg.get("fixed_usdt", 20))
    mode = str(cfg.get("mode", "shadow"))
    shadow_only = mode == "shadow"

    return DCACandidate(
        action=BUY_DCA,
        source="dca",
        rationale=f"DCA dip {loss_pct:.1f}% (round {rounds + 1}/{max_rounds})",
        usdt_amount=fixed_usdt,
        shadow_only=shadow_only,
    )