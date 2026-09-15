"""Single runtime truth for simulated live trading (no real exchange orders)."""

from __future__ import annotations

from core.execution_mode import places_real_orders
from data_manager import get_config, is_demo_mode, is_dry_run_enhanced, is_live_dry_run, resolve_ledger_scope


def is_simulated_trading(config: dict | None = None) -> bool:
    """True when execution uses the local order ledger, not Gate mainnet.

    Live follows ``core.execution_mode`` (#410): shadow and testnet are both
    simulated for accounting; only a resolved ``real`` is not.
    """
    cfg = config or get_config()
    if is_demo_mode():
        return True
    mode = cfg.get("trading_mode", "paper")
    if mode == "live":
        return is_live_dry_run(cfg)
    if mode == "paper":
        return bool(cfg.get("virtual_trading", True))
    return False


def simulated_ledger_scope(trading_mode: str | None = None, config: dict | None = None) -> str:
    """Active ledger scope for simulated trading (preserves demo Mongo history on staging)."""
    return resolve_ledger_scope(trading_mode)


def uses_order_ledger_cash(config: dict | None = None) -> bool:
    """Cash replayed from filled orders (staging/demo). Trade-history dry-run uses live trades."""
    cfg = config or get_config()
    if is_demo_mode():
        return True
    if is_dry_run_enhanced(cfg):
        return False
    return is_simulated_trading(cfg)


def uses_simulated_portfolio(config: dict | None = None) -> bool:
    """Portfolio uses local ledger balances, not Gate spot wallet."""
    return is_simulated_trading(config)


def is_real_live_trading(config: dict | None = None) -> bool:
    """True only when live execution resolves to ``real`` — real Gate orders.

    ≡ ``resolve_execution_mode(cfg).places_real_orders`` (#410): shadow and
    testnet are False even with ``dry_run: false``. ``live_confirmed`` stays a
    hard requirement (it is one of the real guards).
    """
    cfg = config or get_config()
    if cfg.get("trading_mode") != "live":
        return False
    if not cfg.get("live_confirmed"):
        return False
    return places_real_orders(cfg)


def simulated_live_config_updates(config: dict | None = None) -> dict:
    """Config patch: executable simulated live (dry-run ledger, no Mainnet).

    Returns **only the keys that change**. ``live`` carries just ``dry_run`` —
    the merged ``live.*`` block (operator ``execution``, ``max_usdt_per_trade``,
    …) must not be copied in, or ``patch_config`` would freeze the operator
    baseline into the tenant body (#456). ``deep_merge_dicts`` on both persist
    paths keeps the untouched ``live.*`` siblings.

    ``config`` is accepted for call-site compatibility; the patch does not
    depend on the current config.
    """
    del config  # unused since #456 — the patch is constant
    return {
        "trading_mode": "live",
        "virtual_trading": False,
        "live_confirmed": True,
        "live": {"dry_run": True},
    }