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
    from core.simulated_trading import is_simulated_trading

    if is_simulated_trading(cfg):
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


def nav_total_pnl(total_value: float, initial_capital: float) -> float:
    """Portfolio PnL from NAV: cash + marked positions minus starting capital."""
    return float(total_value) - float(initial_capital)


def portfolio_pnl_for_display(
    total_value: float,
    initial_capital: float,
    trade_realized: float,
    open_lots_mtm: float,
) -> dict[str, float]:
    """Headline PnL from NAV; trade_realized from sells; open_lots_mtm from live MTM.

    nav_residual = total_pnl - trade_realized - open_lots_mtm captures reinvested
    sell proceeds and other NAV effects not visible in sell.pnl or open-lot MTM.
    """
    total_pnl = nav_total_pnl(total_value, initial_capital)
    trade = float(trade_realized or 0)
    mtm = float(open_lots_mtm or 0)
    nav_residual = total_pnl - trade - mtm
    pct = (total_pnl / float(initial_capital) * 100.0) if initial_capital > 0 else 0.0
    return {
        "total_pnl": total_pnl,
        "unrealized": mtm,
        "open_lots_mtm": mtm,
        "trade_realized": trade,
        "nav_residual": nav_residual,
        "pnl_pct": pct,
    }


def split_nav_pnl_for_display(
    total_value: float,
    initial_capital: float,
    trade_realized: float = 0.0,
    *,
    unrealized: float | None = None,
) -> dict[str, float]:
    """Backward-compatible alias — pass trade_realized from ledger order replay."""
    pnl = portfolio_pnl_for_display(
        total_value,
        initial_capital,
        trade_realized,
        float(unrealized or 0) if unrealized is not None else 0.0,
    )
    return {
        **pnl,
        "realized": pnl["trade_realized"],
    }