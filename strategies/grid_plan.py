"""Pure grid plan: levels + buy/sell slices (Phase A — no exchange limits)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from core.actions import BUY, HOLD, SELL_FULL, SELL_PARTIAL_20, SELL_PARTIAL_30, SELL_PARTIAL_50


@dataclass
class GridLevelPlan:
    index: int
    price: float
    side: str  # buy | sell
    slice_pct: float  # buy: fraction of base_usdt; sell: fraction of open position
    filled: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "GridLevelPlan":
        return cls(
            index=int(raw.get("index", 0)),
            price=float(raw.get("price", 0) or 0),
            side=str(raw.get("side") or "buy"),
            slice_pct=float(raw.get("slice_pct", 0.2) or 0.2),
            filled=bool(raw.get("filled", False)),
        )


@dataclass
class GridAction:
    action: str  # HOLD | BUY | SELL_PARTIAL_* | SELL_FULL | RECENTER
    level_index: int | None = None
    level_price: float = 0.0
    buy_usdt_frac: float = 0.0  # of max trade / budget
    sell_pos_frac: float = 0.0  # of open position
    rationale: str = ""
    re_center: bool = False


@dataclass
class GridPlan:
    symbol: str
    timeframe: str
    center: float
    spacing: float
    levels: list[GridLevelPlan] = field(default_factory=list)
    last_recenter_price: float = 0.0
    n_buy_levels: int = 6
    n_sell_levels: int = 6
    touch_eps: float = 0.001  # 0.1%

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "center": self.center,
            "spacing": self.spacing,
            "last_recenter_price": self.last_recenter_price,
            "n_buy_levels": self.n_buy_levels,
            "n_sell_levels": self.n_sell_levels,
            "touch_eps": self.touch_eps,
            "levels": [lv.to_dict() for lv in self.levels],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "GridPlan":
        levels = [GridLevelPlan.from_dict(x) for x in (raw.get("levels") or []) if isinstance(x, dict)]
        return cls(
            symbol=str(raw.get("symbol") or ""),
            timeframe=str(raw.get("timeframe") or "4h"),
            center=float(raw.get("center", 0) or 0),
            spacing=float(raw.get("spacing", 0) or 0),
            levels=levels,
            last_recenter_price=float(raw.get("last_recenter_price", raw.get("center", 0)) or 0),
            n_buy_levels=int(raw.get("n_buy_levels", 6) or 6),
            n_sell_levels=int(raw.get("n_sell_levels", 6) or 6),
            touch_eps=float(raw.get("touch_eps", 0.001) or 0.001),
        )


def _slice_pct_for_buy_level(i: int, n: int) -> float:
    """Deeper levels get slightly larger slices (DCA-style)."""
    if n <= 0:
        return 0.2
    # i=1 nearest → 0.15; deepest → ~0.28
    return round(0.12 + 0.03 * i, 3)


def _slice_pct_for_sell_level(i: int, n: int) -> float:
    """Near levels take smaller profits; outer levels larger."""
    if n <= 0:
        return 0.25
    return round(0.18 + 0.04 * i, 3)


def build_grid_plan(
    symbol: str,
    timeframe: str,
    center_price: float,
    *,
    atr_pct: float = 3.0,
    spacing_atr_mult: float = 0.8,
    n_buy_levels: int = 6,
    n_sell_levels: int = 6,
    touch_eps: float = 0.001,
) -> GridPlan:
    """Build a fresh plan around *center_price*."""
    center = float(center_price)
    atr = max(float(atr_pct), 0.1)
    spacing = max(center * (atr / 100.0) * float(spacing_atr_mult), center * 1e-6)
    n_buy = max(1, int(n_buy_levels))
    n_sell = max(1, int(n_sell_levels))
    levels: list[GridLevelPlan] = []
    for i in range(1, n_buy + 1):
        levels.append(
            GridLevelPlan(
                index=i,
                price=center - i * spacing,
                side="buy",
                slice_pct=_slice_pct_for_buy_level(i, n_buy),
            )
        )
    for i in range(1, n_sell + 1):
        levels.append(
            GridLevelPlan(
                index=i,
                price=center + i * spacing,
                side="sell",
                slice_pct=_slice_pct_for_sell_level(i, n_sell),
            )
        )
    return GridPlan(
        symbol=symbol,
        timeframe=timeframe,
        center=center,
        spacing=spacing,
        levels=levels,
        last_recenter_price=center,
        n_buy_levels=n_buy,
        n_sell_levels=n_sell,
        touch_eps=float(touch_eps),
    )


def should_recenter(
    plan: GridPlan,
    price: float,
    *,
    atr_pct: float = 3.0,
    re_center_atr_mult: float = 2.5,
) -> bool:
    if plan.center <= 0 or price <= 0:
        return False
    atr = max(float(atr_pct), 0.1)
    threshold = plan.center * (atr / 100.0) * float(re_center_atr_mult)
    return abs(float(price) - plan.center) > threshold


def recenter_plan(
    plan: GridPlan,
    price: float,
    *,
    atr_pct: float = 3.0,
    spacing_atr_mult: float = 0.8,
) -> GridPlan:
    return build_grid_plan(
        plan.symbol,
        plan.timeframe,
        price,
        atr_pct=atr_pct,
        spacing_atr_mult=spacing_atr_mult,
        n_buy_levels=plan.n_buy_levels,
        n_sell_levels=plan.n_sell_levels,
        touch_eps=plan.touch_eps,
    )


def _sell_action_for_frac(frac: float) -> str:
    f = float(frac)
    if f >= 0.9:
        return SELL_FULL
    if f >= 0.45:
        return SELL_PARTIAL_50
    if f >= 0.28:
        return SELL_PARTIAL_30
    return SELL_PARTIAL_20


def evaluate_plan_at_price(
    plan: GridPlan,
    price: float,
    *,
    has_position: bool = False,
    allow_buys: bool = True,
    allow_sells: bool = True,
) -> GridAction:
    """First unfilled level touched by *price* wins (buys preferred if both — rare)."""
    if price <= 0 or plan.spacing <= 0:
        return GridAction(action=HOLD, rationale="invalid price/plan")

    eps = max(float(plan.touch_eps), 1e-6)
    # Prefer buys when price is low (more rotation into inventory first)
    ordered = sorted(
        plan.levels,
        key=lambda lv: (0 if lv.side == "buy" else 1, abs(lv.price - price)),
    )
    for lv in ordered:
        if lv.filled:
            continue
        if lv.side == "buy" and allow_buys and price <= lv.price * (1.0 + eps):
            lv.filled = True
            return GridAction(
                action=BUY,
                level_index=lv.index,
                level_price=lv.price,
                buy_usdt_frac=max(0.05, min(0.5, float(lv.slice_pct))),
                rationale=f"Grid buy L{lv.index} @ {lv.price:.6g} (slice {lv.slice_pct:.0%})",
            )
        if lv.side == "sell" and allow_sells and has_position and price >= lv.price * (1.0 - eps):
            lv.filled = True
            frac = max(0.1, min(1.0, float(lv.slice_pct)))
            return GridAction(
                action=_sell_action_for_frac(frac),
                level_index=lv.index,
                level_price=lv.price,
                sell_pos_frac=frac,
                rationale=f"Grid sell L{lv.index} @ {lv.price:.6g} (slice {frac:.0%})",
            )
    return GridAction(action=HOLD, rationale="Grid monitoring")


def spacing_atr_mult_for_coin(
    *,
    volatility_tier: str = "",
    coin_class: str = "",
    base: float = 0.8,
) -> float:
    """Phase B: wider grid for volatile/meme, tighter for stable."""
    tier = (volatility_tier or "").lower()
    cls = (coin_class or "").lower()
    mult = float(base)
    if tier == "volatile" or cls in ("meme", "micro"):
        mult = max(mult, 1.1)
    elif tier == "stable" or cls in ("large", "bluechip"):
        mult = min(mult, 0.55)
    return mult


def plan_from_legacy_state(
    symbol: str,
    timeframe: str,
    state: dict[str, Any],
    *,
    default_slice: float = 0.2,
) -> GridPlan:
    """Import old GridState dict shape into a GridPlan."""
    levels_raw = state.get("levels") or []
    levels: list[GridLevelPlan] = []
    bi = si = 0
    for raw in levels_raw:
        if not isinstance(raw, dict):
            continue
        side = str(raw.get("side") or "buy")
        if side == "buy":
            bi += 1
            idx = bi
            sp = _slice_pct_for_buy_level(bi, 6)
        else:
            si += 1
            idx = si
            sp = _slice_pct_for_sell_level(si, 6)
        levels.append(
            GridLevelPlan(
                index=idx,
                price=float(raw.get("price", 0) or 0),
                side=side,
                slice_pct=float(raw.get("slice_pct", sp) or sp),
                filled=bool(raw.get("filled", False)),
            )
        )
    center = float(state.get("center_price", state.get("center", 0)) or 0)
    return GridPlan(
        symbol=symbol,
        timeframe=timeframe,
        center=center,
        spacing=float(state.get("spacing", 0) or 0),
        levels=levels,
        last_recenter_price=float(state.get("last_recenter_price", center) or center),
    )


def simulate_plan_path(
    prices: list[float],
    *,
    symbol: str = "SIM/USDT",
    timeframe: str = "4h",
    atr_pct: float = 3.0,
    spacing_atr_mult: float = 0.8,
    re_center_atr_mult: float = 2.5,
    initial_cash: float = 10_000.0,
    base_buy_usdt: float = 500.0,
    fee_pct: float = 0.1,
) -> dict[str, Any]:
    """Bar-walk plan evaluation for local backtests (no I/O)."""
    if not prices:
        return {"error": "no prices", "trades": 0}
    plan = build_grid_plan(
        symbol,
        timeframe,
        prices[0],
        atr_pct=atr_pct,
        spacing_atr_mult=spacing_atr_mult,
    )
    cash = float(initial_cash)
    amount = 0.0
    entry_avg = 0.0
    trades: list[dict] = []
    fee_m = fee_pct / 100.0

    for i, px in enumerate(prices):
        px = float(px)
        if should_recenter(plan, px, atr_pct=atr_pct, re_center_atr_mult=re_center_atr_mult):
            plan = recenter_plan(plan, px, atr_pct=atr_pct, spacing_atr_mult=spacing_atr_mult)
            trades.append({"i": i, "price": px, "action": "RECENTER"})

        act = evaluate_plan_at_price(
            plan, px, has_position=amount > 1e-12, allow_buys=True, allow_sells=True,
        )
        if act.action == BUY and act.buy_usdt_frac > 0:
            usdt = min(cash, base_buy_usdt * act.buy_usdt_frac)
            if usdt >= 10 and px > 0:
                fee = usdt * fee_m
                got = (usdt - fee) / px
                new_amt = amount + got
                entry_avg = ((entry_avg * amount) + px * got) / new_amt if new_amt > 0 else px
                amount = new_amt
                cash -= usdt
                trades.append(
                    {
                        "i": i,
                        "price": px,
                        "action": "BUY",
                        "usdt": round(usdt, 2),
                        "level": act.level_index,
                    }
                )
        elif act.action != HOLD and act.sell_pos_frac > 0 and amount > 0:
            sell_amt = amount * act.sell_pos_frac
            proceeds = sell_amt * px * (1.0 - fee_m)
            cash += proceeds
            amount -= sell_amt
            trades.append(
                {
                    "i": i,
                    "price": px,
                    "action": act.action,
                    "usdt": round(proceeds, 2),
                    "level": act.level_index,
                }
            )

    final_px = float(prices[-1])
    equity = cash + amount * final_px
    buy_hold = float(initial_cash) / prices[0] * final_px if prices[0] > 0 else initial_cash
    return {
        "initial_cash": initial_cash,
        "final_cash": round(cash, 2),
        "final_amount": amount,
        "final_equity": round(equity, 2),
        "buy_hold_equity": round(buy_hold, 2),
        "vs_buy_hold_pct": round((equity / buy_hold - 1.0) * 100.0, 2) if buy_hold > 0 else 0.0,
        "trades": len([t for t in trades if t["action"] not in ("RECENTER",)]),
        "recenters": len([t for t in trades if t["action"] == "RECENTER"]),
        "trade_log": trades,
        "plan": plan.to_dict(),
    }
