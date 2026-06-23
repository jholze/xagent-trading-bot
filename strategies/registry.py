from core.config import get_bot_config
from data_manager import is_dry_run_enhanced
from strategies.base import BaseStrategy

_STRATEGY_CLASSES = {}


def _load_registry():
    if _STRATEGY_CLASSES:
        return _STRATEGY_CLASSES
    from strategies.technical_rsi_bb import TechnicalRSIStrategy
    _STRATEGY_CLASSES["technical_rsi_bb"] = TechnicalRSIStrategy
    return _STRATEGY_CLASSES




def _explicit_strategy_entry(symbol: str, tf: str) -> dict | None:
    cfg = get_bot_config()
    for entry in cfg.raw.get("strategies", []):
        if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == tf:
            return entry
    return None


_BUY_PARAM_KEYS = (
    "volume_multiplier",
    "reversal_volume_multiplier",
    "buy_regime",
    "rsi_buy_low",
    "rsi_buy_high",
    "reversal_rsi_cross_low",
    "reversal_rsi_cross_high",
)


from strategies.sell_profile import apply_position_sell_overlay


def _hermes_memory_params(symbol: str, tf: str) -> dict | None:
    """Load per-coin params Hermes learned (survives sell/rebuy cycles)."""
    try:
        from hermes.memory import store

        profile = store.load_profile(symbol, tf)
    except Exception:
        return None

    params = dict(profile.get("params") or {})
    if not params:
        return None

    params.update({
        "symbol": symbol,
        "timeframe": tf,
        "strategy_profile": "hermes_baseline",
        "hermes_baseline_updated_at": profile.get("updated_at"),
    })
    return params


def _resolve_volatility_tier(
    coin: dict,
    atr_pct: float,
    va_cfg: dict,
    frozen_tier: str | None = None,
) -> str | None:
    if not va_cfg.get("enabled", False):
        return None
    from intelligence.volatility_classifier import volatility_tier

    return volatility_tier(coin, atr_pct, va_cfg, frozen_tier=frozen_tier)


def _has_open_position(symbol: str, timeframe: str) -> bool:
    from strategies.positions import get_position, is_open_position

    return is_open_position(get_position(symbol, timeframe))


def resolve_effective_timeframe(
    coin: dict,
    atr_pct: float | None = None,
    frozen_tier: str | None = None,
) -> str:
    """Pick analysis timeframe: legacy positions keep their TF; volatile coins use 1h."""
    cfg = get_bot_config()
    symbol = coin.get("symbol", "")
    watchlist_tf = coin.get("timeframe", "4h")
    va_cfg = cfg.volatile_altcoin_config
    volatile_tf = str(va_cfg.get("timeframe") or "").strip()

    from intelligence.strategy_backtest import classify_coin

    coin_class = classify_coin(symbol, coin.get("strategy_params"))
    if coin_class == "large_cap":
        return watchlist_tf

    candidate_tfs = [watchlist_tf]
    if volatile_tf:
        candidate_tfs.extend([volatile_tf, "4h", "1h"])
    else:
        candidate_tfs.extend(["4h", "1h"])
    for tf in dict.fromkeys(candidate_tfs):
        if tf and _has_open_position(symbol, tf):
            return tf

    if not va_cfg.get("enabled", False) or not volatile_tf:
        return watchlist_tf

    if coin_class == "meme":
        return volatile_tf
    if va_cfg.get("micro_cap_override", True) and coin.get("market_cap_tier") == "micro":
        return volatile_tf

    volatile_sources = ("cmc_trending", "dry_run_expansion")
    if coin.get("source") in volatile_sources:
        if atr_pct is None:
            return volatile_tf
        tier = _resolve_volatility_tier(coin, atr_pct, va_cfg, frozen_tier=frozen_tier)
        if tier == "volatile":
            return volatile_tf
        return watchlist_tf

    if atr_pct is not None:
        tier = _resolve_volatility_tier(coin, atr_pct, va_cfg, frozen_tier=frozen_tier)
        if tier == "volatile":
            return volatile_tf

    return watchlist_tf


