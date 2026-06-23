import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import BotConfig
from data_manager import (
    load_effective_watchlist,
    remove_coin,
    save_dry_run_expansion,
    save_dry_run_overlay,
)
from services.dry_run_watchlist import DryRunWatchlistSync


class TestDryRunWatchlist(unittest.TestCase):
    def _enhanced_config(self):
        cfg = BotConfig()
        cfg._raw = {
            "trading_mode": "live",
            "live": {
                "dry_run": True,
                "dry_run_enhanced": True,
                "simulated_balance_usdt": 5000,
                "trending_watchlist": {
                    "enabled": True,
                    "max_coins": 15,
                    "refresh_hours": 6,
                    "gate_only": True,
                    "exclude_symbols": ["USDT", "USDC"],
                },
            },
            "cmc": {"api_key_env": "CMC_API_KEY"},
            "dry_run_defaults": {},
        }
        return cfg

    def test_load_effective_watchlist_merges_overlay(self):
        base = [{"symbol": "BTC/USDT", "active": True}]
        overlay = {
            "refreshed_at": datetime.now().isoformat(),
            "source": "trending/latest",
            "coins": [{"symbol": "PEPE/USDT", "source": "cmc_trending", "active": True}],
        }
        with patch("data_manager.load_watchlist", return_value=base), \
             patch("data_manager.uses_watchlist_expansion", return_value=False), \
             patch("data_manager.load_dry_run_overlay", return_value=overlay), \
             patch("data_manager.is_dry_run_enhanced", return_value=True):
            merged = load_effective_watchlist()
        symbols = [c["symbol"] for c in merged]
        self.assertEqual(symbols, ["BTC/USDT", "PEPE/USDT"])

    def test_load_effective_watchlist_without_expansion_returns_base_only(self):
        base = [{"symbol": "BTC/USDT", "active": True}]
        with patch("data_manager.load_watchlist", return_value=base), \
             patch("data_manager.uses_watchlist_expansion", return_value=False), \
             patch("data_manager.is_dry_run_enhanced", return_value=False), \
             patch("data_manager.trending_watchlist_live_enabled", return_value=False):
            merged = load_effective_watchlist()
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["symbol"], "BTC/USDT")

    def test_load_effective_watchlist_merges_live_trending_overlay(self):
        base = [{"symbol": "BTC/USDT", "active": True}]
        overlay = {
            "refreshed_at": datetime.now().isoformat(),
            "source": "trending/latest",
            "coins": [{"symbol": "WIF/USDT", "source": "cmc_trending", "active": True}],
        }
        with patch("data_manager.load_watchlist", return_value=base), \
             patch("data_manager.uses_watchlist_expansion", return_value=False), \
             patch("data_manager.is_dry_run_enhanced", return_value=False), \
             patch("data_manager.trending_watchlist_live_enabled", return_value=True), \
             patch("data_manager.load_cmc_trending_overlay", return_value=overlay):
            merged = load_effective_watchlist()
        symbols = [c["symbol"] for c in merged]
        self.assertEqual(symbols, ["BTC/USDT", "WIF/USDT"])

    def test_load_effective_watchlist_merges_dry_run_expansion(self):
        base = [{"symbol": "BTC/USDT", "active": True}]
        expansion = {
            "coins": [{"symbol": "ETH/USDT", "source": "dry_run_expansion", "active": True}],
        }
        with patch("data_manager.load_watchlist", return_value=base), \
             patch("data_manager.uses_watchlist_expansion", return_value=True), \
             patch("data_manager.load_dry_run_expansion", return_value=expansion), \
             patch("data_manager.is_dry_run_enhanced", return_value=False):
            merged = load_effective_watchlist()
        symbols = [c["symbol"] for c in merged]
        self.assertEqual(symbols, ["BTC/USDT", "ETH/USDT"])

    def test_sync_filters_non_gate_coins(self):
        cfg = self._enhanced_config()
        provider = MagicMock()
        provider.fetch_trending_symbols.return_value = (["PEPE", "FAKECOIN", "DOGE"], "trending/latest")

        with tempfile.TemporaryDirectory() as tmp:
            overlay_path = os.path.join(tmp, "watchlist.dry_run_overlay.json")
            with patch("data_manager.get_data_file", return_value=overlay_path), \
                 patch("data_manager.load_watchlist", return_value=[{"symbol": "BTC/USDT"}]), \
                 patch("data_manager.is_dry_run_enhanced", return_value=True), \
                 patch("services.dry_run_watchlist.get_prices_batch", return_value={
                     "PEPE/USDT": 0.00001,
                     "FAKECOIN/USDT": 0,
                     "DOGE/USDT": 0.12,
                 }):
                sync = DryRunWatchlistSync(cfg, provider=provider)
                overlay = sync.sync_if_needed(force=True)

        symbols = [c["symbol"] for c in overlay["coins"]]
        self.assertIn("PEPE/USDT", symbols)
        self.assertIn("DOGE/USDT", symbols)
        self.assertNotIn("FAKECOIN/USDT", symbols)
        self.assertEqual(overlay["source"], "trending/latest")

    def test_sync_skips_when_refresh_not_due(self):
        cfg = self._enhanced_config()
        provider = MagicMock()
        recent = {
            "refreshed_at": datetime.now().isoformat(),
            "source": "trending/latest",
            "coins": [{"symbol": "PEPE/USDT", "source": "cmc_trending"}],
        }
        with patch("services.dry_run_watchlist.load_dry_run_overlay", return_value=recent), \
             patch("services.dry_run_watchlist.is_dry_run_enhanced", return_value=True):
            sync = DryRunWatchlistSync(cfg, provider=provider)
            overlay = sync.sync_if_needed(force=False)
        provider.fetch_trending_symbols.assert_not_called()
        self.assertEqual(overlay["coins"][0]["symbol"], "PEPE/USDT")

    def test_dedupe_on_merge(self):
        base = [{"symbol": "PEPE/USDT", "active": True}]
        overlay = {
            "coins": [{"symbol": "PEPE/USDT", "source": "cmc_trending", "active": True}],
        }
        with patch("data_manager.load_watchlist", return_value=base), \
             patch("data_manager.uses_watchlist_expansion", return_value=False), \
             patch("data_manager.load_dry_run_overlay", return_value=overlay), \
             patch("data_manager.is_dry_run_enhanced", return_value=True):
            merged = load_effective_watchlist()
        self.assertEqual(len(merged), 1)


    def test_remove_coin_from_dry_run_expansion(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            base_path = os.path.join(tmp, "watchlist.json")
            expansion_path = os.path.join(tmp, "watchlist.dry_run_expansion.json")

            def _data_file(name):
                if name == "watchlist.json":
                    return base_path
                if name == "watchlist.dry_run_expansion.json":
                    return expansion_path
                return os.path.join(tmp, name)

            with open(base_path, "w", encoding="utf-8") as f:
                json.dump({"coins": [{"symbol": "BTC/USDT", "active": True}]}, f)

            with patch("data_manager.get_data_file", side_effect=_data_file), \
                 patch("data_manager.uses_watchlist_expansion", return_value=True), \
                 patch("data_manager.is_dry_run_enhanced", return_value=False):
                save_dry_run_expansion({
                    "coins": [{"symbol": "ETH/USDT", "source": "dry_run_expansion", "active": True}],
                })
                success, _ = remove_coin("ETH/USDT")
                merged = load_effective_watchlist()

            self.assertTrue(success)
            self.assertEqual([c["symbol"] for c in merged], ["BTC/USDT"])


if __name__ == "__main__":
    unittest.main()