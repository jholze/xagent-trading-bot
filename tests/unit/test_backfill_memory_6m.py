"""Smoke tests for 6m backfill helpers (no live Mongo required for pure bits)."""

from __future__ import annotations

import unittest
from unittest.mock import patch


class TestNormalizeAndUniverse(unittest.TestCase):
    def test_normalize(self):
        import importlib.util
        from pathlib import Path

        p = Path(__file__).resolve().parents[2] / "scripts" / "backfill_memory_6m.py"
        spec = importlib.util.spec_from_file_location("backfill_6m", p)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(mod)
        self.assertEqual(mod._normalize_sym("eth"), "ETH/USDT")
        self.assertEqual(mod._normalize_sym("SOL/USDT"), "SOL/USDT")
        self.assertEqual(mod._normalize_sym("TEST/FOO"), "")

    def test_collect_universe_merges_sources(self):
        import importlib.util
        from pathlib import Path
        from types import SimpleNamespace

        p = Path(__file__).resolve().parents[2] / "scripts" / "backfill_memory_6m.py"
        spec = importlib.util.spec_from_file_location("backfill_6m", p)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(mod)

        store = SimpleNamespace(
            list_trades=lambda limit=5000: [
                SimpleNamespace(symbol="ETH/USDT"),
                SimpleNamespace(symbol="SOL/USDT"),
            ]
        )
        with patch(
            "strategies.positions.list_active_positions",
            return_value=[{"symbol": "BTC/USDT"}, {"symbol": "ETH/USDT"}],
        ), patch(
            "data_manager.load_effective_watchlist",
            return_value=[{"symbol": "ARB", "active": True}, {"symbol": "OP/USDT", "active": True}],
        ), patch("storage.mongo_client.get_database") as gdb:
            gdb.return_value.orders.find_one.return_value = None
            uni = mod.collect_universe(lookback_days=180, store=store)
        self.assertIn("ETH/USDT", uni)
        self.assertIn("BTC/USDT", uni)
        self.assertIn("ARB/USDT", uni)
        self.assertIn("OP/USDT", uni)
        self.assertEqual(len(uni), len(set(uni)))


if __name__ == "__main__":
    unittest.main()
