"""Replay signal pipeline — signal generation separate from execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable


@dataclass
class ReplaySignal:
    symbol: str
    action: str
    ts: datetime
    source: str
    metadata: dict = field(default_factory=dict)


@dataclass
class ReplayResult:
    signals: list[ReplaySignal]
    trades: list[dict]
    metrics: dict


def run_signals(
    signals: Iterable[ReplaySignal],
    *,
    execute_fn: Callable[[ReplaySignal], dict | None] | None = None,
    realistic_fill: bool = False,
) -> ReplayResult:
    """Fill signals sequentially; optional execute_fn simulates order outcomes."""
    ordered = sorted(signals, key=lambda s: s.ts)
    trades: list[dict] = []
    for sig in ordered:
        if execute_fn is None:
            continue
        outcome = execute_fn(sig)
        if outcome:
            trades.append({**outcome, "symbol": sig.symbol, "action": sig.action, "source": sig.source})
    metrics = {
        "n_signals": len(ordered),
        "n_trades": len(trades),
        "realistic_fill": realistic_fill,
    }
    return ReplayResult(signals=ordered, trades=trades, metrics=metrics)


def signals_from_orders(orders: list[dict], *, symbol: str | None = None) -> list[ReplaySignal]:
    out: list[ReplaySignal] = []
    for order in orders:
        sym = str(order.get("symbol") or "")
        if symbol and sym != symbol:
            continue
        side = str(order.get("side") or order.get("type") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        ts_raw = (order.get("timestamps") or {}).get("filled") or order.get("created_at")
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", ""))
        except Exception:
            continue
        out.append(
            ReplaySignal(
                symbol=sym,
                action=side,
                ts=ts,
                source=str(order.get("source") or "ledger"),
                metadata={"order_id": order.get("id"), "signal": order.get("signal")},
            )
        )
    return out