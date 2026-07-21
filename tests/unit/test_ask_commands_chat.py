""" /ask must route replies to the asking chat (multi-tenant). """

from __future__ import annotations

import unittest
from unittest.mock import patch

from notifications.telegram_commands import ask_commands


class TestAskCommandsChatRouting(unittest.TestCase):
    def test_uses_current_chat_id_not_only_env(self):
        sent = []

        def _send(msg, chat_id=None, **kwargs):
            sent.append({"msg": msg, "chat_id": chat_id})
            return True

        with patch.object(ask_commands, "current_chat_id", return_value="6512212782"), \
             patch.object(ask_commands, "enqueue_question", return_value=("abc123", "")) as enq, \
             patch.object(ask_commands, "send_telegram_message", side_effect=_send), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "111OPERATOR"}):
            ok = ask_commands.handle("/ask warum ONDO?")
        self.assertTrue(ok)
        enq.assert_called_once_with("6512212782", "warum ONDO?")
        self.assertTrue(sent)
        self.assertEqual(sent[0]["chat_id"], "6512212782")
        self.assertIn("abc123", sent[0]["msg"])


if __name__ == "__main__":
    unittest.main()
