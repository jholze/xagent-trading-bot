#!/usr/bin/env python3
"""Walk-forward paper measurement for issue #616 arms A / B0 / B1 / C.

Freeze-ID: FBR-v1.1-watchlist-616. Research only. Does not trade, does not
set shorts.allow_live, and does not enqueue live orders.

Repro:
  python scripts/wf_watchlist_abc.py \\
    --start 2026-07-01 --end 2026-09-28 \\
    --freeze FBR-v1.1-watchlist-616 \\
    --arms A,B0,B1,C \\
    --orders <filled-orders.json> \\
    --out artifacts/wf_watchlist_abc_20260929
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

BOT_ROOT = Path(__file__).resolve().parents[1]
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from strategies.short_cover import evaluate_short_cover  # noqa: E402
from strategies.short_math import (  # noqa: E402
    apply_liq_buffer,
    liquidation_price_isolated,
    should_stop_or_liquidate,
    stop_price,
)
from strategies.short_policy import (  # noqa: E402
    AUTO_SOURCES,
    auto_short_notional_usdt,
    resolve_short_params,
)

FREEZE_ID = "FBR-v1.1-watchlist-616"
FEE_BP_SIDE = 7.5
FEE_FRAC = FEE_BP_SIDE / 10_000.0
FUNDING_RATE_8H = 0.0001
BAR_SECONDS = 3600

# Original #616 body (pre Viktor regime bump) for this first Lena measurement.
# HARVEST stays in the allow-list. A missing regime tape still fail-closes.
REGIME_ALLOW = ("RISK_OFF", "HARVEST")

ENQUEUE_SOURCES = (
    "rsi_sell",
    "exit_1h_rsi_rollover",
    "oracle_climax_harvest",
    "exit_volume_climax",
    "SELL_FULL",
    "manual_sell",
)

SETUP = {
    "ttl_hours": 48.0,
    "post_long_cooldown_min": 30.0,
    "reentry_cooldown_min": 60.0,
    "max_entries": 40,
    "min_exit_notional_usdt": 25.0,
    "atr_pct_min": 1.0,
    "atr_pct_max": 5.0,
    "atr_period": 14,
    "rsi_period": 14,
    "confluence_min": 2,
    "rsi_large_min": 55.0,
    "rsi_large_max": 60.0,
    "rsi_mid_min": 55.0,
    "rsi_mid_max": 65.0,
    "large_mcap_usd": 1_000_000_000.0,
    "bounce_atr_mult": 0.5,
    "bounce_fail_atr": 0.25,
    "bounce_lookback_bars_1h": 12,
    "mcap_floor_usd": 100_000_000.0,
    "size_factor_of_long_exit": 0.5,
    "desk_mult_risk_off": 0.35,
    "short_usdt_cap": 1500.0,
    "stop_k_large": 1.5,
    "stop_k_mid": 2.0,
    "time_cap_hours_large": 24.0,
    "time_cap_hours_mid": 12.0,
    "max_loss_usdt": 80.0,
    "daily_short_loss_cap_usdt": 200.0,
    "liq_buffer": 0.05,
    "leverage": 2.0,
}

# Frozen calendar from the artefact. Ends are exclusive (next midnight UTC).
CANONICAL_FOLDS = (
    {
        "id": "F1",
        "train_start": "2026-07-01",
        "train_end": "2026-08-01",
        "oos_start": "2026-08-01",
        "oos_end": "2026-08-15",
    },
    {
        "id": "F2",
        "train_start": "2026-07-01",
        "train_end": "2026-08-15",
        "oos_start": "2026-08-15",
        "oos_end": "2026-08-29",
    },
    {
        "id": "F3",
        "train_start": "2026-07-01",
        "train_end": "2026-08-29",
        "oos_start": "2026-08-29",
        "oos_end": "2026-09-12",
    },
    {
        "id": "F4",
        "train_start": "2026-07-01",
        "train_end": "2026-09-12",
        "oos_start": "2026-09-12",
        "oos_end": "2026-09-29",
    },
)

STOP_SOURCES_A = frozenset({"trailing_stop", "liquidation"})
STOP_SOURCES_B = frozenset({"watchlist_stop", "liquidation", "watchlist_max_loss"})


def _utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


def parse_day(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class Exit:
    id: str
    symbol: str
    ts: datetime
    exit_source: str
    signal: str
    source: str
    pnl: float
    usdt: float
    timeframe: str
    ledger: str


@dataclass
class Bar:
    ts: int  # open, unix seconds
    open: float
    high: float
    low: float
    close: float

    @property
    def open_dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc)

    @property
    def close_dt(self) -> datetime:
        return self.open_dt + timedelta(seconds=BAR_SECONDS)


@dataclass
class Trade:
    arm: str
    symbol: str
    exit_id: str
    entry_ts: datetime
    exit_ts: datetime
    entry_px: float
    exit_px: float
    qty: float
    notional: float
    gross: float
    fees: float
    funding: float
    pnl: float
    cover_source: str
    fold_id: str = ""


@dataclass
class WatchOutcome:
    arm: str
    exit_id: str
    symbol: str
    exit_ts: datetime
    status: str  # opened | expired | censored | rejected
    reason: str = ""
    fold_id: str = ""


def fee_funding(notional_entry: float, entry_px: float, exit_px: float, qty: float, hours: float) -> tuple[float, float, float, float]:
    """Return gross, fees, funding, net. Short pays funding when rate > 0."""
    gross = qty * (entry_px - exit_px)
    exit_notional = abs(qty * exit_px)
    fees = (abs(notional_entry) + exit_notional) * FEE_FRAC
    funding = abs(notional_entry) * FUNDING_RATE_8H * (max(0.0, hours) / 8.0)
    net = gross - fees - funding
    return gross, fees, funding, net


def profit_factor(pnls: list[float]) -> float | None:
    """Gross wins / gross losses on net trade PnL. n>0 and no losses → inf."""
    if not pnls:
        return None
    wins = sum(p for p in pnls if p > 0)
    losses = sum(-p for p in pnls if p < 0)
    if losses == 0:
        return math.inf
    return wins / losses


def max_drawdown_usdt(pnls_in_time: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for p in pnls_in_time:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > worst:
            worst = dd
    return worst


def fold_metrics(trades: list[Trade], *, stop_sources: frozenset[str], expired: int | None) -> dict[str, Any]:
    n = len(trades)
    pnls = [t.pnl for t in trades]
    if n == 0:
        pf: float | None = None
        winrate: float | None = None
        stop_pct: float | None = None
    else:
        pf = profit_factor(pnls)
        winrate = sum(1 for p in pnls if p > 0) / n
        stop_pct = sum(1 for t in trades if t.cover_source in stop_sources) / n
    return {
        "n": n,
        "pf": pf,
        "maxdd": max_drawdown_usdt(pnls),
        "winrate": winrate,
        "stop_pct": stop_pct,
        "expired_wo_open": expired,
        "sum_pnl": sum(pnls) if n else 0.0,
    }


def _day_span(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into up to 3 OOS slices. End exclusive."""
    total = (end - start).total_seconds()
    if total <= 0:
        return []
    n = 3
    edges = [start + timedelta(seconds=total * i / n) for i in range(n + 1)]
    # Align interior edges to UTC midnights when the span is multi-day, so
    # folds are readable. Keep the exact start and end.
    aligned = [edges[0]]
    for edge in edges[1:-1]:
        midnight = _utc(edge.year, edge.month, edge.day)
        if midnight <= aligned[-1]:
            midnight = aligned[-1] + timedelta(days=1)
        if midnight >= end:
            continue
        aligned.append(midnight)
    aligned.append(end)
    # Dedup and ensure 3 slices when possible by falling back to equal cuts.
    cleaned: list[datetime] = []
    for edge in aligned:
        if not cleaned or edge > cleaned[-1]:
            cleaned.append(edge)
    if len(cleaned) - 1 < 3:
        cleaned = edges
    slices = []
    for i in range(len(cleaned) - 1):
        slices.append((cleaned[i], cleaned[i + 1]))
    return slices


