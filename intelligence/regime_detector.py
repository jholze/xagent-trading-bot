"""
RegimeDetector – erweiterte Marktregime-Erkennung mit Sentiment-Fusion.

Erweitert (ersetzt nicht) die bestehende volatility_tier Logik.
Gibt ein RegimeResult zurück, das von StrategyAllocator und anderen Komponenten verwendet wird.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

try:
    import talib
except ImportError:
    talib = None

from core.models import RegimeResult
from intelligence.volatility_classifier import volatility_tier, tier_score, classify_coin
from logger import log


DEFAULT_REGIMES = [
    "RANGING",
    "STRONG_UPTREND",
    "STRONG_DOWNTREND",
    "CHOPPY_HIGH_VOL",
    "TRANSITION",
]


class RegimeDetector:
    """
    Erkennt Marktregime pro Coin.

    Technische Komponenten:
    - ADX + DI
    - 200 EMA Slope
    - Bollinger Band Width / ATR Percentile
    - Wiederverwendung der bestehenden volatility_tier Logik

    Sentiment Komponente:
    - LunarCrush, Santiment, X-Signale, Fear & Greed (werden normalisiert übergeben)

    Gewichtete Fusion (default 0.62 Tech / 0.38 Sentiment)
    """

    def __init__(self, config: Optional[dict] = None):
        self.cfg = config or {}
        self.tech_weight = float(self.cfg.get("tech_weight", 0.62))
        self.sentiment_weight = float(self.cfg.get("sentiment_weight", 0.38))
        self.hysteresis = float(self.cfg.get("hysteresis", 0.15))
        self.cooldown_bars = int(self.cfg.get("cooldown_bars", 6))

        # Einfacher interner State für Hysteresis (pro Symbol+TF)
        self._last_regime: Dict[str, str] = {}
        self._bars_in_regime: Dict[str, int] = {}

    def _get_key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}_{timeframe}"

    def _normalize_sentiment(self, social_context: Optional[dict]) -> float:
        """
        Erwartet social_context mit möglichen Keys:
        - lunarcrush_sentiment (0-100)
        - santiment_sentiment (-1 .. 1 oder 0-100)
        - x_sentiment / x_confidence
        - fear_greed (0-100)
        """
        if not social_context:
            return 0.0

        scores = []
        weights = []

        # LunarCrush (0-100)
        lc = social_context.get("lunarcrush_sentiment")
        if lc is not None:
            s = (float(lc) - 50) / 50.0  # → -1 .. +1
            scores.append(s)
            weights.append(1.0)

        # Santiment (kann -1..1 oder 0-100 sein)
        st = social_context.get("santiment_sentiment")
        if st is not None:
            s = float(st)
            if s > 1.0:
                s = (s - 50) / 50.0
            scores.append(s)
            weights.append(1.0)

        # X / Social
        x_sent = social_context.get("x_sentiment")
        x_conf = social_context.get("x_confidence", 50)
        if x_sent is not None:
            s = float(x_sent)
            conf = float(x_conf) / 100.0
            scores.append(s)
            weights.append(conf)

        # Fear & Greed (0-100, 50 = neutral)
        fg = social_context.get("fear_greed")
        if fg is not None:
            s = (float(fg) - 50) / 50.0
            scores.append(s)
            weights.append(0.7)  # etwas geringeres Gewicht, da global

        if not scores:
            return 0.0

        total_weight = sum(weights) or 1.0
        return sum(s * w for s, w in zip(scores, weights)) / total_weight

    def _compute_technical_score(
        self,
        ohlcv: pd.DataFrame,
        current_price: float,
        atr_pct: Optional[float] = None,
        coin: Optional[dict] = None,
        cfg: Optional[dict] = None,
    ) -> Dict[str, float]:
        """
        Berechnet einen technischen Regime-Score (-1 .. +1) + Komponenten.
        """
        if len(ohlcv) < 200:
            return {"tech_score": 0.0, "components": {}}

        close = ohlcv["close"]
        high = ohlcv["high"]
        low = ohlcv["low"]

        # --- ADX + DI ---
        if talib is not None:
            try:
                adx = talib.ADX(high, low, close, timeperiod=14).iloc[-1]
                plus_di = talib.PLUS_DI(high, low, close, timeperiod=14).iloc[-1]
                minus_di = talib.MINUS_DI(high, low, close, timeperiod=14).iloc[-1]
            except Exception:
                adx = plus_di = minus_di = np.nan
        else:
            adx = plus_di = minus_di = np.nan

        # NaN is truthy in Python, so `adx or 0` does not default when talib fails.
        adx_safe = 0.0 if adx != adx else float(adx)
        trend_strength = min(max(adx_safe / 50.0, 0.0), 1.0)  # 0..1
        direction = 0.0
        if plus_di == plus_di and minus_di == minus_di and (plus_di + minus_di) > 0:
            direction = (plus_di - minus_di) / (plus_di + minus_di)

        # --- 200 EMA Slope ---
        ema200 = close.ewm(span=200, adjust=False).mean()
        ema_slope = (ema200.iloc[-1] - ema200.iloc[-20]) / (ema200.iloc[-20] or 1e-9)
        ema_trend = np.clip(ema_slope * 10, -1.0, 1.0)

        # --- Bollinger Band Width / ATR ---
        if talib is not None:
            try:
                upper, middle, lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2)
                bb_width = (upper.iloc[-1] - lower.iloc[-1]) / middle.iloc[-1]
            except Exception:
                bb_width = 0.0
        else:
            bb_width = 0.0

        atr_val = atr_pct if atr_pct is not None else 3.0
        vol_score = min(atr_val / 8.0, 1.0)  # grobe Normalisierung

        # Kombinierter Tech-Score
        tech_score = (
            0.40 * direction * trend_strength +
            0.25 * ema_trend +
            0.15 * (1.0 if bb_width < 0.08 else -0.5) +   # enge Bänder → eher ranging
            0.20 * (vol_score if vol_score > 0.6 else 0.0)  # hohe Vol → choppy
        )

        components = {
            "adx": float(adx_safe),
            "direction": float(direction),
            "ema200_slope": float(ema_slope),
            "bb_width": float(bb_width),
            "atr_pct": float(atr_val),
            "tech_score": float(np.clip(tech_score, -1.0, 1.0)),
        }

        return {"tech_score": float(np.clip(tech_score, -1.0, 1.0)), "components": components}

    def detect(
        self,
        coin: dict,
        ohlcv_df: pd.DataFrame,
        current_price: float,
        atr_pct: Optional[float] = None,
        social_context: Optional[dict] = None,
        previous_regime: Optional[str] = None,
        bars_since_last_change: int = 0,
    ) -> RegimeResult:
        """
        Hauptmethode.
        """
        symbol = coin.get("symbol", "UNKNOWN")
        tf = coin.get("timeframe", "4h")
        key = self._get_key(symbol, tf)

        cfg = self.cfg
        vol_cfg = cfg.get("volatile_altcoin_config", {}) if isinstance(cfg, dict) else {}

        # 1. Technischer Score (robust bei wenig Daten)
        if ohlcv_df is None or len(ohlcv_df) < 30:
            tech_score = 0.0
            tech_components = {}
        else:
            tech = self._compute_technical_score(
                ohlcv_df, current_price, atr_pct=atr_pct, coin=coin, cfg=cfg
            )
            tech_score = tech["tech_score"]
            tech_components = tech.get("components", {})

        # 2. Volatility Tier (bestehende Logik wiederverwenden - erweitern, nicht ersetzen)
        vol_tier = "stable"
        try:
            vol_tier = volatility_tier(
                coin,
                atr_pct or 3.0,
                vol_cfg,
                range_24h_pct=coin.get("range_24h_pct"),
                change_24h_pct=coin.get("change_24h_pct"),
            )
        except Exception as e:
            log(f"[RegimeDetector] volatility_tier failed: {e}", "WARNING")

        # 3. Sentiment Score (robust)
        sentiment_score = self._normalize_sentiment(social_context or {})

        # 4. Weighted Score (konfigurierbar)
        weighted = self.tech_weight * tech_score + self.sentiment_weight * sentiment_score

        # 5. Regime Mapping mit Hysteresis + Cooldown
        regime = self._map_to_regime(
            weighted, tech_score, sentiment_score, vol_tier, key, previous_regime, bars_since_last_change
        )

        confidence = min(0.3 + 0.7 * abs(weighted), 0.98)

        result = RegimeResult(
            primary_regime=regime,
            confidence=round(confidence, 3),
            weighted_score=round(weighted, 4),
            volatility_tier=vol_tier,
            sentiment_score=round(sentiment_score, 3),
            components={
                "tech": round(tech_score, 3),
                "sentiment": round(sentiment_score, 3),
                **tech_components,
            },
            details={
                "tech_weight": self.tech_weight,
                "sentiment_weight": self.sentiment_weight,
                "bars_in_regime": self._bars_in_regime.get(key, 0),
            },
        )

        # Update internal hysteresis state
        if regime != previous_regime:
            self._last_regime[key] = regime
            self._bars_in_regime[key] = 1
        else:
            self._bars_in_regime[key] = self._bars_in_regime.get(key, 0) + 1

        return result

    def _map_to_regime(
        self,
        weighted: float,
        tech_score: float,
        sentiment: float,
        vol_tier: str,
        key: str,
        previous: Optional[str],
        bars: int,
    ) -> str:
        # Hysteresis: erst nach Cooldown wechseln
        if previous and bars < self.cooldown_bars:
            return previous

        # Sehr starkes negatives Sentiment → defensiv / choppy oder transition
        if sentiment < -0.55:
            if abs(tech_score) > 0.4:
                return "STRONG_DOWNTREND"
            return "CHOPPY_HIGH_VOL"

        # Technik-dominiert
        if weighted > 0.55:
            return "STRONG_UPTREND"
        if weighted < -0.55:
            return "STRONG_DOWNTREND"

        if abs(weighted) < 0.35 and vol_tier == "volatile":
            return "CHOPPY_HIGH_VOL"

        if abs(weighted) < 0.40:
            return "RANGING"

        # Übergangszone
        return "TRANSITION"
