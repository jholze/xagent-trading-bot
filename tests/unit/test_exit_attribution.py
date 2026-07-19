"""exit_source resolution + persistence on sell orders."""

from __future__ import annotations

import unittest

from core.models import TradeOrder
from services.order_service import OrderService
from strategies.exit_attribution import resolve_exit_source, truncate_rationale


class TestResolveExitSource(unittest.TestCase):
    def test_prefers_sell_source(self):
        self.assertEqual(
            resolve_exit_source(sell_source="trailing_stop", sources=["technical", "grid"]),
            "trailing_stop",
        )

    def test_priority_over_generic(self):
        self.assertEqual(
            resolve_exit_source(sources=["multi_source", "stop_loss", "technical"]),
            "stop_loss",
        )

    def test_structure_source(self):
        self.assertEqual(
            resolve_exit_source(sources=["bb_upper", "technical"]),
            "bb_upper",
        )

    def test_ignores_channel_only(self):
        self.assertEqual(
            resolve_exit_source(sell_source="auto", sources=["auto"], action="SELL_FULL"),
            "unknown_sell",
        )
        self.assertEqual(
            resolve_exit_source(sell_source="auto", sources=["auto", "multi_source"], action="SELL_PARTIAL_50"),
            "unknown_sell",
        )

    def test_truncate_rationale(self):
        long = "a" * 300
        out = truncate_rationale(long, max_len=40)
        self.assertEqual(len(out), 40)
        self.assertTrue(out.endswith("…"))


class TestOrderPersistsExitSource(unittest.TestCase):
    def test_create_and_reject_store_exit_fields(self):
        svc = OrderService("paper")
        order = TradeOrder(
            "SELL",
            "LAB/USDT",
            0.15,
            100,
            signal="SELL_PARTIAL_50",
            source="auto",
            exit_source="time_profit_exit",
            exit_rationale="Time->profit exit (48h held, gain=2.1%, sell 50%)",
        )
        rec = svc.create_from_request(order, status="executing", telegram_token="ex1")
        self.assertEqual(rec["exit_source"], "time_profit_exit")
        self.assertIn("Time->profit", rec["exit_rationale"])

        from core.models import RiskDecision

        rej = svc.record_rejected(
            order,
            RiskDecision(approved=False, message="cooldown", code="trade_cooldown"),
        )
        self.assertEqual(rej["exit_source"], "time_profit_exit")
        self.assertEqual(rej["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
