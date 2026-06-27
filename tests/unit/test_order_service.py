import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import RiskDecision, TradeOrder, TradeResult
from services.order_service import (
    OrderService,
    format_order_detail,
    format_order_line,
    infer_manual_source,
    ledger_label,
    source_label,
)


class TestOrderService(unittest.TestCase):
    def setUp(self):
        from services import order_service

        order_service._ORDERS_READ_CACHE.clear()
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
        from services import order_service

        self.scope.stop()
        self.scope_patch.stop()
        order_service._ORDERS_READ_CACHE.clear()

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

    def test_stats_executed_24h_counts_buys_and_sells(self):
        svc = OrderService("paper")
        buy = TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=100)
        created = svc.create_from_request(buy, status="executing", telegram_token="b1")
        svc.link_execution_result(created["id"], TradeResult(True, "BUY", "ARIA/USDT", amount=2000, price=0.05, usdt_amount=100))
        sell = TradeOrder("SELL", "SOL/USDT", 70, 2, signal="SELL")
        created_s = svc.create_from_request(sell, status="executing", telegram_token="s1")
        svc.link_execution_result(created_s["id"], TradeResult(True, "SELL", "SOL/USDT", amount=2, price=70, usdt_amount=140, pnl=5))

        stats = svc.stats_executed_24h()
        self.assertEqual(stats["filled"], 2)
        self.assertEqual(stats["buys"], 1)
        self.assertEqual(stats["sells"], 1)

    def test_trade_book_only_hides_rejected(self):
        svc = OrderService("paper")
        buy = TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=100)
        created = svc.create_from_request(buy, status="executing", telegram_token="filled1")
        svc.link_execution_result(created["id"], TradeResult(True, "BUY", "ARIA/USDT", amount=2000, price=0.05, usdt_amount=100))
        reject = TradeOrder("SELL", "SOL/USDT", 70, 2, signal="SELL")
        svc.record_rejected(reject, RiskDecision(approved=False, message="Cooldown", code="trade_cooldown", order=reject))

        all_orders, _ = svc.list_orders()
        book_orders, _ = svc.list_orders(trade_book_only=True)
        self.assertEqual(len(all_orders), 2)
        self.assertEqual(len(book_orders), 1)
        self.assertEqual(book_orders[0]["status"], "filled")

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

    def test_format_order_line_includes_trade_date(self):
        line = format_order_line({
            "status": "filled", "display_seq": 3, "side": "buy",
            "symbol": "ARIA/USDT", "source": "manual",
            "request": {"usdt": 200}, "execution": {"usdt": 200},
            "timestamps": {"created": "2026-06-07T19:16:08", "filled": "2026-06-07T19:16:08"},
        })
        self.assertIn("#3", line)
        self.assertIn("ARIA", line)
        self.assertIn("07.06.2026 19:16", line)

    def test_format_order_detail_shows_buy_date_label(self):
        detail = format_order_detail({
            "display_seq": 1, "status": "filled", "side": "buy",
            "symbol": "ARIA/USDT", "source": "manual", "ledger_scope": "paper",
            "request": {"price": 0.05, "usdt": 200},
            "risk": {}, "execution": {"usdt": 200, "price": 0.05, "amount": 4000},
            "timestamps": {"created": "2026-06-07T19:16:08", "filled": "2026-06-07T19:16:08"},
        })
        self.assertIn("Kaufdatum", detail)
        self.assertIn("07.06.2026 19:16", detail)

    def test_format_order_detail_shows_sell_date_label(self):
        detail = format_order_detail({
            "display_seq": 2, "status": "filled", "side": "sell",
            "symbol": "SOL/USDT", "source": "manual", "ledger_scope": "paper",
            "request": {"price": 70, "amount": 2},
            "risk": {}, "execution": {"usdt": 140, "price": 70, "amount": 2},
            "pnl": 3.5,
            "timestamps": {"created": "2026-06-08T10:30:00", "filled": "2026-06-08T10:30:00"},
        })
        self.assertIn("Verkaufdatum", detail)
        self.assertIn("08.06.2026 10:30", detail)

    def test_ledger_label(self):
        self.assertEqual(ledger_label("demo"), "DEMO")
        self.assertEqual(ledger_label("paper"), "PAPER")
        self.assertEqual(ledger_label("live"), "GATE/LIVE")

    def test_source_label_manual(self):
        self.assertEqual(source_label("manual"), "Manuell")
        line = format_order_line({
            "status": "filled", "display_seq": 1, "side": "buy",
            "symbol": "CAT/USDT", "source": "manual",
            "request": {"usdt": 500}, "execution": {"usdt": 500},
            "timestamps": {"filled": "2026-06-12T15:35:11"},
        })
        self.assertIn("Manuell", line)

    def test_infer_manual_source_heuristic(self):
        self.assertEqual(infer_manual_source({"side": "buy", "signal": "", "source": "auto"}), "manual")
        self.assertEqual(infer_manual_source({"side": "sell", "signal": "SELL", "source": "auto"}), "manual")
        self.assertIsNone(infer_manual_source({"side": "sell", "signal": "SELL_FULL", "source": "auto"}))

    def test_reconcile_legacy_manual_sources(self):
        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("BUY", "CAT/USDT", 1.5e-06, 0, usdt_amount=500),
            status="filled",
            telegram_token="manual1",
        )
        data = svc._load()
        data["orders"][0]["source"] = "auto"
        svc._save(data)
        self.assertEqual(svc.reconcile_legacy_sources(), 1)
        self.assertEqual(svc.get_by_id("manual1")["source"], "manual")

    def test_link_execution_result_updates_source(self):
        svc = OrderService("paper")
        created = svc.create_from_request(
            TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=100),
            status="pending_confirmation",
            telegram_token="exec2",
        )
        approved = TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=100, source="manual", order_id="exec2")
        result = TradeResult(True, "BUY", "ARIA/USDT", amount=2000, price=0.05, usdt_amount=100)
        svc.link_execution_result("exec2", result, approved)
        self.assertEqual(svc.get_by_id("exec2")["source"], "manual")


if __name__ == "__main__":
    unittest.main()