import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from notifications.telegram_commands import command_context as ctx


class TestCommandContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ctx.json"
        ctx._CONTEXT_FILE = self.path

    def tearDown(self):
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def test_set_and_get_context(self):
        ctx.set_context("123", "buy", default_usdt=200)
        entry = ctx.get_context("123")
        self.assertEqual(entry["command"], "buy")
        self.assertEqual(entry["meta"]["default_usdt"], 200)

    def test_build_buy_command(self):
        built = ctx._build_command("buy", "1 25", {"default_usdt": 200})
        self.assertEqual(built, "/buy 1 25")

    def test_build_buy_default_usdt(self):
        built = ctx._build_command("buy", "2", {"default_usdt": 150})
        self.assertEqual(built, "/buy 2 150")

    def test_expired_context_cleared(self):
        ctx.set_context("99", "add")
        store = ctx._load_store()
        store["contexts"]["99"]["updated_at"] = (datetime.now() - timedelta(minutes=20)).isoformat()
        ctx._save_store(store)
        self.assertIsNone(ctx.get_context("99"))

    def test_try_resolve_dispatches(self):
        ctx.set_context("42", "add")
        with patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock:
            self.assertTrue(ctx.try_resolve("42", "RAVE"))
            mock.assert_called_once_with("/add RAVE")
        self.assertIsNone(ctx.get_context("42"))

    def test_build_sell_command_with_symbol(self):
        built = ctx._build_command("sell", "RAVE 30", {})
        self.assertEqual(built, "/sell RAVE 30")

    def test_try_resolve_slash_clears_stale_context(self):
        ctx.set_context("42", "morning")
        with patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock:
            self.assertTrue(ctx.try_resolve("42", "/positions"))
            mock.assert_called_once_with("/positions")
        self.assertIsNone(ctx.get_context("42"))

    def test_build_sell_command_position_only_no_default(self):
        built = ctx._build_command("sell", "RAVE", {})
        self.assertEqual(built, "/sell RAVE")

    def test_build_sell_command_index_only_no_default(self):
        built = ctx._build_command("sell", "3", {})
        self.assertEqual(built, "/sell 3")

    def test_parse_sell_percent_token_rejects_missing_and_invalid(self):
        self.assertIsNone(ctx.parse_sell_percent_token(""))
        self.assertIsNone(ctx.parse_sell_percent_token("abc"))
        self.assertIsNone(ctx.parse_sell_percent_token("0"))
        self.assertIsNone(ctx.parse_sell_percent_token("150"))
        self.assertEqual(ctx.parse_sell_percent_token("25"), "25")
        self.assertEqual(ctx.parse_sell_percent_token("25%"), "25")
        self.assertEqual(ctx.parse_sell_percent_token("100"), "100")

    def test_build_sell_command_awaiting_pct(self):
        built = ctx._build_command(
            "sell", "25", {"state": "sell_awaiting_pct", "position": "RAVE"}
        )
        self.assertEqual(built, "/sell RAVE 25")

    def test_build_sell_command_awaiting_pct_invalid_no_default(self):
        built = ctx._build_command(
            "sell", "abc", {"state": "sell_awaiting_pct", "position": "RAVE"}
        )
        self.assertIsNone(built)

    def test_try_resolve_sell_position_only_no_percent_default(self):
        ctx.set_context("99", "sell", state="sell_awaiting_position")
        with patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock:
            self.assertTrue(ctx.try_resolve("99", "RAVE"))
            mock.assert_called_once_with("/sell RAVE")

    def test_try_resolve_sell_pct_invalid_keeps_context_and_reasks(self):
        ctx.set_context("99", "sell", state="sell_awaiting_pct", position="RAVE", label="RAVE")
        with patch(
            "notifications.telegram_commands.trading_commands.prompt_sell_percentage"
        ) as mock_prompt, patch(
            "notifications.telegram_commands.router.dispatch_command"
        ) as mock_dispatch:
            self.assertTrue(ctx.try_resolve("99", "abc"))
            mock_prompt.assert_called_once_with("RAVE", invalid=True)
            mock_dispatch.assert_not_called()
        entry = ctx.get_context("99")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_pct")
        self.assertEqual(entry["meta"]["position"], "RAVE")


if __name__ == "__main__":
    unittest.main()