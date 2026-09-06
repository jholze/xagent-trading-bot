"""Post-apply validation of a Hermes promotion against realized orders (#308).

Attribution (in order):
1. ``hermes_experiment_id`` on the trade / ``request`` / ``request_extra``
   (stamped at order time when hermes-promoted params reach the order path).
2. Else: trades for the same symbol with timestamp ≥ promotion ``applied_at``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

ATTRIBUTION = (
    "Prefer request.hermes_experiment_id (and top-level hermes_experiment_id); "
    "else filter by promotion timestamp + symbol."
)


@dataclass
class PostApplyDecision:
    action: str  # revert | ok | no_verdict_yet
    realized_pnl: float = 0.0
    win_rate: float = 0.0
    n_trades: int = 0
    backtest_win_rate: float = 0.0
    reason: str = ""
    attribution: str = ATTRIBUTION


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif not value:
        return None
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "")[:26])
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _trade_experiment_id(trade: dict) -> str:
    req = trade.get("request") or trade.get("request_extra") or {}
    return str(
        trade.get("hermes_experiment_id")
        or req.get("hermes_experiment_id")
        or ""
    )


def _trade_ts(trade: dict) -> datetime | None:
    ts = trade.get("timestamp") or (trade.get("timestamps") or {}).get("created")
    return _parse_ts(ts)


def _is_sell(trade: dict) -> bool:
    t = str(trade.get("type") or trade.get("side") or "").upper()
    return t in {"SELL", "SELL_30", "SELL_20", "SELL_FULL"}


def attributed_trades(
    *,
    experiment_id: str,
    symbol: str,
    applied_at: datetime | str,
    trades: list[dict],
) -> list[dict]:
    applied = _parse_ts(applied_at) or datetime.now(timezone.utc)
    tagged = []
    fallback = []
    for trade in trades or []:
        if str(trade.get("symbol") or "") != symbol:
            continue
        ts = _trade_ts(trade)
        if ts is None or ts < applied:
            continue
        if _trade_experiment_id(trade) == experiment_id:
            tagged.append(trade)
        else:
            fallback.append(trade)
    return tagged if tagged else fallback


def evaluate(
    *,
    experiment_id: str,
    symbol: str,
    applied_at: datetime | str,
    variant_metrics: dict,
    trades: list[dict],
    min_trades: int = 5,
    win_rate_gap_pp: float = 20.0,
) -> PostApplyDecision:
    """Compare realized P&L/win-rate of post-promotion trades to backtest quality.

    Revert when realized P&L < 0 AND live win rate is more than
    ``win_rate_gap_pp`` percentage points below the variant backtest win rate,
    with at least ``min_trades`` sells. Otherwise ``no_verdict_yet`` (too few
    trades) or ``ok``.
    """
    sample = attributed_trades(
        experiment_id=experiment_id,
        symbol=symbol,
        applied_at=applied_at,
        trades=trades,
    )
    sells = [t for t in sample if _is_sell(t)]
    pnls = [float(t.get("pnl") or 0) for t in sells]
    n = len(sells)
    realized = round(sum(pnls), 4)
    wins = sum(1 for p in pnls if p > 0)
    live_wr = (wins / n * 100.0) if n else 0.0
    back_wr = float((variant_metrics or {}).get("win_rate") or 0)
    if n < int(min_trades):
        return PostApplyDecision(
            action="no_verdict_yet",
            realized_pnl=realized,
            win_rate=round(live_wr, 2),
            n_trades=n,
            backtest_win_rate=back_wr,
            reason=f"no verdict yet ({n} < {min_trades} trades)",
        )
    gap = back_wr - live_wr
    if realized < 0 and gap > float(win_rate_gap_pp):
        return PostApplyDecision(
            action="revert",
            realized_pnl=realized,
            win_rate=round(live_wr, 2),
            n_trades=n,
            backtest_win_rate=back_wr,
            reason=(
                f"realized PnL {realized:.2f} < 0 and win rate {live_wr:.1f}% "
                f"is {gap:.1f} pp below backtest {back_wr:.1f}%"
            ),
        )
    return PostApplyDecision(
        action="ok",
        realized_pnl=realized,
        win_rate=round(live_wr, 2),
        n_trades=n,
        backtest_win_rate=back_wr,
        reason="post-apply ok",
    )


def load_ledger_trades(symbol: str | None = None) -> list[dict]:
    """Load live trade history plus orders that carry hermes_experiment_id."""
    trades: list[dict] = []
    try:
        from data_manager import load_live_trade_history

        history = load_live_trade_history() or {}
        trades.extend(history.get("trades") or [])
    except Exception:
        pass
    try:
        from hermes.live_evidence import _load_orders

        for order in _load_orders("live"):
            req = order.get("request") or {}
            row = dict(order)
            row.setdefault("type", str(order.get("side") or "").upper())
            row.setdefault("timestamp", (order.get("timestamps") or {}).get("created"))
            if req.get("hermes_experiment_id") and not row.get("hermes_experiment_id"):
                row["hermes_experiment_id"] = req["hermes_experiment_id"]
            trades.append(row)
    except Exception:
        pass
    if symbol:
        trades = [t for t in trades if t.get("symbol") == symbol]
    return trades
