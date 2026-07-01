"""Classify coins as stable vs volatile for strategy profile selection."""

from __future__ import annotations

from intelligence.strategy_backtest import classify_coin


def _tier_scoring_cfg(volatile_config: dict | None) -> dict:
    return dict((volatile_config or {}).get("tier_scoring") or {})


def _atr_points(atr_pct: float, bands: list[dict]) -> int:
    for band in sorted(bands, key=lambda b: float(b.get("min_pct", 0)), reverse=True):
        if atr_pct >= float(band.get("min_pct", 0)):
            return int(band.get("points", 0))
    return 0


def tier_score(
    coin: dict,
    atr_pct: float,
    volatile_config: dict | None = None,
    range_24h_pct: float | None = None,
    change_24h_pct: float | None = None,
) -> int:
    """Multi-factor volatility score (higher = more volatile)."""
    cfg = _tier_scoring_cfg(volatile_config)
    symbol = coin.get("symbol", "")
    coin_class = classify_coin(symbol, coin.get("strategy_params"))

    class_points = dict(cfg.get("class_points") or {})
    score = int(class_points.get(coin_class, 0))

    bands = list(cfg.get("atr_bands") or [])
    score += _atr_points(atr_pct, bands)

    source_points = dict(cfg.get("source_points") or {})
    source = coin.get("source")
    if source in source_points:
        score += int(source_points[source])

    if range_24h_pct is not None:
        min_range = float(cfg.get("range_24h_min_pct", 12.0))
        if range_24h_pct >= min_range:
            score += int(cfg.get("range_24h_points", 1))

    if change_24h_pct is not None:
        down_threshold = float(cfg.get("change_24h_down_pct", -5.0))
        if change_24h_pct <= down_threshold:
            score += int(cfg.get("change_24h_down_points", 1))

    return score


def _legacy_volatility_tier(
    coin: dict,
    atr_pct: float,
    cfg: dict,
) -> str:
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


def _scored_volatility_tier(
    coin: dict,
    atr_pct: float,
    cfg: dict,
    range_24h_pct: float | None = None,
    change_24h_pct: float | None = None,
) -> str:
    scoring = _tier_scoring_cfg(cfg)
    score = tier_score(coin, atr_pct, cfg, range_24h_pct, change_24h_pct)
    enter = int(scoring.get("volatile_enter_score", 4))
    exit_score = int(scoring.get("volatile_exit_score", 2))

    prev = coin.get("_volatility_tier_prev")
    if prev == "volatile":
        return "volatile" if score >= exit_score else "stable"
    if prev == "stable":
        return "volatile" if score >= enter else "stable"
    return "volatile" if score >= enter else "stable"


def volatility_tier(
    coin: dict,
    atr_pct: float,
    volatile_config: dict | None = None,
    frozen_tier: str | None = None,
    range_24h_pct: float | None = None,
    change_24h_pct: float | None = None,
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

    scoring = _tier_scoring_cfg(cfg)
    if scoring.get("enabled", False):
        return _scored_volatility_tier(
            coin, atr_pct, cfg, range_24h_pct, change_24h_pct
        )

    return _legacy_volatility_tier(coin, atr_pct, cfg)