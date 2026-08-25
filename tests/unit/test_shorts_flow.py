"""P0 shorts: lot side + paper execute_order does not treat SHORT as SELL."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.models import TradeOrder
from services.portfolio_service import PortfolioService
from strategies.positions import (
    clear_positions_memory,
    get_position,
    update_position,
)
from strategies.short_math import is_short, unrealized_pnl


class TestShortsFlow(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()

    def tearDown(self):
        clear_positions_memory()

    def test_update_position_short_then_cover(self):
        update_position("AAA/USDT", "4h", "SHORT", 2.0, 10, leverage=2)
        pos = get_position("AAA/USDT", "4h")
        self.assertTrue(is_short(pos))
        self.assertAlmostEqual(float(pos["amount"]), 10)
        self.assertEqual(float(pos["leverage"]), 2)
        update_position("AAA/USDT", "4h", "COVER", 1.5, 10)
        pos = get_position("AAA/USDT", "4h")
        self.assertFalse(is_short(pos) and float(pos["amount"]) > 0)
        self.assertAlmostEqual(unrealized_pnl("short", 10, 2.0, 1.5), 5.0)

    def test_execute_order_unknown_type_not_sell(self):
        svc = PortfolioService()
        with patch.object(svc, "execute_sell") as sell:
            out = svc.execute_order(TradeOrder(type="NOPE", symbol="X/USDT", price=1, amount=1))
        self.assertFalse(out.executed)
        sell.assert_not_called()

    def test_one_way_short_on_long_rejected_by_risk(self):
        from risk.risk_manager import RiskManager

        update_position("BBB/USDT", "4h", "BUY", 1.0, 5)
        rm = RiskManager()
        with patch.object(rm, "_available_usdt", return_value=10_000):
            dec = rm.evaluate(
                TradeOrder(type="SHORT", symbol="BBB/USDT", price=1.0, amount=0, usdt_amount=100),
                "4h",
                source="manual",
            )
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "one_way")
