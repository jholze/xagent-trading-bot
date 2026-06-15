"""Classify coins as stable vs volatile for strategy profile selection."""

from __future__ import annotations

from intelligence.strategy_backtest import classify_coin


def volatility_tier(
    coin: dict,
    atr_pct: float,
    volatile_config: dict | None = None,
    frozen_tier: str | None = None,
) -> str:
    """
    Return ``stable`` or ``volatile``.

    When ``frozen_tier`` is set (position entry lock), that value wins.
    """
    cfg = volatile_config or {}
    if frozen_tier in ("stable", "volatile"):
        return frozen_tier

    symbol = coin.get("symbol", "")
    coin_class = classify_coin(symbol, coin.get("strategy_params"))
    if coin_class == "large_cap":
        return "stable"

    if cfg.get("micro_cap_override", True) and coin.get("market_cap_tier") == "micro":
        return "volatile"

    if coin_class == "meme":
        return "volatile"

    enter = float(cfg.get("atr_volatile_enter_pct", 5.0))
    exit_pct = float(cfg.get("atr_stable_exit_pct", 3.5))

    if atr_pct >= enter:
        return "volatile"
    if atr_pct < exit_pct:
        return "stable"

    prev = coin.get("_volatility_tier_prev")
    if prev in ("stable", "volatile"):
        return prev
    return "stable"
