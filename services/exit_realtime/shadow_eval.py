"""Pure would-sell evaluation for realtime shadow (no I/O)."""

from __future__ import annotations

from typing import Any

from core.models import MarketContext


def to_gate_pair(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    if "/" in s:
        base, quote = s.split("/", 1)
        return f"{base}_{quote}"
    if s.endswith("USDT"):
        return f"{s[:-4]}_USDT"
    return s


def from_gate_pair(pair: str) -> str:
    p = str(pair or "").strip().upper()
    if "_" in p:
        return p.replace("_", "/", 1)
    return p


def build_market(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    average_entry: float,
    atr_pct: float = 3.0,
    strategy_params: dict | None = None,
) -> MarketContext:
    return MarketContext(
        symbol=symbol,
        timeframe=timeframe or "1h",
        current_price=float(price),
        has_position=True,
        average_entry=float(average_entry or 0),
        atr_pct=float(atr_pct or 3.0),
        strategy_params=dict(strategy_params or {}),
    )


def evaluate_would_sells(
    *,
    symbol: str,
    timeframe: str,
    price: float,
    position: dict[str, Any],
    strategy_params: dict[str, Any],
    sources: frozenset[str] | set[str] | None = None,
    atr_pct: float = 3.0,
) -> list[dict[str, Any]]:
    """Return list of would-sell events (dicts) for allowed sources."""
    allowed = sources or frozenset({"trailing_take_profit", "trailing_stop"})
    entry = float(position.get("average_entry") or 0)
    if entry <= 0 or price <= 0:
        return []

    # Work on a shallow copy so peak bump does not mutate caller unexpectedly
    pos = dict(position)
    recent_high = float(pos.get("recent_high") or 0)
    if price > recent_high:
        pos["recent_high"] = float(price)
        recent_high = float(price)

    market = build_market(
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        average_entry=entry,
        atr_pct=atr_pct,
        strategy_params=strategy_params,
    )
    gain = (price / entry - 1.0) * 100.0
    peak_gain = (recent_high / entry - 1.0) * 100.0 if recent_high > 0 else gain
    drop = (1.0 - price / recent_high) * 100.0 if recent_high > 0 else 0.0

    out: list[dict[str, Any]] = []
    if "trailing_take_profit" in allowed:
        try:
            from strategies.trailing_take_profit import evaluate_trailing_take_profit

            cand = evaluate_trailing_take_profit(market, pos, strategy_params)
            if cand:
                out.append(
                    {
                        "source": cand.source,
                        "action": cand.action,
                        "priority": cand.priority,
                        "rationale": cand.rationale,
                        "strategy_shadow": bool(cand.shadow_only),
                    }
                )
        except Exception as exc:
            out.append({"source": "trailing_take_profit", "error": str(exc)[:160]})

    if "trailing_stop" in allowed:
        try:
            from strategies.trailing_stop import evaluate_trailing_stop

            cand = evaluate_trailing_stop(market, pos, strategy_params)
            if cand:
                out.append(
                    {
                        "source": cand.source,
                        "action": cand.action,
                        "priority": cand.priority,
                        "rationale": cand.rationale,
                        "strategy_shadow": bool(cand.shadow_only),
                    }
                )
        except Exception as exc:
            out.append({"source": "trailing_stop", "error": str(exc)[:160]})

    if not out:
        return []

    # Peak may have been bumped on local pos copy
    recent_high = float(pos.get("recent_high") or recent_high)
    peak_gain = (recent_high / entry - 1.0) * 100.0 if recent_high > 0 else gain
    drop = (1.0 - price / recent_high) * 100.0 if recent_high > 0 else 0.0

    base = {
        "type": "exit_ws_shadow",
        "symbol": symbol,
        "timeframe": timeframe,
        "price": round(float(price), 10),
        "entry": round(entry, 10),
        "recent_high": round(float(recent_high), 10),
        "gain_pct": round(gain, 4),
        "peak_gain_pct": round(peak_gain, 4),
        "drop_from_high_pct": round(drop, 4),
        "atr_pct": round(float(atr_pct), 4),
    }
    return [{**base, **ev} for ev in out]
