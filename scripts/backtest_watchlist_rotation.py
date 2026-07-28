#!/usr/bin/env python3
"""Portfolio rotation backtest on the current watchlist.

Simulates: keep max_slots always filled from the watchlist universe,
enter on short-term momentum rank, exit with trail/lifetime (base | mid | agg).

Also: **day-by-day Gate top gainers** over the last N days (not 7d CMC rank) —
for each UTC day, rank liquid Gate USDT pairs by that day's return, then check
whether the sim bought them (same day / ±1d) and whether they were on the watchlist.

  DEMO_MODE=1 python3.13 scripts/backtest_watchlist_rotation.py --days 10
  DEMO_MODE=1 python3.13 scripts/backtest_watchlist_rotation.py --days 10 --daily-top 10 --gate-scan 150

No Mongo required (watchlist from disk/config). Public Gate OHLCV only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEMO_MODE", "1")

import ccxt  # noqa: E402

from historical_prices import _fetch_ohlcv_range  # noqa: E402

TIMEFRAME = "1h"
MS_H = 3_600_000

# Exit policy packs (same spirit as backtest_exit_policy_10d)
EXIT_VARIANTS: dict[str, dict] = {
    "base": {
        "trail_pct": 6.0,
        "trail_arm": 15.0,
        "trail_min_gain": 10.0,
        "life_arm": 3.0,
        "life_max_h": 96.0,
        "life_min_gain": 1.0,
        "life_skip_peak": 40.0,
        "force_max_h": 0.0,  # 0 = off
    },
    "rot_mid": {
        "trail_pct": 6.0,
        "trail_arm": 10.0,
        "trail_min_gain": 6.0,
        "life_arm": 3.0,
        "life_max_h": 48.0,
        "life_min_gain": 1.0,
        "life_skip_peak": 40.0,
        "force_max_h": 0.0,
    },
    "rot_agg": {
        "trail_pct": 5.0,
        "trail_arm": 8.0,
        "trail_min_gain": 5.0,
        "life_arm": 3.0,
        "life_max_h": 24.0,
        "life_min_gain": 1.0,
        "life_skip_peak": 40.0,
        "force_max_h": 0.0,
    },
    # User thesis: always free slots — force rotate even underwater
    "rot_full": {
        "trail_pct": 5.0,
        "trail_arm": 8.0,
        "trail_min_gain": 5.0,
        "life_arm": 2.0,
        "life_max_h": 24.0,
        "life_min_gain": 0.5,
        "life_skip_peak": 40.0,
        "force_max_h": 36.0,  # hard time-stop
    },
}


@dataclass
class Position:
    symbol: str
    entry_ts: datetime
    entry_bar_i: int
    entry_price: float
    usdt: float
    amount: float
    peak_price: float
    peak_gain_pct: float = 0.0
    trail_armed: bool = False
    profit_armed_at: datetime | None = None


@dataclass
class Trade:
    symbol: str
    side: str  # buy | sell
    ts: str
    price: float
    usdt: float
    pnl: float = 0.0
    pnl_pct: float = 0.0
    peak_gain_pct: float = 0.0
    hold_h: float = 0.0
    reason: str = ""
    entry_score: float = 0.0


@dataclass
class SimStats:
    variant: str
    n_buys: int = 0
    n_sells: int = 0
    realized_pnl: float = 0.0
    open_mtm: float = 0.0
    total_pnl: float = 0.0
    win_sells: int = 0
    loss_sells: int = 0
    avg_hold_h: float = 0.0
    avg_win_usdt: float = 0.0
    avg_loss_usdt: float = 0.0
    median_win_usdt: float = 0.0
    small_wins_le_50: int = 0  # "30×$40" style
    occupancy_avg: float = 0.0
    cash_end: float = 0.0
    equity_end: float = 0.0
    equity_start: float = 0.0
    ret_pct: float = 0.0
    buy_hold_ret_pct: float = 0.0
    reasons: dict = field(default_factory=dict)
    top_trades: list = field(default_factory=list)
    trades: list = field(default_factory=list)


def _load_watchlist_file(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        coins = data
    else:
        coins = data.get("coins") or []
    return [
        c
        for c in coins
        if isinstance(c, dict) and c.get("active", True) and c.get("symbol")
    ]


def load_universe(mode: str, watchlist_path: str, max_coins: int) -> list[str]:
    """Return unique symbols (BASE/USDT)."""
    coins: list[dict] = []
    if watchlist_path:
        coins = _load_watchlist_file(watchlist_path)
    else:
        try:
            from data_manager import load_effective_watchlist, load_trade_watchlist

            if mode == "trade":
                coins = list(load_trade_watchlist() or [])
            else:
                coins = list(load_effective_watchlist() or [])
        except Exception as e:
            print(f"watchlist load failed: {e}", file=sys.stderr)
            for p in ("watchlist.demo.json", "watchlist.json"):
                if (ROOT / p).exists():
                    coins = _load_watchlist_file(str(ROOT / p))
                    break
    syms: list[str] = []
    seen: set[str] = set()
    for c in coins:
        if not c.get("active", True):
            continue
        s = str(c.get("symbol") or "").strip()
        if not s or s in seen:
            continue
        # skip pure stables as trade targets
        base = s.split("/")[0].upper()
        if base in ("USDT", "USDC", "DAI", "BUSD", "FDUSD"):
            continue
        seen.add(s)
        syms.append(s)
        if max_coins > 0 and len(syms) >= max_coins:
            break
    return syms


def fetch_ohlcv_parallel(
    symbols: list[str], start: datetime, end: datetime, workers: int = 6
) -> dict[str, list]:
    out: dict[str, list] = {}

    def one(sym: str) -> tuple[str, list]:
        try:
            bars = _fetch_ohlcv_range(sym, start, end, timeframe=TIMEFRAME)
            return sym, bars or []
        except Exception:
            return sym, []

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, s): s for s in symbols}
        for fut in as_completed(futs):
            sym, bars = fut.result()
            out[sym] = bars
            done += 1
            if done % 10 == 0 or done == len(symbols):
                print(f"  ohlcv {done}/{len(symbols)} last={sym} bars={len(bars)}", flush=True)
    return out


def align_bars(
    ohlcv: dict[str, list], start: datetime, end: datetime
) -> tuple[list[int], dict[str, dict[int, tuple]]]:
    """Common timeline (1h) + per-symbol map ts_ms -> (o,h,l,c,v)."""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    # snap to hour
    start_ms = (start_ms // MS_H) * MS_H
    end_ms = (end_ms // MS_H) * MS_H

    per: dict[str, dict[int, tuple]] = {}
    all_ts: set[int] = set()
    for sym, bars in ohlcv.items():
        m: dict[int, tuple] = {}
        for b in bars:
            ts = int(b[0])
            if ts < start_ms or ts > end_ms:
                continue
            # floor to hour
            ts = (ts // MS_H) * MS_H
            m[ts] = (float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5]))
            all_ts.add(ts)
        per[sym] = m

    timeline = sorted(t for t in all_ts if start_ms <= t <= end_ms)
    return timeline, per


def momentum_score(
    per_sym: dict[int, tuple], ts: int, lookback_h: int = 24
) -> float | None:
    """Return % change over lookback hours, or None if missing data."""
    cur = per_sym.get(ts)
    if not cur:
        return None
    past_ts = ts - lookback_h * MS_H
    # find nearest past bar within 3h
    past = None
    for d in range(0, 4):
        past = per_sym.get(past_ts - d * MS_H) or per_sym.get(past_ts + d * MS_H)
        if past:
            break
    if not past or past[3] <= 0 or cur[3] <= 0:
        return None
    return (cur[3] / past[3] - 1.0) * 100.0


def check_exit(pos: Position, bar_ts: datetime, high: float, low: float, close: float, cfg: dict) -> str | None:
    """Return exit reason or None."""
    pos.peak_price = max(pos.peak_price, high)
    pos.peak_gain_pct = max(pos.peak_gain_pct, (pos.peak_price / pos.entry_price - 1) * 100)
    gain = (close / pos.entry_price - 1) * 100
    hold_h = (bar_ts - pos.entry_ts).total_seconds() / 3600

    if pos.profit_armed_at is None and gain >= float(cfg["life_arm"]):
        pos.profit_armed_at = bar_ts
    if pos.peak_gain_pct >= float(cfg["trail_arm"]):
        pos.trail_armed = True

    # trail
    if pos.trail_armed:
        drop = (1 - low / pos.peak_price) * 100 if pos.peak_price > 0 else 0
        if drop >= float(cfg["trail_pct"]) and gain >= float(cfg["trail_min_gain"]):
            return "trail"

    # lifetime after arm
    if (
        pos.profit_armed_at
        and gain >= float(cfg["life_min_gain"])
        and pos.peak_gain_pct < float(cfg["life_skip_peak"])
    ):
        life_h = (bar_ts - pos.profit_armed_at).total_seconds() / 3600
        if life_h >= float(cfg["life_max_h"]):
            return "lifetime"

    # force rotate (even red)
    force_h = float(cfg.get("force_max_h") or 0)
    if force_h > 0 and hold_h >= force_h:
        return "force_time"

    return None


def build_eligible_by_day(
    base: set[str],
    by_day: dict[str, list[dict]],
    top_n: int,
    mode: str,
) -> dict[str, set[str]]:
    """Eligible trade universe per UTC day.

    mode:
      - none: base only
      - prev: base ∪ previous UTC day's Gate top_n  (no look-ahead)
      - same: base ∪ same day's Gate top_n         (oracle / look-ahead)
    """
    days = sorted(by_day.keys())
    out: dict[str, set[str]] = {}
    for i, day in enumerate(days):
        elig = set(base)
        if mode == "prev" and i > 0:
            prev = days[i - 1]
            elig |= {r["symbol"] for r in by_day[prev][:top_n]}
        elif mode == "same":
            elig |= {r["symbol"] for r in by_day[day][:top_n]}
        # mode none / unknown → base only
        out[day] = elig
    return out


def expand_symbol_pool(
    base: list[str],
    by_day: dict[str, list[dict]],
    top_n: int,
) -> list[str]:
    """Base watchlist + every symbol that appears in any day's top_n."""
    seen = set(base)
    out = list(base)
    for day in sorted(by_day.keys()):
        for r in by_day[day][:top_n]:
            s = r["symbol"]
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def simulate_portfolio(
    variant: str,
    cfg: dict,
    timeline: list[int],
    per: dict[str, dict[int, tuple]],
    symbols: list[str],
    *,
    slots: int,
    size_usdt: float,
    cash0: float,
    mom_lb: int,
    min_entry_mom: float,
    reentry_cooldown_h: float,
    fee_pct: float,
    eligible_by_day: dict[str, set[str]] | None = None,
    base_watchlist: set[str] | None = None,
) -> SimStats:
    cash = cash0
    positions: dict[str, Position] = {}
    cooldown_until: dict[str, datetime] = {}
    trades: list[Trade] = []
    holds: list[float] = []
    sell_pnls: list[float] = []
    reasons: dict[str, int] = {}
    occ_sum = 0
    fee = fee_pct / 100.0
    base_wl = base_watchlist or set()
    expand_buys = 0
    expand_sell_pnl = 0.0

    # warm-up: need lookback bars
    start_i = mom_lb + 2
    if start_i >= len(timeline):
        return SimStats(variant=variant, equity_start=cash0, equity_end=cash0)

    def equity_at(ts: int) -> float:
        eq = cash
        for sym, p in positions.items():
            bar = per.get(sym, {}).get(ts)
            if bar:
                eq += p.amount * bar[3]
            else:
                eq += p.usdt  # stale
        return eq

    for i in range(start_i, len(timeline)):
        ts = timeline[i]
        bar_ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        day_key = bar_ts.date().isoformat()
        eligible = eligible_by_day.get(day_key) if eligible_by_day else None

        # 1) exits first
        to_close: list[tuple[str, str, float]] = []  # sym, reason, px
        for sym, pos in list(positions.items()):
            bar = per.get(sym, {}).get(ts)
            if not bar:
                continue
            _o, high, low, close, _v = bar
            reason = check_exit(pos, bar_ts, high, low, close, cfg)
            if reason:
                to_close.append((sym, reason, close))

        for sym, reason, px in to_close:
            pos = positions.pop(sym)
            proceeds = pos.amount * px * (1 - fee)
            pnl = proceeds - pos.usdt
            pnl_pct = (px / pos.entry_price - 1) * 100
            hold_h = (bar_ts - pos.entry_ts).total_seconds() / 3600
            cash += proceeds
            from_expand = bool(base_wl) and sym not in base_wl
            if from_expand:
                expand_sell_pnl += pnl
            trades.append(
                Trade(
                    symbol=sym,
                    side="sell",
                    ts=bar_ts.isoformat(),
                    price=px,
                    usdt=round(proceeds, 2),
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 2),
                    peak_gain_pct=round(pos.peak_gain_pct, 1),
                    hold_h=round(hold_h, 1),
                    reason=reason + ("|expand" if from_expand else ""),
                )
            )
            holds.append(hold_h)
            sell_pnls.append(pnl)
            reasons[reason] = reasons.get(reason, 0) + 1
            cd = bar_ts + timedelta(hours=reentry_cooldown_h)
            cooldown_until[sym] = cd

        # 2) fill free slots — rank by momentum among eligible candidates
        free = slots - len(positions)
        if free > 0 and cash >= size_usdt * 0.99:
            scored: list[tuple[float, str, float]] = []  # score, sym, price
            for sym in symbols:
                if eligible is not None and sym not in eligible:
                    continue
                if sym in positions:
                    continue
                cd = cooldown_until.get(sym)
                if cd and bar_ts < cd:
                    continue
                bar = per.get(sym, {}).get(ts)
                if not bar or bar[3] <= 0:
                    continue
                sc = momentum_score(per[sym], ts, mom_lb)
                if sc is None:
                    continue
                if sc < min_entry_mom:
                    continue
                scored.append((sc, sym, bar[3]))
            scored.sort(reverse=True)  # best momentum first

            for sc, sym, px in scored:
                if free <= 0 or cash < size_usdt * 0.99:
                    break
                notional = min(size_usdt, cash)
                if notional < size_usdt * 0.5:
                    break
                cost = notional * (1 + fee)
                if cost > cash:
                    continue
                amt = (notional / px) if px > 0 else 0
                if amt <= 0:
                    continue
                cash -= cost
                positions[sym] = Position(
                    symbol=sym,
                    entry_ts=bar_ts,
                    entry_bar_i=i,
                    entry_price=px,
                    usdt=notional,
                    amount=amt,
                    peak_price=px,
                )
                from_expand = bool(base_wl) and sym not in base_wl
                if from_expand:
                    expand_buys += 1
                trades.append(
                    Trade(
                        symbol=sym,
                        side="buy",
                        ts=bar_ts.isoformat(),
                        price=px,
                        usdt=round(notional, 2),
                        entry_score=round(sc, 2),
                        reason=("expand_mom" if from_expand else "momentum"),
                    )
                )
                free -= 1

        occ_sum += len(positions)

    # mark open at end
    last_ts = timeline[-1]
    end_ts = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
    open_mtm = 0.0
    for sym, pos in positions.items():
        bar = per.get(sym, {}).get(last_ts)
        px = bar[3] if bar else pos.entry_price
        mtm = pos.amount * px - pos.usdt
        open_mtm += mtm
        hold_h = (end_ts - pos.entry_ts).total_seconds() / 3600
        trades.append(
            Trade(
                symbol=sym,
                side="open",
                ts=end_ts.isoformat(),
                price=px,
                usdt=round(pos.amount * px, 2),
                pnl=round(mtm, 2),
                pnl_pct=round((px / pos.entry_price - 1) * 100, 2),
                peak_gain_pct=round(pos.peak_gain_pct, 1),
                hold_h=round(hold_h, 1),
                reason="eod_open",
            )
        )

    sells = [t for t in trades if t.side == "sell"]
    buys = [t for t in trades if t.side == "buy"]
    wins = [t.pnl for t in sells if t.pnl > 0]
    losses = [t.pnl for t in sells if t.pnl <= 0]
    realized = sum(t.pnl for t in sells)
    n_bars = max(1, len(timeline) - start_i)
    equity_end = cash + sum(
        pos.amount * (per.get(sym, {}).get(last_ts, (0, 0, 0, pos.entry_price, 0))[3])
        for sym, pos in positions.items()
    )

    # equal-weight buy&hold of same universe (for context)
    bh = _buy_hold_ret(per, symbols, timeline[start_i], last_ts)

    def _med(xs: list[float]) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        return s[len(s) // 2]

    top = sorted(sells, key=lambda t: t.pnl, reverse=True)[:15]
    bot = sorted(sells, key=lambda t: t.pnl)[:8]

    # tag expand stats into reasons for visibility
    if expand_buys:
        reasons["expand_buys"] = expand_buys
    if abs(expand_sell_pnl) > 1e-9:
        reasons["expand_sell_pnl"] = round(expand_sell_pnl, 2)

    return SimStats(
        variant=variant,
        n_buys=len(buys),
        n_sells=len(sells),
        realized_pnl=round(realized, 2),
        open_mtm=round(open_mtm, 2),
        total_pnl=round(realized + open_mtm, 2),
        win_sells=len(wins),
        loss_sells=len(losses),
        avg_hold_h=round(sum(holds) / len(holds), 1) if holds else 0.0,
        avg_win_usdt=round(sum(wins) / len(wins), 2) if wins else 0.0,
        avg_loss_usdt=round(sum(losses) / len(losses), 2) if losses else 0.0,
        median_win_usdt=round(_med(wins), 2),
        small_wins_le_50=sum(1 for w in wins if 0 < w <= 50),
        occupancy_avg=round(occ_sum / n_bars, 2),
        cash_end=round(cash, 2),
        equity_end=round(equity_end, 2),
        equity_start=cash0,
        ret_pct=round((equity_end / cash0 - 1) * 100, 2) if cash0 else 0.0,
        buy_hold_ret_pct=round(bh, 2),
        reasons=reasons,
        top_trades=[asdict(t) for t in top + bot],
        trades=[asdict(t) for t in trades],
    )


def _nearest_bar(
    m: dict[int, tuple], ts: int, radius: int = 6
) -> tuple | None:
    if ts in m:
        return m[ts]
    for d in range(1, radius + 1):
        b = m.get(ts + d * MS_H) or m.get(ts - d * MS_H)
        if b:
            return b
    return None


def _buy_hold_ret(
    per: dict[str, dict[int, tuple]], symbols: list[str], t0: int, t1: int
) -> float:
    rets = []
    for sym in symbols:
        m = per.get(sym) or {}
        a, b = _nearest_bar(m, t0), _nearest_bar(m, t1)
        if a and b and a[3] > 0:
            rets.append(b[3] / a[3] - 1)
    if not rets:
        return 0.0
    return (sum(rets) / len(rets)) * 100


def period_gainer_stats(
    per: dict[str, dict[int, tuple]],
    symbols: list[str],
    t0: int,
    t1: int,
) -> list[dict]:
    """Per-symbol period return + peak from window open (top gainers)."""
    rows: list[dict] = []
    for sym in symbols:
        m = per.get(sym) or {}
        if not m:
            continue
        a = _nearest_bar(m, t0)
        b = _nearest_bar(m, t1)
        if not a or a[3] <= 0:
            continue
        open_px = a[3]
        end_px = b[3] if b and b[3] > 0 else open_px
        # scan highs/lows in window
        peak = open_px
        trough = open_px
        for ts, bar in m.items():
            if ts < t0 or ts > t1:
                continue
            # bar = (open, high, low, close, vol)
            peak = max(peak, bar[1], bar[3])
            if bar[2] > 0:
                trough = min(trough, bar[2])
        period_ret = (end_px / open_px - 1) * 100
        peak_ret = (peak / open_px - 1) * 100
        end_from_peak = (end_px / peak - 1) * 100 if peak > 0 else 0.0
        rows.append(
            {
                "symbol": sym,
                "period_ret_pct": round(period_ret, 2),
                "peak_ret_pct": round(peak_ret, 2),
                "end_from_peak_pct": round(end_from_peak, 2),
                "open": open_px,
                "end": end_px,
                "peak": peak,
            }
        )
    # rank by peak excursion first (true "gainers"), then period ret
    rows.sort(key=lambda r: (r["peak_ret_pct"], r["period_ret_pct"]), reverse=True)
    return rows


def gainer_coverage(
    gainers: list[dict],
    results: dict[str, SimStats],
    variants: list[str],
) -> list[dict]:
    """Join top gainers with per-variant buy/sell activity."""
    out: list[dict] = []
    for g in gainers:
        sym = g["symbol"]
        row: dict = {**g, "by_variant": {}}
        for vname in variants:
            st = results.get(vname)
            if not st:
                continue
            buys = [t for t in st.trades if t.get("side") == "buy" and t.get("symbol") == sym]
            sells = [t for t in st.trades if t.get("side") == "sell" and t.get("symbol") == sym]
            opens = [t for t in st.trades if t.get("side") == "open" and t.get("symbol") == sym]
            realized = sum(float(t.get("pnl") or 0) for t in sells)
            open_mtm = sum(float(t.get("pnl") or 0) for t in opens)
            first_buy = buys[0]["ts"] if buys else None
            first_score = buys[0].get("entry_score") if buys else None
            max_sell_peak = max((float(t.get("peak_gain_pct") or 0) for t in sells), default=0.0)
            row["by_variant"][vname] = {
                "bought": len(buys) > 0,
                "n_buys": len(buys),
                "n_sells": len(sells),
                "first_buy": first_buy,
                "first_entry_score": first_score,
                "realized_pnl": round(realized, 2),
                "open_mtm": round(open_mtm, 2),
                "total_pnl": round(realized + open_mtm, 2),
                "max_pos_peak_pct": round(max_sell_peak, 1),
                "exit_reasons": _count_reasons(sells),
            }
        out.append(row)
    return out


def _count_reasons(sells: list[dict]) -> dict[str, int]:
    d: dict[str, int] = {}
    for t in sells:
        r = str(t.get("reason") or "?")
        d[r] = d.get(r, 0) + 1
    return d


def print_gainer_coverage(
    coverage: list[dict],
    variants: list[str],
    top_n: int,
    rank_by: str = "peak",
) -> None:
    """Print top gainers and whether the system bought them."""
    if rank_by == "period":
        ranked = sorted(coverage, key=lambda r: r["period_ret_pct"], reverse=True)
        label = "period end-to-end ret"
    else:
        ranked = sorted(coverage, key=lambda r: r["peak_ret_pct"], reverse=True)
        label = "peak from window open"

    top = ranked[:top_n]
    print("\n" + "=" * 88)
    print(f"TOP GAINERS ({label}) vs system buys")
    print("=" * 88)
    # header
    vshort = [v[:7] for v in variants]
    hdr = f"{'#':>2} {'symbol':14} {'peak%':>7} {'end%':>7} {'fade%':>7}"
    for v in vshort:
        hdr += f"  {v:>7}"
    print(hdr)
    print("-" * len(hdr))

    hit_counts = {v: 0 for v in variants}
    for i, row in enumerate(top, 1):
        line = (
            f"{i:2d} {row['symbol']:14} {row['peak_ret_pct']:+7.1f} "
            f"{row['period_ret_pct']:+7.1f} {row['end_from_peak_pct']:+7.1f}"
        )
        for v in variants:
            info = (row.get("by_variant") or {}).get(v) or {}
            if info.get("bought"):
                hit_counts[v] += 1
                # show n_buys and realized
                cell = f"Y×{info['n_buys']}"
                line += f"  {cell:>7}"
            else:
                line += f"  {'—':>7}"
        print(line)

    print("-" * len(hdr))
    print("hits among top", top_n, ":", ", ".join(f"{v}={hit_counts[v]}" for v in variants))

    # detail block for primary variant (prefer rot_mid)
    primary = "rot_mid" if "rot_mid" in variants else variants[0]
    print(f"\nDetail ({primary}) — top gainers bought / missed:")
    bought_rows = []
    missed_rows = []
    for row in top:
        info = (row.get("by_variant") or {}).get(primary) or {}
        if info.get("bought"):
            bought_rows.append((row, info))
        else:
            missed_rows.append(row)

    print(f"  BOUGHT {len(bought_rows)}/{len(top)}:")
    for row, info in bought_rows:
        print(
            f"    {row['symbol']:14} peak={row['peak_ret_pct']:+6.1f}% end={row['period_ret_pct']:+6.1f}%  "
            f"buys={info['n_buys']} first={str(info.get('first_buy') or '')[:16]}  "
            f"score={info.get('first_entry_score')}  "
            f"realized=${info['realized_pnl']:+.0f} open=${info['open_mtm']:+.0f}  "
            f"exits={info.get('exit_reasons')}"
        )
    print(f"  MISSED {len(missed_rows)}/{len(top)}:")
    for row in missed_rows:
        print(
            f"    {row['symbol']:14} peak={row['peak_ret_pct']:+6.1f}% end={row['period_ret_pct']:+6.1f}%  "
            f"(never ranked high enough / slots full / below min-mom)"
        )

    # also: did we buy gainers that later faded? capture rate of peak
    if bought_rows:
        print(f"\n  Capture note ({primary}): position peak_gain on sells vs coin period peak")
        for row, info in bought_rows[:12]:
            print(
                f"    {row['symbol']:14} coin_peak={row['peak_ret_pct']:+6.1f}%  "
                f"our_max_pos_peak={info.get('max_pos_peak_pct', 0):+5.1f}%  "
                f"pnl=${info['total_pnl']:+.0f}"
            )


# ---------------------------------------------------------------------------
# Day-by-day Gate top gainers (retrospective, per calendar day)
# ---------------------------------------------------------------------------

_STABLES = {
    "USDT", "USDC", "USD", "DAI", "BUSD", "FDUSD", "TUSD", "USDD", "USDE",
    "EUR", "EURT", "PYUSD",
}
_LEV_SUFFIXES = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")


def _is_tradeable_spot_usdt(symbol: str) -> bool:
    if not symbol or not symbol.endswith("/USDT"):
        return False
    if ":" in symbol:  # swap/futures style
        return False
    base = symbol.split("/")[0].upper()
    if base in _STABLES:
        return False
    if any(base.endswith(s) for s in _LEV_SUFFIXES):
        return False
    return True


def gate_liquid_usdt_symbols(
    min_quote_vol: float = 500_000.0,
    scan_limit: int = 150,
) -> list[tuple[str, float]]:
    """Return [(symbol, quoteVolume)] top liquid Gate spot USDT pairs."""
    ex = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    tickers = ex.fetch_tickers()
    rows: list[tuple[str, float]] = []
    for sym, t in tickers.items():
        if not _is_tradeable_spot_usdt(sym):
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            # fallback approximate
            last = float(t.get("last") or 0)
            base_vol = float(t.get("baseVolume") or 0)
            qv = last * base_vol if last > 0 else 0.0
        qv = float(qv or 0)
        if qv < min_quote_vol:
            continue
        rows.append((sym, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[: max(1, scan_limit)]


def fetch_daily_ohlcv_parallel(
    symbols: list[str],
    start: datetime,
    end: datetime,
    workers: int = 8,
) -> dict[str, list]:
    """1d OHLCV [ts,o,h,l,c,v] per symbol."""
    out: dict[str, list] = {}
    # pad one day for prev-close returns
    fetch_start = start - timedelta(days=2)

    def one(sym: str) -> tuple[str, list]:
        try:
            bars = _fetch_ohlcv_range(sym, fetch_start, end, timeframe="1d")
            return sym, bars or []
        except Exception:
            return sym, []

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, s): s for s in symbols}
        for fut in as_completed(futs):
            sym, bars = fut.result()
            out[sym] = bars
            done += 1
            if done % 25 == 0 or done == len(symbols):
                print(
                    f"  daily-ohlcv {done}/{len(symbols)} last={sym} bars={len(bars)}",
                    flush=True,
                )
    return out


def daily_returns_by_day(
    daily_ohlcv: dict[str, list],
    start: datetime,
    end: datetime,
) -> dict[str, list[dict]]:
    """Map YYYY-MM-DD -> list of {symbol, day_ret_pct, open, close, volume}.

    day_ret = close/prev_close - 1 (true day change). Falls back to close/open.
    """
    start_d = start.astimezone(timezone.utc).date()
    end_d = end.astimezone(timezone.utc).date()
    # collect per day
    by_day: dict[str, list[dict]] = {}

    for sym, bars in daily_ohlcv.items():
        if not bars:
            continue
        # sort by ts
        bars = sorted(bars, key=lambda b: int(b[0]))
        for i, b in enumerate(bars):
            ts = datetime.fromtimestamp(int(b[0]) / 1000, tz=timezone.utc)
            d = ts.date()
            if d < start_d or d > end_d:
                continue
            o, h, l, c, v = float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])
            if c <= 0:
                continue
            if i > 0 and float(bars[i - 1][4]) > 0:
                prev = float(bars[i - 1][4])
                ret = (c / prev - 1.0) * 100.0
            elif o > 0:
                ret = (c / o - 1.0) * 100.0
            else:
                continue
            key = d.isoformat()
            by_day.setdefault(key, []).append(
                {
                    "symbol": sym,
                    "day_ret_pct": round(ret, 2),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                    "day": key,
                }
            )

    # sort each day desc by return
    for key in by_day:
        by_day[key].sort(key=lambda r: r["day_ret_pct"], reverse=True)
    return by_day


