"""Single runtime truth for simulated live trading (no real exchange orders)."""

from __future__ import annotations

from data_manager import get_config, is_demo_mode, is_live_dry_run, resolve_ledger_scope


def is_simulated_trading(config: dict | None = None) -> bool:
    """True when execution uses the local order ledger, not Gate mainnet."""
    cfg = config or get_config()
    if is_demo_mode():
        return True
    mode = cfg.get("trading_mode", "paper")
    if mode == "live":
        return bool(cfg.get("live", {}).get("dry_run", True))
    if mode == "paper":
        return bool(cfg.get("virtual_trading", True))
    return False


def simulated_ledger_scope(trading_mode: str | None = None, config: dict | None = None) -> str:
    """Active ledger scope for simulated trading (preserves demo Mongo history on staging)."""
    return resolve_ledger_scope(trading_mode)


def uses_order_ledger_cash(config: dict | None = None) -> bool:
    """Cash for portfolio/NAV must be replayed from filled orders, not stale trade_history."""
    return is_simulated_trading(config)


def uses_simulated_portfolio(config: dict | None = None) -> bool:
    """Portfolio uses local ledger balances, not Gate spot wallet."""
    return is_simulated_trading(config)