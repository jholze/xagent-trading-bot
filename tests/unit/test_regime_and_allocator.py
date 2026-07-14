"""Basic tests for RegimeDetector and StrategyAllocator."""

import pandas as pd
import pytest

from intelligence.regime_detector import RegimeDetector, RegimeResult
from intelligence.strategy_allocator import StrategyAllocator, AllocationDecision


def make_sample_df(length=100, trend=0.0):
    """Erzeugt einfache Preisdaten."""
    prices = [100.0 + i * trend + (i % 5) * 0.5 for i in range(length)]
    df = pd.DataFrame({
        "close": prices,
        "high": [p + 1 for p in prices],
        "low": [p - 1 for p in prices],
    })
    return df


def test_regime_detector_basic():
    detector = RegimeDetector({"tech_weight": 0.62, "sentiment_weight": 0.38})
    df = make_sample_df(trend=0.1)
    result = detector.detect(
        {"symbol": "BTC/USDT", "timeframe": "4h"},
        df,
        current_price=110.0,
        atr_pct=2.5,
        social_context={"lunarcrush_sentiment": 65},
    )
    assert isinstance(result, RegimeResult)
    assert result.primary_regime in ["RANGING", "STRONG_UPTREND", "CHOPPY_HIGH_VOL", "TRANSITION"]
    assert -1.0 <= result.sentiment_score <= 1.0
    assert 0.0 <= result.confidence <= 1.0


def test_allocator_ranging_neutral():
    allocator = StrategyAllocator()
    regime = RegimeResult(
        primary_regime="RANGING",
        confidence=0.8,
        weighted_score=0.1,
        volatility_tier="stable",
        sentiment_score=0.1,
    )
    decision = allocator.allocate(regime, {"symbol": "TEST/USDT"})
    assert decision.strategy_weights.get("grid", 0) > 0.5
    assert not decision.defensive_mode


def test_allocator_extreme_negative():
    allocator = StrategyAllocator()
    regime = RegimeResult(
        primary_regime="RANGING",
        confidence=0.7,
        weighted_score=-0.7,
        volatility_tier="volatile",
        sentiment_score=-0.7,
    )
    decision = allocator.allocate(regime, {"symbol": "TEST/USDT"})
    assert decision.defensive_mode
    assert decision.exposure_multiplier <= 0.4
    assert decision.strategy_weights.get("grid", 1) < 0.1


def test_grid_is_selected_when_allocator_wants_it(monkeypatch):
    """Test that high grid weight leads to GridStrategy being chosen."""
    from strategies import registry
    regime = RegimeResult(
        primary_regime="RANGING",
        confidence=0.9,
        weighted_score=0.1,
        volatility_tier="stable",
        sentiment_score=0.1,
    )
    alloc = AllocationDecision(
        strategy_weights={"grid": 0.8, "momentum": 0.2},
        exposure_multiplier=1.0,
    )
    params = registry.resolve_strategy_params(
        {"symbol": "TEST/USDT", "timeframe": "4h"},
        regime_result=regime,
        allocation=alloc,
    )
    # The get_strategy logic looks at allocation in params
    params["allocation"] = {"strategy_weights": {"grid": 0.8, "momentum": 0.2}}
    strat = registry.get_strategy({"strategy_params": params})
    assert strat.name == "grid" or "grid" in str(type(strat)).lower()


def test_regime_hysteresis():
    detector = RegimeDetector({"cooldown_bars": 3})
    df = make_sample_df(trend=0.05)
    r1 = detector.detect({"symbol": "T", "timeframe": "1h"}, df, 105.0, atr_pct=1.5, previous_regime="RANGING", bars_since_last_change=1)
    # Sollte wegen Cooldown noch RANGING bleiben
    assert r1.primary_regime == "RANGING"


def test_grid_strategy_persist_and_analyze(monkeypatch):
    """Exercise GridStrategy level detection + state persistence (in-mem + config path)."""
    from strategies.grid import GridStrategy, GridState, GridLevel
    from core.models import MarketContext, AllocationDecision as AD

    gs = GridStrategy()
    # seed a state that should trigger buy on current price
    key = "GRIDTEST/USDT_1h"
    st = GridState(
        center_price=100.0,
        spacing=1.0,
        levels=[GridLevel(99.0, "buy", filled=False), GridLevel(101.0, "sell", filled=False)],
        last_recenter_price=100.0,
    )
    gs._states[key] = st

    mk = MarketContext(symbol="GRIDTEST/USDT", timeframe="1h", current_price=98.9, rsi=30, atr_pct=2.0)
    mk.strategy_params = {"atr_pct": 2.0, "allocation": {"strategy_weights": {"grid": 0.8}}}
    mk.allocation = AD(strategy_weights={"grid": 0.8})

    sig = gs.analyze({"symbol": "GRIDTEST/USDT", "timeframe": "1h"}, mk)
    assert sig.action in ("BUY", "HOLD")  # may be buy if level hit
    assert sig.strategy_profile == "grid"
    # persist call should not crash
    gs._persist_state("GRIDTEST/USDT", "1h", st)
    assert key in gs._states


def test_decision_engine_regime_enabled_smoke(monkeypatch):
    """End-to-end DecisionEngine with regime+allocator enabled (uses mocks for fetches)."""
    import pandas as pd
    from unittest.mock import patch
    from strategies.decision_engine import DecisionEngine
    from services.market_service import MarketService

    eng = DecisionEngine()
    # enable opt-in features
    eng.config.raw.setdefault("regime_detector", {})["enabled"] = True
    eng.config.raw.setdefault("strategy_allocator", {})["enabled"] = True

    sym = "REGTEST/USDT"
    # patch heavy network parts in MarketService used by evaluate
    fake_ohlcv = pd.DataFrame({
        "close": [100 + i * 0.2 for i in range(220)],
        "high": [101 + i * 0.2 for i in range(220)],
        "low": [99 + i * 0.2 for i in range(220)],
        "ts": list(range(220)),
    })
    fake_ind = {
        "rsi": 42.0,
        "lower_bb": 95.0,
        "upper_bb": 105.0,
        "middle_bb": 100.0,
        "atr_pct": 2.8,
        "vol_multiplier": 1.1,
        "range_24h_pct": 4.0,
        "change_24h_pct": 1.5,
    }

    with patch.object(eng.market, "fetch_ohlcv", return_value=fake_ohlcv), \
         patch.object(eng.market, "fetch_indicators", return_value=fake_ind), \
         patch.object(eng.market, "fetch_15m_sensor_metrics", return_value=None):
        # build context then evaluate (bypasses some external)
        ctx = eng.build_market_context({"symbol": sym, "timeframe": "4h"}, current_price=101.0)
        # call internal evaluate (regime path exercised)
        analysis = eng._evaluate_internal(
            coin={"symbol": sym, "timeframe": "4h"},
            market=ctx,
            x_signals=[],
            cmc_signals=[],
            lc_signals=[],
        )
    assert analysis is not None
    assert analysis.action in ("BUY", "BUY_STRONG", "HOLD", "SELL_FULL", "SELL_PARTIAL_10")
    # regime info attached when detector runs (may be empty string if no data led to attach, but no crash)
    assert hasattr(analysis, "regime")
