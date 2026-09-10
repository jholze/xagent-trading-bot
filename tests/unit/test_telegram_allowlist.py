import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from webhooks.auth import telegram_chat_id_from_update, telegram_sender_allowed


class TestTelegramAllowlist(unittest.TestCase):
    def test_rejects_when_no_owner_configured(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_CHAT_IDS")}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(telegram_sender_allowed("123"))

    def test_accepts_owner_and_extra(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_CHAT_ID": "111", "TELEGRAM_ALLOWED_CHAT_IDS": "222, 333"},
            clear=False,
        ):
            self.assertTrue(telegram_sender_allowed(111))
            self.assertTrue(telegram_sender_allowed("222"))
            self.assertFalse(telegram_sender_allowed("999"))

    def test_extracts_chat_from_message_and_callback(self):
        self.assertEqual(
            telegram_chat_id_from_update({"message": {"chat": {"id": 42}}}),
            "42",
        )
        self.assertEqual(
            telegram_chat_id_from_update(
                {"callback_query": {"message": {"chat": {"id": 7}}, "from": {"id": 8}}}
            ),
            "7",
        )

    def test_webhook_ignores_stranger(self):
        try:
            from aria_bot import app
        except ImportError:
            self.skipTest("flask not installed in this environment")
        client = app.test_client()
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111"}, clear=False), patch(
            "aria_bot.handle_telegram_command"
        ) as mock_cmd:
            resp = client.post(
                "/",
                json={"message": {"text": "/mode live", "chat": {"id": 999}}},
            )
        self.assertEqual(resp.status_code, 200)
        mock_cmd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