def _buy_profile_source(tier: str, coin: dict, cfg) -> dict:
    from intelligence.strategy_backtest import classify_coin

    raw = cfg.raw
    if tier == "volatile":
        return cfg.volatile_altcoin_config
    coin_class = classify_coin(coin.get("symbol", ""), coin.get("strategy_params"))
    if coin_class == "large_cap":
        stable = raw.get("stable_altcoin", {})
        return stable if stable.get("enabled", True) else {}
    if coin_class == "meme":
        return cfg.volatile_altcoin_config
    mid = raw.get("mid_cap_defaults", {})
    return mid if mid else raw.get("altcoin_social", {})


def _buy_profile_overlay(base: dict, coin: dict, tier: str | None, cfg) -> dict:
    if not tier:
        return base
    source = _buy_profile_source(tier, coin, cfg)
    if not source:
        return base
    merged = dict(base)
    for key in _BUY_PARAM_KEYS:
        if key in source:
            merged[key] = source[key]
    if "dca" in source:
        merged["dca"] = source["dca"]
    merged["volatility_tier"] = tier
    return merged


def _pure_volatile_profile(va_cfg: dict, tier: str, symbol: str, tf: str, cfg) -> dict:
    profile = dict(va_cfg)
    if is_dry_run_enhanced():
        profile.update(cfg.dry_run_defaults)
    profile.update({
        "symbol": symbol,
        "timeframe": tf,
        "strategy_profile": "volatile_altcoin",
        "volatility_tier": tier,
    })
    if profile.get("take_profit_pct") is None:
        profile.pop("take_profit_pct", None)
    return profile


def resolve_strategy_params(
    coin: dict,
    has_position: bool = False,
    atr_pct: float = 3.0,
    frozen_tier: str | None = None,
) -> dict:
    """Pick strategy params: strategies[] > Hermes memory > volatile > altcoin_social > defaults."""
    cfg = get_bot_config()
    symbol = coin.get("symbol", "")
    tf = coin.get("timeframe", "4h")

    explicit = _explicit_strategy_entry(symbol, tf)
    if explicit:
        return dict(explicit)

    hermes_params = _hermes_memory_params(symbol, tf)
    va_cfg = cfg.volatile_altcoin_config
    tier = _resolve_volatility_tier(coin, atr_pct, va_cfg, frozen_tier=frozen_tier)
    volatile_active = has_position and tier == "volatile"

    stable_cfg = cfg.stable_altcoin_config

    if hermes_params:
        base = _buy_profile_overlay(hermes_params, coin, tier, cfg)
        return apply_position_sell_overlay(
            base,
            tier=tier,
            has_position=has_position,
            symbol=symbol,
            tf=tf,
            volatile_cfg=va_cfg,
            stable_cfg=stable_cfg,
            cfg=cfg,
        )

    if volatile_active:
        return _pure_volatile_profile(va_cfg, tier, symbol, tf, cfg)

    if coin.get("source") == "cmc_trending" and tier == "volatile":
        return _pure_volatile_profile(va_cfg, tier, symbol, tf, cfg)

    if coin.get("source") == "cmc_trending" or coin.get("market_cap_tier") == "micro":
        profile = dict(cfg.altcoin_social_config)
        if is_dry_run_enhanced():
            profile.update(cfg.dry_run_defaults)
        profile.update({"symbol": symbol, "timeframe": tf})
        return _buy_profile_overlay(profile, coin, tier, cfg)

    params = cfg.strategy_params(symbol, tf)
    base = dict(params) if params else {}
    base = _buy_profile_overlay(base, coin, tier, cfg)
    return apply_position_sell_overlay(
        base,
        tier=tier,
        has_position=has_position,
        symbol=symbol,
        tf=tf,
        volatile_cfg=va_cfg,
        stable_cfg=stable_cfg,
        cfg=cfg,
    )

