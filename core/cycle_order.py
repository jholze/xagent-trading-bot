"""Watchlist scan ordering for the main trading loop."""

from __future__ import annotations


def order_watchlist_positions_first(
    active_coins: list[dict],
    open_positions: list[dict] | None = None,
) -> list[dict]:
    """Return watchlist coins with open positions first (for faster sell reactions)."""
    if open_positions is None:
        from strategies.positions import list_active_positions

        open_positions = list_active_positions()
    if not open_positions:
        return list(active_coins)

    by_symbol: dict[str, dict] = {}
    for coin in active_coins:
        by_symbol[coin["symbol"]] = coin

    ordered: list[dict] = []
    seen: set[str] = set()
    for pos in open_positions:
        sym = pos.get("symbol", "")
        if not sym or sym in seen:
            continue
        coin = by_symbol.get(sym)
        if coin is None:
            coin = {
                "symbol": sym,
                "timeframe": pos.get("timeframe", "4h"),
                "active": True,
            }
        ordered.append(coin)
        seen.add(sym)

    for coin in active_coins:
        sym = coin.get("symbol", "")
        if sym and sym not in seen:
            ordered.append(coin)
            seen.add(sym)

    return ordered