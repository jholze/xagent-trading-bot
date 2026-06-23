import unittest

from core.models import MarketContext
from strategies.technical_rsi_bb import TechnicalRSIStrategy


class TestTakeProfitTiers(unittest.TestCase):
    def setUp(self):
        self.symbol = "PUMP/USDT"
        self.tf = "1h"

    def test_fires_lowest_undone_tier(self):
        strategy = TechnicalRSIStrategy()
        params = {
            "take_profit_tiers": [40, 80, 120],
            "stop_loss_pct": 50,
        }
        market = MarketContext(
            symbol=self.symbol,
            timeframe=self.tf,
            current_price=1.5,
            rsi=55.0,
            lower_bb=1.0,
            has_position=True,
            average_entry=1.0,
            strategy_params=params,
            sim_state={"rsi_sell_tiers_done": {}, "last_rsi": 50},
        )
        coin = {"symbol": self.symbol, "timeframe": self.tf, "strategy_params": params}
        result = strategy.analyze(coin, market)
        self.assertEqual(result.normalized_action, "SELL_PARTIAL_30")
        self.assertIn("take_profit_40", result.sources)

    def test_skips_completed_tier(self):
        strategy = TechnicalRSIStrategy()
        params = {
            "take_profit_tiers": [40, 80, 120],
            "stop_loss_pct": 50,
        }
        market = MarketContext(
            symbol=self.symbol,
            timeframe=self.tf,
            current_price=1.9,
            rsi=55.0,
            lower_bb=1.0,
            has_position=True,
            average_entry=1.0,
            strategy_params=params,
            sim_state={"rsi_sell_tiers_done": {"tp40": True}, "last_rsi": 50},
        )
        coin = {"symbol": self.symbol, "timeframe": self.tf, "strategy_params": params}
        result = strategy.analyze(coin, market)
        self.assertEqual(result.normalized_action, "SELL_PARTIAL_30")
        self.assertIn("take_profit_80", result.sources)

if __name__ == "__main__":
    unittest.main()