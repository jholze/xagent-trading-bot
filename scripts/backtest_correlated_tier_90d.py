#!/usr/bin/env python3
"""Phase-1 portfolio backtest: correlated-tier + stagnant-rotation vs today's bot.

Question
--------
Over the last 90 days, at 1h and separately at 4h, across coins we actually
traded plus the current watchlist, how would the bot have performed WITH
sell_policy.correlated_tier + stagnant_rotation enabled, versus WITHOUT
(today's production flags, both false)?

Phase 1: one two-pass engine, one cost model, one benchmark (BTC buy-and-hold).
Phase 2 (--phase 2): shuffled-timing control, post-hoc regime buckets, a
bounded one-at-a-time parameter sweep, and 3 rolling walk-forward folds.
Phase 3 (--phase 3): opportunity-cost. Log BUY fills the book rejected solely
because slots were full, then score those candidates with a cheap fixed-horizon
OHLCV lookup (24h/72h/7d) — not a second shadow book. Also re-runs the one
Phase-2 sweep point that actually fired stagnant_rotation and asks whether the
freed slot admitted anything better. Two new simulation passes, 1h only.
Does not rewrite the simulation loop; Phase 1 flags in config.json stay as-is.

Method (same rigor bar as scripts/backtest_volume_ignition_60d.py)
------------------------------------------------------------------
1. NO LOOKAHEAD. Signal is computed from data available at bar close t.
   Any resulting buy/sell/rotation fill is applied at the OPEN of bar t+1.
2. NO SELECTION. The assembled universe (traded ∪ watchlist ∪ tier proxies)
   is walked as one shared portfolio. No winner cherry-picking.
3. COSTS. fee_rt (default 0.2% round-trip) + slippage in bps on both sides,
   plus a participation cap: ticket <= participation * quote-volume of the
   signal bar. Same numbers as the volume-ignition backtest for comparability.
4. CAPACITY. Chronological portfolio with max_open_positions (from config)
   and a cash floor (risk.cash_floor_pct of initial capital).
5. REAL DECISION CODE. DecisionEngine.evaluate_with_market,
   apply_rotation_sell_filters / evaluate_stagnant_rotation_close,
   apply_correlated_tier_overlay, GroupDrawdownTracker. Not a reimplementation.
6. CONFIG ISOLATION. Two in-memory deep-copies of config.json. The persisted
   file is never written. Baseline flips the two experiment flags off;
   experiment flips them on and keeps the tuned group/idle/gain values.

No orders are sent. Public OHLCV only.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import json
import math
import random
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DIR = ROOT / "auswertungen" / "cache" / "correlated_tier"
OUT_DIR = ROOT / "auswertungen" / "gis"

HOUR = 3600
FEE_RT_DEFAULT = 0.002
SLIP_BPS_DEFAULT = 25.0
PARTICIPATION_DEFAULT = 0.02
MIN_TICKET_DEFAULT = 50.0

DecisionFn = Callable[[dict[str, Any]], dict[str, Any] | None]


# ---------------------------------------------------------------- config ---

def load_production_config_readonly() -> dict[str, Any]:
    """Read config.json. Never write it back."""
    return json.loads((ROOT / "config.json").read_text())


def _ensure_path(d: dict, *keys: str) -> dict:
    cur = d
    for k in keys:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    return cur


def prepare_shared_replay_config(raw: dict[str, Any]) -> dict[str, Any]:
    """In-memory-only adjustments that apply equally to both passes.

    These are NOT the experiment flags. They exist because the live 15m
    sensor / exit-sensor / DCA-sniper paths have no historical feed in
    this Phase-1 engine. Flipping them here keeps baseline and experiment
    comparable. The persisted file is not touched.
    """
    cfg = copy.deepcopy(raw)
    _ensure_path(cfg, "entry_sensor_15m")["enabled"] = False
    _ensure_path(cfg, "exit_sensor")["enabled"] = False
    _ensure_path(cfg, "dca_sniper")["enabled"] = False
    return cfg


def build_pass_configs(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (baseline, experiment) in-memory configs. Does not touch disk."""
    shared = prepare_shared_replay_config(raw)
    baseline = copy.deepcopy(shared)
    experiment = copy.deepcopy(shared)
    _ensure_path(baseline, "sell_policy", "correlated_tier")["enabled"] = False
    _ensure_path(baseline, "sell_policy", "rotation")["stagnant_rotation_enabled"] = False
    _ensure_path(experiment, "sell_policy", "correlated_tier")["enabled"] = True
    _ensure_path(experiment, "sell_policy", "rotation")["stagnant_rotation_enabled"] = True
    return baseline, experiment


def max_open_from_config(raw: dict[str, Any]) -> int:
    try:
        return int(raw.get("max_open_positions") or 36)
    except (TypeError, ValueError):
        return 36


