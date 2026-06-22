"""Exit ladder — structured partial sells with terminal full close."""

from __future__ import annotations

from strategies.positions import get_position

TERMINAL_SIGNALS = frozenset(
    {"SELL_STOP_FULL", "SELL_FULL", "SELL_STOP", "SELL_TRAIL_FULL"}
)


def ladder_config(strategy_params: dict | None) -> dict:
    params = strategy_params or {}
    cfg = dict(params.get("exit_ladder") or {})
    if cfg.get("enabled") is not False and params.get("strategy_profile") in (
        "volatile_altcoin",
        "hermes_baseline+volatile",
    ):
        if not cfg and params.get("exit_ladder_enabled"):
            cfg = {"enabled": True}
    tiers = cfg.get("tiers")
    if isinstance(tiers, list) and tiers:
        cfg.setdefault("enabled", True)
        cfg["tiers"] = [float(t) for t in tiers]
    return cfg


def ladder_enabled(strategy_params: dict | None) -> bool:
    cfg = ladder_config(strategy_params)
    return bool(cfg.get("enabled")) and bool(cfg.get("tiers"))


def is_terminal_signal(signal: str) -> bool:
    sig = (signal or "").upper()
    if sig in TERMINAL_SIGNALS:
        return True
    return "FULL" in sig or sig == "SELL_TRAIL_FULL"


def _infer_step_from_sold_percent(sold_pct: float, tiers: list[float]) -> int:
    """Best-effort sync for positions that already partial-sold before ladder existed."""
    if sold_pct <= 0:
        return 0
    if sold_pct >= 0.999:
        return len(tiers)
    cumulative = 0.0
    for idx, tier in enumerate(tiers):
        cumulative += tier
        if sold_pct <= cumulative + 0.02:
            return idx + 1
    return len(tiers)


def current_ladder_step(position: dict, tiers: list[float]) -> int:
    step = position.get("exit_ladder_step")
    if step is not None:
        return int(step)
    return _infer_step_from_sold_percent(float(position.get("sold_percent", 0) or 0), tiers)


def resolve_sell_amount(
    signal: str,
    symbol: str,
    timeframe: str,
    price: float,
    strategy_params: dict | None,
) -> float | None:
    """Return coin amount to sell, or None to use legacy fraction mapping."""
    cfg = ladder_config(strategy_params)
    if not cfg.get("enabled"):
        return None
    tiers = cfg.get("tiers") or []
    if not tiers:
        return None

    pos = get_position(symbol, timeframe)
    amount = float(pos.get("amount", 0) or 0)
    if amount <= 0 or price <= 0:
        return 0.0

    step = current_ladder_step(pos, tiers)
    terminal = is_terminal_signal(signal) or "trailing_stop" in (signal or "")

    if step >= len(tiers) and not terminal:
        return 0.0

    if terminal or step >= len(tiers) - 1:
        return amount

    peak = float(pos.get("peak_amount") or 0) or amount
    target_coins = peak * tiers[step]
    sell_coins = min(amount, target_coins)

    min_notional = float(cfg.get("min_tier_notional_usdt", 20))
    remainder_usdt = max(0.0, amount - sell_coins) * price
    if 0 < remainder_usdt < min_notional:
        return amount

    if sell_coins * price < min_notional and amount * price >= min_notional:
        return amount

    return sell_coins


def resolve_sell_fraction(
    signal: str,
    symbol: str,
    timeframe: str,
    price: float,
    strategy_params: dict | None,
) -> float | None:
    amount = resolve_sell_amount(signal, symbol, timeframe, price, strategy_params)
    if amount is None:
        return None
    pos = get_position(symbol, timeframe)
    held = float(pos.get("amount", 0) or 0)
    if held <= 0:
        return 0.0
    return min(1.0, amount / held)


def advance_ladder_step(
    position: dict,
    signal: str,
    strategy_params: dict | None,
    *,
    amount_sold: float,
    amount_before: float,
) -> None:
    cfg = ladder_config(strategy_params)
    if not cfg.get("enabled"):
        return
    tiers = cfg.get("tiers") or []
    if not tiers:
        return

    step = current_ladder_step(position, tiers)
    terminal = is_terminal_signal(signal) or "trailing_stop" in (signal or "")
    sold_all = amount_before > 0 and amount_sold >= amount_before * 0.999

    if terminal or sold_all or step >= len(tiers) - 1:
        position["exit_ladder_step"] = len(tiers)
    else:
        position["exit_ladder_step"] = step + 1