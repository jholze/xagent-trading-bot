"""Risk-adjusted metrics on the live closed-trade ledger (#307).

Wraps ``hermes.metrics`` (trade quality / expectancy, max drawdown, Sharpe)
and adds hit rate and profit factor. Read-only.
"""

from __future__ import annotations

from typing import Any, Iterable

from hermes.metrics import compute_trade_quality, max_drawdown_pct, sharpe_from_trades
from services.reporting import clamp_days
from services.reporting.attribution import list_closed_trades
from services.reporting.fills import _as_float


def _start_equity(explicit: float | None) -> float:
    if explicit is not None:
        try:
            v = float(explicit)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    try:
        from core.portfolio_baseline import initial_capital

        v = float(initial_capital() or 0)
        if v > 0:
            return v
    except Exception:
        pass
    return 10_000.0


def _equity_curve(pnls: Iterable[float], start: float) -> list[float]:
    eq = [float(start)]
    running = float(start)
    for pnl in pnls:
        running += float(pnl)
        eq.append(running)
    return eq


def metrics_from_closed_trades(
    trades: Iterable[dict],
    *,
    days: int = 7,
    start_equity: float | None = None,
) -> dict[str, Any]:
    """Hit rate, profit factor, expectancy, max drawdown on a closed-trade list.

    Expectancy is Hermes ``trade_quality`` (win_rate × avg_win − loss_rate × avg_loss)
    on the same SELL-shaped records. Hit rate is Hermes win_count / n_sells.
    """
    days = clamp_days(days)
    sells = [t for t in trades if str(t.get("type") or "").upper() == "SELL"]
    tq = compute_trade_quality(sells)
    n = len(sells)
    hit_rate = (tq["win_count"] / n) if n else 0.0
    gross_win = float(tq["avg_win"]) * int(tq["win_count"])
    gross_loss = float(tq["avg_loss"]) * int(tq["loss_count"])
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    pnls = [_as_float(t.get("pnl")) for t in sells]
    start = _start_equity(start_equity)
    dd = max_drawdown_pct(_equity_curve(pnls, start)) if sells else 0.0
    sharpe = sharpe_from_trades(sells) if sells else 0.0

    return {
        "days": days,
        "n_trades": n,
        "hit_rate": round(hit_rate, 6),
        "hit_rate_pct": round(hit_rate * 100.0, 2),
        "profit_factor": profit_factor if profit_factor == float("inf") else round(profit_factor, 4),
        "expectancy": round(float(tq["trade_quality"]), 4),
        "avg_win": tq["avg_win"],
        "avg_loss": tq["avg_loss"],
        "win_count": tq["win_count"],
        "loss_count": tq["loss_count"],
        "max_drawdown_pct": float(dd),
        "sharpe": float(sharpe),
        "realized_pnl": round(sum(pnls), 6),
        "start_equity": start,
        "empty": n == 0,
    }


def live_metrics(days: int = 7, *, start_equity: float | None = None) -> dict[str, Any]:
    """Risk-adjusted metrics over live closed trades for *days* lookback."""
    days = clamp_days(days)
    try:
        trades = list_closed_trades(days)
    except Exception:
        trades = []
    return metrics_from_closed_trades(trades, days=days, start_equity=start_equity)


def format_live_metrics_block(days: int = 7) -> str:
    """Compact HTML block for /plan and the morning briefing. Empty → ''."""
    try:
        from notifications.telegram_i18n import signed_money, t
    except Exception:
        return ""
    try:
        m = live_metrics(days)
    except Exception:
        return ""
    if m.get("empty"):
        return ""
    pf = m["profit_factor"]
    pf_s = "∞" if pf == float("inf") else f"{float(pf):.2f}"
    title = t("live_metrics_title", days=int(m["days"]))
    line = t(
        "live_metrics_line",
        hit=f"{m['hit_rate_pct']:.0f}%",
        pf=pf_s,
        exp=signed_money(float(m["expectancy"]), decimals=2),
        dd=f"{m['max_drawdown_pct']:.1f}%",
    )
    return f"{title}\n{line}"
