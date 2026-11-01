"""Lot-level replay engine.

The recorded tape decides *what was bought and when*. A ``Policy`` decides
what the bot's own exit and DCA code would have done with those lots on
hourly bars:

* hard stop (full and partial) through ``evaluate_long_hard_stop`` of the
  loaded ``strategies.dca`` module, on the bar low (the WS exit path sees
  ticks, so the low is the price it could have seen first);
* partial size via ``strategies.positions.sell_fraction_for_signal``;
* the ``partial_sell_guard`` size check with the tenant's limits, and the
  #738 escalation of a rejected marked hard partial to one full sell;
* DCA adds either replayed from the tape (``dca_mode="tape"``, the real DCA
  code's recorded output, scaled with the entry size) or decided bar by bar
  by the real ``should_dca`` (``dca_mode="rule"``);
* owner locks (``no_auto_sell``) block stop sells from ``locked_at`` on;
* every other tape sell (trail, take-profit, rotation, manual) is applied as
  recorded: the same fraction of what the simulated lot still holds.

Fills pay taker fee plus class slippage (``costs.CostModel``). Open lots are
marked at the last close net of exit costs, so a policy that sold and one
that holds are compared on the same footing.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Callable, Iterable

from .costs import CostModel
from .data import BookLot, Candle, Fill, TenantParams, to_naive_berlin_iso
from .policy import SimClock

UTC = timezone.utc
HOUR = 3600
FLAT_USDT = 1.0


@dataclass
class Episode:
    """One tape lot: first buy while flat, through adds and sells, until flat."""

    tenant: str
    symbol: str
    timeframe: str
    entry: Fill
    events: list[Fill]  # all fills incl. the entry, time-ordered
    closed_at: datetime | None  # tape went flat; None = still open at tape end

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.tenant, self.symbol, self.timeframe)


def build_episodes(fills: Iterable[Fill]) -> list[Episode]:
    by_lot: dict[tuple[str, str, str], list[Fill]] = {}
    for f in fills:
        by_lot.setdefault(f.lot_key, []).append(f)
    episodes: list[Episode] = []
    for key, rows in by_lot.items():
        amount = 0.0
        current: list[Fill] = []
        for f in rows:
            if f.side == "buy":
                if not current:
                    if f.is_add:
                        # an add with no open tape lot: the opening buy predates the
                        # export. It still opens an episode so its money is counted.
                        pass
                current.append(f)
                amount += f.amount
            else:
                if not current:
                    continue  # sell of a lot opened before the export
                current.append(f)
                amount = max(0.0, amount - f.amount)
                if amount * f.price < FLAT_USDT:
                    episodes.append(Episode(*key, entry=current[0], events=current, closed_at=f.ts))
                    current, amount = [], 0.0
        if current:
            episodes.append(Episode(*key, entry=current[0], events=current, closed_at=None))
    episodes.sort(key=lambda e: (e.entry.ts, e.key))
    return episodes


@dataclass
class Policy:
    name: str
    dca: ModuleType  # loaded strategies.dca (on the sim clock)
    clock: SimClock
    escalate_rejected_hard_partial: bool = True  # #738
    dca_mode: str = "tape"  # tape | rule
    respect_locks: bool = True
    stops_enabled: bool = True
    entry_filter: Callable[[Episode], bool | None] | None = None
    size_multiplier: Callable[[Episode], float] | None = None
    description: str = ""


@dataclass
class LotResult:
    key: tuple[str, str, str]
    entry_ts: datetime
    window: str
    invested: float  # USDT spent (buys incl. fees) inside the run, plus start mark
    proceeds: float  # USDT received (sells net of fees) + end liquidation value
    pnl: float
    ret: float
    open_at_end: bool
    end_unreal_pct: float | None  # mark vs average_entry for an open lot
    end_cost_basis: float
    stop_full: int = 0
    stop_partial: int = 0
    escalated: int = 0
    partial_rejects: int = 0
    dca_adds: int = 0
    locked_blocks: int = 0
    min_unreal_pct: float = 0.0
    tape_sells_applied: int = 0


@dataclass
class RunResult:
    policy: str
    lots: list[LotResult]
    pnl_curve: dict[int, float]  # hour ts -> cumulative PnL change at that hour (delta)
    capital_curve: dict[int, float]  # hour ts -> delta of cost basis deployed
    skipped_no_candles: int = 0
    filtered_out: int = 0
    notes: list[str] = field(default_factory=list)


def _bar_index(candles: list[Candle], ts: int) -> int:
    return bisect_left(candles, (ts,))


class _Indicators:
    """Rolling RSI(14), Bollinger(20,2), ATR(14)% from closed 1h bars, for rule DCA."""

    def __init__(self, candles: list[Candle]):
        self.c = candles

    def at(self, i: int) -> dict:
        if i < 21:
            return {}
        closes = [b[4] for b in self.c[max(0, i - 60) : i + 1]]
        gains = losses = 0.0
        for a, b in zip(closes[-15:-1], closes[-14:]):
            d = b - a
            gains += max(d, 0.0)
            losses += max(-d, 0.0)
        rsi = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
        window = closes[-20:]
        mid = sum(window) / 20.0
        sd = math.sqrt(sum((x - mid) ** 2 for x in window) / 20.0)
        trs = []
        for j in range(i - 13, i + 1):
            h, low, pc = self.c[j][2], self.c[j][3], self.c[j - 1][4]
            trs.append(max(h - low, abs(h - pc), abs(low - pc)))
        atr_pct = (sum(trs) / 14.0) / closes[-1] * 100.0 if closes[-1] > 0 else 0.0
        return {"rsi": rsi, "lower_bb": mid - 2 * sd, "middle_bb": mid, "upper_bb": mid + 2 * sd, "atr_pct": atr_pct}


def _position_doc(ep: Episode, tier: str) -> dict:
    return {
        "symbol": ep.symbol,
        "tenant_id": ep.tenant,
        "strategy_tier": tier,
        "amount": 0.0,
        "peak_amount": 0.0,
        "average_entry": 0.0,
        "sold_percent": 0.0,
        "dca_rounds": 0,
        "dca_recovery_rounds": 0,
        "last_dca_at": None,
        "last_dca_recovery_at": None,
        "hard_partial_stop_done": False,
    }


def replay_state_until(ep: Episode, until: datetime, tier: str) -> dict:
    """The tape's own position doc for this lot just before ``until``."""
    pos = _position_doc(ep, tier)
    for f in ep.events:
        if f.ts >= until:
            break
        _apply_buy_or_sell_doc(pos, f)
    return pos


