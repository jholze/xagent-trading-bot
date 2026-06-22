"""Churn replay — detect sell→buy patterns blocked by rebuy cooldown."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from core.config import get_bot_config
from hermes.replay_engine import ReplaySignal, run_signals, signals_from_orders
from services.order_service import OrderService


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except Exception:
        return None


def analyze_churn(
    symbol: str,
    *,
    since: datetime | None = None,
    min_hours_rebuy: float | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    cfg = get_bot_config()
    min_hours = float(min_hours_rebuy if min_hours_rebuy is not None else cfg.min_hours_after_sell_before_rebuy)
    svc = OrderService(scope=scope)
    orders, _ = svc.list_orders(trade_book_only=True, per_page=500)
    if since:
        orders = [
            o for o in orders
            if (_parse_ts((o.get("timestamps") or {}).get("filled")) or datetime.min) >= since
        ]
    sym_orders = sorted(
        [o for o in orders if o.get("symbol") == symbol],
        key=lambda o: str((o.get("timestamps") or {}).get("filled") or ""),
    )
    signals = signals_from_orders(sym_orders, symbol=symbol)
    replay = run_signals(signals)

    churn_pairs: list[dict] = []
    last_sell_at: datetime | None = None
    for sig in replay.signals:
        if sig.action == "SELL":
            last_sell_at = sig.ts
        elif sig.action == "BUY" and last_sell_at:
            gap_h = (sig.ts - last_sell_at).total_seconds() / 3600.0
            blocked = gap_h < min_hours
            churn_pairs.append(
                {
                    "sell_at": last_sell_at.isoformat(),
                    "buy_at": sig.ts.isoformat(),
                    "gap_hours": round(gap_h, 2),
                    "would_block_rebuy": blocked,
                }
            )
            last_sell_at = None

    blocked = [p for p in churn_pairs if p["would_block_rebuy"]]
    return {
        "symbol": symbol,
        "min_hours_rebuy": min_hours,
        "orders": len(sym_orders),
        "churn_pairs": len(churn_pairs),
        "blocked_rebuys": len(blocked),
        "pairs": churn_pairs[:20],
        "metrics": replay.metrics,
    }


def format_telegram_summary(result: dict) -> str:
    from hermes.metrics import format_job_report

    sym = result.get("symbol", "?")
    blocked = result.get("blocked_rebuys", 0)
    pairs = result.get("churn_pairs", 0)
    min_h = result.get("min_hours_rebuy", 4)
    extra = [
        f"Sell→Buy-Paare: {pairs}",
        f"Rebuy-Cooldown ({min_h:.0f}h) würde blockieren: <b>{blocked}</b>",
    ]
    for p in (result.get("pairs") or [])[:5]:
        flag = "🚫" if p.get("would_block_rebuy") else "✅"
        extra.append(f"{flag} {p.get('gap_hours')}h nach Sell")
    return format_job_report(
        kind=f"Churn Replay {sym}",
        symbol=sym,
        n_trades=result.get("orders", 0),
        extra_lines=extra,
    )