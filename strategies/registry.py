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


def resolve_strategy_params(
    coin: dict,
    has_position: bool = False,
    atr_pct: float = 3.0,
    frozen_tier: str | None = None,
) -> dict:
    """Pick strategy params: explicit strategies[] > volatile_altcoin > altcoin_social > defaults."""
    cfg = get_bot_config()
    symbol = coin.get("symbol", "")
    tf = coin.get("timeframe", "4h")

    explicit = _explicit_strategy_entry(symbol, tf)
    if explicit:
        return dict(explicit)

    va_cfg = cfg.volatile_altcoin_config
    if has_position and va_cfg.get("enabled", False):
        from intelligence.volatility_classifier import volatility_tier

        tier = volatility_tier(coin, atr_pct, va_cfg, frozen_tier=frozen_tier)
        if tier == "volatile":
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

    if coin.get("source") == "cmc_trending" or coin.get("market_cap_tier") == "micro":
        profile = dict(cfg.altcoin_social_config)
        if is_dry_run_enhanced():
            profile.update(cfg.dry_run_defaults)
        profile.update({"symbol": symbol, "timeframe": tf})
        return profile

    params = cfg.strategy_params(symbol, tf)
    return dict(params) if params else {}

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
        if coin.get("source") == "cmc_trending" or coin.get("market_cap_tier") == "micro":
            profile = dict(cfg.altcoin_social_config)
            if is_dry_run_enhanced():
                profile.update(cfg.dry_run_defaults)
            profile.update({"symbol": symbol, "timeframe": tf})
            merged["strategy_params"] = profile
        else:
            params = cfg.strategy_params(symbol, tf)
            if params:
                merged["strategy_params"] = params

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