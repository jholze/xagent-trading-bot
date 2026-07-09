import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data_manager import prune_non_gate_watchlist_sources, prune_watchlist_coins_gate_only


class TestWatchlistGatePrune(unittest.TestCase):
    def test_prune_watchlist_coins_gate_only(self):
        coins = [
            {"symbol": "PEPE/USDT", "active": True},
            {"symbol": "FAKE/USDT", "active": True},
        ]
        kept, removed = prune_watchlist_coins_gate_only(
            coins,
            gate_prices={"PEPE/USDT": 0.00001, "FAKE/USDT": 0},
            gate_only=True,
        )
        self.assertEqual([c["symbol"] for c in kept], ["PEPE/USDT"])
        self.assertEqual(removed, ["FAKE/USDT"])

    def test_prune_skipped_when_gate_only_disabled(self):
        coins = [{"symbol": "FAKE/USDT", "active": True}]
        kept, removed = prune_watchlist_coins_gate_only(coins, gate_only=False)
        self.assertEqual(kept, coins)
        self.assertEqual(removed, [])

    def test_prune_non_gate_overlay_sources(self):
        cfg = {
            "trading_mode": "live",
            "live": {"dry_run": True, "dry_run_enhanced": True},
            "cmc": {
                "trending_watchlist": {
                    "enabled": True,
                    "live_enabled": True,
                    "gate_only": True,
                    "prune_non_gate": True,
                    "prune_base_watchlist": False,
                }
            },
        }
        overlay = {
            "refreshed_at": "2026-01-01T00:00:00",
            "coins": [
                {"symbol": "PEPE/USDT", "source": "cmc_trending"},
                {"symbol": "BOBO/USDT", "source": "cmc_trending"},
            ],
        }
        with patch("data_manager.trending_watchlist_live_enabled", return_value=True), \
             patch("data_manager.is_dry_run_enhanced", return_value=False), \
             patch("data_manager.uses_watchlist_expansion", return_value=False), \
             patch("data_manager.load_cmc_trending_overlay", return_value=overlay), \
             patch("data_manager.save_cmc_trending_overlay") as mock_save, \
             patch("price_fetcher.get_gate_prices_batch", return_value={
                 "PEPE/USDT": 0.00001,
                 "BOBO/USDT": 0,
             }):
            result = prune_non_gate_watchlist_sources(cfg)

        self.assertEqual(result["removed"], ["BOBO/USDT"])
        saved = mock_save.call_args[0][0]
        self.assertEqual(len(saved["coins"]), 1)
        self.assertEqual(saved["coins"][0]["symbol"], "PEPE/USDT")

    def test_prune_base_watchlist_when_enabled(self):
        cfg = {
            "cmc": {
                "trending_watchlist": {
                    "gate_only": True,
                    "prune_non_gate": True,
                    "prune_base_watchlist": True,
                }
            },
        }
        with patch("data_manager.trending_watchlist_live_enabled", return_value=False), \
             patch("data_manager.is_dry_run_enhanced", return_value=False), \
             patch("data_manager.uses_watchlist_expansion", return_value=False), \
             patch("data_manager.load_watchlist", return_value=[
                 {"symbol": "ARIA/USDT"},
                 {"symbol": "CAT/USDT"},
             ]), \
             patch("data_manager.save_watchlist") as mock_save, \
             patch("price_fetcher.get_gate_prices_batch", return_value={
                 "ARIA/USDT": 0.02,
                 "CAT/USDT": 0,
             }):
            result = prune_non_gate_watchlist_sources(cfg)

        self.assertEqual(result["removed"], ["CAT/USDT"])
        saved = mock_save.call_args[0][0]
        self.assertEqual([c["symbol"] for c in saved], ["ARIA/USDT"])


if __name__ == "__main__":
    unittest.main()