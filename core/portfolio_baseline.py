"""Single source for portfolio starting capital across display, risk, and NAV."""

from __future__ import annotations

from data_manager import (
    live_sim_initial_capital,
    uses_exchange_ledger,
    uses_simulated_live_portfolio,
)


def initial_capital(
    scope: str = None,
    config: dict = None,
    history: dict = None,
    *,
    trading_mode: str = None,
) -> float:
    """Baseline USDT for PnL and cash replay for the active ledger scope."""
    from data_manager import get_config, resolve_ledger_scope

    cfg = config or get_config()
    mode = trading_mode or cfg.get("trading_mode", "paper")
    hist = history or {}
    if uses_exchange_ledger(mode) or uses_simulated_live_portfolio(cfg):
        return live_sim_initial_capital(cfg)
    trades = hist.get("trades") or []
    if any(t.get("mode") == "live" for t in trades):
        return live_sim_initial_capital(cfg)
    resolved = scope or resolve_ledger_scope(mode)
    if resolved == "demo":
        return live_sim_initial_capital(cfg)
    paper = (cfg.get("paper") or {}).get("initial_capital_usdt")
    if paper:
        return float(paper)
    return float(cfg.get("initial_capital_usdt", 5000))