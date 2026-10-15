"""Trade quality and opportunity metrics for Hermes 2.0."""

from __future__ import annotations

import math

from core.models import SandboxMetrics


def _trade_pnl(trade: dict) -> float:
    """Sell pnl for a quality sum. Missing is the two-sided fee loss, not 0."""
    from storage.order_ledger_v2 import pnl_for_sum

    return pnl_for_sum(trade if isinstance(trade, dict) else {"pnl": None})


def compute_trade_quality(trades: list) -> dict:
    sells = [t for t in trades if t.get("type") == "SELL"]
    if not sells:
        return {
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "trade_quality": 0.0,
            "win_count": 0,
            "loss_count": 0,
        }

    pnls = [_trade_pnl(t) for t in sells]
    wins = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p < 0]
    win_rate = len(wins) / len(sells)
    loss_rate = 1.0 - win_rate
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    trade_quality = (win_rate * avg_win) - (loss_rate * avg_loss)

    return {
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "trade_quality": round(trade_quality, 4),
        "win_count": len(wins),
        "loss_count": len(losses),
    }


def opportunity_score(trades: list, bars_tested: int, bars_per_day: float = 6.0) -> float:
    """Trades per week × positive trade quality."""
    tq = compute_trade_quality(trades)
    quality = max(0.0, float(tq["trade_quality"]))
    if bars_tested <= 0:
        return 0.0
    days = bars_tested / bars_per_day if bars_per_day > 0 else 1.0
    weeks = max(days / 7.0, 1 / 7.0)
    sells = len([t for t in trades if t.get("type") == "SELL"])
    trades_per_week = sells / weeks
    return round(trades_per_week * quality, 4)


def enrich_sandbox_metrics(
    metrics: SandboxMetrics,
    trades: list,
    bars_tested: int,
    bars_per_day: float = 6.0,
) -> SandboxMetrics:
    tq = compute_trade_quality(trades)
    buys = len([t for t in trades if t.get("type") == "BUY"])
    metrics.trade_quality = tq["trade_quality"]
    metrics.opportunity_score = opportunity_score(trades, bars_tested, bars_per_day)
    metrics.buy_signals = buys
    return metrics


def max_drawdown_pct(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100.0)
    return round(max_dd, 2)


def annualized_return_pct(total_return_pct: float, days: float) -> float:
    if days <= 0:
        return 0.0
    return round(((1 + total_return_pct / 100.0) ** (365.0 / days) - 1) * 100.0, 2)


def format_job_report(
    *,
    kind: str,
    symbol: str = "",
    days: float = 0,
    n_trades: int = 0,
    total_return_pct: float = 0.0,
    sharpe: float = 0.0,
    max_drawdown_pct: float = 0.0,
    pnl_delta: float | None = None,
    extra_lines: list[str] | None = None,
) -> str:
    """Unified Telegram summary for evaluation jobs (Phase 4)."""
    lines = [f"📊 <b>{kind}</b>"]
    if symbol:
        lines.append(f"Symbol: <b>{symbol}</b>")
    if days:
        lines.append(f"Zeitraum: {days:.0f}d")
    lines.append(f"Trades: {n_trades}")
    lines.append(f"Return: {total_return_pct:+.1f}% | Sharpe: {sharpe:.2f} | Max DD: {max_drawdown_pct:.1f}%")
    if pnl_delta is not None:
        lines.append(f"PnL-Delta: ${pnl_delta:+.2f}")
    for line in extra_lines or []:
        lines.append(line)
    return "\n".join(lines)


def sharpe_from_trades(trades: list) -> float:
    sells = [t for t in trades if t.get("type") == "SELL"]
    returns = []
    for trade in sells:
        usdt = trade.get("usdt_received", 0)
        if usdt and usdt > 0:
            returns.append(_trade_pnl(trade) / usdt)
    if len(returns) > 1:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var) if var > 0 else 0
        return round((mean_r / std) * math.sqrt(len(returns)), 2) if std > 0 else 0.0
    if returns:
        return 1.0 if returns[0] > 0 else -1.0
    return 0.0