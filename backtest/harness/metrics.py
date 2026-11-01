"""Run metrics and the KILL / OBSERVE / GO verdict."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from . import stats
from .engine import HOUR, RunResult

LOSS_LOT_PCT = -20.0
DAY = 86400


@dataclass
class RunMetrics:
    lots: int
    invested_usdt: float
    pnl_usdt: float
    pnl_pct: float  # pnl / invested
    max_dd_pct: float  # peak-to-trough of cumulative PnL over peak deployed capital
    open_lots: int
    open_le_minus20: int
    share_open_le_minus20: float  # percent of open lots
    hit_rate: float  # percent of lots with pnl > 0 (open lots marked)
    avg_win_usdt: float
    avg_loss_usdt: float
    capital_in_losing_lots: float  # cost basis of open lots below entry
    stop_full: int
    stop_partial: int
    escalated: int
    partial_rejects: int
    lock_blocked_bars: int
    skipped_no_candles: int
    filtered_out: int
    lots_le_minus20: int = 0  # all lots (open marked, closed realized) with ret <= -20 %
    share_lots_le_minus20: float = 0.0
    early_exits: int = 0
    flat_stops: int = 0
    skipped_adds: int = 0


def summarize(run: RunResult) -> RunMetrics:
    lots = run.lots
    inv = sum(x.invested for x in lots)
    pnl = sum(x.pnl for x in lots)
    open_lots = [x for x in lots if x.open_at_end]
    le20 = [x for x in open_lots if x.end_unreal_pct is not None and x.end_unreal_pct <= LOSS_LOT_PCT]
    wins = [x.pnl for x in lots if x.pnl > 0]
    losses = [x.pnl for x in lots if x.pnl <= 0]
    cum = peak = dd = 0.0
    cap = peak_cap = 0.0
    for ts in sorted(set(run.pnl_curve) | set(run.capital_curve)):
        cum += run.pnl_curve.get(ts, 0.0)
        cap += run.capital_curve.get(ts, 0.0)
        peak_cap = max(peak_cap, cap)
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return RunMetrics(
        lots=len(lots),
        invested_usdt=inv,
        pnl_usdt=pnl,
        pnl_pct=100.0 * pnl / inv if inv else 0.0,
        max_dd_pct=100.0 * dd / peak_cap if peak_cap else 0.0,
        open_lots=len(open_lots),
        open_le_minus20=len(le20),
        share_open_le_minus20=100.0 * len(le20) / len(open_lots) if open_lots else 0.0,
        hit_rate=100.0 * len(wins) / len(lots) if lots else 0.0,
        avg_win_usdt=statistics.fmean(wins) if wins else 0.0,
        avg_loss_usdt=statistics.fmean(losses) if losses else 0.0,
        capital_in_losing_lots=sum(x.end_cost_basis for x in open_lots if (x.end_unreal_pct or 0) < 0),
        stop_full=sum(x.stop_full for x in lots),
        stop_partial=sum(x.stop_partial for x in lots),
        escalated=sum(x.escalated for x in lots),
        partial_rejects=sum(x.partial_rejects for x in lots),
        lock_blocked_bars=sum(x.locked_blocks for x in lots),
        skipped_no_candles=run.skipped_no_candles,
        filtered_out=run.filtered_out,
        lots_le_minus20=sum(1 for x in lots if x.ret <= LOSS_LOT_PCT / 100.0),
        share_lots_le_minus20=100.0 * sum(1 for x in lots if x.ret <= LOSS_LOT_PCT / 100.0) / len(lots) if lots else 0.0,
        early_exits=sum(x.early_exits for x in lots),
        flat_stops=sum(x.flat_stops for x in lots),
        skipped_adds=sum(x.skipped_adds for x in lots),
    )


def daily_returns(run: RunResult, day_keys: list[int]) -> list[float]:
    """Daily PnL over the capital deployed that day (hour-average). 0 on idle days."""
    pnl: dict[int, float] = {}
    cap_hours: dict[int, float] = {}
    cap = 0.0
    last_day = None
    for ts in sorted(set(run.pnl_curve) | set(run.capital_curve)):
        d = ts - ts % DAY
        pnl[d] = pnl.get(d, 0.0) + run.pnl_curve.get(ts, 0.0)
        cap += run.capital_curve.get(ts, 0.0)
        cap_hours[d] = cap_hours.get(d, 0.0) + max(cap, 0.0)
        last_day = d
    del last_day
    out = []
    for d in day_keys:
        c = cap_hours.get(d, 0.0) / (DAY / HOUR)
        out.append(pnl.get(d, 0.0) / c if c > 1.0 else 0.0)
    return out


@dataclass
class Verdict:
    label: str  # GO | OBSERVE | KILL
    windows_won: int
    windows: int
    dsr: float
    ci_low: float
    ci_high: float
    guardrails_worse: list[str] = field(default_factory=list)
    reason: str = ""


GUARDRAILS = (
    ("share_open_le_minus20", "lower"),
    ("max_dd_pct", "lower"),
    ("capital_in_losing_lots", "lower"),
)


# A guardrail counts as worse only beyond 1 % (relative) of the baseline value,
# so path noise of a few basis points does not decide a verdict. Raw values are
# always printed next to the verdict.
GUARDRAIL_REL_TOLERANCE = 0.01


def guardrails_worse(base: RunMetrics, cand: RunMetrics, *, rel_tolerance: float = GUARDRAIL_REL_TOLERANCE) -> list[str]:
    out = []
    for name, better in GUARDRAILS:
        b, c = getattr(base, name), getattr(cand, name)
        if better == "lower" and c > b + abs(b) * rel_tolerance + 1e-9:
            out.append(name)
    return out


def decide(
    *,
    window_wins: list[bool],
    excess_daily: list[float],
    n_trials: int,
    worse: list[str],
    informational: bool = False,
    dsr_threshold: float = 0.5,
) -> Verdict:
    dsr = stats.deflated_sharpe(excess_daily, n_trials)
    ci = stats.bootstrap_ci(excess_daily, block=5)
    won = sum(window_wins)
    need = 2 if len(window_wins) >= 3 else len(window_wins)
    if won < need:
        label, why = "KILL", f"won {won}/{len(window_wins)} windows (< {need})"
    elif worse:
        label, why = "KILL", "guardrail worse: " + ", ".join(worse)
    elif dsr > dsr_threshold and ci.low > 0:
        label, why = "GO", f"won {won}/{len(window_wins)}, DSR {dsr:.2f} > {dsr_threshold}, CI excludes 0"
    else:
        label = "OBSERVE"
        why = f"won {won}/{len(window_wins)} but DSR {dsr:.2f}" + (
            " <= 0.5" if dsr <= dsr_threshold else ""
        ) + ("; CI includes 0" if ci.low <= 0 else "")
    if informational:
        why = f"bug fix under rule 5, verdict is informational: {why}"
    return Verdict(label, won, len(window_wins), dsr, ci.low, ci.high, worse, why)
