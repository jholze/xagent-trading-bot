import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import RiskDecision, TradeOrder, TradeResult
from services.order_service import OrderService, format_order_line, ledger_label


class TestOrderService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope_patch = patch("data_manager.ORDERS_SCOPE_FILES", {
            "demo": os.path.join(self.tmp.name, "orders.demo.json"),
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        })
        self.scope_patch.start()
        self.scope = patch("services.order_service.resolve_ledger_scope", return_value="paper")
        self.scope.start()

    def tearDown(self):
        self.scope.stop()
        self.scope_patch.stop()

    def test_create_and_list_orders(self):
        svc = OrderService("paper")
        order = TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=100)
        record = svc.create_from_request(order, telegram_token="tok123")
        self.assertEqual(record["status"], "pending_confirmation")
        self.assertEqual(record["id"], "tok123")
        self.assertEqual(record["display_seq"], 1)

        orders, pages = svc.list_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(pages, 1)

    def test_record_rejected_and_stats(self):
        svc = OrderService("paper")
        order = TradeOrder("BUY", "SOL/USDT", 100, 0, usdt_amount=50)
        decision = RiskDecision(approved=False, message="Max positions", code="max_open_positions", order=order)
        svc.record_rejected(order, decision)
        stats = svc.stats_24h()
        self.assertEqual(stats["rejected"], 1)

    def test_link_execution_result_filled(self):
        svc = OrderService("paper")
        order = TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=100)
        created = svc.create_from_request(order, status="executing", telegram_token="exec1")
        result = TradeResult(True, "BUY", "ARIA/USDT", amount=2000, price=0.05, usdt_amount=100)
        svc.link_execution_result(created["id"], result)
        updated = svc.get_by_id("exec1")
        self.assertEqual(updated["status"], "filled")
        self.assertEqual(updated["execution"]["usdt"], 100)

    def test_expire_stale_pending(self):
        svc = OrderService("paper")
        order = TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=100)
        record = svc.create_from_request(order, telegram_token="old1")
        data = svc._load()
        record = svc._find(data, order_id="old1")
        record["timestamps"]["created"] = (datetime.now() - timedelta(minutes=15)).isoformat()
        svc._save(data)
        self.assertEqual(svc.expire_stale_pending(), 1)
        self.assertEqual(svc.get_by_id("old1")["status"], "expired")

    def test_scope_isolation(self):
        paper = OrderService("paper")
        live = OrderService("live")
        paper.create_from_request(TradeOrder("BUY", "A/USDT", 1, 0, usdt_amount=10), telegram_token="p1")
        live.create_from_request(TradeOrder("SELL", "B/USDT", 2, 1, signal="SELL"), telegram_token="l1")
        p_orders, _ = paper.list_orders()
        l_orders, _ = live.list_orders()
        self.assertEqual(len(p_orders), 1)
        self.assertEqual(p_orders[0]["symbol"], "A/USDT")
        self.assertEqual(len(l_orders), 1)
        self.assertEqual(l_orders[0]["symbol"], "B/USDT")

    def test_format_order_line(self):
        line = format_order_line({
            "status": "filled", "display_seq": 3, "side": "buy",
            "symbol": "ARIA/USDT", "source": "manual",
            "request": {"usdt": 200}, "execution": {"usdt": 200},
        })
        self.assertIn("#3", line)
        self.assertIn("ARIA", line)

    def test_ledger_label(self):
        self.assertEqual(ledger_label("demo"), "DEMO")
        self.assertEqual(ledger_label("paper"), "PAPER")
        self.assertEqual(ledger_label("live"), "GATE/LIVE")


if __name__ == "__main__":
    unittest.main()