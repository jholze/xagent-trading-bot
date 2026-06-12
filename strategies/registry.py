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
        if coin.get("source") == "cmc_trending" and is_dry_run_enhanced():
            defaults = dict(cfg.dry_run_defaults)
            defaults.update({"symbol": symbol, "timeframe": tf})
            merged["strategy_params"] = defaults
        else:
            params = cfg.strategy_params(symbol, tf)
            if params:
                merged["strategy_params"] = params

    return merged


def list_registered_strategies() -> list:
    return list(_load_registry().keys())


def get_strategy(coin: dict) -> BaseStrategy:
    coin = resolve_coin_config(coin)
    registry = _load_registry()
    strategy_class = coin.get("strategy_class", "technical_rsi_bb")
    cls = registry.get(strategy_class)
    if cls is None:
        from strategies.technical_rsi_bb import TechnicalRSIStrategy
        cls = TechnicalRSIStrategy
    return cls()


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