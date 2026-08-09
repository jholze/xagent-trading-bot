#!/usr/bin/env python3
"""60d recovery-hold / DCA-sniper counterfactual on hard-fallen raster coins.

Screens the effective watchlist (+ demo expansion) for alts that:
  - fit volatile/raster style (not pure major-only grid)
  - dropped hard from 60d high (≥ min_dd_pct)

Then on 1h bars compares three policies after a near-top entry:

  A0  baseline_stale_trail   — trail from pre-dump peak (BEAT-class accidental exit)
  A1  legacy_small_dca       — small fixed add, short grace, then trail
  A2  sniper_heavy_hold      — bag-relative heavy + recovery_hold until BE+2%

Usage:
  DEMO_MODE=1 python3 scripts/dca_sniper_replay_60d.py
  DEMO_MODE=1 python3 scripts/dca_sniper_replay_60d.py --days 60 --min-dd 35 --top 12

Public Gate OHLCV only. No deploy, no orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEMO_MODE", "1")

from historical_prices import _fetch_ohlcv_range  # noqa: E402

from services.dca_sniper.pure import (  # noqa: E402
    compute_heavy_size,
    is_grid_excluded,
    profile_key,
)

MAJOR_BASES = frozenset(
    {
        "BTC",
        "ETH",
        "BNB",
        "XRP",
        "SOL",  # keep majors out of sniper-style recovery sample
        "USDT",
        "USDC",
        "DAI",
        "FDUSD",
        "BUSD",
    }
)


@dataclass
class ScreenRow:
    symbol: str
    high: float
    low_after: float
    dd_pct: float
    last: float
    ret_60d_pct: float
    bars_1d: int


@dataclass
class PolicyResult:
    policy: str
    symbol: str
    entry_price: float
    exit_price: float | None
    exit_reason: str
    pnl_usdt: float
    pnl_pct: float
    dca_usdt: float
    dca_at: str | None
    hold_blocked_trail: int = 0
    be_plus_at: str | None = None
    still_open: bool = False
    peak_used: float = 0.0
    avg_after_dca: float = 0.0
    notes: str = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def load_raster_symbols(max_coins: int = 80) -> list[str]:
    """Watchlist + demo expansion, alts that fit volatile raster (exclude pure majors)."""
    syms: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = str(s or "").strip().upper()
        if not s.endswith("/USDT"):
            if "/" not in s:
                s = f"{s}/USDT"
        base = s.split("/")[0]
        if base in MAJOR_BASES:
            return
        if s in seen:
            return
        # skip obvious pure grid large-caps already filtered by MAJOR
        seen.add(s)
        syms.append(s)

    try:
        from data_manager import load_effective_watchlist, load_trade_watchlist

        for loader in (load_effective_watchlist, load_trade_watchlist):
            try:
                for c in loader() or []:
                    if isinstance(c, dict) and c.get("active", True):
                        add(c.get("symbol") or "")
            except Exception:
                pass
    except Exception:
        pass

    for path in (
        ROOT / "watchlist.demo.json",
        ROOT / "watchlist.json",
        ROOT / "watchlist.dry_run_expansion.demo.json",
    ):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text())
            coins = raw if isinstance(raw, list) else (raw.get("coins") or raw.get("watchlist") or [])
            for c in coins:
                if isinstance(c, dict):
                    add(c.get("symbol") or "")
                elif isinstance(c, str):
                    add(c)
        except Exception:
            pass

    # config strategies (meme/vol)
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
        for c in cfg.get("strategies") or []:
            if isinstance(c, dict):
                add(c.get("symbol") or "")
    except Exception:
        pass

    # known hard-dump names often in bot lore / gainer path
    for s in (
        "BEAT/USDT",
        "BLESS/USDT",
        "LAB/USDT",
        "HYPE/USDT",
        "WLD/USDT",
        "SKYAI/USDT",
        "SIREN/USDT",
        "HMSTR/USDT",
        "LIT/USDT",
        "ZEC/USDT",
        "NEAR/USDT",
        "SUI/USDT",
        "ADA/USDT",
        "DOGE/USDT",
        "RAVE/USDT",
        "HIGH/USDT",
        "ARIA/USDT",
        "TRUMP/USDT",
        "WIF/USDT",
        "PEPE/USDT",
        "BONK/USDT",
        "ORDI/USDT",
        "TIA/USDT",
        "SEI/USDT",
        "INJ/USDT",
        "OP/USDT",
        "ARB/USDT",
        "APT/USDT",
        "SUI/USDT",
        "JUP/USDT",
        "W/USDT",
        "ENA/USDT",
        "EIGEN/USDT",
        "MOVE/USDT",
        "AI16Z/USDT",
        "VIRTUAL/USDT",
        "PENGU/USDT",
        "FARTCOIN/USDT",
        "POPCAT/USDT",
        "MEW/USDT",
        "NEIRO/USDT",
        "GOAT/USDT",
        "ACT/USDT",
        "PNUT/USDT",
        "CHILLGUY/USDT",
        "MOODENG/USDT",
        "SPX/USDT",
        "GIGA/USDT",
        "AIXBT/USDT",
        "ZEREBRO/USDT",
        "GRIFFAIN/USDT",
        "ALCH/USDT",
        "FWOG/USDT",
        "BRETT/USDT",
        "TOSHI/USDT",
        "DEGEN/USDT",
        "HIGHER/USDT",
        "AERO/USDT",
        "PENDLE/USDT",
        "JTO/USDT",
        "PYTH/USDT",
        "W/USDT",
        "STRK/USDT",
        "ZK/USDT",
        "MANTA/USDT",
        "ALT/USDT",
        "PIXEL/USDT",
        "PORTAL/USDT",
        "XAI/USDT",
        "ACE/USDT",
        "NFP/USDT",
        "AI/USDT",
        "X/USDT",
        "SAGA/USDT",
        "TAO/USDT",
        "RENDER/USDT",
        "FET/USDT",
        "TAO/USDT",
    ):
        add(s)

    if max_coins > 0:
        return syms[:max_coins]
    return syms


def screen_hard_falls(
    symbols: list[str],
    *,
    days: int,
    min_dd_pct: float,
    workers: int,
) -> list[ScreenRow]:
    end = _utc_now()
    start = end - timedelta(days=days + 2)
    rows: list[ScreenRow] = []

    def one(sym: str) -> ScreenRow | None:
        bars = _fetch_ohlcv_range(sym, start, end, timeframe="1d")
        if not bars or len(bars) < max(20, days // 3):
            return None
        highs = [float(b[2]) for b in bars]
        lows = [float(b[3]) for b in bars]
        closes = [float(b[4]) for b in bars]
        hi = max(highs)
        hi_i = highs.index(hi)
        if hi_i >= len(bars) - 3:
            return None  # high too recent — no dump window
        low_after = min(lows[hi_i:])
        if hi <= 0:
            return None
        dd = (low_after / hi - 1.0) * 100.0
        if dd > -abs(min_dd_pct):
            return None
        ret = (closes[-1] / closes[0] - 1.0) * 100.0 if closes[0] > 0 else 0.0
        return ScreenRow(
            symbol=sym,
            high=hi,
            low_after=low_after,
            dd_pct=round(dd, 2),
            last=closes[-1],
            ret_60d_pct=round(ret, 2),
            bars_1d=len(bars),
        )

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, s): s for s in symbols}
        for fut in as_completed(futs):
            done += 1
            if done % 15 == 0 or done == len(symbols):
                print(f"  screen {done}/{len(symbols)}", flush=True)
            try:
                r = fut.result()
            except Exception:
                r = None
            if r:
                rows.append(r)
    rows.sort(key=lambda r: r.dd_pct)  # most negative first
    return rows


def _bars_to_series(bars: list) -> list[dict[str, Any]]:
    out = []
    for b in bars:
        out.append(
            {
                "ts": int(b[0]),
                "o": float(b[1]),
                "h": float(b[2]),
                "l": float(b[3]),
                "c": float(b[4]),
                "v": float(b[5]),
                "iso": datetime.fromtimestamp(int(b[0]) / 1000, tz=timezone.utc).isoformat(),
            }
        )
    return out


def _trail_stop(peak: float, trail_pct: float, entry: float, floor_at_entry: bool = True) -> float:
    stop = peak * (1.0 - trail_pct / 100.0)
    if floor_at_entry:
        stop = max(stop, entry)
    return stop


def _free_fall(bars: list[dict[str, Any]], i: int) -> bool:
    """True if last 4 bars make lower lows (no dip structure)."""
    if i < 3:
        return False
    return bars[i]["l"] < bars[i - 1]["l"] < bars[i - 2]["l"] < bars[i - 3]["l"]


def _reclaim_ok(bars: list[dict[str, Any]], i: int, lookback: int = 12) -> bool:
    """3-bar higher-lows + ≥3% bounce from local low in lookback window."""
    if i < 3:
        return False
    if _free_fall(bars, i):
        return False
    w0 = max(0, i - lookback)
    local_low = min(b["l"] for b in bars[w0 : i + 1])
    if local_low <= 0:
        return False
    # bounce from local low
    if bars[i]["c"] < local_low * 1.03:
        return False
    # three rising lows (or equal)
    if not (
        bars[i]["l"] >= bars[i - 1]["l"] * 0.998
        and bars[i - 1]["l"] >= bars[i - 2]["l"] * 0.998
    ):
        return False
    # close not making new lows vs 3 bars ago
    if bars[i]["c"] < bars[i - 3]["c"] * 0.99:
        return False
    return True


def simulate_coin(
    symbol: str,
    bars_1h: list[dict[str, Any]],
    *,
    entry_notional: float = 2000.0,
    trail_pct: float = 12.0,
    trail_arm_peak_pct: float = 8.0,
    dca_loss_trigger: float = -18.0,
    small_dca_usdt: float = 500.0,
    be_buffer_pct: float = 2.0,
    grace_hours_legacy: float = 12.0,
    spendable_dca: float = 8000.0,
    equity: float = 100_000.0,
    max_loss_for_entry_path: float = -55.0,
) -> dict[str, PolicyResult]:
    """Policy pack on same entry path after finding dump structure."""
    if len(bars_1h) < 48:
        return {}

    highs = [b["h"] for b in bars_1h]
    hi = max(highs)
    hi_i = highs.index(hi)
    # need dump after high
    if hi_i > len(bars_1h) - 24:
        return {}
    entry_i = hi_i
    entry_px = bars_1h[entry_i]["c"]
    if entry_px <= 0:
        entry_px = hi

    # require later deep red but not total death after entry
    min_after = min(b["l"] for b in bars_1h[entry_i:])
    min_dd = (min_after / entry_px - 1.0) * 100
    if min_dd > dca_loss_trigger:
        return {}
    if min_dd < max_loss_for_entry_path:
        # path goes too deep for recovery thesis — still run, but flag in caller via empty skip optional
        pass

    amount0 = entry_notional / entry_px
    common = dict(
        symbol=symbol,
        bars=bars_1h,
        entry_i=entry_i,
        entry_px=entry_px,
        amount=amount0,
        trail_pct=trail_pct,
        trail_arm_peak_pct=trail_arm_peak_pct,
        dca_loss_trigger=dca_loss_trigger,
        be_buffer_pct=be_buffer_pct,
        spendable_dca=spendable_dca,
        equity=equity,
    )
    results: dict[str, PolicyResult] = {}

    # A0 BEAT: small DCA, NO reanchor, stale peak, no hold
    results["A0_beat_stale"] = _run_policy(
        **common,
        policy="A0_beat_stale",
        do_dca=True,
        dca_usdt=small_dca_usdt,
        reanchor_on_dca=False,
        recovery_hold=False,
        force_peak=hi,
        grace_hours=0.0,
        already_armed=True,
        floor_at_entry=False,
        require_reclaim=False,
        heavy=False,
    )

    # A1 legacy: small DCA, reanchor, grace, no hold
    results["A1_legacy_small"] = _run_policy(
        **common,
        policy="A1_legacy_small",
        do_dca=True,
        dca_usdt=small_dca_usdt,
        reanchor_on_dca=True,
        recovery_hold=False,
        force_peak=None,
        grace_hours=grace_hours_legacy,
        already_armed=False,
        floor_at_entry=True,
        require_reclaim=False,
        heavy=False,
    )

    # A2 sniper heavy + hold, no reclaim gate (aggressive)
    results["A2_heavy_hold"] = _run_policy(
        **common,
        policy="A2_heavy_hold",
        do_dca=True,
        dca_usdt=None,
        reanchor_on_dca=True,
        recovery_hold=True,
        force_peak=None,
        grace_hours=0.0,
        already_armed=False,
        floor_at_entry=True,
        require_reclaim=False,
        heavy=True,
    )

    # A3 BEST CANDIDATE: heavy + hold ONLY on 3-bar reclaim (quality sniper)
    results["A3_heavy_hold_reclaim"] = _run_policy(
        **common,
        policy="A3_heavy_hold_reclaim",
        do_dca=True,
        dca_usdt=None,
        reanchor_on_dca=True,
        recovery_hold=True,
        force_peak=None,
        grace_hours=0.0,
        already_armed=False,
        floor_at_entry=True,
        require_reclaim=True,
        heavy=True,
    )

    # A4: small DCA + reanchor + hold (capital-light recovery protection)
    results["A4_small_hold_reclaim"] = _run_policy(
        **common,
        policy="A4_small_hold_reclaim",
        do_dca=True,
        dca_usdt=small_dca_usdt,
        reanchor_on_dca=True,
        recovery_hold=True,
        force_peak=None,
        grace_hours=0.0,
        already_armed=False,
        floor_at_entry=True,
        require_reclaim=True,
        heavy=False,
    )

    # A5: reanchor peak only after dip (no DCA) + hold false — control for reanchor alone
    results["A5_reanchor_only"] = _run_policy(
        **common,
        policy="A5_reanchor_only",
        do_dca=True,
        dca_usdt=0.01,  # tiny marker → treat as reanchor event without size
        reanchor_on_dca=True,
        recovery_hold=False,
        force_peak=hi,
        grace_hours=0.0,
        already_armed=True,
        floor_at_entry=True,
        require_reclaim=True,
        heavy=False,
        reanchor_without_size=True,
    )
    return results


def _run_policy(
    symbol: str,
    bars: list[dict[str, Any]],
    *,
    entry_i: int,
    entry_px: float,
    amount: float,
    policy: str,
    do_dca: bool,
    dca_usdt: float | None,
    reanchor_on_dca: bool,
    recovery_hold: bool,
    force_peak: float | None,
    trail_pct: float,
    trail_arm_peak_pct: float,
    dca_loss_trigger: float,
    be_buffer_pct: float,
    grace_hours: float,
    spendable_dca: float,
    equity: float,
    already_armed: bool,
    heavy: bool = False,
    floor_at_entry: bool = True,
    require_reclaim: bool = False,
    reanchor_without_size: bool = False,
) -> PolicyResult:
    avg = entry_px
    amt = amount
    cost = avg * amt
    peak = force_peak if force_peak and force_peak > 0 else entry_px
    armed = already_armed
    hold = False
    dca_done = False
    dca_spent = 0.0
    dca_iso = None
    hold_blocks = 0
    be_plus_iso = None
    grace_until_ts = 0
    exit_px = None
    exit_reason = "eow_open"
    still_open = True

    for i in range(entry_i, len(bars)):
        b = bars[i]
        px = b["c"]
        hi = b["h"]
        lo = b["l"]
        ts = b["ts"]
        if hi > peak:
            peak = hi

        loss_pct = (px / avg - 1.0) * 100.0 if avg > 0 else 0.0

        # DCA / reanchor trigger once
        if do_dca and not dca_done and loss_pct <= dca_loss_trigger:
            if _free_fall(bars, i):
                structure_ok = False
            elif require_reclaim:
                structure_ok = _reclaim_ok(bars, i)
            else:
                structure_ok = not _free_fall(bars, i)
            if structure_ok:
                rest = amt * px
                if reanchor_without_size:
                    add = 0.0
                    dca_done = True
                    dca_iso = b["iso"]
                    if reanchor_on_dca:
                        peak = max(px, avg)
                    # no hold / no size
                else:
                    if heavy:
                        add = compute_heavy_size(
                            rest_notional=rest,
                            score=8.0,
                            heavy_min_score=6.0,
                            profile=profile_key("volatile_altcoin", "", symbol),
                            profile_f={"volatile": 0.85, "meme": 0.9, "default": 0.75},
                            spendable_dca=spendable_dca,
                            max_single_add_usdt=3000,
                            max_bag_pct_equity=6.0,
                            equity=equity,
                            bag_now=rest,
                            min_meaningful_usdt=200,
                        )
                    else:
                        add = float(dca_usdt or 0)
                    if add > 0 and px > 0:
                        add_amt = add / px
                        new_cost = cost + add
                        amt = amt + add_amt
                        avg = new_cost / amt if amt > 0 else avg
                        cost = new_cost
                        dca_spent = add
                        dca_done = True
                        dca_iso = b["iso"]
                        if reanchor_on_dca:
                            peak = max(px, avg)
                        if recovery_hold:
                            hold = True
                        if grace_hours > 0:
                            grace_until_ts = ts + int(grace_hours * 3600 * 1000)

        # BE+ promote
        if hold and avg > 0 and px >= avg * (1.0 + be_buffer_pct / 100.0):
            hold = False
            be_plus_iso = b["iso"]
            # trail can arm after promote
            if (peak / avg - 1.0) * 100 >= trail_arm_peak_pct:
                armed = True

        # arm trail
        peak_gain = (peak / avg - 1.0) * 100.0 if avg > 0 else 0.0
        if not armed and peak_gain >= trail_arm_peak_pct:
            armed = True

        # trail fire?
        if armed and amt > 0:
            stop = _trail_stop(peak, trail_pct, avg, floor_at_entry=floor_at_entry)
            # use low wick touch; if stop >> market (stale peak), fill at bar close
            hit = lo <= stop
            if hit:
                if hold:
                    hold_blocks += 1
                    # blocked — continue
                elif ts < grace_until_ts:
                    hold_blocks += 1  # count grace as block-ish
                else:
                    # realistic fill: cannot sell above market when stop is stale-high
                    exit_px = min(stop, px) if stop > px else stop
                    exit_reason = "trailing_stop"
                    still_open = False
                    pnl = (exit_px - avg) * amt
                    pnl_pct = (exit_px / avg - 1.0) * 100 if avg else 0
                    return PolicyResult(
                        policy=policy,
                        symbol=symbol,
                        entry_price=entry_px,
                        exit_price=exit_px,
                        exit_reason=exit_reason,
                        pnl_usdt=round(pnl, 2),
                        pnl_pct=round(pnl_pct, 2),
                        dca_usdt=round(dca_spent, 2),
                        dca_at=dca_iso,
                        hold_blocked_trail=hold_blocks,
                        be_plus_at=be_plus_iso,
                        still_open=False,
                        peak_used=peak,
                        avg_after_dca=avg,
                    )

        # hard SL -40% from avg (always)
        if avg > 0 and lo <= avg * 0.60:
            if True:  # hard SL always
                exit_px = avg * 0.60
                exit_reason = "hard_sl"
                still_open = False
                pnl = (exit_px - avg) * amt
                return PolicyResult(
                    policy=policy,
                    symbol=symbol,
                    entry_price=entry_px,
                    exit_price=exit_px,
                    exit_reason=exit_reason,
                    pnl_usdt=round(pnl, 2),
                    pnl_pct=round((exit_px / avg - 1.0) * 100, 2),
                    dca_usdt=round(dca_spent, 2),
                    dca_at=dca_iso,
                    hold_blocked_trail=hold_blocks,
                    be_plus_at=be_plus_iso,
                    still_open=False,
                    peak_used=peak,
                    avg_after_dca=avg,
                )

    # end of window
    last = bars[-1]["c"]
    pnl = (last - avg) * amt
    return PolicyResult(
        policy=policy,
        symbol=symbol,
        entry_price=entry_px,
        exit_price=last if still_open else exit_px,
        exit_reason="eow_mark" if still_open else exit_reason,
        pnl_usdt=round(pnl, 2),
        pnl_pct=round((last / avg - 1.0) * 100 if avg else 0, 2),
        dca_usdt=round(dca_spent, 2),
        dca_at=dca_iso,
        hold_blocked_trail=hold_blocks,
        be_plus_at=be_plus_iso,
        still_open=still_open,
        peak_used=peak,
        avg_after_dca=avg,
        notes="mark-to-market eow",
    )


def summarize(policy_rows: list[PolicyResult]) -> dict[str, Any]:
    if not policy_rows:
        return {}
    pnls = [r.pnl_usdt for r in policy_rows]
    return {
        "n": len(policy_rows),
        "sum_pnl": round(sum(pnls), 2),
        "mean_pnl": round(mean(pnls), 2),
        "median_pnl": round(median(pnls), 2),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p <= 0),
        "trail_exits": sum(1 for r in policy_rows if r.exit_reason == "trailing_stop"),
        "hard_sl": sum(1 for r in policy_rows if r.exit_reason == "hard_sl"),
        "eow_open": sum(1 for r in policy_rows if r.still_open),
        "total_dca": round(sum(r.dca_usdt for r in policy_rows), 2),
        "total_hold_blocks": sum(r.hold_blocked_trail for r in policy_rows),
        "be_plus_count": sum(1 for r in policy_rows if r.be_plus_at),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="60d DCA sniper / recovery hold replay")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--min-dd", type=float, default=30.0, help="min drawdown from 60d high %")
    ap.add_argument(
        "--max-dd",
        type=float,
        default=55.0,
        help="skip death-spiral rugs worse than this |dd| (recovery not realistic)",
    )
    ap.add_argument("--top", type=int, default=15, help="max hard-fall coins to simulate")
    ap.add_argument("--screen-limit", type=int, default=100)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", type=str, default="")
    ap.add_argument(
        "--sweep",
        action="store_true",
        help="run mild band 30-55 and print best policy ranking",
    )
    args = ap.parse_args()

    if args.sweep:
        args.min_dd = 30.0
        args.max_dd = 55.0
        args.top = max(args.top, 15)

    print("=== DCA Sniper / Recovery-Hold 60d Replay ===", flush=True)
    print(
        f"window={args.days}d dd_band=[{args.min_dd},{args.max_dd}]% top={args.top} "
        f"sweep={bool(args.sweep)}",
        flush=True,
    )

    symbols = load_raster_symbols(max_coins=args.screen_limit)
    print(f"raster candidates: {len(symbols)}", flush=True)

    screened = screen_hard_falls(
        symbols, days=args.days, min_dd_pct=args.min_dd, workers=args.workers
    )
    print(f"hard falls (≥{args.min_dd}% dd): {len(screened)}", flush=True)
    for r in screened[: args.top]:
        print(
            f"  {r.symbol:16} dd={r.dd_pct:7.1f}%  ret60={r.ret_60d_pct:7.1f}%  "
            f"hi={r.high:.6g} low={r.low_after:.6g}",
            flush=True,
        )

    # recovery-eligible band: hard fall but not pure death coin
    max_dd = abs(float(args.max_dd))
    eligible = [r for r in screened if abs(r.dd_pct) <= max_dd]
    print(
        f"recovery band dd ∈ [{args.min_dd}, {max_dd}]%: {len(eligible)} "
        f"(excluded {len(screened) - len(eligible)} death-spirals)",
        flush=True,
    )
    pick = eligible[: args.top]
    if not pick:
        print("No coins passed screen — widen --min-dd/--max-dd or universe.", flush=True)
        return 1

    end = _utc_now()
    start = end - timedelta(days=args.days + 3)
    all_results: list[PolicyResult] = []
    per_coin: dict[str, dict[str, Any]] = {}

    for row in pick:
        sym = row.symbol
        # skip if grid-only would be excluded from sniper (we still sim A0/A1)
        print(f"\nsimulate {sym} …", flush=True)
        bars_raw = _fetch_ohlcv_range(sym, start, end, timeframe="1h")
        if not bars_raw or len(bars_raw) < 100:
            print(f"  skip {sym}: insufficient 1h bars ({len(bars_raw or [])})", flush=True)
            continue
        series = _bars_to_series(bars_raw)
        res = simulate_coin(sym, series)
        if not res:
            print(f"  skip {sym}: no dump structure after high", flush=True)
            continue
        per_coin[sym] = {
            "screen": asdict(row),
            "policies": {k: asdict(v) for k, v in res.items()},
        }
        for pr in res.values():
            all_results.append(pr)
            print(
                f"  {pr.policy:22} pnl={pr.pnl_usdt:9.1f} ({pr.pnl_pct:6.1f}%)  "
                f"exit={pr.exit_reason:14} dca={pr.dca_usdt:7.0f}  "
                f"hold_blocks={pr.hold_blocked_trail} be+={bool(pr.be_plus_at)}",
                flush=True,
            )

    # aggregate by policy
    by_pol: dict[str, list[PolicyResult]] = {}
    for r in all_results:
        by_pol.setdefault(r.policy, []).append(r)

    print("\n=== SUMMARY ===", flush=True)
    summaries = {}
    for pol, rows in sorted(by_pol.items()):
        s = summarize(rows)
        summaries[pol] = s
        print(
            f"{pol:22} n={s['n']:2}  sum_pnl={s['sum_pnl']:10.1f}  "
            f"median={s['median_pnl']:8.1f}  wins={s['wins']}/{s['n']}  "
            f"trail_exits={s['trail_exits']}  hold_blocks={s['total_hold_blocks']}  "
            f"dca_usdt={s['total_dca']:.0f}",
            flush=True,
        )

    # rank policies vs A0 baseline
    a0 = {r.symbol: r for r in by_pol.get("A0_beat_stale", []) or by_pol.get("A0_stale_trail", [])}
    ranking = []
    for pol, rows in by_pol.items():
        if pol.startswith("A0"):
            continue
        deltas = []
        for r in rows:
            if r.symbol in a0:
                deltas.append(r.pnl_usdt - a0[r.symbol].pnl_usdt)
        if not deltas:
            continue
        s = summaries.get(pol) or {}
        ranking.append(
            {
                "policy": pol,
                "median_delta_vs_a0": round(median(deltas), 2),
                "mean_delta_vs_a0": round(mean(deltas), 2),
                "sum_pnl": s.get("sum_pnl"),
                "median_pnl": s.get("median_pnl"),
                "wins": s.get("wins"),
                "n": s.get("n"),
                "trail_exits": s.get("trail_exits"),
                "hold_blocks": s.get("total_hold_blocks"),
                "total_dca": s.get("total_dca"),
                "hard_sl": s.get("hard_sl"),
                "score": round(
                    median(deltas)
                    + 0.15 * mean(deltas)
                    - 0.05 * float(s.get("hard_sl") or 0) * 100
                    + 0.02 * float(s.get("total_hold_blocks") or 0),
                    2,
                ),
            }
        )
    ranking.sort(key=lambda x: x["score"], reverse=True)
    print("\n=== RANKING vs A0 (best first) ===", flush=True)
    for i, row in enumerate(ranking, 1):
        print(
            f"{i}. {row['policy']:26} score={row['score']:8.1f}  "
            f"medΔ={row['median_delta_vs_a0']:8.1f}  sum={row['sum_pnl']}  "
            f"hard_sl={row['hard_sl']}  hold_blk={row['hold_blocks']}  dca={row['total_dca']}",
            flush=True,
        )
    best = ranking[0] if ranking else None
    if best:
        print(f"\n>>> BEST: {best['policy']} (score={best['score']})", flush=True)

    out = {
        "generated_at": _utc_now().isoformat(),
        "params": {
            "days": args.days,
            "min_dd_pct": args.min_dd,
            "max_dd_pct": args.max_dd,
            "top": args.top,
            "screen_n": len(screened),
            "simulated": list(per_coin.keys()),
            "sweep": bool(args.sweep),
        },
        "screened": [asdict(r) for r in screened[:40]],
        "summary": summaries,
        "ranking_vs_a0": ranking,
        "best_policy": best,
        "per_coin": per_coin,
        "notes": [
            "Entry near 60d high (tough recovery path).",
            "A0_beat_stale: DCA without peak reanchor (BEAT-class).",
            "A1_legacy_small: small DCA + reanchor + 12h grace.",
            "A2_heavy_hold: sniper heavy + hold, no reclaim gate.",
            "A3_heavy_hold_reclaim: heavy + hold ONLY on 3-bar reclaim (quality sniper).",
            "A4_small_hold_reclaim: small DCA + hold + reclaim (capital-light).",
            "A5_reanchor_only: reclaim reanchor without size.",
            "Hard SL -40% always. Relative ranking only.",
        ],
    }

    out_path = Path(args.out) if args.out else (
        ROOT / "auswertungen" / f"dca_sniper_replay_60d_{_utc_now().strftime('%Y%m%d_%H%M')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    md_path = out_path.with_suffix(".md")
    md = _to_markdown(out)
    md_path.write_text(md, encoding="utf-8")
    print(f"\nWrote {out_path}", flush=True)
    print(f"Wrote {md_path}", flush=True)
    return 0


def _to_markdown(out: dict) -> str:
    lines = [
        f"# DCA Sniper / Recovery-Hold Replay ({out['params']['days']}d)",
        "",
        f"Generated: `{out['generated_at']}`",
        "",
        "## Screen (hard falls in raster)",
        "",
        "| Symbol | DD from high | 60d ret |",
        "|--------|-------------:|--------:|",
    ]
    for r in out.get("screened") or []:
        lines.append(
            f"| {r['symbol']} | {r['dd_pct']:.1f}% | {r['ret_60d_pct']:.1f}% |"
        )
    lines += ["", "## Policy summary", ""]
    lines.append(
        "| Policy | n | sum PnL | median | wins | trail exits | hold blocks | DCA USDT |"
    )
    lines.append("|--------|--:|--------:|-------:|-----:|------------:|------------:|---------:|")
    for pol, s in (out.get("summary") or {}).items():
        lines.append(
            f"| `{pol}` | {s['n']} | {s['sum_pnl']:.1f} | {s['median_pnl']:.1f} | "
            f"{s['wins']}/{s['n']} | {s['trail_exits']} | {s['total_hold_blocks']} | {s['total_dca']:.0f} |"
        )
    best = out.get("best_policy") or {}
    lines += [
        "",
        "## Ranking vs A0",
        "",
        "| Rank | Policy | Score | med Δ | sum PnL | hard SL | hold blocks |",
        "|-----:|--------|------:|------:|--------:|--------:|------------:|",
    ]
    for i, row in enumerate(out.get("ranking_vs_a0") or [], 1):
        lines.append(
            f"| {i} | `{row['policy']}` | {row['score']} | {row['median_delta_vs_a0']} | "
            f"{row['sum_pnl']} | {row['hard_sl']} | {row['hold_blocks']} |"
        )
    if best:
        lines += ["", f"**BEST:** `{best.get('policy')}` (score={best.get('score')})", ""]
    lines += ["", "## Notes", ""]
    for n in out.get("notes") or []:
        lines.append(f"- {n}")
    lines += ["", "## Per coin", ""]
    for sym, block in (out.get("per_coin") or {}).items():
        lines.append(f"### {sym}")
        lines.append("")
        sc = block.get("screen") or {}
        lines.append(f"DD {sc.get('dd_pct')}% · 60d ret {sc.get('ret_60d_pct')}%")
        lines.append("")
        lines.append("| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |")
        lines.append("|--------|---------:|------:|------|----:|------------:|----|")
        for pol, pr in (block.get("policies") or {}).items():
            lines.append(
                f"| `{pol}` | {pr['pnl_usdt']:.1f} | {pr['pnl_pct']:.1f} | "
                f"{pr['exit_reason']} | {pr['dca_usdt']:.0f} | {pr['hold_blocked_trail']} | "
                f"{'yes' if pr.get('be_plus_at') else 'no'} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