def resolve_coin_config(coin: dict) -> dict:
    """Merge watchlist coin with matching config.strategies[] entry."""
    cfg = get_bot_config()
    symbol = coin.get("symbol", "")
    tf = coin.get("timeframe", "4h")
    merged = dict(coin)

    for entry in cfg.raw.get("strategies", []):
        if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == tf:
            if entry.get("live_enabled") is False and cfg.trading_mode == "live":
                continue
            merged["timeframe"] = entry.get("timeframe", tf)
            merged["strategy_class"] = entry.get("strategy_class", "technical_rsi_bb")
            merged["strategy_params"] = entry
            break
    else:
        merged.setdefault("strategy_class", "technical_rsi_bb")
        merged["strategy_params"] = resolve_strategy_params(coin, has_position=False)

    return merged


def list_registered_strategies() -> list:
    return list(_load_registry().keys())


def get_strategy(coin: dict) -> BaseStrategy:
    preset_params = coin.get("strategy_params")
    coin = resolve_coin_config(coin)
    if preset_params and preset_params.get("strategy_profile"):
        coin["strategy_params"] = preset_params
    registry = _load_registry()
    strategy_class = coin.get("strategy_class", "technical_rsi_bb")
    cls = registry.get(strategy_class)
    if cls is None:
        from strategies.technical_rsi_bb import TechnicalRSIStrategy
        cls = TechnicalRSIStrategy
    return cls()


def sync_hermes_baseline_to_config(baseline: dict, experiment_id: str = "") -> tuple:
    """Patch config.strategies[] with Hermes baseline params for symbol/timeframe."""
    from data_manager import get_config, reload_config, save_config

    symbol = baseline.get("symbol")
    timeframe = baseline.get("timeframe", "4h")
    params = baseline.get("params") or {}
    if not symbol or not params:
        return False, "Baseline missing symbol or params"

    cfg = get_config()
    strategies = cfg.setdefault("strategies", [])
    updated = False
    for entry in strategies:
        if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == timeframe:
            for key, value in params.items():
                entry[key] = value
            entry["hermes_experiment_id"] = experiment_id
            entry["hermes_updated_at"] = baseline.get("updated_at")
            entry["description"] = entry.get("description") or f"Hermes-tuned {symbol} {timeframe}"
            updated = True
            break

    if not updated:
        new_entry = dict(params)
        new_entry.update({
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_class": "technical_rsi_bb",
            "description": f"Hermes-tuned {symbol} {timeframe}",
            "hermes_experiment_id": experiment_id,
            "hermes_updated_at": baseline.get("updated_at"),
        })
        strategies.append(new_entry)

    if save_config(cfg):
        reload_config()
        return True, f"Hermes baseline synced to config.strategies for {symbol} {timeframe}"
    return False, "Failed to save config.json"


def promote_hypothesis_to_config(hypothesis: dict) -> tuple:
    """Promote a sandbox hypothesis into config.strategies[]."""
    from data_manager import get_config, save_config

    symbol = hypothesis.get("symbol")
    if not symbol:
        return False, "Hypothesis has no symbol — assign one before promotion"

    cfg = get_config()
    strategies = cfg.setdefault("strategies", [])
    tf = hypothesis.get("timeframe", "4h")
    for entry in strategies:
        if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == tf:
            if entry.get("sandbox_id") == hypothesis.get("id"):
                return True, "Already promoted"
            return False, f"Strategy already exists for {symbol} {tf}"

    params = dict(hypothesis.get("params") or {})
    params.update({
        "symbol": symbol,
        "timeframe": tf,
        "strategy_class": "technical_rsi_bb",
        "description": f"Promoted from sandbox: {hypothesis.get('name', '')}",
        "sandbox_id": hypothesis.get("id"),
        "source_account": hypothesis.get("source_account"),
    })
    strategies.append(params)
    if save_config(cfg):
        return True, f"Added {symbol} ({tf}) to strategies"
    return False, "Failed to save config.json"