def build_folds(exits: list[Exit], req_start: datetime, req_end: datetime) -> dict[str, Any]:
    """Use canonical F1–F4 when ≥3 OOS folds contain exits; else adapt."""
    canonical = []
    for spec in CANONICAL_FOLDS:
        canonical.append(
            {
                "id": spec["id"],
                "train_start": parse_day(spec["train_start"]),
                "train_end": parse_day(spec["train_end"]),
                "oos_start": parse_day(spec["oos_start"]),
                "oos_end": parse_day(spec["oos_end"]),
            }
        )
    if not exits:
        return {
            "folds": canonical,
            "adapted": False,
            "window_start": req_start,
            "window_end": req_end,
            "reason": "no filled long exits in the loaded ledgers",
            "canonical_oos_with_events": [],
            "wf_incomplete": True,
        }
    data_start = min(e.ts for e in exits)
    data_end = max(e.ts for e in exits) + timedelta(seconds=1)
    in_request = [e for e in exits if req_start <= e.ts < req_end]
    if in_request:
        window_start = min(e.ts for e in in_request)
        window_end = max(e.ts for e in in_request) + timedelta(seconds=1)
        outside = len(exits) - len(in_request)
    else:
        window_start = data_start
        window_end = data_end
        outside = len(exits)

    def _hits(folds: list[dict]) -> list[str]:
        hit = []
        for fold in folds:
            if any(fold["oos_start"] <= e.ts < fold["oos_end"] for e in exits):
                hit.append(fold["id"])
        return hit

    canon_hits = _hits(canonical)
    target_covered = req_start <= data_start and data_end <= req_end + timedelta(days=1)
    # Canonical fold contract is the Jul–Sep calendar. Use it only when at
    # least 3 of those OOS windows actually contain exits.
    if len(canon_hits) >= 3:
        return {
            "folds": canonical,
            "adapted": False,
            "window_start": window_start,
            "window_end": window_end,
            "reason": "canonical F1–F4 each have exit mass in at least 3 OOS windows",
            "canonical_oos_with_events": canon_hits,
            "wf_incomplete": False,
            "exits_outside_request": outside,
        }

    # Largest honest window: every loaded exit, expanded to UTC day bounds
    # so the last exit's calendar day is inside the last fold.
    day0 = _utc(data_start.year, data_start.month, data_start.day)
    day1 = _utc(data_end.year, data_end.month, data_end.day) + timedelta(days=1)
    slices = _day_span(day0, day1)
    folds = []
    for i, (a, b) in enumerate(slices, start=1):
        folds.append(
            {
                "id": f"F{i}",
                "train_start": day0,
                "train_end": a,
                "oos_start": a,
                "oos_end": b,
            }
        )
    reason = (
        "Target ledger 2026-07-01→2026-09-28 is not in the loaded fills. "
        f"Canonical OOS windows with ≥1 exit: {canon_hits or 'none'}. "
        "Folds adapted to the largest continuous real-fill window "
        "(equal UTC slices, expanding train prefix unused for parameters)."
    )
    return {
        "folds": folds,
        "adapted": True,
        "window_start": day0,
        "window_end": day1,
        "reason": reason,
        "canonical_oos_with_events": canon_hits,
        "wf_incomplete": len(folds) < 3,
        "exits_outside_request": 0 if in_request else outside,
        "requested_window_exit_count": len(in_request),
        "target_window_fully_covered": bool(target_covered and len(canon_hits) >= 3),
    }


def assign_fold(ts: datetime, folds: list[dict]) -> str:
    for fold in folds:
        if fold["oos_start"] <= ts < fold["oos_end"]:
            return fold["id"]
    return ""


def wilder_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = []
    losses = []
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    out[period] = 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g = max(ch, 0.0)
        l = max(-ch, 0.0)
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        out[i] = 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    return out


def wilder_atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    if len(bars) <= period:
        return out
    trs: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            tr = bar.high - bar.low
        else:
            prev = bars[i - 1].close
            tr = max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev))
        trs.append(tr)
    acc = sum(trs[1 : period + 1]) / period
    out[period] = acc
    for i in range(period + 1, len(bars)):
        acc = (acc * (period - 1) + trs[i]) / period
        out[i] = acc
    return out


def _index_at_or_before(bars: list[Bar], ts: datetime) -> int | None:
    """Last bar whose close is <= ts (closed bars only)."""
    target = int(ts.timestamp())
    lo, hi = 0, len(bars) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if bars[mid].ts + BAR_SECONDS <= target:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _next_open_index(bars: list[Bar], ts: datetime) -> int | None:
    """First bar whose open is strictly after ts (next 1h open)."""
    target = int(ts.timestamp())
    for i, bar in enumerate(bars):
        if bar.ts > target:
            return i
    return None


def enqueue_key_hit(exit: Exit) -> bool:
    keys = {exit.exit_source, exit.signal, exit.source}
    if exit.source == "manual":
        keys.add("manual_sell")
    return bool(keys & set(ENQUEUE_SOURCES))


def _lookback_return(bars: list[Bar], idx: int, lookback: int) -> float | None:
    j = idx - lookback
    if j < 0:
        return None
    base = bars[j].close
    if base <= 0:
        return None
    return bars[idx].close / base - 1.0


def bounce_failed(bars: list[Bar], idx: int, atr: float) -> bool | None:
    look = int(SETUP["bounce_lookback_bars_1h"])
    if idx + 1 < look or atr <= 0:
        return None
    window = bars[idx - look + 1 : idx + 1]
    if len(window) < look:
        return None
    bounce_high = max(b.high for b in window)
    bounce_low = min(b.low for b in window)
    bounced = (bounce_high - bounce_low) >= float(SETUP["bounce_atr_mult"]) * atr
    failed = bars[idx].close <= bounce_high - float(SETUP["bounce_fail_atr"]) * atr
    return bool(bounced and failed)


def cap_class_for_mcap(mcap: float | None) -> str | None:
    if mcap is None:
        return None
    if mcap >= float(SETUP["large_mcap_usd"]):
        return "large"
    return "mid"


