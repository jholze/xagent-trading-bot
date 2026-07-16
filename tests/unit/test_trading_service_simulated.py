"""TradingService: simulated live is executable without /live_confirm."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import BotConfig
from core.simulated_trading import simulated_live_config_updates
from notifications.telegram_commands import mode_commands
from services.trading_service import TradingService


def _cfg(raw: dict) -> BotConfig:
    bot = BotConfig()
    bot._raw = raw
    return bot


class TestTradingServiceSimulated(unittest.TestCase):
    def test_can_execute_simulated_live_without_live_confirm(self):
        raw = {
            "trading_mode": "live",
            "live_confirmed": False,
            "live": {"dry_run": True, "dry_run_enhanced": True},
        }
        svc = TradingService(config=_cfg(raw))
        ok, reason = svc.can_execute()
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_can_execute_after_mode_paper_maps_to_simulated_live(self):
        raw = {
            "trading_mode": "paper",
            "virtual_trading": True,
            "live": {"dry_run": True},
        }
        with patch("notifications.telegram_commands.mode_commands.get_config", return_value=raw), \
             patch("notifications.telegram_commands.mode_commands._save_mode_updates", return_value=True), \
             patch("notifications.telegram_commands.mode_commands.reload_config"), \
             patch("notifications.telegram_commands.mode_commands.on_trading_mode_change", return_value=""), \
             patch("notifications.telegram_commands.mode_commands.send_telegram_message"):
            self.assertTrue(mode_commands.handle("/mode paper"))
        updates = simulated_live_config_updates(raw)
        self.assertTrue(updates["live_confirmed"])
        self.assertTrue(updates["live"]["dry_run"])
        svc = TradingService(config=_cfg({**raw, **updates}))
        ok, _ = svc.can_execute()
        self.assertTrue(ok)

    def test_mode_label_simulated_live(self):
        raw = {
            "trading_mode": "live",
            "live_confirmed": True,
            "live": {"dry_run": True},
        }
        with patch.dict(os.environ, {"DEMO_MODE": "1"}, clear=False), \
             patch("data_manager.resolve_ledger_backend", return_value="mongo"):
            label = TradingService(config=_cfg(raw)).mode_label()
        self.assertIn("Simulated Live", label)
        self.assertNotIn("paper", label.lower())

    def test_staging_live_confirm_enables_simulated_execution(self):
        with patch.dict(os.environ, {"DEMO_MODE": "1"}, clear=False), \
             patch("notifications.telegram_commands.mode_commands._save_mode_updates", return_value=True) as mock_save, \
             patch("notifications.telegram_commands.mode_commands.reload_config"), \
             patch("notifications.telegram_commands.mode_commands.on_trading_mode_change", return_value=""), \
             patch("notifications.telegram_commands.mode_commands.send_telegram_message") as mock_send:
            self.assertTrue(mode_commands.handle("/live_confirm"))
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        self.assertTrue(saved.get("live_confirmed"))
        self.assertTrue(saved.get("live", {}).get("dry_run"))
        self.assertIn("Simulated Live", mock_send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()