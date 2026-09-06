"""#305 item 2: HTML-escape free text; unescape-and-resend on entity parse 400."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from telegram_notifier import _send_telegram_direct, send_signal_message


class TestSignalMessageHtmlEscape(unittest.TestCase):
    def test_risk_message_with_lt_and_amp_is_escaped(self):
        coin = {"symbol": "RAVE/USDT", "name": "RaveDAO"}
        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("telegram_notifier.send_telegram_message") as mock_send:
            mock_send.return_value = True
            send_signal_message(
                "SELL_30",
                coin,
                0.65,
                75.0,
                0.60,
                0.8,
                "🔴",
                "Bearish",
                executed=False,
                trade_message="short mcap 12 < min 50 & fee",
            )
        self.assertTrue(mock_send.called)
        text = mock_send.call_args[0][0]
        self.assertIn("<b>Grund:</b>", text)
        self.assertIn("&lt;", text)
        self.assertIn("&amp;", text)
        self.assertNotIn("12 < min", text)
        self.assertNotIn("50 & fee", text)


class TestTelegramParseEntitiesFallback(unittest.TestCase):
    def test_http_400_parse_entities_resends_plain_once(self):
        bad = MagicMock(status_code=400, text="Bad Request: can't parse entities")
        bad.json.return_value = {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: can't parse entities",
        }
        good = MagicMock(status_code=200, text='{"ok":true}')
        good.json.return_value = {"ok": True}

        env = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "111",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch("telegram_notifier.requests.post", side_effect=[bad, good]) as mock_post, \
             patch("telegram_notifier.message_prefix", return_value=""), \
             patch("telegram_notifier._headless_tenant_tag", return_value=""):
            ok = _send_telegram_direct("<b>hi</b> 3 < 5 & x")

        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 2)
        first_payload = mock_post.call_args_list[0].kwargs.get("json")
        if first_payload is None:
            first_payload = mock_post.call_args_list[0][1]["json"]
        self.assertEqual(first_payload.get("parse_mode"), "HTML")
        second_payload = mock_post.call_args_list[1].kwargs.get("json")
        if second_payload is None:
            second_payload = mock_post.call_args_list[1][1]["json"]
        self.assertFalse(second_payload.get("parse_mode"))
        self.assertNotIn("parse_mode", second_payload)
        self.assertIn("3 < 5", second_payload["text"])
        self.assertNotIn("<b>", second_payload["text"])


if __name__ == "__main__":
    unittest.main()