def setup_gates(
    *,
    regime: str | None,
    atr: float | None,
    close: float,
    rsi: float | None,
    mcap: float | None,
    bounce: bool | None,
    coin_ret: float | None,
    btc_ret: float | None,
    post_long_blocked: bool,
    reentry_blocked: bool,
    symbol_open: bool,
    open_count: int,
    max_open: int,
    daily_pnl: float,
    has_next_bar: bool,
) -> tuple[bool, list[str], dict[str, Any]]:
    """All G0–G8. Missing regime / ATR / RSI / mcap / bounce fail closed."""
    reasons: list[str] = []
    if post_long_blocked:
        reasons.append("G0_post_long")
    if reentry_blocked:
        reasons.append("G0_reentry")

    regime_ok = regime in REGIME_ALLOW
    if regime is None:
        reasons.append("G1_regime_missing")
    elif not regime_ok:
        reasons.append("G1_regime")

    atr_pct = None
    atr_ok = False
    if atr is None or close <= 0:
        reasons.append("G2_atr_missing")
    else:
        atr_pct = atr / close * 100.0
        atr_ok = float(SETUP["atr_pct_min"]) <= atr_pct <= float(SETUP["atr_pct_max"])
        if not atr_ok:
            reasons.append("G2_atr_band")

    klass = cap_class_for_mcap(mcap)
    rsi_ok = False
    if mcap is None:
        reasons.append("G7_mcap_unknown")
    elif mcap < float(SETUP["mcap_floor_usd"]):
        reasons.append("G7_mcap_floor")
    if rsi is None or klass is None:
        reasons.append("G4_rsi_missing")
    else:
        if klass == "large":
            rsi_ok = float(SETUP["rsi_large_min"]) <= rsi <= float(SETUP["rsi_large_max"])
        else:
            rsi_ok = float(SETUP["rsi_mid_min"]) <= rsi <= float(SETUP["rsi_mid_max"])
        if not rsi_ok:
            reasons.append("G4_rsi_band")

    under_ok = True
    if klass == "mid":
        if coin_ret is None or btc_ret is None:
            reasons.append("G5_return_missing")
            under_ok = False
        else:
            # (1+r_coin)/(1+r_btc) < 1  <=>  coin underperformed BTC.
            under_ok = (1.0 + coin_ret) < (1.0 + btc_ret)
            if not under_ok:
                reasons.append("G5_btc_underperf")

    if bounce is None:
        reasons.append("G6_bounce_missing")
        bounce_ok = False
    else:
        bounce_ok = bounce
        if not bounce_ok:
            reasons.append("G6_bounce")

    c1 = regime_ok
    c2 = atr_ok
    c3 = rsi_ok
    c4 = bounce_ok
    confluence = int(c1) + int(c2) + int(c3) + int(c4)
    if confluence < int(SETUP["confluence_min"]):
        reasons.append("G3_confluence")

    if symbol_open:
        reasons.append("G7_symbol_open")
    if open_count >= max_open:
        reasons.append("G7_max_open")
    if daily_pnl <= -float(SETUP["daily_short_loss_cap_usdt"]):
        reasons.append("G7_daily_loss")
    if not has_next_bar:
        reasons.append("G8_no_next_bar")

    info = {
        "atr_pct": atr_pct,
        "confluence": confluence,
        "cap_class": klass,
        "rsi": rsi,
        "regime": regime,
    }
    return (len(reasons) == 0), reasons, info


def _arm_a_config() -> dict:
    """Ist cover params from staging config.json, fee forced to the freeze 7.5bp.

    fee_source is not 'auto' so CostModel does not hit the network.
    """
    raw = json.loads((BOT_ROOT / "config.json").read_text(encoding="utf-8"))
    shorts = dict(raw.get("shorts") or {})
    # Measurement must not follow a live allow_live flip if one were present.
    shorts["allow_live"] = False
    return {
        "shorts": shorts,
        "costs": {
            "fee_source": "freeze_7_5bp",
            "gate": {
                "swap": {
                    "fee_taker_pct": FEE_BP_SIDE / 100.0,
                    "fee_maker_pct": FEE_BP_SIDE / 100.0,
                    "slippage_pct": 0.0,
                }
            },
        },
        "max_usdt_per_trade": float(raw.get("max_usdt_per_trade") or 4500),
    }


def _liq_and_stop(entry: float, leverage: float) -> tuple[float, float]:
    stop = stop_price("short", entry, 0.12, leverage)
    liq = liquidation_price_isolated("short", entry, leverage, fee_frac=FEE_FRAC)
    liq = apply_liq_buffer("short", entry, liq, float(SETUP["liq_buffer"]))
    return stop, liq


def simulate_arm_a(
    exits: list[Exit],
    bars_by_symbol: dict[str, list[Bar]],
    *,
    config_raw: dict,
    folds: list[dict],
    max_open: int = 6,
) -> tuple[list[Trade], list[dict]]:
    cap = float(config_raw.get("max_usdt_per_trade") or 4500)
    ordered = sorted(exits, key=lambda e: e.ts)
    open_lots: list[dict] = []
    trades: list[Trade] = []
    skipped: list[dict] = []

    def _close_due(now: datetime) -> None:
        still = []
        for lot in open_lots:
            if lot["cover_ts"] <= now:
                trades.append(lot["trade"])
            else:
                still.append(lot)
        open_lots[:] = still

    for exit in ordered:
        if exit.exit_source not in set(AUTO_SOURCES):
            continue
        _close_due(exit.ts)
        if exit.usdt < float(SETUP["min_exit_notional_usdt"]):
            skipped.append({"id": exit.id, "reason": "notional"})
            continue
        if any(lot["symbol"] == exit.symbol for lot in open_lots):
            skipped.append({"id": exit.id, "reason": "symbol_already_short"})
            continue
        if len(open_lots) >= max_open:
            skipped.append({"id": exit.id, "reason": "max_open"})
            continue
        bars = bars_by_symbol.get(exit.symbol) or []
        fill_i = _next_open_index(bars, exit.ts)
        if fill_i is None:
            skipped.append({"id": exit.id, "reason": "no_ohlcv_after_exit"})
            continue
        rsi = wilder_rsi([b.close for b in bars], int(SETUP["rsi_period"]))
        entry_bar = bars[fill_i]
        entry_px = entry_bar.open
        if entry_px <= 0:
            skipped.append({"id": exit.id, "reason": "bad_entry_px"})
            continue
        notional = auto_short_notional_usdt(exit.usdt, cap=cap, config_raw=config_raw)
        if notional <= 0:
            skipped.append({"id": exit.id, "reason": "zero_size"})
            continue
        qty = notional / entry_px
        params = resolve_short_params(symbol=exit.symbol, tier="volatile", config_raw=config_raw)
        lev = float(params["leverage"])
        stop_px = stop_price("short", entry_px, float(params["stop_margin_pct"]), lev)
        liq_px = liquidation_price_isolated("short", entry_px, lev, fee_frac=FEE_FRAC)
        liq_px = apply_liq_buffer("short", entry_px, liq_px, float(params["liquidation_buffer"]))
        recent_low = entry_px
        cover_source = None
        cover_px = None
        cover_ts = None
        for i in range(fill_i, len(bars)):
            bar = bars[i]
            now = bar.close_dt
            # Adverse extreme before the favourable low, so a stop inside the
            # bar is not skipped in favour of a trail on the same bar.
            adverse = should_stop_or_liquidate("short", bar.high, stop=stop_px, liq=liq_px)
            if adverse:
                level = liq_px if adverse == "liquidation" else stop_px
                cover_source = "liquidation" if adverse == "liquidation" else "trailing_stop"
                cover_px = bar.open if bar.open >= level else level
                cover_ts = now
                break
            recent_low = min(recent_low, bar.low)
            pos = {
                "side": "short",
                "amount": qty,
                "average_entry": entry_px,
                "entry_at": iso(entry_bar.open_dt),
                "recent_low": recent_low,
                "last_rsi": rsi[i],
                "leverage": lev,
                "strategy_tier": "volatile",
            }
            hit = evaluate_short_cover(
                pos, bar.close, now=now, symbol=exit.symbol, config_raw=config_raw
            )
            if hit and hit["source"] in ("liquidation", "trailing_stop"):
                level = liq_px if hit["source"] == "liquidation" else stop_px
                cover_source = hit["source"]
                cover_px = bar.open if bar.open >= level else level
                cover_ts = now
                break
            if hit:
                cover_source = hit["source"]
                cover_px = bar.close
                cover_ts = now
                break
        if cover_source is None or cover_px is None or cover_ts is None:
            skipped.append({"id": exit.id, "reason": "censored_open_at_sample_end"})
            continue
        hours = (cover_ts - entry_bar.open_dt).total_seconds() / 3600.0
        gross, fees, funding, net = fee_funding(notional, entry_px, cover_px, qty, hours)
        trade = Trade(
            arm="A",
            symbol=exit.symbol,
            exit_id=exit.id,
            entry_ts=entry_bar.open_dt,
            exit_ts=cover_ts,
            entry_px=entry_px,
            exit_px=cover_px,
            qty=qty,
            notional=notional,
            gross=gross,
            fees=fees,
            funding=funding,
            pnl=net,
            cover_source=cover_source,
            fold_id=assign_fold(exit.ts, folds),
        )
        open_lots.append({"symbol": exit.symbol, "cover_ts": cover_ts, "trade": trade})
    _close_due(_utc(2100, 1, 1))
    trades.sort(key=lambda t: t.entry_ts)
    return trades, skipped


