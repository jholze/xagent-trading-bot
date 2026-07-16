"""
GridStrategy – slice-based grid for spot (Phase A).

Activated via StrategyAllocator / force strategy_class=grid.
Uses pure GridPlan for levels + partial buy/sell sizes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List

from core.actions import HOLD
from core.config import get_bot_config
from core.models import MarketContext, SignalAnalysis
from logger import log
from strategies.base import BaseStrategy
from strategies.grid_plan import (
    GridPlan,
    build_grid_plan,
    evaluate_plan_at_price,
    plan_from_legacy_state,
    recenter_plan,
    should_recenter,
)
from strategies.trading_modes import (
    MODE_DEFENSIVE,
    MODE_GRID,
    MODE_HYBRID,
    mode_allows_grid_sells,
    mode_allows_new_grid_buys,
    resolve_trading_mode,
)


# Backward-compatible names (tests / older imports)
@dataclass
class GridLevel:
    price: float
    side: str
    filled: bool = False


@dataclass
class GridState:
    center_price: float
    spacing: float
    levels: List[GridLevel] = field(default_factory=list)
    last_recenter_price: float = 0.0


class GridStrategy(BaseStrategy):
    name = "grid"
    _persist_debounce_sec = 30.0
    _last_persist_at: Dict[str, float] = {}

    def __init__(self):
        self._plans: Dict[str, GridPlan] = {}
        self._states: Dict[str, GridState] = {}

    def _get_key(self, symbol: str, tf: str) -> str:
        return f"{symbol}_{tf}"

    def _grid_cfg(self) -> dict:
        return get_bot_config().grid_config

    def _load_or_init_plan(
        self, symbol: str, tf: str, current_price: float, params: dict
    ) -> GridPlan:
        key = self._get_key(symbol, tf)
        if key in self._plans:
            return self._plans[key]

        try:
            from data_manager import get_config

            cfg = get_config()
            grid_states = (cfg or {}).get("grid_states", {}) or {}
            stored = grid_states.get(key) or grid_states.get(f"{symbol}_{tf}")
            if stored and isinstance(stored, dict):
                if stored.get("levels") and "center" in stored:
                    plan = GridPlan.from_dict({**stored, "symbol": symbol, "timeframe": tf})
                else:
                    plan = plan_from_legacy_state(symbol, tf, stored)
                if plan.center > 0 and plan.levels:
                    self._plans[key] = plan
                    return plan
        except Exception:
            pass

        gcfg = self._grid_cfg()
        spacing_mult = float(
            params.get("grid_spacing_atr_mult", gcfg.get("default_spacing_atr_mult", 0.8))
        )
        atr_pct = float(params.get("atr_pct", 3.0))
        n_levels = int(gcfg.get("max_levels", 12) or 12)
        n_side = max(1, n_levels // 2)
        plan = build_grid_plan(
            symbol,
            tf,
            current_price,
            atr_pct=atr_pct,
            spacing_atr_mult=spacing_mult,
            n_buy_levels=n_side,
            n_sell_levels=n_side,
        )
        self._plans[key] = plan
        return plan

    def _persist_plan(self, plan: GridPlan, *, force: bool = False) -> None:
        key = self._get_key(plan.symbol, plan.timeframe)
        now = time.time()
        if not force:
            last = self._last_persist_at.get(key, 0.0)
            if now - last < self._persist_debounce_sec:
                self._plans[key] = plan
                return
        self._last_persist_at[key] = now
        serial = plan.to_dict()
        # Keep legacy field names for /grid status readers
        serial["center_price"] = plan.center
        try:
            from data_manager import get_config, save_config

            cfg = dict(get_config() or {})
            gs = cfg.setdefault("grid_states", {})
            gs[key] = serial
            save_config(cfg)
            self._plans[key] = plan
        except Exception as e:
            log(f"[Grid] persist skipped for {key}: {e}", "DEBUG")
            self._plans[key] = plan

    def analyze(
        self,
        coin: dict,
        market: MarketContext,
        x_signals=None,
    ) -> SignalAnalysis:
        symbol = coin.get("symbol") or market.symbol
        tf = market.timeframe
        price = float(market.current_price or 0)
        params = dict(market.strategy_params or {})
        allocation = getattr(market, "allocation", None) or params.get("allocation")

        force_grid = (params.get("strategy_class") == "grid") or (coin.get("strategy_class") == "grid")
        mode = resolve_trading_mode(allocation, force_grid=force_grid)

        if mode == MODE_DEFENSIVE and not market.has_position:
            return SignalAnalysis(
                action=HOLD,
                symbol=symbol,
                timeframe=tf,
                rsi=market.rsi,
                lower_bb=market.lower_bb,
                vol_multiplier=market.vol_multiplier,
                ampel_emoji="🟠",
                ampel_text="Defensive — no new grid buys",
                sources=["grid", "mode_defensive"],
                normalized_action=HOLD,
                strategy_profile="grid",
                rationale=f"mode={MODE_DEFENSIVE}",
                confidence=0.5,
            )

        if mode not in (MODE_GRID, MODE_HYBRID) and not force_grid:
            return SignalAnalysis(
                action=HOLD,
                symbol=symbol,
                timeframe=tf,
                rsi=market.rsi,
                lower_bb=market.lower_bb,
                vol_multiplier=market.vol_multiplier,
                ampel_emoji="🟡",
                ampel_text="Grid deaktiviert durch Allocator",
                sources=["grid"],
                normalized_action=HOLD,
                strategy_profile="grid",
                rationale=f"mode={mode}",
                confidence=0.4,
            )

        gcfg = self._grid_cfg()
        atr_pct = float(params.get("atr_pct", market.atr_pct or 3.0))
        spacing_mult = float(
            params.get("grid_spacing_atr_mult", gcfg.get("default_spacing_atr_mult", 0.8))
        )
        re_center_mult = float(
            params.get("re_center_atr_mult", gcfg.get("re_center_atr_mult", 2.5))
        )

        # Prefer pre-seeded legacy _states (unit tests)
        key = self._get_key(symbol, tf)
        if key in self._states and key not in self._plans:
            st = self._states[key]
            plan = plan_from_legacy_state(
                symbol,
                tf,
                {
                    "center_price": st.center_price,
                    "spacing": st.spacing,
                    "levels": [
                        {"price": lv.price, "side": lv.side, "filled": lv.filled}
                        for lv in st.levels
                    ],
                    "last_recenter_price": st.last_recenter_price,
                },
            )
            self._plans[key] = plan
        else:
            plan = self._load_or_init_plan(symbol, tf, price, {**params, "atr_pct": atr_pct})

        if should_recenter(
            plan, price, atr_pct=atr_pct, re_center_atr_mult=re_center_mult,
        ):
            plan = recenter_plan(
                plan, price, atr_pct=atr_pct, spacing_atr_mult=spacing_mult,
            )
            self._persist_plan(plan, force=True)
            log(f"[Grid] Re-centered plan for {symbol} @ {price:.6g}", "INFO")

        act = evaluate_plan_at_price(
            plan,
            price,
            has_position=bool(market.has_position),
            allow_buys=mode_allows_new_grid_buys(mode),
            allow_sells=mode_allows_grid_sells(mode),
        )
        if act.action != HOLD:
            self._persist_plan(plan, force=True)
        else:
            self._persist_plan(plan, force=False)

        # HYBRID: only take grid sells / mild buys (smaller slice)
        buy_frac = act.buy_usdt_frac
        if mode == MODE_HYBRID and act.action == "BUY":
            buy_frac = max(0.05, buy_frac * 0.6)

        dca_usdt = 0.0
        if act.action == "BUY" and buy_frac > 0:
            base = float(get_bot_config().max_usdt_per_trade or 500)
            dca_usdt = round(base * buy_frac, 2)

        return SignalAnalysis(
            action=act.action,
            symbol=symbol,
            timeframe=tf,
            rsi=market.rsi,
            lower_bb=market.lower_bb,
            vol_multiplier=market.vol_multiplier,
            ampel_emoji="🔵" if act.action != HOLD else "🟡",
            ampel_text=act.rationale or "Grid monitoring",
            sources=["grid", f"mode_{mode.lower()}"],
            normalized_action=act.action,
            rationale=f"{act.rationale} | mode={mode}",
            strategy_profile="grid",
            confidence=0.72 if act.action != HOLD else 0.55,
            dca_usdt=dca_usdt,
            atr_pct=atr_pct,
        )

    # --- backward-compatible helpers used by tests ---
    def _persist_state(self, symbol: str, tf: str, state, *, force: bool = False) -> None:
        """Legacy test helper: accept GridState-like or GridPlan."""
        if isinstance(state, GridPlan):
            self._persist_plan(state, force=force)
            return
        # GridState dataclass from older tests
        levels = []
        for lv in getattr(state, "levels", []) or []:
            levels.append(
                {
                    "price": float(getattr(lv, "price", 0)),
                    "side": str(getattr(lv, "side", "buy")),
                    "filled": bool(getattr(lv, "filled", False)),
                }
            )
        plan = plan_from_legacy_state(
            symbol,
            tf,
            {
                "center_price": float(getattr(state, "center_price", 0)),
                "spacing": float(getattr(state, "spacing", 0)),
                "levels": levels,
                "last_recenter_price": float(getattr(state, "last_recenter_price", 0)),
            },
        )
        self._persist_plan(plan, force=force)
        # Keep attribute name used in old tests
        self._states = getattr(self, "_states", {})
        self._states[self._get_key(symbol, tf)] = state
