#!/usr/bin/env python3
"""Realtime Exit Radar GUI — Gate WS + trail proximity (probe UI).

UI: tools/exit_radar/static/index.html (Interior.dev-inspired motion patterns).
Backend: public Gate spot.tickers + open ledger positions. Also surfaces bot
exit_realtime hub stats when running in-process.

Usage:
  python3 scripts/gate_ws_live_dashboard.py --ledger-scope demo --port 8765

This process does not place orders. Live sells are handled by the bot hub
(exit_realtime); this GUI visualizes radar + optional hub counters.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import ssl
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WS_URL = "wss://api.gateio.ws/ws/v4/"
CHANNEL = "spot.tickers"
DEFAULT_PAIRS = (
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "DRAMG/USDT",
    "MUG/USDT",
    "UNI/USDT",
    "XPL/USDT",
    "AAVE/USDT",
)


def to_gate_pair(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    if "/" in s:
        base, quote = s.split("/", 1)
        return f"{base}_{quote}"
    if s.endswith("USDT"):
        return f"{s[:-4]}_USDT"
    return s


def from_gate_pair(pair: str) -> str:
    p = str(pair or "").strip().upper()
    if "_" in p:
        return p.replace("_", "/", 1)
    return p


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _hours_since(iso_ts: str | None, now: datetime | None = None) -> float | None:
    if not iso_ts:
        return None
    try:
        last_ts = datetime.fromisoformat(str(iso_ts).replace("Z", ""))
        if last_ts.tzinfo is not None:
            last_ts = last_ts.replace(tzinfo=None)
    except Exception:
        return None
    now = now or datetime.now()
    return (now - last_ts).total_seconds() / 3600.0


def _resolve_trail_pct(peak_gain_pct: float, ttp: dict) -> float:
    if not ttp.get("dynamic_trail", True):
        return float(ttp.get("trail_pct", 6.0))
    lo = float(ttp.get("trail_pct_min", 3.0))
    hi = float(ttp.get("trail_pct_max", 12.0))
    scale_start = float(ttp.get("trail_pct_scale_start_pct", 18.0))
    scale_peak = float(ttp.get("trail_pct_scale_peak_pct", 45.0))
    if peak_gain_pct <= scale_start:
        return lo
    if peak_gain_pct >= scale_peak:
        return hi
    if scale_peak <= scale_start:
        return hi
    t = (peak_gain_pct - scale_start) / (scale_peak - scale_start)
    return lo + t * (hi - lo)


def load_open_positions(scope: str) -> list[dict[str, Any]]:
    """Load open ledger positions + resolved exit params (read-only)."""
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
    # Prefer public Railway proxy when present (internal hostnames fail locally)
    pub = os.environ.get("MONGO_PUBLIC_URL") or ""
    if pub and not os.environ.get("MONGO_URL"):
        os.environ["MONGO_URL"] = pub
    if os.environ.get("MONGO_URL") and "railway.internal" not in os.environ.get(
        "MONGO_URL", ""
    ):
        os.environ.setdefault("DEMO_ALLOW_REMOTE_MONGO", "1")
    try:
        from scripts.operator_mongo import prepare_operator_mongo

        meta = prepare_operator_mongo()
        print(
            f"[{_now_iso()}] mongo db={meta.get('db')} host={meta.get('host')}",
            flush=True,
        )
    except Exception as e:
        print(f"[{_now_iso()}] operator_mongo skip: {e}", flush=True)

    from strategies.positions import is_open_position, load_positions, positions
    from strategies.registry import resolve_strategy_params

    load_positions(scope)
    try:
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    global_sl = float(raw.get("stop_loss_pct") or 50)
    partial_default = float(raw.get("partial_stop_pct") or 25)

    out: list[dict[str, Any]] = []
    for key, pos in positions.items():
        if not is_open_position(pos):
            continue
        base, _, tf = key.rpartition("_")
        symbol = base.replace("_", "/")
        entry = float(pos.get("average_entry") or 0)
        if entry <= 0:
            continue
        coin = {"symbol": symbol, "timeframe": tf}
        try:
            params = resolve_strategy_params(
                coin, has_position=True, frozen_tier=pos.get("strategy_tier")
            )
        except TypeError:
            params = resolve_strategy_params(coin, has_position=True)
        except Exception:
            params = {}

        ttp = dict(params.get("trailing_take_profit") or {})
        ts = dict(params.get("trailing_stop") or {})
        life = dict(params.get("profit_max_lifetime") or {})
        sl = params.get("stop_loss_pct")
        if sl is None:
            sl = global_sl

        out.append(
            {
                "symbol": symbol,
                "timeframe": tf,
                "entry": entry,
                "amount": float(pos.get("amount") or 0),
                "recent_high": float(pos.get("recent_high") or 0),
                "strategy_tier": pos.get("strategy_tier"),
                "first_buy_at": pos.get("first_buy_at") or pos.get("entry_at"),
                "profit_armed_at": pos.get("profit_armed_at"),
                "trail_tp_steps": int(pos.get("trail_tp_steps") or 0),
                "sold_percent": float(pos.get("sold_percent") or 0),
                "dca_rounds": int(pos.get("dca_rounds") or 0),
                "ttp": {
                    "enabled": bool(ttp.get("enabled", False)),
                    "arm_gain_pct": float(ttp.get("arm_gain_pct") or 12),
                    "trail_pct": float(ttp.get("trail_pct") or 6),
                    "trail_pct_min": float(ttp.get("trail_pct_min") or 3),
                    "trail_pct_max": float(ttp.get("trail_pct_max") or 12),
                    "trail_pct_scale_start_pct": float(
                        ttp.get("trail_pct_scale_start_pct") or 18
                    ),
                    "trail_pct_scale_peak_pct": float(
                        ttp.get("trail_pct_scale_peak_pct") or 45
                    ),
                    "dynamic_trail": bool(ttp.get("dynamic_trail", True)),
                    "min_gain_pct": float(
                        ttp.get("min_gain_pct_floor")
                        or ttp.get("min_gain_pct")
                        or 8
                    ),
                    "cooldown_hours": float(ttp.get("cooldown_hours") or 6),
                },
                "trailing_stop": {
                    "enabled": bool(ts.get("enabled", True)),
                    "activation_gain_pct": float(ts.get("activation_gain_pct") or 5),
                    "min_trail_pct": float(ts.get("min_trail_pct") or 8),
                    "max_trail_pct": float(ts.get("max_trail_pct") or 25),
                    "atr_multiplier": float(ts.get("atr_multiplier") or 2),
                },
                "stop_loss_pct": float(sl),
                "partial_stop_pct": float(
                    params.get("partial_stop_pct") or partial_default
                ),
                "safety_tp_pct": params.get("safety_tp_pct"),
                "safety_tp_min_gain_pct": params.get("safety_tp_min_gain_pct"),
                "take_profit_tiers": list(params.get("take_profit_tiers") or []),
                "rsi_sell_min_gain_pct": params.get("rsi_sell_min_gain_pct"),
                "bb_sell_min_gain_pct": params.get("bb_sell_min_gain_pct"),
                "life": {
                    "enabled": bool(life.get("enabled")),
                    "arm_gain_pct": float(life.get("arm_gain_pct") or 3),
                    "max_hours": float(life.get("max_hours") or 96),
                    "min_gain_pct": float(life.get("min_gain_pct") or 1),
                    "skip_if_peak_above_pct": float(
                        life.get("skip_if_peak_above_pct") or 40
                    ),
                },
                "prefer_full_close": True,
            }
        )
    out.sort(key=lambda r: r["symbol"])
    return out


def evaluate_position(
    pos: dict[str, Any],
    price: float,
    *,
    atr_pct_est: float = 5.0,
) -> dict[str, Any]:
    """Pure exit-proximity evaluation against live mark price (viz only)."""
    entry = float(pos["entry"])
    if entry <= 0 or price <= 0:
        return {"symbol": pos["symbol"], "ok": False}

    # Live peak tracking for viz (starts from ledger recent_high or max(entry, price))
    ledger_high = float(pos.get("recent_high") or 0)
    live_high = float(pos.get("_live_high") or 0)
    recent_high = max(ledger_high, live_high, entry, price)
    pos["_live_high"] = recent_high

    gain = (price / entry - 1.0) * 100.0
    peak_gain = (recent_high / entry - 1.0) * 100.0
    drop_from_high = (1.0 - price / recent_high) * 100.0 if recent_high > 0 else 0.0

    ttp = pos["ttp"]
    ts = pos["trailing_stop"]
    life = pos["life"]
    sl_pct = float(pos["stop_loss_pct"])
    partial_sl = float(pos["partial_stop_pct"])

    # --- Trailing take-profit ---
    ttp_trail = _resolve_trail_pct(peak_gain, ttp)
    ttp_armed = bool(ttp.get("enabled")) and peak_gain >= float(ttp["arm_gain_pct"])
    ttp_gain_ok = gain >= float(ttp["min_gain_pct"])
    ttp_drop_needed = ttp_trail
    ttp_room = ttp_trail - drop_from_high  # >0 still room; ≤0 would fire
    ttp_fire_price = recent_high * (1.0 - ttp_trail / 100.0) if recent_high else 0.0
    ttp_would = (
        bool(ttp.get("enabled"))
        and ttp_armed
        and ttp_gain_ok
        and drop_from_high >= ttp_trail
    )
    ttp_near = (
        ttp_armed
        and ttp_gain_ok
        and not ttp_would
        and ttp_room <= 1.5  # within 1.5pp of trail width
    )
    dist_to_arm = float(ttp["arm_gain_pct"]) - peak_gain

    # --- ATR trailing stop (approx with min_trail when no live ATR) ---
    raw_trail = atr_pct_est * float(ts["atr_multiplier"])
    ts_trail = max(float(ts["min_trail_pct"]), min(float(ts["max_trail_pct"]), raw_trail))
    ts_active = bool(ts.get("enabled", True)) and gain >= float(ts["activation_gain_pct"])
    ts_room = ts_trail - drop_from_high
    ts_fire_price = recent_high * (1.0 - ts_trail / 100.0) if recent_high else 0.0
    ts_would = ts_active and drop_from_high >= ts_trail
    ts_near = ts_active and not ts_would and ts_room <= 2.0

    # --- Stop loss ---
    sl_price = entry * (1.0 - sl_pct / 100.0)
    partial_sl_price = entry * (1.0 - partial_sl / 100.0)
    sl_dist = gain + sl_pct  # how many pp above hard SL (0 = at SL)
    sl_would = gain <= -sl_pct
    partial_sl_would = gain <= -partial_sl
    sl_near = not sl_would and sl_dist <= 8.0  # within 8pp of SL

    # --- TP tiers ---
    tiers = [float(t) for t in (pos.get("take_profit_tiers") or [])]
    tiers_hit = [t for t in tiers if gain >= t]
    next_tier = next((t for t in tiers if gain < t), None)
    dist_next_tier = (next_tier - gain) if next_tier is not None else None

    # --- Safety TP ---
    safety_pct = pos.get("safety_tp_pct")
    safety_min = pos.get("safety_tp_min_gain_pct")
    safety_would = False
    if safety_pct is not None and safety_min is not None:
        # simplified: peak reached safety_min and price still above safety_tp band
        safety_would = peak_gain >= float(safety_min) and gain >= float(safety_pct)

    # --- Profit max lifetime ---
    hold_h = _hours_since(pos.get("first_buy_at"))
    profit_armed = bool(pos.get("profit_armed_at")) or (
        life.get("enabled") and peak_gain >= float(life["arm_gain_pct"])
    )
    # arm in viz when peak crosses life arm (don't mutate ledger)
    if life.get("enabled") and peak_gain >= float(life["arm_gain_pct"]):
        profit_armed = True
    life_skip = peak_gain >= float(life["skip_if_peak_above_pct"])
    life_would = False
    life_progress = 0.0
    if life.get("enabled") and profit_armed and not life_skip and hold_h is not None:
        life_progress = min(1.0, hold_h / max(0.01, float(life["max_hours"])))
        life_would = (
            hold_h >= float(life["max_hours"]) and gain >= float(life["min_gain_pct"])
        )

    # --- Soft TA gates (thresholds only — no RSI/BB without candles) ---
    rsi_gate = pos.get("rsi_sell_min_gain_pct")
    bb_gate = pos.get("bb_sell_min_gain_pct")
    rsi_gate_met = rsi_gate is not None and gain >= float(rsi_gate)
    bb_gate_met = bb_gate is not None and gain >= float(bb_gate)

    would_sources: list[str] = []
    if ttp_would:
        would_sources.append("trailing_take_profit")
    if ts_would:
        would_sources.append("trailing_stop")
    if sl_would:
        would_sources.append("stop_loss")
    elif partial_sl_would:
        would_sources.append("partial_stop")
    if life_would:
        would_sources.append("profit_max_lifetime")
    if safety_would:
        would_sources.append("safety_tp")

    near_sources: list[str] = []
    if ttp_near:
        near_sources.append("trailing_take_profit")
    if ts_near:
        near_sources.append("trailing_stop")
    if sl_near:
        near_sources.append("stop_loss")
    if (
        life.get("enabled")
        and profit_armed
        and not life_skip
        and hold_h is not None
        and life_progress >= 0.85
        and not life_would
    ):
        near_sources.append("profit_max_lifetime")
    if next_tier is not None and dist_next_tier is not None and dist_next_tier <= 3.0:
        near_sources.append(f"tp_tier_{int(next_tier)}")

    # urgency: higher = more interesting on the radar
    urgency = 0.0
    if would_sources:
        urgency = 100.0 + len(would_sources) * 10
    elif near_sources:
        urgency = 60.0 + (10.0 - min(ttp_room if ttp_near else 10, 10))
    else:
        # progress toward interesting states
        if ttp_armed:
            urgency = 40.0 + max(0.0, 10.0 - ttp_room)
        elif peak_gain > 0:
            arm = float(ttp["arm_gain_pct"])
            urgency = 20.0 * max(0.0, min(1.0, peak_gain / max(arm, 1.0)))
        if gain < 0:
            urgency = max(urgency, 15.0 * min(1.0, abs(gain) / max(sl_pct, 1.0)))

    notional = float(pos.get("amount") or 0) * price
    pnl_usdt = float(pos.get("amount") or 0) * (price - entry)

    status = "idle"
    if would_sources:
        status = "would_exit"
    elif near_sources:
        status = "near_exit"
    elif ttp_armed or ts_active:
        status = "armed"
    elif gain > 0:
        status = "in_profit"
    elif gain < -1:
        status = "in_loss"

    return {
        "ok": True,
        "symbol": pos["symbol"],
        "timeframe": pos.get("timeframe"),
        "price": price,
        "entry": entry,
        "recent_high": recent_high,
        "gain_pct": round(gain, 3),
        "peak_gain_pct": round(peak_gain, 3),
        "drop_from_high_pct": round(drop_from_high, 3),
        "notional_usdt": round(notional, 2),
        "pnl_usdt": round(pnl_usdt, 2),
        "amount": pos.get("amount"),
        "sold_percent": pos.get("sold_percent"),
        "dca_rounds": pos.get("dca_rounds"),
        "strategy_tier": pos.get("strategy_tier"),
        "status": status,
        "urgency": round(urgency, 2),
        "would_exit": bool(would_sources),
        "would_sources": would_sources,
        "near_exit": bool(near_sources),
        "near_sources": near_sources,
        "prefer_full_close": bool(pos.get("prefer_full_close", True)),
        "ttp": {
            "enabled": bool(ttp.get("enabled")),
            "armed": ttp_armed,
            "arm_gain_pct": float(ttp["arm_gain_pct"]),
            "dist_to_arm_pp": round(dist_to_arm, 2),
            "trail_pct": round(ttp_trail, 2),
            "drop_pct": round(drop_from_high, 3),
            "room_pp": round(ttp_room, 3),
            "fire_price": ttp_fire_price,
            "min_gain_pct": float(ttp["min_gain_pct"]),
            "would": ttp_would,
            "near": ttp_near,
        },
        "trailing_stop": {
            "enabled": bool(ts.get("enabled", True)),
            "active": ts_active,
            "activation_gain_pct": float(ts["activation_gain_pct"]),
            "trail_pct": round(ts_trail, 2),
            "room_pp": round(ts_room, 3),
            "fire_price": ts_fire_price,
            "would": ts_would,
            "near": ts_near,
            "atr_pct_est": atr_pct_est,
        },
        "stop_loss": {
            "pct": sl_pct,
            "price": sl_price,
            "dist_pp": round(sl_dist, 2),
            "would": sl_would,
            "near": sl_near,
            "partial_pct": partial_sl,
            "partial_price": partial_sl_price,
            "partial_would": partial_sl_would,
        },
        "tp_tiers": {
            "tiers": tiers,
            "hit": tiers_hit,
            "next": next_tier,
            "dist_next_pp": round(dist_next_tier, 2) if dist_next_tier is not None else None,
        },
        "safety_tp": {
            "pct": safety_pct,
            "min_gain_pct": safety_min,
            "would": safety_would,
        },
        "life": {
            "enabled": bool(life.get("enabled")),
            "armed": profit_armed,
            "hold_hours": round(hold_h, 1) if hold_h is not None else None,
            "max_hours": float(life["max_hours"]),
            "progress": round(life_progress, 3),
            "skip_runner": life_skip,
            "min_gain_pct": float(life["min_gain_pct"]),
            "would": life_would,
        },
        "ta_gates": {
            "rsi_min_gain_pct": rsi_gate,
            "rsi_gate_met": rsi_gate_met,
            "bb_min_gain_pct": bb_gate,
            "bb_gate_met": bb_gate_met,
            "note": "RSI/BB values need candles — only gain gates shown",
        },
    }


@dataclass
class PairState:
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    change_24h: float = 0.0
    updates: int = 0
    last_delta_pct: float = 0.0
    last_ts: float = 0.0
    flash: int = 0


@dataclass
class Hub:
    pairs: list[str]
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    min_delta_pct: float = 0.05
    enqueue_threshold_pct: float = 0.15
    event_q: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=2000))
    clients: list[queue.Queue] = field(default_factory=list)
    clients_lock: threading.Lock = field(default_factory=threading.Lock)
    by_pair: dict[str, PairState] = field(default_factory=dict)
    exit_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    prev_would: dict[str, frozenset] = field(default_factory=dict)
    stats: dict[str, Any] = field(
        default_factory=lambda: {
            "messages": 0,
            "ticker_updates": 0,
            "subscribe_acks": 0,
            "errors": 0,
            "reconnects": 0,
            "would_enqueue": 0,
            "would_exit_fires": 0,
            "started_at": time.time(),
            "connected": False,
            "subscribed": False,
            "last_stage": "idle",
            "stages": {
                "connect": 0,
                "subscribe": 0,
                "tick_in": 0,
                "delta": 0,
                "enqueue": 0,
                "exit_eval": 0,
            },
        }
    )
    feed: list[dict] = field(default_factory=list)
    feed_lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    pos_lock: threading.Lock = field(default_factory=threading.Lock)

    def broadcast(self, event: dict) -> None:
        event.setdefault("ts", _now_iso())
        event.setdefault("server_ts", time.time())
        with self.feed_lock:
            self.feed.append(event)
            if len(self.feed) > 200:
                self.feed = self.feed[-150:]
        dead: list[queue.Queue] = []
        with self.clients_lock:
            for cq in self.clients:
                try:
                    cq.put_nowait(event)
                except queue.Full:
                    dead.append(cq)
            for cq in dead:
                try:
                    self.clients.remove(cq)
                except ValueError:
                    pass

    def eval_symbol(self, symbol: str, price: float) -> dict[str, Any] | None:
        with self.pos_lock:
            pos = self.positions.get(symbol)
            if not pos:
                return None
            # atr estimate from 24h change magnitude if available
            pair = to_gate_pair(symbol)
            st = self.by_pair.get(pair)
            atr_est = 5.0
            if st and st.change_24h:
                atr_est = max(2.0, min(15.0, abs(float(st.change_24h)) * 0.35 + 2.0))
            row = evaluate_position(pos, price, atr_pct_est=atr_est)
            self.exit_cache[symbol] = row

            # transition alerts
            sources = frozenset(row.get("would_sources") or [])
            prev = self.prev_would.get(symbol, frozenset())
            new_hits = sources - prev
            if new_hits:
                self.stats["would_exit_fires"] += 1
                self.stats["last_stage"] = "exit_eval"
                self.stats["stages"]["exit_eval"] = int(time.time() * 1000)
                self.broadcast(
                    {
                        "type": "would_exit",
                        "stage": "exit_eval",
                        "symbol": symbol,
                        "sources": sorted(new_hits),
                        "gain_pct": row.get("gain_pct"),
                        "peak_gain_pct": row.get("peak_gain_pct"),
                        "drop_from_high_pct": row.get("drop_from_high_pct"),
                        "msg": (
                            f"WOULD EXIT {symbol} via {', '.join(sorted(new_hits))} "
                            f"(gain={row.get('gain_pct')}% peak={row.get('peak_gain_pct')}% "
                            f"drop={row.get('drop_from_high_pct')}%)"
                        ),
                    }
                )
            self.prev_would[symbol] = sources
            return row

    def snapshot(self) -> dict:
        elapsed = max(0.001, time.time() - float(self.stats["started_at"]))
        rows = []
        for pair, st in sorted(self.by_pair.items()):
            rows.append(
                {
                    "symbol": from_gate_pair(pair),
                    "pair": pair,
                    "last": st.last,
                    "bid": st.bid,
                    "ask": st.ask,
                    "change_24h": st.change_24h,
                    "updates": st.updates,
                    "last_delta_pct": st.last_delta_pct,
                    "age_ms": int((time.time() - st.last_ts) * 1000)
                    if st.last_ts
                    else None,
                    "flash": st.flash,
                }
            )
            if st.flash > 0:
                st.flash = max(0, st.flash - 1)

        # re-eval exits with last prices
        exits: list[dict] = []
        with self.pos_lock:
            for symbol, pos in self.positions.items():
                pair = to_gate_pair(symbol)
                st = self.by_pair.get(pair)
                price = st.last if st and st.last > 0 else 0.0
                if price <= 0:
                    # still show position with entry only
                    exits.append(
                        {
                            "ok": True,
                            "symbol": symbol,
                            "price": 0,
                            "entry": pos["entry"],
                            "gain_pct": None,
                            "status": "waiting_tick",
                            "urgency": 0,
                            "would_exit": False,
                            "near_exit": False,
                            "would_sources": [],
                            "near_sources": [],
                        }
                    )
                    continue
                atr_est = 5.0
                if st and st.change_24h:
                    atr_est = max(
                        2.0, min(15.0, abs(float(st.change_24h)) * 0.35 + 2.0)
                    )
                row = evaluate_position(pos, price, atr_pct_est=atr_est)
                self.exit_cache[symbol] = row
                exits.append(row)

        exits.sort(
            key=lambda r: (
                0 if r.get("would_exit") else 1 if r.get("near_exit") else 2,
                -float(r.get("urgency") or 0),
                str(r.get("symbol") or ""),
            )
        )

        n_would = sum(1 for e in exits if e.get("would_exit"))
        n_near = sum(1 for e in exits if e.get("near_exit") and not e.get("would_exit"))
        n_armed = sum(
            1
            for e in exits
            if (e.get("ttp") or {}).get("armed")
            or (e.get("trailing_stop") or {}).get("active")
        )
        n_profit = sum(
            1 for e in exits if (e.get("gain_pct") is not None and e["gain_pct"] > 0)
        )
        n_loss = sum(
            1 for e in exits if (e.get("gain_pct") is not None and e["gain_pct"] < 0)
        )
        total_pnl = sum(float(e.get("pnl_usdt") or 0) for e in exits if e.get("price"))
        total_notional = sum(
            float(e.get("notional_usdt") or 0) for e in exits if e.get("price")
        )

        bot_hub: dict[str, Any] = {"running": False, "disabled": True}
        try:
            from services.exit_realtime.config import (
                exit_realtime_enabled,
                exit_realtime_mode,
            )
            from services.exit_realtime.hub import get_hub

            if not exit_realtime_enabled():
                bot_hub = {"running": False, "disabled": True}
            else:
                h = get_hub()
                if h is None:
                    bot_hub = {
                        "running": False,
                        "disabled": False,
                        "mode": exit_realtime_mode(),
                    }
                else:
                    st = h.stats()
                    bot_hub = {
                        "running": True,
                        "disabled": False,
                        "mode": exit_realtime_mode(),
                        **st,
                    }
        except Exception:
            bot_hub = {"running": False, "disabled": True, "error": True}

        return {
            "type": "snapshot",
            "connected": self.stats["connected"],
            "subscribed": self.stats["subscribed"],
            "last_stage": self.stats["last_stage"],
            "stages": dict(self.stats["stages"]),
            "bot_hub": bot_hub,
            "stats": {
                "messages": self.stats["messages"],
                "ticker_updates": self.stats["ticker_updates"],
                "subscribe_acks": self.stats["subscribe_acks"],
                "errors": self.stats["errors"],
                "would_enqueue": self.stats["would_enqueue"],
                "would_exit_fires": self.stats["would_exit_fires"],
                "updates_per_sec": round(self.stats["ticker_updates"] / elapsed, 2),
                "elapsed_sec": round(elapsed, 1),
                "clients": len(self.clients),
                "positions": len(self.positions),
            },
            "exit_summary": {
                "positions": len(exits),
                "would_exit": n_would,
                "near_exit": n_near,
                "armed": n_armed,
                "in_profit": n_profit,
                "in_loss": n_loss,
                "total_pnl_usdt": round(total_pnl, 2),
                "total_notional_usdt": round(total_notional, 2),
            },
            "pairs": rows,
            "exits": exits,
            "pipeline_pairs": [from_gate_pair(to_gate_pair(p)) for p in self.pairs],
            "thresholds": {
                "min_delta_pct": self.min_delta_pct,
                "enqueue_threshold_pct": self.enqueue_threshold_pct,
            },
        }


def start_gate_ws(hub: Hub) -> threading.Thread:
    try:
        import websocket
    except ImportError as e:
        raise SystemExit(
            "Need websocket-client: pip install websocket-client"
        ) from e

    gate_pairs = [to_gate_pair(p) for p in hub.pairs]

    def on_open(ws: Any) -> None:
        hub.stats["connected"] = True
        hub.stats["last_stage"] = "connect"
        hub.stats["stages"]["connect"] = int(time.time() * 1000)
        hub.broadcast(
            {"type": "stage", "stage": "connect", "msg": f"connected {WS_URL}"}
        )
        # One pair per subscribe: Gate rejects the whole batch if any pair is unknown.
        for gp in gate_pairs:
            sub = {
                "time": int(time.time()),
                "channel": CHANNEL,
                "event": "subscribe",
                "payload": [gp],
            }
            try:
                ws.send(json.dumps(sub))
            except Exception as e:
                hub.broadcast({"type": "error", "msg": f"subscribe send {gp}: {e}"})
        hub.stats["last_stage"] = "subscribe"
        hub.stats["stages"]["subscribe"] = int(time.time() * 1000)
        hub.broadcast(
            {
                "type": "stage",
                "stage": "subscribe",
                "msg": f"subscribe {len(gate_pairs)} pairs (1-by-1)",
                "pairs": gate_pairs,
            }
        )

    def on_message(ws: Any, message: str) -> None:
        hub.stats["messages"] += 1
        try:
            msg = json.loads(message)
        except Exception:
            hub.stats["errors"] += 1
            hub.broadcast({"type": "error", "msg": "non-json frame"})
            return

        channel = msg.get("channel")
        event = msg.get("event")
        if event == "subscribe":
            hub.stats["subscribe_acks"] += 1
            if msg.get("error"):
                hub.stats["errors"] += 1
                payload = msg.get("payload") or []
                hub.broadcast(
                    {
                        "type": "error",
                        "msg": f"subscribe fail {payload}: {msg.get('error')}",
                    }
                )
            else:
                # any successful pair is enough to mark stream live
                hub.stats["subscribed"] = True
            return
        if channel != CHANNEL or event != "update":
            return

        result = msg.get("result")
        items: list[dict] = []
        if isinstance(result, dict):
            items = [result]
        elif isinstance(result, list):
            items = [x for x in result if isinstance(x, dict)]

        for item in items:
            pair = str(item.get("currency_pair") or "")
            try:
                last = float(item.get("last") or 0)
            except (TypeError, ValueError):
                continue
            if not pair or last <= 0:
                continue
            try:
                bid = float(item.get("highest_bid") or 0)
            except (TypeError, ValueError):
                bid = 0.0
            try:
                ask = float(item.get("lowest_ask") or 0)
            except (TypeError, ValueError):
                ask = 0.0
            try:
                chg = float(item.get("change_percentage") or 0)
            except (TypeError, ValueError):
                chg = 0.0

            st = hub.by_pair.setdefault(pair, PairState())
            prev = st.last
            delta_pct = abs(last / prev - 1.0) * 100.0 if prev > 0 else 0.0
            st.last = last
            st.bid = bid
            st.ask = ask
            st.change_24h = chg
            st.updates += 1
            st.last_delta_pct = delta_pct
            st.last_ts = time.time()
            st.flash = 3
            hub.stats["ticker_updates"] += 1
            hub.stats["last_stage"] = "tick_in"
            hub.stats["stages"]["tick_in"] = int(time.time() * 1000)

            sym = from_gate_pair(pair)
            hub.broadcast(
                {
                    "type": "tick",
                    "stage": "tick_in",
                    "symbol": sym,
                    "pair": pair,
                    "last": last,
                    "bid": bid,
                    "ask": ask,
                    "change_24h": chg,
                    "delta_pct": round(delta_pct, 4),
                    "updates": st.updates,
                }
            )

            # Exit radar eval
            exit_row = hub.eval_symbol(sym, last)
            if exit_row and (
                exit_row.get("would_exit")
                or exit_row.get("near_exit")
                or delta_pct >= hub.min_delta_pct
            ):
                hub.stats["stages"]["exit_eval"] = int(time.time() * 1000)
                hub.broadcast(
                    {
                        "type": "exit_update",
                        "stage": "exit_eval",
                        "symbol": sym,
                        "exit": exit_row,
                    }
                )

            if prev > 0 and delta_pct >= hub.min_delta_pct:
                hub.stats["last_stage"] = "delta"
                hub.stats["stages"]["delta"] = int(time.time() * 1000)
                hub.broadcast(
                    {
                        "type": "delta",
                        "stage": "delta",
                        "symbol": sym,
                        "delta_pct": round(delta_pct, 4),
                        "last": last,
                        "msg": f"{sym} moved {delta_pct:+.3f}%",
                    }
                )

            if prev > 0 and delta_pct >= hub.enqueue_threshold_pct:
                hub.stats["would_enqueue"] += 1
                hub.stats["last_stage"] = "enqueue"
                hub.stats["stages"]["enqueue"] = int(time.time() * 1000)
                hub.broadcast(
                    {
                        "type": "enqueue",
                        "stage": "enqueue",
                        "symbol": sym,
                        "delta_pct": round(delta_pct, 4),
                        "priority": 20,
                        "reason": "position_delta",
                        "msg": (
                            f"WOULD enqueue {sym} priority=20 "
                            f"(|Δ|={delta_pct:.3f}% ≥ {hub.enqueue_threshold_pct}%)"
                        ),
                    }
                )

    def on_error(ws: Any, error: Any) -> None:
        hub.stats["errors"] += 1
        hub.stats["connected"] = False
        hub.broadcast({"type": "error", "msg": str(error)})

    def on_close(ws: Any, code: Any, msg: Any) -> None:
        hub.stats["connected"] = False
        hub.stats["subscribed"] = False
        hub.broadcast(
            {"type": "stage", "stage": "idle", "msg": f"ws closed code={code}"}
        )

    def _run() -> None:
        while not hub.stop.is_set():
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            try:
                ws.run_forever(
                    ping_interval=20,
                    ping_timeout=10,
                    sslopt={"context": _ssl_context()},
                )
            except Exception as e:
                hub.stats["errors"] += 1
                hub.broadcast({"type": "error", "msg": f"run_forever: {e}"})
            if hub.stop.is_set():
                break
            hub.stats["reconnects"] += 1
            hub.broadcast(
                {
                    "type": "stage",
                    "stage": "connect",
                    "msg": "reconnecting in 2s…",
                }
            )
            time.sleep(2.0)

    th = threading.Thread(target=_run, name="gate-ws", daemon=True)
    th.start()
    return th


def _load_dashboard_html() -> str:
    """Prefer Interior-inspired GUI; fallback to minimal inline page."""
    path = ROOT / "tools" / "exit_radar" / "static" / "index.html"
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return (
            "<!DOCTYPE html><html><body><h1>Exit Radar</h1>"
            "<p>Missing tools/exit_radar/static/index.html</p></body></html>"
        )



# UI: tools/exit_radar/static/index.html (via _load_dashboard_html)

def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            if args and str(args[0]).startswith("GET /events"):
                return
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                html = _load_dashboard_html()
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/snapshot":
                body = json.dumps(hub.snapshot()).encode("utf-8")
                self._send(200, body, "application/json")
                return
            if path == "/api/health":
                body = json.dumps(
                    {
                        "ok": True,
                        "connected": hub.stats["connected"],
                        "updates": hub.stats["ticker_updates"],
                        "positions": len(hub.positions),
                        "gui": "tools/exit_radar/static/index.html",
                    }
                ).encode("utf-8")
                self._send(200, body, "application/json")
                return
            if path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                cq: queue.Queue = queue.Queue(maxsize=500)
                with hub.clients_lock:
                    hub.clients.append(cq)
                try:
                    init = json.dumps(hub.snapshot())
                    self.wfile.write(f"data: {init}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    return
                try:
                    while not hub.stop.is_set():
                        try:
                            ev = cq.get(timeout=15.0)
                            payload = json.dumps(ev)
                            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            try:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                            except Exception:
                                break
                finally:
                    with hub.clients_lock:
                        try:
                            hub.clients.remove(cq)
                        except ValueError:
                            pass
                return
            self._send(404, b"not found", "text/plain")

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Live Gate WS + exit-rule radar dashboard"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--pairs",
        default="",
        help="Comma-separated symbols (default: open ledger or built-in defaults)",
    )
    p.add_argument(
        "--ledger-scope",
        default="demo",
        help="Ledger scope for open positions (default: demo)",
    )
    p.add_argument("--no-ledger", action="store_true", help="Skip ledger load")
    p.add_argument("--min-delta-pct", type=float, default=0.05)
    p.add_argument("--enqueue-threshold-pct", type=float, default=0.12)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)

    positions_list: list[dict[str, Any]] = []
    if not args.no_ledger:
        try:
            positions_list = load_open_positions(str(args.ledger_scope))
            print(
                f"[{_now_iso()}] loaded {len(positions_list)} open positions "
                f"(scope={args.ledger_scope})",
                flush=True,
            )
        except Exception as e:
            print(f"[{_now_iso()}] ledger load failed: {e}", flush=True)

    if args.pairs.strip():
        pairs = [x.strip() for x in str(args.pairs).split(",") if x.strip()]
    elif positions_list:
        pairs = sorted({r["symbol"] for r in positions_list})
    else:
        pairs = list(DEFAULT_PAIRS)

    if not pairs:
        print("No pairs", file=sys.stderr)
        return 2

    pos_map = {r["symbol"]: r for r in positions_list}
    hub = Hub(
        pairs=pairs,
        positions=pos_map,
        min_delta_pct=float(args.min_delta_pct),
        enqueue_threshold_pct=float(args.enqueue_threshold_pct),
    )
    start_gate_ws(hub)

    handler = make_handler(hub)
    server = ThreadingHTTPServer((args.host, int(args.port)), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[{_now_iso()}] dashboard {url}", flush=True)
    print(f"[{_now_iso()}] pairs ({len(pairs)}): {', '.join(pairs[:12])}"
          f"{'…' if len(pairs)>12 else ''}", flush=True)
    print(
        f"[{_now_iso()}] min_delta={args.min_delta_pct}%  "
        f"enqueue_sim≥{args.enqueue_threshold_pct}%  "
        f"positions={len(pos_map)}",
        flush=True,
    )
    print(f"[{_now_iso()}] Ctrl+C to stop", flush=True)

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _stop(*_a: Any) -> None:
        hub.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        hub.stop.set()
        server.server_close()
        print(
            f"[{_now_iso()}] stopped. updates={hub.stats['ticker_updates']} "
            f"would_enqueue={hub.stats['would_enqueue']} "
            f"exit_fires={hub.stats['would_exit_fires']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
