"""Trending watchlist sync: lock, startup race, cold-bootstrap notify policy."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import BotConfig
from services import dry_run_watchlist as tw_module
from services.dry_run_watchlist import TrendingWatchlistSync, sync_trending_watchlist_once


def _enhanced_config() -> BotConfig:
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
                "refresh_hours": 0,
                "gate_only": True,
                "exclude_symbols": ["USDT", "USDC"],
            },
        },
        "cmc": {"api_key_env": "CMC_API_KEY"},
        "dry_run_defaults": {},
        "volatile_altcoin": {"timeframe": "1h"},
    }
    return cfg


@contextmanager
def _sync_patches(
    *,
    overlay_path: str,
    provider: MagicMock,
    gate_prices: dict,
    base_watchlist: list | None = None,
    telegram_mock: MagicMock | None = None,
):
    base_watchlist = base_watchlist if base_watchlist is not None else [{"symbol": "BTC/USDT"}]
    patches = [
        patch("data_manager.get_data_file", return_value=overlay_path),
        patch("data_manager.load_watchlist", return_value=base_watchlist),
        patch("data_manager.is_dry_run_enhanced", return_value=True),
        patch("services.dry_run_watchlist.CMCTrendingProvider", return_value=provider),
        patch("services.dry_run_watchlist.get_gate_prices_batch", return_value=gate_prices),
    ]
    if telegram_mock is not None:
        patches.append(patch("telegram_notifier.send_telegram_message", telegram_mock))
    patches.append(patch("data_manager.prune_non_gate_watchlist_sources"))

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


class TestTrendingWatchlistSync(unittest.TestCase):
    def setUp(self):
        tw_module._sync_lock = threading.Lock()

    def _overlay_path(self, tmp: str) -> str:
        return os.path.join(tmp, "watchlist.dry_run_overlay.json")

    def _write_overlay(self, path: str, data: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_concurrent_sync_sends_at_most_one_telegram(self):
        cfg = _enhanced_config()
        cfg._raw["live"]["trending_watchlist"]["refresh_hours"] = 4
        provider = MagicMock()
        provider.fetch_trending_symbols.return_value = (["PEPE", "WIF"], "trending/latest")
        gate = {"PEPE/USDT": 0.00001, "WIF/USDT": 1.2}
        results: list[dict] = []
        mock_send = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            overlay_path = self._overlay_path(tmp)
            self._write_overlay(
                overlay_path,
                {
                    "refreshed_at": "2020-01-01T00:00:00",
                    "source": "trending/latest",
                    "coins": [{"symbol": "DOGE/USDT", "source": "cmc_trending"}],
                },
            )
            with _sync_patches(
                overlay_path=overlay_path,
                provider=provider,
                gate_prices=gate,
                telegram_mock=mock_send,
            ):
                threads = [
                    threading.Thread(
                        target=lambda: results.append(sync_trending_watchlist_once(cfg)),
                        name="sync-a",
                    ),
                    threading.Thread(
                        target=lambda: results.append(sync_trending_watchlist_once(cfg)),
                        name="sync-b",
                    ),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=5)

        self.assertEqual(len(results), 2)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(provider.fetch_trending_symbols.call_count, 1)
        added_syms = {a["symbol"] for a in results[0].get("added", [])}
        self.assertEqual(added_syms, {"PEPE/USDT", "WIF/USDT"})

    def test_cold_bootstrap_skips_telegram(self):
        cfg = _enhanced_config()
        provider = MagicMock()
        provider.fetch_trending_symbols.return_value = (["PEPE"], "trending/latest")
        mock_send = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            overlay_path = self._overlay_path(tmp)
            with _sync_patches(
                overlay_path=overlay_path,
                provider=provider,
                gate_prices={"PEPE/USDT": 0.1},
                base_watchlist=[],
                telegram_mock=mock_send,
            ):
                overlay = sync_trending_watchlist_once(cfg, force=True)

        self.assertEqual(len(overlay.get("coins", [])), 1)
        mock_send.assert_not_called()

    def test_delta_sync_notifies_once(self):
        cfg = _enhanced_config()
        provider = MagicMock()
        provider.fetch_trending_symbols.return_value = (["PEPE", "WIF"], "trending/latest")
        mock_send = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            overlay_path = self._overlay_path(tmp)
            self._write_overlay(
                overlay_path,
                {
                    "refreshed_at": datetime.now().isoformat(),
                    "source": "trending/latest",
                    "coins": [{"symbol": "PEPE/USDT", "source": "cmc_trending", "trending_rank": 1}],
                },
            )
            with _sync_patches(
                overlay_path=overlay_path,
                provider=provider,
                gate_prices={"PEPE/USDT": 0.1, "WIF/USDT": 1.0},
                base_watchlist=[],
                telegram_mock=mock_send,
            ):
                overlay = sync_trending_watchlist_once(cfg, force=True)

        self.assertEqual([a["symbol"] for a in overlay.get("added", [])], ["WIF/USDT"])
        mock_send.assert_called_once()
        body = mock_send.call_args[0][0]
        self.assertIn("Watchlist+ CMC Trending", body)
        self.assertIn("WIF", body)

    def test_startup_race_global_and_background_fallback(self):
        """Mirrors bot restart: price_loop sync + background fallback in parallel."""
        from services.background_runtime import _ensure_trending_watchlist
        from services.cycle_shared import sync_global_watchlist_once

        cfg = _enhanced_config()
        cfg._raw["live"]["trending_watchlist"]["refresh_hours"] = 4
        provider = MagicMock()
        provider.fetch_trending_symbols.return_value = (["PEPE", "DOGE"], "trending/latest")
        mock_send = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            overlay_path = self._overlay_path(tmp)
            with _sync_patches(
                overlay_path=overlay_path,
                provider=provider,
                gate_prices={"PEPE/USDT": 0.1, "DOGE/USDT": 0.2},
                base_watchlist=[],
                telegram_mock=mock_send,
            ), patch("core.config.get_bot_config", return_value=cfg):
                threads = [
                    threading.Thread(target=lambda: sync_global_watchlist_once(cfg), name="price-loop"),
                    threading.Thread(target=_ensure_trending_watchlist, name="bg-fallback"),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=5)

        mock_send.assert_not_called()
        self.assertEqual(provider.fetch_trending_symbols.call_count, 1)

    def test_background_loop_does_not_call_trending_sync(self):
        import inspect
        import services.background_runtime as bg

        source = inspect.getsource(bg._loop)
        self.assertNotIn("_ensure_trending_watchlist()", source)

    def test_sync_global_watchlist_uses_coordinator(self):
        from services import cycle_shared

        cfg = _enhanced_config()
        with patch("services.dry_run_watchlist.sync_trending_watchlist_once") as mock_sync, \
             patch("data_manager.prune_non_gate_watchlist_sources"):
            cycle_shared.sync_global_watchlist_once(cfg)
        mock_sync.assert_called_once_with(cfg)


if __name__ == "__main__":
    unittest.main()