def _apply_buy_or_sell_doc(pos: dict, f: Fill) -> None:
    if f.side == "buy":
        amt = pos["amount"] + f.amount
        pos["average_entry"] = (pos["average_entry"] * pos["amount"] + f.price * f.amount) / amt if amt > 0 else 0.0
        pos["amount"] = amt
        pos["peak_amount"] = max(pos["peak_amount"], amt)
        if f.is_add:
            pos["dca_rounds"] = int(pos["dca_rounds"]) + 1
            pos["last_dca_at"] = to_naive_berlin_iso(f.ts)
    else:
        pos["amount"] = max(0.0, pos["amount"] - f.amount)
        if pos["peak_amount"] > 0:
            pos["sold_percent"] = 1.0 - pos["amount"] / pos["peak_amount"]
        if f.signal == "SELL_STOP_PARTIAL":
            pos["hard_partial_stop_done"] = True


class Engine:
    def __init__(
        self,
        *,
        candles_1h: Callable[[str], list[Candle]],
        tenants: dict[str, TenantParams],
        book: dict[tuple[str, str, str], BookLot],
        positions_mod: ModuleType,
        costs: CostModel | None = None,
        default_tier: str = "volatile",
        window_of: Callable[[datetime], str] | None = None,
    ):
        self.candles_1h = candles_1h
        self.tenants = tenants
        self.book = book
        self.positions_mod = positions_mod
        self.costs = costs or CostModel()
        self.default_tier = default_tier
        self.window_of = window_of or (lambda _ts: "all")
        self._tier_by_symbol: dict[tuple[str, str], str] = {}
        for (tenant, symbol, _tf), lot in book.items():
            if lot.tier:
                self._tier_by_symbol.setdefault((tenant, symbol), lot.tier)

    # ------------------------------------------------------------------
    def tier_of(self, ep: Episode) -> str:
        lot = self.book.get(ep.key)
        if lot and lot.tier:
            return lot.tier
        return self._tier_by_symbol.get((ep.tenant, ep.symbol), self.default_tier)

    def params_of(self, ep: Episode) -> tuple[dict, dict]:
        tp = self.tenants.get(ep.tenant) or self.tenants["default"]
        return tp.strategy_params(self.tier_of(ep)), tp.partial_limits()

    # ------------------------------------------------------------------
    def run(
        self,
        policy: Policy,
        episodes: list[Episode],
        start: datetime,
        end: datetime,
        *,
        carry_in: bool,
        horizon_end: datetime | None = None,
    ) -> RunResult:
        """Simulate ``episodes`` under ``policy``.

        ``carry_in=True`` (book mode): lots open at ``start`` begin from the
        tape's state at ``start``, marked at the first bar's open, and the run
        stops at ``end``. ``carry_in=False`` (entry mode): only lots entered in
        [start, end) run, each until ``horizon_end`` (default ``end``).
        """
        horizon = horizon_end or end
        res = RunResult(policy.name, [], {}, {})
        for ep in episodes:
            if carry_in:
                active = ep.entry.ts < end and (ep.closed_at is None or ep.closed_at > start)
            else:
                active = start <= ep.entry.ts < end
            if not active:
                continue
            if policy.entry_filter is not None and ep.entry.ts >= start:
                verdict = policy.entry_filter(ep)
                if verdict is False:
                    res.filtered_out += 1
                    continue
            candles = self.candles_1h(ep.symbol)
            if not candles:
                res.skipped_no_candles += 1
                continue
            lot = self._simulate(policy, ep, candles, start, end if carry_in else horizon, carry_in, res)
            if lot is None:
                res.skipped_no_candles += 1
            else:
                res.lots.append(lot)
        return res

    # ------------------------------------------------------------------
    def _simulate(
        self,
        policy: Policy,
        ep: Episode,
        candles: list[Candle],
        start: datetime,
        stop_at: datetime,
        carry_in: bool,
        res: RunResult,
    ) -> LotResult | None:
        tier = self.tier_of(ep)
        params, limits = self.params_of(ep)
        slip = self.costs.slippage_pct(ep.symbol, tier)
        fee_pct = self.costs.fee_pct / 100.0
        mult = float(policy.size_multiplier(ep)) if policy.size_multiplier and ep.entry.ts >= start else 1.0
        dca = policy.dca
        clock = policy.clock
        sell_frac_partial = float(self.positions_mod.sell_fraction_for_signal("SELL_STOP_PARTIAL"))
        base_stop = float(params.get("stop_loss_pct") or 50.0)
        partial_bound = params.get("partial_stop_pct")
        if partial_bound is None:
            partial_bound = base_stop * float(params.get("partial_stop_ratio", 0.67))
        bound = min(float(partial_bound), base_stop)
        lock = self.book.get(ep.key)
        lock_since = lock.lock_since if (lock and policy.respect_locks and "no_auto_sell" in lock.lock_modes) else None
        indicators = _Indicators(candles) if policy.dca_mode == "rule" else None

        begin = max(start, ep.entry.ts) if carry_in else ep.entry.ts
        first_bar_ts = (int(begin.timestamp()) // HOUR) * HOUR
        if not carry_in or ep.entry.ts >= start:
            first_bar_ts += HOUR  # fill on the next bar's open after the signal
        i = _bar_index(candles, first_bar_ts)
        end_ts = int(stop_at.timestamp())
        if i >= len(candles) or candles[i][0] >= end_ts:
            return None

        lot = LotResult(ep.key, ep.entry.ts, self.window_of(ep.entry.ts), 0.0, 0.0, 0.0, 0.0, False, None, 0.0)
        pos = _position_doc(ep, tier)
        cost_basis = 0.0  # cash in the open amount (incl. buy fees)
        prev_pnl = 0.0
        prev_cb = 0.0
        tape_amount = 0.0
        pending = list(ep.events)

        if carry_in and ep.entry.ts < start:
            state = replay_state_until(ep, start, tier)
            pos.update(state)
            tape_amount = state["amount"]
            pending = [f for f in ep.events if f.ts >= start]
            mark = candles[i][1]
            if pos["amount"] * mark < FLAT_USDT:
                return None
            cost_basis = pos["amount"] * mark  # start value: window PnL is from here
            lot.invested += cost_basis
            prev_cb = cost_basis
            res.capital_curve[candles[i][0]] = res.capital_curve.get(candles[i][0], 0.0) + cost_basis
        else:
            # entry: the opening buy, on the next bar open
            usdt = ep.entry.usdt * mult
            px = self.costs.buy_price(candles[i][1], slip)
            amt = usdt / px
            fee = usdt * fee_pct
            pos["amount"] = amt
            pos["peak_amount"] = amt
            pos["average_entry"] = px
            cost_basis = usdt + fee
            lot.invested += usdt + fee
            tape_amount = ep.entry.amount
            pending = ep.events[1:]
            if ep.entry.is_add:
                pos["dca_rounds"] = 1
                pos["last_dca_at"] = to_naive_berlin_iso(ep.entry.ts)

        closed = False
        last_close = candles[i][1]
        last_bar_ts = candles[i][0]
        while i < len(candles) and candles[i][0] < end_ts and not closed:
            bar_ts, o, h, low, c, _v = candles[i]
            bar_end = bar_ts + HOUR
            # 1) recorded events inside this bar
            while pending and int(pending[0].ts.timestamp()) < bar_end:
                f = pending.pop(0)
                if int(f.ts.timestamp()) < bar_ts and not carry_in:
                    pass
                before_tape = tape_amount
                if f.side == "buy":
                    tape_amount += f.amount
                    if f.is_add and policy.dca_mode == "rule":
                        continue  # rule mode decides adds itself
                    usdt = f.usdt * mult
                    px = self.costs.buy_price(f.price, slip)
                    amt = usdt / px
                    fee = usdt * fee_pct
                    new_amt = pos["amount"] + amt
                    pos["average_entry"] = (pos["average_entry"] * pos["amount"] + px * amt) / new_amt
                    pos["amount"] = new_amt
                    pos["peak_amount"] = max(pos["peak_amount"], new_amt)
                    cost_basis += usdt + fee
                    lot.invested += usdt + fee
                    if f.is_add:
                        pos["dca_rounds"] = int(pos["dca_rounds"]) + 1
                        pos["last_dca_at"] = to_naive_berlin_iso(f.ts)
                        lot.dca_adds += 1
                else:
                    tape_amount = max(0.0, tape_amount - f.amount)
                    is_stop = f.exit_source == "stop_loss" or f.signal.startswith("SELL_STOP")
                    if is_stop and policy.stops_enabled:
                        continue  # stops are re-decided by the policy
                    frac = 1.0 if before_tape <= 0 else min(1.0, f.amount / before_tape)
                    if tape_amount * f.price < FLAT_USDT:
                        frac = 1.0
                    closed = self._sell(pos, lot, frac, self.costs.sell_price(f.price, slip), fee_pct)
                    cost_basis *= (1.0 - frac)
                    lot.tape_sells_applied += 1
                    if closed:
                        break
            if closed:
                last_close = c
                last_bar_ts = bar_ts
                break
            clock.set(datetime.fromtimestamp(bar_end, UTC))
            locked = lock_since is not None and bar_end >= int(lock_since.timestamp())
            avg = pos["average_entry"]
            # 2) hard stop on the bar low
            if policy.stops_enabled and avg > 0 and pos["amount"] > 0:
                loss_at_low = (1.0 - low / avg) * 100.0
                lot.min_unreal_pct = min(lot.min_unreal_pct, -loss_at_low)
                if loss_at_low > bound:
                    hit = dca.evaluate_long_hard_stop(
                        price=low, entry=avg, position=pos, strategy_params=params, base_stop_loss_pct=base_stop
                    )
                    if hit is not None and locked:
                        lot.locked_blocks += 1
                        hit = None
                    if hit is not None:
                        action = hit[0]
                        full_t, part_t, _grace = dca.effective_stop_loss_thresholds(pos, params, base_stop)
                        if action == "SELL_STOP_FULL":
                            level = avg * (1.0 - float(full_t) / 100.0)
                            mid = min(o, max(level, low))
                            closed = self._sell(pos, lot, 1.0, self.costs.sell_price(mid, slip), fee_pct)
                            cost_basis = 0.0
                            lot.stop_full += 1
                        else:
                            level = avg * (1.0 - float(part_t if part_t is not None else partial_bound) / 100.0)
                            mid = min(o, max(level, low))
                            code = self._partial_guard(pos, sell_frac_partial, mid, limits)
                            if code is None:
                                closed = self._sell(pos, lot, sell_frac_partial, self.costs.sell_price(mid, slip), fee_pct)
                                cost_basis *= (1.0 - sell_frac_partial)
                                pos["hard_partial_stop_done"] = True
                                lot.stop_partial += 1
                            elif (
                                code in ("min_position", "min_notional", "sold_cap_unconfigured")
                                and policy.escalate_rejected_hard_partial
                                and mid < avg
                            ):
                                closed = self._sell(pos, lot, 1.0, self.costs.sell_price(mid, slip), fee_pct)
                                cost_basis = 0.0
                                lot.escalated += 1
                            else:
                                lot.partial_rejects += 1
            # 3) rule-mode DCA at the bar close
            if not closed and indicators is not None and pos["amount"] > 0 and not locked:
                ind = indicators.at(i)
                if ind:
                    from core.models import MarketContext

                    market = MarketContext(
                        symbol=ep.symbol,
                        timeframe=ep.timeframe,
                        current_price=c,
                        has_position=True,
                        average_entry=pos["average_entry"],
                        strategy_params=params,
                        **ind,
                    )
                    decision = dca.should_dca(market, pos, params)
                    if decision.should_dca and not decision.shadow_only and decision.usdt_amount > 0:
                        usdt = float(decision.usdt_amount) * mult
                        px = self.costs.buy_price(c, slip)
                        amt = usdt / px
                        new_amt = pos["amount"] + amt
                        pos["average_entry"] = (pos["average_entry"] * pos["amount"] + px * amt) / new_amt
                        pos["amount"] = new_amt
                        pos["peak_amount"] = max(pos["peak_amount"], new_amt)
                        cost_basis += usdt * (1 + fee_pct)
                        lot.invested += usdt * (1 + fee_pct)
                        pos["dca_rounds"] = int(pos["dca_rounds"]) + 1
                        pos["last_dca_at"] = to_naive_berlin_iso(datetime.fromtimestamp(bar_end, UTC))
                        lot.dca_adds += 1
            last_close = c
            last_bar_ts = bar_ts
            # 4) curves
            value = pos["amount"] * self.costs.sell_price(c, slip) * (1 - fee_pct)
            pnl_now = lot.proceeds + value - lot.invested
            res.pnl_curve[bar_ts] = res.pnl_curve.get(bar_ts, 0.0) + (pnl_now - prev_pnl)
            res.capital_curve[bar_ts] = res.capital_curve.get(bar_ts, 0.0) + (cost_basis - prev_cb)
            prev_pnl, prev_cb = pnl_now, cost_basis
            i += 1

        # end mark
        end_value = pos["amount"] * self.costs.sell_price(last_close, slip) * (1 - fee_pct)
        lot.open_at_end = pos["amount"] * last_close >= FLAT_USDT
        if lot.open_at_end and pos["average_entry"] > 0:
            lot.end_unreal_pct = (last_close / pos["average_entry"] - 1.0) * 100.0
            lot.end_cost_basis = pos["amount"] * pos["average_entry"]  # true entry cost, not the window mark
        lot.proceeds += end_value
        lot.pnl = lot.proceeds - lot.invested
        lot.ret = lot.pnl / lot.invested if lot.invested > 0 else 0.0
        final_ts = last_bar_ts
        res.pnl_curve[final_ts] = res.pnl_curve.get(final_ts, 0.0) + (lot.pnl - prev_pnl)
        res.capital_curve[final_ts] = res.capital_curve.get(final_ts, 0.0) - prev_cb
        return lot

    # ------------------------------------------------------------------
    @staticmethod
    def _partial_guard(pos: dict, frac: float, price: float, limits: dict) -> str | None:
        """Same order as ``RiskManager._partial_sell_blocked`` (cap, position, notional)."""
        cap = limits.get("max_sold_percent")
        if cap is None:
            return "sold_cap_unconfigured"
        if float(pos.get("sold_percent") or 0) >= float(cap):
            return "sold_cap"
        if pos["amount"] * price < limits["min_position_usdt"]:
            return "min_position"
        if pos["amount"] * frac * price < limits["min_notional_usdt"]:
            return "min_notional"
        return None

    @staticmethod
    def _sell(pos: dict, lot: LotResult, frac: float, px: float, fee_pct: float) -> bool:
        amt = pos["amount"] * frac
        gross = amt * px
        lot.proceeds += gross * (1 - fee_pct)
        pos["amount"] -= amt
        if pos["peak_amount"] > 0:
            pos["sold_percent"] = 1.0 - pos["amount"] / pos["peak_amount"]
        if frac >= 1.0 or pos["amount"] * px < FLAT_USDT:
            pos["amount"] = 0.0
            return True
        return False


def window_namer(windows: list[tuple[str, datetime, datetime]]) -> Callable[[datetime], str]:
    def name(ts: datetime) -> str:
        for label, a, b in windows:
            if a <= ts < b:
                return label
        return "out"

    return name


def hours(a: datetime, b: datetime) -> Iterable[int]:
    t = (int(a.timestamp()) // HOUR) * HOUR
    while t < int(b.timestamp()):
        yield t
        t += HOUR


def day_floor(ts: int) -> int:
    return ts - ts % 86400


__all__ = [
    "Engine",
    "Episode",
    "LotResult",
    "Policy",
    "RunResult",
    "build_episodes",
    "replay_state_until",
    "window_namer",
    "timedelta",
]
