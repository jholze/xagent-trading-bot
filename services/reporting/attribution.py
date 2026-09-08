"""Per-strategy realized P&L attribution from closed trades (#307).

Groups filled sell/cover orders by ``source`` (channel) and ``exit_source``
(taxonomy in ``strategies/exit_attribution.py``). Source totals reuse
``scripts.daily_auswertung.pnl_by_source`` so the SELL rollup is not copied.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from scripts.daily_auswertung import pnl_by_source as daily_pnl_by_source
from services.reporting import clamp_days
from services.reporting.fills import _as_float, _event_ts, _side_token, list_filled_orders


def _order_pnl(order: dict) -> float:
    raw = order.get("pnl")
    if isinstance(raw, dict):
        raw = raw.get("usdt", raw.get("realized", raw.get("pnl")))
    return _as_float(raw)


def order_to_closed_trade(order: dict) -> dict[str, Any]:
    """Map a filled sell/cover order to the trade shape Hermes and daily_auswertung use."""
    side = _side_token(order)
    exe = order.get("execution") or {}
    req = order.get("request") or {}
    usdt = _as_float(exe.get("usdt") or exe.get("usdt_amount") or req.get("usdt"))
    ts = _event_ts(order)
    source = str(order.get("source") or "").strip() or "?"
    exit_source = str(order.get("exit_source") or "").strip() or source
    return {
        "type": "SELL",
        "side": side,
        "symbol": order.get("symbol") or "?",
        "source": source,
        "exit_source": exit_source,
        "pnl": _order_pnl(order),
        "usdt_received": usdt,
        "usdt_amount": usdt,
        "timestamp": ts.isoformat() if ts is not None else "",
        "fee": _as_float((exe.get("fee") if not isinstance(exe.get("fee"), dict) else 0)),
        "_order_id": order.get("id") or order.get("display_seq"),
    }


def list_closed_trades(days: int = 7) -> list[dict]:
    """Filled sell/cover trades in the lookback window, oldest first."""
    days = clamp_days(days)
    out: list[dict] = []
    for order in list_filled_orders(days):
        if _side_token(order) not in ("sell", "cover"):
            continue
        out.append(order_to_closed_trade(order))
    out.sort(key=lambda t: str(t.get("timestamp") or ""))
    return out


def _group_stats(trades: Iterable[dict], field: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for trade in trades:
        key = str(trade.get(field) or "").strip() or "?"
        slot = buckets.setdefault(key, {"name": key, "pnl": 0.0, "n": 0, "wins": 0})
        pnl = _as_float(trade.get("pnl"))
        slot["pnl"] += pnl
        slot["n"] += 1
        if pnl > 0:
            slot["wins"] += 1
    rows: list[dict[str, Any]] = []
    for slot in buckets.values():
        n = int(slot["n"])
        wins = int(slot["wins"])
        rows.append(
            {
                "name": slot["name"],
                "pnl": round(float(slot["pnl"]), 6),
                "n": n,
                "wins": wins,
                "win_rate": round((wins / n) * 100.0, 2) if n else 0.0,
            }
        )
    rows.sort(key=lambda r: (r["pnl"], r["name"]))
    return rows


def attribution_summary(
    trades: Iterable[dict] | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Realized P&L, trade count and win rate by ``source`` and ``exit_source``."""
    days = clamp_days(days)
    closed = list(trades) if trades is not None else list_closed_trades(days)
    sells = [t for t in closed if str(t.get("type") or "").upper() == "SELL"]
    # Same SELL-only source rollup the daily report prints (markdown table).
    source_md = daily_pnl_by_source(sells)
    by_source = _group_stats(sells, "source")
    by_exit = _group_stats(sells, "exit_source")
    total_pnl = round(sum(float(t.get("pnl") or 0) for t in sells), 6)
    wins = sum(1 for t in sells if _as_float(t.get("pnl")) > 0)
    n = len(sells)
    return {
        "days": days,
        "n_trades": n,
        "realized_pnl": total_pnl,
        "win_rate": round((wins / n) * 100.0, 2) if n else 0.0,
        "by_source": by_source,
        "by_exit_source": by_exit,
        "source_markdown": source_md,
        "empty": n == 0,
    }
