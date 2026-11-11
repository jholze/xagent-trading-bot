"""Resolve a lot's strategy tier at buy time (#711).

Before #711 the tier was frozen lazily: ``DecisionEngine.build_market_context``
called ``lock_strategy_tier`` only on a later cycle that analysed the open lot.
Until then ``pos["strategy_tier"]`` was null, so the WS fast-exit feed resolved
params with a live tier while the cycle froze a (possibly different) one, and
the long market-cap floor read the tier from a lot that did not exist yet
(always ``volatile`` -> 5M, never the 20M ``stable`` floor).

This module gives the buy path one place to name the tier:

* ``normalize_tier``: only ``stable`` / ``volatile`` are valid; anything else is None.
* ``classify_entry_tier``: the same ``volatility_tier`` classifier the cycle
  uses. Without an ATR it only answers the cases the classifier decides by
  coin class (large cap -> stable, meme / micro cap -> volatile), else None.
* ``entry_tier_for_order``: order hint first, then the class shortcut.
* ``resolve_entry_tier_live``: fetch indicators for the buy timeframe and
  classify like the cycle. Only for buy paths that ran no analysis (RelVol).

None means "unknown". Callers must not turn it into ``volatile`` silently;
the mcap floor keeps the legacy volatile floor for None but logs it.
"""
from __future__ import annotations

from typing import Any

VALID_TIERS = ("stable", "volatile")


def normalize_tier(tier: Any) -> str | None:
    t = str(tier or "").strip().lower()
    return t if t in VALID_TIERS else None


def _va_cfg(config=None) -> dict:
    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config()
        cfg = getattr(config, "volatile_altcoin_config", None)
        if isinstance(cfg, dict):
            return cfg
        raw = getattr(config, "raw", config)
        if isinstance(raw, dict):
            return dict(raw.get("volatile_altcoin") or {})
    except Exception:
        pass
    return {}


def freeze_on_entry_enabled(config=None) -> bool:
    """Same switch the cycle's lazy lock honours (default on)."""
    return bool(_va_cfg(config).get("freeze_tier_on_entry", True))


def classify_entry_tier(
    symbol: str,
    *,
    atr_pct: float | None = None,
    range_24h_pct: float | None = None,
    change_24h_pct: float | None = None,
    coin: dict | None = None,
    config=None,
) -> str | None:
    from intelligence.strategy_backtest import classify_coin
    from intelligence.volatility_classifier import volatility_tier

    coin = dict(coin or {})
    coin.setdefault("symbol", symbol)
    va = _va_cfg(config)
    if atr_pct is None:
        cc = classify_coin(symbol, coin.get("strategy_params"))
        if cc == "large_cap":
            return "stable"
        if va.get("micro_cap_override", True) and coin.get("market_cap_tier") == "micro":
            return "volatile"
        if cc == "meme":
            return "volatile"
        return None
    try:
        atr = float(atr_pct)
    except (TypeError, ValueError):
        return None
    return normalize_tier(
        volatility_tier(
            coin,
            atr,
            va,
            range_24h_pct=range_24h_pct,
            change_24h_pct=change_24h_pct,
        )
    )


def tier_from_params(params: dict | None) -> str | None:
    if not isinstance(params, dict):
        return None
    return normalize_tier(params.get("volatility_tier"))


def entry_tier_for_order(order, *, config=None) -> str | None:
    """Tier for a new BUY: order hint, else coin-class shortcut, else None."""
    hint = normalize_tier(getattr(order, "strategy_tier", None))
    if hint:
        return hint
    try:
        return classify_entry_tier(str(getattr(order, "symbol", "") or ""), config=config)
    except Exception:
        return None


def resolve_entry_tier_live(
    symbol: str,
    timeframe: str,
    price: float,
    *,
    market=None,
    coin: dict | None = None,
    config=None,
) -> str | None:
    """Classify from fresh indicators on the buy timeframe. Never raises."""
    try:
        quick = classify_entry_tier(symbol, coin=coin, config=config)
        if quick:
            return quick
        if market is None:
            from services.market_service import MarketService

            market = MarketService()
        _df, ind = market.fetch_ohlcv_and_indicators(symbol, timeframe, float(price))
        ind = ind or {}
        atr = ind.get("atr_pct")
        if atr is None:
            return None
        return classify_entry_tier(
            symbol,
            atr_pct=float(atr),
            range_24h_pct=ind.get("range_24h_pct"),
            change_24h_pct=ind.get("change_24h_pct"),
            coin={**(coin or {}), "symbol": symbol, "timeframe": timeframe},
            config=config,
        )
    except Exception as exc:
        try:
            from logger import log

            log(f"entry tier live resolve failed {symbol} {timeframe}: {exc}", "WARNING")
        except Exception:
            pass
        return None
