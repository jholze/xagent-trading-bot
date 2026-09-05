"""Soft hot-reload registry (A1–A6)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notifications.telegram_i18n import reload_messages, t
from services.reload_registry import (
    format_reload_help_html,
    last_reload,
    normalize_scopes,
    run_reload,
)
from tests.support.offline import gate_prices_listed


class TestReloadRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._gate_p = patch("price_fetcher.get_gate_prices_batch", side_effect=gate_prices_listed)
        cls._gate_p.start()
        cls._ticker_p = patch("price_fetcher.get_ticker_price", return_value=1.0)
        cls._ticker_p.start()

    @classmethod
    def tearDownClass(cls):
        cls._ticker_p.stop()
        cls._gate_p.stop()

    def test_normalize_scopes_all(self):
        self.assertEqual(
            normalize_scopes("all"),
            ["ui", "config", "lists", "cache"],
        )
        self.assertEqual(normalize_scopes(None), ["ui", "config", "lists", "cache"])
        self.assertEqual(normalize_scopes("ui config"), ["ui", "config"])
        self.assertEqual(normalize_scopes("ui,cache"), ["ui", "cache"])

    def test_reload_ui_picks_up_message_changes(self):
        from notifications import telegram_i18n as i18n

        # Force clean load
        i18n._MESSAGES = None
        reload_messages()
        before = t("portfolio_title", lang="de")

        # Point catalog at a temp file with an override
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telegram_messages.json"
            data = {
                "de": {"portfolio_title": "📊 Portfolio HOT"},
                "en": {"portfolio_title": "📊 Portfolio HOT EN"},
            }
            path.write_text(json.dumps(data), encoding="utf-8")
            with patch.object(i18n, "_PATH", path):
                i18n._MESSAGES = None
                report = run_reload("ui", source="test", actor="unit")
                self.assertTrue(report.ok)
                self.assertEqual(t("portfolio_title", lang="de"), "📊 Portfolio HOT")

        # Restore real catalog
        i18n._MESSAGES = None
        reload_messages()
        self.assertEqual(t("portfolio_title", lang="de"), before)

    def test_reload_all_writes_audit_and_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "reload_audit.jsonl"
            with patch("services.reload_registry._AUDIT_PATH", audit):
                report = run_reload("all", source="test", actor="pytest")
                self.assertTrue(report.ok)
                self.assertEqual(
                    [r.scope for r in report.results],
                    ["ui", "config", "lists", "cache"],
                )
                self.assertTrue(audit.exists())
                lines = audit.read_text(encoding="utf-8").strip().splitlines()
                self.assertGreaterEqual(len(lines), 1)
                payload = json.loads(lines[-1])
                self.assertEqual(payload["source"], "test")
                self.assertEqual(payload["actor"], "pytest")
                self.assertTrue(payload["ok"])
                snap = last_reload()
                self.assertIsNotNone(snap)
                self.assertEqual(snap["source"], "test")

    def test_reload_cache_clears_price_ram(self):
        from price_fetcher import _cache_set, _price_cache, clear_price_cache

        _cache_set("TEST/USDT", 1.23)
        self.assertIn("TEST/USDT", _price_cache)
        report = run_reload("cache", source="test")
        self.assertTrue(report.ok)
        self.assertNotIn("TEST/USDT", _price_cache)
        # clear again is fine
        self.assertEqual(clear_price_cache(), 0)

    def test_help_html(self):
        html = format_reload_help_html()
        self.assertIn("/reload ui", html)
        self.assertIn("/reload all", html)

    def test_plan_startup_scopes_new_deploy_vs_restart(self):
        from services.reload_registry import plan_startup_reload_scopes

        scopes, reason = plan_startup_reload_scopes(
            current_commit="abc1234",
            previous_commit="old9999",
            mode="deploy",
        )
        self.assertEqual(scopes, ["ui", "config", "lists", "cache"])
        self.assertEqual(reason, "new_deploy")

        scopes2, reason2 = plan_startup_reload_scopes(
            current_commit="abc1234",
            previous_commit="abc1234",
            mode="deploy",
        )
        self.assertEqual(scopes2, ["cache"])
        self.assertEqual(reason2, "same_commit_restart")

        scopes3, reason3 = plan_startup_reload_scopes(
            current_commit="abc1234",
            previous_commit="abc1234",
            mode="off",
        )
        self.assertEqual(scopes3, [])
        self.assertEqual(reason3, "disabled")

    def test_auto_reload_on_startup_new_deploy(self):
        from services import reload_registry as rr

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker.json"
            audit = Path(tmp) / "audit.jsonl"
            with patch.object(rr, "_BUILD_MARKER", marker), \
                 patch.object(rr, "_AUDIT_PATH", audit), \
                 patch.object(rr, "_current_build_commit", return_value="deadbee"), \
                 patch.object(rr, "_read_build_marker", return_value={"commit": "cafebabe"}), \
                 patch.object(rr, "_write_build_marker") as write_m, \
                 patch.object(rr, "_auto_reload_mode", return_value="deploy"):
                report = rr.auto_reload_on_startup(actor="test")
            self.assertIsNotNone(report)
            self.assertEqual(report.scopes, ["ui", "config", "lists", "cache"])
            self.assertTrue(report.ok)
            write_m.assert_called()
            auto = rr.last_auto_reload()
            self.assertEqual(auto.get("reason"), "new_deploy")


class TestReloadCommand(unittest.TestCase):
    def test_handle_help(self):
        from notifications.telegram_commands import reload_commands

        sent = []
        with patch(
            "notifications.telegram_commands.reload_commands.send_telegram_message",
            side_effect=lambda m: sent.append(m),
        ):
            self.assertTrue(reload_commands.handle("/reload"))
        self.assertEqual(len(sent), 1)
        self.assertIn("Soft Reload", sent[0])

    def test_handle_ui(self):
        from notifications.telegram_commands import reload_commands

        sent = []
        with patch(
            "notifications.telegram_commands.reload_commands.send_telegram_message",
            side_effect=lambda m: sent.append(m),
        ), patch(
            "notifications.telegram_commands.reload_commands.current_chat_id",
            return_value=42,
        ):
            self.assertTrue(reload_commands.handle("/reload ui"))
        self.assertEqual(len(sent), 1)
        self.assertIn("ui", sent[0])
        self.assertIn("Reload", sent[0])

    def test_handle_unknown_scope(self):
        from notifications.telegram_commands import reload_commands

        sent = []
        with patch(
            "notifications.telegram_commands.reload_commands.send_telegram_message",
            side_effect=lambda m: sent.append(m),
        ):
            self.assertTrue(reload_commands.handle("/reload banana"))
        self.assertIn("Unbekannter", sent[0])

    def test_dispatch_routes_reload(self):
        from notifications.telegram_commands.router import dispatch_command

        with patch(
            "notifications.telegram_commands.reload_commands.send_telegram_message"
        ) as mock_send:
            self.assertTrue(dispatch_command("/reload"))
            mock_send.assert_called()


if __name__ == "__main__":
    unittest.main()
