"""Watchlist scan ordering for the main trading loop."""

from __future__ import annotations

_GAINER_SOURCES = frozenset(
    {
        "gate_prev_top",
        "gainer_continuation",
        "gainer_live_heat",
        "gainer_prev",  # alias
    }
)


def _is_gainer_source(coin: dict) -> bool:
    src = str(coin.get("source") or "").strip()
    if src in _GAINER_SOURCES:
        return True
    if src.startswith("gainer_"):
        return True
    return False


def order_watchlist_positions_first(
    active_coins: list[dict],
    open_positions: list[dict] | None = None,
    *,
    prefer_gainer: bool = False,
) -> list[dict]:
    """Return watchlist coins with open positions first (for faster sell reactions).

    If prefer_gainer, after positions process gainer-tagged expand coins before
    plain discovery (Issue #162). Indicators still decide buys.
    """
    if open_positions is None:
        from strategies.positions import list_active_positions

        open_positions = list_active_positions()

    by_symbol: dict[str, dict] = {}
    for coin in active_coins:
        by_symbol[coin["symbol"]] = coin

    ordered: list[dict] = []
    seen: set[str] = set()
    for pos in open_positions or []:
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

    rest = [c for c in active_coins if c.get("symbol") and c["symbol"] not in seen]
    if prefer_gainer:
        gainers = [c for c in rest if _is_gainer_source(c)]
        others = [c for c in rest if not _is_gainer_source(c)]
        # stronger day_ret / rank first among gainers
        gainers.sort(
            key=lambda c: (
                -float(c.get("gainer_day_ret") or c.get("pct_24h") or 0),
                int(c.get("gainer_rank") or 999),
            )
        )
        rest = gainers + others

    for coin in rest:
        sym = coin.get("symbol", "")
        if sym and sym not in seen:
            ordered.append(coin)
            seen.add(sym)

    return ordered