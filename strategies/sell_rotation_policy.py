"""Rotation-safe sell policy (D′), trail-exclusive, ladder terminal, tail idle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from core.actions import (
    SELL_FULL,
    SELL_PARTIAL_10,
    SELL_PARTIAL_20,
    SELL_PARTIAL_30,
    is_sell,
)

PROFIT_PARTIAL_ACTIONS = frozenset({
    SELL_PARTIAL_10,
    SELL_PARTIAL_20,
    SELL_PARTIAL_30,
    "SELL_10",
    "SELL_20",
    "SELL_30",
    "SELL_TP",
})
from core.models import MarketContext
from strategies.exit_ladder import current_ladder_step, ladder_config, ladder_enabled
from strategies.positions import is_open_position, position_notional_usdt
from strategies.sell_sources import (
    STRUCTURE_SOURCES,
    TRAIL_EXCLUSIVE_BLOCK_SOURCES,
    TRAIL_ROTATION_SOURCES,
)

TRAIL_SOURCES = TRAIL_ROTATION_SOURCES
BLOCKED_BELOW_ARM = TRAIL_EXCLUSIVE_BLOCK_SOURCES

POLICY_DEFAULTS = {
    "mode": "shadow",
    "shadow_log_decisions": True,
    "trail_exclusive": True,
    "evict_min_gain_pct": 0.0,
    "arm_gain_pct": 12.0,
    "pre_arm_ta_allowed": True,
    "pre_arm_min_gain_pct": 10.0,
    "pre_arm_max_gain_pct": 15.0,
    "tail_idle_hours": 24.0,
    "tail_exempt_sold_pct": 0.50,
    "tail_exempt_notional_usdt": 800.0,
    "trail_exit_full_close": True,
    "profit_exit_full_close": False,
}


@dataclass
class SellPolicyAudit:
    rotation_blocked: bool = False
    recovery_candidate: bool = False
    tail_exempt: bool = False
    ladder_terminal_would_close: bool = False
    tail_idle_would_close: bool = False
    trail_exclusive_blocked: list[str] = field(default_factory=list)
    would_sell: str = ""
    would_source: str = ""


def sell_policy_root(config_raw: dict | None) -> dict:
    return dict((config_raw or {}).get("sell_policy") or {})


def rotation_config(config_raw: dict | None, strategy_params: dict | None = None) -> dict:
    root = sell_policy_root(config_raw)
    rotation = dict(root.get("rotation") or {})
    cfg = {**POLICY_DEFAULTS, **rotation}
    if strategy_params:
        sp = dict(strategy_params.get("sell_policy") or {})
        cfg.update(sp.get("rotation") or {})
        tier = str(strategy_params.get("volatility_tier") or "volatile").lower()
        if tier == "stable":
            cfg["arm_gain_pct"] = float(cfg.get("arm_gain_pct_stable", 15.0))
    return cfg


def policy_mode(config_raw: dict | None) -> str:
    return str(sell_policy_root(config_raw).get("mode", "shadow")).lower()


def policy_shadow_active(config_raw: dict | None) -> bool:
    return policy_mode(config_raw) == "shadow"


def rotation_gain_pct(market: MarketContext) -> float:
    entry = market.average_entry
    if entry <= 0:
        return 0.0
    return (market.current_price / entry - 1) * 100


def can_rotation_evict(
    market: MarketContext,
    position: dict,
    cfg: dict,
) -> bool:
    gain = rotation_gain_pct(market)
    realized = float(position.get("realized_pnl", 0) or 0)
    min_gain = float(cfg.get("evict_min_gain_pct", 0.0))
    return gain >= min_gain or realized > 0


def is_tail_position(position: dict, cfg: dict) -> bool:
    if not is_open_position(position):
        return False
    sold = float(position.get("sold_percent", 0) or 0)
    if sold >= float(cfg.get("tail_exempt_sold_pct", 0.5)):
        return True
    notional = position_notional_usdt(position)
    cap = float(cfg.get("tail_exempt_notional_usdt", 800.0))
    return 0 < notional < cap


def count_open_full_slots(config_raw: dict | None = None) -> int:
    from strategies.positions import count_open_full_slots as _count

    return _count(config_raw)


def count_open_tail_slots(config_raw: dict | None = None) -> int:
    from strategies.positions import count_open_tail_slots as _count

    return _count(config_raw)


def _hours_since(iso_ts: str | None, now: datetime | None = None) -> float | None:
    if not iso_ts:
        return None
    try:
        last_ts = datetime.fromisoformat(str(iso_ts).replace("Z", ""))
    except Exception:
        return None
    now = now or datetime.now()
    return (now - last_ts).total_seconds() / 3600.0


def _sold_pct(position: dict) -> float:
    sold = float(position.get("sold_percent", 0) or 0)
    if sold > 0:
        return sold
    peak = float(position.get("peak_amount", 0) or 0)
    amount = float(position.get("amount", 0) or 0)
    if peak > 0 and amount > 0:
        return 1.0 - (amount / peak)
    return 0.0


@dataclass
class RotationSellCandidate:
    action: str
    priority: int
    source: str
    rationale: str


def evaluate_ladder_terminal(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
    cfg: dict,
) -> RotationSellCandidate | None:
    if not ladder_enabled(strategy_params):
        return None
    tiers = ladder_config(strategy_params).get("tiers") or []
    if not tiers:
        return None
    step = current_ladder_step(position, tiers)
    amount = float(position.get("amount", 0) or 0)
    if amount <= 0 or step < len(tiers):
        return None
    if not can_rotation_evict(market, position, cfg):
        return None
    return RotationSellCandidate(
        action=SELL_FULL,
        priority=6,
        source="ladder_terminal",
        rationale=f"Ladder terminal step={step}/{len(tiers)} gain={rotation_gain_pct(market):.1f}%",
    )


def evaluate_tail_idle_close(
    market: MarketContext,
    position: dict,
    cfg: dict,
    *,
    now: datetime | None = None,
) -> RotationSellCandidate | None:
    idle_h = float(cfg.get("tail_idle_hours", 24.0))
    if idle_h <= 0:
        return None
    sold = _sold_pct(position)
    if sold < 0.20:
        return None
    last_trade = position.get("last_trade_at")
    if (position.get("last_trade_type") or "").upper() != "SELL" and position.get("last_action"):
        if "SELL" in str(position.get("last_action", "")).upper():
            last_trade = last_trade or position.get("last_trade_at")
    elapsed = _hours_since(last_trade, now)
    if elapsed is None or elapsed < idle_h:
        return None
    if not can_rotation_evict(market, position, cfg):
        return None
    return RotationSellCandidate(
        action=SELL_FULL,
        priority=4,
        source="tail_idle",
        rationale=f"Tail idle {elapsed:.0f}h sold={sold:.0%} gain={rotation_gain_pct(market):.1f}%",
    )


def _trail_tp_config(strategy_params: dict | None) -> dict:
    return dict((strategy_params or {}).get("trailing_take_profit") or {})


def _trail_tp_live_enabled(strategy_params: dict | None) -> bool:
    trail_cfg = _trail_tp_config(strategy_params)
    if not trail_cfg.get("enabled", False):
        return False
    mode = str(trail_cfg.get("mode", "live")).strip().lower()
    return mode not in ("off", "disabled", "shadow")


def _peak_gain_pct(market: MarketContext, position: dict) -> float:
    entry = market.average_entry
    if entry <= 0:
        return 0.0
    recent_high = float(position.get("recent_high") or 0) or market.current_price
    return (recent_high / entry - 1) * 100


def trail_replacement_armed(
    strategy_params: dict | None,
    market: MarketContext,
    position: dict,
) -> bool:
    """True when live trailing_take_profit is configured and peak gain reached arm threshold."""
    if not _trail_tp_live_enabled(strategy_params):
        return False
    arm_gain = float(_trail_tp_config(strategy_params).get("arm_gain_pct", 15.0))
    return _peak_gain_pct(market, position) >= arm_gain


def filter_trail_exclusive(
    candidates: list[tuple],
    market: MarketContext,
    position: dict,
    cfg: dict,
    strategy_params: dict | None = None,
) -> tuple[list[tuple], list[str]]:
    if not cfg.get("trail_exclusive", True):
        return candidates, []

    if not _trail_tp_live_enabled(strategy_params):
        return candidates, []

    if not trail_replacement_armed(strategy_params, market, position):
        return candidates, []

    gain = rotation_gain_pct(market)
    arm = float(cfg.get("arm_gain_pct", 12.0))
    blocked_labels: list[str] = []
    kept: list[tuple] = []

    for action, priority, source in candidates:
        src = (source or "").lower()
        if gain < arm and src in BLOCKED_BELOW_ARM:
            blocked_labels.append(source)
            continue
        if gain >= arm and src in BLOCKED_BELOW_ARM and src not in TRAIL_SOURCES:
            blocked_labels.append(source)
            continue
        if gain >= arm and cfg.get("trail_exit_full_close") and src in TRAIL_SOURCES:
            kept.append((SELL_FULL, max(priority, 5), source))
            continue
        kept.append((action, priority, source))

    return kept, blocked_labels


def filter_profit_full_close(
    candidates: list[tuple],
    market: MarketContext,
    position: dict,
    cfg: dict,
) -> list[tuple]:
    """Staging/test mode: close entire position on profit partial signals (no ladder tails)."""
    if not cfg.get("profit_exit_full_close"):
        return candidates
    if not can_rotation_evict(market, position, cfg):
        return candidates

    upgraded: list[tuple] = []
    for action, priority, source in candidates:
        src = (source or "").lower()
        if "stop" in src or action not in PROFIT_PARTIAL_ACTIONS:
            upgraded.append((action, priority, source))
            continue
        upgraded.append((SELL_FULL, max(priority, 5), source))
    return upgraded


def apply_rotation_sell_filters(
    candidates: list[tuple],
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
    config_raw: dict | None,
) -> tuple[list[tuple], SellPolicyAudit]:
    cfg = rotation_config(config_raw, strategy_params)
    audit = SellPolicyAudit()
    audit.tail_exempt = is_tail_position(position, cfg)

    filtered, blocked = filter_trail_exclusive(
        candidates, market, position, cfg, strategy_params=strategy_params,
    )
    audit.trail_exclusive_blocked = blocked
    filtered = filter_profit_full_close(filtered, market, position, cfg)

    for extra in (
        evaluate_ladder_terminal(market, position, strategy_params, cfg),
        evaluate_tail_idle_close(market, position, cfg),
    ):
        if extra:
            if extra.source == "ladder_terminal":
                audit.ladder_terminal_would_close = True
            if extra.source == "tail_idle":
                audit.tail_idle_would_close = True
            filtered.append((extra.action, extra.priority, extra.source))

    out: list[tuple] = []
    for action, priority, source in filtered:
        if is_sell(action) and not can_rotation_evict(market, position, cfg):
            if source in ("ladder_terminal", "tail_idle", "bb_upper", "vol_exhaustion", "vol_dump"):
                audit.rotation_blocked = True
                continue
            if source not in ("x_stop_loss", "technical") and "stop" not in (source or "").lower():
                if action != SELL_FULL or source in ("ladder_terminal", "tail_idle"):
                    audit.rotation_blocked = True
                    continue
        out.append((action, priority, source))

    if out:
        best = max(out, key=lambda c: c[1])
        audit.would_sell = best[0]
        audit.would_source = best[2]

    return out, audit


def audit_to_dict(audit: SellPolicyAudit) -> dict:
    return {
        "rotation_blocked": audit.rotation_blocked,
        "tail_exempt": audit.tail_exempt,
        "ladder_terminal_would_close": audit.ladder_terminal_would_close,
        "tail_idle_would_close": audit.tail_idle_would_close,
        "trail_exclusive_blocked": list(audit.trail_exclusive_blocked),
        "would_sell": audit.would_sell,
        "would_source": audit.would_source,
    }