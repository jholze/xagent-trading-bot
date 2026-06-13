import numpy as np
import pandas as pd

from strategies.decision_engine import DecisionEngine
from core.models import MarketContext
from data.cmc_community_provider import CMCCommunitySignal


def test_cmc_threshold_override_via_strategy_params():
    engine = DecisionEngine()
    market = MarketContext(
        symbol="ARIA/USDT",
        timeframe="4h",
        current_price=1.0,
        rsi=50.0,
        lower_bb=0.9,
        vol_multiplier=1.0,
        has_position=False,
        open_positions=0,
        strategy_params={
            "buy_regime": "dip",
            "cmc_trust_score": 75.0,
            "cmc_min_confidence": 55.0,
        },
    )
    cmc = CMCCommunitySignal(coin="ARIA", action="BUY", confidence=84)
    coin = {"symbol": "ARIA/USDT", "timeframe": "4h", "strategy_params": market.strategy_params}
    analysis = engine.evaluate_with_market(coin, market, cmc_signals=[cmc])
    assert analysis.action == "BUY"
    assert "cmc" in analysis.sources


def test_pipeline_backtester_runs_on_synthetic_data():
    from hermes.pipeline_backtest import PipelineBacktester

    n = 80
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    ts = [1_700_000_000_000 + i * 4 * 3600 * 1000 for i in range(n)]
    df = pd.DataFrame({
        "ts": ts,
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": rng.uniform(1000, 3000, n),
    })
    params = {
        "buy_regime": "both",
        "rsi_buy_low": 20,
        "rsi_buy_high": 55,
        "volume_multiplier": 1.0,
        "cmc_trust_score": 70.0,
        "cmc_min_confidence": 50.0,
        "rsi_sell_30": 70,
        "rsi_sell_20": 85,
        "stop_loss_pct": 15.0,
    }
    result = PipelineBacktester().run("TEST/USDT", "4h", params, df)
    assert result.bars_tested > 0
    assert result.metrics is not None