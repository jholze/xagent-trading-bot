import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.actions import BUY_DCA
from core.models import MarketContext
from strategies.dca import DCACandidate
from strategies.dca_portfolio import (
    _target_priority,
    build_portfolio_dca_plan,
    find_funding_sell,
    portfolio_config,
)
from strategies.positions import get_key, get_position, positions, update_position


class TestDCAPortfolio(unittest.TestCase):
    def setUp(self):
        self._backup = dict(positions)
        positions.clear()

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def test_target_priority_favors_higher_score(self):
        hi = DCACandidate(BUY_DCA, "dca", "x", 200, score=9)
        lo = DCACandidate(BUY_DCA, "dca", "x", 200, score=6)
        self.assertGreater(_target_priority(hi, -10), _target_priority(lo, -10))

    def test_find_funding_sell_tail_idle(self):
        update_position("TAIL/USDT", "4h", "BUY", 1.0, 500)
        tail = get_position("TAIL/USDT", "4h")
        tail["sold_percent"] = 0.6
        tail["average_entry"] = 1.0
        tail["last_trade_at"] = (datetime.now() - timedelta(hours=30)).isoformat()
        tail["last_trade_type"] = "SELL"

        update_position("TARGET/USDT", "4h", "BUY", 1.0, 400)
        tgt = get_position("TARGET/USDT", "4h")
        tgt["average_entry"] = 1.0

        from strategies.dca_portfolio import DCATarget

        target = DCATarget(
            symbol="TARGET/USDT",
            timeframe="4h",
            source="dca",
            candidate=DCACandidate(BUY_DCA, "dca", "dip", 350, score=8),
            priority=10,
            usdt_needed=350,
            loss_pct=-8,
            score=8,
        )
        coins = [{"symbol": "TAIL/USDT"}, {"symbol": "TARGET/USDT"}]
        price_map = {"TAIL/USDT": 1.05, "TARGET/USDT": 0.92}

        with patch("strategies.dca_portfolio._build_market") as mock_market:
            mock_market.side_effect = lambda sym, tf, price, pos, sp: MarketContext(
                symbol=sym,
                timeframe=tf,
                current_price=price,
                rsi=45,
                lower_bb=price * 0.9,
                atr_pct=3,
                has_position=True,
                average_entry=float(pos.get("average_entry", price)),
                open_positions=1,
                strategy_params=sp or {},
            )
            funding = find_funding_sell(
                target, coins, price_map, cash_available=50, cash_needed=350, config_raw={},
            )
        self.assertIsNotNone(funding)
        self.assertEqual(funding.symbol, "TAIL/USDT")

    def test_portfolio_config_defaults(self):
        cfg = portfolio_config({})
        self.assertIn("enabled", cfg)
        self.assertIn("cash_buffer_usdt", cfg)


if __name__ == "__main__":
    unittest.main()