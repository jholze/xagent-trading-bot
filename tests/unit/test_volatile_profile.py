import unittest
from unittest.mock import MagicMock, patch

from core.models import MarketContext, SignalAnalysis
from strategies.decision_engine import DecisionEngine
from strategies.registry import resolve_strategy_params
from strategies.technical_rsi_bb import TechnicalRSIStrategy


class TestVolatileProfile(unittest.TestCase):
    def test_resolve_volatile_profile_for_h_position(self):
        coin = {"symbol": "H/USDT", "timeframe": "4h", "source": "dry_run_expansion"}
        params = resolve_strategy_params(coin, has_position=True, atr_pct=49.0, frozen_tier="volatile")
        self.assertEqual(params.get("strategy_profile"), "volatile_altcoin")
        self.assertEqual(params.get("rsi_sell_30"), 67)
        self.assertIsNone(params.get("take_profit_pct"))

    def test_eth_explicit_not_overridden(self):
        coin = {"symbol": "ETH/USDT", "timeframe": "4h"}
        with patch("strategies.registry._explicit_strategy_entry", return_value={"symbol": "ETH/USDT", "rsi_sell_30": 70}):
            params = resolve_strategy_params(coin, has_position=True, atr_pct=1.7, frozen_tier="volatile")
        self.assertEqual(params.get("rsi_sell_30"), 70)
        self.assertNotEqual(params.get("strategy_profile"), "volatile_altcoin")

    def test_rsi_level_sell_with_min_gain(self):
        strategy = TechnicalRSIStrategy()
        params = {
            "rsi_sell_mode": "level",
            "rsi_sell_30": 67,
            "rsi_sell_20": 75,
            "rsi_sell_min_gain_pct": 15,
            "stop_loss_pct": 50,
        }
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.58,
            rsi=68.0,
            lower_bb=0.5,
            has_position=True,
            average_entry=0.28,
            strategy_params=params,
            sim_state={"rsi_sell_tiers_done": {}, "last_rsi": 60},
        )
        coin = {"symbol": "H/USDT", "timeframe": "4h", "strategy_params": params}
        result = strategy.analyze(coin, market)
        self.assertEqual(result.normalized_action, "SELL_PARTIAL_30")

    def test_rsi_level_blocked_by_low_gain(self):
        strategy = TechnicalRSIStrategy()
        params = {
            "rsi_sell_mode": "level",
            "rsi_sell_30": 67,
            "rsi_sell_min_gain_pct": 15,
            "stop_loss_pct": 50,
        }
        market = MarketContext(
            symbol="WLD/USDT",
            timeframe="4h",
            current_price=0.60,
            rsi=74.0,
            lower_bb=0.55,
            has_position=True,
            average_entry=0.595,
            strategy_params=params,
            sim_state={"rsi_sell_tiers_done": {}, "last_rsi": 60},
        )
        coin = {"symbol": "WLD/USDT", "timeframe": "4h", "strategy_params": params}
        result = strategy.analyze(coin, market)
        self.assertEqual(result.normalized_action, "HOLD")

    def test_shadow_mode_blocks_execution(self):
        engine = DecisionEngine(market_service=MagicMock())
        technical = SignalAnalysis(
            action="HOLD",
            symbol="H/USDT",
            timeframe="4h",
            rsi=68.0,
            lower_bb=0.5,
            vol_multiplier=0.7,
            ampel_emoji="🟡",
            ampel_text="Neutral",
            sources=["technical"],
            normalized_action="HOLD",
        )
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.636,
            rsi=68.0,
            lower_bb=0.5,
            upper_bb=0.627,
            has_position=True,
            average_entry=0.28,
            strategy_params={"strategy_profile": "volatile_altcoin"},
        )
        pos = {"rsi_sell_tiers_done": {}, "recent_high": 0.636}
        with patch.object(
            type(engine.config),
            "volatile_altcoin_config",
            property(lambda self: {"mode": "shadow"}),
        ):
            normalized, action, shadow = engine._apply_shadow_mode("SELL_PARTIAL_30", "SELL_30", market.strategy_params)
        self.assertEqual(normalized, "HOLD")
        self.assertEqual(action, "HOLD")
        self.assertEqual(shadow, "SELL_30")