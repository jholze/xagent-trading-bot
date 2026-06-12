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
             patch("data_manager.load_dry_run_overlay", return_value=overlay), \
             patch("data_manager.is_dry_run_enhanced", return_value=True):
            merged = load_effective_watchlist()
        symbols = [c["symbol"] for c in merged]
        self.assertEqual(symbols, ["BTC/USDT", "PEPE/USDT"])

    def test_load_effective_watchlist_without_enhanced_returns_base_only(self):
        base = [{"symbol": "BTC/USDT", "active": True}]
        with patch("data_manager.load_watchlist", return_value=base), \
             patch("data_manager.is_dry_run_enhanced", return_value=False):
            merged = load_effective_watchlist()
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["symbol"], "BTC/USDT")

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
             patch("data_manager.load_dry_run_overlay", return_value=overlay), \
             patch("data_manager.is_dry_run_enhanced", return_value=True):
            merged = load_effective_watchlist()
        self.assertEqual(len(merged), 1)


if __name__ == "__main__":
    unittest.main()