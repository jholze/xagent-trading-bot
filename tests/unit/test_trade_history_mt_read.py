"""Multi-tenant trade_history reads must merge legacy trades + compound cash."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT
from data_manager import resolve_sim_cash_balance
from storage.mongo_client import TEST_DB_NAME, drop_database
from storage.mongo_ledger import MongoLedgerStore
from storage.tenant_keys import compound_ledger_id


class TestTradeHistoryMtRead(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        os.environ["MULTI_TENANT_ENABLED"] = "1"
        drop_database(test=True)
        self.store = MongoLedgerStore(test=True)
        coll = self.store._collection("trade_history")
        coll.replace_one(
            {"_id": "demo"},
            {
                "_id": "demo",
                "ledger_scope": "demo",
                "virtual_balance": 3648.0,
                "realized_pnl": 1200.0,
                "trades": [
                    {
                        "type": "BUY",
                        "symbol": "ETH/USDT",
                        "usdt_amount": 500.0,
                        "timestamp": "2026-07-15T08:00:00",
                    }
                ],
            },
            upsert=True,
        )
        coll.replace_one(
            {"_id": compound_ledger_id(DEFAULT_TENANT, "demo")},
            {
                "_id": compound_ledger_id(DEFAULT_TENANT, "demo"),
                "tenant_id": DEFAULT_TENANT,
                "ledger_scope": "demo",
                "virtual_balance": 10_512.0,
                "realized_pnl": 15_159.2,
                "trades": [],
            },
            upsert=True,
        )

    def tearDown(self):
        drop_database(test=True)
        os.environ.pop("MULTI_TENANT_ENABLED", None)

    def test_load_merges_compound_cash_with_legacy_trades(self):
        loaded = self.store.load_trade_history("demo", tenant_id=DEFAULT_TENANT)
        self.assertAlmostEqual(float(loaded["virtual_balance"]), 10_512.0, places=2)
        self.assertEqual(len(loaded.get("trades", [])), 1)
        self.assertAlmostEqual(float(loaded["realized_pnl"]), 15_159.2, places=2)


if __name__ == "__main__":
    unittest.main()