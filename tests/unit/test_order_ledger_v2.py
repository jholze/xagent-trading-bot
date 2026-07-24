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
from services.order_service import OrderService, blob_load_count, reset_blob_load_count
from storage.order_ledger_v2 import (
    MemoryOrderLedgerV2,
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
        self.old_day = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

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
        self.store.upsert_order(
            self._filled("new1", "sell", day_key=self.today, seq=2, pnl=5.0)
        )
        self.store.upsert_order(self._filled("new2", "buy", day_key=self.today, seq=3))
        today = self.store.query_day("henry", "demo", self.today, filled_only=True)
        ids = {o["id"] for o in today}
        self.assertEqual(ids, {"new1", "new2"})
        self.assertNotIn("old1", ids)

    def test_day_stats_parity_with_day_list(self):
        """Fixture: buy 100 + sell +12.0 + sell -3.0 → realized 9.0 (matches list sum)."""
        self.store.upsert_order(
            self._filled("b1", "buy", day_key=self.today, seq=1, usdt=100)
        )
        self.store.upsert_order(
            self._filled("s1", "sell", day_key=self.today, seq=2, usdt=50, pnl=12.0)
        )
        self.store.upsert_order(
            self._filled("s2", "sell", day_key=self.today, seq=3, usdt=40, pnl=-3.0)
        )
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
        self.assertEqual({o["id"] for o in day_list}, {"b1", "s1", "s2"})

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
        reset_blob_load_count()
        os.environ["ORDER_LEDGER_V2"] = "1"
        os.environ["ORDER_LEDGER_V2_READS"] = "1"
        os.environ["ORDER_LEDGER_V2_BACKEND"] = "memory"
        os.environ["ORDER_LEDGER_V2_BACKFILL_COMPLETE"] = "1"
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
        from services import order_service

        order_service._ORDERS_READ_CACHE.clear()

    def tearDown(self):
        self.tenant_scope.stop()
        self.scope_patch.stop()
        reset_order_ledger_v2_for_tests()
        reset_blob_load_count()
        for k in (
            "ORDER_LEDGER_V2",
            "ORDER_LEDGER_V2_READS",
            "ORDER_LEDGER_V2_BACKEND",
            "ORDER_LEDGER_V2_BACKFILL_COMPLETE",
        ):
            os.environ.pop(k, None)

    def test_lookup_by_id_and_display_seq_no_blob_load_when_v2(self):
        with tenant_context("henry", scope="paper"):
            svc = OrderService("paper")
            rec = svc.create_from_request(
                TradeOrder("BUY", "SOL/USDT", 10.0, 0, usdt_amount=100),
                status="filled",
                telegram_token="look1",
            )
            svc.update_status(
                "look1", "filled",
                execution={"usdt": 100, "price": 10, "amount": 10},
            )
            # Clear blob cache and count — next _load would increment if called
            from services import order_service

            order_service._ORDERS_READ_CACHE.clear()
            reset_blob_load_count()
            before = blob_load_count()
            by_id = svc.get_by_id("look1")
            by_seq = svc.get_by_display_seq(int(rec["display_seq"]))
            after = blob_load_count()
            self.assertIsNotNone(by_id)
            self.assertEqual(by_id["id"], "look1")
            self.assertEqual(by_seq["id"], "look1")
            self.assertEqual(before, after)
            self.assertEqual(after, 0)

    def test_day_list_excludes_blob_old_and_no_blob_load_when_backfill_complete(self):
        with tenant_context("henry", scope="paper"):
            svc = OrderService("paper")
            # Seed many old orders only in blob
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

            svc.create_from_request(
                TradeOrder("BUY", "SOL/USDT", 10.0, 0, usdt_amount=100),
                status="filled",
                telegram_token="v2buy1",
            )
            svc.update_status(
                "v2buy1", "filled",
                execution={"usdt": 100, "price": 10, "amount": 10},
            )
            svc.create_from_request(
                TradeOrder("SELL", "SOL/USDT", 11.0, 5, signal="SELL_FULL"),
                status="filled",
                telegram_token="v2sell1",
            )
            svc.update_status(
                "v2sell1", "filled",
                execution={"usdt": 55, "price": 11, "amount": 5},
                pnl=5.0,
            )

            from services import order_service

            order_service._ORDERS_READ_CACHE.clear()
            reset_blob_load_count()
            day = svc.list_day_filled_all()
            stats = svc.stats_day_filled()
            loads = blob_load_count()

            ids = {o["id"] for o in day}
            self.assertIn("v2buy1", ids)
            self.assertIn("v2sell1", ids)
            self.assertTrue(all(not i.startswith("blob_old_") for i in ids))
            from_list = OrderService.stats_from_filled_orders(day)
            self.assertEqual(stats["buys"], from_list["buys"])
            self.assertEqual(stats["sells"], from_list["sells"])
            self.assertAlmostEqual(stats["realized_pnl"], from_list["realized_pnl"])
            # Hot path after backfill: no full blob load
            self.assertEqual(loads, 0)

    def test_partial_dual_write_unions_blob_only_today(self):
        """Without BACKFILL_COMPLETE, blob-only same-day fills still appear."""
        os.environ["ORDER_LEDGER_V2_BACKFILL_COMPLETE"] = "0"
        with tenant_context("henry", scope="paper"):
            svc = OrderService("paper")
            # v2 via dual-write
            svc.create_from_request(
                TradeOrder("BUY", "A/USDT", 1.0, 0, usdt_amount=10),
                status="filled",
                telegram_token="v2only",
            )
            svc.update_status(
                "v2only", "filled",
                execution={"usdt": 10, "price": 1, "amount": 10},
            )
            # blob-only same day (no dual-write)
            now = datetime.now().replace(microsecond=0)
            data = svc._load()
            data["orders"].append({
                "id": "blob_only_today",
                "display_seq": 9999,
                "status": "filled",
                "side": "sell",
                "symbol": "B/USDT",
                "ledger_scope": "paper",
                "tenant_id": "henry",
                "execution": {"usdt": 20, "price": 2, "amount": 10},
                "pnl": 3.0,
                "timestamps": {
                    "created": now.isoformat(),
                    "filled": now.isoformat(),
                    "updated": now.isoformat(),
                },
            })
            svc._save(data)

            day = svc.list_day_filled_all()
            ids = {o["id"] for o in day}
            self.assertIn("v2only", ids)
            self.assertIn("blob_only_today", ids)
            stats = svc.stats_day_filled()
            from_list = OrderService.stats_from_filled_orders(day)
            self.assertEqual(stats["buys"], from_list["buys"])
            self.assertEqual(stats["sells"], from_list["sells"])
            self.assertAlmostEqual(stats["realized_pnl"], from_list["realized_pnl"])


if __name__ == "__main__":
    unittest.main()