def _parse_trade_day(ts: str) -> str | None:
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc).date().isoformat()
    except Exception:
        return None


def _day_shift(day: str, delta: int) -> str:
    d = datetime.fromisoformat(day).date() + timedelta(days=delta)
    return d.isoformat()


def cross_daily_gainers_vs_sim(
    by_day: dict[str, list[dict]],
    top_n: int,
    watchlist_syms: set[str],
    results: dict[str, SimStats],
    variants: list[str],
    primary: str = "rot_mid",
) -> dict:
    """For each day × top_n gainer: in_watchlist + buy hit vs sim variants."""
    days_sorted = sorted(by_day.keys())
    # index buys by variant -> symbol -> set of days
    buy_days: dict[str, dict[str, set[str]]] = {}
    hold_approx: dict[str, dict[str, set[str]]] = {}  # days with buy or open or between buy-sell rough
    for vname, st in results.items():
        buy_days[vname] = {}
        for t in st.trades:
            if t.get("side") != "buy":
                continue
            d = _parse_trade_day(t.get("ts") or "")
            if not d:
                continue
            sym = t["symbol"]
            buy_days[vname].setdefault(sym, set()).add(d)

    daily_rows: list[dict] = []
    for day in days_sorted:
        top = by_day[day][:top_n]
        for rank, g in enumerate(top, 1):
            sym = g["symbol"]
            in_wl = sym in watchlist_syms
            entry: dict = {
                "day": day,
                "rank": rank,
                "symbol": sym,
                "day_ret_pct": g["day_ret_pct"],
                "in_watchlist": in_wl,
                "by_variant": {},
            }
            for vname in variants:
                bset = buy_days.get(vname, {}).get(sym, set())
                same = day in bset
                near = same or (_day_shift(day, -1) in bset) or (_day_shift(day, 1) in bset)
                # any buy in window at all
                any_buy = len(bset) > 0
                entry["by_variant"][vname] = {
                    "buy_same_day": same,
                    "buy_within_1d": near,
                    "buy_any_in_window": any_buy,
                    "buy_days": sorted(bset),
                }
            daily_rows.append(entry)

    # summary
    n = len(daily_rows) or 1
    prim = primary if primary in variants else (variants[0] if variants else "rot_mid")
    summary = {
        "days": len(days_sorted),
        "top_n": top_n,
        "slots": len(daily_rows),
        "in_watchlist": sum(1 for r in daily_rows if r["in_watchlist"]),
        "in_watchlist_pct": round(100 * sum(1 for r in daily_rows if r["in_watchlist"]) / n, 1),
        "by_variant": {},
    }
    for vname in variants:
        same = sum(1 for r in daily_rows if r["by_variant"].get(vname, {}).get("buy_same_day"))
        near = sum(1 for r in daily_rows if r["by_variant"].get(vname, {}).get("buy_within_1d"))
        anyb = sum(1 for r in daily_rows if r["by_variant"].get(vname, {}).get("buy_any_in_window"))
        # among those on watchlist
        wl_rows = [r for r in daily_rows if r["in_watchlist"]]
        wn = len(wl_rows) or 1
        summary["by_variant"][vname] = {
            "buy_same_day": same,
            "buy_same_day_pct": round(100 * same / n, 1),
            "buy_within_1d": near,
            "buy_within_1d_pct": round(100 * near / n, 1),
            "buy_any_in_window": anyb,
            "buy_any_in_window_pct": round(100 * anyb / n, 1),
            "wl_only_same_day": sum(
                1 for r in wl_rows if r["by_variant"].get(vname, {}).get("buy_same_day")
            ),
            "wl_only_same_day_pct": round(
                100
                * sum(1 for r in wl_rows if r["by_variant"].get(vname, {}).get("buy_same_day"))
                / wn,
                1,
            ),
            "wl_only_within_1d": sum(
                1 for r in wl_rows if r["by_variant"].get(vname, {}).get("buy_within_1d")
            ),
            "wl_only_within_1d_pct": round(
                100
                * sum(1 for r in wl_rows if r["by_variant"].get(vname, {}).get("buy_within_1d"))
                / wn,
                1,
            ),
        }

    # unique coins that were daily top but never on watchlist
    missed_off_wl = sorted(
        {
            r["symbol"]
            for r in daily_rows
            if not r["in_watchlist"] and r["day_ret_pct"] >= 10
        }
    )
    summary["unique_off_watchlist_ge10pct"] = missed_off_wl[:40]
    summary["primary"] = prim
    return {"daily": daily_rows, "summary": summary, "days": days_sorted}