def cash_floor_from_config(raw: dict[str, Any], start_equity: float) -> float:
    risk = raw.get("risk") or {}
    try:
        pct = float(risk.get("cash_floor_pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    return max(0.0, start_equity * pct / 100.0)


def ticket_from_config(raw: dict[str, Any]) -> float:
    try:
        return float(raw.get("max_usdt_per_trade") or 500)
    except (TypeError, ValueError):
        return 500.0


def start_equity_from_config(raw: dict[str, Any]) -> float:
    try:
        return float(raw.get("initial_capital_usdt") or 10_000)
    except (TypeError, ValueError):
        return 10_000.0


def correlated_tier_symbols(raw: dict[str, Any]) -> list[str]:
    groups = ((raw.get("sell_policy") or {}).get("correlated_tier") or {}).get("groups") or {}
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(groups, dict):
        return out
    for g in groups.values():
        if not isinstance(g, dict):
            continue
        for key in ("proxy_symbols", "member_symbols"):
            vals = g.get(key)
            if vals == "*" or vals == ["*"]:
                continue
            if not isinstance(vals, list):
                continue
            for s in vals:
                sym = str(s or "").strip().upper().replace("-", "/")
                if not sym or sym in seen:
                    continue
                seen.add(sym)
                out.append(sym)
    return out


def us_stock_symbols(raw: dict[str, Any]) -> set[str]:
    groups = ((raw.get("sell_policy") or {}).get("correlated_tier") or {}).get("groups") or {}
    g = groups.get("us_stock") if isinstance(groups, dict) else None
    if not isinstance(g, dict):
        return set()
    out: set[str] = set()
    for key in ("proxy_symbols", "member_symbols"):
        vals = g.get(key)
        if not isinstance(vals, list):
            continue
        for s in vals:
            sym = str(s or "").strip().upper().replace("-", "/")
            if sym:
                out.add(sym)
    return out


def group_for_symbol(symbol: str, config_raw: dict[str, Any]) -> str:
    from strategies.correlated_tier_overlay import resolve_correlated_group

    name = resolve_correlated_group(symbol, config_raw)
    if name:
        return name
    if symbol in us_stock_symbols(config_raw):
        return "us_stock"
    return "crypto_market"


# --------------------------------------------------------------- universe ---

def _norm_symbol(symbol: str | None) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    if "_" in s and "/" not in s:
        a, b = s.rsplit("_", 1)
        s = f"{a}/{b}"
    return s


def _is_filled(order: dict) -> bool:
    st = str(order.get("status") or "").lower()
    if not st:
        return True
    return st in {"filled", "closed", "complete", "completed"}


def symbols_from_ledger(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return set()
    orders = None
    if isinstance(raw, dict):
        blob = raw.get("orders")
        if isinstance(blob, dict):
            orders = blob.get("orders")
        elif isinstance(blob, list):
            orders = blob
        elif isinstance(raw.get("trades"), list):
            orders = raw["trades"]
    if not isinstance(orders, list):
        return set()
    out: set[str] = set()
    for o in orders:
        if not isinstance(o, dict) or not _is_filled(o):
            continue
        sym = _norm_symbol(o.get("symbol"))
        if sym:
            out.add(sym)
    return out


def symbols_from_watchlist_file(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return set()
    coins = raw.get("coins") if isinstance(raw, dict) else raw
    if not isinstance(coins, list):
        return set()
    out: set[str] = set()
    for c in coins:
        if not isinstance(c, dict):
            continue
        if c.get("active") is False:
            continue
        sym = _norm_symbol(c.get("symbol"))
        if sym:
            out.add(sym)
    return out


def assemble_universe(
    raw: dict[str, Any] | None = None,
    extra_ledger_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Union of filled-order symbols, current watchlist, and tier proxies."""
    from core.test_symbols import is_phantom_test_symbol
    from services.universe.split import is_trade_eligible, universe_split_enabled

    raw = raw if raw is not None else load_production_config_readonly()
    sources: dict[str, list[str]] = defaultdict(list)

    ledger_paths = [
        ROOT / "data" / "demo_ledger_backup_railway_live.json",
        ROOT / "data" / "demo_ledger_backup_20260707_restored.json",
        ROOT / "data" / "demo_ledger_pre_cleanup_backup.json",
        ROOT / "auswertungen" / "ledger_backup_railway_test_20260708_235111.json",
        Path("/Users/jholze/Documents/scripts/trading_bot/auswertungen/ledger_backup_railway_test_20260708_235111.json"),
    ]
    if extra_ledger_paths:
        ledger_paths.extend(extra_ledger_paths)

    traded: set[str] = set()
    for p in ledger_paths:
        got = symbols_from_ledger(p)
        for s in got:
            sources[s].append(f"ledger:{p.name}")
        traded |= got

    def _wl(name: str):
        p = ROOT / "data" / name
        return p if p.exists() else ROOT / name

    watch = symbols_from_watchlist_file(_wl("watchlist.json"))
    expand = symbols_from_watchlist_file(_wl("watchlist.dry_run_expansion.json"))
    watchlisted = watch | expand
    for s in watch:
        sources[s].append("watchlist")
    for s in expand:
        sources[s].append("watchlist_expansion")

    split_on = universe_split_enabled(raw)
    trade_eligible: set[str] = set()
    for s in watchlisted:
        if (not split_on) or is_trade_eligible(s, trade_symbols=watchlisted, config=raw):
            trade_eligible.add(s)
            sources[s].append("trade_eligible")

    tier = set(correlated_tier_symbols(raw))
    for s in tier:
        sources[s].append("correlated_tier")

    union = traded | trade_eligible | tier
    dropped_phantom = sorted(s for s in union if is_phantom_test_symbol(s))
    symbols = sorted(s for s in union if not is_phantom_test_symbol(s) and s.endswith("/USDT"))
    return {
        "symbols": symbols,
        "size": len(symbols),
        "traded_historically": sorted(traded & set(symbols)),
        "watchlisted": sorted(watchlisted & set(symbols)),
        "trade_eligible": sorted(trade_eligible & set(symbols)),
        "correlated_tier": sorted(tier & set(symbols)),
        "dropped_phantom": dropped_phantom,
        "sources": {s: sorted(set(sources[s])) for s in symbols},
        "universe_split_enabled": split_on,
    }


# ------------------------------------------------------------------- data ---

def _bar_seconds(timeframe: str) -> int:
    return {
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }.get(timeframe, 3600)


def _to_seconds(ts: int | float) -> int:
    t = int(ts)
    return t // 1000 if t > 10_000_000_000 else t


def qvol(bar: list[float]) -> float:
    """quote-volume ≈ base_vol * typical price. Same as volume-ignition."""
    o, h, l, c, v = bar[1], bar[2], bar[3], bar[4], bar[5]
    return v * ((o + h + l + c) / 4.0)


def _cache_path(sym: str, start: datetime, end: datetime, timeframe: str) -> Path:
    key = f"{sym.replace('/', '_')}_{start:%Y%m%d}_{end:%Y%m%d}_{timeframe}.json.gz"
    return CACHE_DIR / key


def _normalize_bars(raw_bars: list, start: datetime, end: datetime) -> list[list[float]]:
    lo = int(start.timestamp())
    hi = int(end.timestamp())
    seen: dict[int, list[float]] = {}
    for b in raw_bars or []:
        if not b or len(b) < 6:
            continue
        ts = _to_seconds(b[0])
        if lo <= ts <= hi:
            seen[ts] = [ts, float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])]
    return [seen[k] for k in sorted(seen)]


def _spot_gate():
    import ccxt

    return ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def fetch_ohlcv(
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe: str,
) -> list[list[float]]:
    """Disk-cached OHLCV via historical_prices._fetch_ohlcv_range (pages past 1000)."""
    path = _cache_path(symbol, start, end, timeframe)
    if path.exists():
        try:
            with gzip.open(path, "rt") as fh:
                bars = json.load(fh)
            if isinstance(bars, list) and bars:
                return bars
        except Exception:
            pass
    try:
        import historical_prices as hp

        # historical_prices pages past the 1000-bar cap; force spot so we do
        # not inherit a futures/options defaultType from the process.
        orig = hp._gate_exchange
        hp._gate_exchange = _spot_gate
        try:
            raw = hp._fetch_ohlcv_range(symbol, start, end, timeframe=timeframe)
        finally:
            hp._gate_exchange = orig
    except Exception as exc:
        print(f"  ! fetch {symbol} {timeframe}: {exc}")
        raw = []
    bars = _normalize_bars(raw, start, end)
    if not bars:
        # Recently listed pairs: Gate returns [] when `since` is before the
        # listing date. Fall back to the newest 1000 bars and page forward.
        try:
            ex = _spot_gate()
            bar_ms = _bar_seconds(timeframe) * 1000
            chunk = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=1000) or []
            merged = list(chunk)
            if chunk:
                since_ms = int(chunk[-1][0]) + bar_ms
                end_ms = int(end.timestamp() * 1000)
                while since_ms < end_ms and len(chunk) >= 1000:
                    chunk = ex.fetch_ohlcv(
                        symbol, timeframe=timeframe, since=since_ms, limit=1000,
                    ) or []
                    if not chunk:
                        break
                    merged.extend(chunk)
                    last = int(chunk[-1][0])
                    if last <= since_ms:
                        break
                    since_ms = last + bar_ms
            bars = _normalize_bars(merged, start, end)
        except Exception as exc:
            print(f"  ! fetch-fallback {symbol} {timeframe}: {exc}")
            bars = []
    if not bars:
        return []
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(path, "wt") as fh:
            json.dump(bars, fh)
    except Exception:
        pass
    return bars


def load_all(
    symbols: list[str],
    start: datetime,
    end: datetime,
    timeframe: str,
    workers: int,
) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(fetch_ohlcv, s, start, end, timeframe): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                bars = fut.result()
            except Exception:
                bars = []
            done += 1
            if done % 10 == 0 or done == len(symbols):
                print(f"  ... {done}/{len(symbols)}")
            if bars:
                out[sym] = bars
    return out


def min_bars_needed(days: int, timeframe: str, coverage: float = 0.80) -> int:
    per_day = 86400 / _bar_seconds(timeframe)
    return int(days * per_day * coverage)


# ---------------------------------------------------------------- cost ---

def fill_price(side: str, raw_price: float, slip_bps: float) -> float:
    """Apply one-sided slippage. Buy pays up, sell receives down."""
    adj = slip_bps / 10_000.0
    if side == "buy":
        return raw_price * (1.0 + adj)
    return raw_price * (1.0 - adj)


def realizable_notional(
    want: float,
    bar_qvol: float,
    participation: float,
    min_ticket: float,
) -> float:
    """Cap ticket by bar liquidity. 0 if the remainder is dust."""
    if want <= 0:
        return 0.0
    cap = participation * max(0.0, bar_qvol) if participation > 0 else want
    size = min(want, cap) if cap > 0 else 0.0
    if size < min_ticket:
        return 0.0
    return size


def round_trip_net(entry: float, exit_: float, fee_rt: float) -> float:
    """Same formula as volume-ignition: gross − fee_rt."""
    if entry <= 0:
        return 0.0
    return (exit_ / entry - 1.0) - fee_rt


# ------------------------------------------------------------- indicators ---

def add_indicators(bars: list[list[float]]) -> list[dict[str, float]]:
    """RSI/BB/vol/ATR on the full series. Row i uses bars[0..i] only (causal)."""
    if not bars:
        return []
    try:
        import numpy as np
        import talib
    except ImportError:
        return _indicators_fallback(bars)

    close = np.array([b[4] for b in bars], dtype=float)
    high = np.array([b[2] for b in bars], dtype=float)
    low = np.array([b[3] for b in bars], dtype=float)
    vol = np.array([b[5] for b in bars], dtype=float)
    rsi = talib.RSI(close, timeperiod=14)
    upper, mid, lower = talib.BBANDS(close, timeperiod=20)
    atr = talib.ATR(high, low, close, timeperiod=14)
    vol_avg = np.convolve(vol, np.ones(20) / 20.0, mode="full")[: len(vol)]
    rows: list[dict[str, float]] = []
    for i, b in enumerate(bars):
        va = float(vol_avg[i]) if i >= 19 else float("nan")
        recent = float(vol[max(0, i - 3): i + 1].mean())
        rows.append({
            "ts": float(b[0]),
            "open": float(b[1]),
            "high": float(b[2]),
            "low": float(b[3]),
            "close": float(b[4]),
            "volume": float(b[5]),
            "rsi": float(rsi[i]) if rsi[i] == rsi[i] else 50.0,
            "upper": float(upper[i]) if upper[i] == upper[i] else float(b[4]),
            "middle": float(mid[i]) if mid[i] == mid[i] else float(b[4]),
            "lower": float(lower[i]) if lower[i] == lower[i] else float(b[4]),
            "atr_pct": float(atr[i] / b[4] * 100.0) if atr[i] == atr[i] and b[4] else 3.0,
            "vol_avg": va if va == va else float(b[5]),
            "vol_mult": (recent / va) if va == va and va > 0 else 1.0,
            "ready": bool(i >= 20 and rsi[i] == rsi[i]),
        })
    return rows


def _indicators_fallback(bars: list[list[float]]) -> list[dict[str, float]]:
    """Pure-python RSI/BB so fixture tests do not require talib."""
    closes = [float(b[4]) for b in bars]
    rows: list[dict[str, float]] = []
    for i, b in enumerate(bars):
        window = closes[max(0, i - 13): i + 1]
        if len(window) >= 2:
            gains = [max(0.0, window[j] - window[j - 1]) for j in range(1, len(window))]
            losses = [max(0.0, window[j - 1] - window[j]) for j in range(1, len(window))]
            ag = sum(gains) / len(gains)
            al = sum(losses) / len(losses)
            rsi = 100.0 - 100.0 / (1.0 + (ag / al if al > 0 else 99.0))
        else:
            rsi = 50.0
        bb_w = closes[max(0, i - 19): i + 1]
        mid = sum(bb_w) / len(bb_w)
        var = sum((x - mid) ** 2 for x in bb_w) / len(bb_w)
        std = math.sqrt(var)
        vol_w = [float(bars[j][5]) for j in range(max(0, i - 19), i + 1)]
        va = sum(vol_w) / len(vol_w)
        recent = sum(float(bars[j][5]) for j in range(max(0, i - 3), i + 1)) / min(4, i + 1)
        rows.append({
            "ts": float(b[0]),
            "open": float(b[1]),
            "high": float(b[2]),
            "low": float(b[3]),
            "close": float(b[4]),
            "volume": float(b[5]),
            "rsi": rsi,
            "upper": mid + 2 * std,
            "middle": mid,
            "lower": mid - 2 * std,
            "atr_pct": 3.0,
            "vol_avg": va,
            "vol_mult": (recent / va) if va > 0 else 1.0,
            "ready": i >= 20,
        })
    return rows


# ----------------------------------------------------------- selloff book ---

class HistoricalSelloffBook:
    def __init__(
        self,
        data: dict[str, list[list[float]]],
        config_raw: dict[str, Any],
    ) -> None:
        from services.correlated_tier.config import correlated_tier_groups
        from services.correlated_tier.drawdown_tracker import GroupDrawdownTracker

        self.flags: dict[tuple[str, int], bool] = {}
        groups = correlated_tier_groups(config_raw)
        for name, g in (groups or {}).items():
            if not isinstance(g, dict) or g.get("enabled") is False:
                continue
            proxies = [str(s) for s in (g.get("proxy_symbols") or []) if s]
            if not proxies:
                continue
            tracker = GroupDrawdownTracker(
                name,
                proxies,
                drawdown_pct=float(g.get("drawdown_pct") or 5.0),
                window_sec=float(g.get("window_sec") or 600.0),
                min_confirming=int(g.get("min_confirming") or 1),
                sample_keep_sec=max(float(g.get("window_sec") or 600.0) * 4, 3600.0),
            )
            # Align on union of proxy bar timestamps.
            ts_set: set[int] = set()
            by_sym: dict[str, dict[int, list[float]]] = {}
            for sym in proxies:
                bars = data.get(sym) or data.get(_norm_symbol(sym)) or []
                by_sym[sym] = {int(b[0]): b for b in bars}
                ts_set.update(by_sym[sym])
            for ts in sorted(ts_set):
                for sym in proxies:
                    b = by_sym[sym].get(ts)
                    if not b:
                        continue
                    # Four intra-bar ticks so a 10–15 min window can fire.
                    samples = (
                        (ts, float(b[1])),
                        (ts + 200, float(b[2])),
                        (ts + 400, float(b[3])),
                        (ts + 600, float(b[4])),
                    )
                    for tick_ts, px in samples:
                        tracker.on_tick(sym, px, now=float(tick_ts))
                ev = tracker.evaluate(now=float(ts + 600))
                self.flags[(name, ts)] = bool(ev.get("active"))

    def active(self, symbol: str, ts: int, config_raw: dict[str, Any] | None) -> bool:
        from services.correlated_tier.config import correlated_tier_enabled
        from strategies.correlated_tier_overlay import resolve_correlated_group

        if not correlated_tier_enabled(config_raw):
            return False
        group = resolve_correlated_group(symbol, config_raw)
        if not group:
            return False
        return bool(self.flags.get((group, int(ts)), False))


# ---------------------------------------------------------- isolation I/O ---

class _QuietLog:
    def __call__(self, *args: Any, **kwargs: Any) -> None:
        return None


class StubMarketService:
    """No-network MarketService. evaluate_with_market must not hit ccxt."""

    def __init__(self, frames: dict[str, Any] | None = None) -> None:
        self._frames = frames or {}

    def begin_cycle(self) -> None:
        return None

    def prefetch_btc_ohlcv(self, *args: Any, **kwargs: Any) -> None:
        return None

    def fetch_15m_sensor_metrics(self, *args: Any, **kwargs: Any) -> None:
        return None

    def fetch_exit_metrics_15m(self, *args: Any, **kwargs: Any) -> None:
        return None

    def fetch_exit_metrics_1h(self, *args: Any, **kwargs: Any) -> None:
        return None

    def btc_relative_return_delta(self, *args: Any, **kwargs: Any) -> None:
        return None

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300):
        import pandas as pd

        frame = self._frames.get(symbol)
        if frame is None:
            return pd.DataFrame()
        return frame.tail(int(limit))

    def fetch_funding_rate(self, *args: Any, **kwargs: Any) -> None:
        return None

    def btc_underperformance_ratio(self, *args: Any, **kwargs: Any) -> None:
        return None


@contextmanager
def isolated_runtime(
    config_raw: dict[str, Any],
    *,
    selloff_book: HistoricalSelloffBook | None = None,
) -> Iterator[dict[str, Any]]:
    """Pin in-memory config + isolated position book. Never flush to disk."""
    import data_manager
    import logger as logger_mod
    import strategies.positions as posmod
    import strategies.sell_rotation_policy as srp
    from services.correlated_tier import api as ct_api
    from strategies.correlated_tier_overlay import apply_correlated_tier_overlay
    from strategies.sell_rotation_policy import (
        apply_rotation_sell_filters,
        evaluate_stagnant_rotation_close,
    )

    state: dict[str, Any] = {
        "config_raw": config_raw,
        "clock": datetime.now(timezone.utc).replace(tzinfo=None),
        "bar_ts": 0,
        "selloff_book": selloff_book,
        "overlay_enabled_seen": [],
        "rotation_stagnant_seen": [],
        "apply_correlated_tier_overlay": apply_correlated_tier_overlay,
        "apply_rotation_sell_filters": apply_rotation_sell_filters,
        "evaluate_stagnant_rotation_close": evaluate_stagnant_rotation_close,
    }

    prev_cache = data_manager._config_cache
    data_manager._config_cache = config_raw

    saved_positions = {k: copy.deepcopy(v) for k, v in posmod.positions.items()}
    saved_open = posmod._open_positions_count
    saved_counts = dict(posmod._open_counts)
    posmod.positions.clear()
    posmod._open_positions_count = 0
    posmod._open_counts[posmod._active_key] = 0

    real_flush = posmod.flush_positions
    posmod.flush_positions = lambda *a, **k: None  # noqa: ARG005

    real_log = logger_mod.log
    logger_mod.log = _QuietLog()

    real_hours = srp._hours_since

    def _hours_since_clock(iso_ts, now=None):
        return real_hours(iso_ts, now or state["clock"])

    srp._hours_since = _hours_since_clock

    real_selloff = ct_api.correlated_tier_selloff_active

    def _selloff(symbol, config_raw=None):
        cfg = config_raw if config_raw is not None else state["config_raw"]
        book = state["selloff_book"]
        if book is None:
            return False
        return book.active(symbol, int(state["bar_ts"]), cfg)

    ct_api.correlated_tier_selloff_active = _selloff

    real_overlay = apply_correlated_tier_overlay

    def _overlay(params, symbol, cfg):
        enabled = bool(((cfg or {}).get("sell_policy") or {}).get("correlated_tier") or {}).get("enabled")
        state["overlay_enabled_seen"].append(bool(enabled))
        return real_overlay(params, symbol, cfg)

    # Leave the production name intact; callers use state["apply_..."] which is
    # the real function. Isolation tests spy via wrap_overlay().

    try:
        from strategies import watch_15m_state

        real_watch_save = watch_15m_state._save
        watch_15m_state._save = lambda *a, **k: None  # noqa: ARG005
    except Exception:
        real_watch_save = None
        watch_15m_state = None

    try:
        yield state
    finally:
        data_manager._config_cache = prev_cache
        posmod.positions.clear()
        posmod.positions.update(saved_positions)
        posmod._open_positions_count = saved_open
        posmod._open_counts.clear()
        posmod._open_counts.update(saved_counts)
        posmod.flush_positions = real_flush
        logger_mod.log = real_log
        srp._hours_since = real_hours
        ct_api.correlated_tier_selloff_active = real_selloff
        if watch_15m_state is not None and real_watch_save is not None:
            watch_15m_state._save = real_watch_save


def wrap_overlay_spy(state: dict[str, Any]) -> None:
    """Record enabled-flag of every overlay call (used by isolation test)."""
    real = state["apply_correlated_tier_overlay"]

    def spy(params, symbol, cfg):
        root = ((cfg or {}).get("sell_policy") or {}).get("correlated_tier") or {}
        state["overlay_enabled_seen"].append(bool(root.get("enabled")))
        rot = ((cfg or {}).get("sell_policy") or {}).get("rotation") or {}
        state["rotation_stagnant_seen"].append(bool(rot.get("stagnant_rotation_enabled")))
        return real(params, symbol, cfg)

    state["apply_correlated_tier_overlay"] = spy


# ---------------------------------------------------------- decision path ---

def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None).isoformat()


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None)


def _stamp_sim_time(pos: dict, ts: int, fields: tuple[str, ...]) -> None:
    stamp = _iso(ts)
    for f in fields:
        if pos.get(f):
            pos[f] = stamp


@dataclass
class PendingAction:
    symbol: str
    action: str
    signal_ts: int
    signal_idx: int
    signal_close: float
    bar_qvol: float
    sources: list[str] = field(default_factory=list)
    sell_source: str = ""
    rationale: str = ""


def _new_engine(config_raw: dict[str, Any], frames: dict[str, Any] | None = None):
    from core.config import BotConfig
    from strategies.decision_engine import DecisionEngine

    engine = DecisionEngine(market_service=StubMarketService(frames))
    engine.config = BotConfig(raw=config_raw)
    return engine


def decide_bar(
    *,
    symbol: str,
    timeframe: str,
    row: dict[str, float],
    config_raw: dict[str, Any],
    state: dict[str, Any],
    engine,
    open_count: int,
    decision_fn: DecisionFn | None,
    forced_entries: dict[str, set[int]] | None = None,
) -> PendingAction | None:
    """Signal at this bar's close. Caller applies the action at the next open."""
    from core.actions import is_buy, is_sell, normalize
    from strategies.positions import get_position, is_open_position

    ts = int(row["ts"])
    state["bar_ts"] = ts
    state["clock"] = _dt(ts)
    pos = get_position(symbol, timeframe)
    has_pos = is_open_position(pos)

    if decision_fn is not None:
        # Always touch the real overlay with this pass's config so isolation
        # tests can spy the enabled flag without constructing DecisionEngine.
        try:
            state["apply_correlated_tier_overlay"]({}, symbol, config_raw)
        except Exception:
            pass
        payload = {
            "symbol": symbol,
            "timeframe": timeframe,
            "row": row,
            "config_raw": config_raw,
            "has_position": has_pos,
            "position": pos,
            "strategy_params": {},
            "open_count": open_count,
        }
        got = decision_fn(payload)
        if not got:
            return None
        action = str(got.get("action") or "HOLD")
        if action in ("", "HOLD", "IGNORE"):
            return None
        return PendingAction(
            symbol=symbol,
            action=action,
            signal_ts=ts,
            signal_idx=int(got.get("signal_idx") or 0),
            signal_close=float(row["close"]),
            bar_qvol=qvol([row["ts"], row["open"], row["high"], row["low"], row["close"], row["volume"]]),
            sources=list(got.get("sources") or ["decision_fn"]),
            sell_source=str(got.get("sell_source") or ""),
            rationale=str(got.get("rationale") or ""),
        )

    if forced_entries is not None and not has_pos:
        allowed = forced_entries.get(symbol) or set()
        if ts not in allowed:
            return None
        return PendingAction(
            symbol=symbol,
            action="BUY",
            signal_ts=ts,
            signal_idx=0,
            signal_close=float(row["close"]),
            bar_qvol=qvol([row["ts"], row["open"], row["high"], row["low"], row["close"], row["volume"]]),
            sources=["shuffled_entry"],
            sell_source="",
            rationale="forced shuffled entry",
        )

    from core.models import MarketContext
    from strategies.registry import resolve_strategy_params

    params = resolve_strategy_params(
        {"symbol": symbol, "timeframe": timeframe},
        has_position=has_pos,
        atr_pct=float(row.get("atr_pct") or 3.0),
        frozen_tier=pos.get("strategy_tier"),
    )
    params = state["apply_correlated_tier_overlay"](params or {}, symbol, config_raw)

    market = MarketContext(
        symbol=symbol,
        timeframe=timeframe,
        current_price=float(row["close"]),
        rsi=float(row["rsi"]),
        lower_bb=float(row["lower"]),
        middle_bb=float(row["middle"]),
        upper_bb=float(row["upper"]),
        atr_pct=float(row.get("atr_pct") or 3.0),
        vol_multiplier=float(row.get("vol_mult") or 1.0),
        has_position=has_pos,
        average_entry=float(pos.get("average_entry") or 0.0),
        open_positions=open_count,
        strategy_params=params,
    )
    analysis = None
    try:
        analysis = engine.evaluate_with_market(
            {"symbol": symbol, "timeframe": timeframe, "strategy_params": params},
            market,
        )
    except Exception:
        analysis = None

    action = normalize(getattr(analysis, "normalized_action", None) or getattr(analysis, "action", "HOLD") or "HOLD")
    sources = list(getattr(analysis, "sources", None) or [])
    sell_source = str(getattr(analysis, "sell_source", "") or "")
    rationale = str(getattr(analysis, "rationale", "") or "")

    # Re-run rotation / stagnant with the simulated clock. DecisionEngine's
    # internal call uses wall-clock now unless we patched _hours_since (we did),
    # but we still invoke the real functions here so the experiment path is
    # guaranteed to see our in-memory config_raw.
    if has_pos:
        from strategies.positions import count_open_full_slots

        candidates: list[tuple] = []
        if is_sell(action):
            candidates.append((action, 5, sell_source or "decision_engine"))
        try:
            open_full = count_open_full_slots(config_raw)
        except Exception:
            open_full = open_count
        try:
            eff_cap = int(config_raw.get("max_open_positions") or 0)
        except (TypeError, ValueError):
            eff_cap = 0
        try:
            filtered, audit = state["apply_rotation_sell_filters"](
                candidates,
                market,
                pos,
                params,
                config_raw,
                sell_sources=sources,
                open_full_slots=open_full,
                eff_cap=eff_cap,
                now=state["clock"],
            )
        except Exception:
            filtered, audit = candidates, None
        extra = None
        try:
            from strategies.sell_rotation_policy import rotation_config

            extra = state["evaluate_stagnant_rotation_close"](
                market,
                pos,
                rotation_config(config_raw, params),
                open_full_slots=open_full,
                eff_cap=eff_cap,
                now=state["clock"],
            )
        except Exception:
            extra = None
        if extra:
            filtered = list(filtered) + [(extra.action, extra.priority, extra.source)]
            sources.append(extra.source)
            sell_source = extra.source
            rationale = extra.rationale
        if filtered:
            best = max(filtered, key=lambda c: c[1])
            action = best[0]
            sell_source = best[2] or sell_source
        elif audit is not None and not is_buy(action):
            # rotation may have dropped a sell
            if not extra:
                action = "HOLD"

    if action in ("", "HOLD", "IGNORE"):
        return None
    if not (is_buy(action) or is_sell(action)):
        return None
    if forced_entries is not None and is_buy(action):
        return None
    return PendingAction(
        symbol=symbol,
        action=action,
        signal_ts=ts,
        signal_idx=0,
        signal_close=float(row["close"]),
        bar_qvol=qvol([row["ts"], row["open"], row["high"], row["low"], row["close"], row["volume"]]),
        sources=sources,
        sell_source=sell_source,
        rationale=rationale,
    )


# ------------------------------------------- Phase 3 opportunity-cost ---

# Live RiskDecision.code when risk_manager.py denies a new BUY because
# open_slots >= cap.max_open_eff (see risk/position_capacity.py).
CAPACITY_REJECT_CODE = "max_open_positions"

FORWARD_HORIZONS_SEC: dict[str, int] = {
    "24h": 24 * HOUR,
    "72h": 72 * HOUR,
    "7d": 7 * 86400,
}

# "shortly after" a stagnant_rotation fill: same bar plus a few 1h bars.
REDEPLOY_WINDOW_SEC = 4 * HOUR

# Exact Phase-2 sweep point that produced the one observed fire.
PHASE3_TIGHT_OVERRIDES: dict[str, Any] = {
    "max_open_positions": 18,
    "stagnant_slack_slots": 8,
    "stagnant_gain_pct": 6.0,
    "stagnant_idle_hours": 12.0,
}


def is_capacity_rejection(
    *,
    action: str,
    has_position: bool,
    is_dca: bool,
    open_slots: int,
    max_open_eff: int,
) -> bool:
    """True iff this BUY would be denied solely by the slot ceiling.

    Mirrors risk/risk_manager.py: when `not has_position` and
    `open_slots >= cap.max_open_eff` → RiskDecision(code="max_open_positions").
    Sells, DCA, and already-open symbols are never gated by this check.
    Other reject reasons (cash floor, illiquidity, cooldown, universe cap)
    are a different code and must not be counted here.
    """
    act = str(action or "").upper()
    if not act.startswith("BUY"):
        return False
    if has_position or is_dca:
        return False
    try:
        cap = int(max_open_eff)
        open_n = int(open_slots)
    except (TypeError, ValueError):
        return False
    if cap <= 0:
        return False
    return open_n >= cap


def close_at_or_after(bars: list[list[float]], ts: int) -> tuple[int, float] | None:
    """First cached bar with timestamp >= ts → (bar_ts, close). None if tape ends."""
    tsi = int(ts)
    for b in bars or []:
        if not b:
            continue
        bt = int(b[0])
        if bt >= tsi:
            return bt, float(b[4])
    return None


def fixed_horizon_return(
    bars: list[list[float]],
    entry_ts: int,
    entry_price: float,
    horizon_sec: int,
) -> dict[str, Any] | None:
    """Simple hold: buy at entry_price, mark at the first close >= entry_ts+horizon.

    Not a shadow position. No stop / trail / rotation / fees. Missing tape → None
    (do not silently mark out at the last available close).
    """
    try:
        px = float(entry_price)
        horizon = int(horizon_sec)
    except (TypeError, ValueError):
        return None
    if px <= 0 or horizon <= 0 or not bars:
        return None
    hit = close_at_or_after(bars, int(entry_ts) + horizon)
    if hit is None:
        return None
    exit_ts, exit_px = hit
    if exit_px <= 0:
        return None
    return {
        "horizon_sec": horizon,
        "exit_ts": int(exit_ts),
        "exit_price": float(exit_px),
        "ret": float(exit_px) / px - 1.0,
    }


def summarize_horizon_returns(rets: list[float]) -> dict[str, Any]:
    """mean / median / percent-positive. Empty input → n=0 and None stats."""
    if not rets:
        return {"n": 0, "mean": None, "median": None, "pct_positive": None}
    vals = [float(r) for r in rets]
    pos = sum(1 for r in vals if r > 0)
    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "median": float(median(vals)),
        "pct_positive": pos / len(vals),
    }


def attach_forward_returns(
    events: list[dict[str, Any]],
    data: dict[str, list[list[float]]],
    *,
    ts_key: str,
    price_key: str,
    dest_key: str = "forward",
) -> list[dict[str, Any]]:
    """Stamp each event with 24h/72h/7d close-to-entry returns from cached OHLCV."""
    out: list[dict[str, Any]] = []
    for ev in events or []:
        row = dict(ev)
        bars = data.get(str(ev.get("symbol") or "")) or []
        try:
            ts = int(ev.get(ts_key) or 0)
            px = float(ev.get(price_key) or 0)
        except (TypeError, ValueError):
            ts, px = 0, 0.0
        fwd: dict[str, Any] = {}
        for name, sec in FORWARD_HORIZONS_SEC.items():
            got = fixed_horizon_return(bars, ts, px, sec)
            fwd[name] = None if got is None else {
                "ret": got["ret"],
                "exit_ts": got["exit_ts"],
                "exit_price": got["exit_price"],
            }
        row[dest_key] = fwd
        out.append(row)
    return out


def compare_reject_vs_taken(
    rejections: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    data: dict[str, list[list[float]]],
) -> dict[str, Any]:
    """Same fixed-horizon lookup on rejected candidates vs filled baseline BUYs."""
    rej = attach_forward_returns(
        rejections, data, ts_key="fill_ts", price_key="would_be_entry_price",
    )
    buys = [t for t in (trades or []) if t.get("type") == "BUY"]
    taken = attach_forward_returns(buys, data, ts_key="fill_ts", price_key="fill_price")
    horizons: dict[str, Any] = {}
    for name in FORWARD_HORIZONS_SEC:
        rj = [
            float(r["forward"][name]["ret"])
            for r in rej
            if isinstance((r.get("forward") or {}).get(name), dict)
        ]
        tk = [
            float(r["forward"][name]["ret"])
            for r in taken
            if isinstance((r.get("forward") or {}).get(name), dict)
        ]
        sj = summarize_horizon_returns(rj)
        st = summarize_horizon_returns(tk)
        mean_delta = None
        if sj["mean"] is not None and st["mean"] is not None:
            mean_delta = float(sj["mean"]) - float(st["mean"])
        horizons[name] = {
            "rejected": sj,
            "taken": st,
            "mean_delta_rejected_minus_taken": mean_delta,
        }
    return {
        "n_rejected": len(rej),
        "n_taken": len(taken),
        "horizons": horizons,
        "rejected": rej,
    }


def _first_sell_after(
    trades: list[dict[str, Any]],
    symbol: str,
    after_ts: int,
) -> dict[str, Any] | None:
    hits = [
        t for t in (trades or [])
        if t.get("type") == "SELL"
        and t.get("symbol") == symbol
        and int(t.get("fill_ts") or 0) >= int(after_ts)
    ]
    hits.sort(key=lambda t: int(t.get("fill_ts") or 0))
    if not hits:
        return None
    t = hits[0]
    return {
        "symbol": t.get("symbol"),
        "fill_ts": t.get("fill_ts"),
        "fill_dt": t.get("fill_dt"),
        "fill_price": t.get("fill_price"),
        "net_pct": t.get("net_pct"),
        "pnl": t.get("pnl"),
        "exit": t.get("exit"),
        "group": t.get("group"),
    }


def match_rotation_redeploys(
    trades: list[dict[str, Any]],
    capacity_rejections: list[dict[str, Any]],
    *,
    window_sec: int = REDEPLOY_WINDOW_SEC,
) -> list[dict[str, Any]]:
    """For each stagnant_rotation sell, did a waiting candidate get the freed slot?

    The sim consumes a pending BUY at fill time — a skipped BUY is not queued.
    'Waiting' therefore means: a capacity-reject at/near the fire whose symbol
    then prints a BUY fill inside (fire_ts, fire_ts+window], or a same-bar BUY
    of that symbol. No such BUY → admitted is None (the fire did not redeploy).
    """
    fires = [
        t for t in (trades or [])
        if t.get("type") == "SELL" and t.get("exit") == "stagnant_rotation"
    ]
    buys = [t for t in (trades or []) if t.get("type") == "BUY"]
    out: list[dict[str, Any]] = []
    win = int(window_sec)
    for fire in fires:
        fire_ts = int(fire.get("fill_ts") or 0)
        fire_sym = fire.get("symbol")
        nearby = [
            r for r in (capacity_rejections or [])
            if abs(int(r.get("fill_ts") or 0) - fire_ts) <= win
            and r.get("symbol") != fire_sym
        ]
        later = [
            b for b in buys
            if fire_ts <= int(b.get("fill_ts") or 0) <= fire_ts + win
            and b.get("symbol") != fire_sym
        ]
        later.sort(key=lambda b: int(b.get("fill_ts") or 0))
        reject_syms = {r.get("symbol") for r in nearby}
        admitted = next((b for b in later if b.get("symbol") in reject_syms), None)
        if admitted is None and later:
            admitted = later[0]
        waiting = None
        if admitted:
            waiting = next(
                (r for r in nearby if r.get("symbol") == admitted.get("symbol")),
                None,
            )
        realized = None
        if admitted:
            realized = _first_sell_after(
                trades, str(admitted.get("symbol") or ""), int(admitted.get("fill_ts") or 0),
            )
        out.append({
            "rotated_symbol": fire_sym,
            "fire_ts": fire_ts,
            "fire_dt": fire.get("fill_dt") or fire.get("dt"),
            "fire_fill_price": fire.get("fill_price"),
            "fire_net_pct": fire.get("net_pct"),
            "fire_pnl": fire.get("pnl"),
            "fire_group": fire.get("group"),
            "admitted": admitted,
            "had_waiting_reject": bool(
                admitted is not None and waiting is not None
                and waiting.get("symbol") == admitted.get("symbol")
            ),
            "waiting_reject": waiting,
            "admitted_realized": realized,
            "nearby_reject_n": len(nearby),
            "window_buy_n": len(later),
        })
    return out


# ---------------------------------------------------------- portfolio sim ---

@dataclass
class SimKnobs:
    fee_rt: float = FEE_RT_DEFAULT
    slip_bps: float = SLIP_BPS_DEFAULT
    ticket: float = 500.0
    max_open: int = 36
    participation: float = PARTICIPATION_DEFAULT
    min_ticket: float = MIN_TICKET_DEFAULT
    start_equity: float = 10_000.0
    cash_floor: float = 0.0
    timeframe: str = "1h"


def _amount(pos: dict) -> float:
    try:
        return float(pos.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def simulate_portfolio(
    data: dict[str, list[list[float]]],
    config_raw: dict[str, Any],
    knobs: SimKnobs,
    *,
    decision_fn: DecisionFn | None = None,
    warmup_ts: float = 0.0,
    spy_overlay: bool = False,
    peak_stamp_mode: str = "every_bar",
    forced_entries: dict[str, set[int]] | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Walk the whole universe forward as one shared portfolio.

    Causality: decide on bar-close t, fill at open of the next bar for that
    symbol. Pending actions that have no next bar are marked-out at the last
    close (end-of-window) and counted separately.
    """
    from core.actions import is_buy, is_sell
    from strategies.positions import (
        count_open_positions,
        get_position,
        is_open_position,
        sell_fraction_for_signal,
        update_market_snapshot,
        update_position,
    )

    frames: dict[str, list[dict[str, float]]] = {
        s: add_indicators(bars) for s, bars in data.items()
    }
    index: dict[str, dict[int, int]] = {}
    all_ts: set[int] = set()
    for s, rows in frames.items():
        index[s] = {}
        for i, r in enumerate(rows):
            ts = int(r["ts"])
            index[s][ts] = i
            all_ts.add(ts)
    timeline = sorted(all_ts)

    book = HistoricalSelloffBook(data, config_raw) if decision_fn is None else None
    cash = float(knobs.start_equity)
    last_px: dict[str, float] = {}
    pending: dict[str, PendingAction] = {}
    trades: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    curve: list[tuple[int, float]] = []
    skipped_slots = skipped_cash = skipped_illiquid = 0
    skipped_no_next = 0
    selloff_bars = 0
    peak_open = 0
    capacity_rejections: list[dict[str, Any]] = []

    def mark_equity(ts: int) -> float:
        eq = cash
        from strategies.positions import list_active_positions

        try:
            lots = list_active_positions()
        except Exception:
            lots = []
        for lot in lots or []:
            sym = lot.get("symbol") if isinstance(lot, dict) else None
            if not sym:
                continue
            px = last_px.get(sym) or float(lot.get("average_entry") or 0)
            eq += _amount(lot) * px
        curve.append((ts, eq))
        return eq

    with isolated_runtime(config_raw, selloff_book=book) as state:
        if spy_overlay:
            wrap_overlay_spy(state)
        engine = None if decision_fn is not None else _new_engine(config_raw)

        n_ts = len(timeline)
        for n_done, ts in enumerate(timeline, 1):
            if verbose and (n_done == 1 or n_done == n_ts or n_done % 250 == 0):
                print(f"       ... bar {n_done}/{n_ts}")
            # 1) apply fills decided on the previous bar, at THIS bar's OPEN
            due = [p for p in list(pending.values()) if p.symbol in index and ts in index[p.symbol]]
            for act in due:
                pending.pop(act.symbol, None)
                rows = frames[act.symbol]
                i = index[act.symbol][ts]
                raw_open = float(rows[i]["open"])
                side = "buy" if is_buy(act.action) else "sell"
                px = fill_price(side, raw_open, knobs.slip_bps)
                pos = get_position(act.symbol, knobs.timeframe)
                open_n = count_open_positions()

                if is_buy(act.action):
                    is_dca = act.action == "BUY_DCA" or (is_open_position(pos) and "DCA" in act.action)
                    if (not is_dca) and open_n >= knobs.max_open:
                        skipped_slots += 1
                        # Existing skip stays as-is (Phase 1/2 invariant).
                        # Log only the live risk_manager condition: a *new*
                        # BUY denied solely by the slot ceiling, not cash /
                        # illiquidity / cooldown / an add-on to an open lot.
                        if is_capacity_rejection(
                            action=act.action,
                            has_position=bool(is_open_position(pos)),
                            is_dca=bool(is_dca),
                            open_slots=int(open_n),
                            max_open_eff=int(knobs.max_open),
                        ):
                            capacity_rejections.append({
                                "symbol": act.symbol,
                                "action": act.action,
                                "signal_ts": act.signal_ts,
                                "fill_ts": ts,
                                "signal_close": act.signal_close,
                                "raw_open": raw_open,
                                "would_be_entry_price": px,
                                "open_slots": int(open_n),
                                "max_open_eff": int(knobs.max_open),
                                "code": CAPACITY_REJECT_CODE,
                                "group": group_for_symbol(act.symbol, config_raw),
                                "dt": _iso(act.signal_ts),
                                "fill_dt": _iso(ts),
                            })
                        continue
                    spendable = cash - knobs.cash_floor
                    want = min(knobs.ticket, max(0.0, spendable))
                    size = realizable_notional(want, act.bar_qvol, knobs.participation, knobs.min_ticket)
                    if size <= 0:
                        if want < knobs.min_ticket or spendable < knobs.min_ticket:
                            skipped_cash += 1
                        else:
                            skipped_illiquid += 1
                        continue
                    if cash < size:
                        skipped_cash += 1
                        continue
                    amount = size / px if px > 0 else 0.0
                    if amount <= 0:
                        continue
                    update_position(act.symbol, knobs.timeframe, "BUY_DCA" if is_dca else "BUY", px, amount)
                    _stamp_sim_time(
                        get_position(act.symbol, knobs.timeframe),
                        ts,
                        ("last_trade_at", "first_buy_at", "entry_at", "peak_at", "last_dca_at"),
                    )
                    cash -= size
                    last_px[act.symbol] = px
                    trades.append({
                        "type": "BUY",
                        "symbol": act.symbol,
                        "action": act.action,
                        "dt": _iso(act.signal_ts),
                        "fill_dt": _iso(ts),
                        "signal_ts": act.signal_ts,
                        "fill_ts": ts,
                        "signal_close": act.signal_close,
                        "fill_price": px,
                        "raw_open": raw_open,
                        "usdt": round(size, 4),
                        "amount": amount,
                        "sources": act.sources,
                        "group": group_for_symbol(act.symbol, config_raw),
                    })
                elif is_sell(act.action) and is_open_position(pos):
                    entry = float(pos.get("average_entry") or 0)
                    frac = sell_fraction_for_signal(act.action)
                    if frac <= 0:
                        frac = 1.0
                    held_amt = _amount(pos)
                    want_amt = held_amt * frac
                    want_usdt = want_amt * px
                    size = realizable_notional(
                        want_usdt,
                        act.bar_qvol,
                        knobs.participation,
                        0.0,
                    )
                    if size <= 0:
                        # still allow a full close of a tiny tail
                        size = want_usdt
                    sell_amt = min(held_amt, size / px if px > 0 else 0.0)
                    if sell_amt <= 0:
                        continue
                    received = sell_amt * px
                    # one-way fee is half the round-trip; ignition subtracts fee_rt
                    # on the completed trade. We book half here and half conceptually
                    # on the matching buy via the net formula at close-out.
                    fee = received * (knobs.fee_rt / 2.0)
                    received_net = received - fee
                    pnl = (px - entry) * sell_amt - (entry * sell_amt * (knobs.fee_rt / 2.0)) - fee
                    update_position(act.symbol, knobs.timeframe, act.action, px, sell_amt)
                    _stamp_sim_time(
                        get_position(act.symbol, knobs.timeframe),
                        ts,
                        ("last_trade_at",),
                    )
                    cash += received_net
                    last_px[act.symbol] = px
                    trades.append({
                        "type": "SELL",
                        "symbol": act.symbol,
                        "action": act.action,
                        "dt": _iso(act.signal_ts),
                        "fill_dt": _iso(ts),
                        "signal_ts": act.signal_ts,
                        "fill_ts": ts,
                        "signal_close": act.signal_close,
                        "fill_price": px,
                        "raw_open": raw_open,
                        "usdt_received": round(received_net, 4),
                        "amount": sell_amt,
                        "pnl": round(pnl, 4),
                        "net_pct": round(100.0 * ((px / entry - 1.0) - knobs.fee_rt), 4) if entry > 0 else 0.0,
                        "sources": act.sources,
                        "exit": act.sell_source or "decision_engine",
                        "group": group_for_symbol(act.symbol, config_raw),
                    })

            # 2) mark, bump peaks, decide at this close
            for sym, rows in frames.items():
                if ts not in index[sym]:
                    continue
                i = index[sym][ts]
                row = rows[i]
                last_px[sym] = float(row["close"])
                pos = get_position(sym, knobs.timeframe)
                if is_open_position(pos):
                    try:
                        changed = update_market_snapshot(
                            sym,
                            knobs.timeframe,
                            float(row["close"]),
                            atr_pct=float(row.get("atr_pct") or 0),
                            peak_hint=float(row["high"]),
                        )
                        # Phase 1 stamped peak_at on every bar, which zeroed
                        # stagnant idle. Phase 2 can stamp only on a genuine
                        # new high so idle can accumulate (production semantics).
                        if peak_stamp_mode == "every_bar" or changed:
                            _stamp_sim_time(get_position(sym, knobs.timeframe), ts, ("peak_at",))
                    except Exception:
                        pass

            if book is not None:
                # count how many groups are in selloff at this ts
                for key, flag in book.flags.items():
                    if key[1] == ts and flag:
                        selloff_bars += 1
                        break

            if ts < warmup_ts:
                mark_equity(ts)
                continue

            open_n = count_open_positions()
            peak_open = max(peak_open, open_n)
            for sym, rows in frames.items():
                if ts not in index[sym]:
                    continue
                if sym in pending:
                    continue
                row = rows[index[sym][ts]]
                if not row.get("ready"):
                    continue
                i = index[sym][ts]
                act = decide_bar(
                    symbol=sym,
                    timeframe=knobs.timeframe,
                    row=row,
                    config_raw=config_raw,
                    state=state,
                    engine=engine,
                    open_count=open_n,
                    decision_fn=decision_fn,
                    forced_entries=forced_entries,
                )
                if act is None:
                    continue
                act.signal_idx = i
                has_next = i + 1 < len(rows)
                next_open = float(rows[i + 1]["open"]) if has_next else None
                signals.append({
                    "symbol": sym,
                    "action": act.action,
                    "ts": act.signal_ts,
                    "idx": i,
                    "entry_idx": i + 1 if has_next else None,
                    "entry_price": next_open,
                    "signal_close": act.signal_close,
                    "dt": _iso(act.signal_ts),
                })
                if has_next:
                    pending[sym] = act
                else:
                    skipped_no_next += 1

            mark_equity(ts)

        # leftover pending with no next bar: count, do not fill at this close
        skipped_no_next += len(pending)
        pending.clear()

        # mark out remaining open lots at last close (end-of-window)
        from strategies.positions import list_active_positions

        try:
            leftover = list(list_active_positions() or [])
        except Exception:
            leftover = []
        for lot in leftover:
            if not isinstance(lot, dict):
                continue
            sym = lot.get("symbol")
            if not sym:
                continue
            rows = frames.get(sym) or []
            if not rows:
                continue
            last = rows[-1]
            px = fill_price("sell", float(last["close"]), knobs.slip_bps)
            entry = float(lot.get("average_entry") or 0)
            amt = _amount(lot)
            if amt <= 0:
                continue
            received = amt * px
            fee = received * (knobs.fee_rt / 2.0)
            received_net = received - fee
            pnl = (px - entry) * amt - (entry * amt * (knobs.fee_rt / 2.0)) - fee
            update_position(sym, knobs.timeframe, "SELL_FULL", px, amt)
            cash += received_net
            trades.append({
                "type": "SELL",
                "symbol": sym,
                "action": "SELL_FULL",
                "dt": _iso(int(last["ts"])),
                "fill_dt": _iso(int(last["ts"])),
                "signal_ts": int(last["ts"]),
                "fill_ts": int(last["ts"]),
                "signal_close": float(last["close"]),
                "fill_price": px,
                "raw_open": float(last["close"]),
                "usdt_received": round(received_net, 4),
                "amount": amt,
                "pnl": round(pnl, 4),
                "net_pct": round(100.0 * ((px / entry - 1.0) - knobs.fee_rt), 4) if entry > 0 else 0.0,
                "sources": ["end_of_window"],
                "exit": "end_of_window",
                "group": group_for_symbol(sym, config_raw),
            })
        if leftover:
            last_ts = timeline[-1] if timeline else 0
            mark_equity(last_ts)

        overlay_seen = list(state["overlay_enabled_seen"])
        stagnant_seen = list(state["rotation_stagnant_seen"])

    out = summarize_run(
        trades,
        curve,
        knobs,
        peak_open=peak_open,
        skipped_slots=skipped_slots,
        skipped_cash=skipped_cash,
        skipped_illiquid=skipped_illiquid,
        skipped_no_next=skipped_no_next,
        selloff_bars=selloff_bars,
        overlay_enabled_seen=overlay_seen,
        rotation_stagnant_seen=stagnant_seen,
        ending_cash=cash,
    )
    out["signals"] = signals
    out["capacity_rejections"] = attach_forward_returns(
        capacity_rejections, data, ts_key="fill_ts", price_key="would_be_entry_price",
    )
    return out


def sharpe_like(trades: list[dict[str, Any]]) -> float:
    """Same shape as hermes.metrics.sharpe_from_trades (mean/std * sqrt(n))."""
    sells = [t for t in trades if t.get("type") == "SELL"]
    rets: list[float] = []
    for t in sells:
        if t.get("net_pct") is not None:
            rets.append(float(t["net_pct"]) / 100.0)
        else:
            usdt = float(t.get("usdt_received") or 0)
            if usdt > 0:
                rets.append(float(t.get("pnl") or 0) / usdt)
    if len(rets) > 1:
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        return round((mean_r / std) * math.sqrt(len(rets)), 2) if std > 0 else 0.0
    if rets:
        return 1.0 if rets[0] > 0 else -1.0
    return 0.0


def _group_breakdown(trades: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t.get("type") == "SELL":
            by[str(t.get("group") or "crypto_market")].append(t)
    out: dict[str, Any] = {}
    for name, rows in by.items():
        pnls = [float(t.get("pnl") or 0) for t in rows]
        nets = [float(t.get("net_pct") or 0) for t in rows]
        wins = [p for p in pnls if p > 0]
        out[name] = {
            "n": len(rows),
            "win_rate": round(len(wins) / len(rows), 4) if rows else 0.0,
            "total_pnl_usdt": round(sum(pnls), 2),
            "avg_pct": round(sum(nets) / len(nets), 2) if nets else 0.0,
            "median_pct": round(median(nets), 2) if nets else 0.0,
        }
    return out


def summarize_run(
    trades: list[dict[str, Any]],
    curve: list[tuple[int, float]],
    knobs: SimKnobs,
    *,
    peak_open: int,
    skipped_slots: int,
    skipped_cash: int,
    skipped_illiquid: int,
    skipped_no_next: int,
    selloff_bars: int,
    overlay_enabled_seen: list[bool],
    rotation_stagnant_seen: list[bool],
    ending_cash: float,
) -> dict[str, Any]:
    sells = [t for t in trades if t.get("type") == "SELL"]
    buys = [t for t in trades if t.get("type") == "BUY"]
    diag = {
        "peak_open": peak_open,
        "skipped_no_slot": skipped_slots,
        "skipped_cash_floor": skipped_cash,
        "skipped_too_illiquid": skipped_illiquid,
        "skipped_no_next_bar": skipped_no_next,
        "selloff_bars_seen": selloff_bars,
        "overlay_enabled_seen": overlay_enabled_seen,
        "rotation_stagnant_seen": rotation_stagnant_seen,
        "ending_cash": round(ending_cash, 2),
    }
    if not sells:
        return {
            **diag,
            "n": 0,
            "n_buys": len(buys),
            "note": "keine Trades",
            "win_rate": 0.0,
            "total_pnl_usdt": 0.0,
            "return_on_start_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_like": 0.0,
            "by_group": {},
            "trades": trades,
        }

    rets = [float(t.get("net_pct") or 0) for t in sells]
    pnls = [float(t.get("pnl") or 0) for t in sells]
    wins = [r for r in rets if r > 0]
    rets_sorted = sorted(rets)
    peak = knobs.start_equity
    mdd = 0.0
    for _, e in curve:
        peak = max(peak, e)
        if peak > 0:
            mdd = min(mdd, (e - peak) / peak)
    total_pnl = sum(pnls)
    return {
        **diag,
        "n": len(sells),
        "n_buys": len(buys),
        "win_rate": round(len(wins) / len(sells), 4),
        "median_pct": round(median(rets), 2),
        "avg_pct": round(sum(rets) / len(rets), 2),
        "p10_pct": round(rets_sorted[int(0.10 * (len(rets) - 1))], 2),
        "p90_pct": round(rets_sorted[int(0.90 * (len(rets) - 1))], 2),
        "best_pct": round(max(rets), 2),
        "worst_pct": round(min(rets), 2),
        "total_pnl_usdt": round(total_pnl, 2),
        "return_on_start_pct": round(100.0 * total_pnl / knobs.start_equity, 2) if knobs.start_equity else 0.0,
        "max_drawdown_pct": round(100.0 * mdd, 2),
        "sharpe_like": sharpe_like(trades),
        "by_group": _group_breakdown(trades),
        "exit_reasons": {
            k: sum(1 for t in sells if t.get("exit") == k)
            for k in {t.get("exit") for t in sells}
        },
        "trades": trades,
    }


# -------------------------------------------------------------- benchmark ---

def btc_buy_hold(
    start: datetime,
    end: datetime,
    start_equity: float,
    data: dict[str, list[list[float]]] | None = None,
) -> dict[str, Any]:
    bars = (data or {}).get("BTC/USDT")
    if not bars:
        bars = fetch_ohlcv("BTC/USDT", start, end, "1h")
    if len(bars) < 2:
        return {"btc_buy_hold_pct": None, "pnl_usdt": None, "note": "insufficient BTC history"}
    ret = bars[-1][4] / bars[0][1] - 1.0
    return {
        "btc_buy_hold_pct": round(100.0 * ret, 2),
        "pnl_usdt": round(start_equity * ret, 2),
        "entry_open": bars[0][1],
        "exit_close": bars[-1][4],
        "bars": len(bars),
    }


# ---------------------------------------------------------------- report ---

LIMITATIONS = [
    "Survivorship: universe is today's watchlist plus symbols that appear in surviving ledger snapshots; coins delisted and fully dropped from every snapshot are missing.",
    "Causality is enforced by the engine (signal at close t, fill at open t+1). End-of-window leftover positions are marked out at the last close (not a next open) so P&L is fully realized; those exits are tagged exit=end_of_window.",
    "Cost model matches scripts/backtest_volume_ignition_60d.py (fee_rt=0.002 + 25 bps slip + 2% participation), NOT config.slippage_percent=1.5 (that live buffer is not a fill model).",
    "Position sizing is a simplified ticket: min(max_usdt_per_trade, cash-cash_floor, participation*qvol). Full risk/risk_manager.py (moderate_deploy, venue_quality, adaptive cash_policy, slot eviction) is not wired in.",
    "entry_sensor_15m, exit_sensor and dca_sniper are disabled in BOTH in-memory copies: this engine has no 15m history and no live WS sniper. Cycle DCA (evaluate_dca_addon) remains available. The persisted config.json is unchanged.",
    "Social/CMC/LunarCrush/Santiment signals are not replayed (no historical feed). DecisionEngine therefore sees technical + rotation + correlated-tier overlay only.",
    "Correlated-tier selloff uses the real GroupDrawdownTracker on proxy OHLCV. Live windows are 10–15 minutes; we only have 1h/4h bars, so each bar is sampled as four synthetic ticks (o/h/l/c). Intra-bar path is an approximation, not tick tape.",
    "correlated_tier_selloff_active normally reads a Redis flag. Redis is not replayed; the historical tracker result is injected for the experiment pass (and is a no-op when enabled=false).",
    "Simulated clock: position peak_at/last_trade_at are stamped with bar time; sell_rotation_policy._hours_since is patched to that clock so stagnant_idle_hours is measured in simulated time, not wall-clock.",
    "The isolated in-memory position book is the real strategies.positions store with flush_positions no-op'd so the operator ledger is never written.",
    "us_stock proxies (CRWVG/NBISG/SOXLG/MVLLG) are recently listed: a 90-day since= fetch returns empty from Gate, so we fall back to the newest bars and keep them as 'tier-partial' even below the 80% coverage cut. They do not span the full window.",
    "No shuffled-timing control and no walk-forward / regime buckets — those are Phase 2.",
]


def strip_trades(metrics: dict[str, Any]) -> dict[str, Any]:
    out = dict(metrics)
    out.pop("trades", None)
    # keep spy lists only as booleans for the JSON (can be huge)
    seen = out.get("overlay_enabled_seen")
    if isinstance(seen, list):
        out["overlay_enabled_any_true"] = any(seen)
        out["overlay_enabled_any_false"] = any(not x for x in seen)
        out["overlay_calls"] = len(seen)
        out.pop("overlay_enabled_seen", None)
    seen_s = out.get("rotation_stagnant_seen")
    if isinstance(seen_s, list):
        out["rotation_stagnant_any_true"] = any(seen_s)
        out["rotation_stagnant_any_false"] = any(not x for x in seen_s)
        out.pop("rotation_stagnant_seen", None)
    return out


def run_two_pass(
    data: dict[str, list[list[float]]],
    raw: dict[str, Any],
    knobs: SimKnobs,
    warmup_ts: float,
    *,
    peak_stamp_mode: str = "every_bar",
    verbose: bool = True,
    experiment_overrides: dict[str, Any] | None = None,
    baseline_overrides: dict[str, Any] | None = None,
    skip_baseline: bool = False,
    skip_experiment: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    baseline_cfg, experiment_cfg = build_pass_configs(raw)
    if baseline_overrides:
        baseline_cfg = apply_in_memory_overrides(baseline_cfg, baseline_overrides)
    if experiment_overrides:
        experiment_cfg = apply_in_memory_overrides(experiment_cfg, experiment_overrides)
    base: dict[str, Any] = {}
    exp: dict[str, Any] = {}
    if not skip_baseline:
        print(f"[pass] baseline  (correlated_tier=false, stagnant_rotation=false)")
        base = simulate_portfolio(
            data, baseline_cfg, knobs, warmup_ts=warmup_ts, spy_overlay=True,
            peak_stamp_mode=peak_stamp_mode, verbose=verbose,
        )
        print(
            f"       n={base.get('n', 0)} win%={base.get('win_rate', 0)} "
            f"pnl={base.get('total_pnl_usdt', 0)} mdd={base.get('max_drawdown_pct', 0)} "
            f"sharpe={base.get('sharpe_like', 0)}"
        )
    if not skip_experiment:
        print(f"[pass] experiment (correlated_tier=true,  stagnant_rotation=true)")
        exp = simulate_portfolio(
            data, experiment_cfg, knobs, warmup_ts=warmup_ts, spy_overlay=True,
            peak_stamp_mode=peak_stamp_mode, verbose=verbose,
        )
        print(
            f"       n={exp.get('n', 0)} win%={exp.get('win_rate', 0)} "
            f"pnl={exp.get('total_pnl_usdt', 0)} mdd={exp.get('max_drawdown_pct', 0)} "
            f"sharpe={exp.get('sharpe_like', 0)}"
        )
    return baseline_cfg, experiment_cfg, base, exp


def write_json_report(report: dict[str, Any], timeframe: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = OUT_DIR / f"correlated_tier_backtest_90d_{timeframe}_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path


def _fmt_metrics(m: dict[str, Any]) -> str:
    if not m or m.get("n", 0) == 0:
        return f"n=0  (keine Trades)  skips slot={m.get('skipped_no_slot', 0)} cash={m.get('skipped_cash_floor', 0)} illiq={m.get('skipped_too_illiquid', 0)}"
    return (
        f"n={m.get('n')}  win%={m.get('win_rate')}  "
        f"avg={m.get('avg_pct')}%  med={m.get('median_pct')}%  "
        f"pnl={m.get('total_pnl_usdt')} USDT  ret={m.get('return_on_start_pct')}%  "
        f"mdd={m.get('max_drawdown_pct')}%  sharpe≈{m.get('sharpe_like')}"
    )


def write_markdown(
    *,
    window: dict[str, Any],
    universe: dict[str, Any],
    by_tf: dict[str, dict[str, Any]],
    paths: list[Path],
) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = OUT_DIR / f"{day}_correlated-tier-backtest-90d-phase1.md"
    lines: list[str] = []
    lines.append("# Correlated-tier + stagnant-rotation — 90-Tage Phase-1 Backtest")
    lines.append("")
    lines.append(
        f"**Fenster:** {window['start'][:10]} → {window['end'][:10]} · "
        f"**Universum:** {universe['size']} Symbole "
        f"(traded={len(universe['traded_historically'])}, "
        f"watchlist={len(universe['watchlisted'])}, "
        f"tier={len(universe['correlated_tier'])})"
    )
    lines.append("")
    lines.append("## Ergebnis in einem Satz")
    lines.append("")
    # pick 1h as the headline if present
    headline_tf = "1h" if "1h" in by_tf else next(iter(by_tf), "")
    if headline_tf:
        h = by_tf[headline_tf]
        b, e = h["baseline"], h["experiment"]
        bp, ep = b.get("total_pnl_usdt", 0) or 0, e.get("total_pnl_usdt", 0) or 0
        if (e.get("n") or 0) == 0 and (b.get("n") or 0) == 0:
            lines.append(
                f"**Beide Läufe ({headline_tf}) haben nicht gehandelt.** "
                "Ohne Fills ist der Experiment-Vergleich leer — siehe Limitations."
            )
        elif ep > bp:
            lines.append(
                f"**Experiment schlägt Baseline auf {headline_tf}** "
                f"({ep:+.2f} vs {bp:+.2f} USDT realisiert). "
                "Das ist ein einzelner 90-Tage-Pfad, kein Sweep."
            )
        elif ep < bp:
            lines.append(
                f"**Experiment liegt auf {headline_tf} hinter der Baseline** "
                f"({ep:+.2f} vs {bp:+.2f} USDT). "
                "Die neuen Flags hätten in diesem Fenster nicht geholfen."
            )
        else:
            lines.append(
                f"**Kein Unterschied auf {headline_tf}** "
                f"(beide {bp:+.2f} USDT). Die Flags haben den Pfad nicht verändert."
            )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 1. Universum")
    lines.append("")
    lines.append(f"- Größe: **{universe['size']}**")
    lines.append(f"- Historisch gehandelt: {len(universe['traded_historically'])}")
    lines.append(f"- Aktuelle Watchlist (base + expansion): {len(universe['watchlisted'])}")
    lines.append(f"- correlated_tier Proxies/Members: `{', '.join(universe['correlated_tier'])}`")
    if universe.get("dropped_insufficient"):
        lines.append(
            f"- Zu wenig Historie / unlisted verworfen: {len(universe['dropped_insufficient'])} "
            f"({', '.join(universe['dropped_insufficient'][:12])}"
            f"{'…' if len(universe['dropped_insufficient']) > 12 else ''})"
        )
    if universe.get("tier_partial"):
        lines.append(
            f"- correlated_tier mit Teilhistorie behalten: {universe['tier_partial']}"
        )
    if universe.get("dropped_phantom"):
        lines.append(f"- Phantom-Testsymbole verworfen: {universe['dropped_phantom']}")
    lines.append("")
    lines.append("Vollständige Liste:")
    lines.append("")
    lines.append("```")
    lines.append(", ".join(universe["symbols"]))
    lines.append("```")
    lines.append("")
    lines.append("## 2. Headline-Zahlen")
    lines.append("")
    for tf, block in by_tf.items():
        lines.append(f"### {tf}")
        lines.append("")
        lines.append(f"- **Baseline** (Flags aus): `{_fmt_metrics(block['baseline'])}`")
        lines.append(f"- **Experiment** (Flags an): `{_fmt_metrics(block['experiment'])}`")
        bh = block.get("benchmark") or {}
        if bh.get("btc_buy_hold_pct") is not None:
            lines.append(
                f"- **BTC Buy&Hold** (gleiches Fenster, gleiches Startkapital): "
                f"{bh['btc_buy_hold_pct']:+.2f}% / {bh.get('pnl_usdt')} USDT"
            )
        lines.append("")
        lines.append("| Gruppe | Lauf | n | Win% | Avg % | Med % | PnL USDT |")
        lines.append("|--------|------|--:|-----:|------:|------:|---------:|")
        for run_name, metrics in (("baseline", block["baseline"]), ("experiment", block["experiment"])):
            groups = metrics.get("by_group") or {}
            if not groups:
                lines.append(f"| — | {run_name} | 0 |  |  |  |  |")
                continue
            for gname, g in groups.items():
                lines.append(
                    f"| {gname} | {run_name} | {g.get('n', 0)} | {g.get('win_rate', 0)} | "
                    f"{g.get('avg_pct', 0)} | {g.get('median_pct', 0)} | {g.get('total_pnl_usdt', 0)} |"
                )
        lines.append("")
    lines.append("## 3. Limitations / Approximations")
    lines.append("")
    for item in LIMITATIONS:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 4. Dateien")
    lines.append("")
    for p in paths:
        lines.append(f"- `{p}`")
    lines.append("")
    path.write_text("\n".join(lines))
    return path


# -------------------------------------------------------------- Phase 2 ---

SHUFFLE_SEED_DEFAULT = 42
REGIME_LOOKBACK_SEC = 7 * 86400
REGIME_RISK_OFF_LT = -0.10
REGIME_CHOP_LE = 0.05
PHASE1_REPORTS = {
    "1h": OUT_DIR / "correlated_tier_backtest_90d_1h_20260812_193319.json",
    "4h": OUT_DIR / "correlated_tier_backtest_90d_4h_20260812_193405.json",
}
US_STOCK_DEFAULT = {"CRWVG/USDT", "NBISG/USDT", "SOXLG/USDT", "MVLLG/USDT"}

PHASE2_LIMITATIONS = [
    "Phase 1 headlines stand. This file does not re-score the 90-day 1h/4h two-pass.",
    "Shuffled-timing reuses the Phase-1 cost model and capacity knobs. Entries are random valid bars per symbol (seeded); exits use that symbol's paired BUY→SELL signal hold, not DecisionEngine — this isolates entry timing from the overlay.",
    "Phase 1 JSON stripped the trade tape (strip_trades). Per-trade P&L-by-regime is recovered from a cheap us_stock-only replay (4 members + BTC/ETH proxies) on cached OHLCV, plus a full-universe walk-forward time-slice. That us_stock-only tape does not compete for the 36-slot book, so fill counts can differ from Phase 1; the overlay path is the same.",
    "Regime label = 7-day rolling BTC close-to-close return: < -10% risk_off_bucket, -10%..+5% chop_bucket, > +5% risk_on_bucket. Thresholds are stated, not fitted.",
    "us_stock tokens are recently listed (CRWVG/MVLLG ~20.5d, NBISG/SOXLG ~34.5d inside the 90d window). Early folds have no us_stock sample — folds-won is reported among folds that actually traded the group.",
    "Parameter sweep is one dimension at a time, 1h only, capped. Overlay-knob points keep Phase-1 peak_at stamping (every_bar) so they stay comparable to Phase 1. Tight-book points stamp peak_at only on a genuine new high so stagnant idle can accumulate.",
    "Walk-forward follows hermes/validation.py rolling_folds: half-open [start, start+fold_days), step_days forward. Default 30d/30d → 3 non-overlapping folds.",
    "config.json is never written. All flag/knob changes are in-memory deep copies.",
]


def regime_label(ret: float | None) -> str:
    """Map a 7d BTC return to a bucket. Thresholds: < -10%, -10..+5, > +5."""
    if ret is None:
        return "unknown_bucket"
    try:
        r = float(ret)
    except (TypeError, ValueError):
        return "unknown_bucket"
    if r < REGIME_RISK_OFF_LT:
        return "risk_off_bucket"
    if r <= REGIME_CHOP_LE:
        return "chop_bucket"
    return "risk_on_bucket"


def _close_at_or_before(bars: list[list[float]], ts: int) -> tuple[int, float] | None:
    hit: tuple[int, float] | None = None
    tsi = int(ts)
    for b in bars:
        bt = int(b[0])
        if bt > tsi:
            break
        hit = (bt, float(b[4]))
    return hit


def rolling_btc_return(
    bars: list[list[float]],
    ts: int,
    lookback_sec: int = REGIME_LOOKBACK_SEC,
) -> float | None:
    """close(ts) / close(ts - lookback) - 1. None if either close is missing."""
    here = _close_at_or_before(bars, int(ts))
    prev = _close_at_or_before(bars, int(ts) - int(lookback_sec))
    if here is None or prev is None:
        return None
    if prev[1] <= 0:
        return None
    return here[1] / prev[1] - 1.0


def aggregate_trades_by_regime(
    trades: list[dict[str, Any]],
    btc_bars: list[list[float]],
    *,
    ts_field: str = "fill_ts",
    lookback_sec: int = REGIME_LOOKBACK_SEC,
) -> dict[str, Any]:
    """Post-hoc: bucket SELL pnl by BTC 7d-return regime at fill (or signal) time."""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades or []:
        if t.get("type") != "SELL":
            continue
        ts = t.get(ts_field)
        if ts is None:
            ts = t.get("signal_ts") or t.get("fill_ts")
        try:
            tsi = int(ts)
        except (TypeError, ValueError):
            buckets["unknown_bucket"].append(t)
            continue
        lab = regime_label(rolling_btc_return(btc_bars, tsi, lookback_sec=lookback_sec))
        buckets[lab].append(t)

    out: dict[str, Any] = {}
    for name, rows in buckets.items():
        pnls = [float(t.get("pnl") or 0) for t in rows]
        nets = [float(t.get("net_pct") or 0) for t in rows]
        by_g = _group_breakdown(rows)
        out[name] = {
            "n": len(rows),
            "total_pnl_usdt": round(sum(pnls), 2),
            "avg_pct": round(sum(nets) / len(nets), 2) if nets else 0.0,
            "by_group": by_g,
        }
    return out


def regime_calendar(
    btc_bars: list[list[float]],
    start_ts: int,
    end_ts: int,
    step_sec: int = 86400,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t = int(start_ts)
    end_i = int(end_ts)
    while t < end_i:
        ret = rolling_btc_return(btc_bars, t)
        out.append({
            "ts": t,
            "dt": _iso(t),
            "btc_7d_ret": None if ret is None else round(ret, 6),
            "regime": regime_label(ret),
        })
        t += int(step_sec)
    return out


def buy_count_targets(signals: list[dict[str, Any]], n_buys: int) -> dict[str, int]:
    """Scale per-symbol BUY signals down to the filled-buy count (largest remainder)."""
    counts: dict[str, int] = defaultdict(int)
    for s in signals or []:
        act = str(s.get("action") or "").upper()
        if act.startswith("BUY"):
            sym = str(s.get("symbol") or "")
            if sym:
                counts[sym] += 1
    if not counts or int(n_buys) <= 0:
        return {}
    total = sum(counts.values())
    raw = {sym: int(n_buys) * cnt / total for sym, cnt in counts.items()}
    floors = {sym: int(v) for sym, v in raw.items()}
    rem = int(n_buys) - sum(floors.values())
    order = sorted(raw, key=lambda s: (raw[s] - floors[s], counts[s], s), reverse=True)
    for sym in order:
        if rem <= 0:
            break
        floors[sym] += 1
        rem -= 1
    return {k: v for k, v in floors.items() if v > 0}


def pair_signal_holds(signals: list[dict[str, Any]]) -> dict[str, list[int]]:
    """FIFO BUY → next SELL hold seconds, per symbol."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in signals or []:
        sym = str(s.get("symbol") or "")
        if sym:
            by[sym].append(s)
    holds: dict[str, list[int]] = {}
    for sym, rows in by.items():
        rows = sorted(rows, key=lambda r: int(r.get("ts") or 0))
        pending: list[int] = []
        hs: list[int] = []
        for r in rows:
            act = str(r.get("action") or "").upper()
            ts = int(r.get("ts") or 0)
            if act.startswith("BUY"):
                pending.append(ts)
            elif act.startswith("SELL") and pending:
                bts = pending.pop(0)
                if ts > bts:
                    hs.append(ts - bts)
        if hs:
            holds[sym] = hs
    return holds


def shuffle_entry_plan(
    data: dict[str, list[list[float]]],
    targets: dict[str, int],
    holds: dict[str, list[int]],
    seed: int,
    warmup_ts: float,
    min_idx: int = 24,
) -> dict[str, list[tuple[int, int]]]:
    """Pick random valid (entry_ts, exit_ts) pairs per symbol. Seeded."""
    rng = random.Random(int(seed))
    plan: dict[str, list[tuple[int, int]]] = {}
    warm = float(warmup_ts)
    for sym in sorted(targets):
        n = int(targets[sym] or 0)
        if n <= 0:
            continue
        bars = data.get(sym) or []
        if len(bars) < min_idx + 2:
            plan[sym] = []
            continue
        hold_list = list(holds.get(sym) or []) or [24 * HOUR]
        used: set[int] = set()
        entries: list[tuple[int, int]] = []
        for i in range(n):
            hold_sec = int(hold_list[i % len(hold_list)])
            if hold_sec <= 0:
                hold_sec = 24 * HOUR
            candidates: list[int] = []
            for j, b in enumerate(bars):
                if j < min_idx or j + 1 >= len(bars):
                    continue
                ts = int(b[0])
                if ts < warm or ts in used:
                    continue
                exit_need = ts + hold_sec
                if any(int(bars[k][0]) >= exit_need for k in range(j + 1, len(bars))):
                    candidates.append(j)
            if not candidates:
                # last resort: ignore the used-set
                for j, b in enumerate(bars):
                    if j < min_idx or j + 1 >= len(bars):
                        continue
                    ts = int(b[0])
                    if ts < warm:
                        continue
                    exit_need = ts + hold_sec
                    if any(int(bars[k][0]) >= exit_need for k in range(j + 1, len(bars))):
                        candidates.append(j)
            if not candidates:
                break
            idx = candidates[rng.randrange(len(candidates))]
            entry_ts = int(bars[idx][0])
            exit_need = entry_ts + hold_sec
            exit_ts = next(int(b[0]) for b in bars[idx + 1:] if int(b[0]) >= exit_need)
            entries.append((entry_ts, exit_ts))
            used.add(entry_ts)
        plan[sym] = sorted(entries)
    return plan


def make_plan_decision_fn(
    plan: dict[str, list[tuple[int, int]]],
) -> DecisionFn:
    """BUY on planned entry_ts when flat; SELL_FULL on/after planned exit_ts."""
    queues: dict[str, list[tuple[int, int]]] = {
        s: list(pairs) for s, pairs in plan.items()
    }
    active_exit: dict[str, int] = {}

    def fn(payload: dict[str, Any]) -> dict[str, Any] | None:
        sym = str(payload.get("symbol") or "")
        ts = int(payload["row"]["ts"])
        if payload.get("has_position"):
            ex = active_exit.get(sym)
            if ex is not None and ts >= ex:
                return {"action": "SELL_FULL", "sell_source": "shuffled_hold", "sources": ["shuffled_hold"]}
            return None
        q = queues.get(sym) or []
        if q and q[0][0] == ts:
            _entry, exit_ts = q.pop(0)
            active_exit[sym] = exit_ts
            return {"action": "BUY", "sources": ["shuffled_entry"]}
        return None

    return fn


def rolling_fold_bounds(
    start_ts: int,
    end_ts: int,
    fold_days: int,
    step_days: int,
) -> list[tuple[int, int, int]]:
    """Hermes rolling_folds convention on unix-seconds: half-open [start, start+fold)."""
    fold_sec = int(fold_days) * 86400
    step_sec = int(step_days) * 86400
    if fold_sec <= 0 or step_sec <= 0:
        return []
    out: list[tuple[int, int, int]] = []
    window_start = int(start_ts)
    end_i = int(end_ts)
    fold_id = 0
    while window_start + fold_sec <= end_i:
        window_end = window_start + fold_sec
        out.append((fold_id, window_start, window_end))
        fold_id += 1
        window_start += step_sec
    return out


def slice_ohlcv(
    data: dict[str, list[list[float]]],
    start_ts: int,
    end_ts: int,
    warmup_sec: int = 0,
) -> dict[str, list[list[float]]]:
    lo = int(start_ts) - int(warmup_sec)
    hi = int(end_ts)
    out: dict[str, list[list[float]]] = {}
    for sym, bars in data.items():
        kept = [b for b in bars if lo <= int(b[0]) < hi]
        if kept:
            out[sym] = kept
    return out


def apply_in_memory_overrides(cfg: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Deep-copy cfg and apply sweep knobs. Never touches disk."""
    out = copy.deepcopy(cfg)
    if not overrides:
        return out
    us = _ensure_path(out, "sell_policy", "correlated_tier", "groups", "us_stock")
    if "trail_pct" in overrides:
        _ensure_path(us, "trailing_take_profit")["trail_pct"] = float(overrides["trail_pct"])
    if "full_close_gain_pct" in overrides:
        us["full_close_gain_pct"] = float(overrides["full_close_gain_pct"])
    rot = _ensure_path(out, "sell_policy", "rotation")
    for key in ("stagnant_slack_slots", "stagnant_gain_pct", "stagnant_idle_hours"):
        if key in overrides:
            rot[key] = overrides[key]
    if "max_open_positions" in overrides:
        out["max_open_positions"] = int(overrides["max_open_positions"])
    return out


def compact_trades(trades: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    keep = (
        "type", "symbol", "group", "action", "signal_ts", "fill_ts", "dt",
        "fill_dt", "pnl", "net_pct", "usdt", "usdt_received", "exit",
    )
    out: list[dict[str, Any]] = []
    for t in trades or []:
        out.append({k: t[k] for k in keep if k in t})
    return out


def group_metric(metrics: dict[str, Any], group: str, key: str = "total_pnl_usdt", default: float = 0.0):
    g = (metrics.get("by_group") or {}).get(group) or {}
    return g.get(key, default)


def stagnant_fire_count(metrics: dict[str, Any]) -> int:
    exits = metrics.get("exit_reasons") or {}
    n = int(exits.get("stagnant_rotation") or 0)
    if n:
        return n
    return sum(1 for t in (metrics.get("trades") or []) if t.get("exit") == "stagnant_rotation")


def listing_span(bars: list[list[float]], window_start: int, window_end: int) -> dict[str, Any]:
    if not bars:
        return {"n_bars": 0, "effective_days": 0.0, "coverage_of_window": 0.0}
    first, last = int(bars[0][0]), int(bars[-1][0])
    span = max(0, last - first)
    win = max(1, int(window_end) - int(window_start))
    return {
        "first_ts": first,
        "last_ts": last,
        "first_dt": _iso(first),
        "last_dt": _iso(last),
        "n_bars": len(bars),
        "effective_days": round(span / 86400.0, 2),
        "coverage_of_window": round(span / win, 4),
    }


def load_phase1_report(timeframe: str) -> dict[str, Any]:
    path = PHASE1_REPORTS.get(timeframe)
    if path is None or not path.exists():
        raise FileNotFoundError(f"Phase 1 report missing for {timeframe}: {path}")
    return json.loads(path.read_text())


def _parse_iso(ts: str) -> datetime:
    raw = str(ts).replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _headline(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "n": metrics.get("n", 0),
        "n_buys": metrics.get("n_buys", 0),
        "win_rate": metrics.get("win_rate", 0),
        "avg_pct": metrics.get("avg_pct"),
        "median_pct": metrics.get("median_pct"),
        "total_pnl_usdt": metrics.get("total_pnl_usdt", 0),
        "return_on_start_pct": metrics.get("return_on_start_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "sharpe_like": metrics.get("sharpe_like"),
        "by_group": metrics.get("by_group") or {},
        "exit_reasons": metrics.get("exit_reasons") or {},
        "peak_open": metrics.get("peak_open"),
        "skipped_no_slot": metrics.get("skipped_no_slot"),
        "stagnant_rotation_n": stagnant_fire_count(metrics),
    }


def run_shuffled_pass(
    data: dict[str, list[list[float]]],
    raw: dict[str, Any],
    knobs: SimKnobs,
    warmup_ts: float,
    phase1: dict[str, Any],
    seed: int,
) -> dict[str, Any]:
    """Same universe / cost / capacity; randomized entry timing; seeded."""
    exp = phase1.get("experiment") or {}
    sigs = list(exp.get("signals") or [])
    n_buys = int(exp.get("n_buys") or 0)
    targets = buy_count_targets(sigs, n_buys)
    holds = pair_signal_holds(sigs)
    plan = shuffle_entry_plan(data, targets, holds, seed=seed, warmup_ts=warmup_ts)
    planned_n = sum(len(v) for v in plan.values())
    print(f"[shuffled] seed={seed} target_buys={n_buys} planned={planned_n} symbols={len(plan)}")
    _, experiment_cfg = build_pass_configs(raw)
    fn = make_plan_decision_fn(plan)
    got = simulate_portfolio(
        data, experiment_cfg, knobs, decision_fn=fn, warmup_ts=warmup_ts, verbose=True,
    )
    print(
        f"       shuffled n={got.get('n', 0)} pnl={got.get('total_pnl_usdt', 0)} "
        f"us_stock={group_metric(got, 'us_stock')}"
    )
    return {
        "seed": seed,
        "target_buys": n_buys,
        "planned_entries": planned_n,
        "targets": targets,
        **_headline(got),
        "timing_edge_vs_experiment_pnl": round(
            float(exp.get("total_pnl_usdt") or 0) - float(got.get("total_pnl_usdt") or 0), 2
        ),
        "timing_edge_vs_experiment_avg_pct": round(
            float(exp.get("avg_pct") or 0) - float(got.get("avg_pct") or 0), 2
        ),
        "us_stock_pnl": group_metric(got, "us_stock"),
        "us_stock_avg_pct": group_metric(got, "us_stock", "avg_pct"),
        "crypto_market_pnl": group_metric(got, "crypto_market"),
    }


def run_us_stock_regime_tape(
    data: dict[str, list[list[float]]],
    raw: dict[str, Any],
    knobs: SimKnobs,
    warmup_ts: float,
    btc_bars: list[list[float]],
) -> dict[str, Any]:
    """Cheap 4-coin (+proxies) replay to recover a us_stock trade tape for regime join."""
    us = us_stock_symbols(raw) or set(US_STOCK_DEFAULT)
    keep = set(us) | {"BTC/USDT", "ETH/USDT"}
    slim = {s: bars for s, bars in data.items() if s in keep}
    print(f"[regime-tape] us_stock-only replay symbols={sorted(slim)}")
    _, _, base, exp = run_two_pass(slim, raw, knobs, warmup_ts, verbose=True)
    return {
        "symbols": sorted(slim),
        "baseline": {
            **_headline(base),
            "by_regime": aggregate_trades_by_regime(base.get("trades") or [], btc_bars),
        },
        "experiment": {
            **_headline(exp),
            "by_regime": aggregate_trades_by_regime(exp.get("trades") or [], btc_bars),
        },
        "delta_us_stock_pnl_by_regime": _regime_group_delta(
            aggregate_trades_by_regime(base.get("trades") or [], btc_bars),
            aggregate_trades_by_regime(exp.get("trades") or [], btc_bars),
            group="us_stock",
        ),
        "note": (
            "us_stock-only tape: no 36-slot competition. Overlay path is real. "
            "Use for regime concentration, not as a restatement of Phase 1 headlines."
        ),
    }


def _regime_group_delta(
    base_reg: dict[str, Any],
    exp_reg: dict[str, Any],
    group: str = "us_stock",
) -> dict[str, Any]:
    names = sorted(set(base_reg) | set(exp_reg))
    out: dict[str, Any] = {}
    for name in names:
        bg = ((base_reg.get(name) or {}).get("by_group") or {}).get(group) or {}
        eg = ((exp_reg.get(name) or {}).get("by_group") or {}).get(group) or {}
        out[name] = {
            "baseline_n": bg.get("n", 0),
            "experiment_n": eg.get("n", 0),
            "baseline_pnl": bg.get("total_pnl_usdt", 0),
            "experiment_pnl": eg.get("total_pnl_usdt", 0),
            "delta_pnl": round(float(eg.get("total_pnl_usdt") or 0) - float(bg.get("total_pnl_usdt") or 0), 2),
            "baseline_avg_pct": bg.get("avg_pct"),
            "experiment_avg_pct": eg.get("avg_pct"),
        }
    return out


def _sweep_spec() -> list[dict[str, Any]]:
    """One-at-a-time. Overlay knobs keep Phase-1 clock; tight-book uses on_progress."""
    return [
        {"name": "trail_pct=2.5", "overrides": {"trail_pct": 2.5}, "peak_stamp": "every_bar", "baseline": False},
        {"name": "trail_pct=5.0", "overrides": {"trail_pct": 5.0}, "peak_stamp": "every_bar", "baseline": False},
        {"name": "full_close_gain_pct=10", "overrides": {"full_close_gain_pct": 10}, "peak_stamp": "every_bar", "baseline": False},
        {"name": "full_close_gain_pct=15", "overrides": {"full_close_gain_pct": 15}, "peak_stamp": "every_bar", "baseline": False},
        {
            "name": "tight_maxopen18_slack2",
            "overrides": {"max_open_positions": 18, "stagnant_slack_slots": 2},
            "peak_stamp": "on_progress",
            "baseline": True,
        },
        {
            "name": "tight_maxopen18_slack8",
            "overrides": {"max_open_positions": 18, "stagnant_slack_slots": 8},
            "peak_stamp": "on_progress",
            "baseline": False,
            "reuse_baseline": "tight_maxopen18_slack2",
        },
        {
            "name": "tight_maxopen18_slack8_gain6_idle12",
            "overrides": {
                "max_open_positions": 18,
                "stagnant_slack_slots": 8,
                "stagnant_gain_pct": 6.0,
                "stagnant_idle_hours": 12.0,
            },
            "peak_stamp": "on_progress",
            "baseline": False,
            "reuse_baseline": "tight_maxopen18_slack2",
        },
        {
            "name": "tight_maxopen12_slack10_gain6_idle12",
            "overrides": {
                "max_open_positions": 12,
                "stagnant_slack_slots": 10,
                "stagnant_gain_pct": 6.0,
                "stagnant_idle_hours": 12.0,
            },
            "peak_stamp": "on_progress",
            "baseline": True,
            "only_if_no_stagnant_yet": True,
        },
    ]


def run_parameter_sweep(
    data: dict[str, list[list[float]]],
    raw: dict[str, Any],
    knobs: SimKnobs,
    warmup_ts: float,
    phase1: dict[str, Any],
    max_passes: int,
) -> list[dict[str, Any]]:
    """Bounded one-at-a-time sweep. Returns one row per point."""
    rows: list[dict[str, Any]] = []
    cached_baselines: dict[str, dict[str, Any]] = {}
    passes = 0
    stagnant_seen = False
    phase1_base = phase1.get("baseline") or {}
    phase1_exp = phase1.get("experiment") or {}

    for spec in _sweep_spec():
        if passes >= max_passes:
            print(f"[sweep] hit max_passes={max_passes}, stopping")
            break
        if spec.get("only_if_no_stagnant_yet") and stagnant_seen:
            print(f"[sweep] skip {spec['name']} (stagnant already observed)")
            continue
        overrides = dict(spec.get("overrides") or {})
        peak_mode = str(spec.get("peak_stamp") or "every_bar")
        k = SimKnobs(**{**knobs.__dict__})
        if "max_open_positions" in overrides:
            k.max_open = int(overrides["max_open_positions"])
        need_base = bool(spec.get("baseline"))
        reuse = spec.get("reuse_baseline")
        print(f"[sweep] {spec['name']} peak_stamp={peak_mode} baseline={need_base}")
        base: dict[str, Any] = {}
        if need_base:
            _, _, base, exp = run_two_pass(
                data, raw, k, warmup_ts,
                peak_stamp_mode=peak_mode,
                experiment_overrides=overrides,
                baseline_overrides={kk: overrides[kk] for kk in ("max_open_positions",) if kk in overrides},
            )
            passes += 2
            cached_baselines[spec["name"]] = base
        else:
            _, _, _skip, exp = run_two_pass(
                data, raw, k, warmup_ts,
                peak_stamp_mode=peak_mode,
                experiment_overrides=overrides,
                skip_baseline=True,
            )
            passes += 1
            if reuse and reuse in cached_baselines:
                base = cached_baselines[reuse]
            else:
                base = phase1_base
        fires = stagnant_fire_count(exp)
        if fires:
            stagnant_seen = True
        base_us = group_metric(base, "us_stock") if base else group_metric(phase1_base, "us_stock")
        exp_us = group_metric(exp, "us_stock")
        row = {
            "name": spec["name"],
            "overrides": overrides,
            "peak_stamp_mode": peak_mode,
            "passes_used": 2 if need_base else 1,
            "baseline_pnl": (base or phase1_base).get("total_pnl_usdt"),
            "experiment_pnl": exp.get("total_pnl_usdt"),
            "delta_pnl_vs_baseline": round(
                float(exp.get("total_pnl_usdt") or 0) - float((base or phase1_base).get("total_pnl_usdt") or 0), 2
            ),
            "delta_pnl_vs_phase1_experiment": round(
                float(exp.get("total_pnl_usdt") or 0) - float(phase1_exp.get("total_pnl_usdt") or 0), 2
            ),
            "n": exp.get("n"),
            "n_buys": exp.get("n_buys"),
            "us_stock_pnl": exp_us,
            "us_stock_pnl_delta_vs_baseline": round(float(exp_us or 0) - float(base_us or 0), 2),
            "stagnant_rotation_fired": fires > 0,
            "stagnant_rotation_n": fires,
            "peak_open": exp.get("peak_open"),
            "exit_reasons": exp.get("exit_reasons") or {},
        }
        rows.append(row)
        print(
            f"       Δpnl={row['delta_pnl_vs_baseline']} us_stockΔ={row['us_stock_pnl_delta_vs_baseline']} "
            f"stagnant={fires} n={row['n']}"
        )
    print(f"[sweep] total simulation passes={passes} (cap {max_passes})")
    return rows


def run_walk_forward(
    data: dict[str, list[list[float]]],
    raw: dict[str, Any],
    knobs: SimKnobs,
    window_start: int,
    window_end: int,
    fold_days: int,
    step_days: int,
    timeframe: str,
) -> dict[str, Any]:
    """3–4 rolling folds, baseline vs experiment, folds-won on us_stock pnl."""
    folds = rolling_fold_bounds(window_start, window_end, fold_days, step_days)
    warmup_sec = _bar_seconds(timeframe) * 24
    fold_rows: list[dict[str, Any]] = []
    won = 0
    scored = 0
    for fold_id, lo, hi in folds:
        sliced = slice_ohlcv(data, lo, hi, warmup_sec=warmup_sec)
        print(f"[fold] {timeframe} #{fold_id} {_iso(lo)} → {_iso(hi)} symbols={len(sliced)}")
        _, _, base, exp = run_two_pass(sliced, raw, knobs, warmup_ts=float(lo), verbose=True)
        b_us = float(group_metric(base, "us_stock") or 0)
        e_us = float(group_metric(exp, "us_stock") or 0)
        b_n = int(group_metric(base, "us_stock", "n", 0) or 0)
        e_n = int(group_metric(exp, "us_stock", "n", 0) or 0)
        has_sample = (b_n + e_n) > 0
        beat = e_us > b_us
        if has_sample:
            scored += 1
            if beat:
                won += 1
        fold_rows.append({
            "fold_id": fold_id,
            "start": _iso(lo),
            "end": _iso(hi),
            "start_ts": lo,
            "end_ts": hi,
            "us_stock_sample": has_sample,
            "baseline_us_stock_n": b_n,
            "experiment_us_stock_n": e_n,
            "baseline_us_stock_pnl": b_us,
            "experiment_us_stock_pnl": e_us,
            "delta_us_stock_pnl": round(e_us - b_us, 2),
            "experiment_beat_baseline": beat,
            "baseline_pnl": base.get("total_pnl_usdt"),
            "experiment_pnl": exp.get("total_pnl_usdt"),
            "baseline_n": base.get("n"),
            "experiment_n": exp.get("n"),
            "stagnant_rotation_n": stagnant_fire_count(exp),
        })
        print(
            f"       us_stock base={b_us} exp={e_us} sample={has_sample} "
            f"beat={beat} stagnant={stagnant_fire_count(exp)}"
        )
    return {
        "fold_days": fold_days,
        "step_days": step_days,
        "folds_total": len(fold_rows),
        "folds_with_us_stock_sample": scored,
        "folds_won": won,
        "folds_won_score": f"{won}/{scored}" if scored else "0/0",
        "folds": fold_rows,
    }


def write_phase2_markdown(report: dict[str, Any], path: Path) -> Path:
    lines: list[str] = []
    lines.append("# Correlated-tier + stagnant-rotation — 90-Tage Phase-2 Rigor")
    lines.append("")
    win = report.get("window") or {}
    lines.append(
        f"**Fenster:** {str(win.get('start', ''))[:10]} → {str(win.get('end', ''))[:10]} · "
        f"Phase-1 Reports unverändert (kein Re-Score der 90d-Headlines)."
    )
    lines.append("")
    lines.append("## Ergebnis in einem Satz")
    lines.append("")
    lines.append(report.get("verdict_sentence") or "Siehe Verdict unten.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 0. us_stock sample size (nicht 90 Tage)")
    lines.append("")
    lines.append(
        "CRWVG/MVLLG sind erst seit ~20.5 Tagen gelistet, NBISG/SOXLG seit ~34.5. "
        "Jede Zahl, die so tut als wäre das ein volles 90-Tage-Sample, ist falsch."
    )
    lines.append("")
    spans = report.get("us_stock_listing") or {}
    if spans:
        lines.append("| Symbol | TF | Bars | First | Last | Effective days | Window coverage |")
        lines.append("|--------|----|-----:|-------|------|---------------:|----------------:|")
        for tf, by_sym in spans.items():
            for sym, sp in by_sym.items():
                lines.append(
                    f"| {sym} | {tf} | {sp.get('n_bars', 0)} | {str(sp.get('first_dt', ''))[:16]} | "
                    f"{str(sp.get('last_dt', ''))[:16]} | {sp.get('effective_days', 0)} | "
                    f"{sp.get('coverage_of_window', 0)} |"
                )
        lines.append("")
    lines.append("## 1. Shuffled-timing control")
    lines.append("")
    lines.append(
        "Gleiche Coins, gleiches Buy-Count-Ziel, gleicher Cost/Capacity-Rahmen — "
        "Einstiegszeitpunkt je Symbol zufällig (seed fest). Trennt 'das Signal timed "
        "richtig' von 'diese Coins liefen sowieso'."
    )
    lines.append("")
    lines.append("| TF | Lauf | n | Avg % | PnL USDT | us_stock PnL | us_stock avg % |")
    lines.append("|----|------|--:|------:|---------:|-------------:|---------------:|")
    for tf, block in (report.get("by_tf") or {}).items():
        p1 = block.get("phase1") or {}
        sh = block.get("shuffled") or {}
        bh = (block.get("benchmark") or {}).get("btc_buy_hold") or block.get("btc_buy_hold") or {}
        for label, m in (
            ("baseline", p1.get("baseline") or {}),
            ("experiment", p1.get("experiment") or {}),
            ("shuffled", sh),
        ):
            us = (m.get("by_group") or {}).get("us_stock") or {}
            us_pnl = m.get("us_stock_pnl", us.get("total_pnl_usdt"))
            us_avg = m.get("us_stock_avg_pct", us.get("avg_pct"))
            lines.append(
                f"| {tf} | {label} | {m.get('n', '')} | {m.get('avg_pct', '')} | "
                f"{m.get('total_pnl_usdt', '')} | {us_pnl} | {us_avg} |"
            )
        if bh.get("btc_buy_hold_pct") is not None:
            lines.append(
                f"| {tf} | BTC buy&hold |  | {bh.get('btc_buy_hold_pct')} | {bh.get('pnl_usdt')} |  |  |"
            )
    lines.append("")
    for tf, block in (report.get("by_tf") or {}).items():
        sh = block.get("shuffled") or {}
        if sh:
            lines.append(
                f"- **{tf} timing-edge** (experiment − shuffled) PnL "
                f"`{sh.get('timing_edge_vs_experiment_pnl')}` USDT / avg "
                f"`{sh.get('timing_edge_vs_experiment_avg_pct')}` pp. "
                f"seed={sh.get('seed')} planned={sh.get('planned_entries')} "
                f"target_buys={sh.get('target_buys')}."
            )
    lines.append("")
    lines.append("## 2. Regime buckets (post-hoc, 7d BTC-Return)")
    lines.append("")
    lines.append(
        "Schwellen (fest, nicht gefittet): **risk_off** = 7d-BTC < −10%, "
        "**chop** = −10% … +5%, **risk_on** = > +5%. "
        "Kein neuer Full-Universe-90d-Lauf — Join gegen BTC-Cache + us_stock-only tape."
    )
    lines.append("")
    cal = report.get("regime_calendar_summary") or {}
    if cal:
        lines.append(
            f"Kalender (1 Punkt/Tag über das 90d-Fenster): "
            + ", ".join(f"{k}={v}" for k, v in cal.items() if k != "n_days")
            + f" (n_days={cal.get('n_days', '')})."
        )
        lines.append("")
    lines.append("| TF | Bucket | base n | exp n | base us_stock PnL | exp us_stock PnL | Δ PnL |")
    lines.append("|----|--------|-------:|------:|------------------:|-----------------:|------:|")
    for tf, block in (report.get("by_tf") or {}).items():
        deltas = ((block.get("regime") or {}).get("delta_us_stock_pnl_by_regime")) or {}
        for bucket, row in deltas.items():
            lines.append(
                f"| {tf} | {bucket} | {row.get('baseline_n', 0)} | {row.get('experiment_n', 0)} | "
                f"{row.get('baseline_pnl', 0)} | {row.get('experiment_pnl', 0)} | "
                f"{row.get('delta_pnl', 0)} |"
            )
    lines.append("")
    lines.append(
        "Wenn der 1h-hilft / 4h-schadet-Effekt in **einem** Bucket (oder nur in der "
        "Listing-Schlussphase) sitzt, ist das ein Regime-Artefakt — dieselbe Warnung, "
        "die das Team bei anderen Features gezogen hat: ein einzelner Stretch ist kein "
        "Promotion-Grund."
    )
    lines.append("")
    lines.append("## 3. Parameter-Sweep (1h, one-at-a-time)")
    lines.append("")
    sweep = report.get("sweep") or []
    lines.append("| Point | n | Δ PnL vs baseline | us_stock Δ | stagnant n | peak_open |")
    lines.append("|-------|--:|------------------:|-----------:|-----------:|----------:|")
    for row in sweep:
        lines.append(
            f"| {row.get('name')} | {row.get('n')} | {row.get('delta_pnl_vs_baseline')} | "
            f"{row.get('us_stock_pnl_delta_vs_baseline')} | {row.get('stagnant_rotation_n')} | "
            f"{row.get('peak_open')} |"
        )
    lines.append("")
    if report.get("sweep_note"):
        lines.append(report["sweep_note"])
        lines.append("")
    lines.append("## 4. Walk-forward folds (Hermes-Konvention)")
    lines.append("")
    for tf, block in (report.get("by_tf") or {}).items():
        wf = block.get("walk_forward") or {}
        lines.append(
            f"### {tf} — folds won **{wf.get('folds_won_score', '?')}** "
            f"(nur Folds mit us_stock-Sample; total folds={wf.get('folds_total', 0)})"
        )
        lines.append("")
        lines.append("| Fold | Window | us_stock n (b/e) | base PnL | exp PnL | Δ | beat? |")
        lines.append("|-----:|--------|-----------------:|---------:|--------:|--:|:-----:|")
        for f in wf.get("folds") or []:
            lines.append(
                f"| {f.get('fold_id')} | {str(f.get('start', ''))[:10]} → {str(f.get('end', ''))[:10]} | "
                f"{f.get('baseline_us_stock_n')}/{f.get('experiment_us_stock_n')} | "
                f"{f.get('baseline_us_stock_pnl')} | {f.get('experiment_us_stock_pnl')} | "
                f"{f.get('delta_us_stock_pnl')} | {'yes' if f.get('experiment_beat_baseline') else 'no'} |"
            )
        lines.append("")
    lines.append("## 5. Verdict")
    lines.append("")
    lines.append(report.get("verdict") or "")
    lines.append("")
    lines.append("## 6. Was wurde begrenzt / warum")
    lines.append("")
    for item in report.get("runtime_bounds") or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 7. Limitations")
    lines.append("")
    for item in PHASE2_LIMITATIONS:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 8. Dateien")
    lines.append("")
    for p in report.get("files") or []:
        lines.append(f"- `{p}`")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path


def _calendar_summary(cal: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    for row in cal:
        counts[str(row.get("regime") or "unknown_bucket")] += 1
    return {"n_days": len(cal), **dict(counts)}


def _build_verdict(report: dict[str, Any]) -> tuple[str, str]:
    """Honest one-liner + longer verdict from whatever Phase 2 actually measured."""
    by_tf = report.get("by_tf") or {}
    sweep = report.get("sweep") or []
    bits: list[str] = []
    one: list[str] = []

    for tf, block in by_tf.items():
        sh = block.get("shuffled") or {}
        edge = sh.get("timing_edge_vs_experiment_pnl")
        if edge is not None:
            if float(edge) > 0:
                bits.append(
                    f"{tf}: experiment PnL beat shuffled by {edge} USDT "
                    f"(timing added something vs random entries)."
                )
            elif float(edge) < 0:
                bits.append(
                    f"{tf}: shuffled beat experiment by {abs(float(edge))} USDT "
                    f"(random timing was better — signal timing did not earn its keep)."
                )
            else:
                bits.append(f"{tf}: shuffled ≈ experiment.")
        wf = block.get("walk_forward") or {}
        bits.append(
            f"{tf} walk-forward us_stock folds-won {wf.get('folds_won_score', '?')} "
            f"({wf.get('folds_with_us_stock_sample', 0)} folds had any us_stock trades)."
        )
        reg = ((block.get("regime") or {}).get("delta_us_stock_pnl_by_regime")) or {}
        signs = {k: float(v.get("delta_pnl") or 0) for k, v in reg.items()}
        if signs:
            pos = [k for k, v in signs.items() if v > 0]
            neg = [k for k, v in signs.items() if v < 0]
            if pos and not neg:
                bits.append(f"{tf} us_stock Δ is non-negative in every populated regime bucket ({pos}).")
            elif neg and not pos:
                bits.append(f"{tf} us_stock Δ is non-positive in every populated regime bucket ({neg}).")
            else:
                bits.append(
                    f"{tf} us_stock Δ flips sign across regimes {signs} — concentrated / regime-specific."
                )

    stagnant_rows = [r for r in sweep if r.get("stagnant_rotation_fired")]
    if sweep:
        if stagnant_rows:
            desc = ", ".join(f"{r['name']} n={r['stagnant_rotation_n']} Δ={r['us_stock_pnl_delta_vs_baseline']}" for r in stagnant_rows)
            bits.append(f"stagnant_rotation fired in: {desc}.")
        else:
            bits.append("stagnant_rotation still never fired in the bounded sweep.")

    # Promotion decision
    wf1 = ((by_tf.get("1h") or {}).get("walk_forward") or {})
    wf4 = ((by_tf.get("4h") or {}).get("walk_forward") or {})
    won1, tot1 = wf1.get("folds_won", 0), wf1.get("folds_with_us_stock_sample", 0)
    won4, tot4 = wf4.get("folds_won", 0), wf4.get("folds_with_us_stock_sample", 0)
    sh1 = (by_tf.get("1h") or {}).get("shuffled") or {}
    sh4 = (by_tf.get("4h") or {}).get("shuffled") or {}
    edge1 = sh1.get("timing_edge_vs_experiment_pnl")
    edge4 = sh4.get("timing_edge_vs_experiment_pnl")

    keep_defaults = True
    reasons: list[str] = []
    if tot1 and won1 < tot1:
        keep_defaults = True
        reasons.append(f"1h folds-won {won1}/{tot1} is not a clean sweep")
    if tot4 and won4 == 0:
        reasons.append(f"4h folds-won {won4}/{tot4}: experiment never beat baseline on us_stock")
    if edge4 is not None and float(edge4) < 0:
        reasons.append("4h shuffled ≥ experiment (no timing edge)")
    if tot1 <= 1:
        reasons.append(
            "us_stock only exists in the last ~20–34 days, so walk-forward has at most one "
            "real fold — that is not a generalization check"
        )

    if tot4 and won4 == 0 and tot1 and won1 <= 1:
        verdict_kind = (
            "Phase 2 does **not** support flipping `correlated_tier.enabled` or "
            "`stagnant_rotation_enabled` in config.json. The 1h us_stock lift from Phase 1 "
            "is confined to a short recently-listed sample and does not survive 4h or a "
            "walk-forward that can actually score more than one fold. Leave the defaults off."
        )
        one.append(
            "**Leave config.json defaults off.** 1h-helps/4h-hurts is a short-sample / "
            "regime-local result, not a promotion case."
        )
    elif keep_defaults:
        verdict_kind = (
            "Phase 2 evidence is mixed-to-negative for a config change. Keep the current "
            "false defaults. Do not treat the Phase 1 1h us_stock print as a reason to ship."
        )
        one.append("**Keep current config.json defaults (both flags false).** Mixed/noisy — not shippable.")
    else:
        verdict_kind = (
            "Phase 2 is more supportive than Phase 1 alone, but us_stock history is still "
            "only 20–34 days. A config flip would still be premature without a longer listing window."
        )
        one.append("**Do not flip config.json yet** — sample is too short even where the sign is friendly.")

    bits.append(verdict_kind)
    sentence = one[0] if one else verdict_kind.split(".")[0] + "."
    return sentence, "\n\n".join(bits)


def run_phase2(args: argparse.Namespace) -> int:
    """Bounded rigor layer on top of the already-verified Phase 1 engine."""
    raw = load_production_config_readonly()
    start_equity = start_equity_from_config(raw)
    ticket = ticket_from_config(raw)
    max_open = max_open_from_config(raw)
    cash_floor = cash_floor_from_config(raw, start_equity)
    seed = int(getattr(args, "seed", SHUFFLE_SEED_DEFAULT) or SHUFFLE_SEED_DEFAULT)
    fold_days = int(getattr(args, "fold_days", 30) or 30)
    step_days = int(getattr(args, "fold_step_days", 30) or 30)
    max_sweep = int(getattr(args, "max_sweep_passes", 12) or 12)
    tfs = [t.strip() for t in str(getattr(args, "timeframes", None) or "1h,4h").split(",") if t.strip()]

    print(f"[phase2] seed={seed} folds={fold_days}/{step_days} max_sweep_passes={max_sweep} tfs={tfs}")
    print(f"[phase2] persisted flags untouched: ct="
          f"{((raw.get('sell_policy') or {}).get('correlated_tier') or {}).get('enabled')} "
          f"stagnant={((raw.get('sell_policy') or {}).get('rotation') or {}).get('stagnant_rotation_enabled')}")

    runtime_bounds = [
        f"Sweep cap = {max_sweep} simulation passes, 1h only, one dimension at a time "
        f"(not a cartesian grid).",
        f"Walk-forward = {fold_days}d folds / {step_days}d step → "
        f"target 3 folds, both requested timeframes.",
        "Shuffled pass uses decision_fn (no DecisionEngine) so it stays cheap.",
        "Regime P&L uses a us_stock-only replay rather than a second full 52-coin 90d harvest.",
        "Phase 1 90d two-pass was not re-run.",
    ]

    by_tf: dict[str, dict[str, Any]] = {}
    listing: dict[str, dict[str, Any]] = {}
    json_paths: list[str] = []
    window_meta: dict[str, Any] = {}
    calendar_summary: dict[str, Any] = {}
    sweep_rows: list[dict[str, Any]] = []
    sweep_note = ""
    loaded: dict[str, dict[str, Any]] = {}

    # Prefer the cheaper 4h path first so a timeout still leaves a complete TF.
    ordered_tfs = [t for t in ("4h", "1h") if t in tfs] + [t for t in tfs if t not in ("4h", "1h")]

    for tf in ordered_tfs:
        p1 = load_phase1_report(tf)
        start = _parse_iso(p1["window"]["start"])
        end = _parse_iso(p1["window"]["end"])
        window_meta = {"start": start.isoformat(), "end": end.isoformat(), "days": p1["window"].get("days", 90)}
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        symbols = list((p1.get("universe") or {}).get("symbols") or [])
        warm = timedelta(seconds=_bar_seconds(tf) * 24)
        print(f"\n[phase2 {tf}] load cache {start:%Y-%m-%d} → {end:%Y-%m-%d} n={len(symbols)}")
        data = load_all(symbols, start - warm, end, tf, int(getattr(args, "workers", 6) or 6))
        listing[tf] = {
            s: listing_span(data.get(s) or [], start_ts, end_ts)
            for s in sorted((us_stock_symbols(raw) or US_STOCK_DEFAULT) & set(data))
        }
        knobs = SimKnobs(
            fee_rt=float(getattr(args, "fee_rt", FEE_RT_DEFAULT)),
            slip_bps=float(getattr(args, "slip_bps", SLIP_BPS_DEFAULT)),
            ticket=ticket,
            max_open=max_open,
            participation=float(getattr(args, "participation", PARTICIPATION_DEFAULT)),
            min_ticket=float(getattr(args, "min_ticket", MIN_TICKET_DEFAULT)),
            start_equity=start_equity,
            cash_floor=cash_floor,
            timeframe=tf,
        )
        btc = data.get("BTC/USDT") or []
        if tf == "1h" or not calendar_summary:
            cal = regime_calendar(btc, start_ts, end_ts)
            calendar_summary = _calendar_summary(cal)
        loaded[tf] = {
            "p1": p1, "data": data, "knobs": knobs,
            "start_ts": start_ts, "end_ts": end_ts, "btc": btc,
        }
        by_tf[tf] = {
            "phase1": {
                "baseline": p1.get("baseline") or {},
                "experiment": p1.get("experiment") or {},
            },
            "benchmark": p1.get("benchmark") or {},
        }

    def _checkpoint(tag: str) -> None:
        """Write a draft markdown so a kill/timeout still leaves findings."""
        draft = {
            "window": window_meta,
            "by_tf": by_tf,
            "sweep": sweep_rows,
            "sweep_note": sweep_note,
            "us_stock_listing": listing,
            "regime_calendar_summary": calendar_summary,
            "runtime_bounds": runtime_bounds + [f"checkpoint={tag}"],
            "files": json_paths,
        }
        sentence, verdict = _build_verdict(draft)
        draft["verdict_sentence"] = sentence
        draft["verdict"] = verdict
        write_phase2_markdown(draft, OUT_DIR / "2026-08-12_correlated-tier-backtest-90d-phase2.md")
        print(f"[checkpoint] {tag} → markdown")

    # Cheap passes first (shuffled + us_stock regime tape), both TFs.
    for tf in ordered_tfs:
        ctx = loaded[tf]
        print(f"\n[phase2 {tf}] shuffled-timing + regime tape")
        by_tf[tf]["shuffled"] = run_shuffled_pass(
            ctx["data"], raw, ctx["knobs"], float(ctx["start_ts"]), ctx["p1"], seed,
        )
        by_tf[tf]["regime"] = run_us_stock_regime_tape(
            ctx["data"], raw, ctx["knobs"], float(ctx["start_ts"]), ctx["btc"],
        )
        _checkpoint(f"after-shuffled-regime-{tf}")

    # Walk-forward next (3 folds × 2 passes × N tfs). 4h first.
    for tf in ordered_tfs:
        ctx = loaded[tf]
        print(f"\n[phase2 {tf}] walk-forward")
        by_tf[tf]["walk_forward"] = run_walk_forward(
            ctx["data"], raw, ctx["knobs"],
            ctx["start_ts"], ctx["end_ts"], fold_days, step_days, tf,
        )
        _checkpoint(f"after-walk-forward-{tf}")

    # Sweep last and only on 1h so we can cut it if the clock runs out.
    if "1h" in loaded:
        ctx = loaded["1h"]
        print("\n[phase2 1h] parameter sweep")
        sweep_rows = run_parameter_sweep(
            ctx["data"], raw, ctx["knobs"], float(ctx["start_ts"]), ctx["p1"], max_sweep,
        )
        fired = [r["name"] for r in sweep_rows if r.get("stagnant_rotation_fired")]
        if fired:
            sweep_note = f"stagnant_rotation observed at: {', '.join(fired)}."
        else:
            sweep_note = (
                "stagnant_rotation did not fire in the bounded 1h sweep "
                "(including the tight-book / easier-gain points that ran)."
            )
        runtime_bounds.append("Sweep ran on 1h only; 4h overlay/stagnant grid was not repeated.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    for tf in ordered_tfs:
        ctx = loaded[tf]
        tf_report = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "phase": 2,
            "timeframe": tf,
            "window": window_meta,
            "seed": seed,
            "phase1_source": str(PHASE1_REPORTS[tf]),
            "us_stock_listing": listing.get(tf) or {},
            "shuffled": by_tf[tf].get("shuffled") or {},
            "regime": by_tf[tf].get("regime") or {},
            "walk_forward": by_tf[tf].get("walk_forward") or {},
            "sweep": sweep_rows if tf == "1h" else [],
            "benchmark": by_tf[tf].get("benchmark") or {},
        }
        outp = OUT_DIR / f"correlated_tier_backtest_90d_phase2_{tf}_{stamp}.json"
        outp.write_text(json.dumps(tf_report, indent=2, default=str))
        print(f"[out] {outp}")
        json_paths.append(str(outp))

    md_report: dict[str, Any] = {
        "window": window_meta,
        "by_tf": by_tf,
        "sweep": sweep_rows,
        "sweep_note": sweep_note,
        "us_stock_listing": listing,
        "regime_calendar_summary": calendar_summary,
        "runtime_bounds": runtime_bounds,
        "files": json_paths,
    }
    sentence, verdict = _build_verdict(md_report)
    md_report["verdict_sentence"] = sentence
    md_report["verdict"] = verdict
    md_path = OUT_DIR / "2026-08-12_correlated-tier-backtest-90d-phase2.md"
    write_phase2_markdown(md_report, md_path)
    md_report["files"] = json_paths + [str(md_path)]
    # rewrite md with files list complete
    write_phase2_markdown(md_report, md_path)
    print(f"[out] {md_path}")
    print(f"[verdict] {sentence}")
    return 0


# -------------------------------------------------------------- Phase 3 ---

PHASE3_MD = OUT_DIR / "2026-08-13_correlated-tier-opportunity-cost-phase3.md"

PHASE3_LIMITATIONS = [
    "Forward returns are a cached-OHLCV close lookup at entry_ts + {24h, 72h, 7d}, "
    "not a re-simulated shadow position with its own stop / trail / rotation / fee path. "
    "That is deliberate: the question is 'was the missed name better on a hold', not "
    "'would DecisionEngine have exited it well'.",
    "The sim still uses the Phase-1/2 static knobs.max_open ceiling. Live "
    "risk.position_capacity (enabled in config.json, adaptive max_open_eff from regime / "
    "cash mode / memory) is not replayed — same isolation as Phase 1/2. The reject "
    "code we log is still risk_manager's max_open_positions.",
    "Capacity is checked at fill time (next-bar open), which is the sim analogue of "
    "RiskManager.evaluate. A skipped pending BUY is discarded, not queued: freeing a "
    "slot does not automatically admit the last rejected name. Redeploy requires a "
    "fresh BUY fill in the window.",
    "Other skip reasons (cash floor, participation/illiquidity) are counted separately "
    "and are not capacity-rejections. Universe-trade-cap and rebuy-cooldown are not "
    "wired into this engine (Phase 1 limitation, unchanged).",
    "1h only. Phase 1 4h baseline had skipped_no_slot=0 (peak_open=35 < 36), so a 4h "
    "pass would add a simulation pass and zero capacity-reject observations.",
    "Part B has n=1 fire by construction (the only Phase-2 sweep point that fired). "
    "A single event cannot support a general claim about redeploy quality.",
    "config.json is never written. All flag/knob changes are in-memory deep copies.",
]


def _fmt_pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{100.0 * float(x):+.{digits}f}%"


def _horizon_row(block: dict[str, Any], name: str) -> dict[str, Any]:
    return (block.get("horizons") or {}).get(name) or {}


def write_phase3_markdown(report: dict[str, Any], path: Path) -> Path:
    window = report.get("window") or {}
    base = report.get("baseline") or {}
    cmp_ = report.get("reject_vs_taken") or {}
    tight = report.get("tight_fire") or {}
    redeploys = report.get("redeploys") or []
    bounds = report.get("runtime_bounds") or []
    verdict = str(report.get("verdict") or "")
    sentence = str(report.get("verdict_sentence") or "")

    lines: list[str] = []
    lines.append("# Correlated-tier — 90-Tage Phase-3 Opportunity Cost")
    lines.append("")
    lines.append(
        f"**Fenster:** {_parse_iso(window['start']):%Y-%m-%d} → "
        f"{_parse_iso(window['end']):%Y-%m-%d} · 1h only · "
        "config.json nicht geschrieben."
    )
    lines.append("")
    lines.append("## Ergebnis in einem Satz")
    lines.append("")
    lines.append(sentence or verdict.split("\n", 1)[0])
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 0. Was diese Phase misst (und was nicht)")
    lines.append("")
    lines.append(
        "Phase 1/2 scored whether a rotated or trail-overlaid position's *own* "
        "outcome was better. Phase 3 asks the opportunity-cost question: when a "
        "BUY is denied because the book is at `max_open`, was the thing we could "
        "not buy better — on a simple hold — than the things we did buy? And when "
        "the one observed `stagnant_rotation` fire freed a slot, did anything "
        "actually take that slot?"
    )
    lines.append("")
    lines.append(
        "Forward return = first cached close at or after `fill_ts + horizon`, "
        "divided by the would-be (or actual) entry price, minus 1. No fees, no "
        "stops, no DecisionEngine exit. Same lookup on rejected names and on "
        "taken BUYs so the comparison is the same object."
    )
    lines.append("")
    lines.append(
        "Capacity reject = live `RiskDecision(code='max_open_positions')`: a flat "
        "new BUY with `open_slots >= max_open_eff`. Cash-floor and illiquidity "
        "skips stay in their own counters."
    )
    lines.append("")
    lines.append("## 1. Why 1h only")
    lines.append("")
    p1_4h = report.get("phase1_4h_capacity") or {}
    lines.append(
        f"Phase 1 4h baseline already reported `skipped_no_slot="
        f"{p1_4h.get('skipped_no_slot', 0)}` at `peak_open="
        f"{p1_4h.get('peak_open', '?')}` (cap 36). Capacity never bound on 4h. "
        "Re-running 4h would spend a simulation pass to reconfirm zero events. "
        "1h Phase 1 had `skipped_no_slot=42` at `peak_open=36` — that is the "
        "timeframe where the question can have an answer."
    )
    lines.append("")
    lines.append("## 2. A — Production baseline (`max_open=36`)")
    lines.append("")
    lines.append(
        f"- Window: `{window.get('start')}` → `{window.get('end')}`"
    )
    lines.append(
        f"- n_buys={base.get('n_buys')}  n_sells={base.get('n')}  "
        f"peak_open={base.get('peak_open')}  "
        f"skipped_no_slot={base.get('skipped_no_slot')}  "
        f"capacity_rejections={base.get('n_capacity_rejections')}  "
        f"skipped_cash_floor={base.get('skipped_cash_floor')}  "
        f"skipped_too_illiquid={base.get('skipped_too_illiquid')}"
    )
    lines.append(
        f"- Phase 1 baseline sanity: skipped_no_slot=42, n_buys=453, peak_open=36. "
        f"This re-run: skipped_no_slot={base.get('skipped_no_slot')}, "
        f"n_buys={base.get('n_buys')}, peak_open={base.get('peak_open')}."
    )
    lines.append("")
    lines.append("### Forward-return distribution: rejected vs taken")
    lines.append("")
    lines.append(
        "| Horizon | Rejected n | Rejected mean | Rejected median | Rejected % pos "
        "| Taken n | Taken mean | Taken median | Taken % pos | Δ mean (rej − taken) |"
    )
    lines.append(
        "|---------|-----------:|--------------:|----------------:|---------------:"
        "|--------:|-----------:|-------------:|------------:|---------------------:|"
    )
    for name in ("24h", "72h", "7d"):
        row = _horizon_row(cmp_, name)
        rj = row.get("rejected") or {}
        tk = row.get("taken") or {}
        lines.append(
            f"| {name} | {rj.get('n', 0)} | {_fmt_pct(rj.get('mean'))} | "
            f"{_fmt_pct(rj.get('median'))} | {_fmt_pct(rj.get('pct_positive'), 1)} | "
            f"{tk.get('n', 0)} | {_fmt_pct(tk.get('mean'))} | "
            f"{_fmt_pct(tk.get('median'))} | {_fmt_pct(tk.get('pct_positive'), 1)} | "
            f"{_fmt_pct(row.get('mean_delta_rejected_minus_taken'))} |"
        )
    lines.append("")
    by_g = cmp_.get("rejected_by_group") or {}
    if by_g:
        lines.append("Rejected candidates by group:")
        lines.append("")
        lines.append("| Group | n |")
        lines.append("|-------|--:|")
        for gname, n in sorted(by_g.items()):
            lines.append(f"| {gname} | {n} |")
        lines.append("")
    lines.append(
        "A positive Δ mean means the names we could not buy beat the names we "
        "did buy on that hold horizon. Sign is not a promotion case by itself — "
        "read the n and the overlap of the distributions."
    )
    lines.append("")
    lines.append("## 3. B — The one stagnant_rotation fire (tight book)")
    lines.append("")
    lines.append(
        "Re-run of Phase 2 sweep point `tight_maxopen18_slack8_gain6_idle12`: "
        "`max_open=18`, `stagnant_slack_slots=8`, `stagnant_gain_pct=6`, "
        "`stagnant_idle_hours=12`, 1h, `peak_stamp=on_progress`. Experiment flags "
        "on (in memory only)."
    )
    lines.append("")
    lines.append(
        f"- n_buys={tight.get('n_buys')}  n_sells={tight.get('n')}  "
        f"peak_open={tight.get('peak_open')}  "
        f"skipped_no_slot={tight.get('skipped_no_slot')}  "
        f"capacity_rejections={tight.get('n_capacity_rejections')}  "
        f"stagnant_rotation_n={tight.get('stagnant_rotation_n')}  "
        f"pnl={tight.get('total_pnl_usdt')}"
    )
    lines.append(
        "Phase 2 recorded this point as n=383 / n_buys=160 / peak_open=18 / "
        "stagnant_n=1 / Δ vs its tight baseline +2469.75 USDT (us_stock Δ −59.49). "
        "This re-run is the experiment pass only (no second tight-baseline)."
    )
    lines.append("")
    if not redeploys:
        lines.append(
            "No `stagnant_rotation` sell appeared in this re-run. "
            "Cannot score redeploy. That would itself be a finding "
            "(the single Phase-2 fire is not stable), but check the JSON."
        )
        lines.append("")
    for i, ev in enumerate(redeploys, 1):
        lines.append(f"### Fire {i}: `{ev.get('rotated_symbol')}` at `{ev.get('fire_dt')}`")
        lines.append("")
        lines.append(
            f"- Rotated out: `{ev.get('rotated_symbol')}`  group=`{ev.get('fire_group')}`  "
            f"fill_price={ev.get('fire_fill_price')}  realized net_pct at rotation="
            f"{ev.get('fire_net_pct')}  pnl={ev.get('fire_pnl')}"
        )
        held = ev.get("rotated_if_held") or {}
        if held:
            bits = []
            for name in ("24h", "72h", "7d"):
                h = held.get(name)
                bits.append(f"{name} {_fmt_pct((h or {}).get('ret') if h else None)}")
            lines.append(f"- If held from the rotation fill (same simple lookup): {', '.join(bits)}")
        lines.append(
            f"- Nearby capacity-rejects in ±{int(ev.get('window_sec', REDEPLOY_WINDOW_SEC))//3600}h: "
            f"{ev.get('nearby_reject_n', 0)}; BUY fills in the post-fire window: "
            f"{ev.get('window_buy_n', 0)}"
        )
        admitted = ev.get("admitted")
        if not admitted:
            lines.append(
                "- **No candidate was admitted shortly after this fire.** "
                "The freed slot did not redeploy into a waiting (or immediately "
                "following) BUY. The one observed fire did not spend the slot "
                "on a better opportunity — it just closed a name."
            )
        else:
            waiting = ev.get("had_waiting_reject")
            w = ev.get("waiting_reject") or {}
            lines.append(
                f"- Admitted shortly after: `{admitted.get('symbol')}` at "
                f"`{admitted.get('fill_dt') or admitted.get('dt')}` "
                f"fill_price={admitted.get('fill_price')} "
                f"group=`{admitted.get('group')}` "
                f"(had been capacity-rejected immediately prior: **{waiting}**)"
            )
            if w:
                lines.append(
                    f"- Waiting reject record: `{w.get('symbol')}` fill_ts={w.get('fill_ts')} "
                    f"would_be_entry={w.get('would_be_entry_price')}"
                )
            real = ev.get("admitted_realized") or {}
            if real:
                lines.append(
                    f"- Admitted name's subsequent realized exit in this run: "
                    f"exit=`{real.get('exit')}` net_pct={real.get('net_pct')} "
                    f"pnl={real.get('pnl')} fill_dt=`{real.get('fill_dt')}`"
                )
            else:
                lines.append(
                    "- Admitted name had no subsequent SELL in this run (still open / unmarked)."
                )
            adm_fwd = ev.get("admitted_if_held") or {}
            if adm_fwd:
                bits = []
                for name in ("24h", "72h", "7d"):
                    h = adm_fwd.get(name)
                    bits.append(f"{name} {_fmt_pct((h or {}).get('ret') if h else None)}")
                lines.append(f"- Admitted name, same simple hold from its fill: {', '.join(bits)}")
        lines.append("")
    lines.append("## 4. Verdict")
    lines.append("")
    lines.append(verdict)
    lines.append("")
    lines.append("## 5. Was wurde begrenzt / warum")
    lines.append("")
    for item in bounds:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 6. Limitations")
    lines.append("")
    for item in report.get("limitations") or PHASE3_LIMITATIONS:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 7. Dateien")
    lines.append("")
    for p in report.get("files") or []:
        lines.append(f"- `{p}`")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path


def _build_phase3_verdict(report: dict[str, Any]) -> tuple[str, str]:
    """Honest, thin-sample-aware verdict. Do not stretch n=1 or a rare bind."""
    cmp_ = report.get("reject_vs_taken") or {}
    base = report.get("baseline") or {}
    n_rej = int(cmp_.get("n_rejected") or base.get("n_capacity_rejections") or 0)
    n_taken = int(cmp_.get("n_taken") or 0)
    n_buys = int(base.get("n_buys") or 0)
    redeploys = report.get("redeploys") or []
    n_fires = len(redeploys)
    n_admitted = sum(1 for e in redeploys if e.get("admitted"))
    n_waiting = sum(1 for e in redeploys if e.get("had_waiting_reject"))

    h24 = _horizon_row(cmp_, "24h")
    d24 = h24.get("mean_delta_rejected_minus_taken")
    d72 = _horizon_row(cmp_, "72h").get("mean_delta_rejected_minus_taken")
    d7d = _horizon_row(cmp_, "7d").get("mean_delta_rejected_minus_taken")

    bits: list[str] = []
    sentence = ""
    if n_rej <= 0:
        sentence = (
            "Capacity almost never binds at production `max_open=36` in this "
            "90-day 1h window, so the 'free up slots' motivation does not have "
            "a sample to stand on."
        )
        bits.append(sentence)
        bits.append(
            "Part A found zero `max_open_positions` rejects on the production "
            "baseline. 4h Phase 1 already had `skipped_no_slot=0`. There is "
            "nothing here that says rotating a winner would have let the book "
            "catch a missed name — the book was not full enough to miss names "
            "for that reason."
        )
    elif n_rej < 10:
        sentence = (
            f"Capacity binds rarely ({n_rej} rejects vs {n_taken} taken BUYs). "
            "The free-up-slots story is possible but the sample is too thin "
            "to claim the missed names were better."
        )
        bits.append(sentence)
    else:
        share = (100.0 * n_rej / n_buys) if n_buys else 0.0
        signs = [d for d in (d24, d72, d7d) if d is not None]
        if signs and all(d > 0 for d in signs):
            cmp_clause = (
                f"missed names beat taken names on mean hold-return at every "
                f"horizon (24h Δ {_fmt_pct(d24)}, 72h {_fmt_pct(d72)}, "
                f"7d {_fmt_pct(d7d)})"
            )
        elif signs and all(d < 0 for d in signs):
            cmp_clause = (
                f"missed names were *worse* than taken names on mean hold-return "
                f"at every horizon (24h Δ {_fmt_pct(d24)}, 72h {_fmt_pct(d72)}, "
                f"7d {_fmt_pct(d7d)})"
            )
        else:
            cmp_clause = (
                f"the reject-vs-taken comparison is mixed across horizons "
                f"(24h Δ {_fmt_pct(d24)}, 72h {_fmt_pct(d72)}, 7d {_fmt_pct(d7d)})"
            )
        sentence = (
            f"Capacity does bind on 1h ({n_rej} `max_open_positions` rejects "
            f"against {n_taken} taken BUYs, ~{share:.1f}% of filled entries), "
            f"and {cmp_clause}."
        )
        bits.append(sentence)

    bits.append("")
    if n_fires == 0:
        bits.append(
            "Part B: the tight-book re-run produced **no** `stagnant_rotation` "
            "fire, so we cannot say whether the Phase-2 event redeployed into "
            "something better. n=0 on a n=1 historical event — do not generalize."
        )
    elif n_admitted == 0:
        bits.append(
            f"Part B: `{n_fires}` stagnant_rotation fire(s), and **none** of them "
            "admitted a BUY in the next 4 hours. The one fire did not redeploy "
            "into a waiting opportunity. That is an important (if unexciting) "
            "result: freeing a slot is not the same as spending it."
        )
        sentence = (
            (sentence + " ") if sentence else ""
        ) + (
            "The single tight-book fire did not redeploy into a waiting candidate."
        )
    elif n_waiting == 0:
        bits.append(
            f"Part B: `{n_fires}` fire(s), `{n_admitted}` BUY(s) filled in the "
            "4h window, but that name had **not** been sitting as a "
            "capacity-rejected candidate at the fire. The slot was used; it "
            "was not used to clear a queue that already existed."
        )
    else:
        bits.append(
            f"Part B: `{n_fires}` fire(s) and `{n_waiting}` of them handed the "
            "freed slot to a name that had just been capacity-rejected. That is "
            "a single-digit sample — report the specific P&L above, do not "
            "promote a rule from it."
        )

    bits.append("")
    bits.append(
        "Do not flip `stagnant_rotation_enabled` (or any other flag) in "
        "config.json on the back of this. Phase 2 already said the overlay "
        "does not generalize; Phase 3 is about whether the *motive* for "
        "rotation — free a slot, catch something better — shows up in this "
        "universe. The evidence is the table and the n=1 fire, not a ship vote."
    )
    return sentence, "\n".join(bits).strip()


def run_phase3(args: argparse.Namespace) -> int:
    """Two new 1h simulation passes. Cache-only. No config.json writes."""
    raw = load_production_config_readonly()
    start_equity = start_equity_from_config(raw)
    ticket = ticket_from_config(raw)
    max_open = max_open_from_config(raw)
    cash_floor = cash_floor_from_config(raw, start_equity)

    p1 = load_phase1_report("1h")
    try:
        p1_4h = load_phase1_report("4h")
    except FileNotFoundError:
        p1_4h = {}
    start = _parse_iso(p1["window"]["start"])
    end = _parse_iso(p1["window"]["end"])
    symbols = list((p1.get("universe") or {}).get("symbols") or [])
    start_ts = int(start.timestamp())
    tf = "1h"
    warm = timedelta(seconds=_bar_seconds(tf) * 24)

    persisted_ct = ((raw.get("sell_policy") or {}).get("correlated_tier") or {}).get("enabled")
    persisted_st = ((raw.get("sell_policy") or {}).get("rotation") or {}).get("stagnant_rotation_enabled")
    print(f"[phase3] 1h only  window={start:%Y-%m-%d} → {end:%Y-%m-%d}  symbols={len(symbols)}")
    print(f"[phase3] persisted flags untouched: ct={persisted_ct} stagnant={persisted_st}")
    print(f"[phase3] max_open={max_open} ticket={ticket} cash_floor={cash_floor:.0f} equity={start_equity}")

    runtime_bounds = [
        "Exactly 2 new full-universe simulation passes (production baseline + "
        "the one Phase-2 tight point that fired). No sweep, no 4h, no walk-forward.",
        "1h only because Phase 1 4h already had skipped_no_slot=0.",
        "Reuse Phase 1 window + OHLCV cache. No network if the cache hits.",
        "Forward returns are a price lookup, not a third simulation of shadow exits.",
        "Tight pass is experiment-only (flags on, peak_stamp=on_progress). "
        "No second tight-baseline — Phase 2 already has that delta.",
    ]

    print(f"[phase3] load cache {start - warm:%Y-%m-%d} → {end:%Y-%m-%d}")
    data = load_all(symbols, start - warm, end, tf, int(getattr(args, "workers", 6) or 6))
    print(f"[phase3] cached symbols with bars: {sum(1 for v in data.values() if v)}/{len(symbols)}")

    knobs_base = SimKnobs(
        fee_rt=float(getattr(args, "fee_rt", FEE_RT_DEFAULT)),
        slip_bps=float(getattr(args, "slip_bps", SLIP_BPS_DEFAULT)),
        ticket=ticket,
        max_open=max_open,
        participation=float(getattr(args, "participation", PARTICIPATION_DEFAULT)),
        min_ticket=float(getattr(args, "min_ticket", MIN_TICKET_DEFAULT)),
        start_equity=start_equity,
        cash_floor=cash_floor,
        timeframe=tf,
    )

    baseline_cfg, _ = build_pass_configs(raw)
    print("[phase3 A] production baseline  (ct=false, stagnant=false, max_open="
          f"{knobs_base.max_open})")
    base = simulate_portfolio(
        data, baseline_cfg, knobs_base, warmup_ts=float(start_ts),
        spy_overlay=True, peak_stamp_mode="every_bar", verbose=True,
    )
    print(
        f"       n={base.get('n', 0)} buys={base.get('n_buys', 0)} "
        f"peak_open={base.get('peak_open')} skipped_no_slot={base.get('skipped_no_slot')} "
        f"capacity_rej={len(base.get('capacity_rejections') or [])} "
        f"pnl={base.get('total_pnl_usdt')}"
    )

    cmp_ = compare_reject_vs_taken(
        list(base.get("capacity_rejections") or []),
        list(base.get("trades") or []),
        data,
    )
    rej_groups: dict[str, int] = {}
    for r in cmp_.get("rejected") or []:
        g = str(r.get("group") or "crypto_market")
        rej_groups[g] = rej_groups.get(g, 0) + 1
    cmp_["rejected_by_group"] = rej_groups
    print("[phase3 A] reject-vs-taken:")
    for name, row in (cmp_.get("horizons") or {}).items():
        rj, tk = row.get("rejected") or {}, row.get("taken") or {}
        print(
            f"       {name}: rej n={rj.get('n')} mean={_fmt_pct(rj.get('mean'))} "
            f"med={_fmt_pct(rj.get('median'))} pos={_fmt_pct(rj.get('pct_positive'), 1)} "
            f"| taken n={tk.get('n')} mean={_fmt_pct(tk.get('mean'))} "
            f"Δ={_fmt_pct(row.get('mean_delta_rejected_minus_taken'))}"
        )

    knobs_tight = SimKnobs(
        **{**knobs_base.__dict__, "max_open": int(PHASE3_TIGHT_OVERRIDES["max_open_positions"])}
    )
    _, experiment_cfg = build_pass_configs(raw)
    experiment_cfg = apply_in_memory_overrides(experiment_cfg, PHASE3_TIGHT_OVERRIDES)
    print(
        "[phase3 B] tight fire config  "
        f"(ct=true, stagnant=true, max_open={knobs_tight.max_open}, "
        "slack=8, gain=6, idle=12, peak_stamp=on_progress)"
    )
    tight = simulate_portfolio(
        data, experiment_cfg, knobs_tight, warmup_ts=float(start_ts),
        spy_overlay=True, peak_stamp_mode="on_progress", verbose=True,
    )
    print(
        f"       n={tight.get('n', 0)} buys={tight.get('n_buys', 0)} "
        f"peak_open={tight.get('peak_open')} skipped_no_slot={tight.get('skipped_no_slot')} "
        f"capacity_rej={len(tight.get('capacity_rejections') or [])} "
        f"stagnant={stagnant_fire_count(tight)} pnl={tight.get('total_pnl_usdt')}"
    )

    redeploys = match_rotation_redeploys(
        list(tight.get("trades") or []),
        list(tight.get("capacity_rejections") or []),
        window_sec=REDEPLOY_WINDOW_SEC,
    )
    for ev in redeploys:
        ev["window_sec"] = REDEPLOY_WINDOW_SEC
        fire_px = ev.get("fire_fill_price")
        fire_ts = ev.get("fire_ts")
        fire_sym = ev.get("rotated_symbol")
        if fire_sym and fire_px and fire_ts:
            held = {}
            bars = data.get(str(fire_sym)) or []
            for name, sec in FORWARD_HORIZONS_SEC.items():
                got = fixed_horizon_return(bars, int(fire_ts), float(fire_px), sec)
                held[name] = None if got is None else {
                    "ret": got["ret"], "exit_ts": got["exit_ts"], "exit_price": got["exit_price"],
                }
            ev["rotated_if_held"] = held
        admitted = ev.get("admitted")
        if admitted:
            adm_fwd = attach_forward_returns(
                [admitted], data, ts_key="fill_ts", price_key="fill_price",
            )
            ev["admitted_if_held"] = (adm_fwd[0].get("forward") if adm_fwd else None)
        print(
            f"[phase3 B] fire {fire_sym} @ {ev.get('fire_dt')} "
            f"waiting={ev.get('had_waiting_reject')} "
            f"admitted={(admitted or {}).get('symbol') if admitted else None} "
            f"nearby_rej={ev.get('nearby_reject_n')} window_buys={ev.get('window_buy_n')}"
        )
    if not redeploys:
        print("[phase3 B] no stagnant_rotation fire in this re-run")

    p1_base = p1.get("baseline") or {}
    p1_4h_base = (p1_4h.get("baseline") or {}) if p1_4h else {}

    def _pass_headline(m: dict[str, Any]) -> dict[str, Any]:
        return {
            "n": m.get("n"),
            "n_buys": m.get("n_buys"),
            "peak_open": m.get("peak_open"),
            "skipped_no_slot": m.get("skipped_no_slot"),
            "skipped_cash_floor": m.get("skipped_cash_floor"),
            "skipped_too_illiquid": m.get("skipped_too_illiquid"),
            "n_capacity_rejections": len(m.get("capacity_rejections") or []),
            "total_pnl_usdt": m.get("total_pnl_usdt"),
            "avg_pct": m.get("avg_pct"),
            "win_rate": m.get("win_rate"),
            "by_group": m.get("by_group") or {},
            "exit_reasons": m.get("exit_reasons") or {},
            "stagnant_rotation_n": stagnant_fire_count(m),
        }

    cmp_out = {
        "n_rejected": cmp_.get("n_rejected"),
        "n_taken": cmp_.get("n_taken"),
        "horizons": cmp_.get("horizons"),
        "rejected_by_group": rej_groups,
        "rejected": cmp_.get("rejected") or [],
    }

    tight_trades_keep = []
    fire_ts_set = {int(e.get("fire_ts") or 0) for e in redeploys}
    keep_syms = {e.get("rotated_symbol") for e in redeploys} | {
        (e.get("admitted") or {}).get("symbol") for e in redeploys
    }
    for t in tight.get("trades") or []:
        ts = int(t.get("fill_ts") or 0)
        if t.get("exit") == "stagnant_rotation":
            tight_trades_keep.append(t)
            continue
        if any(abs(ts - ft) <= REDEPLOY_WINDOW_SEC + 86400 for ft in fire_ts_set):
            if t.get("type") == "BUY" or t.get("symbol") in keep_syms:
                tight_trades_keep.append(t)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = OUT_DIR / f"correlated_tier_backtest_90d_phase3_1h_{stamp}.json"
    md_report: dict[str, Any] = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "phase": 3,
        "timeframe": tf,
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": p1.get("window", {}).get("days", 90),
        },
        "phase1_source": str(PHASE1_REPORTS["1h"]),
        "phase1_4h_capacity": {
            "skipped_no_slot": p1_4h_base.get("skipped_no_slot"),
            "peak_open": p1_4h_base.get("peak_open"),
            "n_buys": p1_4h_base.get("n_buys"),
        },
        "phase1_1h_capacity": {
            "skipped_no_slot": p1_base.get("skipped_no_slot"),
            "peak_open": p1_base.get("peak_open"),
            "n_buys": p1_base.get("n_buys"),
        },
        "params": {
            "fee_rt": knobs_base.fee_rt,
            "slip_bps": knobs_base.slip_bps,
            "ticket": ticket,
            "max_open": max_open,
            "cash_floor": cash_floor,
            "start_equity": start_equity,
            "tight_overrides": dict(PHASE3_TIGHT_OVERRIDES),
            "redeploy_window_sec": REDEPLOY_WINDOW_SEC,
            "forward_horizons_sec": dict(FORWARD_HORIZONS_SEC),
        },
        "baseline": _pass_headline(base),
        "reject_vs_taken": cmp_out,
        "tight_fire": _pass_headline(tight),
        "redeploys": redeploys,
        "tight_fire_nearby_trades": compact_trades(tight_trades_keep),
        "runtime_bounds": runtime_bounds,
        "limitations": PHASE3_LIMITATIONS,
        "persisted_flags": {
            "sell_policy.correlated_tier.enabled": persisted_ct,
            "sell_policy.rotation.stagnant_rotation_enabled": persisted_st,
        },
        "files": [],
    }
    sentence, verdict = _build_phase3_verdict(md_report)
    md_report["verdict_sentence"] = sentence
    md_report["verdict"] = verdict

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(md_report, indent=2, default=str))
    print(f"[out] {json_path}")
    md_report["files"] = [str(json_path), str(PHASE3_MD)]
    write_phase3_markdown(md_report, PHASE3_MD)
    json_path.write_text(json.dumps(md_report, indent=2, default=str))
    print(f"[out] {PHASE3_MD}")
    print(f"[verdict] {sentence}")
    return 0


# ------------------------------------------------------------------- main ---

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--timeframes", default="1h,4h", help="comma-separated, default 1h,4h")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--fee-rt", type=float, default=FEE_RT_DEFAULT)
    ap.add_argument("--slip-bps", type=float, default=SLIP_BPS_DEFAULT)
    ap.add_argument("--participation", type=float, default=PARTICIPATION_DEFAULT)
    ap.add_argument("--min-ticket", type=float, default=MIN_TICKET_DEFAULT)
    ap.add_argument("--max-symbols", type=int, default=0, help="0 = no cap (Phase 1 default)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--phase", type=int, default=1, choices=[1, 2, 3], help="1 = two-pass, 2 = rigor, 3 = opportunity-cost")
    ap.add_argument("--seed", type=int, default=SHUFFLE_SEED_DEFAULT, help="Phase 2 shuffle seed")
    ap.add_argument("--fold-days", type=int, default=30, help="Phase 2 walk-forward fold length")
    ap.add_argument("--fold-step-days", type=int, default=30, help="Phase 2 walk-forward step")
    ap.add_argument("--max-sweep-passes", type=int, default=12, help="Phase 2 sweep simulation-pass cap")
    args = ap.parse_args(argv)

    if int(args.phase) == 2:
        return run_phase2(args)
    if int(args.phase) == 3:
        return run_phase3(args)

    raw = load_production_config_readonly()
    start_equity = start_equity_from_config(raw)
    ticket = ticket_from_config(raw)
    max_open = max_open_from_config(raw)
    cash_floor = cash_floor_from_config(raw, start_equity)

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    # warmup for RSI/BB (20 bars) plus a little slack
    print(f"[config] max_open={max_open} ticket={ticket} cash_floor={cash_floor:.0f} equity={start_equity}")
    print(f"[config] persisted flags: ct={((raw.get('sell_policy') or {}).get('correlated_tier') or {}).get('enabled')} "
          f"stagnant={((raw.get('sell_policy') or {}).get('rotation') or {}).get('stagnant_rotation_enabled')}")

    uni = assemble_universe(raw)
    symbols = list(uni["symbols"])
    if args.max_symbols and len(symbols) > args.max_symbols:
        # keep BTC + tier members, then fill
        keep = set(uni["correlated_tier"]) | {"BTC/USDT", "ETH/USDT"}
        rest = [s for s in symbols if s not in keep]
        symbols = sorted(keep) + rest[: max(0, args.max_symbols - len(keep))]
        print(f"[universe] capped to {len(symbols)} (from {uni['size']})")
    print(f"[universe] {len(symbols)} symbols "
          f"(traded={len(uni['traded_historically'])} watch={len(uni['watchlisted'])} "
          f"tier={uni['correlated_tier']})")
    print("           " + ", ".join(symbols))

    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    by_tf: dict[str, dict[str, Any]] = {}
    json_paths: list[Path] = []
    dropped_union: set[str] = set()
    kept_union: set[str] = set()

    for tf in tfs:
        warm = timedelta(seconds=_bar_seconds(tf) * 24)
        fetch_start = start - warm
        print(f"\n[window] {tf} {start:%Y-%m-%d %H:%M}Z → {end:%Y-%m-%d %H:%M}Z "
              f"+ {warm.total_seconds()/3600:.0f}h warmup")
        print(f"[fetch] {tf} cache={CACHE_DIR}")
        data = load_all(symbols, fetch_start, end, tf, args.workers)
        need = min_bars_needed(args.days, tf)
        warmup_bars = 24
        tier_keep = set(uni["correlated_tier"]) | {"BTC/USDT", "ETH/USDT"}
        dropped: list[str] = []
        partial: list[str] = []
        kept: dict[str, list] = {}
        for s in symbols:
            bars = data.get(s) or []
            if len(bars) >= need:
                kept[s] = bars
            elif s in tier_keep and len(bars) >= warmup_bars:
                kept[s] = bars
                partial.append(s)
            else:
                dropped.append(s)
        data = kept
        dropped_union.update(dropped)
        kept_union.update(data)
        print(f"[fetch] {len(data)} with_history (>= {need} bars or tier-partial); "
              f"{len(dropped)} dropped, {len(partial)} tier-partial")
        if dropped:
            print("         dropped: " + ", ".join(dropped))
        if partial:
            print("         tier-partial: " + ", ".join(
                f"{s}({len(data[s])})" for s in partial
            ))

        knobs = SimKnobs(
            fee_rt=args.fee_rt,
            slip_bps=args.slip_bps,
            ticket=ticket,
            max_open=max_open,
            participation=args.participation,
            min_ticket=args.min_ticket,
            start_equity=start_equity,
            cash_floor=cash_floor,
            timeframe=tf,
        )
        _, _, base, exp = run_two_pass(data, raw, knobs, warmup_ts=start.timestamp())
        bench = btc_buy_hold(start, end, start_equity, data=data)
        print(f"[benchmark] BTC buy&hold {bench.get('btc_buy_hold_pct')}%")

        report = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "phase": 1,
            "timeframe": tf,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": args.days},
            "params": {
                "fee_rt": args.fee_rt,
                "slip_bps": args.slip_bps,
                "participation": args.participation,
                "min_ticket": args.min_ticket,
                "ticket": ticket,
                "max_open": max_open,
                "cash_floor": cash_floor,
                "start_equity": start_equity,
            },
            "universe": {
                "assembled": uni["size"],
                "scanned": len(symbols),
                "with_history": len(data),
                "symbols": sorted(data),
                "dropped_insufficient": dropped,
                "tier_partial": {s: len(data[s]) for s in partial},
                "drop_reasons": {
                    **{s: "insufficient_history_or_unlisted" for s in dropped},
                    **{s: f"recently_listed_kept_partial n={len(data[s])}" for s in partial},
                },
                "traded_historically": uni["traded_historically"],
                "watchlisted": uni["watchlisted"],
                "correlated_tier": uni["correlated_tier"],
                "dropped_phantom": uni["dropped_phantom"],
            },
            "baseline": strip_trades(base),
            "experiment": strip_trades(exp),
            "by_group": {
                "baseline": (base.get("by_group") or {}),
                "experiment": (exp.get("by_group") or {}),
            },
            "benchmark": {"btc_buy_hold": bench},
            "limitations": LIMITATIONS,
            "experiment_flags": {
                "baseline": {
                    "sell_policy.correlated_tier.enabled": False,
                    "sell_policy.rotation.stagnant_rotation_enabled": False,
                },
                "experiment": {
                    "sell_policy.correlated_tier.enabled": True,
                    "sell_policy.rotation.stagnant_rotation_enabled": True,
                    "stagnant_gain_pct": ((raw.get("sell_policy") or {}).get("rotation") or {}).get("stagnant_gain_pct"),
                    "stagnant_idle_hours": ((raw.get("sell_policy") or {}).get("rotation") or {}).get("stagnant_idle_hours"),
                    "stagnant_slack_slots": ((raw.get("sell_policy") or {}).get("rotation") or {}).get("stagnant_slack_slots"),
                    "groups": list((((raw.get("sell_policy") or {}).get("correlated_tier") or {}).get("groups") or {}).keys()),
                },
            },
        }
        out = write_json_report(report, tf)
        print(f"[out] {out}")
        json_paths.append(out)
        by_tf[tf] = {"baseline": strip_trades(base), "experiment": strip_trades(exp), "benchmark": bench}

    uni_md = dict(uni)
    uni_md["dropped_insufficient"] = sorted(dropped_union)
    uni_md["symbols"] = sorted(kept_union) or uni["symbols"]
    # last timeframe's partial map is good enough for the combined md
    if "partial" in dir() and partial:
        uni_md["tier_partial"] = {s: len(data[s]) for s in partial if s in data}
    md = write_markdown(
        window={"start": start.isoformat(), "end": end.isoformat()},
        universe=uni_md,
        by_tf=by_tf,
        paths=json_paths,
    )
    print(f"[out] {md}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    raise SystemExit(main())
