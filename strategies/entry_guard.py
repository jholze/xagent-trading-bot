"""Entry-aware sell guards — prevent 15m-entry whipsaws without blocking profitable exits."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from core.actions import is_sell
from strategies.sell_sources import (
    SOCIAL_SOURCES,
    STOP_SOURCES,
    STRUCTURE_SOURCES,
    TRAILING_SOURCES,
)


class Pump15mState(str, Enum):
    CONTINUATION = "continuation"
    EXHAUSTION = "exhaustion"
    NEUTRAL = "neutral"


def _parse_ts(iso_ts: str | None) -> datetime | None:
    if not iso_ts:
        return None
    try:
        return datetime.fromisoformat(str(iso_ts).replace("Z", ""))
    except Exception:
        return None


def _hours_since(iso_ts: str | None, as_of: datetime | None = None) -> float | None:
    last_ts = _parse_ts(iso_ts)
    if not last_ts:
        return None
    ref = as_of or datetime.now()
    if last_ts.tzinfo and ref.tzinfo is None:
        ref = ref.replace(tzinfo=last_ts.tzinfo)
    elif ref.tzinfo and last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=ref.tzinfo)
    return (ref - last_ts).total_seconds() / 3600.0


def _minutes_since(iso_ts: str | None, as_of: datetime | None = None) -> float | None:
    hours = _hours_since(iso_ts, as_of=as_of)
    return None if hours is None else hours * 60.0


def entry_guard_config(config: dict | None = None) -> dict:
    from core.config import get_bot_config

    if config is None:
        return get_bot_config().entry_guard_config
    raw = (config.get("entry_guard") or {})
    merged = {**get_bot_config().entry_guard_config, **raw}
    if raw.get("by_tier"):
        merged["by_tier"] = {
            **get_bot_config().entry_guard_config.get("by_tier", {}),
            **raw["by_tier"],
        }
    return merged


def _tier_params(position: dict, strategy_params: dict, cfg: dict) -> dict:
    tier = (
        position.get("strategy_tier")
        or strategy_params.get("volatility_tier")
        or "normal"
    )
    tier = str(tier).lower()
    by_tier = cfg.get("by_tier") or {}
    if tier in by_tier:
        return dict(by_tier[tier])
    if tier == "meme":
        return dict(by_tier.get("meme") or by_tier.get("volatile") or {})
    return dict(by_tier.get("normal") or {})


def is_guarded_entry(position: dict, cfg: dict | None = None) -> bool:
    cfg = cfg or entry_guard_config()
    if not cfg.get("enabled", True):
        return False
    sources = set(cfg.get("sources") or [])
    entry_source = str(position.get("entry_source") or "")
    return entry_source in sources


def is_fresh_guarded_entry(
    position: dict,
    cfg: dict | None = None,
    *,
    as_of: datetime | None = None,
) -> bool:
    cfg = cfg or entry_guard_config()
    if not is_guarded_entry(position, cfg):
        return False
    entry_at = position.get("entry_at") or position.get("first_buy_at")
    elapsed = _minutes_since(entry_at, as_of=as_of)
    window = float(cfg.get("fresh_entry_window_minutes", 120))
    return elapsed is not None and elapsed < window


def classify_15m_pump_state(
    metrics: dict | None,
    gain_pct: float,
    cfg: dict | None = None,
) -> Pump15mState:
    cfg = cfg or entry_guard_config()
    if not metrics:
        return Pump15mState.NEUTRAL

    vol_spike = float(metrics.get("volume_spike_ratio", 0) or 0)
    vol_mult = float(cfg.get("vol_spike_mult", 2.0))
    momentum = bool(metrics.get("price_momentum"))
    exhaustion_max = float(cfg.get("vol_exhaustion_15m_max", 0.85))
    exhaustion_min_gain = float(cfg.get("exhaustion_min_gain_pct", 5.0))

    if vol_spike >= vol_mult and momentum:
        return Pump15mState.CONTINUATION
    if gain_pct >= exhaustion_min_gain and vol_spike < exhaustion_max:
        return Pump15mState.EXHAUSTION
    return Pump15mState.NEUTRAL


def _is_stop_loss_source(sell_source: str, action: str) -> bool:
    src = (sell_source or "").lower()
    act = (action or "").upper()
    if src in STOP_SOURCES and "stop" in act:
        return True
    if "STOP" in act or act in ("SELL_FULL", "SELL_STOP_FULL", "SELL_STOP_PARTIAL"):
        return True
    if src == "x_stop_loss":
        return True
    return False


def entry_sell_allowed(
    *,
    position: dict,
    strategy_params: dict,
    sell_source: str,
    action: str,
    gain_pct: float,
    ta_bearish: bool,
    metrics_15m: dict | None = None,
    cfg: dict | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason). Empty reason when allowed."""
    cfg = cfg or entry_guard_config()
    if not cfg.get("enabled", True):
        return True, ""

    guarded = is_guarded_entry(position, cfg)
    fresh = is_fresh_guarded_entry(position, cfg, as_of=now)

    if _is_stop_loss_source(sell_source, action):
        return True, ""

    mega = float(cfg.get("mega_pump_gain_pct", 12.0))
    if gain_pct >= mega:
        return True, ""

    if ta_bearish and gain_pct < 0:
        return True, ""

    tier_cfg = _tier_params(position, strategy_params, cfg)
    min_hold = float(tier_cfg.get("min_hold_minutes", 60))
    min_gain = float(tier_cfg.get("min_gain_structure_pct", 10))

    entry_at = position.get("entry_at") or position.get("first_buy_at")
    elapsed_min = _minutes_since(entry_at, as_of=now)
    if elapsed_min is None:
        elapsed_min = 999.0

    src = (sell_source or "").lower()

    if guarded and not fresh and src in STRUCTURE_SOURCES:
        if gain_pct < min_gain and not ta_bearish:
            return False, f"gain {gain_pct:.1f}% < {min_gain:.0f}% (guarded entry)"
        return True, ""

    if not fresh:
        return True, ""

    pump = classify_15m_pump_state(metrics_15m, gain_pct, cfg)

    if src in STRUCTURE_SOURCES:
        if pump == Pump15mState.CONTINUATION:
            return False, f"15m pump continuation (vol spike, {elapsed_min:.0f}m)"
        if pump == Pump15mState.EXHAUSTION:
            return True, ""
        if elapsed_min < min_hold and gain_pct < min_gain and not ta_bearish:
            return False, f"min hold {min_hold:.0f}m / gain {gain_pct:.1f}% < {min_gain:.0f}%"
        if gain_pct < min_gain and not ta_bearish:
            return False, f"gain {gain_pct:.1f}% < {min_gain:.0f}% (no bearish TA)"
        return True, ""

    if src in TRAILING_SOURCES:
        block_loss_m = float(cfg.get("block_loss_sells_minutes", 15))
        if gain_pct < 0 and elapsed_min < block_loss_m:
            return False, f"loss sell blocked ({elapsed_min:.0f}m < {block_loss_m:.0f}m)"
        if pump == Pump15mState.CONTINUATION and gain_pct < mega:
            return False, "trailing blocked during 15m continuation"
        return True, ""

    if src in SOCIAL_SOURCES:
        if pump == Pump15mState.CONTINUATION and not ta_bearish:
            return False, "social sell blocked during 15m continuation"
        if elapsed_min < min_hold * 0.5 and not ta_bearish and gain_pct < min_gain:
            return False, f"social sell early ({elapsed_min:.0f}m)"
        return True, ""

    if src == "technical" and is_sell(action):
        if "STOP" in (action or "").upper():
            return True, ""
        if pump == Pump15mState.CONTINUATION and gain_pct < min_gain and not ta_bearish:
            return False, "TA partial blocked during 15m continuation"
        return True, ""

    return True, ""


def filter_sell_candidates(
    candidates: list[tuple],
    *,
    position: dict,
    strategy_params: dict,
    gain_pct: float,
    ta_bearish: bool,
    metrics_15m: dict | None = None,
    cfg: dict | None = None,
) -> tuple[list[tuple], list[str]]:
    """Filter (action, priority, source) sell candidates. Returns (kept, blocked rationales)."""
    if not candidates:
        return [], []

    cfg = cfg or entry_guard_config()
    kept: list[tuple] = []
    blocked: list[str] = []
    for action, priority, source in candidates:
        allowed, reason = entry_sell_allowed(
            position=position,
            strategy_params=strategy_params,
            sell_source=source,
            action=action,
            gain_pct=gain_pct,
            ta_bearish=ta_bearish,
            metrics_15m=metrics_15m,
            cfg=cfg,
        )
        if allowed:
            kept.append((action, priority, source))
        elif reason:
            blocked.append(f"EntryGuard->{source}: {reason}")
    return kept, blocked