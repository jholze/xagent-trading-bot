"""Order ledger v2: per-order store, day stats parity, hot-path no full-history load."""

from __future__ import annotations

import enum
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
    IDEMPOTENCY_KEY_INDEX_NAME,
    IDEMPOTENCY_KEY_PARTIAL_FILTER,
    MemoryOrderLedgerV2,
    MongoOrderLedgerV2,
    ORDERS_V2_COLLECTION,
    display_day_key_now,
    enrich_order_record,
    omit_empty_idempotency_key,
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
        self.assertEqual(s["wins"], 1)
        self.assertEqual(s["losses"], 1)
        self.assertEqual(s["unknown_side"], 0)
        self.assertEqual(
            s["filled"],
            s["buys"] + s["sells"] + s["shorts"] + s["covers"] + s["unknown_side"],
        )

    def test_missing_status_counts_as_filled_empty_and_zero_do_not(self):
        s = stats_from_filled_orders([
            {"side": "buy", "execution": {"usdt": 10}},
            {"status": None, "side": "buy", "execution": {"usdt": 10}},
            {"status": "", "side": "buy", "execution": {"usdt": 10}},
            {"status": 0, "side": "buy", "execution": {"usdt": 10}},
            {"status": "FILLED", "side": "buy", "execution": {"usdt": 10}},
        ])
        self.assertEqual(s["filled"], 3)
        self.assertEqual(s["buys"], 3)

    def test_enum_status_and_side_do_not_crash(self):
        class Status(enum.Enum):
            FILLED = "filled"

        class Side(enum.Enum):
            SELL = "sell"

        s = stats_from_filled_orders([
            {"status": Status.FILLED, "side": Side.SELL, "pnl": 4, "execution": {"usdt": 8}},
        ])
        self.assertEqual(s["filled"], 1)
        self.assertEqual(s["sells"], 1)
        self.assertEqual(s["sell_wins"], 1)

    def test_unknown_side_counted_logged_and_keeps_filled_identity(self):
        with self.assertLogs("storage.order_ledger_v2", level="WARNING") as cm:
            s = stats_from_filled_orders([
                {"status": "filled", "side": "hedge", "id": "x1", "execution": {"usdt": 9}},
                {"status": "filled", "side": "buy", "execution": {"usdt": 1}},
            ])
        self.assertEqual(s["filled"], 2)
        self.assertEqual(s["buys"], 1)
        self.assertEqual(s["unknown_side"], 1)
        self.assertEqual(
            s["filled"],
            s["buys"] + s["sells"] + s["shorts"] + s["covers"] + s["unknown_side"],
        )
        self.assertTrue(any("x1" in rec and "hedge" in rec for rec in cm.output))

    def test_bad_pnl_logged_counts_zero(self):
        with self.assertLogs("storage.order_ledger_v2", level="WARNING") as cm:
            s = stats_from_filled_orders([
                {"status": "filled", "side": "sell", "id": "p1", "pnl": "nope", "execution": {"usdt": 5}},
            ])
        self.assertEqual(s["realized_pnl"], 0.0)
        self.assertEqual(s["sells"], 1)
        self.assertTrue(any("p1" in rec and "nope" in rec for rec in cm.output))

    def test_cover_pnl_in_wins_not_sell_wins(self):
        s = stats_from_filled_orders([
            {"status": "filled", "side": "cover", "pnl": 5, "execution": {"usdt": 10}},
            {"status": "filled", "side": "cover", "pnl": -2, "execution": {"usdt": 10}},
            {"status": "filled", "side": "sell", "pnl": 1, "execution": {"usdt": 3}},
        ])
        self.assertAlmostEqual(s["realized_pnl"], 4.0)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 1)
        self.assertEqual(s["sell_wins"], 1)
        self.assertEqual(s["sell_losses"], 0)
        self.assertEqual(s["covers"], 2)


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


# Fixture for the 2026-09-25 operator VIRTUAL sell (#577). Live staging replay
# of today's row runs after review/deploy, not in this session.
_OPERATOR_VIRTUAL_QTY = 8783.52
_OPERATOR_VIRTUAL_PRICE = 0.7886
_OPERATOR_VIRTUAL_PNL = 341.8
_OPERATOR_VIRTUAL_USDT = 6913.0