def _size_b(exit_usdt: float, max_usdt: float) -> float:
    return min(
        float(SETUP["size_factor_of_long_exit"]) * exit_usdt,
        float(SETUP["desk_mult_risk_off"]) * max_usdt,
        float(SETUP["short_usdt_cap"]),
    )


def simulate_arm_b(
    exits: list[Exit],
    bars_by_symbol: dict[str, list[Bar]],
    btc_bars: list[Bar],
    *,
    arm: str,
    skip_loss: bool,
    folds: list[dict],
    regime_at: Callable[[datetime], str | None],
    mcap_of: Callable[[str], float | None],
    clock_end: datetime,
    max_usdt: float,
    max_open: int = 6,
) -> tuple[list[Trade], list[WatchOutcome], Counter]:
    ordered = sorted(exits, key=lambda e: (e.ts, e.id))
    watching: list[dict] = []
    trades: list[Trade] = []
    outcomes: list[WatchOutcome] = []
    fail_hist: Counter = Counter()
    seen_keys: set[str] = set()
    last_short_exit: dict[str, datetime] = {}
    open_until: list[tuple[datetime, str]] = []
    daily_pnl: dict[str, float] = defaultdict(float)

    btc_rsi_unused = btc_bars  # kept so callers pass BTC even if gates short-circuit
    del btc_rsi_unused

    def _release(now: datetime) -> None:
        open_until[:] = [(ts, sym) for ts, sym in open_until if ts > now]

    def _open_symbols() -> set[str]:
        return {sym for _, sym in open_until}

    enqueued: list[dict] = []
    for exit in ordered:
        _release(exit.ts)
        # Concurrent cap: drop watchers whose TTL has already elapsed.
        watching = [w for w in watching if w["expiry"] > exit.ts]
        if not enqueue_key_hit(exit):
            outcomes.append(
                WatchOutcome(arm, exit.id, exit.symbol, exit.ts, "rejected", "source", assign_fold(exit.ts, folds))
            )
            continue
        if exit.usdt < float(SETUP["min_exit_notional_usdt"]):
            outcomes.append(
                WatchOutcome(arm, exit.id, exit.symbol, exit.ts, "rejected", "notional", assign_fold(exit.ts, folds))
            )
            continue
        if skip_loss and exit.pnl < 0:
            outcomes.append(
                WatchOutcome(arm, exit.id, exit.symbol, exit.ts, "rejected", "skip_loss", assign_fold(exit.ts, folds))
            )
            continue
        key = f"wl|{exit.symbol}|{exit.timeframe}|{int(exit.ts.timestamp() * 1000)}"
        if key in seen_keys:
            outcomes.append(
                WatchOutcome(arm, exit.id, exit.symbol, exit.ts, "rejected", "idempotent", assign_fold(exit.ts, folds))
            )
            continue
        if len(watching) >= int(SETUP["max_entries"]):
            outcomes.append(
                WatchOutcome(arm, exit.id, exit.symbol, exit.ts, "rejected", "max_entries", assign_fold(exit.ts, folds))
            )
            continue
        seen_keys.add(key)
        slot = {
            "exit": exit,
            "expiry": exit.ts + timedelta(hours=float(SETUP["ttl_hours"])),
            "status": "watching",
            "key": key,
        }
        watching.append(slot)
        enqueued.append(slot)

    # Walk each accepted watcher. Expired slots were removed from `watching`
    # only to free the cap; they still get a gate scan up to their TTL.
    watching = enqueued
    watching.sort(key=lambda w: w["exit"].ts)
    open_until.clear()
    trades.clear()
    # Re-simulate opens in time order of candidate bars via a merged cursor.
    events: list[tuple[datetime, int, dict]] = []
    prepared = []
    for w in watching:
        exit = w["exit"]
        bars = bars_by_symbol.get(exit.symbol) or []
        prepared.append((w, bars, wilder_atr(bars), wilder_rsi([b.close for b in bars])))
    # Candidate evaluation times: each bar close inside (exit+cooldown, expiry].
    for idx, (w, bars, _atr, _rsi) in enumerate(prepared):
        exit = w["exit"]
        ready = exit.ts + timedelta(minutes=float(SETUP["post_long_cooldown_min"]))
        for b_i, bar in enumerate(bars):
            if bar.close_dt < ready:
                continue
            if bar.close_dt >= w["expiry"] or bar.close_dt > clock_end:
                break
            events.append((bar.close_dt, idx, {"bar_i": b_i}))
    events.sort(key=lambda item: (item[0], item[1]))

    opened_ids: set[str] = set()
    btc_closes_idx = {b.ts: i for i, b in enumerate(btc_bars)}

    for now, idx, payload in events:
        w, bars, atrs, rsis = prepared[idx]
        exit = w["exit"]
        if exit.id in opened_ids or w["status"] != "watching":
            continue
        if now >= w["expiry"]:
            continue
        _release(now)
        bar_i = payload["bar_i"]
        bar = bars[bar_i]
        next_i = bar_i + 1
        has_next = next_i < len(bars) and bars[next_i].open_dt <= clock_end
        regime = regime_at(now)
        mcap = mcap_of(exit.symbol)
        atr = atrs[bar_i]
        rsi = rsis[bar_i]
        bounce = bounce_failed(bars, bar_i, atr) if atr is not None else None
        look = int(SETUP["bounce_lookback_bars_1h"])
        coin_ret = _lookback_return(bars, bar_i, look)
        btc_i = None
        # Match BTC bar by open timestamp.
        if bar.ts in btc_closes_idx:
            btc_i = btc_closes_idx[bar.ts]
        btc_ret = _lookback_return(btc_bars, btc_i, look) if btc_i is not None else None
        last = last_short_exit.get(exit.symbol)
        reentry_blocked = False
        if last is not None:
            reentry_blocked = (now - last).total_seconds() < float(SETUP["reentry_cooldown_min"]) * 60.0
        day_key = now.strftime("%Y-%m-%d")
        ok, reasons, _info = setup_gates(
            regime=regime,
            atr=atr,
            close=bar.close,
            rsi=rsi,
            mcap=mcap,
            bounce=bounce,
            coin_ret=coin_ret,
            btc_ret=btc_ret,
            post_long_blocked=False,  # ready filter already applied
            reentry_blocked=reentry_blocked,
            symbol_open=exit.symbol in _open_symbols(),
            open_count=len(open_until),
            max_open=max_open,
            daily_pnl=daily_pnl[day_key],
            has_next_bar=has_next,
        )
        if not ok:
            fail_hist[reasons[0]] += 1
            continue
        entry_bar = bars[next_i]
        entry_px = entry_bar.open
        if entry_px <= 0:
            fail_hist["G8_bad_entry"] += 1
            continue
        # G8 liq must be computable. It always is for a positive entry.
        _stop_unused, liq_px = _liq_and_stop(entry_px, float(SETUP["leverage"]))
        if liq_px <= 0:
            fail_hist["G8_liq_missing"] += 1
            continue
        notional = _size_b(exit.usdt, max_usdt)
        if notional <= 0:
            fail_hist["size"] += 1
            continue
        klass = cap_class_for_mcap(mcap) or "mid"
        k = float(SETUP["stop_k_large"] if klass == "large" else SETUP["stop_k_mid"])
        cap_h = float(SETUP["time_cap_hours_large"] if klass == "large" else SETUP["time_cap_hours_mid"])
        atr_entry = atr if atr and atr > 0 else None
        if atr_entry is None:
            fail_hist["G2_atr_missing"] += 1
            continue
        stop_px = entry_px + k * atr_entry
        qty = notional / entry_px
        cover = _cover_watchlist(
            bars,
            next_i,
            entry_px,
            qty,
            notional,
            stop_px,
            liq_px,
            cap_h,
            clock_end,
        )
        if cover is None:
            fail_hist["censored_after_open"] += 1
            # Still mark opened so we don't expire it; trade not in n.
            opened_ids.add(exit.id)
            w["status"] = "opened_censored"
            continue
        cover_ts, cover_px, cover_source = cover
        hours = (cover_ts - entry_bar.open_dt).total_seconds() / 3600.0
        gross, fees, funding, net = fee_funding(notional, entry_px, cover_px, qty, hours)
        trade = Trade(
            arm=arm,
            symbol=exit.symbol,
            exit_id=exit.id,
            entry_ts=entry_bar.open_dt,
            exit_ts=cover_ts,
            entry_px=entry_px,
            exit_px=cover_px,
            qty=qty,
            notional=notional,
            gross=gross,
            fees=fees,
            funding=funding,
            pnl=net,
            cover_source=cover_source,
            fold_id=assign_fold(exit.ts, folds),
        )
        trades.append(trade)
        open_until.append((cover_ts, exit.symbol))
        last_short_exit[exit.symbol] = cover_ts
        daily_pnl[cover_ts.strftime("%Y-%m-%d")] += net
        opened_ids.add(exit.id)
        w["status"] = "opened"
        outcomes.append(
            WatchOutcome(arm, exit.id, exit.symbol, exit.ts, "opened", cover_source, assign_fold(exit.ts, folds))
        )

    for w, _bars, _a, _r in prepared:
        exit = w["exit"]
        if exit.id in opened_ids:
            continue
        if w["status"] == "opened_censored":
            outcomes.append(
                WatchOutcome(arm, exit.id, exit.symbol, exit.ts, "censored", "open_unclosed", assign_fold(exit.ts, folds))
            )
            continue
        if w["expiry"] <= clock_end:
            outcomes.append(
                WatchOutcome(arm, exit.id, exit.symbol, exit.ts, "expired", "ttl", assign_fold(exit.ts, folds))
            )
        else:
            outcomes.append(
                WatchOutcome(arm, exit.id, exit.symbol, exit.ts, "censored", "ttl_after_sample", assign_fold(exit.ts, folds))
            )
    trades.sort(key=lambda t: t.entry_ts)
    return trades, outcomes, fail_hist


