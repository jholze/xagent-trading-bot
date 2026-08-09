"""Recovery hold — block accidental auto exits on sniper/focus recovery bags (#223).

While ``recovery_hold`` or ``sniper_focus`` is set on a position (and enforce is on):
- Block trail / TTP / partial / BB / social-class auto sells
- Allow hard full stop-loss and manual sells
- Auto-promote (clear hold) when mark ≥ avg × (1 + be_buffer) default +2%

Peak epoch: after DCA, ``peak_epoch_high`` = max(fill, avg). Stale pre-DCA
``recent_high`` is clamped down at stamp time (not on every trail tick).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Iterable

# Auto-sell sources blocked under recovery hold (exact match)
_BLOCKED_SOURCES = frozenset(
    {
        "trailing_stop",
        "trailing_take_profit",
        "trailing_shadow",
        "trailing_take_profit_shadow",
        "bb_upper",
        "grid",
        "grid_tp",
        "partial_stop",
        "partial_sl",
        "exit_ladder",
        "rsi_sell",
        "time_profit_exit",
        "time_profit_shadow",
        "profit_max_lifetime",
        "profit_max_lifetime_shadow",
        "exit_sensor",
        "exit_sensor_shadow",
        "cmc",
        "lc",
        "x",
        "x_take_profit",
        "market_structure",
        "social",
        "technical",  # non-SL technical sells (BB etc.) — hard SL uses source stop_loss
    }
)

# Always allowed even under hold
_ALLOWED_SOURCES = frozenset(
    {
        "stop_loss",
        "hard_stop",
        "hard_sl",
        "x_stop_loss",
        "manual",
        "manual_sell",
    }
)

_BLOCKED_PREFIXES = (
    "trailing_",
    "exit_sensor",
    "bb_",
    "grid_",
    "partial_",
    "cmc",
    "lc_",
    "social",
    "structure_",
    "time_profit",
    "profit_max",
)


def recovery_hold_config(
    strategy_params: dict | None = None,
    config_raw: dict | None = None,
) -> dict:
    """Merge global + strategy recovery_hold config with defaults."""
    defaults = {
        "enforce": True,
        "be_buffer_pct": 2.0,
        "timeout_days": 14.0,
        "set_on_dca": False,  # sniper sets hold explicitly; optional auto on any DCA
        "stamp_peak_epoch_on_dca": True,
    }
    raw: dict = {}
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    if isinstance(config_raw, dict):
        rh = config_raw.get("recovery_hold")
        if isinstance(rh, dict):
            raw.update(rh)
        va = config_raw.get("volatile_altcoin")
        if isinstance(va, dict):
            vrh = va.get("recovery_hold")
            if isinstance(vrh, dict):
                raw.update(vrh)
    if strategy_params:
        srh = (strategy_params or {}).get("recovery_hold")
        if isinstance(srh, dict):
            raw.update(srh)
        dca = (strategy_params or {}).get("dca")
        if isinstance(dca, dict):
            drh = dca.get("recovery_hold")
            if isinstance(drh, dict):
                raw.update(drh)
    out = {**defaults, **raw}
    env = os.environ.get("RECOVERY_HOLD_ENFORCE", "").strip().lower()
    if env in ("0", "false", "off", "no"):
        out["enforce"] = False
    elif env in ("1", "true", "on", "yes"):
        out["enforce"] = True
    return out


def _flags_set(position: dict | None) -> bool:
    pos = position or {}
    return bool(pos.get("recovery_hold") or pos.get("sniper_focus"))


def is_recovery_hold_active(
    position: dict | None,
    *,
    enforce: bool | None = None,
    strategy_params: dict | None = None,
    config_raw: dict | None = None,
) -> bool:
    """True if position is under recovery/sniper focus hold and enforce is on.

    - ``enforce=False`` → always inactive (kill)
    - ``enforce=True`` → only position flags (caller already checked config)
    - ``enforce=None`` → load config enforce + flags
    """
    if enforce is False:
        return False
    if not _flags_set(position):
        return False
    if enforce is True:
        return True
    cfg = recovery_hold_config(strategy_params, config_raw)
    return bool(cfg.get("enforce", True))


def source_allowed_under_recovery_hold(source: str) -> bool:
    """Return True if this sell source may fire while hold is active.

    Default-deny for unknown sources (fail-closed under hold).
    Hard full SL must be labeled ``stop_loss`` (see decision_engine).
    """
    src = str(source or "").strip().lower()
    if not src:
        return False
    if src in _ALLOWED_SOURCES:
        return True
    # e.g. stop_loss_full, hard_stop_loss — but not partial_stop_loss
    if "stop_loss" in src and "partial" not in src:
        return True
    if src in _BLOCKED_SOURCES:
        return False
    for p in _BLOCKED_PREFIXES:
        if src.startswith(p):
            return False
    return False


def filter_sell_candidates_for_recovery_hold(
    candidates: list,
    position: dict | None,
    *,
    strategy_params: dict | None = None,
    config_raw: dict | None = None,
) -> tuple[list, list[str]]:
    """Drop auto-sell candidates blocked by recovery hold.

    ``candidates`` items are ``(action, priority, source)`` triples.
    Returns (filtered_candidates, blocked_source_labels).
    """
    cfg = recovery_hold_config(strategy_params, config_raw)
    if not cfg.get("enforce", True) or not _flags_set(position):
        return list(candidates or []), []

    kept: list = []
    blocked: list[str] = []
    for item in candidates or []:
        if not item or len(item) < 3:
            kept.append(item)
            continue
        source = str(item[2] or "")
        if source_allowed_under_recovery_hold(source):
            kept.append(item)
        else:
            blocked.append(source)
    return kept, blocked


def maybe_promote_recovery_hold(
    position: dict,
    mark_price: float,
    *,
    strategy_params: dict | None = None,
    config_raw: dict | None = None,
    now: datetime | None = None,
) -> bool:
    """Clear recovery_hold/sniper_focus when mark ≥ avg × (1 + be_buffer).

    Runs whenever flags are set (even if enforce is off) so BE+ can clean ledger.
    Returns True if hold was cleared.
    """
    if not position or not _flags_set(position):
        return False

    cfg = recovery_hold_config(strategy_params, config_raw)
    try:
        avg = float(position.get("average_entry") or 0)
        px = float(mark_price or 0)
    except (TypeError, ValueError):
        return False
    if avg <= 0 or px <= 0:
        return False

    try:
        be_buf = float(cfg.get("be_buffer_pct") or 2.0) / 100.0
    except (TypeError, ValueError):
        be_buf = 0.02
    threshold = avg * (1.0 + max(0.0, be_buf))
    if px + 1e-12 < threshold:
        return False

    position["recovery_hold"] = False
    position["sniper_focus"] = False
    position["recovery_hold_cleared_at"] = (now or datetime.now()).isoformat()
    position["recovery_hold_clear_reason"] = "be_plus"
    if not position.get("exit_state"):
        position["exit_state"] = "RUNNER"
    return True


def stamp_peak_epoch_on_dca(position: dict, fill_price: float) -> float:
    """Set peak_epoch_high = max(fill, avg); clamp stale pre-DCA recent_high down."""
    try:
        avg = float(position.get("average_entry") or 0)
        px = float(fill_price or 0)
    except (TypeError, ValueError):
        avg, px = 0.0, 0.0
    if avg <= 0 and px <= 0:
        try:
            return float(position.get("peak_epoch_high") or position.get("recent_high") or 0)
        except (TypeError, ValueError):
            return 0.0
    if avg <= 0:
        epoch = px
    elif px <= 0:
        epoch = avg
    else:
        epoch = max(px, avg)
    epoch = float(epoch)
    position["peak_epoch_high"] = epoch
    # Hard reset trail peak into post-DCA world (clamp stale high AND lift low peaks)
    position["recent_high"] = epoch
    position["peak_epoch_at"] = datetime.now().isoformat()
    return epoch


def set_recovery_hold(
    position: dict,
    *,
    sniper_focus: bool = True,
    heavy: bool = False,
    now: datetime | None = None,
) -> None:
    """Mark position as recovery hold (sniper or policy)."""
    ts = (now or datetime.now()).isoformat()
    position["recovery_hold"] = True
    position["sniper_focus"] = bool(sniper_focus)
    position["recovery_entered_at"] = position.get("recovery_entered_at") or ts
    position["last_recovery_hold_at"] = ts
    if heavy:
        position["dca_heavy_used"] = True


def auto_sells_blocked_reason(
    position: dict | None,
    source: str,
    *,
    strategy_params: dict | None = None,
    config_raw: dict | None = None,
) -> str | None:
    """If hold blocks this source, return reason string; else None."""
    if not is_recovery_hold_active(
        position,
        strategy_params=strategy_params,
        config_raw=config_raw,
    ):
        return None
    if source_allowed_under_recovery_hold(source):
        return None
    return f"recovery_hold_block:{source or 'unknown'}"


def any_hold_flags(positions: Iterable[dict] | None) -> int:
    """Count open recovery holds (metrics helper)."""
    n = 0
    for p in positions or []:
        if _flags_set(p):
            n += 1
    return n
