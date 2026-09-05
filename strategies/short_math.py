"""Paper-futures math for long/short lots (isolated margin).

Freqtrade: stop is risk on *margin* (10% stop @ 2x ≈ 5% price).
Jesse: isolated liq sits inside the bankruptcy price.
Stop must trigger before liquidation (liquidation_buffer).
"""

from __future__ import annotations

from typing import Any

SIDE_LONG = "long"
SIDE_SHORT = "short"


def clamp_leverage(leverage: float, *, cap: float = 2.0) -> float:
    try:
        lev = float(leverage)
    except (TypeError, ValueError):
        lev = 1.0
    cap_f = max(1.0, float(cap or 1.0))
    return max(1.0, min(cap_f, lev))


def position_side(pos: dict | None) -> str:
    if not isinstance(pos, dict):
        return SIDE_LONG
    raw = str(pos.get("side") or SIDE_LONG).strip().lower()
    return SIDE_SHORT if raw == SIDE_SHORT else SIDE_LONG


def is_short(pos: dict | None) -> bool:
    return position_side(pos) == SIDE_SHORT


def notional_usdt(qty: float, entry: float) -> float:
    return abs(float(qty or 0) * float(entry or 0))


def margin_usdt(qty: float, entry: float, leverage: float) -> float:
    notion = notional_usdt(qty, entry)
    lev = clamp_leverage(leverage)
    if lev <= 0:
        return notion
    return notion / lev


def unrealized_pnl(side: str, qty: float, entry: float, mark: float) -> float:
    q = float(qty or 0)
    e = float(entry or 0)
    m = float(mark or 0)
    if q <= 0 or e <= 0 or m <= 0:
        return 0.0
    if str(side).lower() == SIDE_SHORT:
        return q * (e - m)
    return q * (m - e)


def funding_cost_usdt(notional: float, hours: float, rate_8h: float) -> float:
    """Paper funding: shorts pay when rate_8h > 0. Charged on notional."""
    if notional <= 0 or hours <= 0:
        return 0.0
    return float(notional) * float(rate_8h or 0) * (float(hours) / 8.0)


def roe_pct(pnl: float, margin: float) -> float:
    if margin <= 0:
        return 0.0
    return (float(pnl) / float(margin)) * 100.0


def liquidation_price_isolated(
    side: str,
    entry: float,
    leverage: float,
    *,
    mm_rate: float = 0.005,
    fee_rate: float = 0.001,
) -> float:
    """Mark at which isolated margin is gone (before buffer).

    Short: price *rises*. Long: price *falls*.
    """
    e = float(entry or 0)
    if e <= 0:
        return 0.0
    lev = clamp_leverage(leverage)
    mm = min(0.2, max(0.0, float(mm_rate or 0)))
    fee = min(0.05, max(0.0, float(fee_rate or 0)))
    # Lose (1 - mm) of margin at liq; remaining mm is maintenance.
    move = (1.0 - mm) / lev
    if str(side).lower() == SIDE_SHORT:
        return e * (1.0 + move) * (1.0 + fee)
    return e * (1.0 - move) * (1.0 - fee)


def apply_liq_buffer(side: str, entry: float, liq: float, buffer: float) -> float:
    """Freqtrade-style: fire slightly before exchange liq."""
    e = float(entry or 0)
    q = float(liq or 0)
    b = min(0.5, max(0.0, float(buffer or 0)))
    if e <= 0 or q <= 0:
        return q
    dist = abs(q - e)
    if str(side).lower() == SIDE_SHORT:
        return q - dist * b
    return q + dist * b


def stop_price(
    side: str,
    entry: float,
    stop_margin_pct: float,
    leverage: float,
) -> float:
    """Stop as fraction of *margin* risk (Freqtrade). 0.10 @ 2x → 5% price."""
    e = float(entry or 0)
    if e <= 0:
        return 0.0
    lev = clamp_leverage(leverage)
    risk = min(0.95, max(0.0, float(stop_margin_pct or 0)))
    move = risk / lev
    if str(side).lower() == SIDE_SHORT:
        return e * (1.0 + move)
    return e * (1.0 - move)


def should_stop_or_liquidate(
    side: str,
    mark: float,
    *,
    stop: float | None,
    liq: float | None,
) -> str | None:
    m = float(mark or 0)
    if m <= 0:
        return None
    short = str(side).lower() == SIDE_SHORT
    if short:
        if liq and m >= float(liq):
            return "liquidation"
        if stop and m >= float(stop):
            return "stop"
        return None
    if liq and m <= float(liq):
        return "liquidation"
    if stop and m <= float(stop):
        return "stop"
    return None


def snapshot(pos: dict[str, Any] | None, mark: float, *, cap: float = 2.0) -> dict[str, float | str]:
    side = position_side(pos)
    qty = float((pos or {}).get("amount") or 0)
    entry = float((pos or {}).get("average_entry") or 0)
    lev = clamp_leverage((pos or {}).get("leverage") or 1.0, cap=cap)
    pnl = unrealized_pnl(side, qty, entry, mark)
    mar = margin_usdt(qty, entry, lev)
    liq = liquidation_price_isolated(side, entry, lev)
    return {
        "side": side,
        "qty": qty,
        "entry": entry,
        "mark": float(mark or 0),
        "leverage": lev,
        "notional": notional_usdt(qty, entry),
        "margin": mar,
        "pnl": pnl,
        "roe_pct": roe_pct(pnl, mar),
        "liq_price": liq,
    }