def _cover_watchlist(
    bars: list[Bar],
    entry_i: int,
    entry_px: float,
    qty: float,
    notional: float,
    stop_px: float,
    liq_px: float,
    cap_h: float,
    clock_end: datetime,
) -> tuple[datetime, float, str] | None:
    """Priority: liq → max_loss → stop → time. Fill on 1h OHLC."""
    entry_ts = bars[entry_i].open_dt
    for i in range(entry_i, len(bars)):
        bar = bars[i]
        if bar.open_dt > clock_end:
            break
        now = bar.close_dt
        # Adverse extreme first.
        if bar.high >= liq_px:
            px = bar.open if bar.open >= liq_px else liq_px
            return now, px, "liquidation"
        mtm = qty * (bar.high - entry_px)  # loss if price rises
        if mtm >= float(SETUP["max_loss_usdt"]):
            # Loss reaches the cap somewhere in the bar. Fill at the price
            # where loss == cap when that price is inside the bar, else high.
            # qty * (px - entry) = cap → px = entry + cap/qty
            if qty > 0:
                cap_px = entry_px + float(SETUP["max_loss_usdt"]) / qty
            else:
                cap_px = bar.high
            if bar.open >= cap_px:
                px = bar.open
            else:
                px = min(bar.high, max(cap_px, bar.low))
            return now, px, "watchlist_max_loss"
        if bar.high >= stop_px:
            px = bar.open if bar.open >= stop_px else stop_px
            return now, px, "watchlist_stop"
        age_h = (now - entry_ts).total_seconds() / 3600.0
        if age_h >= cap_h:
            return now, bar.close, "watchlist_time"
    return None


def _walk_orders(obj: Any, acc: list[dict]) -> None:
    if isinstance(obj, dict):
        if obj.get("side") and obj.get("symbol") and isinstance(obj.get("timestamps"), dict):
            acc.append(obj)
            return
        orders = obj.get("orders")
        if isinstance(orders, list):
            for item in orders:
                _walk_orders(item, acc)
            return
        for value in obj.values():
            if isinstance(value, (dict, list)):
                _walk_orders(value, acc)
    elif isinstance(obj, list):
        for value in obj:
            if isinstance(value, (dict, list)):
                _walk_orders(value, acc)