def _operator_virtual_sell_577(*, oid: str = "manual-virtual-577", seq: int = 577) -> dict:
    day = display_day_key_now()
    ts = f"{day}T14:59:47"
    return {
        "id": oid,
        "display_seq": seq,
        "status": "filled",
        "side": "sell",
        "symbol": "VIRTUAL/USDT",
        "source": "manual",
        "idempotency_key": None,
        "qty": _OPERATOR_VIRTUAL_QTY,
        "tenant_id": "henry",
        "ledger_scope": "demo",
        "day_key": day,
        "execution": {
            "price": _OPERATOR_VIRTUAL_PRICE,
            "amount": _OPERATOR_VIRTUAL_QTY,
            "usdt": _OPERATOR_VIRTUAL_USDT,
        },
        "pnl": _OPERATOR_VIRTUAL_PNL,
        "timestamps": {"created": ts, "filled": ts, "updated": ts},
    }


class TestOmitEmptyIdempotencyKey(unittest.TestCase):
    def test_omit_drops_null_empty_and_keeps_real_key(self):
        self.assertNotIn("idempotency_key", omit_empty_idempotency_key(
            {"id": "a", "idempotency_key": None}
        ))
        self.assertNotIn("idempotency_key", omit_empty_idempotency_key(
            {"id": "a", "idempotency_key": ""}
        ))
        self.assertNotIn("idempotency_key", omit_empty_idempotency_key({"id": "a"}))
        kept = omit_empty_idempotency_key({"id": "a", "idempotency_key": "k-1"})
        self.assertEqual(kept["idempotency_key"], "k-1")

    def test_enrich_omits_null_so_legacy_blob_row_is_not_written_as_null(self):
        rec = enrich_order_record(_operator_virtual_sell_577())
        self.assertNotIn("idempotency_key", rec)
        self.assertEqual(rec["id"], "manual-virtual-577")


class TestMemoryOrderLedgerV2Replay577(unittest.TestCase):
    def test_idempotent_replay_of_operator_virtual_sell(self):
        store = MemoryOrderLedgerV2()
        row = _operator_virtual_sell_577()
        day = row["day_key"]
        store.upsert_order(row)
        store.rebuild_day_stats("henry", "demo", day)
        store.upsert_order(row)
        store.rebuild_day_stats("henry", "demo", day)
        day_list = store.query_day("henry", "demo", day, filled_only=True)
        ids = [o["id"] for o in day_list if o["id"] == "manual-virtual-577"]
        self.assertEqual(ids, ["manual-virtual-577"])
        stats = store.get_day_stats("henry", "demo", day)
        self.assertEqual(stats["sells"], 1)
        self.assertAlmostEqual(stats["realized_pnl"], _OPERATOR_VIRTUAL_PNL)
        self.assertEqual(len(store._orders), 1)


class TestOrderServiceV2NullIdempotency(unittest.TestCase):
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

    def test_create_from_request_omits_empty_idempotency_key(self):
        with tenant_context("henry", scope="paper"):
            svc = OrderService("paper")
            rec = svc.create_from_request(
                TradeOrder("SELL", "AAA/USDT", 1.0, 2.0, signal="SELL", source="manual"),
                status="filled",
                telegram_token="man-omit",
            )
            self.assertNotIn("idempotency_key", rec)
            from storage.order_ledger_v2 import get_order_ledger_v2

            stored = get_order_ledger_v2().get_by_id("henry", "paper", "man-omit")
            self.assertIsNotNone(stored)
            self.assertNotIn("idempotency_key", stored)

    def test_two_manual_orders_without_idempotency_key_in_day_list_and_pnl(self):
        with tenant_context("henry", scope="paper"):
            svc = OrderService("paper")
            svc.create_from_request(
                TradeOrder("SELL", "AAA/USDT", 1.0, 10.0, signal="SELL", source="manual"),
                status="filled",
                telegram_token="man-a",
            )
            svc.update_status(
                "man-a", "filled",
                execution={"usdt": 10, "price": 1.0, "amount": 10},
                pnl=12.0,
            )
            svc.create_from_request(
                TradeOrder("SELL", "BBB/USDT", 2.0, 5.0, signal="SELL", source="manual"),
                status="filled",
                telegram_token="man-b",
            )
            svc.update_status(
                "man-b", "filled",
                execution={"usdt": 10, "price": 2.0, "amount": 5},
                pnl=8.5,
            )
            day = svc.list_day_filled_all()
            ids = {o["id"] for o in day}
            self.assertIn("man-a", ids)
            self.assertIn("man-b", ids)
            stats = svc.stats_day_filled()
            fast = svc.stats_day_filled_fast()
            self.assertEqual(stats["sells"], 2)
            self.assertAlmostEqual(stats["realized_pnl"], 20.5)
            self.assertEqual(fast["sells"], 2)
            self.assertAlmostEqual(fast["realized_pnl"], 20.5)

    def test_legacy_null_idempotency_stripped_before_v2_upsert(self):
        with tenant_context("henry", scope="paper"):
            svc = OrderService("paper")
            row = _operator_virtual_sell_577()
            row["ledger_scope"] = "paper"
            svc._dual_write_v2(row)
            from storage.order_ledger_v2 import get_order_ledger_v2

            store = get_order_ledger_v2()
            stored = store.get_by_id("henry", "paper", "manual-virtual-577")
            self.assertIsNotNone(stored)
            self.assertNotIn("idempotency_key", stored)
            day = svc.list_day_filled_all()
            self.assertIn("manual-virtual-577", {o["id"] for o in day})
            stats = svc.stats_day_filled()
            self.assertEqual(stats["sells"], 1)
            self.assertAlmostEqual(stats["realized_pnl"], _OPERATOR_VIRTUAL_PNL)


