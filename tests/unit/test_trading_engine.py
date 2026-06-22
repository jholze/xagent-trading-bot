import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from bus.locks import LedgerLock
from bus.trade_intents import TradeIntent, make_idempotency_key, trade_intent_queue
from core.models import TradeOrder, TradeResult
from services.order_service import OrderService
from services.trading_service import TradingService


class TestLedgerLock(unittest.TestCase):
    def test_thread_lock_serializes(self):
        order = []

        def work():
            with LedgerLock("paper", enabled=False):
                order.append(1)
                order.append(2)

        import threading

        threads = [threading.Thread(target=work) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(order), 6)


class TestIdempotency(unittest.TestCase):
    def setUp(self):
        self.orders = {"orders": [], "ledger_scope": "paper"}
        self.save = patch("services.order_service.save_orders", side_effect=self._save)
        self.load = patch("services.order_service.load_orders", return_value=self.orders)
        self.scope = patch("services.order_service.resolve_ledger_scope", return_value="paper")
        self.save.start()
        self.load.start()
        self.scope.start()

    def tearDown(self):
        self.save.stop()
        self.load.stop()
        self.scope.stop()

    def _save(self, data, scope=None):
        self.orders.update(data)
        return True

    def test_find_by_idempotency_key(self):
        svc = OrderService(scope="paper")
        order = TradeOrder(type="BUY", symbol="H/USDT", price=1.0, amount=0, usdt_amount=25)
        created = svc.create_from_request(order, idempotency_key="idem-1")
        found = svc.find_by_idempotency_key("idem-1")
        self.assertEqual(found["id"], created["id"])


class TestTradeIntentQueue(unittest.TestCase):
    def test_submit_and_wait(self):
        trade_intent_queue._running = False
        trade_intent_queue._queue.queue.clear()

        def executor(intent: TradeIntent) -> TradeResult:
            return TradeResult(True, intent.order.type, intent.order.symbol, order_id="x1")

        trade_intent_queue.start(executor)
        order = TradeOrder(type="BUY", symbol="BTC/USDT", price=1.0, amount=0, usdt_amount=10)
        intent = TradeIntent(
            intent_id="i1",
            idempotency_key=make_idempotency_key("BTC/USDT", "4h", "BUY", "auto", "paper", bucket="2026062212"),
            scope="paper",
            order=order,
            timeframe="4h",
            source="auto",
        )
        trade_intent_queue.submit(intent)
        result = intent.wait(timeout=3)
        self.assertTrue(result.executed)
        trade_intent_queue.stop()


class TestTradingServiceIdempotent(unittest.TestCase):
    def test_duplicate_idempotency_returns_prior(self):
        svc = TradingService()
        prior = {
            "id": "ord1",
            "status": "filled",
            "side": "buy",
            "symbol": "H/USDT",
            "execution": {"amount": 10, "price": 1.0, "usdt": 10},
            "pnl": 0,
        }
        with patch.object(OrderService, "find_by_idempotency_key", return_value=prior):
            order = TradeOrder(type="BUY", symbol="H/USDT", price=1.0, amount=0, usdt_amount=10)
            result = svc._execute_order_locked(
                order, "4h", source="auto", idempotency_key="dup-key", _lock_held=True
            )
        self.assertTrue(result.executed)
        self.assertEqual(result.order_id, "ord1")


if __name__ == "__main__":
    unittest.main()