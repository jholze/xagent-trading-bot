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

    def test_try_resolve_slash_clears_stale_context(self):
        ctx.set_context("42", "morning")
        with patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock:
            self.assertTrue(ctx.try_resolve("42", "/positions"))
            mock.assert_called_once_with("/positions")
        self.assertIsNone(ctx.get_context("42"))


if __name__ == "__main__":
    unittest.main()