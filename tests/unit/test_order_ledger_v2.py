"""Order ledger v2: per-order store, day stats parity, hot-path no full-history load."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder
from core.tenant_context import tenant_context
from services.order_service import OrderService
from storage.order_ledger_v2 import (
    MemoryOrderLedgerV2,
    display_day_key_for_order,
    display_day_key_now,
    reset_order_ledger_v2_for_tests,
    stats_from_filled_orders,
)


class TestOrderLedgerV2Pure(unittest.TestCase):
    def test_stats_from_filled_orders_aggregates(self):
        orders = [
            {"status": "filled", "side": "buy", "execution": {"usdt": 100}},
            {"status": "filled", "side": "sell", "execution": {"usdt": 50}, "pnl": 12.5},
            {"status": "filled", "side": "sell", "execution": {"usdt": 40}, "pnl": -2.5},
            {"status": "rejected", "side": "sell", "pnl": 99},
        ]
        s = stats_from_filled_orders(orders)
        self.assertEqual(s["buys"], 1)
        self.assertEqual(s["sells"], 2)
        self.assertAlmostEqual(s["realized_pnl"], 10.0)
        self.assertEqual(s["sell_wins"], 1)
        self.assertEqual(s["sell_losses"], 1)


class TestMemoryOrderLedgerV2(unittest.TestCase):
    def setUp(self):
        self.store = MemoryOrderLedgerV2()
        self.today = display_day_key_now()
        self.old_day = (
            datetime.now() - timedelta(days=30)
        ).strftime("%Y-%m-%d")

    def _filled(self, oid: str, side: str, *, day_key: str, pnl=None, seq=1, usdt=10.0):
        ts = f"{day_key}T12:00:00"
        return {
            "id": oid,
            "display_seq": seq,
            "status": "filled",
            "side": side,
            "symbol": "AAA/USDT",
            "tenant_id": "henry",
            "ledger_scope": "demo",
            "day_key": day_key,
            "execution": {"usdt": usdt, "price": 1.0, "amount": usdt},
            "pnl": pnl,
            "timestamps": {"created": ts, "filled": ts, "updated": ts},
        }

    def test_upsert_and_lookup_by_id_and_display_seq(self):
        rec = self._filled("abc", "buy", day_key=self.today, seq=7)
        self.store.upsert_order(rec)
        by_id = self.store.get_by_id("henry", "demo", "abc")
        by_seq = self.store.get_by_display_seq("henry", "demo", 7)
        self.assertIsNotNone(by_id)
        self.assertEqual(by_id["id"], "abc")
        self.assertEqual(by_seq["display_seq"], 7)

    def test_day_query_excludes_other_days(self):
        self.store.upsert_order(self._filled("old1", "buy", day_key=self.old_day, seq=1))
        self.store.upsert_order(self._filled("new1", "sell", day_key=self.today, seq=2, pnl=5.0))
        self.store.upsert_order(self._filled("new2", "buy", day_key=self.today, seq=3))
        today = self.store.query_day("henry", "demo", self.today, filled_only=True)
        ids = {o["id"] for o in today}
        self.assertEqual(ids, {"new1", "new2"})
        self.assertNotIn("old1", ids)

    def test_day_stats_parity_with_day_list(self):
        self.store.upsert_order(self._filled("b1", "buy", day_key=self.today, seq=1, usdt=100))
        self.store.upsert_order(
            self._filled("s1", "sell", day_key=self.today, seq=2, usdt=50, pnl=12.0)
        )
        self.store.upsert_order(
            self._filled("s2", "sell", day_key=self.today, seq=3, usdt=40, pnl=-3.0)
        )
        # Noise other day
        self.store.upsert_order(
            self._filled("old", "sell", day_key=self.old_day, seq=9, pnl=999)
        )
        day_list = self.store.query_day(
            "henry", "demo", self.today, filled_only=True, limit=100,
        )
        from_list = stats_from_filled_orders(day_list)
        stats = self.store.get_day_stats("henry", "demo", self.today)
        self.assertEqual(stats["buys"], from_list["buys"])
        self.assertEqual(stats["sells"], from_list["sells"])
        self.assertAlmostEqual(stats["realized_pnl"], from_list["realized_pnl"])
        self.assertEqual(stats["buys"], 1)
        self.assertEqual(stats["sells"], 2)
        self.assertAlmostEqual(stats["realized_pnl"], 9.0)

    def test_hot_path_never_increments_blob_loads(self):
        # Seed many historical + few today via day index only
        for i in range(200):
            day = (datetime.now() - timedelta(days=1 + (i % 60))).strftime("%Y-%m-%d")
            self.store.upsert_order(
                self._filled(f"hist{i}", "buy", day_key=day, seq=i + 1, usdt=1)
            )
        for i in range(3):
            self.store.upsert_order(
                self._filled(f"today{i}", "sell", day_key=self.today, seq=1000 + i, pnl=1.0)
            )
        before = self.store.full_blob_load_count()
        day = self.store.query_day("henry", "demo", self.today, filled_only=True)
        stats = self.store.get_day_stats("henry", "demo", self.today)
        after = self.store.full_blob_load_count()
        self.assertEqual(before, 0)
        self.assertEqual(after, 0)
        self.assertEqual(len(day), 3)
        self.assertEqual(stats["sells"], 3)

    def test_multi_tenant_isolation(self):
        self.store.upsert_order(self._filled("h1", "buy", day_key=self.today, seq=1))
        other = self._filled("d1", "buy", day_key=self.today, seq=1)
        other["tenant_id"] = "default"
        self.store.upsert_order(other)
        h = self.store.query_day("henry", "demo", self.today, filled_only=True)
        d = self.store.query_day("default", "demo", self.today, filled_only=True)
        self.assertEqual({o["id"] for o in h}, {"h1"})
        self.assertEqual({o["id"] for o in d}, {"d1"})


class TestOrderServiceV2DualWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        reset_order_ledger_v2_for_tests()
        os.environ["ORDER_LEDGER_V2"] = "1"
        os.environ["ORDER_LEDGER_V2_READS"] = "1"
        os.environ["ORDER_LEDGER_V2_BACKEND"] = "memory"
        self.scope_patch = patch("data_manager.ORDERS_SCOPE_FILES", {
            "demo": os.path.join(self.tmp.name, "orders.demo.json"),
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        })
        self.scope_patch.start()
        self.tenant_scope = patch(
            "services.order_service.resolve_tenant_scope", return_value="paper"
        )
        self.tenant_scope.start()

    def tearDown(self):
        self.tenant_scope.stop()
        self.scope_patch.stop()
        reset_order_ledger_v2_for_tests()
        for k in ("ORDER_LEDGER_V2", "ORDER_LEDGER_V2_READS", "ORDER_LEDGER_V2_BACKEND"):
            os.environ.pop(k, None)

    def test_create_update_read_via_v2_without_blob_for_day_list(self):
        with tenant_context("henry", scope="paper"):
            svc = OrderService("paper")
            # Seed many old orders only in blob (simulating pre-migration noise)
            data = svc._load()
            old = datetime.now() - timedelta(days=40)
            for i in range(150):
                data["orders"].append({
                    "id": f"blob_old_{i}",
                    "display_seq": i + 1,
                    "status": "filled",
                    "side": "buy",
                    "symbol": "OLD/USDT",
                    "ledger_scope": "paper",
                    "tenant_id": "henry",
                    "request": {"usdt": 1},
                    "execution": {"usdt": 1, "price": 1, "amount": 1},
                    "timestamps": {
                        "created": old.isoformat(),
                        "filled": old.isoformat(),
                        "updated": old.isoformat(),
                    },
                })
            svc._save(data)

            # New orders via OrderService dual-write
            rec = svc.create_from_request(
                TradeOrder("BUY", "SOL/USDT", 10.0, 0, usdt_amount=100),
                status="filled",
                telegram_token="v2buy1",
            )
            svc.update_status(
                "v2buy1",
                "filled",
                execution={"usdt": 100, "price": 10, "amount": 10},
            )
            sell = svc.create_from_request(
                TradeOrder("SELL", "SOL/USDT", 11.0, 5, signal="SELL_FULL"),
                status="filled",
                telegram_token="v2sell1",
            )
            svc.update_status(
                "v2sell1",
                "filled",
                execution={"usdt": 55, "price": 11, "amount": 5},
                pnl=5.0,
            )

            # Instrument: if day path used blob, it would include 150 old — v2 only today
            day = svc.list_day_filled_all()
            ids = {o["id"] for o in day}
            self.assertIn("v2buy1", ids)
            self.assertIn("v2sell1", ids)
            self.assertTrue(all(not i.startswith("blob_old_") for i in ids))

            stats = svc.stats_day_filled()
            from_list = OrderService.stats_from_filled_orders(day)
            self.assertEqual(stats["buys"], from_list["buys"])
            self.assertEqual(stats["sells"], from_list["sells"])
            self.assertAlmostEqual(stats["realized_pnl"], from_list["realized_pnl"])

            # Lookup by display_seq uses v2
            found = svc.get_by_display_seq(int(rec["display_seq"]))
            self.assertIsNotNone(found)
            self.assertEqual(found["id"], "v2buy1")

    def test_hot_path_blob_load_count_stays_zero_on_v2_day_query(self):
        from storage.order_ledger_v2 import get_order_ledger_v2

        with tenant_context("henry", scope="paper"):
            store = get_order_ledger_v2()
            self.assertIsInstance(store, MemoryOrderLedgerV2)
            svc = OrderService("paper")
            for i in range(5):
                tok = f"hot{i}"
                svc.create_from_request(
                    TradeOrder("BUY", "X/USDT", 1.0, 0, usdt_amount=10),
                    status="filled",
                    telegram_token=tok,
                )
                svc.update_status(
                    tok, "filled",
                    execution={"usdt": 10, "price": 1, "amount": 10},
                )
            before = store.full_blob_load_count()
            day = svc.list_day_filled_all()
            stats = svc.stats_day_filled()
            after = store.full_blob_load_count()
            self.assertEqual(before, after)
            self.assertEqual(after, 0)
            self.assertGreaterEqual(len(day), 5)
            self.assertEqual(
                stats["buys"],
                len([o for o in day if (o.get("side") or "").lower() == "buy"]),
            )


if __name__ == "__main__":
    unittest.main()
