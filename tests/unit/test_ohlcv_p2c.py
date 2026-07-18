"""PR-P2c: Decision+Regime share one OHLCV fetch (issue #66)."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import MarketContext, SignalAnalysis
from strategies.decision_engine import DecisionEngine
from strategies.positions import clear_positions_memory


def _ohlcv_df(n: int = 80) -> pd.DataFrame:
    rows = []
    for i in range(n):
        price = 100.0 + i * 0.1
        rows.append(
            {
                "ts": 1_700_000_000_000 + i * 3_600_000,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 1000.0 + i,
            }
        )
    df = pd.DataFrame(rows)
    import talib

    df["rsi"] = talib.RSI(df["close"], timeperiod=14)
    df["upper"], df["middle"], df["lower"] = talib.BBANDS(df["close"], timeperiod=20)
    df["vol_avg"] = df["volume"].rolling(window=20).mean()
    return df


def _hold_analysis(symbol: str = "P2C/USDT") -> SignalAnalysis:
    return SignalAnalysis(
        action="HOLD",
        symbol=symbol,
        timeframe="4h",
        rsi=45.0,
        lower_bb=97.0,
        vol_multiplier=1.0,
        ampel_emoji="🟡",
        ampel_text="neutral",
        should_notify=False,
        normalized_action="HOLD",
    )


class TestP2cDecisionSingleOhlcvFetch(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()

    def _engine(self, fetch_calls: list):
        df = _ohlcv_df(120)
        engine = DecisionEngine(market_service=MagicMock())
        engine.config = MagicMock()
        engine.config.raw = {
            "regime_detector": {
                "enabled": True,
                "tech_weight": 0.6,
                "sentiment_weight": 0.4,
            },
            "strategy_allocator": {"enabled": False},
        }
        engine.config.regime_detector_config = engine.config.raw["regime_detector"]
        engine.config.volatile_altcoin_config = {}
        engine.config.max_open_positions = 10

        def fake_fetch_ohlcv(symbol, timeframe, limit=100):
            fetch_calls.append((symbol, timeframe, int(limit)))
            return df.copy()

        def fake_indicators_from_df(df_in, timeframe, current_price, symbol=""):
            return {
                "rsi": 45.0,
                "lower_bb": current_price * 0.97,
                "middle_bb": current_price,
                "upper_bb": current_price * 1.03,
                "vol_multiplier": 1.0,
                "atr": current_price * 0.03,
                "atr_pct": 3.0,
                "range_24h_pct": 5.0,
                "change_24h_pct": 1.0,
            }

        def fake_fetch_ohlcv_and_indicators(symbol, timeframe, current_price, limit=100):
            d = fake_fetch_ohlcv(symbol, timeframe, limit)
            return d, fake_indicators_from_df(d, timeframe, current_price, symbol=symbol)

        engine.market.fetch_ohlcv = fake_fetch_ohlcv
        engine.market.fetch_ohlcv_and_indicators = fake_fetch_ohlcv_and_indicators
        engine.market.indicators_from_df = fake_indicators_from_df
        engine.market.fetch_funding_rate = MagicMock(return_value=None)
        engine.market.btc_underperformance_ratio = MagicMock(return_value=None)
        return engine, df

    def test_build_market_context_uses_limit_300_when_regime_on(self):
        fetch_calls: list = []
        engine, _ = self._engine(fetch_calls)
        coin = {"symbol": "P2C/USDT", "timeframe": "4h", "strategy_params": {}}

        with patch(
            "intelligence.strategy_backtest.classify_coin",
            return_value="large_cap",
        ), patch(
            "strategies.decision_engine.resolve_coin_config",
            side_effect=lambda c, **kw: dict(c),
        ), patch(
            "strategies.decision_engine.resolve_strategy_params",
            return_value={},
        ), patch(
            "strategies.decision_engine.resolve_effective_timeframe",
            return_value="4h",
        ):
            ctx = engine.build_market_context(coin, 100.0)

        self.assertEqual(fetch_calls, [("P2C/USDT", "4h", 300)])
        self.assertIsNotNone(ctx.ohlcv_df)
        self.assertFalse(ctx.ohlcv_df.empty)

    def test_regime_reuses_market_ohlcv_df(self):
        fetch_calls: list = []
        engine, df = self._engine(fetch_calls)
        market = MarketContext(
            symbol="P2C/USDT",
            timeframe="4h",
            current_price=100.0,
            rsi=45.0,
            lower_bb=97.0,
            middle_bb=100.0,
            upper_bb=103.0,
            atr_pct=3.0,
            vol_multiplier=1.0,
            has_position=False,
            strategy_params={},
            ohlcv_df=df,
        )
        coin = {"symbol": "P2C/USDT", "timeframe": "4h", "strategy_params": {}}
        strat = MagicMock()
        strat.analyze.return_value = _hold_analysis()

        with patch(
            "strategies.decision_engine.resolve_coin_config",
            side_effect=lambda c, **kw: dict(c),
        ), patch(
            "strategies.decision_engine.resolve_strategy_params",
            return_value={},
        ), patch(
            "strategies.decision_engine.get_strategy",
            return_value=strat,
        ), patch(
            "strategies.decision_engine.evaluate_trailing_stop",
            return_value=None,
        ), patch(
            "strategies.decision_engine.evaluate_trailing_take_profit",
            return_value=None,
        ), patch(
            "strategies.decision_engine.evaluate_time_profit_exit",
            return_value=None,
        ), patch(
            "strategies.decision_engine.evaluate_profit_max_lifetime",
            return_value=None,
        ), patch(
            "strategies.decision_engine.evaluate_dca_addon",
            return_value=None,
        ), patch(
            "strategies.decision_engine.evaluate_market_structure_sells",
            return_value=[],
        ), patch(
            "strategies.decision_engine.evaluate_market_structure_buy_boost",
            return_value=None,
        ), patch(
            "strategies.decision_engine.evaluate_exit_sensor_sells",
            return_value=[],
        ), patch(
            "strategies.decision_engine.evaluate_entry_sensor_15m",
            return_value=None,
        ), patch(
            "strategies.decision_engine.sync_profit_armed_at",
        ), patch(
            "strategies.decision_engine.apply_rotation_sell_filters",
            side_effect=lambda action, *a, **k: (action, {}),
        ), patch(
            "strategies.decision_engine.filter_sell_candidates",
            side_effect=lambda candidates, *a, **k: candidates,
        ), patch(
            "strategies.decision_engine.is_fresh_guarded_entry",
            return_value=False,
        ):
            out = engine.evaluate_with_market(coin, market, [], [], [])

        self.assertEqual(fetch_calls, [], f"regime must reuse ohlcv_df; got {fetch_calls}")
        self.assertIsNotNone(out)
        self.assertEqual(out.action, "HOLD")
        self.assertTrue(bool(getattr(market, "regime", None) or out.regime))


if __name__ == "__main__":
    unittest.main()
