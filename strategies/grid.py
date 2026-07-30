"""
GridStrategy – slice-based grid for spot (Phase A).

Activated via StrategyAllocator / force strategy_class=grid.
Uses pure GridPlan for levels + partial buy/sell sizes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List

from core.actions import HOLD, SELL_PARTIAL_50
from core.config import get_bot_config
from core.models import MarketContext, SignalAnalysis
from logger import log
from strategies.base import BaseStrategy
from strategies.grid_plan import (
    GridPlan,
    apply_grid_sell_guards,
    build_grid_plan,
    evaluate_plan_at_price,
    grid_gain_pct,
    plan_from_legacy_state,
    recenter_plan,
    should_block_recenter_below_entry,
    should_recenter,
    spacing_atr_mult_for_coin,
)
from strategies.grid_memory_policy import apply_grid_memory_sell_policy
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
            from storage.grid_plan_store import load_grid_plan

            stored = load_grid_plan(symbol, tf)
            if stored and isinstance(stored, dict):
                if stored.get("levels") and (
                    "center" in stored or "center_price" in stored
                ):
                    raw = dict(stored)
                    if "center" not in raw and "center_price" in raw:
                        raw["center"] = raw["center_price"]
                    plan = GridPlan.from_dict({**raw, "symbol": symbol, "timeframe": tf})
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
        if gcfg.get("fee_aware", True):
            try:
                from strategies.grid_limits import enforce_fee_spacing

                fee = float(gcfg.get("assumed_fee_pct", 0.1))
                plan = enforce_fee_spacing(plan, fee_pct=fee)
            except Exception:
                pass
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
            from storage.grid_plan_store import save_grid_plan

            save_grid_plan(plan.symbol, plan.timeframe, serial)
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
        vol_tier = str(
            params.get("volatility_tier")
            or getattr(market, "volatility_tier", "")
            or ""
        )
        try:
            from intelligence.strategy_backtest import classify_coin

            coin_class = classify_coin(symbol, params)
        except Exception:
            coin_class = ""

        mode = resolve_trading_mode(
            allocation,
            force_grid=force_grid,
            volatility_tier=vol_tier,
            coin_class=coin_class,
        )

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
                rationale=f"mode={MODE_DEFENSIVE} tier={vol_tier or '?'}",
                confidence=0.5,
                volatility_tier=vol_tier,
            )

        # Regime flip while in position: rotate out a slice (Phase B)
        if mode == MODE_DEFENSIVE and market.has_position:
            return SignalAnalysis(
                action=SELL_PARTIAL_50,
                symbol=symbol,
                timeframe=tf,
                rsi=market.rsi,
                lower_bb=market.lower_bb,
                vol_multiplier=market.vol_multiplier,
                ampel_emoji="🟠",
                ampel_text="Defensive — reduce grid inventory",
                sources=["grid", "mode_defensive", "regime_flip"],
                normalized_action=SELL_PARTIAL_50,
                strategy_profile="grid",
                rationale=f"mode={MODE_DEFENSIVE} reduce | tier={vol_tier or '?'}",
                confidence=0.65,
                volatility_tier=vol_tier,
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
        base_spacing = float(
            params.get("grid_spacing_atr_mult", gcfg.get("default_spacing_atr_mult", 0.8))
        )
        spacing_mult = spacing_atr_mult_for_coin(
            volatility_tier=vol_tier,
            coin_class=coin_class,
            base=base_spacing,
            volatile_mult=float(gcfg.get("volatile_spacing_atr_mult", 1.15)),
            stable_mult=float(gcfg.get("stable_spacing_atr_mult", 0.55)),
            meme_mult=float(gcfg.get("meme_spacing_atr_mult", 1.25)),
        )
        # Allocator spacing_mode (wide/aggressive) + Santiment global spacing (P2)
        grid_extra = params.get("grid") if isinstance(params.get("grid"), dict) else {}
        spacing_mode = str(
            params.get("spacing_mode")
            or grid_extra.get("spacing_mode")
            or ""
        ).lower()
        if spacing_mode == "wide":
            spacing_mult *= 1.25
        elif spacing_mode == "aggressive":
            spacing_mult *= 0.9
        try:
            from services.market_policy_fusion import get_global_market_bias
            from services.santiment_policy import santiment_risk_config

            if santiment_risk_config().get("apply_grid_spacing", True):
                bias = get_global_market_bias()
                if bias.get("apply_grid_spacing") and bias.get("active"):
                    spacing_mult *= float(bias.get("grid_spacing_mult") or 1.0)
        except Exception:
            pass
        re_center_mult = float(
            params.get("re_center_atr_mult", gcfg.get("re_center_atr_mult", 2.5))
        )
        # Volatile: re-center less aggressively (avoid thrash)
        if vol_tier == "volatile" or coin_class == "meme":
            re_center_mult = max(re_center_mult, float(gcfg.get("volatile_re_center_atr_mult", 3.2)))

        # Level TF (default 1h): plan key independent of watchlist 4h coin TF
        level_tf = str(
            params.get("grid_level_timeframe")
            or gcfg.get("level_timeframe")
            or "1h"
        ).strip() or "1h"
        plan_tf = level_tf

        # Prefer pre-seeded legacy _states (unit tests) — key by coin TF for tests
        legacy_key = self._get_key(symbol, tf)
        key = self._get_key(symbol, plan_tf)
        if legacy_key in self._states and key not in self._plans:
            st = self._states[legacy_key]
            plan = plan_from_legacy_state(
                symbol,
                plan_tf,
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
            plan = self._load_or_init_plan(
                symbol, plan_tf, price, {**params, "atr_pct": atr_pct},
            )

        sell_policy = dict(gcfg.get("sell_policy") or {})
        avg_entry = float(getattr(market, "average_entry", 0) or 0)

        if should_recenter(
            plan, price, atr_pct=atr_pct, re_center_atr_mult=re_center_mult,
        ):
            if should_block_recenter_below_entry(
                price, avg_entry, policy=sell_policy,
            ):
                log(
                    f"[Grid] Re-center skipped {symbol}: price {price:.6g} "
                    f"below entry {avg_entry:.6g} (sell_policy)",
                    "INFO",
                )
            else:
                plan = recenter_plan(
                    plan, price, atr_pct=atr_pct, spacing_atr_mult=spacing_mult,
                )
                self._persist_plan(plan, force=True)
                log(
                    f"[Grid] Re-centered plan for {symbol} @ {price:.6g} ({plan_tf})",
                    "INFO",
                )

        bar_low, bar_high = self._level_bar_range(
            symbol, plan_tf, price, gcfg,
        )
        act = evaluate_plan_at_price(
            plan,
            price,
            has_position=bool(market.has_position),
            allow_buys=mode_allows_new_grid_buys(mode),
            allow_sells=mode_allows_grid_sells(mode),
            bar_low=bar_low,
            bar_high=bar_high,
        )
        # Guards: center + min-gain (no harvest under entry) + memory facts
        mem_bits: list[str] = []
        if "SELL" in str(act.action or "").upper():
            act = apply_grid_sell_guards(
                act,
                plan=plan,
                sell_price=price,
                average_entry=avg_entry,
                mode=mode,
                policy=sell_policy,
            )
            gain = grid_gain_pct(price, avg_entry)
            try:
                from intelligence.memory.coin_facts import summarize_facts_for_symbol

                flags = summarize_facts_for_symbol(symbol)
                before = act.action
                act = apply_grid_memory_sell_policy(
                    act,
                    gain_pct=gain,
                    flags=flags,
                    policy=sell_policy,
                )
                if before != act.action or "memory:" in (act.rationale or ""):
                    mem_bits.append("memory_checked")
                    if flags.event_count:
                        mem_bits.append(f"facts={flags.event_count}")
                    if flags.summary:
                        mem_bits.append(flags.summary[:60])
            except Exception as e:
                log(f"[Grid] memory sell check fail-open {symbol}: {e}", "DEBUG")
        if act.action != HOLD:
            self._persist_plan(plan, force=True)
        else:
            self._persist_plan(plan, force=False)

        # HYBRID: slightly smaller than pure grid (was 0.6 — too timid with full cash)
        buy_frac = act.buy_usdt_frac
        if mode == MODE_HYBRID and act.action == "BUY":
            buy_frac = max(0.08, buy_frac * 0.85)

        dca_usdt = 0.0
        if act.action == "BUY" and buy_frac > 0:
            base = float(get_bot_config().max_usdt_per_trade or 500)
            dca_usdt = round(base * buy_frac, 2)

        is_sell = "SELL" in str(act.action or "").upper()
        sources = ["grid", f"mode_{mode.lower()}"]
        if is_sell:
            sources.append("grid_sell")
        if mem_bits:
            sources.append("grid_memory")

        rat = (
            f"{act.rationale} | mode={mode} | tier={vol_tier or coin_class or '?'} "
            f"| spacing×{spacing_mult:.2f} | levels={plan_tf}"
        )
        if mem_bits:
            rat = f"{rat} | {' · '.join(mem_bits)}"

        return SignalAnalysis(
            action=act.action,
            symbol=symbol,
            timeframe=tf,
            rsi=market.rsi,
            lower_bb=market.lower_bb,
            vol_multiplier=market.vol_multiplier,
            ampel_emoji="🔵" if act.action != HOLD else "🟡",
            ampel_text=act.rationale or "Grid monitoring",
            sources=sources,
            normalized_action=act.action,
            rationale=rat,
            strategy_profile="grid",
            confidence=0.72 if act.action != HOLD else 0.55,
            dca_usdt=dca_usdt,
            atr_pct=atr_pct,
            volatility_tier=vol_tier,
            sell_source="grid" if is_sell else "",
        )

    def _level_bar_range(
        self,
        symbol: str,
        level_tf: str,
        live_price: float,
        gcfg: dict,
    ) -> tuple[float, float]:
        """High/low over recent level-TF bars so hits are not only last close."""
        px = float(live_price or 0)
        if not gcfg.get("use_bar_range_hits", True) or px <= 0:
            return px, px
        lookback = max(1, int(gcfg.get("bar_lookback", 2) or 2))
        try:
            from services.market_service import MarketService

            df = MarketService().fetch_ohlcv(symbol, level_tf, limit=lookback + 2)
            if df is None or df.empty:
                return px, px
            tail = df.tail(lookback)
            if "low" in tail.columns and "high" in tail.columns:
                lo = float(tail["low"].min())
                hi = float(tail["high"].max())
                if lo > 0 and hi > 0:
                    return min(lo, px), max(hi, px)
        except Exception as e:
            log(f"[Grid] 1h range fetch skip {symbol}: {e}", "DEBUG")
        return px, px

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
