"""Ledger-backed sell/rotation replay with policy variants A–D and D′."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable

PARTIAL_SIGNALS = frozenset({
    "SELL_PARTIAL_10", "SELL_PARTIAL_20", "SELL_PARTIAL_30", "SELL_PARTIAL_50",
    "SELL_10", "SELL_20", "SELL_30", "SELL_TP",
})
STRUCTURE_SOURCES = frozenset({"bb_upper", "vol_exhaustion", "vol_dump"})
TRAIL_SOURCES = frozenset({"trailing_take_profit", "profit_max_lifetime", "time_profit_exit"})


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except Exception:
        return None


def order_filled_ts(order: dict) -> datetime | None:
    return parse_ts((order.get("timestamps") or {}).get("filled"))


def order_price_usdt(order: dict) -> tuple[float, float, float]:
    """Return (price, amount, usdt)."""
    ex = order.get("execution") or {}
    req = order.get("request") or {}
    price = float(ex.get("price") or req.get("price") or 0)
    amount = float(ex.get("amount") or req.get("amount") or 0)
    usdt = ex.get("usdt") if ex.get("usdt") is not None else req.get("usdt")
    if usdt is not None:
        return price, amount, float(usdt)
    return price, amount, price * amount


def is_partial_signal(signal: str) -> bool:
    sig = (signal or "").upper()
    return sig in PARTIAL_SIGNALS or "PARTIAL" in sig


def is_full_signal(signal: str) -> bool:
    sig = (signal or "").upper()
    return sig in ("SELL_FULL", "SELL") or ("FULL" in sig and "PARTIAL" not in sig)


def infer_sell_category(signal: str, source_hint: str | None = None) -> str:
    sig = (signal or "").upper()
    hint = (source_hint or "").lower()
    if "STOP" in sig:
        return "stop"
    if is_full_signal(sig):
        return "full"
    if hint in STRUCTURE_SOURCES:
        return "structure"
    if hint in TRAIL_SOURCES:
        return "trail"
    if hint in ("technical",) or sig in ("SELL_30", "SELL_20", "SELL_10"):
        return "rsi"
    if hint in ("cmc", "lc", "x", "x_take_profit"):
        return "social"
    if hint and hint.startswith("take_profit"):
        return "take_profit"
    if is_partial_signal(sig):
        return "early_partial"
    return "other"


@dataclass
class SellEvent:
    ts: datetime
    signal: str
    price: float
    amount: float
    usdt: float
    pnl: float
    category: str
    gain_pct: float
    sold_fraction_of_peak: float = 0.0


@dataclass
class PositionCycle:
    symbol: str
    timeframe: str
    entry_ts: datetime | None = None
    close_ts: datetime | None = None
    buys: int = 0
    buy_usdt: float = 0.0
    sells: list[SellEvent] = field(default_factory=list)
    still_open: bool = False
    amount: float = 0.0
    avg_entry: float = 0.0
    peak_amount: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class PolicySpec:
    name: str
    label: str
    arm_gain_pct: float = 12.0
    block_categories_below_arm: frozenset[str] = frozenset({
        "structure", "early_partial", "rsi", "social", "take_profit",
    })
    allow_stop: bool = True
    trail_exit_full_close: bool = False
    ladder_terminal_full_close: bool = False
    tail_exempt_sold_pct: float | None = None
    tail_exempt_notional_usdt: float | None = None
    tail_idle_hours: float | None = None
    rotation_evict_min_gain_pct: float | None = None


@dataclass
class PolicyResult:
    policy: str
    label: str
    executed_sells: int = 0
    blocked_sells: int = 0
    full_close_conversions: int = 0
    tail_auto_closes: int = 0
    realized_pnl: float = 0.0
    deferred_pnl: float = 0.0
    cycles_closed: int = 0
    open_cycles: int = 0
    tail_cycles: int = 0
    effective_open_slots: float = 0.0
    slot_days: float = 0.0
    median_hold_days: float | None = None


POLICIES: dict[str, PolicySpec] = {
    "A": PolicySpec(
        name="A",
        label="Baseline (observed)",
        arm_gain_pct=0.0,
        block_categories_below_arm=frozenset(),
    ),
    "B": PolicySpec(
        name="B",
        label="Trail-exclusive (block early partials below arm)",
        arm_gain_pct=12.0,
    ),
    "C": PolicySpec(
        name="C",
        label="B + full-close on trail/ladder exit",
        arm_gain_pct=12.0,
        trail_exit_full_close=True,
        ladder_terminal_full_close=True,
    ),
    "D": PolicySpec(
        name="D",
        label="C + tail slot exemption + idle cleanup",
        arm_gain_pct=12.0,
        trail_exit_full_close=True,
        ladder_terminal_full_close=True,
        tail_exempt_sold_pct=0.50,
        tail_exempt_notional_usdt=800.0,
        tail_idle_hours=24.0,
    ),
    "D_prime": PolicySpec(
        name="D_prime",
        label="D + rotation-safe (no eviction when gain < 0)",
        arm_gain_pct=12.0,
        trail_exit_full_close=True,
        ladder_terminal_full_close=True,
        tail_exempt_sold_pct=0.50,
        tail_exempt_notional_usdt=800.0,
        tail_idle_hours=24.0,
        rotation_evict_min_gain_pct=0.0,
    ),
}

RECOVERY_DEFAULTS = {
    "loss_pct_min": -25.0,
    "loss_pct_max": -2.0,
    "max_sold_percent": 0.85,
    "min_remainder_usdt": 150.0,
}

POLICY_ORDER = ("A", "B", "C", "D", "D_prime")


def load_decision_sell_hints(path: Path | None) -> dict[tuple[str, datetime], str]:
    """Map (symbol, ts_bucket) -> primary sell source from decisions.jsonl."""
    if not path or not path.exists():
        return {}
    hints: dict[tuple[str, datetime], str] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = str(rec.get("action") or rec.get("normalized_action") or "").upper()
            if "SELL" not in action:
                continue
            symbol = rec.get("symbol")
            ts = parse_ts(rec.get("ts") or rec.get("timestamp"))
            if not symbol or not ts:
                continue
            sources = list(rec.get("sources") or [])
            hint = ""
            for src in sources:
                s = str(src).lower()
                if s in STRUCTURE_SOURCES or s in TRAIL_SOURCES or s in (
                    "technical", "cmc", "lc", "x", "x_take_profit",
                ) or s.startswith("take_profit"):
                    hint = s
                    break
            if not hint and sources:
                hint = str(sources[0]).lower()
            bucket = ts.replace(second=0, microsecond=0)
            hints[(symbol, bucket)] = hint
    return hints


def lookup_source_hint(
    hints: dict[tuple[str, datetime], str],
    symbol: str,
    ts: datetime,
) -> str | None:
    for delta_min in (0, -1, 1, -2, 2, -5, 5):
        bucket = (ts + timedelta(minutes=delta_min)).replace(second=0, microsecond=0)
        key = (symbol, bucket)
        if key in hints:
            return hints[key]
    return None


def build_cycles(orders: Iterable[dict], *, hints: dict | None = None) -> list[PositionCycle]:
    """Split filled orders into buy→sell cycles per symbol/timeframe."""
    hints = hints or {}
    sorted_orders = sorted(
        [o for o in orders if o.get("status") == "filled"],
        key=lambda o: order_filled_ts(o) or datetime.min,
    )
    active: dict[tuple[str, str], PositionCycle] = {}
    closed: list[PositionCycle] = []

    for order in sorted_orders:
        symbol = order.get("symbol") or ""
        tf = order.get("timeframe") or "4h"
        key = (symbol, tf)
        ts = order_filled_ts(order)
        if not ts:
            continue
        side = (order.get("side") or "").lower()
        price, amount, usdt = order_price_usdt(order)
        if price <= 0 or amount <= 0:
            continue

        cycle = active.get(key)
        if side == "buy":
            if cycle is None or (cycle.amount <= 1e-12 and not cycle.still_open):
                if cycle and cycle.buys > 0:
                    closed.append(cycle)
                cycle = PositionCycle(symbol=symbol, timeframe=tf, entry_ts=ts)
                active[key] = cycle
            elif cycle.amount <= 1e-12:
                cycle.entry_ts = ts
                cycle.sells = []
                cycle.realized_pnl = 0.0
                cycle.peak_amount = 0.0

            prev_amt = cycle.amount
            prev_cost = prev_amt * cycle.avg_entry
            cycle.amount += amount
            cycle.avg_entry = (prev_cost + usdt) / cycle.amount if cycle.amount > 0 else price
            cycle.peak_amount = max(cycle.peak_amount, cycle.amount)
            cycle.buys += 1
            cycle.buy_usdt += usdt
            cycle.still_open = True
            if cycle.entry_ts is None:
                cycle.entry_ts = ts
        elif side == "sell" and cycle is not None and cycle.amount > 1e-12:
            gain_pct = ((price / cycle.avg_entry) - 1) * 100 if cycle.avg_entry > 0 else 0.0
            signal = (order.get("signal") or "").strip()
            source_hint = lookup_source_hint(hints, symbol, ts)
            category = infer_sell_category(signal, source_hint)
            sold_frac = amount / cycle.peak_amount if cycle.peak_amount > 0 else 0.0
            pnl = float(order.get("pnl") or 0)
            cycle.sells.append(
                SellEvent(
                    ts=ts,
                    signal=signal,
                    price=price,
                    amount=amount,
                    usdt=usdt,
                    pnl=pnl,
                    category=category,
                    gain_pct=gain_pct,
                    sold_fraction_of_peak=sold_frac,
                )
            )
            cycle.amount = max(0.0, cycle.amount - amount)
            cycle.realized_pnl += pnl
            if cycle.amount <= 1e-12:
                cycle.still_open = False
                cycle.close_ts = ts
                closed.append(cycle)
                active[key] = PositionCycle(symbol=symbol, timeframe=tf)

    for cycle in active.values():
        if cycle.buys > 0 and (cycle.still_open or cycle.amount > 1e-12):
            cycle.still_open = True
            closed.append(cycle)
    return closed


def _cycle_mark_price(cycle: PositionCycle) -> float:
    if cycle.sells:
        return cycle.sells[-1].price
    return cycle.avg_entry if cycle.avg_entry > 0 else 0.0


def _cycle_gain_pct(cycle: PositionCycle, *, price: float | None = None) -> float:
    mark = price if price is not None else _cycle_mark_price(cycle)
    if cycle.avg_entry <= 0 or mark <= 0:
        return 0.0
    return ((mark / cycle.avg_entry) - 1) * 100


def _cycle_sold_pct(cycle: PositionCycle) -> float:
    peak = cycle.peak_amount if cycle.peak_amount > 0 else cycle.amount
    if peak <= 0 or cycle.amount <= 1e-12:
        return 1.0 if cycle.amount <= 1e-12 else 0.0
    return 1.0 - (cycle.amount / peak)


def _can_rotation_evict(spec: PolicySpec, gain_pct: float, realized_pnl: float) -> bool:
    if spec.rotation_evict_min_gain_pct is None:
        return True
    return gain_pct >= spec.rotation_evict_min_gain_pct or realized_pnl > 0


def _is_tail(cycle: PositionCycle, spec: PolicySpec, *, as_of: datetime) -> bool:
    if cycle.amount <= 1e-12 or cycle.peak_amount <= 0:
        return False
    sold_pct = 1.0 - (cycle.amount / cycle.peak_amount)
    notional = cycle.amount * cycle.avg_entry
    if spec.tail_exempt_sold_pct is not None and sold_pct >= spec.tail_exempt_sold_pct:
        return True
    if spec.tail_exempt_notional_usdt is not None and notional < spec.tail_exempt_notional_usdt:
        return True
    if spec.tail_idle_hours and cycle.sells:
        last_sell = cycle.sells[-1].ts
        idle_h = (as_of - last_sell).total_seconds() / 3600.0
        if idle_h >= spec.tail_idle_hours and sold_pct >= 0.20:
            return True
    return False


def _should_block(event: SellEvent, spec: PolicySpec) -> bool:
    if spec.name == "A":
        return False
    if event.category == "stop":
        return not spec.allow_stop
    if event.category == "full":
        return False
    if event.gain_pct >= spec.arm_gain_pct:
        return False
    return event.category in spec.block_categories_below_arm


def _convert_to_full(event: SellEvent, spec: PolicySpec, remaining: float) -> bool:
    if not is_partial_signal(event.signal):
        return False
    if spec.trail_exit_full_close and event.category in TRAIL_SOURCES:
        return True
    if spec.ladder_terminal_full_close and event.category in (
        "structure", "early_partial", "rsi", "take_profit", "trail",
    ):
        return True
    return False


def _replay_cycle(
    cycle: PositionCycle,
    spec: PolicySpec,
    *,
    as_of: datetime,
) -> tuple[float, bool, int, int, int, int, list[SellEvent], float, float]:
    """Simulate one cycle. Returns (realized, closed, executed, blocked, full_conv, tail_close, sim_sells, amount_left, peak)."""
    peak = cycle.peak_amount if cycle.peak_amount > 0 else sum(e.amount for e in cycle.sells) + cycle.amount
    if peak <= 0:
        return 0.0, False, 0, 0, 0, 0, [], 0.0, 0.0

    amount = peak
    avg_entry = cycle.avg_entry if cycle.avg_entry > 0 else cycle.buy_usdt / peak
    realized = 0.0
    executed = blocked = full_conv = tail_close = 0
    sim_sells: list[SellEvent] = []
    closed = False

    if spec.name == "A":
        for event in cycle.sells:
            realized += event.pnl
            executed += 1
            amount = max(0.0, amount - event.amount)
            sim_sells.append(event)
            if amount <= 1e-12:
                closed = True
                break
        if not closed and cycle.amount <= 1e-12 and cycle.close_ts:
            closed = True
        return realized, closed, executed, 0, 0, 0, sim_sells, max(0.0, cycle.amount), peak

    for event in cycle.sells:
        if _should_block(event, spec):
            blocked += 1
            continue

        sell_amount = event.amount
        sell_pnl = event.pnl
        if _convert_to_full(event, spec, amount):
            sell_amount = amount
            sell_pnl = (event.price - avg_entry) * sell_amount
            full_conv += 1

        amount = max(0.0, amount - sell_amount)
        realized += sell_pnl
        executed += 1
        sim_sells.append(event)
        if amount <= 1e-12:
            closed = True
            break

    if not closed and spec.tail_idle_hours and sim_sells and amount > 0:
        sold_pct = 1.0 - (amount / peak)
        idle_h = (as_of - sim_sells[-1].ts).total_seconds() / 3600.0
        if idle_h >= spec.tail_idle_hours and sold_pct >= 0.20:
            gain_pct = ((sim_sells[-1].price / avg_entry) - 1) * 100 if avg_entry > 0 else 0.0
            if _can_rotation_evict(spec, gain_pct, realized):
                realized += (sim_sells[-1].price - avg_entry) * amount
                amount = 0.0
                closed = True
                tail_close = 1

    return realized, closed, executed, blocked, full_conv, tail_close, sim_sells, amount, peak


def simulate_policy(
    cycles: list[PositionCycle],
    spec: PolicySpec,
    *,
    as_of: datetime | None = None,
    max_open_slots: int = 40,
) -> PolicyResult:
    as_of = as_of or datetime.now()
    result = PolicyResult(policy=spec.name, label=spec.label)
    hold_days: list[float] = []
    open_slot_days = 0.0

    for cycle in cycles:
        if not cycle.entry_ts or cycle.buys == 0:
            continue

        realized, closed, executed, blocked, full_conv, tail_close, sim_sells, amount_left, peak = _replay_cycle(
            cycle, spec, as_of=as_of,
        )
        result.realized_pnl += realized
        result.executed_sells += executed
        result.blocked_sells += blocked
        result.full_close_conversions += full_conv
        result.tail_auto_closes += tail_close

        if closed:
            result.cycles_closed += 1
        else:
            result.open_cycles += 1
            tail_cycle = PositionCycle(
                symbol=cycle.symbol,
                timeframe=cycle.timeframe,
                amount=amount_left,
                avg_entry=cycle.avg_entry,
                peak_amount=peak,
                sells=sim_sells,
            )
            if _is_tail(tail_cycle, spec, as_of=as_of) and spec.tail_exempt_sold_pct is not None:
                result.tail_cycles += 1
            open_slot_days += max(0.0, (as_of - cycle.entry_ts).total_seconds() / 86400.0)

        end_ts = cycle.close_ts if closed else as_of
        if cycle.entry_ts and end_ts:
            hold_days.append(max(0.0, (end_ts - cycle.entry_ts).total_seconds() / 86400.0))

    result.slot_days = open_slot_days
    result.median_hold_days = median(hold_days) if hold_days else None
    full_open = result.open_cycles - (result.tail_cycles if spec.tail_exempt_sold_pct else 0)
    result.effective_open_slots = min(float(max_open_slots), max(0.0, full_open))
    return result


@dataclass
class BaselineStats:
    filled_orders: int = 0
    buys: int = 0
    sells: int = 0
    sell_volume_usdt: float = 0.0
    partial_sell_share: float = 0.0
    partial_30_pnl: float = 0.0
    closed_cycles: int = 0
    open_cycles: int = 0
    zombie_tails: int = 0
    sell_by_category: Counter = field(default_factory=Counter)
    sell_by_signal: Counter = field(default_factory=Counter)


def compute_baseline(orders: list[dict], cycles: list[PositionCycle]) -> BaselineStats:
    stats = BaselineStats()
    stats.filled_orders = len(orders)
    sell_vol = 0.0
    partial_vol = 0.0
    for o in orders:
        side = (o.get("side") or "").lower()
        if side == "buy":
            stats.buys += 1
        elif side == "sell":
            stats.sells += 1
            _, _, usdt = order_price_usdt(o)
            sell_vol += usdt
            sig = o.get("signal") or "SELL"
            stats.sell_by_signal[sig] += 1
            if is_partial_signal(sig):
                partial_vol += usdt
            if sig == "SELL_PARTIAL_30":
                stats.partial_30_pnl += float(o.get("pnl") or 0)
    stats.sell_volume_usdt = sell_vol
    stats.partial_sell_share = partial_vol / sell_vol if sell_vol > 0 else 0.0
    stats.closed_cycles = sum(1 for c in cycles if c.close_ts and not c.still_open)
    stats.open_cycles = sum(1 for c in cycles if c.still_open or (c.amount > 0 and not c.close_ts))
    for c in cycles:
        if not c.still_open and c.close_ts:
            continue
        if c.peak_amount > 0 and c.amount > 0:
            sold = 1.0 - c.amount / c.peak_amount
            if sold >= 0.20:
                stats.zombie_tails += 1
        for ev in c.sells:
            stats.sell_by_category[ev.category] += 1
    return stats


def filter_cycles(
    cycles: list[PositionCycle],
    *,
    open_only: bool = False,
    since: datetime | None = None,
) -> list[PositionCycle]:
    out: list[PositionCycle] = []
    for c in cycles:
        if open_only and c.close_ts is not None and c.amount <= 1e-12:
            continue
        if open_only and not c.still_open and c.amount <= 1e-12:
            continue
        if since and c.entry_ts and c.entry_ts < since:
            if not (c.still_open or (c.amount > 1e-12 and not c.close_ts)):
                continue
        out.append(c)
    return out


def open_cycles_now(cycles: list[PositionCycle]) -> list[PositionCycle]:
    return [
        c for c in cycles
        if c.buys > 0 and (c.still_open or c.amount > 1e-12) and c.close_ts is None
    ]


@dataclass
class ForwardOpenResult:
    policy: str
    would_close_now: int = 0
    would_close_losers: int = 0
    tail_exempt: int = 0
    full_slots: int = 0
    free_slots: int = 0
    details: list[dict] = field(default_factory=list)


@dataclass
class RecoveryEligibility:
    eligible: int = 0
    blocked: int = 0
    minus_tails: int = 0
    details: list[dict] = field(default_factory=list)


@dataclass
class TailSlotSnapshot:
    open_total: int = 0
    open_full_slots: int = 0
    open_tail_exempt: int = 0
    free_buy_slots: int = 0


def forward_open_analysis(
    open_cycles: list[PositionCycle],
    spec: PolicySpec,
    *,
    as_of: datetime | None = None,
    max_open_slots: int = 40,
) -> ForwardOpenResult:
    """Forward-looking: what to do with positions *as they are now*."""
    as_of = as_of or datetime.now()
    out = ForwardOpenResult(policy=spec.name)
    for c in open_cycles:
        peak = c.peak_amount if c.peak_amount > 0 else c.amount
        if peak <= 0:
            continue
        sold_pct = 1.0 - (c.amount / peak) if c.amount > 0 else 1.0
        notional = c.amount * c.avg_entry
        last_sell = c.sells[-1].ts if c.sells else None
        idle_h = (as_of - last_sell).total_seconds() / 3600.0 if last_sell else 0.0

        gain_pct = _cycle_gain_pct(c)
        would_close = False
        tail_exempt = False
        reason = "hold"

        if spec.name == "A":
            tail_exempt = False
        elif spec.name in ("B", "C"):
            tail_exempt = False
        else:
            if spec.tail_exempt_sold_pct is not None and sold_pct >= spec.tail_exempt_sold_pct:
                tail_exempt = True
                reason = f"tail_exempt sold={sold_pct:.0%}"
            if spec.tail_exempt_notional_usdt is not None and 0 < notional < spec.tail_exempt_notional_usdt:
                tail_exempt = True
                reason = f"tail_exempt ${notional:.0f}"
            if (
                spec.tail_idle_hours
                and idle_h >= spec.tail_idle_hours
                and sold_pct >= 0.20
                and c.amount > 0
            ):
                if _can_rotation_evict(spec, gain_pct, c.realized_pnl):
                    would_close = True
                    reason = f"idle_close {idle_h:.0f}h"
                else:
                    reason = f"rotation_blocked loss={gain_pct:.1f}%"
            if spec.ladder_terminal_full_close and sold_pct >= 0.80 and c.amount > 0:
                if _can_rotation_evict(spec, gain_pct, c.realized_pnl):
                    would_close = True
                    reason = f"ladder_terminal sold={sold_pct:.0%}"
                elif reason == "hold":
                    reason = f"rotation_blocked loss={gain_pct:.1f}%"

        if would_close:
            out.would_close_now += 1
            if gain_pct < 0 and c.realized_pnl <= 0:
                out.would_close_losers += 1
        elif tail_exempt:
            out.tail_exempt += 1
        else:
            out.full_slots += 1

        out.details.append({
            "symbol": c.symbol,
            "timeframe": c.timeframe,
            "sold_pct": round(sold_pct, 3),
            "notional": round(notional, 2),
            "gain_pct": round(gain_pct, 2),
            "realized_pnl": round(c.realized_pnl, 2),
            "idle_hours": round(idle_h, 1),
            "would_close": would_close,
            "tail_exempt": tail_exempt,
            "reason": reason,
        })

    counted = out.would_close_now + out.tail_exempt + out.full_slots
    out.free_slots = max(0, max_open_slots - out.full_slots)
    out.full_slots = min(out.full_slots, counted)
    return out


def compute_recovery_eligibility(
    open_cycles: list[PositionCycle],
    *,
    recovery: dict[str, float] | None = None,
    as_of: datetime | None = None,
) -> RecoveryEligibility:
    """Count open minus-tails eligible for DCA-Recovery (Teil 1 gates)."""
    as_of = as_of or datetime.now()
    cfg = {**RECOVERY_DEFAULTS, **(recovery or {})}
    out = RecoveryEligibility()

    for c in open_cycles:
        if c.amount <= 1e-12:
            continue
        sold_pct = _cycle_sold_pct(c)
        notional = c.amount * c.avg_entry
        gain_pct = _cycle_gain_pct(c)

        if sold_pct <= 0 or gain_pct >= 0:
            continue
        out.minus_tails += 1

        block_reason = None
        if sold_pct >= cfg["max_sold_percent"]:
            block_reason = f"sold>={cfg['max_sold_percent']:.0%}"
        elif notional < cfg["min_remainder_usdt"]:
            block_reason = f"notional<{cfg['min_remainder_usdt']:.0f}"
        elif gain_pct < cfg["loss_pct_min"] or gain_pct > cfg["loss_pct_max"]:
            block_reason = f"loss_band [{cfg['loss_pct_min']},{cfg['loss_pct_max']}]"

        row = {
            "symbol": c.symbol,
            "timeframe": c.timeframe,
            "sold_pct": round(sold_pct, 3),
            "notional": round(notional, 2),
            "gain_pct": round(gain_pct, 2),
            "eligible": block_reason is None,
            "block_reason": block_reason,
        }
        out.details.append(row)
        if block_reason:
            out.blocked += 1
        else:
            out.eligible += 1

    return out


def compute_tail_slot_snapshot(
    open_cycles: list[PositionCycle],
    spec: PolicySpec,
    *,
    as_of: datetime | None = None,
    max_open_slots: int = 40,
) -> TailSlotSnapshot:
    """open_full_slots vs open_total per Teil 4.4 tail-slot model."""
    as_of = as_of or datetime.now()
    snap = TailSlotSnapshot(open_total=len(open_cycles))
    for c in open_cycles:
        if c.amount <= 1e-12:
            continue
        tail_cycle = PositionCycle(
            symbol=c.symbol,
            timeframe=c.timeframe,
            amount=c.amount,
            avg_entry=c.avg_entry,
            peak_amount=c.peak_amount,
            sells=c.sells,
        )
        if _is_tail(tail_cycle, spec, as_of=as_of) and spec.tail_exempt_sold_pct is not None:
            snap.open_tail_exempt += 1
        else:
            snap.open_full_slots += 1
    snap.free_buy_slots = max(0, max_open_slots - snap.open_full_slots)
    return snap


def validate_plan_gate1(
    report: dict[str, Any],
    *,
    max_open_slots: int = 40,
    min_free_delta: int = 8,
    min_recovery_eligible: int = 8,
    max_pnl_degradation_pct: float = 5.0,
) -> dict[str, Any]:
    """Gate 1 Go/No-Go for rotation-safe D′ vs baseline A."""
    fwd_a = report["forward_open"]["A"]
    fwd_dp = report["forward_open"]["D_prime"]
    recovery: RecoveryEligibility = report["recovery"]
    open_a = report["open_policies"]["A"]
    open_dp = report["open_policies"]["D_prime"]

    free_delta = fwd_dp.free_slots - fwd_a.free_slots
    loser_closes = fwd_dp.would_close_losers
    recovery_threshold = min(min_recovery_eligible, max(1, recovery.minus_tails))

    pnl_a = open_a.realized_pnl
    pnl_dp = open_dp.realized_pnl
    if abs(pnl_a) > 1e-6:
        pnl_delta_pct = ((pnl_dp - pnl_a) / abs(pnl_a)) * 100
        pnl_pass = pnl_delta_pct >= -max_pnl_degradation_pct
    else:
        pnl_delta_pct = 0.0 if pnl_dp == pnl_a else float("inf")
        pnl_pass = pnl_dp >= pnl_a

    gates = {
        "free_slots_delta_vs_a": {
            "value": free_delta,
            "threshold": min_free_delta,
            "pass": free_delta >= min_free_delta,
        },
        "no_loser_eviction": {
            "value": loser_closes,
            "threshold": 0,
            "pass": loser_closes == 0,
        },
        "recovery_eligible": {
            "value": recovery.eligible,
            "threshold": recovery_threshold,
            "pass": recovery.eligible >= recovery_threshold,
        },
        "pnl_vs_a": {
            "value": round(pnl_delta_pct, 2) if pnl_delta_pct != float("inf") else None,
            "threshold": -max_pnl_degradation_pct,
            "pass": pnl_pass,
            "pnl_a": round(pnl_a, 2),
            "pnl_d_prime": round(pnl_dp, 2),
        },
    }
    return {
        "go": all(g["pass"] for g in gates.values()),
        "gates": gates,
        "forward_d_prime": {
            "would_close_now": fwd_dp.would_close_now,
            "tail_exempt": fwd_dp.tail_exempt,
            "free_slots": fwd_dp.free_slots,
        },
        "forward_a": {"free_slots": fwd_a.free_slots},
    }


def compare_policies(
    orders: list[dict],
    *,
    decisions_path: Path | None = None,
    max_open_slots: int = 40,
    as_of: datetime | None = None,
    since: datetime | None = None,
) -> dict[str, Any]:
    hints = load_decision_sell_hints(decisions_path)
    all_cycles = build_cycles(orders, hints=hints)
    cycles = filter_cycles(all_cycles, since=since) if since else all_cycles
    open_now = open_cycles_now(all_cycles)
    baseline = compute_baseline(orders, all_cycles)
    results = {
        spec.name: simulate_policy(cycles, spec, as_of=as_of, max_open_slots=max_open_slots)
        for spec in POLICIES.values()
    }
    open_results = {
        spec.name: simulate_policy(open_now, spec, as_of=as_of, max_open_slots=max_open_slots)
        for spec in POLICIES.values()
    }
    forward = {
        spec.name: forward_open_analysis(open_now, spec, as_of=as_of, max_open_slots=max_open_slots)
        for spec in POLICIES.values()
    }
    recovery = compute_recovery_eligibility(open_now, as_of=as_of)
    tail_slots = compute_tail_slot_snapshot(
        open_now,
        POLICIES["D_prime"],
        as_of=as_of,
        max_open_slots=max_open_slots,
    )
    report = {
        "baseline": baseline,
        "cycles": all_cycles,
        "open_cycles": open_now,
        "policies": results,
        "open_policies": open_results,
        "forward_open": forward,
        "recovery": recovery,
        "tail_slots": tail_slots,
        "decision_hints_loaded": len(hints),
    }
    report["validation"] = validate_plan_gate1(report, max_open_slots=max_open_slots)
    return report


def _policy_table(results: dict[str, PolicyResult], *, max_open_slots: int, title: str) -> list[str]:
    lines = [title, "-" * 72]
    header = f"{'Policy':7s} {'Sells':>6s} {'Blk':>5s} {'Full':>5s} {'Tail':>5s} {'Close':>6s} {'Open':>5s} {'Tails':>6s} {'Free':>5s}"
    lines.append(header)
    lines.append("-" * len(header))
    for key in POLICY_ORDER:
        r = results[key]
        free = max(0, max_open_slots - int(r.effective_open_slots))
        label = "D′" if key == "D_prime" else r.policy
        lines.append(
            f"{label:7s} {r.executed_sells:6d} {r.blocked_sells:5d} "
            f"{r.full_close_conversions:5d} {r.tail_auto_closes:5d} "
            f"{r.cycles_closed:6d} {r.open_cycles:5d} {r.tail_cycles:6d} {free:5d}"
        )
    return lines


def _policy_display_name(key: str) -> str:
    return "D′" if key == "D_prime" else key


def format_report(report: dict[str, Any], *, max_open_slots: int = 40) -> str:
    b: BaselineStats = report["baseline"]
    open_count = len(report.get("open_cycles") or [])
    lines = [
        "=" * 72,
        "SELL ROTATION ANALYSIS — Demo Ledger",
        "=" * 72,
        f"Filled orders:      {b.filled_orders}  (buys {b.buys}, sells {b.sells})",
        f"Partial sell share: {b.partial_sell_share * 100:.1f}% of sell volume",
        f"SELL_PARTIAL_30 PnL: ${b.partial_30_pnl:,.0f}",
        f"Closed cycles:      {b.closed_cycles}",
        f"Open cycles now:    {open_count}",
        f"Zombie tails:       {b.zombie_tails}  (open, >=20% sold)",
        f"Decision hints:     {report.get('decision_hints_loaded', 0)}",
        "",
        "Sell categories (open cycles, inferred):",
    ]
    open_cats: Counter = Counter()
    for c in report.get("open_cycles") or []:
        for ev in c.sells:
            open_cats[ev.category] += 1
    if open_cats:
        for cat, n in open_cats.most_common():
            lines.append(f"  {cat:16s} {n:4d}")
    else:
        lines.append("  (none)")

    fwd_a = report["forward_open"]["A"]
    fwd_d = report["forward_open"]["D"]
    fwd_dp = report["forward_open"]["D_prime"]
    recovery: RecoveryEligibility = report["recovery"]
    tail_slots: TailSlotSnapshot = report["tail_slots"]
    validation = report.get("validation") or {}

    lines.extend([
        "",
        f"FORWARD OPEN ANALYSIS ({open_count} positions — apply policy to current state):",
        "-" * 72,
        f"  Policy A:   {fwd_a.full_slots} full slots, {fwd_a.free_slots} free",
        f"  Policy D:   {fwd_d.would_close_now} would close now, {fwd_d.tail_exempt} tail-exempt, "
        f"{fwd_d.full_slots} full slots → {fwd_d.free_slots} free",
        f"  Policy D′:  {fwd_dp.would_close_now} would close now ({fwd_dp.would_close_losers} losers), "
        f"{fwd_dp.tail_exempt} tail-exempt, {fwd_dp.full_slots} full slots → {fwd_dp.free_slots} free",
        "",
        "TAIL SLOT MODEL (D′ accounting):",
        f"  open_total={tail_slots.open_total}  full_slots={tail_slots.open_full_slots}  "
        f"tail_exempt={tail_slots.open_tail_exempt}  free_buy_slots={tail_slots.free_buy_slots}",
        "",
        "DCA-RECOVERY ELIGIBILITY (open minus-tails):",
        f"  minus_tails={recovery.minus_tails}  eligible={recovery.eligible}  blocked={recovery.blocked}",
    ])
    if recovery.details:
        for row in sorted(recovery.details, key=lambda r: r["gain_pct"])[:10]:
            flag = "ELIG" if row["eligible"] else "BLOCK"
            extra = "" if row["eligible"] else f" — {row['block_reason']}"
            lines.append(
                f"  [{flag}] {row['symbol']:14s} {row['timeframe']:3s} "
                f"sold={row['sold_pct']*100:4.0f}% gain={row['gain_pct']:6.1f}%{extra}"
            )
    lines.append("")
    lines.extend(_policy_table(
        report["open_policies"],
        max_open_slots=max_open_slots,
        title=f"CURRENT OPEN — historical replay on open lots (secondary)",
    ))
    lines.append("")
    lines.extend(_policy_table(
        report["policies"],
        max_open_slots=max_open_slots,
        title="ALL CYCLES IN WINDOW — historical replay (secondary)",
    ))
    lines.extend([
        "",
        "Legend:",
        "  Blk  = sells blocked (early partials below arm gain)",
        "  Full = partial converted to full close",
        "  Tail = idle tail auto-closed (policy D)",
        "  Free = max_open_positions - effective full slots (tails exempt in D)",
        "",
        "Policy labels:",
    ])
    for key in POLICY_ORDER:
        lines.append(f"  {_policy_display_name(key)}: {POLICIES[key].label}")
    lines.extend([
        "",
        "ROTATION DELTA (forward D′ vs A):",
        f"  Immediate full-closes: {fwd_dp.would_close_now} (losers blocked: {fwd_dp.would_close_losers})",
        f"  Tail-exempt slots:     {fwd_dp.tail_exempt}",
        f"  Free slots (D′):       {fwd_dp.free_slots} vs {fwd_a.free_slots} (A)",
        f"  Rotation headroom:     +{fwd_dp.free_slots - fwd_a.free_slots} slots",
        "",
        "GATE 1 VALIDATION:",
        f"  Result: {'GO' if validation.get('go') else 'NO-GO'}",
    ])
    for name, gate in (validation.get("gates") or {}).items():
        status = "PASS" if gate.get("pass") else "FAIL"
        lines.append(f"  [{status}] {name}: value={gate.get('value')} threshold={gate.get('threshold')}")

    if fwd_dp.details:
        lines.append("")
        lines.append("Policy D′ actions on open tails:")
        for row in sorted(fwd_dp.details, key=lambda r: (-r["sold_pct"], -r["idle_hours"]))[:12]:
            if row["would_close"] or row["tail_exempt"] or "rotation_blocked" in row["reason"]:
                if row["would_close"]:
                    flag = "CLOSE"
                elif row["tail_exempt"]:
                    flag = "EXEMPT"
                else:
                    flag = "BLOCK"
                lines.append(
                    f"  [{flag}] {row['symbol']:14s} {row['timeframe']:3s} "
                    f"sold={row['sold_pct']*100:4.0f}% gain={row['gain_pct']:5.1f}% "
                    f"idle={row['idle_hours']:4.0f}h — {row['reason']}"
                )
    return "\n".join(lines)