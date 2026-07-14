"""
StrategyAllocator – entscheidet basierend auf RegimeResult und Sentiment,
welche Strategien wie stark gewichtet werden und welche Parameter verwendet werden.

Wird als separates Modul implementiert (nicht inline in DecisionEngine oder Registry).
"""

from __future__ import annotations

from typing import Dict

from core.models import RegimeResult, AllocationDecision
from core.config import get_bot_config
from logger import log

# Re-export for convenience / existing imports in tests
from core.models import AllocationDecision  # noqa: F401


class StrategyAllocator:
    """
    Entscheidet die Kapitalallokation und Strategie-Gewichtung pro Coin.

    Berücksichtigt:
    - Erkanntes Regime
    - Sentiment-Score
    - Konfigurierbare Regeln (strategy_allocator in config.json)
    """

    def __init__(self, config: dict | None = None):
        self.cfg = config or {}
        self.allocator_cfg = get_bot_config().strategy_allocator_config

    def allocate(
        self,
        regime_result: RegimeResult,
        coin: dict,
        has_position: bool = False,
        current_exposure: float = 0.0,
    ) -> AllocationDecision:
        regime = regime_result.primary_regime
        sentiment = regime_result.sentiment_score
        vol_tier = regime_result.volatility_tier

        # Defaults aus Config
        neutral_thresh = self.allocator_cfg.get("neutral_sentiment_threshold", 0.35)
        confirm_thresh = self.allocator_cfg.get("confirm_sentiment_threshold", 0.45)
        defensive_thresh = self.allocator_cfg.get("defensive_sentiment_threshold", -0.55)

        default_grid_w = self.allocator_cfg.get("default_grid_weight", 0.6)
        default_mom_w = self.allocator_cfg.get("default_momentum_weight", 0.4)

        weights = {"grid": 0.0, "momentum": 1.0}
        exposure_mult = 1.0
        grid_params = {}
        mom_override = {}
        defensive = False
        rationale_parts = []

        # === 1. Extrem negatives Sentiment → Defensiv-Override ===
        if sentiment <= defensive_thresh:
            exposure_mult = 0.30
            weights = {"grid": 0.0, "momentum": 0.30}
            defensive = True
            rationale_parts.append(f"Extreme negative sentiment ({sentiment:.2f}) → defensive mode")
            return AllocationDecision(
                strategy_weights=weights,
                exposure_multiplier=exposure_mult,
                grid_params=grid_params,
                momentum_params_override=mom_override,
                defensive_mode=defensive,
                rationale=" | ".join(rationale_parts),
            )

        # === 2. Regime + Sentiment Regeln ===
        if regime == "RANGING":
            if abs(sentiment) <= neutral_thresh:
                weights = {"grid": default_grid_w, "momentum": default_mom_w}
                grid_params = {"spacing_mode": "aggressive", "re_center_enabled": True}
                rationale_parts.append("Ranging + neutral sentiment → Grid priorisiert")
            else:
                weights = {"grid": 0.45, "momentum": 0.55}
                rationale_parts.append("Ranging + sentiment bias")

        elif regime == "STRONG_UPTREND":
            if sentiment >= confirm_thresh:
                weights = {"grid": 0.15, "momentum": 0.85}
                mom_override = {"buy_bias": "strong"}
                rationale_parts.append("Strong uptrend + confirming positive sentiment → Momentum priorisiert")
            else:
                weights = {"grid": 0.35, "momentum": 0.65}
                rationale_parts.append("Strong uptrend (sentiment neutral/schwach)")

        elif regime == "STRONG_DOWNTREND":
            exposure_mult = 0.40
            weights = {"grid": 0.10, "momentum": 0.30}
            defensive = True
            rationale_parts.append("Strong downtrend → stark reduzierte Exposure")

        elif regime == "CHOPPY_HIGH_VOL":
            exposure_mult = 0.55
            weights = {"grid": 0.25, "momentum": 0.30}
            grid_params = {"spacing_mode": "wide"}
            rationale_parts.append("Choppy high vol → reduzierte Positionen + weites Grid")

        elif regime == "TRANSITION":
            exposure_mult = 0.70
            weights = {"grid": 0.40, "momentum": 0.40}
            rationale_parts.append("Transition phase → vorsichtige Allokation")

        else:
            weights = {"grid": default_grid_w, "momentum": default_mom_w}

        # === 3. Volatilitäts-Anpassung (bestehende Logik respektieren) ===
        if vol_tier == "volatile":
            exposure_mult *= 0.85
            if "grid" in weights:
                weights["grid"] = max(0.0, weights.get("grid", 0) * 0.8)

        # Normalisiere Gewichte auf 1.0
        total_w = sum(weights.values()) or 1.0
        weights = {k: round(v / total_w, 3) for k, v in weights.items()}

        rationale = " | ".join(rationale_parts) if rationale_parts else "Default allocation"

        return AllocationDecision(
            strategy_weights=weights,
            exposure_multiplier=round(exposure_mult, 3),
            grid_params=grid_params,
            momentum_params_override=mom_override,
            defensive_mode=defensive,
            rationale=rationale,
        )
