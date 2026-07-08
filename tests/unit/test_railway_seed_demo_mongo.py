import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


class TestRailwaySeedDemoMongo(unittest.TestCase):
    @patch("storage.mongo_ledger.MongoLedgerStore")
    @patch("storage.mongo_client.ping_database", return_value=True)
    def test_keeps_ledger_when_orders_below_min_but_nonzero(self, _ping, mock_store_cls):
        from scripts import railway_seed_demo_mongo as seed

        store = MagicMock()
        store.load_orders.return_value = {"orders": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
        mock_store_cls.return_value = store

        rc = seed.main()
        self.assertEqual(rc, 0)
        store.save_orders.assert_not_called()
        store.save_trade_history.assert_not_called()

    @patch("storage.mongo_ledger.MongoLedgerStore")
    @patch("storage.mongo_client.ping_database", return_value=True)
    @patch("scripts.railway_seed_demo_mongo._load_seed")
    def test_seeds_fresh_start_only_when_empty(self, mock_load, _ping, mock_store_cls):
        from scripts import railway_seed_demo_mongo as seed

        store = MagicMock()
        store.load_orders.side_effect = [
            {"orders": []},
            {"orders": []},
        ]
        store.load_positions.return_value = {"positions": {}}
        mock_store_cls.return_value = store
        mock_load.side_effect = lambda name: {
            "orders.json": {
                "ledger_scope": "demo",
                "orders": [],
                "fresh_start": True,
            },
            "history.json": {
                "virtual_balance": 100000.0,
                "fresh_start": True,
                "trades": [],
            },
        }.get(name)

        with patch("strategies.positions.bootstrap_positions"), patch(
            "strategies.positions.flush_positions"
        ):
            rc = seed.main()
        self.assertEqual(rc, 0)
        store.save_orders.assert_called_once()


if __name__ == "__main__":
    unittest.main()