def print_daily_gate_gainers(report: dict, variants: list[str], primary: str = "rot_mid") -> None:
    daily = report["daily"]
    summary = report["summary"]
    days = report["days"]
    prim = primary if primary in variants else variants[0]

    print("\n" + "=" * 96)
    print("GATE DAY-BY-DAY TOP GAINERS (UTC) vs system buys")
    print("  method: liquid Gate USDT pairs, rank by that calendar day's close/prev_close")
    print("=" * 96)

    by_day: dict[str, list[dict]] = {}
    for r in daily:
        by_day.setdefault(r["day"], []).append(r)

    for day in days:
        rows = sorted(by_day.get(day, []), key=lambda x: x["rank"])
        if not rows:
            continue
        print(f"\n--- {day}  top {len(rows)} ---")
        print(
            f"{'#':>2} {'symbol':14} {'day%':>7} {'WL':>3}  "
            f"{'same':>5} {'±1d':>5}  note"
        )
        for r in rows:
            info = r["by_variant"].get(prim) or {}
            wl = "Y" if r["in_watchlist"] else "—"
            same = "Y" if info.get("buy_same_day") else "—"
            near = "Y" if info.get("buy_within_1d") else "—"
            note = ""
            if not r["in_watchlist"]:
                note = "not on trade-watchlist"
            elif info.get("buy_same_day"):
                note = "bought same day"
            elif info.get("buy_within_1d"):
                note = "bought ±1d"
            elif info.get("buy_any_in_window"):
                note = f"bought other day {info.get('buy_days')}"
            else:
                note = "on WL but never bought (rank/slots/mom)"
            print(
                f"{r['rank']:2d} {r['symbol']:14} {r['day_ret_pct']:+7.1f} {wl:>3}  "
                f"{same:>5} {near:>5}  {note}"
            )

    print("\n" + "-" * 96)
    print(
        f"SUMMARY  days={summary['days']}  slots={summary['slots']}  "
        f"top_n/day={summary['top_n']}"
    )
    print(
        f"  on trade-watchlist: {summary['in_watchlist']}/{summary['slots']} "
        f"({summary['in_watchlist_pct']}%)"
    )
    for vname in variants:
        s = summary["by_variant"].get(vname) or {}
        print(
            f"  {vname:10}  same-day buy {s.get('buy_same_day', 0)}/{summary['slots']} "
            f"({s.get('buy_same_day_pct', 0)}%)  "
            f"±1d {s.get('buy_within_1d', 0)} ({s.get('buy_within_1d_pct', 0)}%)  "
            f"| among WL-only same-day {s.get('wl_only_same_day_pct', 0)}%  "
            f"±1d {s.get('wl_only_within_1d_pct', 0)}%"
        )
    off = summary.get("unique_off_watchlist_ge10pct") or []
    if off:
        print(f"  big movers (≥+10% day) never on WL (sample): {', '.join(off[:20])}")
    print(
        f"\n  primary detail variant: {prim}\n"
        "  Note: 'bought' = momentum-rotation sim, not live Hermes. "
        "Watchlist is current point-in-time."
    )


