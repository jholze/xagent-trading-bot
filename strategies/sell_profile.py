"""Merge tier-specific sell policy blocks from config onto resolved strategy params."""

from __future__ import annotations

from data_manager import is_dry_run_enhanced

BASE_SELL_KEYS = (
    "rsi_sell_mode",
    "rsi_sell_30",
    "rsi_sell_20",
    "rsi_sell_min_gain_pct",
    "take_profit_tiers",
    "take_profit_pct",
    "safety_tp_pct",
    "safety_tp_min_gain_pct",
    "exit_ladder",
    "trailing_stop",
)

VOLATILE_EXTRA_KEYS = (
    "mode",
    "bb_sell_enabled",
    "bb_sell_upper_ratio",
    "bb_sell_rsi_min",
    "vol_exhaustion_sell_enabled",
    "vol_exhaustion_max",
    "vol_exhaustion_rsi_min",
    "vol_exhaustion_min_gain_pct",
    "vol_dump_sell_enabled",
    "vol_dump_min_multiplier",
    "vol_dump_price_drop_pct",
    "vol_dump_requires_prior_gain_pct",
    "vol_buy_boost_enabled",
    "vol_buy_boost_min",
    "cmc_sell_requires_ta",
    "cmc_sell_min_confidence",
    "cmc_min_confidence",
    "cmc_min_hours_between_sells",
    "dca",
)


def _merge_keys(base: dict, source: dict, keys: tuple[str, ...]) -> dict:
    merged = dict(base)
    for key in keys:
        if key in source:
            merged[key] = source[key]
    return merged


def _strip_null_take_profit(merged: dict) -> dict:
    if merged.get("take_profit_pct") is None:
        merged.pop("take_profit_pct", None)
    return merged


def overlay_sell_keys(
    base: dict,
    sell_cfg: dict,
    *,
    tier: str,
    symbol: str,
    tf: str,
    keys: tuple[str, ...],
    strategy_profile: str,
) -> dict:
    merged = _merge_keys(base, sell_cfg, keys)
    merged.update({
        "symbol": symbol,
        "timeframe": tf,
        "volatility_tier": tier,
        "strategy_profile": strategy_profile,
    })
    return _strip_null_take_profit(merged)


def overlay_volatile_sell(
    base: dict,
    volatile_cfg: dict,
    *,
    tier: str,
    symbol: str,
    tf: str,
    cfg=None,
) -> dict:
    from core.config import get_bot_config

    keys = BASE_SELL_KEYS + VOLATILE_EXTRA_KEYS
    merged = overlay_sell_keys(
        base,
        volatile_cfg,
        tier=tier,
        symbol=symbol,
        tf=tf,
        keys=keys,
        strategy_profile="hermes_baseline+volatile",
    )
    if is_dry_run_enhanced():
        cfg = cfg or get_bot_config()
        for key in ("cmc_min_confidence", "cmc_sell_min_confidence", "cmc_min_hours_between_sells"):
            if key in cfg.dry_run_defaults:
                merged[key] = cfg.dry_run_defaults[key]
    return merged


def overlay_stable_sell(
    base: dict,
    stable_cfg: dict,
    *,
    tier: str,
    symbol: str,
    tf: str,
) -> dict:
    existing = base.get("strategy_profile")
    if existing and existing != "stable_altcoin":
        profile = f"{existing}+stable_sell"
    else:
        profile = "stable_altcoin"
    return overlay_sell_keys(
        base,
        stable_cfg,
        tier=tier,
        symbol=symbol,
        tf=tf,
        keys=BASE_SELL_KEYS,
        strategy_profile=profile,
    )


def apply_position_sell_overlay(
    base: dict,
    *,
    tier: str | None,
    has_position: bool,
    symbol: str,
    tf: str,
    volatile_cfg: dict,
    stable_cfg: dict,
    cfg=None,
) -> dict:
    if not has_position or not tier:
        return base
    if tier == "volatile":
        return overlay_volatile_sell(
            base, volatile_cfg, tier=tier, symbol=symbol, tf=tf, cfg=cfg,
        )
    if tier == "stable" and stable_cfg.get("enabled", True):
        return overlay_stable_sell(
            base, stable_cfg, tier=tier, symbol=symbol, tf=tf,
        )
    return base