def load_exit_file(path: Path) -> list[Exit]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    blob: list[dict] = []
    if isinstance(raw, list):
        blob = [row for row in raw if isinstance(row, dict)]
    else:
        _walk_orders(raw, blob)
    exits: list[Exit] = []
    for order in blob:
        slim = "ts" in order and "timestamps" not in order
        if not slim:
            if str(order.get("status") or "").lower() not in ("filled", ""):
                continue
            if str(order.get("side") or "").lower() != "sell":
                continue
        if str(order.get("signal") or "") != "SELL_FULL":
            continue
        ts = parse_ts(
            order.get("ts")
            or (order.get("timestamps") or {}).get("filled")
            or (order.get("timestamps") or {}).get("created")
        )
        if ts is None:
            continue
        exe = order.get("execution") if isinstance(order.get("execution"), dict) else {}
        try:
            usdt = float(exe.get("usdt") or 0)
        except (TypeError, ValueError):
            usdt = 0.0
        try:
            pnl = float(order.get("pnl") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        exits.append(
            Exit(
                id=str(order.get("id") or ""),
                symbol=str(order.get("symbol") or ""),
                ts=ts,
                exit_source=str(order.get("exit_source") or ""),
                signal=str(order.get("signal") or ""),
                source=str(order.get("source") or ""),
                pnl=pnl,
                usdt=usdt,
                timeframe=str(order.get("timeframe") or "1h"),
                ledger=str(path),
            )
        )
    return exits


def load_exits(paths: list[Path]) -> list[Exit]:
    seen: set[str] = set()
    out: list[Exit] = []
    for path in paths:
        for exit in load_exit_file(path):
            key = exit.id or f"{exit.symbol}|{iso(exit.ts)}|{exit.usdt}"
            if key in seen:
                continue
            seen.add(key)
            out.append(exit)
    out.sort(key=lambda e: e.ts)
    return out


def fetch_gate_1h(symbol: str, start: datetime, end: datetime, *, pause: float = 0.12) -> list[Bar]:
    """Public Gate spot 1h candles. A later-page error keeps bars already parsed.

    Chunks stay under the exchange's point cap. The in-progress hour is dropped.
    """
    pair = symbol.replace("/", "_")
    url_base = "https://api.gateio.ws/api/v4/spot/candlesticks"
    cursor = int(start.timestamp())
    end_s = int(end.timestamp())
    rows: dict[int, Bar] = {}
    while cursor < end_s:
        chunk_end = min(end_s, cursor + BAR_SECONDS * 240)
        query = urllib.parse.urlencode(
            {
                "currency_pair": pair,
                "interval": "1h",
                "from": str(cursor),
                "to": str(chunk_end),
            }
        )
        req = urllib.request.Request(
            url_base + "?" + query,
            headers={"Accept": "application/json", "User-Agent": "wf-watchlist-abc/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            break
        if not isinstance(payload, list) or not payload:
            break
        max_ts = cursor
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            if len(row) >= 8 and str(row[7]).lower() != "true":
                continue
            ts = int(float(row[0]))
            # Gate: t, quote_volume, close, high, low, open, base_volume
            try:
                close = float(row[2])
                high = float(row[3])
                low = float(row[4])
                opn = float(row[5])
            except (TypeError, ValueError):
                continue
            if ts < int(start.timestamp()) or ts >= end_s:
                continue
            rows[ts] = Bar(ts=ts, open=opn, high=high, low=low, close=close)
            if ts > max_ts:
                max_ts = ts
        nxt = max_ts + BAR_SECONDS
        if nxt <= cursor:
            break
        cursor = nxt
        if pause:
            time.sleep(pause)
    return [rows[k] for k in sorted(rows)]


def load_or_fetch_bars(
    symbols: list[str],
    start: datetime,
    end: datetime,
    cache_dir: Path,
    *,
    fetch: Callable[..., list[Bar]] = fetch_gate_1h,
) -> dict[str, list[Bar]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, list[Bar]] = {}
    for symbol in symbols:
        name = symbol.replace("/", "_") + "_1h.json"
        path = cache_dir / name
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            out[symbol] = [Bar(**row) for row in data]
            continue
        bars = fetch(symbol, start, end)
        # Do not cache a total miss: a transport error must be retried.
        if bars:
            path.write_text(
                json.dumps([bar.__dict__ for bar in bars]),
                encoding="utf-8",
            )
        out[symbol] = bars
    return out


def _json_num(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    if isinstance(value, float):
        return round(value, 6)
    return value


def _fmt_pf(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, str):
        return value
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    return f"{float(value):.3f}"


def _fmt_num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _fmt_expired(value: Any, arm: str) -> str:
    if arm in ("A", "C"):
        return "—"
    if value is None:
        return "n/a"
    return str(int(value))


def render_table(rows: list[dict]) -> str:
    header = "| Fold | Arm | n | PF | MaxDD | Winrate | Stop% | expired_wo_open | sum_pnl |"
    sep = "|------|-----|---|----|-------|---------|-------|-----------------|---------|"
    lines = [header, sep]
    for row in rows:
        lines.append(
            "| {fold} | {arm} | {n} | {pf} | {dd} | {wr} | {stop} | {exp} | {pnl} |".format(
                fold=row["fold"],
                arm=row["arm"],
                n=row["n"],
                pf=_fmt_pf(row["pf"]),
                dd=_fmt_num(row["maxdd"]),
                wr=_fmt_pf(row["winrate"]) if row["arm"] != "C" else "n/a",
                stop=_fmt_pf(row["stop_pct"]) if row["arm"] != "C" else "n/a",
                exp=_fmt_expired(row["expired_wo_open"], row["arm"]),
                pnl=_fmt_num(row["sum_pnl"]),
            )
        )
    return "\n".join(lines)


def _row_from_metrics(fold: str, arm: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "fold": fold,
        "arm": arm,
        "n": metrics["n"],
        "pf": _json_num(metrics["pf"]),
        "maxdd": round(float(metrics["maxdd"]), 6),
        "winrate": _json_num(metrics["winrate"]),
        "stop_pct": _json_num(metrics["stop_pct"]),
        "expired_wo_open": metrics["expired_wo_open"],
        "sum_pnl": round(float(metrics["sum_pnl"]), 6),
    }


def aggregate(trades: list[Trade], outcomes: list[WatchOutcome], arm: str, folds: list[dict]) -> dict[str, Any]:
    oos_ids = {f["id"] for f in folds}
    pooled = [t for t in trades if t.fold_id in oos_ids]
    if arm in ("B0", "B1"):
        expired = sum(1 for o in outcomes if o.status == "expired" and o.fold_id in oos_ids)
    else:
        expired = None
    stops = STOP_SOURCES_B if arm.startswith("B") else STOP_SOURCES_A
    return fold_metrics(pooled, stop_sources=stops, expired=expired)


def soft_gates(metrics_by_arm: dict[str, dict[str, Any]], per_fold: dict[str, dict[str, dict]]) -> dict[str, Any]:
    """Literal §4 checks. n=0 folds are inconclusive, not failures."""
    out: dict[str, Any] = {}
    a = metrics_by_arm["A"]
    for arm in ("B0", "B1"):
        b = metrics_by_arm[arm]
        fold_flags = []
        for fold_id, arms in per_fold.items():
            cell = arms[arm]
            if cell["n"] == 0:
                fold_flags.append({"fold": fold_id, "state": "empty"})
            elif cell["sum_pnl"] < 0:
                fold_flags.append({"fold": fold_id, "state": "fail"})
            else:
                fold_flags.append({"fold": fold_id, "state": "pass"})
        states = [f["state"] for f in fold_flags]
        not_all_fail = any(s != "fail" for s in states) if states else False
        any_traded_pass = any(s == "pass" for s in states)
        n_b = int(b["n"])
        n_a = int(a["n"])
        sum_ok = float(b["sum_pnl"]) >= float(a["sum_pnl"])
        pf_b = b["pf"]
        pf_a = a["pf"]
        pf_ok = False
        if isinstance(pf_b, (int, float)) and isinstance(pf_a, (int, float)):
            pf_ok = float(pf_b) >= float(pf_a)
        n_need = max(10, math.ceil(0.5 * n_a))
        b_vs_a = bool(sum_ok or (pf_ok and n_b >= n_need))
        if n_b < 10:
            b_vs_c = {"pass": False, "reason": "n<10; Go-P2 soft-gate not applicable and not passed"}
        else:
            pf_pos = isinstance(pf_b, (int, float)) and not isinstance(pf_b, bool) and float(pf_b) >= 1.0
            b_vs_c = {
                "pass": bool(float(b["sum_pnl"]) > 0 and pf_pos),
                "reason": "n>=10 requires sum_pnl>0 and PF>=1",
            }
        out[arm] = {
            "folds": fold_flags,
            "not_all_folds_fail_literal": not_all_fail,
            "any_traded_fold_nonnegative": any_traded_pass,
            "b_vs_a_pass": b_vs_a,
            "b_vs_a_detail": {
                "sum_pnl_b": b["sum_pnl"],
                "sum_pnl_a": a["sum_pnl"],
                "sum_pnl_ge": sum_ok,
                "pf_b": _json_num(pf_b),
                "pf_a": _json_num(pf_a),
                "pf_ge": pf_ok,
                "n_b": n_b,
                "n_required": n_need,
            },
            "b_vs_c": b_vs_c,
            "soft_gates_pass": bool(not_all_fail and any_traded_pass and b_vs_a and b_vs_c["pass"]),
        }
    return out


def run_measurement(
    exits: list[Exit],
    bars_by_symbol: dict[str, list[Bar]],
    btc_bars: list[Bar],
    *,
    folds_meta: dict[str, Any],
    regime_at: Callable[[datetime], str | None] | None = None,
    mcap_of: Callable[[str], float | None] | None = None,
    clock_end: datetime | None = None,
    config_raw: dict | None = None,
) -> dict[str, Any]:
    folds = folds_meta["folds"]
    config_raw = config_raw or _arm_a_config()
    max_usdt = float(config_raw.get("max_usdt_per_trade") or 4500)
    if regime_at is None:
        regime_at = lambda _ts: None
    if mcap_of is None:
        mcap_of = lambda _sym: None
    if clock_end is None:
        stamps = [b.close_dt for bars in bars_by_symbol.values() for b in bars[-1:]]
        clock_end = max(stamps) if stamps else folds_meta["window_end"]

    trades_a, skipped_a = simulate_arm_a(exits, bars_by_symbol, config_raw=config_raw, folds=folds)
    trades_b0, out_b0, hist_b0 = simulate_arm_b(
        exits, bars_by_symbol, btc_bars,
        arm="B0", skip_loss=True, folds=folds, regime_at=regime_at, mcap_of=mcap_of,
        clock_end=clock_end, max_usdt=max_usdt,
    )
    trades_b1, out_b1, hist_b1 = simulate_arm_b(
        exits, bars_by_symbol, btc_bars,
        arm="B1", skip_loss=False, folds=folds, regime_at=regime_at, mcap_of=mcap_of,
        clock_end=clock_end, max_usdt=max_usdt,
    )
    by_arm_trades = {"A": trades_a, "B0": trades_b0, "B1": trades_b1, "C": []}
    by_arm_out = {"A": [], "B0": out_b0, "B1": out_b1, "C": []}

    rows = []
    per_fold: dict[str, dict[str, dict]] = {}
    for fold in folds:
        per_fold[fold["id"]] = {}
        for arm in ("A", "B0", "B1", "C"):
            if arm == "C":
                metrics = {
                    "n": 0,
                    "pf": None,
                    "maxdd": 0.0,
                    "winrate": None,
                    "stop_pct": None,
                    "expired_wo_open": None,
                    "sum_pnl": 0.0,
                }
            else:
                subset = [t for t in by_arm_trades[arm] if t.fold_id == fold["id"]]
                if arm.startswith("B"):
                    expired = sum(
                        1 for o in by_arm_out[arm] if o.status == "expired" and o.fold_id == fold["id"]
                    )
                    stops = STOP_SOURCES_B
                else:
                    expired = None
                    stops = STOP_SOURCES_A
                metrics = fold_metrics(subset, stop_sources=stops, expired=expired)
            per_fold[fold["id"]][arm] = metrics
            rows.append(_row_from_metrics(fold["id"], arm, metrics))

    agg = {}
    for arm in ("A", "B0", "B1", "C"):
        if arm == "C":
            metrics = {
                "n": 0,
                "pf": None,
                "maxdd": 0.0,
                "winrate": None,
                "stop_pct": None,
                "expired_wo_open": None,
                "sum_pnl": 0.0,
            }
        else:
            metrics = aggregate(by_arm_trades[arm], by_arm_out[arm], arm, folds)
            metrics["pf"] = _json_num(metrics["pf"])
            metrics["winrate"] = _json_num(metrics["winrate"])
            metrics["stop_pct"] = _json_num(metrics["stop_pct"])
        agg[arm] = metrics
        rows.append(_row_from_metrics("Agg OOS", arm, metrics))

    # soft_gates wants numeric pf; recompute from trades to avoid the "inf" string.
    numeric_agg = {}
    for arm in ("A", "B0", "B1", "C"):
        if arm == "C":
            numeric_agg[arm] = agg[arm]
        else:
            numeric_agg[arm] = aggregate(by_arm_trades[arm], by_arm_out[arm], arm, folds)
    gates = soft_gates(numeric_agg, per_fold)

    def _trade_dict(t: Trade) -> dict:
        return {
            "arm": t.arm,
            "symbol": t.symbol,
            "exit_id": t.exit_id,
            "fold": t.fold_id,
            "entry_ts": iso(t.entry_ts),
            "exit_ts": iso(t.exit_ts),
            "entry_px": t.entry_px,
            "exit_px": t.exit_px,
            "qty": t.qty,
            "notional": round(t.notional, 4),
            "gross": round(t.gross, 4),
            "fees": round(t.fees, 4),
            "funding": round(t.funding, 4),
            "pnl": round(t.pnl, 4),
            "cover_source": t.cover_source,
        }

    return {
        "rows": rows,
        "per_fold": {
            fid: {arm: {k: _json_num(v) if k in ("pf", "winrate", "stop_pct") else v for k, v in cell.items()} for arm, cell in arms.items()}
            for fid, arms in per_fold.items()
        },
        "agg": {arm: {k: _json_num(v) if k in ("pf", "winrate", "stop_pct") else v for k, v in cell.items()} for arm, cell in agg.items()},
        "soft_gates": gates,
        "trades": [_trade_dict(t) for t in trades_a + trades_b0 + trades_b1],
        "skipped_a": skipped_a,
        "watch_b0": [o.__dict__ | {"exit_ts": iso(o.exit_ts)} for o in out_b0],
        "watch_b1": [o.__dict__ | {"exit_ts": iso(o.exit_ts)} for o in out_b1],
        "gate_fail_first_b0": dict(hist_b0),
        "gate_fail_first_b1": dict(hist_b1),
        "clock_end": iso(clock_end),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    folds = payload["data_window"]["folds"]
    fold_lines = [
        "| Fold | Train (UTC) | OOS (UTC) |",
        "|------|-------------|-----------|",
    ]
    for fold in folds:
        fold_lines.append(
            f"| {fold['id']} | {fold['train_start']} → {fold['train_end']} | {fold['oos_start']} → {fold['oos_end']} |"
        )
    rec = payload["recommendation"]
    lines = [
        f"# #616 Walk-Forward Artefakt — gemessen",
        "",
        f"**Freeze-ID:** `{payload['freeze_id']}`",
        f"**WF_INCOMPLETE:** `{str(payload['WF_INCOMPLETE']).lower()}`",
        f"**shorts.allow_live:** false (not modified). Paper/research only.",
        "",
        "## Data window",
        "",
        payload["data_window"]["reason"],
        "",
        f"- Requested (--end inclusive): {payload['data_window']['requested_start']} → {payload['data_window'].get('requested_end_inclusive', payload['data_window']['requested_end'])} UTC",
        f"- Used: {payload['data_window']['used_start']} → {payload['data_window']['used_end']} UTC",
        f"- Folds adapted: {payload['data_window']['adapted']}",
        f"- Canonical OOS windows with ≥1 exit: {payload['data_window']['canonical_oos_with_events']}",
        f"- Long full exits in sample: {payload['data_window']['n_exits']}",
        f"- Allowlisted for Arm A (`AUTO_SOURCES`): {payload['data_window']['n_allowlisted']}",
        "",
        "\n".join(fold_lines),
        "",
        "## 5a OOS",
        "",
        render_table(payload["rows"]),
        "",
        "PF / Winrate / Stop% are on net short PnL after 7.5 bp/side and 0.01%/8h funding.",
        "MaxDD is the peak-to-trough of the fold's short equity (start 0), in USDT.",
        "Agg OOS pools closed shorts whose **entry decision** (the long exit) falls in an OOS fold.",
        "",
        "## Soft gates",
        "",
        "```json",
        json.dumps(payload["soft_gates"], indent=2, default=str),
        "```",
        "",
        f"**Recommendation:** {rec}",
        "",
        "## Assumptions",
        "",
    ]
    for item in payload["assumptions"]:
        lines.append(f"- {item}")
    lines += [
        "",
        "## Repro",
        "",
        "```",
        payload["repro"],
        "```",
        "",
    ]
    return "\n".join(lines) + "\n"


def _recommendation(gates: dict[str, Any], incomplete: bool, b0_opens: int) -> str:
    b0_pass = bool(gates.get("B0", {}).get("soft_gates_pass"))
    if incomplete or not b0_pass or b0_opens == 0:
        return "keep veto:lena"
    return "grant review:lena"


def measure_files(
    order_paths: list[Path],
    *,
    req_start: datetime,
    req_end: datetime,
    out_dir: Path,
    freeze_id: str,
    fetch_bars: bool = True,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    exits = load_exits(order_paths)
    meta = build_folds(exits, req_start, req_end)
    symbols = sorted({e.symbol for e in exits if e.symbol})
    warmup_start = meta["window_start"] - timedelta(days=20)
    fetch_end = meta["window_end"] + timedelta(days=3)
    cache = cache_dir or (out_dir / "ohlcv_cache")
    if fetch_bars:
        bars = load_or_fetch_bars(symbols + ["BTC/USDT"], warmup_start, fetch_end, cache)
    else:
        bars = {sym: [] for sym in symbols}
        bars["BTC/USDT"] = []
    btc = bars.get("BTC/USDT") or []
    symbol_bars = {sym: bars.get(sym) or [] for sym in symbols}
    clock_candidates = [b.close_dt for series in symbol_bars.values() for b in series[-1:]]
    if btc:
        clock_candidates.append(btc[-1].close_dt)
    clock_end = max(clock_candidates) if clock_candidates else meta["window_end"]
    config_raw = _arm_a_config()
    measured = run_measurement(
        exits,
        symbol_bars,
        btc,
        folds_meta=meta,
        regime_at=lambda _ts: None,
        mcap_of=lambda _sym: None,
        clock_end=clock_end,
        config_raw=config_raw,
    )
    n_allow = sum(1 for e in exits if e.exit_source in set(AUTO_SOURCES))
    assumptions = [
        "Event sample is filled SELL_FULL orders from the files passed to --orders, deduped by id. Partials are excluded.",
        "Demo-scope ledgers under data/ are not mixed in unless their path is passed explicitly. They have no exit_source, a different ledger_scope, and a gap after 2026-07-07.",
        "Tenant default MCP list_orders_public caps at 200 most recent fills (services/mcp/explain.py MAX_LIMIT). A 2160h query on 2026-09-29 still returned 2026-09-20→2026-09-29 only.",
        "No historical desk-regime tape (RISK_OFF / HARVEST) is in the repo or the order export. regime_at() is None on every bar. Arm B gates fail closed (G1_regime_missing). Regime was not inferred from price.",
        "No historical market-cap tape. mcap_of() is None. Arm B also fails G7_mcap_unknown. Arm A is not dropped for missing mcap; the exits are the operator's real fills. Universe mcap≥100M could not be applied.",
        "No pair-map deny list is applied (none found in repo). Lock flags are absent from the export; the lock gate is not tripped because no lock flag is present.",
        "SELL_FULL is treated as a flat book on that symbol for the counterfactual. Other-timeframe residual longs are not in the export.",
        f"Arm A allowlist is strategies.short_policy.AUTO_SOURCES on this checkout: {list(AUTO_SOURCES)}. Size is auto_short_notional_usdt (0.35×sell, cap max_usdt_per_trade). Tier is volatile because orders carry no strategy_tier, so evaluate_short_cover time_cap is 4h.",
        "Arm A cover is evaluate_short_cover on 1h OHLC: high is tested first for liq/stop; then low updates recent_low; close is tested for trail, RSI, and time. Stop/liq fill at the threshold (or at the open if the bar gaps through). Trail/RSI/time fill at the bar close. This is not a tick replay.",
        "Arm B regime_allow for this run is the original #616 body ['RISK_OFF','HARVEST'], not the later RISK_OFF-only bump. The bump does not matter while regime is missing.",
        "Arm B cover (if a gate ever passes) is liq, then max_loss 80 USDT, then k×ATR (1.5 large / 2.0 mid), then time 24h/12h. Viktor daily cap 200 is implemented. No B trade opened on this run.",
        "Fees 7.5bp per side and funding 0.01% per 8h on entry notional are applied to short PnL. CostModel network fee fetch is disabled (fee_source=freeze_7_5bp).",
        "expired_wo_open counts watchlist entries whose 48h TTL elapses at or before the last closed 1h bar without an open. Entries whose TTL is after that bar are censored, not expired.",
        "max_entries=40 is the concurrent watching cap. A slot frees when that entry's TTL elapses. It is not a lifetime cap of 40.",
        "Arm C contributes no short. n=0 and sum_pnl=0 on every row.",
        "Train slices are not used to choose parameters. Freeze numbers are the constants in this script.",
    ]
    b0_opens = sum(1 for o in measured["watch_b0"] if o["status"] == "opened")
    incomplete = bool(meta["wf_incomplete"])
    rec = _recommendation(measured["soft_gates"], incomplete, b0_opens)
    fold_view = []
    for fold in meta["folds"]:
        fold_view.append(
            {
                "id": fold["id"],
                "train_start": iso(fold["train_start"]),
                "train_end": iso(fold["train_end"]),
                "oos_start": iso(fold["oos_start"]),
                "oos_end": iso(fold["oos_end"]),
            }
        )
    payload = {
        "freeze_id": freeze_id,
        "WF_INCOMPLETE": incomplete,
        "recommendation": rec,
        "generated_at": iso(datetime.now(timezone.utc)),
        "data_window": {
            "requested_start": iso(req_start),
            "requested_end": iso(req_end),
            "requested_end_inclusive": iso(req_end - timedelta(days=1)),
            "used_start": iso(meta["window_start"]),
            "used_end": iso(meta["window_end"]),
            "adapted": bool(meta["adapted"]),
            "reason": meta["reason"],
            "canonical_oos_with_events": meta.get("canonical_oos_with_events") or [],
            "n_exits": len(exits),
            "n_allowlisted": n_allow,
            "order_files": [str(p) for p in order_paths],
            "symbols": symbols,
            "symbols_missing_ohlcv": [s for s in symbols if not symbol_bars.get(s)],
            "btc_bars": len(btc),
            "clock_end": measured["clock_end"],
            "folds": fold_view,
        },
        "rows": measured["rows"],
        "per_fold": measured["per_fold"],
        "agg": measured["agg"],
        "soft_gates": measured["soft_gates"],
        "assumptions": assumptions,
        "gate_fail_first_b0": measured["gate_fail_first_b0"],
        "gate_fail_first_b1": measured["gate_fail_first_b1"],
        "skipped_a": measured["skipped_a"],
        "trades": measured["trades"],
        "watch_counts_b0": dict(Counter(o["status"] for o in measured["watch_b0"])),
        "watch_counts_b1": dict(Counter(o["status"] for o in measured["watch_b1"])),
        "repro": (
            "python scripts/wf_watchlist_abc.py "
            f"--start {req_start:%Y-%m-%d} --end {req_end:%Y-%m-%d} "
            f"--freeze {freeze_id} --arms A,B0,B1,C "
            f"--orders {out_dir / 'exits_used.json'} "
            f"--out {out_dir}"
        ),
        "order_inputs": [str(p) for p in order_paths],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    # Slim exit extract so the artefact can be re-read without the MCP dump.
    extract = [
        {
            "id": e.id,
            "symbol": e.symbol,
            "ts": iso(e.ts),
            "exit_source": e.exit_source,
            "signal": e.signal,
            "source": e.source,
            "pnl": e.pnl,
            "usdt": e.usdt,
            "timeframe": e.timeframe,
        }
        for e in exits
    ]
    (out_dir / "exits_used.json").write_text(json.dumps(extract, indent=2), encoding="utf-8")
    (out_dir / "wf_watchlist_abc_20260929.json").write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / "wf_watchlist_abc_20260929.md").write_text(render_markdown(payload), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-09-28")
    parser.add_argument("--freeze", default=FREEZE_ID)
    parser.add_argument("--arms", default="A,B0,B1,C")
    parser.add_argument("--orders", nargs="+", required=True, help="Filled-order JSON files")
    parser.add_argument("--out", default="artifacts/wf_watchlist_abc_20260929")
    parser.add_argument("--no-fetch", action="store_true", help="Do not call Gate (empty bars)")
    args = parser.parse_args(argv)
    if args.freeze != FREEZE_ID:
        print(f"refusing freeze {args.freeze}; this script is {FREEZE_ID}", file=sys.stderr)
        return 2
    wanted = [a.strip() for a in args.arms.split(",") if a.strip()]
    if wanted != ["A", "B0", "B1", "C"]:
        print("this measurement runs arms A,B0,B1,C together", file=sys.stderr)
        return 2
    req_start = parse_day(args.start)
    req_end = parse_day(args.end) + timedelta(days=1)
    payload = measure_files(
        [Path(p) for p in args.orders],
        req_start=req_start,
        req_end=req_end,
        out_dir=Path(args.out),
        freeze_id=args.freeze,
        fetch_bars=not args.no_fetch,
    )
    print(render_table(payload["rows"]))
    print("WF_INCOMPLETE", payload["WF_INCOMPLETE"])
    print(payload["recommendation"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
