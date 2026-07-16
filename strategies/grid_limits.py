"""Phase C: limit-order specs + shadow book (local / dry-run first).

Live Gate multi-limit placement is gated by ``grid.use_limit_orders`` and is
only wired when a real executor is injected. Default path remains market slices
via GridStrategy + TradingService.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from strategies.grid_plan import GridPlan, GridLevelPlan


@dataclass
class LimitOrderSpec:
    symbol: str
    side: str  # buy | sell
    price: float
    usdt: float = 0.0
    amount: float = 0.0
    client_id: str = ""
    level_index: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class GridLimitExecutor(Protocol):
    def sync_plan(self, plan: GridPlan) -> list[LimitOrderSpec]:
        """Ensure open limits match plan; return desired specs."""
        ...

    def on_fill(self, client_id: str, fill_price: float, fill_amount: float) -> None:
        ...


def limit_orders_enabled(config_raw: dict | None) -> bool:
    grid = (config_raw or {}).get("grid") or {}
    return bool(grid.get("use_limit_orders", False)) and bool(grid.get("enabled", True))


def fee_aware_min_spacing(
    price: float,
    *,
    fee_pct: float = 0.1,
    safety_mult: float = 3.0,
) -> float:
    """Minimum level distance so a round-trip can clear fees."""
    px = max(float(price), 1e-12)
    # buy+sell fees ≈ 2 * fee; require safety_mult × that as price gap fraction
    gap_frac = (2.0 * float(fee_pct) / 100.0) * float(safety_mult)
    return px * max(gap_frac, 1e-6)


def enforce_fee_spacing(plan: GridPlan, *, fee_pct: float = 0.1) -> GridPlan:
    """Widen plan.spacing if levels would be too tight vs fees; rebuild levels."""
    from strategies.grid_plan import build_grid_plan

    min_sp = fee_aware_min_spacing(plan.center, fee_pct=fee_pct)
    if plan.spacing >= min_sp:
        return plan
    # Derive atr_pct equivalent: spacing = center * (atr/100) * mult → reverse not needed
    # Rebuild with spacing forced via atr so spacing matches min_sp
    # spacing = center * atr/100 * mult; choose atr so spacing = min_sp with mult=1
    atr_pct = (min_sp / max(plan.center, 1e-12)) * 100.0
    return build_grid_plan(
        plan.symbol,
        plan.timeframe,
        plan.center,
        atr_pct=max(atr_pct, 0.05),
        spacing_atr_mult=1.0,
        n_buy_levels=plan.n_buy_levels,
        n_sell_levels=plan.n_sell_levels,
        touch_eps=plan.touch_eps,
    )


def plan_to_limit_specs(
    plan: GridPlan,
    *,
    has_position: bool = False,
    position_amount: float = 0.0,
    base_buy_usdt: float = 500.0,
    only_unfilled: bool = True,
) -> list[LimitOrderSpec]:
    """Convert open grid levels to limit order specs (no exchange I/O)."""
    specs: list[LimitOrderSpec] = []
    for lv in plan.levels:
        if only_unfilled and lv.filled:
            continue
        cid = f"grid:{plan.symbol}:{plan.timeframe}:{lv.side}:L{lv.index}"
        if lv.side == "buy":
            usdt = max(10.0, float(base_buy_usdt) * float(lv.slice_pct))
            specs.append(
                LimitOrderSpec(
                    symbol=plan.symbol,
                    side="buy",
                    price=float(lv.price),
                    usdt=usdt,
                    client_id=cid,
                    level_index=lv.index,
                )
            )
        elif lv.side == "sell" and has_position and position_amount > 0:
            amt = float(position_amount) * float(lv.slice_pct)
            if amt <= 0:
                continue
            specs.append(
                LimitOrderSpec(
                    symbol=plan.symbol,
                    side="sell",
                    price=float(lv.price),
                    amount=amt,
                    client_id=cid,
                    level_index=lv.index,
                )
            )
    return specs


@dataclass
class ShadowFill:
    client_id: str
    side: str
    price: float
    amount: float
    usdt: float
    bar_index: int = 0


@dataclass
class GridLimitShadowBook:
    """In-memory limit book for local backtests (Phase C without Gate)."""

    open_orders: dict[str, LimitOrderSpec] = field(default_factory=dict)
    fills: list[ShadowFill] = field(default_factory=list)

    def sync(self, specs: list[LimitOrderSpec]) -> None:
        """Replace open book with new specs (re-center = cancel/replace)."""
        self.open_orders = {s.client_id: s for s in specs}

    def cancel_all(self) -> None:
        self.open_orders.clear()

    def match_bar(self, price: float, bar_index: int = 0) -> list[ShadowFill]:
        """Fill limits crossed by *price* (simple touch model)."""
        done: list[ShadowFill] = []
        for cid, spec in list(self.open_orders.items()):
            filled = False
            amount = 0.0
            usdt = 0.0
            if spec.side == "buy" and price <= spec.price * 1.0005:
                usdt = float(spec.usdt or 0)
                amount = usdt / max(spec.price, 1e-12)
                filled = usdt > 0
            elif spec.side == "sell" and price >= spec.price * 0.9995:
                amount = float(spec.amount or 0)
                usdt = amount * spec.price
                filled = amount > 0
            if not filled:
                continue
            fill = ShadowFill(
                client_id=cid,
                side=spec.side,
                price=spec.price,
                amount=amount,
                usdt=usdt,
                bar_index=bar_index,
            )
            done.append(fill)
            self.fills.append(fill)
            del self.open_orders[cid]
            # Mark plan level filled if client_id encodes L{n}
            _ = cid
        return done


def mark_plan_level_filled(plan: GridPlan, client_id: str) -> bool:
    """Set filled=True on the level referenced by a grid client_id."""
    # grid:SYM:tf:side:L{n}
    if ":L" not in client_id:
        return False
    try:
        side_part, lpart = client_id.rsplit(":L", 1)
        side = side_part.rsplit(":", 1)[-1]
        idx = int(lpart)
    except Exception:
        return False
    for lv in plan.levels:
        if lv.side == side and lv.index == idx:
            lv.filled = True
            return True
    return False


def simulate_limit_grid_path(
    prices: list[float],
    *,
    symbol: str = "SIM/USDT",
    timeframe: str = "4h",
    atr_pct: float = 3.0,
    spacing_atr_mult: float = 0.8,
    fee_pct: float = 0.1,
    initial_cash: float = 10_000.0,
    base_buy_usdt: float = 500.0,
    re_center_atr_mult: float = 2.5,
) -> dict[str, Any]:
    """Bar walk with shadow limit book (Phase C local)."""
    from strategies.grid_plan import (
        build_grid_plan,
        recenter_plan,
        should_recenter,
    )

    if not prices:
        return {"error": "no prices"}
    plan = build_grid_plan(
        symbol, timeframe, prices[0],
        atr_pct=atr_pct, spacing_atr_mult=spacing_atr_mult,
    )
    plan = enforce_fee_spacing(plan, fee_pct=fee_pct)
    book = GridLimitShadowBook()
    cash = float(initial_cash)
    amount = 0.0
    fee_m = fee_pct / 100.0
    trades = 0

    def _resync():
        specs = plan_to_limit_specs(
            plan,
            has_position=amount > 1e-12,
            position_amount=amount,
            base_buy_usdt=base_buy_usdt,
        )
        book.sync(specs)

    _resync()
    recenters = 0
    for i, px in enumerate(prices):
        px = float(px)
        if should_recenter(plan, px, atr_pct=atr_pct, re_center_atr_mult=re_center_atr_mult):
            book.cancel_all()
            plan = recenter_plan(plan, px, atr_pct=atr_pct, spacing_atr_mult=spacing_atr_mult)
            plan = enforce_fee_spacing(plan, fee_pct=fee_pct)
            recenters += 1
            _resync()
        for fill in book.match_bar(px, bar_index=i):
            mark_plan_level_filled(plan, fill.client_id)
            if fill.side == "buy" and cash >= fill.usdt:
                fee = fill.usdt * fee_m
                got = (fill.usdt - fee) / max(fill.price, 1e-12)
                amount += got
                cash -= fill.usdt
                trades += 1
            elif fill.side == "sell" and amount > 0:
                sell_amt = min(amount, fill.amount)
                cash += sell_amt * fill.price * (1.0 - fee_m)
                amount -= sell_amt
                trades += 1
            _resync()

    final_px = float(prices[-1])
    equity = cash + amount * final_px
    buy_hold = initial_cash / prices[0] * final_px if prices[0] > 0 else initial_cash
    return {
        "initial_cash": initial_cash,
        "final_cash": round(cash, 2),
        "final_equity": round(equity, 2),
        "buy_hold_equity": round(buy_hold, 2),
        "vs_buy_hold_pct": round((equity / buy_hold - 1.0) * 100.0, 2) if buy_hold else 0.0,
        "trades": trades,
        "recenters": recenters,
        "open_limits": len(book.open_orders),
        "fills": len(book.fills),
        "mode": "limit_shadow",
    }
