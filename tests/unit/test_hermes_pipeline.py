import numpy as np
import pandas as pd
from unittest.mock import patch

from strategies.decision_engine import DecisionEngine
from core.models import MarketContext
from data.cmc_community_provider import CMCCommunitySignal
from tests.support.offline import gate_prices_listed


@patch("price_fetcher.get_gate_prices_batch", side_effect=gate_prices_listed)
@patch("price_fetcher.get_ticker_price", return_value=1.0)
def test_cmc_threshold_override_via_strategy_params(_ticker, _gate):
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
    with patch("services.watchlist_quality.universe.cmc_only_buy_allowed", return_value=(True, "")), \
         patch("services.market_policy_fusion.get_global_market_bias", return_value={"active": False}), \
         patch("strategies.decision_engine.resolve_coin_config", side_effect=lambda c: {
             **c, "strategy_class": "technical_rsi_bb", "strategy_params": market.strategy_params,
         }):
        analysis = engine.evaluate_with_market(coin, market, cmc_signals=[cmc])
    assert analysis.action == "BUY"
    assert "cmc" in analysis.sources


@patch("price_fetcher.get_gate_prices_batch", side_effect=gate_prices_listed)
@patch("price_fetcher.get_ticker_price", return_value=1.0)
@patch("services.market_service.MarketService._fetch_ohlcv", return_value=None)
@patch("services.market_service.MarketService.fetch_15m_sensor_metrics", return_value=None)
def test_pipeline_backtester_runs_on_synthetic_data(_sensor, _ohlcv, _ticker, _gate):
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