def print_stats(st: SimStats) -> None:
    print(f"\n--- {st.variant} ---")
    print(
        f"  equity ${st.equity_start:,.0f} → ${st.equity_end:,.0f}  "
        f"ret={st.ret_pct:+.2f}%  (equal-weight B&H universe {st.buy_hold_ret_pct:+.2f}%)"
    )
    print(
        f"  buys={st.n_buys} sells={st.n_sells}  "
        f"realized=${st.realized_pnl:+,.0f} open_mtm=${st.open_mtm:+,.0f} "
        f"total=${st.total_pnl:+,.0f}"
    )
    print(
        f"  wins={st.win_sells} losses={st.loss_sells}  "
        f"avg_win=${st.avg_win_usdt:.0f} med_win=${st.median_win_usdt:.0f} "
        f"avg_loss=${st.avg_loss_usdt:.0f}  small_wins(≤$50)={st.small_wins_le_50}"
    )
    print(
        f"  avg_hold={st.avg_hold_h:.0f}h  avg_occupancy={st.occupancy_avg:.1f} slots  "
        f"cash_end=${st.cash_end:,.0f}  exits={st.reasons}"
    )
    if st.top_trades:
        print("  best sells:")
        for t in st.top_trades[:8]:
            if t.get("side") != "sell":
                continue
            print(
                f"    {t['symbol']:14} pnl=${t['pnl']:+7.0f} ({t['pnl_pct']:+5.1f}%) "
                f"peak={t['peak_gain_pct']:5.1f}% hold={t['hold_h']:5.0f}h via {t['reason']}"
            )
        print("  worst sells:")
        sells = [t for t in st.top_trades if t.get("side") == "sell"]
        # top_trades is best then worst — show last few negative
        for t in sells[-5:]:
            if t["pnl"] >= 0:
                continue
            print(
                f"    {t['symbol']:14} pnl=${t['pnl']:+7.0f} ({t['pnl_pct']:+5.1f}%) "
                f"hold={t['hold_h']:5.0f}h via {t['reason']}"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--slots", type=int, default=30, help="max open positions (full book target)")
    ap.add_argument("--size", type=float, default=250.0, help="USDT per new position")
    ap.add_argument("--cash", type=float, default=0.0, help="start cash (0 = slots*size*1.05)")
    ap.add_argument("--universe", choices=("trade", "observe"), default="trade")
    ap.add_argument("--watchlist", default="", help="optional JSON path")
    ap.add_argument("--max-coins", type=int, default=60, help="cap universe size")
    ap.add_argument("--mom-h", type=int, default=24, help="momentum lookback hours")
    ap.add_argument("--min-mom", type=float, default=1.0, help="min %% mom to enter")
    ap.add_argument("--cooldown-h", type=float, default=6.0, help="re-entry cooldown after exit")
    ap.add_argument("--fee-pct", type=float, default=0.1)
    ap.add_argument(
        "--variants",
        default="base,rot_mid,rot_agg,rot_full",
        help="comma list of EXIT_VARIANTS keys",
    )
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument(
        "--top-gainers",
        type=int,
        default=15,
        help="how many period (watchlist) top gainers to cross-check vs buys",
    )
    ap.add_argument(
        "--gainer-rank",
        choices=("peak", "period"),
        default="peak",
        help="rank watchlist gainers by peak excursion or end-to-end period return",
    )
    ap.add_argument(
        "--daily-top",
        type=int,
        default=10,
        help="per UTC day: how many Gate top gainers to list (0=skip)",
    )
    ap.add_argument(
        "--gate-scan",
        type=int,
        default=150,
        help="how many liquid Gate USDT pairs to scan for daily tops",
    )
    ap.add_argument(
        "--min-quote-vol",
        type=float,
        default=500_000.0,
        help="min 24h quote volume (USDT) to include in Gate scan",
    )
    ap.add_argument(
        "--skip-period-gainers",
        action="store_true",
        help="skip watchlist period-peak gainer table (daily Gate only)",
    )
    ap.add_argument(
        "--expand-universe",
        action="store_true",
        help=(
            "A/B: trade-watchlist only vs WL + Gate daily tops. "
            "prev = yesterday's top (no look-ahead); same = oracle same-day top"
        ),
    )
    ap.add_argument(
        "--expand-modes",
        default="none,prev,same",
        help="comma list: none | prev | same (used with --expand-universe)",
    )
    ap.add_argument(
        "--expand-exit",
        default="rot_mid",
        help="exit pack for expand A/B (default rot_mid)",
    )
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)
    # extra history for momentum warm-up
    fetch_start = start - timedelta(hours=args.mom_h + 12)

    base_symbols = load_universe(args.universe, args.watchlist, args.max_coins)
    if not base_symbols:
        print("ERROR: empty universe", file=sys.stderr)
        return 1
    base_set = set(base_symbols)

    cash0 = args.cash if args.cash > 0 else args.slots * args.size * 1.05
    print(
        f"Watchlist rotation backtest  days={args.days}  universe={args.universe}  "
        f"base_coins={len(base_symbols)}  slots={args.slots}  size=${args.size:.0f}  "
        f"cash=${cash0:,.0f}",
        flush=True,
    )
    print(f"  base sample: {base_symbols[:12]}{'…' if len(base_symbols)>12 else ''}", flush=True)

    t0 = time.time()
    wanted = [v.strip() for v in args.variants.split(",") if v.strip()]
    expand_modes = [m.strip() for m in args.expand_modes.split(",") if m.strip()]
    do_expand = bool(args.expand_universe)
    do_daily = args.daily_top > 0 or do_expand

    # --- Gate daily tops (needed for expand + daily report) ---
    by_day: dict[str, list[dict]] = {}
    daily_report: dict | None = None
    if do_daily:
        print(
            f"\nGate daily tops: scanning top {args.gate_scan} liquid USDT pairs "
            f"(min vol ${args.min_quote_vol:,.0f})…",
            flush=True,
        )
        t_gate = time.time()
        liquid = gate_liquid_usdt_symbols(
            min_quote_vol=args.min_quote_vol,
            scan_limit=args.gate_scan,
        )
        gate_syms = [s for s, _ in liquid]
        print(f"  liquid pairs={len(gate_syms)}  (e.g. {gate_syms[:8]})", flush=True)
        daily_ohlcv = fetch_daily_ohlcv_parallel(
            gate_syms, start, now, workers=max(args.workers, 8)
        )
        by_day = daily_returns_by_day(daily_ohlcv, start, now)
        print(
            f"  daily ranks ready days={len(by_day)}  ({time.time() - t_gate:.0f}s)",
            flush=True,
        )

    # symbol pool for 1h OHLCV
    if do_expand and by_day:
        symbols = expand_symbol_pool(base_symbols, by_day, args.daily_top or 10)
        print(
            f"  expanded pool: base={len(base_symbols)} + daily-tops → {len(symbols)} symbols",
            flush=True,
        )
    else:
        symbols = list(base_symbols)

    print("fetching 1h OHLCV…", flush=True)
    ohlcv = fetch_ohlcv_parallel(symbols, fetch_start, now, workers=args.workers)
    ok = [s for s in symbols if len(ohlcv.get(s) or []) >= args.mom_h + 10]
    base_ok = [s for s in base_symbols if s in set(ok)]
    print(
        f"  usable symbols={len(ok)}/{len(symbols)}  "
        f"(base usable={len(base_ok)})  ({time.time()-t0:.0f}s)",
        flush=True,
    )
    if len(base_ok) < 5:
        print("ERROR: not enough OHLCV on base watchlist", file=sys.stderr)
        return 1

    timeline, per = align_bars({s: ohlcv[s] for s in ok}, start, now)
    print(f"  timeline bars={len(timeline)}  ({TIMEFRAME})", flush=True)
    if len(timeline) < args.mom_h + 5:
        print("ERROR: short timeline", file=sys.stderr)
        return 1

    results: dict[str, SimStats] = {}

    def _run(
        label: str,
        exit_name: str,
        elig: dict[str, set[str]] | None,
        trade_syms: list[str],
    ) -> SimStats:
        cfg = EXIT_VARIANTS[exit_name]
        st = simulate_portfolio(
            label,
            cfg,
            timeline,
            per,
            trade_syms,
            slots=args.slots,
            size_usdt=args.size,
            cash0=cash0,
            mom_lb=args.mom_h,
            min_entry_mom=args.min_mom,
            reentry_cooldown_h=args.cooldown_h,
            fee_pct=args.fee_pct,
            eligible_by_day=elig,
            base_watchlist=base_set,
        )
        results[label] = st
        print_stats(st)
        # expand buy sample
        exp_buys = [
            t for t in st.trades
            if t.get("side") == "buy" and "expand" in str(t.get("reason") or "")
        ]
        if exp_buys:
            print(f"  expand buys: {len(exp_buys)}  sample:")
            for t in exp_buys[:12]:
                print(
                    f"    BUY {t['symbol']:14} {str(t.get('ts') or '')[:16]} "
                    f"score={t.get('entry_score')} ${float(t.get('usdt') or 0):.0f}"
                )
            exp_sells = [
                t for t in st.trades
                if t.get("side") == "sell" and "expand" in str(t.get("reason") or "")
            ]
            if exp_sells:
                exp_sells_s = sorted(exp_sells, key=lambda x: float(x.get("pnl") or 0), reverse=True)
                print("  expand sells (best/worst):")
                for t in exp_sells_s[:5]:
                    print(
                        f"    {t['symbol']:14} pnl=${float(t['pnl']):+.0f} "
                        f"({float(t.get('pnl_pct') or 0):+.1f}%) "
                        f"hold={float(t.get('hold_h') or 0):.0f}h {t.get('reason')}"
                    )
                for t in exp_sells_s[-3:]:
                    if float(t.get("pnl") or 0) >= 0:
                        continue
                    print(
                        f"    {t['symbol']:14} pnl=${float(t['pnl']):+.0f} "
                        f"({float(t.get('pnl_pct') or 0):+.1f}%) "
                        f"hold={float(t.get('hold_h') or 0):.0f}h {t.get('reason')}"
                    )
        return st

    if do_expand and by_day:
        exit_name = args.expand_exit if args.expand_exit in EXIT_VARIANTS else "rot_mid"
        print(
            f"\n=== UNIVERSE EXPAND A/B  exit={exit_name}  daily_top={args.daily_top} ===",
            flush=True,
        )
        for mode in expand_modes:
            if mode not in ("none", "prev", "same"):
                print(f"skip unknown expand mode {mode}", file=sys.stderr)
                continue
            elig = build_eligible_by_day(base_set, by_day, args.daily_top or 10, mode)
            # for none, restrict to base only via eligible map
            if mode == "none":
                elig = build_eligible_by_day(base_set, by_day, 0, "none")
            label = f"{exit_name}+{mode}"
            print(f"\n>> mode={mode}  label={label}", flush=True)
            if mode == "prev":
                print("   (eligible = WL ∪ yesterday Gate top — no look-ahead)", flush=True)
            elif mode == "same":
                print("   (eligible = WL ∪ same-day Gate top — ORACLE / look-ahead)", flush=True)
            else:
                print("   (eligible = trade watchlist only)", flush=True)
            _run(label, exit_name, elig, ok)
    else:
        # classic exit-variant compare on base watchlist
        for name in wanted:
            if name not in EXIT_VARIANTS:
                print(f"skip unknown variant {name}", file=sys.stderr)
                continue
            _run(name, name, None, base_ok)

    # comparison table
    print("\n" + "=" * 72)
    print(f"{'variant':18} {'ret%':>7} {'realized':>10} {'sells':>6} {'avgHold':>8} "
          f"{'medWin$':>8} {'≤$50w':>6} {'occ':>5}")
    for name, st in results.items():
        print(
            f"{name:18} {st.ret_pct:+7.2f} {st.realized_pnl:+10.0f} {st.n_sells:6d} "
            f"{st.avg_hold_h:7.0f}h {st.median_win_usdt:8.0f} {st.small_wins_le_50:6d} "
            f"{st.occupancy_avg:5.1f}"
        )
    if results:
        bh = next(iter(results.values())).buy_hold_ret_pct
        print(f"{'B&H (trade univ)':18} {bh:+7.2f}  (equal-weight on sim symbol set)")

    # --- watchlist period gainers (optional) ---
    top_for_json: list = []
    if not args.skip_period_gainers and not do_expand:
        t0_bar = timeline[args.mom_h + 2] if len(timeline) > args.mom_h + 2 else timeline[0]
        t1_bar = timeline[-1]
        all_gainers = period_gainer_stats(per, base_ok, t0_bar, t1_bar)
        coverage = gainer_coverage(all_gainers, results, list(results.keys()))
        top_n = max(1, min(args.top_gainers, len(coverage)))
        print_gainer_coverage(coverage, list(results.keys()), top_n=top_n, rank_by=args.gainer_rank)
        top_for_json = sorted(coverage, key=lambda r: r["peak_ret_pct"], reverse=True)[
            : max(top_n, 25)
        ]

    # --- day-by-day report vs primary result ---
    if by_day and args.daily_top > 0:
        # pick primary sim for cross: prefer prev expand, else rot_mid, else first
        primary = None
        for cand in (
            f"{args.expand_exit}+prev",
            f"{args.expand_exit}+none",
            "rot_mid",
            "rot_mid+none",
        ):
            if cand in results:
                primary = cand
                break
        if primary is None:
            primary = next(iter(results))
        daily_report = cross_daily_gainers_vs_sim(
            by_day,
            top_n=args.daily_top,
            watchlist_syms=base_set,
            results=results,
            variants=list(results.keys()),
            primary=primary,
        )
        print_daily_gate_gainers(daily_report, list(results.keys()), primary=primary)

    # write report
    out_dir = ROOT / "auswertungen"
    out_dir.mkdir(exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    path = out_dir / f"watchlist_rotation_{stamp}.json"
    payload = {
        "meta": {
            "days": args.days,
            "universe": args.universe,
            "base_symbols": base_ok,
            "pool_symbols": ok,
            "slots": args.slots,
            "size_usdt": args.size,
            "cash0": cash0,
            "mom_h": args.mom_h,
            "min_mom": args.min_mom,
            "cooldown_h": args.cooldown_h,
            "fee_pct": args.fee_pct,
            "daily_top": args.daily_top,
            "gate_scan": args.gate_scan,
            "min_quote_vol": args.min_quote_vol,
            "expand_universe": do_expand,
            "expand_modes": expand_modes if do_expand else [],
            "expand_exit": args.expand_exit if do_expand else None,
            "generated_at": now.isoformat(),
            "elapsed_s": round(time.time() - t0, 1),
            "note": (
                "Counterfactual portfolio: momentum entry + exit packs. "
                "expand prev = WL ∪ prior UTC day Gate top (no look-ahead). "
                "expand same = oracle same-day Gate top."
            ),
        },
        "variants": {
            k: {kk: vv for kk, vv in asdict(st).items() if kk != "trades"}
            | {"n_trades_log": len(st.trades)}
            for k, st in results.items()
        },
        "top_gainers_coverage": top_for_json,
        "daily_gate_gainers": daily_report,
        "trades_by_variant": {
            k: st.trades for k, st in results.items() if "prev" in k or k == "rot_mid"
        },
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {path}")
    print(
        "\nNote: Entries = 24h momentum rank (not Hermes/Fusion). "
        "Watchlist = current point-in-time. "
        "expand/prev = realistic; expand/same = look-ahead upper bound."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
