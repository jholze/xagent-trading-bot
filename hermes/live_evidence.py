"""Dry-run / live ledger metrics for Hermes promotion guardrails."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from data_manager import load_live_trade_history, resolve_orders_file


@dataclass
class LiveMetrics:
    symbol: str
    lookback_days: int = 7
    live_trades: int = 0
    live_sell_trades: int = 0
    live_sell_pnl: float = 0.0
    live_win_rate: float = 0.0
    live_pnl_by_source: dict = field(default_factory=dict)
    live_rejections: int = 0
    live_order_attempts: int = 0
    live_reject_rate: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "")[:26])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_orders(scope: str = "live") -> list[dict]:
    path = resolve_orders_file(scope)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("orders") or [])
    except Exception:
        return []


def compute_live_metrics(
    symbol: str,
    lookback_days: int = 7,
    include_manual_trades: bool = True,
    scope: str = "live",
    now: datetime | None = None,
) -> LiveMetrics:
    """Aggregate dry-run ledger stats for a symbol over a rolling window."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)
    history = load_live_trade_history()
    trades = [
        t for t in history.get("trades", [])
        if t.get("symbol") == symbol and t.get("mode", "live") == "live"
    ]

    window_trades = []
    sells = []
    pnl_by_source: dict[str, float] = {}

    for trade in trades:
        ts = _parse_ts(trade.get("timestamp", ""))
        if ts < since:
            continue
        source = trade.get("source") or "unknown"
        if not include_manual_trades and source == "manual":
            continue
        window_trades.append(trade)
        if trade.get("type") == "SELL":
            sells.append(trade)
            pnl = float(trade.get("pnl") or 0)
            pnl_by_source[source] = pnl_by_source.get(source, 0.0) + pnl

    sell_pnl = sum(float(t.get("pnl") or 0) for t in sells)
    wins = sum(1 for t in sells if float(t.get("pnl") or 0) > 0)
    win_rate = (wins / len(sells) * 100.0) if sells else 0.0

    orders = _load_orders(scope)
    attempts = 0
    rejections = 0
    for order in orders:
        if order.get("symbol") != symbol:
            continue
        created = order.get("timestamps", {}).get("created", "")
        if not created:
            continue
        if _parse_ts(created) < since:
            continue
        attempts += 1
        if order.get("status") == "rejected":
            rejections += 1

    reject_rate = (rejections / attempts) if attempts else 0.0

    return LiveMetrics(
        symbol=symbol,
        lookback_days=lookback_days,
        live_trades=len(window_trades),
        live_sell_trades=len(sells),
        live_sell_pnl=round(sell_pnl, 4),
        live_win_rate=round(win_rate, 2),
        live_pnl_by_source={k: round(v, 4) for k, v in pnl_by_source.items()},
        live_rejections=rejections,
        live_order_attempts=attempts,
        live_reject_rate=round(reject_rate, 4),
    )


def format_live_metrics_line(metrics: LiveMetrics | None) -> str:
    if not metrics or metrics.live_trades == 0:
        return "Live 7d: no trades"
    return (
        f"Live {metrics.lookback_days}d: {metrics.live_sell_pnl:+.2f} USDT "
        f"({metrics.live_sell_trades} sells, WR {metrics.live_win_rate:.0f}%)"
    )