class TestMongoIdempotencyIndex577(unittest.TestCase):
    def setUp(self):
        from storage.mongo_client import drop_database

        drop_database(test=True)
        self.store = MongoOrderLedgerV2(test=True)

    def tearDown(self):
        from storage.mongo_client import drop_database

        drop_database(test=True)

    def _filled(self, oid: str, seq: int, *, idem=None, include_null=False, pnl=1.0):
        day = display_day_key_now()
        rec = {
            "id": oid,
            "display_seq": seq,
            "status": "filled",
            "side": "sell",
            "symbol": "AAA/USDT",
            "source": "manual",
            "tenant_id": "henry",
            "ledger_scope": "demo",
            "day_key": day,
            "execution": {"usdt": 10, "price": 1.0, "amount": 10},
            "pnl": pnl,
            "timestamps": {
                "created": f"{day}T12:00:00",
                "filled": f"{day}T12:00:00",
                "updated": f"{day}T12:00:00",
            },
        }
        if include_null:
            rec["idempotency_key"] = None
        elif idem is not None:
            rec["idempotency_key"] = idem
        return rec

    def test_ensure_indexes_replaces_sparse_unique_with_partial(self):
        oc = self.store._db()[ORDERS_V2_COLLECTION]
        oc.create_index(
            [("idempotency_key", 1)],
            name=IDEMPOTENCY_KEY_INDEX_NAME,
            unique=True,
            sparse=True,
        )
        before = oc.index_information()[IDEMPOTENCY_KEY_INDEX_NAME]
        self.assertTrue(before.get("unique"))
        self.assertTrue(before.get("sparse"))
        self.store.ensure_indexes()
        after = oc.index_information()[IDEMPOTENCY_KEY_INDEX_NAME]
        self.assertTrue(after.get("unique"))
        self.assertFalse(after.get("sparse", False))
        self.assertEqual(
            after.get("partialFilterExpression"),
            IDEMPOTENCY_KEY_PARTIAL_FILTER,
        )

    def test_two_manual_orders_without_key_both_upsert(self):
        self.store.ensure_indexes()
        self.store.upsert_order(self._filled("m1", 1, include_null=True, pnl=10.0))
        self.store.upsert_order(self._filled("m2", 2, include_null=True, pnl=5.0))
        day = display_day_key_now()
        listed = self.store.query_day("henry", "demo", day, filled_only=True)
        self.assertEqual({o["id"] for o in listed}, {"m1", "m2"})
        stats = self.store.get_day_stats("henry", "demo", day)
        self.assertEqual(stats["sells"], 2)
        self.assertAlmostEqual(stats["realized_pnl"], 15.0)
        for oid in ("m1", "m2"):
            stored = self.store.get_by_id("henry", "demo", oid)
            self.assertNotIn("idempotency_key", stored)

    def test_idempotent_mongo_replay_of_operator_row(self):
        self.store.ensure_indexes()
        row = _operator_virtual_sell_577()
        self.store.upsert_order(row)
        self.store.rebuild_day_stats("henry", "demo", row["day_key"])
        self.store.upsert_order(row)
        listed = self.store.query_day(
            "henry", "demo", row["day_key"], filled_only=True,
        )
        self.assertEqual([o["id"] for o in listed], ["manual-virtual-577"])
        stats = self.store.get_day_stats("henry", "demo", row["day_key"])
        self.assertEqual(stats["sells"], 1)
        self.assertAlmostEqual(stats["realized_pnl"], _OPERATOR_VIRTUAL_PNL)


if __name__ == "__main__":
    unittest.main()
