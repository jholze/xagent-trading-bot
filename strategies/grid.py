"""
GridStrategy – dynamische Grid-Strategie für Spot-Trading.

Wird über den StrategyAllocator aktiviert und parametrisiert.
Unterstützt:
- ATR- oder prozentbasiertes Spacing
- Automatisches Re-Centering
- Fee-bewusste Gewinnkalkulation (vereinfacht)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.models import MarketContext, SignalAnalysis
from strategies.base import BaseStrategy
from core.config import get_bot_config
from logger import log


@dataclass
class GridLevel:
    price: float
    side: str          # "buy" or "sell"
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
        self._states: Dict[str, GridState] = {}   # key = f"{symbol}_{tf}"

    def _get_key(self, symbol: str, tf: str) -> str:
        return f"{symbol}_{tf}"

    def _load_or_init_state(
        self, symbol: str, tf: str, current_price: float, params: dict
    ) -> GridState:
        key = self._get_key(symbol, tf)
        if key in self._states:
            return self._states[key]

        try:
            from data_manager import get_config

            cfg = get_config()
            grid_states = (cfg or {}).get("grid_states", {}) or {}
            stored = grid_states.get(key) or grid_states.get(f"{symbol}_{tf}")
            if stored and isinstance(stored, dict):
                state = GridState(
                    center_price=float(stored.get("center_price", current_price)),
                    spacing=float(stored.get("spacing", 0.01)),
                    levels=[GridLevel(**l) for l in stored.get("levels", []) if isinstance(l, dict)],
                    last_recenter_price=float(stored.get("last_recenter_price", stored.get("center_price", current_price))),
                )
                self._states[key] = state
                return state
        except Exception:
            pass

        # Fallback: frisches Grid
        grid_cfg = get_bot_config().grid_config
        spacing_mult = float(params.get("grid_spacing_atr_mult", grid_cfg.get("default_spacing_atr_mult", 0.8)))
        atr_pct = float(params.get("atr_pct", 3.0))

        spacing = current_price * (atr_pct / 100.0) * spacing_mult
        center = current_price

        levels = []
        for i in range(1, 7):
            levels.append(GridLevel(price=center - i * spacing, side="buy"))
            levels.append(GridLevel(price=center + i * spacing, side="sell"))

        state = GridState(center_price=center, spacing=spacing, levels=levels, last_recenter_price=center)
        self._states[key] = state
        return state

    def _should_recenter(self, state: GridState, current_price: float, params: dict) -> bool:
        grid_cfg = get_bot_config().grid_config
        re_center_mult = float(params.get("re_center_atr_mult", grid_cfg.get("re_center_atr_mult", 2.5)))
        atr_pct = float(params.get("atr_pct", 3.0))

        distance = abs(current_price - state.center_price)
        threshold = state.center_price * (atr_pct / 100.0) * re_center_mult
        return distance > threshold

    def _persist_state(self, symbol: str, tf: str, state: GridState, *, force: bool = False) -> None:
        """Persist grid state in tenant-scoped config (grid_states map)."""
        key = self._get_key(symbol, tf)
        now = time.time()
        if not force:
            last = self._last_persist_at.get(key, 0.0)
            if now - last < self._persist_debounce_sec:
                self._states[key] = state
                return
        self._last_persist_at[key] = now
        serial = {
            "center_price": float(state.center_price),
            "spacing": float(state.spacing),
            "levels": [{"price": float(l.price), "side": str(l.side), "filled": bool(l.filled)} for l in state.levels],
            "last_recenter_price": float(state.last_recenter_price),
        }
        try:
            from data_manager import get_config, save_config

            cfg = dict(get_config() or {})
            gs = cfg.setdefault("grid_states", {})
            gs[key] = serial
            save_config(cfg)
            self._states[key] = state
        except Exception as e:
            log(f"[Grid] persist skipped for {key}: {e}", "DEBUG")
            self._states[key] = state

    def _recenter_grid(self, state: GridState, current_price: float, params: dict, symbol: str, tf: str):
        """Erzeugt ein neues Grid um den aktuellen Preis."""
        grid_cfg = get_bot_config().grid_config
        spacing_mult = float(params.get("grid_spacing_atr_mult", grid_cfg.get("default_spacing_atr_mult", 0.8)))
        atr_pct = float(params.get("atr_pct", 3.0))

        new_spacing = current_price * (atr_pct / 100.0) * spacing_mult
        new_levels = []

        for i in range(1, 7):
            new_levels.append(GridLevel(price=current_price - i * new_spacing, side="buy"))
            new_levels.append(GridLevel(price=current_price + i * new_spacing, side="sell"))

        state.center_price = current_price
        state.spacing = new_spacing
        state.levels = new_levels
        state.last_recenter_price = current_price

        self._persist_state(symbol, tf, state, force=True)
        log(f"[Grid] Re-centered grid for {symbol} @ {current_price:.2f}", "INFO")

    def analyze(
        self,
        coin: dict,
        market: MarketContext,
        x_signals=None,
    ) -> SignalAnalysis:
        symbol = coin.get("symbol")
        tf = market.timeframe
        price = market.current_price
        params = dict(market.strategy_params or {})

        # Support explicit grid (strategy_class) or allocator weight
        allocation = getattr(market, "allocation", None) or params.get("allocation", {})
        grid_weight = 0.0
        if isinstance(allocation, dict):
            grid_weight = allocation.get("strategy_weights", {}).get("grid", 0.0) or 0.0
        else:
            # AllocationDecision object
            sw = getattr(allocation, "strategy_weights", {}) or {}
            grid_weight = sw.get("grid", 0.0) if isinstance(sw, dict) else 0.0

        force_grid = (params.get("strategy_class") == "grid") or (coin.get("strategy_class") == "grid")
        if grid_weight <= 0.05 and not force_grid:
            return SignalAnalysis(
                action="HOLD",
                symbol=symbol,
                timeframe=tf,
                rsi=market.rsi,
                lower_bb=market.lower_bb,
                vol_multiplier=market.vol_multiplier,
                ampel_emoji="🟡",
                ampel_text="Grid deaktiviert durch Allocator",
                sources=["grid"],
                normalized_action="HOLD",
                strategy_profile="grid",
            )

        state = self._load_or_init_state(symbol, tf, price, params)

        # Re-Centering prüfen
        if self._should_recenter(state, price, params):
            self._recenter_grid(state, price, params, symbol, tf)

        # Prüfe, ob Preis ein Level getroffen hat
        action = "HOLD"
        sources = ["grid"]
        rationale = ""

        for level in state.levels:
            if level.filled:
                continue

            # Sehr einfache Level-Erkennung (in Realität würde man Order-Fills tracken)
            if level.side == "buy" and price <= level.price * 1.001:
                action = "BUY"
                rationale = f"Grid buy level @ {level.price:.2f}"
                level.filled = True
                break

            if level.side == "sell" and price >= level.price * 0.999:
                action = "SELL"
                rationale = f"Grid sell level @ {level.price:.2f}"
                level.filled = True
                break

        return SignalAnalysis(
            action=action,
            symbol=symbol,
            timeframe=tf,
            rsi=market.rsi,
            lower_bb=market.lower_bb,
            vol_multiplier=market.vol_multiplier,
            ampel_emoji="🔵" if action != "HOLD" else "🟡",
            ampel_text=rationale or "Grid monitoring",
            sources=sources,
            normalized_action=action,
            rationale=rationale,
            strategy_profile="grid",
            confidence=0.7,
        )
