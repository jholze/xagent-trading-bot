"""OrderService read cache must not leak across tenants."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import services.order_service as order_service
from core.tenant_context import tenant_context
from services.order_service import OrderService


class TestOrderTenantCache(unittest.TestCase):
    def setUp(self):
        order_service._ORDERS_READ_CACHE.clear()
        self.load_calls: list[str] = []

    def tearDown(self):
        order_service._ORDERS_READ_CACHE.clear()

    def _fake_load(self, scope):
        from core.tenant_context import resolve_tenant_id

        tid = resolve_tenant_id()
        self.load_calls.append(tid)
        return {"ledger_scope": scope, "orders": [{"symbol": f"{tid}/USDT"}]}

    def test_cache_isolated_per_tenant(self):
        with patch("services.order_service.load_orders", side_effect=self._fake_load):
            with patch("services.order_service.resolve_ledger_scope", return_value="paper"):
                with tenant_context("henry", scope="paper"):
                    a = OrderService()._load()
                    self.assertEqual(a["orders"][0]["symbol"], "henry/USDT")
                with tenant_context("default", scope="paper"):
                    b = OrderService()._load()
                    self.assertEqual(b["orders"][0]["symbol"], "default/USDT")
                with tenant_context("henry", scope="paper"):
                    c = OrderService()._load()
                    self.assertEqual(c["orders"][0]["symbol"], "henry/USDT")
        # Third henry read must hit cache, not Mongo/JSON again.
        self.assertEqual(self.load_calls, ["henry", "default"])


class TestTradeIntentTenantPropagation(unittest.TestCase):
    def test_submit_captures_tenant_on_intent(self):
        from bus.trade_intents import trade_intent_queue
        from core.models import TradeOrder
        from services import trading_engine_runtime as ter
        from services.trading_engine_runtime import submit_trade_intent

        trade_intent_queue.stop()
        trade_intent_queue._running = False
        ter._started = False
        captured: list = []

        def _capture(intent):
            captured.append(intent)
            intent.set_result(
                __import__("core.models", fromlist=["TradeResult"]).TradeResult(
                    True, intent.order.type, intent.order.symbol
                )
            )

        with patch.object(trade_intent_queue, "submit", side_effect=_capture):
            order = TradeOrder(type="BUY", symbol="BTC/USDT", price=1.0, amount=0, usdt_amount=10)
            with tenant_context("henry", scope="paper", owner_chat_id="999"):
                submit_trade_intent(order, "4h", source="auto")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].tenant_id, "henry")
        self.assertEqual(captured[0].owner_chat_id, "999")
        self.assertEqual(captured[0].scope, "paper")


if __name__ == "__main__":
    unittest.main()